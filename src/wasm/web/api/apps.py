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
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from wasm.core.config import REDACTED, redact_secrets
from wasm.core.store import App, Service, get_store
from wasm.core.utils import domain_to_app_name
from wasm.deployers.helpers.env_manager import EnvManager, redact_url_credentials
from wasm.managers.service_manager import ServiceManager
from wasm.validators.environment import EnvironmentValidationError, validate_environment
from wasm.validators.port import find_available_port, validate_port
from wasm.web.api.auth import get_current_session
from wasm.web.api.deps import JobAcceptedResponse, WASMErrorRoute, strict_domain
from wasm.web.auth import get_audit_logger, get_client_ip
from wasm.web.jobs import JobType, delete_app_job, deploy_app_job, get_job_manager

router = APIRouter(route_class=WASMErrorRoute)

#: Port preferred when the client does not pick one.
DEFAULT_PORT = 3000


class AppInfo(BaseModel):
    """
    A deployed application and the live state of its service.

    Attributes:
        name: Application name, which is its domain.
        domain: Domain the application is served on.
        status: ``running``, ``stopped`` or ``static``.
        active: Whether the unit is active.
        enabled: Whether the unit starts on boot.
        pid: Main PID when running.
        uptime: How long the unit has been up.
        port: Upstream port.
        app_type: Deployer that owns it.
        path: Application directory.
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


class AppListResponse(BaseModel):
    """Response for listing applications."""

    apps: list[AppInfo]
    total: int


class CreateAppRequest(BaseModel):
    """Request to deploy a new application."""

    domain: str = Field(..., description="Target domain name")
    source: str = Field(..., description="Git URL or local path")
    app_type: str = Field(default="auto", description="Application type")
    port: int | None = Field(default=None, description="Application port")
    webserver: str = Field(default="nginx", description="Web server to use")
    branch: str | None = Field(default=None, description="Git branch to deploy")
    ssl: bool = Field(default=True, description="Obtain a certificate")
    env_vars: dict[str, str] = Field(default_factory=dict, description="Environment variables")


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


def _service_status(store_service: Service | None, manager: ServiceManager) -> dict[str, Any]:
    """
    Read the live systemd state of an application's unit.

    Args:
        store_service: The service record, when the application has one.
        manager: Service manager used to query systemd.

    Returns:
        The status mapping, empty when the application has no unit.
    """
    if store_service is None:
        return {}
    return manager.get_status(store_service.name)


def _to_app_info(app: App, status: dict[str, Any], has_service: bool) -> AppInfo:
    """
    Combine a stored application with its live service status.

    Args:
        app: The stored application.
        status: Live systemd status, empty when there is no unit.
        has_service: Whether a unit is registered for the application.

    Returns:
        The API representation.
    """
    pid = status.get("pid")
    active = bool(status.get("active", False))
    return AppInfo(
        name=app.domain,
        domain=app.domain,
        status="running" if active else ("stopped" if has_service else "static"),
        active=active,
        enabled=bool(status.get("enabled", False)),
        pid=int(pid) if pid and str(pid) != "0" else None,
        uptime=str(status["uptime"]) if status.get("uptime") else None,
        port=app.port,
        app_type=app.app_type,
        path=app.app_path,
    )


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

    Args:
        session: The authenticated session.

    Returns:
        The applications, each with the live state of its service.
    """
    store = get_store()
    manager = ServiceManager(verbose=False)

    apps = []
    for app in store.list_apps():
        service = store.get_service_by_app_id(app.id) if app.id else None
        apps.append(_to_app_info(app, _service_status(service, manager), service is not None))

    return AppListResponse(apps=apps, total=len(apps))


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
        },
        metadata={"domain": domain, "app_type": body.app_type, "port": port},
    )

    return JobAcceptedResponse(
        job_id=job.id,
        status=job.status.value,
        message=f"Deployment queued for {domain}",
        job=job.to_dict(),
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

    return _to_app_info(app, _service_status(service, manager), service is not None)


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

    This reads the file :mod:`wasm.deployers.helpers.env_manager` writes, the
    same one ``wasm env show`` reads on the terminal - not the snapshot the
    store recorded at deploy time, which can drift the moment anyone edits
    the file by hand.

    Args:
        domain: Domain of the application.
        request: The incoming request, for the audit record.
        session: The authenticated session.
        unmask: When true, values are returned in clear instead of redacted.
            Every such read is audited, naming the caller but never a value.

    Returns:
        The stored variables, redacted unless unmask was asked for.

    Raises:
        HTTPException: 404 when the application is unknown.
    """
    app = _env_app(domain)
    values = (
        EnvManager(verbose=False).get_current_values(Path(app.app_path)) if app.app_path else {}
    )

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
    session: Annotated[dict, Depends(get_current_session)],
) -> AppEnvUpdateResponse:
    """
    Replace an application's ``.env`` file wholesale.

    Every name and value is validated against what can safely reach a
    systemd unit (:mod:`wasm.validators.environment`) before anything is
    written, so a rejected variable leaves the file on disk untouched. The
    write goes through :class:`~wasm.deployers.helpers.env_manager.EnvManager`,
    the same seam ``wasm env configure`` uses, so the file lands 0600 either
    way.

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

    app_path = Path(app.app_path)
    manager = EnvManager(verbose=False)
    before = manager.get_current_values(app_path)
    manager.write_env_files(app_path, clean)

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
    session: Annotated[dict, Depends(get_current_session)],
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
    )

    return JobAcceptedResponse(
        job_id=job.id,
        status=job.status.value,
        message=f"Deletion queued for {validated}",
        job=job.to_dict(),
    )
