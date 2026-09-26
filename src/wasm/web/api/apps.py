"""
Applications API endpoints.

The panel does not deploy applications itself: it hands the work to the job
manager, which drives the same deployers the CLI drives. Two things changed
here for that to be true.

- **A deploy no longer runs inside the request.** ``POST /api/apps`` used to
  call ``deployer.deploy()`` from an ``async def`` handler, which pinned the
  event loop for the whole build - minutes of ``npm install`` during which the
  panel served nothing, not even a heartbeat - and then timed out the client
  anyway. It now answers ``202 Accepted`` with a job id.
- **Logs come from the service manager.** The handler used to run
  ``journalctl`` through :mod:`subprocess` with the service name interpolated
  by hand, bypassing the shared command runner.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from wasm.core import app_state
from wasm.core.app_state import AppState, resolve_state_with_status, resolve_states_with_status
from wasm.core.config import REDACTED, redact_secrets
from wasm.core.exceptions import DeploymentError, ValidationError, WASMError
from wasm.core.store import (
    DEFAULT_KEEP_RELEASES,
    App,
    DeploymentRecord,
    DeploymentTrigger,
    Service,
    get_store,
)
from wasm.core.utils import domain_to_app_name
from wasm.deployers.base import BaseDeployer
from wasm.deployers.helpers.app_env import read_app_env, write_app_env
from wasm.deployers.helpers.env_manager import EnvManager, redact_url_credentials
from wasm.deployers.helpers.layout import RELEASES
from wasm.deployers.inspect import inspect_source
from wasm.deployers.lifecycle import activate_release, list_releases, set_resource_limits
from wasm.deployers.migrate import MigrationPlan, migrate, plan_migration
from wasm.deployers.registry import DeployerRegistry, available_types
from wasm.deployers.releases import is_release_id
from wasm.managers.backup_manager import RollbackManager
from wasm.managers.service_manager import ResourceLimits, ServiceManager
from wasm.validators.environment import EnvironmentValidationError, validate_environment
from wasm.validators.port import find_available_port, validate_port
from wasm.web.api.auth import get_current_session
from wasm.web.api.deps import (
    JobAcceptedResponse,
    WASMErrorRoute,
    ensure_elevated,
    require_elevated,
    strict_domain,
)
from wasm.web.auth import actor_label, ensure_scope, get_audit_logger, get_client_ip
from wasm.web.jobs import JobType, delete_app_job, deploy_app_job, get_job_manager
from wasm.web.pydantic_compat import iso_offset_validator

#: app_state's display labels, translated to the fixed API vocabulary.
#: Decoupled from AppState.label on purpose: that string is for a terminal
#: column and free to reword, this one is a public contract every client
#: parses.
_STATUS_LABELS: dict[str, str] = {
    app_state.RUNNING: "running",
    app_state.RESTARTING: "restarting",
    app_state.NOT_RESPONDING: "no_answer",
    app_state.STOPPED: "stopped",
    app_state.FAILED: "failed",
    app_state.STATIC: "static",
    app_state.UNKNOWN: "unknown",
}

router = APIRouter(route_class=WASMErrorRoute)

#: Port preferred when the client does not pick one.
DEFAULT_PORT = 3000


class LastDeploymentOut(BaseModel):
    """
    An application's most recent deployment attempt.

    Attributes:
        id: Deployment id, the store's own primary key.
        status: One of :class:`~wasm.core.store.DeploymentStatus`: ``queued``,
            ``running``, ``success``, ``failed`` or ``rolled_back``.
        finished_at: When it finished, ISO 8601 with an explicit UTC offset;
            None while it is still running.
        git_commit: Short commit it deployed, when the source is git.
    """

    id: int
    status: str
    finished_at: str | None = None
    git_commit: str | None = None

    _iso_timestamps = iso_offset_validator("finished_at")


class AppInfo(BaseModel):
    """
    A deployed application and the live state of its service.

    Attributes:
        name: Application name, which is its domain.
        domain: Domain the application is served on.
        status: What is true about it right now, resolved by
            :func:`wasm.core.app_state.resolve_state` - the one place the CLI
            and the panel agree on this: ``running``, ``restarting`` (systemd
            is crash-looping the unit), ``no_answer`` (the unit is up but
            nothing accepts connections on its port), ``stopped``, ``failed``
            (systemd gave up on it), ``static`` (served directly by the web
            server, there is no unit) or ``unknown`` (systemd could not be
            asked).
        active: Whether the unit is active.
        enabled: Whether the unit starts on boot.
        pid: Main PID when running.
        uptime: How long the unit has been up.
        port: Upstream port.
        app_type: Deployer that owns it.
        path: Application directory.
        source: Git URL or local path it was deployed from.
        branch: Git branch it tracks, or None for a source that has none.
        layout: ``inplace`` or ``releases``.
        keep_releases: Release directories kept before older ones are pruned.
            Meaningful only on ``releases``; the in-place default otherwise.
        build_command: Argv the deployer runs to build the project, read off
            the same ``get_build_command()`` the deploy used. Empty in a
            list response - computing it means instantiating the deployer
            once per application, which this endpoint does not do for a
            list - and filled in when this application is fetched on its own.
        start_command: What its unit runs, exactly as the deploy recorded it
            on the service row. None for a static site, which has no unit.
        memory_max_mb: Memory limit of its unit, in MB, or None.
        cpu_quota_percent: CPU quota of its unit, in percent of one CPU, or None.
        tasks_max: Task limit of its unit, or None.
        webhook_enabled: Whether a webhook secret is set for it. The secret
            itself is never part of this or any other response; it is set
            through ``POST /api/apps/{domain}/webhook-secret`` and cleared
            through the ``DELETE`` of the same path.
        unit: The systemd unit that runs it, or None for a static site.
        run_as: The account its unit runs as, or None for a static site.
        last_deployment: Its most recent deployment attempt, or None when
            nothing has ever been recorded for it.
    """

    name: str
    domain: str
    status: str
    active: bool
    enabled: bool
    pid: int | None = None
    uptime: str | None = None
    port: int | None = None
    app_type: str | None = None
    path: str | None = None
    source: str | None = None
    branch: str | None = None
    layout: str = "inplace"
    keep_releases: int = DEFAULT_KEEP_RELEASES
    build_command: list[str] = Field(default_factory=list)
    start_command: str | None = None
    memory_max_mb: int | None = None
    cpu_quota_percent: int | None = None
    tasks_max: int | None = None
    webhook_enabled: bool = False
    unit: str | None = None
    run_as: str | None = None
    last_deployment: LastDeploymentOut | None = None


class AppListResponse(BaseModel):
    """Response for listing applications."""

    apps: list[AppInfo]
    total: int


class CreateAppRequest(BaseModel):
    """
    Request to deploy a new application.

    The deployer-specific fields carry exactly what the deployers'
    ``configure`` methods already accept, no more: ``subdomain_overrides``,
    ``workspace_filter`` and ``skip_database`` are read by the monorepo
    deployer, ``compose_file`` and ``compose_profiles`` by the docker-compose
    one, and every deployer ignores the options that do not concern it, which
    is the interface's own contract.
    """

    domain: str = Field(..., description="Target domain name")
    source: str = Field(..., description="Git URL or local path")
    app_type: str = Field(default="auto", description="Application type")
    port: int | None = Field(default=None, description="Application port")
    webserver: str = Field(default="nginx", description="Web server to use")
    branch: str | None = Field(default=None, description="Git branch to deploy")
    ssl: bool = Field(default=True, description="Obtain a certificate")
    env_vars: dict[str, str] = Field(default_factory=dict, description="Environment variables")
    subdomain_overrides: dict[str, str] = Field(
        default_factory=dict,
        description="Monorepo: workspace name to subdomain overrides",
    )
    workspace_filter: list[str] | None = Field(
        default=None, description="Monorepo: deploy only these workspaces"
    )
    skip_database: bool = Field(default=False, description="Monorepo: skip database provisioning")
    compose_file: str | None = Field(
        default=None, description="Docker Compose: compose file, relative to the project"
    )
    compose_profiles: list[str] | None = Field(
        default=None, description="Docker Compose: profiles to activate"
    )
    layout: Literal["inplace", "releases"] | None = Field(
        default=None,
        description="Build every deploy as a release behind a health gate, or in place. "
        "Omitted: the server's deploy.layout",
    )
    include_www: bool = Field(
        default=False, description="Also answer on www.<domain>, as a redirect to it"
    )
    persistent_paths: list[str] | None = Field(
        default=None,
        description="Releases only: paths, relative to the application, linked into shared/ "
        "and kept across every release (uploads, storage)",
    )
    memory_max_mb: int | None = Field(
        default=None, description="MemoryMax for the unit, in MB; at least 64. Null: no limit"
    )
    cpu_quota_percent: int | None = Field(
        default=None,
        description="CPUQuota for the unit, in percent of one CPU (200 is two CPUs); "
        "1 to 100 per CPU. Null: no limit",
    )
    tasks_max: int | None = Field(
        default=None,
        description="TasksMax for the unit, processes and threads; at least 16. Null: no limit",
    )


class AppActionResponse(BaseModel):
    """Response for an application action that completed immediately."""

    success: bool
    message: str
    domain: str


class AppLogsResponse(BaseModel):
    """Response carrying journal output for an application."""

    domain: str
    logs: str
    lines: int


class AppEnvResponse(BaseModel):
    """
    An application's environment, as recorded in its ``.env`` file.

    Attributes:
        domain: Domain of the application.
        variables: Name to value mapping. Unless ``unmasked`` is true, a
            secret-looking name and a URL credential embedded in a value are
            both replaced by the fixed :data:`~wasm.core.config.REDACTED`
            placeholder, exactly as ``wasm env show`` does on the terminal.
        unmasked: Whether this response carries values in clear.
    """

    domain: str
    variables: dict[str, str]
    unmasked: bool


class UpdateAppEnvRequest(BaseModel):
    """Request to replace an application's ``.env`` file wholesale."""

    variables: dict[str, str] = Field(
        default_factory=dict,
        description="The complete name to value mapping the .env file should hold",
    )


class AppEnvUpdateResponse(BaseModel):
    """
    Confirmation that an application's environment was rewritten.

    Attributes:
        domain: Domain of the application.
        restart_required: Always true. The running process, if any, keeps the
            environment it started with; the caller must restart it to pick
            up the change.
    """

    domain: str
    restart_required: bool = True


def _last_deployment_out(record: DeploymentRecord | None) -> LastDeploymentOut | None:
    """
    Translate a store deployment row to its API model.

    Args:
        record: The application's newest deployment, when it has one.

    Returns:
        None when there is no history, or the row has no id (never
        persisted); otherwise the API model.
    """
    if record is None or record.id is None:
        return None
    return LastDeploymentOut(
        id=record.id,
        status=record.status,
        finished_at=record.finished_at,
        git_commit=record.git_commit,
    )


def _to_app_info(
    app: App,
    state: AppState,
    status: dict[str, Any],
    service: Service | None,
    *,
    webhook_enabled: bool,
    last_deployment: DeploymentRecord | None,
) -> AppInfo:
    """
    Combine a stored application with its resolved state and live status.

    Args:
        app: The stored application.
        state: What :func:`wasm.core.app_state.resolve_state` decided is
            true about it.
        status: The systemd status ``state`` was resolved from, empty for a
            static application, which is never queried.
        service: The application's service record, when it has one.
        webhook_enabled: Whether a webhook secret is set for it.
        last_deployment: Its most recent deployment history row, if any.

    Returns:
        The API representation.
    """
    pid = status.get("pid")
    return AppInfo(
        name=app.domain,
        domain=app.domain,
        status=_STATUS_LABELS.get(state.label, state.label.lower()),
        active=bool(status.get("active", False)),
        enabled=bool(status.get("enabled", False)),
        pid=int(pid) if pid and str(pid) != "0" else None,
        uptime=str(status["uptime"]) if status.get("uptime") else None,
        port=app.port,
        app_type=app.app_type,
        path=app.app_path,
        source=app.source or None,
        branch=app.branch,
        layout=app.layout,
        keep_releases=app.keep_releases,
        start_command=service.command if service is not None and service.command else None,
        memory_max_mb=app.memory_max_mb,
        cpu_quota_percent=app.cpu_quota_percent,
        tasks_max=app.tasks_max,
        webhook_enabled=webhook_enabled,
        unit=service.name if service is not None else None,
        run_as=service.user if service is not None else None,
        last_deployment=_last_deployment_out(last_deployment),
    )


def _deployer_build_command(app: App) -> list[str]:
    """
    Read the build command a fresh deploy of this application would run.

    Instantiates the deployer and reads its ``get_build_command()`` the same
    way :func:`wasm.deployers.inspect.inspect_source` does for a checkout
    that has not been deployed yet - ``configure()`` and nothing past it, no
    install, no build, no network. Cheap enough for one application, which is
    why only the detail endpoint calls this: the list endpoint would pay this
    once per application shown, and :func:`_to_app_info` already gives every
    caller a ``build_command`` (empty) without it.

    Args:
        app: The stored application.

    Returns:
        Argv the deployer would run to build the project. Empty when the
        type is not registered, has nothing to build, or the computation
        itself failed - this is a convenience for the detail view, not a
        fact worth failing the request over.
    """
    deployer_class = DeployerRegistry.get(app.app_type)
    if deployer_class is None or not issubclass(deployer_class, BaseDeployer):
        return []
    try:
        instance = deployer_class(verbose=False)
        instance.configure(
            domain=app.domain,
            source=app.source,
            branch=app.branch,
            app_path=Path(app.app_path) if app.app_path else None,
        )
        return instance.get_build_command()
    except (WASMError, OSError):
        return []


def _looks_secret(name: str) -> bool:
    """
    Check a variable name against the deployer's secret patterns.

    Args:
        name: Environment variable name.

    Returns:
        True if the value behind this name must not be shown in clear.
    """
    upper = name.upper()
    return any(pattern in upper for pattern in EnvManager.SECRET_PATTERNS)


def _redact_env(values: Mapping[str, str]) -> dict[str, str]:
    """
    Replace every secret-looking value with the fixed REDACTED placeholder.

    The same three-classifier approach ``wasm env show`` uses on the
    terminal: a key-based pass (:func:`~wasm.core.config.redact_secrets`), a
    substring match against :data:`EnvManager.SECRET_PATTERNS` for the names
    it misses, and a value-based pass for a password embedded inside a
    connection string such as ``DATABASE_URL``. The placeholder is fixed
    width, so a response never reveals the length of a secret or whether one
    is set at all.

    Args:
        values: The environment as read from the .env file.

    Returns:
        A new mapping safe to send to a browser.
    """
    by_word: Mapping[str, Any] = redact_secrets(dict(values))
    redacted: dict[str, str] = {}
    for key, value in values.items():
        if by_word.get(key) == REDACTED or _looks_secret(key):
            redacted[key] = REDACTED
        else:
            redacted[key] = redact_url_credentials(str(value))
    return redacted


def _env_app(domain: str) -> App:
    """
    Look up the application whose ``.env`` file a request is about.

    Args:
        domain: Domain from the request.

    Returns:
        The stored application record.

    Raises:
        HTTPException: 404 when nothing is deployed at the domain.
    """
    validated = strict_domain(domain)
    app = get_store().get_app(validated)
    if app is None:
        raise HTTPException(status_code=404, detail=f"Application not found: {validated}")
    return app


@router.get("", response_model=AppListResponse)
def list_apps(session: Annotated[dict, Depends(get_current_session)]) -> AppListResponse:
    """
    List every deployed application.

    Every application's service record, webhook flag and last deployment
    come from one store query each, and every application's systemd status
    is read concurrently through :func:`~wasm.core.app_state.resolve_states_with_status`
    - so this endpoint costs a handful of queries and one round of systemctl
    calls, not four times the number of applications deployed.

    Args:
        session: The authenticated session.

    Returns:
        The applications, each with the live state of its service.
    """
    store = get_store()
    manager = ServiceManager(verbose=False)

    apps = store.list_apps()
    domains = [app.domain for app in apps]

    services_by_app_id = {
        service.app_id: service for service in store.list_services() if service.app_id is not None
    }
    webhook_flags = store.list_webhook_flags(domains)
    last_deployments = store.get_latest_deployments(domains)
    states = resolve_states_with_status(apps, manager)

    result = []
    for app in apps:
        state, status = states[app.domain]
        service = services_by_app_id.get(app.id) if app.id is not None else None
        result.append(
            _to_app_info(
                app,
                state,
                status,
                service,
                webhook_enabled=webhook_flags.get(app.domain, False),
                last_deployment=last_deployments.get(app.domain),
            )
        )

    return AppListResponse(apps=result, total=len(result))


@router.post("", response_model=JobAcceptedResponse, status_code=202)
def create_app(
    body: CreateAppRequest, session: Annotated[dict, Depends(get_current_session)]
) -> JobAcceptedResponse:
    """
    Queue the deployment of a new application.

    Args:
        body: The deployment request.
        session: The authenticated session.

    Returns:
        The queued job.

    Raises:
        HTTPException: 409 when the domain is already deployed, 503 when no
            port is free.
        PortError: When the requested port is not usable.
        DomainError: When the domain is not acceptable.
        ValidationError: A resource limit is out of range (400, with the
            range) - the same check ``PATCH .../limits`` runs, so a limit
            given at creation cannot be more permissive than one set later.
    """
    domain = strict_domain(body.domain)

    if get_store().get_app(domain):
        raise HTTPException(status_code=409, detail=f"Application already exists: {domain}")

    if body.port is not None:
        port: int | None = validate_port(body.port)
    else:
        port = find_available_port(preferred=DEFAULT_PORT)
        if port is None:
            raise HTTPException(status_code=503, detail="No available port found")

    ResourceLimits(
        memory_max_mb=body.memory_max_mb,
        cpu_quota_percent=body.cpu_quota_percent,
        tasks_max=body.tasks_max,
    ).validated()

    job = get_job_manager().create_job(
        job_type=JobType.DEPLOY,
        name=f"Deploy {domain}",
        description=f"Deploying a {body.app_type} application to {domain}",
        func=deploy_app_job,
        kwargs={
            "domain": domain,
            "source": body.source,
            "app_type": body.app_type,
            "port": port,
            "branch": body.branch,
            "env_vars": body.env_vars,
            "webserver": body.webserver,
            "ssl": body.ssl,
            "subdomain_overrides": body.subdomain_overrides,
            "workspace_filter": body.workspace_filter,
            "skip_database": body.skip_database,
            "compose_file": body.compose_file,
            "compose_profiles": body.compose_profiles,
            "layout": body.layout,
            "include_www": body.include_www,
            "persistent_paths": body.persistent_paths,
            "memory_max_mb": body.memory_max_mb,
            "cpu_quota_percent": body.cpu_quota_percent,
            "tasks_max": body.tasks_max,
        },
        metadata={"domain": domain, "app_type": body.app_type, "port": port},
        actor=actor_label(session),
    )

    return JobAcceptedResponse(
        job_id=job.id,
        status=job.status.value,
        message=f"Deployment queued for {domain}",
        job=job.to_dict(),
    )


class InspectSourceRequest(BaseModel):
    """Request to preview what a repository is before deploying it."""

    source: str = Field(..., description="Git URL, archive URL or local path")
    branch: str | None = Field(default=None, description="Git branch to inspect")


class EnvKeyResponse(BaseModel):
    """One environment variable discovered in a repository's .env.example."""

    name: str
    default: str | None = None
    secret: bool
    required: bool


class SourceInspectionResponse(BaseModel):
    """
    What a repository is, discovered before anything is deployed from it.

    Attributes:
        app_type: The application type the new-app wizard would deploy as;
            the first entry of ``detected_types``.
        detected_types: Every registered application type that recognised
            the repository, most specific first (registry priority order).
        package_manager: The Node package manager the repository's lock file
            implies, or None when it is not a Node project.
        install_command: Argv the chosen deployer would run to install
            dependencies. Empty when the type has none.
        build_command: Argv the chosen deployer would run to build the
            project. Empty when there is nothing to build.
        start_command: Shell command the chosen deployer would run as the
            service's ``ExecStart``. Empty for a static site.
        default_port: Port the chosen deployer uses when none is requested.
        env_keys: Environment variables discovered from ``.env.example``.
        branch: The branch inspected, or the checkout's current branch when
            none was requested.
        commit: Short commit hash of the checkout, empty when the source is
            not a Git repository.
    """

    app_type: str
    detected_types: list[str]
    package_manager: str | None
    install_command: list[str]
    build_command: list[str]
    start_command: str
    default_port: int
    env_keys: list[EnvKeyResponse]
    branch: str
    commit: str


# NOTE: declared before GET /{domain} and its siblings so a request for
# /api/apps/inspect is never shadowed by a route that treats "inspect" as a
# domain.
@router.post("/inspect", response_model=SourceInspectionResponse)
def inspect_app_source(
    body: InspectSourceRequest, session: Annotated[dict, Depends(get_current_session)]
) -> SourceInspectionResponse:
    """
    Preview what a repository is before deploying it.

    Fetches the source into a throwaway checkout, detects the application
    type, and reports the commands, port and environment variables a
    deployment would use, so the new-app wizard has something real to show
    instead of a guess. Nothing is written outside the checkout, which is
    removed before this returns, and no application, domain or unit is
    created.

    Args:
        body: The source to inspect and the branch to check out.
        session: The authenticated session.

    Returns:
        The inspection result.

    Raises:
        SourceError: The source is invalid, or fetching it failed. Answered
            as 400: the operator gave a source WASM cannot reach, not a
            server fault.
        ValidationError: The checkout matches no registered application
            type. ``inspect_source`` raises ``DeploymentError`` for this -
            right for the CLI, where it means the whole operation failed -
            but here it is the wizard's input that could not be classified,
            so it is translated to the API's validation-error contract
            (400 with details) instead of the 500 an unqualified
            ``DeploymentError`` would answer.
    """
    try:
        result = inspect_source(body.source, branch=body.branch)
    except DeploymentError as exc:
        raise ValidationError(exc.message, details=exc.details) from exc
    return SourceInspectionResponse(
        app_type=result.app_type,
        detected_types=result.detected_types,
        package_manager=result.package_manager,
        install_command=result.install_command,
        build_command=result.build_command,
        start_command=result.start_command,
        default_port=result.default_port,
        env_keys=[
            EnvKeyResponse(
                name=key.name,
                default=key.default,
                secret=key.secret,
                required=key.required,
            )
            for key in result.env_keys
        ],
        branch=result.branch,
        commit=result.commit,
    )


class AppTypeInfo(BaseModel):
    """One application type the new-app wizard may offer."""

    type: str
    name: str
    default_port: int


class AppTypesResponse(BaseModel):
    """Every application type WASM can deploy."""

    types: list[AppTypeInfo]


# NOTE: declared before GET /{domain} for the same reason /inspect is: a
# request for /api/apps/types must not be read as a request for the
# application whose domain is literally "types".
@router.get("/types", response_model=AppTypesResponse)
def list_app_types(session: Annotated[dict, Depends(get_current_session)]) -> AppTypesResponse:
    """
    List the application types WASM can deploy.

    :func:`~wasm.deployers.registry.available_types` is the one source of
    truth - the CLI's ``--type`` choices come from it too - so a deployer
    registered with :meth:`~wasm.deployers.registry.DeployerRegistry.register`
    reaches the wizard the moment it exists, instead of needing a second,
    hand-kept copy of the list in the console.

    Args:
        session: The authenticated session.

    Returns:
        Every registered type, most specific first, ``auto`` last.
    """
    return AppTypesResponse(
        types=[
            AppTypeInfo(type=entry["type"], name=entry["name"], default_port=entry["default_port"])
            for entry in available_types()
        ]
    )


@router.get("/{domain}", response_model=AppInfo)
def get_app(domain: str, session: Annotated[dict, Depends(get_current_session)]) -> AppInfo:
    """
    Describe one application.

    Args:
        domain: Domain of the application.
        session: The authenticated session.

    Returns:
        The application description.

    Raises:
        HTTPException: 404 when the application is unknown.
    """
    validated = strict_domain(domain)

    store = get_store()
    app = store.get_app(validated)
    if app is None:
        raise HTTPException(status_code=404, detail=f"Application not found: {validated}")

    service = store.get_service_by_app_id(app.id) if app.id else None
    manager = ServiceManager(verbose=False)
    state, status = resolve_state_with_status(app, manager)

    recent = store.list_deployments(domain=app.domain, limit=1)

    info = _to_app_info(
        app,
        state,
        status,
        service,
        webhook_enabled=store.get_webhook_secret(app.domain) is not None,
        last_deployment=recent[0] if recent else None,
    )
    # Only the detail view pays for this: one deployer instantiation, not one
    # per application in the list.
    info.build_command = _deployer_build_command(app)
    return info


def _service_action(domain: str, action: str, past_tense: str) -> AppActionResponse:
    """
    Run one systemctl verb against an application's unit.

    Args:
        domain: Domain of the application, as supplied by the client.
        action: ServiceManager method to call.
        past_tense: Word used in the response message.

    Returns:
        The action outcome.

    Raises:
        HTTPException: 404 when the application has no unit.
        ServiceError: When systemd refuses the operation.
    """
    validated = strict_domain(domain)
    app_name = domain_to_app_name(validated)

    manager = ServiceManager(verbose=False)
    if not manager.get_status(app_name).get("exists"):
        raise HTTPException(status_code=404, detail=f"Application not found: {validated}")

    getattr(manager, action)(app_name)

    return AppActionResponse(
        success=True, message=f"Application {past_tense}: {validated}", domain=validated
    )


@router.post("/{domain}/start", response_model=AppActionResponse)
def start_app(
    domain: str, session: Annotated[dict, Depends(get_current_session)]
) -> AppActionResponse:
    """
    Start an application.

    Args:
        domain: Domain of the application.
        session: The authenticated session.

    Returns:
        The action outcome.
    """
    return _service_action(domain, "start", "started")


@router.post("/{domain}/stop", response_model=AppActionResponse)
def stop_app(
    domain: str, session: Annotated[dict, Depends(get_current_session)]
) -> AppActionResponse:
    """
    Stop an application.

    Args:
        domain: Domain of the application.
        session: The authenticated session.

    Returns:
        The action outcome.
    """
    return _service_action(domain, "stop", "stopped")


@router.post("/{domain}/restart", response_model=AppActionResponse)
def restart_app(
    domain: str, session: Annotated[dict, Depends(get_current_session)]
) -> AppActionResponse:
    """
    Restart an application.

    Args:
        domain: Domain of the application.
        session: The authenticated session.

    Returns:
        The action outcome.
    """
    return _service_action(domain, "restart", "restarted")


@router.get("/{domain}/logs", response_model=AppLogsResponse)
def get_app_logs(
    domain: str,
    session: Annotated[dict, Depends(get_current_session)],
    lines: Annotated[int, Query(ge=1, le=1000)] = 100,
) -> AppLogsResponse:
    """
    Read journal output for an application.

    Args:
        domain: Domain of the application.
        lines: How many lines to return.
        session: The authenticated session.

    Returns:
        The log output.
    """
    validated = strict_domain(domain)
    app_name = domain_to_app_name(validated)

    logs = ServiceManager(verbose=False).logs(app_name, lines=lines) or "No logs available"

    return AppLogsResponse(domain=validated, logs=logs, lines=lines)


@router.get("/{domain}/env", response_model=AppEnvResponse)
def get_app_env(
    domain: str,
    request: Request,
    session: Annotated[dict, Depends(get_current_session)],
    unmask: Annotated[bool, Query()] = False,
) -> AppEnvResponse:
    """
    Read an application's environment from its ``.env`` file.

    This reads the file :mod:`wasm.deployers.helpers.app_env` writes, the
    same one ``wasm env show`` reads on the terminal - not the snapshot the
    store recorded at deploy time, which can drift the moment anyone edits
    the file by hand. On the release layout that is ``shared/.env``.

    Args:
        domain: Domain of the application.
        request: The incoming request, for the audit record.
        session: The authenticated session.
        unmask: When true, values are returned in clear instead of redacted.
            Every such read is audited, naming the caller but never a value.

    Returns:
        The stored variables, redacted unless unmask was asked for.

    Raises:
        HTTPException: 403 when unmasking with less than an admin credential,
            404 when the application is unknown.
    """
    if unmask:
        # Here, in the function that produces the secrets, and not in the URL
        # policy: the panel's reveal and edit pages call this function too, and
        # a guard keyed on the /api path let a read token through them.
        ensure_scope(request, session, "admin")
        ensure_elevated(request, session)

    app = _env_app(domain)
    values = read_app_env(app)

    if unmask:
        audit = get_audit_logger()
        if audit:
            audit.record(
                action="apps.env.reveal",
                result="success",
                client_ip=get_client_ip(request),
                actor=str(session.get("sid")),
                resource=f"/api/apps/{app.domain}/env",
                detail=f"revealed {len(values)} variable(s) in clear",
            )
        return AppEnvResponse(domain=app.domain, variables=dict(values), unmasked=True)

    return AppEnvResponse(domain=app.domain, variables=_redact_env(values), unmasked=False)


@router.put("/{domain}/env", response_model=AppEnvUpdateResponse)
def update_app_env(
    domain: str,
    body: UpdateAppEnvRequest,
    request: Request,
    session: Annotated[dict, Depends(require_elevated)],
) -> AppEnvUpdateResponse:
    """
    Replace an application's ``.env`` file wholesale.

    Every name and value is validated against what can safely reach a
    systemd unit (:mod:`wasm.validators.environment`) before anything is
    written, so a rejected variable leaves the file on disk untouched. The
    write goes through :func:`~wasm.deployers.helpers.app_env.write_app_env`,
    the same function ``wasm env configure`` uses, so the file lands 0600,
    owned by the service account, in ``shared/`` on the release layout.

    The application is not restarted: a process already running keeps the
    environment it started with until it is, so the caller is told a restart
    is required rather than one being queued silently underneath it.

    Args:
        domain: Domain of the application.
        body: The complete name to value mapping to write.
        request: The incoming request, for the audit record.
        session: The authenticated session.

    Returns:
        Confirmation that a restart is required to pick up the change.

    Raises:
        HTTPException: 404 when the application is unknown, 422 when a name
            or a value is not safe to write into a systemd unit.
    """
    app = _env_app(domain)

    try:
        clean = validate_environment(body.variables)
    except EnvironmentValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    before = read_app_env(app)
    write_app_env(app, clean)

    changed = sorted(key for key in set(before) | set(clean) if before.get(key) != clean.get(key))
    audit = get_audit_logger()
    if audit:
        audit.record(
            action="apps.env.update",
            result="success",
            client_ip=get_client_ip(request),
            actor=str(session.get("sid")),
            resource=f"/api/apps/{app.domain}/env",
            detail=f"changed keys: {', '.join(changed)}" if changed else "no keys changed",
        )

    return AppEnvUpdateResponse(domain=app.domain, restart_required=True)


@router.delete("/{domain}", response_model=JobAcceptedResponse, status_code=202)
def delete_app(
    domain: str,
    session: Annotated[dict, Depends(require_elevated)],
    remove_files: Annotated[bool, Query()] = False,
    remove_ssl: Annotated[bool, Query()] = False,
) -> JobAcceptedResponse:
    """
    Queue the removal of an application.

    Deletion stops a unit, rewrites the web server configuration, may call
    certbot and may delete a large directory, so it runs as a job rather than
    on the request path.

    Args:
        domain: Domain of the application.
        remove_files: Also delete the application directory.
        remove_ssl: Also delete the certificate.
        session: The authenticated session.

    Returns:
        The queued job.

    Raises:
        HTTPException: 404 when the application is unknown.
    """
    validated = strict_domain(domain)

    if get_store().get_app(validated) is None:
        raise HTTPException(status_code=404, detail=f"Application not found: {validated}")

    job = get_job_manager().create_job(
        job_type=JobType.DELETE,
        name=f"Delete {validated}",
        description=f"Deleting the application at {validated}",
        func=delete_app_job,
        kwargs={
            "domain": validated,
            "remove_files": remove_files,
            "remove_ssl": remove_ssl,
        },
        metadata={"domain": validated},
        actor=actor_label(session),
    )

    return JobAcceptedResponse(
        job_id=job.id,
        status=job.status.value,
        message=f"Deletion queued for {validated}",
        job=job.to_dict(),
    )


class RollbackPointOut(BaseModel):
    """One backup an application can be rolled back to."""

    id: str
    created_at: str
    description: str
    size_bytes: int
    git_commit: str | None = None

    _iso_timestamps = iso_offset_validator("created_at")


class RollbackPointsResponse(BaseModel):
    """The backups an application can return to, newest first."""

    items: list[RollbackPointOut]
    total: int


@router.get("/{domain}/rollback-points", response_model=RollbackPointsResponse)
def list_rollback_points(
    domain: str, session: Annotated[dict, Depends(get_current_session)]
) -> RollbackPointsResponse:
    """
    List the backups an application can be rolled back to.

    Deliberately not gated on the application still being deployed: a backup
    for a domain WASM no longer serves is still a rollback point until it is
    pruned, the same reasoning that keeps deployment history around after an
    application is deleted (see :mod:`wasm.web.views.deployments`).

    Args:
        domain: Domain whose rollback points are asked for.
        session: The authenticated session.

    Returns:
        The points, newest first, from
        :meth:`~wasm.managers.backup_manager.RollbackManager.list_rollback_points`
        - the one implementation, shared with ``wasm backup rollback --list``.
    """
    validated = strict_domain(domain)
    points = RollbackManager(verbose=False).list_rollback_points(validated)
    items = [
        RollbackPointOut(
            id=point.id,
            created_at=point.created_at,
            description=point.description,
            size_bytes=point.size_bytes,
            git_commit=point.git_commit,
        )
        for point in points
    ]
    return RollbackPointsResponse(items=items, total=len(items))


# ---------------------------------------------------------------------------
# Releases
# ---------------------------------------------------------------------------


class ReleaseOut(BaseModel):
    """
    One release of an application on the release layout.

    Attributes:
        id: Release id, the directory name under ``releases/``.
        commit: Short commit it was built from; None for a non-git source.
        created_at: When it was created, ISO 8601 with an explicit UTC offset.
        activated_at: When it last became active, if it ever did.
        status: ``active``, ``superseded``, ``rolled_back``, ``failed`` or
            ``built``.
        active: Whether it is the one serving.
        on_disk: Whether it can be activated. A failed release is listed for
            a while after its directory was removed.
    """

    id: str
    commit: str | None = None
    created_at: str
    activated_at: str | None = None
    status: str
    active: bool
    on_disk: bool

    _iso_timestamps = iso_offset_validator("created_at", "activated_at")


class ReleasesResponse(BaseModel):
    """The releases of an application, newest first."""

    domain: str
    items: list[ReleaseOut]
    total: int


class ReleaseActivationResponse(BaseModel):
    """
    The outcome of activating a release.

    Attributes:
        domain: The application's domain.
        release_id: The release now serving.
        previous_id: The release that served before, if any.
        changed: False when the release was already active and nothing was done.
        rolled_back: Whether the release activated is older than the one it
            replaced.
        deployment_id: The deployment history row that records it.
    """

    domain: str
    release_id: str
    previous_id: str | None = None
    changed: bool
    rolled_back: bool
    deployment_id: int | None = None


def _release_app_or_error(domain: str) -> App:
    """
    Look up an application whose releases a request is about.

    Args:
        domain: Domain from the request.

    Returns:
        The stored application, on the release layout.

    Raises:
        HTTPException: 404 when it is unknown, 409 when it is deployed in
            place: it has no releases until it is migrated.
    """
    app = _env_app(domain)
    if app.layout != RELEASES:
        raise HTTPException(
            status_code=409,
            detail=f"{app.domain} is deployed in place and has no releases. Migrate it to "
            f"releases first (POST /api/apps/{app.domain}/migrate), or roll back to a backup.",
        )
    return app


@router.get("/{domain}/releases", response_model=ReleasesResponse)
def get_app_releases(
    domain: str, session: Annotated[dict, Depends(get_current_session)]
) -> ReleasesResponse:
    """
    List an application's releases, newest first.

    Args:
        domain: Domain of the application.
        session: The authenticated session.

    Returns:
        The releases, from :func:`wasm.deployers.lifecycle.list_releases`,
        the same listing ``wasm releases list`` prints.

    Raises:
        HTTPException: 404 when the application is unknown, 409 when it is
            deployed in place.
    """
    app = _release_app_or_error(domain)
    items = [
        ReleaseOut(
            id=release.id,
            commit=release.commit,
            created_at=release.created_at,
            activated_at=release.activated_at,
            status=release.status,
            active=release.active,
            on_disk=release.on_disk,
        )
        for release in list_releases(app.domain)
    ]
    return ReleasesResponse(domain=app.domain, items=items, total=len(items))


@router.post("/{domain}/releases/{release_id}/activate", response_model=ReleaseActivationResponse)
def activate_app_release(
    domain: str, release_id: str, session: Annotated[dict, Depends(get_current_session)]
) -> ReleaseActivationResponse:
    """
    Make a release the one that serves: an instant rollback, or a roll forward.

    Needs the ``deploy`` scope, like queueing an update: it changes what code
    runs, and nothing else. The release passes the same health gate as a
    deploy; one that does not is recorded as failed and the release that was
    serving is put back before this answers.

    Args:
        domain: Domain of the application.
        release_id: The release to activate.
        session: The authenticated session.

    Returns:
        What was done.

    Raises:
        HTTPException: 400 for something that is not a release id, 404 for an
            unknown application or a release that is not on disk, 409 for an
            application deployed in place.
        DeploymentError: The release did not pass its health check; the
            details carry the probe's and the journal's own output.
    """
    app = _release_app_or_error(domain)
    if not is_release_id(release_id):
        raise HTTPException(status_code=400, detail=f"Not a release id: {release_id!r}")
    if not any(r.id == release_id and r.on_disk for r in list_releases(app.domain)):
        raise HTTPException(
            status_code=404, detail=f"Release {release_id} of {app.domain} is not on disk"
        )

    outcome = activate_release(app.domain, release_id, trigger=DeploymentTrigger.PANEL.value)
    return ReleaseActivationResponse(
        domain=outcome.domain,
        release_id=outcome.release.id,
        previous_id=outcome.previous.id if outcome.previous is not None else None,
        changed=outcome.changed,
        rolled_back=outcome.went_back,
        deployment_id=outcome.deployment_id,
    )


# ---------------------------------------------------------------------------
# Migration to the release layout
# ---------------------------------------------------------------------------


class MigrationPlanOut(BaseModel):
    """
    What migrating an in-place application to releases would do.

    Attributes:
        domain: The application's domain.
        app_path: Its directory.
        release_id: The name the first release would get (a forecast).
        commit: The commit the tree is at, when it is a git checkout.
        persistent: Paths that move to ``shared/`` and are linked into every
            release from now on.
        persistent_source: ``git`` (what git does not track), ``explicit``
            (as named) or ``common`` (the usual upload directories).
        env_files: Environment files that move to ``shared/``.
        unit: The unit that runs it, or None for a site.
        unit_rewrite: Whether the unit is rewritten to run from ``current``.
        site_rewrite: Whether the site is rewritten to serve ``current``.
        untracked_files: Files that stay in the first release only.
        warnings: What the operator should read before going ahead.
        files: Regular files the directory holds; all of them are kept.
        bytes: Their total size.
    """

    domain: str
    app_path: str
    release_id: str
    commit: str | None = None
    persistent: list[str]
    persistent_source: str
    env_files: list[str]
    unit: str | None = None
    unit_rewrite: bool
    site_rewrite: bool
    untracked_files: list[str]
    warnings: list[str]
    files: int
    bytes: int


class MigrateRequest(BaseModel):
    """Request to migrate an application to the release layout."""

    persist: list[str] | None = Field(
        default=None,
        description="Paths to keep in shared/, relative to the application. "
        "Omitted: what git does not track, or the usual upload directories",
    )


class MigrationResultOut(BaseModel):
    """
    What a migration did.

    Attributes:
        domain: The application's domain.
        release_id: The first release, now active.
        persistent: What is kept in ``shared/``.
        env_files: Environment files moved to ``shared/``.
        files_before: Regular files before.
        files_after: Regular files after, ``shared/`` included; always equal.
        bytes_before: Their size before.
        bytes_after: Their size after.
        unit_rewritten: Whether the unit was rewritten.
        site_rewritten: Whether the site was rewritten.
        deployment_id: The deployment history row that records it.
    """

    domain: str
    release_id: str
    persistent: list[str]
    env_files: list[str]
    files_before: int
    files_after: int
    bytes_before: int
    bytes_after: int
    unit_rewritten: bool
    site_rewritten: bool
    deployment_id: int | None = None


def _migratable(domain: str) -> App:
    """
    Look up an application a migration request is about.

    Args:
        domain: Domain from the request.

    Returns:
        The stored application, deployed in place.

    Raises:
        HTTPException: 404 when it is unknown, 409 when it is on releases
            already.
    """
    app = _env_app(domain)
    if app.layout == RELEASES:
        raise HTTPException(
            status_code=409, detail=f"{app.domain} is on the release layout already"
        )
    return app


def _plan_out(plan: MigrationPlan) -> MigrationPlanOut:
    """
    Translate a plan to its API model.

    Args:
        plan: The plan.

    Returns:
        The model.
    """
    return MigrationPlanOut(
        domain=plan.domain,
        app_path=plan.app_path,
        release_id=plan.release_id,
        commit=plan.commit,
        persistent=list(plan.persistent),
        persistent_source=plan.persistent_source,
        env_files=list(plan.env_files),
        unit=plan.unit,
        unit_rewrite=plan.unit_rewrite,
        site_rewrite=plan.site_rewrite,
        untracked_files=list(plan.untracked_files),
        warnings=list(plan.warnings),
        files=plan.count.files,
        bytes=plan.count.bytes,
    )


@router.get("/{domain}/migrate/plan", response_model=MigrationPlanOut)
def get_migration_plan(
    domain: str,
    session: Annotated[dict, Depends(get_current_session)],
    persist: Annotated[list[str] | None, Query()] = None,
) -> MigrationPlanOut:
    """
    Show what migrating an in-place application to releases would do. Changes nothing.

    Args:
        domain: Domain of the application.
        session: The authenticated session.
        persist: Paths to keep in ``shared/``; repeat the parameter for each.

    Returns:
        The plan, from :func:`wasm.deployers.migrate.plan_migration`.

    Raises:
        HTTPException: 404 when the application is unknown, 409 when it is on
            releases already.
        ValidationError: A path in ``persist`` is not inside the application.
        DeploymentError: Its type cannot use releases.
    """
    app = _migratable(domain)
    return _plan_out(plan_migration(app.domain, persist))


@router.post("/{domain}/migrate", response_model=MigrationResultOut)
def migrate_app(
    domain: str,
    body: MigrateRequest,
    session: Annotated[dict, Depends(require_elevated)],
) -> MigrationResultOut:
    """
    Move an in-place application onto the release layout.

    Rewrites the unit and the site and moves the whole application tree, so
    it needs sudo mode. The plan is worked out again here rather than taken
    from the client: what is executed is what is on disk now.

    Args:
        domain: Domain of the application.
        body: Paths to keep in ``shared/``, if the detection is not wanted.
        session: The authenticated, elevated session.

    Returns:
        What was done.

    Raises:
        HTTPException: 404 when the application is unknown, 409 when it is on
            releases already.
        ValidationError: A path in ``persist`` is not inside the application.
        DeploymentError: A step failed or the application did not answer on
            the new layout; everything was put back, and the details carry
            the health check's own output.
    """
    app = _migratable(domain)
    plan = plan_migration(app.domain, body.persist)
    result = migrate(app.domain, plan, trigger=DeploymentTrigger.PANEL.value)
    return MigrationResultOut(
        domain=result.domain,
        release_id=result.release_id,
        persistent=list(result.persistent),
        env_files=list(result.env_files),
        files_before=result.before.files,
        files_after=result.after.files,
        bytes_before=result.before.bytes,
        bytes_after=result.after.bytes,
        unit_rewritten=result.unit_rewritten,
        site_rewritten=result.site_rewritten,
        deployment_id=result.deployment_id,
    )


# ---------------------------------------------------------------------------
# Resource limits
# ---------------------------------------------------------------------------


class UpdateLimitsRequest(BaseModel):
    """
    The memory, CPU and task limits an application's unit must have.

    The three are set together: a field left out or null removes that limit.
    """

    memory_max_mb: int | None = Field(
        default=None, description="MemoryMax, in MB; at least 64. Null: no limit"
    )
    cpu_quota_percent: int | None = Field(
        default=None,
        description="CPUQuota, in percent of one CPU (200 is two CPUs); 1 to 100 per CPU. "
        "Null: no limit",
    )
    tasks_max: int | None = Field(
        default=None, description="TasksMax, processes and threads; at least 16. Null: no limit"
    )
    restart: bool = Field(
        default=False, description="Restart now, so the processes run under the new limits"
    )


class LimitsResponse(BaseModel):
    """
    The limits an application has now.

    Attributes:
        domain: The application's domain.
        memory_max_mb: Memory limit in MB, or None.
        cpu_quota_percent: CPU quota in percent of one CPU, or None.
        tasks_max: Task limit, or None.
        units: The units rewritten.
        restarted: Whether they were restarted.
        restart_required: Whether the running processes still have the old
            limits until they are restarted.
    """

    domain: str
    memory_max_mb: int | None = None
    cpu_quota_percent: int | None = None
    tasks_max: int | None = None
    units: list[str]
    restarted: bool
    restart_required: bool


@router.patch("/{domain}/limits", response_model=LimitsResponse)
def update_app_limits(
    domain: str,
    body: UpdateLimitsRequest,
    session: Annotated[dict, Depends(require_elevated)],
) -> LimitsResponse:
    """
    Set the memory, CPU and task limits of an application's unit.

    Rewrites the unit (every unit, for a monorepo) and reloads systemd, which
    is a change to what runs as root's configuration, so it needs sudo mode
    like editing a unit by hand does. The values are validated where every
    caller's are, in the service manager.

    Args:
        domain: Domain of the application.
        body: The limits, and whether to restart now.
        session: The authenticated, elevated session.

    Returns:
        The limits the application has now.

    Raises:
        HTTPException: 404 when the application is unknown.
        ValidationError: A limit is out of range (400, with the range).
        DeploymentError: Nothing runs as a unit for it.
    """
    app = _env_app(domain)
    change = set_resource_limits(
        app.domain,
        ResourceLimits(
            memory_max_mb=body.memory_max_mb,
            cpu_quota_percent=body.cpu_quota_percent,
            tasks_max=body.tasks_max,
        ),
        restart=body.restart,
    )
    return LimitsResponse(
        domain=change.domain,
        memory_max_mb=change.limits.memory_max_mb,
        cpu_quota_percent=change.limits.cpu_quota_percent,
        tasks_max=change.limits.tasks_max,
        units=list(change.units),
        restarted=change.restarted,
        restart_required=not change.restarted,
    )
