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

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from wasm.core.store import App, Service, get_store
from wasm.core.utils import domain_to_app_name
from wasm.managers.service_manager import ServiceManager
from wasm.validators.port import find_available_port, validate_port
from wasm.web.api.auth import get_current_session
from wasm.web.api.deps import JobAcceptedResponse, WASMErrorRoute, strict_domain
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
