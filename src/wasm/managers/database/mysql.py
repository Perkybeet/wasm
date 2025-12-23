# Copyright (c) 2024-2025 Yago López Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
MySQL/MariaDB database manager for WASM.
"""

import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from wasm.core.exceptions import (
    DatabaseError,
    DatabaseNotFoundError,
    DatabaseExistsError,
    DatabaseUserError,
    DatabaseEngineError,
    DatabaseBackupError,
    DatabaseQueryError,
)
from wasm.managers.database.base import (
    BaseDatabaseManager,
    DatabaseInfo,
    UserInfo,
    BackupInfo,
)
from wasm.managers.database.registry import DatabaseRegistry


class MySQLManager(BaseDatabaseManager):
    """
    Manager for MySQL/MariaDB databases.
    
    Supports both MySQL and MariaDB, automatically detecting which is installed.
    """
    
    ENGINE_NAME = "mysql"
    DISPLAY_NAME = "MySQL/MariaDB"
    DEFAULT_PORT = 3306
    SERVICE_NAME = "mysql"  # Updated during init if MariaDB
    PACKAGE_NAMES = ["mysql-server"]
    MARIADB_PACKAGES = ["mariadb-server"]
    
    # System databases to exclude from listings
    SYSTEM_DATABASES = {
        "information_schema",
        "mysql",
        "performance_schema",
        "sys",
    }
    
    def __init__(self, verbose: bool = False):
        """Initialize MySQL manager."""
        super().__init__(verbose=verbose)
        self._detect_variant()
    
    def _detect_variant(self) -> None:
        """Detect if MySQL or MariaDB is installed."""
        # Check for MariaDB first
        result = self._run(["which", "mariadb"])
        if result.success:
            self.SERVICE_NAME = "mariadb"
            self.DISPLAY_NAME = "MariaDB"
            return
        
        # Check for MySQL
        result = self._run(["which", "mysql"])
        if result.success:
            # Check if it's actually MariaDB
            result = self._run(["mysql", "--version"])
            if result.success and "mariadb" in result.stdout.lower():
                self.SERVICE_NAME = "mariadb"
                self.DISPLAY_NAME = "MariaDB"
    
    def is_installed(self) -> bool:
        """Check if MySQL/MariaDB is installed."""
        result = self._run(["which", "mysql"])
        return result.success
    
    def get_version(self) -> Optional[str]:
        """Get MySQL/MariaDB version."""
        result = self._run(["mysql", "--version"])
        if result.success:
            # Parse version from output
            match = re.search(r"(\d+\.\d+\.\d+)", result.stdout)
            if match:
                return match.group(1)
        return None
    
    def install(self) -> bool:
        """Install MySQL/MariaDB."""
        self.logger.info(f"Installing {self.DISPLAY_NAME}...")
        
        # Update package list
        result = self._run_sudo(["apt-get", "update"])
        if not result.success:
            raise DatabaseEngineError("Failed to update package list", result.stderr)
        
        # Try MariaDB first (more common on modern systems)
        result = self._run_sudo([
            "apt-get", "install", "-y",
            *self.MARIADB_PACKAGES,
        ])
        
        if result.success:
            self.SERVICE_NAME = "mariadb"
            self.DISPLAY_NAME = "MariaDB"
        else:
            # Fall back to MySQL
            result = self._run_sudo([
                "apt-get", "install", "-y",
                *self.PACKAGE_NAMES,
            ])
            if not result.success:
                raise DatabaseEngineError(
                    f"Failed to install {self.DISPLAY_NAME}",
                    result.stderr
                )
        
        # Enable and start service
        self.enable()
        self.start()
        
        # Secure installation (set root password, remove test db, etc.)
        self._secure_installation()
        
        return True
    
    def _secure_installation(self) -> None:
        """Run basic security hardening."""
        # Remove anonymous users
        self._run_sudo([
            "mysql", "-e",
            "DELETE FROM mysql.user WHERE User='';"
        ])
        
        # Remove test database
        self._run_sudo([
            "mysql", "-e",
            "DROP DATABASE IF EXISTS test;"
        ])
        
        # Flush privileges
        self._run_sudo([
            "mysql", "-e",
            "FLUSH PRIVILEGES;"
        ])
    
    def uninstall(self, purge: bool = False) -> bool:
        """Uninstall MySQL/MariaDB."""
        self.logger.info(f"Uninstalling {self.DISPLAY_NAME}...")
        
        # Stop service
        try:
            self.stop()
        except Exception:
            pass
        
        action = "purge" if purge else "remove"
        
        # Try removing both variants
        for packages in [self.MARIADB_PACKAGES, self.PACKAGE_NAMES]:
            result = self._run_sudo([
                "apt-get", action, "-y",
                *packages,
            ])
        
        if purge:
            # Remove data directory
            self._run_sudo(["rm", "-rf", "/var/lib/mysql"])
            self._run_sudo(["rm", "-rf", "/etc/mysql"])
        
        return True
    
    def _execute_sql(
        self,
        sql: str,
        database: Optional[str] = None,
        return_output: bool = True,
    ) -> tuple[bool, str]:
        """
        Execute SQL command.
        
        Args:
            sql: SQL command.
            database: Database to use.
            return_output: Return query output.
            
        Returns:
            Tuple of (success, output).
        """
        cmd = ["mysql", "-N", "-B"]
        
        # Add credentials if configured
        # Note: Config is loaded from /etc/wasm/config.yaml
        # Users should ensure this file is readable only by root if it contains passwords
        creds = self.config.get("databases", {}).get("credentials", {}).get("mysql", {})
        user = creds.get("user")
        password = creds.get("password")
        
        if user:
            cmd.extend(["-u", user])
        if password:
            cmd.extend([f"-p{password}"])
            
        if database:
            cmd.extend(["-D", database])
        cmd.extend(["-e", sql])
        
        result = self._run_sudo(cmd)
        return result.success, result.stdout if result.success else result.stderr
    
    # ==================== Database Management ====================
    
    def create_database(
        self,
        name: str,
        owner: Optional[str] = None,
        encoding: Optional[str] = None,
        **kwargs,
    ) -> DatabaseInfo:
        """Create a new MySQL database."""
        if self.database_exists(name):
            raise DatabaseExistsError(f"Database '{name}' already exists")
        
        charset = encoding or "utf8mb4"
        collation = kwargs.get("collation", "utf8mb4_unicode_ci")
        
        sql = f"CREATE DATABASE `{name}` CHARACTER SET {charset} COLLATE {collation};"
        success, output = self._execute_sql(sql)
        
        if not success:
            raise DatabaseError(f"Failed to create database '{name}'", output)
        
        # Grant privileges to owner if specified
        if owner:
            self.grant_privileges(owner, name)
        
        self.logger.info(f"Created database: {name}")
        return self.get_database_info(name)
    
    def drop_database(self, name: str, force: bool = False) -> bool:
        """Drop a MySQL database."""
        if not self.database_exists(name):
            if force:
                return True
            raise DatabaseNotFoundError(f"Database '{name}' does not exist")
        
        sql = f"DROP DATABASE `{name}`;"
        success, output = self._execute_sql(sql)
        
        if not success:
            raise DatabaseError(f"Failed to drop database '{name}'", output)
        
        self.logger.info(f"Dropped database: {name}")
        return True
    
    def database_exists(self, name: str) -> bool:
        """Check if a database exists."""
        sql = f"SELECT SCHEMA_NAME FROM INFORMATION_SCHEMA.SCHEMATA WHERE SCHEMA_NAME = '{name}';"
        success, output = self._execute_sql(sql)
        return success and name in output
    
    def list_databases(self) -> List[DatabaseInfo]:
        """List all databases."""
        sql = """
            SELECT 
                SCHEMA_NAME,
                DEFAULT_CHARACTER_SET_NAME,
                DEFAULT_COLLATION_NAME
            FROM INFORMATION_SCHEMA.SCHEMATA;
        """
        success, output = self._execute_sql(sql)
        
        if not success:
            return []
        
        databases = []
        for line in output.strip().split("\n"):
            if not line:
                continue
            parts = line.split("\t")
            if len(parts) >= 1:
                name = parts[0]
                if name in self.SYSTEM_DATABASES:
                    continue
                
                try:
                    info = self.get_database_info(name)
                    databases.append(info)
                except Exception:
                    databases.append(DatabaseInfo(
                        name=name,
                        engine=self.ENGINE_NAME,
                        encoding=parts[1] if len(parts) > 1 else None,
                    ))
        
        return databases
    
    def get_database_info(self, name: str) -> DatabaseInfo:
        """Get detailed database information."""
        if not self.database_exists(name):
            raise DatabaseNotFoundError(f"Database '{name}' does not exist")
        
        # Get size and table count
        sql = f"""
            SELECT 
                SUM(DATA_LENGTH + INDEX_LENGTH) as size,
                COUNT(*) as tables
            FROM INFORMATION_SCHEMA.TABLES 
            WHERE TABLE_SCHEMA = '{name}';
        """
        success, output = self._execute_sql(sql)
        
        size = None
        tables = 0
        if success and output.strip():
            parts = output.strip().split("\t")
            if len(parts) >= 2:
                try:
                    size_bytes = int(parts[0]) if parts[0] and parts[0] != "NULL" else 0
                    size = self._format_size(size_bytes)
                    tables = int(parts[1]) if parts[1] else 0
                except (ValueError, TypeError):
                    pass
        
        # Get encoding
        sql = f"SELECT DEFAULT_CHARACTER_SET_NAME FROM INFORMATION_SCHEMA.SCHEMATA WHERE SCHEMA_NAME = '{name}';"
        success, output = self._execute_sql(sql)
        encoding = output.strip() if success else None
        
        return DatabaseInfo(
            name=name,
            engine=self.ENGINE_NAME,
            size=size,
            tables=tables,
            encoding=encoding,
        )
    
    @staticmethod
    def _format_size(size: int) -> str:
        """Format size in human readable format."""
        for unit in ["B", "KB", "MB", "GB", "TB"]:
            if size < 1024:
                return f"{size:.1f} {unit}"
            size /= 1024
        return f"{size:.1f} PB"
    
    # ==================== User Management ====================
    
    def create_user(
        self,
        username: str,
        password: Optional[str] = None,
        host: str = "localhost",
        **kwargs,
    ) -> tuple[UserInfo, str]:
        """Create a new MySQL user."""
        if self.user_exists(username, host):
            raise DatabaseUserError(f"User '{username}'@'{host}' already exists")
        
        password = password or self.generate_password()
        
        sql = f"CREATE USER '{username}'@'{host}' IDENTIFIED BY '{password}';"
        success, output = self._execute_sql(sql)
        
        if not success:
            raise DatabaseUserError(f"Failed to create user '{username}'", output)
        
        # Flush privileges
        self._execute_sql("FLUSH PRIVILEGES;")
        
        self.logger.info(f"Created user: {username}@{host}")
        
        user_info = UserInfo(
            username=username,
            engine=self.ENGINE_NAME,
            host=host,
        )
        
        return user_info, password
    
    def drop_user(self, username: str, host: str = "localhost") -> bool:
        """Drop a MySQL user."""
        if not self.user_exists(username, host):
            raise DatabaseUserError(f"User '{username}'@'{host}' does not exist")
        
        sql = f"DROP USER '{username}'@'{host}';"
        success, output = self._execute_sql(sql)
        
        if not success:
            raise DatabaseUserError(f"Failed to drop user '{username}'", output)
        
        self._execute_sql("FLUSH PRIVILEGES;")
        
        self.logger.info(f"Dropped user: {username}@{host}")
        return True
    
    def user_exists(self, username: str, host: str = "localhost") -> bool:
        """Check if a user exists."""
        sql = f"SELECT User FROM mysql.user WHERE User = '{username}' AND Host = '{host}';"
        success, output = self._execute_sql(sql)
        return success and username in output
    
    def list_users(self) -> List[UserInfo]:
        """List all MySQL users."""
        sql = "SELECT User, Host FROM mysql.user WHERE User != '' ORDER BY User;"
        success, output = self._execute_sql(sql)
        
        if not success:
            return []
        
        users = []
        for line in output.strip().split("\n"):
            if not line:
                continue
            parts = line.split("\t")
            if len(parts) >= 2:
                users.append(UserInfo(
                    username=parts[0],
                    engine=self.ENGINE_NAME,
                    host=parts[1],
                ))
        
        return users
    
    def grant_privileges(
        self,
        username: str,
        database: str,
        privileges: List[str] = None,
        host: str = "localhost",
    ) -> bool:
        """Grant privileges to a user on a database."""
        privs = ", ".join(privileges) if privileges else "ALL PRIVILEGES"
        
        sql = f"GRANT {privs} ON `{database}`.* TO '{username}'@'{host}';"
        success, output = self._execute_sql(sql)
        
        if not success:
            raise DatabaseUserError(f"Failed to grant privileges", output)
        
        self._execute_sql("FLUSH PRIVILEGES;")
        
        self.logger.info(f"Granted {privs} on {database} to {username}@{host}")
        return True
    
    def revoke_privileges(
        self,
        username: str,
        database: str,
        privileges: List[str] = None,
        host: str = "localhost",
    ) -> bool:
        """Revoke privileges from a user on a database."""
        privs = ", ".join(privileges) if privileges else "ALL PRIVILEGES"
        
        sql = f"REVOKE {privs} ON `{database}`.* FROM '{username}'@'{host}';"
        success, output = self._execute_sql(sql)
        
        if not success:
            raise DatabaseUserError(f"Failed to revoke privileges", output)
        
        self._execute_sql("FLUSH PRIVILEGES;")
        
        self.logger.info(f"Revoked {privs} on {database} from {username}@{host}")
        return True
    
    # ==================== Backup & Restore ====================
    
    def backup(
        self,
        database: str,
        output_path: Optional[Path] = None,
        compress: bool = True,
        **kwargs,
    ) -> BackupInfo:
        """Create a MySQL database backup using mysqldump."""
        if not self.database_exists(database):
            raise DatabaseNotFoundError(f"Database '{database}' does not exist")
        
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"{self.ENGINE_NAME}-{database}-{timestamp}.sql"
        if compress:
            filename += ".gz"
        
        backup_path = output_path or (self.BACKUP_DIR / filename)
        
        # Create backup directory if needed
        backup_path.parent.mkdir(parents=True, exist_ok=True)
        
        cmd = ["mysqldump", "--single-transaction", "--routines", "--triggers", database]
        
        if compress:
            # Pipe through gzip
            full_cmd = " ".join(cmd) + f" | gzip > {backup_path}"
            result = self._run_sudo(["bash", "-c", full_cmd])
        else:
            result = self._run_sudo(cmd)
            if result.success:
                backup_path.write_text(result.stdout)
        
        if not result.success:
            raise DatabaseBackupError(f"Failed to backup database '{database}'", result.stderr)
        
        stat = backup_path.stat()
        
        self.logger.info(f"Created backup: {backup_path}")
        
        return BackupInfo(
            path=backup_path,
            database=database,
            engine=self.ENGINE_NAME,
            size=stat.st_size,
            created=datetime.now(),
            compressed=compress,
        )
    
    def restore(
        self,
        database: str,
        backup_path: Path,
        drop_existing: bool = False,
        **kwargs,
    ) -> bool:
        """Restore a MySQL database from backup."""
        backup_path = Path(backup_path)
        
        if not backup_path.exists():
            raise DatabaseBackupError(f"Backup file not found: {backup_path}")
        
        if drop_existing and self.database_exists(database):
            self.drop_database(database, force=True)
        
        # Create database if it doesn't exist
        if not self.database_exists(database):
            self.create_database(database)
        
        # Restore based on compression
        if backup_path.suffix == ".gz":
            full_cmd = f"gunzip < {backup_path} | mysql {database}"
            result = self._run_sudo(["bash", "-c", full_cmd])
        else:
            result = self._run_sudo(["bash", "-c", f"mysql {database} < {backup_path}"])
        
        if not result.success:
            raise DatabaseBackupError(f"Failed to restore database '{database}'", result.stderr)
        
        self.logger.info(f"Restored database: {database} from {backup_path}")
        return True
    
    # ==================== Query Execution ====================
    
    def execute_query(
        self,
        database: str,
        query: str,
        **kwargs,
    ) -> tuple[bool, str]:
        """Execute a SQL query."""
        if not self.database_exists(database):
            raise DatabaseNotFoundError(f"Database '{database}' does not exist")
        
        success, output = self._execute_sql(query, database=database)
        
        if not success:
            raise DatabaseQueryError(f"Query failed", output)
        
        return success, output
    
    def get_connection_string(
        self,
        database: str,
        username: str,
        password: str,
        host: str = "localhost",
    ) -> str:
        """Get a MySQL connection string."""
        return f"mysql://{username}:{password}@{host}:{self.DEFAULT_PORT}/{database}"
    
    def get_interactive_command(
        self,
        database: Optional[str] = None,
        username: Optional[str] = None,
    ) -> List[str]:
        """Get the command to connect interactively."""
        cmd = ["mysql"]
        if username:
            cmd.extend(["-u", username, "-p"])
        if database:
            cmd.append(database)
        return cmd


# Register the manager
DatabaseRegistry.register(MySQLManager, aliases=["mariadb", "maria"])
