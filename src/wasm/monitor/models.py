"""
Data carried by the monitor.

Everything here is a plain, frozen record. The monitor observes a machine and
writes down what it saw; nothing in this module authorises an action, and the
vocabulary is deliberate: a process is *observed* or *notable*, never
"neutralised". Naming a report after an action the tool does not take is how
the previous version ended up terminating processes and deleting directories.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

#: Signal kinds. A signal says why a process was written down, nothing else.
#: There is deliberately no command-line signal: an unprivileged user chooses
#: their own argv, so anything derived from it is an attacker-controlled input
#: to a daemon that runs as root.
SIGNAL_NAME_PATTERN = "name-pattern"
SIGNAL_RESOURCE_USAGE = "resource-usage"

#: Severities. Both mean "a human should look", they differ only in ordering.
SEVERITY_NOTICE = "notice"
SEVERITY_WARNING = "warning"


@dataclass(frozen=True)
class ProcessInfo:
    """
    A snapshot of one running process.

    Attributes:
        pid: Process identifier at the time of the snapshot.
        name: Executable name as reported by the kernel.
        user: Account the process runs as.
        cpu_percent: CPU usage percentage.
        memory_percent: Resident memory as a percentage of total RAM.
        command: Full command line, joined with spaces.
        status: Kernel process state, such as running or sleeping.
        num_threads: Thread count.
        parent_pid: Parent process identifier, when known.
        parent_name: Parent executable name, when it could be resolved.
        create_time: Process start time as a UNIX timestamp.
        cwd: Working directory, when readable.
    """

    pid: int
    name: str
    user: str = ""
    cpu_percent: float = 0.0
    memory_percent: float = 0.0
    command: str = ""
    status: str = "running"
    num_threads: int = 1
    parent_pid: int | None = None
    parent_name: str | None = None
    create_time: float | None = None
    cwd: str = ""


@dataclass(frozen=True)
class ProcessObservation:
    """
    A process singled out for a human to look at.

    This is a note in a logbook. It carries no recommended action and triggers
    none: the monitor never signals, terminates or deletes anything.

    Attributes:
        process: The process as it looked when observed.
        signal: Which check wrote this down, one of the SIGNAL_ constants.
        severity: SEVERITY_NOTICE or SEVERITY_WARNING.
        detail: Human-readable explanation of what stood out.
        observed_at: When the observation was made.
    """

    process: ProcessInfo
    signal: str
    severity: str
    detail: str
    observed_at: datetime = field(default_factory=datetime.now)


@dataclass(frozen=True)
class DiskUsage:
    """
    Capacity of one mounted filesystem.

    Attributes:
        device: Backing device.
        mountpoint: Where the filesystem is mounted.
        fstype: Filesystem type.
        total_bytes: Size of the filesystem.
        used_bytes: Bytes in use.
        free_bytes: Bytes available.
        percent: Percentage used.
    """

    device: str
    mountpoint: str
    fstype: str
    total_bytes: int
    used_bytes: int
    free_bytes: int
    percent: float


@dataclass(frozen=True)
class ResourceMetrics:
    """
    A point-in-time reading of machine resources.

    Attributes:
        collected_at: When the reading was taken.
        cpu_percent: System-wide CPU usage.
        cpu_count: Logical CPU count.
        load_average: One, five and fifteen minute load averages.
        memory_total_bytes: Total RAM.
        memory_used_bytes: RAM in use.
        memory_available_bytes: RAM available to new allocations.
        memory_percent: RAM usage percentage.
        swap_total_bytes: Total swap.
        swap_used_bytes: Swap in use.
        swap_percent: Swap usage percentage.
        disks: Usage of each real filesystem.
        net_bytes_sent: Bytes sent since boot.
        net_bytes_recv: Bytes received since boot.
        process_count: Number of processes on the machine.
        uptime_seconds: Seconds since boot.
    """

    collected_at: datetime
    cpu_percent: float
    cpu_count: int
    load_average: tuple[float, float, float]
    memory_total_bytes: int
    memory_used_bytes: int
    memory_available_bytes: int
    memory_percent: float
    swap_total_bytes: int
    swap_used_bytes: int
    swap_percent: float
    disks: tuple[DiskUsage, ...]
    net_bytes_sent: int
    net_bytes_recv: int
    process_count: int
    uptime_seconds: float


@dataclass(frozen=True)
class ServiceHealth:
    """
    State of one systemd unit.

    Attributes:
        unit: Unit name, without the .service suffix.
        active: True when systemd reports the unit as active.
        enabled: True when the unit starts at boot.
        active_state: Raw output of ``systemctl is-active``.
        enabled_state: Raw output of ``systemctl is-enabled``.
    """

    unit: str
    active: bool
    enabled: bool
    active_state: str = ""
    enabled_state: str = ""
