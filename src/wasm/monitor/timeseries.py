"""
RRD-style persistence for the panel's metric charts.

A collector thread samples the machine and every app unit every couple of
seconds; the dashboard asks for windows of an hour, a day or a month. Keeping
every raw sample for a month would be over a million rows per metric, almost
all of it invisible at chart resolution, so this store keeps three tiers the
way RRDtool does:

- raw samples for the last hour (:data:`RAW_RETENTION_SECONDS`),
- one-minute means for the last day (:data:`MINUTE_RETENTION_SECONDS`),
- one-hour means for the last thirty days (:data:`HOUR_RETENTION_SECONDS`),
  after which data is deleted, because the panel offers no window past it.

:meth:`MetricsStore.consolidate` moves data down the tiers with aggregated
``INSERT``/``DELETE`` statements — no per-row Python — and its cutoffs are
aligned to whole destination buckets, so only complete minutes and hours are
ever aggregated and running it again with the same clock is a no-op.

Because consolidation deletes exactly what it aggregates, the tiers partition
time: the newest hour is always raw, the rest of the newest day is minute
means, everything older is hour means. That is what lets
:meth:`MetricsStore.query` read a window as a union of tiers without ever
representing the same period twice — and why a day-wide chart still shows its
newest hour instead of a blank right edge.

The database is a sibling of ``observations.db`` and follows its concurrency
model: one connection per thread and WAL, so the collector's 2-second writes
never block a web handler's reads.
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time
from collections.abc import Callable, Iterable
from pathlib import Path

from wasm.core.logger import Logger

#: Where the database lives when WASM is installed system-wide.
SYSTEM_DB_PATH = Path("/var/lib/wasm/metrics.db")

#: Fallback for an unprivileged run, so a developer never writes to /var/lib.
USER_DB_RELATIVE_PATH = Path(".local/share/wasm/metrics.db")

#: A metric write or read must never block for long on the database.
BUSY_TIMEOUT = 10

#: How long raw samples are kept before rolling up to minute means.
RAW_RETENTION_SECONDS = 3_600

#: Bucket size of the first consolidation tier: one mean per minute.
MINUTE_RESOLUTION = 60

#: How long minute means are kept before rolling up to hour means.
MINUTE_RETENTION_SECONDS = 86_400

#: Bucket size of the second consolidation tier: one mean per hour.
HOUR_RESOLUTION = 3_600

#: How long hour means are kept before deletion.
HOUR_RETENTION_SECONDS = 30 * 86_400

#: Default cap on points a query returns; about one per pixel of a panel chart.
DEFAULT_MAX_POINTS = 400

#: Upsert rather than fail on a duplicate second: a collector that lands twice
#: on the same timestamp (clock step, NTP slew) is reporting a gauge, and the
#: newer reading is the truer one.
_INSERT_SAMPLE_SQL = """
    INSERT INTO samples (metric, ts, value)
    VALUES (?, ?, ?)
    ON CONFLICT (metric, ts) DO UPDATE SET value = excluded.value
"""

#: One window read across all three tiers. The :minutes and :hours flags are
#: how query() chooses its sources by window width; the tiers cover disjoint
#: time ranges (consolidation deletes what it aggregates), so UNION ALL never
#: counts a period twice.
_WINDOW_SQL = """
    SELECT ts, value FROM samples
    WHERE metric = :metric AND ts > :cutoff
    UNION ALL
    SELECT ts, value FROM consolidated
    WHERE :minutes AND metric = :metric AND resolution = :minute_resolution AND ts > :cutoff
    UNION ALL
    SELECT ts, value FROM consolidated
    WHERE :hours AND metric = :metric AND resolution = :hour_resolution AND ts > :cutoff
    ORDER BY ts
"""

#: Aggregate expired rows of a finer tier into destination-bucket means. The
#: caller aligns :cutoff to a destination bucket boundary, so every aggregated
#: bucket is complete; DO NOTHING keeps the existing mean should a
#: late-recorded sample try to rebuild one.
_ROLL_UP_SQL = """
    INSERT INTO consolidated (metric, resolution, ts, value)
    SELECT metric, :to_resolution, (ts / :to_resolution) * :to_resolution, AVG(value)
    FROM {source}
    WHERE {expired}
    GROUP BY metric, ts / :to_resolution
    ON CONFLICT (metric, resolution, ts) DO NOTHING
"""

_ROLL_RAW_TO_MINUTES_SQL = _ROLL_UP_SQL.format(source="samples", expired="ts < :cutoff")

_ROLL_MINUTES_TO_HOURS_SQL = _ROLL_UP_SQL.format(
    source="consolidated", expired="resolution = :from_resolution AND ts < :cutoff"
)


def default_metrics_db_path() -> Path:
    """
    Choose the database location for the current process.

    Resolved on every call rather than at import time, so a changed HOME (a
    test sandbox, a service switching user) is honoured.

    Returns:
        The system path when its directory is writable, the user path otherwise.
    """
    system_dir = SYSTEM_DB_PATH.parent
    if system_dir.is_dir() and os.access(system_dir, os.W_OK):
        return SYSTEM_DB_PATH
    return Path.home() / USER_DB_RELATIVE_PATH


class MetricsStore:
    """
    SQLite time series with RRD-style retention.

    One connection per thread, because the collector writes the same file the
    web handlers read.
    """

    SCHEMA_VERSION = 1

    def __init__(
        self,
        db_path: Path | None = None,
        *,
        clock: Callable[[], float] = time.time,
        verbose: bool = False,
    ) -> None:
        """
        Args:
            db_path: Database file. Defaults to :func:`default_metrics_db_path`.
            clock: Source of the current time in epoch seconds. Injected so
                tests never depend on the wall clock.
            verbose: Enable verbose logging.
        """
        self.logger = Logger(verbose=verbose)
        self.db_path = db_path or default_metrics_db_path()
        self._clock = clock
        self._local = threading.local()
        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        """
        Return this thread's connection, opening it on first use.

        Returns:
            An open SQLite connection with row access by name.
        """
        connection = getattr(self._local, "connection", None)
        if connection is None:
            connection = sqlite3.connect(str(self.db_path), timeout=BUSY_TIMEOUT)
            connection.row_factory = sqlite3.Row
            # The collector writes this file while the web panel reads it;
            # without WAL one of them gets "database is locked" instead of an
            # answer.
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=NORMAL")
            self._local.connection = connection
        return connection

    def _init_db(self) -> None:
        """Create the schema if this is a fresh database."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        conn = self._get_connection()
        with conn:
            # WITHOUT ROWID: the primary key is the whole access pattern, and
            # a hidden rowid would double every index for nothing.
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS samples (
                    metric TEXT NOT NULL,
                    ts INTEGER NOT NULL,
                    value REAL NOT NULL,
                    PRIMARY KEY (metric, ts)
                ) WITHOUT ROWID
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS consolidated (
                    metric TEXT NOT NULL,
                    resolution INTEGER NOT NULL,
                    ts INTEGER NOT NULL,
                    value REAL NOT NULL,
                    PRIMARY KEY (metric, resolution, ts)
                ) WITHOUT ROWID
                """
            )

        self.logger.debug(f"Metrics store ready at {self.db_path}")

    def record(self, metric: str, value: float, *, ts: int | None = None) -> None:
        """
        Store one sample.

        Args:
            metric: Metric name, e.g. ``system.cpu`` or ``app.example.com.mem``.
            value: The sampled value. Must be a real number: SQLite stores NaN
                as NULL, which the schema rejects, loudly.
            ts: Sample time in epoch seconds. Defaults to the injected clock.
        """
        stamp = int(self._clock()) if ts is None else int(ts)
        conn = self._get_connection()
        with conn:
            conn.execute(_INSERT_SAMPLE_SQL, (metric, stamp, float(value)))

    def record_many(self, pairs: Iterable[tuple[str, float]], *, ts: int | None = None) -> None:
        """
        Store one collector tick: many metrics, one timestamp, one transaction.

        Args:
            pairs: ``(metric, value)`` pairs sampled together.
            ts: Sample time in epoch seconds. Defaults to the injected clock.
        """
        stamp = int(self._clock()) if ts is None else int(ts)
        rows = [(metric, stamp, float(value)) for metric, value in pairs]
        if not rows:
            return
        conn = self._get_connection()
        with conn:
            conn.executemany(_INSERT_SAMPLE_SQL, rows)

    def query(
        self, metric: str, *, window_s: int, max_points: int = DEFAULT_MAX_POINTS
    ) -> list[tuple[int, float]]:
        """
        Read one metric over the last ``window_s`` seconds, oldest first.

        The sources are chosen by window width: raw samples always, plus the
        minute tier for windows past an hour, plus the hour tier for windows
        past a day. When the window still holds more than ``max_points``
        points, they are reduced on the fly to means over fixed time buckets.

        Args:
            metric: Metric name.
            window_s: Width of the window ending now, in seconds. The window
                is half-open: a point exactly ``window_s`` old is excluded.
            max_points: Most points to return.

        Returns:
            ``(ts, value)`` pairs sorted by timestamp.

        Raises:
            ValueError: If ``window_s`` or ``max_points`` is not positive.
        """
        if window_s <= 0:
            raise ValueError(f"window_s must be positive, got {window_s}")
        if max_points <= 0:
            raise ValueError(f"max_points must be positive, got {max_points}")

        window = int(window_s)
        cutoff = int(self._clock()) - window
        rows = self._get_connection().execute(
            _WINDOW_SQL,
            {
                "metric": metric,
                "cutoff": cutoff,
                "minutes": int(window > RAW_RETENTION_SECONDS),
                "hours": int(window > MINUTE_RETENTION_SECONDS),
                "minute_resolution": MINUTE_RESOLUTION,
                "hour_resolution": HOUR_RESOLUTION,
            },
        )
        points = [(int(row[0]), float(row[1])) for row in rows]
        if len(points) <= max_points:
            return points
        return _bucket_means(points, cutoff=cutoff, window_s=window, max_points=max_points)

    def consolidate(self, *, now: int | None = None) -> None:
        """
        Move data down the tiers and drop what fell off the end.

        Raw samples past their retention become minute means, minute means
        past theirs become hour means, hour means past thirty days are
        deleted. Aggregation is one ``INSERT``/``SELECT`` per tier; the
        cutoffs are aligned down to whole destination buckets, so a bucket is
        only ever aggregated once it can no longer grow — which is what makes
        a second run with the same clock a no-op.

        Args:
            now: The current time in epoch seconds. Defaults to the injected
                clock.
        """
        now_ts = int(self._clock()) if now is None else int(now)
        raw_cutoff = (now_ts - RAW_RETENTION_SECONDS) // MINUTE_RESOLUTION * MINUTE_RESOLUTION
        minute_cutoff = (now_ts - MINUTE_RETENTION_SECONDS) // HOUR_RESOLUTION * HOUR_RESOLUTION
        expiry = now_ts - HOUR_RETENTION_SECONDS

        conn = self._get_connection()
        with conn:
            conn.execute(
                _ROLL_RAW_TO_MINUTES_SQL,
                {"to_resolution": MINUTE_RESOLUTION, "cutoff": raw_cutoff},
            )
            raw_dropped = conn.execute("DELETE FROM samples WHERE ts < ?", (raw_cutoff,)).rowcount
            conn.execute(
                _ROLL_MINUTES_TO_HOURS_SQL,
                {
                    "from_resolution": MINUTE_RESOLUTION,
                    "to_resolution": HOUR_RESOLUTION,
                    "cutoff": minute_cutoff,
                },
            )
            minutes_dropped = conn.execute(
                "DELETE FROM consolidated WHERE resolution = ? AND ts < ?",
                (MINUTE_RESOLUTION, minute_cutoff),
            ).rowcount
            expired = conn.execute(
                "DELETE FROM consolidated WHERE resolution = ? AND ts < ?",
                (HOUR_RESOLUTION, expiry),
            ).rowcount

        if raw_dropped or minutes_dropped or expired:
            self.logger.debug(
                f"Consolidated {raw_dropped} raw sample(s) and {minutes_dropped} "
                f"minute mean(s); expired {expired} hour mean(s)"
            )

    def list_metrics(self) -> list[str]:
        """
        Name every metric with data in any tier.

        Returns:
            Metric names, sorted.
        """
        rows = self._get_connection().execute(
            "SELECT metric FROM samples UNION SELECT metric FROM consolidated ORDER BY metric"
        )
        return [str(row[0]) for row in rows]


def _bucket_means(
    points: list[tuple[int, float]], *, cutoff: int, window_s: int, max_points: int
) -> list[tuple[int, float]]:
    """
    Reduce ordered points to at most ``max_points`` bucket means.

    Buckets are fixed time slices of the window rather than equal shares of
    the points, so a stretch of dense raw data thins evenly instead of
    crowding the sparse consolidated stretch off the chart. Each bucket keeps
    the timestamp of its first real point. The row count here is bounded by
    the tiers (a few thousand at worst), so plain Python is cheap.

    Args:
        points: ``(ts, value)`` pairs sorted by timestamp, all with
            ``ts > cutoff``.
        cutoff: Exclusive lower bound of the window.
        window_s: Width of the window in seconds.
        max_points: Most buckets to produce.

    Returns:
        ``(ts, mean)`` pairs sorted by timestamp.
    """
    # Ceiling division; with ts - cutoff in [1, window_s] the bucket index
    # stays under max_points, so the cap is exact.
    width = -(-window_s // max_points)
    means: list[tuple[int, float]] = []
    bucket = -1
    bucket_ts = 0
    total = 0.0
    count = 0
    for ts, value in points:
        index = (ts - cutoff - 1) // width
        if index != bucket:
            if count:
                means.append((bucket_ts, total / count))
            bucket = index
            bucket_ts = ts
            total = 0.0
            count = 0
        total += value
        count += 1
    if count:
        means.append((bucket_ts, total / count))
    return means
