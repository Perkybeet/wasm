"""
Process and resource observability for WASM.

**What it measures.** CPU, memory, swap, load average, per-filesystem capacity,
network counters and uptime for the machine; the process table, of which it
writes down only processes over a resource threshold or carrying a known
malware executable name; and the ``systemctl is-active``/``is-enabled`` state
of the units listed in ``monitor.watch_units``.

**How often.** Once every ``monitor.scan_interval`` seconds, at least
:data:`MIN_SCAN_INTERVAL`, 60 by default. Resource metrics are read live and
never stored; only observations are persisted.

**Where it keeps it.** One SQLite file, ``/var/lib/wasm/observations.db``
(``~/.local/share/wasm/observations.db`` for an unprivileged run). It is
bounded three ways: repeats inside an hour collapse into one row, rows past
``monitor.retention_days`` are deleted, and the row count is capped at
:data:`DEFAULT_MAX_OBSERVATIONS`.

**What it does not do.** Listed in :data:`MONITOR_SCOPE`, and enforced by tests
that read the package source: no process is signalled or terminated, no file
outside its own systemd unit is written or deleted, nothing about the machine
is sent to a third party, and no check is driven by a process command line.
"""

from wasm.monitor.email_notifier import DEFAULT_SMTP_TIMEOUT, EmailNotifier, SMTPConfig
from wasm.monitor.metrics import (
    DEFAULT_CPU_SAMPLE_INTERVAL,
    MAX_COMMAND_LENGTH,
    collect_resource_metrics,
    collect_service_health,
    list_processes,
)
from wasm.monitor.models import (
    SEVERITY_NOTICE,
    SEVERITY_WARNING,
    SIGNAL_NAME_PATTERN,
    SIGNAL_RESOURCE_USAGE,
    DiskUsage,
    ProcessInfo,
    ProcessObservation,
    ResourceMetrics,
    ServiceHealth,
)
from wasm.monitor.observation_store import (
    DEFAULT_DEDUPE_WINDOW_SECONDS,
    DEFAULT_MAX_OBSERVATIONS,
    ObservationStore,
    default_db_path,
)
from wasm.monitor.process_monitor import (
    DEFAULT_CPU_THRESHOLD,
    DEFAULT_MEMORY_THRESHOLD,
    DEFAULT_RETENTION_DAYS,
    DEFAULT_SCAN_INTERVAL,
    MIN_SCAN_INTERVAL,
    MONITOR_SCOPE,
    MonitorConfig,
    ProcessMonitor,
)
from wasm.monitor.signals import is_known_safe, observe_process, observe_processes

__all__ = [
    "DEFAULT_CPU_SAMPLE_INTERVAL",
    "DEFAULT_CPU_THRESHOLD",
    "DEFAULT_DEDUPE_WINDOW_SECONDS",
    "DEFAULT_MAX_OBSERVATIONS",
    "DEFAULT_MEMORY_THRESHOLD",
    "DEFAULT_RETENTION_DAYS",
    "DEFAULT_SCAN_INTERVAL",
    "DEFAULT_SMTP_TIMEOUT",
    "MAX_COMMAND_LENGTH",
    "MIN_SCAN_INTERVAL",
    "MONITOR_SCOPE",
    "SEVERITY_NOTICE",
    "SEVERITY_WARNING",
    "SIGNAL_NAME_PATTERN",
    "SIGNAL_RESOURCE_USAGE",
    "DiskUsage",
    "EmailNotifier",
    "MonitorConfig",
    "ObservationStore",
    "ProcessInfo",
    "ProcessMonitor",
    "ProcessObservation",
    "ResourceMetrics",
    "SMTPConfig",
    "ServiceHealth",
    "collect_resource_metrics",
    "collect_service_health",
    "default_db_path",
    "is_known_safe",
    "list_processes",
    "observe_process",
    "observe_processes",
]
