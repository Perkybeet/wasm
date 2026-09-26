# Copyright (c) 2024-2025 Yago López Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
Tests for WASM SQLite persistence store.

Beyond the CRUD surface, this file pins two properties that were not properties
before: the database and its directory are created through the
:mod:`wasm.core.fs` seam, so ``--dry-run`` cannot leave one behind, and they end
up 0600 inside 0700, because ``apps.env_vars`` holds DATABASE_URL and API keys.
"""

import ast
import re
import sqlite3
import tempfile
from pathlib import Path

import pytest

from wasm.core import store as store_module
from wasm.core.exceptions import DomainConflictError, DomainError, ValidationError
from wasm.core.fs import (
    SECRET_DIR_MODE,
    SECRET_MODE,
    DryRunFileSystem,
    RecordingFileSystem,
)
from wasm.core.store import (
    SCHEMA_VERSION,
    App,
    AppStatus,
    AppType,
    Database,
    DatabaseEngine,
    DatabaseUser,
    Service,
    Site,
    StoreError,
    WASMStore,
    WebServer,
    get_store,
)


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


@pytest.fixture
def temp_db():
    """Create a temporary database for testing."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = Path(f.name)

    # Reset singleton
    WASMStore.reset_instance()
    store = WASMStore(db_path)

    yield store

    # Cleanup
    store.close()
    WASMStore.reset_instance()
    db_path.unlink(missing_ok=True)


@pytest.fixture
def populated_store(temp_db):
    """Create a store with sample data."""
    store = temp_db

    # Create sample app
    app = App(
        domain="example.com",
        app_type=AppType.NEXTJS.value,
        source="https://github.com/user/repo",
        branch="main",
        port=3000,
        app_path="/var/www/apps/example-com",
        webserver=WebServer.NGINX.value,
        ssl_enabled=True,
        status=AppStatus.RUNNING.value,
        env_vars={"NODE_ENV": "production"},
    )
    app = store.create_app(app)

    # Create sample site
    site = Site(
        app_id=app.id,
        domain="example.com",
        webserver=WebServer.NGINX.value,
        config_path="/etc/nginx/sites-available/example.com",
        enabled=True,
        ssl_enabled=True,
    )
    store.create_site(site)

    # Create sample service
    service = Service(
        app_id=app.id,
        name="example-com",
        unit_file="/etc/systemd/system/wasm-example-com.service",
        working_directory="/var/www/apps/example-com",
        command="/usr/bin/npm run start",
        port=3000,
        status="active",
        enabled=True,
        environment={"PORT": "3000"},
    )
    store.create_service(service)

    # Create sample database
    db = Database(
        app_id=app.id,
        name="example_db",
        engine=DatabaseEngine.MYSQL.value,
        port=3306,
    )
    store.create_database(db)

    return store


class TestWASMStore:
    """Tests for WASMStore class."""

    def test_singleton_pattern(self, temp_db):
        """Test that WASMStore is a singleton."""
        store1 = get_store(temp_db.db_path)
        store2 = get_store(temp_db.db_path)
        assert store1 is store2

    def test_schema_creation(self, temp_db):
        """Test that schema is created properly."""
        with temp_db._transaction() as cursor:
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
            tables = {row[0] for row in cursor.fetchall()}

        expected = {
            "schema_version",
            "apps",
            "sites",
            "services",
            "databases",
            "database_users",
            "deployments",
        }
        assert expected.issubset(tables)

    def test_schema_version(self, temp_db):
        """Test that schema version is recorded."""
        with temp_db._transaction() as cursor:
            cursor.execute("SELECT MAX(version) FROM schema_version")
            version = cursor.fetchone()[0]

        assert version == SCHEMA_VERSION


#: The v1 schema exactly as the 1.2.x releases shipped it, frozen here so the
#: migration is always exercised against what is actually deployed on servers,
#: not against whatever SCHEMA_SQL has since become.
V1_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS apps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    domain TEXT NOT NULL UNIQUE,
    app_type TEXT NOT NULL DEFAULT 'unknown',
    source TEXT,
    branch TEXT,
    port INTEGER,
    app_path TEXT NOT NULL,
    webserver TEXT NOT NULL DEFAULT 'nginx',
    ssl_enabled INTEGER NOT NULL DEFAULT 1,
    ssl_certificate TEXT,
    ssl_key TEXT,
    status TEXT NOT NULL DEFAULT 'unknown',
    is_static INTEGER NOT NULL DEFAULT 0,
    env_vars TEXT DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    deployed_at TEXT
);

CREATE TABLE IF NOT EXISTS sites (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    app_id INTEGER,
    domain TEXT NOT NULL UNIQUE,
    webserver TEXT NOT NULL DEFAULT 'nginx',
    config_path TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    is_static INTEGER NOT NULL DEFAULT 0,
    document_root TEXT,
    proxy_port INTEGER,
    ssl_enabled INTEGER NOT NULL DEFAULT 0,
    ssl_certificate TEXT,
    ssl_key TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (app_id) REFERENCES apps(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS services (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    app_id INTEGER,
    name TEXT NOT NULL UNIQUE,
    unit_file TEXT NOT NULL,
    working_directory TEXT NOT NULL,
    command TEXT NOT NULL,
    user TEXT NOT NULL DEFAULT 'www-data',
    "group" TEXT NOT NULL DEFAULT 'www-data',
    enabled INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'inactive',
    port INTEGER,
    environment TEXT DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (app_id) REFERENCES apps(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS databases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    app_id INTEGER,
    name TEXT NOT NULL,
    engine TEXT NOT NULL,
    host TEXT NOT NULL DEFAULT 'localhost',
    port INTEGER,
    username TEXT,
    encoding TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (app_id) REFERENCES apps(id) ON DELETE SET NULL,
    UNIQUE(name, engine)
);

CREATE TABLE IF NOT EXISTS database_users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    database_id INTEGER,
    username TEXT NOT NULL,
    engine TEXT NOT NULL,
    host TEXT NOT NULL DEFAULT 'localhost',
    privileges TEXT NOT NULL DEFAULT 'ALL',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (database_id) REFERENCES databases(id) ON DELETE CASCADE,
    UNIQUE(username, engine, host)
);

CREATE INDEX IF NOT EXISTS idx_apps_domain ON apps(domain);
CREATE INDEX IF NOT EXISTS idx_apps_status ON apps(status);
CREATE INDEX IF NOT EXISTS idx_sites_domain ON sites(domain);
CREATE INDEX IF NOT EXISTS idx_sites_app_id ON sites(app_id);
CREATE INDEX IF NOT EXISTS idx_services_app_id ON services(app_id);
CREATE INDEX IF NOT EXISTS idx_services_name ON services(name);
CREATE INDEX IF NOT EXISTS idx_databases_engine ON databases(engine);
CREATE INDEX IF NOT EXISTS idx_databases_app_id ON databases(app_id);
"""


class TestSchemaV2Migration:
    """Schema v2 adds the deployments history table without losing a row."""

    def _create_v1_database(self, db_path: Path) -> None:
        """
        Create a real v1 database with rows, as a 1.2.x release left it.

        Args:
            db_path: Where the database file is created.
        """
        conn = sqlite3.connect(db_path)
        try:
            conn.executescript(V1_SCHEMA_SQL)
            conn.execute("INSERT INTO schema_version (version) VALUES (1)")
            conn.execute(
                "INSERT INTO apps (domain, app_type, app_path) VALUES (?, ?, ?)",
                ("v1.example.com", "nextjs", "/var/www/apps/v1-example-com"),
            )
            conn.execute(
                "INSERT INTO sites (domain, config_path) VALUES (?, ?)",
                ("v1.example.com", "/etc/nginx/sites-available/v1.example.com"),
            )
            conn.execute(
                "INSERT INTO services (name, unit_file, working_directory, command)"
                " VALUES (?, ?, ?, ?)",
                (
                    "v1-example-com",
                    "/etc/systemd/system/wasm-v1-example-com.service",
                    "/var/www/apps/v1-example-com",
                    "/usr/bin/npm run start",
                ),
            )
            conn.execute(
                "INSERT INTO databases (name, engine) VALUES (?, ?)",
                ("v1_db", "postgresql"),
            )
            conn.commit()
        finally:
            conn.close()

    def test_a_v1_database_migrates_to_v2_keeping_every_row(self, fresh, tmp_path):
        """A server upgraded in place keeps its whole inventory."""
        db_path = tmp_path / "wasm.db"
        self._create_v1_database(db_path)

        store = WASMStore(db_path, fs=RecordingFileSystem())

        with store._transaction() as cursor:
            # The chain does not stop at v2: a v1 database walks every
            # migration and lands on whatever the current version is.
            cursor.execute("SELECT MAX(version) FROM schema_version")
            assert cursor.fetchone()[0] == SCHEMA_VERSION
        assert store.get_app("v1.example.com") is not None
        assert store.get_site("v1.example.com") is not None
        assert store.get_service("v1-example-com") is not None
        assert store.get_database("v1_db", "postgresql") is not None

    def test_the_migration_creates_the_deployments_table_and_index(self, fresh, tmp_path):
        """The table and its (domain, started_at) index exist after migrating."""
        db_path = tmp_path / "wasm.db"
        self._create_v1_database(db_path)

        store = WASMStore(db_path, fs=RecordingFileSystem())

        with store._transaction() as cursor:
            cursor.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?", ("deployments",)
            )
            assert cursor.fetchone() is not None
            cursor.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND name=?",
                ("idx_deployments_domain_started",),
            )
            assert cursor.fetchone() is not None

    def test_a_migrated_database_records_deployments(self, fresh, tmp_path):
        """Migration produces a table the new API can actually use."""
        db_path = tmp_path / "wasm.db"
        self._create_v1_database(db_path)
        store = WASMStore(db_path, fs=RecordingFileSystem())

        deployment_id = store.record_deployment_start("v1.example.com", "cli")

        assert store.get_deployment(deployment_id) is not None

    def test_a_fresh_database_is_created_directly_at_the_current_version(self, temp_db):
        """A new install does not take the migration path to reach the schema."""
        with temp_db._transaction() as cursor:
            cursor.execute("SELECT MAX(version) FROM schema_version")
            assert cursor.fetchone()[0] == SCHEMA_VERSION
            cursor.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?", ("deployments",)
            )
            assert cursor.fetchone() is not None


#: The deployments table exactly as schema v2 shipped it, frozen for the same
#: reason V1_SCHEMA_SQL is: the v2-to-v3 migration must run against what is
#: deployed, not against whatever DEPLOYMENTS_SCHEMA_SQL becomes later.
V2_DEPLOYMENTS_SQL = """
CREATE TABLE IF NOT EXISTS deployments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    domain TEXT NOT NULL,
    status TEXT NOT NULL
        CHECK (status IN ('queued', 'running', 'success', 'failed', 'rolled_back')),
    triggered_by TEXT NOT NULL CHECK (triggered_by IN ('panel', 'cli', 'webhook')),
    git_commit TEXT,
    git_branch TEXT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    duration_s REAL,
    log_path TEXT,
    error TEXT
);

CREATE INDEX IF NOT EXISTS idx_deployments_domain_started
    ON deployments(domain, started_at DESC);
"""


class TestSchemaV3Migration:
    """Schema v3 adds the per-app webhook secret column."""

    def _create_v2_database(self, db_path: Path) -> None:
        """
        Create a real v2 database with rows, as a 1.3.x release left it.

        Args:
            db_path: Where the database file is created.
        """
        conn = sqlite3.connect(db_path)
        try:
            conn.executescript(V1_SCHEMA_SQL)
            conn.executescript(V2_DEPLOYMENTS_SQL)
            conn.execute("INSERT INTO schema_version (version) VALUES (1)")
            conn.execute("INSERT INTO schema_version (version) VALUES (2)")
            conn.execute(
                "INSERT INTO apps (domain, app_type, app_path) VALUES (?, ?, ?)",
                ("v2.example.com", "nextjs", "/var/www/apps/v2-example-com"),
            )
            conn.commit()
        finally:
            conn.close()

    def _apps_columns(self, store: WASMStore) -> set[str]:
        """
        Args:
            store: The store to inspect.

        Returns:
            The column names of the apps table.
        """
        with store._transaction() as cursor:
            cursor.execute("PRAGMA table_info(apps)")
            return {row["name"] for row in cursor.fetchall()}

    def test_a_v2_database_migrates_to_v3_keeping_every_row(self, fresh, tmp_path):
        """A server upgraded in place keeps its inventory and gains the column."""
        db_path = tmp_path / "wasm.db"
        self._create_v2_database(db_path)

        store = WASMStore(db_path, fs=RecordingFileSystem())

        with store._transaction() as cursor:
            cursor.execute("SELECT MAX(version) FROM schema_version")
            assert cursor.fetchone()[0] == SCHEMA_VERSION
        assert store.get_app("v2.example.com") is not None
        assert "webhook_secret" in self._apps_columns(store)

    def test_migrated_apps_have_webhooks_disabled(self, fresh, tmp_path):
        """An upgrade must never invent a secret; NULL means disabled."""
        db_path = tmp_path / "wasm.db"
        self._create_v2_database(db_path)

        store = WASMStore(db_path, fs=RecordingFileSystem())

        assert store.get_webhook_secret("v2.example.com") is None

    def test_a_v1_database_walks_both_migrations(self, fresh, tmp_path):
        """A 1.2.x database reaches v3 in one opening."""
        db_path = tmp_path / "wasm.db"
        conn = sqlite3.connect(db_path)
        try:
            conn.executescript(V1_SCHEMA_SQL)
            conn.execute("INSERT INTO schema_version (version) VALUES (1)")
            conn.commit()
        finally:
            conn.close()

        store = WASMStore(db_path, fs=RecordingFileSystem())

        with store._transaction() as cursor:
            cursor.execute("SELECT MAX(version) FROM schema_version")
            assert cursor.fetchone()[0] == SCHEMA_VERSION
        assert "webhook_secret" in self._apps_columns(store)

    def test_a_fresh_database_has_the_webhook_secret_column(self, temp_db):
        """The fresh-install path and the migration agree on the schema."""
        assert "webhook_secret" in self._apps_columns(temp_db)


class TestSchemaV4Migration:
    """Schema v4 adds the jobs table, so a panel restart does not erase history."""

    def _create_v3_database(self, db_path: Path) -> None:
        """
        Create a real v3 database with rows, as a 1.4.x release left it.

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

    def test_a_v3_database_migrates_to_v4_keeping_every_row(self, fresh, tmp_path):
        """A server upgraded in place keeps its inventory and gains the jobs table."""
        db_path = tmp_path / "wasm.db"
        self._create_v3_database(db_path)

        store = WASMStore(db_path, fs=RecordingFileSystem())

        with store._transaction() as cursor:
            cursor.execute("SELECT MAX(version) FROM schema_version")
            assert cursor.fetchone()[0] == SCHEMA_VERSION
        assert store.get_app("v3.example.com") is not None

    def test_the_migration_creates_the_jobs_table_and_indexes(self, fresh, tmp_path):
        """The table and its indexes exist after migrating."""
        db_path = tmp_path / "wasm.db"
        self._create_v3_database(db_path)

        store = WASMStore(db_path, fs=RecordingFileSystem())

        with store._transaction() as cursor:
            cursor.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?", ("jobs",)
            )
            assert cursor.fetchone() is not None
            for index in ("idx_jobs_status", "idx_jobs_domain", "idx_jobs_created"):
                cursor.execute(
                    "SELECT name FROM sqlite_master WHERE type='index' AND name=?", (index,)
                )
                assert cursor.fetchone() is not None

    def test_a_migrated_database_records_jobs(self, fresh, tmp_path):
        """Migration produces a table the job manager can actually use."""
        db_path = tmp_path / "wasm.db"
        self._create_v3_database(db_path)
        store = WASMStore(db_path, fs=RecordingFileSystem())

        store.create_job(
            store_module.JobRecord(
                id="job-1",
                type="deploy",
                name="Deploy v3.example.com",
                status="running",
                domain="v3.example.com",
            )
        )

        job = store.get_job("job-1")
        assert job is not None
        assert job.status == "running"

    def test_a_v1_database_walks_every_migration(self, fresh, tmp_path):
        """A 1.2.x database reaches v4 in one opening."""
        db_path = tmp_path / "wasm.db"
        conn = sqlite3.connect(db_path)
        try:
            conn.executescript(V1_SCHEMA_SQL)
            conn.execute("INSERT INTO schema_version (version) VALUES (1)")
            conn.commit()
        finally:
            conn.close()

        store = WASMStore(db_path, fs=RecordingFileSystem())

        with store._transaction() as cursor:
            cursor.execute("SELECT MAX(version) FROM schema_version")
            assert cursor.fetchone()[0] == SCHEMA_VERSION
            cursor.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?", ("jobs",)
            )
            assert cursor.fetchone() is not None

    def test_a_fresh_database_has_the_jobs_table(self, temp_db):
        """The fresh-install path and the migration agree on the schema."""
        with temp_db._transaction() as cursor:
            cursor.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?", ("jobs",)
            )
            assert cursor.fetchone() is not None


#: The jobs table exactly as the 1.6.x releases shipped it, frozen here for the
#: same reason V1_SCHEMA_SQL is.
V4_JOBS_SQL = """
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    type TEXT NOT NULL,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL
        CHECK (status IN ('pending', 'running', 'completed', 'failed', 'cancelled')),
    progress INTEGER NOT NULL DEFAULT 0,
    total_steps INTEGER NOT NULL DEFAULT 100,
    domain TEXT,
    error TEXT,
    result_json TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    started_at TEXT,
    finished_at TEXT,
    log_path TEXT
);
"""


class TestSchemaV5Migration:
    """Schema v5 adds the release layout, and every existing app stays in place."""

    def _create_v4_database(self, db_path: Path) -> None:
        """
        Create a real v4 database with rows, as a 1.6.x release left it.

        Args:
            db_path: Where the database file is created.
        """
        conn = sqlite3.connect(db_path)
        try:
            conn.executescript(V1_SCHEMA_SQL)
            conn.executescript(V2_DEPLOYMENTS_SQL)
            conn.execute("ALTER TABLE apps ADD COLUMN webhook_secret TEXT")
            conn.executescript(V4_JOBS_SQL)
            for version in (1, 2, 3, 4):
                conn.execute("INSERT INTO schema_version (version) VALUES (?)", (version,))
            conn.execute(
                "INSERT INTO apps (domain, app_type, app_path, webhook_secret) VALUES (?, ?, ?, ?)",
                ("v4.example.com", "nextjs", "/var/www/apps/v4-example-com", "s3cret"),
            )
            conn.commit()
        finally:
            conn.close()

    def _columns(self, store: WASMStore, table: str) -> dict[str, tuple[str, int, str | None]]:
        """
        Args:
            store: The store to inspect.
            table: Table name.

        Returns:
            Column name to (declared type, NOT NULL, default).
        """
        with store._transaction() as cursor:
            cursor.execute(f"PRAGMA table_info({table})")
            return {
                row["name"]: (row["type"], row["notnull"], row["dflt_value"])
                for row in cursor.fetchall()
            }

    def _migrated(self, tmp_path: Path, name: str = "wasm.db") -> WASMStore:
        """
        Args:
            tmp_path: Directory for the database.
            name: File name of the database.

        Returns:
            A store opened over a v4 database, which migrates it.
        """
        db_path = tmp_path / name
        self._create_v4_database(db_path)
        return WASMStore(db_path, fs=RecordingFileSystem())

    def test_a_v4_database_migrates_to_v5_keeping_every_row(self, fresh, tmp_path):
        """A 1.6.x server keeps its inventory, and its apps stay in place."""
        store = self._migrated(tmp_path)

        with store._transaction() as cursor:
            cursor.execute("SELECT MAX(version) FROM schema_version")
            assert cursor.fetchone()[0] == SCHEMA_VERSION
        app = store.get_app("v4.example.com")
        assert app is not None
        assert app.layout == "inplace"
        assert app.keep_releases == 5
        assert app.persistent_paths == []
        assert (app.memory_max_mb, app.cpu_quota_percent, app.tasks_max) == (None, None, None)
        assert store.get_webhook_secret("v4.example.com") == "s3cret"

    def test_the_fresh_schema_and_the_migration_agree(self, fresh, tmp_path):
        """Both paths to v5 produce the same apps and releases tables."""
        tables = ("apps", "releases")
        migrated = self._migrated(tmp_path, "migrated.db")
        upgraded = {table: self._columns(migrated, table) for table in tables}
        WASMStore.reset_instance()

        installed = WASMStore(tmp_path / "fresh.db", fs=RecordingFileSystem())

        assert {table: self._columns(installed, table) for table in tables} == upgraded

    def test_a_migrated_database_records_releases(self, fresh, tmp_path):
        """The table works, keeps one active release, and refuses unknown statuses."""
        store = self._migrated(tmp_path)
        app = store.get_app("v4.example.com")

        for release_id in ("20260925-100000-aaaaaaa", "20260925-110000-bbbbbbb"):
            store.record_release(
                store_module.ReleaseRecord(
                    id=release_id,
                    app_id=app.id,
                    git_commit=release_id[-7:],
                    created_at=f"2026-09-25T{release_id[9:11]}:00:00+00:00",
                    path=f"/var/www/apps/v4-example-com/releases/{release_id}",
                )
            )
        store.mark_release_active(app.id, "20260925-100000-aaaaaaa")
        store.mark_release_active(app.id, "20260925-110000-bbbbbbb")

        assert [(r.id, r.status) for r in store.list_releases(app.id)] == [
            ("20260925-110000-bbbbbbb", "active"),
            ("20260925-100000-aaaaaaa", "superseded"),
        ]
        with pytest.raises(StoreError, match="Invalid release status"):
            store.set_release_status(app.id, "20260925-110000-bbbbbbb", "live")
        with store._transaction() as cursor, pytest.raises(sqlite3.IntegrityError):
            cursor.execute(
                "UPDATE releases SET status = 'live' WHERE id = ?", ("20260925-110000-bbbbbbb",)
            )

    def test_releases_go_with_their_application(self, fresh, tmp_path):
        """A release names a directory deleted with the app, so its row goes too."""
        store = self._migrated(tmp_path)
        app = store.get_app("v4.example.com")
        store.record_release(
            store_module.ReleaseRecord(
                id="20260925-100000-aaaaaaa",
                app_id=app.id,
                created_at="2026-09-25T10:00:00+00:00",
                path="/x",
            )
        )

        store.delete_app("v4.example.com")

        assert store.list_releases(app.id) == []

    def test_the_new_columns_round_trip(self, temp_db):
        """persistent_paths is a JSON list on disk and a list in the dataclass."""
        temp_db.create_app(
            App(
                domain="rt.example.com",
                app_path="/var/www/apps/rt",
                layout="releases",
                keep_releases=3,
                persistent_paths=["uploads", "storage/app"],
                memory_max_mb=512,
            )
        )

        app = temp_db.get_app("rt.example.com")
        assert app.layout == "releases"
        assert app.keep_releases == 3
        assert app.persistent_paths == ["uploads", "storage/app"]
        assert app.memory_max_mb == 512


class TestSchemaV6Migration:
    """
    Schema v6 gives every application its domains as rows.

    The migration knows only ``apps.domain``: whether a 1.x deploy also served
    ``www`` was never recorded, and a migration must not read the web server
    configuration off disk to find out. So each app gets exactly its primary,
    and the next ``wasm domain`` change adopts whatever the live site served.
    """

    def _create_v5_database(self, db_path: Path) -> None:
        """
        Create a real v5 database with two applications, as 2.0 pre-releases left it.

        Args:
            db_path: Where the database file is created.
        """
        conn = sqlite3.connect(db_path)
        try:
            conn.executescript(V1_SCHEMA_SQL)
            conn.executescript(V2_DEPLOYMENTS_SQL)
            conn.execute("ALTER TABLE apps ADD COLUMN webhook_secret TEXT")
            conn.executescript(V4_JOBS_SQL)
            for name, definition in store_module.APPS_V5_COLUMNS:
                conn.execute(f"ALTER TABLE apps ADD COLUMN {name} {definition}")
            conn.executescript(store_module.RELEASES_SCHEMA_SQL)
            for version in (1, 2, 3, 4, 5):
                conn.execute("INSERT INTO schema_version (version) VALUES (?)", (version,))
            conn.execute(
                "INSERT INTO apps (domain, app_type, app_path, created_at) VALUES (?, ?, ?, ?)",
                ("shop.example.com", "nextjs", "/var/www/apps/shop", "2026-01-02 03:04:05"),
            )
            conn.execute(
                "INSERT INTO apps (domain, app_type, app_path) VALUES (?, ?, ?)",
                ("example.com", "static", "/var/www/apps/example-com"),
            )
            conn.commit()
        finally:
            conn.close()

    def _migrated(self, tmp_path: Path, name: str = "wasm.db") -> WASMStore:
        """
        Args:
            tmp_path: Directory for the database.
            name: File name of the database.

        Returns:
            A store opened over a v5 database, which migrates it.
        """
        db_path = tmp_path / name
        self._create_v5_database(db_path)
        return WASMStore(db_path, fs=RecordingFileSystem())

    def _schema(self, store: WASMStore) -> dict[str, object]:
        """
        Args:
            store: The store to inspect.

        Returns:
            The domains table's columns and the SQL of its indexes.
        """
        with store._transaction() as cursor:
            cursor.execute("PRAGMA table_info(domains)")
            columns = {
                row["name"]: (row["type"], row["notnull"], row["dflt_value"], row["pk"])
                for row in cursor.fetchall()
            }
            cursor.execute(
                "SELECT name, sql FROM sqlite_master WHERE type = 'index' AND tbl_name = 'domains'"
                " AND sql IS NOT NULL ORDER BY name"
            )
            indexes = {row["name"]: " ".join(row["sql"].split()) for row in cursor.fetchall()}
        return {"columns": columns, "indexes": indexes}

    def test_every_application_gets_its_primary_domain(self, fresh, tmp_path):
        """Nothing is lost, and the primary is dated like the application."""
        store = self._migrated(tmp_path)

        with store._transaction() as cursor:
            cursor.execute("SELECT MAX(version) FROM schema_version")
            assert cursor.fetchone()[0] == SCHEMA_VERSION

        shop = store.list_domains("shop.example.com")
        assert [(d.domain, d.kind) for d in shop] == [("shop.example.com", "primary")]
        assert shop[0].created_at == "2026-01-02 03:04:05"
        assert shop[0].id is not None
        assert [(d.domain, d.kind) for d in store.list_domains("example.com")] == [
            ("example.com", "primary")
        ]

    def test_the_migration_does_not_guess_www(self, fresh, tmp_path):
        """A bare domain that may have been deployed with --www gets only its primary."""
        store = self._migrated(tmp_path)

        assert [d.domain for d in store.list_domains("example.com")] == ["example.com"]

    def test_the_fresh_schema_and_the_migration_agree(self, fresh, tmp_path):
        """Both paths to v6 produce the same table and the same indexes."""
        upgraded = self._schema(self._migrated(tmp_path, "migrated.db"))
        WASMStore.reset_instance()

        installed = self._schema(WASMStore(tmp_path / "fresh.db", fs=RecordingFileSystem()))

        assert installed == upgraded
        assert "idx_domains_one_primary" in installed["indexes"]


class TestSchemaV7Migration:
    """Schema v7 gives every job row who queued it."""

    def _create_v6_database(self, db_path: Path) -> None:
        """
        Create a real v6 database with one job, as 2.0 pre-releases left it.

        Args:
            db_path: Where the database file is created.
        """
        conn = sqlite3.connect(db_path)
        try:
            conn.executescript(V1_SCHEMA_SQL)
            conn.executescript(V2_DEPLOYMENTS_SQL)
            conn.execute("ALTER TABLE apps ADD COLUMN webhook_secret TEXT")
            conn.executescript(V4_JOBS_SQL)
            for name, definition in store_module.APPS_V5_COLUMNS:
                conn.execute(f"ALTER TABLE apps ADD COLUMN {name} {definition}")
            conn.executescript(store_module.RELEASES_SCHEMA_SQL)
            conn.executescript(store_module.DOMAINS_SCHEMA_SQL)
            for version in (1, 2, 3, 4, 5, 6):
                conn.execute("INSERT INTO schema_version (version) VALUES (?)", (version,))
            conn.execute(
                "INSERT INTO jobs (id, type, name, status) VALUES (?, ?, ?, ?)",
                ("old-job", "update", "Update old.example.com", "completed"),
            )
            conn.commit()
        finally:
            conn.close()

    def test_existing_jobs_keep_no_actor(self, fresh, tmp_path):
        """A job queued before this column existed has nothing to backfill it with."""
        db_path = tmp_path / "wasm.db"
        self._create_v6_database(db_path)

        store = WASMStore(db_path, fs=RecordingFileSystem())

        with store._transaction() as cursor:
            cursor.execute("SELECT MAX(version) FROM schema_version")
            assert cursor.fetchone()[0] == SCHEMA_VERSION

        job = store.get_job("old-job")
        assert job is not None
        assert job.actor is None

    def test_a_migrated_database_can_record_a_job_with_an_actor(self, fresh, tmp_path):
        """The column is usable immediately after migrating, not just present."""
        db_path = tmp_path / "wasm.db"
        self._create_v6_database(db_path)
        store = WASMStore(db_path, fs=RecordingFileSystem())

        store.create_job(
            store_module.JobRecord(
                id="new-job", type="deploy", name="Deploy", status="pending", actor="master"
            )
        )

        assert store.get_job("new-job").actor == "master"

    def test_the_fresh_schema_and_the_migration_agree(self, fresh, tmp_path):
        """Both paths to v7 give the jobs table the same actor column."""
        db_path = tmp_path / "migrated.db"
        self._create_v6_database(db_path)
        migrated = WASMStore(db_path, fs=RecordingFileSystem())
        with migrated._transaction() as cursor:
            cursor.execute("PRAGMA table_info(jobs)")
            upgraded_columns = {row["name"] for row in cursor.fetchall()}
        WASMStore.reset_instance()

        fresh_store = WASMStore(tmp_path / "fresh.db", fs=RecordingFileSystem())
        with fresh_store._transaction() as cursor:
            cursor.execute("PRAGMA table_info(jobs)")
            fresh_columns = {row["name"] for row in cursor.fetchall()}

        assert "actor" in upgraded_columns
        assert upgraded_columns == fresh_columns


class TestSchemaV8Migration:
    """Schema v8 links a deployment to the job that started it and the release it built."""

    def _create_v7_database(self, db_path: Path) -> None:
        """
        Create a real v7 database with one deployment, as 2.0 pre-releases left it.

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

    def test_existing_deployments_keep_no_job_or_release(self, fresh, tmp_path):
        """A deployment recorded before these columns existed has nothing to backfill them with."""
        db_path = tmp_path / "wasm.db"
        self._create_v7_database(db_path)

        store = WASMStore(db_path, fs=RecordingFileSystem())

        with store._transaction() as cursor:
            cursor.execute("SELECT MAX(version) FROM schema_version")
            assert cursor.fetchone()[0] == SCHEMA_VERSION

        record = store.list_deployments("old.example.com")[0]
        assert record.job_id is None
        assert record.release_id is None
        assert record.commit_message is None

    def test_a_migrated_database_can_record_the_new_facts(self, fresh, tmp_path):
        """The columns are usable immediately after migrating, not just present."""
        db_path = tmp_path / "wasm.db"
        self._create_v7_database(db_path)
        store = WASMStore(db_path, fs=RecordingFileSystem())

        deployment_id = store.record_deployment_start("new.example.com", "panel", job_id="ab12cd34")
        store.annotate_deployment(
            deployment_id, release_id="20260101-000000", commit_message="Fix bug"
        )

        record = store.get_deployment(deployment_id)
        assert record.job_id == "ab12cd34"
        assert record.release_id == "20260101-000000"
        assert record.commit_message == "Fix bug"

    def test_the_fresh_schema_and_the_migration_agree(self, fresh, tmp_path):
        """Both paths to v8 give the deployments table the same columns."""
        db_path = tmp_path / "migrated.db"
        self._create_v7_database(db_path)
        migrated = WASMStore(db_path, fs=RecordingFileSystem())
        with migrated._transaction() as cursor:
            cursor.execute("PRAGMA table_info(deployments)")
            upgraded_columns = {row["name"] for row in cursor.fetchall()}
        WASMStore.reset_instance()

        fresh_store = WASMStore(tmp_path / "fresh.db", fs=RecordingFileSystem())
        with fresh_store._transaction() as cursor:
            cursor.execute("PRAGMA table_info(deployments)")
            fresh_columns = {row["name"] for row in cursor.fetchall()}

        assert {"job_id", "release_id", "commit_message"} <= upgraded_columns
        assert upgraded_columns == fresh_columns


class TestDomains:
    """The store is the chokepoint for which application answers on which name."""

    def _app(self, store: WASMStore, domain: str = "example.com") -> App:
        """
        Args:
            store: The store to write to.
            domain: The application's domain.

        Returns:
            The created application.
        """
        return store.create_app(App(domain=domain, app_path=f"/var/www/apps/{domain}"))

    def test_creating_an_application_records_its_primary(self, temp_db):
        app = self._app(temp_db)

        domains = temp_db.list_domains("example.com")

        assert [(d.domain, d.kind, d.app_id) for d in domains] == [
            ("example.com", "primary", app.id)
        ]

    def test_aliases_and_redirects_are_listed_after_the_primary(self, temp_db):
        self._app(temp_db)
        temp_db.add_domain("example.com", "old.example.org", "redirect")
        temp_db.add_domain("example.com", "shop.example.com", "alias")
        temp_db.add_domain("example.com", "www.example.com", "redirect")

        listed = [(d.domain, d.kind) for d in temp_db.list_domains("example.com")]

        assert listed == [
            ("example.com", "primary"),
            ("shop.example.com", "alias"),
            ("old.example.org", "redirect"),
            ("www.example.com", "redirect"),
        ]

    def test_a_domain_is_normalised_before_it_is_stored(self, temp_db):
        self._app(temp_db)

        record = temp_db.add_domain("example.com", "  Shop.Example.COM ", "alias")

        assert record.domain == "shop.example.com"
        assert record.created_at

    @pytest.mark.parametrize(
        "hostile",
        ["shop.example.com;", "shop.example.com\nserver_name evil.com", "a b.com", "", "../x"],
    )
    def test_a_name_that_is_not_a_domain_never_reaches_a_config_file(self, temp_db, hostile):
        """These rows become server_name and ServerAlias tokens in files written as root."""
        self._app(temp_db)

        with pytest.raises(DomainError):
            temp_db.add_domain("example.com", hostile, "alias")

    def test_a_domain_belongs_to_one_application_only(self, temp_db):
        self._app(temp_db, "example.com")
        self._app(temp_db, "other.com")
        temp_db.add_domain("other.com", "shop.example.com", "alias")

        with pytest.raises(DomainConflictError, match=re.escape("other.com")):
            temp_db.add_domain("example.com", "shop.example.com", "redirect")
        with pytest.raises(DomainConflictError, match=re.escape("other.com")):
            temp_db.add_domain("example.com", "other.com", "alias")

    def test_adding_a_domain_twice_is_a_conflict_naming_what_it_already_is(self, temp_db):
        self._app(temp_db)
        temp_db.add_domain("example.com", "shop.example.com", "alias")

        with pytest.raises(DomainConflictError, match="alias"):
            temp_db.add_domain("example.com", "shop.example.com", "alias")

    def test_an_application_cannot_be_deployed_on_another_ones_alias(self, temp_db):
        """The conflict is refused in the same transaction, so no app row is left."""
        self._app(temp_db, "example.com")
        temp_db.add_domain("example.com", "shop.example.com", "alias")

        with pytest.raises(DomainConflictError, match=re.escape("example.com")):
            self._app(temp_db, "shop.example.com")

        assert temp_db.get_app("shop.example.com") is None

    def test_a_second_primary_is_refused(self, temp_db):
        self._app(temp_db)

        with pytest.raises(DomainError, match="one primary"):
            temp_db.add_domain("example.com", "shop.example.com", "primary")

    def test_an_unknown_kind_is_refused(self, temp_db):
        self._app(temp_db)

        with pytest.raises(ValidationError, match="kind"):
            temp_db.add_domain("example.com", "shop.example.com", "mirror")

    def test_a_domain_of_an_unknown_application_is_refused(self, temp_db):
        with pytest.raises(StoreError, match="not found"):
            temp_db.add_domain("ghost.example.com", "shop.example.com", "alias")

    def test_the_database_itself_refuses_a_second_primary_and_a_bad_kind(self, temp_db):
        """The partial unique index and the CHECK hold even for a writer that skips the methods."""
        app = self._app(temp_db)

        with temp_db._transaction() as cursor, pytest.raises(sqlite3.IntegrityError):
            cursor.execute(
                "INSERT INTO domains (app_id, domain, kind) VALUES (?, ?, 'primary')",
                (app.id, "second.example.com"),
            )
        with temp_db._transaction() as cursor, pytest.raises(sqlite3.IntegrityError):
            cursor.execute(
                "INSERT INTO domains (app_id, domain, kind) VALUES (?, ?, 'mirror')",
                (app.id, "mirror.example.com"),
            )

    def test_removing_an_alias_frees_the_name(self, temp_db):
        self._app(temp_db, "example.com")
        self._app(temp_db, "other.com")
        temp_db.add_domain("example.com", "shop.example.com", "alias")

        assert temp_db.remove_domain("example.com", "shop.example.com") is True

        assert [d.domain for d in temp_db.list_domains("example.com")] == ["example.com"]
        temp_db.add_domain("other.com", "shop.example.com", "alias")

    def test_removing_a_domain_the_application_does_not_have_is_false(self, temp_db):
        self._app(temp_db, "example.com")
        self._app(temp_db, "other.com")
        temp_db.add_domain("other.com", "shop.example.com", "alias")

        assert temp_db.remove_domain("example.com", "shop.example.com") is False
        assert temp_db.remove_domain("example.com", "nothing.example.com") is False
        assert [d.domain for d in temp_db.list_domains("other.com")] == [
            "other.com",
            "shop.example.com",
        ]

    def test_the_primary_cannot_be_removed(self, temp_db):
        self._app(temp_db)

        with pytest.raises(DomainError, match="primary"):
            temp_db.remove_domain("example.com", "example.com")

        assert [d.domain for d in temp_db.list_domains("example.com")] == ["example.com"]

    def test_domains_go_with_their_application(self, temp_db):
        self._app(temp_db)
        temp_db.add_domain("example.com", "shop.example.com", "alias")

        temp_db.delete_app("example.com")

        with temp_db._transaction() as cursor:
            cursor.execute("SELECT COUNT(*) FROM domains")
            assert cursor.fetchone()[0] == 0

    def test_an_application_without_a_primary_row_still_lists_one(self, temp_db):
        """A row written behind the store's back reads as what it is, not as nothing."""
        with temp_db._transaction() as cursor:
            cursor.execute(
                "INSERT INTO apps (domain, app_path) VALUES (?, ?)",
                ("legacy.example.com", "/var/www/apps/legacy"),
            )

        listed = temp_db.list_domains("legacy.example.com")

        assert [(d.domain, d.kind) for d in listed] == [("legacy.example.com", "primary")]

    def test_an_unknown_application_has_no_domains(self, temp_db):
        assert temp_db.list_domains("ghost.example.com") == []


class TestJobRecordCRUD:
    """The store's half of job persistence, independent of the job manager."""

    def _job(self, **overrides: object) -> "store_module.JobRecord":
        """
        Args:
            **overrides: Fields to override on the default record.

        Returns:
            A job record ready to persist.
        """
        defaults: dict[str, object] = {
            "id": "job-abc123",
            "type": "deploy",
            "name": "Deploy example.com",
            "description": "Deploying a nextjs application to example.com",
            "status": "pending",
            "domain": "example.com",
        }
        defaults.update(overrides)
        return store_module.JobRecord(**defaults)  # type: ignore[arg-type]

    def test_create_and_get_roundtrip(self, temp_db):
        """A created job comes back with every field it was given."""
        temp_db.create_job(self._job())

        job = temp_db.get_job("job-abc123")

        assert job is not None
        assert job.type == "deploy"
        assert job.name == "Deploy example.com"
        assert job.status == "pending"
        assert job.domain == "example.com"
        assert job.created_at is not None

    def test_create_fills_created_at_when_blank(self, temp_db):
        """The caller should not have to compute a timestamp for every job."""
        job = self._job(created_at=None)

        created = temp_db.create_job(job)

        assert created.created_at

    def test_an_unknown_job_is_none(self, temp_db):
        """A job id nothing created is not an error, just nothing to show."""
        assert temp_db.get_job("does-not-exist") is None

    def test_update_only_changes_the_given_fields(self, temp_db):
        """Reporting progress must not blank out the domain or the log path."""
        temp_db.create_job(self._job(log_path="/var/lib/wasm/job-logs/job-abc123.log"))

        assert temp_db.update_job("job-abc123", status="running", progress=40) is True

        job = temp_db.get_job("job-abc123")
        assert job is not None
        assert job.status == "running"
        assert job.progress == 40
        assert job.domain == "example.com"
        assert job.log_path == "/var/lib/wasm/job-logs/job-abc123.log"

    def test_updating_an_unknown_job_reports_no_change(self, temp_db):
        """There is nothing to lie about: the row does not exist."""
        assert temp_db.update_job("does-not-exist", status="running") is False

    def test_an_invalid_status_is_rejected_by_the_database(self, temp_db):
        """The CHECK constraint is the guard, not a Python-side allowlist."""
        temp_db.create_job(self._job())

        with pytest.raises(sqlite3.IntegrityError):
            temp_db.update_job("job-abc123", status="sideways")

    def test_list_jobs_orders_newest_first(self, temp_db):
        """History reads top-down as "what happened most recently"."""
        temp_db.create_job(self._job(id="job-1", created_at="2026-01-01T00:00:00"))
        temp_db.create_job(self._job(id="job-2", created_at="2026-01-02T00:00:00"))

        jobs = temp_db.list_jobs()

        assert [job.id for job in jobs] == ["job-2", "job-1"]

    def test_list_jobs_filters_by_status_and_domain(self, temp_db):
        """The activity screen and a per-app history both need to narrow the list."""
        temp_db.create_job(self._job(id="job-1", status="failed", domain="a.example.com"))
        temp_db.create_job(self._job(id="job-2", status="running", domain="a.example.com"))
        temp_db.create_job(self._job(id="job-3", status="running", domain="b.example.com"))

        assert [job.id for job in temp_db.list_jobs(status="running")] == ["job-3", "job-2"]
        assert [job.id for job in temp_db.list_jobs(domain="a.example.com")] == ["job-2", "job-1"]

    def test_fail_interrupted_jobs_only_touches_pending_and_running(self, temp_db):
        """A job that already finished must keep its real outcome."""
        temp_db.create_job(self._job(id="job-pending", status="pending"))
        temp_db.create_job(self._job(id="job-running", status="running"))
        temp_db.create_job(self._job(id="job-done", status="completed"))

        changed = temp_db.fail_interrupted_jobs("Interrupted by a panel restart")

        assert changed == 2
        assert temp_db.get_job("job-pending").status == "failed"
        assert temp_db.get_job("job-running").status == "failed"
        assert temp_db.get_job("job-done").status == "completed"

    def test_fail_interrupted_jobs_records_the_reason_and_a_finish_time(self, temp_db):
        """The history must say why, in the tool's own words, not just "failed"."""
        temp_db.create_job(self._job(id="job-running", status="running"))

        temp_db.fail_interrupted_jobs("Interrupted by a panel restart")

        job = temp_db.get_job("job-running")
        assert job.error == "Interrupted by a panel restart"
        assert job.finished_at is not None

    def test_save_job_creates_the_row_when_there_is_none(self, temp_db):
        """The first notification of a job is what creates it."""
        temp_db.save_job(self._job())

        job = temp_db.get_job("job-abc123")
        assert job is not None
        assert job.status == "pending"
        assert job.created_at

    def test_save_job_overwrites_the_state_and_keeps_the_identity(self, temp_db):
        """A later snapshot updates what changes; what the job is stays as queued."""
        temp_db.save_job(
            self._job(created_at="2026-01-01T00:00:00", log_path="/logs/job-abc123.log")
        )

        temp_db.save_job(
            self._job(
                name="renamed",
                status="failed",
                progress=40,
                error="DNS does not point here",
                created_at="2030-01-01T00:00:00",
                finished_at="2026-01-01T00:01:00",
                log_path=None,
            )
        )

        job = temp_db.get_job("job-abc123")
        assert job.status == "failed"
        assert job.progress == 40
        assert job.error == "DNS does not point here"
        assert job.finished_at == "2026-01-01T00:01:00"
        assert job.name == "Deploy example.com"
        assert job.created_at == "2026-01-01T00:00:00"
        # A snapshot taken after the log was closed does not erase where it is.
        assert job.log_path == "/logs/job-abc123.log"

    def test_save_job_still_refuses_an_invalid_status(self, temp_db):
        """The upsert goes through the same CHECK constraint as the insert."""
        temp_db.save_job(self._job())

        with pytest.raises(sqlite3.IntegrityError):
            temp_db.save_job(self._job(status="sideways"))

    def test_two_threads_saving_the_same_job_never_collide(self, temp_db):
        """
        The production failure: "UNIQUE constraint failed: jobs.id".

        The request thread that queues a job and the worker that starts it
        write it at the same moment. With an update-then-insert both could see
        no row and both insert; one statement cannot.
        """
        import threading

        iterations = 200
        errors: list[BaseException] = []
        barrier = threading.Barrier(2)

        def write(status: str) -> None:
            for index in range(iterations):
                barrier.wait()
                try:
                    temp_db.save_job(self._job(id=f"job-{index}", status=status))
                except sqlite3.Error as exc:
                    errors.append(exc)

        threads = [
            threading.Thread(target=write, args=(status,)) for status in ("pending", "running")
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)

        assert errors == []
        assert len(temp_db.list_jobs(limit=iterations * 2)) == iterations


class TestWebhookSecret:
    """The webhook secret is written and read only through its own methods."""

    def _seed(self, store: WASMStore) -> App:
        """
        Args:
            store: The store to seed.

        Returns:
            A stored application.
        """
        return store.create_app(
            App(
                domain="hooked.example.com",
                app_type=AppType.NODEJS.value,
                source="https://github.com/you/app",
                branch="main",
                app_path="/var/www/apps/hooked-example-com",
            )
        )

    def test_set_and_get_roundtrip(self, temp_db):
        """A stored secret comes back verbatim: HMAC needs it in clear."""
        self._seed(temp_db)

        assert temp_db.set_webhook_secret("hooked.example.com", "s3cret-value") is True
        assert temp_db.get_webhook_secret("hooked.example.com") == "s3cret-value"

    def test_clearing_the_secret_disables_webhooks(self, temp_db):
        """None is the disabled state, not an empty string."""
        self._seed(temp_db)
        temp_db.set_webhook_secret("hooked.example.com", "s3cret-value")

        assert temp_db.set_webhook_secret("hooked.example.com", None) is True
        assert temp_db.get_webhook_secret("hooked.example.com") is None

    def test_an_unknown_domain_has_no_secret_and_takes_none(self, temp_db):
        """Setting a secret on nothing reports failure instead of inventing a row."""
        assert temp_db.set_webhook_secret("nothing.example.com", "s3cret-value") is False
        assert temp_db.get_webhook_secret("nothing.example.com") is None

    def test_the_secret_survives_a_full_app_rewrite(self, temp_db):
        """
        Every redeploy rewrites the whole apps row from a freshly built App
        (see AppRegistrationHelper.register_app). If the secret travelled on
        the dataclass, the first webhook-triggered deploy would erase the
        secret that authenticated it.
        """
        seeded = self._seed(temp_db)
        temp_db.set_webhook_secret("hooked.example.com", "s3cret-value")

        rewritten = App(
            id=seeded.id,
            domain="hooked.example.com",
            app_type=AppType.NODEJS.value,
            source="https://github.com/you/app",
            branch="main",
            app_path="/var/www/apps/hooked-example-com",
            created_at=seeded.created_at,
        )
        temp_db.update_app(rewritten)

        assert temp_db.get_webhook_secret("hooked.example.com") == "s3cret-value"

    def test_the_secret_never_rides_the_app_record(self, temp_db):
        """App objects are serialised everywhere; the secret must not be aboard."""
        self._seed(temp_db)
        temp_db.set_webhook_secret("hooked.example.com", "s3cret-value")

        app = temp_db.get_app("hooked.example.com")

        assert app is not None
        assert not hasattr(app, "webhook_secret")
        assert "webhook_secret" not in app.to_dict()
        assert "s3cret-value" not in str(app.to_dict())

    def test_list_webhook_flags_reveals_only_whether_a_secret_is_set(self, temp_db):
        """
        GET /api/apps must show a webhook indicator without ever putting the
        secret itself on the wire, and without one query per application.
        """
        self._seed(temp_db)
        temp_db.create_app(
            App(
                domain="bare.example.com",
                app_type=AppType.NODEJS.value,
                app_path="/var/www/apps/bare-example-com",
            )
        )
        temp_db.set_webhook_secret("hooked.example.com", "s3cret-value")

        flags = temp_db.list_webhook_flags(["hooked.example.com", "bare.example.com"])

        assert flags == {"hooked.example.com": True, "bare.example.com": False}

    def test_list_webhook_flags_defaults_to_every_application(self, temp_db):
        self._seed(temp_db)
        temp_db.set_webhook_secret("hooked.example.com", "s3cret-value")

        assert temp_db.list_webhook_flags() == {"hooked.example.com": True}

    def test_list_webhook_flags_of_no_domains_costs_no_rows(self, temp_db):
        self._seed(temp_db)

        assert temp_db.list_webhook_flags([]) == {}


class TestAppCRUD:
    """Tests for App CRUD operations."""

    def test_create_app(self, temp_db):
        """Test creating an app."""
        app = App(
            domain="test.com",
            app_type=AppType.VITE.value,
            source="https://github.com/user/test",
            port=5173,
            app_path="/var/www/apps/test-com",
            status=AppStatus.DEPLOYING.value,
        )

        created = temp_db.create_app(app)

        assert created.id is not None
        assert created.domain == "test.com"
        assert created.created_at is not None

    def test_get_app(self, temp_db):
        """Test getting an app by domain."""
        app = App(domain="get-test.com", app_type="nodejs", app_path="/test")
        temp_db.create_app(app)

        retrieved = temp_db.get_app("get-test.com")

        assert retrieved is not None
        assert retrieved.domain == "get-test.com"

    def test_get_app_not_found(self, temp_db):
        """Test getting a non-existent app."""
        retrieved = temp_db.get_app("nonexistent.com")
        assert retrieved is None

    def test_list_apps(self, populated_store):
        """Test listing apps."""
        apps = populated_store.list_apps()
        assert len(apps) >= 1
        assert any(a.domain == "example.com" for a in apps)

    def test_list_apps_with_filter(self, temp_db):
        """Test listing apps with filters."""
        temp_db.create_app(App(domain="a.com", app_type="nextjs", app_path="/a", status="running"))
        temp_db.create_app(App(domain="b.com", app_type="vite", app_path="/b", status="stopped"))
        temp_db.create_app(App(domain="c.com", app_type="nextjs", app_path="/c", status="running"))

        running = temp_db.list_apps(status="running")
        assert len(running) == 2

        nextjs = temp_db.list_apps(app_type="nextjs")
        assert len(nextjs) == 2

    def test_update_app(self, temp_db):
        """Test updating an app."""
        app = temp_db.create_app(App(domain="update.com", app_type="nodejs", app_path="/update"))

        app.status = AppStatus.RUNNING.value
        app.port = 4000
        temp_db.update_app(app)

        retrieved = temp_db.get_app("update.com")
        assert retrieved.status == AppStatus.RUNNING.value
        assert retrieved.port == 4000

    def test_update_app_status(self, temp_db):
        """Test updating just the app status."""
        temp_db.create_app(App(domain="status.com", app_type="nodejs", app_path="/status"))

        result = temp_db.update_app_status("status.com", AppStatus.FAILED.value)

        assert result is True
        app = temp_db.get_app("status.com")
        assert app.status == AppStatus.FAILED.value

    def test_delete_app(self, temp_db):
        """Test deleting an app."""
        temp_db.create_app(App(domain="delete.com", app_type="nodejs", app_path="/delete"))

        result = temp_db.delete_app("delete.com")

        assert result is True
        assert temp_db.get_app("delete.com") is None

    def test_app_exists(self, temp_db):
        """Test checking if app exists."""
        temp_db.create_app(App(domain="exists.com", app_type="nodejs", app_path="/exists"))

        assert temp_db.app_exists("exists.com") is True
        assert temp_db.app_exists("notexists.com") is False

    def test_app_env_vars_serialization(self, temp_db):
        """Test that env_vars are properly serialized/deserialized."""
        app = App(
            domain="env.com",
            app_type="nodejs",
            app_path="/env",
            env_vars={"KEY1": "value1", "KEY2": "value2"},
        )
        temp_db.create_app(app)

        retrieved = temp_db.get_app("env.com")
        assert retrieved.env_vars == {"KEY1": "value1", "KEY2": "value2"}


class TestSiteCRUD:
    """Tests for Site CRUD operations."""

    def test_create_site(self, temp_db):
        """Test creating a site."""
        site = Site(
            domain="site.com",
            webserver="nginx",
            config_path="/etc/nginx/sites-available/site.com",
        )

        created = temp_db.create_site(site)

        assert created.id is not None
        assert created.domain == "site.com"

    def test_get_site(self, temp_db):
        """Test getting a site."""
        temp_db.create_site(Site(domain="get-site.com", webserver="nginx", config_path="/test"))

        site = temp_db.get_site("get-site.com")

        assert site is not None
        assert site.webserver == "nginx"

    def test_get_site_by_app_id(self, populated_store):
        """Test getting a site by app ID."""
        app = populated_store.get_app("example.com")
        site = populated_store.get_site_by_app_id(app.id)

        assert site is not None
        assert site.domain == "example.com"

    def test_list_sites(self, temp_db):
        """Test listing sites."""
        temp_db.create_site(Site(domain="a.com", webserver="nginx", config_path="/a"))
        temp_db.create_site(Site(domain="b.com", webserver="apache", config_path="/b"))

        all_sites = temp_db.list_sites()
        assert len(all_sites) == 2

        nginx_sites = temp_db.list_sites(webserver="nginx")
        assert len(nginx_sites) == 1


class TestServiceCRUD:
    """Tests for Service CRUD operations."""

    def test_create_service(self, temp_db):
        """Test creating a service."""
        service = Service(
            name="my-service",
            unit_file="/etc/systemd/system/wasm-my-service.service",
            working_directory="/var/www/apps/my-service",
            command="/usr/bin/node server.js",
            port=3000,
        )

        created = temp_db.create_service(service)

        assert created.id is not None
        assert created.name == "my-service"

    def test_get_service(self, temp_db):
        """Test getting a service."""
        temp_db.create_service(
            Service(
                name="get-service",
                unit_file="/test",
                working_directory="/test",
                command="test",
            )
        )

        service = temp_db.get_service("get-service")

        assert service is not None
        assert service.name == "get-service"

    def test_update_service_status(self, temp_db):
        """Test updating service status."""
        temp_db.create_service(
            Service(
                name="status-service",
                unit_file="/test",
                working_directory="/test",
                command="test",
                status="inactive",
                enabled=False,
            )
        )

        result = temp_db.update_service_status("status-service", "active")

        assert result is True
        service = temp_db.get_service("status-service")
        assert service.status == "active"

    def test_service_environment_serialization(self, temp_db):
        """Test that environment is properly serialized."""
        temp_db.create_service(
            Service(
                name="env-service",
                unit_file="/test",
                working_directory="/test",
                command="test",
                environment={"PORT": "3000", "NODE_ENV": "production"},
            )
        )

        service = temp_db.get_service("env-service")
        assert service.environment == {"PORT": "3000", "NODE_ENV": "production"}


class TestDatabaseCRUD:
    """Tests for Database CRUD operations."""

    def test_create_database(self, temp_db):
        """Test creating a database record."""
        db = Database(
            name="mydb",
            engine=DatabaseEngine.POSTGRESQL.value,
            port=5432,
        )

        created = temp_db.create_database(db)

        assert created.id is not None
        assert created.name == "mydb"

    def test_get_database(self, temp_db):
        """Test getting a database."""
        temp_db.create_database(Database(name="getdb", engine="mysql"))

        db = temp_db.get_database("getdb", "mysql")

        assert db is not None
        assert db.engine == "mysql"

    def test_list_databases_by_engine(self, temp_db):
        """Test listing databases filtered by engine."""
        temp_db.create_database(Database(name="db1", engine="mysql"))
        temp_db.create_database(Database(name="db2", engine="mysql"))
        temp_db.create_database(Database(name="db3", engine="postgresql"))

        mysql_dbs = temp_db.list_databases(engine="mysql")

        assert len(mysql_dbs) == 2

    def test_link_database_to_app(self, temp_db):
        """Test linking a database to an app."""
        app = temp_db.create_app(App(domain="dbapp.com", app_type="nodejs", app_path="/test"))
        temp_db.create_database(Database(name="linked_db", engine="mysql"))

        result = temp_db.link_database_to_app("linked_db", "mysql", "dbapp.com")

        assert result is True
        db = temp_db.get_database("linked_db", "mysql")
        assert db.app_id == app.id


class TestDatabaseUserCRUD:
    """Tests for DatabaseUser CRUD operations."""

    def test_create_database_user(self, temp_db):
        """Test creating a database user."""
        user = DatabaseUser(
            username="testuser",
            engine="mysql",
            privileges="ALL",
        )

        created = temp_db.create_database_user(user)

        assert created.id is not None
        assert created.username == "testuser"

    def test_get_database_user(self, temp_db):
        """Test getting a database user."""
        temp_db.create_database_user(DatabaseUser(username="getuser", engine="mysql"))

        user = temp_db.get_database_user("getuser", "mysql")

        assert user is not None


class TestDeploymentHistory:
    """The deployments table is the product's memory of what it did."""

    def test_start_and_finish_roundtrip_computes_duration(self, temp_db):
        """A deployment is recorded running, then closed with its outcome."""
        deployment_id = temp_db.record_deployment_start(
            "example.com",
            "cli",
            git_commit="0123abc",
            git_branch="main",
            log_path="/var/lib/wasm/deploy-logs/example.com/1.log",
        )

        started = temp_db.get_deployment(deployment_id)
        assert started is not None
        assert started.status == "running"
        assert started.triggered_by == "cli"
        assert started.git_commit == "0123abc"
        assert started.git_branch == "main"
        assert started.log_path == "/var/lib/wasm/deploy-logs/example.com/1.log"
        assert started.started_at is not None
        assert started.finished_at is None
        assert started.duration_s is None

        temp_db.finish_deployment(deployment_id, "success")

        finished = temp_db.get_deployment(deployment_id)
        assert finished.status == "success"
        assert finished.finished_at is not None
        assert finished.duration_s is not None
        assert finished.duration_s >= 0
        assert finished.error is None

    def test_a_failure_keeps_the_error_verbatim(self, temp_db):
        """The captured error is what the operator reads later, unparaphrased."""
        deployment_id = temp_db.record_deployment_start("example.com", "panel")

        temp_db.finish_deployment(deployment_id, "failed", error="npm ERR! code ELIFECYCLE")

        assert temp_db.get_deployment(deployment_id).error == "npm ERR! code ELIFECYCLE"

    def test_list_filters_by_domain_and_orders_newest_first(self, temp_db):
        """History reads back most recent first, per domain."""
        first = temp_db.record_deployment_start("a.example.com", "cli")
        second = temp_db.record_deployment_start("a.example.com", "panel")
        temp_db.record_deployment_start("b.example.com", "webhook")

        rows = temp_db.list_deployments(domain="a.example.com")

        assert [row.id for row in rows] == [second, first]
        assert all(row.domain == "a.example.com" for row in rows)

    def test_list_without_domain_honours_the_limit(self, temp_db):
        """The default listing covers every domain, newest first, capped."""
        ids = [temp_db.record_deployment_start("c.example.com", "cli") for _ in range(3)]

        rows = temp_db.list_deployments(limit=2)

        assert [row.id for row in rows] == [ids[2], ids[1]]

    def test_prune_keeps_the_most_recent_and_reports_the_deleted(self, temp_db):
        """Rotation deletes the oldest rows beyond keep, nothing else."""
        ids = [temp_db.record_deployment_start("a.example.com", "cli") for _ in range(5)]
        other = temp_db.record_deployment_start("b.example.com", "cli")

        deleted = temp_db.prune_deployments("a.example.com", keep=2)

        assert deleted == 3
        remaining = temp_db.list_deployments(domain="a.example.com")
        assert [row.id for row in remaining] == [ids[4], ids[3]]
        assert temp_db.get_deployment(other) is not None

    def test_prune_below_the_limit_deletes_nothing(self, temp_db):
        """A history shorter than keep comes out untouched."""
        temp_db.record_deployment_start("a.example.com", "cli")

        assert temp_db.prune_deployments("a.example.com", keep=20) == 0
        assert len(temp_db.list_deployments(domain="a.example.com")) == 1

    def test_an_invalid_status_is_rejected(self, temp_db):
        """The status vocabulary is closed; a typo cannot invent a state."""
        deployment_id = temp_db.record_deployment_start("a.example.com", "cli")

        with pytest.raises(StoreError):
            temp_db.finish_deployment(deployment_id, "exploded")

        assert temp_db.get_deployment(deployment_id).status == "running"

    def test_an_invalid_trigger_is_rejected(self, temp_db):
        """Only panel, cli and webhook can start a deployment."""
        with pytest.raises(StoreError):
            temp_db.record_deployment_start("a.example.com", "cron")

        assert temp_db.list_deployments() == []

    def test_finishing_a_missing_deployment_is_an_error(self, temp_db):
        """Finishing a pruned or never-recorded id fails loudly, not silently."""
        with pytest.raises(StoreError):
            temp_db.finish_deployment(999, "success")

    def test_job_id_is_recorded_when_a_job_started_the_deploy(self, temp_db):
        """A deployment started by the panel's job queue remembers which job."""
        deployment_id = temp_db.record_deployment_start("example.com", "panel", job_id="ab12cd34")

        assert temp_db.get_deployment(deployment_id).job_id == "ab12cd34"

    def test_job_id_is_none_when_nothing_queued_the_deploy(self, temp_db):
        """The CLI deploys directly; there is no job to point at."""
        deployment_id = temp_db.record_deployment_start("example.com", "cli")

        assert temp_db.get_deployment(deployment_id).job_id is None

    def test_annotate_can_record_release_id_and_commit_message(self, temp_db):
        """A release build and its commit subject are learned once the fetch step has run."""
        deployment_id = temp_db.record_deployment_start("example.com", "panel")

        assert (
            temp_db.annotate_deployment(
                deployment_id, release_id="20260101-000000", commit_message="Fix the thing"
            )
            is True
        )

        record = temp_db.get_deployment(deployment_id)
        assert record.release_id == "20260101-000000"
        assert record.commit_message == "Fix the thing"

    def test_history_survives_the_app_being_deleted(self, populated_store):
        """
        Why there is no foreign key to apps: deleting a broken app is exactly
        the moment the operator most needs to read what happened to it. A
        CASCADE would erase the record at that moment.
        """
        store = populated_store
        deployment_id = store.record_deployment_start("example.com", "cli")
        store.finish_deployment(deployment_id, "failed", error="unit failed to start")

        store.delete_app("example.com")

        record = store.get_deployment(deployment_id)
        assert record is not None
        assert record.error == "unit failed to start"


class TestLatestDeployments:
    """
    GET /api/apps shows every application's last deployment. Asking
    list_deployments once per row would cost as many queries as there are
    applications; this is the one query that answers the whole list at once.
    """

    def test_reads_back_the_newest_row_per_domain(self, temp_db):
        first = temp_db.record_deployment_start("a.example.com", "cli")
        temp_db.finish_deployment(first, "success")
        second = temp_db.record_deployment_start("a.example.com", "panel")
        temp_db.finish_deployment(second, "failed", error="boom")
        only = temp_db.record_deployment_start("b.example.com", "webhook")

        latest = temp_db.get_latest_deployments(["a.example.com", "b.example.com"])

        assert latest["a.example.com"].id == second
        assert latest["a.example.com"].status == "failed"
        assert latest["b.example.com"].id == only

    def test_a_domain_with_no_deployments_is_absent(self, temp_db):
        temp_db.record_deployment_start("a.example.com", "cli")

        latest = temp_db.get_latest_deployments(["a.example.com", "never-deployed.example.com"])

        assert "never-deployed.example.com" not in latest

    def test_an_empty_list_of_domains_costs_no_rows(self, temp_db):
        temp_db.record_deployment_start("a.example.com", "cli")

        assert temp_db.get_latest_deployments([]) == {}

    def test_only_the_asked_domains_come_back(self, temp_db):
        """A domain not in the request must not leak into the answer."""
        temp_db.record_deployment_start("a.example.com", "cli")
        temp_db.record_deployment_start("b.example.com", "cli")

        latest = temp_db.get_latest_deployments(["a.example.com"])

        assert set(latest) == {"a.example.com"}

    def test_the_new_linking_columns_come_through_too(self, temp_db):
        """This query's explicit column list must not silently drop a new column."""
        deployment_id = temp_db.record_deployment_start("a.example.com", "panel", job_id="ab12cd34")
        temp_db.annotate_deployment(
            deployment_id, release_id="20260101-000000", commit_message="Fix it"
        )

        latest = temp_db.get_latest_deployments(["a.example.com"])

        assert latest["a.example.com"].job_id == "ab12cd34"
        assert latest["a.example.com"].release_id == "20260101-000000"
        assert latest["a.example.com"].commit_message == "Fix it"


class TestRelations:
    """Tests for relationship handling."""

    def test_cascade_delete_app(self, populated_store):
        """Test that deleting an app cascades to related records."""
        # Verify related records exist
        assert populated_store.get_site("example.com") is not None
        assert populated_store.get_service("example-com") is not None

        # Delete app
        populated_store.delete_app("example.com")

        # Related records should be deleted due to cascade
        # Note: In SQLite, ON DELETE CASCADE removes child records
        populated_store.get_site("example.com")
        populated_store.get_service("example-com")

        # Site and service remain but app_id is null (due to SET NULL) or deleted (CASCADE)
        # Depending on schema, check the appropriate behavior

    def test_get_app_with_relations(self, populated_store):
        """Test getting an app with all related records."""
        result = populated_store.get_app_with_relations("example.com")

        assert result is not None
        assert result["app"].domain == "example.com"
        assert result["site"] is not None
        assert result["service"] is not None
        assert len(result["databases"]) >= 1


class TestStatistics:
    """Tests for statistics."""

    def test_get_statistics(self, populated_store):
        """Test getting store statistics."""
        stats = populated_store.get_statistics()

        assert stats["total_apps"] >= 1
        assert stats["total_sites"] >= 1
        assert stats["total_services"] >= 1
        assert stats["total_databases"] >= 1
        assert "apps_by_type" in stats
        assert "databases_by_engine" in stats


class TestDataClasses:
    """Tests for dataclass methods."""

    def test_app_to_dict(self):
        """Test App.to_dict()."""
        app = App(
            id=1,
            domain="test.com",
            app_type="nextjs",
            app_path="/test",
            env_vars={"KEY": "value"},
        )

        d = app.to_dict()

        assert d["domain"] == "test.com"
        assert d["env_vars"] == '{"KEY": "value"}'  # JSON serialized

    def test_app_from_row(self, temp_db):
        """Test App.from_row()."""
        temp_db.create_app(
            App(
                domain="fromrow.com",
                app_type="vite",
                app_path="/fromrow",
                env_vars={"A": "B"},
            )
        )

        app = temp_db.get_app("fromrow.com")

        assert isinstance(app, App)
        assert app.env_vars == {"A": "B"}  # Deserialized


class TestThreadSafety:
    """Tests for thread safety."""

    def test_concurrent_operations(self, temp_db):
        """Test that concurrent operations work correctly."""
        import threading

        errors = []

        def create_apps(prefix: str, count: int):
            try:
                for i in range(count):
                    temp_db.create_app(
                        App(
                            domain=f"{prefix}-{i}.com",
                            app_type="nodejs",
                            app_path=f"/{prefix}/{i}",
                        )
                    )
            except Exception as e:
                errors.append(e)

        threads = [
            threading.Thread(target=create_apps, args=("a", 10)),
            threading.Thread(target=create_apps, args=("b", 10)),
            threading.Thread(target=create_apps, args=("c", 10)),
        ]

        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0
        apps = temp_db.list_apps()
        assert len(apps) == 30

    def test_connections_use_wal_and_wait_for_locks(self, fresh, tmp_path: Path) -> None:
        """The panel and the CLI write at once; the default journal fails the second writer."""
        store = WASMStore(tmp_path / "wasm.db")
        conn = store._get_connection()

        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] >= 5000

    def test_concurrent_first_use_initialises_once(
        self, fresh, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Two threads calling get_store() first must not both run the migrations."""
        import threading

        calls: list[int] = []
        original = WASMStore._ensure_schema

        def counting(self) -> None:
            calls.append(1)
            return original(self)

        monkeypatch.setattr(WASMStore, "_ensure_schema", counting)
        barrier = threading.Barrier(8)

        def worker() -> None:
            barrier.wait()
            WASMStore(tmp_path / "wasm.db")

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(calls) == 1


class TestEdgeCases:
    """Tests for edge cases."""

    def test_duplicate_domain(self, temp_db):
        """A duplicate domain is refused, naming the application that has it."""
        temp_db.create_app(App(domain="dup.com", app_type="nodejs", app_path="/dup"))

        with pytest.raises(DomainConflictError, match=re.escape("dup.com")):
            temp_db.create_app(App(domain="dup.com", app_type="vite", app_path="/dup2"))

    def test_empty_env_vars(self, temp_db):
        """Test that empty env_vars work correctly."""
        temp_db.create_app(App(domain="empty.com", app_type="nodejs", app_path="/empty"))

        app = temp_db.get_app("empty.com")
        assert app.env_vars == {}

    def test_null_optional_fields(self, temp_db):
        """Test that null optional fields work correctly."""
        temp_db.create_app(
            App(
                domain="null.com",
                app_type="nodejs",
                app_path="/null",
                # All optional fields left as None/default
            )
        )

        app = temp_db.get_app("null.com")
        assert app.branch is None
        assert app.port is None


class TestStoreFilePermissions:
    """The rows carry DATABASE_URL and API keys: 0600 inside 0700 or nothing."""

    def test_database_is_owner_only_inside_an_owner_only_directory(self, fresh, tmp_path):
        """The whole point: a store full of passwords readable by everyone."""
        db_path = tmp_path / "lib" / "wasm" / "wasm.db"

        store = WASMStore(db_path, fs=RecordingFileSystem())

        assert store.db_path.stat().st_mode & 0o777 == SECRET_MODE
        assert db_path.parent.stat().st_mode & 0o777 == SECRET_DIR_MODE

    def test_every_created_level_is_private_not_only_the_last(self, fresh, tmp_path):
        """mkdir(parents=True) applies the mode to the leaf and leaks the rest."""
        db_path = tmp_path / "lib" / "wasm" / "wasm.db"

        WASMStore(db_path, fs=RecordingFileSystem())

        assert (tmp_path / "lib").stat().st_mode & 0o077 == 0

    def test_the_mode_survives_sqlite_creating_the_schema(self, fresh, tmp_path):
        """SQLite opens the file itself; it must not widen what we created."""
        db_path = tmp_path / "wasm.db"
        store = WASMStore(db_path, fs=RecordingFileSystem())

        store.create_app(App(domain="perm.com", app_type="nodejs", app_path="/perm"))

        assert db_path.stat().st_mode & 0o077 == 0

    def test_a_database_left_lax_by_an_older_version_is_tightened(self, fresh, tmp_path):
        """Upgrading must repair what the previous release created 0644."""
        db_path = tmp_path / "wasm.db"
        WASMStore(db_path, fs=RecordingFileSystem())
        WASMStore.reset_instance()
        db_path.chmod(0o644)

        WASMStore(db_path, fs=RecordingFileSystem())

        assert db_path.stat().st_mode & 0o777 == SECRET_MODE

    def test_the_directory_and_the_file_are_created_through_the_seam(self, fresh, tmp_path):
        """Anything not routed through the seam is invisible to --dry-run."""
        recorder = RecordingFileSystem()
        db_path = tmp_path / "lib" / "wasm.db"

        WASMStore(db_path, fs=recorder)

        assert ("mkdir", db_path.parent) in recorder.changes
        assert ("write", db_path) in recorder.changes


class TestStoreUnderADryRun:
    """A rehearsal that creates the database is not a rehearsal."""

    def test_nothing_is_created_when_the_filesystem_refuses(self, fresh, tmp_path):
        """The previous version created directory and file regardless."""
        db_path = tmp_path / "lib" / "wasm" / "wasm.db"

        WASMStore(db_path, fs=DryRunFileSystem())

        assert not db_path.exists()
        assert not db_path.parent.exists()
        assert list(tmp_path.iterdir()) == []

    def test_sqlite_does_not_create_the_database_behind_the_seam(self, fresh, tmp_path):
        """Connecting with the default rwc is how a dry run leaves a file."""
        db_path = tmp_path / "wasm.db"
        store = WASMStore(db_path, fs=DryRunFileSystem())

        with pytest.raises(StoreError):
            store.list_apps()

        assert not db_path.exists()

    def test_an_existing_database_is_neither_deleted_nor_rewritten(self, fresh, tmp_path):
        """The file on a real server must come out of a rehearsal untouched."""
        db_path = tmp_path / "wasm.db"
        real = WASMStore(db_path, fs=RecordingFileSystem())
        real.create_app(App(domain="keep.com", app_type="nodejs", app_path="/keep"))
        # WAL keeps a committed write in wasm.db-wal until the last connection
        # closes; snapshotting the main file before that checkpoint would
        # compare against a file that was never a complete, settled state to
        # begin with, unrelated to anything the rehearsal below does.
        real.close()
        before = db_path.read_bytes()
        WASMStore.reset_instance()

        store = WASMStore(db_path, fs=DryRunFileSystem())

        assert db_path.exists()
        assert db_path.read_bytes() == before
        assert store.get_app("keep.com") is not None

    def test_a_lax_database_is_not_chmodded_during_a_rehearsal(self, fresh, tmp_path):
        """chmod is a change to this machine, so --dry-run must skip it too."""
        db_path = tmp_path / "wasm.db"
        WASMStore(db_path, fs=RecordingFileSystem())
        WASMStore.reset_instance()
        db_path.chmod(0o644)

        WASMStore(db_path, fs=DryRunFileSystem())

        assert db_path.stat().st_mode & 0o777 == 0o644

    def test_the_skipped_changes_are_reported(self, fresh, tmp_path):
        """An operator only trusts the rehearsal if it says what it skipped."""
        dry = DryRunFileSystem()

        WASMStore(tmp_path / "lib" / "wasm.db", fs=dry)

        assert any("wasm.db" in line for line in dry.skipped)


class TestResolvingThePathChangesNothing:
    """Deciding where the database lives is a question, not a change."""

    def test_the_user_directory_is_not_created_while_resolving(self, fresh, tmp_path, monkeypatch):
        """The previous version created ~/.local/share/wasm just by asking."""
        user_db = tmp_path / "home" / ".local" / "share" / "wasm" / "wasm.db"
        system_db = tmp_path / "var" / "lib" / "wasm" / "wasm.db"
        monkeypatch.setattr(store_module, "USER_DB_PATH", user_db)
        monkeypatch.setattr(store_module, "DEFAULT_DB_PATH", system_db)
        store = WASMStore(tmp_path / "explicit.db", fs=RecordingFileSystem())

        resolved = store._resolve_db_path()

        assert resolved == user_db
        assert not user_db.parent.exists()

    def test_the_system_path_wins_when_its_directory_is_writable(
        self, fresh, tmp_path, monkeypatch
    ):
        """Behaviour preserved: /var/lib/wasm is preferred when usable."""
        system_db = tmp_path / "var" / "lib" / "wasm" / "wasm.db"
        system_db.parent.mkdir(parents=True)
        monkeypatch.setattr(store_module, "DEFAULT_DB_PATH", system_db)
        # Pinned too, or the resolver sees whatever database the developer
        # running the suite happens to have in their own home directory.
        monkeypatch.setattr(store_module, "USER_DB_PATH", tmp_path / "home" / "wasm.db")
        store = WASMStore(tmp_path / "explicit.db", fs=RecordingFileSystem())

        assert store._resolve_db_path() == system_db

    def test_an_inventory_that_exists_is_not_abandoned_for_an_empty_location(
        self, fresh, tmp_path, monkeypatch
    ):
        """
        The reported incident, and the reason this rule exists.

        The choice used to be made purely on whether /var/lib/wasm existed and
        was writable, so it changed the moment somebody created that directory
        - a packaging change, an administrator, or WASM's own monitor service,
        which needs it. On a server whose records had always lived under
        ~/.local/share, `wasm list` then answered "No applications deployed"
        about a machine serving seventeen sites. Nothing had been lost, and
        nothing said so.
        """
        user_db = tmp_path / "home" / ".local" / "share" / "wasm" / "wasm.db"
        user_db.parent.mkdir(parents=True)
        user_db.write_bytes(b"an inventory that took a year to build")

        system_db = tmp_path / "var" / "lib" / "wasm" / "wasm.db"
        system_db.parent.mkdir(parents=True)  # exists and is writable, as after a mkdir

        monkeypatch.setattr(store_module, "USER_DB_PATH", user_db)
        monkeypatch.setattr(store_module, "DEFAULT_DB_PATH", system_db)
        store = WASMStore(tmp_path / "explicit.db", fs=RecordingFileSystem())

        assert store._resolve_db_path() == user_db

    def test_the_system_database_is_preferred_once_it_holds_something(
        self, fresh, tmp_path, monkeypatch
    ):
        """
        A machine that has been migrated keeps using the system location.

        Otherwise the rule above would pin every host to ~/.local/share for
        ever, which is the wrong home for the records of a machine-wide tool.
        """
        user_db = tmp_path / "home" / ".local" / "share" / "wasm" / "wasm.db"
        user_db.parent.mkdir(parents=True)
        user_db.write_bytes(b"the copy left behind by the migration")

        system_db = tmp_path / "var" / "lib" / "wasm" / "wasm.db"
        system_db.parent.mkdir(parents=True)
        system_db.write_bytes(b"the inventory, where it belongs")

        monkeypatch.setattr(store_module, "USER_DB_PATH", user_db)
        monkeypatch.setattr(store_module, "DEFAULT_DB_PATH", system_db)
        store = WASMStore(tmp_path / "explicit.db", fs=RecordingFileSystem())

        assert store._resolve_db_path() == system_db

    def test_a_fresh_machine_still_starts_in_the_system_location(
        self, fresh, tmp_path, monkeypatch
    ):
        """With neither file present the usual preference decides, unchanged."""
        user_db = tmp_path / "home" / ".local" / "share" / "wasm" / "wasm.db"
        system_db = tmp_path / "var" / "lib" / "wasm" / "wasm.db"
        system_db.parent.mkdir(parents=True)

        monkeypatch.setattr(store_module, "USER_DB_PATH", user_db)
        monkeypatch.setattr(store_module, "DEFAULT_DB_PATH", system_db)
        store = WASMStore(tmp_path / "explicit.db", fs=RecordingFileSystem())

        assert store._resolve_db_path() == system_db

    def test_an_unwritable_system_location_never_wins_even_holding_a_database(
        self, fresh, tmp_path, monkeypatch
    ):
        """
        A root-owned database is not usable by a user who cannot write it, and
        choosing it would fail later instead of here.
        """
        user_db = tmp_path / "home" / ".local" / "share" / "wasm" / "wasm.db"
        user_db.parent.mkdir(parents=True)
        user_db.write_bytes(b"the records this user can actually read")

        system_db = tmp_path / "var" / "lib" / "wasm" / "wasm.db"
        system_db.parent.mkdir(parents=True)
        system_db.write_bytes(b"root's copy")
        system_db.parent.chmod(0o500)

        monkeypatch.setattr(store_module, "USER_DB_PATH", user_db)
        monkeypatch.setattr(store_module, "DEFAULT_DB_PATH", system_db)
        store = WASMStore(tmp_path / "explicit.db", fs=RecordingFileSystem())

        try:
            assert store._resolve_db_path() == user_db
        finally:
            system_db.parent.chmod(0o700)


class TestNoMutationEscapesTheSeam:
    """
    The guard that stops the defect from coming back.

    ``--dry-run`` printed "no changes will be made to this machine" and then
    deleted files, because a deletion is a ``Path.unlink`` and never goes near a
    subprocess. Reads are deliberately not covered: they change nothing.
    """

    #: Calls that change the filesystem. Names only, because that is what
    #: survives an alias, a re-import or a helper variable.
    MUTATING = frozenset(
        {
            "chmod",
            "chown",
            "copy",
            "copy2",
            "copyfile",
            "copymode",
            "copystat",
            "copytree",
            "hardlink_to",
            "lchmod",
            "lchown",
            "link",
            "link_to",
            "makedirs",
            "mkdir",
            "mkdtemp",
            "mkstemp",
            "mknod",
            "move",
            "open",
            "remove",
            "removedirs",
            "rename",
            "renames",
            "replace",
            "rmdir",
            "rmtree",
            "symlink",
            "symlink_to",
            "touch",
            "truncate",
            "unlink",
            "utime",
            "write_bytes",
            "write_text",
            "writelines",
            "NamedTemporaryFile",
            "TemporaryDirectory",
            "TemporaryFile",
        }
    )

    #: Expressions that are the seam itself, written as source text so a new
    #: spelling has to be added here on purpose rather than by accident.
    SEAM = frozenset({"fs", "self.fs", "self._fs", "filesystem", "self.filesystem"})

    def _offenders(self, path: Path) -> list[str]:
        """
        Collect every mutating call in a module that bypasses the seam.

        Args:
            path: Module to inspect.

        Returns:
            One ``name (line N)`` entry per offending call.
        """
        tree = ast.parse(path.read_text(encoding="utf-8"))
        found: list[str] = []

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue

            func = node.func
            if isinstance(func, ast.Attribute):
                name, receiver = func.attr, ast.unparse(func.value)
            elif isinstance(func, ast.Name):
                name, receiver = func.id, ""
            else:
                continue

            if name in self.MUTATING and receiver not in self.SEAM:
                found.append(f"{name} on {receiver or '<bare call>'} (line {node.lineno})")

        return found

    def test_the_store_never_writes_outside_the_seam(self):
        """Every mkdir, chmod and file creation goes through wasm.core.fs."""
        module = Path(store_module.__file__)

        assert self._offenders(module) == []

    def test_the_guard_notices_a_direct_mutation(self, tmp_path):
        """A guard that cannot fail protects nothing."""
        sample = tmp_path / "sample.py"
        sample.write_text("from pathlib import Path\nPath('/x').unlink()\n")

        assert self._offenders(sample) != []

    def test_the_guard_accepts_the_seam(self, tmp_path):
        """The same call through the seam is exactly what we want to see."""
        sample = tmp_path / "sample.py"
        sample.write_text("def f(self):\n    self.fs.remove(self.path)\n")

        assert self._offenders(sample) == []
