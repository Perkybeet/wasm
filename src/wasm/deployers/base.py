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

Three paths, because the release layout pulls apart what the in-place layout
kept in one directory:

- :attr:`BaseDeployer.app_path` is the application directory, the one the
  store records. In place it is also where the code is built and run.
- :attr:`BaseDeployer.build_path` is where the code being deployed is built:
  the application directory in place, the new release directory on releases.
  Everything that reads or builds the project uses it.
- :attr:`BaseDeployer.runtime_path` is what the unit and the web server are
  given: the application directory in place, ``current`` on releases, so the
  same unit and site serve every release that is activated after them.

In place, all three are the same directory, which is what keeps that layout
exactly as it was.
"""

from __future__ import annotations

import sqlite3
from abc import abstractmethod
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, ClassVar, Literal

from wasm.core.applock import app_lock
from wasm.core.config import Config
from wasm.core.exceptions import (
    BuildError,
    CertificateError,
    DeploymentError,
    OutOfMemoryError,
    ServiceError,
    ValidationError,
    WASMError,
)
from wasm.core.fs import SECRET_MODE, DryRunFileSystem, FileSystem
from wasm.core.logger import Icons
from wasm.core.runner import CommandResult, CommandRunner, get_runner
from wasm.core.store import (
    DEFAULT_KEEP_RELEASES,
    App,
    AppStatus,
    DeploymentTrigger,
    DomainKind,
    ReleaseRecord,
    ReleaseStatus,
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
from wasm.deployers.helpers.health_gate import HealthCheck, HealthGate
from wasm.deployers.helpers.layout import RELEASES, choose_layout, env_file_in
from wasm.deployers.helpers.nginx_config import NginxAdvancedConfig
from wasm.deployers.helpers.permissions import hand_over_file, hand_over_tree
from wasm.deployers.helpers.registration import StoreRegistrar
from wasm.deployers.helpers.release_build import (
    REPO_CACHE_DIR,
    StagedRelease,
    discard_release,
    reuse_dependencies,
    stage_release,
    stamp_installed_dependencies,
)
from wasm.deployers.helpers.summary import print_deployment_summary
from wasm.deployers.helpers.target import claim_deploy_target
from wasm.deployers.interface import AppDeployer, StepReporter, UpdateResult
from wasm.deployers.pipeline import DeployStep, run_pipeline
from wasm.deployers.recorder import (
    CapturingLogger,
    DeploymentRecorder,
    checkout_git_info,
    recorder_for,
    recording,
)
from wasm.deployers.releases import CURRENT_LINK, ReleaseManager
from wasm.managers.apache_manager import ApacheManager
from wasm.managers.cert_manager import CertManager
from wasm.managers.nginx_manager import NginxManager
from wasm.managers.service_manager import ResourceLimits, ServiceManager
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

#: A local, read-only ``git log`` never touches the network; this is generous
#: only against a checkout on a badly loaded disk.
GIT_LOG_TIMEOUT = 10

#: Failures while writing release bookkeeping. The release on disk is the
#: truth; a row that could not be written is reported, never fatal.
_RECORDING_ERRORS = (WASMError, sqlite3.Error)


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

    #: Whether this deployer can build releases. Monorepo and docker-compose
    #: do not derive from this class and do not have the attribute, which the
    #: layout choice reads as False.
    SUPPORTS_RELEASES: ClassVar[bool] = True

    #: Class-level defaults for the layout state, so an instance assembled
    #: without ``__init__`` (as some tests do) still behaves in place.
    _layout: str | None = None
    _staged: StagedRelease | None = None
    _app_record: App | None = None
    _replace_existing: bool = False

    def __init__(
        self,
        verbose: bool = False,
        runner: CommandRunner | None = None,
        fs: FileSystem | None = None,
    ):
        """
        Initialize the deployer.

        Args:
            verbose: Enable verbose logging.
            runner: Command runner used for install and build commands. Defaults
                to the process-wide runner, which is what enforces --dry-run.
            fs: Filesystem every change goes through. Defaults to the
                process-wide one, for the same reason.
        """
        self.verbose = verbose
        self.config = Config()
        # Capturable so a deployment recording can mirror the full build
        # output, including the detail a non-verbose console suppresses.
        self.logger = CapturingLogger(verbose=verbose)
        self.store = get_store()
        self._runner = runner
        self._fs = fs

        # Managers are built on first use. Detection instantiates every
        # registered deployer just to ask "is this yours?", and constructing
        # four managers and a store connection to answer no is waste that also
        # made a deployer impossible to create without a working system.
        self._source_manager: SourceManager | None = None
        self._service_manager: ServiceManager | None = None
        self._cert_manager: CertManager | None = None
        self._registrar: StoreRegistrar | None = None

        # Helpers
        self._pm_helper = PackageManagerHelper(logger=self.logger, runner=self.runner)
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
        self.trigger: str = DeploymentTrigger.CLI.value
        #: The background job that started this deployment, when one did.
        #: None for the CLI and for a webhook, which run with nothing queuing
        #: them.
        self.job_id: str | None = None
        #: The id of the history row :meth:`deploy` or :meth:`update` just
        #: wrote, once it returns. None until then, and None again if
        #: recording itself failed (see :class:`DeploymentRecorder`) or the
        #: run was a dry one.
        self.last_deployment_id: int | None = None
        self.memory_max_mb: int | None = None
        self.cpu_quota_percent: int | None = None
        self.tasks_max: int | None = None
        self._resource_limits_given: bool = False

        # Package manager (auto = auto-detect)
        self._package_manager: PackageManager = "auto"
        self.package_manager: str = "npm"  # Resolved package manager

        # Prisma support
        self.has_prisma: bool = False

        # Deployment progress, shared between pipeline steps
        self._ssl_obtained: bool = False
        self._app_record: App | None = None
        self._is_new_deployment: bool = True

        # Layout. The request is what configure() was told; the layout is
        # decided once per deploy or update, against the store, by
        # resolve_layout(). Until then everything behaves in place.
        self._requested_layout: str | None = None
        self._layout: str | None = None
        self._persistent_request: list[str] | None = None
        self._releases: ReleaseManager | None = None
        self._staged: StagedRelease | None = None
        #: The release whose dependencies the last build reused, if any.
        self.dependencies_reused_from: str | None = None

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
        trigger: str = DeploymentTrigger.CLI.value,
        layout: str | None = None,
        persistent_paths: Sequence[str] | None = None,
        job_id: str | None = None,
        memory_max_mb: int | None = None,
        cpu_quota_percent: int | None = None,
        tasks_max: int | None = None,
        resource_limits_given: bool = False,
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
            include_www: Also answer on ``www.<domain>``, recorded as a domain
                of kind ``redirect`` to the primary unless the application
                already has that name in some other role.
            trigger: What initiated this deployment, recorded in the history:
                ``cli`` (the default), ``panel`` or ``webhook``.
            layout: ``inplace`` or ``releases`` for a new application,
                ``default`` for the server's configured layout, or None for
                in place. An existing application always keeps its own; see
                :func:`wasm.deployers.helpers.layout.choose_layout`.
            persistent_paths: Paths, relative to the application, that live
                in ``shared/`` and are linked into every release (uploads,
                storage). None keeps what the application already has.
            job_id: The background job driving this deployment, when the
                panel queued it. Recorded on the deployment history row so
                the two can be linked; None for the CLI and for a webhook.
            memory_max_mb: ``MemoryMax`` the unit is created with, in MB.
                Only takes effect when ``resource_limits_given`` is true; see
                :meth:`~wasm.deployers.helpers.registration.StoreRegistrar.register_app`.
            cpu_quota_percent: ``CPUQuota`` the unit is created with, in
                percent of one CPU. Same rule as ``memory_max_mb``.
            tasks_max: ``TasksMax`` the unit is created with. Same rule as
                ``memory_max_mb``.
            resource_limits_given: Whether the three limits above were part of
                this call at all. False (the default, and every caller except
                a fresh ``POST /api/apps`` with limits in the body) leaves an
                existing application's limits exactly as they were - set once
                through ``PATCH .../limits`` or at creation, a redeploy or an
                update must not silently clear them.
            **options: ``replace_existing`` deploys into a directory that
                already holds files (``wasm create --force``): in place they
                are replaced, on releases a release is added beside them.
                Anything else is accepted and ignored, so a caller can pass
                the union of every deployer's settings without knowing which
                one it got.
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
        self.trigger = trigger
        self.job_id = job_id
        self.memory_max_mb = memory_max_mb
        self.cpu_quota_percent = cpu_quota_percent
        self.tasks_max = tasks_max
        self._resource_limits_given = resource_limits_given
        self._package_manager = package_manager  # type: ignore[assignment]
        self._requested_layout = layout
        self._persistent_request = list(persistent_paths) if persistent_paths is not None else None
        self._layout = None
        self._releases = None
        self._staged = None
        # Deploying over a directory that already holds files is refused
        # unless asked for (wasm create --force); see claim_deploy_target.
        self._replace_existing = bool(options.get("replace_existing", False))
        self.deploy_target = None

        # Set app name and path
        self.app_name = domain_to_app_name(domain)
        self.app_path = app_path or (self.config.apps_directory / self.app_name)

    # Layout and paths -----------------------------------------------------

    def resolve_layout(self, existing: App | None = None) -> str:
        """
        Decide, once, whether this deployment builds a release or works in place.

        Args:
            existing: The application's store row, when the caller already
                read it. Read here otherwise.

        Returns:
            ``inplace`` or ``releases``.

        Raises:
            DeploymentError: When the requested layout conflicts with the
                application's, or this deployer cannot build releases.
        """
        if self._layout is None:
            if existing is None and self.domain:
                existing = self.store.get_app(self.domain)
            self._layout = choose_layout(
                existing,
                self._requested_layout,
                app_type=self.APP_TYPE,
                supports_releases=self.SUPPORTS_RELEASES,
                config=self.config,
            )
        return self._layout

    @property
    def uses_releases(self) -> bool:
        """Whether this deployment builds a release. False until the layout is resolved."""
        return self._layout == RELEASES

    @property
    def releases(self) -> ReleaseManager:
        """The manager of this application's releases."""
        if self._releases is None:
            self._releases = ReleaseManager(
                self.app_path, fs=self._fs, runner=self._runner, logger=self.logger
            )
        return self._releases

    @property
    def build_path(self) -> Path:
        """Where the code being deployed is built: the new release, or the app itself."""
        if self._staged is not None:
            return self._staged.path
        return self.app_path

    @property
    def runtime_path(self) -> Path:
        """What the unit and the web server point at: ``current``, or the app itself."""
        if self.uses_releases:
            return self.app_path / CURRENT_LINK
        return self.app_path

    def _at_runtime(self, path: Path) -> Path:
        """
        Translate a path inside the build tree to the path the service will see.

        Args:
            path: A path at or below :attr:`build_path`.

        Returns:
            The same path below :attr:`runtime_path`. In place the two trees
            are one, and the path comes back untouched.
        """
        if self.runtime_path == self.build_path:
            return path
        try:
            return self.runtime_path / path.relative_to(self.build_path)
        except ValueError:
            return path

    def adopt_release(self, staged: StagedRelease) -> None:
        """
        Build a release someone else already staged, instead of fetching one.

        :class:`~wasm.deployers.auto.AutoDeployer` has to fetch the source to
        know which deployer builds it; fetching again would clone twice and
        leave an empty release behind.

        Args:
            staged: The release, with its source in place.
        """
        self._staged = staged
        self._releases = staged.manager
        self.source_already_fetched = True

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
                cwd=cwd or self.build_path,
                env=run_env or None,
                timeout=timeout,
            )
        else:
            result = self.runner.run(
                command,
                cwd=cwd or self.build_path,
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
        return self._pm_helper.detect(self.build_path, self._package_manager)

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
        return self._ensure_prisma_helper().detect(self.build_path)

    def _get_pm_install_command(self) -> list[str]:
        """
        Get the package manager install command.

        Returns:
            Install command as list.
        """
        return self._pm_helper.get_install_command(self.package_manager, self.build_path)

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

        return self._ensure_prisma_helper().generate(self.build_path)

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
        migrations_dir = self.build_path / "prisma" / "migrations"
        if not migrations_dir.exists():
            self.logger.debug("No Prisma migrations found")
            return True

        return self._ensure_prisma_helper().migrate(self.build_path, deploy=deploy)

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
        # Only the primary: every other name the site answers on is a row in
        # the store, and WebServerManager reads the rows where it writes the
        # file, so no caller - this one included - decides them.
        server_names = self.domain

        if self._nginx_advanced_config is not None:
            ctx = self._nginx_config_builder.build_context(
                self._nginx_advanced_config,
                self.domain,
                ssl=self.ssl,
                app_path=str(self.runtime_path),
            )
            ctx["server_names"] = server_names
            return ctx

        return {
            "domain": self.domain,
            "server_names": server_names,
            "port": self.port,
            "app_path": str(self.runtime_path),
            "app_name": self.app_name,
            "ssl": self.ssl,
            "health_check": self.get_health_check(),
        }

    def _detect_nginx_config(self) -> None:
        """
        Detect and parse wasm.nginx.yaml if present.

        Sets self._nginx_advanced_config if a valid config file is found.
        """
        if not self.build_path or not self.build_path.exists():
            return

        config_path = self._nginx_config_builder.detect(self.build_path)
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
        if not self.build_path or not self.build_path.exists():
            return False

        # Skip if user already provided env vars
        if self.env_vars:
            return False

        # A release shares the .env every earlier release used; generating a
        # new one would replace the secrets the application already runs on.
        if self.uses_releases and self._env_file().exists():
            return False

        # Check for .env.example
        return (self.build_path / ".env.example").exists()

    def _generated_env(self) -> dict[str, str]:
        """
        Fill in what ``.env.example`` asks for, without asking anyone.

        Returns:
            Defaults from the example, with a generated secret wherever the
            example names one and leaves it empty.
        """
        variables = self._env_manager.discover(self.build_path)
        if not variables:
            return {}
        self.logger.debug(f"Discovered {len(variables)} env variables")
        return self._env_manager.prompt_non_interactive(variables)

    def _unit_environment(self) -> dict[str, str]:
        """
        The variables the unit sets inline, and the only ones it carries.

        They are not secret and they are WASM's to decide: PORT is the port the
        site proxies to. Everything else - what ``--env-file`` or ``env_vars``
        gave, what ``.env.example`` generated - goes to the env file, because a
        unit is 0644 and ``systemctl show`` prints its ``Environment=`` to any
        local user.

        Returns:
            Variable name to value.
        """
        return {"PORT": str(self.port), "NODE_ENV": "production"}

    def _prepare_env(self) -> None:
        """
        Write the variables given at create time into the env file, before the build.

        The variables from ``--env-file`` or ``POST /api/apps`` are merged over
        what the file already holds; when none were given and the source ships
        a ``.env.example``, it is filled in instead. The build runs after this,
        so a build that reads ``DATABASE_URL`` from ``.env`` finds it, and the
        build commands keep seeing the variables in their own environment too.

        Raises:
            EnvironmentValidationError: When a variable given at create time
                has an unusable name or a control character in its value,
                before anything is written.
        """
        given = validate_environment(self.env_vars)
        generated = self._generated_env() if self._should_configure_env() else {}
        if not (given or generated):
            return

        owned = self._unit_environment()
        for key in sorted(given.keys() & owned.keys()):
            if given[key] != owned[key]:
                self.logger.warning(
                    f"{key}={given[key]} is not used: WASM sets {key}={owned[key]} in the unit"
                )

        current = self._env_manager.read_env_file(self._env_file())
        self._write_env_file({**current, **generated, **given})
        if generated:
            self.logger.substep("Created .env from .env.example")
        self.env_vars = {**generated, **given}

    def _write_env_file(self, values: Mapping[str, str]) -> None:
        """
        Replace the env file with these variables, minus the ones the unit sets.

        systemd lets ``EnvironmentFile=`` override ``Environment=``, the
        opposite of dotenv, which never overrode what the unit set. So the file
        must not carry PORT or NODE_ENV: a PORT=3000 copied from
        ``.env.example`` would move the second application on a server onto
        the first one's port.

        Args:
            values: The complete set of variables.
        """
        owned = self._unit_environment()
        kept = {key: value for key, value in values.items() if key not in owned}
        self._env_manager.write_env_file(self._env_file(), kept)

    def _settle_env_file(self) -> Path:
        """
        Make the env file what the unit about to be written expects.

        Two things can be wrong with it on a redeploy. The unit an earlier
        version wrote carried the variables inline, and the application ran
        with them: rewriting the unit without them would drop them, so they
        move into the file first, winning over what the file says, since theirs
        were the values in force. And the file may set PORT or NODE_ENV, which
        the new unit would let override its own. The file is only rewritten
        when one of the two applies.

        Returns:
            The env file the unit references.
        """
        env_file = self._env_file()
        owned = self._unit_environment()
        previous = self.store.get_service(self.app_name) if self.app_name else None
        inline = {
            key: value
            for key, value in (previous.environment if previous else {}).items()
            if key not in owned
        }
        current = self._env_manager.read_env_file(env_file)
        moved = sorted(key for key, value in inline.items() if current.get(key) != value)
        conflicting = sorted(key for key in owned if key in current and current[key] != owned[key])
        if not (moved or conflicting):
            return env_file

        if moved:
            self.logger.substep(f"Moved {', '.join(moved)} from the unit to {env_file}")
        if conflicting:
            self.logger.substep(
                f"Removed {', '.join(conflicting)} from {env_file}: the unit sets it"
            )
        self._write_env_file({**current, **inline})
        hand_over_file(
            env_file,
            user=self.config.service_user,
            group=self.config.service_group,
            mode=SECRET_MODE,
            runner=self.runner,
            logger=self.logger,
        )
        return env_file

    def _env_file(self) -> Path:
        """
        Return where this application's ``.env`` lives.

        Returns:
            ``shared/.env`` on releases, the application directory's in place.
        """
        return env_file_in(self.app_path, self._layout)

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
        """
        Take back the application directory a failed deploy fetched into.

        During a deploy, what was there before decides: a directory this
        deploy created is deleted, one it found empty is emptied, one that
        held files is never touched. Outside a deploy nothing records that,
        and the directory is deleted, as this always did.
        """
        if self.deploy_target is not None:
            self.deploy_target.undo_fetch(self.fs, self.logger)
            return
        if self.app_path and self.app_path.exists():
            self.logger.debug(f"Removing app files: {self.app_path}")
            try:
                self.fs.remove_tree(self.app_path)
            except OSError as e:
                # An undo runs while something has already gone wrong; one
                # directory that will not go away must not hide that failure.
                self.logger.debug(f"Could not remove {self.app_path}: {e}")

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

    def _leftover_unit(self) -> bool:
        """
        Report whether a unit is on disk for an application that runs no process.

        Asked again when the unit is retired rather than trusted from the
        pipeline's construction: a Vite build decides whether it serves files
        or runs a server only once it has looked at the source.

        Returns:
            True when this type serves files (no start command) and a unit
            file for the application is in the managed directory.
        """
        if not self.app_name or self.get_start_command():
            return False
        return self.service_manager.service_exists(self.app_name)

    def retire_leftover_unit(self) -> None:
        """
        Stop, disable and delete the unit a process type left behind.

        An application first deployed as a type that runs a process and later
        redeployed as one that serves files keeps its old unit otherwise, and
        with ``Restart=always`` that unit restarts a command the new tree does
        not have every ten seconds, forever: one production server was found
        at 7750 consecutive "Missing script: start" failures. The deletion is
        :meth:`ServiceManager.delete_service`, which refuses a unit WASM does
        not own; that refusal, like any other failure here, is reported and
        does not fail a deployment whose site already serves the new files.
        """
        if not self._leftover_unit():
            return
        name = self.app_name or ""
        try:
            self.service_manager.delete_service(name)
        except ServiceError as exc:
            self.logger.warning(f"The unit of {name} was left in place: {exc.message}")
            if exc.details:
                self.logger.substep(exc.details)
            return
        self.logger.substep(
            f"Removed the unit of {name}: as {self.APP_TYPE} it is served by the web server "
            "and runs no process"
        )

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

        self._record_requested_domains()
        manager = self._webserver_manager()
        self._write_site(manager, with_ssl=with_ssl)
        manager.reload()
        return True

    def _write_site(self, manager: NginxManager | ApacheManager, *, with_ssl: bool) -> None:
        """
        Render the site and put it on disk, without reloading anything.

        Args:
            manager: The web server manager to write through.
            with_ssl: Render the TLS server blocks.
        """
        context = self.get_template_context()
        # Override SSL setting based on parameter
        context["ssl"] = with_ssl

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

        self.registrar.register_site(
            domain=self.domain,
            webserver=self.webserver,
            template=template,
            app_path=self.runtime_path,
            port=self.port,
            with_ssl=with_ssl,
        )

    def _record_requested_domains(self) -> None:
        """
        Record the ``www`` name ``--www`` asked for, as a redirect to the primary.

        A redirect rather than an alias: one canonical address is what a site
        wants, and a redirect costs the visitor nothing. An application that
        already has the name keeps it in whatever role it has - an operator
        who made it an alias with ``wasm domain`` is not overruled by a flag
        on a redeploy.

        Nothing is recorded when there is no application row to attach it to,
        which is the case under ``--dry-run``: the store rolled the row back.
        """
        if not self.include_www or self.store.get_app(self.domain) is None:
            return
        www = f"www.{self.domain}"
        if any(record.domain == www for record in self.store.list_domains(self.domain)):
            return
        self.store.add_domain(self.domain, www, DomainKind.REDIRECT.value)
        self.logger.substep(f"{www} redirects to {self.domain}")

    def inspect_site(self) -> None:
        """
        Read what the site's configuration depends on from the tree being served.

        A deploy learns these as a side effect of building; re-rendering the
        site of an application that is already deployed has to learn them
        without building anything, so each deployer that renders from more
        than its settings says what it reads here. Reads only.
        """
        self._detect_nginx_config()

    def webserver_manager(self) -> NginxManager | ApacheManager:
        """
        Return the manager of the web server that serves this application.

        Returns:
            An NginxManager or an ApacheManager.
        """
        return self._webserver_manager()

    def has_certificate(self) -> bool:
        """
        Tell whether a certificate lineage for this application is on disk.

        Returns:
            True when certbot's live directory holds one for the primary.
        """
        try:
            return self.cert_manager.cert_exists(self.domain)
        except CertificateError:
            return False

    def refresh_site(self, *, with_ssl: bool) -> None:
        """
        Render the site of a deployed application again, and load it.

        The same rendering a deploy does, from what is deployed: the layout the
        store records, the active release on releases, and whatever the
        deployer reads off the served tree (:meth:`inspect_site`). Nothing is
        fetched or built, and the unit is not touched. This is how a change to
        an application's domains reaches its web server.

        The whole configuration is tested before the web server is asked to
        reload. A failing test puts the previous file back, so a change that
        nginx refuses never lingers on disk to fail the next reload of some
        other site.

        Args:
            with_ssl: Render the TLS server blocks.

        Raises:
            DeploymentError: When the application is on releases and none is
                active, or the web server does not reload.
            ValidationError: When the web server rejects the configuration;
                ``details`` is its own output, and the previous configuration
                is back in place.
        """
        self.resolve_layout(self._app_row())
        if self.uses_releases and self._staged is None:
            active = self.releases.current()
            if active is None:
                raise DeploymentError(
                    f"{self.domain} has no active release to serve",
                    details=f"Build one first: wasm update {self.domain}",
                )
            self.adopt_release(
                StagedRelease(path=active.path, commit=active.commit, manager=self.releases)
            )
        self.inspect_site()

        manager = self._webserver_manager()
        previous = (
            manager.get_site_config(self.domain) if manager.site_exists(self.domain) else None
        )
        self._write_site(manager, with_ssl=with_ssl)

        problem = manager.config_errors()
        if problem is not None:
            if previous is None:
                manager.delete_site(self.domain)
            else:
                manager.replace_site_config(self.domain, previous, validate=False)
            raise ValidationError(
                f"{self.webserver} rejected the new configuration of {self.domain}",
                details=problem,
            )
        if not manager.reload():
            raise DeploymentError(
                f"{self.webserver} did not reload the configuration of {self.domain}",
                details=f"The configuration is valid; see why the reload failed with: "
                f"systemctl status {self.webserver}",
            )

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

        # Only what the unit decides itself is written into it; the variables
        # given at create time are in the env file _prepare_env wrote, which the
        # unit loads. Everything interpolated into a unit is still validated:
        # a newline there starts a new directive.
        env = validate_environment(self._unit_environment())
        env_file = self._settle_env_file()
        start_command = validate_unit_value(start_command, field="ExecStart")
        working_directory = validate_unit_value(str(self.runtime_path), field="WorkingDirectory")
        description = validate_unit_value(
            f"WASM: {self.domain} ({self.APP_TYPE})", field="Description"
        )

        self.service_manager.create_service(
            name=self.app_name,
            command=start_command,
            working_directory=working_directory,
            environment=env,
            environment_file=str(env_file),
            description=description,
            # The application's settings, not this deployment's: a redeploy
            # writes a new unit and must not drop the limits set on the old.
            limits=ResourceLimits.of(self._app_row()),
        )

        # Enable service
        self.service_manager.enable(self.app_name)

        self.registrar.register_service(
            domain=self.domain,
            name=self.app_name,
            command=start_command,
            working_directory=self.runtime_path,
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

        # Every name the application answers on, redirects included: they
        # are served on 443 as well. CertManager reads the same rows when it
        # places the order; they are listed here so the operator sees them.
        names = [record.domain for record in self.store.list_domains(self.domain)]
        names = names or [self.domain]
        if self.include_www and f"www.{self.domain}" not in names:
            # Under --dry-run the store keeps no rows; the rehearsal still
            # shows what the certificate would cover.
            names.append(f"www.{self.domain}")
        additional_domains = [name for name in names if name != self.domain]
        if additional_domains:
            self.logger.substep(f"Including: {', '.join(additional_domains)}")

        # Use nginx plugin if using nginx
        nginx = self.webserver == "nginx"
        apache = self.webserver == "apache"

        self.cert_manager.obtain(
            self.domain,
            nginx=nginx,
            apache=apache,
            additional_domains=additional_domains or None,
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
        check = self._health_check_settings()
        url = check.url(self.port)
        self.logger.substep(f"Checking: {url}")

        return wait_until_healthy(
            url,
            retries=retries,
            delay=delay,
            on_attempt=self.logger.debug,
            # Without an expectation of its own, the strict check this
            # report always made: a 200, redirects followed.
            accept=check.accepts if check.expect is not None else None,
        )

    def _health_check_settings(self) -> HealthCheck:
        """
        Read what the application's health check asks.

        Returns:
            The application's own path, expectation and timeout, over this
            deployer's path.
        """
        return HealthCheck.for_app(self._app_row(), default_path=self.get_health_check())

    def build_pipeline(self) -> list[DeployStep]:
        """
        Describe the deployment as an ordered list of steps.

        Subclasses override this to add, drop or reorder steps instead of
        rewriting the whole ``deploy`` method, which is how static and vite
        deployments used to end up with their own copies of the workflow.

        Returns:
            The steps to execute, each with the undo that reverses it.
        """
        if self.uses_releases:
            return self._release_pipeline()
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
                title="Setting permissions",
                icon=Icons.LOCK,
                run=self._set_permissions,
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

    def _release_pipeline(
        self,
        build_steps: list[DeployStep] | None = None,
        *,
        with_service: bool = True,
    ) -> list[DeployStep]:
        """
        Describe a deployment that builds a release and activates it behind a health gate.

        The release is fetched, linked to ``shared/``, installed and built in
        its own directory; the unit and the site are written against
        ``current``; activation swaps ``current`` and restarts, and a release
        that does not answer is rolled back to the one before it.

        For a new application every step keeps its undo, so a failed first
        deploy leaves nothing behind. For one that exists, the site and the
        unit are left alone on failure: they point at ``current``, which the
        rollback has already pointed back at a release that works.

        Args:
            build_steps: What turns the fetched source into something that
                runs. Defaults to installing and building.
            with_service: Whether the application runs as a unit.

        Returns:
            The steps to execute, each with the undo that reverses it.
        """
        new_app = self._is_new_deployment
        steps = [
            DeployStep(
                title="Fetching source into a new release",
                icon=Icons.DOWNLOAD,
                run=self._step_fetch_release,
                undo=self._undo_release_fetch,
            ),
        ]
        steps += (
            build_steps
            if build_steps is not None
            else [
                DeployStep(
                    title="Installing dependencies",
                    icon=Icons.PACKAGE,
                    run=self._install_release_dependencies,
                ),
                DeployStep(
                    title="Building application",
                    icon=Icons.BUILD,
                    run=self.build,
                ),
            ]
        )
        steps += [
            DeployStep(
                title="Setting permissions",
                icon=Icons.LOCK,
                run=self._set_permissions,
            ),
            DeployStep(
                title="Creating site configuration",
                icon=Icons.GLOBE,
                run=lambda: self.create_site(with_ssl=False),
                undo=self.remove_site if new_app else None,
            ),
            DeployStep(
                title="Obtaining SSL certificate",
                icon=Icons.LOCK,
                run=self._step_certificate,
                skip_if=lambda: not self.ssl,
            ),
        ]
        if with_service:
            steps.append(
                DeployStep(
                    title="Creating systemd service",
                    icon=Icons.GEAR,
                    run=self.create_service,
                    undo=self.remove_service if new_app else None,
                )
            )
        steps.append(
            DeployStep(
                title="Activating release",
                icon=Icons.ROCKET,
                run=self._step_activate,
            )
        )
        return steps

    def _step_fetch_release(self) -> None:
        """
        Put the source in a new release and link it to what releases share.

        The ``.env`` and the persistent paths are linked before anything is
        installed or built, because both may need them: a build that reads
        ``DATABASE_URL`` or a postinstall that writes into ``storage/``.
        """
        if self._staged is None:
            self.logger.substep(f"Source: {self.source}")
            self._staged = stage_release(
                self.source,
                self.branch,
                releases=self.releases,
                source_manager=self.source_manager,
                logger=self.logger,
            )
        staged = self._staged
        self._record_release_row(staged)

        # Both describe the code that was just fetched.
        self._detect_nginx_config()
        self._prepare_env()

        links = staged.manager.link_shared(staged.path, self._persistent_paths())
        for path in links.linked:
            self.logger.substep(f"Linked {path} to shared/{path}")

    def _install_release_dependencies(self) -> None:
        """
        Install the release's dependencies, or take them from the active release.

        The copy happens before :meth:`pre_install`, because the Python
        deployer creates its virtual environment there and a copy into an
        existing directory would nest inside it.
        """
        staged = self._require_staged()
        self.dependencies_reused_from = reuse_dependencies(
            staged.path,
            staged.manager.current(),
            runner=self.runner,
            fs=self.fs,
            logger=self.logger,
        )
        if self.dependencies_reused_from is None:
            self.install_dependencies()
            # So the next deploy can tell whether reusing this install would
            # still match the runtime it was built with.
            stamp_installed_dependencies(
                staged.path, runner=self.runner, fs=self.fs, logger=self.logger
            )
            return

        self.pre_install()
        self.logger.substep(f"Dependencies reused from {self.dependencies_reused_from}")
        self.post_install()

    def _step_activate(self) -> None:
        """Activate the release behind the health gate, then record that it runs."""
        self._activate_release()
        self._mark_running()

    def _require_staged(self) -> StagedRelease:
        """
        Return the release being built.

        Returns:
            The staged release.

        Raises:
            DeploymentError: When no release was staged, which means a step
                ran out of order.
        """
        if self._staged is None:
            raise DeploymentError(
                "No release is being built",
                details="The fetch step stages the release; it has to run first.",
            )
        return self._staged

    def _activate_release(self) -> None:
        """
        Point ``current`` at the new release, restart, and keep it only if it answers.

        A release that does not pass the health check is recorded as failed
        and the previous one is activated and restarted again, so the
        application goes back to what served a moment ago. The deployment
        then fails with the probe's and the journal's own output.

        Raises:
            DeploymentError: When the release did not pass the health check,
                whether or not there was a release to go back to.
        """
        staged = self._require_staged()
        previous = staged.manager.activate(staged.path)
        if previous is None:
            self.logger.substep(f"Activated release {staged.id}")
        else:
            self.logger.substep(f"Activated release {staged.id} (was {previous.id})")

        healthy, evidence = self._restart_and_probe()
        if healthy:
            self._record_release_active(staged)
            self._prune_releases(staged)
            return

        self._record_release_status(staged, ReleaseStatus.FAILED)
        if previous is None:
            raise DeploymentError(
                f"Release {staged.id} did not pass its health check",
                details=evidence,
            )

        self.logger.warning(f"Release {staged.id} did not pass its health check")
        self.logger.substep(f"Going back to release {previous.id}")
        staged.manager.activate(previous.path)
        restored, _ = self._restart_and_probe()
        state = "is active again" if restored else "is active again but is not answering either"
        raise DeploymentError(
            f"Release {staged.id} did not pass its health check; release {previous.id} {state}",
            details=evidence,
        )

    def _restart_and_probe(self) -> tuple[bool, str]:
        """
        Restart the application on whatever ``current`` points at, and ask if it is up.

        Returns:
            Whether it is healthy, and when it is not, the evidence: the
            failed probes and the unit's journal, verbatim.
        """
        return self._health_gate().restart_and_probe()

    def _health_gate(self) -> HealthGate:
        """
        Build the gate a release must pass to stay active.

        A service answers over HTTP, on the application's health path, with a
        status its expectation accepts: without settings of its own, any
        response below 500 means the process started and routes requests,
        redirects included. A site without a service is checked by the
        deployer's own :meth:`health_check`, which looks for the files it
        serves.

        Returns:
            The gate, the same one an operator's rollback goes through.
        """
        serves = bool(self.get_start_command())
        check = self._health_check_settings()
        return HealthGate(
            unit=self.app_name if serves and self.app_name else None,
            url=check.url(self.port) if serves else None,
            check=check,
            services=self.service_manager,
            logger=self.logger,
            # Looked up here, at call time, so it is the one this module holds.
            probe=wait_until_healthy,
            restart=self.restart,
            files_check=None if serves else self.health_check,
        )

    def _undo_release_fetch(self) -> None:
        """
        Undo the fetch of a release deploy that failed.

        A new application whose directory this deploy created (or found
        empty) leaves nothing behind. Anything else loses only the release
        this deploy staged: the releases, ``shared/`` and whatever else the
        directory held before are not this deploy's.
        """
        target = self.deploy_target
        if self._is_new_deployment and (target is None or not target.had_files):
            self.remove_source()
            return
        staged = self._staged
        if self._is_new_deployment and staged is not None:
            active = staged.manager.current()
            if active is not None and active.id == staged.id:
                # A first release that failed its gate had nothing to go back
                # to, so current was made by this deploy and points at it.
                self.fs.remove(staged.manager.current_link)
        self._abandon_release()

    def _abandon_release(self) -> None:
        """
        Throw away a release that failed before it could serve.

        Recorded as failed and removed from disk, unless ``current`` points
        at it: whatever went wrong, the release that is serving is never the
        one deleted.
        """
        staged = self._staged
        if staged is None:
            return
        self._record_release_status(staged, ReleaseStatus.FAILED)
        discard_release(staged.path, releases=staged.manager, logger=self.logger)

    def _persistent_paths(self) -> list[str]:
        """
        Return the paths every release shares through ``shared/``.

        Returns:
            What this deployment was configured with, or else what the
            application has recorded.
        """
        if self._persistent_request is not None:
            return list(self._persistent_request)
        app = self._app_row()
        return list(app.persistent_paths) if app is not None else []

    def _keep_releases(self) -> int:
        """
        Return how many releases the application keeps on disk.

        Returns:
            The recorded retention, never less than 1.
        """
        app = self._app_row()
        keep = app.keep_releases if app is not None else DEFAULT_KEEP_RELEASES
        return max(1, keep or DEFAULT_KEEP_RELEASES)

    def _app_row(self) -> App | None:
        """
        Return the application's store row.

        Returns:
            The row this deployment registered, or the stored one, or None.
        """
        if self._app_record is not None:
            return self._app_record
        return self.store.get_app(self.domain) if self.domain else None

    def _records_releases(self) -> int | None:
        """
        Tell whether release rows can be written, and for which application.

        Returns:
            The application id, or None under ``--dry-run`` (the store rolls
            the app row back, so a release row would point at nothing) and
            when the application is not registered.
        """
        if isinstance(self.fs, DryRunFileSystem):
            return None
        app = self.store.get_app(self.domain) if self.domain else None
        return app.id if app is not None else None

    def _record_release_row(self, staged: StagedRelease) -> None:
        """
        Remember a release that was just staged.

        Args:
            staged: The release.
        """
        app_id = self._records_releases()
        if app_id is None:
            return
        try:
            if self.store.get_release(app_id, staged.id) is None:
                self.store.record_release(
                    ReleaseRecord(
                        id=staged.id,
                        app_id=app_id,
                        git_commit=staged.short_commit,
                        created_at=datetime.now(timezone.utc).isoformat(),
                        status=ReleaseStatus.BUILT.value,
                        path=str(staged.path),
                    )
                )
        except _RECORDING_ERRORS as exc:
            self.logger.warning(f"Could not record release {staged.id}: {exc}")

    def _record_release_status(self, staged: StagedRelease, status: ReleaseStatus) -> None:
        """
        Record how a release ended up.

        Args:
            staged: The release.
            status: Its new status.
        """
        app_id = self._records_releases()
        if app_id is None:
            return
        try:
            self.store.set_release_status(app_id, staged.id, status.value)
        except _RECORDING_ERRORS as exc:
            self.logger.warning(f"Could not record release {staged.id} as {status.value}: {exc}")

    def _record_release_active(self, staged: StagedRelease) -> None:
        """
        Record that a release is now the active one.

        Args:
            staged: The release.
        """
        app_id = self._records_releases()
        if app_id is None:
            return
        try:
            self.store.mark_release_active(app_id, staged.id)
        except _RECORDING_ERRORS as exc:
            self.logger.warning(f"Could not record release {staged.id} as active: {exc}")

    def _prune_releases(self, staged: StagedRelease) -> None:
        """
        Delete the releases beyond the application's retention, and forget them.

        Rows of releases that failed and were removed are kept while they are
        among the newest ``keep`` rows, so recent failures stay visible, and
        forgotten after that.

        Args:
            staged: The release just activated.
        """
        keep = self._keep_releases()
        removed = staged.manager.prune(keep)
        for release in removed:
            self.logger.substep(f"Pruned release {release.id}")

        app_id = self._records_releases()
        if app_id is None:
            return
        try:
            self.store.forget_pruned_releases(
                app_id,
                on_disk={release.id for release in staged.manager.list()},
                removed={release.id for release in removed},
                keep=keep,
            )
        except _RECORDING_ERRORS as exc:
            self.logger.warning(f"Could not forget pruned releases: {exc}")

    def _step_fetch(self) -> None:
        """Fetch the source, then read the configuration that ships with it."""
        self.fetch_source()

        # Both of these describe the code that was just fetched, so they cannot
        # run any earlier.
        self._detect_nginx_config()
        self._prepare_env()

    def _step_certificate(self) -> None:
        """
        Obtain a certificate and re-render the site with TLS enabled.

        A certificate failure is not a deployment failure: the application is
        still reachable over HTTP, and forcing a rollback here would throw away
        a working build because DNS had not propagated yet. When a certificate
        is already on disk - the order failed because one new alias does not
        resolve yet - the site keeps TLS with it.
        """
        try:
            self.obtain_certificate()
        except (CertificateError, WASMError) as e:
            if self.has_certificate():
                # Extending the lineage to a name whose DNS is not ready must
                # not take TLS off the names the certificate already covers.
                self.logger.warning(f"The certificate could not be extended: {e}")
                self.logger.substep("The existing certificate keeps serving what it covers")
                self._ssl_obtained = True
                self.create_site(with_ssl=True)
                return
            self.logger.warning(f"SSL certificate failed: {e}")
            self.logger.warning("Continuing deployment without SSL...")
            self.logger.substep("Application will be available via HTTP only")
            return

        self._ssl_obtained = True
        self.logger.substep("Updating site configuration with SSL")
        self.create_site(with_ssl=True)

    def _step_start(self) -> None:
        """Start the service and record that the deployment succeeded."""
        self.start()
        self._mark_running()

    def _mark_running(self) -> None:
        """Record that the deployment succeeded and the application runs."""
        if self._app_record is not None:
            self._app_record.status = AppStatus.RUNNING.value
            self._app_record.ssl_enabled = self._ssl_obtained
            self._app_record.deployed_at = datetime.now().isoformat()
            self.store.update_app(self._app_record)

        if self.app_name:
            self.store.update_service_status(self.app_name, active=True, enabled=True)

    def _set_permissions(self) -> None:
        """
        Hand the deployed tree over to the account the service runs as.

        The deployment ran as root, so without this step the service user
        cannot write into its own app directory and the unit fails at start
        with EACCES.

        On releases that is the new release and ``shared/``, where uploads and
        the ``.env`` live; the repository cache and the application directory
        itself stay root's, because the service never writes there.
        """
        if not self.uses_releases:
            hand_over_tree(
                self.app_path,
                user=self.config.service_user,
                group=self.config.service_group,
                runner=self.runner,
                fs=self.fs,
                logger=self.logger,
                env_files=self._env_files(),
            )
            return

        shared = self.releases.shared_dir
        trees = [self.build_path] + ([shared] if shared.is_dir() else [])
        for tree in trees:
            hand_over_tree(
                tree,
                user=self.config.service_user,
                group=self.config.service_group,
                runner=self.runner,
                fs=self.fs,
                logger=self.logger,
                env_files=_dotenv_files(tree),
            )

    def _env_files(self) -> list[Path]:
        """
        List the environment files whose modes must survive ``_set_permissions``.

        Returns:
            The dotenv files at the application root. Subclasses that write
            environment files elsewhere override this.
        """
        if not self.app_path.is_dir():
            return []
        return sorted(path for path in self.app_path.glob(".env*") if path.is_file())

    def _recorder(self) -> DeploymentRecorder:
        """
        Build the recorder :meth:`deploy` and :meth:`update` write history with.

        Returns:
            A recorder for this deployment attempt, wired to this deployer's
            store, logger and filesystem. One shared construction site, so the
            deploy and update paths cannot record differently.
        """
        return recorder_for(
            self,
            git_info=self._git_info,
            commit_message=self._commit_message_for_recording,
            release_id=self._release_id_for_recording,
        )

    def _commit_message_for_recording(self) -> str | None:
        """
        Read the subject line of the deployed commit, for a git source only.

        In place, that is :attr:`app_path` itself. On releases the exported
        release has no ``.git`` - it is exported without one - so this reads
        the repository cache instead, which persists across deploys.

        Returns:
            The subject (``git log -1 --format=%s``), or None when the
            checkout is not a git repository or the command fails. A missing
            subject must not cost the deployment its history row.
        """
        checkout = (self.app_path / REPO_CACHE_DIR) if self._staged is not None else self.app_path
        if not (checkout / ".git").is_dir():
            return None
        # The staged commit, not the cache's HEAD: a rebuild of an older
        # commit exports it while the cache stays on the head of the branch.
        staged = self._staged.commit if self._staged is not None else None
        result = self.runner.run(
            ["git", "log", "-1", "--format=%s", *([staged] if staged else [])],
            cwd=checkout,
            timeout=GIT_LOG_TIMEOUT,
        )
        subject = result.stdout.strip()
        return subject if result.success and subject else None

    def _release_id_for_recording(self) -> str | None:
        """
        Read the release this deployment built, once the fetch step has staged it.

        Returns:
            The release id, or None for an in-place deployment or one where
            the fetch step has not run yet (a failure before it).
        """
        return self._staged.id if self._staged is not None else None

    def _git_info(self) -> tuple[str | None, str | None]:
        """
        Read the short commit and branch of the deployed tree, when it is one.

        Returns:
            ``(commit, branch)``, each None when the tree is not a git
            checkout or the information is unavailable. Test doubles stand in
            for the source manager without implementing all of it, so a
            manager that cannot answer is treated as "unknown", not an error.
        """
        reader = getattr(self.source_manager, "get_repo_info", None)
        if self._staged is not None:
            # A release has no .git; its commit is part of what was staged,
            # and the branch is whatever the repository cache follows.
            branch = self.branch
            cache = self.app_path / REPO_CACHE_DIR
            if branch is None and reader is not None and (cache / ".git").is_dir():
                branch = reader(cache).get("branch")
            return self._staged.short_commit, branch
        return checkout_git_info(self.source_manager, self.app_path)()

    def update(self, on_step: StepReporter | None = None) -> UpdateResult:
        """
        Rebuild this application without a full redeploy.

        The sequence used to live in ``wasm.cli.commands.webapp``, which drove
        the deployer step by step and reached into ``_package_manager`` to do
        it. Keeping it here means the update path is the deployer's own, gets
        the same detection and error handling as a deploy, and can be tested.

        In place, the tree is rebuilt where it is and the caller restarts the
        unit. On releases, the source is fetched into a new release, built
        there and activated behind the health gate; a release that does not
        answer is rolled back before this raises, so the caller has nothing
        left to restart.

        Like a deploy, an update is recorded in the deployment history with
        its build log captured; recording failures are reported and never
        fail the update itself.

        Args:
            on_step: Called as each step begins.

        Returns:
            What was done, for the caller to present.

        Raises:
            WASMError: When a step fails.
        """
        report = on_step or (lambda _message: None)
        releases = self.resolve_layout() == RELEASES

        with recording(
            self._recorder(),
            git_branch=self.branch,
            on_failure=(lambda _exc: self._abandon_release()) if releases else None,
        ) as recorder:
            result = self._update_release(report) if releases else self._update_in_place(report)
            if self._leftover_unit():
                report("Removing the unit a previous deployment left")
                self.retire_leftover_unit()
        self.last_deployment_id = recorder.deployment_id
        return result

    def _update_in_place(self, report: StepReporter) -> UpdateResult:
        """
        Rebuild the application in the tree it runs from.

        Args:
            report: Called as each step begins.

        Returns:
            What was done.
        """
        report("Inspecting the project")
        self.pre_install()

        report("Installing dependencies")
        self.install_dependencies()

        prisma_updated = self._update_prisma(report)

        report("Building")
        self.build()

        # The pull, the install and the build all ran as root, and the
        # service writes into what they produced: Next.js creates
        # .next/cache/images on the first optimised image, and uploads land
        # in directories a pull may have just added.
        self._set_permissions()

        return self._update_result(prisma_updated)

    def _update_release(self, report: StepReporter) -> UpdateResult:
        """
        Build the latest source as a new release and activate it behind the health gate.

        Args:
            report: Called as each step begins.

        Returns:
            What was done.

        Raises:
            DeploymentError: When the new release did not pass its health
                check; the previous one is active and restarted by then.
        """
        report("Fetching into a new release")
        self._step_fetch_release()

        report("Installing dependencies")
        self._install_release_dependencies()

        prisma_updated = self._update_prisma(report)

        report("Building")
        self.build()

        self._set_permissions()

        report(f"Activating release {self._require_staged().id}")
        self._activate_release()

        return self._update_result(prisma_updated)

    def _update_prisma(self, report: StepReporter) -> bool:
        """
        Regenerate the Prisma client and apply migrations, when the project uses Prisma.

        Args:
            report: Called when the step begins.

        Returns:
            Whether Prisma was updated.
        """
        if not self.has_prisma:
            return False
        report("Updating Prisma")
        self.generate_prisma()
        self.run_prisma_migrate(deploy=True)
        return True

    def _update_result(self, prisma_updated: bool) -> UpdateResult:
        """
        Describe what an update did.

        Args:
            prisma_updated: Whether Prisma was updated.

        Returns:
            The result for the caller.
        """
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

        Every run is also recorded in the deployment history with its build
        log captured to a file; recording failures are reported as warnings
        and never fail the deployment itself.

        Args:
            total_steps: Ignored. The pipeline knows how many steps it has;
                the parameter is kept so existing callers still work.

        Returns:
            True if the application ended up deployed.

        Raises:
            WASMError: Whatever the failing step raised, after the rollback.
            AppBusyError: Another operation is running on the application.
        """
        if not self.domain:
            raise DeploymentError(
                "Deployer was not configured",
                details="Call configure(domain=..., source=...) before deploy().",
            )

        # Held from the first look at the directory to the last step: an
        # update, a migration or a second deploy of the same application
        # must not interleave with this one.
        with app_lock(self.domain, "deploy"):
            return self._deploy()

    def _deploy(self) -> bool:
        """
        Run the deployment, holding the application's lock.

        Returns:
            True if the application ended up deployed.

        Raises:
            WASMError: Whatever the failing step raised, after the rollback.
        """
        self._ssl_obtained = False

        # A redeployment must not lose its app row just because this attempt
        # failed, so only a genuinely new app registers an undo for it.
        existing = self.store.get_app(self.domain)
        is_new_deployment = existing is None
        self._is_new_deployment = is_new_deployment
        self.resolve_layout(existing)
        if self.deploy_target is None:
            # Before anything is fetched: a directory that holds an
            # application must not be emptied by the fetch, nor deleted by
            # the undo of a deploy that fails.
            self.deploy_target = claim_deploy_target(
                self.app_path,
                domain=self.domain,
                existing=existing,
                replace=self._replace_existing,
            )

        self.logger.debug("Running pre-flight validation...")
        self.pre_flight_check()

        self._app_record = self._register_app_in_store(AppStatus.DEPLOYING.value)

        steps = self.build_pipeline()
        if self._leftover_unit():
            # Last, once the site serves the new files: until then the old
            # unit may still be what answers for the domain.
            steps.append(
                DeployStep(
                    title="Removing the unit a previous deployment left",
                    icon=Icons.GEAR,
                    run=self.retire_leftover_unit,
                )
            )
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

        with recording(
            self._recorder(), git_branch=self.branch, on_failure=self._deploy_failed
        ) as recorder:
            run_pipeline(steps, self.logger)
        self.last_deployment_id = recorder.deployment_id

        self._report_result()
        return True

    def _deploy_failed(self, error: BaseException) -> None:
        """
        Mark a failed deployment in the application's row and say so.

        Args:
            error: What the failing step raised.
        """
        if not self._is_new_deployment and self._app_record is not None:
            # The rows survive a failed redeployment; mark them honestly.
            self._app_record.status = AppStatus.FAILED.value
            self.store.update_app(self._app_record)
        self.logger.error(f"Deployment failed: {error}")

    def _report_result(self) -> None:
        """Print the summary, plus troubleshooting hints when unhealthy."""
        # A release only becomes active after passing the health gate, so a
        # second, stricter probe could only contradict what was just proven.
        healthy = True if self.uses_releases else self.health_check()
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
            layout=self._layout,
            persistent_paths=self._persistent_request,
            memory_max_mb=self.memory_max_mb,
            cpu_quota_percent=self.cpu_quota_percent,
            tasks_max=self.tasks_max,
            limits_given=self._resource_limits_given,
        )


def _dotenv_files(tree: Path) -> list[Path]:
    """
    List the dotenv files that are really in a directory.

    Args:
        tree: A release, or ``shared/``.

    Returns:
        Regular ``.env*`` files at its top level. A release's ``.env`` is a
        link into ``shared/`` and is left out: it is protected where it lives.
    """
    if not tree.is_dir():
        return []
    return sorted(path for path in tree.glob(".env*") if path.is_file() and not path.is_symlink())
