# Copyright (c) 2024-2025 Yago López Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
Base deployer class for WASM.

Defines the interface and common functionality for all deployers.
"""

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Dict, List, Optional, Literal

from wasm.core.config import Config
from wasm.core.logger import Logger
from wasm.core.exceptions import DeploymentError, BuildError, OutOfMemoryError
from wasm.core.utils import run_command, domain_to_app_name
from wasm.core.store import (
    get_store,
    App,
    Site,
    Service,
    AppType,
    AppStatus,
    WebServer,
)
from wasm.managers.nginx_manager import NginxManager
from wasm.managers.apache_manager import ApacheManager
from wasm.managers.service_manager import ServiceManager
from wasm.managers.cert_manager import CertManager
from wasm.managers.source_manager import SourceManager
from wasm.deployers.helpers import PackageManagerHelper, PathResolver, PrismaHelper


# Type for package managers
PackageManager = Literal["npm", "pnpm", "bun", "yarn", "auto"]


class BaseDeployer(ABC):
    """
    Abstract base class for application deployers.
    
    Each deployer handles the deployment workflow for a specific
    type of application (Next.js, Node.js, Python, etc.).
    """
    
    # Deployer identification
    APP_TYPE: str = "base"
    DISPLAY_NAME: str = "Base Application"
    
    # Files used to detect this app type
    DETECTION_FILES: List[str] = []
    DETECTION_PATTERNS: List[str] = []
    
    # Default port
    DEFAULT_PORT: int = 3000
    
    # System dependencies
    SYSTEM_DEPS: List[str] = []
    
    def __init__(self, verbose: bool = False):
        """
        Initialize the deployer.
        
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
        self._prisma_helper: Optional[PrismaHelper] = None  # Initialized after app_path is set
        
        # Deployment configuration
        self.domain: Optional[str] = None
        self.source: Optional[str] = None
        self.port: int = self.DEFAULT_PORT
        self.app_path: Optional[Path] = None
        self.app_name: Optional[str] = None
        self.webserver: str = "nginx"
        self.ssl: bool = True
        self.branch: Optional[str] = None
        self.env_vars: Dict[str, str] = {}
        
        # Package manager (auto = auto-detect)
        self._package_manager: PackageManager = "auto"
        self.package_manager: str = "npm"  # Resolved package manager
        
        # Prisma support
        self.has_prisma: bool = False
    
    def configure(
        self,
        domain: str,
        source: str,
        port: Optional[int] = None,
        webserver: str = "nginx",
        ssl: bool = True,
        branch: Optional[str] = None,
        env_vars: Optional[Dict[str, str]] = None,
        app_path: Optional[Path] = None,
        package_manager: PackageManager = "auto",
    ) -> None:
        """
        Configure the deployer.
        
        Args:
            domain: Target domain.
            source: Source URL or path.
            port: Application port.
            webserver: Web server to use (nginx/apache).
            ssl: Enable SSL.
            branch: Git branch.
            env_vars: Environment variables.
            app_path: Custom application path.
            package_manager: Package manager to use (npm/pnpm/bun/auto).
        """
        self.domain = domain
        self.source = source
        self.port = port or self.DEFAULT_PORT
        self.webserver = webserver
        self.ssl = ssl
        self.branch = branch
        self.env_vars = env_vars or {}
        self._package_manager = package_manager
        
        # Set app name and path
        self.app_name = domain_to_app_name(domain)
        self.app_path = app_path or (self.config.apps_directory / self.app_name)
    
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
    
    def _detect_package_manager(self) -> str:
        """
        Detect the package manager used in the project.

        Returns:
            Detected package manager name.
        """
        return self._pm_helper.detect(self.app_path, self._package_manager)
    
    def _verify_package_manager(self) -> None:
        """
        Verify the package manager is installed and available.
        Falls back to an available package manager if the requested one is not installed.

        Raises:
            DeploymentError: If no package manager is available at all.
        """
        self.package_manager = self._pm_helper.verify(self.package_manager)
    
    def _detect_prisma(self) -> bool:
        """
        Detect if project uses Prisma ORM.

        Returns:
            True if Prisma is detected.
        """
        return self._ensure_prisma_helper().detect(self.app_path)
    
    def _get_pm_install_command(self) -> List[str]:
        """
        Get the package manager install command.

        Returns:
            Install command as list.
        """
        return self._pm_helper.get_install_command(self.package_manager)
    
    def _get_pm_run_command(self, script: str) -> List[str]:
        """
        Get the package manager run command.

        Args:
            script: Script name to run.

        Returns:
            Run command as list.
        """
        return self._pm_helper.get_run_command(self.package_manager, script)
    
    def _get_pm_exec_command(self, command: str) -> List[str]:
        """
        Get the package manager exec/dlx command.

        Args:
            command: Command to execute.

        Returns:
            Exec command as list.
        """
        return self._pm_helper.get_exec_command(self.package_manager, command)
    
    def _is_private_path(self, path: str) -> bool:
        """
        Check if path is in a private/user-specific directory.

        Args:
            path: Absolute path to check.

        Returns:
            True if path is in a private directory.
        """
        return self._path_resolver.is_private_path(path)
    
    def _find_global_executable(self, executable: str) -> Optional[str]:
        """
        Find executable in global/system paths only.

        Args:
            executable: Name of executable to find.

        Returns:
            Absolute path if found in system paths, None otherwise.
        """
        return self._path_resolver.find_global_executable(executable)
    
    def _resolve_absolute_path(self, command: str) -> str:
        """
        Resolve command to use absolute paths for executables.

        This is required for systemd services as ExecStart
        must use absolute paths. Prefers global system paths
        over user-specific installations (like nvm) to avoid
        permission issues with systemd services.

        Args:
            command: Command string (e.g., "npm run start")

        Returns:
            Command with absolute path (e.g., "/usr/bin/npm run start")
        """
        return self._path_resolver.resolve_command(command)
    
    def _ensure_prisma_helper(self) -> PrismaHelper:
        """
        Ensure PrismaHelper is initialized and return it.

        Returns:
            Initialized PrismaHelper instance.
        """
        if self._prisma_helper is None:
            self._prisma_helper = PrismaHelper(
                logger=self.logger,
                run_command=self._run,
                get_exec_command=self._get_pm_exec_command,
            )
        return self._prisma_helper

    def generate_prisma(self) -> bool:
        """
        Generate Prisma client if Prisma is detected.

        Returns:
            True if successful or not needed.
        """
        if not self.has_prisma:
            return True

        return self._ensure_prisma_helper().generate(self.app_path)

    def run_prisma_migrate(self, deploy: bool = True) -> bool:
        """
        Run Prisma migrations.

        Args:
            deploy: If True, run deploy (production), else run dev.

        Returns:
            True if successful.
        """
        if not self.has_prisma:
            return True

        # Check if there's a migrations folder
        migrations_dir = self.app_path / "prisma" / "migrations"
        if not migrations_dir.exists():
            self.logger.debug("No Prisma migrations found")
            return True

        return self._ensure_prisma_helper().migrate(self.app_path, deploy=deploy)
    
    @abstractmethod
    def detect(self, path: Path) -> bool:
        """
        Detect if path contains this type of application.
        
        Args:
            path: Path to check.
            
        Returns:
            True if this deployer can handle the application.
        """
        pass
    
    @abstractmethod
    def get_install_command(self) -> List[str]:
        """
        Get the command to install dependencies.
        
        Returns:
            Command as list of arguments.
        """
        pass
    
    @abstractmethod
    def get_build_command(self) -> List[str]:
        """
        Get the command to build the application.
        
        Returns:
            Command as list of arguments.
        """
        pass
    
    @abstractmethod
    def get_start_command(self) -> str:
        """
        Get the command to start the application.
        
        Returns:
            Start command string.
        """
        pass
    
    def get_health_check(self) -> str:
        """
        Get the health check endpoint.
        
        Returns:
            Health check path (default: /).
        """
        return "/"
    
    def get_nginx_template(self) -> str:
        """
        Get the Nginx template name for this app type.
        
        Returns:
            Template name.
        """
        return "proxy"
    
    def get_apache_template(self) -> str:
        """
        Get the Apache template name for this app type.
        
        Returns:
            Template name.
        """
        return "proxy"
    
    def get_template_context(self) -> Dict:
        """
        Get template context for configuration files.
        
        Returns:
            Context dictionary.
        """
        return {
            "domain": self.domain,
            "port": self.port,
            "app_path": str(self.app_path),
            "app_name": self.app_name,
            "ssl": self.ssl,
            "health_check": self.get_health_check(),
        }
    
    def check_dependencies(self) -> bool:
        """
        Check if system dependencies are installed.

        Returns:
            True if all dependencies are available.
        """
        from wasm.core.utils import command_exists

        for dep in self.SYSTEM_DEPS:
            if not command_exists(dep):
                self.logger.warning(f"Missing dependency: {dep}")
                return False

        return True

    def pre_flight_check(self) -> bool:
        """
        Perform pre-deployment validation checks.

        Validates that all conditions are met before starting deployment:
        - Repository is accessible (for git sources)
        - Sufficient disk space
        - Port is available
        - System dependencies are installed

        Returns:
            True if all checks pass.

        Raises:
            DeploymentError: If any check fails.
        """
        checks_passed = True
        issues = []

        self.logger.debug("Running pre-flight checks...")

        # 1. Check system dependencies
        if not self.check_dependencies():
            issues.append(f"Missing system dependencies: {', '.join(self.SYSTEM_DEPS)}")
            checks_passed = False

        # 2. Check repository accessibility (for git sources)
        if self.source and (
            self.source.startswith("git@") or
            self.source.startswith("https://") or
            self.source.startswith("http://") or
            self.source.startswith("git://")
        ):
            result = run_command(
                ["git", "ls-remote", "--exit-code", self.source],
                timeout=30
            )
            if not result.success:
                issues.append(f"Repository not accessible: {self.source}")
                if "Permission denied" in str(result.stderr):
                    issues.append("Check SSH key configuration: wasm setup ssh --test")
                checks_passed = False

        # 3. Check disk space (require at least 1GB free)
        import shutil
        try:
            apps_dir = self.config.apps_directory
            if apps_dir.exists():
                stat = shutil.disk_usage(str(apps_dir))
                free_gb = stat.free / (1024 ** 3)
                if free_gb < 1.0:
                    issues.append(f"Insufficient disk space: {free_gb:.1f}GB free (need 1GB minimum)")
                    checks_passed = False
        except Exception as e:
            self.logger.debug(f"Could not check disk space: {e}")

        # 4. Check if port is available
        if self.port:
            import socket
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                result = sock.connect_ex(('127.0.0.1', self.port))
                if result == 0:
                    # Port is in use - check if it's our own service
                    existing_app = self.store.get_app(self.domain)
                    if not existing_app or existing_app.port != self.port:
                        issues.append(f"Port {self.port} is already in use")
                        checks_passed = False
            finally:
                sock.close()

        # 5. Check webserver is running
        try:
            if self.webserver == "nginx":
                manager = NginxManager(verbose=self.verbose)
            else:
                manager = ApacheManager(verbose=self.verbose)

            if not manager.is_running():
                issues.append(f"{self.webserver} is not running")
                checks_passed = False
        except Exception as e:
            self.logger.debug(f"Could not check webserver status: {e}")

        if not checks_passed:
            details = "\n".join(f"  - {issue}" for issue in issues)
            raise DeploymentError(
                "Pre-flight checks failed",
                details=f"The following issues were found:\n{details}"
            )

        self.logger.debug("All pre-flight checks passed")
        return True

    def rollback(self, keep_files: bool = False) -> bool:
        """
        Rollback a partial deployment.

        Cleans up any resources created during a failed deployment:
        - Systemd service (if created)
        - Web server configuration (if created)
        - Application files (if keep_files=False)
        - Store records

        Args:
            keep_files: If True, preserve the application files.

        Returns:
            True if rollback completed successfully.
        """
        self.logger.debug("Rolling back partial deployment...")
        errors = []

        # 1. Stop and remove service if it exists
        try:
            if self.app_name:
                status = self.service_manager.status(self.app_name)
                if status.get("exists"):
                    self.logger.debug(f"Removing service: {self.app_name}")
                    try:
                        self.service_manager.stop(self.app_name)
                    except Exception:
                        pass  # Service might not be running
                    self.service_manager.delete_service(self.app_name)
        except Exception as e:
            errors.append(f"Service cleanup failed: {e}")
            self.logger.debug(f"Service cleanup error: {e}")

        # 2. Remove web server configuration
        try:
            if self.domain and self.webserver:
                if self.webserver == "nginx":
                    manager = NginxManager(verbose=self.verbose)
                else:
                    manager = ApacheManager(verbose=self.verbose)

                if manager.site_exists(self.domain):
                    self.logger.debug(f"Removing site config: {self.domain}")
                    manager.disable_site(self.domain)
                    manager.delete_site(self.domain)
                    manager.reload()
        except Exception as e:
            errors.append(f"Site cleanup failed: {e}")
            self.logger.debug(f"Site cleanup error: {e}")

        # 3. Remove application files
        if not keep_files and self.app_path and self.app_path.exists():
            try:
                import shutil
                self.logger.debug(f"Removing app files: {self.app_path}")
                shutil.rmtree(self.app_path)
            except Exception as e:
                errors.append(f"File cleanup failed: {e}")
                self.logger.debug(f"File cleanup error: {e}")

        # 4. Clean up store records
        try:
            if self.domain:
                # Remove service record
                if self.app_name:
                    service = self.store.get_service(self.app_name)
                    if service:
                        self.store.delete_service(service.id)

                # Remove site record
                site = self.store.get_site(self.domain)
                if site:
                    self.store.delete_site(site.id)

                # Remove or update app record
                app = self.store.get_app(self.domain)
                if app:
                    self.store.delete_app(app.id)
        except Exception as e:
            errors.append(f"Store cleanup failed: {e}")
            self.logger.debug(f"Store cleanup error: {e}")

        if errors:
            self.logger.debug(f"Rollback completed with {len(errors)} errors")
        else:
            self.logger.debug("Rollback completed successfully")

        return len(errors) == 0

    def pre_install(self) -> bool:
        """
        Pre-installation hook.
        
        Override to perform actions before dependency installation.
        Detects package manager and Prisma by default.
        
        Returns:
            True if successful.
        """
        # Detect package manager
        self.package_manager = self._detect_package_manager()
        self.logger.debug(f"Using package manager: {self.package_manager}")
        
        # Verify the package manager is available
        self._verify_package_manager()
        
        # Detect Prisma
        self.has_prisma = self._detect_prisma()
        if self.has_prisma:
            self.logger.debug("Prisma detected")
        
        return True
    
    def post_install(self) -> bool:
        """
        Post-installation hook.
        
        Override to perform actions after dependency installation.
        Generates Prisma client by default if needed.
        
        Returns:
            True if successful.
        """
        # Generate Prisma client if detected
        if self.has_prisma:
            self.generate_prisma()
        
        return True
    
    def pre_build(self) -> bool:
        """
        Pre-build hook.
        
        Override to perform actions before building.
        
        Returns:
            True if successful.
        """
        return True
    
    def post_build(self) -> bool:
        """
        Post-build hook.
        
        Override to perform actions after building.
        
        Returns:
            True if successful.
        """
        return True
    
    def fetch_source(self) -> bool:
        """
        Fetch the source code.
        
        Returns:
            True if successful.
        """
        self.logger.substep(f"Source: {self.source}")
        self.logger.substep(f"Target: {self.app_path}")
        
        return self.source_manager.fetch(
            self.source,
            self.app_path,
            branch=self.branch,
        )
    
    def install_dependencies(self) -> bool:
        """
        Install application dependencies.
        
        Returns:
            True if successful.
        """
        self.pre_install()
        
        command = self.get_install_command()
        if not command:
            return True
        
        self.logger.substep(f"Running: {' '.join(command)}")
        
        result = self._run(command, timeout=600)
        if not result.success:
            # Try fallback install methods
            fallback_command = None
            
            # Check if it's a frozen lockfile issue (pnpm/yarn/bun)
            if "--frozen-lockfile" in command:
                self.logger.warning("Strict lockfile install failed, trying regular install...")
                fallback_command = [c for c in command if c != "--frozen-lockfile"]
            
            # Check if it's npm ci failing (no package-lock.json)
            elif command == ["npm", "ci"]:
                if "package-lock.json" in str(result.stderr) or "EUSAGE" in str(result.stderr):
                    self.logger.warning("npm ci failed (no lockfile), using npm install...")
                    fallback_command = ["npm", "install"]
            
            if fallback_command:
                self.logger.substep(f"Running: {' '.join(fallback_command)}")
                result = self._run(fallback_command, timeout=600)
            
            if not result.success:
                # Combine stdout and stderr for better error visibility
                error_output = ""
                if result.stderr:
                    error_output = result.stderr
                if result.stdout:
                    if error_output:
                        error_output += "\n" + result.stdout
                    else:
                        error_output = result.stdout
                
                raise DeploymentError(
                    "Dependency installation failed",
                    details=error_output or "No error output captured. Check if the package manager is properly installed.",
                )
        
        self.post_install()
        return True
    
    def build(self) -> bool:
        """
        Build the application.
        
        Returns:
            True if successful.
            
        Raises:
            OutOfMemoryError: If build is killed due to OOM (exit code 137).
            BuildError: If build fails for other reasons.
        """
        self.pre_build()
        
        command = self.get_build_command()
        if not command:
            return True
        
        self.logger.substep(f"Running: {' '.join(command)}")
        
        result = self._run(command, timeout=900)
        if not result.success:
            # Combine stdout and stderr for better error visibility
            error_output = ""
            if result.stderr:
                error_output = result.stderr
            if result.stdout:
                if error_output:
                    error_output += "\n" + result.stdout
                else:
                    error_output = result.stdout
            
            # Check for OOM killer (exit code 137 = 128 + SIGKILL)
            if result.exit_code == 137:
                raise OutOfMemoryError(
                    "Build killed due to insufficient memory (exit code 137)",
                    details=error_output or "Process was killed by the OOM killer.",
                )
            
            raise BuildError(
                "Build failed",
                details=error_output or "No error output captured.",
            )
        
        self.post_build()
        return True
    
    def create_site(self, with_ssl: bool = False) -> bool:
        """
        Create web server site configuration.
        
        Args:
            with_ssl: If True, create config with SSL enabled.
                      If False, create config without SSL (for initial setup).
        
        Returns:
            True if successful.
        """
        context = self.get_template_context()
        # Override SSL setting based on parameter
        context["ssl"] = with_ssl
        
        if self.webserver == "nginx":
            manager = NginxManager(verbose=self.verbose)
            template = self.get_nginx_template()
        else:
            manager = ApacheManager(verbose=self.verbose)
            template = self.get_apache_template()
        
        self.logger.substep(f"Web server: {self.webserver}")
        self.logger.substep(f"Template: {template}")
        if self.ssl:
            self.logger.substep(f"SSL: {'enabled' if with_ssl else 'pending certificate'}")
        
        # Check if site already exists (update vs create)
        if manager.site_exists(self.domain):
            manager.update_site(self.domain, template=template, context=context)
        else:
            # Create site
            manager.create_site(self.domain, template=template, context=context)
            # Enable site
            manager.enable_site(self.domain)
        
        # Reload web server
        manager.reload()
        
        # Register site in store
        self._register_site_in_store(with_ssl, template)
        
        return True
    
    def _register_site_in_store(self, with_ssl: bool, template: str) -> None:
        """Register or update site in persistent store."""
        from wasm.core.config import NGINX_SITES_AVAILABLE, APACHE_SITES_AVAILABLE
        
        config_path = (
            NGINX_SITES_AVAILABLE if self.webserver == "nginx"
            else APACHE_SITES_AVAILABLE
        ) / self.domain
        
        is_static = template == "static"
        
        # Get app_id if app exists
        app = self.store.get_app(self.domain)
        app_id = app.id if app else None
        
        existing_site = self.store.get_site(self.domain)
        
        site = Site(
            id=existing_site.id if existing_site else None,
            app_id=app_id,
            domain=self.domain,
            webserver=self.webserver,
            config_path=str(config_path),
            enabled=True,
            is_static=is_static,
            document_root=str(self.app_path) if is_static else None,
            proxy_port=self.port if not is_static else None,
            ssl_enabled=with_ssl,
            ssl_certificate=f"/etc/letsencrypt/live/{self.domain}/fullchain.pem" if with_ssl else None,
            ssl_key=f"/etc/letsencrypt/live/{self.domain}/privkey.pem" if with_ssl else None,
        )
        
        if existing_site:
            self.store.update_site(site)
        else:
            self.store.create_site(site)
    
    def create_service(self) -> bool:
        """
        Create systemd service.
        
        Returns:
            True if successful.
        """
        start_command = self.get_start_command()
        
        # Resolve to absolute path for systemd compatibility
        start_command = self._resolve_absolute_path(start_command)
        
        self.logger.substep(f"Service: {self.app_name}")
        self.logger.substep(f"Command: {start_command}")
        
        # Build environment with PORT
        env = self.env_vars.copy()
        env["PORT"] = str(self.port)
        env["NODE_ENV"] = "production"
        
        self.service_manager.create_service(
            name=self.app_name,
            command=start_command,
            working_directory=str(self.app_path),
            environment=env,
            description=f"WASM: {self.domain} ({self.APP_TYPE})",
        )
        
        # Enable service
        self.service_manager.enable(self.app_name)
        
        # Register service in store
        self._register_service_in_store(start_command, env)
        
        return True
    
    def _register_service_in_store(self, command: str, env: Dict[str, str]) -> None:
        """Register service in persistent store."""
        from wasm.core.config import SYSTEMD_DIR
        
        # Get app_id if app exists
        app = self.store.get_app(self.domain)
        app_id = app.id if app else None
        
        # Service name without prefix (store handles that)
        service_name = self.app_name
        service_file = SYSTEMD_DIR / f"wasm-{self.app_name}.service"
        
        existing_service = self.store.get_service(service_name)
        
        service = Service(
            id=existing_service.id if existing_service else None,
            app_id=app_id,
            name=service_name,
            unit_file=str(service_file),
            working_directory=str(self.app_path),
            command=command,
            user=self.config.service_user,
            group=self.config.service_group,
            enabled=True,
            status="inactive",  # Will be set to "active" after start
            port=self.port,
            environment=env,
        )
        
        if existing_service:
            self.store.update_service(service)
        else:
            self.store.create_service(service)
    
    def obtain_certificate(self) -> bool:
        """
        Obtain SSL certificate.
        
        Returns:
            True if successful.
        """
        if not self.ssl:
            return True
        
        self.logger.substep(f"Domain: {self.domain}")
        
        # Use nginx plugin if using nginx
        nginx = self.webserver == "nginx"
        apache = self.webserver == "apache"
        
        self.cert_manager.obtain(
            self.domain,
            nginx=nginx,
            apache=apache,
        )
        
        return True
    
    def start(self) -> bool:
        """
        Start the application service.
        
        Returns:
            True if successful.
        """
        self.service_manager.start(self.app_name)
        return True
    
    def stop(self) -> bool:
        """
        Stop the application service.
        
        Returns:
            True if successful.
        """
        self.service_manager.stop(self.app_name)
        return True
    
    def restart(self) -> bool:
        """
        Restart the application service.
        
        Returns:
            True if successful.
        """
        self.service_manager.restart(self.app_name)
        return True
    
    def health_check(self, retries: int = 5, delay: float = 2.0) -> bool:
        """
        Check if the application is healthy.
        
        Args:
            retries: Number of retries.
            delay: Delay between retries in seconds.
            
        Returns:
            True if application is healthy.
        """
        import time
        import urllib.request
        from urllib.error import URLError
        
        endpoint = self.get_health_check()
        url = f"http://127.0.0.1:{self.port}{endpoint}"
        
        self.logger.substep(f"Checking: {url}")
        
        for i in range(retries):
            try:
                with urllib.request.urlopen(url, timeout=5) as response:
                    if response.status == 200:
                        return True
            except URLError as e:
                self.logger.debug(f"Health check URLError: {e}")
            except Exception as e:
                self.logger.debug(f"Health check failed with error: {e}")

            if i < retries - 1:
                self.logger.debug(f"Health check attempt {i + 1} failed, retrying...")
                time.sleep(delay)
        
        return False
    
    def deploy(self, total_steps: int = 7) -> bool:
        """
        Run the full deployment workflow.

        Args:
            total_steps: Total number of deployment steps.

        Returns:
            True if deployment was successful.
        """
        from wasm.core.logger import Icons
        from wasm.core.exceptions import CertificateError
        from datetime import datetime

        # Track if SSL was successfully obtained
        ssl_obtained = False

        # Track if this is a new deployment (for rollback)
        is_new_deployment = not self.store.get_app(self.domain)

        # Pre-flight checks (validation before starting)
        self.logger.debug("Running pre-flight validation...")
        self.pre_flight_check()

        # Register app in store at the start
        app = self._register_app_in_store(AppStatus.DEPLOYING.value)
        
        try:
            # Step 1: Fetch source
            self.logger.step(1, total_steps, "Fetching source code", Icons.DOWNLOAD)
            self.fetch_source()
            
            # Step 2: Install dependencies
            self.logger.step(2, total_steps, "Installing dependencies", Icons.PACKAGE)
            self.install_dependencies()
            
            # Step 3: Build
            self.logger.step(3, total_steps, "Building application", Icons.BUILD)
            self.build()
            
            # Step 4: Create site (initially WITHOUT SSL to allow certbot validation)
            self.logger.step(4, total_steps, "Creating site configuration", Icons.GLOBE)
            self.create_site(with_ssl=False)
            
            # Step 5: SSL certificate (best effort - continue if it fails)
            if self.ssl:
                self.logger.step(5, total_steps, "Obtaining SSL certificate", Icons.LOCK)
                try:
                    self.obtain_certificate()
                    ssl_obtained = True
                    # After obtaining certificate, update site config WITH SSL
                    self.logger.substep("Updating site configuration with SSL")
                    self.create_site(with_ssl=True)
                except CertificateError as e:
                    self.logger.warning(f"SSL certificate failed: {e.message}")
                    self.logger.warning("Continuing deployment without SSL...")
                    self.logger.substep("Application will be available via HTTP only")
                except Exception as e:
                    self.logger.warning(f"SSL certificate failed: {e}")
                    self.logger.warning("Continuing deployment without SSL...")
                    self.logger.substep("Application will be available via HTTP only")
            else:
                self.logger.step(5, total_steps, "Skipping SSL certificate", Icons.LOCK)
            
            # Step 6: Create service
            self.logger.step(6, total_steps, "Creating systemd service", Icons.GEAR)
            self.create_service()
            
            # Step 7: Start and verify
            self.logger.step(7, total_steps, "Starting application", Icons.ROCKET)
            self.start()
            
            # Update app status to running
            app.status = AppStatus.RUNNING.value
            app.ssl_enabled = ssl_obtained
            app.deployed_at = datetime.now().isoformat()
            self.store.update_app(app)
            
            # Update service status
            self.store.update_service_status(self.app_name, active=True, enabled=True)
            
            # Health check
            if self.health_check():
                self._show_deployment_summary(ssl_obtained)
                return True
            else:
                self.logger.warning("Application started but health check failed")
                self._show_deployment_summary(ssl_obtained)
                self.logger.blank()
                self.logger.info("Troubleshooting commands:")
                self.logger.info(f"  wasm logs {self.domain}        # View application logs")
                self.logger.info(f"  wasm status {self.domain}      # Check service status")
                return True  # Still consider it successful
                
        except Exception as e:
            # Update app status to failed
            app.status = AppStatus.FAILED.value
            self.store.update_app(app)
            self.logger.error(f"Deployment failed: {e}")

            # Rollback partial deployment for new apps
            if is_new_deployment:
                self.logger.warning("Rolling back partial deployment...")
                try:
                    self.rollback(keep_files=False)
                    self.logger.info("Rollback completed successfully")
                except Exception as rollback_error:
                    self.logger.debug(f"Rollback error: {rollback_error}")
                    self.logger.warning("Rollback had some errors. Manual cleanup may be needed.")

            raise
    
    def _show_deployment_summary(self, ssl_obtained: bool) -> None:
        """
        Show deployment summary with useful information.

        Args:
            ssl_obtained: Whether SSL certificate was obtained.
        """
        import socket

        self.logger.success("Application deployed successfully!")
        self.logger.blank()

        # Basic info
        protocol = "https" if ssl_obtained else "http"
        self.logger.key_value("URL", f"{protocol}://{self.domain}")
        self.logger.key_value("Service", f"wasm-{self.app_name}")
        self.logger.key_value("Port", str(self.port))
        self.logger.key_value("App Path", str(self.app_path))

        # Get server IP for DNS configuration
        try:
            hostname = socket.gethostname()
            # Try to get the public IP
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                s.connect(("8.8.8.8", 80))
                server_ip = s.getsockname()[0]
            finally:
                s.close()
            self.logger.key_value("Server IP", server_ip)
        except Exception:
            pass

        # SSL status
        if self.ssl and not ssl_obtained:
            self.logger.blank()
            self.logger.warning("SSL was requested but could not be obtained.")
            self.logger.info("To add SSL later, run: wasm cert create -d " + self.domain)

        # Useful commands
        self.logger.blank()
        self.logger.info("Useful commands:")
        self.logger.info(f"  wasm status {self.domain}      # Check application status")
        self.logger.info(f"  wasm logs {self.domain}        # View application logs")
        self.logger.info(f"  wasm restart {self.domain}     # Restart the application")
        self.logger.info(f"  wasm update {self.domain}      # Update from source")

        # DNS reminder if SSL failed
        if self.ssl and not ssl_obtained:
            self.logger.blank()
            self.logger.info("DNS Configuration (for SSL):")
            self.logger.info(f"  Add an A record pointing {self.domain} to your server IP")
            self.logger.info(f"  Then run: wasm cert create -d {self.domain}")

    def _register_app_in_store(self, status: str) -> App:
        """
        Register or update application in persistent store.
        
        Args:
            status: Initial app status.
            
        Returns:
            The created or updated App object.
        """
        existing_app = self.store.get_app(self.domain)
        
        # Determine if this is a static app
        is_static = not bool(self.get_start_command())
        
        app = App(
            id=existing_app.id if existing_app else None,
            domain=self.domain,
            app_type=self.APP_TYPE,
            source=self.source,
            branch=self.branch,
            port=self.port if not is_static else None,
            app_path=str(self.app_path),
            webserver=self.webserver,
            ssl_enabled=self.ssl,
            status=status,
            is_static=is_static,
            env_vars=self.env_vars,
        )
        
        if existing_app:
            # Preserve created_at and deployed_at if updating
            app.created_at = existing_app.created_at
            return self.store.update_app(app)
        else:
            return self.store.create_app(app)
