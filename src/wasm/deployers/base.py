# Copyright (c) 2024-2025 Yago López Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
Base deployer class for WASM.

Defines the interface and common functionality for all deployers.

``deploy()`` is a pipeline description, not an implementation: each step names
the manager that does the work and the undo that reverses it. Everything that
belongs to a manager (nginx, systemd, certbot) or to the store lives there, not
here.
"""

from __future__ import annotations

import shutil
from abc import abstractmethod
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, ClassVar, Literal

from wasm.core.config import Config
from wasm.core.exceptions import BuildError, DeploymentError, OutOfMemoryError, WASMError
from wasm.core.logger import Icons, Logger
from wasm.core.runner import CommandResult, CommandRunner, get_runner
from wasm.core.store import (
    App,
    AppStatus,
    get_store,
)
from wasm.core.utils import domain_to_app_name
from wasm.deployers.helpers import (
    EnvManager,
    NginxConfigBuilder,
    PackageManagerHelper,
    PathResolver,
    PrismaHelper,
    preflight,
)
from wasm.deployers.helpers.health import failure_output, wait_until_healthy
from wasm.deployers.helpers.nginx_config import NginxAdvancedConfig
from wasm.deployers.helpers.registration import StoreRegistrar
from wasm.deployers.helpers.summary import print_deployment_summary
from wasm.deployers.interface import AppDeployer, StepReporter, UpdateResult
from wasm.deployers.pipeline import DeployStep, run_pipeline
from wasm.managers.apache_manager import ApacheManager
from wasm.managers.cert_manager import CertManager
from wasm.managers.nginx_manager import NginxManager
from wasm.managers.service_manager import ServiceManager
from wasm.managers.source_manager import SourceManager
from wasm.validators.environment import validate_environment, validate_unit_value

# Type for package managers
PackageManager = Literal["npm", "pnpm", "bun", "yarn", "auto"]

#: Deadlines for the two commands that legitimately take minutes. Generous, but
#: finite: an npm install that wedges on a private registry must eventually fail
#: the deploy instead of holding the terminal forever.
INSTALL_TIMEOUT = 1800
BUILD_TIMEOUT = 2700

#: Everything else in a deployer is a quick local command.
COMMAND_TIMEOUT = 300


class BaseDeployer(AppDeployer):
    """
    Abstract base class for application deployers.

    Each deployer handles the deployment workflow for a specific
    type of application (Next.js, Node.js, Python, etc.).
    """

    # Deployer identification
    APP_TYPE: str = "base"
    DISPLAY_NAME: str = "Base Application"

    # Files used to detect this app type
    DETECTION_FILES: ClassVar[list[str]] = []
    DETECTION_PATTERNS: ClassVar[list[str]] = []

    # Default port
    DEFAULT_PORT: int = 3000

    # System dependencies
    SYSTEM_DEPS: ClassVar[list[str]] = []

    def __init__(self, verbose: bool = False, runner: CommandRunner | None = None):
        """
        Initialize the deployer.

        Args:
            verbose: Enable verbose logging.
            runner: Command runner used for install and build commands. Defaults
                to the process-wide runner, which is what enforces --dry-run.
        """
        self.verbose = verbose
        self.config = Config()
        self.logger = Logger(verbose=verbose)
        self.store = get_store()
        self._runner = runner

        # Managers are built on first use. Detection instantiates every
        # registered deployer just to ask "is this yours?", and constructing
        # four managers and a store connection to answer no is waste that also
        # made a deployer impossible to create without a working system.
        self._source_manager: SourceManager | None = None
        self._service_manager: ServiceManager | None = None
        self._cert_manager: CertManager | None = None
        self._registrar: StoreRegistrar | None = None

        # Helpers
        self._pm_helper = PackageManagerHelper(logger=self.logger)
        self._path_resolver = PathResolver(logger=self.logger)
        self._prisma_helper: PrismaHelper | None = None  # Initialized after app_path is set

        # Deployment configuration. These are empty, not None, until configure()
        # runs: every one of them is fed straight to a manager that requires a
        # value, and "Optional everywhere" only moved the check to twenty call
        # sites that did not make it.
        self.domain: str = ""
        self.source: str = ""
        self.port: int = self.DEFAULT_PORT
        self.app_path: Path = Path()
        self.app_name: str = ""
        self.webserver: str = "nginx"
        self.ssl: bool = True
        self.include_www: bool = False
        self.branch: str | None = None
        self.env_vars: dict[str, str] = {}

        # Package manager (auto = auto-detect)
        self._package_manager: PackageManager = "auto"
        self.package_manager: str = "npm"  # Resolved package manager

        # Prisma support
        self.has_prisma: bool = False

        # Deployment progress, shared between pipeline steps
        self._ssl_obtained: bool = False
        self._app_record: App | None = None

        # Advanced nginx config (detected from wasm.nginx.yaml)
        self._nginx_config_builder = NginxConfigBuilder(verbose=verbose)
        self._nginx_advanced_config: NginxAdvancedConfig | None = None

        # Env manager
        self._env_manager = EnvManager(verbose=verbose)

    @property
    def runner(self) -> CommandRunner:
        """The command runner this deployer executes through."""
        return self._runner if self._runner is not None else get_runner()

    @property
    def source_manager(self) -> SourceManager:
        """The manager that fetches source code."""
        if self._source_manager is None:
            self._source_manager = SourceManager(verbose=self.verbose)
        return self._source_manager

    @source_manager.setter
    def source_manager(self, manager: SourceManager) -> None:
        """
        Replace the source manager.

        Args:
            manager: The manager to use instead of the default.
        """
        self._source_manager = manager

    @property
    def service_manager(self) -> ServiceManager:
        """The manager that writes and drives systemd units."""
        if self._service_manager is None:
            self._service_manager = ServiceManager(verbose=self.verbose)
        return self._service_manager

    @service_manager.setter
    def service_manager(self, manager: ServiceManager) -> None:
        """
        Replace the service manager.

        Args:
            manager: The manager to use instead of the default.
        """
        self._service_manager = manager

    @property
    def cert_manager(self) -> CertManager:
        """The manager that obtains certificates."""
        if self._cert_manager is None:
            self._cert_manager = CertManager(verbose=self.verbose)
        return self._cert_manager

    @cert_manager.setter
    def cert_manager(self, manager: CertManager) -> None:
        """
        Replace the certificate manager.

        Args:
            manager: The manager to use instead of the default.
        """
        self._cert_manager = manager

    @property
    def registrar(self) -> StoreRegistrar:
        """The component that writes app, site and service rows."""
        if self._registrar is None:
            self._registrar = StoreRegistrar(self.store)
        return self._registrar

    def configure(
        self,
        domain: str,
        source: str,
        *,
        port: int | None = None,
        webserver: str = "nginx",
        ssl: bool = True,
        branch: str | None = None,
        env_vars: dict[str, str] | None = None,
        app_path: Path | None = None,
        package_manager: str = "auto",
        include_www: bool = False,
        **options: Any,
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
            include_www: Include www subdomain in certificate and web server config.
            **options: Accepted and ignored, so a caller can pass the union of
                every deployer's settings without knowing which one it got.
        """
        from wasm.validators.domain import should_include_www

        self.domain = domain
        self.source = source
        self.port = port or self.DEFAULT_PORT
        self.webserver = webserver
        self.ssl = ssl
        self.include_www = include_www and should_include_www(domain)
        self.branch = branch
        self.env_vars = env_vars or {}
        self._package_manager = package_manager  # type: ignore[assignment]

        # Set app name and path
        self.app_name = domain_to_app_name(domain)
        self.app_path = app_path or (self.config.apps_directory / self.app_name)

    def _run(
        self,
        command: Sequence[str],
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        timeout: int = COMMAND_TIMEOUT,
        *,
        stream: bool = False,
    ) -> CommandResult:
        """
        Execute a command in the application directory.

        Args:
            command: Program and arguments.
            cwd: Working directory. Defaults to the application directory.
            env: Extra environment, merged over the configured env_vars.
            timeout: Deadline in seconds.
            stream: Print output line by line while the command runs. Used for
                installs and builds, which otherwise look frozen for minutes.

        Returns:
            The command outcome.
        """
        self.logger.debug(f"Running: {' '.join(command)}")

        run_env = dict(self.env_vars)
        if env:
            run_env.update(env)

        if stream:
            result = self.runner.stream(
                command,
                on_line=self.logger.debug,
                cwd=cwd or self.app_path,
                env=run_env or None,
                timeout=timeout,
            )
        else:
            result = self.runner.run(
                command,
                cwd=cwd or self.app_path,
                env=run_env or None,
                timeout=timeout,
            )
            self.logger.command_output(result.stdout, result.stderr)
        return result

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

    def _get_pm_install_command(self) -> list[str]:
        """
        Get the package manager install command.

        Returns:
            Install command as list.
        """
        return self._pm_helper.get_install_command(self.package_manager)

    def _get_pm_run_command(self, script: str) -> list[str]:
        """
        Get the package manager run command.

        Args:
            script: Script name to run.

        Returns:
            Run command as list.
        """
        return self._pm_helper.get_run_command(self.package_manager, script)

    def _get_pm_exec_command(self, command: str) -> list[str]:
        """
        Get the package manager exec/dlx command.

        Args:
            command: Command to execute.

        Returns:
            Exec command as list.
        """
        return self._pm_helper.get_exec_command(self.package_manager, command)

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
    def get_install_command(self) -> list[str]:
        """
        Get the command to install dependencies.

        Returns:
            Command as list of arguments.
        """
        pass

    @abstractmethod
    def get_build_command(self) -> list[str]:
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

        Returns "advanced" if a wasm.nginx.yaml config was detected,
        otherwise returns the default "proxy" template.

        Returns:
            Template name.
        """
        if self._nginx_advanced_config is not None:
            return "advanced"
        return "proxy"

    def get_apache_template(self) -> str:
        """
        Get the Apache template name for this app type.

        Returns:
            Template name.
        """
        return "proxy"

    def get_template_context(self) -> dict:
        """
        Get template context for configuration files.

        If an advanced nginx config is detected, returns the
        context built by NginxConfigBuilder instead.

        Returns:
            Context dictionary.
        """
        server_names = self.domain
        if self.include_www:
            server_names = f"{self.domain} www.{self.domain}"

        if self._nginx_advanced_config is not None:
            ctx = self._nginx_config_builder.build_context(
                self._nginx_advanced_config,
                self.domain,
                ssl=self.ssl,
                app_path=str(self.app_path),
            )
            ctx["server_names"] = server_names
            return ctx

        return {
            "domain": self.domain,
            "server_names": server_names,
            "port": self.port,
            "app_path": str(self.app_path),
            "app_name": self.app_name,
            "ssl": self.ssl,
            "health_check": self.get_health_check(),
        }

    def _detect_nginx_config(self) -> None:
        """
        Detect and parse wasm.nginx.yaml if present.

        Sets self._nginx_advanced_config if a valid config file is found.
        """
        if not self.app_path or not self.app_path.exists():
            return

        config_path = self._nginx_config_builder.detect(self.app_path)
        if config_path:
            self.logger.debug(f"Found advanced nginx config: {config_path}")
            self._nginx_advanced_config = self._nginx_config_builder.parse(config_path)

            errors = self._nginx_config_builder.validate(self._nginx_advanced_config)
            if errors:
                self.logger.warning(f"Nginx config validation errors: {', '.join(errors)}")
                self._nginx_advanced_config = None

    def _should_configure_env(self) -> bool:
        """
        Check if automatic env configuration should run.

        Returns True if .env.example exists and no --env-file was provided.

        Returns:
            True if env configuration should be performed.
        """
        if not self.app_path or not self.app_path.exists():
            return False

        # Skip if user already provided env vars
        if self.env_vars:
            return False

        # Check for .env.example
        return (self.app_path / ".env.example").exists()

    def _configure_env(self) -> None:
        """
        Auto-configure environment variables using EnvManager.

        Discovers variables from .env.example, fills them
        non-interactively (defaults + auto-generated secrets),
        and writes the .env file.
        """
        variables = self._env_manager.discover(self.app_path)
        if not variables:
            return

        self.logger.debug(f"Discovered {len(variables)} env variables")

        # Use non-interactive mode (defaults + auto-generated secrets)
        values = self._env_manager.prompt_non_interactive(variables)

        # Merge with any existing env vars (CLI-provided take precedence)
        for key, val in self.env_vars.items():
            values[key] = val

        # Write .env file
        self._env_manager.write_env_files(self.app_path, values)
        self.logger.substep("Created .env from .env.example")

        # Update env_vars so they're available for systemd
        self.env_vars.update(values)

    def check_dependencies(self) -> bool:
        """
        Check if system dependencies are installed.

        Returns:
            True if all dependencies are available.
        """
        for dep in self.SYSTEM_DEPS:
            if not self.runner.exists(dep):
                self.logger.warning(f"Missing dependency: {dep}")
                return False

        return True

    def _webserver_manager(self) -> NginxManager | ApacheManager:
        """
        Return the manager for the configured web server.

        Returns:
            An NginxManager or an ApacheManager.
        """
        if self.webserver == "nginx":
            return NginxManager(verbose=self.verbose)
        return ApacheManager(verbose=self.verbose)

    def pre_flight_check(self) -> bool:
        """
        Validate the machine before anything is changed.

        Every check runs, so one command reports every problem instead of the
        first one. The checks themselves live in
        :mod:`wasm.deployers.helpers.preflight`.

        Returns:
            True if all checks pass.

        Raises:
            DeploymentError: If any check fails, listing all of them.
        """
        self.logger.debug("Running pre-flight checks...")

        existing_app = self.store.get_app(self.domain) if self.domain else None
        issues: list[str] = []

        missing = preflight.missing_programs(self.runner, self.SYSTEM_DEPS)
        if missing:
            issues.append(f"Missing system dependencies: {', '.join(missing)}")

        if self.source:
            issues += preflight.repository_unreachable(self.runner, self.source)

        issues += preflight.insufficient_disk_space(self.config.apps_directory)

        if self.port:
            issues += preflight.port_taken(
                self.port,
                allowed_owner_port=existing_app.port if existing_app else None,
            )

        issues += preflight.webserver_down(self._webserver_manager(), self.webserver)

        if issues:
            details = "\n".join(f"  - {issue}" for issue in issues)
            raise DeploymentError(
                "Pre-flight checks failed", details=f"The following issues were found:\n{details}"
            )

        self.logger.debug("All pre-flight checks passed")
        return True

    # Undo actions ---------------------------------------------------------
    #
    # Each of these reverses exactly one pipeline step. They are idempotent and
    # tolerate the resource never having been created, because a step can fail
    # halfway through its own work.

    def remove_source(self) -> None:
        """Delete the fetched application directory."""
        if self.app_path and self.app_path.exists():
            self.logger.debug(f"Removing app files: {self.app_path}")
            shutil.rmtree(self.app_path, ignore_errors=True)

    def remove_site(self) -> None:
        """Remove the web server site configuration for this domain."""
        if not self.domain:
            return
        manager = self._webserver_manager()
        if manager.site_exists(self.domain):
            self.logger.debug(f"Removing site config: {self.domain}")
            manager.disable_site(self.domain)
            manager.delete_site(self.domain)
            manager.reload()

    def remove_service(self) -> None:
        """Stop and delete the systemd service for this application."""
        if not self.app_name:
            return
        status = self.service_manager.get_status(self.app_name)
        if not status.get("exists"):
            return
        self.logger.debug(f"Removing service: {self.app_name}")
        try:
            self.service_manager.stop(self.app_name)
        except WASMError as e:
            self.logger.debug(f"Service was not running: {e}")
        self.service_manager.delete_service(self.app_name)

    def forget_records(self) -> None:
        """Delete the app, site and service rows this deployment created."""
        if self.domain:
            self.registrar.forget(domain=self.domain, service_name=self.app_name)

    def rollback(self, keep_files: bool = False) -> bool:
        """
        Undo everything a deployment may have created.

        The pipeline undoes only the steps that ran, which is what a failed
        deploy needs. This method is the blunt version, kept for callers that
        want to clean up after the fact.

        Args:
            keep_files: If True, preserve the application files.

        Returns:
            True if every cleanup action completed.
        """
        actions: list[tuple[str, Callable[[], None]]] = [
            ("service", self.remove_service),
            ("site", self.remove_site),
            ("store records", self.forget_records),
        ]
        if not keep_files:
            actions.append(("files", self.remove_source))

        errors = 0
        for what, action in actions:
            try:
                action()
            # Cleanup is an error boundary: one failure must not abort the rest.
            except Exception as e:
                errors += 1
                self.logger.debug(f"Rollback of {what} failed: {e}")

        return errors == 0

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
        if self.source_already_fetched:
            # AutoDeployer put the code here to detect the type; fetching again
            # with clean=True would delete it and clone it a second time.
            self.logger.substep(f"Source already present at {self.app_path}")
            return True

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

        result = self._run(command, timeout=INSTALL_TIMEOUT, stream=True)
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
                result = self._run(fallback_command, timeout=INSTALL_TIMEOUT, stream=True)

            if not result.success:
                error_output = failure_output(result)
                raise DeploymentError(
                    "Dependency installation failed",
                    details=error_output
                    or "No error output captured. Check if the package manager is properly installed.",
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

        result = self._run(command, timeout=BUILD_TIMEOUT, stream=True)
        if not result.success:
            error_output = failure_output(result)

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
        if not self.domain:
            raise DeploymentError(
                "Deployer was not configured",
                details="Call configure(domain=..., source=...) before deploy().",
            )

        context = self.get_template_context()
        # Override SSL setting based on parameter
        context["ssl"] = with_ssl

        manager = self._webserver_manager()
        template = (
            self.get_nginx_template() if self.webserver == "nginx" else self.get_apache_template()
        )

        self.logger.substep(f"Web server: {self.webserver}")
        self.logger.substep(f"Template: {template}")
        if self.ssl:
            self.logger.substep(f"SSL: {'enabled' if with_ssl else 'pending certificate'}")

        # Check if site already exists (update vs create)
        if manager.site_exists(self.domain):
            manager.update_site(self.domain, template=template, context=context)
        else:
            manager.create_site(self.domain, template=template, context=context)
            manager.enable_site(self.domain)

        manager.reload()

        self.registrar.register_site(
            domain=self.domain,
            webserver=self.webserver,
            template=template,
            app_path=self.app_path,
            port=self.port,
            with_ssl=with_ssl,
        )

        return True

    def create_service(self) -> bool:
        """
        Create systemd service.

        Returns:
            True if successful.

        Raises:
            EnvironmentValidationError: If an environment variable or a unit
                directive value cannot be written safely into the unit file.
        """
        if not self.domain or not self.app_name:
            raise DeploymentError(
                "Deployer was not configured",
                details="Call configure(domain=..., source=...) before deploy().",
            )

        start_command = self.get_start_command()

        # Resolve to absolute path for systemd compatibility
        start_command = self._resolve_absolute_path(start_command)

        self.logger.substep(f"Service: {self.app_name}")
        self.logger.substep(f"Command: {start_command}")

        # Build environment with PORT
        env = self.env_vars.copy()
        env["PORT"] = str(self.port)
        env["NODE_ENV"] = "production"

        # Everything below is interpolated into a systemd unit, where a newline
        # starts a new directive. env_vars arrives unfiltered from the CLI and
        # from POST /api/apps, so it is validated before it can reach the unit.
        env = validate_environment(env)
        start_command = validate_unit_value(start_command, field="ExecStart")
        working_directory = validate_unit_value(str(self.app_path), field="WorkingDirectory")
        description = validate_unit_value(
            f"WASM: {self.domain} ({self.APP_TYPE})", field="Description"
        )

        self.service_manager.create_service(
            name=self.app_name,
            command=start_command,
            working_directory=working_directory,
            environment=env,
            description=description,
        )

        # Enable service
        self.service_manager.enable(self.app_name)

        self.registrar.register_service(
            domain=self.domain,
            name=self.app_name,
            command=start_command,
            working_directory=self.app_path,
            environment=env,
            port=self.port,
            user=self.config.service_user,
            group=self.config.service_group,
        )

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

        additional_domains = None
        if self.include_www:
            www_domain = f"www.{self.domain}"
            additional_domains = [www_domain]
            self.logger.substep(f"Including: {www_domain}")

        # Use nginx plugin if using nginx
        nginx = self.webserver == "nginx"
        apache = self.webserver == "apache"

        self.cert_manager.obtain(
            self.domain,
            nginx=nginx,
            apache=apache,
            additional_domains=additional_domains,
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
        url = f"http://127.0.0.1:{self.port}{self.get_health_check()}"
        self.logger.substep(f"Checking: {url}")

        return wait_until_healthy(url, retries=retries, delay=delay, on_attempt=self.logger.debug)

    def build_pipeline(self) -> list[DeployStep]:
        """
        Describe the deployment as an ordered list of steps.

        Subclasses override this to add, drop or reorder steps instead of
        rewriting the whole ``deploy`` method, which is how static and vite
        deployments used to end up with their own copies of the workflow.

        Returns:
            The steps to execute, each with the undo that reverses it.
        """
        return [
            DeployStep(
                title="Fetching source code",
                icon=Icons.DOWNLOAD,
                run=self._step_fetch,
                undo=self.remove_source,
            ),
            DeployStep(
                title="Installing dependencies",
                icon=Icons.PACKAGE,
                run=self.install_dependencies,
            ),
            DeployStep(
                title="Building application",
                icon=Icons.BUILD,
                run=self.build,
            ),
            DeployStep(
                title="Creating site configuration",
                icon=Icons.GLOBE,
                run=lambda: self.create_site(with_ssl=False),
                undo=self.remove_site,
            ),
            DeployStep(
                title="Obtaining SSL certificate",
                icon=Icons.LOCK,
                run=self._step_certificate,
                skip_if=lambda: not self.ssl,
            ),
            DeployStep(
                title="Creating systemd service",
                icon=Icons.GEAR,
                run=self.create_service,
                undo=self.remove_service,
            ),
            DeployStep(
                title="Starting application",
                icon=Icons.ROCKET,
                run=self._step_start,
            ),
        ]

    def _step_fetch(self) -> None:
        """Fetch the source, then read the configuration that ships with it."""
        self.fetch_source()

        # Both of these describe the code that was just fetched, so they cannot
        # run any earlier.
        self._detect_nginx_config()
        if self._should_configure_env():
            self._configure_env()

    def _step_certificate(self) -> None:
        """
        Obtain a certificate and re-render the site with TLS enabled.

        A certificate failure is not a deployment failure: the application is
        still reachable over HTTP, and forcing a rollback here would throw away
        a working build because DNS had not propagated yet.
        """
        from wasm.core.exceptions import CertificateError

        try:
            self.obtain_certificate()
        except (CertificateError, WASMError) as e:
            self.logger.warning(f"SSL certificate failed: {e}")
            self.logger.warning("Continuing deployment without SSL...")
            self.logger.substep("Application will be available via HTTP only")
            return

        self._ssl_obtained = True
        self.logger.substep("Updating site configuration with SSL")
        self.create_site(with_ssl=True)

    def _step_start(self) -> None:
        """Start the service and record that the deployment succeeded."""
        from datetime import datetime

        self.start()

        if self._app_record is not None:
            self._app_record.status = AppStatus.RUNNING.value
            self._app_record.ssl_enabled = self._ssl_obtained
            self._app_record.deployed_at = datetime.now().isoformat()
            self.store.update_app(self._app_record)

        if self.app_name:
            self.store.update_service_status(self.app_name, active=True, enabled=True)

    def update(self, on_step: StepReporter | None = None) -> UpdateResult:
        """
        Rebuild this application in place, without a full redeploy.

        The sequence used to live in ``wasm.cli.commands.webapp``, which drove
        the deployer step by step and reached into ``_package_manager`` to do
        it. Keeping it here means the update path is the deployer's own, gets
        the same detection and error handling as a deploy, and can be tested.

        Args:
            on_step: Called as each step begins.

        Returns:
            What was done, for the caller to present.

        Raises:
            WASMError: When a step fails.
        """
        report = on_step or (lambda _message: None)

        report("Inspecting the project")
        self.pre_install()

        report("Installing dependencies")
        self.install_dependencies()

        prisma_updated = False
        if self.has_prisma:
            report("Updating Prisma")
            self.generate_prisma()
            self.run_prisma_migrate(deploy=True)
            prisma_updated = True

        report("Building")
        self.build()

        start_command = self.get_start_command()
        return UpdateResult(
            package_manager=self.package_manager,
            prisma_updated=prisma_updated,
            is_static=not bool(start_command),
            start_command=start_command,
        )

    def deploy(self, total_steps: int = 7) -> bool:
        """
        Run the full deployment workflow.

        The workflow itself is :meth:`build_pipeline`. This method only handles
        what surrounds it: validation, the store row that tracks progress, and
        reporting. A step that fails undoes every step that ran before it,
        including the app row, so a failed first deployment leaves nothing.

        Args:
            total_steps: Ignored. The pipeline knows how many steps it has;
                the parameter is kept so existing callers still work.

        Returns:
            True if the application ended up deployed.

        Raises:
            WASMError: Whatever the failing step raised, after the rollback.
        """
        if not self.domain:
            raise DeploymentError(
                "Deployer was not configured",
                details="Call configure(domain=..., source=...) before deploy().",
            )

        self._ssl_obtained = False

        self.logger.debug("Running pre-flight validation...")
        self.pre_flight_check()

        # A redeployment must not lose its app row just because this attempt
        # failed, so only a genuinely new app registers an undo for it.
        is_new_deployment = self.store.get_app(self.domain) is None
        self._app_record = self._register_app_in_store(AppStatus.DEPLOYING.value)

        steps = self.build_pipeline()
        if is_new_deployment:
            steps.insert(
                0,
                DeployStep(
                    title="Registering application",
                    icon=Icons.PACKAGE,
                    run=lambda: None,
                    undo=self.forget_records,
                ),
            )

        try:
            run_pipeline(steps, self.logger)
        except Exception as e:
            if not is_new_deployment and self._app_record is not None:
                # The rows survive a failed redeployment; mark them honestly.
                self._app_record.status = AppStatus.FAILED.value
                self.store.update_app(self._app_record)
            self.logger.error(f"Deployment failed: {e}")
            raise

        self._report_result()
        return True

    def _report_result(self) -> None:
        """Print the summary, plus troubleshooting hints when unhealthy."""
        healthy = self.health_check()
        print_deployment_summary(
            self.logger,
            domain=self.domain or "",
            app_name=self.app_name or "",
            port=self.port,
            app_path=self.app_path,
            ssl_requested=self.ssl,
            ssl_obtained=self._ssl_obtained,
        )
        if healthy:
            return

        self.logger.warning("Application started but health check failed")
        self.logger.blank()
        self.logger.info("Troubleshooting commands:")
        self.logger.info(f"  wasm logs {self.domain}        # View application logs")
        self.logger.info(f"  wasm status {self.domain}      # Check service status")

    def _register_app_in_store(self, status: str) -> App:
        """
        Register or update application in persistent store.

        Args:
            status: Initial app status.

        Returns:
            The created or updated App object.
        """
        return self.registrar.register_app(
            domain=self.domain or "",
            app_type=self.APP_TYPE,
            source=self.source or "",
            branch=self.branch,
            port=self.port,
            app_path=self.app_path,
            webserver=self.webserver,
            ssl_enabled=self.ssl,
            status=status,
            is_static=not bool(self.get_start_command()),
            env_vars=self.env_vars,
        )
