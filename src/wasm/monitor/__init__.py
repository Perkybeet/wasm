"""
Process and resource monitoring for WASM.

The monitor observes: it reads resource metrics, the process table and systemd
unit health, records what stands out, and can email a report. It does not
terminate processes, delete files, or send anything about the machine to a
third party.
"""

from wasm.monitor.email_notifier import DEFAULT_SMTP_TIMEOUT, EmailNotifier, SMTPConfig
from wasm.monitor.metrics import (
    collect_resource_metrics,
    collect_service_health,
    list_processes,
)
from wasm.monitor.models import (
    SEVERITY_NOTICE,
    SEVERITY_WARNING,
    SIGNAL_COMMAND_PATTERN,
    SIGNAL_NAME_PATTERN,
    SIGNAL_RESOURCE_USAGE,
    DiskUsage,
    ProcessInfo,
    ProcessObservation,
    ResourceMetrics,
    ServiceHealth,
)
from wasm.monitor.observation_store import ObservationStore
from wasm.monitor.process_monitor import (
    DEFAULT_CPU_THRESHOLD,
    DEFAULT_MEMORY_THRESHOLD,
    DEFAULT_RETENTION_DAYS,
    DEFAULT_SCAN_INTERVAL,
    MonitorConfig,
    ProcessMonitor,
)
from wasm.monitor.signals import observe_process, observe_processes

__all__ = [
    "DEFAULT_CPU_THRESHOLD",
    "DEFAULT_MEMORY_THRESHOLD",
    "DEFAULT_RETENTION_DAYS",
    "DEFAULT_SCAN_INTERVAL",
    "DEFAULT_SMTP_TIMEOUT",
    "SEVERITY_NOTICE",
    "SEVERITY_WARNING",
    "SIGNAL_COMMAND_PATTERN",
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
    "list_processes",
    "observe_process",
    "observe_processes",
]
