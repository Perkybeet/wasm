"""
Persistence for monitor observations.

This replaces the old threat store. The rows are the same shape as before minus
the fiction: there is no "action taken" column, because the monitor takes no
actions, and nothing is "resolved", because nothing was ever a case. An
observation is either still interesting or acknowledged by an operator.
"""

from __future__ import annotations

import os
import sqlite3
import threading
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from wasm.core.logger import Logger
from wasm.monitor.models import ProcessObservation

#: Where the database lives when WASM is installed system-wide.
SYSTEM_DB_PATH = Path("/var/lib/wasm/observations.db")

#: Fallback for an unprivileged run, so a developer never writes to /var/lib.
USER_DB_RELATIVE_PATH = Path(".local/share/wasm/observations.db")

#: A monitor scan must never block on the database.
BUSY_TIMEOUT = 10


def default_db_path() -> Path:
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


class ObservationStore:
    """
    SQLite log of what the monitor noticed.

    One connection per thread, because the web UI reads the same file the
    monitor daemon writes.
    """

    SCHEMA_VERSION = 1

    def __init__(self, db_path: Path | None = None, verbose: bool = False) -> None:
        """
        Args:
            db_path: Database file. Defaults to :func:`default_db_path`.
            verbose: Enable verbose logging.
        """
        self.logger = Logger(verbose=verbose)
        self.db_path = db_path or default_db_path()
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
            self._local.connection = connection
        return connection

    def _init_db(self) -> None:
        """Create the schema if this is a fresh database."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        conn = self._get_connection()
        with conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS observations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    observed_at TEXT NOT NULL,
                    pid INTEGER NOT NULL,
                    process_name TEXT NOT NULL,
                    user TEXT,
                    cpu_percent REAL,
                    memory_percent REAL,
                    command TEXT,
                    signal TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    detail TEXT,
                    parent_pid INTEGER,
                    parent_name TEXT,
                    acknowledged INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_observations_time ON observations(observed_at DESC)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_observations_severity ON observations(severity)"
            )

        self.logger.debug(f"Observation store ready at {self.db_path}")

    def save(self, observation: ProcessObservation) -> int:
        """
        Store one observation.

        Args:
            observation: The observation to record.

        Returns:
            The row id of the stored observation.
        """
        process = observation.process
        conn = self._get_connection()
        with conn:
            cursor = conn.execute(
                """
                INSERT INTO observations
                    (observed_at, pid, process_name, user, cpu_percent, memory_percent,
                     command, signal, severity, detail, parent_pid, parent_name)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    (observation.observed_at or datetime.now()).isoformat(),
                    process.pid,
                    process.name,
                    process.user,
                    process.cpu_percent,
                    process.memory_percent,
                    process.command,
                    observation.signal,
                    observation.severity,
                    observation.detail,
                    process.parent_pid,
                    process.parent_name,
                ),
            )
        return int(cursor.lastrowid or 0)

    def save_many(self, observations: Sequence[ProcessObservation]) -> list[int]:
        """
        Store several observations.

        Args:
            observations: The observations to record.

        Returns:
            The row ids, in the order given.
        """
        return [self.save(observation) for observation in observations]

    def recent(
        self,
        limit: int = 50,
        include_acknowledged: bool = False,
        severity: str | None = None,
    ) -> list[dict[str, Any]]:
        """
        Read the most recent observations.

        Args:
            limit: Maximum number of rows to return.
            include_acknowledged: Include rows an operator already dismissed.
            severity: Restrict to one severity.

        Returns:
            Rows as dictionaries, newest first.
        """
        query = "SELECT * FROM observations WHERE 1=1"
        params: list[Any] = []

        if not include_acknowledged:
            query += " AND acknowledged = 0"
        if severity:
            query += " AND severity = ?"
            params.append(severity)

        query += " ORDER BY observed_at DESC LIMIT ?"
        params.append(limit)

        rows = self._get_connection().execute(query, params).fetchall()
        return [dict(row) for row in rows]

    def get(self, observation_id: int) -> dict[str, Any] | None:
        """
        Read one observation.

        Args:
            observation_id: Row id to look up.

        Returns:
            The row as a dictionary, or None when it does not exist.
        """
        row = (
            self._get_connection()
            .execute("SELECT * FROM observations WHERE id = ?", (observation_id,))
            .fetchone()
        )
        return dict(row) if row else None

    def acknowledge(self, observation_id: int) -> bool:
        """
        Mark an observation as seen by an operator.

        Args:
            observation_id: Row id to acknowledge.

        Returns:
            True when a row was updated.
        """
        conn = self._get_connection()
        with conn:
            cursor = conn.execute(
                "UPDATE observations SET acknowledged = 1 WHERE id = ?",
                (observation_id,),
            )
        return cursor.rowcount > 0

    def stats(self) -> dict[str, int]:
        """
        Count observations by severity and acknowledgement.

        Returns:
            Totals keyed by counter name.
        """
        conn = self._get_connection()
        counts = {
            "total": "SELECT COUNT(*) FROM observations",
            "warning": "SELECT COUNT(*) FROM observations WHERE severity = 'warning'",
            "notice": "SELECT COUNT(*) FROM observations WHERE severity = 'notice'",
            "acknowledged": "SELECT COUNT(*) FROM observations WHERE acknowledged = 1",
            "open": "SELECT COUNT(*) FROM observations WHERE acknowledged = 0",
        }
        return {name: int(conn.execute(sql).fetchone()[0]) for name, sql in counts.items()}

    def purge_older_than(self, days: int) -> int:
        """
        Drop observations past the retention window.

        Args:
            days: Number of days to keep.

        Returns:
            Number of rows dropped.
        """
        conn = self._get_connection()
        with conn:
            cursor = conn.execute(
                "DELETE FROM observations WHERE datetime(observed_at) < datetime('now', ?)",
                (f"-{int(days)} days",),
            )
        dropped = cursor.rowcount
        if dropped > 0:
            self.logger.debug(f"Purged {dropped} observation(s) older than {days} days")
        return dropped
