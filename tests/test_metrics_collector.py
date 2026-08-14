# Copyright (c) 2024-2026 Yago Lopez Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
Tests for the metrics collector behind the panel's charts and live feed.

Everything here drives :meth:`MetricsCollector.sample_once` with an injected
monotonic clock, a fake psutil and a fake cgroup tree in the test's own
directory, so no test depends on the machine it runs on. What is defended:

- **Rates are deltas, not readings.** Network bytes per second and per-app CPU
  percent are computed from counter deltas over the injected clock's elapsed
  time; a first sample has no delta and must publish no rate.
- **A unit without a cgroup costs nothing.** Stopped units, cgroup v1 and
  containers simply lack the files; that application is skipped for the tick,
  with no error and no invented zero.
- **Operational failures do not stop the tick.** The collector runs unattended
  on a daemon thread for the life of the web process; a thread that died at
  3am to a transient error is a chart that silently ends at 3am.
- **The thread stops when asked** and the snapshot handed to the SSE stream is
  a copy, safe to hold while the collector keeps ticking.
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from wasm.monitor.timeseries import MetricsStore
from wasm.web import metrics_collector
from wasm.web.metrics_collector import MetricsCollector, _read_cpu_usec

#: A fixed wall-clock "now" for the store, so persisted rows have known stamps.
NOW = 1_700_002_800


class FrozenClock:
    """A clock the test moves by hand."""

    def __init__(self, now: float = 0.0) -> None:
        self.now = float(now)

    def advance(self, seconds: float) -> None:
        self.now += seconds

    def __call__(self) -> float:
        return self.now


class FakePsutil:
    """Stands in for psutil, with counters the test moves by hand."""

    def __init__(self) -> None:
        self.cpu = 12.5
        self.bytes_recv = 10_000
        self.bytes_sent = 20_000

    def cpu_percent(self, interval: Any = None) -> float:
        return self.cpu

    def virtual_memory(self) -> Any:
        return SimpleNamespace(used=512 * 1024, total=1024 * 1024)

    def swap_memory(self) -> Any:
        return SimpleNamespace(used=64 * 1024)

    def disk_usage(self, path: str) -> Any:
        return SimpleNamespace(used=30_000, total=100_000)

    def net_io_counters(self) -> Any:
        return SimpleNamespace(bytes_recv=self.bytes_recv, bytes_sent=self.bytes_sent)


class FakeApp:
    """An application row, with only what the collector reads off it."""

    def __init__(self, domain: str) -> None:
        self.domain = domain


class FakeAppStore:
    """Stands in for the WASM store's application list."""

    def __init__(self, domains: list[str]) -> None:
        self.domains = domains

    def list_apps(self) -> list[FakeApp]:
        return [FakeApp(domain) for domain in self.domains]


@pytest.fixture
def clock() -> FrozenClock:
    """Provide a monotonic clock frozen at zero."""
    return FrozenClock()


@pytest.fixture
def store(tmp_path: Path) -> MetricsStore:
    """Provide a metrics store on a throwaway database."""
    return MetricsStore(tmp_path / "metrics.db", clock=lambda: NOW)


@pytest.fixture
def fake_psutil(monkeypatch: pytest.MonkeyPatch) -> FakePsutil:
    """Replace the collector's psutil with counters the test controls."""
    fake = FakePsutil()
    monkeypatch.setattr(metrics_collector, "psutil", fake)
    monkeypatch.setattr(metrics_collector.os, "getloadavg", lambda: (0.42, 0.2, 0.1))
    return fake


@pytest.fixture
def domains(monkeypatch: pytest.MonkeyPatch) -> FakeAppStore:
    """Give the collector a deterministic application list."""
    fake = FakeAppStore([])
    monkeypatch.setattr("wasm.core.store.get_store", lambda: fake)
    return fake


@pytest.fixture
def collector(
    store: MetricsStore,
    clock: FrozenClock,
    tmp_path: Path,
    fake_psutil: FakePsutil,
    domains: FakeAppStore,
) -> MetricsCollector:
    """Build a collector wired entirely to fakes."""
    return MetricsCollector(store, cgroup_root=tmp_path / "cgroup", clock=clock)


def write_cgroup(root: Path, domain: str, *, usage_usec: int, memory: int) -> Path:
    """
    Lay out one unit's cgroup files the way systemd does.

    Args:
        root: The fake cgroup root.
        domain: The application domain.
        usage_usec: Cumulative CPU time for cpu.stat.
        memory: Resident bytes for memory.current.

    Returns:
        The unit's cgroup directory.
    """
    unit = root / f"wasm-{domain}.service"
    unit.mkdir(parents=True, exist_ok=True)
    (unit / "cpu.stat").write_text(
        f"usage_usec {usage_usec}\nuser_usec {usage_usec // 2}\nsystem_usec {usage_usec // 2}\n"
    )
    (unit / "memory.current").write_text(f"{memory}\n")
    return unit


# ---------------------------------------------------------------------------
# System metrics
# ---------------------------------------------------------------------------


def test_gauges_are_recorded_every_tick(collector: MetricsCollector, store: MetricsStore) -> None:
    """The plain readings, totals included, land in the store and the snapshot."""
    snapshot = collector.sample_once()

    assert snapshot["cpu.percent"] == 12.5
    assert snapshot["mem.used_bytes"] == 512 * 1024
    assert snapshot["mem.total_bytes"] == 1024 * 1024
    assert snapshot["swap.used_bytes"] == 64 * 1024
    assert snapshot["disk.used_bytes"] == 30_000
    assert snapshot["disk.total_bytes"] == 100_000
    assert snapshot["load.1m"] == 0.42
    assert store.query("cpu.percent", window_s=60) == [(NOW, 12.5)]


def test_network_rates_are_deltas_over_the_injected_clock(
    collector: MetricsCollector, clock: FrozenClock, fake_psutil: FakePsutil
) -> None:
    """bytes_recv grows by 4096 over 2 seconds, so the rate is 2048 B/s."""
    first = collector.sample_once()

    clock.advance(2.0)
    fake_psutil.bytes_recv += 4096
    fake_psutil.bytes_sent += 1024
    second = collector.sample_once()

    assert "net.rx_bytes_s" not in first, "a first sample has no delta to rate"
    assert second["net.rx_bytes_s"] == 2048.0
    assert second["net.tx_bytes_s"] == 512.0


def test_a_counter_reset_does_not_become_a_negative_rate(
    collector: MetricsCollector, clock: FrozenClock, fake_psutil: FakePsutil
) -> None:
    """An interface bounce resets kernel counters; that is a gap, not a spike."""
    collector.sample_once()
    clock.advance(2.0)
    fake_psutil.bytes_recv = 0

    snapshot = collector.sample_once()

    assert "net.rx_bytes_s" not in snapshot


def test_without_psutil_the_system_is_skipped_and_apps_still_sample(
    collector: MetricsCollector,
    clock: FrozenClock,
    tmp_path: Path,
    domains: FakeAppStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """psutil is an optional extra; its absence must not blank the app charts."""
    monkeypatch.setattr(metrics_collector, "psutil", None)
    domains.domains = ["example.com"]
    write_cgroup(tmp_path / "cgroup", "example.com", usage_usec=0, memory=2048)

    snapshot = collector.sample_once()

    assert snapshot == {"app.example.com.mem.bytes": 2048.0}


# ---------------------------------------------------------------------------
# Per-application metrics
# ---------------------------------------------------------------------------


def test_app_cpu_percent_is_a_delta_over_the_cgroup_counter(
    collector: MetricsCollector, clock: FrozenClock, tmp_path: Path, domains: FakeAppStore
) -> None:
    """One second of CPU time over two seconds of wall time is 50 percent."""
    domains.domains = ["example.com"]
    root = tmp_path / "cgroup"
    write_cgroup(root, "example.com", usage_usec=1_000_000, memory=1024)
    first = collector.sample_once()

    clock.advance(2.0)
    write_cgroup(root, "example.com", usage_usec=2_000_000, memory=4096)
    second = collector.sample_once()

    assert "app.example.com.cpu.percent" not in first, "a first sample has no delta"
    assert first["app.example.com.mem.bytes"] == 1024.0
    assert second["app.example.com.cpu.percent"] == 50.0
    assert second["app.example.com.mem.bytes"] == 4096.0


def test_an_app_without_a_cgroup_is_skipped_without_error(
    collector: MetricsCollector, clock: FrozenClock, tmp_path: Path, domains: FakeAppStore
) -> None:
    """A stopped unit, cgroup v1 or a container: no files, no metrics, no noise."""
    domains.domains = ["present.com", "absent.com"]
    write_cgroup(tmp_path / "cgroup", "present.com", usage_usec=500, memory=1024)

    snapshot = collector.sample_once()

    assert "app.present.com.mem.bytes" in snapshot
    assert not any("absent.com" in metric for metric in snapshot)


def test_a_restarted_unit_does_not_produce_a_negative_cpu_rate(
    collector: MetricsCollector, clock: FrozenClock, tmp_path: Path, domains: FakeAppStore
) -> None:
    """After a restart the counter begins again at zero; that tick has no rate."""
    domains.domains = ["example.com"]
    root = tmp_path / "cgroup"
    write_cgroup(root, "example.com", usage_usec=5_000_000, memory=1024)
    collector.sample_once()

    clock.advance(2.0)
    write_cgroup(root, "example.com", usage_usec=100, memory=1024)
    snapshot = collector.sample_once()

    assert "app.example.com.cpu.percent" not in snapshot


def test_a_unit_that_disappears_and_returns_starts_its_delta_over(
    collector: MetricsCollector, clock: FrozenClock, tmp_path: Path, domains: FakeAppStore
) -> None:
    """The old counter is forgotten while the cgroup is gone, so the first
    sample after it returns is a baseline, not a rate against a former life."""
    domains.domains = ["example.com"]
    root = tmp_path / "cgroup"
    unit = write_cgroup(root, "example.com", usage_usec=1_000_000, memory=1024)
    collector.sample_once()

    clock.advance(2.0)
    (unit / "cpu.stat").unlink()
    collector.sample_once()

    clock.advance(2.0)
    write_cgroup(root, "example.com", usage_usec=9_000_000, memory=1024)
    snapshot = collector.sample_once()

    assert "app.example.com.cpu.percent" not in snapshot


def test_the_application_list_is_cached_between_refreshes(
    collector: MetricsCollector, clock: FrozenClock, tmp_path: Path, domains: FakeAppStore
) -> None:
    """The store is asked on a slow timer, not once per two-second tick."""
    domains.domains = ["old.com"]
    root = tmp_path / "cgroup"
    write_cgroup(root, "old.com", usage_usec=1, memory=1)
    write_cgroup(root, "new.com", usage_usec=1, memory=1)
    collector.sample_once()

    domains.domains = ["new.com"]
    clock.advance(2.0)
    within_window = collector.sample_once()
    clock.advance(metrics_collector.APPS_REFRESH_SECONDS)
    after_refresh = collector.sample_once()

    assert "app.old.com.mem.bytes" in within_window
    assert "app.new.com.mem.bytes" not in within_window
    assert "app.new.com.mem.bytes" in after_refresh
    assert "app.old.com.mem.bytes" not in after_refresh


# ---------------------------------------------------------------------------
# Operational failures do not stop the tick
# ---------------------------------------------------------------------------


def test_a_failing_psutil_does_not_stop_the_tick(
    collector: MetricsCollector, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A transient system read error costs the reading, not the thread."""

    class Broken:
        def cpu_percent(self, interval: Any = None) -> float:
            raise OSError("proc went away")

    monkeypatch.setattr(metrics_collector, "psutil", Broken())

    assert collector.sample_once() == {}


def test_a_failing_store_does_not_stop_the_tick(
    collector: MetricsCollector, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Persistence failing must not take the live snapshot down with it."""

    def refuse(pairs: Any, **kwargs: Any) -> None:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(collector.store, "record_many", refuse)

    snapshot = collector.sample_once()

    assert snapshot["cpu.percent"] == 12.5
    assert collector.latest() == snapshot


def test_a_failing_app_store_keeps_the_previous_domain_list(
    collector: MetricsCollector,
    clock: FrozenClock,
    tmp_path: Path,
    domains: FakeAppStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A transient store error must not blank every application chart."""
    domains.domains = ["example.com"]
    write_cgroup(tmp_path / "cgroup", "example.com", usage_usec=1, memory=1024)
    collector.sample_once()

    def refuse() -> Any:
        raise sqlite3.OperationalError("store unavailable")

    monkeypatch.setattr("wasm.core.store.get_store", refuse)
    clock.advance(metrics_collector.APPS_REFRESH_SECONDS + 1)
    snapshot = collector.sample_once()

    assert "app.example.com.mem.bytes" in snapshot


# ---------------------------------------------------------------------------
# Consolidation
# ---------------------------------------------------------------------------


def test_the_store_is_consolidated_on_its_timer(
    collector: MetricsCollector, clock: FrozenClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Retention has no scheduler; the sampling loop is what drives it."""
    calls: list[float] = []
    monkeypatch.setattr(collector.store, "consolidate", lambda **kwargs: calls.append(clock.now))

    collector.sample_once()
    assert calls == [], "consolidation must wait for its interval"

    clock.advance(metrics_collector.CONSOLIDATE_SECONDS)
    collector.sample_once()
    assert len(calls) == 1

    clock.advance(2.0)
    collector.sample_once()
    assert len(calls) == 1, "consolidation must not run on every tick"


# ---------------------------------------------------------------------------
# The thread and the snapshot
# ---------------------------------------------------------------------------


def test_start_and_stop_are_clean_and_idempotent(collector: MetricsCollector) -> None:
    """stop() ends the thread; calling either twice is a no-op, not an error."""
    collector.interval_s = 0.01
    collector.start()
    thread = collector._thread
    assert thread is not None and thread.is_alive()

    collector.start()
    assert collector._thread is thread, "a second start must not spawn a second thread"

    collector.stop()
    assert not thread.is_alive()
    assert collector._thread is None
    collector.stop()


def test_the_snapshot_is_a_copy(collector: MetricsCollector) -> None:
    """A caller holding the snapshot must not be able to edit the collector's."""
    collector.sample_once()

    held = collector.latest()
    held["cpu.percent"] = -1.0

    assert collector.latest()["cpu.percent"] == 12.5


def test_the_snapshot_can_be_read_while_the_collector_ticks(
    collector: MetricsCollector, clock: FrozenClock
) -> None:
    """latest() is called from the event loop while the thread samples."""
    errors: list[BaseException] = []
    stop = threading.Event()

    def read_constantly() -> None:
        while not stop.is_set():
            try:
                snapshot = collector.latest()
                assert isinstance(snapshot, dict)
            except Exception as exc:
                errors.append(exc)
                return

    reader = threading.Thread(target=read_constantly)
    reader.start()
    try:
        for _ in range(200):
            clock.advance(2.0)
            collector.sample_once()
    finally:
        stop.set()
        reader.join(timeout=5)

    assert errors == []


# ---------------------------------------------------------------------------
# cpu.stat parsing
# ---------------------------------------------------------------------------


def test_cpu_stat_parsing(tmp_path: Path) -> None:
    """The counter is read by name, wherever it sits in the file."""
    stat = tmp_path / "cpu.stat"
    stat.write_text("nr_periods 3\nusage_usec 12345\nuser_usec 900\n")

    assert _read_cpu_usec(stat) == 12345


@pytest.mark.parametrize("content", ["", "user_usec 900\n", "usage_usec not-a-number\n"])
def test_cpu_stat_without_a_usable_counter_reads_as_none(tmp_path: Path, content: str) -> None:
    """A malformed file is a skipped metric, never an exception."""
    stat = tmp_path / "cpu.stat"
    stat.write_text(content)

    assert _read_cpu_usec(stat) is None


def test_a_missing_cpu_stat_reads_as_none(tmp_path: Path) -> None:
    """The common case on cgroup v1, in containers and for stopped units."""
    assert _read_cpu_usec(tmp_path / "cpu.stat") is None


# ---------------------------------------------------------------------------
# The process-wide wiring
# ---------------------------------------------------------------------------


def test_start_and_stop_wire_and_clear_the_singleton(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_psutil: FakePsutil,
    domains: FakeAppStore,
) -> None:
    """What the lifespan calls: start exposes the collector, stop retires it."""
    monkeypatch.setattr(metrics_collector, "_store", None)
    monkeypatch.setattr(metrics_collector, "_collector", None)
    monkeypatch.setattr(
        metrics_collector, "default_metrics_db_path", lambda: tmp_path / "metrics.db"
    )

    started = metrics_collector.start_metrics_collector()
    try:
        assert started is not None
        assert metrics_collector.get_metrics_collector() is started
        assert metrics_collector.start_metrics_collector() is started
    finally:
        metrics_collector.stop_metrics_collector()

    assert metrics_collector.get_metrics_collector() is None
