# Copyright (c) 2024-2025 Yago López Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
MongoDB database manager for WASM.
"""

import json
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


class MongoDBManager(BaseDatabaseManager):
    """
    Manager for MongoDB databases.
    """
    
    ENGINE_NAME = "mongodb"
    DISPLAY_NAME = "MongoDB"
    DEFAULT_PORT = 27017
    SERVICE_NAME = "mongod"
    PACKAGE_NAMES = ["mongodb-org"]
    
    # System databases to exclude from listings
    SYSTEM_DATABASES = {
        "admin",
        "config",
        "local",
    }
    
    def __init__(self, verbose: bool = False):
        """Initialize MongoDB manager."""
        super().__init__(verbose=verbose)
    
    def is_installed(self) -> bool:
        """Check if MongoDB is installed."""
        result = self._run(["which", "mongod"])
        return result.success
    
    def get_version(self) -> Optional[str]:
        """Get MongoDB version."""
        result = self._run(["mongod", "--version"])
        if result.success:
            match = re.search(r"db version v(\d+\.\d+\.\d+)", result.stdout)
            if match:
                return match.group(1)
        return None
    
    def install(self) -> bool:
        """Install MongoDB."""
        self.logger.info(f"Installing {self.DISPLAY_NAME}...")
        
        # Import MongoDB GPG key
        result = self._run_sudo([
            "bash", "-c",
            "curl -fsSL https://www.mongodb.org/static/pgp/server-7.0.asc | "
            "gpg --dearmor -o /usr/share/keyrings/mongodb-server-7.0.gpg"
        ])
        
        # Add MongoDB repository
        result = self._run_sudo([
            "bash", "-c",
            'echo "deb [ arch=amd64,arm64 signed-by=/usr/share/keyrings/mongodb-server-7.0.gpg ] '
            'https://repo.mongodb.org/apt/ubuntu jammy/mongodb-org/7.0 multiverse" | '
            "tee /etc/apt/sources.list.d/mongodb-org-7.0.list"
        ])
        
        # Update package list
        result = self._run_sudo(["apt-get", "update"])
        if not result.success:
            raise DatabaseEngineError("Failed to update package list", result.stderr)
        
        # Install MongoDB
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
        """Uninstall MongoDB."""
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
            self._run_sudo(["rm", "-rf", "/var/lib/mongodb"])
            self._run_sudo(["rm", "-rf", "/var/log/mongodb"])
            self._run_sudo(["rm", "-rf", "/etc/mongod.conf"])
        
        return True
    
    def _execute_mongo(
        self,
        command: str,
        database: str = "admin",
    ) -> tuple[bool, str]:
        """
        Execute MongoDB command using mongosh.
        
        Args:
            command: JavaScript command to execute.
            database: Database to use.
            
        Returns:
            Tuple of (success, output).
        """
        # Try mongosh first (MongoDB 6+), fall back to mongo
        for shell in ["mongosh", "mongo"]:
            which_result = self._run(["which", shell])
            if which_result.success:
                cmd = [shell, database, "--quiet", "--eval", command]
                result = self._run(cmd)
                return result.success, result.stdout if result.success else result.stderr
        
        return False, "MongoDB shell not found (mongosh or mongo)"
    
    def _execute_mongo_json(
        self,
        command: str,
        database: str = "admin",
    ) -> tuple[bool, Any]:
        """Execute MongoDB command and parse JSON output."""
        # Wrap command to output JSON
        json_cmd = f"EJSON.stringify({command})"
        success, output = self._execute_mongo(json_cmd, database)
        
        if success and output.strip():
            try:
                return True, json.loads(output.strip())
            except json.JSONDecodeError:
                return success, output
        
        return success, output
    
    # ==================== Database Management ====================
    
    def create_database(
        self,
        name: str,
        owner: Optional[str] = None,
        encoding: Optional[str] = None,
        **kwargs,
    ) -> DatabaseInfo:
        """
        Create a new MongoDB database.
        
        MongoDB creates databases on first use, so we just insert a temp document.
        """
        if self.database_exists(name):
            raise DatabaseExistsError(f"Database '{name}' already exists")
        
        # Create a collection to instantiate the database
        cmd = f"db.getSiblingDB('{name}').createCollection('_wasm_init')"
        success, output = self._execute_mongo(cmd)
        
        if not success:
            raise DatabaseError(f"Failed to create database '{name}'", output)
        
        # Drop the temp collection
        self._execute_mongo(f"db.getSiblingDB('{name}')._wasm_init.drop()")
        
        self.logger.info(f"Created database: {name}")
        return self.get_database_info(name)
    
    def drop_database(self, name: str, force: bool = False) -> bool:
        """Drop a MongoDB database."""
        if not self.database_exists(name) and not force:
            raise DatabaseNotFoundError(f"Database '{name}' does not exist")
        
        cmd = f"db.getSiblingDB('{name}').dropDatabase()"
        success, output = self._execute_mongo(cmd)
        
        if not success:
            raise DatabaseError(f"Failed to drop database '{name}'", output)
        
        self.logger.info(f"Dropped database: {name}")
        return True
    
    def database_exists(self, name: str) -> bool:
        """Check if a database exists."""
        cmd = "db.adminCommand('listDatabases').databases.map(d => d.name)"
        success, output = self._execute_mongo(cmd)
        
        if success:
            return name in output
        return False
    
    def list_databases(self) -> List[DatabaseInfo]:
        """List all databases."""
        cmd = "db.adminCommand('listDatabases')"
        success, data = self._execute_mongo_json(cmd)
        
        if not success:
            return []
        
        databases = []
        if isinstance(data, dict) and "databases" in data:
            for db_info in data["databases"]:
                name = db_info.get("name", "")
                if name in self.SYSTEM_DATABASES:
                    continue
                
                size = db_info.get("sizeOnDisk", 0)
                
                databases.append(DatabaseInfo(
                    name=name,
                    engine=self.ENGINE_NAME,
                    size=self._format_size(size),
                    extra={"sizeOnDisk": size, "empty": db_info.get("empty", False)},
                ))
        
        return databases
    
    def get_database_info(self, name: str) -> DatabaseInfo:
        """Get detailed database information."""
        if not self.database_exists(name):
            raise DatabaseNotFoundError(f"Database '{name}' does not exist")
        
        # Get database stats
        cmd = f"db.getSiblingDB('{name}').stats()"
        success, data = self._execute_mongo_json(cmd)
        
        size = None
        collections = 0
        
        if success and isinstance(data, dict):
            size = self._format_size(data.get("dataSize", 0))
            collections = data.get("collections", 0)
        
        return DatabaseInfo(
            name=name,
            engine=self.ENGINE_NAME,
            size=size,
            tables=collections,  # Using tables field for collection count
            extra=data if isinstance(data, dict) else {},
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
        """Create a new MongoDB user."""
        if self.user_exists(username):
            raise DatabaseUserError(f"User '{username}' already exists")
        
        password = password or self.generate_password()
        database = kwargs.get("database", "admin")
        roles = kwargs.get("roles", [{"role": "readWrite", "db": database}])
        
        # Build create user command
        roles_json = json.dumps(roles)
        cmd = f"""
            db.getSiblingDB('{database}').createUser({{
                user: '{username}',
                pwd: '{password}',
                roles: {roles_json}
            }})
        """
        
        success, output = self._execute_mongo(cmd)
        
        if not success:
            raise DatabaseUserError(f"Failed to create user '{username}'", output)
        
        self.logger.info(f"Created user: {username}")
        
        user_info = UserInfo(
            username=username,
            engine=self.ENGINE_NAME,
            host=host,
            databases=[database],
            privileges=[r.get("role", "") for r in roles if isinstance(r, dict)],
        )
        
        return user_info, password
    
    def drop_user(self, username: str, host: str = "localhost") -> bool:
        """Drop a MongoDB user."""
        if not self.user_exists(username):
            raise DatabaseUserError(f"User '{username}' does not exist")
        
        cmd = f"db.dropUser('{username}')"
        success, output = self._execute_mongo(cmd, database="admin")
        
        if not success:
            raise DatabaseUserError(f"Failed to drop user '{username}'", output)
        
        self.logger.info(f"Dropped user: {username}")
        return True
    
    def user_exists(self, username: str, host: str = "localhost") -> bool:
        """Check if a user exists."""
        cmd = f"db.getUser('{username}')"
        success, output = self._execute_mongo(cmd, database="admin")
        return success and output.strip() and output.strip() != "null"
    
    def list_users(self) -> List[UserInfo]:
        """List all MongoDB users."""
        cmd = "db.getUsers()"
        success, data = self._execute_mongo_json(cmd, database="admin")
        
        if not success:
            return []
        
        users = []
        user_list = data.get("users", []) if isinstance(data, dict) else data if isinstance(data, list) else []
        
        for user_data in user_list:
            if isinstance(user_data, dict):
                username = user_data.get("user", "")
                roles = user_data.get("roles", [])
                
                databases = list(set(
                    r.get("db", "") for r in roles
                    if isinstance(r, dict) and r.get("db")
                ))
                privileges = list(set(
                    r.get("role", "") for r in roles
                    if isinstance(r, dict)
                ))
                
                users.append(UserInfo(
                    username=username,
                    engine=self.ENGINE_NAME,
                    databases=databases,
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
        """Grant roles to a user on a database."""
        roles = privileges or ["readWrite"]
        
        roles_json = json.dumps([{"role": r, "db": database} for r in roles])
        cmd = f"db.grantRolesToUser('{username}', {roles_json})"
        
        success, output = self._execute_mongo(cmd, database="admin")
        
        if not success:
            raise DatabaseUserError(f"Failed to grant privileges", output)
        
        self.logger.info(f"Granted {roles} on {database} to {username}")
        return True
    
    def revoke_privileges(
        self,
        username: str,
        database: str,
        privileges: List[str] = None,
        host: str = "localhost",
    ) -> bool:
        """Revoke roles from a user on a database."""
        roles = privileges or ["readWrite"]
        
        roles_json = json.dumps([{"role": r, "db": database} for r in roles])
        cmd = f"db.revokeRolesFromUser('{username}', {roles_json})"
        
        success, output = self._execute_mongo(cmd, database="admin")
        
        if not success:
            raise DatabaseUserError(f"Failed to revoke privileges", output)
        
        self.logger.info(f"Revoked {roles} on {database} from {username}")
        return True
    
    # ==================== Backup & Restore ====================
    
    def backup(
        self,
        database: str,
        output_path: Optional[Path] = None,
        compress: bool = True,
        **kwargs,
    ) -> BackupInfo:
        """Create a MongoDB database backup using mongodump."""
        if not self.database_exists(database):
            raise DatabaseNotFoundError(f"Database '{database}' does not exist")
        
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        dirname = f"{self.ENGINE_NAME}-{database}-{timestamp}"
        
        backup_dir = self.BACKUP_DIR / dirname
        
        # Create backup directory
        self._run_sudo(["mkdir", "-p", str(self.BACKUP_DIR)])
        
        # Run mongodump
        cmd = ["mongodump", "--db", database, "--out", str(backup_dir)]
        
        if compress:
            cmd.append("--gzip")
        
        result = self._run_sudo(cmd)
        
        if not result.success:
            raise DatabaseBackupError(f"Failed to backup database '{database}'", result.stderr)
        
        # Create a tarball
        tar_file = self.BACKUP_DIR / f"{dirname}.tar.gz"
        self._run_sudo([
            "tar", "-czf", str(tar_file),
            "-C", str(self.BACKUP_DIR),
            dirname
        ])
        
        # Remove the directory
        self._run_sudo(["rm", "-rf", str(backup_dir)])
        
        # Get file stats
        stat_result = self._run(["stat", "-c", "%s", str(tar_file)])
        size = int(stat_result.stdout.strip()) if stat_result.success else 0
        
        self.logger.info(f"Created backup: {tar_file}")
        
        return BackupInfo(
            path=tar_file,
            database=database,
            engine=self.ENGINE_NAME,
            size=size,
            created=datetime.now(),
            compressed=True,
        )
    
    def restore(
        self,
        database: str,
        backup_path: Path,
        drop_existing: bool = False,
        **kwargs,
    ) -> bool:
        """Restore a MongoDB database from backup."""
        backup_path = Path(backup_path)
        
        if not backup_path.exists():
            raise DatabaseBackupError(f"Backup file not found: {backup_path}")
        
        if drop_existing and self.database_exists(database):
            self.drop_database(database, force=True)
        
        # Create temp directory
        import tempfile
        with tempfile.TemporaryDirectory() as temp_dir:
            # Extract tarball
            if backup_path.suffix == ".gz" and ".tar" in backup_path.name:
                result = self._run_sudo([
                    "tar", "-xzf", str(backup_path),
                    "-C", temp_dir
                ])
                
                if not result.success:
                    raise DatabaseBackupError("Failed to extract backup", result.stderr)
                
                # Find the dump directory
                dump_dir = Path(temp_dir)
                subdirs = list(dump_dir.iterdir())
                if subdirs:
                    dump_dir = subdirs[0]
            else:
                dump_dir = backup_path
            
            # Run mongorestore
            cmd = ["mongorestore", "--db", database, "--drop" if drop_existing else ""]
            cmd = [c for c in cmd if c]  # Remove empty strings
            
            # Check if gzipped
            gzip_files = list(dump_dir.rglob("*.gz"))
            if gzip_files:
                cmd.append("--gzip")
            
            cmd.append(str(dump_dir / database if (dump_dir / database).exists() else dump_dir))
            
            result = self._run_sudo(cmd)
            
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
        """Execute a MongoDB command."""
        if not self.database_exists(database):
            raise DatabaseNotFoundError(f"Database '{database}' does not exist")
        
        success, output = self._execute_mongo(query, database=database)
        
        if not success:
            raise DatabaseQueryError(f"Query failed", output)
        
        return success, output if isinstance(output, str) else json.dumps(output, indent=2)
    
    def get_connection_string(
        self,
        database: str,
        username: str,
        password: str,
        host: str = "localhost",
    ) -> str:
        """Get a MongoDB connection string."""
        return f"mongodb://{username}:{password}@{host}:{self.DEFAULT_PORT}/{database}"
    
    def get_interactive_command(
        self,
        database: Optional[str] = None,
        username: Optional[str] = None,
    ) -> List[str]:
        """Get the command to connect interactively."""
        # Prefer mongosh over mongo
        for shell in ["mongosh", "mongo"]:
            result = self._run(["which", shell])
            if result.success:
                cmd = [shell]
                if database:
                    cmd.append(database)
                if username:
                    cmd.extend(["--username", username])
                return cmd
        
        return ["mongosh"]  # Default


# Register the manager
DatabaseRegistry.register(MongoDBManager, aliases=["mongo", "mongod"])
