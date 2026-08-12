"""
Collection of the facts the monitor reports: resources, processes, services.

This is the half of the old monitor that earned its keep. It reads, it does not
write: nothing in this module changes the state of the machine. Process and
resource data comes from psutil; unit state comes from ``systemctl`` through the
:class:`~wasm.core.runner.CommandRunner`, which is the only place in WASM
allowed to execute an external program.
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from wasm.core.exceptions import MonitorError
from wasm.core.runner import CommandRunner, get_runner
from wasm.monitor.models import DiskUsage, ProcessInfo, ResourceMetrics, ServiceHealth

try:
    import psutil
except ImportError:  # pragma: no cover - exercised only where psutil is absent
    psutil = None

#: Attributes fetched in a single pass over /proc. Anything not listed here
#: would cost an extra syscall per process, per scan.
PROCESS_ATTRS = (
    "pid",
    "name",
    "username",
    "cpu_percent",
    "memory_percent",
    "cmdline",
    "create_time",
    "ppid",
    "status",
    "num_threads",
    "cwd",
)

#: Kernel bookkeeping mounts. Reporting their "capacity" is noise.
PSEUDO_FILESYSTEMS = frozenset(
    {
        "autofs",
        "binfmt_misc",
        "bpf",
        "cgroup",
        "cgroup2",
        "configfs",
        "debugfs",
        "devpts",
        "devtmpfs",
        "efivarfs",
        "fusectl",
        "hugetlbfs",
        "mqueue",
        "proc",
        "pstore",
        "securityfs",
        "squashfs",
        "sysfs",
        "tracefs",
    }
)

#: systemctl answers immediately or it is broken; a long deadline only hides that.
SYSTEMCTL_TIMEOUT = 15

#: Command lines are truncated here, once, at the boundary where they enter the
#: program. A local user can give a process a 100 KB argv; kept whole, a scan
#: per minute would write that to the observation database forever. This is
#: enough to identify a process and bounded enough to store.
MAX_COMMAND_LENGTH = 512

#: Marker appended to a command line that was cut, so a reader knows it was.
TRUNCATION_MARKER = "..."

#: Seconds between the two passes of a one-shot CPU sample. psutil reports CPU
#: usage as a delta between reads, so the first read of a fresh process is
#: always 0.0: a single scan without this window reports an idle machine.
DEFAULT_CPU_SAMPLE_INTERVAL = 0.5


def _require_psutil() -> Any:
    """
    Return the psutil module, or explain how to get it.

    Returns:
        The imported psutil module.

    Raises:
        MonitorError: When psutil is not installed.
    """
    if psutil is None:
        raise MonitorError(
            "psutil is required to collect monitor metrics",
            details="Install it with: pip install 'wasm-cli[monitor]' (or python3-psutil)",
        )
    return psutil


def _truncate_command(cmdline: Sequence[str], fallback: str) -> str:
    """
    Join a command line, stopping once it is long enough to identify a process.

    Args:
        cmdline: Arguments as psutil reported them.
        fallback: Value to use when the command line is empty, normally the
            executable name.

    Returns:
        The joined command line, never longer than MAX_COMMAND_LENGTH.
    """
    if not cmdline:
        return fallback[:MAX_COMMAND_LENGTH]

    parts: list[str] = []
    length = 0
    for argument in cmdline:
        parts.append(argument)
        # +1 for the separating space; stop as soon as the budget is spent so a
        # 20.000-argument argv is never fully joined into memory.
        length += len(argument) + 1
        if length > MAX_COMMAND_LENGTH:
            break

    joined = " ".join(parts)
    if len(joined) <= MAX_COMMAND_LENGTH:
        return joined
    return joined[: MAX_COMMAND_LENGTH - len(TRUNCATION_MARKER)] + TRUNCATION_MARKER


def list_processes(cpu_sample_interval: float = 0.0) -> list[ProcessInfo]:
    """
    Take a snapshot of every process the current user can see.

    Args:
        cpu_sample_interval: Seconds to wait between priming psutil's CPU
            counters and reading them. Zero for a long-running loop, where the
            previous scan is the reference point; a fraction of a second for a
            one-shot scan, which would otherwise report 0% for everything.

    Returns:
        One ProcessInfo per process, in the order psutil reported them.

    Raises:
        MonitorError: When psutil is not installed.
    """
    ps = _require_psutil()

    if cpu_sample_interval > 0:
        # Priming pass: psutil caches the CPU times of each process, so the
        # second read below is a real delta over this window.
        for proc in ps.process_iter(["cpu_percent"]):
            try:
                proc.info  # noqa: B018 - touching info is what fills the cache
            except (ps.NoSuchProcess, ps.AccessDenied, ps.ZombieProcess):
                continue
        time.sleep(cpu_sample_interval)

    raw: list[dict[str, Any]] = []
    for proc in ps.process_iter(list(PROCESS_ATTRS)):
        try:
            raw.append(dict(proc.info))
        except (ps.NoSuchProcess, ps.AccessDenied, ps.ZombieProcess):
            # A process that exited mid-scan is not an error, it is Tuesday.
            continue

    names_by_pid = {entry.get("pid"): entry.get("name") or "" for entry in raw}

    processes: list[ProcessInfo] = []
    for entry in raw:
        cmdline = entry.get("cmdline") or []
        parent_pid = entry.get("ppid") or None
        processes.append(
            ProcessInfo(
                pid=entry.get("pid") or 0,
                name=entry.get("name") or "",
                user=entry.get("username") or "",
                cpu_percent=entry.get("cpu_percent") or 0.0,
                memory_percent=entry.get("memory_percent") or 0.0,
                command=_truncate_command(cmdline, entry.get("name") or ""),
                status=entry.get("status") or "unknown",
                num_threads=entry.get("num_threads") or 1,
                parent_pid=parent_pid,
                parent_name=names_by_pid.get(parent_pid) if parent_pid else None,
                create_time=entry.get("create_time"),
                cwd=entry.get("cwd") or "",
            )
        )

    return processes


def _is_read_only(partition: Any) -> bool:
    """
    Report whether a mount is read-only.

    Args:
        partition: A psutil partition record.

    Returns:
        True when the mount options say read-only.
    """
    options = str(getattr(partition, "opts", "") or "")
    return "ro" in options.split(",")


def _collect_disks(ps: Any) -> tuple[DiskUsage, ...]:
    """
    Read capacity for every real, writable filesystem.

    Two filters keep the report meaningful on a normal machine. Read-only
    mounts are skipped: every snap is a squashfs sitting at 100% by
    construction, and a full filesystem nobody can write to is not a capacity
    problem. Repeated devices are skipped as well, because bind mounts and
    containers can mount the same filesystem dozens of times, and each one
    costs a statvfs on every scan.

    Args:
        ps: The psutil module.

    Returns:
        Usage of each mounted filesystem that reports meaningful capacity.
    """
    disks: list[DiskUsage] = []
    seen_devices: set[str] = set()
    try:
        partitions = ps.disk_partitions(all=False)
    except (OSError, PermissionError):
        return ()

    for partition in partitions:
        if partition.fstype in PSEUDO_FILESYSTEMS or _is_read_only(partition):
            continue
        if partition.device in seen_devices:
            continue
        seen_devices.add(partition.device)
        try:
            usage = ps.disk_usage(partition.mountpoint)
        except (OSError, PermissionError):
            # Empty optical drives and unreadable mounts must not sink a scan.
            continue
        disks.append(
            DiskUsage(
                device=partition.device,
                mountpoint=partition.mountpoint,
                fstype=partition.fstype,
                total_bytes=usage.total,
                used_bytes=usage.used,
                free_bytes=usage.free,
                percent=usage.percent,
            )
        )

    return tuple(disks)


def collect_resource_metrics() -> ResourceMetrics:
    """
    Read CPU, memory, disk and network counters for the machine.

    Returns:
        A single point-in-time reading.

    Raises:
        MonitorError: When psutil is not installed.
    """
    ps = _require_psutil()

    memory = ps.virtual_memory()
    swap = ps.swap_memory()

    try:
        load_average = tuple(float(v) for v in ps.getloadavg())
    except (OSError, AttributeError):
        load_average = (0.0, 0.0, 0.0)

    try:
        net = ps.net_io_counters()
        net_sent, net_recv = int(net.bytes_sent), int(net.bytes_recv)
    except (OSError, AttributeError):
        net_sent, net_recv = 0, 0

    boot_time = float(ps.boot_time())

    return ResourceMetrics(
        collected_at=datetime.now(),
        cpu_percent=float(ps.cpu_percent(interval=None)),
        cpu_count=int(ps.cpu_count() or 1),
        load_average=(load_average[0], load_average[1], load_average[2]),
        memory_total_bytes=int(memory.total),
        memory_used_bytes=int(memory.used),
        memory_available_bytes=int(getattr(memory, "available", 0)),
        memory_percent=float(memory.percent),
        swap_total_bytes=int(swap.total),
        swap_used_bytes=int(swap.used),
        swap_percent=float(swap.percent),
        disks=_collect_disks(ps),
        net_bytes_sent=net_sent,
        net_bytes_recv=net_recv,
        process_count=len(ps.pids()),
        uptime_seconds=max(0.0, time.time() - boot_time),
    )


def collect_service_health(
    units: Sequence[str],
    runner: CommandRunner | None = None,
) -> list[ServiceHealth]:
    """
    Ask systemd about a set of units.

    Args:
        units: Unit names, with or without the .service suffix.
        runner: Command runner to use. Defaults to the process-wide one.

    Returns:
        One ServiceHealth per requested unit, in the order given.
    """
    command_runner = runner or get_runner()

    health: list[ServiceHealth] = []
    for unit in units:
        active = command_runner.run(
            ["systemctl", "is-active", unit],
            timeout=SYSTEMCTL_TIMEOUT,
        )
        enabled = command_runner.run(
            ["systemctl", "is-enabled", unit],
            timeout=SYSTEMCTL_TIMEOUT,
        )
        active_state = active.output
        enabled_state = enabled.output
        health.append(
            ServiceHealth(
                unit=unit,
                active=active_state in ("active", "activating"),
                enabled=enabled_state in ("enabled", "enabled-runtime", "static"),
                active_state=active_state,
                enabled_state=enabled_state,
            )
        )

    return health
