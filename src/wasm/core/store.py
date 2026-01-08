# Copyright (c) 2024-2025 Yago López Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
SQLite persistence layer for WASM.

Provides a centralized store for all WASM-managed resources:
- Applications (deployed web apps)
- Sites (Nginx/Apache configurations)
- Services (systemd services)
- Databases (MySQL, PostgreSQL, Redis, MongoDB)
"""

import sqlite3
import json
from contextlib import contextmanager
from dataclasses import dataclass, field, asdict
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Iterator
import threading


# Database location
DEFAULT_DB_PATH = Path("/var/lib/wasm/wasm.db")
USER_DB_PATH = Path.home() / ".local/share/wasm/wasm.db"


class AppType(str, Enum):
    """Application type enumeration."""
    NEXTJS = "nextjs"
    NODEJS = "nodejs"
    PYTHON = "python"
    VITE = "vite"
    STATIC = "static"
    UNKNOWN = "unknown"


class AppStatus(str, Enum):
    """Application status enumeration."""
    DEPLOYING = "deploying"
    RUNNING = "running"
    STOPPED = "stopped"
    FAILED = "failed"
    UNKNOWN = "unknown"


class WebServer(str, Enum):
    """Web server type enumeration."""
    NGINX = "nginx"
    APACHE = "apache"


class DatabaseEngine(str, Enum):
    """Database engine enumeration."""
    MYSQL = "mysql"
    POSTGRESQL = "postgresql"
    REDIS = "redis"
    MONGODB = "mongodb"


@dataclass
class App:
    """Application record."""
    id: Optional[int] = None
    domain: str = ""
    app_type: str = AppType.UNKNOWN.value
    source: str = ""
    branch: Optional[str] = None
    port: Optional[int] = None
    app_path: str = ""
    webserver: str = WebServer.NGINX.value
    ssl_enabled: bool = True
    ssl_certificate: Optional[str] = None
    ssl_key: Optional[str] = None
    status: str = AppStatus.UNKNOWN.value
    is_static: bool = False
    env_vars: Dict[str, str] = field(default_factory=dict)
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    deployed_at: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        d = asdict(self)
        if isinstance(d.get('env_vars'), dict):
            d['env_vars'] = json.dumps(d['env_vars'])
        return d

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "App":
        """Create from database row."""
        data = dict(row)
        if data.get('env_vars'):
            try:
                data['env_vars'] = json.loads(data['env_vars'])
            except (json.JSONDecodeError, TypeError):
                data['env_vars'] = {}
        return cls(**data)


@dataclass
class Site:
    """Site configuration record."""
    id: Optional[int] = None
    app_id: Optional[int] = None
    domain: str = ""
    webserver: str = WebServer.NGINX.value
    config_path: str = ""
    enabled: bool = True
    is_static: bool = False
    document_root: Optional[str] = None
    proxy_port: Optional[int] = None
    ssl_enabled: bool = False
    ssl_certificate: Optional[str] = None
    ssl_key: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return asdict(self)

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Site":
        """Create from database row."""
        return cls(**dict(row))


@dataclass
class Service:
    """Systemd service record."""
    id: Optional[int] = None
    app_id: Optional[int] = None
    name: str = ""
    unit_file: str = ""
    working_directory: str = ""
    command: str = ""
    user: str = "www-data"
    group: str = "www-data"
    enabled: bool = True
    status: str = "inactive"  # active, inactive, failed
    port: Optional[int] = None
    environment: Dict[str, str] = field(default_factory=dict)
    created_at: Optional[str] = None
    updated_at: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        d = asdict(self)
        if isinstance(d.get('environment'), dict):
            d['environment'] = json.dumps(d['environment'])
        return d

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Service":
        """Create from database row."""
        data = dict(row)
        if data.get('environment'):
            try:
                data['environment'] = json.loads(data['environment'])
            except (json.JSONDecodeError, TypeError):
                data['environment'] = {}
        return cls(**data)


@dataclass
class Database:
    """Database record."""
    id: Optional[int] = None
    app_id: Optional[int] = None
    name: str = ""
    engine: str = DatabaseEngine.MYSQL.value
    host: str = "localhost"
    port: Optional[int] = None
    username: Optional[str] = None
    encoding: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return asdict(self)

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Database":
        """Create from database row."""
        return cls(**dict(row))


@dataclass
class DatabaseUser:
    """Database user record."""
    id: Optional[int] = None
    database_id: Optional[int] = None
    username: str = ""
    engine: str = DatabaseEngine.MYSQL.value
    host: str = "localhost"
    privileges: str = "ALL"
    created_at: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return asdict(self)

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "DatabaseUser":
        """Create from database row."""
        return cls(**dict(row))


# Schema version for migrations
SCHEMA_VERSION = 1

SCHEMA_SQL = """
-- Schema version tracking
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Applications table
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

-- Sites table (Nginx/Apache configurations)
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

-- Services table (systemd services)
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

-- Databases table
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

-- Database users table
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

-- Indexes for performance
CREATE INDEX IF NOT EXISTS idx_apps_domain ON apps(domain);
CREATE INDEX IF NOT EXISTS idx_apps_status ON apps(status);
CREATE INDEX IF NOT EXISTS idx_sites_domain ON sites(domain);
CREATE INDEX IF NOT EXISTS idx_sites_app_id ON sites(app_id);
CREATE INDEX IF NOT EXISTS idx_services_app_id ON services(app_id);
CREATE INDEX IF NOT EXISTS idx_services_name ON services(name);
CREATE INDEX IF NOT EXISTS idx_databases_engine ON databases(engine);
CREATE INDEX IF NOT EXISTS idx_databases_app_id ON databases(app_id);
"""


class WASMStore:
    """
    SQLite-based persistence store for WASM.
    
    Thread-safe singleton that manages all WASM resources.
    """
    
    _instance: Optional["WASMStore"] = None
    _lock = threading.Lock()
    
    def __new__(cls, db_path: Optional[Path] = None) -> "WASMStore":
        """Singleton pattern with thread safety."""
        with cls._lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._initialized = False
            return cls._instance
    
    def __init__(self, db_path: Optional[Path] = None):
        """
        Initialize the store.
        
        Args:
            db_path: Custom database path. Defaults to system or user path.
        """
        if self._initialized:
            return
        
        self._db_path = self._resolve_db_path(db_path)
        self._local = threading.local()
        self._ensure_schema()
        self._initialized = True
    
    def _resolve_db_path(self, db_path: Optional[Path] = None) -> Path:
        """
        Resolve the database path.
        
        Priority:
        1. Explicit path provided
        2. System path if writable (/var/lib/wasm/)
        3. User path (~/.local/share/wasm/)
        """
        if db_path:
            return Path(db_path)
        
        # Try system path first
        if DEFAULT_DB_PATH.parent.exists():
            try:
                # Check if we can write to system path
                DEFAULT_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
                return DEFAULT_DB_PATH
            except PermissionError:
                pass
        
        # Fall back to user path
        USER_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        return USER_DB_PATH
    
    @property
    def db_path(self) -> Path:
        """Get the database file path."""
        return self._db_path
    
    def _get_connection(self) -> sqlite3.Connection:
        """Get a thread-local database connection."""
        if not hasattr(self._local, 'connection') or self._local.connection is None:
            self._local.connection = sqlite3.connect(
                str(self._db_path),
                check_same_thread=False,
            )
            self._local.connection.row_factory = sqlite3.Row
            # Enable foreign keys
            self._local.connection.execute("PRAGMA foreign_keys = ON")
        return self._local.connection
    
    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Cursor]:
        """Context manager for database transactions."""
        conn = self._get_connection()
        cursor = conn.cursor()
        try:
            yield cursor
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    
    def _ensure_schema(self) -> None:
        """Ensure database schema exists and is up to date."""
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        
        with self._transaction() as cursor:
            # Check if schema_version table exists
            cursor.execute("""
                SELECT name FROM sqlite_master 
                WHERE type='table' AND name='schema_version'
            """)
            
            if not cursor.fetchone():
                # Fresh install - create all tables
                cursor.executescript(SCHEMA_SQL)
                cursor.execute(
                    "INSERT INTO schema_version (version) VALUES (?)",
                    (SCHEMA_VERSION,)
                )
            else:
                # Check for migrations
                cursor.execute("SELECT MAX(version) FROM schema_version")
                current_version = cursor.fetchone()[0] or 0
                
                if current_version < SCHEMA_VERSION:
                    self._run_migrations(cursor, current_version)
    
    def _run_migrations(self, cursor: sqlite3.Cursor, from_version: int) -> None:
        """
        Run database migrations.
        
        Args:
            cursor: Database cursor.
            from_version: Current schema version.
        """
        # Migration functions will be added as schema evolves
        migrations = {
            # 1: self._migrate_v1_to_v2,
        }
        
        for version in range(from_version + 1, SCHEMA_VERSION + 1):
            if version in migrations:
                migrations[version](cursor)
            cursor.execute(
                "INSERT INTO schema_version (version) VALUES (?)",
                (version,)
            )
    
    # =========================================================================
    # Application CRUD
    # =========================================================================
    
    def create_app(self, app: App) -> App:
        """
        Create a new application record.
        
        Args:
            app: Application data.
            
        Returns:
            Created application with ID.
        """
        now = datetime.now().isoformat()
        app.created_at = now
        app.updated_at = now
        
        with self._transaction() as cursor:
            data = app.to_dict()
            del data['id']  # Let SQLite auto-generate
            
            columns = ', '.join(data.keys())
            placeholders = ', '.join(['?' for _ in data])
            
            cursor.execute(
                f"INSERT INTO apps ({columns}) VALUES ({placeholders})",
                list(data.values())
            )
            app.id = cursor.lastrowid
        
        return app
    
    def get_app(self, domain: str) -> Optional[App]:
        """
        Get application by domain.
        
        Args:
            domain: Application domain.
            
        Returns:
            Application or None if not found.
        """
        with self._transaction() as cursor:
            cursor.execute("SELECT * FROM apps WHERE domain = ?", (domain,))
            row = cursor.fetchone()
            return App.from_row(row) if row else None
    
    def get_app_by_id(self, app_id: int) -> Optional[App]:
        """
        Get application by ID.
        
        Args:
            app_id: Application ID.
            
        Returns:
            Application or None if not found.
        """
        with self._transaction() as cursor:
            cursor.execute("SELECT * FROM apps WHERE id = ?", (app_id,))
            row = cursor.fetchone()
            return App.from_row(row) if row else None
    
    def list_apps(
        self,
        status: Optional[str] = None,
        app_type: Optional[str] = None,
    ) -> List[App]:
        """
        List all applications.
        
        Args:
            status: Filter by status.
            app_type: Filter by application type.
            
        Returns:
            List of applications.
        """
        query = "SELECT * FROM apps WHERE 1=1"
        params = []
        
        if status:
            query += " AND status = ?"
            params.append(status)
        if app_type:
            query += " AND app_type = ?"
            params.append(app_type)
        
        query += " ORDER BY created_at DESC"
        
        with self._transaction() as cursor:
            cursor.execute(query, params)
            return [App.from_row(row) for row in cursor.fetchall()]
    
    def update_app(self, app: App) -> App:
        """
        Update an application.
        
        Args:
            app: Application with updated data.
            
        Returns:
            Updated application.
        """
        app.updated_at = datetime.now().isoformat()
        
        with self._transaction() as cursor:
            data = app.to_dict()
            app_id = data.pop('id')
            data.pop('created_at')  # Don't update created_at
            
            set_clause = ', '.join([f"{k} = ?" for k in data.keys()])
            
            cursor.execute(
                f"UPDATE apps SET {set_clause} WHERE id = ?",
                list(data.values()) + [app_id]
            )
        
        return app
    
    def update_app_status(self, domain: str, status: str) -> bool:
        """
        Update application status.
        
        Args:
            domain: Application domain.
            status: New status.
            
        Returns:
            True if updated.
        """
        with self._transaction() as cursor:
            cursor.execute(
                "UPDATE apps SET status = ?, updated_at = ? WHERE domain = ?",
                (status, datetime.now().isoformat(), domain)
            )
            return cursor.rowcount > 0
    
    def delete_app(self, domain: str) -> bool:
        """
        Delete an application and all related records.
        
        Args:
            domain: Application domain.
            
        Returns:
            True if deleted.
        """
        with self._transaction() as cursor:
            cursor.execute("DELETE FROM apps WHERE domain = ?", (domain,))
            return cursor.rowcount > 0
    
    def app_exists(self, domain: str) -> bool:
        """Check if an application exists."""
        with self._transaction() as cursor:
            cursor.execute(
                "SELECT 1 FROM apps WHERE domain = ?",
                (domain,)
            )
            return cursor.fetchone() is not None
    
    # =========================================================================
    # Site CRUD
    # =========================================================================
    
    def create_site(self, site: Site) -> Site:
        """Create a new site record."""
        now = datetime.now().isoformat()
        site.created_at = now
        site.updated_at = now
        
        with self._transaction() as cursor:
            data = site.to_dict()
            del data['id']
            
            columns = ', '.join(data.keys())
            placeholders = ', '.join(['?' for _ in data])
            
            cursor.execute(
                f"INSERT INTO sites ({columns}) VALUES ({placeholders})",
                list(data.values())
            )
            site.id = cursor.lastrowid
        
        return site
    
    def get_site(self, domain: str) -> Optional[Site]:
        """Get site by domain."""
        with self._transaction() as cursor:
            cursor.execute("SELECT * FROM sites WHERE domain = ?", (domain,))
            row = cursor.fetchone()
            return Site.from_row(row) if row else None
    
    def get_site_by_app_id(self, app_id: int) -> Optional[Site]:
        """Get site by app ID."""
        with self._transaction() as cursor:
            cursor.execute("SELECT * FROM sites WHERE app_id = ?", (app_id,))
            row = cursor.fetchone()
            return Site.from_row(row) if row else None
    
    def list_sites(
        self,
        webserver: Optional[str] = None,
        enabled: Optional[bool] = None,
    ) -> List[Site]:
        """List all sites."""
        query = "SELECT * FROM sites WHERE 1=1"
        params = []
        
        if webserver:
            query += " AND webserver = ?"
            params.append(webserver)
        if enabled is not None:
            query += " AND enabled = ?"
            params.append(1 if enabled else 0)
        
        query += " ORDER BY created_at DESC"
        
        with self._transaction() as cursor:
            cursor.execute(query, params)
            return [Site.from_row(row) for row in cursor.fetchall()]
    
    def update_site(self, site: Site) -> Site:
        """Update a site."""
        site.updated_at = datetime.now().isoformat()
        
        with self._transaction() as cursor:
            data = site.to_dict()
            site_id = data.pop('id')
            data.pop('created_at')
            
            set_clause = ', '.join([f"{k} = ?" for k in data.keys()])
            
            cursor.execute(
                f"UPDATE sites SET {set_clause} WHERE id = ?",
                list(data.values()) + [site_id]
            )
        
        return site
    
    def delete_site(self, domain: str) -> bool:
        """Delete a site."""
        with self._transaction() as cursor:
            cursor.execute("DELETE FROM sites WHERE domain = ?", (domain,))
            return cursor.rowcount > 0
    
    def update_site_ssl(
        self, 
        domain: str, 
        ssl: bool, 
        ssl_certificate: Optional[str] = None,
        ssl_key: Optional[str] = None
    ) -> bool:
        """Update SSL status for a site."""
        with self._transaction() as cursor:
            cursor.execute(
                """UPDATE sites SET 
                   ssl_enabled = ?, ssl_certificate = ?, ssl_key = ?, updated_at = ?
                   WHERE domain = ?""",
                (1 if ssl else 0, ssl_certificate, ssl_key, datetime.now().isoformat(), domain)
            )
            return cursor.rowcount > 0
    
    def site_exists(self, domain: str) -> bool:
        """Check if a site exists."""
        with self._transaction() as cursor:
            cursor.execute("SELECT 1 FROM sites WHERE domain = ?", (domain,))
            return cursor.fetchone() is not None
    
    # =========================================================================
    # Service CRUD
    # =========================================================================
    
    def create_service(self, service: Service) -> Service:
        """Create a new service record."""
        now = datetime.now().isoformat()
        service.created_at = now
        service.updated_at = now
        
        with self._transaction() as cursor:
            data = service.to_dict()
            del data['id']
            
            # Handle reserved keyword 'group'
            columns = ', '.join([f'"{k}"' if k == 'group' else k for k in data.keys()])
            placeholders = ', '.join(['?' for _ in data])
            
            cursor.execute(
                f"INSERT INTO services ({columns}) VALUES ({placeholders})",
                list(data.values())
            )
            service.id = cursor.lastrowid
        
        return service
    
    def get_service(self, name: str) -> Optional[Service]:
        """Get service by name."""
        with self._transaction() as cursor:
            cursor.execute("SELECT * FROM services WHERE name = ?", (name,))
            row = cursor.fetchone()
            return Service.from_row(row) if row else None
    
    def get_service_by_app_id(self, app_id: int) -> Optional[Service]:
        """Get service by app ID."""
        with self._transaction() as cursor:
            cursor.execute("SELECT * FROM services WHERE app_id = ?", (app_id,))
            row = cursor.fetchone()
            return Service.from_row(row) if row else None
    
    def list_services(
        self,
        status: Optional[str] = None,
        enabled: Optional[bool] = None,
    ) -> List[Service]:
        """List all services."""
        query = "SELECT * FROM services WHERE 1=1"
        params = []
        
        if status is not None:
            query += " AND status = ?"
            params.append(status)
        if enabled is not None:
            query += " AND enabled = ?"
            params.append(1 if enabled else 0)
        
        query += " ORDER BY created_at DESC"
        
        with self._transaction() as cursor:
            cursor.execute(query, params)
            return [Service.from_row(row) for row in cursor.fetchall()]
    
    def update_service(self, service: Service) -> Service:
        """Update a service."""
        service.updated_at = datetime.now().isoformat()
        
        with self._transaction() as cursor:
            data = service.to_dict()
            service_id = data.pop('id')
            data.pop('created_at')
            
            set_clause = ', '.join([
                f'"{k}" = ?' if k == 'group' else f'{k} = ?'
                for k in data.keys()
            ])
            
            cursor.execute(
                f"UPDATE services SET {set_clause} WHERE id = ?",
                list(data.values()) + [service_id]
            )
        
        return service
    
    def update_service_status(
        self, 
        name: str, 
        status: Optional[str] = None,
        active: Optional[bool] = None,
        enabled: Optional[bool] = None,
    ) -> bool:
        """
        Update service status and/or enabled state.
        
        Args:
            name: Service name.
            status: Status string ('active', 'inactive', 'failed').
            active: If True, set status='active'; if False, status='inactive'.
            enabled: Whether service is enabled.
            
        Returns:
            True if updated.
        """
        # Handle active bool -> status string conversion
        if active is not None and status is None:
            status = "active" if active else "inactive"
        
        updates = []
        params = []
        
        if status is not None:
            updates.append("status = ?")
            params.append(status)
        
        if enabled is not None:
            updates.append("enabled = ?")
            params.append(1 if enabled else 0)
        
        if not updates:
            return False
        
        updates.append("updated_at = ?")
        params.append(datetime.now().isoformat())
        params.append(name)
        
        with self._transaction() as cursor:
            cursor.execute(
                f"UPDATE services SET {', '.join(updates)} WHERE name = ?",
                params
            )
            return cursor.rowcount > 0
    
    def delete_service(self, name: str) -> bool:
        """Delete a service."""
        with self._transaction() as cursor:
            cursor.execute("DELETE FROM services WHERE name = ?", (name,))
            return cursor.rowcount > 0
    
    def service_exists(self, name: str) -> bool:
        """Check if a service exists."""
        with self._transaction() as cursor:
            cursor.execute("SELECT 1 FROM services WHERE name = ?", (name,))
            return cursor.fetchone() is not None
    
    # =========================================================================
    # Database CRUD
    # =========================================================================
    
    def create_database(self, database: Database) -> Database:
        """Create a new database record."""
        now = datetime.now().isoformat()
        database.created_at = now
        database.updated_at = now
        
        with self._transaction() as cursor:
            data = database.to_dict()
            del data['id']
            
            columns = ', '.join(data.keys())
            placeholders = ', '.join(['?' for _ in data])
            
            cursor.execute(
                f"INSERT INTO databases ({columns}) VALUES ({placeholders})",
                list(data.values())
            )
            database.id = cursor.lastrowid
        
        return database
    
    def get_database(self, name: str, engine: str) -> Optional[Database]:
        """Get database by name and engine."""
        with self._transaction() as cursor:
            cursor.execute(
                "SELECT * FROM databases WHERE name = ? AND engine = ?",
                (name, engine)
            )
            row = cursor.fetchone()
            return Database.from_row(row) if row else None
    
    def list_databases(
        self,
        engine: Optional[str] = None,
        app_id: Optional[int] = None,
    ) -> List[Database]:
        """List all databases."""
        query = "SELECT * FROM databases WHERE 1=1"
        params = []
        
        if engine:
            query += " AND engine = ?"
            params.append(engine)
        if app_id is not None:
            query += " AND app_id = ?"
            params.append(app_id)
        
        query += " ORDER BY created_at DESC"
        
        with self._transaction() as cursor:
            cursor.execute(query, params)
            return [Database.from_row(row) for row in cursor.fetchall()]
    
    def update_database(self, database: Database) -> Database:
        """Update a database."""
        database.updated_at = datetime.now().isoformat()
        
        with self._transaction() as cursor:
            data = database.to_dict()
            db_id = data.pop('id')
            data.pop('created_at')
            
            set_clause = ', '.join([f"{k} = ?" for k in data.keys()])
            
            cursor.execute(
                f"UPDATE databases SET {set_clause} WHERE id = ?",
                list(data.values()) + [db_id]
            )
        
        return database
    
    def delete_database(self, name: str, engine: str) -> bool:
        """Delete a database record."""
        with self._transaction() as cursor:
            cursor.execute(
                "DELETE FROM databases WHERE name = ? AND engine = ?",
                (name, engine)
            )
            return cursor.rowcount > 0
    
    def link_database_to_app(self, db_name: str, engine: str, app_domain: str) -> bool:
        """Link a database to an application."""
        with self._transaction() as cursor:
            cursor.execute("SELECT id FROM apps WHERE domain = ?", (app_domain,))
            app_row = cursor.fetchone()
            if not app_row:
                return False
            
            cursor.execute(
                "UPDATE databases SET app_id = ?, updated_at = ? WHERE name = ? AND engine = ?",
                (app_row['id'], datetime.now().isoformat(), db_name, engine)
            )
            return cursor.rowcount > 0
    
    # =========================================================================
    # Database User CRUD
    # =========================================================================
    
    def create_database_user(self, user: DatabaseUser) -> DatabaseUser:
        """Create a new database user record."""
        user.created_at = datetime.now().isoformat()
        
        with self._transaction() as cursor:
            data = user.to_dict()
            del data['id']
            
            columns = ', '.join(data.keys())
            placeholders = ', '.join(['?' for _ in data])
            
            cursor.execute(
                f"INSERT INTO database_users ({columns}) VALUES ({placeholders})",
                list(data.values())
            )
            user.id = cursor.lastrowid
        
        return user
    
    def get_database_user(self, username: str, engine: str, host: str = "localhost") -> Optional[DatabaseUser]:
        """Get database user."""
        with self._transaction() as cursor:
            cursor.execute(
                "SELECT * FROM database_users WHERE username = ? AND engine = ? AND host = ?",
                (username, engine, host)
            )
            row = cursor.fetchone()
            return DatabaseUser.from_row(row) if row else None
    
    def list_database_users(
        self,
        engine: Optional[str] = None,
        database_id: Optional[int] = None,
    ) -> List[DatabaseUser]:
        """List database users."""
        query = "SELECT * FROM database_users WHERE 1=1"
        params = []
        
        if engine:
            query += " AND engine = ?"
            params.append(engine)
        if database_id is not None:
            query += " AND database_id = ?"
            params.append(database_id)
        
        query += " ORDER BY created_at DESC"
        
        with self._transaction() as cursor:
            cursor.execute(query, params)
            return [DatabaseUser.from_row(row) for row in cursor.fetchall()]
    
    def delete_database_user(self, username: str, engine: str, host: str = "localhost") -> bool:
        """Delete a database user record."""
        with self._transaction() as cursor:
            cursor.execute(
                "DELETE FROM database_users WHERE username = ? AND engine = ? AND host = ?",
                (username, engine, host)
            )
            return cursor.rowcount > 0
    
    # =========================================================================
    # Utility methods
    # =========================================================================
    
    def get_app_with_relations(self, domain: str) -> Optional[Dict[str, Any]]:
        """
        Get application with all related resources.
        
        Args:
            domain: Application domain.
            
        Returns:
            Dictionary with app, site, service, and databases.
        """
        app = self.get_app(domain)
        if not app:
            return None
        
        result = {
            'app': app,
            'site': self.get_site_by_app_id(app.id) if app.id else None,
            'service': self.get_service_by_app_id(app.id) if app.id else None,
            'databases': self.list_databases(app_id=app.id) if app.id else [],
        }
        
        return result
    
    def sync_service_status_from_systemd(self, name: str, active: bool, enabled: bool) -> None:
        """
        Sync service status from systemd state.
        
        Args:
            name: Service name.
            active: Whether service is active in systemd.
            enabled: Whether service is enabled in systemd.
        """
        self.update_service_status(name, active=active, enabled=enabled)
    
    def get_statistics(self) -> Dict[str, Any]:
        """Get store statistics."""
        with self._transaction() as cursor:
            stats = {}
            
            cursor.execute("SELECT COUNT(*) FROM apps")
            stats['total_apps'] = cursor.fetchone()[0]
            
            cursor.execute("SELECT COUNT(*) FROM apps WHERE status = 'running'")
            stats['running_apps'] = cursor.fetchone()[0]
            
            cursor.execute("SELECT COUNT(*) FROM sites")
            stats['total_sites'] = cursor.fetchone()[0]
            
            cursor.execute("SELECT COUNT(*) FROM services")
            stats['total_services'] = cursor.fetchone()[0]
            
            cursor.execute("SELECT COUNT(*) FROM databases")
            stats['total_databases'] = cursor.fetchone()[0]
            
            cursor.execute(
                "SELECT app_type, COUNT(*) as count FROM apps GROUP BY app_type"
            )
            stats['apps_by_type'] = {row['app_type']: row['count'] for row in cursor.fetchall()}
            
            cursor.execute(
                "SELECT engine, COUNT(*) as count FROM databases GROUP BY engine"
            )
            stats['databases_by_engine'] = {row['engine']: row['count'] for row in cursor.fetchall()}
            
            return stats
    
    def close(self) -> None:
        """Close database connections."""
        if hasattr(self._local, 'connection') and self._local.connection:
            self._local.connection.close()
            self._local.connection = None
    
    @classmethod
    def reset_instance(cls) -> None:
        """Reset the singleton instance (useful for testing)."""
        with cls._lock:
            if cls._instance:
                cls._instance.close()
            cls._instance = None


# Convenience function to get store instance
def get_store(db_path: Optional[Path] = None) -> WASMStore:
    """
    Get the WASM store instance.
    
    Args:
        db_path: Optional custom database path.
        
    Returns:
        WASMStore singleton instance.
    """
    return WASMStore(db_path)
