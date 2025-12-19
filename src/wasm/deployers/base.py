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
from wasm.managers.nginx_manager import NginxManager
from wasm.managers.apache_manager import ApacheManager
from wasm.managers.service_manager import ServiceManager
from wasm.managers.cert_manager import CertManager
from wasm.managers.source_manager import SourceManager


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
        
        # Managers
        self.source_manager = SourceManager(verbose=verbose)
        self.service_manager = ServiceManager(verbose=verbose)
        self.cert_manager = CertManager(verbose=verbose)
        
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
        if self._package_manager != "auto":
            return self._package_manager
        
        if not self.app_path:
            return "npm"
        
        # Check for lock files
        if (self.app_path / "pnpm-lock.yaml").exists():
            return "pnpm"
        elif (self.app_path / "bun.lockb").exists():
            return "bun"
        elif (self.app_path / "yarn.lock").exists():
            return "yarn"
        
        return "npm"
    
    def _verify_package_manager(self) -> None:
        """
        Verify the package manager is installed and available.
        Falls back to an available package manager if the requested one is not installed.
        
        Raises:
            DeploymentError: If no package manager is available at all.
        """
        from wasm.core.utils import command_exists
        from wasm.core.dependencies import DependencyChecker
        
        pm = self.package_manager
        
        if command_exists(pm):
            return  # Requested PM is available, all good
        
        # Requested PM not available, check what is available
        checker = DependencyChecker()
        available = checker.get_available_package_managers()
        
        if not available:
            # No package managers at all
            raise DeploymentError(
                "No package manager available",
                details=(
                    "No Node.js package manager (npm, pnpm, yarn, bun) is installed.\n\n"
                    "To fix this, run the setup wizard:\n"
                    "  sudo wasm setup init\n\n"
                    "Or install Node.js manually which includes npm:\n"
                    "  curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -\n"
                    "  sudo apt install -y nodejs"
                )
            )
        
        # Fall back to first available package manager
        fallback_pm = available[0]
        self.logger.warning(
            f"Package manager '{pm}' not installed. Using '{fallback_pm}' instead."
        )
        self.logger.info(f"Available package managers: {', '.join(available)}")
        self.package_manager = fallback_pm
    
    def _detect_prisma(self) -> bool:
        """
        Detect if project uses Prisma ORM.
        
        Returns:
            True if Prisma is detected.
        """
        if not self.app_path:
            return False
        
        # Check for prisma directory
        if (self.app_path / "prisma").exists():
            return True
        
        # Check package.json for prisma
        package_json = self.app_path / "package.json"
        if package_json.exists():
            try:
                import json
                with open(package_json) as f:
                    pkg = json.load(f)
                    deps = pkg.get("dependencies", {})
                    dev_deps = pkg.get("devDependencies", {})
                    if "@prisma/client" in deps or "prisma" in dev_deps:
                        return True
            except Exception:
                pass
        
        return False
    
    def _get_pm_install_command(self) -> List[str]:
        """
        Get the package manager install command.
        
        Returns:
            Install command as list.
        """
        pm = self.package_manager
        
        if pm == "pnpm":
            return ["pnpm", "install", "--frozen-lockfile"]
        elif pm == "bun":
            return ["bun", "install", "--frozen-lockfile"]
        elif pm == "yarn":
            return ["yarn", "install", "--frozen-lockfile"]
        else:  # npm
            return ["npm", "ci"]
    
    def _get_pm_run_command(self, script: str) -> List[str]:
        """
        Get the package manager run command.
        
        Args:
            script: Script name to run.
            
        Returns:
            Run command as list.
        """
        pm = self.package_manager
        
        if pm == "pnpm":
            return ["pnpm", "run", script]
        elif pm == "bun":
            return ["bun", "run", script]
        elif pm == "yarn":
            return ["yarn", script]
        else:  # npm
            return ["npm", "run", script]
    
    def _get_pm_exec_command(self, command: str) -> List[str]:
        """
        Get the package manager exec/dlx command.
        
        Args:
            command: Command to execute.
            
        Returns:
            Exec command as list.
        """
        pm = self.package_manager
        cmd_parts = command.split()
        
        if pm == "pnpm":
            return ["pnpm", "exec"] + cmd_parts
        elif pm == "bun":
            return ["bunx"] + cmd_parts
        elif pm == "yarn":
            return ["yarn"] + cmd_parts
        else:  # npm
            return ["npx"] + cmd_parts
    
    def _resolve_absolute_path(self, command: str) -> str:
        """
        Resolve command to use absolute paths for executables.
        
        This is required for systemd services as ExecStart
        must use absolute paths.
        
        Args:
            command: Command string (e.g., "npm run start")
            
        Returns:
            Command with absolute path (e.g., "/usr/bin/npm run start")
        """
        import shutil
        
        parts = command.split()
        if not parts:
            return command
        
        executable = parts[0]
        
        # Already absolute
        if executable.startswith("/"):
            return command
        
        # Try to find the absolute path
        abs_path = shutil.which(executable)
        if abs_path:
            parts[0] = abs_path
            return " ".join(parts)
        
        # Fallback: return original (systemd will fail, but error will be clearer)
        return command
    
    def generate_prisma(self) -> bool:
        """
        Generate Prisma client if Prisma is detected.
        
        Returns:
            True if successful or not needed.
        """
        if not self.has_prisma:
            return True
        
        self.logger.substep("Generating Prisma client")
        
        # Run prisma generate
        command = self._get_pm_exec_command("prisma generate")
        result = self._run(command, timeout=120)
        
        if not result.success:
            self.logger.warning(f"Prisma generate failed: {result.stderr}")
            # Don't fail the whole deployment for this
            return True
        
        return True
    
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
        
        self.logger.substep("Running Prisma migrations")
        
        if deploy:
            command = self._get_pm_exec_command("prisma migrate deploy")
        else:
            command = self._get_pm_exec_command("prisma migrate dev")
        
        result = self._run(command, timeout=300)
        
        if not result.success:
            self.logger.warning(f"Prisma migrate failed: {result.stderr}")
            # Continue anyway - migrations might already be applied
            return True
        
        return True
    
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
        
        return True
    
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
        
        return True
    
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
            except URLError:
                pass
            except Exception:
                pass
            
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
        
        # Track if SSL was successfully obtained
        ssl_obtained = False
        
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
            
            # Health check
            if self.health_check():
                self.logger.success(f"Application deployed successfully!")
                self.logger.blank()
                protocol = "https" if ssl_obtained else "http"
                self.logger.key_value("URL", f"{protocol}://{self.domain}")
                self.logger.key_value("Service", self.app_name)
                self.logger.key_value("Port", str(self.port))
                if self.ssl and not ssl_obtained:
                    self.logger.blank()
                    self.logger.warning("SSL was requested but could not be obtained.")
                    self.logger.info("To add SSL later, run: wasm cert create -d " + self.domain)
                return True
            else:
                self.logger.warning("Application started but health check failed")
                return True  # Still consider it successful
                
        except Exception as e:
            self.logger.error(f"Deployment failed: {e}")
            raise
