"""
Services API endpoints.

Provides endpoints for managing systemd services.

Two rules govern this module:

- **Every name is validated before it becomes a path.** The panel runs as root,
  and a unit name arriving in a JSON body is not constrained by the router's
  path matching. Names go through :func:`wasm.validators.names.validate_service_name`
  and paths through :func:`wasm.validators.names.resolve_within`, so a write can
  only ever land inside :data:`SYSTEMD_UNIT_DIR`.
- **Handlers are synchronous.** They call systemctl and journalctl, which block.
  Declared ``async def`` they would run on the event loop and freeze the whole
  panel for every other request; declared ``def``, FastAPI runs them in the
  threadpool.
"""

from __future__ import annotations

import re
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel

from wasm.core.config import SYSTEMD_DIR
from wasm.core.exceptions import SecurityError, ValidationError, WASMError
from wasm.core.store import get_store
from wasm.managers.service_manager import ServiceManager
from wasm.validators.names import resolve_within, validate_service_name
from wasm.web.api.auth import get_current_session

router = APIRouter()

#: Directory unit files are read from and written to. Module level on purpose:
#: the path used to be interpolated inline at each call site, which made the
#: write path impossible to exercise in a test and is how the traversal
#: survived. Tests point this at a sandbox.
SYSTEMD_UNIT_DIR: Path = SYSTEMD_DIR

#: Units created by older WASM versions carry this prefix.
LEGACY_PREFIX = "wasm-"

#: Values systemd accepts for ``Restart=``.
VALID_RESTART_POLICIES = frozenset(
    {
        "no",
        "always",
        "on-success",
        "on-failure",
        "on-abnormal",
        "on-abort",
        "on-watchdog",
    }
)

#: Accounts a unit may run as.
USER_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]{0,31}\$?$")

#: Environment variable names, as accepted by the shell and by systemd.
ENV_KEY_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class ServiceInfo(BaseModel):
    """Service information."""

    name: str
    description: str | None = None
    active: bool
    enabled: bool
    status: str
    pid: int | None = None
    uptime: str | None = None
    memory: str | None = None


class ServiceListResponse(BaseModel):
    """Response for listing services."""

    services: list[ServiceInfo]
    total: int


class ServiceActionResponse(BaseModel):
    """Response for service actions."""

    success: bool
    message: str
    service: str


class CreateServiceRequest(BaseModel):
    """Request to create a new service."""

    name: str
    command: str | None = None
    user: str = "root"
    working_directory: str = "/var/www"
    restart: str = "always"
    environment: dict | None = None
    raw_content: str | None = None  # Raw systemd unit content for advanced mode


class UpdateServiceConfigRequest(BaseModel):
    """Request to update service configuration."""

    config: str


def _bad_request(exc: WASMError) -> HTTPException:
    """
    Turn a validation or containment failure into a client error.

    Args:
        exc: The raised WASM error.

    Returns:
        An HTTPException carrying the actionable message.
    """
    return HTTPException(status_code=400, detail=str(exc))


def _unit_path(service_name: str) -> Path:
    """
    Build the unit file path for an already validated service name.

    Args:
        service_name: Validated unit name, without the ``.service`` suffix.

    Returns:
        The absolute unit file path, guaranteed to be inside SYSTEMD_UNIT_DIR.

    Raises:
        ValidationError: When the resulting name is not a usable path component.
        SecurityError: When the path would leave the unit directory, including
            through a symlink already present in it.
    """
    return resolve_within(SYSTEMD_UNIT_DIR, f"{service_name}.service")


def _resolve_unit(name: str) -> tuple[str, Path]:
    """
    Validate a requested service name and locate its unit file.

    Mirrors the legacy-prefix lookup of
    :meth:`wasm.managers.service_manager.ServiceManager._resolve_service_name`,
    but against :data:`SYSTEMD_UNIT_DIR` instead of a hardcoded directory, so the
    API and its tests agree on where units live.

    Args:
        name: Service name as supplied by the client.

    Returns:
        Tuple of the resolved unit name and its path.

    Raises:
        ValidationError: When the name is not a safe unit name.
        SecurityError: When the unit path would leave the unit directory.
    """
    service_name = validate_service_name(name).removesuffix(".service")
    base = service_name.removeprefix(LEGACY_PREFIX) or service_name

    legacy_name = f"{LEGACY_PREFIX}{base}"
    legacy_path = _unit_path(legacy_name)
    if legacy_path.exists():
        return legacy_name, legacy_path

    return base, _unit_path(base)


def _reject_control_characters(value: str, field: str) -> str:
    """
    Refuse values that could smuggle extra directives into a unit file.

    A newline in ``ExecStart=`` is a new systemd directive, so simple mode must
    not accept one even though advanced mode lets an operator write a whole unit
    by hand.

    Args:
        value: The field value.
        field: Field name, used in the error message.

    Returns:
        The value, unchanged.

    Raises:
        ValidationError: When the value contains a control character.
    """
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValidationError(
            f"Invalid {field}: control characters are not allowed",
            details=(
                f"Remove line breaks and control characters from {field}, or use "
                "advanced mode to supply the whole unit file."
            ),
        )
    return value


def _render_unit(data: CreateServiceRequest, service_name: str) -> str:
    """
    Render a unit file from the simple-mode fields of a create request.

    Args:
        data: The create request.
        service_name: The already validated unit name.

    Returns:
        The unit file content.

    Raises:
        ValidationError: When any field would corrupt the unit file.
    """
    if not data.command:
        raise ValidationError(
            "Command is required in simple mode",
            details="Provide 'command', or send 'raw_content' with a full unit file.",
        )

    command = _reject_control_characters(data.command, "command")
    working_directory = _reject_control_characters(data.working_directory, "working_directory")

    if not working_directory.startswith("/"):
        raise ValidationError(
            f"Working directory must be absolute: {working_directory!r}",
            details="systemd rejects relative WorkingDirectory values.",
        )

    if not USER_NAME_PATTERN.match(data.user):
        raise ValidationError(
            f"Invalid user: {data.user!r}",
            details="Use an existing account name made of letters, digits, '_' and '-'.",
        )

    if data.restart not in VALID_RESTART_POLICIES:
        raise ValidationError(
            f"Invalid restart policy: {data.restart!r}",
            details=f"Use one of: {', '.join(sorted(VALID_RESTART_POLICIES))}.",
        )

    env_section = ""
    if data.environment:
        env_lines = []
        for key, value in data.environment.items():
            if not ENV_KEY_PATTERN.match(str(key)):
                raise ValidationError(
                    f"Invalid environment variable name: {key!r}",
                    details=(
                        "Names must start with a letter or '_' and contain only "
                        "letters, digits and '_'."
                    ),
                )
            text = _reject_control_characters(str(value), f"environment value for {key}")
            if '"' in text or "\\" in text:
                raise ValidationError(
                    f"Invalid environment value for {key}",
                    details="Double quotes and backslashes are not supported here.",
                )
            env_lines.append(f'Environment="{key}={text}"')
        env_section = "\n".join(env_lines) + "\n"

    return f"""[Unit]
Description=WASM Service: {service_name}
After=network.target

[Service]
Type=simple
User={data.user}
WorkingDirectory={working_directory}
ExecStart={command}
Restart={data.restart}
RestartSec=5
{env_section}
[Install]
WantedBy=multi-user.target
"""


@router.get("", response_model=ServiceListResponse)
def list_services(
    request: Request,
    wasm_only: bool = Query(default=True, description="Only show WASM services"),
    session: dict = Depends(get_current_session),
):
    """
    List all services (or only WASM services).
    """
    store = get_store()
    service_manager = ServiceManager(verbose=False)

    # Get services from store
    stored_services = store.list_services()

    result = []
    for svc in stored_services:
        # Get live status from systemd (ServiceManager resolves name automatically)
        live_status = service_manager.get_status(svc.name)

        result.append(
            ServiceInfo(
                name=svc.name,
                description=svc.command,
                active=live_status.get("active", False),
                enabled=live_status.get("enabled", False),
                status="running" if live_status.get("active") else "stopped",
                pid=live_status.get("pid"),
                uptime=live_status.get("uptime"),
                memory=live_status.get("memory"),
            )
        )

    return ServiceListResponse(services=result, total=len(result))


@router.get("/{name}", response_model=ServiceInfo)
def get_service(name: str, request: Request, session: dict = Depends(get_current_session)):
    """
    Get details for a specific service.
    """
    try:
        service_name = validate_service_name(name)
    except ValidationError as exc:
        raise _bad_request(exc) from exc

    store = get_store()
    service_manager = ServiceManager(verbose=False)

    # Handle both prefixed and non-prefixed names for backwards compatibility
    svc = store.get_service(service_name)
    if not svc:
        svc = store.get_service(f"{LEGACY_PREFIX}{service_name}")

    if not svc:
        raise HTTPException(status_code=404, detail=f"Service not found: {service_name}")

    # Get live status from systemd (ServiceManager resolves name automatically)
    live_status = service_manager.get_status(svc.name)

    return ServiceInfo(
        name=svc.name,
        description=svc.command,
        active=live_status.get("active", False),
        enabled=live_status.get("enabled", False),
        status="running" if live_status.get("active") else "stopped",
        pid=live_status.get("pid"),
        uptime=live_status.get("uptime"),
        memory=live_status.get("memory"),
    )


def _run_service_action(name: str, action: str, past_tense: str) -> ServiceActionResponse:
    """
    Validate a service name and run one systemctl verb through the manager.

    Args:
        name: Service name as supplied by the client.
        action: ServiceManager method to call (start, stop, restart, ...).
        past_tense: Word used in the response message.

    Returns:
        The action response.

    Raises:
        HTTPException: 400 for an unsafe name, 404 when the unit does not exist,
            500 when systemd refuses the operation.
    """
    try:
        service_name, service_path = _resolve_unit(name)
    except (ValidationError, SecurityError) as exc:
        raise _bad_request(exc) from exc

    if not service_path.exists():
        raise HTTPException(status_code=404, detail=f"Service not found: {service_name}")

    service_manager = ServiceManager(verbose=False)
    try:
        getattr(service_manager, action)(service_name)
    except WASMError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return ServiceActionResponse(
        success=True,
        message=f"Service {past_tense}: {service_name}",
        service=service_name,
    )


@router.post("/{name}/start", response_model=ServiceActionResponse)
def start_service(name: str, request: Request, session: dict = Depends(get_current_session)):
    """
    Start a service.
    """
    return _run_service_action(name, "start", "started")


@router.post("/{name}/stop", response_model=ServiceActionResponse)
def stop_service(name: str, request: Request, session: dict = Depends(get_current_session)):
    """
    Stop a service.
    """
    return _run_service_action(name, "stop", "stopped")


@router.post("/{name}/restart", response_model=ServiceActionResponse)
def restart_service(name: str, request: Request, session: dict = Depends(get_current_session)):
    """
    Restart a service.
    """
    return _run_service_action(name, "restart", "restarted")


@router.post("/{name}/enable", response_model=ServiceActionResponse)
def enable_service(name: str, request: Request, session: dict = Depends(get_current_session)):
    """
    Enable a service to start on boot.
    """
    return _run_service_action(name, "enable", "enabled")


@router.post("/{name}/disable", response_model=ServiceActionResponse)
def disable_service(name: str, request: Request, session: dict = Depends(get_current_session)):
    """
    Disable a service from starting on boot.
    """
    return _run_service_action(name, "disable", "disabled")


@router.get("/{name}/logs")
def get_service_logs(
    name: str,
    request: Request,
    lines: int = Query(default=100, ge=1, le=1000),
    session: dict = Depends(get_current_session),
):
    """
    Get service logs from journalctl.
    """
    try:
        service_name, _ = _resolve_unit(name)
    except (ValidationError, SecurityError) as exc:
        raise _bad_request(exc) from exc

    service_manager = ServiceManager(verbose=False)
    try:
        logs = service_manager.logs(service_name, lines=lines) or "No logs available"
    except WASMError as exc:
        logs = f"Error retrieving logs: {exc}"

    return {"service": service_name, "logs": logs, "lines": lines}


@router.get("/{name}/config")
def get_service_config(name: str, request: Request, session: dict = Depends(get_current_session)):
    """
    Get the systemd unit file content for a service.
    """
    try:
        service_name, service_path = _resolve_unit(name)
    except (ValidationError, SecurityError) as exc:
        raise _bad_request(exc) from exc

    if not service_path.is_file():
        raise HTTPException(status_code=404, detail=f"Service not found: {service_name}")

    try:
        content = service_path.read_text()
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Error reading config: {exc}") from exc

    return {"service": service_name, "config": content, "path": str(service_path)}


@router.put("/{name}/config", response_model=ServiceActionResponse)
def update_service_config(
    name: str,
    data: UpdateServiceConfigRequest,
    request: Request,
    session: dict = Depends(get_current_session),
):
    """
    Update the systemd unit file content for a service.
    """
    try:
        service_name, service_path = _resolve_unit(name)
    except (ValidationError, SecurityError) as exc:
        raise _bad_request(exc) from exc

    if not service_path.is_file():
        raise HTTPException(status_code=404, detail=f"Service not found: {service_name}")

    try:
        service_path.write_text(data.config)
        service_path.chmod(0o644)
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Error updating config: {exc}") from exc

    ServiceManager(verbose=False).daemon_reload()

    return ServiceActionResponse(
        success=True,
        message=f"Configuration updated for {service_name}. Restart the service to apply changes.",
        service=service_name,
    )


@router.post("", response_model=ServiceActionResponse)
def create_service(
    data: CreateServiceRequest, request: Request, session: dict = Depends(get_current_session)
):
    """
    Create a new systemd service.
    """
    # New services don't use the legacy prefix.
    try:
        service_name = validate_service_name(data.name).removesuffix(".service")
        service_path = _unit_path(service_name)
        service_content = data.raw_content or _render_unit(data, service_name)
    except (ValidationError, SecurityError) as exc:
        raise _bad_request(exc) from exc

    if service_path.exists():
        raise HTTPException(status_code=400, detail=f"Service already exists: {service_name}")

    try:
        service_path.write_text(service_content)
        service_path.chmod(0o644)
    except OSError as exc:
        # A half-written unit would be picked up by the next daemon-reload.
        service_path.unlink(missing_ok=True)
        raise HTTPException(status_code=500, detail=f"Failed to create service: {exc}") from exc

    service_manager = ServiceManager(verbose=False)
    service_manager.daemon_reload()
    service_manager.enable(service_name)

    return ServiceActionResponse(
        success=True, message=f"Service created: {service_name}", service=service_name
    )


@router.delete("/{name}", response_model=ServiceActionResponse)
def delete_service(name: str, request: Request, session: dict = Depends(get_current_session)):
    """
    Delete a systemd service.
    """
    try:
        service_name, service_path = _resolve_unit(name)
    except (ValidationError, SecurityError) as exc:
        raise _bad_request(exc) from exc

    if not service_path.is_file():
        raise HTTPException(status_code=404, detail=f"Service not found: {service_name}")

    service_manager = ServiceManager(verbose=False)
    service_manager.stop(service_name)
    service_manager.disable(service_name)

    try:
        service_path.unlink()
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Failed to delete service: {exc}") from exc

    service_manager.daemon_reload()

    return ServiceActionResponse(
        success=True, message=f"Service deleted: {service_name}", service=service_name
    )
