# Copyright (c) 2024-2026 Yago Lopez Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
Tests for the RRD-style metrics store.

Every test drives the store through an injected clock and an injected database
path: nothing here depends on the wall clock or writes outside the test's own
directory. Where a test needs to see the physical tables — that consolidation
really deleted the raw rows it aggregated, not merely stopped returning them —
it opens the SQLite file directly instead of trusting the API under test to
report on itself.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest

from wasm.monitor.timeseries import (
    HOUR_RESOLUTION,
    MINUTE_RESOLUTION,
    MetricsStore,
)

#: A fixed "now", divisible by 3600 so consolidation buckets land on round
#: timestamps the assertions can spell out.
NOW = 1_700_002_800

HOUR = 3_600
DAY = 86_400


class FrozenClock:
    """A clock the test moves by hand."""

    def __init__(self, now: float = NOW) -> None:
        self.now = float(now)

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock() -> FrozenClock:
    """Provide a clock frozen at :data:`NOW`."""
    return FrozenClock()


@pytest.fixture
def store(tmp_path: Path, clock: FrozenClock) -> MetricsStore:
    """Provide a store on a throwaway database with the frozen clock."""
    return MetricsStore(tmp_path / "metrics.db", clock=clock)


def physical_tables(store: MetricsStore) -> tuple[list[Any], list[Any]]:
    """Read both tables straight from the file, bypassing the store."""
    conn = sqlite3.connect(str(store.db_path))
    try:
        samples = conn.execute(
            "SELECT metric, ts, value FROM samples ORDER BY metric, ts"
        ).fetchall()
        consolidated = conn.execute(
            "SELECT metric, resolution, ts, value FROM consolidated ORDER BY metric, resolution, ts"
        ).fetchall()
    finally:
        conn.close()
    return samples, consolidated


def test_record_and_query_roundtrip_in_time_order(store: MetricsStore) -> None:
    """Samples come back sorted by timestamp regardless of insertion order."""
    store.record("cpu", 3.0, ts=NOW - 10)
    store.record("cpu", 1.0, ts=NOW - 30)
    store.record("cpu", 2.0, ts=NOW - 20)

    assert store.query("cpu", window_s=HOUR) == [
        (NOW - 30, 1.0),
        (NOW - 20, 2.0),
        (NOW - 10, 3.0),
    ]


def test_record_stamps_with_the_injected_clock(store: MetricsStore) -> None:
    """A sample recorded without a timestamp gets the clock's time, not the wall's."""
    store.record("cpu", 1.5)

    assert store.query("cpu", window_s=60) == [(NOW, 1.5)]


def test_record_many_writes_one_tick_at_one_timestamp(store: MetricsStore) -> None:
    """One collector tick lands every metric on the same timestamp."""
    store.record_many([("cpu", 1.0), ("mem", 2.0)])

    assert store.query("cpu", window_s=60) == [(NOW, 1.0)]
    assert store.query("mem", window_s=60) == [(NOW, 2.0)]


def test_recording_the_same_second_twice_keeps_the_last_value(store: MetricsStore) -> None:
    """A duplicate (metric, ts) overwrites instead of raising: last write wins."""
    store.record("cpu", 1.0, ts=NOW - 5)
    store.record("cpu", 9.0, ts=NOW - 5)

    assert store.query("cpu", window_s=HOUR) == [(NOW - 5, 9.0)]


def test_query_respects_the_window(store: MetricsStore) -> None:
    """The window is the last window_s seconds, exclusive at the old end."""
    store.record("cpu", 1.0, ts=NOW - 4_000)
    store.record("cpu", 2.0, ts=NOW - HOUR)  # exactly window_s ago: outside
    store.record("cpu", 3.0, ts=NOW - HOUR + 1)
    store.record("cpu", 4.0, ts=NOW - 10)

    assert store.query("cpu", window_s=HOUR) == [(NOW - HOUR + 1, 3.0), (NOW - 10, 4.0)]


def test_query_rejects_a_nonpositive_window(store: MetricsStore) -> None:
    """A window of zero seconds is a caller bug, not an empty answer."""
    with pytest.raises(ValueError):
        store.query("cpu", window_s=0)


def test_consolidate_rolls_raw_samples_older_than_an_hour_into_minute_means(
    store: MetricsStore,
) -> None:
    """Old raw samples become exact per-minute means and the raws are deleted."""
    minute = NOW - 2 * HOUR
    store.record("cpu", 1.0, ts=minute)
    store.record("cpu", 2.0, ts=minute + 2)
    store.record("cpu", 3.0, ts=minute + 4)
    store.record("cpu", 10.0, ts=minute + 61)
    store.record("cpu", 20.0, ts=minute + 63)
    store.record("cpu", 5.0, ts=NOW - 30)  # young: must stay raw

    store.consolidate()

    samples, consolidated = physical_tables(store)
    assert samples == [("cpu", NOW - 30, 5.0)]
    assert consolidated == [
        ("cpu", MINUTE_RESOLUTION, minute, pytest.approx(2.0)),
        ("cpu", MINUTE_RESOLUTION, minute + 60, pytest.approx(15.0)),
    ]


def test_consolidate_twice_changes_nothing(store: MetricsStore) -> None:
    """Consolidation is idempotent: a second run with the same clock is a no-op."""
    minute = NOW - 2 * HOUR
    for offset, value in ((0, 1.0), (2, 2.0), (61, 8.0)):
        store.record("cpu", value, ts=minute + offset)
    store.record("cpu", 5.0, ts=NOW - 30)

    store.consolidate(now=NOW)
    first = physical_tables(store)
    store.consolidate(now=NOW)

    assert physical_tables(store) == first


def test_consolidate_chains_minutes_into_hours_and_expires_after_thirty_days(
    store: MetricsStore,
) -> None:
    """One call rolls day-old data all the way to hour means and drops month-old data."""
    hour = NOW - 2 * DAY
    store.record("cpu", 10.0, ts=hour)
    store.record("cpu", 20.0, ts=hour + 60)
    store.record("cpu", 99.0, ts=NOW - 40 * DAY)  # past 30-day retention

    store.consolidate()

    samples, consolidated = physical_tables(store)
    assert samples == []
    assert consolidated == [("cpu", HOUR_RESOLUTION, hour, pytest.approx(15.0))]


def test_query_reads_the_tier_that_matches_the_window(store: MetricsStore) -> None:
    """Each window sees the tiers that cover it: raw, plus minutes, plus hours."""
    store.record("cpu", 3.0, ts=NOW - 2 * DAY)  # will end as an hour mean
    store.record("cpu", 2.0, ts=NOW - 2 * HOUR)  # will end as a minute mean
    store.record("cpu", 1.0, ts=NOW - 30)  # stays raw
    store.consolidate()

    assert store.query("cpu", window_s=HOUR) == [(NOW - 30, 1.0)]
    assert store.query("cpu", window_s=DAY) == [(NOW - 2 * HOUR, 2.0), (NOW - 30, 1.0)]
    assert store.query("cpu", window_s=30 * DAY) == [
        (NOW - 2 * DAY, 3.0),
        (NOW - 2 * HOUR, 2.0),
        (NOW - 30, 1.0),
    ]


def test_query_downsamples_to_max_points_with_bucket_means(store: MetricsStore) -> None:
    """A query over max_points returns exact means over fixed time buckets."""
    # 300 samples, one every 2 s, covering the last 600 s: ts NOW-598 .. NOW.
    for j in range(300):
        store.record("cpu", float(j), ts=NOW - 600 + 2 * (j + 1))

    intact = store.query("cpu", window_s=600, max_points=300)
    assert len(intact) == 300

    points = store.query("cpu", window_s=600, max_points=50)
    assert len(points) == 50
    # Bucket width is 12 s = 6 samples; each bucket keeps its first timestamp.
    assert points[0] == (NOW - 598, pytest.approx(2.5))
    assert points[-1] == (NOW - 10, pytest.approx(296.5))
    timestamps = [ts for ts, _ in points]
    assert timestamps == sorted(timestamps)


def test_metrics_stay_separate_through_consolidation(store: MetricsStore) -> None:
    """Metrics never mix, and a fully consolidated metric is still listed."""
    store.record("cpu", 1.0, ts=NOW - 2 * HOUR)
    store.record("mem", 5.0, ts=NOW - 2 * HOUR)
    store.record("cpu", 2.0, ts=NOW - 30)
    store.record("old_only", 7.0, ts=NOW - 2 * HOUR)

    store.consolidate()

    assert store.query("cpu", window_s=DAY) == [(NOW - 2 * HOUR, 1.0), (NOW - 30, 2.0)]
    assert store.query("mem", window_s=DAY) == [(NOW - 2 * HOUR, 5.0)]
    assert store.query("disk", window_s=DAY) == []
    assert store.list_metrics() == ["cpu", "mem", "old_only"]
