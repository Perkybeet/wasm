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
- **WASM's own units are not services here.** The console, the monitor and
  the units behind cron jobs and backup schedules are refused to every
  mutation, and the console's and the monitor's journals need ``admin``: see
  :func:`_refuse_own_unit` and :func:`get_service_logs`.
"""

from __future__ import annotations

import re
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel

from wasm.core.config import SYSTEMD_DIR, Config
from wasm.core.exceptions import PermissionError as WASMPermissionError
from wasm.core.exceptions import ServiceError, ValidationError, WASMError
from wasm.core.store import get_store
from wasm.managers.service_manager import ServiceManager, readable_unit_name
from wasm.validators.names import resolve_within, validate_service_name
from wasm.web.api.auth import get_current_session
from wasm.web.api.deps import WASMErrorRoute, require_elevated
from wasm.web.auth import ensure_scope

# The error boundary: ValidationError and SecurityError from name/path
# validation used to be caught by hand at every call site and turned into an
# HTTPException(400) that duplicated exactly what WASMErrorRoute already does
# for any WASMError. Letting them propagate is the one implementation.
router = APIRouter(route_class=WASMErrorRoute)

#: Directory unit files are read from and written to. Module level on purpose:
#: the path used to be interpolated inline at each call site, which made the
#: write path impossible to exercise in a test and is how the traversal
#: survived. Tests point this at a sandbox.
SYSTEMD_UNIT_DIR: Path = SYSTEMD_DIR

#: Units created by older WASM versions carry this prefix.
LEGACY_PREFIX = "wasm-"

#: The one classifier of WASM's own units, bound when this module is
#: imported: the tests swap ``ServiceManager`` for a recording stub, and the
#: refusal must not depend on what a stub chose to implement.
_is_own_unit_name = ServiceManager._is_own_unit_name

#: Where each of WASM's own units is managed instead, keyed by the unit name
#: or, for scheduled work, by its prefix. Every key is covered by
#: ``ServiceManager.OWN_UNITS`` or ``ServiceManager.OWN_UNIT_PREFIXES``.
_OWN_UNIT_HINTS: dict[str, str] = {
    "wasm-web": (
        "wasm-web is the console itself. Manage it on the machine with 'wasm web stop', "
        "'wasm web restart', 'wasm web enable' or 'wasm web disable'."
    ),
    "wasm-monitor": (
        "wasm-monitor is WASM's process monitor. Manage it on the machine with "
        "'wasm monitor enable', 'wasm monitor disable' or 'wasm monitor uninstall'."
    ),
    "wasm-cron-": (
        "This unit runs a cron job. Manage it from Cron (/api/cron) or with 'wasm cron ...', "
        "which keeps its timer in step."
    ),
    "wasm-backup-": (
        "This unit runs a backup schedule. Manage it from Backups (/api/backup-schedules) "
        "or with 'wasm backup schedule ...', which keeps its timer in step."
    ),
}

#: Units whose journal is the console's or the monitor's own output, which only an
#: admin credential reads, here and on ``/ws/logs``.
OWN_JOURNAL_UNITS = frozenset({"wasm-web", "wasm-monitor"})


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
    """
    Service information.

    Attributes:
        managed: Whether WASM manages this unit. False only when listing with
            ``wasm_only=false``, which walks every unit on the host; such a
            row carries systemd's state fields and nothing else (``enabled``
            false, no PID, memory or uptime), read from the one listing.
        active_state: Systemd's own ``ActiveState`` (``active``, ``failed``,
            ``activating``, ...). Distinguishes a unit systemd is repeatedly
            restarting from one that is cleanly stopped, which ``active``
            alone cannot: both report ``active=false`` between attempts.
        sub_state: Systemd's own ``SubState`` (``running``, ``dead``,
            ``auto-restart``, ...), the finer-grained half of the same story.
        result: Systemd's own ``Result`` for the last run (``success``,
            ``exit-code``, ``signal``, ...).
    """

    name: str
    description: str | None = None
    active: bool
    enabled: bool
    status: str
    pid: int | None = None
    uptime: str | None = None
    memory: str | None = None
    managed: bool = True
    active_state: str | None = None
    sub_state: str | None = None
    result: str | None = None


class ServiceListResponse(BaseModel):
    """Response for listing services."""

    services: list[ServiceInfo]
    total: int


class ServiceActionResponse(BaseModel):
    """Response for service actions."""

    success: bool
    message: str
    service: str


class ServiceLogsResponse(BaseModel):
    """Response for reading a service's journal."""

    service: str
    logs: str
    lines: int


class ServiceConfigResponse(BaseModel):
    """
    Response for reading a service's unit file.

    A separate class from :class:`UpdateServiceConfigRequest`, even though the
    ``config`` field is the same string in both directions: sharing one model
    between a request and a response body makes FastAPI generate distinct
    input and output schemas for it, and a field with a default would then
    come out optional to send but required to receive - correct for neither
    direction. ``path`` has no default either way, so this stays simple, but
    the module keeps every read-only body in its own class for that reason.
    """

    service: str
    config: str
    path: str


class CreateServiceRequest(BaseModel):
    """
    Request to create a new service.

    ``user`` left unset resolves to the configured ``service_user`` (the same
    source the CLI uses), never to root. An operator who explicitly asks for
    "root" still gets root.
    """

    name: str
    command: str | None = None
    user: str | None = None
    working_directory: str = "/var/www"
    restart: str = "always"
    environment: dict | None = None
    raw_content: str | None = None  # Raw systemd unit content for advanced mode


class UpdateServiceConfigRequest(BaseModel):
    """Request to update service configuration."""

    config: str


class VerifyUnitRequest(BaseModel):
    """Request to check a candidate unit file before it is saved."""

    content: str


class VerifyUnitResponse(BaseModel):
    """
    Result of checking a candidate unit file.

    Attributes:
        success: Whether systemd-analyze accepted it.
        output: Its own explanation, verbatim - empty on a clean pass.
    """

    success: bool
    output: str


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

    This is not the same duplicate ``delete_service`` used to be: it goes
    through :func:`wasm.validators.names.resolve_within`, which refuses a name
    that resolves through a symlink planted inside the unit directory to a
    file outside it. ``ServiceManager.inspect_unit`` does not perform that
    check - it trusts its own directory - so this stays as the request's own
    containment guard, independent of whatever manager ends up doing the
    write.

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


def _own_unit(*names: str) -> str | None:
    """
    Find which of the given names, if any, is one of WASM's own units.

    Args:
        names: Unit names without the ``.service`` suffix: the one the client
            spelled and the one it resolved to, which differ when ``web``
            resolves to a ``wasm-web.service`` on disk.

    Returns:
        The first of them that is WASM's own, or None.
    """
    return next((name for name in names if _is_own_unit_name(name)), None)


def _refuse_own_unit(*names: str) -> None:
    """
    Refuse a mutation of WASM's own units through the services API.

    The console and the monitor are managed units, so every services
    endpoint used to accept them: an admin token could stop ``wasm-web``
    without sudo mode and take the console down under its own operator. The
    units behind cron jobs and backup schedules have their own API, which
    keeps each one's timer and store row in step; acting on the unit alone
    leaves them out of step. Checked here, before the manager is built,
    because the manager itself must keep managing these units for the CLI.

    Args:
        names: Unit names without the ``.service`` suffix, as for
            :func:`_own_unit`.

    Raises:
        WASMPermissionError: 403, naming where the unit is managed instead.
    """
    own = _own_unit(*names)
    if own is None:
        return
    hint = _OWN_UNIT_HINTS.get(own) or next(
        (text for prefix, text in _OWN_UNIT_HINTS.items() if own.startswith(prefix)),
        "Manage it with the 'wasm' command that created it.",
    )
    raise WASMPermissionError(
        f"{own} is one of WASM's own units and cannot be changed through the services API",
        details=hint,
    )


def _requested_name(name: str) -> str:
    """
    Validate a client-supplied unit name and strip its ``.service`` suffix.

    Args:
        name: Service name as supplied by the client.

    Returns:
        The bare unit name.

    Raises:
        ValidationError: When the name is not a safe unit name.
    """
    return validate_service_name(name).removesuffix(".service")


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

    # An unset field means "use what the CLI uses", not root: the same
    # resolution the manager itself applies in create_service().
    user = data.user or Config().service_user
    if not USER_NAME_PATTERN.match(user):
        raise ValidationError(
            f"Invalid user: {user!r}",
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
User={user}
WorkingDirectory={working_directory}
ExecStart={command}
Restart={data.restart}
RestartSec=5
{env_section}
[Install]
WantedBy=multi-user.target
"""


def _service_info(name: str, description: str | None, live_status: dict) -> ServiceInfo:
    """
    Build the API model from a manager status dict.

    Args:
        name: Unit name, used when the status dict did not resolve one (it
            always does, but a scripted test double is not obliged to).
        description: What the store records as the unit's command, or None
            for a unit the store has never heard of.
        live_status: As :meth:`ServiceManager.get_status` returns it.

    Returns:
        The API representation.
    """
    # systemctl show reports MainPID as a string, "0" for a stopped unit and
    # "" from a status a scripted test double left blank; neither is a pid.
    raw_pid = live_status.get("pid")
    pid = int(raw_pid) if raw_pid is not None and raw_pid not in ("", "0") else None

    return ServiceInfo(
        name=live_status.get("name") or name,
        description=description,
        active=live_status.get("active", False),
        enabled=live_status.get("enabled", False),
        status="running" if live_status.get("active") else "stopped",
        pid=pid,
        uptime=live_status.get("uptime"),
        memory=live_status.get("memory"),
        managed=bool(live_status.get("managed", True)),
        active_state=live_status.get("active_state") or None,
        sub_state=live_status.get("sub_state") or None,
        result=live_status.get("result") or None,
    )


@router.get("", response_model=ServiceListResponse)
def list_services(
    request: Request,
    wasm_only: bool = Query(default=True, description="Only show WASM services"),
    session: dict = Depends(get_current_session),
):
    """
    List services.

    ``wasm_only`` (the default) lists the units WASM manages - the one
    definition in :meth:`~wasm.managers.service_manager.ServiceManager.managed_units`
    the console's top bar counts too, so the two always agree. Set it to false
    for a full inventory of every unit on the host, each flagged ``managed``,
    which is how a diagnostics view tells a foreign unit's own crash loop from
    one of WASM's own. A foreign unit carries only its state: it is listed
    from systemd's own listing, never probed or acted on.
    """
    statuses = ServiceManager(verbose=False).list_statuses(all_services=not wasm_only)
    commands = {service.name: service.command for service in get_store().list_services()}
    result = [
        _service_info(status["name"], commands.get(status["name"]), status) for status in statuses
    ]
    return ServiceListResponse(services=result, total=len(result))


@router.post("/verify", response_model=VerifyUnitResponse)
def verify_unit(
    data: VerifyUnitRequest, request: Request, session: dict = Depends(get_current_session)
) -> VerifyUnitResponse:
    """
    Check a candidate unit file with systemd-analyze, without saving it.

    Declared before ``/{name}`` on purpose: a parametrised route registered
    first would match ``verify`` as a service name. Used by the unit editor
    to catch a mistake before "save" ever reaches a real unit file.
    """
    success, output = ServiceManager(verbose=False).verify_unit(data.content)
    return VerifyUnitResponse(success=success, output=output)


@router.get("/{name}", response_model=ServiceInfo)
def get_service(name: str, request: Request, session: dict = Depends(get_current_session)):
    """
    Get details for a specific service.

    Answers for a unit WASM manages, whether or not the store's services
    table has a row for it (since 0.14.1 an application's unit is named after
    the application and may have none). Any other unit is a 404, which is how
    the console knows to describe it from the all-units listing instead;
    systemd's own escaped names (``systemd-fsck@dev-disk-by\\x2dlabel-BOOT``)
    arrive URL-encoded and get that 404, not a 400.
    """
    service_name = readable_unit_name(name)
    try:
        validate_service_name(service_name)
    except ValidationError as exc:
        # WASM never creates a name its own validator refuses, so this is a
        # unit systemd or a package named: not one of ours.
        raise HTTPException(status_code=404, detail=f"Service not found: {service_name}") from exc

    service_manager = ServiceManager(verbose=False)
    info = service_manager.inspect_unit(service_name)
    if not info.exists or not info.managed:
        raise HTTPException(status_code=404, detail=f"Service not found: {service_name}")

    stored = get_store().get_service(info.unit)
    live_status = service_manager.get_status(info.unit)
    return _service_info(info.unit, stored.command if stored else None, live_status)


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
        ValidationError: For an unsafe name.
        SecurityError: For a path that would leave the unit directory.
        WASMPermissionError: 403 for one of WASM's own units.
        HTTPException: 404 when the unit does not exist. A failure inside
            ``action`` propagates as whatever WASMError systemd's manager
            raised, mapped by :data:`~wasm.web.api.deps._STATUS_BY_ERROR`.
    """
    service_name, service_path = _resolve_unit(name)
    _refuse_own_unit(_requested_name(name), service_name)

    if not service_path.exists():
        raise HTTPException(status_code=404, detail=f"Service not found: {service_name}")

    service_manager = ServiceManager(verbose=False)
    getattr(service_manager, action)(service_name)

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


@router.get("/{name}/logs", response_model=ServiceLogsResponse)
def get_service_logs(
    name: str,
    request: Request,
    lines: int = Query(default=100, ge=1, le=1000),
    session: dict = Depends(get_current_session),
) -> ServiceLogsResponse:
    """
    Get service logs from journalctl.

    The console's and the monitor's journals need an admin credential, not
    the ``read`` a GET would otherwise ask for: the console logs every SQL
    statement run from it (``wasm.audit``), the paths and client addresses of
    every request, and the verbatim output of failed git, certbot and
    notification calls; the monitor logs what it saw of other processes.
    An application's journal is its own output and stays ``read``.
    """
    service_name, _ = _resolve_unit(name)
    if _own_unit(_requested_name(name), service_name) in OWN_JOURNAL_UNITS:
        ensure_scope(request, session, "admin")

    service_manager = ServiceManager(verbose=False)
    try:
        logs = service_manager.logs(service_name, lines=lines) or "No logs available"
    except WASMError as exc:
        logs = f"Error retrieving logs: {exc}"

    return ServiceLogsResponse(service=service_name, logs=logs, lines=lines)


@router.get("/{name}/config", response_model=ServiceConfigResponse)
def get_service_config(
    name: str, request: Request, session: dict = Depends(get_current_session)
) -> ServiceConfigResponse:
    """
    Get the systemd unit file content for a service.

    Simple-mode services inline their environment as ``Environment=`` lines,
    so the unit can carry secrets: reading it needs an admin credential, not
    the ``read`` a GET would otherwise ask for.
    """
    ensure_scope(request, session, "admin")
    service_name, service_path = _resolve_unit(name)

    if not service_path.is_file():
        raise HTTPException(status_code=404, detail=f"Service not found: {service_name}")

    try:
        content = service_path.read_text()
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Error reading config: {exc}") from exc

    return ServiceConfigResponse(service=service_name, config=content, path=str(service_path))


@router.put("/{name}/config", response_model=ServiceActionResponse)
def update_service_config(
    name: str,
    data: UpdateServiceConfigRequest,
    request: Request,
    session: dict = Depends(require_elevated),
):
    """
    Update the systemd unit file content for a service.
    """
    service_name, service_path = _resolve_unit(name)
    _refuse_own_unit(_requested_name(name), service_name)

    if not service_path.is_file():
        raise HTTPException(status_code=404, detail=f"Service not found: {service_name}")

    # Through the manager, never straight to disk. The manager is where unit
    # ownership is checked, and writing here would let the panel rewrite any
    # unit in /etc/systemd/system, which is the exact hole the ownership guard
    # exists to close.
    #
    # ServiceError is caught explicitly because a rewrite that drops the WASM
    # marker is a conflict with the unit's own management, not a malformed
    # request; WASMErrorRoute's default for an unmapped WASMError is 500,
    # which would be wrong here.
    try:
        ServiceManager(verbose=False).update_config(service_name, data.config)
    except ServiceError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    return ServiceActionResponse(
        success=True,
        message=f"Configuration updated for {service_name}. Restart the service to apply changes.",
        service=service_name,
    )


@router.post("", response_model=ServiceActionResponse)
def create_service(
    data: CreateServiceRequest, request: Request, session: dict = Depends(require_elevated)
):
    """
    Create a new systemd service.

    Sudo mode, for either form of the request - a raw unit or the fields
    that render one: a unit is a command systemd runs as root, restarted for
    as long as the machine is up, which is the same standing reach editing a
    unit by hand has.
    """
    # Creation goes through the manager so that the name, the environment and
    # the ownership rules are enforced in one place. Writing the file here,
    # which is what this endpoint used to do, meant raw_content could put any
    # content into any unit path as root while the manager's guard looked on.
    #
    # ServiceError is caught explicitly for the same reason as in
    # update_service_config: a name collision is a conflict, not the 500
    # WASMErrorRoute's default would answer for an unmapped WASMError.
    service_name = _requested_name(data.name)
    # A unit named after the console or the monitor would be the one
    # 'wasm web enable' or 'wasm monitor enable' then finds in their place.
    _refuse_own_unit(service_name)
    service_manager = ServiceManager(verbose=False)
    try:
        if data.raw_content is not None:
            service_manager.create_from_unit(service_name, data.raw_content)
        else:
            if data.restart not in VALID_RESTART_POLICIES:
                raise ValidationError(
                    f"Invalid restart policy: {data.restart!r}",
                    details=f"Use one of: {', '.join(sorted(VALID_RESTART_POLICIES))}.",
                )
            service_manager.create_service(
                name=service_name,
                command=data.command or "",
                working_directory=data.working_directory,
                user=data.user,
                environment=data.environment or {},
                restart=data.restart,
            )
    except ServiceError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    service_manager.enable(service_name)

    return ServiceActionResponse(
        success=True, message=f"Service created: {service_name}", service=service_name
    )


@router.delete("/{name}", response_model=ServiceActionResponse)
def delete_service(name: str, request: Request, session: dict = Depends(require_elevated)):
    """
    Delete a systemd service.
    """
    service_name, service_path = _resolve_unit(name)
    _refuse_own_unit(_requested_name(name), service_name)

    if not service_path.is_file():
        raise HTTPException(status_code=404, detail=f"Service not found: {service_name}")

    # Deletion goes through the manager, the one place that checks ownership,
    # unlinks through the filesystem seam (which --dry-run can refuse) and
    # removes the store row. This endpoint used to stop/disable the unit and
    # unlink the file itself, straight past the ownership guard and past the
    # store: a service deleted from the panel kept showing up in
    # 'wasm service list' until something else happened to notice the file
    # was gone.
    ServiceManager(verbose=False).delete_service(service_name)

    return ServiceActionResponse(
        success=True, message=f"Service deleted: {service_name}", service=service_name
    )
