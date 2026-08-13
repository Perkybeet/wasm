# Copyright (c) 2024-2026 Yago Lopez Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
The state of this one machine, as the panel's header shows it.

This is deliberately about a single box. WASM manages the server it runs on, so
the header is an instrument reading rather than a fleet summary, and an
operator with several servers open in several tabs can tell them apart at a
glance.
"""

from __future__ import annotations

import logging
import os
import platform
import socket
import time
from collections import deque
from dataclasses import dataclass

from wasm.core.exceptions import WASMError
from wasm.web.views.rendering import duration, filesize

try:
    import psutil
except ImportError:  # pragma: no cover - psutil is an optional extra
    psutil = None  # type: ignore[assignment]

log = logging.getLogger(__name__)

#: How many load samples the sparkline draws. At one sample per refresh this is
#: a couple of minutes of history, which is the window in which a spike is
#: still worth reacting to.
LOAD_HISTORY = 24

_load_history: deque[float] = deque(maxlen=LOAD_HISTORY)


@dataclass
class MachineState:
    """
    A snapshot of the host, shaped for the header template.

    Attributes:
        hostname: What to call this machine.
        os: Operating system description, shown on hover.
        uptime: How long the machine has been up, already formatted.
        load: Load average over one, five and fifteen minutes.
        load_history_points: SVG polyline points for the load sparkline.
        memory_used: Bytes of memory in use.
        memory_total: Bytes of memory installed.
        disk_used: Bytes used on the filesystem holding the applications.
        disk_total: Bytes available on that filesystem.
        units_active: Count of managed services that are running.
        units_failed: Count of managed services that have failed.
        units_busy: Count of managed services mid-operation.
    """

    hostname: str
    os: str
    uptime: str
    load: tuple[float, float, float]
    load_history_points: str
    memory_used: int
    memory_total: int
    disk_used: int
    disk_total: int
    units_active: int = 0
    units_failed: int = 0
    units_busy: int = 0

    @property
    def memory_used_human(self) -> str:
        """Memory in use, as a system tool would print it."""
        return filesize(self.memory_used)

    @property
    def memory_total_human(self) -> str:
        """Memory installed, as a system tool would print it."""
        return filesize(self.memory_total)

    @property
    def disk_used_human(self) -> str:
        """Disk in use, as a system tool would print it."""
        return filesize(self.disk_used)

    @property
    def disk_total_human(self) -> str:
        """Disk capacity, as a system tool would print it."""
        return filesize(self.disk_total)


def _sparkline(samples: list[float], width: int = 80, height: int = 18) -> str:
    """
    Turn load samples into SVG polyline points.

    Args:
        samples: Recent load values, oldest first.
        width: Viewbox width.
        height: Viewbox height.

    Returns:
        A points attribute value. Empty when there is nothing to draw.
    """
    if len(samples) < 2:
        return ""
    peak = max(max(samples), 1.0)
    step = width / (len(samples) - 1)
    points = [
        f"{index * step:.1f},{height - (value / peak) * (height - 2) - 1:.1f}"
        for index, value in enumerate(samples)
    ]
    return " ".join(points)


def _count_units() -> tuple[int, int, int]:
    """
    Tally the state of every systemd unit WASM manages.

    This reads :meth:`~wasm.managers.service_manager.ServiceManager.list_services`,
    the same live systemd source the services screen and ``wasm service list``
    already read, rather than the store's status column, which nothing writes
    to after a deploy and which is exactly how ``wasm list`` and ``wasm health``
    used to disagree. It is one ``systemctl list-units`` call scoped to units
    WASM owns, never a probe per unit: the strip refreshes every five seconds,
    and the per-application state resolver in :mod:`wasm.core.app_state`, at
    three systemctl calls and a socket probe per application, is too costly to
    run on that timer.

    The active/failed/busy split mirrors the precedence
    :func:`~wasm.core.app_state.resolve_state` uses for the same two systemd
    fields, so the header never disagrees with the rest of the panel about
    what "failed" or "working" means.

    Returns:
        Counts of active, failed and busy (restarting) units. A unit that is
        merely stopped counts as none of the three, the same as an idle
        resource elsewhere in the panel.
    """
    from wasm.managers.service_manager import ServiceManager

    try:
        services = ServiceManager(verbose=False).list_services()
    except WASMError as exc:
        log.warning("Could not read service states for the machine strip: %s", exc)
        return (0, 0, 0)

    active = failed = busy = 0
    for service in services:
        active_state = str(service.get("active", "")).strip()
        sub_state = str(service.get("sub", "")).strip()

        if active_state == "failed" or sub_state == "failed":
            failed += 1
        elif sub_state == "auto-restart" or active_state == "activating":
            busy += 1
        elif active_state == "active":
            active += 1

    return active, failed, busy


def read_machine(apps_root: str = "/var/www/apps") -> MachineState:
    """
    Read the current state of the host.

    Args:
        apps_root: Directory whose filesystem the disk meter reports on. The
            applications live there, so that is the space that runs out first
            and the space an operator cares about.

    Returns:
        A snapshot for the header.
    """
    hostname = socket.gethostname()
    os_name = f"{platform.system()} {platform.release()}"

    try:
        load = os.getloadavg()
    except OSError:  # pragma: no cover - not available on every platform
        load = (0.0, 0.0, 0.0)
    _load_history.append(load[0])

    if psutil is not None:
        memory = psutil.virtual_memory()
        memory_used, memory_total = memory.used, memory.total
        target = apps_root if os.path.isdir(apps_root) else "/"
        disk = psutil.disk_usage(target)
        disk_used, disk_total = disk.used, disk.total
        uptime_seconds = time.time() - psutil.boot_time()
    else:
        memory_used = memory_total = disk_used = disk_total = 0
        uptime_seconds = 0.0

    units_active, units_failed, units_busy = _count_units()

    return MachineState(
        hostname=hostname,
        os=os_name,
        uptime=duration(uptime_seconds),
        load=(load[0], load[1], load[2]),
        load_history_points=_sparkline(list(_load_history)),
        memory_used=memory_used,
        memory_total=memory_total,
        disk_used=disk_used,
        disk_total=disk_total,
        units_active=units_active,
        units_failed=units_failed,
        units_busy=units_busy,
    )
