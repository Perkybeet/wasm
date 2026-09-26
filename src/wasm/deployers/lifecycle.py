# Copyright (c) 2024-2026 Yago Lopez Prado
# SPDX-License-Identifier: AGPL-3.0-or-later

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

The other operations on a deployed application that every surface shares
live here for the same reason: going back to a release that is on disk
(:func:`activate_release`, behind the same health gate), listing them
(:func:`list_releases`), setting the resource limits of its units
(:func:`set_resource_limits`) and removing it (:func:`delete_app`).

Every one of them holds the application's lock
(:func:`wasm.core.applock.app_lock`) while it runs, so two of them never
interleave on one application.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from wasm.core.applock import app_lock
from wasm.core.config import Config
from wasm.core.exceptions import (
    DeploymentError,
    ServiceError,
    SourceError,
    ValidationError,
    WASMError,
)
from wasm.core.fs import SECRET_MODE, get_fs, is_rehearsal
from wasm.core.logger import Logger
from wasm.core.store import (
    App,
    AppType,
    DeploymentRecord,
    DeploymentStatus,
    DeploymentTrigger,
    ReleaseRecord,
    ReleaseStatus,
    WASMStore,
    get_store,
)
from wasm.core.utils import domain_to_app_name
from wasm.deployers.base import BaseDeployer
from wasm.deployers.docker_compose import (
    DockerComposeDeployer,
    compose_file_from_unit,
    compose_file_option,
)
from wasm.deployers.helpers.health import wait_until_healthy
from wasm.deployers.helpers.health_gate import HealthCheck, HealthGate
from wasm.deployers.helpers.layout import INPLACE, RELEASES, app_root, env_file_in
from wasm.deployers.helpers.release_build import stage_release
from wasm.deployers.interface import UpdateResult
from wasm.deployers.monorepo import MonorepoDeployer
from wasm.deployers.recorder import CapturingLogger, DeploymentRecorder, recording
from wasm.deployers.registry import detect_app_type, get_deployer
from wasm.deployers.releases import (
    CURRENT_LINK,
    REPO_CACHE_DIR,
    Release,
    ReleaseManager,
    release_order_key,
)
from wasm.managers.apache_manager import ApacheManager
from wasm.managers.backup_manager import BackupManager, RollbackManager
from wasm.managers.cert_manager import CertManager
from wasm.managers.nginx_manager import NginxManager
from wasm.managers.service_manager import ResourceLimits, ServiceManager
from wasm.managers.source_manager import SourceManager, validate_commit_id
from wasm.managers.webserver import delete_site_completely
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
        deployment_id: Id of the deployment history row this update wrote,
            when recording succeeded. None if it failed (see
            :class:`~wasm.deployers.recorder.DeploymentRecorder`).
    """

    domain: str
    app_type: str
    package_manager: str
    prisma_updated: bool
    is_static: bool
    restarted: tuple[str, ...]
    active: bool
    deployment_id: int | None = None


def update_app(
    domain: str,
    *,
    source: str | None = None,
    branch: str | None = None,
    commit: str | None = None,
    package_manager: str = "auto",
    trigger: str = DeploymentTrigger.CLI.value,
    job_id: str | None = None,
    on_phase: PhaseReporter | None = None,
    on_step: Callable[[str], None] | None = None,
    logger: Logger | None = None,
    verbose: bool = False,
    rollback_of: int | None = None,
) -> AppUpdate:
    """
    Pull the latest code into a deployed application, rebuild it and restart it.

    With ``commit``, that exact commit is deployed instead of the head of the
    branch: "rebuild this deployment". On releases, a release built from it
    that is still on disk and not active is activated behind the health gate
    (instant, nothing is rebuilt); otherwise the commit is exported from the
    repository cache into a new release and built. In place, the checkout is
    detached at the commit and rebuilt; the next update without a commit goes
    back to following the branch (see
    :meth:`~wasm.managers.source_manager.SourceManager.checkout_commit`).

    Args:
        domain: Domain of the application.
        source: Fetch from this source instead of pulling the current one.
            This replaces the tree; the ``.env`` file is carried across.
        branch: Git branch to update from.
        commit: Deploy this commit, full or abbreviated; it must name exactly
            one commit, and is fetched when the clone does not have it.
        package_manager: Node package manager, or ``auto``.
        trigger: Who asked for the update, recorded in the deployment history.
        job_id: The background job driving this update, when the panel
            queued it. Recorded on the deployment history row.
        on_phase: Called as each phase begins, with its position and the
            total: :data:`PHASES` in place, :data:`RELEASE_PHASES` on releases.
        on_step: Called as each step of the rebuild begins.
        logger: Logger for the details of each phase.
        verbose: Verbosity of the managers and the deployer.
        rollback_of: The deployment this update goes back to, when it is
            :func:`rollback_to_deployment` rebuilding that deployment's
            commit. In place, the restart then passes the health gate, and
            one that does not answer fails the update (its row is
            ``failed``); once it does, the deployment that was serving is
            marked rolled back, as any rollback marks it.

    Returns:
        What was done.

    Raises:
        WASMError: When the application is unknown or a step fails. A failed
            build leaves the previous build running.
        SourceError: The commit is not a commit id, names more than one
            commit or none, or the application is not deployed from git.
        ValidationError: A commit was given together with a source or a
            branch, which it makes meaningless.
        AppBusyError: Another operation is running on the application.
    """
    domain = validate_domain(domain)
    if commit is not None:
        commit = validate_commit_id(commit)
        if source or branch:
            raise ValidationError(
                "A commit names exactly what to deploy; a source or a branch does not apply",
                details=f"Run either 'wasm update {domain} --commit {commit}' or an update "
                "from a source or a branch, not both.",
            )
    # Held for the whole update, backup to restart: a rollback, a migration
    # or a second update (a webhook firing while an operator updates by hand)
    # must not interleave with it. Refused at once, naming this update.
    try:
        with app_lock(domain, "update"):
            outcome = _update_app(
                domain,
                source=source,
                branch=branch,
                commit=commit,
                package_manager=package_manager,
                trigger=trigger,
                job_id=job_id,
                on_phase=on_phase,
                on_step=on_step,
                logger=logger,
                verbose=verbose,
                gated=rollback_of is not None,
            )
            # A rehearsal recorded nothing, and must not rewrite real rows.
            if rollback_of is not None and not is_rehearsal():
                _mark_replaced_rolled_back(
                    get_store(), domain, outcome.deployment_id, logger or Logger(verbose=verbose)
                )
            return outcome
    finally:
        # Whatever happened, what is live may have changed.
        _forget_upstream(domain)


def _update_app(
    domain: str,
    *,
    source: str | None,
    branch: str | None,
    commit: str | None,
    package_manager: str,
    trigger: str,
    job_id: str | None,
    on_phase: PhaseReporter | None,
    on_step: Callable[[str], None] | None,
    logger: Logger | None,
    verbose: bool,
    gated: bool = False,
) -> AppUpdate:
    """
    Update an application whose lock the caller holds.

    Args:
        domain: A validated domain.
        source: Fetch from this source instead of pulling the current one.
        branch: Git branch to update from.
        commit: A validated commit id to deploy instead of the branch head.
        package_manager: Node package manager, or ``auto``.
        trigger: Who asked for the update.
        job_id: The background job driving this update, when there is one.
        on_phase: Called as each phase begins.
        on_step: Called as each step of the rebuild begins.
        logger: Logger for the details of each phase.
        verbose: Verbosity of the managers and the deployer.
        gated: In place, restart behind the health gate and fail when it
            does not answer, instead of restarting and reporting.

    Returns:
        What was done.

    Raises:
        WASMError: When the application is unknown or a step fails.
        DeploymentError: Gated, the application did not answer after the
            restart.
    """
    log = logger or Logger(verbose=verbose)
    phase = on_phase or (lambda _index, _total, _message: None)

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
            commit=commit,
            package_manager=package_manager,
            trigger=trigger,
            job_id=job_id,
            phase=phase,
            on_step=on_step,
            log=log,
            verbose=verbose,
        )

    if commit is not None and not (app_path / ".git").exists():
        # Checked before the backup: nothing is worth doing for a tree that
        # has no history to take the commit from.
        raise SourceError(
            f"{domain} is not a git checkout; there is no commit {commit} to deploy",
            details=f"An application deployed from a directory or an archive is rebuilt "
            f"from its source: wasm update {domain}",
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
    # Read before the checkout moves: what a monorepo or a stack goes back to.
    previous_commit = _serving_commit(source_manager, app, app_path)
    if commit is not None:
        phase(2, PHASES, f"Checking out commit {commit}")
        full = source_manager.checkout_commit(app_path, commit)
        log.substep(
            f"Commit {full[:7]}, detached: the next update without --commit follows the "
            "branch again"
        )
    elif source:
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
        result, deployment_id = _rebuild_monorepo(
            domain, app_path, app_name, on_step, verbose, trigger, job_id, previous_commit
        )
    elif app_type == "docker-compose":
        result, deployment_id = _rebuild_compose(
            domain, app_path, app_name, on_step, verbose, trigger, job_id, previous_commit
        )
    else:
        deployer = get_deployer(app_type, verbose=verbose)
        deployer.configure(
            domain=domain,
            source=str(app_path),
            # The health gate probes the port the application listens on,
            # not the deployer's default, which is somebody else's.
            port=app.port if app is not None else None,
            app_path=app_path,
            package_manager=package_manager,
            trigger=trigger,
            job_id=job_id,
            # Restarted behind the health gate inside the deployer's own
            # recording, so a build that does not answer is a failed row
            # with the probes in its log.
            gate_in_place=gated,
        )
        result = deployer.update(on_step=on_step)
        # getattr: a duck-typed test double implements configure/update/deploy
        # only, per the AppDeployer contract, and must not have to grow this
        # attribute just to be updatable.
        deployment_id = getattr(deployer, "last_deployment_id", None)

    phase(5, PHASES, "Restarting")
    if result.is_static:
        restarted: tuple[str, ...] = ()
        active = True
    elif result.restarted is not None:
        # The deployer restarted and probed them behind the health gate.
        restarted, active = result.restarted, bool(result.restarted)
    elif app_type == "monorepo":
        restarted, active = _restart_workspaces(app.id if app else None, app_name, log, verbose)
    elif gated and app is not None:
        # A deployer that did not gate its own restart (one that is not a
        # BaseDeployer): the same gate, here.
        healthy, evidence = health_gate_for(app, store, log).restart_and_probe()
        if not healthy:
            error = DeploymentError(f"{domain} did not pass its health check", details=evidence)
            _record_failure_after_the_fact(store, deployment_id, error, log)
            raise error
        restarted, active = (app_name,), True
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
        deployment_id=deployment_id,
    )


def _update_release(
    app: App,
    app_path: Path,
    *,
    source: str | None,
    branch: str | None,
    commit: str | None,
    package_manager: str,
    trigger: str,
    job_id: str | None,
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
        commit: Deploy this commit instead of the head of the branch: the
            newest release built from it that is on disk and not active is
            activated, and otherwise it is built as a new release.
        package_manager: Node package manager, or ``auto``.
        trigger: Who asked for the update.
        job_id: The background job driving this update, when there is one.
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

    if commit is not None and validate_source(fetch_from)[0] != "git":
        raise SourceError(
            f"{app.domain} is not deployed from git; there is no commit {commit} to deploy",
            details=f"Its source is {fetch_from}. Rebuild it from there: wasm update {app.domain}",
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
        job_id=job_id,
    )

    releases = ReleaseManager(app_path)
    active = releases.current()
    if commit is not None:
        # Only the release pipeline of BaseDeployer can be handed a staged
        # release; every type that supports releases is built on it.
        if not isinstance(deployer, BaseDeployer):
            raise DeploymentError(
                f"{app_type} applications cannot deploy a single commit",
                details=f"Update {app.domain} from its branch instead: wasm update {app.domain}",
            )
        source_manager = deployer.source_manager
        full = _resolve_in_cache(source_manager, fetch_from, app_path, branch or app.branch, commit)
        # A release that failed its health gate when it was last activated
        # is still on disk; activating it again would serve what the gate
        # refused. Its commit is built afresh instead.
        failed = _failed_releases(get_store(), app)
        built = next(
            (
                r
                for r in releases.list()
                if not r.active and r.id not in failed and r.commit and full.startswith(r.commit)
            ),
            None,
        )
        if built is not None:
            phase(2, RELEASE_PHASES, f"Activating release {built.id}, built from {full[:7]}")
            activation = _activate_release(app, built.id, trigger=trigger, log=log)
            return AppUpdate(
                domain=app.domain,
                app_type=app_type,
                package_manager=package_manager,
                prisma_updated=False,
                is_static=app.is_static,
                restarted=() if app.is_static else (app_path.name,),
                active=True,
                deployment_id=activation.deployment_id,
            )
        phase(2, RELEASE_PHASES, f"Building commit {full[:7]} as a new release")
        deployer.adopt_release(
            stage_release(
                fetch_from,
                branch or app.branch,
                releases=deployer.releases,
                source_manager=source_manager,
                logger=log,
                commit=full,
            )
        )
    else:
        phase(2, RELEASE_PHASES, "Building and activating a new release")
    log.substep(
        f"Release {active.id} stays on disk to fall back to"
        if active is not None
        else "There is no active release to fall back to"
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
        deployment_id=getattr(deployer, "last_deployment_id", None),
    )


def _resolve_in_cache(
    source_manager: SourceManager, source: str, app_path: Path, branch: str | None, commit: str
) -> str:
    """
    Name the commit to deploy, from the repository cache of a release application.

    The cache is cloned first when there is none yet; one that exists is only
    fetched when it does not have the commit.

    Args:
        source_manager: Manager git goes through.
        source: The application's git source.
        app_path: The application directory.
        branch: The branch the cache follows.
        commit: A validated commit id.

    Returns:
        The full commit id.

    Raises:
        SourceError: The commit names none or more than one commit, or git failed.
    """
    cache = app_path / REPO_CACHE_DIR
    if not (cache / ".git").is_dir():
        source_manager.sync_cache(source, cache, branch)
    return source_manager.resolve_commit(cache, commit)


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
    env_file = env_file_in(app_path, INPLACE)
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


def _serving_commit(source_manager: SourceManager, app: App | None, app_path: Path) -> str | None:
    """
    Read the commit an in-place monorepo or stack serves, before an update moves it.

    Only those two types go back by commit when the update fails its health
    gate (their deployers check it out again); every other type is either on
    releases or keeps 1.x behaviour, and pays nothing for this.

    Args:
        source_manager: Manager git goes through.
        app: The application's row, if it has one.
        app_path: The application directory.

    Returns:
        The commit, or None for another type, a tree that is not a git
        checkout, or one whose commit cannot be read.
    """
    if app is None or app.app_type not in (AppType.MONOREPO.value, AppType.DOCKER_COMPOSE.value):
        return None
    if not (app_path / ".git").exists():
        return None
    commit = source_manager.get_repo_info(app_path).get("commit")
    return str(commit) if commit else None


def _rebuild_monorepo(
    domain: str,
    app_path: Path,
    app_name: str,
    on_step: Callable[[str], None] | None,
    verbose: bool,
    trigger: str,
    job_id: str | None = None,
    previous_commit: str | None = None,
) -> tuple[UpdateResult, int | None]:
    """
    Rebuild every workspace of a monorepo, and restart each behind the health gate.

    Args:
        domain: Domain of the application.
        app_path: The application's directory.
        app_name: Directory name of the application.
        on_step: Called as each step begins.
        verbose: Verbosity of the deployer.
        trigger: Who asked, recorded in the deployment history.
        job_id: The background job driving this update, when there is one.
        previous_commit: The commit the tree was on before this update: what
            the deployer checks out and rebuilds when a workspace does not answer.

    Returns:
        What the deployer did, and the deployment history row it wrote.
    """
    deployer = MonorepoDeployer(verbose=verbose)
    deployer.app_path = app_path
    deployer.app_name = app_name
    deployer.domain = domain
    deployer.trigger = trigger
    deployer.job_id = job_id
    deployer.package_manager = "pnpm"
    deployer.previous_commit = previous_commit
    result = deployer.update(on_step=on_step)
    return result, getattr(deployer, "last_deployment_id", None)


def _rebuild_compose(
    domain: str,
    app_path: Path,
    app_name: str,
    on_step: Callable[[str], None] | None,
    verbose: bool,
    trigger: str,
    job_id: str | None = None,
    previous_commit: str | None = None,
) -> tuple[UpdateResult, int | None]:
    """
    Rebuild the images of a Docker Compose project and recreate its containers.

    The deployer records what serves before building, and puts it back when
    the new containers do not pass the health gate (see
    :class:`~wasm.deployers.docker_compose.ServingState`).

    Args:
        domain: Domain of the application.
        app_path: The application's directory.
        app_name: Directory name of the application.
        on_step: Called as each step begins.
        verbose: Verbosity of the deployer.
        trigger: Who asked, recorded in the deployment history.
        job_id: The background job driving this update, when there is one.
        previous_commit: The commit the tree was on before this update: the
            compose file a failed update checks out again.

    Returns:
        What the deployer did, and the deployment history row it wrote.
    """
    deployer = DockerComposeDeployer(verbose=verbose)
    deployer.app_path = app_path
    deployer.app_name = app_name
    deployer.domain = domain
    unit_compose_file = compose_file_from_unit(
        ServiceManager(verbose=verbose).get_service_config(app_name)
    )
    if unit_compose_file:
        deployer.compose_file = str(compose_file_option(unit_compose_file))
    deployer.trigger = trigger
    deployer.job_id = job_id
    deployer.previous_commit = previous_commit
    result = deployer.update(on_step=on_step)
    return result, getattr(deployer, "last_deployment_id", None)


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
    Restart every unit a monorepo runs as, when its deployer left that to the caller.

    A registered monorepo's deployer restarts and probes its units itself,
    behind the health gate (:meth:`~wasm.deployers.monorepo.MonorepoDeployer.update`);
    this is what restarts the rest.

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


# ---------------------------------------------------------------------------
# Releases: what there is, and going back to one
# ---------------------------------------------------------------------------

#: Failures while writing release bookkeeping. The link on disk is the truth;
#: a row that could not be written is reported, never fatal.
_RECORDING_ERRORS = (WASMError, sqlite3.Error)


@dataclass(frozen=True)
class ReleaseInfo:
    """
    One release of an application, as the CLI and the API show it.

    Attributes:
        id: Release id, the directory name.
        commit: Short commit it was built from, or None for a non-git source.
        created_at: When it was created, ISO 8601 in UTC.
        activated_at: When it last became active, if it ever did.
        status: ``active``, ``superseded``, ``rolled_back``, ``failed`` or
            ``built``, from the store; ``active`` whenever ``current`` points
            at it, whatever the row says.
        active: Whether ``current`` points at it.
        on_disk: Whether its directory still exists. A release that failed
            and was removed is still listed while its row is kept, and cannot
            be activated.
    """

    id: str
    commit: str | None
    created_at: str
    activated_at: str | None
    status: str
    active: bool
    on_disk: bool


@dataclass(frozen=True)
class ReleaseActivation:
    """
    What :func:`activate_release` did.

    Attributes:
        domain: The application's domain.
        release: The release now active.
        previous: The release that was active before, if any.
        changed: False when the release was already active and nothing was
            done: no restart, no history row.
        went_back: Whether the release activated is older than the one it
            replaced, which makes this a rollback.
        deployment_id: The history row, when one was written.
    """

    domain: str
    release: Release
    previous: Release | None
    changed: bool
    went_back: bool
    deployment_id: int | None


def list_releases(domain: str) -> list[ReleaseInfo]:
    """
    List the releases of an application on the release layout.

    The directories are the truth about what exists and which is active; the
    store adds what a directory cannot say (when it was activated, how it
    ended) and remembers the recent failures that were removed from disk.

    Args:
        domain: The application's domain.

    Returns:
        Releases, newest first.

    Raises:
        WASMError: The application is unknown.
        DeploymentError: It is not on the release layout.
    """
    app = _release_app(validate_domain(domain))
    store = get_store()
    rows = {row.id: row for row in store.list_releases(app.id)} if app.id is not None else {}
    on_disk = {release.id: release for release in ReleaseManager(app_root(app)).list()}

    infos: list[ReleaseInfo] = []
    for release_id in sorted({*rows, *on_disk}, key=release_order_key, reverse=True):
        release = on_disk.get(release_id)
        row = rows.get(release_id)
        if release is not None:
            commit, created_at, active = release.commit, release.created_at, release.active
        else:
            commit = row.git_commit if row is not None else None
            created_at = (row.created_at if row is not None else None) or ""
            active = False
        if active:
            status = ReleaseStatus.ACTIVE.value
        else:
            status = row.status if row is not None else ReleaseStatus.BUILT.value
        infos.append(
            ReleaseInfo(
                id=release_id,
                commit=commit,
                created_at=created_at,
                activated_at=row.activated_at if row is not None else None,
                status=status,
                active=active,
                on_disk=release is not None,
            )
        )
    return infos


def activate_release(
    domain: str,
    release_id: str | None = None,
    *,
    trigger: str = DeploymentTrigger.CLI.value,
    logger: Logger | None = None,
    verbose: bool = False,
) -> ReleaseActivation:
    """
    Make an existing release the one that serves: an instant rollback.

    ``current`` is swapped atomically, the unit restarted, and the release
    kept only if it passes the same health gate a deploy does. One that does
    not is recorded as failed and the release that was serving is activated
    and restarted again, so the operator ends where they started.

    Recorded in the deployment history as its own row, with its log. When the
    release activated is older than the one it replaced, the most recent
    successful deployment - the build that stopped serving - is marked rolled
    back, as a rollback from a backup does.

    Args:
        domain: The application's domain.
        release_id: The release to activate. None means the one created just
            before the active one.
        trigger: Who asked, recorded in the history: ``cli``, ``panel`` or
            ``webhook``.
        logger: Logger for the progress. Its output is captured into the
            history row's log when it is a :class:`CapturingLogger`.
        verbose: Verbosity of the default logger.

    Returns:
        What was done.

    Raises:
        WASMError: The application is unknown.
        DeploymentError: It is not on the release layout, the release does
            not exist or there is nothing earlier to go back to, or the
            release did not pass the health gate (the previous one is active
            again by then).
        AppBusyError: Another operation is running on the application.
    """
    log = logger if logger is not None else CapturingLogger(verbose=verbose)
    app = _release_app(validate_domain(domain))
    # An update pruning releases, or a second activation, must not run while
    # this one re-points current and restarts.
    try:
        with app_lock(app.domain, "release activation"):
            return _activate_release(app, release_id, trigger=trigger, log=log)
    finally:
        _forget_upstream(app.domain)


def _activate_release(
    app: App, release_id: str | None, *, trigger: str, log: Logger
) -> ReleaseActivation:
    """
    Activate a release of an application whose lock the caller holds.

    Args:
        app: The application, on the release layout.
        release_id: The release to activate, or None for the one before the
            active one.
        trigger: Who asked, recorded in the history.
        log: Logger for the progress.

    Returns:
        What was done.

    Raises:
        DeploymentError: See :func:`activate_release`.
    """
    root = app_root(app)
    releases = ReleaseManager(root, logger=log)
    previous = releases.current()
    target = _activation_target(releases, previous, release_id)

    if previous is not None and previous.id == target.id:
        log.info(f"Release {target.id} is already active; nothing to do")
        return ReleaseActivation(
            domain=app.domain,
            release=target,
            previous=previous,
            changed=False,
            went_back=False,
            deployment_id=None,
        )

    went_back = previous is not None and release_order_key(target.id) < release_order_key(
        previous.id
    )
    store = get_store()
    recorder = DeploymentRecorder(
        store,
        app.domain,
        trigger,
        logger=log,
        git_info=lambda: (target.commit, _cache_branch(root, app.branch)),
    )
    with recording(recorder, git_branch=app.branch):
        releases.activate(target.path)
        log.substep(
            f"Activated release {target.id}"
            + (f" (was {previous.id})" if previous is not None else "")
        )
        healthy, evidence = health_gate_for(app, store, log).restart_and_probe()
        if not healthy:
            _set_release_status(store, app, target.id, ReleaseStatus.FAILED, log)
            raise _restore_previous(app, store, releases, target, previous, evidence, log)

        _record_activation(store, app, target, previous if went_back else None, log)
        if went_back:
            recorder.mark_previous_success_rolled_back()
    return ReleaseActivation(
        domain=app.domain,
        release=target,
        previous=previous,
        changed=True,
        went_back=went_back,
        deployment_id=recorder.deployment_id,
    )


def _release_app(domain: str) -> App:
    """
    Read the row of an application that must be on the release layout.

    Args:
        domain: A validated domain.

    Returns:
        The row.

    Raises:
        WASMError: The application is unknown.
        DeploymentError: It is in place, where there are no releases.
    """
    app = get_store().get_app(domain)
    if app is None:
        raise WASMError(
            f"Application not found: {domain}",
            details="Run 'wasm list' to see what is deployed.",
        )
    if app.layout != RELEASES:
        raise DeploymentError(
            f"{domain} is deployed in place and has no releases",
            details=f"Roll back to a backup with 'wasm rollback {domain}', or move it onto "
            f"releases with 'wasm app migrate {domain}'.",
        )
    return app


def _activation_target(
    releases: ReleaseManager, previous: Release | None, release_id: str | None
) -> Release:
    """
    Find the release to activate.

    Args:
        releases: The application's releases.
        previous: The active release.
        release_id: The one asked for, or None for the one before the active one.

    Returns:
        The release.

    Raises:
        DeploymentError: It does not exist, or there is nothing earlier.
    """
    listed = releases.list()
    if release_id is not None:
        found = next((r for r in listed if r.id == release_id), None)
        if found is None:
            ids = ", ".join(r.id for r in listed) or "none"
            raise DeploymentError(
                f"Release {release_id!r} does not exist",
                details=f"Releases on disk, newest first: {ids}.",
            )
        return found
    if previous is None:
        raise DeploymentError(
            "There is no active release to roll back from",
            details="Name the release to activate: wasm releases list <domain>.",
        )
    index = next(i for i, r in enumerate(listed) if r.id == previous.id)
    if index + 1 >= len(listed):
        raise DeploymentError(
            f"Release {previous.id} is the oldest one; there is nothing earlier to roll back to",
            details="Older releases were pruned or never existed. Deploy a known-good commit "
            "instead, or restore a backup.",
        )
    return listed[index + 1]


def health_gate_for(
    app: App,
    store: WASMStore,
    log: Logger,
    *,
    restart: Callable[[], object] | None = None,
) -> HealthGate:
    """
    Build the health gate for an application from what the store records.

    A deploy builds its gate from the deployer, which knows the build; an
    activation or a migration only has the row, and asks the same questions
    of it: which unit serves, on which port, what its health check asks, and
    for a site nothing runs for, whether the directory the web server serves
    has a page.

    Args:
        app: The application.
        store: Where its service and site rows are.
        log: Where the probes are reported.
        restart: What restarts the application, when it is more than its
            one unit (every workspace of a monorepo).

    Returns:
        The gate.
    """
    services = ServiceManager()
    service = store.get_service_by_app_id(app.id) if app.id is not None else None
    if not app.is_static:
        unit = service.name if service is not None else app_root(app).name
        check = HealthCheck.for_app(app)
        return HealthGate(
            unit=unit,
            url=check.url(app.port),
            check=check,
            services=services,
            logger=log,
            probe=wait_until_healthy,
            restart=restart,
        )

    site = store.get_site(app.domain)
    document_root = Path(site.document_root) if site and site.document_root else None
    return HealthGate(
        unit=None,
        url=None,
        services=services,
        logger=log,
        files_check=None if document_root is None else (document_root / "index.html").is_file,
    )


def _restore_previous(
    app: App,
    store: WASMStore,
    releases: ReleaseManager,
    target: Release,
    previous: Release | None,
    evidence: str,
    log: Logger,
) -> DeploymentError:
    """
    Put the release that was serving back after the target failed its gate.

    Args:
        app: The application.
        store: The store.
        releases: The application's releases.
        target: The release that failed.
        previous: The release that was serving, if any.
        evidence: What the gate saw.
        log: Where progress is reported.

    Returns:
        The error to raise, which says which release is active now.
    """
    if previous is None:
        return DeploymentError(
            f"Release {target.id} did not pass its health check", details=evidence
        )
    log.warning(f"Release {target.id} did not pass its health check")
    log.substep(f"Going back to release {previous.id}")
    releases.activate(previous.path)
    restored, _ = health_gate_for(app, store, log).restart_and_probe()
    state = "is active again" if restored else "is active again but is not answering either"
    return DeploymentError(
        f"Release {target.id} did not pass its health check; release {previous.id} {state}",
        details=evidence,
    )


def _record_activation(
    store: WASMStore, app: App, target: Release, left_behind: Release | None, log: Logger
) -> None:
    """
    Record that a release is active, and how the one it replaced ended.

    Args:
        store: The store.
        app: The application.
        target: The release now active.
        left_behind: The release rolled back from, when this went back;
            otherwise the one replaced is simply superseded.
        log: Where a failure to record is reported.
    """
    if app.id is None:
        return
    try:
        if store.get_release(app.id, target.id) is None:
            # Created before the store kept releases, or by the migration of
            # an in-place app before its row was written.
            store.record_release(
                ReleaseRecord(
                    id=target.id,
                    app_id=app.id,
                    git_commit=target.commit,
                    created_at=datetime.now(timezone.utc).isoformat(),
                    status=ReleaseStatus.BUILT.value,
                    path=str(target.path),
                )
            )
        store.mark_release_active(app.id, target.id)
        if left_behind is not None:
            store.set_release_status(app.id, left_behind.id, ReleaseStatus.ROLLED_BACK.value)
    except _RECORDING_ERRORS as exc:
        log.warning(f"Could not record release {target.id} as active: {exc}")


def _set_release_status(
    store: WASMStore, app: App, release_id: str, status: ReleaseStatus, log: Logger
) -> None:
    """
    Record how a release ended, when the store has a row for it.

    Args:
        store: The store.
        app: The application.
        release_id: The release.
        status: Its new status.
        log: Where a failure to record is reported.
    """
    if app.id is None:
        return
    try:
        store.set_release_status(app.id, release_id, status.value)
    except _RECORDING_ERRORS as exc:
        log.warning(f"Could not record release {release_id} as {status.value}: {exc}")


def _cache_branch(root: Path, recorded: str | None) -> str | None:
    """
    Name the branch a release application follows.

    Args:
        root: The application directory.
        recorded: The branch the store records.

    Returns:
        The recorded branch, or the one the repository cache is on.
    """
    if recorded or not (root / REPO_CACHE_DIR / ".git").is_dir():
        return recorded
    return SourceManager().get_repo_info(root / REPO_CACHE_DIR).get("branch")


# ---------------------------------------------------------------------------
# Is there anything new to deploy
# ---------------------------------------------------------------------------

#: Seconds the remote gets to say where a branch points. The question blocks
#: the request that asked it, so it gets far less than a fetch.
UPSTREAM_TIMEOUT = 15

#: Seconds an answer about the remote is reused.
UPSTREAM_CACHE_SECONDS = 10.0

#: One question per application at a time, and the answers already given,
#: keyed by domain and branch, with when they were given (monotonic).
_upstream_guard = threading.Lock()
_upstream_flights: dict[str, threading.Lock] = {}
_upstream_answers: dict[tuple[str, str | None], tuple[float, UpstreamState | None]] = {}

#: When rebuilding the commit that is already live is still worth it. Shared
#: by the console's ``nothing_new`` answer and the CLI's question.
NOTHING_NEW_HINT = (
    "Rebuilding the same commit still makes sense when the environment or the "
    "dependencies changed, or when the last build broke. Update with force to "
    "rebuild it anyway."
)


@dataclass(frozen=True)
class UpstreamState:
    """
    The head of the branch an application follows, against the commit it runs.

    Attributes:
        domain: The application's domain.
        branch: The branch compared.
        live_commit: The commit that is live, as recorded on disk: the active
            release's, or the in-place checkout's (short).
        remote_commit: The full commit the branch points at on the remote.
    """

    domain: str
    branch: str
    live_commit: str
    remote_commit: str

    @property
    def has_new_commits(self) -> bool:
        """Whether the branch has moved past the commit that is live."""
        return not self.remote_commit.lower().startswith(self.live_commit.lower())

    @property
    def summary(self) -> str:
        """The sentence both surfaces show when there is nothing new."""
        return f"No new commits on {self.branch} since {self.live_commit[:7]}, which is live"


def check_upstream(
    domain: str, *, branch: str | None = None, logger: Logger | None = None
) -> UpstreamState | None:
    """
    Ask the remote whether the branch has anything the live build does not.

    ``git ls-remote`` only: nothing is downloaded and nothing on disk changes,
    so it runs before the update is even queued. The console and the CLI ask
    it; the webhook does not, because a push is itself the news.

    One question per application at a time: a second caller waits for the
    answer the first is getting and shares it, and an answer is reused for
    :data:`UPSTREAM_CACHE_SECONDS`, so a burst of clicks is one ``ls-remote``.
    The remote gets :data:`UPSTREAM_TIMEOUT` seconds to answer, not the
    minutes a fetch is allowed: the question blocks the request that asked.

    Args:
        domain: The application's domain.
        branch: The branch the update would follow; the one the application
            follows when None.
        logger: Where a failure to ask is reported.

    Returns:
        The comparison, or None when there is nothing to compare: the
        application is unknown, is not deployed from git, has no live commit,
        or the remote could not be asked (reported on ``logger``; the update
        itself will say why, verbosely).
    """
    log = logger if logger is not None else Logger()
    domain = validate_domain(domain)
    with _upstream_guard:
        flight = _upstream_flights.setdefault(domain, threading.Lock())
    with flight:
        key = (domain, branch)
        cached = _upstream_answers.get(key)
        if cached is not None and time.monotonic() - cached[0] < UPSTREAM_CACHE_SECONDS:
            return cached[1]
        state = _ask_upstream(domain, branch, log)
        _upstream_answers[key] = (time.monotonic(), state)
        return state


def _ask_upstream(domain: str, branch: str | None, log: Logger) -> UpstreamState | None:
    """
    Compare the head of the remote branch with what is live, once.

    Args:
        domain: A validated domain.
        branch: The branch to ask about, or None for the one followed.
        log: Where a failure to ask is reported.

    Returns:
        See :func:`check_upstream`.
    """
    store = get_store()
    app = store.get_app(domain)
    if app is None:
        return None
    root = app_root(app)
    source: str | None
    try:
        if app.layout == RELEASES:
            source = app.source
            active = ReleaseManager(root).current()
            live = active.commit if active is not None else None
            follow = branch or app.branch or _cache_branch(root, None)
        else:
            if not (root / ".git").exists():
                return None
            info = SourceManager().get_repo_info(root)
            # Not the checkout's HEAD: after an update whose build failed the
            # checkout is on the new commit while the old build serves, and
            # the new commit is exactly what is still to be deployed.
            live = _last_good_commit(store, domain) or info.get("commit")
            source = app.source or info.get("remote")
            follow = branch or info.get("branch") or app.branch
        if not source or not live or validate_source(source)[0] != "git":
            return None
        head = SourceManager().remote_head(source, follow, timeout=UPSTREAM_TIMEOUT)
    except SourceError as exc:
        log.warning(f"Could not tell whether there is anything new to deploy: {exc.message}")
        return None
    return UpstreamState(
        domain=app.domain, branch=head.branch, live_commit=live, remote_commit=head.commit
    )


def _last_good_commit(store: WASMStore, domain: str) -> str | None:
    """
    Read the commit the last successful deployment of an in-place application built.

    Args:
        store: The store.
        domain: The application's domain.

    Returns:
        Its commit, or None when no successful deployment recorded one (a
        history older than recording, a source that is not git).
    """
    try:
        records = store.list_deployments(domain, limit=20)
    except _RECORDING_ERRORS:
        # The history is advice here; the checkout's commit stands in.
        return None
    for record in records:
        if record.status == DeploymentStatus.SUCCESS.value:
            return record.git_commit
    return None


def _forget_upstream(domain: str) -> None:
    """
    Drop the cached answers about an application, once what is live changed.

    Args:
        domain: The application's domain.
    """
    with _upstream_guard:
        for key in [key for key in _upstream_answers if key[0] == domain]:
            del _upstream_answers[key]


# ---------------------------------------------------------------------------
# Going back to a deployment
# ---------------------------------------------------------------------------

#: Statuses of a deployment whose result can be gone back to: one that
#: served, including one a later rollback replaced.
_RESTORABLE = frozenset({DeploymentStatus.SUCCESS.value, DeploymentStatus.ROLLED_BACK.value})

#: Statuses of a deployment that has not finished yet.
_UNFINISHED = frozenset({DeploymentStatus.QUEUED.value, DeploymentStatus.RUNNING.value})

#: Types that rebuild in place from a restored tree. A monorepo or a stack
#: has units and images that a restored tree does not bring back, so without
#: history to check out it has no way back but a backup restored by hand.
_NO_SNAPSHOT_REBUILD = frozenset({AppType.MONOREPO.value, AppType.DOCKER_COMPOSE.value})

#: What a snapshot restore carries over from the tree it replaces: no archive
#: holds ``.git``, and a tree that was a checkout must stay one.
_SNAPSHOT_KEEPS = (".git",)


@dataclass(frozen=True)
class DeploymentRollback:
    """
    What :func:`rollback_to_deployment` did.

    Attributes:
        domain: The application's domain.
        deployment_id: The deployment gone back to.
        release_id: The release activated, on releases.
        backup_id: The snapshot backup restored, in place without history.
        history_id: The history row the operation wrote, when it wrote one.
        commit: The commit rebuilt, in place in a git checkout.
    """

    domain: str
    deployment_id: int
    release_id: str | None
    backup_id: str | None
    history_id: int | None
    commit: str | None = None


def rollback_availability(records: Sequence[DeploymentRecord]) -> dict[int, str | None]:
    """
    Say, for each deployment, whether it can be gone back to, and if not why.

    On releases that takes its release, on disk, not active and not one that
    failed its health gate. In place in a git checkout it takes its commit:
    going back rebuilds it where the application runs, so anything but the
    deployment that is live can be gone back to. In place without history it
    takes its snapshot (the pre-update backup the next update took, which
    must still exist) and a type that can rebuild a restored tree.

    Args:
        records: Deployments, of any applications.

    Returns:
        Deployment id to None when it can be gone back to, or to the reason
        it cannot.
    """
    store = get_store()
    apps: dict[str, App | None] = {}
    releases: dict[str, tuple[set[str], str | None, set[str]]] = {}
    live: dict[str, int | None] = {}
    backups: dict[str, bool] = {}
    backup_manager: BackupManager | None = None
    answers: dict[int, str | None] = {}

    for record in records:
        if record.id is None:
            continue
        if record.domain not in apps:
            apps[record.domain] = store.get_app(record.domain)
        app = apps[record.domain]
        if app is None:
            answers[record.id] = f"{record.domain} is no longer deployed"
            continue
        if record.status not in _RESTORABLE:
            answers[record.id] = (
                f"Deployment {record.id} did not finish serving anything to go back to"
            )
            continue

        if app.layout == RELEASES:
            if record.domain not in releases:
                manager = ReleaseManager(app_root(app))
                current = manager.current()
                releases[record.domain] = (
                    {release.id for release in manager.list()},
                    current.id if current is not None else None,
                    _failed_releases(store, app),
                )
            on_disk, active, failed = releases[record.domain]
            if record.release_id is None:
                answers[record.id] = f"Deployment {record.id} built no release"
            elif record.release_id == active:
                answers[record.id] = f"Release {record.release_id} is already live"
            elif record.release_id not in on_disk:
                answers[record.id] = (
                    f"Release {record.release_id} is no longer on disk; rebuild its commit instead"
                )
            elif record.release_id in failed:
                answers[record.id] = (
                    f"Release {record.release_id} did not pass its health check when it was "
                    "last activated; rebuild its commit instead"
                )
            else:
                answers[record.id] = None
            continue

        if _goes_back_by_commit(app, record):
            if record.domain not in live:
                live[record.domain] = _live_deployment(store, record.domain)
            answers[record.id] = (
                f"Deployment {record.id} is what is live"
                if live[record.domain] == record.id
                else None
            )
            continue

        if app.app_type in _NO_SNAPSHOT_REBUILD:
            answers[record.id] = (
                f"A {app.app_type} application that is not a git checkout cannot be rebuilt "
                f"from a snapshot; restore a backup with wasm rollback {app.domain}"
            )
            continue
        snapshot = record.snapshot_backup
        if snapshot is None:
            answers[record.id] = f"No backup holds what deployment {record.id} produced; " + (
                "rebuild its commit instead"
                if record.git_commit
                else f"restore a backup with wasm rollback {app.domain}"
            )
            continue
        if snapshot not in backups:
            if backup_manager is None:
                backup_manager = BackupManager()
            backups[snapshot] = backup_manager.get_backup(snapshot) is not None
        answers[record.id] = (
            None
            if backups[snapshot]
            else f"Backup {snapshot} of deployment {record.id} no longer exists"
        )
    return answers


def _goes_back_by_commit(app: App, record: DeploymentRecord) -> bool:
    """
    Tell whether going back to an in-place deployment rebuilds its commit.

    Args:
        app: The application, in place.
        record: The deployment.

    Returns:
        True when the deployment recorded its commit and the tree is a git
        checkout to check it out in.
    """
    return bool(record.git_commit) and (app_root(app) / ".git").exists()


def _live_deployment(store: WASMStore, domain: str) -> int | None:
    """
    Name the deployment an in-place application serves.

    Args:
        store: The store.
        domain: The application's domain.

    Returns:
        The newest finished deployment when it succeeded, or None: after a
        failed one the tree is not what any deployment produced, and going
        back to the last good one is what repairs it.
    """
    for record in store.list_deployments(domain, limit=20):
        if record.status in _UNFINISHED:
            continue
        return record.id if record.status == DeploymentStatus.SUCCESS.value else None
    return None


def _failed_releases(store: WASMStore, app: App) -> set[str]:
    """
    Name the releases of an application that failed their health gate.

    Args:
        store: The store.
        app: The application, on releases.

    Returns:
        Their ids. A failed activation leaves the release on disk; it must
        not be offered, or activated by a rebuild of its commit, again.
    """
    if app.id is None:
        return set()
    return {
        row.id for row in store.list_releases(app.id) if row.status == ReleaseStatus.FAILED.value
    }


def rollback_to_deployment(
    domain: str,
    deployment_id: int,
    *,
    trigger: str = DeploymentTrigger.CLI.value,
    restore_env: bool = False,
    logger: Logger | None = None,
    verbose: bool = False,
) -> DeploymentRollback:
    """
    Put back what one deployment of an application produced.

    On releases, its release is activated behind the health gate (see
    :func:`activate_release`). In place in a git checkout, its commit is
    rebuilt where the application runs, by :func:`update_app`: the tree
    stays a checkout, what the application wrote into it (uploads, SQLite
    files, the ``.env``) stays as it is, the restart passes the health gate,
    and a monorepo or a stack goes back through its own deployer. In place
    without history, its snapshot backup is restored over the tree by
    :meth:`~wasm.managers.backup_manager.RollbackManager.rollback`, which
    takes a safety backup first, keeps ``.git`` and, unless asked, the
    ``.env``, rebuilds strictly and passes the same health gate. Every way,
    the operation is a history row of its own and the deployment it
    replaced is marked rolled back.

    Args:
        domain: The application's domain.
        deployment_id: The deployment to go back to.
        trigger: Who asked, recorded in the history.
        restore_env: Restoring a snapshot, put back the ``.env`` it holds
            too. Off by default: a secret rotated since must not come back.
        logger: Where the operation reports its progress.
        verbose: Verbosity of the managers.

    Returns:
        What was done.

    Raises:
        WASMError: The deployment is not one of this application's.
        DeploymentError: It cannot be gone back to (see
            :func:`rollback_availability`), its rebuild failed, or it did not
            pass the health gate.
        BackupError: The restore failed.
        AppBusyError: Another operation is running on the application.
    """
    domain = validate_domain(domain)
    store = get_store()
    record = store.get_deployment(deployment_id)
    if record is None or record.domain != domain:
        raise WASMError(
            f"Deployment {deployment_id} of {domain} not found",
            details=f"See the application's history: GET /api/deployments?domain={domain}",
        )
    reason = rollback_availability([record]).get(deployment_id)
    if reason is not None:
        raise DeploymentError(
            reason,
            details=(
                f"Rebuild its commit instead: wasm update {domain} --commit {record.git_commit}"
                if record.git_commit
                else f"Restore a backup instead: wasm rollback {domain}"
            ),
        )

    app = store.get_app(domain)
    if app is None:
        # rollback_availability read it a moment ago; a deletion since won.
        raise WASMError(
            f"Application not found: {domain}", details="Run 'wasm list' to see what is deployed."
        )
    if app.layout == RELEASES and record.release_id is not None:
        activation = activate_release(
            domain, record.release_id, trigger=trigger, logger=logger, verbose=verbose
        )
        return DeploymentRollback(
            domain=domain,
            deployment_id=deployment_id,
            release_id=activation.release.id,
            backup_id=None,
            history_id=activation.deployment_id,
        )

    commit = record.git_commit if _goes_back_by_commit(app, record) else None
    if commit is not None:
        outcome = update_app(
            domain,
            commit=commit,
            trigger=trigger,
            logger=logger,
            verbose=verbose,
            rollback_of=deployment_id,
        )
        return DeploymentRollback(
            domain=domain,
            deployment_id=deployment_id,
            release_id=None,
            backup_id=None,
            history_id=outcome.deployment_id,
            commit=commit,
        )

    backup_id = record.snapshot_backup
    manager = RollbackManager(verbose=verbose)
    try:
        manager.rollback(
            domain=domain,
            backup_id=backup_id,
            trigger=trigger,
            restore_env=restore_env,
            keep=_SNAPSHOT_KEEPS,
            app_type=app.app_type,
            gate=lambda: health_gate_for(app, store, manager.logger).restart_and_probe(),
        )
    finally:
        _forget_upstream(domain)
    return DeploymentRollback(
        domain=domain,
        deployment_id=deployment_id,
        release_id=None,
        backup_id=backup_id,
        history_id=manager.last_deployment_id,
    )


def _mark_replaced_rolled_back(
    store: WASMStore, domain: str, own_id: int | None, log: Logger
) -> None:
    """
    Mark the deployment a rollback replaced as rolled back.

    The rollback's own row is the newest success; the one before it is the
    build that stopped serving, as :meth:`DeploymentRecorder.mark_previous_success_rolled_back`
    marks it for a recording still in progress.

    Args:
        store: The store.
        domain: The application's domain.
        own_id: The row the rollback wrote, never a candidate.
        log: Where a failure to record is reported.
    """
    try:
        for record in store.list_deployments(domain, limit=20):
            if record.id is None or record.id == own_id:
                continue
            if record.status == DeploymentStatus.SUCCESS.value:
                store.mark_deployment_rolled_back(record.id)
                return
    except _RECORDING_ERRORS as exc:
        log.warning(f"Could not mark the deployment the rollback replaced: {exc}")


def _record_failure_after_the_fact(
    store: WASMStore, deployment_id: int | None, error: WASMError, log: Logger
) -> None:
    """
    Turn a finished row into a failed one, when what followed its build failed.

    Args:
        store: The store.
        deployment_id: The row, when there is one.
        error: What failed. Only its message is stored: its details hold the
            journal, which the recorder would have scrubbed.
        log: Where a failure to record is reported.
    """
    if deployment_id is None:
        return
    try:
        store.finish_deployment(deployment_id, DeploymentStatus.FAILED.value, error=error.message)
    except _RECORDING_ERRORS as exc:
        log.warning(f"Could not record deployment {deployment_id} as failed: {exc}")


# ---------------------------------------------------------------------------
# Resource limits
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LimitsChange:
    """
    What :func:`set_resource_limits` did.

    Attributes:
        domain: The application's domain.
        limits: The limits it has now.
        units: The units rewritten.
        restarted: Whether they were restarted to run under them. When not,
            the running processes keep the limits they started with.
    """

    domain: str
    limits: ResourceLimits
    units: tuple[str, ...]
    restarted: bool


def set_resource_limits(
    domain: str,
    limits: ResourceLimits,
    *,
    restart: bool = False,
    logger: Logger | None = None,
) -> LimitsChange:
    """
    Give an application exactly these memory, CPU and task limits.

    Every unit the application runs as is rewritten (a monorepo has one per
    workspace) and the limits are recorded on its row, so a redeploy writes
    them into the new unit too. Either every unit gets them or none does.

    Restarted, the application must pass the same health gate as a deploy:
    a memory limit it cannot start under would otherwise leave it down, so
    the previous units are put back and restarted, and this raises with the
    probe's and the journal's own output.

    Args:
        domain: The application's domain.
        limits: The limits; a None field removes that limit.
        restart: Restart the units so the processes run under them now.
        logger: Where the restart and the probes are reported.

    Returns:
        What was done.

    Raises:
        WASMError: The application is unknown.
        ValidationError: A limit is out of range.
        DeploymentError: Nothing runs as a unit for it: a static site, or a
            Docker Compose stack, whose containers are not in the unit's
            cgroup and are limited in the compose file. Or, restarted, it
            did not answer under the new limits, and the old ones are back.
        ServiceError: A unit could not be rewritten; the ones already
            rewritten are put back.
        AppBusyError: Another operation is running on the application.
    """
    log = logger if logger is not None else Logger()
    domain = validate_domain(domain)
    store = get_store()
    app = store.get_app(domain)
    if app is None:
        raise WASMError(
            f"Application not found: {domain}", details="Run 'wasm list' to see what is deployed."
        )
    limits.validated()
    # Rewriting the units while a migration or an update rewrites or restarts
    # them would leave whichever finished last.
    with app_lock(app.domain, "resource limits change"):
        return _set_resource_limits(app, limits, restart=restart, store=store, log=log)


def _set_resource_limits(
    app: App, limits: ResourceLimits, *, restart: bool, store: WASMStore, log: Logger
) -> LimitsChange:
    """
    Apply resource limits to an application whose lock the caller holds.

    Args:
        app: The application.
        limits: The validated limits.
        restart: Restart the units so the processes run under them now.
        store: The store.
        log: Where the restart and the probes are reported.

    Returns:
        What was done.

    Raises:
        DeploymentError: See :func:`set_resource_limits`.
        ServiceError: A unit could not be rewritten.
    """
    domain = app.domain
    if app.app_type == "docker-compose":
        raise DeploymentError(
            f"{domain} runs in Docker containers, which its unit's limits do not reach",
            details="Set deploy.resources.limits for each service in the compose file.",
        )
    units = [s.name for s in store.list_services() if app.id is not None and s.app_id == app.id]
    if not units and not app.is_static:
        units = [app_root(app).name]
    if not units:
        raise DeploymentError(
            f"{domain} is a static site; no process of its own runs to be limited",
            details="The web server serves it straight from disk.",
        )

    services = ServiceManager()
    written: list[tuple[str, str]] = []

    def put_back() -> None:
        for unit, previous in reversed(written):
            services.update_config(unit, previous)

    try:
        for unit in units:
            written.append((unit, services.set_resource_limits(unit, limits)))
    except WASMError:
        put_back()
        raise

    if restart:

        def restart_all() -> None:
            for unit in units:
                services.restart(unit)

        gate = health_gate_for(app, store, log, restart=restart_all)
        healthy, evidence = gate.restart_and_probe()
        if not healthy:
            put_back()
            restored, _ = gate.restart_and_probe()
            state = "running again" if restored else "back, but it is not answering either"
            raise DeploymentError(
                f"{domain} did not answer under the new limits; the previous ones are {state}",
                details=evidence,
            )

    app.memory_max_mb = limits.memory_max_mb
    app.cpu_quota_percent = limits.cpu_quota_percent
    app.tasks_max = limits.tasks_max
    store.update_app(app)
    return LimitsChange(domain=domain, limits=limits, units=tuple(units), restarted=restart)


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------


def set_health_check(
    domain: str,
    *,
    path: str | None,
    expect: str | None,
    timeout: int | None,
) -> App:
    """
    Tell the health gate what to ask of an application.

    Every activation from then on - deploy, update, rollback, migration, a
    limits restart - probes ``path`` and accepts only ``expect`` within
    ``timeout``, and the diagnosis probes the same. Nothing restarts: the
    settings are read the next time something is activated. The values are
    validated where they are stored.

    Args:
        domain: The application's domain.
        path: Path to probe, or None for ``/``.
        expect: Statuses that mean up, such as ``200-399``, or None for any
            status below 500.
        timeout: Seconds it gets to answer, from 5 to 600, or None for the
            default.

    Returns:
        The application's row as it is now.

    Raises:
        WASMError: The application is unknown.
        DeploymentError: It is a static site, which the gate checks by its
            files, not over HTTP.
        ValidationError: A value is not one the gate can use.
        AppBusyError: Another operation is running on the application.
    """
    store = get_store()
    app = _known_app(store, validate_domain(domain))
    if app.is_static:
        raise DeploymentError(
            f"{app.domain} is a static site; nothing answers HTTP for it but the web server",
            details="Its health check is that the directory it serves has an index.html.",
        )
    # A deploy probing with the old settings while they change would be
    # judged by neither; refused at once instead, naming what runs.
    with app_lock(app.domain, "health check change"):
        store.set_app_health(app.domain, path=path, expect=expect, timeout=timeout)
    return _known_app(store, app.domain)


def _known_app(store: WASMStore, domain: str) -> App:
    """
    Read an application's row, or say it is not deployed.

    Args:
        store: The store.
        domain: A validated domain.

    Returns:
        The row.

    Raises:
        WASMError: There is none.
    """
    app = store.get_app(domain)
    if app is None:
        raise WASMError(
            f"Application not found: {domain}", details="Run 'wasm list' to see what is deployed."
        )
    return app


# ---------------------------------------------------------------------------
# Release retention
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RetentionChange:
    """
    What :func:`set_release_retention` did.

    Attributes:
        domain: The application's domain.
        keep_releases: The retention it has now.
        pruned: Ids of the releases removed from disk, oldest first.
    """

    domain: str
    keep_releases: int
    pruned: tuple[str, ...]


def set_release_retention(
    domain: str, keep: int, *, logger: Logger | None = None
) -> RetentionChange:
    """
    Set how many releases an application keeps, and prune to it now.

    Pruning keeps the newest ``keep``, the active release wherever it is and
    the one a rollback would go to (:meth:`ReleaseManager.prune`), so no
    number takes away what serves or the way back. The rows of the pruned
    releases are forgotten as a deploy's prune forgets them.

    Args:
        domain: The application's domain.
        keep: Releases to keep, 1 to 50.
        logger: Where each pruned release is reported.

    Returns:
        What was done.

    Raises:
        WASMError: The application is unknown.
        DeploymentError: It is in place, where there are no releases.
        ValidationError: ``keep`` is out of range; nothing is pruned.
        AppBusyError: Another operation is running on the application.
    """
    log = logger if logger is not None else Logger()
    app = _release_app(validate_domain(domain))
    store = get_store()
    # A deploy pruning at the same moment, or an activation re-pointing
    # current, must not race the list this prune decides from.
    with app_lock(app.domain, "release retention change"):
        store.set_keep_releases(app.domain, keep)
        releases = ReleaseManager(app_root(app), logger=log)
        removed = releases.prune(keep)
        for release in removed:
            log.substep(f"Pruned release {release.id}")
        # Under --dry-run the prune only said what it would remove; the rows
        # describe what is on disk, which did not change.
        if app.id is not None and not is_rehearsal():
            store.forget_pruned_releases(
                app.id,
                on_disk={release.id for release in releases.list()},
                removed={release.id for release in removed},
                keep=keep,
            )
    return RetentionChange(
        domain=app.domain, keep_releases=keep, pruned=tuple(r.id for r in removed)
    )


# ---------------------------------------------------------------------------
# Deletion
# ---------------------------------------------------------------------------

#: How many phases :func:`delete_app` reports.
DELETE_PHASES = 5


@dataclass(frozen=True)
class AppDeletion:
    """
    What :func:`delete_app` did.

    Attributes:
        domain: The application's domain.
        units: The units removed.
        containers_stopped: Whether a Docker Compose stack was taken down.
        volumes_removed: Whether its named volumes went with it.
        certificate_removed: Whether a certificate was removed.
        files_removed: Whether the application directory was removed.
        warnings: What could not be removed, and why; the rest was.
    """

    domain: str
    units: tuple[str, ...]
    containers_stopped: bool
    volumes_removed: bool
    certificate_removed: bool
    files_removed: bool
    warnings: tuple[str, ...]


def delete_app(
    domain: str,
    *,
    remove_files: bool = True,
    remove_certificate: bool = True,
    remove_volumes: bool = False,
    on_phase: PhaseReporter | None = None,
    logger: Logger | None = None,
) -> AppDeletion:
    """
    Remove a deployed application: its containers, units, site, certificate, files and rows.

    The one deletion, for ``wasm delete`` and the console's delete job alike.
    They used to differ: the console's job never took a Docker Compose stack
    down, so its containers went on running from a directory it then deleted,
    and a unit that would not stop left its unit file behind.

    Each step is attempted whatever the one before it did, and what failed is
    returned as a warning: a deletion that stopped at the first failure left
    the rest of the application half there.

    Args:
        domain: The application's domain.
        remove_files: Also remove the application directory.
        remove_certificate: Also remove the domain's certificate.
        remove_volumes: Also remove a Docker Compose stack's named volumes.
            Off unless asked for explicitly: they hold its databases.
        on_phase: Called as each phase begins, with its position and
            :data:`DELETE_PHASES`.
        logger: Where the details are reported.

    Returns:
        What was done.

    Raises:
        WASMError: Nothing is deployed at that domain.
        AppBusyError: Another operation is running on the application.
    """
    log = logger if logger is not None else Logger()
    domain = validate_domain(domain)
    # A deploy writing the tree, or an update restarting the unit, must not
    # run while it is taken apart.
    with app_lock(domain, "deletion"):
        return _delete_app(
            domain,
            remove_files=remove_files,
            remove_certificate=remove_certificate,
            remove_volumes=remove_volumes,
            phase=on_phase or (lambda _index, _total, _message: None),
            log=log,
        )


def _delete_app(
    domain: str,
    *,
    remove_files: bool,
    remove_certificate: bool,
    remove_volumes: bool,
    phase: PhaseReporter,
    log: Logger,
) -> AppDeletion:
    """
    Remove an application whose lock the caller holds.

    Args:
        domain: A validated domain.
        remove_files: Also remove the application directory.
        remove_certificate: Also remove the domain's certificate.
        remove_volumes: Also remove a Docker Compose stack's named volumes.
        phase: Reporter for the :data:`DELETE_PHASES` phases.
        log: Where the details are reported.

    Returns:
        What was done.

    Raises:
        WASMError: Nothing is deployed at that domain.
    """
    store = get_store()
    app = store.get_app(domain)
    # The store holds the real path, which is what finds a legacy app
    # deployed under a wasm- prefix.
    app_path = (
        Path(app.app_path)
        if app is not None and app.app_path
        else Config().apps_directory / domain_to_app_name(domain)
    )
    if app is None and not app_path.exists():
        raise WASMError(
            f"Application not found: {domain}",
            details="Nothing to delete; check 'wasm list' for the exact domain.",
        )
    warnings: list[str] = []

    def failed(what: str, error: BaseException) -> None:
        warnings.append(f"{what}: {error}")
        log.warning(f"{what}: {error}")

    containers_stopped = False
    if app is not None and app.app_type == "docker-compose":
        phase(1, DELETE_PHASES, "Stopping Docker Compose containers")
        deployer = DockerComposeDeployer(verbose=log.verbose)
        deployer.app_path = app_path
        deployer.app_name = app_path.name
        deployer.domain = domain
        try:
            containers_stopped = deployer.down(remove_volumes=remove_volumes)
        except WASMError as exc:
            failed("The containers were not taken down", exc)
        # Before the files go: the compose file names the services whose
        # kept images are removed.
        try:
            for tag in deployer.remove_kept_images():
                log.substep(f"Removed {tag}")
        except WASMError as exc:
            failed("The images kept for going back were not all removed", exc)
    else:
        phase(1, DELETE_PHASES, "Stopping the application")

    phase(2, DELETE_PHASES, "Removing its units")
    units = [s.name for s in store.list_services() if app is not None and s.app_id == app.id]
    if not units:
        units = [app_path.name]
    services = ServiceManager(verbose=log.verbose)
    for unit in units:
        try:
            # Stops, disables and removes it, and tolerates one that is gone.
            services.delete_service(unit)
        except WASMError as exc:
            failed(f"The unit {unit} was not removed", exc)

    phase(3, DELETE_PHASES, "Removing its site and certificate")
    deletion = delete_site_completely(
        domain,
        nginx=NginxManager(verbose=log.verbose),
        apache=ApacheManager(verbose=log.verbose),
        cert_manager=CertManager(verbose=log.verbose),
        delete_certificate=remove_certificate,
    )

    files_removed = False
    if remove_files and app_path.exists():
        phase(4, DELETE_PHASES, "Removing application files")
        try:
            get_fs().remove_tree(app_path)
            files_removed = not is_rehearsal()
        except OSError as exc:
            failed(f"{app_path} was not removed", exc)
    else:
        phase(4, DELETE_PHASES, "Keeping application files")

    phase(5, DELETE_PHASES, "Removing its records")
    if app is not None and not is_rehearsal():
        store.delete_site(domain)
        for unit in units:
            store.delete_service(unit)
        store.delete_app(domain)

    return AppDeletion(
        domain=domain,
        units=tuple(units),
        containers_stopped=containers_stopped,
        volumes_removed=containers_stopped and remove_volumes,
        certificate_removed=deletion.certificate_removed,
        files_removed=files_removed,
        warnings=tuple(warnings),
    )
