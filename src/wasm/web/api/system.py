"""
System API endpoints.

Observability only. This module reports what the machine is doing; it does not
act on it. The previous version exposed ``POST /system/processes/{pid}/kill``,
which let anyone with a panel session send SIGTERM or SIGKILL to **any** pid on
the host as root - including sshd, the database and pid 1. Decision D5 of the
v1 design takes "acting on processes" out of the product: the monitor is
observability, not an antivirus, so the endpoint is gone rather than merely
restricted. Stopping something WASM manages is done through its service, which
is what ``/api/services/{name}/stop`` is for.

Handlers are synchronous: ``psutil.cpu_percent(interval=...)`` and
``disk_usage`` block, and on the event loop they would stall every other client
for the duration.
"""

from __future__ import annotations

import os
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from wasm import __version__
from wasm.core.exceptions import DependencyError
from wasm.web.api.auth import get_current_session
from wasm.web.api.deps import WASMErrorRoute

router = APIRouter(route_class=WASMErrorRoute)

#: Sort keys the process listing accepts.
PROCESS_SORT_KEYS = frozenset({"cpu", "memory", "pid", "name"})

#: Bytes in a gibibyte, used for every size reported by this module.
_GIB = 1024**3


class DiskInfo(BaseModel):
    """Disk usage of one mounted filesystem."""

    device: str
    mount_point: str
    total_gb: float
    used_gb: float
    free_gb: float
    percent_used: float


class MemoryInfo(BaseModel):
    """Memory and swap usage."""

    total_gb: float
    used_gb: float
    free_gb: float
    available_gb: float
    percent_used: float
    swap_total_gb: float
    swap_used_gb: float
    swap_percent: float


class CpuInfo(BaseModel):
    """CPU count, utilisation and load average."""

    cores: int
    percent: float
    load_1min: float
    load_5min: float
    load_15min: float


class SystemInfo(BaseModel):
    """Everything the dashboard shows about the host."""

    hostname: str
    os: str
    kernel: str
    uptime: str
    cpu: CpuInfo
    memory: MemoryInfo
    disks: list[DiskInfo]


class ProcessInfo(BaseModel):
    """One process, as observed."""

    pid: int
    name: str
    cpu_percent: float
    memory_percent: float
    memory_mb: float
    status: str
    user: str
    command: str | None = None


class ProcessListResponse(BaseModel):
    """Response for the process listing."""

    processes: list[ProcessInfo]
    total: int


class InterfaceAddress(BaseModel):
    """One address configured on an interface."""

    type: str
    address: str
    netmask: str | None = None


class InterfaceInfo(BaseModel):
    """One network interface and its counters."""

    name: str
    addresses: list[InterfaceAddress] = Field(default_factory=list)
    is_up: bool = False
    speed_mbps: int = 0
    bytes_sent: int = 0
    bytes_recv: int = 0
    packets_sent: int = 0
    packets_recv: int = 0


class NetworkResponse(BaseModel):
    """Response for the network listing."""

    interfaces: list[InterfaceInfo]


class UpdateInfo(BaseModel):
    """Installed version and, when known, the released one."""

    current_version: str
    latest_version: str | None = None
    has_update: bool
    update_command: str | None = None
    release_url: str | None = None


def _psutil() -> Any:
    """
    Import psutil, turning its absence into an actionable error.

    Returns:
        The psutil module.

    Raises:
        DependencyError: When psutil is not installed.
    """
    try:
        import psutil
    except ImportError as exc:
        raise DependencyError(
            "psutil is not installed, so system metrics are unavailable",
            details="Install the web extra: pip install 'wasm-cli[web]'.",
        ) from exc
    return psutil


def _uptime() -> str:
    """
    Read the host uptime.

    Returns:
        Uptime as ``1d 2h 3m``, or ``unknown`` when /proc is unreadable.
    """
    try:
        with open("/proc/uptime") as handle:
            seconds = float(handle.read().split()[0])
    except (OSError, ValueError, IndexError):
        return "unknown"

    days = int(seconds // 86400)
    hours = int((seconds % 86400) // 3600)
    minutes = int((seconds % 3600) // 60)

    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    parts.append(f"{minutes}m")
    return " ".join(parts)


def _os_name() -> str:
    """
    Read the distribution name.

    Returns:
        The pretty name from /etc/os-release, or ``Linux``.
    """
    try:
        with open("/etc/os-release") as handle:
            for line in handle:
                key, _, value = line.strip().partition("=")
                if key == "PRETTY_NAME":
                    return value.strip('"')
    except OSError:
        pass
    return "Linux"


def _cpu_info(interval: float) -> CpuInfo:
    """
    Sample CPU utilisation.

    Args:
        interval: Sampling window in seconds. A longer window is more accurate
            and blocks the calling thread for that long, which is why these
            handlers run in the threadpool.

    Returns:
        The CPU description.
    """
    psutil = _psutil()
    load_1, load_5, load_15 = os.getloadavg()
    return CpuInfo(
        cores=psutil.cpu_count() or 1,
        percent=psutil.cpu_percent(interval=interval),
        load_1min=load_1,
        load_5min=load_5,
        load_15min=load_15,
    )


def _memory_info() -> MemoryInfo:
    """
    Sample memory and swap usage.

    Returns:
        The memory description.
    """
    psutil = _psutil()
    mem = psutil.virtual_memory()
    swap = psutil.swap_memory()
    return MemoryInfo(
        total_gb=round(mem.total / _GIB, 2),
        used_gb=round(mem.used / _GIB, 2),
        free_gb=round(mem.free / _GIB, 2),
        available_gb=round(mem.available / _GIB, 2),
        percent_used=mem.percent,
        swap_total_gb=round(swap.total / _GIB, 2),
        swap_used_gb=round(swap.used / _GIB, 2),
        swap_percent=swap.percent,
    )


def _disk_info() -> list[DiskInfo]:
    """
    Sample usage of every mounted filesystem that can be read.

    Returns:
        One entry per readable mount point.
    """
    psutil = _psutil()
    disks: list[DiskInfo] = []
    for partition in psutil.disk_partitions():
        try:
            usage = psutil.disk_usage(partition.mountpoint)
        except (PermissionError, OSError):
            # A mount point the panel cannot stat is reported by omission; it
            # is not an error worth failing the whole dashboard for.
            continue
        disks.append(
            DiskInfo(
                device=partition.device,
                mount_point=partition.mountpoint,
                total_gb=round(usage.total / _GIB, 2),
                used_gb=round(usage.used / _GIB, 2),
                free_gb=round(usage.free / _GIB, 2),
                percent_used=usage.percent,
            )
        )
    return disks


@router.get("", response_model=SystemInfo)
def get_system_info(session: Annotated[dict, Depends(get_current_session)]) -> SystemInfo:
    """
    Describe the host.

    Args:
        session: The authenticated session.

    Returns:
        Hostname, kernel, uptime, CPU, memory and disks.
    """
    uname = os.uname()
    return SystemInfo(
        hostname=uname.nodename,
        os=_os_name(),
        kernel=uname.release,
        uptime=_uptime(),
        cpu=_cpu_info(0.1),
        memory=_memory_info(),
        disks=_disk_info(),
    )


@router.get("/cpu", response_model=CpuInfo)
def get_cpu_info(session: Annotated[dict, Depends(get_current_session)]) -> CpuInfo:
    """
    Sample CPU usage over half a second.

    Args:
        session: The authenticated session.

    Returns:
        The CPU description.
    """
    return _cpu_info(0.5)


@router.get("/memory", response_model=MemoryInfo)
def get_memory_info(session: Annotated[dict, Depends(get_current_session)]) -> MemoryInfo:
    """
    Report memory and swap usage.

    Args:
        session: The authenticated session.

    Returns:
        The memory description.
    """
    return _memory_info()


@router.get("/disks", response_model=list[DiskInfo])
def get_disk_info(session: Annotated[dict, Depends(get_current_session)]) -> list[DiskInfo]:
    """
    Report disk usage per mount point.

    Args:
        session: The authenticated session.

    Returns:
        One entry per readable mount point.
    """
    return _disk_info()


@router.get("/processes", response_model=ProcessListResponse)
def get_processes(
    session: Annotated[dict, Depends(get_current_session)],
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
    sort_by: Annotated[str, Query()] = "cpu",
) -> ProcessListResponse:
    """
    List running processes.

    This is a read-only view. There is deliberately no endpoint that signals a
    process; see the module docstring.

    Args:
        limit: How many processes to return after sorting.
        sort_by: One of ``cpu``, ``memory``, ``pid`` or ``name``. An unknown
            key falls back to ``cpu``.
        session: The authenticated session.

    Returns:
        The processes, sorted and truncated.
    """
    psutil = _psutil()

    processes: list[ProcessInfo] = []
    fields = [
        "pid",
        "name",
        "cpu_percent",
        "memory_percent",
        "memory_info",
        "status",
        "username",
        "cmdline",
    ]
    for proc in psutil.process_iter(fields):
        try:
            info = proc.info
            memory_info = info.get("memory_info")
            cmdline = info.get("cmdline") or []
            processes.append(
                ProcessInfo(
                    pid=info["pid"],
                    name=info.get("name") or "",
                    cpu_percent=info.get("cpu_percent") or 0.0,
                    memory_percent=info.get("memory_percent") or 0.0,
                    memory_mb=round((memory_info.rss if memory_info else 0) / (1024**2), 2),
                    status=info.get("status") or "unknown",
                    user=info.get("username") or "unknown",
                    command=" ".join(cmdline[:5]) or None,
                )
            )
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            # The process list is a snapshot of a moving target; one entry that
            # vanished mid-iteration is normal.
            continue

    key = sort_by if sort_by in PROCESS_SORT_KEYS else "cpu"
    if key == "pid":
        processes.sort(key=lambda p: p.pid)
    elif key == "name":
        processes.sort(key=lambda p: p.name)
    elif key == "memory":
        processes.sort(key=lambda p: p.memory_percent, reverse=True)
    else:
        processes.sort(key=lambda p: p.cpu_percent, reverse=True)

    return ProcessListResponse(processes=processes[:limit], total=len(processes))


@router.get("/network", response_model=NetworkResponse)
def get_network_info(session: Annotated[dict, Depends(get_current_session)]) -> NetworkResponse:
    """
    Describe the network interfaces and their counters.

    Args:
        session: The authenticated session.

    Returns:
        One entry per interface.
    """
    psutil = _psutil()

    stats = psutil.net_if_stats()
    counters = psutil.net_io_counters(pernic=True)

    interfaces: list[InterfaceInfo] = []
    for name, addresses in psutil.net_if_addrs().items():
        stat = stats.get(name)
        io = counters.get(name)

        entries: list[InterfaceAddress] = []
        for addr in addresses:
            if addr.family.name == "AF_INET":
                entries.append(
                    InterfaceAddress(type="IPv4", address=addr.address, netmask=addr.netmask)
                )
            elif addr.family.name == "AF_INET6":
                entries.append(InterfaceAddress(type="IPv6", address=addr.address))

        interfaces.append(
            InterfaceInfo(
                name=name,
                addresses=entries,
                is_up=bool(stat.isup) if stat else False,
                speed_mbps=int(stat.speed) if stat else 0,
                bytes_sent=io.bytes_sent if io else 0,
                bytes_recv=io.bytes_recv if io else 0,
                packets_sent=io.packets_sent if io else 0,
                packets_recv=io.packets_recv if io else 0,
            )
        )

    return NetworkResponse(interfaces=interfaces)


@router.get("/version", response_model=UpdateInfo)
def check_version(session: Annotated[dict, Depends(get_current_session)]) -> UpdateInfo:
    """
    Report the installed version and, when known, the released one.

    Args:
        session: The authenticated session.

    Returns:
        The version comparison and how to update.
    """
    import time

    from wasm.core.update_checker import UpdateChecker

    cached = UpdateChecker._read_cache()

    if not UpdateChecker._is_cache_valid():
        latest = UpdateChecker._fetch_latest_version()
        if latest:
            has_update = UpdateChecker._is_newer_version(latest, __version__)
            UpdateChecker._write_cache(
                {
                    "latest_version": latest,
                    "has_update": has_update,
                    "checked_at": time.time(),
                }
            )
            cached = {"latest_version": latest, "has_update": has_update}

    latest_version = cached.get("latest_version") if cached else None
    has_update = bool(cached.get("has_update", False)) if cached else False

    update_command = None
    if has_update:
        update_command = UpdateChecker._get_update_command(
            UpdateChecker._detect_installation_method()
        )

    release_url = (
        f"https://github.com/Perkybeet/wasm/releases/tag/v{latest_version}"
        if latest_version and has_update
        else None
    )

    return UpdateInfo(
        current_version=__version__,
        latest_version=latest_version,
        has_update=has_update,
        update_command=update_command,
        release_url=release_url,
    )
