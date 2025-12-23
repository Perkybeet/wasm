# Copyright (c) 2024-2025 Yago López Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
PostgreSQL database manager for WASM.
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


class PostgresManager(BaseDatabaseManager):
    """
    Manager for PostgreSQL databases.
    """
    
    ENGINE_NAME = "postgresql"
    DISPLAY_NAME = "PostgreSQL"
    DEFAULT_PORT = 5432
    SERVICE_NAME = "postgresql"
    PACKAGE_NAMES = ["postgresql", "postgresql-contrib"]
    
    # System databases to exclude from listings
    SYSTEM_DATABASES = {
        "postgres",
        "template0",
        "template1",
    }
    
    def __init__(self, verbose: bool = False):
        """Initialize PostgreSQL manager."""
        super().__init__(verbose=verbose)
    
    def is_installed(self) -> bool:
        """Check if PostgreSQL is installed."""
        result = self._run(["which", "psql"])
        return result.success
    
    def get_version(self) -> Optional[str]:
        """Get PostgreSQL version."""
        result = self._run(["psql", "--version"])
        if result.success:
            match = re.search(r"(\d+\.\d+)", result.stdout)
            if match:
                return match.group(1)
        return None
    
    def install(self) -> bool:
        """Install PostgreSQL."""
        self.logger.info(f"Installing {self.DISPLAY_NAME}...")
        
        # Update package list
        result = self._run_sudo(["apt-get", "update"])
        if not result.success:
            raise DatabaseEngineError("Failed to update package list", result.stderr)
        
        # Install PostgreSQL
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
        
        return True
    
    def uninstall(self, purge: bool = False) -> bool:
        """Uninstall PostgreSQL."""
        self.logger.info(f"Uninstalling {self.DISPLAY_NAME}...")
        
        # Stop service
        try:
            self.stop()
        except Exception:
            pass
        
        action = "purge" if purge else "remove"
        
        result = self._run_sudo([
            "apt-get", action, "-y",
            *self.PACKAGE_NAMES,
        ])
        
        if purge:
            # Remove data directory
            self._run_sudo(["rm", "-rf", "/var/lib/postgresql"])
            self._run_sudo(["rm", "-rf", "/etc/postgresql"])
        
        return True
    
    def _execute_sql(
        self,
        sql: str,
        database: str = "postgres",
        return_output: bool = True,
    ) -> tuple[bool, str]:
        """
        Execute SQL command as postgres user.
        
        Args:
            sql: SQL command.
            database: Database to use.
            return_output: Return query output.
            
        Returns:
            Tuple of (success, output).
        """
        cmd = [
            "sudo", "-u", "postgres",
            "psql", "-d", database,
            "-t", "-A", "-c", sql
        ]
        
        result = self._run(cmd)
        return result.success, result.stdout if result.success else result.stderr
    
    # ==================== Database Management ====================
    
    def create_database(
        self,
        name: str,
        owner: Optional[str] = None,
        encoding: Optional[str] = None,
        **kwargs,
    ) -> DatabaseInfo:
        """Create a new PostgreSQL database."""
        if self.database_exists(name):
            raise DatabaseExistsError(f"Database '{name}' already exists")
        
        encoding = encoding or "UTF8"
        template = kwargs.get("template", "template0")
        
        sql = f"CREATE DATABASE \"{name}\" ENCODING '{encoding}' TEMPLATE {template}"
        
        if owner:
            sql += f" OWNER \"{owner}\""
        
        sql += ";"
        
        success, output = self._execute_sql(sql)
        
        if not success:
            raise DatabaseError(f"Failed to create database '{name}'", output)
        
        self.logger.info(f"Created database: {name}")
        return self.get_database_info(name)
    
    def drop_database(self, name: str, force: bool = False) -> bool:
        """Drop a PostgreSQL database."""
        if not self.database_exists(name):
            if force:
                return True
            raise DatabaseNotFoundError(f"Database '{name}' does not exist")
        
        # Terminate existing connections if force
        if force:
            self._execute_sql(
                f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = '{name}';"
            )
        
        sql = f"DROP DATABASE \"{name}\";"
        success, output = self._execute_sql(sql)
        
        if not success:
            raise DatabaseError(f"Failed to drop database '{name}'", output)
        
        self.logger.info(f"Dropped database: {name}")
        return True
    
    def database_exists(self, name: str) -> bool:
        """Check if a database exists."""
        sql = f"SELECT 1 FROM pg_database WHERE datname = '{name}';"
        success, output = self._execute_sql(sql)
        return success and "1" in output
    
    def list_databases(self) -> List[DatabaseInfo]:
        """List all databases."""
        sql = """
            SELECT datname, pg_encoding_to_char(encoding), pg_database_size(datname)
            FROM pg_database WHERE datistemplate = false;
        """
        success, output = self._execute_sql(sql)
        
        if not success:
            return []
        
        databases = []
        for line in output.strip().split("\n"):
            if not line:
                continue
            parts = line.split("|")
            if len(parts) >= 1:
                name = parts[0]
                if name in self.SYSTEM_DATABASES:
                    continue
                
                size = None
                if len(parts) >= 3:
                    try:
                        size = self._format_size(int(parts[2]))
                    except (ValueError, TypeError):
                        pass
                
                databases.append(DatabaseInfo(
                    name=name,
                    engine=self.ENGINE_NAME,
                    encoding=parts[1] if len(parts) > 1 else None,
                    size=size,
                ))
        
        return databases
    
    def get_database_info(self, name: str) -> DatabaseInfo:
        """Get detailed database information."""
        if not self.database_exists(name):
            raise DatabaseNotFoundError(f"Database '{name}' does not exist")
        
        # Get database info
        sql = f"""
            SELECT 
                datname,
                pg_encoding_to_char(encoding),
                pg_database_size('{name}'),
                r.rolname as owner
            FROM pg_database d
            JOIN pg_roles r ON d.datdba = r.oid
            WHERE datname = '{name}';
        """
        success, output = self._execute_sql(sql)
        
        encoding = None
        size = None
        owner = None
        
        if success and output.strip():
            parts = output.strip().split("|")
            if len(parts) >= 4:
                encoding = parts[1]
                try:
                    size = self._format_size(int(parts[2]))
                except (ValueError, TypeError):
                    pass
                owner = parts[3]
        
        # Get table count
        sql = f"SELECT COUNT(*) FROM information_schema.tables WHERE table_schema = 'public' AND table_catalog = '{name}';"
        success, output = self._execute_sql(sql, database=name)
        tables = 0
        if success:
            try:
                tables = int(output.strip())
            except (ValueError, TypeError):
                pass
        
        return DatabaseInfo(
            name=name,
            engine=self.ENGINE_NAME,
            size=size,
            tables=tables,
            owner=owner,
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
        """Create a new PostgreSQL user (role)."""
        if self.user_exists(username):
            raise DatabaseUserError(f"User '{username}' already exists")
        
        password = password or self.generate_password()
        
        # Build CREATE ROLE command
        options = ["LOGIN"]
        if kwargs.get("superuser"):
            options.append("SUPERUSER")
        if kwargs.get("createdb"):
            options.append("CREATEDB")
        if kwargs.get("createrole"):
            options.append("CREATEROLE")
        
        sql = f"CREATE ROLE \"{username}\" WITH {' '.join(options)} PASSWORD '{password}';"
        success, output = self._execute_sql(sql)
        
        if not success:
            raise DatabaseUserError(f"Failed to create user '{username}'", output)
        
        self.logger.info(f"Created user: {username}")
        
        user_info = UserInfo(
            username=username,
            engine=self.ENGINE_NAME,
            host=host,
        )
        
        return user_info, password
    
    def drop_user(self, username: str, host: str = "localhost") -> bool:
        """Drop a PostgreSQL user (role)."""
        if not self.user_exists(username):
            raise DatabaseUserError(f"User '{username}' does not exist")
        
        sql = f"DROP ROLE \"{username}\";"
        success, output = self._execute_sql(sql)
        
        if not success:
            raise DatabaseUserError(f"Failed to drop user '{username}'", output)
        
        self.logger.info(f"Dropped user: {username}")
        return True
    
    def user_exists(self, username: str, host: str = "localhost") -> bool:
        """Check if a user exists."""
        sql = f"SELECT 1 FROM pg_roles WHERE rolname = '{username}';"
        success, output = self._execute_sql(sql)
        return success and "1" in output
    
    def list_users(self) -> List[UserInfo]:
        """List all PostgreSQL users."""
        sql = """
            SELECT rolname, rolsuper, rolcreatedb, rolcreaterole
            FROM pg_roles 
            WHERE rolname NOT LIKE 'pg_%' 
            ORDER BY rolname;
        """
        success, output = self._execute_sql(sql)
        
        if not success:
            return []
        
        users = []
        for line in output.strip().split("\n"):
            if not line:
                continue
            parts = line.split("|")
            if len(parts) >= 1:
                username = parts[0]
                privileges = []
                if len(parts) >= 2 and parts[1] == "t":
                    privileges.append("SUPERUSER")
                if len(parts) >= 3 and parts[2] == "t":
                    privileges.append("CREATEDB")
                if len(parts) >= 4 and parts[3] == "t":
                    privileges.append("CREATEROLE")
                
                users.append(UserInfo(
                    username=username,
                    engine=self.ENGINE_NAME,
                    privileges=privileges,
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
        
        sql = f"GRANT {privs} ON DATABASE \"{database}\" TO \"{username}\";"
        success, output = self._execute_sql(sql)
        
        if not success:
            raise DatabaseUserError(f"Failed to grant privileges", output)
        
        # Also grant on all tables in public schema
        sql = f"GRANT {privs} ON ALL TABLES IN SCHEMA public TO \"{username}\";"
        self._execute_sql(sql, database=database)
        
        self.logger.info(f"Granted {privs} on {database} to {username}")
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
        
        sql = f"REVOKE {privs} ON DATABASE \"{database}\" FROM \"{username}\";"
        success, output = self._execute_sql(sql)
        
        if not success:
            raise DatabaseUserError(f"Failed to revoke privileges", output)
        
        self.logger.info(f"Revoked {privs} on {database} from {username}")
        return True
    
    # ==================== Backup & Restore ====================
    
    def backup(
        self,
        database: str,
        output_path: Optional[Path] = None,
        compress: bool = True,
        **kwargs,
    ) -> BackupInfo:
        """Create a PostgreSQL database backup using pg_dump."""
        if not self.database_exists(database):
            raise DatabaseNotFoundError(f"Database '{database}' does not exist")
        
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"{self.ENGINE_NAME}-{database}-{timestamp}.sql"
        if compress:
            filename += ".gz"
        
        backup_path = output_path or (self.BACKUP_DIR / filename)
        
        # Create backup directory if needed
        self._run_sudo(["mkdir", "-p", str(backup_path.parent)])
        
        format_opt = kwargs.get("format", "plain")  # plain, custom, directory, tar
        
        if compress and format_opt == "plain":
            # Pipe through gzip
            full_cmd = f"sudo -u postgres pg_dump {database} | gzip > {backup_path}"
            result = self._run(["bash", "-c", full_cmd])
        else:
            cmd = ["sudo", "-u", "postgres", "pg_dump", "-Fc" if format_opt == "custom" else "", database]
            cmd = [c for c in cmd if c]  # Remove empty strings
            result = self._run(cmd)
            if result.success:
                self._run_sudo(["bash", "-c", f"echo '{result.stdout}' > {backup_path}"])
        
        if not result.success:
            raise DatabaseBackupError(f"Failed to backup database '{database}'", result.stderr)
        
        # Get file stats
        stat_result = self._run(["stat", "-c", "%s", str(backup_path)])
        size = int(stat_result.stdout.strip()) if stat_result.success else 0
        
        self.logger.info(f"Created backup: {backup_path}")
        
        return BackupInfo(
            path=backup_path,
            database=database,
            engine=self.ENGINE_NAME,
            size=size,
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
        """Restore a PostgreSQL database from backup."""
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
            full_cmd = f"gunzip < {backup_path} | sudo -u postgres psql {database}"
            result = self._run(["bash", "-c", full_cmd])
        else:
            result = self._run(["sudo", "-u", "postgres", "psql", database, "-f", str(backup_path)])
        
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
        """Get a PostgreSQL connection string."""
        return f"postgresql://{username}:{password}@{host}:{self.DEFAULT_PORT}/{database}"
    
    def get_interactive_command(
        self,
        database: Optional[str] = None,
        username: Optional[str] = None,
    ) -> List[str]:
        """Get the command to connect interactively."""
        cmd = ["sudo", "-u", "postgres", "psql"]
        if database:
            cmd.extend(["-d", database])
        if username:
            cmd.extend(["-U", username])
        return cmd


# Register the manager
DatabaseRegistry.register(PostgresManager, aliases=["postgres", "pg", "pgsql"])
