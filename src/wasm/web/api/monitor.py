"""
Monitor API endpoints.

A thin translation of HTTP to :mod:`wasm.monitor`. Two things were wrong here
and both are fixed by that rule:

- The handlers built a ``MonitorConfig`` with ``auto_terminate``, ``use_ai`` and
  ``dry_run``, and read ``threat_level`` off the results. None of those exist
  since the monitor became observability, so every call returned 500. The
  vocabulary now matches the model: observations, not threats; acknowledged,
  not resolved.
- They were ``async def`` while calling ``systemctl`` and walking ``/proc``,
  which blocks the event loop for the whole panel. They are plain ``def``, so
  FastAPI runs them in its threadpool.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from wasm.core.config import Config
from wasm.core.exceptions import EmailError, MonitorError, WASMError
from wasm.monitor import (
    DEFAULT_CPU_SAMPLE_INTERVAL,
    DEFAULT_CPU_THRESHOLD,
    DEFAULT_MAX_OBSERVATIONS,
    DEFAULT_MEMORY_THRESHOLD,
    DEFAULT_RETENTION_DAYS,
    DEFAULT_SCAN_INTERVAL,
    MONITOR_SCOPE,
    EmailNotifier,
    ObservationStore,
    ProcessMonitor,
    ProcessObservation,
    collect_resource_metrics,
    list_processes,
)
from wasm.web.api.auth import get_current_session

logger = logging.getLogger(__name__)

router = APIRouter()

#: Upper bound on rows any list endpoint returns, so a caller cannot ask the
#: panel to serialise the whole observation store.
MAX_PAGE_SIZE = 500


class MonitorStatus(BaseModel):
    """
    State of the monitor systemd unit.

    Attributes:
        installed: Whether the unit file exists.
        enabled: Whether the unit starts at boot.
        active: Whether the unit is running now.
        pid: Main PID, when running.
        uptime: When the unit entered the active state.
        scope: What the monitor will not do, for the panel to display.
    """

    installed: bool
    enabled: bool
    active: bool
    pid: int | None = None
    uptime: str | None = None
    scope: list[str] = []


class MonitorSettings(BaseModel):
    """
    The monitor settings that exist.

    Attributes:
        scan_interval: Seconds between scans.
        cpu_threshold: CPU percentage above which a process is noted.
        memory_threshold: Memory percentage above which a process is noted.
        retention_days: How long observations are kept.
        max_observations: Row cap of the observation store.
        notify: Whether reports are emailed.
        watch_units: systemd units checked on each scan.
    """

    scan_interval: int
    cpu_threshold: float
    memory_threshold: float
    retention_days: int
    max_observations: int
    notify: bool
    watch_units: list[str]


class ProcessEntry(BaseModel):
    """
    One row of the process table.

    Attributes:
        pid: Process identifier.
        name: Executable name.
        user: Account the process runs as.
        cpu_percent: CPU usage percentage.
        memory_percent: Resident memory percentage.
        command: Command line, already truncated by the collector.
        status: Kernel process state.
    """

    pid: int
    name: str
    user: str
    cpu_percent: float
    memory_percent: float
    command: str = ""
    status: str = "running"


class ProcessListResponse(BaseModel):
    """
    Processes visible to the panel.

    Attributes:
        total: Number of processes seen.
        processes: The page requested.
    """

    total: int
    processes: list[ProcessEntry]


class ObservationEntry(BaseModel):
    """
    One thing the monitor wrote down.

    Attributes:
        id: Row identifier, absent for an observation not yet stored.
        observed_at: When it was noticed, ISO 8601.
        pid: Process identifier at the time.
        process_name: Executable name.
        user: Account the process ran as.
        cpu_percent: CPU usage percentage.
        memory_percent: Resident memory percentage.
        command: Command line, truncated.
        signal: Which check produced it.
        severity: notice or warning.
        detail: Human-readable explanation.
        acknowledged: Whether an operator dismissed it.
    """

    id: int | None = None
    observed_at: str
    pid: int
    process_name: str
    user: str | None = None
    cpu_percent: float | None = None
    memory_percent: float | None = None
    command: str | None = None
    signal: str
    severity: str
    detail: str | None = None
    acknowledged: bool = False


class ObservationListResponse(BaseModel):
    """
    A page of stored observations.

    Attributes:
        observations: The rows.
        count: How many rows are in this page.
        stats: Totals by severity and acknowledgement.
    """

    observations: list[ObservationEntry]
    count: int
    stats: dict[str, int] | None = None


class ScanResponse(BaseModel):
    """
    Result of a single scan.

    Attributes:
        scanned: How many processes were inspected.
        observations: What stood out, warnings first.
        count: Number of observations.
        scope: What the scan did not do.
    """

    scanned: int
    observations: list[ObservationEntry]
    count: int
    scope: list[str] = []


class DiskEntry(BaseModel):
    """
    Capacity of one filesystem.

    Attributes:
        mountpoint: Where it is mounted.
        device: Backing device.
        fstype: Filesystem type.
        total_bytes: Size.
        used_bytes: Bytes in use.
        free_bytes: Bytes available.
        percent: Percentage used.
    """

    mountpoint: str
    device: str
    fstype: str
    total_bytes: int
    used_bytes: int
    free_bytes: int
    percent: float


class MetricsResponse(BaseModel):
    """
    A point-in-time reading of machine resources.

    Attributes:
        collected_at: When the reading was taken, ISO 8601.
        cpu_percent: System-wide CPU usage.
        cpu_count: Logical CPU count.
        load_average: One, five and fifteen minute load averages.
        memory_total_bytes: Total RAM.
        memory_used_bytes: RAM in use.
        memory_percent: RAM usage percentage.
        swap_percent: Swap usage percentage.
        disks: Capacity of each real filesystem.
        net_bytes_sent: Bytes sent since boot.
        net_bytes_recv: Bytes received since boot.
        process_count: Number of processes.
        uptime_seconds: Seconds since boot.
    """

    collected_at: str
    cpu_percent: float
    cpu_count: int
    load_average: list[float]
    memory_total_bytes: int
    memory_used_bytes: int
    memory_percent: float
    swap_percent: float
    disks: list[DiskEntry]
    net_bytes_sent: int
    net_bytes_recv: int
    process_count: int
    uptime_seconds: float


#: The authentication dependency, as an annotated type. Declaring it here
#: rather than as a default value keeps the dependency out of the signature's
#: mutable defaults, which is the form FastAPI recommends.
Session = Annotated[dict, Depends(get_current_session)]


def _observation_entry(observation: ProcessObservation) -> ObservationEntry:
    """
    Convert an in-memory observation to its API shape.

    Args:
        observation: What a scan noticed.

    Returns:
        The serialisable entry.
    """
    process = observation.process
    return ObservationEntry(
        observed_at=observation.observed_at.isoformat(),
        pid=process.pid,
        process_name=process.name,
        user=process.user,
        cpu_percent=process.cpu_percent,
        memory_percent=process.memory_percent,
        command=process.command,
        signal=observation.signal,
        severity=observation.severity,
        detail=observation.detail,
    )


@router.get("/status", response_model=MonitorStatus)
def get_monitor_status(session: Session) -> MonitorStatus:
    """
    Report what systemd knows about the monitor unit.

    Args:
        session: Authenticated session, injected.

    Returns:
        The unit state, plus the scope note the panel displays.

    Raises:
        HTTPException: When systemd could not be queried.
    """
    try:
        status = ProcessMonitor(verbose=False).get_service_status()
    except WASMError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return MonitorStatus(
        installed=bool(status["installed"]),
        enabled=bool(status["enabled"]),
        active=bool(status["active"]),
        pid=status["pid"],
        uptime=status["uptime"],
        scope=list(MONITOR_SCOPE),
    )


@router.get("/config", response_model=MonitorSettings)
def get_monitor_config(session: Session) -> MonitorSettings:
    """
    Read the monitor settings.

    Args:
        session: Authenticated session, injected.

    Returns:
        The settings that exist, with defaults for anything unset.
    """
    config = Config()
    settings = config.get("monitor", {}) or {}

    return MonitorSettings(
        scan_interval=int(settings.get("scan_interval", DEFAULT_SCAN_INTERVAL)),
        cpu_threshold=float(settings.get("cpu_threshold", DEFAULT_CPU_THRESHOLD)),
        memory_threshold=float(settings.get("memory_threshold", DEFAULT_MEMORY_THRESHOLD)),
        retention_days=int(settings.get("retention_days", DEFAULT_RETENTION_DAYS)),
        max_observations=int(settings.get("max_observations", DEFAULT_MAX_OBSERVATIONS)),
        notify=bool(settings.get("notify", False)),
        watch_units=list(settings.get("watch_units", []) or []),
    )


@router.get("/metrics", response_model=MetricsResponse)
def get_metrics(session: Session) -> MetricsResponse:
    """
    Read CPU, memory, disk and network counters.

    Args:
        session: Authenticated session, injected.

    Returns:
        A single reading. Nothing is stored: the panel polls for the current
        value, and a time series would grow without bound.

    Raises:
        HTTPException: When psutil is not installed.
    """
    try:
        metrics = collect_resource_metrics()
    except MonitorError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    return MetricsResponse(
        collected_at=metrics.collected_at.isoformat(),
        cpu_percent=metrics.cpu_percent,
        cpu_count=metrics.cpu_count,
        load_average=list(metrics.load_average),
        memory_total_bytes=metrics.memory_total_bytes,
        memory_used_bytes=metrics.memory_used_bytes,
        memory_percent=metrics.memory_percent,
        swap_percent=metrics.swap_percent,
        disks=[
            DiskEntry(
                mountpoint=disk.mountpoint,
                device=disk.device,
                fstype=disk.fstype,
                total_bytes=disk.total_bytes,
                used_bytes=disk.used_bytes,
                free_bytes=disk.free_bytes,
                percent=disk.percent,
            )
            for disk in metrics.disks
        ],
        net_bytes_sent=metrics.net_bytes_sent,
        net_bytes_recv=metrics.net_bytes_recv,
        process_count=metrics.process_count,
        uptime_seconds=metrics.uptime_seconds,
    )


@router.get("/processes", response_model=ProcessListResponse)
def get_all_processes(
    session: Session,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = 100,
    sort_by: Annotated[str, Query(pattern="^(cpu|memory|name|pid)$")] = "cpu",
) -> ProcessListResponse:
    """
    List the processes on the machine.

    Args:
        limit: Maximum number of rows to return.
        sort_by: One of cpu, memory, name, pid.
        session: Authenticated session, injected.

    Returns:
        The requested page, sorted as asked.

    Raises:
        HTTPException: When psutil is not installed.
    """
    try:
        processes = list_processes()
    except MonitorError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    keys = {
        "cpu": (lambda p: -p.cpu_percent),
        "memory": (lambda p: -p.memory_percent),
        "name": (lambda p: p.name.lower()),
        "pid": (lambda p: p.pid),
    }
    ordered = sorted(processes, key=keys[sort_by])

    return ProcessListResponse(
        total=len(processes),
        processes=[
            ProcessEntry(
                pid=p.pid,
                name=p.name,
                user=p.user,
                cpu_percent=p.cpu_percent,
                memory_percent=p.memory_percent,
                command=p.command,
                status=p.status,
            )
            for p in ordered[:limit]
        ],
    )


@router.post("/scan", response_model=ScanResponse)
def run_scan(session: Session) -> ScanResponse:
    """
    Run one scan and return what stood out.

    There is no dry-run parameter because there is no other mode: a scan reads
    the process table and writes rows to the observation store.

    Args:
        session: Authenticated session, injected.

    Returns:
        The observations, warnings first.

    Raises:
        HTTPException: When the process table cannot be read.
    """
    monitor = ProcessMonitor(verbose=False)
    try:
        observations = monitor.scan_once(cpu_sample_interval=DEFAULT_CPU_SAMPLE_INTERVAL)
        scanned = len(list_processes())
    except MonitorError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    return ScanResponse(
        scanned=scanned,
        observations=[_observation_entry(o) for o in observations],
        count=len(observations),
        scope=list(MONITOR_SCOPE),
    )


@router.get("/observations", response_model=ObservationListResponse)
def get_observations(
    session: Session,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = 50,
    include_acknowledged: bool = False,
    severity: Annotated[str | None, Query(pattern="^(notice|warning)$")] = None,
) -> ObservationListResponse:
    """
    Read stored observations, newest first.

    Args:
        limit: Maximum number of rows to return.
        include_acknowledged: Include rows an operator already dismissed.
        severity: Restrict to notice or warning.
        session: Authenticated session, injected.

    Returns:
        The requested page and the store totals.

    Raises:
        HTTPException: When the store cannot be read.
    """
    try:
        store = ObservationStore(verbose=False)
        rows = store.recent(
            limit=limit,
            include_acknowledged=include_acknowledged,
            severity=severity,
        )
        stats = store.stats()
    except (WASMError, OSError) as exc:
        raise HTTPException(
            status_code=500, detail=f"Observation store unavailable: {exc}"
        ) from exc

    return ObservationListResponse(
        observations=[_row_to_entry(row) for row in rows],
        count=len(rows),
        stats=stats,
    )


def _row_to_entry(row: dict[str, Any]) -> ObservationEntry:
    """
    Convert a stored row to its API shape.

    Args:
        row: A row from the observation store.

    Returns:
        The serialisable entry.
    """
    return ObservationEntry(
        id=row.get("id"),
        observed_at=str(row.get("observed_at", "")),
        pid=int(row.get("pid", 0)),
        process_name=str(row.get("process_name", "")),
        user=row.get("user"),
        cpu_percent=row.get("cpu_percent"),
        memory_percent=row.get("memory_percent"),
        command=row.get("command"),
        signal=str(row.get("signal", "")),
        severity=str(row.get("severity", "")),
        detail=row.get("detail"),
        acknowledged=bool(row.get("acknowledged", 0)),
    )


@router.post("/observations/{observation_id}/acknowledge")
def acknowledge_observation(
    observation_id: int,
    session: Session,
) -> dict[str, Any]:
    """
    Mark an observation as seen.

    Acknowledging changes a flag in WASM's own database. It does nothing to the
    process the observation is about.

    Args:
        observation_id: Row identifier.
        session: Authenticated session, injected.

    Returns:
        A success payload.

    Raises:
        HTTPException: When the row does not exist or the store is unreadable.
    """
    try:
        acknowledged = ObservationStore(verbose=False).acknowledge(observation_id)
    except (WASMError, OSError) as exc:
        raise HTTPException(
            status_code=500, detail=f"Observation store unavailable: {exc}"
        ) from exc

    if not acknowledged:
        raise HTTPException(status_code=404, detail=f"Observation {observation_id} not found")

    return {"success": True, "message": f"Observation {observation_id} acknowledged"}


def _service_action(action: str) -> dict[str, Any]:
    """
    Run one systemd action against the monitor unit.

    Args:
        action: One of install, uninstall, enable, disable, start, stop.

    Returns:
        A success payload.

    Raises:
        HTTPException: When systemd or the filesystem refused.
    """
    monitor = ProcessMonitor(verbose=False)
    methods = {
        "install": monitor.install_service,
        "uninstall": monitor.uninstall_service,
        "enable": monitor.enable_service,
        "disable": monitor.disable_service,
        "start": monitor.start_service,
        "stop": monitor.stop_service,
    }

    try:
        methods[action]()
    except WASMError as exc:
        logger.error("Monitor service %s failed: %s", action, exc)
        detail = getattr(exc, "details", "") or ""
        raise HTTPException(
            status_code=500,
            detail=f"{exc}{f': {detail}' if detail else ''}",
        ) from exc

    return {"success": True, "message": f"Monitor service {action} completed"}


@router.post("/install")
def install_monitor(session: Session) -> dict[str, Any]:
    """
    Write the systemd unit.

    Args:
        session: Authenticated session, injected.

    Returns:
        A success payload.
    """
    return _service_action("install")


@router.post("/uninstall")
def uninstall_monitor(session: Session) -> dict[str, Any]:
    """
    Remove the systemd unit WASM wrote.

    Args:
        session: Authenticated session, injected.

    Returns:
        A success payload.
    """
    return _service_action("uninstall")


@router.post("/enable")
def enable_monitor(session: Session) -> dict[str, Any]:
    """
    Enable the unit and start it now.

    Args:
        session: Authenticated session, injected.

    Returns:
        A success payload.
    """
    return _service_action("enable")


@router.post("/disable")
def disable_monitor(session: Session) -> dict[str, Any]:
    """
    Disable the unit and stop it now.

    Args:
        session: Authenticated session, injected.

    Returns:
        A success payload.
    """
    return _service_action("disable")


@router.post("/start")
def start_monitor(session: Session) -> dict[str, Any]:
    """
    Start the unit without enabling it at boot.

    Args:
        session: Authenticated session, injected.

    Returns:
        A success payload.
    """
    return _service_action("start")


@router.post("/stop")
def stop_monitor(session: Session) -> dict[str, Any]:
    """
    Stop the unit without disabling it at boot.

    Args:
        session: Authenticated session, injected.

    Returns:
        A success payload.
    """
    return _service_action("stop")


@router.post("/test-email")
def test_email(session: Session) -> dict[str, Any]:
    """
    Send a test message through the configured SMTP relay.

    Args:
        session: Authenticated session, injected.

    Returns:
        A success payload.

    Raises:
        HTTPException: When SMTP is not configured or delivery failed.
    """
    notifier = EmailNotifier(verbose=False)

    if not notifier.smtp_config.host:
        raise HTTPException(
            status_code=400,
            detail="SMTP is not configured. Set monitor.smtp.host in Settings.",
        )
    if not notifier.recipients:
        raise HTTPException(
            status_code=400,
            detail="No recipients configured. Set monitor.email_recipients in Settings.",
        )

    try:
        notifier.send_test_email()
    except EmailError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    return {"success": True, "message": "Test email sent"}
