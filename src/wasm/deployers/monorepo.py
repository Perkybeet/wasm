# Copyright (c) 2024-2025 Yago Lopez Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
Monorepo deployer for WASM.

Handles deployment of Turborepo/pnpm workspace monorepos with multiple
applications, shared databases, and unified build processes.
"""

import json
import secrets
import shutil
import sqlite3
import string
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, ClassVar

from wasm.core.config import Config
from wasm.core.exceptions import (
    BuildError,
    DeploymentError,
    OutOfMemoryError,
    ServiceError,
    WASMError,
)
from wasm.core.fs import SECRET_MODE, FileSystem
from wasm.core.logger import Icons, Logger
from wasm.core.runner import CommandResult, CommandRunner, get_runner
from wasm.core.store import (
    App,
    AppStatus,
    Database,
    DatabaseEngine,
    MonorepoWorkspace,
    Service,
    Site,
    get_store,
)
from wasm.core.utils import domain_to_app_name
from wasm.deployers.helpers import (
    PackageManagerHelper,
    PathResolver,
    PrismaHelper,
    TurboHelper,
    WorkspaceHelper,
)
from wasm.deployers.interface import AppDeployer, StepReporter, UpdateResult
from wasm.deployers.registry import DeployerRegistry
from wasm.managers.apache_manager import ApacheManager
from wasm.managers.cert_manager import CertManager
from wasm.managers.nginx_manager import NginxManager
from wasm.managers.service_manager import ServiceManager
from wasm.managers.source_manager import SourceManager

#: Installs and builds in a monorepo touch every workspace; they need minutes,
#: but they still need a deadline.
INSTALL_TIMEOUT = 1800

#: Everything else here is a quick local command.
COMMAND_TIMEOUT = 300


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


class MonorepoDeployer(AppDeployer):
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
    DETECTION_FILES: ClassVar[list[str]] = [
        "turbo.json",
        "pnpm-workspace.yaml",
    ]

    # A workspace with several deployable apps outranks every single-app signal
    # inside it. See DEFAULT_DETECTION_PRIORITY in interface.py.
    DETECTION_PRIORITY = 90

    # Default ports
    DEFAULT_BASE_PORT = 3000
    DEFAULT_PORT = 3000

    def __init__(
        self,
        verbose: bool = False,
        runner: CommandRunner | None = None,
        fs: FileSystem | None = None,
    ):
        """
        Initialize the monorepo deployer.

        Args:
            verbose: Enable verbose logging.
            runner: Command runner used for installs and builds. Defaults to the
                process-wide runner, which is what enforces --dry-run.
            fs: Filesystem every change goes through. Defaults to the
                process-wide one, for the same reason.
        """
        self.verbose = verbose
        self._runner = runner
        self._fs = fs
        self.config = Config()
        self.logger = Logger(verbose=verbose)
        self.store = get_store()

        # Managers are built on first use: detection instantiates every
        # registered deployer just to ask "is this yours?".
        self._source_manager: SourceManager | None = None
        self._service_manager: ServiceManager | None = None
        self._cert_manager: CertManager | None = None

        # Helpers
        self._pm_helper = PackageManagerHelper(logger=self.logger, runner=self.runner)
        self._path_resolver = PathResolver(logger=self.logger)
        self._workspace_helper = WorkspaceHelper(logger=self.logger)
        self._turbo_helper = TurboHelper(logger=self.logger)
        self._prisma_helper: PrismaHelper | None = None

        # Deployment configuration. Empty rather than None until configure()
        # runs: every value here is handed to a manager that requires one.
        self.domain: str = ""
        self.source: str = ""
        self.app_path: Path = Path()
        self.app_name: str = ""
        self.webserver: str = "nginx"
        self.ssl: bool = True
        self.branch: str | None = None
        self.env_vars: dict[str, str] = {}

        # Workspace configuration
        self.workspaces: list[MonorepoWorkspace] = []
        self.subdomain_overrides: dict[str, str] = {}
        self.workspace_filter: list[str] | None = None

        # Database configuration
        self.databases: dict[str, DatabaseConfig] = {}
        self.skip_database: bool = False

        # Package manager
        self.package_manager: str = "pnpm"

        # Rollback tracking
        self._created_services: list[str] = []
        self._created_sites: list[str] = []
        self._is_new_deployment: bool = True

    @property
    def runner(self) -> CommandRunner:
        """The command runner this deployer executes through."""
        return self._runner if self._runner is not None else get_runner()

    @property
    def source_manager(self) -> SourceManager:
        """The manager that fetches source code."""
        if self._source_manager is None:
            self._source_manager = SourceManager(verbose=self.verbose, fs=self._fs)
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
            domain: Target domain (e.g., example.com).
            source: Source URL or path.
            port: Base port. Workspaces are assigned consecutive ports from it.
            webserver: Web server to use (nginx/apache).
            ssl: Enable SSL.
            branch: Git branch.
            env_vars: Global environment variables.
            app_path: Custom application path.
            package_manager: Ignored; a turbo/pnpm workspace uses pnpm.
            include_www: Ignored; workspaces are served on their own subdomains.
            **options: ``subdomain_overrides`` maps workspace names to
                subdomains, ``workspace_filter`` limits which workspaces deploy,
                and ``skip_database`` disables provisioning.
        """
        self.domain = domain
        self.source = source
        self.base_port = port or self.DEFAULT_BASE_PORT
        self.webserver = webserver
        self.ssl = ssl
        self.branch = branch
        self.env_vars = env_vars or {}
        self.subdomain_overrides = options.get("subdomain_overrides") or {}
        self.workspace_filter = options.get("workspace_filter")
        self.skip_database = bool(options.get("skip_database", False))

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
            1 for d in apps_dir.iterdir() if d.is_dir() and (d / "package.json").exists()
        )

        return app_count >= 2

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
        Execute a command inside the monorepo.

        Args:
            command: Program and arguments.
            cwd: Working directory. Defaults to the monorepo root.
            env: Extra environment, merged over the configured env_vars.
            timeout: Deadline in seconds.
            stream: Report output line by line while it runs, for installs and
                builds that would otherwise look frozen for minutes.

        Returns:
            The command outcome.
        """
        self.logger.debug(f"Running: {' '.join(command)}")

        run_env = dict(self.env_vars)
        if env:
            run_env.update(env)

        if stream:
            return self.runner.stream(
                command,
                on_line=self.logger.debug,
                cwd=cwd or self.app_path,
                env=run_env or None,
                timeout=timeout,
            )
        return self.runner.run(
            command,
            cwd=cwd or self.app_path,
            env=run_env or None,
            timeout=timeout,
        )

    def _generate_password(self, length: int = 32) -> str:
        """Generate a secure random password."""
        alphabet = string.ascii_letters + string.digits
        return "".join(secrets.choice(alphabet) for _ in range(length))

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
                except (CertificateError, WASMError) as e:
                    # No certificate still leaves every workspace reachable over
                    # HTTP; that is not worth throwing the build away for.
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
                # Rollback is the last line of defence; whatever it hits, the
                # original failure is what the caller must see.
                except Exception as rollback_error:
                    self.logger.debug(f"Rollback error: {rollback_error}")
                    self.logger.warning("Rollback had some errors. Manual cleanup may be needed.")

            raise

    def update(self, on_step: StepReporter | None = None) -> UpdateResult:
        """
        Rebuild every workspace of a monorepo that is already deployed.

        The CLI used to do this itself: it built a deployer, assigned
        ``app_path``, ``app_name``, ``domain`` and ``package_manager`` by hand
        and then called four private methods in order. That made the update a
        second pipeline that no test could reach and that drifted from
        :meth:`deploy` every time either side changed.

        Restarting the services is deliberately not part of this: which units
        exist is a store question, and the caller already owns it.

        Args:
            on_step: Called as each step begins.

        Returns:
            What was done, for the caller to present.

        Raises:
            DeploymentError: When the deployer was not configured, or when
                dependency installation fails.
            BuildError: When the build fails.
        """
        if not self.app_path or self.app_path == Path():
            raise DeploymentError(
                "Deployer was not configured",
                details="Call configure(domain=..., source=...) before update().",
            )

        report = on_step or (lambda _message: None)

        if not self.workspaces:
            report("Discovering workspaces")
            try:
                self._discover_workspaces()
            except DeploymentError as e:
                # Only the build timeout estimate depends on the count, so a
                # layout this cannot read must not block a rebuild.
                self.logger.debug(f"Could not enumerate workspaces: {e}")

        report("Installing dependencies")
        self._install_dependencies()

        report("Running database migrations")
        prisma_updated = self._run_prisma_migrations()

        report("Building applications")
        self._set_permissions()
        self._build_all()

        return UpdateResult(
            package_manager=self.package_manager,
            prisma_updated=prisma_updated,
            # Every workspace of a monorepo runs as its own unit; there is
            # nothing static about it.
            is_static=False,
            start_command="",
        )

    def _pre_flight_check(self) -> None:
        """Perform pre-deployment validation."""
        issues = []

        # Check pnpm is installed
        if not self.runner.exists("pnpm"):
            issues.append("pnpm is not installed. Install it with: npm install -g pnpm")

        # Check git for git sources
        if self.source and (
            self.source.startswith("git@")
            or self.source.startswith("https://")
            or self.source.endswith(".git")
        ):
            if not self.runner.exists("git"):
                issues.append("git is not installed")
            else:
                result = self.runner.run(
                    ["git", "ls-remote", "--exit-code", self.source], timeout=30
                )
                if not result.success:
                    issues.append(f"Repository not accessible: {self.source}")

        # Check disk space
        apps_dir = self.config.apps_directory
        if apps_dir.exists():
            usage = shutil.disk_usage(apps_dir)
            free_gb = usage.free / (1024**3)
            if free_gb < 2:
                issues.append(f"Low disk space: {free_gb:.1f}GB free (recommend 2GB+)")

        if issues:
            raise DeploymentError(
                "Pre-flight checks failed", details="\n".join(f"  - {issue}" for issue in issues)
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
            self.workspaces = [ws for ws in self.workspaces if ws.name in self.workspace_filter]

        if not self.workspaces:
            raise DeploymentError(
                "No deployable workspaces found",
                details="Check that apps/ directory contains valid applications",
            )

        for ws in self.workspaces:
            self.logger.substep(
                f"{ws.name} ({ws.app_type}) -> {ws.subdomain}.{self.domain}:{ws.port}"
            )

    def _detect_database_requirements(self) -> dict[str, DatabaseConfig]:
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
                for svc_config in services.values():
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
            except (yaml.YAMLError, OSError, AttributeError) as e:
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
            except OSError as e:
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
            except WASMError as e:
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

            # Grant privileges. This covers the public schema too, which is what
            # Prisma migrations need; the previous code reached past the manager
            # into its private _execute_sql to interpolate a user name straight
            # into GRANT statements.
            try:
                manager.grant_privileges(
                    database=db_config.name,
                    username=db_config.user,
                )
            except WASMError as e:
                self.logger.debug(f"Grant on {db_config.name} reported: {e}")

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
                except (WASMError, sqlite3.Error) as e:
                    self.logger.debug(f"Database row already present: {e}")

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

    def _write_env_file(self, path: Path, env_vars: dict[str, str]) -> None:
        """
        Write environment variables to a file.

        Args:
            path: File to write.
            env_vars: Variables to record, one per line.
        """
        lines = []
        for key, value in sorted(env_vars.items()):
            # Don't quote values for systemd compatibility
            lines.append(f"{key}={value}")
        # SECRET_MODE: this file holds DATABASE_URL with the password this
        # deployer just generated, plus whatever else .env.example asked for.
        self.fs.write_text(path, "\n".join(lines) + "\n", mode=SECRET_MODE)

    def _install_dependencies(self) -> None:
        """Install dependencies using pnpm."""
        # Verify pnpm
        # Not negotiable: a turbo/pnpm workspace declares its internal
        # dependencies with pnpm's workspace protocol, which npm cannot
        # resolve at all.
        self.package_manager = self._pm_helper.verify("pnpm", negotiable=False)

        result = self._run(
            ["pnpm", "install", "--frozen-lockfile"],
            timeout=600,
        )

        if not result.success:
            raise DeploymentError(
                "Failed to install dependencies", details=result.stderr or result.stdout
            )

    def _run_prisma_migrations(self) -> bool:
        """
        Run Prisma migrations if detected.

        Returns:
            True when a Prisma client was generated or a migration ran, so an
            update can report whether the database was touched.
        """
        # Check for project scripts first (preferred method)
        package_json = self.app_path / "package.json"
        has_db_scripts = False

        if package_json.exists():
            try:
                data = json.loads(package_json.read_text())
                scripts = data.get("scripts", {})
                has_db_scripts = "db:generate" in scripts or "db:migrate" in scripts
            except (json.JSONDecodeError, OSError, AttributeError) as e:
                self.logger.debug(f"Could not read {package_json}: {e}")

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

            return True

        # Fallback: Check for Prisma schema directly
        prisma_dirs = [
            self.app_path / "packages" / "database" / "prisma",
            self.app_path / "prisma",
        ]

        for prisma_dir in prisma_dirs:
            schema_file = prisma_dir / "schema.prisma"
            if schema_file.exists():
                self.logger.substep(
                    f"Found Prisma schema: {schema_file.relative_to(self.app_path)}"
                )

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
                        [
                            "pnpm",
                            "exec",
                            "prisma",
                            "migrate",
                            "deploy",
                            "--schema",
                            str(schema_file),
                        ],
                    )
                    if not result.success:
                        self.logger.warning(f"Prisma migrate failed: {result.stderr}")
                else:
                    self.logger.substep("No migrations to run")

                return True

        self.logger.substep("No Prisma schema found")
        return False

    def _set_permissions(self) -> None:
        """Set correct ownership and permissions for the app directory."""
        service_user = self.config.service_user
        service_group = self.config.service_group

        try:
            # Change ownership recursively
            result = self.runner.run(
                ["chown", "-R", f"{service_user}:{service_group}", str(self.app_path)],
                timeout=60,
            )
            if not result.success:
                self.logger.debug(f"chown failed: {result.stderr}")

            # Ensure directories are executable and writable
            self.runner.run(
                ["chmod", "-R", "u+rwX,g+rX,o+rX", str(self.app_path)],
                timeout=60,
            )
        except OSError as e:
            self.logger.debug(f"Failed to set permissions: {e}")

        # That -R also put o+r on the .env files written a step earlier, which
        # hold the database password this deployer generated. The chown above
        # has just made the service account their owner, so 0600 is readable by
        # the application and by nobody else.
        for env_file in self._env_files():
            if env_file.exists():
                self.fs.chmod(env_file, SECRET_MODE)

    def _env_files(self) -> list[Path]:
        """
        List the environment files this deployment writes.

        Returns:
            The per-workspace files, and the root one Prisma reads.
        """
        files = [self.app_path / ws.path / ".env.production" for ws in self.workspaces]
        files.append(self.app_path / ".env")
        return files

    def _build_all(self) -> None:
        """Build all applications using Turborepo."""
        build_timeout = self._turbo_helper.estimate_build_timeout(
            self.app_path, len(self.workspaces)
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
                    ),
                )
            raise BuildError("Build failed", details=result.stderr or result.stdout)

    def _webserver_manager(self) -> NginxManager | ApacheManager:
        """
        Return the manager for the configured web server.

        Returns:
            An NginxManager or an ApacheManager.
        """
        if self.webserver == "nginx":
            return NginxManager(verbose=self.verbose)
        return ApacheManager(verbose=self.verbose)

    def _create_sites(self, with_ssl: bool = False) -> None:
        """
        Create nginx/apache site configurations for all workspaces.

        Args:
            with_ssl: Render the configurations with TLS enabled.
        """
        manager = self._webserver_manager()
        if isinstance(manager, NginxManager):
            self._create_nginx_sites(manager, with_ssl)
        else:
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

        self._install_nginx_site(config_content)

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
            lines.extend(
                [
                    f"upstream {upstream_name}_backend {{",
                    f"    server 127.0.0.1:{ws.port};",
                    "}",
                    "",
                ]
            )

        # HTTP server for ACME challenges
        server_names = f"{self.domain} " + " ".join(
            f"{ws.subdomain}.{self.domain}" for ws in self.workspaces
        )
        lines.extend(
            [
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
            ]
        )

        if with_ssl:
            lines.extend(
                [
                    "    location / {",
                    "        return 301 https://$host$request_uri;",
                    "    }",
                    "}",
                    "",
                ]
            )

            # HTTPS servers for each workspace
            for ws in self.workspaces:
                upstream_name = ws.name.replace("-", "_")
                lines.extend(
                    [
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
                    ]
                )
        else:
            # HTTP-only configuration
            for ws in self.workspaces:
                upstream_name = ws.name.replace("-", "_")
                lines.extend(
                    [
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
                    ]
                )

            lines.append("}")

        self._install_nginx_site("\n".join(lines))

        for ws in self.workspaces:
            self._register_site_in_store(ws, with_ssl)

    def _install_nginx_site(self, config_content: str) -> None:
        """
        Write the site configuration and enable it.

        Args:
            config_content: The rendered nginx configuration.
        """
        from wasm.core.config import NGINX_SITES_AVAILABLE, NGINX_SITES_ENABLED

        config_file = NGINX_SITES_AVAILABLE / self.domain
        self.fs.write_text(config_file, config_content)
        # Linking unconditionally: the previous "if not exists" skipped a
        # dangling link, which left the site disabled with no way to notice.
        self.fs.symlink(config_file, NGINX_SITES_ENABLED / self.domain)

        self._created_sites.append(self.domain)

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
            ssl_certificate=f"/etc/letsencrypt/live/{self.domain}/fullchain.pem"
            if with_ssl
            else None,
            ssl_key=f"/etc/letsencrypt/live/{self.domain}/privkey.pem" if with_ssl else None,
        )

        if existing_site:
            self.store.update_site(site)
        else:
            self.store.create_site(site)

    def _obtain_certificate(self) -> None:
        """Obtain SSL certificate for all subdomains."""
        domains = [self.domain] + [f"{ws.subdomain}.{self.domain}" for ws in self.workspaces]

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
        env: dict[str, str],
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
            except WASMError as e:
                raise ServiceError(f"Failed to start {service_name}", details=str(e)) from e

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
        workspaces_json = json.dumps(
            [
                {
                    "name": ws.name,
                    "path": ws.path,
                    "app_type": ws.app_type,
                    "subdomain": ws.subdomain,
                    "port": ws.port,
                }
                for ws in self.workspaces
            ]
        )

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
            except (WASMError, OSError) as e:
                errors.append(f"Service cleanup error: {e}")

        # Remove site configuration
        try:
            manager = self._webserver_manager()

            if manager.site_exists(self.domain):
                manager.disable_site(self.domain)
                manager.delete_site(self.domain)
                manager.reload()
        except (WASMError, OSError) as e:
            errors.append(f"Site cleanup error: {e}")

        # Remove files
        if self.app_path and self.app_path.exists():
            try:
                self.fs.remove_tree(self.app_path)
            except OSError as e:
                errors.append(f"File cleanup error: {e}")

        # Clean store records
        try:
            # The store deletes by natural key. Passing row ids matched nothing,
            # so every failed monorepo deployment left its rows behind.
            for service_name in self._created_services:
                service = self.store.get_service(service_name)
                if service:
                    self.store.delete_service(service.name)

            for ws in self.workspaces:
                site = self.store.get_site(f"{ws.subdomain}.{self.domain}")
                if site:
                    self.store.delete_site(site.domain)

            app = self.store.get_app(self.domain)
            if app:
                self.store.delete_app(app.domain)
        except (WASMError, sqlite3.Error) as e:
            errors.append(f"Store cleanup error: {e}")

        if errors:
            self.logger.debug(f"Rollback errors: {errors}")


DeployerRegistry.register(MonorepoDeployer)
