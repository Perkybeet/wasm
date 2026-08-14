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
- Deployments (history of every deployment attempt)

The rows hold credentials: ``apps.env_vars`` and ``services.environment`` carry
DATABASE_URL, API keys and generated secrets. So the database file is 0600
inside a 0700 directory, and both are created through
:mod:`wasm.core.fs` rather than by SQLite: SQLite creates the file with the
process umask, which is how a store full of passwords ends up world readable,
and a creation that does not go through the seam is a creation ``--dry-run``
cannot stop.
"""

import json
import logging
import os
import sqlite3
import stat
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote

from wasm.core.exceptions import WASMError
from wasm.core.fs import SECRET_DIR_MODE, SECRET_MODE, FileSystem, get_fs, is_rehearsal

logger = logging.getLogger(__name__)

# Database location
DEFAULT_DB_PATH = Path("/var/lib/wasm/wasm.db")
USER_DB_PATH = Path.home() / ".local/share/wasm/wasm.db"


class StoreError(WASMError):
    """Raised when the store cannot be opened or created."""


class AppType(str, Enum):
    """Application type enumeration."""

    NEXTJS = "nextjs"
    NODEJS = "nodejs"
    PYTHON = "python"
    VITE = "vite"
    STATIC = "static"
    MONOREPO = "monorepo"
    DOCKER_COMPOSE = "docker-compose"
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


class DeploymentStatus(str, Enum):
    """Deployment lifecycle status enumeration."""

    QUEUED = "queued"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    ROLLED_BACK = "rolled_back"


class DeploymentTrigger(str, Enum):
    """What initiated a deployment."""

    PANEL = "panel"
    CLI = "cli"
    WEBHOOK = "webhook"


@dataclass
class App:
    """Application record."""

    id: int | None = None
    domain: str = ""
    app_type: str = AppType.UNKNOWN.value
    source: str = ""
    branch: str | None = None
    port: int | None = None
    app_path: str = ""
    webserver: str = WebServer.NGINX.value
    ssl_enabled: bool = True
    ssl_certificate: str | None = None
    ssl_key: str | None = None
    status: str = AppStatus.UNKNOWN.value
    is_static: bool = False
    env_vars: dict[str, str] = field(default_factory=dict)
    created_at: str | None = None
    updated_at: str | None = None
    deployed_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary."""
        d = asdict(self)
        if isinstance(d.get("env_vars"), dict):
            d["env_vars"] = json.dumps(d["env_vars"])
        return d

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "App":
        """Create from database row."""
        data = dict(row)
        # The webhook secret is deliberately not a field of this dataclass.
        # Every App that leaves the store gets serialised somewhere - API
        # responses, templates, logs - and every redeploy rewrites the whole
        # row from a freshly built App (AppRegistrationHelper.register_app),
        # which would silently blank the column. Keeping the secret off the
        # record makes both impossible at the chokepoint: it is readable only
        # through get_webhook_secret and writable only through
        # set_webhook_secret.
        data.pop("webhook_secret", None)
        if data.get("env_vars"):
            try:
                data["env_vars"] = json.loads(data["env_vars"])
            except (json.JSONDecodeError, TypeError):
                data["env_vars"] = {}
        return cls(**data)


@dataclass
class Site:
    """Site configuration record."""

    id: int | None = None
    app_id: int | None = None
    domain: str = ""
    webserver: str = WebServer.NGINX.value
    config_path: str = ""
    enabled: bool = True
    is_static: bool = False
    document_root: str | None = None
    proxy_port: int | None = None
    ssl_enabled: bool = False
    ssl_certificate: str | None = None
    ssl_key: str | None = None
    created_at: str | None = None
    updated_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary."""
        return asdict(self)

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Site":
        """Create from database row."""
        return cls(**dict(row))


@dataclass
class Service:
    """Systemd service record."""

    id: int | None = None
    app_id: int | None = None
    name: str = ""
    unit_file: str = ""
    working_directory: str = ""
    command: str = ""
    user: str = "www-data"
    group: str = "www-data"
    enabled: bool = True
    status: str = "inactive"  # active, inactive, failed
    port: int | None = None
    environment: dict[str, str] = field(default_factory=dict)
    created_at: str | None = None
    updated_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary."""
        d = asdict(self)
        if isinstance(d.get("environment"), dict):
            d["environment"] = json.dumps(d["environment"])
        return d

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Service":
        """Create from database row."""
        data = dict(row)
        if data.get("environment"):
            try:
                data["environment"] = json.loads(data["environment"])
            except (json.JSONDecodeError, TypeError):
                data["environment"] = {}
        return cls(**data)


@dataclass
class Database:
    """Database record."""

    id: int | None = None
    app_id: int | None = None
    name: str = ""
    engine: str = DatabaseEngine.MYSQL.value
    host: str = "localhost"
    port: int | None = None
    username: str | None = None
    encoding: str | None = None
    created_at: str | None = None
    updated_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary."""
        return asdict(self)

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Database":
        """Create from database row."""
        return cls(**dict(row))


@dataclass
class DatabaseUser:
    """Database user record."""

    id: int | None = None
    database_id: int | None = None
    username: str = ""
    engine: str = DatabaseEngine.MYSQL.value
    host: str = "localhost"
    privileges: str = "ALL"
    created_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary."""
        return asdict(self)

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "DatabaseUser":
        """Create from database row."""
        return cls(**dict(row))


@dataclass
class DeploymentRecord:
    """One deployment attempt, kept as history."""

    id: int | None = None
    domain: str = ""
    status: str = DeploymentStatus.QUEUED.value
    triggered_by: str = DeploymentTrigger.CLI.value
    git_commit: str | None = None
    git_branch: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    duration_s: float | None = None
    log_path: str | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary."""
        return asdict(self)

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "DeploymentRecord":
        """Create from database row."""
        return cls(**dict(row))


@dataclass
class MonorepoWorkspace:
    """
    Configuration for a workspace app within a monorepo.

    Used by MonorepoDeployer to track individual apps in a Turborepo/pnpm
    workspace monorepo.
    """

    name: str = ""
    path: str = ""
    app_type: str = AppType.UNKNOWN.value
    subdomain: str = ""
    port: int = 3000
    start_command: str | None = None
    health_check: str = "/"
    env_vars: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary."""
        d = asdict(self)
        if isinstance(d.get("env_vars"), dict):
            d["env_vars"] = json.dumps(d["env_vars"])
        return d


# Schema version for migrations
SCHEMA_VERSION = 3

_DEPLOYMENT_STATUSES_SQL = ", ".join(f"'{status.value}'" for status in DeploymentStatus)
_DEPLOYMENT_TRIGGERS_SQL = ", ".join(f"'{trigger.value}'" for trigger in DeploymentTrigger)

# Schema v2: deployment history as a first-class entity. Shared by the fresh
# install path and the v1-to-v2 migration so there is exactly one definition.
#
# The column is ``triggered_by`` rather than ``trigger``: TRIGGER is a reserved
# word in SQLite, and the services table already shows what a reserved column
# name costs - every statement touching ``"group"`` must remember its quotes or
# fail at runtime.
#
# There is deliberately no foreign key to apps: ON DELETE CASCADE would erase
# the history of a domain at the exact moment - deleting a broken app - when
# the operator most needs to read it. The record outlives the app and survives
# a later redeploy under the same name.
DEPLOYMENTS_SCHEMA_SQL = f"""
-- Deployment history
CREATE TABLE IF NOT EXISTS deployments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    domain TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ({_DEPLOYMENT_STATUSES_SQL})),
    triggered_by TEXT NOT NULL CHECK (triggered_by IN ({_DEPLOYMENT_TRIGGERS_SQL})),
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

SCHEMA_SQL = (
    """
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
    -- Schema v3: per-application webhook secret; NULL means webhooks are
    -- disabled. Stored in clear on purpose: see set_webhook_secret. The
    -- v2-to-v3 migration adds this same column with ALTER TABLE.
    webhook_secret TEXT,
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
    + DEPLOYMENTS_SCHEMA_SQL
)


class WASMStore:
    """
    SQLite-based persistence store for WASM.

    Thread-safe singleton that manages all WASM resources.
    """

    _instance: Optional["WASMStore"] = None
    _lock = threading.Lock()

    def __new__(cls, db_path: Path | None = None, fs: FileSystem | None = None) -> "WASMStore":
        """Singleton pattern with thread safety."""
        with cls._lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._initialized = False
            return cls._instance

    def __init__(self, db_path: Path | None = None, fs: FileSystem | None = None):
        """
        Initialize the store.

        Args:
            db_path: Custom database path. Defaults to system or user path.
            fs: Filesystem the database file and its directory are created
                through. Defaults to the process-wide one, which is what makes
                ``--dry-run`` and the test doubles work without every call site
                knowing about them.
        """
        if self._initialized:
            return

        self._fs = fs
        self._db_path = self._resolve_db_path(db_path)
        self._local = threading.local()
        self._ensure_schema()
        self._initialized = True

    @property
    def fs(self) -> FileSystem:
        """
        The filesystem every change this store makes on disk goes through.

        Returns:
            The injected filesystem, or the process-wide one.
        """
        return self._fs or get_fs()

    def _resolve_db_path(self, db_path: Path | None = None) -> Path:
        """
        Resolve the database path.

        Priority:
        1. Explicit path provided
        2. A database that already exists, system before user
        3. System path if writable (/var/lib/wasm/)
        4. User path (~/.local/share/wasm/)

        Nothing is created here. Deciding *where* the database lives is a
        question, not a change, and an early version answered it by trying to
        create the directory, so merely resolving a path left a directory
        behind on a run that was supposed to change nothing.

        **Why an existing database outranks the system location.** The choice
        used to be made purely on whether ``/var/lib/wasm`` happened to exist
        and be writable, so it changed the moment somebody created that
        directory - a packaging change, an administrator, or WASM's own monitor
        service, which needs it. On a server whose inventory had always lived
        under ``~/.local/share``, ``wasm list`` then answered "No applications
        deployed" about a machine serving seventeen sites. Nothing was lost and
        nothing said so, which is the worst way for a tool to be wrong: the
        records were one directory away and the operator was told they did not
        exist.

        Falling back to a database that is actually there removes the failure
        entirely. A fresh machine still gets the system path, because neither
        file exists and rule 3 decides it.

        Args:
            db_path: Explicit database path, if the caller has one.

        Returns:
            The path the store will use.
        """
        if db_path:
            return Path(db_path)

        system_writable = DEFAULT_DB_PATH.parent.is_dir() and os.access(
            DEFAULT_DB_PATH.parent, os.W_OK
        )

        # An inventory that exists wins over one that would be created. Two
        # empty files, or none at all, fall through to the usual preference.
        for candidate in (DEFAULT_DB_PATH, USER_DB_PATH):
            if candidate.is_file() and candidate.stat().st_size > 0:
                if candidate == DEFAULT_DB_PATH and not system_writable:
                    continue
                return candidate

        if system_writable:
            return DEFAULT_DB_PATH

        return USER_DB_PATH

    @property
    def db_path(self) -> Path:
        """Get the database file path."""
        return self._db_path

    def _get_connection(self) -> sqlite3.Connection:
        """
        Get a thread-local database connection.

        Returns:
            The connection belonging to the calling thread.

        Raises:
            StoreError: If the database file cannot be opened.
        """
        if getattr(self._local, "connection", None) is None:
            self._local.connection = self._connect()
        return self._local.connection

    def _connect(self) -> sqlite3.Connection:
        """
        Open the database, refusing to create it.

        ``mode=rw`` instead of the default ``rwc``: the file is created by
        :meth:`_secure_db_file` through the filesystem seam, with 0600 applied
        at creation. If it is missing by the time we connect, the seam declined
        to create it, and letting SQLite create one behind the seam's back is
        exactly the defect the seam exists to remove: a rehearsal that leaves a
        file, and a world-readable one at that.

        Returns:
            A new connection with foreign keys enabled.

        Raises:
            StoreError: If the database file cannot be opened.
        """
        uri = f"file:{quote(str(self._db_path.resolve()))}?mode=rw"
        try:
            connection = sqlite3.connect(uri, uri=True, check_same_thread=False)
        except sqlite3.OperationalError as exc:
            raise StoreError(
                f"Cannot open the WASM database at {self._db_path}",
                details=(
                    "The file is missing or cannot be opened. It is created on the "
                    "first real run; --dry-run deliberately does not create it, so "
                    "run the command once without --dry-run. Otherwise check that "
                    f"{self._db_path.parent} exists and is writable by this user."
                ),
            ) from exc

        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Cursor]:
        """
        Run a database transaction, or rehearse one.

        Every write to the store passes through here, which is why the dry-run
        check lives here rather than in each caller. It is needed because a
        rehearsal that leaves the store changed is worse than one that leaves a
        file changed: the record and the machine then disagree, and the next
        real command acts on a state that never existed.

        Yields:
            A cursor. Under ``--dry-run`` the work is done and then rolled
            back, so a caller that reads back what it just wrote still sees a
            consistent picture inside the transaction.
        """
        conn = self._get_connection()
        cursor = conn.cursor()
        try:
            yield cursor
            if is_rehearsal():
                conn.rollback()
            else:
                conn.commit()
        except Exception:
            conn.rollback()
            raise

    def _secure_directory(self, path: Path) -> None:
        """
        Create the directory the database lives in, owner-only.

        Every missing level is created 0700, not just the last one: that is how
        a private directory ends up under a world-readable parent. A directory
        left lax by an older version is tightened, but only when it is ours and
        not shared (sticky bit); tightening ``/tmp`` or another shared location
        would break the machine for every other account, and the 0600 file mode
        already protects the content.

        Args:
            path: Directory to create or tighten.

        Raises:
            OSError: If the directory cannot be created.
        """
        self.fs.make_dir(path, mode=SECRET_DIR_MODE, parents=True)

        if not path.is_dir():
            # The seam declined to create it, so there is nothing to tighten.
            return

        info = path.stat()
        is_shared = bool(info.st_mode & stat.S_ISVTX)
        if info.st_mode & 0o077 and not is_shared and info.st_uid == os.geteuid():
            self.fs.chmod(path, SECRET_DIR_MODE)

    def _secure_db_file(self) -> bool:
        """
        Create the database file with owner-only permissions.

        The rows hold application secrets (``env_vars`` carries DATABASE_URL and
        similar), so the file is created through the filesystem seam with 0600
        applied at creation, before SQLite ever opens it; SQLite would create it
        with the process umask instead. A database left lax by an older version
        is tightened here as well.

        Returns:
            True if the database file is present once this returns. False means
            the filesystem declined to create it, which is what a dry run does.

        Raises:
            OSError: If the file or its directory cannot be secured.
        """
        self._secure_directory(self._db_path.parent)

        try:
            info = os.lstat(self._db_path)
        except OSError:
            # Missing, or a dangling symlink, for which exists() answers False.
            self.fs.write_text(self._db_path, "", mode=SECRET_MODE)
            return self._db_path.exists()

        if stat.S_ISLNK(info.st_mode):
            # chmod follows the link, which would hand the mode change to a file
            # of the attacker's choosing.
            logger.warning("Refusing to change permissions through the symlink %s", self._db_path)
            return True

        if info.st_mode & 0o077:
            self.fs.chmod(self._db_path, SECRET_MODE)

        return True

    def _ensure_schema(self) -> None:
        """
        Ensure database schema exists and is up to date.

        Nothing happens when the filesystem declined to create the database
        file. Creating the schema means connecting, and connecting to a missing
        file is how SQLite would create it outside the seam: a dry run would
        leave a database behind after announcing it would change nothing.
        """
        if not self._secure_db_file():
            logger.debug("Filesystem declined to create %s; skipping schema", self._db_path)
            return

        with self._transaction() as cursor:
            # Check if schema_version table exists
            cursor.execute("""
                SELECT name FROM sqlite_master
                WHERE type='table' AND name='schema_version'
            """)

            if not cursor.fetchone():
                # Fresh install - create all tables
                cursor.executescript(SCHEMA_SQL)
                cursor.execute("INSERT INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,))
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
        # Keyed by the version each migration produces, matching the loop.
        migrations = {
            2: self._migrate_v1_to_v2,
            3: self._migrate_v2_to_v3,
        }

        for version in range(from_version + 1, SCHEMA_VERSION + 1):
            if version in migrations:
                migrations[version](cursor)
            cursor.execute("INSERT INTO schema_version (version) VALUES (?)", (version,))

    def _migrate_v1_to_v2(self, cursor: sqlite3.Cursor) -> None:
        """
        Add the deployments history table (schema v2).

        Args:
            cursor: Cursor the migration runs on.
        """
        cursor.executescript(DEPLOYMENTS_SCHEMA_SQL)

    def _migrate_v2_to_v3(self, cursor: sqlite3.Cursor) -> None:
        """
        Add the per-application webhook secret column (schema v3).

        NULL, which every existing row gets, means webhooks are disabled: an
        upgrade must never invent a credential.

        Args:
            cursor: Cursor the migration runs on.
        """
        cursor.execute("ALTER TABLE apps ADD COLUMN webhook_secret TEXT")

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
            del data["id"]  # Let SQLite auto-generate

            columns = ", ".join(data.keys())
            placeholders = ", ".join(["?" for _ in data])

            cursor.execute(
                f"INSERT INTO apps ({columns}) VALUES ({placeholders})", list(data.values())
            )
            app.id = cursor.lastrowid

        return app

    def get_app(self, domain: str) -> App | None:
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

    def get_app_by_id(self, app_id: int) -> App | None:
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
        status: str | None = None,
        app_type: str | None = None,
    ) -> list[App]:
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
            app_id = data.pop("id")
            data.pop("created_at")  # Don't update created_at

            set_clause = ", ".join([f"{k} = ?" for k in data.keys()])

            cursor.execute(
                f"UPDATE apps SET {set_clause} WHERE id = ?", [*list(data.values()), app_id]
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
                (status, datetime.now().isoformat(), domain),
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
            cursor.execute("SELECT 1 FROM apps WHERE domain = ?", (domain,))
            return cursor.fetchone() is not None

    def set_webhook_secret(self, domain: str, secret: str | None) -> bool:
        """
        Store or clear the webhook secret of an application.

        The secret is stored in clear, deliberately. GitHub and Gitea sign
        every delivery with ``HMAC-SHA256(secret, body)``, and verifying that
        signature means recomputing it, which needs the original secret; a
        stored hash could only ever support plain equality (GitLab's token
        header) and would make signature verification impossible. The database
        file already holds credentials of the same sensitivity in the same
        table - ``apps.env_vars`` carries DATABASE_URL and API keys - and is
        created 0600 inside a 0700 directory, exactly like config.yaml with
        its database credentials.

        Args:
            domain: Application domain.
            secret: The secret in clear, or None to disable webhooks.

        Returns:
            True if the application exists and the row was updated.
        """
        with self._transaction() as cursor:
            cursor.execute(
                "UPDATE apps SET webhook_secret = ?, updated_at = ? WHERE domain = ?",
                (secret, datetime.now().isoformat(), domain),
            )
            return cursor.rowcount > 0

    def get_webhook_secret(self, domain: str) -> str | None:
        """
        Read the webhook secret of an application.

        Args:
            domain: Application domain.

        Returns:
            The secret in clear, or None when the application does not exist
            or has webhooks disabled. The two cases are indistinguishable on
            purpose: the webhook endpoint must not reveal which domains are
            deployed.
        """
        with self._transaction() as cursor:
            cursor.execute("SELECT webhook_secret FROM apps WHERE domain = ?", (domain,))
            row = cursor.fetchone()
            return row["webhook_secret"] if row else None

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
            del data["id"]

            columns = ", ".join(data.keys())
            placeholders = ", ".join(["?" for _ in data])

            cursor.execute(
                f"INSERT INTO sites ({columns}) VALUES ({placeholders})", list(data.values())
            )
            site.id = cursor.lastrowid

        return site

    def get_site(self, domain: str) -> Site | None:
        """Get site by domain."""
        with self._transaction() as cursor:
            cursor.execute("SELECT * FROM sites WHERE domain = ?", (domain,))
            row = cursor.fetchone()
            return Site.from_row(row) if row else None

    def get_site_by_app_id(self, app_id: int) -> Site | None:
        """Get site by app ID."""
        with self._transaction() as cursor:
            cursor.execute("SELECT * FROM sites WHERE app_id = ?", (app_id,))
            row = cursor.fetchone()
            return Site.from_row(row) if row else None

    def list_sites(
        self,
        webserver: str | None = None,
        enabled: bool | None = None,
    ) -> list[Site]:
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
            site_id = data.pop("id")
            data.pop("created_at")

            set_clause = ", ".join([f"{k} = ?" for k in data.keys()])

            cursor.execute(
                f"UPDATE sites SET {set_clause} WHERE id = ?", [*list(data.values()), site_id]
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
        ssl_certificate: str | None = None,
        ssl_key: str | None = None,
    ) -> bool:
        """Update SSL status for a site."""
        with self._transaction() as cursor:
            cursor.execute(
                """UPDATE sites SET
                   ssl_enabled = ?, ssl_certificate = ?, ssl_key = ?, updated_at = ?
                   WHERE domain = ?""",
                (1 if ssl else 0, ssl_certificate, ssl_key, datetime.now().isoformat(), domain),
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
            del data["id"]

            # Handle reserved keyword 'group'
            columns = ", ".join([f'"{k}"' if k == "group" else k for k in data.keys()])
            placeholders = ", ".join(["?" for _ in data])

            cursor.execute(
                f"INSERT INTO services ({columns}) VALUES ({placeholders})", list(data.values())
            )
            service.id = cursor.lastrowid

        return service

    def get_service(self, name: str) -> Service | None:
        """Get service by name."""
        with self._transaction() as cursor:
            cursor.execute("SELECT * FROM services WHERE name = ?", (name,))
            row = cursor.fetchone()
            return Service.from_row(row) if row else None

    def get_service_by_app_id(self, app_id: int) -> Service | None:
        """Get service by app ID."""
        with self._transaction() as cursor:
            cursor.execute("SELECT * FROM services WHERE app_id = ?", (app_id,))
            row = cursor.fetchone()
            return Service.from_row(row) if row else None

    def list_services(
        self,
        status: str | None = None,
        enabled: bool | None = None,
    ) -> list[Service]:
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
            service_id = data.pop("id")
            data.pop("created_at")

            set_clause = ", ".join(
                [f'"{k}" = ?' if k == "group" else f"{k} = ?" for k in data.keys()]
            )

            cursor.execute(
                f"UPDATE services SET {set_clause} WHERE id = ?", [*list(data.values()), service_id]
            )

        return service

    def update_service_status(
        self,
        name: str,
        status: str | None = None,
        active: bool | None = None,
        enabled: bool | None = None,
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
            cursor.execute(f"UPDATE services SET {', '.join(updates)} WHERE name = ?", params)
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
            del data["id"]

            columns = ", ".join(data.keys())
            placeholders = ", ".join(["?" for _ in data])

            cursor.execute(
                f"INSERT INTO databases ({columns}) VALUES ({placeholders})", list(data.values())
            )
            database.id = cursor.lastrowid

        return database

    def get_database(self, name: str, engine: str) -> Database | None:
        """Get database by name and engine."""
        with self._transaction() as cursor:
            cursor.execute("SELECT * FROM databases WHERE name = ? AND engine = ?", (name, engine))
            row = cursor.fetchone()
            return Database.from_row(row) if row else None

    def list_databases(
        self,
        engine: str | None = None,
        app_id: int | None = None,
    ) -> list[Database]:
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
            db_id = data.pop("id")
            data.pop("created_at")

            set_clause = ", ".join([f"{k} = ?" for k in data.keys()])

            cursor.execute(
                f"UPDATE databases SET {set_clause} WHERE id = ?", [*list(data.values()), db_id]
            )

        return database

    def delete_database(self, name: str, engine: str) -> bool:
        """Delete a database record."""
        with self._transaction() as cursor:
            cursor.execute("DELETE FROM databases WHERE name = ? AND engine = ?", (name, engine))
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
                (app_row["id"], datetime.now().isoformat(), db_name, engine),
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
            del data["id"]

            columns = ", ".join(data.keys())
            placeholders = ", ".join(["?" for _ in data])

            cursor.execute(
                f"INSERT INTO database_users ({columns}) VALUES ({placeholders})",
                list(data.values()),
            )
            user.id = cursor.lastrowid

        return user

    def get_database_user(
        self, username: str, engine: str, host: str = "localhost"
    ) -> DatabaseUser | None:
        """Get database user."""
        with self._transaction() as cursor:
            cursor.execute(
                "SELECT * FROM database_users WHERE username = ? AND engine = ? AND host = ?",
                (username, engine, host),
            )
            row = cursor.fetchone()
            return DatabaseUser.from_row(row) if row else None

    def list_database_users(
        self,
        engine: str | None = None,
        database_id: int | None = None,
    ) -> list[DatabaseUser]:
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
                (username, engine, host),
            )
            return cursor.rowcount > 0

    # =========================================================================
    # Deployment history
    # =========================================================================

    def record_deployment_start(
        self,
        domain: str,
        trigger: str,
        git_commit: str | None = None,
        git_branch: str | None = None,
        log_path: str | None = None,
    ) -> int:
        """
        Record the start of a deployment attempt.

        The row is created in ``running`` state with ``started_at`` set to now;
        :meth:`finish_deployment` closes it with the outcome.

        Args:
            domain: Domain being deployed.
            trigger: What initiated the deployment: 'panel', 'cli' or 'webhook'.
            git_commit: Commit being deployed, when known.
            git_branch: Branch being deployed, when known.
            log_path: Where the captured build log is written, when there is one.

        Returns:
            The id of the history row, to pass to :meth:`finish_deployment`.

        Raises:
            StoreError: If ``trigger`` is not an accepted value.
        """
        try:
            DeploymentTrigger(trigger)
        except ValueError as exc:
            raise StoreError(
                f"Invalid deployment trigger {trigger!r}",
                details="Accepted triggers: "
                + ", ".join(member.value for member in DeploymentTrigger),
            ) from exc

        with self._transaction() as cursor:
            cursor.execute(
                """INSERT INTO deployments
                   (domain, status, triggered_by, git_commit, git_branch, started_at, log_path)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    domain,
                    DeploymentStatus.RUNNING.value,
                    trigger,
                    git_commit,
                    git_branch,
                    datetime.now().isoformat(),
                    log_path,
                ),
            )
            deployment_id = cursor.lastrowid

        if deployment_id is None:
            raise StoreError(
                "SQLite reported no id for the new deployment row",
                details="This should not happen after a successful INSERT.",
            )
        return deployment_id

    def annotate_deployment(
        self,
        deployment_id: int,
        *,
        log_path: str | None = None,
        git_commit: str | None = None,
        git_branch: str | None = None,
    ) -> bool:
        """
        Record facts about a deployment that are only learned after it starts.

        The captured log is named after the row id, so the row has to exist
        before its path does; the commit being deployed is only known once the
        fetch step has run. Both arrive here instead of widening
        :meth:`record_deployment_start` with values nobody has yet.

        Args:
            deployment_id: Id returned by :meth:`record_deployment_start`.
            log_path: Where the captured build log is written.
            git_commit: Commit being deployed (short hash).
            git_branch: Branch being deployed.

        Returns:
            True if the row exists and something was updated. None arguments
            leave their columns as they are.
        """
        updates = []
        params: list[Any] = []
        for column, value in (
            ("log_path", log_path),
            ("git_commit", git_commit),
            ("git_branch", git_branch),
        ):
            if value is not None:
                updates.append(f"{column} = ?")
                params.append(value)

        if not updates:
            return False

        params.append(deployment_id)
        with self._transaction() as cursor:
            cursor.execute(f"UPDATE deployments SET {', '.join(updates)} WHERE id = ?", params)
            return cursor.rowcount > 0

    def mark_deployment_rolled_back(self, deployment_id: int) -> bool:
        """
        Reclassify a finished deployment as rolled back, keeping its timing.

        Not :meth:`finish_deployment`: that recomputes ``finished_at`` and
        ``duration_s``, which is right for a run that is ending and wrong for
        a record being reclassified days later - the original duration is
        history, not state. So this changes the status and nothing else.

        Args:
            deployment_id: Id of the deployment whose build was rolled back.

        Returns:
            True if the row exists and was updated.
        """
        with self._transaction() as cursor:
            cursor.execute(
                "UPDATE deployments SET status = ? WHERE id = ?",
                (DeploymentStatus.ROLLED_BACK.value, deployment_id),
            )
            return cursor.rowcount > 0

    def finish_deployment(self, deployment_id: int, status: str, error: str | None = None) -> None:
        """
        Record the outcome of a deployment.

        ``finished_at`` and ``duration_s`` are computed here from the recorded
        start, so callers only say how it ended.

        Args:
            deployment_id: Id returned by :meth:`record_deployment_start`.
            status: Final status, usually 'success', 'failed' or 'rolled_back'.
            error: What went wrong, verbatim, when the deployment failed.

        Raises:
            StoreError: If the deployment does not exist or ``status`` is not
                an accepted value.
        """
        try:
            DeploymentStatus(status)
        except ValueError as exc:
            raise StoreError(
                f"Invalid deployment status {status!r}",
                details="Accepted statuses: "
                + ", ".join(member.value for member in DeploymentStatus),
            ) from exc

        with self._transaction() as cursor:
            cursor.execute("SELECT started_at FROM deployments WHERE id = ?", (deployment_id,))
            row = cursor.fetchone()
            if row is None:
                raise StoreError(
                    f"Deployment {deployment_id} does not exist",
                    details="It may have been pruned; there is nothing to finish.",
                )

            started = datetime.fromisoformat(row["started_at"])
            finished = datetime.now()
            cursor.execute(
                """UPDATE deployments
                   SET status = ?, finished_at = ?, duration_s = ?, error = ?
                   WHERE id = ?""",
                (
                    status,
                    finished.isoformat(),
                    (finished - started).total_seconds(),
                    error,
                    deployment_id,
                ),
            )

    def get_deployment(self, deployment_id: int) -> DeploymentRecord | None:
        """
        Get a deployment history row by id.

        Args:
            deployment_id: Deployment id.

        Returns:
            The record, or None if not found.
        """
        with self._transaction() as cursor:
            cursor.execute("SELECT * FROM deployments WHERE id = ?", (deployment_id,))
            row = cursor.fetchone()
            return DeploymentRecord.from_row(row) if row else None

    def list_deployments(
        self, domain: str | None = None, limit: int = 50
    ) -> list[DeploymentRecord]:
        """
        List deployment history, most recent first.

        Args:
            domain: Only this domain's history; all domains when None.
            limit: Maximum number of rows returned.

        Returns:
            Deployment records ordered by start time, newest first.
        """
        query = "SELECT * FROM deployments"
        params: list[Any] = []

        if domain:
            query += " WHERE domain = ?"
            params.append(domain)

        # id breaks the tie between rows started in the same instant.
        query += " ORDER BY started_at DESC, id DESC LIMIT ?"
        params.append(limit)

        with self._transaction() as cursor:
            cursor.execute(query, params)
            return [DeploymentRecord.from_row(row) for row in cursor.fetchall()]

    def prune_deployments(self, domain: str, keep: int = 20) -> int:
        """
        Delete a domain's oldest deployment rows beyond ``keep``.

        Args:
            domain: Domain whose history is rotated.
            keep: How many of the most recent rows survive.

        Returns:
            How many rows were deleted.
        """
        with self._transaction() as cursor:
            cursor.execute(
                """DELETE FROM deployments
                   WHERE domain = ? AND id NOT IN (
                       SELECT id FROM deployments WHERE domain = ?
                       ORDER BY started_at DESC, id DESC LIMIT ?
                   )""",
                (domain, domain, keep),
            )
            return cursor.rowcount

    # =========================================================================
    # Utility methods
    # =========================================================================

    def get_app_with_relations(self, domain: str) -> dict[str, Any] | None:
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
            "app": app,
            "site": self.get_site_by_app_id(app.id) if app.id else None,
            "service": self.get_service_by_app_id(app.id) if app.id else None,
            "databases": self.list_databases(app_id=app.id) if app.id else [],
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

    def get_statistics(self) -> dict[str, Any]:
        """Get store statistics."""
        with self._transaction() as cursor:
            stats = {}

            cursor.execute("SELECT COUNT(*) FROM apps")
            stats["total_apps"] = cursor.fetchone()[0]

            cursor.execute("SELECT COUNT(*) FROM apps WHERE status = 'running'")
            stats["running_apps"] = cursor.fetchone()[0]

            cursor.execute("SELECT COUNT(*) FROM sites")
            stats["total_sites"] = cursor.fetchone()[0]

            cursor.execute("SELECT COUNT(*) FROM services")
            stats["total_services"] = cursor.fetchone()[0]

            cursor.execute("SELECT COUNT(*) FROM databases")
            stats["total_databases"] = cursor.fetchone()[0]

            cursor.execute("SELECT app_type, COUNT(*) as count FROM apps GROUP BY app_type")
            stats["apps_by_type"] = {row["app_type"]: row["count"] for row in cursor.fetchall()}

            cursor.execute("SELECT engine, COUNT(*) as count FROM databases GROUP BY engine")
            stats["databases_by_engine"] = {
                row["engine"]: row["count"] for row in cursor.fetchall()
            }

            return stats

    def close(self) -> None:
        """Close database connections."""
        if hasattr(self._local, "connection") and self._local.connection:
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
def get_store(db_path: Path | None = None, fs: FileSystem | None = None) -> WASMStore:
    """
    Get the WASM store instance.

    Args:
        db_path: Optional custom database path.
        fs: Optional filesystem to create the database through. Only honoured
            when the singleton is built; an existing instance keeps its own.

    Returns:
        WASMStore singleton instance.
    """
    return WASMStore(db_path, fs)
