"""
Persistent storage for detected threats.

Stores threat history in SQLite for audit and analysis purposes.
"""

import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from wasm.core.logger import Logger
from wasm.monitor.email_notifier import ThreatReport


# Storage paths (same pattern as core store)
DEFAULT_DB_PATH = Path("/var/lib/wasm/threats.db")
USER_DB_PATH = Path.home() / ".local/share/wasm/threats.db"


class ThreatStore:
    """SQLite-based threat history storage."""

    SCHEMA_VERSION = 1

    def __init__(self, db_path: Optional[Path] = None, verbose: bool = False):
        """
        Initialize threat store.

        Args:
            db_path: Path to database file. If None, uses system or user path.
            verbose: Enable verbose logging.
        """
        self.logger = Logger(verbose=verbose)
        self._local = threading.local()

        if db_path:
            self.db_path = db_path
        else:
            self.db_path = self._get_db_path()

        self._init_db()

    def _get_db_path(self) -> Path:
        """
        Get the database path to use.

        Tries system path first, falls back to user path.
        """
        # Try system path first
        if DEFAULT_DB_PATH.parent.exists():
            try:
                DEFAULT_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
                # Test if we can write
                test_file = DEFAULT_DB_PATH.parent / ".write_test"
                test_file.touch()
                test_file.unlink()
                return DEFAULT_DB_PATH
            except (PermissionError, OSError):
                pass

        # Fall back to user path
        USER_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        return USER_DB_PATH

    def _get_connection(self) -> sqlite3.Connection:
        """Get thread-local database connection."""
        if not hasattr(self._local, "connection") or self._local.connection is None:
            self._local.connection = sqlite3.connect(
                str(self.db_path),
                check_same_thread=False,
            )
            self._local.connection.row_factory = sqlite3.Row
        return self._local.connection

    def _init_db(self):
        """Initialize database schema."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        conn = self._get_connection()
        cursor = conn.cursor()

        # Create threats table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS threats (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                pid INTEGER NOT NULL,
                process_name TEXT NOT NULL,
                user TEXT,
                cpu_percent REAL,
                memory_percent REAL,
                command TEXT,
                threat_level TEXT NOT NULL,
                confidence REAL,
                reason TEXT,
                action_taken TEXT,
                parent_pid INTEGER,
                parent_name TEXT,
                resolved BOOLEAN DEFAULT 0
            )
        """)

        # Create index for faster queries
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_threats_timestamp
            ON threats(timestamp DESC)
        """)

        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_threats_level
            ON threats(threat_level)
        """)

        conn.commit()
        self.logger.debug(f"Threat store initialized at {self.db_path}")

    def save_threat(self, report: ThreatReport) -> int:
        """
        Save a threat report to the database.

        Args:
            report: The threat report to save.

        Returns:
            The ID of the saved threat.
        """
        conn = self._get_connection()
        cursor = conn.cursor()

        timestamp = (
            report.timestamp.isoformat()
            if report.timestamp
            else datetime.now().isoformat()
        )

        cursor.execute(
            """
            INSERT INTO threats
            (timestamp, pid, process_name, user, cpu_percent,
             memory_percent, command, threat_level, confidence,
             reason, action_taken, parent_pid, parent_name)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
            (
                timestamp,
                report.pid,
                report.process_name,
                report.user,
                report.cpu_percent,
                report.memory_percent,
                report.command,
                report.threat_level,
                report.confidence,
                report.reason,
                report.action_taken,
                report.parent_pid,
                report.parent_name,
            ),
        )

        conn.commit()
        threat_id = cursor.lastrowid

        self.logger.debug(
            f"Saved threat #{threat_id}: {report.process_name} "
            f"({report.threat_level})"
        )

        return threat_id

    def save_threats(self, reports: List[ThreatReport]) -> List[int]:
        """
        Save multiple threat reports.

        Args:
            reports: List of threat reports to save.

        Returns:
            List of saved threat IDs.
        """
        return [self.save_threat(report) for report in reports]

    def get_recent_threats(
        self,
        limit: int = 50,
        include_resolved: bool = False,
        threat_level: Optional[str] = None,
    ) -> List[dict]:
        """
        Get recent threats from history.

        Args:
            limit: Maximum number of threats to return.
            include_resolved: Include resolved threats.
            threat_level: Filter by threat level (malicious, suspicious).

        Returns:
            List of threat records as dictionaries.
        """
        conn = self._get_connection()
        cursor = conn.cursor()

        query = "SELECT * FROM threats WHERE 1=1"
        params = []

        if not include_resolved:
            query += " AND resolved = 0"

        if threat_level:
            query += " AND threat_level = ?"
            params.append(threat_level)

        query += " ORDER BY timestamp DESC LIMIT ?"
        params.append(limit)

        cursor.execute(query, params)
        rows = cursor.fetchall()

        return [dict(row) for row in rows]

    def get_threat_by_id(self, threat_id: int) -> Optional[dict]:
        """
        Get a specific threat by ID.

        Args:
            threat_id: The threat ID.

        Returns:
            Threat record as dictionary, or None if not found.
        """
        conn = self._get_connection()
        cursor = conn.cursor()

        cursor.execute("SELECT * FROM threats WHERE id = ?", (threat_id,))
        row = cursor.fetchone()

        return dict(row) if row else None

    def mark_resolved(self, threat_id: int) -> bool:
        """
        Mark a threat as resolved.

        Args:
            threat_id: The threat ID to mark as resolved.

        Returns:
            True if updated successfully.
        """
        conn = self._get_connection()
        cursor = conn.cursor()

        cursor.execute(
            "UPDATE threats SET resolved = 1 WHERE id = ?",
            (threat_id,),
        )
        conn.commit()

        updated = cursor.rowcount > 0
        if updated:
            self.logger.debug(f"Marked threat #{threat_id} as resolved")

        return updated

    def get_stats(self) -> dict:
        """
        Get threat statistics.

        Returns:
            Dictionary with threat statistics.
        """
        conn = self._get_connection()
        cursor = conn.cursor()

        stats = {
            "total": 0,
            "malicious": 0,
            "suspicious": 0,
            "resolved": 0,
            "unresolved": 0,
        }

        cursor.execute("SELECT COUNT(*) FROM threats")
        stats["total"] = cursor.fetchone()[0]

        cursor.execute(
            "SELECT COUNT(*) FROM threats WHERE threat_level = 'malicious'"
        )
        stats["malicious"] = cursor.fetchone()[0]

        cursor.execute(
            "SELECT COUNT(*) FROM threats WHERE threat_level = 'suspicious'"
        )
        stats["suspicious"] = cursor.fetchone()[0]

        cursor.execute("SELECT COUNT(*) FROM threats WHERE resolved = 1")
        stats["resolved"] = cursor.fetchone()[0]

        cursor.execute("SELECT COUNT(*) FROM threats WHERE resolved = 0")
        stats["unresolved"] = cursor.fetchone()[0]

        return stats

    def cleanup_old_threats(self, days: int = 30) -> int:
        """
        Remove threats older than specified days.

        Args:
            days: Number of days to keep.

        Returns:
            Number of deleted records.
        """
        conn = self._get_connection()
        cursor = conn.cursor()

        cursor.execute(
            """
            DELETE FROM threats
            WHERE datetime(timestamp) < datetime('now', ? || ' days')
        """,
            (f"-{days}",),
        )

        conn.commit()
        deleted = cursor.rowcount

        if deleted > 0:
            self.logger.info(f"Cleaned up {deleted} old threat records")

        return deleted
