# Copyright (c) 2024-2025 Yago Lopez Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
Monorepo deployer for WASM.

Handles deployment of Turborepo/pnpm workspace monorepos with multiple
applications, shared databases, and unified build processes.
"""

import json
import os
import secrets
import string
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Set

from wasm.core.config import Config
from wasm.core.exceptions import (
    DeploymentError,
    BuildError,
    OutOfMemoryError,
    ServiceError,
)
from wasm.core.logger import Logger, Icons
from wasm.core.store import (
    get_store,
    App,
    Site,
    Service,
    Database,
    MonorepoWorkspace,
    AppType,
    AppStatus,
    WebServer,
    DatabaseEngine,
)
from wasm.core.utils import run_command, domain_to_app_name, command_exists
from wasm.managers.nginx_manager import NginxManager
from wasm.managers.apache_manager import ApacheManager
from wasm.managers.service_manager import ServiceManager
from wasm.managers.cert_manager import CertManager
from wasm.managers.source_manager import SourceManager
from wasm.deployers.helpers import (
    PackageManagerHelper,
    PathResolver,
    PrismaHelper,
    WorkspaceHelper,
    TurboHelper,
)


@dataclass
class DatabaseConfig:
    """Configuration for a database to provision."""
    engine: str
    name: str = ""
    user: str = ""
    password: str = ""
    host: str = "localhost"
    port: int = 0
    db_number: int = 0  # For Redis


class MonorepoDeployer:
    """
    Deployer for Turborepo/pnpm workspace monorepos.

    Handles deployment of multi-app monorepos with:
    - Unified build via Turborepo
    - Multiple systemd services (one per workspace app)
    - Multiple nginx site configurations (subdomain-based routing)
    - Shared database provisioning (PostgreSQL, Redis)
    - Prisma migrations from shared packages
    - Atomic rollback on failure
    """

    APP_TYPE = "monorepo"
    DISPLAY_NAME = "Monorepo (Turborepo/pnpm)"

    # Files used to detect this app type
    DETECTION_FILES = [
        "turbo.json",
        "pnpm-workspace.yaml",
    ]

    # Default ports
    DEFAULT_BASE_PORT = 3000

    def __init__(self, verbose: bool = False):
        """
        Initialize the monorepo deployer.

        Args:
            verbose: Enable verbose logging.
        """
        self.verbose = verbose
        self.config = Config()
        self.logger = Logger(verbose=verbose)
        self.store = get_store()

        # Managers
        self.source_manager = SourceManager(verbose=verbose)
        self.service_manager = ServiceManager(verbose=verbose)
        self.cert_manager = CertManager(verbose=verbose)

        # Helpers
        self._pm_helper = PackageManagerHelper(logger=self.logger)
        self._path_resolver = PathResolver(logger=self.logger)
        self._workspace_helper = WorkspaceHelper(logger=self.logger)
        self._turbo_helper = TurboHelper(logger=self.logger)
        self._prisma_helper: Optional[PrismaHelper] = None

        # Deployment configuration
        self.domain: Optional[str] = None
        self.source: Optional[str] = None
        self.app_path: Optional[Path] = None
        self.app_name: Optional[str] = None
        self.webserver: str = "nginx"
        self.ssl: bool = True
        self.branch: Optional[str] = None
        self.env_vars: Dict[str, str] = {}

        # Workspace configuration
        self.workspaces: List[MonorepoWorkspace] = []
        self.subdomain_overrides: Dict[str, str] = {}
        self.workspace_filter: Optional[List[str]] = None

        # Database configuration
        self.databases: Dict[str, DatabaseConfig] = {}
        self.skip_database: bool = False

        # Package manager
        self.package_manager: str = "pnpm"

        # Rollback tracking
        self._created_services: List[str] = []
        self._created_sites: List[str] = []
        self._is_new_deployment: bool = True

    def configure(
        self,
        domain: str,
        source: str,
        webserver: str = "nginx",
        ssl: bool = True,
        branch: Optional[str] = None,
        env_vars: Optional[Dict[str, str]] = None,
        app_path: Optional[Path] = None,
        subdomain_overrides: Optional[Dict[str, str]] = None,
        workspace_filter: Optional[List[str]] = None,
        skip_database: bool = False,
    ) -> None:
        """
        Configure the deployer.

        Args:
            domain: Target domain (e.g., example.com).
            source: Source URL or path.
            webserver: Web server to use (nginx/apache).
            ssl: Enable SSL.
            branch: Git branch.
            env_vars: Global environment variables.
            app_path: Custom application path.
            subdomain_overrides: Dict mapping app names to subdomains.
            workspace_filter: List of workspace names to deploy (None = all).
            skip_database: Skip database provisioning.
        """
        self.domain = domain
        self.source = source
        self.webserver = webserver
        self.ssl = ssl
        self.branch = branch
        self.env_vars = env_vars or {}
        self.subdomain_overrides = subdomain_overrides or {}
        self.workspace_filter = workspace_filter
        self.skip_database = skip_database

        # Set app name and path
        self.app_name = domain_to_app_name(domain)
        self.app_path = app_path or (self.config.apps_directory / self.app_name)

    def detect(self, path: Path) -> bool:
        """
        Detect if path contains a Turborepo/pnpm monorepo.

        Requires turbo.json AND workspace configuration AND at least 2
        deployable applications in apps/ to distinguish from single apps
        that use Turborepo for build caching.

        Args:
            path: Path to check.

        Returns:
            True if this deployer can handle the project.
        """
        # Must have turbo.json (primary monorepo build tool)
        if not (path / "turbo.json").exists():
            return False

        # Must have workspace configuration
        has_workspace_config = (path / "pnpm-workspace.yaml").exists()
        if not has_workspace_config:
            package_json = path / "package.json"
            if package_json.exists():
                try:
                    with open(package_json) as f:
                        pkg = json.load(f)
                        has_workspace_config = "workspaces" in pkg
                except (json.JSONDecodeError, OSError):
                    pass

        if not has_workspace_config:
            return False

        # Must have multiple deployable apps in apps/ directory
        apps_dir = path / "apps"
        if not apps_dir.is_dir():
            return False

        app_count = sum(
            1 for d in apps_dir.iterdir()
            if d.is_dir() and (d / "package.json").exists()
        )

        return app_count >= 2

    def _run(
        self,
        command: List[str],
        cwd: Optional[Path] = None,
        env: Optional[Dict] = None,
        timeout: Optional[int] = None,
    ):
        """Run a command and return result."""
        self.logger.debug(f"Running: {' '.join(command)}")

        # Merge environment variables
        run_env = self.env_vars.copy()
        if env:
            run_env.update(env)

        return run_command(
            command,
            cwd=cwd or self.app_path,
            env=run_env if run_env else None,
            timeout=timeout,
        )

    def _generate_password(self, length: int = 32) -> str:
        """Generate a secure random password."""
        alphabet = string.ascii_letters + string.digits
        return ''.join(secrets.choice(alphabet) for _ in range(length))

    def deploy(self) -> bool:
        """
        Deploy the monorepo with all workspaces.

        Returns:
            True if deployment was successful.
        """
        from wasm.core.exceptions import CertificateError

        total_steps = 11
        ssl_obtained = False

        # Track if this is a new deployment (for rollback)
        self._is_new_deployment = not self.store.get_app(self.domain)

        # Pre-flight checks
        self.logger.debug("Running pre-flight validation...")
        self._pre_flight_check()

        # Register app in store
        app = self._register_app_in_store(AppStatus.DEPLOYING.value)

        try:
            # Step 1: Fetch source
            self.logger.step(1, total_steps, "Fetching source code", Icons.DOWNLOAD)
            self._fetch_source()

            # Step 2: Discover workspaces
            self.logger.step(2, total_steps, "Discovering workspaces", Icons.SEARCH)
            self._discover_workspaces()

            # Step 3: Provision databases
            self.logger.step(3, total_steps, "Provisioning databases", Icons.DATABASE)
            if self.skip_database:
                self.logger.substep("Database provisioning skipped")
            else:
                self._provision_databases()

            # Step 4: Configure environment
            self.logger.step(4, total_steps, "Configuring environment", Icons.GEAR)
            self._configure_environment()

            # Step 5: Install dependencies
            self.logger.step(5, total_steps, "Installing dependencies", Icons.PACKAGE)
            self._install_dependencies()

            # Set permissions for service user
            self._set_permissions()

            # Step 6: Prisma migrations
            self.logger.step(6, total_steps, "Running database migrations", Icons.DATABASE)
            self._run_prisma_migrations()

            # Step 7: Build
            self.logger.step(7, total_steps, "Building applications", Icons.BUILD)
            self._build_all()

            # Step 8: Create sites (without SSL initially)
            self.logger.step(8, total_steps, "Creating site configurations", Icons.GLOBE)
            self._create_sites(with_ssl=False)

            # Step 9: SSL certificate
            if self.ssl:
                self.logger.step(9, total_steps, "Obtaining SSL certificate", Icons.LOCK)
                try:
                    self._obtain_certificate()
                    ssl_obtained = True
                    self.logger.substep("Updating site configurations with SSL")
                    self._create_sites(with_ssl=True)
                except CertificateError as e:
                    self.logger.warning(f"SSL certificate failed: {e.message}")
                    self.logger.warning("Continuing deployment without SSL...")
                except Exception as e:
                    self.logger.warning(f"SSL certificate failed: {e}")
                    self.logger.warning("Continuing deployment without SSL...")
            else:
                self.logger.step(9, total_steps, "Skipping SSL certificate", Icons.LOCK)

            # Step 10: Create services
            self.logger.step(10, total_steps, "Creating systemd services", Icons.GEAR)
            self._create_services()

            # Step 11: Start and verify
            self.logger.step(11, total_steps, "Starting applications", Icons.ROCKET)
            self._start_and_verify()

            # Update app status
            app.status = AppStatus.RUNNING.value
            app.ssl_enabled = ssl_obtained
            app.deployed_at = datetime.now().isoformat()
            self.store.update_app(app)

            # Show summary
            self._show_deployment_summary(ssl_obtained)

            return True

        except Exception as e:
            # Update app status to failed
            app.status = AppStatus.FAILED.value
            self.store.update_app(app)
            self.logger.error(f"Deployment failed: {e}")

            # Rollback partial deployment for new apps
            if self._is_new_deployment:
                self.logger.warning("Rolling back partial deployment...")
                try:
                    self._rollback()
                    self.logger.info("Rollback completed successfully")
                except Exception as rollback_error:
                    self.logger.debug(f"Rollback error: {rollback_error}")
                    self.logger.warning("Rollback had some errors. Manual cleanup may be needed.")

            raise

    def _pre_flight_check(self) -> None:
        """Perform pre-deployment validation."""
        issues = []

        # Check pnpm is installed
        if not command_exists("pnpm"):
            issues.append(
                "pnpm is not installed. Install it with: npm install -g pnpm"
            )

        # Check git for git sources
        if self.source and (
            self.source.startswith("git@") or
            self.source.startswith("https://") or
            self.source.endswith(".git")
        ):
            if not command_exists("git"):
                issues.append("git is not installed")
            else:
                result = run_command(
                    ["git", "ls-remote", "--exit-code", self.source],
                    timeout=30
                )
                if not result.success:
                    issues.append(f"Repository not accessible: {self.source}")

        # Check disk space
        import shutil
        apps_dir = self.config.apps_directory
        if apps_dir.exists():
            usage = shutil.disk_usage(apps_dir)
            free_gb = usage.free / (1024 ** 3)
            if free_gb < 2:
                issues.append(
                    f"Low disk space: {free_gb:.1f}GB free (recommend 2GB+)"
                )

        if issues:
            raise DeploymentError(
                "Pre-flight checks failed",
                details="\n".join(f"  - {issue}" for issue in issues)
            )

    def _fetch_source(self) -> None:
        """Fetch source code from repository or local path."""
        self.source_manager.fetch(
            source=self.source,
            destination=self.app_path,
            branch=self.branch,
            force=not self._is_new_deployment,
        )

    def _discover_workspaces(self) -> None:
        """Discover and analyze workspace apps."""
        self.workspaces = self._workspace_helper.analyze_all_workspaces(
            self.app_path,
            subdomain_overrides=self.subdomain_overrides,
        )

        # Filter workspaces if specified
        if self.workspace_filter:
            self.workspaces = [
                ws for ws in self.workspaces
                if ws.name in self.workspace_filter
            ]

        if not self.workspaces:
            raise DeploymentError(
                "No deployable workspaces found",
                details="Check that apps/ directory contains valid applications"
            )

        for ws in self.workspaces:
            self.logger.substep(
                f"{ws.name} ({ws.app_type}) -> {ws.subdomain}.{self.domain}:{ws.port}"
            )

    def _detect_database_requirements(self) -> Dict[str, DatabaseConfig]:
        """Detect required databases from project configuration."""
        databases = {}

        # Check docker-compose.yml
        compose_file = self.app_path / "docker-compose.yml"
        if compose_file.exists():
            try:
                import yaml
                with open(compose_file) as f:
                    compose = yaml.safe_load(f)

                services = compose.get("services", {})
                for svc_name, svc_config in services.items():
                    image = svc_config.get("image", "")

                    if "postgres" in image.lower():
                        # Extract default values from environment
                        env = svc_config.get("environment", {})
                        if isinstance(env, list):
                            env = dict(e.split("=", 1) for e in env if "=" in e)

                        databases["postgresql"] = DatabaseConfig(
                            engine="postgresql",
                            name=env.get("POSTGRES_DB", f"{self.app_name}_db"),
                            user=env.get("POSTGRES_USER", f"{self.app_name}_user"),
                            password=self._generate_password(),
                            port=5432,
                        )

                    elif "redis" in image.lower():
                        databases["redis"] = DatabaseConfig(
                            engine="redis",
                            name="",
                            port=6379,
                            db_number=0,
                        )

            except ImportError:
                self.logger.debug("PyYAML not available, skipping docker-compose parsing")
            except Exception as e:
                self.logger.debug(f"Error parsing docker-compose.yml: {e}")

        # Check for Prisma (indicates PostgreSQL needed)
        prisma_schema = self.app_path / "packages" / "database" / "prisma" / "schema.prisma"
        if prisma_schema.exists() and "postgresql" not in databases:
            try:
                content = prisma_schema.read_text()
                if 'provider = "postgresql"' in content:
                    databases["postgresql"] = DatabaseConfig(
                        engine="postgresql",
                        name=f"{self.app_name}_db",
                        user=f"{self.app_name}_user",
                        password=self._generate_password(),
                        port=5432,
                    )
            except Exception as e:
                self.logger.debug(f"Error reading Prisma schema: {e}")

        return databases

    def _provision_databases(self) -> None:
        """Provision required databases."""
        self.databases = self._detect_database_requirements()

        if not self.databases:
            self.logger.substep("No databases detected")
            return

        for db_type, db_config in self.databases.items():
            try:
                if db_type == "postgresql":
                    self._provision_postgresql(db_config)
                elif db_type == "redis":
                    self._provision_redis(db_config)
            except Exception as e:
                self.logger.warning(f"Database provisioning failed for {db_type}: {e}")
                self.logger.warning("You may need to configure the database manually")

    def _provision_postgresql(self, db_config: DatabaseConfig) -> None:
        """Provision PostgreSQL database."""
        try:
            from wasm.managers.database import DatabaseRegistry

            manager = DatabaseRegistry.get("postgresql")
            if not manager:
                self.logger.warning("PostgreSQL manager not available")
                return

            if not manager.is_installed():
                self.logger.warning("PostgreSQL is not installed")
                return

            self.logger.substep(f"Creating PostgreSQL database: {db_config.name}")

            # Create database
            try:
                manager.create_database(db_config.name)
            except Exception as e:
                if "already exists" not in str(e).lower():
                    raise
                self.logger.debug(f"Database {db_config.name} already exists")

            # Create user with CREATEDB (needed for Prisma shadow database)
            try:
                manager.create_user(
                    username=db_config.user,
                    password=db_config.password,
                    createdb=True,  # Prisma needs this for migrations
                )
            except Exception as e:
                if "already exists" not in str(e).lower():
                    raise
                self.logger.debug(f"User {db_config.user} already exists")

            # Grant privileges
            try:
                manager.grant_privileges(
                    database=db_config.name,
                    username=db_config.user,
                )
            except Exception:
                pass  # May fail if already granted

            # Grant schema permissions (needed for Prisma migrations)
            try:
                schema_sql = f"""
                GRANT ALL ON SCHEMA public TO {db_config.user};
                ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO {db_config.user};
                ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON SEQUENCES TO {db_config.user};
                """
                manager._execute_sql(schema_sql, database=db_config.name)
            except Exception:
                pass  # May fail but usually not critical

            # Register in store
            app = self.store.get_app(self.domain)
            if app:
                db_record = Database(
                    app_id=app.id,
                    name=db_config.name,
                    engine=DatabaseEngine.POSTGRESQL.value,
                    host=db_config.host,
                    port=db_config.port,
                    username=db_config.user,
                )
                try:
                    self.store.create_database(db_record)
                except Exception:
                    pass  # May already exist

        except ImportError:
            self.logger.debug("Database manager not available")

    def _provision_redis(self, db_config: DatabaseConfig) -> None:
        """Verify Redis is available."""
        try:
            from wasm.managers.database import DatabaseRegistry

            manager = DatabaseRegistry.get("redis")
            if not manager:
                self.logger.warning("Redis manager not available")
                return

            if not manager.is_installed():
                self.logger.warning("Redis is not installed")
                return

            self.logger.substep("Redis verified")

        except ImportError:
            self.logger.debug("Database manager not available")

    def _configure_environment(self) -> None:
        """Configure environment variables for all workspaces.

        Uses EnvManager to discover variables from .env.example files,
        auto-generate secrets, and write .env files. Falls back to
        manual configuration for database URLs and workspace ports.
        """
        from wasm.deployers.helpers import EnvManager

        env_manager = EnvManager(verbose=self.verbose)

        # Build database URLs
        if "postgresql" in self.databases:
            db = self.databases["postgresql"]
            db_url = f"postgresql://{db.user}:{db.password}@{db.host}:{db.port}/{db.name}"
            self.env_vars["DATABASE_URL"] = db_url

        if "redis" in self.databases:
            db = self.databases["redis"]
            self.env_vars["REDIS_HOST"] = db.host
            self.env_vars["REDIS_PORT"] = str(db.port)
            self.env_vars["REDIS_URL"] = f"redis://{db.host}:{db.port}/{db.db_number}"

        # Global environment
        self.env_vars["NODE_ENV"] = "production"

        # Discover variables from .env.example files
        discovered = env_manager.discover(self.app_path)
        if discovered:
            self.logger.substep(f"Discovered {len(discovered)} env variables")
            auto_values = env_manager.prompt_non_interactive(discovered)
            # CLI-provided and database env vars take precedence
            for key, val in self.env_vars.items():
                auto_values[key] = val
            self.env_vars.update(auto_values)

        # Create .env files for each workspace
        for ws in self.workspaces:
            ws_env = self.env_vars.copy()
            ws_env["PORT"] = str(ws.port)
            ws_env.update(ws.env_vars)

            env_file = self.app_path / ws.path / ".env.production"
            self._write_env_file(env_file, ws_env)
            self.logger.substep(f"Created {ws.path}/.env.production")

        # Root .env for Prisma
        root_env_file = self.app_path / ".env"
        if "DATABASE_URL" in self.env_vars:
            self._write_env_file(root_env_file, {"DATABASE_URL": self.env_vars["DATABASE_URL"]})

    def _write_env_file(self, path: Path, env_vars: Dict[str, str]) -> None:
        """Write environment variables to a file."""
        lines = []
        for key, value in sorted(env_vars.items()):
            # Don't quote values for systemd compatibility
            lines.append(f"{key}={value}")
        path.write_text("\n".join(lines) + "\n")

    def _install_dependencies(self) -> None:
        """Install dependencies using pnpm."""
        # Verify pnpm
        self.package_manager = self._pm_helper.verify("pnpm")

        result = self._run(
            ["pnpm", "install", "--frozen-lockfile"],
            timeout=600,
        )

        if not result.success:
            raise DeploymentError(
                "Failed to install dependencies",
                details=result.stderr or result.stdout
            )

    def _run_prisma_migrations(self) -> None:
        """Run Prisma migrations if detected."""
        # Check for project scripts first (preferred method)
        package_json = self.app_path / "package.json"
        has_db_scripts = False

        if package_json.exists():
            try:
                data = json.loads(package_json.read_text())
                scripts = data.get("scripts", {})
                has_db_scripts = "db:generate" in scripts or "db:migrate" in scripts
            except Exception:
                pass

        if has_db_scripts:
            # Use project's own Prisma scripts
            self.logger.substep("Using project db scripts for Prisma")

            if "db:generate" in scripts:
                self.logger.substep("Generating Prisma client (pnpm db:generate)")
                result = self._run(["pnpm", "db:generate"], timeout=120)
                if not result.success:
                    self.logger.warning(f"Prisma generate failed: {result.stderr}")

            if "db:migrate" in scripts:
                self.logger.substep("Running Prisma migrations (pnpm db:migrate)")
                result = self._run(["pnpm", "db:migrate"], timeout=120)
                if not result.success:
                    self.logger.warning(f"Prisma migrate failed: {result.stderr}")

            return

        # Fallback: Check for Prisma schema directly
        prisma_dirs = [
            self.app_path / "packages" / "database" / "prisma",
            self.app_path / "prisma",
        ]

        for prisma_dir in prisma_dirs:
            schema_file = prisma_dir / "schema.prisma"
            if schema_file.exists():
                self.logger.substep(f"Found Prisma schema: {schema_file.relative_to(self.app_path)}")

                # Generate client
                self.logger.substep("Generating Prisma client")
                result = self._run(
                    ["pnpm", "exec", "prisma", "generate", "--schema", str(schema_file)],
                )
                if not result.success:
                    self.logger.warning(f"Prisma generate failed: {result.stderr}")

                # Check for migrations
                migrations_dir = prisma_dir / "migrations"
                if migrations_dir.exists() and any(migrations_dir.iterdir()):
                    self.logger.substep("Running Prisma migrations")
                    result = self._run(
                        ["pnpm", "exec", "prisma", "migrate", "deploy", "--schema", str(schema_file)],
                    )
                    if not result.success:
                        self.logger.warning(f"Prisma migrate failed: {result.stderr}")
                else:
                    self.logger.substep("No migrations to run")

                return

        self.logger.substep("No Prisma schema found")

    def _set_permissions(self) -> None:
        """Set correct ownership and permissions for the app directory."""
        service_user = self.config.service_user
        service_group = self.config.service_group

        try:
            # Change ownership recursively
            result = run_command(
                ["chown", "-R", f"{service_user}:{service_group}", str(self.app_path)],
                timeout=60,
            )
            if not result.success:
                self.logger.debug(f"chown failed: {result.stderr}")

            # Ensure directories are executable and writable
            run_command(
                ["chmod", "-R", "u+rwX,g+rX,o+rX", str(self.app_path)],
                timeout=60,
            )
        except Exception as e:
            self.logger.debug(f"Failed to set permissions: {e}")

    def _build_all(self) -> None:
        """Build all applications using Turborepo."""
        build_timeout = self._turbo_helper.estimate_build_timeout(
            self.app_path,
            len(self.workspaces)
        )

        self.logger.substep(f"Building {len(self.workspaces)} workspace(s)")

        result = self._run(
            ["pnpm", "build"],
            timeout=build_timeout,
        )

        if not result.success:
            # Check for OOM
            if result.exit_code == 137:
                raise OutOfMemoryError(
                    "Build process was killed (likely out of memory)",
                    details=(
                        "The build process was terminated, possibly due to insufficient memory.\n\n"
                        "Try:\n"
                        "  - Increasing server memory\n"
                        "  - Building fewer workspaces at once\n"
                        "  - Setting NODE_OPTIONS='--max-old-space-size=4096'"
                    )
                )
            raise BuildError(
                "Build failed",
                details=result.stderr or result.stdout
            )

    def _create_sites(self, with_ssl: bool = False) -> None:
        """Create nginx/apache site configurations for all workspaces."""
        if self.webserver == "nginx":
            manager = NginxManager(verbose=self.verbose)
            self._create_nginx_sites(manager, with_ssl)
        else:
            manager = ApacheManager(verbose=self.verbose)
            self._create_apache_sites(manager, with_ssl)

        manager.reload()

    def _create_nginx_sites(self, manager: NginxManager, with_ssl: bool) -> None:
        """Create nginx configurations for each workspace."""
        from jinja2 import Template

        # Load monorepo template
        template_path = Path(__file__).parent.parent / "templates" / "nginx" / "monorepo.conf.j2"

        # Fallback to generating config programmatically if template doesn't exist
        if not template_path.exists():
            self._create_nginx_sites_inline(manager, with_ssl)
            return

        template = Template(template_path.read_text())

        context = {
            "domain": self.domain,
            "workspaces": self.workspaces,
            "ssl": with_ssl,
            "primary_subdomain": self.workspaces[0].subdomain if self.workspaces else "app",
        }

        config_content = template.render(**context)

        # Write configuration
        config_file = Path(f"/etc/nginx/sites-available/{self.domain}")
        config_file.write_text(config_content)

        # Enable site
        enabled_link = Path(f"/etc/nginx/sites-enabled/{self.domain}")
        if not enabled_link.exists():
            enabled_link.symlink_to(config_file)

        self._created_sites.append(self.domain)

        # Register sites in store
        for ws in self.workspaces:
            self._register_site_in_store(ws, with_ssl)

    def _create_nginx_sites_inline(self, manager: NginxManager, with_ssl: bool) -> None:
        """Create nginx config inline without template file."""
        lines = [
            f"# Nginx configuration for {self.domain} (Monorepo)",
            "# Generated by WASM",
            "",
        ]

        # Upstream blocks
        for ws in self.workspaces:
            upstream_name = ws.name.replace("-", "_")
            lines.extend([
                f"upstream {upstream_name}_backend {{",
                f"    server 127.0.0.1:{ws.port};",
                "}",
                "",
            ])

        # HTTP server for ACME challenges
        server_names = f"{self.domain} " + " ".join(
            f"{ws.subdomain}.{self.domain}" for ws in self.workspaces
        )
        lines.extend([
            "server {",
            "    listen 80;",
            "    listen [::]:80;",
            f"    server_name {server_names};",
            "",
            "    location /.well-known/acme-challenge/ {",
            "        root /var/www/html;",
            "        allow all;",
            "    }",
            "",
        ])

        if with_ssl:
            lines.extend([
                "    location / {",
                "        return 301 https://$host$request_uri;",
                "    }",
                "}",
                "",
            ])

            # HTTPS servers for each workspace
            for ws in self.workspaces:
                upstream_name = ws.name.replace("-", "_")
                lines.extend([
                    "server {",
                    "    listen 443 ssl http2;",
                    "    listen [::]:443 ssl http2;",
                    f"    server_name {ws.subdomain}.{self.domain};",
                    "",
                    f"    ssl_certificate /etc/letsencrypt/live/{self.domain}/fullchain.pem;",
                    f"    ssl_certificate_key /etc/letsencrypt/live/{self.domain}/privkey.pem;",
                    "",
                    "    ssl_protocols TLSv1.2 TLSv1.3;",
                    "    ssl_prefer_server_ciphers off;",
                    "",
                    f"    access_log /var/log/nginx/{ws.subdomain}.{self.domain}.access.log;",
                    f"    error_log /var/log/nginx/{ws.subdomain}.{self.domain}.error.log;",
                    "",
                    "    location / {",
                    f"        proxy_pass http://{upstream_name}_backend;",
                    "        proxy_http_version 1.1;",
                    "        proxy_set_header Host $host;",
                    "        proxy_set_header X-Real-IP $remote_addr;",
                    "        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;",
                    "        proxy_set_header X-Forwarded-Proto $scheme;",
                    "        proxy_set_header Upgrade $http_upgrade;",
                    '        proxy_set_header Connection "upgrade";',
                    "    }",
                    "}",
                    "",
                ])
        else:
            # HTTP-only configuration
            for ws in self.workspaces:
                upstream_name = ws.name.replace("-", "_")
                lines.extend([
                    f"    location @{upstream_name} {{",
                    f"        proxy_pass http://{upstream_name}_backend;",
                    "        proxy_http_version 1.1;",
                    "        proxy_set_header Host $host;",
                    "        proxy_set_header X-Real-IP $remote_addr;",
                    "        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;",
                    "        proxy_set_header X-Forwarded-Proto $scheme;",
                    "        proxy_set_header Upgrade $http_upgrade;",
                    '        proxy_set_header Connection "upgrade";',
                    "    }",
                    "",
                ])

            lines.append("}")

        config_content = "\n".join(lines)

        # Write configuration
        config_file = Path(f"/etc/nginx/sites-available/{self.domain}")
        config_file.write_text(config_content)

        # Enable site
        enabled_link = Path(f"/etc/nginx/sites-enabled/{self.domain}")
        if not enabled_link.exists():
            enabled_link.symlink_to(config_file)

        self._created_sites.append(self.domain)

        for ws in self.workspaces:
            self._register_site_in_store(ws, with_ssl)

    def _create_apache_sites(self, manager: ApacheManager, with_ssl: bool) -> None:
        """Create Apache configurations (simplified version)."""
        self.logger.warning("Apache support for monorepos is limited")
        # Similar implementation for Apache would go here
        pass

    def _register_site_in_store(self, workspace: MonorepoWorkspace, with_ssl: bool) -> None:
        """Register a site in the store."""
        from wasm.core.config import NGINX_SITES_AVAILABLE

        app = self.store.get_app(self.domain)
        app_id = app.id if app else None

        subdomain = f"{workspace.subdomain}.{self.domain}"

        existing_site = self.store.get_site(subdomain)

        site = Site(
            id=existing_site.id if existing_site else None,
            app_id=app_id,
            domain=subdomain,
            webserver=self.webserver,
            config_path=str(NGINX_SITES_AVAILABLE / self.domain),
            enabled=True,
            is_static=False,
            proxy_port=workspace.port,
            ssl_enabled=with_ssl,
            ssl_certificate=f"/etc/letsencrypt/live/{self.domain}/fullchain.pem" if with_ssl else None,
            ssl_key=f"/etc/letsencrypt/live/{self.domain}/privkey.pem" if with_ssl else None,
        )

        if existing_site:
            self.store.update_site(site)
        else:
            self.store.create_site(site)

    def _obtain_certificate(self) -> None:
        """Obtain SSL certificate for all subdomains."""
        domains = [self.domain] + [
            f"{ws.subdomain}.{self.domain}" for ws in self.workspaces
        ]

        self.logger.substep(f"Requesting certificate for {len(domains)} domain(s)")

        self.cert_manager.create(
            domains=domains,
            webserver=self.webserver,
        )

    def _create_services(self) -> None:
        """Create systemd services for each workspace."""
        for ws in self.workspaces:
            service_name = f"{self.app_name}-{ws.name}"
            working_dir = self.app_path / ws.path

            # Determine start command
            start_command = self._get_workspace_start_command(ws, working_dir)

            # Resolve to absolute path
            start_command = self._path_resolver.resolve_command(start_command)

            self.logger.substep(f"Creating service: {service_name}")

            # Build environment
            env = self.env_vars.copy()
            env["PORT"] = str(ws.port)
            env["NODE_ENV"] = "production"
            # HOME is needed for pnpm to write its cache
            env["HOME"] = str(self.app_path)
            env.update(ws.env_vars)

            # Create service
            self.service_manager.create_service(
                name=service_name,
                command=start_command,
                working_directory=str(working_dir),
                environment=env,
                description=f"WASM: {ws.subdomain}.{self.domain} ({ws.app_type})",
            )

            # Enable service
            self.service_manager.enable(service_name)

            # Register in store
            self._register_service_in_store(ws, service_name, start_command, env)

            self._created_services.append(service_name)

    def _get_workspace_start_command(self, workspace: MonorepoWorkspace, working_dir: Path) -> str:
        """Get the start command for a workspace."""
        if workspace.start_command:
            # Check if it's a script name
            if not workspace.start_command.startswith("/"):
                return f"pnpm run {workspace.start_command}"
            return workspace.start_command

        # Detect based on app type
        if workspace.app_type == "nextjs":
            return "pnpm run start"

        if workspace.app_type == "nodejs":
            # Check for NestJS
            if (working_dir / "nest-cli.json").exists():
                return "node dist/main"
            return "pnpm run start"

        return "pnpm run start"

    def _register_service_in_store(
        self,
        workspace: MonorepoWorkspace,
        service_name: str,
        command: str,
        env: Dict[str, str],
    ) -> None:
        """Register a service in the store."""
        from wasm.core.config import SYSTEMD_DIR

        app = self.store.get_app(self.domain)
        app_id = app.id if app else None

        existing_service = self.store.get_service(service_name)

        service = Service(
            id=existing_service.id if existing_service else None,
            app_id=app_id,
            name=service_name,
            unit_file=str(SYSTEMD_DIR / f"{service_name}.service"),
            working_directory=str(self.app_path / workspace.path),
            command=command,
            user=self.config.service_user,
            group=self.config.service_group,
            enabled=True,
            status="inactive",
            port=workspace.port,
            environment=env,
        )

        if existing_service:
            self.store.update_service(service)
        else:
            self.store.create_service(service)

    def _start_and_verify(self) -> None:
        """Start all services and verify they're running."""
        for service_name in self._created_services:
            self.logger.substep(f"Starting {service_name}")

            try:
                self.service_manager.start(service_name)
            except Exception as e:
                raise ServiceError(
                    f"Failed to start {service_name}",
                    details=str(e)
                )

            # Update status in store
            self.store.update_service_status(service_name, active=True, enabled=True)

        # Health checks
        import time
        time.sleep(3)  # Give services time to start

        for ws in self.workspaces:
            service_name = f"{self.app_name}-{ws.name}"
            status = self.service_manager.get_status(service_name)

            if status.get("active") != "active":
                self.logger.warning(f"Service {service_name} may not be running correctly")

    def _register_app_in_store(self, status: str) -> App:
        """Register application in store."""
        existing_app = self.store.get_app(self.domain)

        # Store workspaces as JSON in env_vars
        workspaces_json = json.dumps([
            {
                "name": ws.name,
                "path": ws.path,
                "app_type": ws.app_type,
                "subdomain": ws.subdomain,
                "port": ws.port,
            }
            for ws in self.workspaces
        ])

        env_with_meta = self.env_vars.copy()
        env_with_meta["_workspaces"] = workspaces_json

        app = App(
            id=existing_app.id if existing_app else None,
            domain=self.domain,
            app_type=self.APP_TYPE,
            source=self.source,
            branch=self.branch,
            port=self.workspaces[0].port if self.workspaces else None,
            app_path=str(self.app_path),
            webserver=self.webserver,
            ssl_enabled=self.ssl,
            status=status,
            is_static=False,
            env_vars=env_with_meta,
        )

        if existing_app:
            app.created_at = existing_app.created_at
            return self.store.update_app(app)
        else:
            return self.store.create_app(app)

    def _show_deployment_summary(self, ssl_obtained: bool) -> None:
        """Show deployment summary."""
        self.logger.success("Monorepo deployed successfully!")
        self.logger.blank()

        protocol = "https" if ssl_obtained else "http"

        self.logger.info("Deployed workspaces:")
        for ws in self.workspaces:
            url = f"{protocol}://{ws.subdomain}.{self.domain}"
            self.logger.key_value(f"  {ws.name}", url)

        self.logger.blank()
        self.logger.key_value("App Path", str(self.app_path))

        if self.ssl and not ssl_obtained:
            self.logger.blank()
            self.logger.warning("SSL was requested but could not be obtained.")
            self.logger.info("To add SSL later, run:")
            domains = [self.domain] + [f"{ws.subdomain}.{self.domain}" for ws in self.workspaces]
            self.logger.info(f"  certbot --nginx -d {' -d '.join(domains)}")

        self.logger.blank()
        self.logger.info("Useful commands:")
        for ws in self.workspaces:
            service_name = f"{self.app_name}-{ws.name}"
            self.logger.info(f"  systemctl status {service_name}")

    def _rollback(self) -> None:
        """Rollback partial deployment."""
        self.logger.debug("Rolling back partial deployment...")
        errors = []

        # Stop and remove services
        for service_name in self._created_services:
            try:
                self.service_manager.stop(service_name)
                self.service_manager.delete_service(service_name)
            except Exception as e:
                errors.append(f"Service cleanup error: {e}")

        # Remove site configuration
        try:
            if self.webserver == "nginx":
                manager = NginxManager(verbose=self.verbose)
            else:
                manager = ApacheManager(verbose=self.verbose)

            if manager.site_exists(self.domain):
                manager.disable_site(self.domain)
                manager.delete_site(self.domain)
                manager.reload()
        except Exception as e:
            errors.append(f"Site cleanup error: {e}")

        # Remove files
        if self.app_path and self.app_path.exists():
            try:
                import shutil
                shutil.rmtree(self.app_path)
            except Exception as e:
                errors.append(f"File cleanup error: {e}")

        # Clean store records
        try:
            for service_name in self._created_services:
                service = self.store.get_service(service_name)
                if service:
                    self.store.delete_service(service.id)

            for ws in self.workspaces:
                site = self.store.get_site(f"{ws.subdomain}.{self.domain}")
                if site:
                    self.store.delete_site(site.id)

            app = self.store.get_app(self.domain)
            if app:
                self.store.delete_app(app.id)
        except Exception as e:
            errors.append(f"Store cleanup error: {e}")

        if errors:
            self.logger.debug(f"Rollback errors: {errors}")


# Register with DeployerRegistry
from wasm.deployers.registry import DeployerRegistry
DeployerRegistry.register(MonorepoDeployer)
