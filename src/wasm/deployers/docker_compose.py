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
import re
import sqlite3
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, ClassVar

import yaml

from wasm.core.applock import app_lock
from wasm.core.config import Config
from wasm.core.exceptions import (
    DeploymentError,
    DockerError,
    SecurityError,
    ValidationError,
    WASMError,
)
from wasm.core.fs import FileSystem
from wasm.core.runner import CommandResult, CommandRunner, get_runner
from wasm.core.store import AppStatus, AppType, DeploymentTrigger, get_store
from wasm.core.utils import domain_to_app_name
from wasm.deployers.helpers.registration import StoreRegistrar
from wasm.deployers.helpers.target import claim_deploy_target
from wasm.deployers.interface import AppDeployer, StepReporter, UpdateResult
from wasm.deployers.recorder import (
    CapturingLogger,
    DeploymentRecorder,
    checkout_git_info,
    recorder_for,
    recording,
)
from wasm.deployers.registry import DeployerRegistry
from wasm.deployers.releases import persistent_path
from wasm.managers.cert_manager import CertManager
from wasm.managers.nginx_manager import NginxManager
from wasm.managers.service_manager import ServiceManager
from wasm.managers.source_manager import SourceManager
from wasm.validators.names import resolve_within

#: Building images pulls layers and compiles; give it room but not forever.
BUILD_TIMEOUT = 1800

#: Bringing a stack up or down, and every query about it.
COMPOSE_TIMEOUT = 300

# Compose file priority order
COMPOSE_FILE_PRIORITY = [
    "docker-compose.prod.yml",
    "docker-compose.prod.yaml",
    "docker-compose.yml",
    "docker-compose.yaml",
    "compose.yml",
    "compose.yaml",
]

#: What Compose itself strips from a directory name before using it as a
#: project name: everything outside [a-z0-9_-], then any leading run of
#: characters that are not a letter or digit.
_PROJECT_NAME_INVALID_CHARS = re.compile(r"[^a-z0-9_-]+")
_PROJECT_NAME_LEADING_JUNK = re.compile(r"^[^a-z0-9]+")

#: ``COMPOSE_PROJECT_NAME`` set in the project's ``.env``, which Compose reads.
_ENV_PROJECT_NAME = re.compile(r"^\s*(?:export\s+)?COMPOSE_PROJECT_NAME\s*=", re.MULTILINE)


def _names_its_own_project(compose_path: Path) -> bool:
    """
    Tell whether a stack chooses its project name itself.

    Args:
        compose_path: The compose file in use.

    Returns:
        True when the file has a top-level ``name:`` or the project's ``.env``
        sets ``COMPOSE_PROJECT_NAME``; also when the file exists but cannot
        be read, so that nothing is pinned over a name that could not be
        checked. A file that is not there names nothing.
    """
    try:
        document = yaml.safe_load(compose_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return False
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        return True
    if isinstance(document, dict) and document.get("name"):
        return True
    env_file = compose_path.parent / ".env"
    try:
        env_text = env_file.read_text(encoding="utf-8") if env_file.is_file() else ""
    except (OSError, UnicodeDecodeError):
        return True
    return _ENV_PROJECT_NAME.search(env_text) is not None


def compose_project_name(app_path: Path, compose_path: Path | None) -> str | None:
    """
    Reproduce the Compose project name this stack has always had.

    Every ``docker compose`` invocation in this module runs with ``app_path``
    as its working directory and an absolute ``-f`` built from it; the
    systemd unit does the same with ``COMPOSE_FILE`` and ``WorkingDirectory``.
    With no ``-p``, no ``COMPOSE_PROJECT_NAME`` and no top-level ``name:`` in
    the compose file, Compose names the project - and therefore every
    container, network and named volume (``<project>_<volume>``) - after the
    directory of that first file: lower-cased, restricted to
    ``[a-z0-9_-]``, and trimmed to start on a letter or digit. That directory
    is ``app_path`` itself unless ``compose_file`` names a subdirectory, in
    which case it is that subdirectory.

    Passing this value back as ``-p`` does not change what Compose would
    already have inferred for either layout; it stops that inference from
    depending on the working directory, the exact ``-f`` path built, or
    Compose's own algorithm never changing - any of which silently renames
    the project, and the day one does, ``docker compose up`` creates new,
    empty volumes instead of reusing the ones with the data. It deliberately
    keeps naming a subdirectory-based project after that subdirectory rather
    than after ``app_path``: repointing it would be exactly the rename this
    exists to prevent, for every application already deployed that way.

    A stack that names its project itself - a top-level ``name:`` in the
    compose file, or ``COMPOSE_PROJECT_NAME`` in the project's ``.env`` - is
    not pinned at all: ``-p`` overrides both, and the name 1.x and the unit
    (which never passes ``-p``) have always used is the stack's own.

    Args:
        app_path: The application directory the source was fetched to.
        compose_path: The compose file in use, or ``None`` before discovery,
            in which case ``app_path`` itself is normalised.

    Returns:
        The Compose project name for this stack, or None when the stack
        names its own project and no ``-p`` must be passed.
    """
    if compose_path is not None and _names_its_own_project(compose_path):
        return None
    source_dir = compose_path.parent if compose_path is not None else app_path
    name = _PROJECT_NAME_INVALID_CHARS.sub("", source_dir.name.lower())
    name = _PROJECT_NAME_LEADING_JUNK.sub("", name)
    return name or "default"


def compose_file_option(raw: str) -> PurePosixPath:
    """
    Validate the ``compose_file`` a deployment names, before anything is fetched.

    The same rule as a persistent path, applied by the same function: the file
    is handed to ``docker compose -f``, which runs as root, so a name that is
    absolute or climbs with ``..`` would read a file from anywhere on the host.
    Whether it exists, and whether a symlink on the way leads out, can only be
    known once the source is on disk; :func:`compose_file_in` checks that.

    Args:
        raw: The path as the operator gave it, relative to the project root.

    Returns:
        The normalized relative path.

    Raises:
        DeploymentError: When the path is empty, absolute, contains ``..`` or
            a NUL byte.
    """
    try:
        return persistent_path(raw)
    except DeploymentError as exc:
        raise DeploymentError(
            f"Compose file {raw!r} is not a path inside the application",
            details="Name the compose file relative to the project root, such as "
            "'docker-compose.prod.yml' or 'docker/compose.yml'. Absolute paths and "
            "'..' are refused because the file is read as root.",
        ) from exc


_UNIT_COMPOSE_FILE = re.compile(
    r'^Environment="COMPOSE_FILE=((?:[^"\\]|\\.)*)"[ \t]*$', re.MULTILINE
)


def compose_file_from_unit(unit_text: str | None) -> str | None:
    """
    Read back the compose file a Compose application's unit names.

    A deploy records a compose file other than the default names only in the
    unit, as ``Environment="COMPOSE_FILE=..."``. An update reads it from there,
    because rediscovering would find only the root-level default names and
    rebuild a different stack, or none.

    Args:
        unit_text: The unit file's content, or None when there is no unit.

    Returns:
        The compose file relative to the application, as the deploy chose it,
        or None when the unit names none.
    """
    if not unit_text:
        return None
    match = _UNIT_COMPOSE_FILE.search(unit_text)
    if match is None:
        return None
    # Undo the template's env_value escaping: specifiers doubled, backslashes and quotes escaped.
    value = re.sub(r"\\(.)", r"\1", match.group(1).replace("%%", "%"))
    return value or None


def compose_file_in(app_path: Path, relative: PurePosixPath | str) -> Path:
    """
    Locate a compose file in a fetched project, refusing any way out of it.

    Args:
        app_path: The application directory the source was fetched to.
        relative: The compose file, relative to ``app_path``.

    Returns:
        ``app_path / relative``, unresolved, so it can still be expressed
        relative to ``app_path`` for the systemd unit.

    Raises:
        DeploymentError: When a symlink in the project resolves the path
            outside ``app_path``, or when it is not an existing file.
    """
    path = app_path / relative
    try:
        resolve_within(app_path, str(relative))
    except (SecurityError, ValidationError) as exc:
        raise DeploymentError(
            f"Compose file {str(relative)!r} resolves outside the application",
            details=f"A symlink in the repository leads out of {app_path}. Commit the "
            "compose file itself instead of a link to it; the file is read as root, so "
            "only files inside the project are accepted.",
        ) from exc
    if not path.is_file():
        raise DeploymentError(
            f"Specified compose file not found: {relative}",
            details=f"Looked for {path}. Check the path is relative to the project root "
            "and that the file is committed on the branch being deployed.",
        )
    return path


@dataclass
class DockerComposeService:
    """Represents a service from a Docker Compose file."""

    name: str = ""
    image: str | None = None
    build: str | None = None
    ports: list[str] = field(default_factory=list)
    volumes: list[str] = field(default_factory=list)
    depends_on: list[str] = field(default_factory=list)
    environment: dict[str, str] = field(default_factory=dict)
    healthcheck: dict | None = None
    is_web: bool = False


class DockerComposeDeployer(AppDeployer):
    """
    Deployer for Docker Compose applications.

    Standalone deployer (does not inherit BaseDeployer) that manages
    the full lifecycle of Docker Compose applications including
    deployment, updates, and removal.
    """

    APP_TYPE = "docker-compose"
    DISPLAY_NAME = "Docker Compose"

    # A compose file states how the author wants the whole thing run, which
    # beats guessing from a package.json. See interface.py for the full order.
    DETECTION_PRIORITY = 80
    DEFAULT_PORT = 3000

    DETECTION_FILES: ClassVar[list[str]] = [
        "docker-compose.prod.yml",
        "docker-compose.prod.yaml",
        "docker-compose.yml",
        "docker-compose.yaml",
        "compose.yml",
        "compose.yaml",
    ]

    def __init__(
        self,
        verbose: bool = False,
        runner: CommandRunner | None = None,
        fs: FileSystem | None = None,
    ):
        """
        Args:
            verbose: Enable verbose logging.
            runner: Command runner used for every docker invocation. Defaults to
                the process-wide runner, which is what enforces --dry-run.
            fs: Filesystem every change goes through. Defaults to the
                process-wide one, for the same reason.
        """
        self.verbose = verbose
        self._runner = runner
        self._fs = fs
        # Capturable, so the deployment history holds the whole build log.
        self.logger = CapturingLogger(verbose=verbose)
        self.config = Config()
        self.store = get_store()
        self.trigger: str = DeploymentTrigger.CLI.value
        self._is_new_deployment = True
        self.replace_existing = False

        # Deployment state
        self.domain = ""
        self.app_name = ""
        self.app_path = Path()
        self.source = ""
        self.branch: str | None = None
        self.webserver = "nginx"
        self.ssl = True
        self.env_vars: dict[str, str] = {}
        self.compose_file: str | None = None
        self.compose_profiles: list[str] = []
        self.port: int | None = None

        # Parsed state
        self.services: list[DockerComposeService] = []
        self.compose_path: Path | None = None

    # Framework config files that indicate docker-compose.yml is likely
    # just for local development (databases, caches, etc.)
    FRAMEWORK_CONFIG_FILES: ClassVar[list[str]] = [
        # Next.js
        "next.config.js",
        "next.config.mjs",
        "next.config.ts",
        # Vite
        "vite.config.js",
        "vite.config.ts",
        "vite.config.mjs",
        # Angular
        "angular.json",
        # Nuxt
        "nuxt.config.js",
        "nuxt.config.ts",
        # Svelte
        "svelte.config.js",
        # Astro
        "astro.config.mjs",
        "astro.config.ts",
        # Remix
        "remix.config.js",
        "remix.config.ts",
        # Django
        "manage.py",
    ]

    def detect(self, path: Path) -> bool:
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
            (path / f).exists() for f in ["docker-compose.prod.yml", "docker-compose.prod.yaml"]
        )

        if has_prod_compose:
            return True

        has_compose = any((path / f).exists() for f in COMPOSE_FILE_PRIORITY)

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
        has_framework = any((path / f).exists() for f in self.FRAMEWORK_CONFIG_FILES)
        if has_framework:
            return False

        return True

    @property
    def runner(self) -> CommandRunner:
        """The command runner this deployer executes through."""
        return self._runner if self._runner is not None else get_runner()

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
        **options: Any,
    ) -> None:
        """
        Configure the deployer.

        Args:
            domain: Target domain name.
            source: Git URL or local path.
            port: Override port for the Nginx proxy.
            webserver: Web server to use.
            ssl: Whether to enable SSL.
            branch: Git branch.
            env_vars: Environment variables.
            app_path: Override the directory the application lives in.
            package_manager: Ignored; images are built by docker.
            include_www: Ignored; compose stacks are proxied on one hostname.
            trigger: What initiated this deployment, recorded in the history.
            **options: ``compose_file`` selects a specific compose file,
                ``compose_profiles`` activates Docker Compose profiles,
                ``job_id`` is the background job driving this deployment, when
                there is one, and ``memory_max_mb``, ``cpu_quota_percent`` and
                ``tasks_max`` are refused rather than accepted (see raises).

        Raises:
            DeploymentError: When ``webserver`` is ``apache``, which every
                site-creation path here builds through nginx regardless of
                what is asked for; or when a memory, CPU or task limit is
                given, since this stack's containers are not in the systemd
                unit's cgroup for it to limit - the unit only runs
                ``docker compose up -d`` once and exits.
        """
        if webserver == "apache":
            raise DeploymentError(
                "Docker Compose applications can only be served through nginx",
                details="Every site this deployer creates - the simple proxy and the "
                "multi-service advanced config - is built with nginx; there is no Apache "
                "equivalent yet. Deploy with --webserver nginx (the default), or configure "
                "Apache by hand outside WASM.",
            )
        given_limits = {
            key: options.get(key)
            for key in ("memory_max_mb", "cpu_quota_percent", "tasks_max")
            if options.get(key) is not None
        }
        if given_limits:
            raise DeploymentError(
                f"{domain} runs in Docker containers, which its systemd unit's limits do not reach",
                details="The unit only runs 'docker compose up -d' and exits; the containers "
                "are not in its cgroup. Set deploy.resources.limits for each service in the "
                "compose file instead of passing memory, CPU or task limits at creation.",
            )

        self.domain = domain
        self.source = source
        self.trigger = trigger
        self.job_id = options.get("job_id")
        self.app_name = domain_to_app_name(domain)
        self.app_path = app_path or (self.config.apps_directory / self.app_name)
        self.webserver = webserver
        self.ssl = ssl
        self.branch = branch
        self.env_vars = env_vars or {}
        compose_file = options.get("compose_file")
        # Checked here, before anything is fetched, because configure() is the
        # one call the CLI, the API's deploy job and the auto deployer share.
        self.compose_file = str(compose_file_option(str(compose_file))) if compose_file else None
        self.compose_profiles = options.get("compose_profiles") or []
        self.port = port
        # A directory that already holds files - bind-mounted data among
        # them - is only deployed into when asked for (wasm create --force).
        self.replace_existing = bool(options.get("replace_existing", False))
        self.deploy_target = None

    def _compose(self, *args: str) -> list[str]:
        """
        Build a ``docker compose`` argument vector for this stack.

        ``-p`` is pinned to the project name Compose would already derive
        from ``app_path`` and the compose file (see
        :func:`compose_project_name`), so the containers, networks and named
        volumes this stack owns keep their name regardless of the working
        directory a future change might run this from.

        Args:
            args: Subcommand and its arguments.

        Returns:
            The full argument vector, including the project, file and
            profile flags.
        """
        cmd = ["docker", "compose"]
        if self.compose_path:
            project = compose_project_name(self.app_path, self.compose_path)
            if project is not None:
                cmd.extend(["-p", project])
            cmd.extend(["-f", str(self.compose_path)])
        for profile in self.compose_profiles:
            cmd.extend(["--profile", profile])
        cmd.extend(args)
        return cmd

    def _run(
        self,
        command: Sequence[str],
        timeout: int = COMPOSE_TIMEOUT,
        *,
        stream: bool = False,
    ) -> CommandResult:
        """
        Execute a command in the application directory.

        Args:
            command: Program and arguments.
            timeout: Deadline in seconds.
            stream: Report output line by line, for image builds.

        Returns:
            The command outcome.
        """
        if stream:
            return self.runner.stream(
                command, on_line=self.logger.debug, cwd=self.app_path, timeout=timeout
            )
        result = self.runner.run(command, cwd=self.app_path, timeout=timeout)
        # Printed only with --verbose, captured into the deployment log always.
        self.logger.command_output(result.stdout, result.stderr)
        return result

    def _is_headless(self) -> bool:
        """
        Check if the application has no web-facing services.

        Returns:
            True if no services expose ports (headless/worker mode).
        """
        return not any(svc.is_web for svc in self.services)

    def deploy(self) -> bool:
        """
        Execute the full deployment workflow.

        For web-facing apps (services with ports):
        1. Fetch source code
        2. Discover compose file
        3. Parse compose services
        4. Configure environment
        5. Build Docker images
        6. Create Nginx site
        7. Obtain SSL certificate
        8. Create systemd service
        9. Start and verify

        For headless/worker apps (no exposed ports):
        Steps 6 and 7 are skipped automatically.

        Returns:
            True when the stack ended up deployed.

        Every attempt is recorded in the deployment history with its log. A
        failed first deployment is undone; a failed redeployment leaves the
        application as it is and marks it failed, because undoing it would
        delete a stack that was serving.

        Raises:
            DeploymentError: If any deployment step fails, or the application
                directory already holds files and replacing them was not
                asked for.
            AppBusyError: Another operation is running on the application.
        """
        # Held for the whole deploy, and the directory claimed before the
        # fetch, which empties it: a stack's bind-mounted data lives there.
        with app_lock(self.domain, "deploy"):
            existing = self.store.get_app(self.domain)
            self._is_new_deployment = existing is None
            if self.deploy_target is None:
                self.deploy_target = claim_deploy_target(
                    self.app_path,
                    domain=self.domain,
                    existing=existing,
                    replace=self.replace_existing,
                )
            with recording(self._recorder(), git_branch=self.branch) as recorder:
                result = self._deploy_steps()
            self.last_deployment_id = recorder.deployment_id
            return result

    def _deploy_steps(self) -> bool:
        """
        Run the deployment steps.

        Returns:
            True when the stack ended up deployed.
        """
        total_steps = 9

        try:
            self.logger.step(1, total_steps, "Fetching source code")
            self._fetch_source()

            self.logger.step(2, total_steps, "Discovering compose file")
            self._discover_compose_file()

            self.logger.step(3, total_steps, "Parsing compose services")
            self._parse_compose_services()

            headless = self._is_headless()

            self.logger.step(4, total_steps, "Configuring environment")
            self._configure_environment()

            self.logger.step(5, total_steps, "Building Docker images")
            self._build_images()

            if headless:
                self.logger.step(6, total_steps, "Skipping Nginx (headless worker)")
                self.logger.substep("No services expose ports, web server not needed")
                self.logger.step(7, total_steps, "Skipping SSL (headless worker)")
                self.logger.substep("No web-facing services, certificate not needed")
                self.ssl = False
            else:
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
            self.logger.key_value("Compose File", self._compose_file_path().name)
            self.logger.key_value("Services", str(len(self.services)))
            if headless:
                self.logger.key_value("Mode", "Headless worker (no web server)")
            else:
                self.logger.key_value("SSL", "Yes" if self.ssl else "No")

            return True

        except Exception as e:
            self.logger.error(f"Deployment failed: {e}")
            if self._is_new_deployment:
                self._rollback()
            else:
                self.store.update_app_status(self.domain, AppStatus.FAILED.value)
            raise

    def _compose_file_path(self) -> Path:
        """
        Return the compose file this stack is deployed from.

        Returns:
            The discovered compose file.

        Raises:
            DeploymentError: When called before discovery ran.
        """
        if self.compose_path is None:
            raise DeploymentError(
                "No compose file has been discovered yet",
                details="_discover_compose_file() must run before the stack is used.",
            )
        return self.compose_path

    def _fetch_source(self) -> None:
        """Fetch source code via git clone or local copy."""
        if self.source_already_fetched:
            # AutoDeployer already placed the code here; fetching again would
            # clean the directory and clone a second time.
            self.logger.substep(f"Source already present at {self.app_path}")
            return
        source_manager = SourceManager(verbose=self.verbose, fs=self._fs)
        source_manager.fetch(
            source=self.source,
            destination=self.app_path,
            branch=self.branch,
        )
        self.logger.substep(f"Source fetched to {self.app_path}")

    def _discover_compose_file(self) -> None:
        """
        Find the compose file to use, inside the application directory.

        Raises:
            DeploymentError: When the named file is missing, when no default
                name is present, or when the file found resolves outside the
                application through a symlink.
        """
        if self.compose_file:
            self.compose_path = compose_file_in(self.app_path, self.compose_file)
            self.logger.substep(f"Using specified: {self.compose_file}")
            return

        for filename in COMPOSE_FILE_PRIORITY:
            if (self.app_path / filename).exists():
                # A default name is as much repository content as a named one.
                self.compose_path = compose_file_in(self.app_path, filename)
                self.logger.substep(f"Found: {filename}")
                return

        raise DeploymentError(
            "No Docker Compose file found",
            "Expected one of: " + ", ".join(COMPOSE_FILE_PRIORITY),
        )

    def _parse_compose_services(self) -> None:
        """Parse Docker Compose file and extract service definitions."""
        try:
            data = yaml.safe_load(self._compose_file_path().read_text(encoding="utf-8"))
        except yaml.YAMLError as e:
            raise DeploymentError(f"Invalid compose file: {e}") from e

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
        """
        Build Docker images defined in the compose file.

        Args:
            no_cache: Rebuild every layer from scratch.

        Raises:
            DockerError: When the build fails.
        """
        cmd = self._compose("build", "--no-cache") if no_cache else self._compose("build")

        # Streamed: an image build is minutes of silence otherwise.
        result = self._run(cmd, timeout=BUILD_TIMEOUT, stream=True)
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
            config = builder.from_docker_compose(self._compose_file_path(), self.domain)
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
                config = builder.from_docker_compose(self._compose_file_path(), self.domain)
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

        except WASMError as e:
            # A missing certificate is not a failed deployment: the stack still
            # answers over HTTP, and DNS often needs longer than the deploy.
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
            working_directory=str(self.app_path),
            description=f"Docker Compose app: {self.domain}",
            environment=service_env,
            template="docker-compose",
            compose_file=compose_file_rel,
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
        cmd = self._compose("ps", "--format", "json")

        for attempt in range(retries):
            result = self._run(cmd)
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
        """
        Register or update the application in the WASM store.

        Through the same registrar as every other deployer: a redeploy
        updates the row it has instead of failing to insert a second one,
        and keeps what this deployment does not know (the layout, the
        retention, the resource limits).
        """
        headless = self._is_headless()
        try:
            StoreRegistrar(self.store).register_app(
                domain=self.domain,
                app_type=AppType.DOCKER_COMPOSE.value,
                source=self.source,
                branch=self.branch,
                port=0 if headless else self._get_primary_port(),
                app_path=self.app_path,
                webserver="" if headless else self.webserver,
                ssl_enabled=self.ssl,
                status=AppStatus.RUNNING.value,
                is_static=False,
                env_vars=self.env_vars,
            )
        except (WASMError, sqlite3.Error) as e:
            self.logger.warning(f"Could not register app in store: {e}")

    def _recorder(self) -> DeploymentRecorder:
        """
        Build the recorder a deploy or an update writes history with.

        Returns:
            A recorder, built where every deployer's is.
        """
        return recorder_for(
            self,
            git_info=checkout_git_info(SourceManager(verbose=self.verbose), self.app_path),
        )

    def _rollback(self) -> None:
        """Clean up on deployment failure."""
        self.logger.info("Rolling back deployment...")

        # Stop containers
        if self.compose_path and self.compose_path.exists():
            self._run(self._compose("down", "--remove-orphans"))

        # Remove systemd service
        try:
            service_manager = ServiceManager(verbose=self.verbose)
            service_manager.delete_service(self.app_name)
        except (WASMError, OSError) as e:
            self.logger.debug(f"Service cleanup failed: {e}")

        # Remove nginx config (only if web-facing)
        if not self._is_headless():
            try:
                nginx = NginxManager(verbose=self.verbose)
                if nginx.site_exists(self.domain):
                    nginx.delete_site(self.domain)
                    nginx.reload()
            except (WASMError, OSError) as e:
                self.logger.debug(f"Site cleanup failed: {e}")

        # Remove app directory: only what this deploy put there.
        if self.deploy_target is not None:
            self.deploy_target.undo_fetch(self.fs, self.logger)
        elif self.app_path.exists():
            try:
                self.fs.remove_tree(self.app_path)
            except OSError as e:
                self.logger.debug(f"File cleanup failed: {e}")

        # Clean store
        try:
            self.store.delete_app(self.domain)
        except (WASMError, sqlite3.Error) as e:
            self.logger.debug(f"Store cleanup failed: {e}")

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
        result = self.runner.run(
            ["systemctl", "reload", f"{self.app_name}.service"], timeout=COMPOSE_TIMEOUT
        )
        if not result.success:
            # Fallback to restart
            service_manager.restart(self.app_name)

    def logs(self, service: str | None = None, lines: int = 50) -> str:
        """
        Get Docker Compose logs.

        Args:
            service: Specific service name (None for all).
            lines: Number of lines to show.

        Returns:
            Log output string.
        """
        cmd = self._compose("logs", "--tail", str(lines))
        if service:
            cmd.append(service)

        result = self._run(cmd)
        return result.stdout if result.success else result.stderr

    def status(self) -> dict[str, Any]:
        """
        Get Docker Compose service status.

        Returns:
            Dictionary with service status information.
        """
        result = self._run(self._compose("ps", "--format", "json"))
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

    def update(self, on_step: StepReporter | None = None) -> UpdateResult:
        """
        Rebuild the images of this stack and recreate its containers.

        This used to take no arguments, return None and pull the source itself,
        which is why the CLI could not drive it through the same call as every
        other deployer and grew a third copy of the update flow instead. The
        source is now fetched by whoever owns that step, exactly as
        :meth:`~wasm.deployers.base.BaseDeployer.update` expects.

        Args:
            on_step: Called as each step begins.

        Returns:
            What was done, for the caller to present.

        Raises:
            DeploymentError: When no compose file can be found.
            DockerError: When the build or the recreate fails.
        """
        with recording(self._recorder(), git_branch=self.branch) as recorder:
            result = self._update_steps(on_step or (lambda _message: None))
        self.last_deployment_id = recorder.deployment_id
        return result

    def _update_steps(self, report: StepReporter) -> UpdateResult:
        """
        Rebuild the images and recreate the containers.

        Args:
            report: Called as each step begins.

        Returns:
            What was done.

        Raises:
            DockerError: When the build or the recreate fails.
        """
        if self.compose_path is None:
            self._discover_compose_file()

        report("Rebuilding Docker images")
        self._build_images()

        report("Recreating containers")
        # Services that name an image instead of building one pull it here,
        # which is a download, not a local recreate.
        result = self._run(self._compose("up", "-d", "--remove-orphans"), timeout=BUILD_TIMEOUT)
        if not result.success:
            raise DockerError("Failed to update containers", result.stderr)

        self.store.update_app_status(self.domain, AppStatus.RUNNING.value)

        return UpdateResult(
            package_manager="docker compose",
            prisma_updated=False,
            # The containers were recreated by the command above, so there is
            # no unit for the caller to restart afterwards.
            is_static=True,
            start_command=" ".join(self._compose("up", "-d")),
        )

    def down(self, remove_volumes: bool = False) -> bool:
        """
        Stop and remove the stack's containers, with the file and project it runs as.

        What :func:`wasm.deployers.lifecycle.delete_app` takes a stack down
        with: the same compose file discovery and the same project name as
        every other command here, so ``docker-compose.prod.yml`` and a
        pinned project are honoured.

        Args:
            remove_volumes: Also remove the named volumes. They hold the
                stack's databases, so only when asked for explicitly.

        Returns:
            Whether docker reported success.

        Raises:
            DeploymentError: No compose file is left to take the stack down with.
        """
        if self.compose_path is None:
            self._discover_compose_file()
        down = (
            ["down", "--volumes", "--remove-orphans"]
            if remove_volumes
            else ["down", "--remove-orphans"]
        )
        result = self._run(self._compose(*down))
        if not result.success:
            self.logger.warning(f"docker compose down failed: {result.stderr.strip()}")
        return result.success

    def delete(self, remove_volumes: bool = False) -> None:
        """
        Delete the Docker Compose application.

        Args:
            remove_volumes: Also remove Docker volumes.
        """
        # Stop and remove containers
        self.down(remove_volumes=remove_volumes)

        # Remove systemd service
        try:
            service_manager = ServiceManager(verbose=self.verbose)
            service_manager.delete_service(self.app_name)
        except (WASMError, OSError) as e:
            self.logger.debug(f"Service cleanup failed: {e}")

        # Remove nginx config
        try:
            nginx = NginxManager(verbose=self.verbose)
            if nginx.site_exists(self.domain):
                nginx.delete_site(self.domain)
                nginx.reload()
        except (WASMError, OSError) as e:
            self.logger.debug(f"Site cleanup failed: {e}")

        # Clean store
        try:
            self.store.delete_site(self.domain)
            self.store.delete_service(self.app_name)
            self.store.delete_app(self.domain)
        except (WASMError, sqlite3.Error) as e:
            self.logger.debug(f"Store cleanup failed: {e}")


DeployerRegistry.register(DockerComposeDeployer)
