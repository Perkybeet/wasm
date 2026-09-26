# Copyright (c) 2024-2025 Yago López Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
Tests that a store schema change is atomic.

``cursor.executescript`` commits any pending transaction before it runs, and
its own statements are never covered by a later ``rollback()`` - not even
when the transaction was opened explicitly with ``BEGIN``. A crash or a
failing statement partway through a migration used to leave the schema
half-applied while ``schema_version`` still reported the version before it,
or after it, depending on exactly where the process died. These tests build
the same frozen old-schema fixture databases as ``tests/test_store.py`` and
inject a failure mid-migration to pin the fix: a version's schema change and
its ``schema_version`` row commit or roll back together, and a version that
already committed is never undone by a later version failing in the same
opening.
"""

import sqlite3
from pathlib import Path

import pytest

from tests.test_store import V1_SCHEMA_SQL, V2_DEPLOYMENTS_SQL, V4_JOBS_SQL
from wasm.core import store as store_module
from wasm.core.fs import RecordingFileSystem
from wasm.core.store import SCHEMA_VERSION, WASMStore


@pytest.fixture
def fresh():
    """
    Guarantee a store singleton that this test owns.

    Yields:
        Nothing; the singleton is reset before and after the test so an
        injected filesystem is actually the one used.
    """
    WASMStore.reset_instance()
    yield
    WASMStore.reset_instance()


def _create_v3_database(db_path: Path) -> None:
    """
    Create a real v3 database with one app, as a 1.4.x release left it.

    Args:
        db_path: Where the database file is created.
    """
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(V1_SCHEMA_SQL)
        conn.executescript(V2_DEPLOYMENTS_SQL)
        conn.execute("ALTER TABLE apps ADD COLUMN webhook_secret TEXT")
        conn.execute("INSERT INTO schema_version (version) VALUES (1)")
        conn.execute("INSERT INTO schema_version (version) VALUES (2)")
        conn.execute("INSERT INTO schema_version (version) VALUES (3)")
        conn.execute(
            "INSERT INTO apps (domain, app_type, app_path) VALUES (?, ?, ?)",
            ("v3.example.com", "nextjs", "/var/www/apps/v3-example-com"),
        )
        conn.commit()
    finally:
        conn.close()


def _create_v7_database(db_path: Path) -> None:
    """
    Create a real v7 database with one deployment, as 2.0 pre-releases left it.

    Identical fixture to ``TestSchemaV8Migration._create_v7_database`` in
    ``tests/test_store.py``: a database at v7 is the exact input the v7-to-v8
    migration this file crashes mid-way through is meant to run against.

    Args:
        db_path: Where the database file is created.
    """
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(V1_SCHEMA_SQL)
        conn.executescript(V2_DEPLOYMENTS_SQL)
        conn.execute("ALTER TABLE apps ADD COLUMN webhook_secret TEXT")
        conn.executescript(V4_JOBS_SQL)
        conn.execute("ALTER TABLE jobs ADD COLUMN actor TEXT")
        for name, definition in store_module.APPS_V5_COLUMNS:
            conn.execute(f"ALTER TABLE apps ADD COLUMN {name} {definition}")
        conn.executescript(store_module.RELEASES_SCHEMA_SQL)
        conn.executescript(store_module.DOMAINS_SCHEMA_SQL)
        for version in (1, 2, 3, 4, 5, 6, 7):
            conn.execute("INSERT INTO schema_version (version) VALUES (?)", (version,))
        conn.execute(
            "INSERT INTO deployments (domain, status, triggered_by, started_at)"
            " VALUES (?, ?, ?, ?)",
            ("old.example.com", "success", "cli", "2026-01-02T03:04:05"),
        )
        conn.commit()
    finally:
        conn.close()


def _raw_tables(db_path: Path) -> set[str]:
    """
    Args:
        db_path: Database file to inspect, opened directly with a plain
            ``sqlite3`` connection - not through a store, so inspecting the
            outcome of a failed migration cannot itself trigger another one.

    Returns:
        Names of every table in the database.
    """
    conn = sqlite3.connect(db_path)
    try:
        return {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        conn.close()


def _raw_columns(db_path: Path, table: str) -> set[str]:
    """
    Args:
        db_path: Database file to inspect, opened directly.
        table: Table name.

    Returns:
        Column names of the given table.
    """
    conn = sqlite3.connect(db_path)
    try:
        return {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    finally:
        conn.close()


def _raw_max_version(db_path: Path) -> int:
    """
    Args:
        db_path: Database file to inspect, opened directly.

    Returns:
        The highest committed ``schema_version`` row, or 0 if none.
    """
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] or 0
    finally:
        conn.close()


class TestAFailingMigrationStepIsAtomic:
    """
    A statement failing partway through one version's migration must not
    leave that version half-applied, whether the failure is the first
    statement or, as here, comes after some of the version's own DDL already
    ran.
    """

    def test_a_failing_v7_to_v8_step_leaves_v7_intact_and_a_retry_completes_it(
        self, fresh, tmp_path
    ):
        """
        The real v7-to-v8 migration adds three columns to ``deployments``.
        Crashing right after the first must roll that column back too, not
        just leave ``schema_version`` at 7 while ``job_id`` quietly exists -
        a fix that only wraps the version row insert, and not the migration's
        own statements, would pass on the version number and fail on this.
        A later, real run must still take the untouched v7 database to v8.
        """
        db_path = tmp_path / "wasm.db"
        _create_v7_database(db_path)

        def _crash_after_first_statement(self: WASMStore, cursor: sqlite3.Cursor) -> None:
            cursor.execute("ALTER TABLE deployments ADD COLUMN job_id TEXT")
            raise sqlite3.OperationalError("simulated crash mid-migration")

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(WASMStore, "_migrate_v7_to_v8", _crash_after_first_statement)
            with pytest.raises(sqlite3.OperationalError):
                WASMStore(db_path, fs=RecordingFileSystem())

        # The failed attempt must not have left a trace: not the version
        # bump, and not the one column it managed to add before crashing.
        assert _raw_max_version(db_path) == 7
        assert "job_id" not in _raw_columns(db_path, "deployments")

        WASMStore.reset_instance()
        store = WASMStore(db_path, fs=RecordingFileSystem())

        with store._transaction() as cursor:
            cursor.execute("SELECT MAX(version) FROM schema_version")
            assert cursor.fetchone()[0] == SCHEMA_VERSION

        record = store.list_deployments("old.example.com")[0]
        assert record.job_id is None
        assert record.release_id is None
        assert record.commit_message is None


class TestAFailingStepDoesNotUndoEarlierCommittedSteps:
    """
    Per-version atomicity: a version's schema change and its
    ``schema_version`` row are committed as soon as that version's migration
    finishes, so a later version failing in the same climb rolls back only
    itself. A v3 database upgrading in one opening walks v4, v5 and v6 before
    it ever reaches the v6-to-v7 step this fails; those three must stay
    committed, and nothing from v7 or v8 must exist.
    """

    def test_a_failure_at_v6_to_v7_keeps_v4_v5_v6_committed(self, fresh, tmp_path):
        db_path = tmp_path / "wasm.db"
        _create_v3_database(db_path)

        def _crash(self: WASMStore, cursor: sqlite3.Cursor) -> None:
            raise sqlite3.OperationalError("simulated crash at v6-to-v7")

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(WASMStore, "_migrate_v6_to_v7", _crash)
            with pytest.raises(sqlite3.OperationalError):
                WASMStore(db_path, fs=RecordingFileSystem())

        assert _raw_max_version(db_path) == 6

        tables = _raw_tables(db_path)
        assert "jobs" in tables  # v3 -> v4
        assert "releases" in tables  # v4 -> v5
        assert "domains" in tables  # v5 -> v6
        assert "layout" in _raw_columns(db_path, "apps")  # v4 -> v5

        # Nothing from the step after the failing one leaked through. (The
        # jobs table is created directly from today's JOBS_SCHEMA_SQL at
        # v3->v4, which already carries the v7 "actor" column - see
        # _migrate_v6_to_v7's docstring - so "actor" existing here says
        # nothing about whether v6->v7 ran; job_id on deployments, added
        # only by v7->v8, is the step that could not possibly have run.)
        assert "job_id" not in _raw_columns(db_path, "deployments")  # v7 -> v8

        WASMStore.reset_instance()
        store = WASMStore(db_path, fs=RecordingFileSystem())

        with store._transaction() as cursor:
            cursor.execute("SELECT MAX(version) FROM schema_version")
            assert cursor.fetchone()[0] == SCHEMA_VERSION
        assert store.get_app("v3.example.com") is not None


class TestAFailingFreshInstallIsAtomic:
    """
    The fresh-install path runs ``SCHEMA_SQL`` and inserts the version row in
    one call, the same shape as a migration step, and must be exactly as
    atomic: a database that never got that far must not end up with some
    tables and no ``schema_version`` to say what happened.
    """

    def test_a_failing_fresh_install_leaves_no_tables_and_a_retry_completes_it(
        self, fresh, tmp_path
    ):
        db_path = tmp_path / "wasm.db"

        def _crash_after_one_table(self: WASMStore) -> None:
            with self._ddl_transaction() as cursor:
                cursor.execute(
                    "CREATE TABLE schema_version ("
                    "version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
                )
                raise sqlite3.OperationalError("simulated crash mid-install")

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(WASMStore, "_create_fresh_schema", _crash_after_one_table)
            with pytest.raises(sqlite3.OperationalError):
                WASMStore(db_path, fs=RecordingFileSystem())

        assert _raw_tables(db_path) == set()

        WASMStore.reset_instance()
        store = WASMStore(db_path, fs=RecordingFileSystem())

        with store._transaction() as cursor:
            cursor.execute("SELECT MAX(version) FROM schema_version")
            assert cursor.fetchone()[0] == SCHEMA_VERSION
        assert "apps" in _raw_tables(db_path)


def _create_v8_database(db_path: Path) -> None:
    """
    Create a real v8 database as 2.0.x leaves it, with data in every table v9 touches.

    One application with a non-default retention and limits, and one
    deployment with the v8 links filled in: the rows the v8-to-v9 migration
    must carry over unchanged while it adds its columns.

    Args:
        db_path: Where the database file is created.
    """
    _create_v7_database(db_path)
    conn = sqlite3.connect(db_path)
    try:
        for column in ("job_id", "release_id", "commit_message"):
            conn.execute(f"ALTER TABLE deployments ADD COLUMN {column} TEXT")
        conn.execute("INSERT INTO schema_version (version) VALUES (8)")
        conn.execute(
            "INSERT INTO apps (domain, app_type, app_path, layout, keep_releases, memory_max_mb)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            ("v8.example.com", "nodejs", "/var/www/apps/v8-example-com", "releases", 7, 512),
        )
        conn.execute(
            "UPDATE deployments SET job_id = 'ab12cd34', release_id = '20260101-000000-aaaaaaa'"
        )
        conn.commit()
    finally:
        conn.close()


class TestSchemaV9Migration:
    """
    Schema v9: the health gate's settings on every application, and the
    backup that holds exactly what a deployment produced.
    """

    def test_a_v8_database_keeps_its_rows_and_gains_empty_settings(self, fresh, tmp_path):
        """Every new column is NULL - "what WASM did before" - and nothing else moves."""
        db_path = tmp_path / "wasm.db"
        _create_v8_database(db_path)

        store = WASMStore(db_path, fs=RecordingFileSystem())

        assert _raw_max_version(db_path) == SCHEMA_VERSION == 9
        app = store.get_app("v8.example.com")
        assert app is not None
        assert (app.layout, app.keep_releases, app.memory_max_mb) == ("releases", 7, 512)
        assert (app.health_path, app.health_expect, app.health_timeout) == (None, None, None)
        record = store.list_deployments("old.example.com")[0]
        assert (record.job_id, record.release_id) == ("ab12cd34", "20260101-000000-aaaaaaa")
        assert record.snapshot_backup is None

    def test_the_migration_is_idempotent(self, fresh, tmp_path):
        """Run twice on the same cursor, the second run finds its columns and adds nothing."""
        db_path = tmp_path / "wasm.db"
        _create_v8_database(db_path)
        store = WASMStore(db_path, fs=RecordingFileSystem())
        before = (_raw_columns(db_path, "apps"), _raw_columns(db_path, "deployments"))

        with store._ddl_transaction() as cursor:
            store._migrate_v8_to_v9(cursor)

        assert (_raw_columns(db_path, "apps"), _raw_columns(db_path, "deployments")) == before
        WASMStore.reset_instance()
        reopened = WASMStore(db_path, fs=RecordingFileSystem())
        assert reopened.get_app("v8.example.com") is not None

    def test_a_failing_v8_to_v9_step_leaves_v8_intact(self, fresh, tmp_path):
        """A crash after the first column rolls that column back with the version."""
        db_path = tmp_path / "wasm.db"
        _create_v8_database(db_path)

        def _crash(self: WASMStore, cursor: sqlite3.Cursor) -> None:
            cursor.execute("ALTER TABLE apps ADD COLUMN health_path TEXT")
            raise sqlite3.OperationalError("simulated crash mid-migration")

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(WASMStore, "_migrate_v8_to_v9", _crash)
            with pytest.raises(sqlite3.OperationalError):
                WASMStore(db_path, fs=RecordingFileSystem())

        assert _raw_max_version(db_path) == 8
        assert "health_path" not in _raw_columns(db_path, "apps")

    def test_the_fresh_schema_and_the_migration_agree(self, fresh, tmp_path):
        """Both paths to v9 give apps and deployments the same columns."""
        db_path = tmp_path / "migrated.db"
        _create_v8_database(db_path)
        WASMStore(db_path, fs=RecordingFileSystem())
        WASMStore.reset_instance()
        WASMStore(tmp_path / "fresh.db", fs=RecordingFileSystem())

        for table in ("apps", "deployments"):
            assert _raw_columns(db_path, table) == _raw_columns(tmp_path / "fresh.db", table)
        assert {"health_path", "health_expect", "health_timeout"} <= _raw_columns(db_path, "apps")
        assert "snapshot_backup" in _raw_columns(db_path, "deployments")

    def test_a_migrated_database_records_the_new_facts(self, fresh, tmp_path):
        """The columns are usable at once: settings round-trip, a snapshot is linked."""
        db_path = tmp_path / "wasm.db"
        _create_v8_database(db_path)
        store = WASMStore(db_path, fs=RecordingFileSystem())

        assert store.set_app_health("v8.example.com", path="/healthz", expect="200-399", timeout=90)
        store.set_deployment_snapshot(1, "v8-example-com_20260102_030405")

        app = store.get_app("v8.example.com")
        assert (app.health_path, app.health_expect, app.health_timeout) == (
            "/healthz",
            "200-399",
            90,
        )
        assert store.get_deployment(1).snapshot_backup == "v8-example-com_20260102_030405"
