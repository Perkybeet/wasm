# Copyright (c) 2024-2026 Yago Lopez Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
The state of this one machine, as JSON.

This used to be rendered HTML: the panel's header polled a fragment, and the
``machine`` server-sent event pushed the same markup down the wire a second
time on a timer. Both readings came from this module, so the two never
disagreed, but the shape they agreed on was a rendered ``<header>`` a browser
had to parse out of a data field. The console reads a REST endpoint and an SSE
event that both return the values below untouched, and draws its own strip
from them; a Jinja fragment cannot be a typed API response.

This module is deliberately about a single box. WASM manages the server it
runs on, so the snapshot is an instrument reading rather than a fleet summary,
and an operator with several servers open in several tabs can tell them apart
by hostname alone.

``wasm.web.views.machine`` still exists, for the legacy Jinja panel that has
not been cut over yet: it reads its numbers from :func:`read_machine` here
rather than sampling psutil or systemd itself, so there remains exactly one
place that does either.
"""

from __future__ import annotations

import logging
import os
import socket
import sqlite3
import time
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from wasm.core.exceptions import WASMError

try:
    import psutil
except ImportError:  # pragma: no cover - psutil is an optional extra
    psutil = None  # type: ignore[assignment]

log = logging.getLogger(__name__)

#: How many load samples the snapshot carries. At one sample per read this is
#: a couple of minutes of history, which is the window in which a spike is
#: still worth reacting to. The console draws its own sparkline from these;
#: rendering one here was this module's job when the strip was HTML.
LOAD_HISTORY = 24

#: Where the disk meter reports on when nothing more specific is asked for.
DEFAULT_APPS_ROOT = "/var/www/apps"

_load_history: deque[float] = deque(maxlen=LOAD_HISTORY)


@dataclass
class MemorySnapshot:
    """Memory usage, in bytes."""

    used: int
    total: int
    percent: float


@dataclass
class DiskSnapshot:
    """Usage of the filesystem holding the applications, in bytes."""

    used: int
    total: int
    percent: float


@dataclass
class UnitTally:
    """
    How many WASM-managed systemd units are in each state.

    A unit systemd is mid-restart is neither genuinely up nor genuinely down;
    it is counted as ``stopped`` here, because the JSON schema has no fourth
    bucket for it - the console reads the ``job`` and ``app`` events for that,
    not a unit poll. The legacy Jinja fragment still shows it separately; see
    :mod:`wasm.web.views.machine`, which keeps that distinction from the same
    :func:`classify_unit` this tally uses.
    """

    running: int
    failed: int
    stopped: int


@dataclass
class AppTally:
    """How many deployed applications are in each state."""

    running: int
    failed: int
    stopped: int
    static: int


@dataclass
class MachineState:
    """
    A snapshot of the host, ready for ``dataclasses.asdict`` and the console.

    Attributes:
        hostname: What to call this machine.
        uptime_s: Seconds since boot.
        load: Load average over one, five and fifteen minutes.
        load_history: Recent one-minute load samples, oldest first.
        cpu_percent: CPU utilisation sampled just now.
        memory: Memory usage.
        disk: Usage of the filesystem holding the applications.
        units: Tally of the systemd units WASM manages.
        apps: Tally of the deployed applications.
    """

    hostname: str
    uptime_s: float
    load: tuple[float, float, float]
    load_history: list[float] = field(default_factory=list)
    cpu_percent: float = 0.0
    memory: MemorySnapshot = field(default_factory=lambda: MemorySnapshot(0, 0, 0.0))
    disk: DiskSnapshot = field(default_factory=lambda: DiskSnapshot(0, 0, 0.0))
    units: UnitTally = field(default_factory=lambda: UnitTally(0, 0, 0))
    apps: AppTally = field(default_factory=lambda: AppTally(0, 0, 0, 0))


def classify_unit(service: Mapping[str, Any]) -> str:
    """
    Sort one systemd unit into a state bucket.

    The active/sub precedence mirrors the one
    :func:`~wasm.core.app_state.resolve_state` uses for a single application,
    so a unit never disagrees with itself between the machine snapshot and the
    richer per-application answer ``wasm list`` and ``wasm health`` give.

    Args:
        service: One entry from :func:`fetch_service_states`.

    Returns:
        ``"active"``, ``"failed"``, ``"busy"`` (systemd is restarting it) or
        ``"stopped"``.
    """
    active_state = str(service.get("active", "")).strip()
    sub_state = str(service.get("sub", "")).strip()

    if active_state == "failed" or sub_state == "failed":
        return "failed"
    if sub_state == "auto-restart" or active_state == "activating":
        return "busy"
    if active_state == "active":
        return "active"
    return "stopped"


def fetch_service_states() -> list[dict[str, Any]]:
    """
    Ask systemd for the units WASM owns.

    :meth:`~wasm.managers.service_manager.ServiceManager.list_services`, which
    is :meth:`~wasm.managers.service_manager.ServiceManager.managed_units`:
    the same definition the Services page and ``wasm service list`` read, so
    the top bar counts exactly the rows the Services page shows. One
    ``systemctl list-units`` call and a scan of the unit directory, used to
    build both the unit tally and the application tally below: probing each
    application's unit and port individually, the way
    :func:`~wasm.core.app_state.resolve_states` does for ``wasm list``, is
    too costly to repeat on the five-second timer the ``machine`` SSE event
    runs on.

    Returns:
        One dictionary per unit, with the fields ``systemctl list-units``
        prints (``name``, ``load``, ``active``, ``sub``) and ``app``, the
        domain of the application the unit runs. Empty when systemd cannot be
        reached, which only degrades every tally built from it to zero rather
        than raising through a page render or the event stream.
    """
    from wasm.managers.service_manager import ServiceManager

    try:
        return ServiceManager(verbose=False).list_services()
    except WASMError as exc:
        log.warning("Could not read service states for the machine snapshot: %s", exc)
        return []


def _count_units(services: list[dict[str, Any]]) -> UnitTally:
    """
    Tally the JSON snapshot's three unit buckets.

    Args:
        services: What :func:`fetch_service_states` returned.

    Returns:
        The tally.
    """
    running = failed = stopped = 0
    for service in services:
        bucket = classify_unit(service)
        if bucket == "active":
            running += 1
        elif bucket == "failed":
            failed += 1
        else:
            # "busy" (mid-restart) folds in here; see UnitTally's docstring.
            stopped += 1
    return UnitTally(running=running, failed=failed, stopped=stopped)


def _count_apps(services: list[dict[str, Any]]) -> AppTally:
    """
    Tally applications by state, from the unit list the unit tally also reads.

    Each application either has no unit at all - it is static, served
    directly by the web server, and asking systemd about it would always say
    "not running" - or runs as the units the listing attributes to it (its
    ``app`` field, from
    :meth:`~wasm.managers.service_manager.ServiceManager.app_units`: the
    legacy prefix, a monorepo's workspaces and Compose are resolved there,
    once). An application with several units is as healthy as its worst one:
    failed when any failed, running only when all run.

    Args:
        services: What :func:`fetch_service_states` returned.

    Returns:
        How many applications are running, failed, stopped or static.
    """
    from wasm.core.store import get_store

    try:
        apps = get_store().list_apps()
    except (WASMError, sqlite3.Error) as exc:
        log.warning("Could not read applications for the machine snapshot: %s", exc)
        return AppTally(running=0, failed=0, stopped=0, static=0)

    by_app: dict[str, list[str]] = {}
    for service in services:
        domain = service.get("app")
        if domain:
            by_app.setdefault(str(domain), []).append(classify_unit(service))

    running = failed = stopped = static = 0
    for app in apps:
        if app.is_static:
            static += 1
            continue

        buckets = by_app.get(app.domain, [])
        if "failed" in buckets:
            failed += 1
        elif buckets and all(bucket == "active" for bucket in buckets):
            running += 1
        else:
            stopped += 1

    return AppTally(running=running, failed=failed, stopped=stopped, static=static)


def _resolve_apps_root(apps_root: str | None) -> str:
    """
    Work out which filesystem the disk meter reports on.

    Args:
        apps_root: An explicit override, or None to read the configured
            ``apps_directory`` - the same flat key every deployer reads, so
            the meter reports on the filesystem applications are actually
            deployed to.

    Returns:
        A directory path. Falls back to :data:`DEFAULT_APPS_ROOT` when no
        override was given and the configuration cannot be read - the same
        default the applications directory itself has.
    """
    if apps_root is not None:
        return apps_root

    from wasm.core.config import Config

    try:
        return str(Config().get("apps_directory", DEFAULT_APPS_ROOT))
    except (WASMError, OSError) as exc:
        log.warning(
            "Could not read apps_directory from the configuration, using the default: %s", exc
        )
        return DEFAULT_APPS_ROOT


def read_machine(apps_root: str | None = None) -> MachineState:
    """
    Read the current state of the host.

    Args:
        apps_root: Directory whose filesystem the disk meter reports on. The
            applications live there, so that is the space that runs out
            first and the space an operator cares about. None reads the
            configured ``apps_directory``.

    Returns:
        A snapshot ready for ``dataclasses.asdict`` and ``MachineOut``.
    """
    apps_root = _resolve_apps_root(apps_root)
    hostname = socket.gethostname()

    try:
        load = os.getloadavg()
    except OSError:  # pragma: no cover - not available on every platform
        load = (0.0, 0.0, 0.0)
    _load_history.append(load[0])

    if psutil is not None:
        memory = psutil.virtual_memory()
        target = apps_root if os.path.isdir(apps_root) else "/"
        disk = psutil.disk_usage(target)
        uptime_seconds = time.time() - psutil.boot_time()
        # A short blocking sample: both callers of this function - the REST
        # handler and the SSE tick - already run off the event loop, in
        # FastAPI's threadpool or events.py's run_in_threadpool respectively.
        cpu_percent = psutil.cpu_percent(interval=0.1)
        memory_snapshot = MemorySnapshot(
            used=memory.used, total=memory.total, percent=memory.percent
        )
        disk_snapshot = DiskSnapshot(used=disk.used, total=disk.total, percent=disk.percent)
    else:
        uptime_seconds = 0.0
        cpu_percent = 0.0
        memory_snapshot = MemorySnapshot(used=0, total=0, percent=0.0)
        disk_snapshot = DiskSnapshot(used=0, total=0, percent=0.0)

    services = fetch_service_states()

    return MachineState(
        hostname=hostname,
        uptime_s=uptime_seconds,
        load=(load[0], load[1], load[2]),
        load_history=list(_load_history),
        cpu_percent=cpu_percent,
        memory=memory_snapshot,
        disk=disk_snapshot,
        units=_count_units(services),
        apps=_count_apps(services),
    )
