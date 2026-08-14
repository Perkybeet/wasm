# Copyright (c) 2024-2026 Yago Lopez Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
The sampling thread behind the panel's charts and its live metrics feed.

One daemon thread in the web process reads the machine through psutil and every
application unit through its cgroup, every couple of seconds. Each tick is
persisted to the RRD-style :class:`~wasm.monitor.timeseries.MetricsStore` for
the history the charts load, and kept as an in-memory snapshot for the ``/events``
stream to push to open pages.

Per-application readings come from cgroup v2, read straight off the
filesystem: systemd already accounts CPU time and resident memory for
``wasm-{domain}.service`` in ``cpu.stat`` and ``memory.current``, so asking
systemctl - a subprocess per app per tick - would be paying process spawns for
numbers the kernel publishes as two files. On a host without a unified cgroup
hierarchy (a container, cgroup v1, a stopped unit) the files are simply absent
and that application's metrics are skipped for the tick.

The collector does not raise for anything a running machine can do to it. It
runs unattended for the life of the web process, and a panel whose metrics
thread died at 3am to a transient read error is a panel whose charts silently
end at 3am. Every operational failure - an unreadable /proc, a locked
database, a cgroup that vanished mid-read - is logged at debug and the next
tick tries again. The catches are the specific errors those sources produce,
not ``except Exception``: a programming error in this module must stay loud,
which is the project's whole position on error handling.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time
from collections.abc import Callable
from pathlib import Path

from wasm.core.exceptions import WASMError
from wasm.monitor.timeseries import MetricsStore, default_metrics_db_path

try:
    import psutil
except ImportError:  # pragma: no cover - psutil is an optional extra
    psutil = None  # type: ignore[assignment]

log = logging.getLogger(__name__)

#: What a psutil reading can raise in practice: its own error family for
#: processes and platforms, and OSError for the /proc and statvfs reads
#: underneath. Named specifically rather than catching Exception, so a bug in
#: this module stays loud instead of becoming a debug line.
_SAMPLING_ERRORS: tuple[type[Exception], ...] = (
    (psutil.Error, OSError, ValueError) if psutil is not None else (OSError, ValueError)
)

#: What reading or writing the SQLite store can raise.
_STORE_ERRORS: tuple[type[Exception], ...] = (sqlite3.Error, OSError, ValueError, WASMError)

#: Where systemd parents the cgroups of the units WASM writes.
CGROUP_ROOT = Path("/sys/fs/cgroup/system.slice")

#: Seconds between samples. Matches the raw tier of the metrics store.
DEFAULT_INTERVAL_SECONDS = 2.0

#: How long the list of application domains is trusted before the store is
#: asked again. A deploy mid-window shows up in its metrics half a minute
#: late, which is cheaper than a SQLite read per tick.
APPS_REFRESH_SECONDS = 30.0

#: Seconds between store consolidations. Consolidation is a no-op until whole
#: buckets have expired, so running it on this timer keeps the database small
#: without a scheduler.
CONSOLIDATE_SECONDS = 300.0

#: cpu.stat counts in microseconds.
USEC_PER_SECOND = 1_000_000


class MetricsCollector:
    """
    Samples the machine and every application unit on a timer.

    The public surface is deliberately small: :meth:`start` and :meth:`stop`
    bracket the thread, :meth:`latest` hands the newest snapshot to the SSE
    stream, and :meth:`sample_once` is one tick, exposed so tests can drive
    the sampling deterministically without a thread or a wall clock.
    """

    def __init__(
        self,
        store: MetricsStore,
        *,
        interval_s: float = DEFAULT_INTERVAL_SECONDS,
        cgroup_root: Path = CGROUP_ROOT,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """
        Args:
            store: Where samples are persisted.
            interval_s: Seconds between ticks.
            cgroup_root: Directory holding the unit cgroups. Injected so tests
                can point it at a fake tree.
            clock: Monotonic time source for rate deltas. Injected so tests
                never depend on real elapsed time.
        """
        self.store = store
        self.interval_s = float(interval_s)
        self.cgroup_root = Path(cgroup_root)
        self._clock = clock
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._snapshot: dict[str, float] = {}
        self._snapshot_lock = threading.Lock()
        self._last_tick: float | None = None
        self._last_net: tuple[int, int] | None = None
        self._last_cpu_usec: dict[str, int] = {}
        self._domains: list[str] = []
        self._domains_read_at: float | None = None
        self._consolidated_at = self._clock()

    def start(self) -> None:
        """Start the sampling thread. Starting twice is a no-op."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="wasm-metrics-collector", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        """Stop the sampling thread and wait for it to finish its tick."""
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=self.interval_s + 5.0)
        self._thread = None

    def latest(self) -> dict[str, float]:
        """
        Return the newest complete snapshot.

        Returns:
            Metric name to value, copied so the caller can hold it while the
            collector keeps ticking. Empty until the first tick lands.
        """
        with self._snapshot_lock:
            return dict(self._snapshot)

    def _run(self) -> None:
        """Tick until asked to stop. The first sample is taken immediately."""
        self.sample_once()
        while not self._stop.wait(self.interval_s):
            self.sample_once()

    def sample_once(self) -> dict[str, float]:
        """
        Take one sample of everything and persist it.

        Operational failures are logged at debug and skipped rather than
        raised, because this runs on an unattended thread whose death would
        silently end the panel's metrics.

        Returns:
            The snapshot this tick produced.
        """
        now = self._clock()
        elapsed = now - self._last_tick if self._last_tick is not None else None
        self._last_tick = now

        pairs: list[tuple[str, float]] = []
        try:
            pairs.extend(self._system_pairs(elapsed))
        except _SAMPLING_ERRORS:
            log.debug("system metrics could not be sampled", exc_info=True)
        pairs.extend(self._app_pairs(now, elapsed))

        try:
            self.store.record_many(pairs)
        except _STORE_ERRORS:
            log.debug("a metrics tick could not be persisted", exc_info=True)

        try:
            if now - self._consolidated_at >= CONSOLIDATE_SECONDS:
                self.store.consolidate()
                self._consolidated_at = now
        except _STORE_ERRORS:
            log.debug("metrics consolidation failed", exc_info=True)

        snapshot = dict(pairs)
        with self._snapshot_lock:
            self._snapshot = snapshot
        return snapshot

    def _system_pairs(self, elapsed: float | None) -> list[tuple[str, float]]:
        """
        Sample the whole machine through psutil.

        Args:
            elapsed: Seconds since the previous tick, or None on the first.

        Returns:
            ``(metric, value)`` pairs. Empty when psutil is not installed.
        """
        if psutil is None:
            return []

        pairs: list[tuple[str, float]] = [
            ("cpu.percent", float(psutil.cpu_percent(interval=None))),
        ]

        memory = psutil.virtual_memory()
        # The total barely changes after boot, but recording it beside the
        # used figure keeps a chart's ceiling in the same query as its line.
        pairs.append(("mem.used_bytes", float(memory.used)))
        pairs.append(("mem.total_bytes", float(memory.total)))
        pairs.append(("swap.used_bytes", float(psutil.swap_memory().used)))

        disk = psutil.disk_usage("/")
        pairs.append(("disk.used_bytes", float(disk.used)))
        pairs.append(("disk.total_bytes", float(disk.total)))

        net = psutil.net_io_counters()
        if net is not None:
            previous = self._last_net
            self._last_net = (int(net.bytes_recv), int(net.bytes_sent))
            if previous is not None and elapsed is not None and elapsed > 0:
                rx = (net.bytes_recv - previous[0]) / elapsed
                tx = (net.bytes_sent - previous[1]) / elapsed
                # A negative delta means the kernel counters reset (an
                # interface bounced); a rate invented from that would be a
                # spike on the chart that never happened.
                if rx >= 0 and tx >= 0:
                    pairs.append(("net.rx_bytes_s", rx))
                    pairs.append(("net.tx_bytes_s", tx))

        try:
            pairs.append(("load.1m", float(os.getloadavg()[0])))
        except OSError:  # pragma: no cover - not available on every platform
            pass

        return pairs

    def _app_pairs(self, now: float, elapsed: float | None) -> list[tuple[str, float]]:
        """
        Sample every application unit through its cgroup.

        Args:
            now: The current monotonic time.
            elapsed: Seconds since the previous tick, or None on the first.

        Returns:
            ``(metric, value)`` pairs for every unit whose cgroup exists. A
            unit without one - stopped, cgroup v1, a container - contributes
            nothing and costs nothing.
        """
        pairs: list[tuple[str, float]] = []
        for domain in self._app_domains(now):
            unit_dir = self.cgroup_root / f"wasm-{domain}.service"

            try:
                memory = int((unit_dir / "memory.current").read_text())
            except (OSError, ValueError):
                log.debug("no readable memory cgroup for %s", domain)
            else:
                pairs.append((f"app.{domain}.mem.bytes", float(memory)))

            usec = _read_cpu_usec(unit_dir / "cpu.stat")
            if usec is None:
                # Forget the counter so a unit that comes back does not have
                # its first delta measured against a life it no longer lives.
                self._last_cpu_usec.pop(domain, None)
                continue
            previous = self._last_cpu_usec.get(domain)
            self._last_cpu_usec[domain] = usec
            if previous is None or elapsed is None or elapsed <= 0:
                continue
            delta = usec - previous
            if delta < 0:
                # The unit restarted between ticks and its counter began
                # again at zero.
                continue
            pairs.append((f"app.{domain}.cpu.percent", delta / (elapsed * USEC_PER_SECOND) * 100))

        return pairs

    def _app_domains(self, now: float) -> list[str]:
        """
        Name the applications worth sampling, refreshed on a slow timer.

        Args:
            now: The current monotonic time.

        Returns:
            The domains of every application in the store. On a store error
            the previous list is kept: a transient read failure must not blank
            every app's chart for a tick.
        """
        if self._domains_read_at is not None and now - self._domains_read_at < APPS_REFRESH_SECONDS:
            return self._domains

        try:
            from wasm.core.store import get_store

            domains = [app.domain for app in get_store().list_apps() if app.domain]
        except _STORE_ERRORS:
            log.debug("the application list could not be refreshed", exc_info=True)
        else:
            self._domains = domains
            # Deleted applications must not keep a CPU counter alive forever.
            for stale in set(self._last_cpu_usec) - set(domains):
                del self._last_cpu_usec[stale]
        self._domains_read_at = now
        return self._domains


def _read_cpu_usec(cpu_stat: Path) -> int | None:
    """
    Read the cumulative CPU time out of a cgroup v2 ``cpu.stat`` file.

    Args:
        cpu_stat: Path to the file.

    Returns:
        The ``usage_usec`` counter, or None when the file is missing or does
        not carry one.
    """
    try:
        text = cpu_stat.read_text()
    except OSError:
        return None
    for line in text.splitlines():
        name, _, value = line.partition(" ")
        if name == "usage_usec":
            try:
                return int(value)
            except ValueError:
                return None
    return None


# ---------------------------------------------------------------------------
# The process-wide instances the web application wires up
# ---------------------------------------------------------------------------

_lock = threading.RLock()
_store: MetricsStore | None = None
_collector: MetricsCollector | None = None


def get_metrics_store() -> MetricsStore:
    """
    Return the process-wide metrics store, creating it on first use.

    Returns:
        The store, on :func:`~wasm.monitor.timeseries.default_metrics_db_path`.
    """
    global _store
    with _lock:
        if _store is None:
            _store = MetricsStore(default_metrics_db_path())
        return _store


def get_metrics_collector() -> MetricsCollector | None:
    """
    Return the running collector, if the application started one.

    Returns:
        The collector, or None outside the web application's lifespan.
    """
    return _collector


def start_metrics_collector() -> MetricsCollector | None:
    """
    Create and start the process-wide collector.

    Called from the web application's lifespan. A panel that cannot write its
    metrics database must still serve, so failure to open the store is logged
    and reported as None rather than raised.

    Returns:
        The running collector, or None when the store could not be opened.
    """
    global _collector
    with _lock:
        if _collector is None:
            try:
                _collector = MetricsCollector(get_metrics_store())
            except (OSError, sqlite3.Error) as exc:
                log.warning("Metrics are disabled: the store could not be opened: %s", exc)
                return None
        _collector.start()
        return _collector


def stop_metrics_collector() -> None:
    """Stop and discard the process-wide collector, if one is running."""
    global _collector
    with _lock:
        collector = _collector
        _collector = None
    if collector is not None:
        collector.stop()
