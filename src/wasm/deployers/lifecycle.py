# Copyright (c) 2024-2026 Yago Lopez Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
The one implementation of "update a deployed application".

The CLI, the panel's update job and the git webhook used to update in three
different ways. The CLI took a backup, pulled, rebuilt in place and restarted.
The panel and the webhook re-ran the whole deploy pipeline instead, whose fetch
step deletes the application directory and clones it again: the ``.env``
edited in the panel, every file the application had written into its own tree
(uploaded images among them) and the secrets generated from ``.env.example``
were replaced on every push. Every caller now goes through :func:`update_app`,
so the three surfaces cannot drift apart again.

The sequence, and why it is in this order:

1. A backup, so ``wasm rollback`` has somewhere to return to.
2. A non-destructive ``git pull``, or, for a tree that is not a git checkout
   or an explicit new source, a fetch that never deletes anything already in
   the tree. The ``.env`` is carried across it.
3. The type recorded at deploy time, not a fresh detection over a tree that
   now contains build output.
4. The deployer's own ``update()``: install, build, and hand the tree back to
   the service user.
5. A restart only once the build succeeded, so a broken build leaves the
   previous one serving.

That is the in-place layout, and an application on it is updated exactly that
way until it is migrated explicitly. An application on the release layout is
updated by its deployer as a whole: the source is fetched into a new release
(through the repository cache for git), built there, activated behind the
health gate and rolled back automatically when it does not answer. There is no
backup first, because the release that was serving stays on disk and is what
the rollback returns to.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from wasm.core.config import Config
from wasm.core.exceptions import ServiceError, WASMError
from wasm.core.fs import SECRET_MODE, get_fs
from wasm.core.logger import Logger
from wasm.core.store import App, DeploymentTrigger, get_store
from wasm.core.utils import domain_to_app_name
from wasm.deployers.docker_compose import DockerComposeDeployer
from wasm.deployers.helpers.layout import INPLACE, RELEASES
from wasm.deployers.interface import UpdateResult
from wasm.deployers.monorepo import MonorepoDeployer
from wasm.deployers.registry import detect_app_type, get_deployer
from wasm.deployers.releases import CURRENT_LINK, ReleaseManager
from wasm.managers.backup_manager import RollbackManager
from wasm.managers.service_manager import ServiceManager
from wasm.managers.source_manager import SourceManager
from wasm.validators.domain import validate_domain
from wasm.validators.source import validate_source

#: Called as each phase begins, with its position, the total and a description.
PhaseReporter = Callable[[int, int, str], None]

#: How many phases :func:`update_app` reports for an in-place application.
PHASES = 5

#: How many phases it reports for an application on the release layout, where
#: fetching, building, activating and checking are one deployer step.
RELEASE_PHASES = 2

#: systemd reports a unit active the instant it forks, so the process gets
#: this long to fail before its state is believed.
_SETTLE_SECONDS = 2


@dataclass(frozen=True)
class AppUpdate:
    """
    What :func:`update_app` did, for the caller to present.

    Attributes:
        domain: The application's domain.
        app_type: The type the application was rebuilt as.
        package_manager: The package manager the build used.
        prisma_updated: Whether Prisma was regenerated and migrated.
        is_static: True when nothing runs as a unit (static sites, and
            docker-compose stacks whose containers the deployer recreated).
        restarted: Units restarted to pick up the new build.
        active: Whether every restarted unit was running afterwards.
    """

    domain: str
    app_type: str
    package_manager: str
    prisma_updated: bool
    is_static: bool
    restarted: tuple[str, ...]
    active: bool


def update_app(
    domain: str,
    *,
    source: str | None = None,
    branch: str | None = None,
    package_manager: str = "auto",
    trigger: str = DeploymentTrigger.CLI.value,
    on_phase: PhaseReporter | None = None,
    on_step: Callable[[str], None] | None = None,
    logger: Logger | None = None,
    verbose: bool = False,
) -> AppUpdate:
    """
    Pull the latest code into a deployed application, rebuild it and restart it.

    Args:
        domain: Domain of the application.
        source: Fetch from this source instead of pulling the current one.
            This replaces the tree; the ``.env`` file is carried across.
        branch: Git branch to update from.
        package_manager: Node package manager, or ``auto``.
        trigger: Who asked for the update, recorded in the deployment history.
        on_phase: Called as each phase begins, with its position and the
            total: :data:`PHASES` in place, :data:`RELEASE_PHASES` on releases.
        on_step: Called as each step of the rebuild begins.
        logger: Logger for the details of each phase.
        verbose: Verbosity of the managers and the deployer.

    Returns:
        What was done.

    Raises:
        WASMError: When the application is unknown or a step fails. A failed
            build leaves the previous build running.
    """
    log = logger or Logger(verbose=verbose)
    phase = on_phase or (lambda _index, _total, _message: None)

    domain = validate_domain(domain)
    store = get_store()

    # The store holds the real path, which is what keeps legacy apps deployed
    # under a wasm- prefix updatable.
    app = store.get_app(domain)
    if app and app.app_path:
        app_path = Path(app.app_path)
    else:
        app_path = Config().apps_directory / domain_to_app_name(domain)
    app_name = app_path.name

    if not app_path.exists():
        raise WASMError(
            f"Application not found: {domain}",
            details=f"Nothing is deployed at {app_path}. Deploy it with: wasm create -d {domain}",
        )

    # An application is updated in place unless its row says releases; a
    # stand-in for a row that predates layouts says nothing, so in place.
    if app is not None and getattr(app, "layout", INPLACE) == RELEASES:
        return _update_release(
            app,
            app_path,
            source=source,
            branch=branch,
            package_manager=package_manager,
            trigger=trigger,
            phase=phase,
            on_step=on_step,
            log=log,
            verbose=verbose,
        )

    phase(1, PHASES, "Creating pre-update backup")
    try:
        backup = RollbackManager(verbose=verbose).create_pre_deploy_backup(
            domain=domain, description="Pre-update automatic backup"
        )
    except (WASMError, OSError) as exc:
        # The update is still worth doing without it; the operator is told
        # that this one has no way back.
        log.warning(f"Backup skipped: {exc}")
    else:
        log.substep(f"Backup created: {backup.id}" if backup else "No existing app to backup")

    source_manager = SourceManager(verbose=verbose)
    recorded_source = app.source if app else ""
    if source:
        phase(2, PHASES, "Fetching from new source")
        log.substep(f"Source: {source}")
        _refetch_without_deleting(source_manager, source, app_path, branch)
    elif not (app_path / ".git").exists() and recorded_source:
        # An archive or a local directory has nothing to pull from; the only
        # way to update it is to fetch it again.
        phase(2, PHASES, "Fetching the recorded source again")
        log.substep(f"Source: {recorded_source}")
        _refetch_without_deleting(source_manager, recorded_source, app_path, branch)
    else:
        phase(2, PHASES, "Pulling latest changes")
        source_manager.pull(app_path, branch=branch)

    phase(3, PHASES, "Detecting application type")
    app_type = _resolve_type(app.app_type if app else None, app_path, verbose)
    log.substep(f"Type: {app_type}")

    phase(4, PHASES, "Rebuilding")
    if app_type == "monorepo":
        result = _rebuild_monorepo(domain, app_path, app_name, on_step, verbose)
    elif app_type == "docker-compose":
        result = _rebuild_compose(domain, app_path, app_name, on_step, verbose)
    else:
        deployer = get_deployer(app_type, verbose=verbose)
        deployer.configure(
            domain=domain,
            source=str(app_path),
            app_path=app_path,
            package_manager=package_manager,
            trigger=trigger,
        )
        result = deployer.update(on_step=on_step)

    phase(5, PHASES, "Restarting")
    if result.is_static:
        restarted: tuple[str, ...] = ()
        active = True
    elif app_type == "monorepo":
        restarted, active = _restart_workspaces(app.id if app else None, app_name, log, verbose)
    else:
        restarted, active = _restart(app_name, log, verbose)

    return AppUpdate(
        domain=domain,
        app_type=app_type,
        package_manager=result.package_manager,
        prisma_updated=result.prisma_updated,
        is_static=result.is_static,
        restarted=restarted,
        active=active,
    )


def _update_release(
    app: App,
    app_path: Path,
    *,
    source: str | None,
    branch: str | None,
    package_manager: str,
    trigger: str,
    phase: PhaseReporter,
    on_step: Callable[[str], None] | None,
    log: Logger,
    verbose: bool,
) -> AppUpdate:
    """
    Update an application on the release layout.

    Args:
        app: The application's store row.
        app_path: The application directory.
        source: Fetch from this source instead of the recorded one.
        branch: Git branch; the recorded one when None.
        package_manager: Node package manager, or ``auto``.
        trigger: Who asked for the update.
        phase: Reporter for the :data:`RELEASE_PHASES` phases.
        on_step: Called as each step of the release build begins.
        log: Logger for the details of each phase.
        verbose: Verbosity of the deployer.

    Returns:
        What was done.

    Raises:
        WASMError: When there is nothing to fetch from, the type cannot build
            releases, or the release failed; a release that failed its
            health check has been rolled back by then.
    """
    phase(1, RELEASE_PHASES, "Detecting application type")
    app_type = _resolve_type(app.app_type, app_path / CURRENT_LINK, verbose)
    log.substep(f"Type: {app_type}")

    fetch_from = source or app.source
    if not fetch_from:
        raise WASMError(
            f"{app.domain} has no recorded source to build a release from",
            details=f"Pass one explicitly: wasm update {app.domain} --source <git URL or path>",
        )

    deployer = get_deployer(app_type, verbose=verbose)
    if not getattr(deployer, "SUPPORTS_RELEASES", False):
        raise WASMError(
            f"{app.domain} is on the releases layout, which {app_type} applications do not support",
            details="Redeploy it with the type it was created with.",
        )

    active = ReleaseManager(app_path).current()
    phase(2, RELEASE_PHASES, "Building and activating a new release")
    log.substep(
        f"Release {active.id} stays on disk to fall back to"
        if active is not None
        else "There is no active release to fall back to"
    )
    deployer.configure(
        domain=app.domain,
        source=fetch_from,
        # The health gate probes the port the application listens on; left
        # out, it would ask the deployer's default port, which on a server
        # with more than one application is somebody else's.
        port=app.port,
        app_path=app_path,
        branch=branch or app.branch,
        package_manager=package_manager,
        trigger=trigger,
    )
    result = deployer.update(on_step=on_step)

    # The deployer restarted the unit and saw the release answer before it
    # returned; a release that did not is rolled back and raised above.
    restarted = () if result.is_static else (app_path.name,)
    return AppUpdate(
        domain=app.domain,
        app_type=app_type,
        package_manager=result.package_manager,
        prisma_updated=result.prisma_updated,
        is_static=result.is_static,
        restarted=restarted,
        active=True,
    )


def _refetch_without_deleting(
    source_manager: SourceManager, source: str, app_path: Path, branch: str | None
) -> None:
    """
    Bring the tree up to date with a source without deleting anything in it.

    An update must never remove what the application wrote into its own tree:
    uploaded images, generated files, anything outside version control. A git
    checkout updated from a git source is reset to the new commit, which only
    touches tracked files. Any other combination is copied over the existing
    tree, so a file dropped from the source stays behind until a redeploy
    instead of taking the application's data with it.

    Args:
        source_manager: Manager the fetch goes through.
        source: The source to fetch.
        app_path: The application's directory.
        branch: Git branch to fetch.
    """
    # Carried across either way: a source that ships its own .env (a local
    # development directory usually does) must not replace production's.
    env_file = app_path / ".env"
    env_backup = env_file.read_text() if env_file.is_file() else None

    source_type, _ = validate_source(source)
    if source_type == "git" and (app_path / ".git").exists():
        source_manager.fetch(source, app_path, branch=branch, force=True)
    else:
        source_manager.fetch(source, app_path, branch=branch, clean=False)

    if env_backup is not None:
        fs = get_fs()
        fs.write_text(env_file, env_backup, mode=SECRET_MODE)


def _resolve_type(stored_type: str | None, app_path: Path, verbose: bool) -> str:
    """
    Decide which deployer rebuilds the application.

    Args:
        stored_type: The type recorded when the application was deployed.
        app_path: The application's directory.
        verbose: Verbosity of the detection.

    Returns:
        The application type.
    """
    # The initial deploy already settled the type; re-detecting can only change
    # its mind for the worse on a tree that now has build output in it.
    if stored_type and stored_type != "unknown":
        return stored_type
    return detect_app_type(app_path, verbose=verbose) or "nodejs"


def _rebuild_monorepo(
    domain: str,
    app_path: Path,
    app_name: str,
    on_step: Callable[[str], None] | None,
    verbose: bool,
) -> UpdateResult:
    """
    Rebuild every workspace of a monorepo.

    Args:
        domain: Domain of the application.
        app_path: The application's directory.
        app_name: Directory name of the application.
        on_step: Called as each step begins.
        verbose: Verbosity of the deployer.

    Returns:
        What the deployer did.
    """
    deployer = MonorepoDeployer(verbose=verbose)
    deployer.app_path = app_path
    deployer.app_name = app_name
    deployer.domain = domain
    deployer.package_manager = "pnpm"
    return deployer.update(on_step=on_step)


def _rebuild_compose(
    domain: str,
    app_path: Path,
    app_name: str,
    on_step: Callable[[str], None] | None,
    verbose: bool,
) -> UpdateResult:
    """
    Rebuild the images of a Docker Compose project and recreate its containers.

    Args:
        domain: Domain of the application.
        app_path: The application's directory.
        app_name: Directory name of the application.
        on_step: Called as each step begins.
        verbose: Verbosity of the deployer.

    Returns:
        What the deployer did.
    """
    deployer = DockerComposeDeployer(verbose=verbose)
    deployer.app_path = app_path
    deployer.app_name = app_name
    deployer.domain = domain
    return deployer.update(on_step=on_step)


def _restart(app_name: str, log: Logger, verbose: bool) -> tuple[tuple[str, ...], bool]:
    """
    Restart the application's unit and report whether it stayed up.

    Args:
        app_name: Name of the unit.
        log: Logger for the outcome.
        verbose: Verbosity of the service manager.

    Returns:
        The units restarted, and whether they were running afterwards.
    """
    service_manager = ServiceManager(verbose=verbose)
    if not service_manager.get_status(app_name).get("exists"):
        log.warning(f"No unit named {app_name}: the application may need to be redeployed")
        return (), False

    service_manager.restart(app_name)
    time.sleep(_SETTLE_SECONDS)
    return (app_name,), bool(service_manager.get_status(app_name).get("active"))


def _restart_workspaces(
    app_id: int | None, app_name: str, log: Logger, verbose: bool
) -> tuple[tuple[str, ...], bool]:
    """
    Restart every unit a monorepo runs as.

    Args:
        app_id: Store id of the application, if it is registered.
        app_name: Directory name of the application, used when it is not.
        log: Logger for the outcome.
        verbose: Verbosity of the service manager.

    Returns:
        The units restarted, and whether all of them were running afterwards.
    """
    if app_id is None:
        return _restart(app_name, log, verbose)

    service_manager = ServiceManager(verbose=verbose)
    names = [s.name for s in get_store().list_services() if s.app_id == app_id]
    restarted: list[str] = []
    failed = False
    for name in names:
        try:
            service_manager.restart(name)
        except ServiceError as exc:
            log.warning(f"Failed to restart {name}: {exc}")
            failed = True
        else:
            restarted.append(name)

    if not restarted:
        return (), False

    time.sleep(_SETTLE_SECONDS)
    # One workspace that did not come back is an application that is not up,
    # however many of its siblings did.
    active = not failed and all(
        service_manager.get_status(name).get("active") for name in restarted
    )
    return tuple(restarted), active
