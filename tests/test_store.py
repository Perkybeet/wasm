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
import sqlite3
import tempfile
from pathlib import Path

import pytest

from wasm.core import store as store_module
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


class TestEdgeCases:
    """Tests for edge cases."""

    def test_duplicate_domain(self, temp_db):
        """Test that duplicate domains raise an error."""
        temp_db.create_app(App(domain="dup.com", app_type="nodejs", app_path="/dup"))

        with pytest.raises(sqlite3.IntegrityError):
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
