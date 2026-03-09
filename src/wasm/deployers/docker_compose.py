# Copyright (c) 2024-2025 Yago López Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
Docker Compose deployer for WASM.

Handles deployment of applications defined by Docker Compose files,
including multi-container setups with path-based Nginx routing,
environment variable management, and systemd integration.
"""

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Any

import yaml

from wasm.core.config import Config, DEFAULT_APPS_DIR
from wasm.core.exceptions import (
    DeploymentError,
    DockerError,
    NginxError,
    ServiceError,
    WASMError,
)
from wasm.core.logger import Logger
from wasm.core.store import get_store, App, AppType, AppStatus, WebServer
from wasm.core.utils import (
    domain_to_app_name,
    run_command,
    run_command_sudo,
    remove_directory,
)
from wasm.managers.cert_manager import CertManager
from wasm.managers.nginx_manager import NginxManager
from wasm.managers.service_manager import ServiceManager
from wasm.managers.source_manager import SourceManager


# Compose file priority order
COMPOSE_FILE_PRIORITY = [
    "docker-compose.prod.yml",
    "docker-compose.prod.yaml",
    "docker-compose.yml",
    "docker-compose.yaml",
    "compose.yml",
    "compose.yaml",
]


@dataclass
class DockerComposeService:
    """Represents a service from a Docker Compose file."""
    name: str = ""
    image: Optional[str] = None
    build: Optional[str] = None
    ports: List[str] = field(default_factory=list)
    volumes: List[str] = field(default_factory=list)
    depends_on: List[str] = field(default_factory=list)
    environment: Dict[str, str] = field(default_factory=dict)
    healthcheck: Optional[Dict] = None
    is_web: bool = False


class DockerComposeDeployer:
    """
    Deployer for Docker Compose applications.

    Standalone deployer (does not inherit BaseDeployer) that manages
    the full lifecycle of Docker Compose applications including
    deployment, updates, and removal.
    """

    APP_TYPE = "docker-compose"
    DISPLAY_NAME = "Docker Compose"
    DETECTION_FILES = [
        "docker-compose.prod.yml", "docker-compose.prod.yaml",
        "docker-compose.yml", "docker-compose.yaml",
        "compose.yml", "compose.yaml",
    ]

    def __init__(self, verbose: bool = False):
        self.verbose = verbose
        self.logger = Logger(verbose=verbose)
        self.config = Config()
        self.store = get_store()

        # Deployment state
        self.domain = ""
        self.app_name = ""
        self.app_path = Path()
        self.source = ""
        self.branch = None
        self.webserver = "nginx"
        self.ssl = True
        self.env_vars = {}
        self.compose_file = None
        self.compose_profiles = []
        self.port = None

        # Parsed state
        self.services: List[DockerComposeService] = []
        self.compose_path = None

    # Framework config files that indicate docker-compose.yml is likely
    # just for local development (databases, caches, etc.)
    FRAMEWORK_CONFIG_FILES = [
        # Next.js
        "next.config.js", "next.config.mjs", "next.config.ts",
        # Vite
        "vite.config.js", "vite.config.ts", "vite.config.mjs",
        # Angular
        "angular.json",
        # Nuxt
        "nuxt.config.js", "nuxt.config.ts",
        # Svelte
        "svelte.config.js",
        # Astro
        "astro.config.mjs", "astro.config.ts",
        # Remix
        "remix.config.js", "remix.config.ts",
        # Django
        "manage.py",
    ]

    @classmethod
    def detect(cls, path: Path) -> bool:
        """
        Detect if a path contains a Docker Compose project.

        Detection priority:
        - If docker-compose.prod.yml exists -> True (strong signal)
        - If only docker-compose.yml exists AND monorepo signals -> False
        - If only docker-compose.yml exists AND framework config files -> False
        - If only docker-compose.yml exists without other signals -> True

        Args:
            path: Path to check.

        Returns:
            True if Docker Compose project detected.
        """
        has_prod_compose = any(
            (path / f).exists()
            for f in ["docker-compose.prod.yml", "docker-compose.prod.yaml"]
        )

        if has_prod_compose:
            return True

        has_compose = any(
            (path / f).exists()
            for f in COMPOSE_FILE_PRIORITY
        )

        if not has_compose:
            return False

        # Check for monorepo signals
        has_turbo = (path / "turbo.json").exists()
        if has_turbo:
            apps_dir = path / "apps"
            if apps_dir.is_dir():
                app_count = sum(1 for d in apps_dir.iterdir() if d.is_dir())
                if app_count >= 2:
                    return False

        # Check for framework config files - if present, docker-compose.yml
        # is likely just for local development (databases, caches, etc.)
        has_framework = any(
            (path / f).exists() for f in cls.FRAMEWORK_CONFIG_FILES
        )
        if has_framework:
            return False

        return True

    def configure(
        self,
        domain: str,
        source: str,
        webserver: str = "nginx",
        ssl: bool = True,
        branch: Optional[str] = None,
        env_vars: Optional[Dict[str, str]] = None,
        compose_file: Optional[str] = None,
        compose_profiles: Optional[List[str]] = None,
        port: Optional[int] = None,
    ) -> None:
        """
        Configure the deployer.

        Args:
            domain: Target domain name.
            source: Git URL or local path.
            webserver: Web server to use.
            ssl: Whether to enable SSL.
            branch: Git branch.
            env_vars: Environment variables.
            compose_file: Specific compose file to use.
            compose_profiles: Docker Compose profiles to activate.
            port: Override port for Nginx proxy.
        """
        self.domain = domain
        self.source = source
        self.app_name = domain_to_app_name(domain)
        self.app_path = self.config.apps_directory / self.app_name
        self.webserver = webserver
        self.ssl = ssl
        self.branch = branch
        self.env_vars = env_vars or {}
        self.compose_file = compose_file
        self.compose_profiles = compose_profiles or []
        self.port = port

    def deploy(self) -> None:
        """
        Execute the full deployment workflow.

        Steps:
        1. Fetch source code
        2. Discover compose file
        3. Parse compose services
        4. Configure environment
        5. Build Docker images
        6. Create Nginx site
        7. Obtain SSL certificate
        8. Create systemd service
        9. Start and verify

        Raises:
            DeploymentError: If any deployment step fails.
        """
        total_steps = 9

        try:
            self.logger.step(1, total_steps, "Fetching source code")
            self._fetch_source()

            self.logger.step(2, total_steps, "Discovering compose file")
            self._discover_compose_file()

            self.logger.step(3, total_steps, "Parsing compose services")
            self._parse_compose_services()

            self.logger.step(4, total_steps, "Configuring environment")
            self._configure_environment()

            self.logger.step(5, total_steps, "Building Docker images")
            self._build_images()

            self.logger.step(6, total_steps, "Creating site configuration")
            self._create_site()

            self.logger.step(7, total_steps, "Obtaining SSL certificate")
            self._obtain_certificate()

            self.logger.step(8, total_steps, "Creating systemd service")
            self._create_systemd_service()

            self.logger.step(9, total_steps, "Starting and verifying")
            self._start_and_verify()

            # Register in store
            self._register_app()

            self.logger.success(f"Docker Compose application deployed: {self.domain}")
            self.logger.blank()
            self.logger.key_value("Domain", self.domain)
            self.logger.key_value("Path", str(self.app_path))
            self.logger.key_value("Compose File", str(self.compose_path.name))
            self.logger.key_value("Services", str(len(self.services)))
            self.logger.key_value("SSL", "Yes" if self.ssl else "No")

        except Exception as e:
            self.logger.error(f"Deployment failed: {e}")
            self._rollback()
            raise

    def _fetch_source(self) -> None:
        """Fetch source code via git clone or local copy."""
        source_manager = SourceManager(verbose=self.verbose)
        source_manager.fetch(
            source=self.source,
            target=self.app_path,
            branch=self.branch,
        )
        self.logger.substep(f"Source fetched to {self.app_path}")

    def _discover_compose_file(self) -> None:
        """Find the best compose file to use."""
        if self.compose_file:
            path = self.app_path / self.compose_file
            if not path.exists():
                raise DeploymentError(
                    f"Specified compose file not found: {self.compose_file}"
                )
            self.compose_path = path
            self.logger.substep(f"Using specified: {self.compose_file}")
            return

        for filename in COMPOSE_FILE_PRIORITY:
            path = self.app_path / filename
            if path.exists():
                self.compose_path = path
                self.logger.substep(f"Found: {filename}")
                return

        raise DeploymentError(
            "No Docker Compose file found",
            "Expected one of: " + ", ".join(COMPOSE_FILE_PRIORITY),
        )

    def _parse_compose_services(self) -> None:
        """Parse Docker Compose file and extract service definitions."""
        try:
            data = yaml.safe_load(self.compose_path.read_text(encoding="utf-8"))
        except yaml.YAMLError as e:
            raise DeploymentError(f"Invalid compose file: {e}")

        if not data or "services" not in data:
            raise DeploymentError("No services defined in compose file")

        self.services = []
        for svc_name, svc_data in data.get("services", {}).items():
            ports = []
            for p in svc_data.get("ports", []):
                ports.append(str(p))

            volumes = []
            for v in svc_data.get("volumes", []):
                volumes.append(str(v))

            depends = svc_data.get("depends_on", [])
            if isinstance(depends, dict):
                depends = list(depends.keys())

            env = {}
            env_list = svc_data.get("environment", [])
            if isinstance(env_list, list):
                for item in env_list:
                    if "=" in str(item):
                        key, _, val = str(item).partition("=")
                        env[key] = val
            elif isinstance(env_list, dict):
                env = {k: str(v) if v is not None else "" for k, v in env_list.items()}

            service = DockerComposeService(
                name=svc_name,
                image=svc_data.get("image"),
                build=str(svc_data.get("build", "")) if svc_data.get("build") else None,
                ports=ports,
                volumes=volumes,
                depends_on=depends,
                environment=env,
                healthcheck=svc_data.get("healthcheck"),
                is_web=bool(ports),
            )
            self.services.append(service)

        self.logger.substep(f"Found {len(self.services)} services")
        for svc in self.services:
            port_info = f" (ports: {', '.join(svc.ports)})" if svc.ports else ""
            self.logger.substep(f"  - {svc.name}{port_info}")

    def _configure_environment(self) -> None:
        """Configure environment variables using EnvManager."""
        from wasm.deployers.helpers.env_manager import EnvManager

        manager = EnvManager(verbose=self.verbose)

        # Discover variables from .env.example files
        variables = manager.discover(self.app_path)

        if variables:
            # Get existing values
            existing = manager.get_current_values(self.app_path)
            existing.update(self.env_vars)

            # Use non-interactive mode (secrets auto-generated, defaults used)
            values = manager.prompt_non_interactive(variables)
            values.update(existing)

            # Write .env file
            manager.write_env_files(self.app_path, values)
            self.logger.substep(f"Configured {len(values)} environment variables")
        elif self.env_vars:
            # Write provided env vars
            manager.write_env_files(self.app_path, self.env_vars)
            self.logger.substep(f"Wrote {len(self.env_vars)} environment variables")
        else:
            self.logger.substep("No environment configuration needed")

    def _build_images(self, no_cache: bool = False) -> None:
        """Build Docker images defined in the compose file."""
        cmd = ["docker", "compose"]
        if self.compose_path:
            cmd.extend(["-f", str(self.compose_path)])

        for profile in self.compose_profiles:
            cmd.extend(["--profile", profile])

        cmd.append("build")
        if no_cache:
            cmd.append("--no-cache")

        result = run_command(cmd, cwd=self.app_path, timeout=600000)
        if not result.success:
            raise DockerError(
                "Failed to build Docker images",
                result.stderr,
            )
        self.logger.substep("Images built successfully")

    def _get_primary_port(self) -> int:
        """Determine the primary port for Nginx proxy."""
        if self.port:
            return self.port

        # Find first web-facing service with ports
        for svc in self.services:
            for port_str in svc.ports:
                parts = port_str.split(":")
                if len(parts) >= 2:
                    try:
                        return int(parts[0])
                    except ValueError:
                        continue
                elif len(parts) == 1:
                    try:
                        return int(parts[0])
                    except ValueError:
                        continue

        return 3000  # Default fallback

    def _create_site(self) -> None:
        """Create Nginx site configuration."""
        nginx = NginxManager(verbose=self.verbose)
        primary_port = self._get_primary_port()

        # Check for advanced nginx config
        from wasm.deployers.helpers.nginx_config import NginxConfigBuilder

        builder = NginxConfigBuilder(verbose=self.verbose)
        config_path = builder.detect(self.app_path)

        if config_path:
            # Use advanced config from wasm.nginx.yaml
            config = builder.parse(config_path)
            errors = builder.validate(config)
            if errors:
                self.logger.warning("Nginx config validation warnings:")
                for err in errors:
                    self.logger.warning(f"  - {err}")

            nginx.create_advanced_site(
                domain=self.domain,
                config=config,
                ssl=False,  # SSL added after certificate
                app_path=str(self.app_path),
            )
        elif len([s for s in self.services if s.is_web]) > 1:
            # Auto-derive from compose ports
            config = builder.from_docker_compose(self.compose_path, self.domain)
            nginx.create_advanced_site(
                domain=self.domain,
                config=config,
                ssl=False,
                app_path=str(self.app_path),
            )
        else:
            # Simple proxy
            nginx.create_site(
                domain=self.domain,
                template="proxy",
                context={
                    "domain": self.domain,
                    "port": primary_port,
                    "app_path": str(self.app_path),
                    "ssl": False,
                },
            )

        nginx.enable_site(self.domain)
        nginx.reload()
        self.logger.substep(f"Nginx configured (port {primary_port})")

    def _obtain_certificate(self) -> None:
        """Obtain SSL certificate via Let's Encrypt."""
        if not self.ssl:
            self.logger.substep("SSL disabled, skipping")
            return

        try:
            cert_manager = CertManager(verbose=self.verbose)
            cert_manager.obtain(self.domain)

            # Update nginx with SSL
            nginx = NginxManager(verbose=self.verbose)
            nginx.delete_site(self.domain)

            primary_port = self._get_primary_port()

            from wasm.deployers.helpers.nginx_config import NginxConfigBuilder
            builder = NginxConfigBuilder(verbose=self.verbose)
            config_path = builder.detect(self.app_path)

            if config_path:
                config = builder.parse(config_path)
                nginx.create_advanced_site(
                    domain=self.domain,
                    config=config,
                    ssl=True,
                    app_path=str(self.app_path),
                )
            elif len([s for s in self.services if s.is_web]) > 1:
                config = builder.from_docker_compose(self.compose_path, self.domain)
                nginx.create_advanced_site(
                    domain=self.domain,
                    config=config,
                    ssl=True,
                    app_path=str(self.app_path),
                )
            else:
                nginx.create_site(
                    domain=self.domain,
                    template="proxy",
                    context={
                        "domain": self.domain,
                        "port": primary_port,
                        "app_path": str(self.app_path),
                        "ssl": True,
                    },
                )

            nginx.enable_site(self.domain)
            nginx.reload()
            self.logger.substep("SSL certificate obtained")

        except Exception as e:
            self.logger.warning(f"SSL certificate failed: {e}")
            self.logger.warning("Continuing without SSL")
            self.ssl = False

    def _create_systemd_service(self) -> None:
        """Create systemd service for Docker Compose management."""
        service_manager = ServiceManager(verbose=self.verbose)

        # Build environment dict for the service
        service_env = {}
        if self.compose_profiles:
            service_env["COMPOSE_PROFILES"] = ",".join(self.compose_profiles)

        compose_file_rel = None
        if self.compose_path:
            compose_file_rel = str(self.compose_path.relative_to(self.app_path))

        service_manager.create_service(
            name=self.app_name,
            template="docker-compose",
            context={
                "name": self.app_name,
                "description": f"Docker Compose app: {self.domain}",
                "working_directory": str(self.app_path),
                "environment": service_env,
                "compose_file": compose_file_rel,
            },
        )
        self.logger.substep(f"Created systemd service: {self.app_name}")

    def _start_and_verify(self) -> None:
        """Start containers and verify they're healthy."""
        service_manager = ServiceManager(verbose=self.verbose)
        service_manager.start(self.app_name)

        # Health check
        healthy = self._health_check()
        if healthy:
            self.logger.substep("All services running and healthy")
        else:
            self.logger.warning("Some services may not be fully healthy yet")
            self.logger.warning(f"Check with: docker compose -f {self.compose_path} ps")

    def _health_check(self, retries: int = 10, delay: int = 3) -> bool:
        """
        Check if all Docker Compose services are running.

        Args:
            retries: Number of retry attempts.
            delay: Seconds between retries.

        Returns:
            True if all services are running.
        """
        cmd = ["docker", "compose"]
        if self.compose_path:
            cmd.extend(["-f", str(self.compose_path)])
        cmd.extend(["ps", "--format", "json"])

        for attempt in range(retries):
            result = run_command(cmd, cwd=self.app_path)
            if not result.success:
                time.sleep(delay)
                continue

            try:
                # Docker compose ps --format json may output one JSON per line
                all_running = True
                for line in result.stdout.strip().splitlines():
                    if not line.strip():
                        continue
                    svc_info = json.loads(line)
                    state = svc_info.get("State", "").lower()
                    health = svc_info.get("Health", "").lower()

                    if state != "running":
                        all_running = False
                        break

                    if health and health not in ("healthy", ""):
                        all_running = False
                        break

                if all_running:
                    return True

            except (json.JSONDecodeError, KeyError):
                pass

            if attempt < retries - 1:
                time.sleep(delay)

        return False

    def _register_app(self) -> None:
        """Register the application in the WASM store."""
        primary_port = self._get_primary_port()

        app = App(
            domain=self.domain,
            app_type=AppType.DOCKER_COMPOSE.value,
            source=self.source,
            branch=self.branch,
            port=primary_port,
            app_path=str(self.app_path),
            webserver=self.webserver,
            ssl_enabled=self.ssl,
            status=AppStatus.RUNNING.value,
            env_vars=self.env_vars,
        )

        try:
            self.store.create_app(app)
        except Exception as e:
            self.logger.warning(f"Could not register app in store: {e}")

    def _rollback(self) -> None:
        """Clean up on deployment failure."""
        self.logger.info("Rolling back deployment...")

        # Stop containers
        if self.compose_path and self.compose_path.exists():
            cmd = ["docker", "compose"]
            cmd.extend(["-f", str(self.compose_path)])
            cmd.extend(["down", "--remove-orphans"])
            run_command(cmd, cwd=self.app_path)

        # Remove systemd service
        try:
            service_manager = ServiceManager(verbose=self.verbose)
            service_manager.delete_service(self.app_name)
        except Exception:
            pass

        # Remove nginx config
        try:
            nginx = NginxManager(verbose=self.verbose)
            if nginx.site_exists(self.domain):
                nginx.delete_site(self.domain)
                nginx.reload()
        except Exception:
            pass

        # Remove app directory
        if self.app_path.exists():
            remove_directory(self.app_path, sudo=True)

        # Clean store
        try:
            self.store.delete_app(self.domain)
        except Exception:
            pass

        self.logger.info("Rollback complete")

    # =========================================================================
    # Lifecycle methods
    # =========================================================================

    def start(self) -> None:
        """Start the Docker Compose application."""
        service_manager = ServiceManager(verbose=self.verbose)
        service_manager.start(self.app_name)
        self.store.update_app_status(self.domain, AppStatus.RUNNING.value)

    def stop(self) -> None:
        """Stop the Docker Compose application."""
        service_manager = ServiceManager(verbose=self.verbose)
        service_manager.stop(self.app_name)
        self.store.update_app_status(self.domain, AppStatus.STOPPED.value)

    def restart(self) -> None:
        """Restart (rebuild and recreate) the Docker Compose application."""
        service_manager = ServiceManager(verbose=self.verbose)
        # reload triggers ExecReload which does `docker compose up -d --build`
        result = run_command_sudo(
            ["systemctl", "reload", f"{self.app_name}.service"]
        )
        if not result.success:
            # Fallback to restart
            service_manager.restart(self.app_name)

    def logs(self, service: Optional[str] = None, lines: int = 50) -> str:
        """
        Get Docker Compose logs.

        Args:
            service: Specific service name (None for all).
            lines: Number of lines to show.

        Returns:
            Log output string.
        """
        cmd = ["docker", "compose"]
        if self.compose_path:
            cmd.extend(["-f", str(self.compose_path)])
        cmd.extend(["logs", "--tail", str(lines)])

        if service:
            cmd.append(service)

        result = run_command(cmd, cwd=self.app_path)
        return result.stdout if result.success else result.stderr

    def status(self) -> Dict[str, Any]:
        """
        Get Docker Compose service status.

        Returns:
            Dictionary with service status information.
        """
        cmd = ["docker", "compose"]
        if self.compose_path:
            cmd.extend(["-f", str(self.compose_path)])
        cmd.extend(["ps", "--format", "json"])

        result = run_command(cmd, cwd=self.app_path)
        services = []

        if result.success:
            for line in result.stdout.strip().splitlines():
                if not line.strip():
                    continue
                try:
                    services.append(json.loads(line))
                except json.JSONDecodeError:
                    pass

        return {
            "domain": self.domain,
            "services": services,
            "total": len(services),
            "running": sum(1 for s in services if s.get("State") == "running"),
        }

    def update(self) -> None:
        """Update the Docker Compose application (git pull + rebuild)."""
        # Pull latest code
        source_manager = SourceManager(verbose=self.verbose)
        source_manager.pull(self.app_path, branch=self.branch)

        # Rebuild and restart
        self._build_images()

        cmd = ["docker", "compose"]
        if self.compose_path:
            cmd.extend(["-f", str(self.compose_path)])
        cmd.extend(["up", "-d", "--remove-orphans"])

        result = run_command(cmd, cwd=self.app_path, timeout=300000)
        if not result.success:
            raise DockerError("Failed to update containers", result.stderr)

        self.store.update_app_status(self.domain, AppStatus.RUNNING.value)

    def delete(self, remove_volumes: bool = False) -> None:
        """
        Delete the Docker Compose application.

        Args:
            remove_volumes: Also remove Docker volumes.
        """
        # Stop and remove containers
        cmd = ["docker", "compose"]
        if self.compose_path and self.compose_path.exists():
            cmd.extend(["-f", str(self.compose_path)])
        cmd.append("down")
        if remove_volumes:
            cmd.append("--volumes")
        cmd.append("--remove-orphans")

        run_command(cmd, cwd=self.app_path)

        # Remove systemd service
        try:
            service_manager = ServiceManager(verbose=self.verbose)
            service_manager.delete_service(self.app_name)
        except Exception:
            pass

        # Remove nginx config
        try:
            nginx = NginxManager(verbose=self.verbose)
            if nginx.site_exists(self.domain):
                nginx.delete_site(self.domain)
                nginx.reload()
        except Exception:
            pass

        # Clean store
        try:
            self.store.delete_site(self.domain)
            self.store.delete_service(self.app_name)
            self.store.delete_app(self.domain)
        except Exception:
            pass


# Register in DeployerRegistry
from wasm.deployers.registry import DeployerRegistry  # noqa: E402

# DockerComposeDeployer doesn't inherit BaseDeployer, so we register
# it directly in the registry dict for detection purposes
DeployerRegistry._deployers[DockerComposeDeployer.APP_TYPE] = DockerComposeDeployer
