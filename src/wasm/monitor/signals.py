"""
Report-only detection.

These checks produce notes, not verdicts, and they are deliberately cheap.
Three properties, each one a defect this module used to have:

- **Nothing is matched against a command line.** An unprivileged user picks
  their own argv, so any check over it is an input an attacker controls. The
  previous version ran ``curl\\s[^|]*\\|\\s*(ba)?sh`` over the whole cmdline:
  quadratic backtracking, driven by a hostile string, inside the single
  threaded loop of a daemon running as root. What a process greps for or
  downloads says nothing about what it is, so the whole signal is gone rather
  than merely made linear.
- **No regular expressions at all.** Matching is exact-name lookup or a bounded
  prefix comparison, both linear in the length of a name that is itself capped.
  A test asserts that ``re`` is not imported here.
- **Nothing returns an action.** The only output is a
  :class:`~wasm.monitor.models.ProcessObservation` for a human to read.
"""

from __future__ import annotations

from collections.abc import Sequence

from wasm.monitor.models import (
    SEVERITY_NOTICE,
    SEVERITY_WARNING,
    SIGNAL_NAME_PATTERN,
    SIGNAL_RESOURCE_USAGE,
    ProcessInfo,
    ProcessObservation,
)

#: Longest executable name compared. The kernel's ``comm`` is 15 characters;
#: anything past this is padding meant to make matching expensive.
MAX_NAME_LENGTH = 64

#: Executable names that are, in practice, only ever malware. Compared as a
#: prefix of the process name so that versioned droppers (``xmrig-6.20``) still
#: match, and never against arguments.
NOTABLE_NAME_PREFIXES: tuple[str, ...] = (
    "xmrig",
    "minerd",
    "cpuminer",
    "cgminer",
    "bfgminer",
    "ethminer",
    "ccminer",
    "kdevtmpfsi",
    "kinsing",
    "kerberods",
    "watchdogs",
)

#: Long-running system and application processes, matched exactly. Exact
#: matching is the point: the previous unanchored ``^systemd`` also accepted
#: ``systemd-xmrig``, so an attacker got onto the whitelist by choosing a name.
KNOWN_SAFE_NAMES: frozenset[str] = frozenset(
    {
        "agetty",
        "apache2",
        "bun",
        "celery",
        "chronyd",
        "containerd",
        "containerd-shim",
        "containerd-shim-runc-v1",
        "containerd-shim-runc-v2",
        "cron",
        "crond",
        "dbus-broker",
        "dbus-broker-lau",
        "dbus-daemon",
        "deno",
        "dockerd",
        "gunicorn",
        "httpd",
        "init",
        "journald",
        "mongod",
        "mysqld",
        "next-server",
        "nginx",
        "node",
        "npm",
        "pnpm",
        "polkitd",
        "redis-server",
        "rsyslogd",
        "snapd",
        "sshd",
        "systemd",
        "systemd-journald",
        "systemd-logind",
        "systemd-networkd",
        "systemd-oomd",
        "systemd-resolved",
        "systemd-timesyncd",
        "systemd-udevd",
        "uvicorn",
        "wasm",
        "wasm-monitor",
        "yarn",
    }
)

#: Executables whose name carries a version suffix: ``python3.11``,
#: ``php-fpm8.2``, ``postgres`` workers. Only digits and dots may follow the
#: prefix, so ``php-fpm-backdoor`` and ``python3-xmrig`` are not covered.
VERSIONED_SAFE_PREFIXES: tuple[str, ...] = (
    "mariadbd",
    "php-fpm",
    "postgres",
    "python",
    "python3",
)


def _is_versioned_variant(name: str, prefix: str) -> bool:
    """
    Report whether a name is a prefix plus a version suffix.

    Args:
        name: Executable name, already lowercased.
        prefix: Whitelisted family prefix.

    Returns:
        True when everything after the prefix is digits and dots.
    """
    if not name.startswith(prefix):
        return False
    suffix = name[len(prefix) :]
    return all(character.isdigit() or character == "." for character in suffix)


def is_known_safe(process: ProcessInfo) -> bool:
    """
    Report whether a process is one of the usual suspects, in the good sense.

    Args:
        process: The process snapshot to classify.

    Returns:
        True when the executable name is a known system or application daemon.
    """
    name = process.name[:MAX_NAME_LENGTH].lower()
    if name in KNOWN_SAFE_NAMES:
        return True
    return any(_is_versioned_variant(name, prefix) for prefix in VERSIONED_SAFE_PREFIXES)


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
    name = process.name[:MAX_NAME_LENGTH].lower()
    safe = is_known_safe(process)

    if not safe:
        for prefix in NOTABLE_NAME_PREFIXES:
            if name.startswith(prefix):
                return ProcessObservation(
                    process=process,
                    signal=SIGNAL_NAME_PATTERN,
                    severity=SEVERITY_WARNING,
                    detail=(
                        f"Executable name starts with {prefix!r}, a name used by known "
                        "cryptomining and botnet malware. This is a note, not a verdict: "
                        "identify the binary before acting on it."
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
