"""
Report-only detection.

These checks produce notes, not verdicts. The previous version matched a
regular expression against a full command line, called the result "malicious"
with 0.95 confidence, and then terminated the process tree and deleted its
working directory. ``grep -r xmrig /var/log`` was enough to trigger it.

Two rules keep that from happening again:

- Name patterns are matched against the executable name only. What a process
  reads or greps for says nothing about what it is.
- Nothing here returns an action. The only output is a
  :class:`~wasm.monitor.models.ProcessObservation` for a human to read.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from wasm.monitor.models import (
    SEVERITY_NOTICE,
    SEVERITY_WARNING,
    SIGNAL_COMMAND_PATTERN,
    SIGNAL_NAME_PATTERN,
    SIGNAL_RESOURCE_USAGE,
    ProcessInfo,
    ProcessObservation,
)

#: Executable names that are, in practice, only ever malware. Matched against
#: the process name, never against its arguments.
NOTABLE_NAME_PATTERNS: tuple[str, ...] = (
    r"^xmrig",
    r"^minerd",
    r"^cpuminer",
    r"^cgminer",
    r"^bfgminer",
    r"^ethminer",
    r"^ccminer",
    r"^kdevtmpfsi",
    r"^kinsing",
    r"^kerberods",
    r"^watchdogs",
)

#: Command lines worth a second look. A notice, never more: every one of these
#: has a legitimate use, which is exactly why nothing acts on them.
NOTABLE_COMMAND_PATTERNS: tuple[str, ...] = (
    r"curl\s[^|]*\|\s*(ba)?sh",
    r"wget\s[^|]*\|\s*(ba)?sh",
    r"\bnc\s+-[a-z]*e\b",
    r"\bncat\b[^|]*\s-e\b",
    r"/dev/tcp/",
    r"\bsocat\b.*\bexec:",
)

#: Long-running system and application processes. Used to keep the resource and
#: command-line notices quiet for software the operator installed on purpose.
KNOWN_SAFE_NAME_PATTERNS: tuple[str, ...] = (
    r"^systemd",
    r"^sshd$",
    r"^nginx",
    r"^apache2$",
    r"^httpd$",
    r"^postgres",
    r"^mysqld$",
    r"^mariadbd$",
    r"^redis-server$",
    r"^mongod$",
    r"^dockerd$",
    r"^containerd",
    r"^node$",
    r"^next-server",
    r"^npm$",
    r"^pnpm$",
    r"^yarn$",
    r"^bun$",
    r"^deno$",
    r"^python3?(\.\d+)?$",
    r"^gunicorn$",
    r"^uvicorn$",
    r"^celery$",
    r"^php-fpm",
    r"^journald$",
    r"^rsyslogd$",
    r"^cron$",
    r"^crond$",
    r"^snapd$",
    r"^dbus",
    r"^polkitd$",
    r"^wasm",
)


def is_known_safe(process: ProcessInfo) -> bool:
    """
    Report whether a process is one of the usual suspects, in the good sense.

    Args:
        process: The process snapshot to classify.

    Returns:
        True when the executable name matches a known-safe pattern.
    """
    name = process.name.lower()
    return any(re.search(pattern, name) for pattern in KNOWN_SAFE_NAME_PATTERNS)


def observe_process(
    process: ProcessInfo,
    cpu_threshold: float,
    memory_threshold: float,
) -> ProcessObservation | None:
    """
    Decide whether a process is worth writing down.

    Args:
        process: The process snapshot to inspect.
        cpu_threshold: CPU percentage above which a process is noted.
        memory_threshold: Memory percentage above which a process is noted.

    Returns:
        An observation, or None when nothing stood out.
    """
    name = process.name.lower()
    command = process.command.lower()
    safe = is_known_safe(process)

    if not safe:
        for pattern in NOTABLE_NAME_PATTERNS:
            if re.search(pattern, name):
                return ProcessObservation(
                    process=process,
                    signal=SIGNAL_NAME_PATTERN,
                    severity=SEVERITY_WARNING,
                    detail=(
                        f"Executable name matches the known-malware pattern "
                        f"{pattern.lstrip('^')!r}. Review the process before acting on it."
                    ),
                )

        for pattern in NOTABLE_COMMAND_PATTERNS:
            if re.search(pattern, command):
                return ProcessObservation(
                    process=process,
                    signal=SIGNAL_COMMAND_PATTERN,
                    severity=SEVERITY_NOTICE,
                    detail=(
                        f"Command line matches the pattern {pattern!r}, which is often "
                        "remote code execution and sometimes an ordinary install script."
                    ),
                )

    if process.cpu_percent > cpu_threshold or process.memory_percent > memory_threshold:
        return ProcessObservation(
            process=process,
            signal=SIGNAL_RESOURCE_USAGE,
            severity=SEVERITY_NOTICE,
            detail=(
                f"Sustained resource usage: CPU {process.cpu_percent:.1f}% "
                f"(threshold {cpu_threshold:.1f}%), memory {process.memory_percent:.1f}% "
                f"(threshold {memory_threshold:.1f}%)."
            ),
        )

    return None


def observe_processes(
    processes: Sequence[ProcessInfo],
    cpu_threshold: float,
    memory_threshold: float,
) -> list[ProcessObservation]:
    """
    Apply :func:`observe_process` to a snapshot of the process table.

    Args:
        processes: Process snapshots to inspect.
        cpu_threshold: CPU percentage above which a process is noted.
        memory_threshold: Memory percentage above which a process is noted.

    Returns:
        Observations, ordered with warnings first and then by CPU usage.
    """
    observations = [
        observation
        for observation in (
            observe_process(process, cpu_threshold, memory_threshold) for process in processes
        )
        if observation is not None
    ]

    return sorted(
        observations,
        key=lambda o: (o.severity != SEVERITY_WARNING, -o.process.cpu_percent),
    )
