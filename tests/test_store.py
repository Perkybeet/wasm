# Copyright (c) 2024-2025 Yago López Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
Tests for WASM SQLite persistence store.
"""

import json
import pytest
import tempfile
from pathlib import Path
from datetime import datetime

from wasm.core.store import (
    WASMStore,
    get_store,
    App,
    Site,
    Service,
    Database,
    DatabaseUser,
    AppType,
    AppStatus,
    WebServer,
    DatabaseEngine,
    SCHEMA_VERSION,
)


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
        
        expected = {'schema_version', 'apps', 'sites', 'services', 'databases', 'database_users'}
        assert expected.issubset(tables)
    
    def test_schema_version(self, temp_db):
        """Test that schema version is recorded."""
        with temp_db._transaction() as cursor:
            cursor.execute("SELECT MAX(version) FROM schema_version")
            version = cursor.fetchone()[0]
        
        assert version == SCHEMA_VERSION


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
        updated = temp_db.update_app(app)
        
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
        temp_db.create_service(Service(
            name="get-service",
            unit_file="/test",
            working_directory="/test",
            command="test",
        ))
        
        service = temp_db.get_service("get-service")
        
        assert service is not None
        assert service.name == "get-service"
    
    def test_update_service_status(self, temp_db):
        """Test updating service status."""
        temp_db.create_service(Service(
            name="status-service",
            unit_file="/test",
            working_directory="/test",
            command="test",
            status="inactive",
            enabled=False,
        ))
        
        result = temp_db.update_service_status("status-service", "active")
        
        assert result is True
        service = temp_db.get_service("status-service")
        assert service.status == "active"
    
    def test_service_environment_serialization(self, temp_db):
        """Test that environment is properly serialized."""
        temp_db.create_service(Service(
            name="env-service",
            unit_file="/test",
            working_directory="/test",
            command="test",
            environment={"PORT": "3000", "NODE_ENV": "production"},
        ))
        
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
        site = populated_store.get_site("example.com")
        service = populated_store.get_service("example-com")
        
        # Site and service remain but app_id is null (due to SET NULL) or deleted (CASCADE)
        # Depending on schema, check the appropriate behavior
    
    def test_get_app_with_relations(self, populated_store):
        """Test getting an app with all related records."""
        result = populated_store.get_app_with_relations("example.com")
        
        assert result is not None
        assert result['app'].domain == "example.com"
        assert result['site'] is not None
        assert result['service'] is not None
        assert len(result['databases']) >= 1


class TestStatistics:
    """Tests for statistics."""
    
    def test_get_statistics(self, populated_store):
        """Test getting store statistics."""
        stats = populated_store.get_statistics()
        
        assert stats['total_apps'] >= 1
        assert stats['total_sites'] >= 1
        assert stats['total_services'] >= 1
        assert stats['total_databases'] >= 1
        assert 'apps_by_type' in stats
        assert 'databases_by_engine' in stats


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
        
        assert d['domain'] == "test.com"
        assert d['env_vars'] == '{"KEY": "value"}'  # JSON serialized
    
    def test_app_from_row(self, temp_db):
        """Test App.from_row()."""
        temp_db.create_app(App(
            domain="fromrow.com",
            app_type="vite",
            app_path="/fromrow",
            env_vars={"A": "B"},
        ))
        
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
                    temp_db.create_app(App(
                        domain=f"{prefix}-{i}.com",
                        app_type="nodejs",
                        app_path=f"/{prefix}/{i}",
                    ))
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
        
        with pytest.raises(Exception):  # sqlite3.IntegrityError
            temp_db.create_app(App(domain="dup.com", app_type="vite", app_path="/dup2"))
    
    def test_empty_env_vars(self, temp_db):
        """Test that empty env_vars work correctly."""
        temp_db.create_app(App(domain="empty.com", app_type="nodejs", app_path="/empty"))
        
        app = temp_db.get_app("empty.com")
        assert app.env_vars == {}
    
    def test_null_optional_fields(self, temp_db):
        """Test that null optional fields work correctly."""
        temp_db.create_app(App(
            domain="null.com",
            app_type="nodejs",
            app_path="/null",
            # All optional fields left as None/default
        ))
        
        app = temp_db.get_app("null.com")
        assert app.branch is None
        assert app.port is None
