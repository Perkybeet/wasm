# Copyright (c) 2024-2025 Yago López Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
Base database manager for WASM.

Provides abstract base class for all database engine managers.
"""

import secrets
import string
from abc import abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from wasm.managers.base_manager import BaseManager
from wasm.core.exceptions import (
    DatabaseError,
    DatabaseConnectionError,
    DatabaseNotFoundError,
    DatabaseExistsError,
    DatabaseUserError,
    DatabaseEngineError,
    DatabaseBackupError,
    DatabaseQueryError,
)


@dataclass
class DatabaseInfo:
    """Information about a database."""
    name: str
    engine: str
    size: Optional[str] = None
    tables: int = 0
    owner: Optional[str] = None
    encoding: Optional[str] = None
    created: Optional[datetime] = None
    connection_string: Optional[str] = None
    extra: Dict[str, Any] = field(default_factory=dict)
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            "name": self.name,
            "engine": self.engine,
            "size": self.size,
            "tables": self.tables,
            "owner": self.owner,
            "encoding": self.encoding,
            "created": self.created.isoformat() if self.created else None,
            "connection_string": self.connection_string,
            **self.extra,
        }


@dataclass
class UserInfo:
    """Information about a database user."""
    username: str
    engine: str
    host: str = "localhost"
    databases: List[str] = field(default_factory=list)
    privileges: List[str] = field(default_factory=list)
    created: Optional[datetime] = None
    extra: Dict[str, Any] = field(default_factory=dict)
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            "username": self.username,
            "engine": self.engine,
            "host": self.host,
            "databases": self.databases,
            "privileges": self.privileges,
            "created": self.created.isoformat() if self.created else None,
            **self.extra,
        }


@dataclass
class BackupInfo:
    """Information about a database backup."""
    path: Path
    database: str
    engine: str
    size: int
    created: datetime
    compressed: bool = False
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            "path": str(self.path),
            "database": self.database,
            "engine": self.engine,
            "size": self.size,
            "size_human": self._format_size(self.size),
            "created": self.created.isoformat(),
            "compressed": self.compressed,
        }
    
    @staticmethod
    def _format_size(size: int) -> str:
        """Format size in human readable format."""
        for unit in ["B", "KB", "MB", "GB", "TB"]:
            if size < 1024:
                return f"{size:.1f} {unit}"
            size /= 1024
        return f"{size:.1f} PB"


class BaseDatabaseManager(BaseManager):
    """
    Abstract base class for database managers.
    
    Each database engine (MySQL, PostgreSQL, Redis, MongoDB) should
    implement this interface.
    """
    
    # Class attributes to be overridden by subclasses
    ENGINE_NAME: str = ""  # e.g., "mysql", "postgresql", "redis", "mongodb"
    DISPLAY_NAME: str = ""  # e.g., "MySQL", "PostgreSQL", "Redis", "MongoDB"
    DEFAULT_PORT: int = 0
    SERVICE_NAME: str = ""  # e.g., "mysql", "postgresql", "redis", "mongod"
    PACKAGE_NAMES: List[str] = []  # Packages to install
    
    # Common directories
    BACKUP_DIR = Path("/var/backups/wasm/databases")
    
    def __init__(self, verbose: bool = False):
        """Initialize the database manager."""
        super().__init__(verbose=verbose)
        self._ensure_backup_dir()
    
    def _ensure_backup_dir(self) -> None:
        """Ensure backup directory exists."""
        if not self.BACKUP_DIR.exists():
            try:
                self._run_sudo(["mkdir", "-p", str(self.BACKUP_DIR)])
                self._run_sudo(["chmod", "750", str(self.BACKUP_DIR)])
            except Exception:
                pass  # Will fail on actual backup if needed
    
    @staticmethod
    def generate_password(length: int = 24) -> str:
        """
        Generate a secure random password.
        
        Args:
            length: Password length.
            
        Returns:
            Secure random password.
        """
        alphabet = string.ascii_letters + string.digits + "!@#$%^&*"
        # Ensure at least one of each type
        password = [
            secrets.choice(string.ascii_lowercase),
            secrets.choice(string.ascii_uppercase),
            secrets.choice(string.digits),
            secrets.choice("!@#$%^&*"),
        ]
        password += [secrets.choice(alphabet) for _ in range(length - 4)]
        secrets.SystemRandom().shuffle(password)
        return "".join(password)
    
    # ==================== Engine Management ====================
    
    @abstractmethod
    def is_installed(self) -> bool:
        """
        Check if the database engine is installed.
        
        Returns:
            True if installed.
        """
        pass
    
    @abstractmethod
    def get_version(self) -> Optional[str]:
        """
        Get the database engine version.
        
        Returns:
            Version string or None if not installed.
        """
        pass
    
    @abstractmethod
    def install(self) -> bool:
        """
        Install the database engine.
        
        Returns:
            True if installation successful.
            
        Raises:
            DatabaseEngineError: If installation fails.
        """
        pass
    
    @abstractmethod
    def uninstall(self, purge: bool = False) -> bool:
        """
        Uninstall the database engine.
        
        Args:
            purge: Remove all data and configuration.
            
        Returns:
            True if uninstallation successful.
            
        Raises:
            DatabaseEngineError: If uninstallation fails.
        """
        pass
    
    def start(self) -> bool:
        """
        Start the database service.
        
        Returns:
            True if started successfully.
        """
        result = self._run_sudo(["systemctl", "start", self.SERVICE_NAME])
        if not result.success:
            raise DatabaseEngineError(
                f"Failed to start {self.DISPLAY_NAME}",
                result.stderr
            )
        return True
    
    def stop(self) -> bool:
        """
        Stop the database service.
        
        Returns:
            True if stopped successfully.
        """
        result = self._run_sudo(["systemctl", "stop", self.SERVICE_NAME])
        if not result.success:
            raise DatabaseEngineError(
                f"Failed to stop {self.DISPLAY_NAME}",
                result.stderr
            )
        return True
    
    def restart(self) -> bool:
        """
        Restart the database service.
        
        Returns:
            True if restarted successfully.
        """
        result = self._run_sudo(["systemctl", "restart", self.SERVICE_NAME])
        if not result.success:
            raise DatabaseEngineError(
                f"Failed to restart {self.DISPLAY_NAME}",
                result.stderr
            )
        return True
    
    def is_running(self) -> bool:
        """
        Check if the database service is running.
        
        Returns:
            True if running.
        """
        result = self._run(["systemctl", "is-active", self.SERVICE_NAME])
        return result.stdout.strip() == "active"
    
    def enable(self) -> bool:
        """
        Enable the database service to start on boot.
        
        Returns:
            True if enabled successfully.
        """
        result = self._run_sudo(["systemctl", "enable", self.SERVICE_NAME])
        return result.success
    
    def disable(self) -> bool:
        """
        Disable the database service from starting on boot.
        
        Returns:
            True if disabled successfully.
        """
        result = self._run_sudo(["systemctl", "disable", self.SERVICE_NAME])
        return result.success
    
    def get_status(self) -> Dict[str, Any]:
        """
        Get the database engine status.
        
        Returns:
            Dictionary with status information.
        """
        return {
            "engine": self.ENGINE_NAME,
            "display_name": self.DISPLAY_NAME,
            "installed": self.is_installed(),
            "version": self.get_version() if self.is_installed() else None,
            "running": self.is_running() if self.is_installed() else False,
            "port": self.DEFAULT_PORT,
            "service": self.SERVICE_NAME,
        }
    
    # ==================== Database Management ====================
    
    @abstractmethod
    def create_database(
        self,
        name: str,
        owner: Optional[str] = None,
        encoding: Optional[str] = None,
        **kwargs,
    ) -> DatabaseInfo:
        """
        Create a new database.
        
        Args:
            name: Database name.
            owner: Owner user (if applicable).
            encoding: Character encoding.
            **kwargs: Additional engine-specific options.
            
        Returns:
            DatabaseInfo object.
            
        Raises:
            DatabaseExistsError: If database already exists.
            DatabaseError: If creation fails.
        """
        pass
    
    @abstractmethod
    def drop_database(self, name: str, force: bool = False) -> bool:
        """
        Drop a database.
        
        Args:
            name: Database name.
            force: Force drop even if in use.
            
        Returns:
            True if dropped successfully.
            
        Raises:
            DatabaseNotFoundError: If database doesn't exist.
            DatabaseError: If drop fails.
        """
        pass
    
    @abstractmethod
    def database_exists(self, name: str) -> bool:
        """
        Check if a database exists.
        
        Args:
            name: Database name.
            
        Returns:
            True if exists.
        """
        pass
    
    @abstractmethod
    def list_databases(self) -> List[DatabaseInfo]:
        """
        List all databases.
        
        Returns:
            List of DatabaseInfo objects.
        """
        pass
    
    @abstractmethod
    def get_database_info(self, name: str) -> DatabaseInfo:
        """
        Get detailed information about a database.
        
        Args:
            name: Database name.
            
        Returns:
            DatabaseInfo object.
            
        Raises:
            DatabaseNotFoundError: If database doesn't exist.
        """
        pass
    
    # ==================== User Management ====================
    
    @abstractmethod
    def create_user(
        self,
        username: str,
        password: Optional[str] = None,
        host: str = "localhost",
        **kwargs,
    ) -> tuple[UserInfo, str]:
        """
        Create a new database user.
        
        Args:
            username: Username.
            password: Password (generated if not provided).
            host: Host restriction.
            **kwargs: Additional engine-specific options.
            
        Returns:
            Tuple of (UserInfo, password).
            
        Raises:
            DatabaseUserError: If creation fails.
        """
        pass
    
    @abstractmethod
    def drop_user(self, username: str, host: str = "localhost") -> bool:
        """
        Drop a database user.
        
        Args:
            username: Username.
            host: Host restriction.
            
        Returns:
            True if dropped successfully.
            
        Raises:
            DatabaseUserError: If drop fails.
        """
        pass
    
    @abstractmethod
    def user_exists(self, username: str, host: str = "localhost") -> bool:
        """
        Check if a user exists.
        
        Args:
            username: Username.
            host: Host restriction.
            
        Returns:
            True if exists.
        """
        pass
    
    @abstractmethod
    def list_users(self) -> List[UserInfo]:
        """
        List all database users.
        
        Returns:
            List of UserInfo objects.
        """
        pass
    
    @abstractmethod
    def grant_privileges(
        self,
        username: str,
        database: str,
        privileges: List[str] = None,
        host: str = "localhost",
    ) -> bool:
        """
        Grant privileges to a user on a database.
        
        Args:
            username: Username.
            database: Database name.
            privileges: List of privileges (default: ALL).
            host: Host restriction.
            
        Returns:
            True if granted successfully.
        """
        pass
    
    @abstractmethod
    def revoke_privileges(
        self,
        username: str,
        database: str,
        privileges: List[str] = None,
        host: str = "localhost",
    ) -> bool:
        """
        Revoke privileges from a user on a database.
        
        Args:
            username: Username.
            database: Database name.
            privileges: List of privileges (default: ALL).
            host: Host restriction.
            
        Returns:
            True if revoked successfully.
        """
        pass
    
    # ==================== Backup & Restore ====================
    
    @abstractmethod
    def backup(
        self,
        database: str,
        output_path: Optional[Path] = None,
        compress: bool = True,
        **kwargs,
    ) -> BackupInfo:
        """
        Create a backup of a database.
        
        Args:
            database: Database name.
            output_path: Custom output path.
            compress: Compress the backup.
            **kwargs: Additional engine-specific options.
            
        Returns:
            BackupInfo object.
            
        Raises:
            DatabaseBackupError: If backup fails.
        """
        pass
    
    @abstractmethod
    def restore(
        self,
        database: str,
        backup_path: Path,
        drop_existing: bool = False,
        **kwargs,
    ) -> bool:
        """
        Restore a database from backup.
        
        Args:
            database: Target database name.
            backup_path: Path to backup file.
            drop_existing: Drop existing database first.
            **kwargs: Additional engine-specific options.
            
        Returns:
            True if restored successfully.
            
        Raises:
            DatabaseBackupError: If restore fails.
        """
        pass
    
    def list_backups(self, database: Optional[str] = None) -> List[BackupInfo]:
        """
        List available backups.
        
        Args:
            database: Filter by database name.
            
        Returns:
            List of BackupInfo objects.
        """
        backups = []
        
        if not self.BACKUP_DIR.exists():
            return backups
        
        pattern = f"{self.ENGINE_NAME}-*.sql*" if not database else f"{self.ENGINE_NAME}-{database}-*.sql*"
        
        for path in self.BACKUP_DIR.glob(pattern):
            try:
                stat = path.stat()
                # Parse filename: engine-database-timestamp.sql[.gz]
                parts = path.stem.replace(".sql", "").split("-")
                if len(parts) >= 3:
                    db_name = parts[1]
                    if database and db_name != database:
                        continue
                    
                    backups.append(BackupInfo(
                        path=path,
                        database=db_name,
                        engine=self.ENGINE_NAME,
                        size=stat.st_size,
                        created=datetime.fromtimestamp(stat.st_mtime),
                        compressed=path.suffix == ".gz",
                    ))
            except Exception:
                continue
        
        return sorted(backups, key=lambda x: x.created, reverse=True)
    
    # ==================== Query Execution ====================
    
    @abstractmethod
    def execute_query(
        self,
        database: str,
        query: str,
        **kwargs,
    ) -> tuple[bool, str]:
        """
        Execute a SQL query.
        
        Args:
            database: Database name.
            query: SQL query to execute.
            **kwargs: Additional options.
            
        Returns:
            Tuple of (success, output).
            
        Raises:
            DatabaseQueryError: If query fails.
        """
        pass
    
    @abstractmethod
    def get_connection_string(
        self,
        database: str,
        username: str,
        password: str,
        host: str = "localhost",
    ) -> str:
        """
        Get a connection string for the database.
        
        Args:
            database: Database name.
            username: Username.
            password: Password.
            host: Host.
            
        Returns:
            Connection string.
        """
        pass
    
    def get_interactive_command(
        self,
        database: Optional[str] = None,
        username: Optional[str] = None,
    ) -> List[str]:
        """
        Get the command to connect interactively.
        
        Args:
            database: Database name.
            username: Username.
            
        Returns:
            Command list for subprocess.
        """
        raise NotImplementedError("Subclass must implement get_interactive_command")
