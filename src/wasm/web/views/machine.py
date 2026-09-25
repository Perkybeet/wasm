# Copyright (c) 2024-2026 Yago Lopez Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
The state of this one machine, as the legacy panel's header shows it.

The JSON snapshot itself - hostname, load, memory, disk, unit and application
tallies - is sampled exactly once, in :mod:`wasm.web.machine`. This module
does not read psutil or systemd on its own; it reshapes that snapshot into the
strings and buckets the Jinja fragment ``fragments/machine.html`` renders, for
the server-rendered panel that has not been cut over to the console yet (see
Task 2.1 of the v2 plan).

One thing does not survive the reshape as a straight lookup: the fragment
still shows a unit that systemd is restarting as "working", separately from
"failed" and "running". The JSON schema folds that case into "stopped" - the
console reads the ``job`` and ``app`` events for that, not a unit poll - so
recovering it here costs one more ``systemctl list-units`` call, built from
the same :func:`~wasm.web.machine.classify_unit` the JSON tally uses. That is
the only extra reading this module does, and it goes away with the rest of
this file once the legacy panel is deleted.
"""

from __future__ import annotations

import platform
from dataclasses import dataclass

from wasm.web.machine import classify_unit, fetch_service_states
from wasm.web.machine import read_machine as _read_snapshot
from wasm.web.views.rendering import duration, filesize


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
    Tally the state of every systemd unit WASM manages, "busy" included.

    The JSON snapshot's own tally, on ``MachineState.units`` in
    :mod:`wasm.web.machine`, folds a restarting unit into "stopped"; this
    fragment still shows it separately, so it re-reads the live list through
    :func:`~wasm.web.machine.fetch_service_states` and classifies it with
    :func:`~wasm.web.machine.classify_unit` - the one place that decision is
    made - rather than deciding it again here.

    Returns:
        Counts of active, failed and busy (restarting) units. A unit that is
        merely stopped counts as none of the three, the same as an idle
        resource elsewhere in the panel.
    """
    active = failed = busy = 0
    for service in fetch_service_states():
        bucket = classify_unit(service)
        if bucket == "active":
            active += 1
        elif bucket == "failed":
            failed += 1
        elif bucket == "busy":
            busy += 1
    return active, failed, busy


def read_machine(apps_root: str = "/var/www/apps") -> MachineState:
    """
    Read the current state of the host, for the legacy Jinja fragment.

    Args:
        apps_root: Directory whose filesystem the disk meter reports on,
            forwarded to :func:`wasm.web.machine.read_machine` untouched.

    Returns:
        A snapshot for the header.
    """
    snapshot = _read_snapshot(apps_root)
    units_active, units_failed, units_busy = _count_units()

    return MachineState(
        hostname=snapshot.hostname,
        os=f"{platform.system()} {platform.release()}",
        uptime=duration(snapshot.uptime_s),
        load=(snapshot.load[0], snapshot.load[1], snapshot.load[2]),
        load_history_points=_sparkline(snapshot.load_history),
        memory_used=snapshot.memory.used,
        memory_total=snapshot.memory.total,
        disk_used=snapshot.disk.used,
        disk_total=snapshot.disk.total,
        units_active=units_active,
        units_failed=units_failed,
        units_busy=units_busy,
    )
