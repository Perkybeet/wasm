# Copyright (c) 2024-2025 Yago López Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
Redis database manager for WASM.

Redis is a key-value store, so it doesn't have traditional databases,
users, or SQL queries. Instead, it uses numbered databases (0-15 by default)
and authentication is global.
"""

import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from wasm.core.exceptions import (
    DatabaseError,
    DatabaseNotFoundError,
    DatabaseEngineError,
    DatabaseBackupError,
    DatabaseQueryError,
    DatabaseUserError,
)
from wasm.managers.database.base import (
    BaseDatabaseManager,
    DatabaseInfo,
    UserInfo,
    BackupInfo,
)
from wasm.managers.database.registry import DatabaseRegistry


class RedisManager(BaseDatabaseManager):
    """
    Manager for Redis databases.
    
    Redis is a key-value store with numbered databases (0-15 by default).
    User management is different from traditional databases.
    """
    
    ENGINE_NAME = "redis"
    DISPLAY_NAME = "Redis"
    DEFAULT_PORT = 6379
    SERVICE_NAME = "redis-server"
    PACKAGE_NAMES = ["redis-server"]
    
    # Redis config file location
    CONFIG_FILE = Path("/etc/redis/redis.conf")
    DATA_DIR = Path("/var/lib/redis")
    
    def __init__(self, verbose: bool = False):
        """Initialize Redis manager."""
        super().__init__(verbose=verbose)
        self._password = None
    
    def is_installed(self) -> bool:
        """Check if Redis is installed."""
        result = self._run(["which", "redis-cli"])
        return result.success
    
    def get_version(self) -> Optional[str]:
        """Get Redis version."""
        result = self._run(["redis-server", "--version"])
        if result.success:
            match = re.search(r"v=(\d+\.\d+\.\d+)", result.stdout)
            if match:
                return match.group(1)
        return None
    
    def install(self) -> bool:
        """Install Redis."""
        self.logger.info(f"Installing {self.DISPLAY_NAME}...")
        
        # Update package list
        result = self._run_sudo(["apt-get", "update"])
        if not result.success:
            raise DatabaseEngineError("Failed to update package list", result.stderr)
        
        # Install Redis
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
        """Uninstall Redis."""
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
            self._run_sudo(["rm", "-rf", str(self.DATA_DIR)])
            self._run_sudo(["rm", "-rf", "/etc/redis"])
        
        return True
    
    def _execute_redis(
        self,
        *args: str,
        db: int = 0,
    ) -> tuple[bool, str]:
        """
        Execute Redis CLI command.
        
        Args:
            *args: Redis command arguments.
            db: Database number.
            
        Returns:
            Tuple of (success, output).
        """
        cmd = ["redis-cli", "-n", str(db)]
        
        if self._password:
            cmd.extend(["-a", self._password])
        
        cmd.extend(args)
        
        result = self._run(cmd)
        return result.success, result.stdout if result.success else result.stderr
    
    def get_status(self) -> Dict[str, Any]:
        """Get Redis status with additional info."""
        status = super().get_status()
        
        if status["running"]:
            # Get additional info
            success, output = self._execute_redis("INFO", "server")
            if success:
                for line in output.split("\n"):
                    if ":" in line:
                        key, value = line.strip().split(":", 1)
                        if key == "redis_mode":
                            status["mode"] = value
                        elif key == "connected_clients":
                            status["clients"] = int(value)
                        elif key == "used_memory_human":
                            status["memory"] = value
        
        return status
    
    # ==================== Database Management ====================
    # Redis uses numbered databases (0-15 by default)
    
    def create_database(
        self,
        name: str,
        owner: Optional[str] = None,
        encoding: Optional[str] = None,
        **kwargs,
    ) -> DatabaseInfo:
        """
        Redis doesn't support creating databases.
        Databases are numbered 0-15 by default.
        """
        raise DatabaseError(
            "Redis uses numbered databases (0-15)",
            "Use SELECT <db_number> to switch databases. Configure 'databases' in redis.conf to change count."
        )
    
    def drop_database(self, name: str, force: bool = False) -> bool:
        """
        Flush all keys from a Redis database.
        
        Args:
            name: Database number (0-15).
            force: Force flush.
        """
        try:
            db_num = int(name)
        except ValueError:
            raise DatabaseError(f"Invalid database number: {name}")
        
        success, output = self._execute_redis("FLUSHDB", db=db_num)
        
        if not success:
            raise DatabaseError(f"Failed to flush database {db_num}", output)
        
        self.logger.info(f"Flushed database: {db_num}")
        return True
    
    def database_exists(self, name: str) -> bool:
        """Check if a database number is valid."""
        try:
            db_num = int(name)
            # Get max databases from config
            success, output = self._execute_redis("CONFIG", "GET", "databases")
            if success:
                parts = output.strip().split("\n")
                if len(parts) >= 2:
                    max_dbs = int(parts[1])
                    return 0 <= db_num < max_dbs
            return 0 <= db_num < 16  # Default max
        except ValueError:
            return False
    
    def list_databases(self) -> List[DatabaseInfo]:
        """List Redis databases with key counts."""
        databases = []
        
        # Get keyspace info
        success, output = self._execute_redis("INFO", "keyspace")
        
        keyspace = {}
        if success:
            for line in output.split("\n"):
                if line.startswith("db"):
                    match = re.match(r"db(\d+):keys=(\d+)", line)
                    if match:
                        keyspace[int(match.group(1))] = int(match.group(2))
        
        # Get number of databases
        success, output = self._execute_redis("CONFIG", "GET", "databases")
        max_dbs = 16
        if success:
            parts = output.strip().split("\n")
            if len(parts) >= 2:
                try:
                    max_dbs = int(parts[1])
                except ValueError:
                    pass
        
        # Create info for databases with keys
        for db_num in range(max_dbs):
            keys = keyspace.get(db_num, 0)
            if keys > 0 or db_num == 0:  # Always show db0
                databases.append(DatabaseInfo(
                    name=str(db_num),
                    engine=self.ENGINE_NAME,
                    tables=keys,  # Using tables field for key count
                    extra={"keys": keys},
                ))
        
        return databases
    
    def get_database_info(self, name: str) -> DatabaseInfo:
        """Get info for a specific Redis database."""
        try:
            db_num = int(name)
        except ValueError:
            raise DatabaseNotFoundError(f"Invalid database number: {name}")
        
        if not self.database_exists(name):
            raise DatabaseNotFoundError(f"Database {db_num} does not exist")
        
        # Get key count
        success, output = self._execute_redis("DBSIZE", db=db_num)
        keys = 0
        if success:
            match = re.search(r"(\d+)", output)
            if match:
                keys = int(match.group(1))
        
        # Get memory usage for this db (approximate)
        success, output = self._execute_redis("INFO", "memory")
        memory = None
        if success:
            for line in output.split("\n"):
                if "used_memory_human" in line:
                    memory = line.split(":")[1].strip()
                    break
        
        return DatabaseInfo(
            name=str(db_num),
            engine=self.ENGINE_NAME,
            size=memory,
            tables=keys,
            extra={"keys": keys},
        )
    
    # ==================== User Management ====================
    # Redis 6+ has ACL support
    
    def create_user(
        self,
        username: str,
        password: Optional[str] = None,
        host: str = "localhost",
        **kwargs,
    ) -> tuple[UserInfo, str]:
        """Create a Redis ACL user (Redis 6+)."""
        if self.user_exists(username):
            raise DatabaseUserError(f"User '{username}' already exists")
        
        password = password or self.generate_password()
        
        # Default permissions
        permissions = kwargs.get("permissions", ["+@all", "~*"])
        
        # Build ACL command
        cmd_parts = ["ACL", "SETUSER", username, "on", f">{password}"]
        cmd_parts.extend(permissions)
        
        success, output = self._execute_redis(*cmd_parts)
        
        if not success:
            if "ERR unknown command" in output:
                raise DatabaseUserError(
                    "ACL commands not supported",
                    "Redis ACL requires Redis 6.0 or later"
                )
            raise DatabaseUserError(f"Failed to create user '{username}'", output)
        
        self.logger.info(f"Created user: {username}")
        
        user_info = UserInfo(
            username=username,
            engine=self.ENGINE_NAME,
            host=host,
            privileges=permissions,
        )
        
        return user_info, password
    
    def drop_user(self, username: str, host: str = "localhost") -> bool:
        """Delete a Redis ACL user."""
        if not self.user_exists(username):
            raise DatabaseUserError(f"User '{username}' does not exist")
        
        success, output = self._execute_redis("ACL", "DELUSER", username)
        
        if not success:
            raise DatabaseUserError(f"Failed to delete user '{username}'", output)
        
        self.logger.info(f"Deleted user: {username}")
        return True
    
    def user_exists(self, username: str, host: str = "localhost") -> bool:
        """Check if a Redis ACL user exists."""
        success, output = self._execute_redis("ACL", "LIST")
        if success:
            for line in output.split("\n"):
                if line.startswith(f"user {username} "):
                    return True
        return False
    
    def list_users(self) -> List[UserInfo]:
        """List all Redis ACL users."""
        success, output = self._execute_redis("ACL", "LIST")
        
        if not success:
            return []
        
        users = []
        for line in output.split("\n"):
            if line.startswith("user "):
                parts = line.split()
                if len(parts) >= 2:
                    username = parts[1]
                    # Parse permissions
                    privileges = parts[2:] if len(parts) > 2 else []
                    
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
        """Grant privileges to a Redis user."""
        if not self.user_exists(username):
            raise DatabaseUserError(f"User '{username}' does not exist")
        
        privs = privileges or ["+@all", "~*"]
        
        cmd_parts = ["ACL", "SETUSER", username]
        cmd_parts.extend(privs)
        
        success, output = self._execute_redis(*cmd_parts)
        
        if not success:
            raise DatabaseUserError(f"Failed to grant privileges", output)
        
        self.logger.info(f"Granted privileges to {username}")
        return True
    
    def revoke_privileges(
        self,
        username: str,
        database: str,
        privileges: List[str] = None,
        host: str = "localhost",
    ) -> bool:
        """Revoke privileges from a Redis user."""
        if not self.user_exists(username):
            raise DatabaseUserError(f"User '{username}' does not exist")
        
        # Revoke all permissions
        success, output = self._execute_redis("ACL", "SETUSER", username, "nocommands", "nokeys")
        
        if not success:
            raise DatabaseUserError(f"Failed to revoke privileges", output)
        
        self.logger.info(f"Revoked privileges from {username}")
        return True
    
    # ==================== Backup & Restore ====================
    
    def backup(
        self,
        database: str,
        output_path: Optional[Path] = None,
        compress: bool = True,
        **kwargs,
    ) -> BackupInfo:
        """
        Create a Redis backup using BGSAVE/RDB dump.
        
        For Redis, we backup the entire instance, not individual databases.
        The database parameter is ignored.
        """
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"{self.ENGINE_NAME}-dump-{timestamp}.rdb"
        if compress:
            filename += ".gz"
        
        backup_path = output_path or (self.BACKUP_DIR / filename)
        
        # Create backup directory if needed
        self._run_sudo(["mkdir", "-p", str(backup_path.parent)])
        
        # Trigger background save
        success, output = self._execute_redis("BGSAVE")
        if not success:
            # Try SAVE if BGSAVE fails
            success, output = self._execute_redis("SAVE")
            if not success:
                raise DatabaseBackupError("Failed to save Redis data", output)
        
        # Wait for save to complete
        import time
        for _ in range(30):
            success, output = self._execute_redis("LASTSAVE")
            if success:
                break
            time.sleep(1)
        
        # Copy the dump file
        rdb_file = self.DATA_DIR / "dump.rdb"
        
        if compress:
            result = self._run_sudo(["bash", "-c", f"gzip -c {rdb_file} > {backup_path}"])
        else:
            result = self._run_sudo(["cp", str(rdb_file), str(backup_path)])
        
        if not result.success:
            raise DatabaseBackupError("Failed to copy backup file", result.stderr)
        
        # Get file stats
        stat_result = self._run(["stat", "-c", "%s", str(backup_path)])
        size = int(stat_result.stdout.strip()) if stat_result.success else 0
        
        self.logger.info(f"Created backup: {backup_path}")
        
        return BackupInfo(
            path=backup_path,
            database="all",  # Redis backup includes all databases
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
        """Restore Redis from RDB backup."""
        backup_path = Path(backup_path)
        
        if not backup_path.exists():
            raise DatabaseBackupError(f"Backup file not found: {backup_path}")
        
        # Stop Redis
        self.stop()
        
        # Decompress if needed
        rdb_file = self.DATA_DIR / "dump.rdb"
        
        if backup_path.suffix == ".gz":
            result = self._run_sudo(["bash", "-c", f"gunzip -c {backup_path} > {rdb_file}"])
        else:
            result = self._run_sudo(["cp", str(backup_path), str(rdb_file)])
        
        if not result.success:
            self.start()  # Restart Redis before failing
            raise DatabaseBackupError("Failed to restore backup file", result.stderr)
        
        # Fix permissions
        self._run_sudo(["chown", "redis:redis", str(rdb_file)])
        
        # Start Redis
        self.start()
        
        self.logger.info(f"Restored from: {backup_path}")
        return True
    
    # ==================== Query Execution ====================
    
    def execute_query(
        self,
        database: str,
        query: str,
        **kwargs,
    ) -> tuple[bool, str]:
        """Execute a Redis command."""
        try:
            db_num = int(database)
        except ValueError:
            db_num = 0
        
        # Parse the command string
        parts = query.strip().split()
        if not parts:
            raise DatabaseQueryError("Empty command")
        
        success, output = self._execute_redis(*parts, db=db_num)
        
        if not success:
            raise DatabaseQueryError(f"Command failed", output)
        
        return success, output
    
    def get_connection_string(
        self,
        database: str,
        username: str,
        password: str,
        host: str = "localhost",
    ) -> str:
        """Get a Redis connection string."""
        try:
            db_num = int(database)
        except ValueError:
            db_num = 0
        
        if username and username != "default":
            return f"redis://{username}:{password}@{host}:{self.DEFAULT_PORT}/{db_num}"
        return f"redis://:{password}@{host}:{self.DEFAULT_PORT}/{db_num}"
    
    def get_interactive_command(
        self,
        database: Optional[str] = None,
        username: Optional[str] = None,
    ) -> List[str]:
        """Get the command to connect interactively."""
        cmd = ["redis-cli"]
        if database:
            try:
                db_num = int(database)
                cmd.extend(["-n", str(db_num)])
            except ValueError:
                pass
        return cmd
    
    # ==================== Redis-specific Commands ====================
    
    def set_password(self, password: str) -> bool:
        """Set the Redis password (requirepass)."""
        success, output = self._execute_redis("CONFIG", "SET", "requirepass", password)
        if success:
            self._password = password
            # Also save to config file
            self._execute_redis("CONFIG", "REWRITE")
        return success
    
    def get_memory_stats(self) -> Dict[str, Any]:
        """Get Redis memory statistics."""
        success, output = self._execute_redis("INFO", "memory")
        
        stats = {}
        if success:
            for line in output.split("\n"):
                if ":" in line:
                    key, value = line.strip().split(":", 1)
                    stats[key] = value
        
        return stats
    
    def flush_all(self) -> bool:
        """Flush all databases."""
        success, output = self._execute_redis("FLUSHALL")
        if not success:
            raise DatabaseError("Failed to flush all databases", output)
        return True


# Register the manager
DatabaseRegistry.register(RedisManager, aliases=["redis-server"])
