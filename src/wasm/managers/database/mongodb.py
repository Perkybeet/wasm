# Copyright (c) 2024-2025 Yago López Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
MongoDB manager.

Scripts are piped into ``mongosh`` on stdin rather than passed with ``--eval``,
because a ``createUser`` script carries a password and argv is world readable.
Every value interpolated into a script is rendered with :func:`json.dumps`, which
is a valid JavaScript literal for strings, numbers and objects alike, so a
database name can never become code.
"""

from __future__ import annotations

import json
import tempfile
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from wasm.core.exceptions import (
    DatabaseBackupError,
    DatabaseEngineError,
    DatabaseError,
    DatabaseExistsError,
    DatabaseNotFoundError,
    DatabaseQueryError,
    DatabaseUserError,
)
from wasm.managers.database.base import (
    PACKAGE_TIMEOUT,
    QUERY_TIMEOUT,
    TRANSFER_TIMEOUT,
    BackupInfo,
    BaseDatabaseManager,
    DatabaseInfo,
    UserInfo,
    format_size,
)
from wasm.managers.database.registry import DatabaseRegistry

#: Roles MongoDB ships. A deployment may define its own, which are accepted as
#: long as the name is a plain identifier.
BUILT_IN_ROLES = frozenset(
    {
        "backup",
        "clusterAdmin",
        "clusterManager",
        "clusterMonitor",
        "dbAdmin",
        "dbAdminAnyDatabase",
        "dbOwner",
        "enableSharding",
        "hostManager",
        "read",
        "readAnyDatabase",
        "readWrite",
        "readWriteAnyDatabase",
        "restore",
        "root",
        "userAdmin",
        "userAdminAnyDatabase",
    }
)

#: Release series of the packages this manager installs.
SERVER_SERIES = "7.0"

#: Where the repository signing key is stored.
KEYRING_PATH = Path(f"/usr/share/keyrings/mongodb-server-{SERVER_SERIES}.gpg")

#: The apt source list this manager owns.
SOURCES_PATH = Path(f"/etc/apt/sources.list.d/mongodb-org-{SERVER_SERIES}.list")


class MongoDBManager(BaseDatabaseManager):
    """Manager for MongoDB deployments."""

    ENGINE_NAME = "mongodb"
    DISPLAY_NAME = "MongoDB"
    DEFAULT_PORT = 27017
    SERVICE_NAME = "mongod"
    PACKAGE_NAMES = ("mongodb-org",)
    CLIENT_BINARY = "mongod"
    VERSION_ARGV = ("mongod", "--version")
    VERSION_PATTERN = r"db version v(\d+\.\d+\.\d+)"
    PURGE_PATHS = ("/var/lib/mongodb", "/var/log/mongodb", "/etc/mongod.conf")
    BACKUP_SUFFIX = ".tar.gz"
    MAX_DATABASE_NAME_LENGTH = 63
    MAX_USER_NAME_LENGTH = 63

    #: Databases that belong to the deployment, not to a user.
    SYSTEM_DATABASES = frozenset({"admin", "config", "local"})

    #: Shells to try, newest first.
    SHELLS = ("mongosh", "mongo")

    # ==================== Installation ====================

    def _pre_install(self) -> None:
        """
        Add the upstream repository, since no distribution ships mongodb-org.

        Raises:
            DatabaseEngineError: When the key cannot be fetched or converted.
        """
        with tempfile.TemporaryDirectory(prefix="wasm-mongodb-") as workdir:
            armoured = Path(workdir) / "server.asc"
            result = self.runner.capture_to_file(
                ["curl", "-fsSL", f"https://www.mongodb.org/static/pgp/server-{SERVER_SERIES}.asc"],
                armoured,
                timeout=PACKAGE_TIMEOUT,
            )
            if not result.success:
                raise DatabaseEngineError(
                    "Failed to download the MongoDB signing key",
                    details=result.stderr.strip() or "Check outbound HTTPS access.",
                )

            result = self._exec(
                ["gpg", "--batch", "--yes", "--dearmor", "-o", str(KEYRING_PATH), str(armoured)],
                timeout=QUERY_TIMEOUT,
            )
            if not result.success:
                raise DatabaseEngineError(
                    "Failed to install the MongoDB signing key",
                    details=result.stderr.strip() or f"Could not write {KEYRING_PATH}.",
                )

        source = (
            f"deb [ arch=amd64,arm64 signed-by={KEYRING_PATH} ] "
            f"https://repo.mongodb.org/apt/ubuntu jammy/mongodb-org/{SERVER_SERIES} multiverse\n"
        )
        try:
            SOURCES_PATH.write_text(source)
        except OSError as exc:
            raise DatabaseEngineError(
                "Failed to add the MongoDB apt source",
                details=f"{exc}. Write {SOURCES_PATH} manually and retry.",
            ) from exc

    # ==================== Shell ====================

    @classmethod
    def validate_privileges(cls, privileges: Sequence[str] | None) -> tuple[str, ...]:
        """
        Check role names before they reach ``grantRolesToUser``.

        Args:
            privileges: Roles requested by the caller, or None for the default.

        Returns:
            The roles, without repeats.

        Raises:
            DatabaseUserError: When a role is not built in and does not look like
                a custom role name.
        """
        requested = list(privileges) if privileges else ["readWrite"]

        roles: list[str] = []
        for role in requested:
            if not isinstance(role, str) or (
                role not in BUILT_IN_ROLES and not role.replace("_", "").isalnum()
            ):
                raise DatabaseUserError(
                    f"Invalid MongoDB role: {role!r}",
                    details=(
                        f"Use a built-in role ({', '.join(sorted(BUILT_IN_ROLES))}) or the name "
                        "of a custom role, made of letters, digits and underscores."
                    ),
                )
            if role not in roles:
                roles.append(role)
        return tuple(roles)

    def _shell(self) -> str:
        """
        Pick the shell binary this host provides.

        Returns:
            The name of the shell to run.

        Raises:
            DatabaseEngineError: When no MongoDB shell is installed.
        """
        for shell in self.SHELLS:
            if self.runner.exists(shell):
                return shell
        raise DatabaseEngineError(
            "No MongoDB shell found",
            details="Install mongosh (or the legacy mongo client) and retry.",
        )

    def _execute_mongo(
        self,
        script: str,
        database: str = "admin",
        *,
        secrets: Sequence[str] = (),
        timeout: int = QUERY_TIMEOUT,
    ) -> tuple[bool, str]:
        """
        Run a JavaScript snippet, passing it on stdin.

        Args:
            script: The snippet.
            database: Database the shell connects to.
            secrets: Values the snippet carries that must not be logged.
            timeout: Deadline in seconds.

        Returns:
            Whether the shell succeeded, and its output or its error text.
        """
        result = self._exec(
            [self._shell(), database, "--quiet"],
            input=script,
            timeout=timeout,
            secrets=secrets,
        )
        return result.success, result.stdout if result.success else result.stderr

    def _execute_mongo_json(
        self,
        expression: str,
        database: str = "admin",
    ) -> tuple[bool, Any]:
        """
        Run an expression and parse its extended JSON result.

        Args:
            expression: The JavaScript expression.
            database: Database the shell connects to.

        Returns:
            Whether the shell succeeded, and the parsed value or the raw output.
        """
        success, output = self._execute_mongo(f"EJSON.stringify({expression})", database)
        if success and output.strip():
            try:
                return True, json.loads(output.strip())
            except json.JSONDecodeError:
                return success, output
        return success, output

    @staticmethod
    def _js(value: Any) -> str:
        """
        Render a Python value as a JavaScript literal.

        Args:
            value: The value to embed in a script.

        Returns:
            A literal that the shell parses as data, never as code.
        """
        return json.dumps(value)

    # ==================== Database Management ====================

    def create_database(
        self,
        name: str,
        owner: str | None = None,
        encoding: str | None = None,
        **kwargs,
    ) -> DatabaseInfo:
        """
        Create a database by creating, then dropping, a placeholder collection.

        Args:
            name: Database name.
            owner: Ignored; MongoDB users are granted roles instead.
            encoding: Ignored; MongoDB stores BSON.
            **kwargs: Unused.

        Returns:
            Information about the new database.

        Raises:
            DatabaseExistsError: When the database already exists.
            DatabaseError: When the name is invalid or creation fails.
        """
        self.validate_database_name(name)
        if self.database_exists(name):
            raise DatabaseExistsError(
                f"Database '{name}' already exists",
                details="Drop it first, or pick another name.",
            )

        success, output = self._execute_mongo(
            f"db.getSiblingDB({self._js(name)}).createCollection('_wasm_init')"
        )
        if not success:
            raise DatabaseError(f"Failed to create database '{name}'", details=output.strip())

        self._execute_mongo(f"db.getSiblingDB({self._js(name)})._wasm_init.drop()")

        self.logger.info(f"Created database: {name}")
        return self.get_database_info(name)

    def drop_database(self, name: str, force: bool = False) -> None:
        """
        Drop a database.

        Args:
            name: Database name.
            force: Accept a missing database as success.

        Raises:
            DatabaseNotFoundError: When the database does not exist.
            DatabaseError: When the drop fails.
        """
        self.validate_database_name(name)
        if not self.database_exists(name):
            if force:
                return
            raise DatabaseNotFoundError(
                f"Database '{name}' does not exist",
                details="Run 'wasm db list --engine mongodb' to see the databases.",
            )

        success, output = self._execute_mongo(f"db.getSiblingDB({self._js(name)}).dropDatabase()")
        if not success:
            raise DatabaseError(f"Failed to drop database '{name}'", details=output.strip())

        self.logger.info(f"Dropped database: {name}")

    def database_exists(self, name: str) -> bool:
        """
        Report whether a database exists.

        Args:
            name: Database name.

        Returns:
            True when the deployment lists the name.
        """
        success, data = self._execute_mongo_json(
            "db.adminCommand('listDatabases').databases.map(d => d.name)"
        )
        if not success:
            return False
        if isinstance(data, list):
            return name in data
        return f'"{name}"' in str(data)

    def list_databases(self) -> list[DatabaseInfo]:
        """
        List the databases that do not belong to the deployment itself.

        Returns:
            One entry per user database.
        """
        success, data = self._execute_mongo_json("db.adminCommand('listDatabases')")
        if not success or not isinstance(data, dict):
            return []

        databases = []
        for entry in data.get("databases", []):
            name = entry.get("name", "")
            if name in self.SYSTEM_DATABASES:
                continue
            size = entry.get("sizeOnDisk", 0)
            databases.append(
                DatabaseInfo(
                    name=name,
                    engine=self.ENGINE_NAME,
                    size=format_size(size),
                    extra={"sizeOnDisk": size, "empty": entry.get("empty", False)},
                )
            )
        return databases

    def get_database_info(self, name: str) -> DatabaseInfo:
        """
        Describe one database.

        Args:
            name: Database name.

        Returns:
            Size and collection count.

        Raises:
            DatabaseNotFoundError: When the database does not exist.
        """
        if not self.database_exists(name):
            raise DatabaseNotFoundError(f"Database '{name}' does not exist")

        success, data = self._execute_mongo_json(f"db.getSiblingDB({self._js(name)}).stats()")

        size = None
        collections = 0
        if success and isinstance(data, dict):
            size = format_size(data.get("dataSize", 0))
            collections = data.get("collections", 0)

        return DatabaseInfo(
            name=name,
            engine=self.ENGINE_NAME,
            size=size,
            tables=collections,
            extra=data if isinstance(data, dict) else {},
        )

    # ==================== User Management ====================

    def create_user(
        self,
        username: str,
        password: str | None = None,
        host: str = "localhost",
        **kwargs,
    ) -> tuple[UserInfo, str]:
        """
        Create a user with roles on a database.

        Args:
            username: User name.
            password: Password. Generated when omitted.
            host: Recorded for the caller; MongoDB has no per-host users.
            **kwargs: Accepts ``database`` and ``roles``.

        Returns:
            The user and its password.

        Raises:
            DatabaseUserError: When the user exists or creation fails.
        """
        self.validate_user_name(username)
        if self.user_exists(username):
            raise DatabaseUserError(
                f"User '{username}' already exists",
                details="Drop the user first, or pick another name.",
            )

        password = password or self.generate_password()
        database = kwargs.get("database", "admin")
        self.validate_database_name(database)

        roles: list[dict[str, str]] = []
        for entry in kwargs.get("roles") or self.validate_privileges(None):
            name = entry.get("role", "") if isinstance(entry, dict) else entry
            target = entry.get("db", database) if isinstance(entry, dict) else database
            self.validate_database_name(target)
            roles.append({"role": self.validate_privileges([name])[0], "db": target})

        script = (
            f"db.getSiblingDB({self._js(database)}).createUser({{"
            f"user: {self._js(username)}, pwd: {self._js(password)}, roles: {self._js(roles)}}})"
        )
        success, output = self._execute_mongo(script, secrets=(password,))
        if not success:
            raise DatabaseUserError(f"Failed to create user '{username}'", details=output.strip())

        self.logger.info(f"Created user: {username}")
        user = UserInfo(
            username=username,
            engine=self.ENGINE_NAME,
            host=host,
            databases=[database],
            privileges=[role["role"] for role in roles],
        )
        return user, password

    def drop_user(self, username: str, host: str = "localhost") -> None:
        """
        Drop a user from the admin database.

        Args:
            username: User name.
            host: Ignored; MongoDB has no per-host users.

        Raises:
            DatabaseUserError: When the user is missing or the drop fails.
        """
        self.validate_user_name(username)
        if not self.user_exists(username):
            raise DatabaseUserError(
                f"User '{username}' does not exist",
                details="Run 'wasm db users --engine mongodb' to see the users.",
            )

        success, output = self._execute_mongo(f"db.dropUser({self._js(username)})")
        if not success:
            raise DatabaseUserError(f"Failed to drop user '{username}'", details=output.strip())

        self.logger.info(f"Dropped user: {username}")

    def user_exists(self, username: str, host: str = "localhost") -> bool:
        """
        Report whether a user exists in the admin database.

        Args:
            username: User name.
            host: Ignored; MongoDB has no per-host users.

        Returns:
            True when getUser returns a document.
        """
        success, output = self._execute_mongo(f"db.getUser({self._js(username)})")
        return bool(success and output.strip() and output.strip() != "null")

    def list_users(self) -> list[UserInfo]:
        """
        List the users of the admin database.

        Returns:
            One entry per user, with its roles and databases.
        """
        success, data = self._execute_mongo_json("db.getUsers()")
        if not success:
            return []

        if isinstance(data, dict):
            entries = data.get("users", [])
        elif isinstance(data, list):
            entries = data
        else:
            entries = []

        users = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            roles = [role for role in entry.get("roles", []) if isinstance(role, dict)]
            users.append(
                UserInfo(
                    username=entry.get("user", ""),
                    engine=self.ENGINE_NAME,
                    databases=sorted({role.get("db", "") for role in roles if role.get("db")}),
                    privileges=sorted({role.get("role", "") for role in roles}),
                )
            )
        return users

    def grant_privileges(
        self,
        username: str,
        database: str,
        privileges: Sequence[str] | None = None,
        host: str = "localhost",
    ) -> None:
        """
        Grant roles on a database to a user.

        Args:
            username: User name.
            database: Database the roles apply to.
            privileges: Role names. readWrite when omitted.
            host: Ignored; MongoDB has no per-host users.

        Raises:
            DatabaseUserError: When a role is invalid or the grant fails.
        """
        self.validate_user_name(username)
        self.validate_database_name(database)
        roles = [{"role": role, "db": database} for role in self.validate_privileges(privileges)]

        success, output = self._execute_mongo(
            f"db.grantRolesToUser({self._js(username)}, {self._js(roles)})"
        )
        if not success:
            raise DatabaseUserError(
                f"Failed to grant roles on '{database}' to '{username}'", details=output.strip()
            )

        self.logger.info(f"Granted {[role['role'] for role in roles]} on {database} to {username}")

    def revoke_privileges(
        self,
        username: str,
        database: str,
        privileges: Sequence[str] | None = None,
        host: str = "localhost",
    ) -> None:
        """
        Revoke roles on a database from a user.

        Args:
            username: User name.
            database: Database the roles apply to.
            privileges: Role names. readWrite when omitted.
            host: Ignored; MongoDB has no per-host users.

        Raises:
            DatabaseUserError: When a role is invalid or the revoke fails.
        """
        self.validate_user_name(username)
        self.validate_database_name(database)
        roles = [{"role": role, "db": database} for role in self.validate_privileges(privileges)]

        success, output = self._execute_mongo(
            f"db.revokeRolesFromUser({self._js(username)}, {self._js(roles)})"
        )
        if not success:
            raise DatabaseUserError(
                f"Failed to revoke roles on '{database}' from '{username}'",
                details=output.strip(),
            )

        self.logger.info(
            f"Revoked {[role['role'] for role in roles]} on {database} from {username}"
        )

    # ==================== Backup & Restore ====================

    def backup(
        self,
        database: str,
        output_path: Path | None = None,
        compress: bool = True,
        **kwargs,
    ) -> BackupInfo:
        """
        Dump a database with mongodump and pack the result into a tarball.

        mongodump writes a directory tree, so the archive, not the dump itself, is
        what lands in the backup directory.

        Args:
            database: Database name.
            output_path: Custom destination for the tarball.
            compress: Compress the dumped BSON files.
            **kwargs: Unused.

        Returns:
            Information about the backup.

        Raises:
            DatabaseNotFoundError: When the database does not exist.
            DatabaseBackupError: When the dump or the archiving fails.
        """
        self.validate_database_name(database)
        if not self.database_exists(database):
            raise DatabaseNotFoundError(f"Database '{database}' does not exist")

        archive = self._backup_path(database, output_path, False)
        self._ensure_directory(archive.parent)

        with tempfile.TemporaryDirectory(prefix="wasm-mongodump-") as workdir:
            argv = ["mongodump", "--db", database, "--out", workdir]
            if compress:
                argv.append("--gzip")

            result = self._exec(argv, timeout=TRANSFER_TIMEOUT)
            if not result.success:
                raise DatabaseBackupError(
                    f"Failed to back up '{database}'",
                    details=result.stderr.strip() or "mongodump reported no error text.",
                )

            result = self._exec(
                ["tar", "-czf", str(archive), "-C", workdir, database],
                timeout=TRANSFER_TIMEOUT,
            )
            if not result.success:
                raise DatabaseBackupError(
                    f"Failed to archive the backup of '{database}'",
                    details=result.stderr.strip() or f"Could not write {archive}.",
                )

        try:
            size = archive.stat().st_size
        except OSError as exc:
            raise DatabaseBackupError(
                f"Backup of '{database}' produced no file",
                details=f"{exc}. tar reported success but {archive} is not readable.",
            ) from exc

        self.logger.info(f"Created backup: {archive}")
        return BackupInfo(
            path=archive,
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
    ) -> None:
        """
        Restore a database from a mongodump tarball or directory.

        Args:
            database: Target database name.
            backup_path: Tarball or dump directory.
            drop_existing: Drop the collections being restored first.
            **kwargs: Unused.

        Raises:
            DatabaseBackupError: When the file is missing or the restore fails.
        """
        self.validate_database_name(database)
        backup_path = Path(backup_path)
        if not backup_path.exists():
            raise DatabaseBackupError(
                f"Backup file not found: {backup_path}",
                details="Run 'wasm db backups' to list the backups WASM knows about.",
            )

        if drop_existing and self.database_exists(database):
            self.drop_database(database, force=True)

        with tempfile.TemporaryDirectory(prefix="wasm-mongorestore-") as workdir:
            if backup_path.is_dir():
                dump_dir = backup_path
            else:
                result = self._exec(
                    ["tar", "-xzf", str(backup_path), "-C", workdir],
                    timeout=TRANSFER_TIMEOUT,
                )
                if not result.success:
                    raise DatabaseBackupError(
                        "Failed to extract the backup",
                        details=result.stderr.strip() or f"{backup_path} is not a gzipped tar.",
                    )
                extracted = sorted(Path(workdir).iterdir())
                dump_dir = extracted[0] if extracted else Path(workdir)

            source = dump_dir / database if (dump_dir / database).is_dir() else dump_dir

            argv = ["mongorestore", "--db", database]
            if drop_existing:
                argv.append("--drop")
            if any(source.rglob("*.gz")):
                argv.append("--gzip")
            argv.append(str(source))

            result = self._exec(argv, timeout=TRANSFER_TIMEOUT)
            if not result.success:
                raise DatabaseBackupError(
                    f"Failed to restore database '{database}'",
                    details=result.stderr.strip() or "mongorestore reported no error text.",
                )

        self.logger.info(f"Restored database: {database} from {backup_path}")

    # ==================== Query Execution ====================

    def execute_query(
        self,
        database: str,
        query: str,
        **kwargs,
    ) -> tuple[bool, str]:
        """
        Run a JavaScript snippet against a database.

        Args:
            database: Database name.
            query: The snippet.
            **kwargs: Unused.

        Returns:
            Success and the snippet's output.

        Raises:
            DatabaseNotFoundError: When the database does not exist.
            DatabaseQueryError: When the snippet fails.
        """
        if not self.database_exists(database):
            raise DatabaseNotFoundError(f"Database '{database}' does not exist")

        success, output = self._execute_mongo(query, database=database)
        if not success:
            raise DatabaseQueryError("Query failed", details=output.strip())
        return success, output

    def get_connection_string(
        self,
        database: str,
        username: str,
        password: str,
        host: str = "localhost",
    ) -> str:
        """
        Build a MongoDB URI.

        Args:
            database: Database name.
            username: User name.
            password: Password.
            host: Host to connect to.

        Returns:
            The connection string.
        """
        return f"mongodb://{username}:{password}@{host}:{self.DEFAULT_PORT}/{database}"

    def get_interactive_command(
        self,
        database: str | None = None,
        username: str | None = None,
    ) -> list[str]:
        """
        Build the command that opens a shell session.

        Args:
            database: Database to connect to.
            username: User to connect as.

        Returns:
            The argument vector.

        Raises:
            DatabaseEngineError: When no MongoDB shell is installed.
        """
        argv = [self._shell()]
        if database:
            argv.append(database)
        if username:
            argv.extend(["--username", username])
        return argv


DatabaseRegistry.register(MongoDBManager, aliases=["mongo", "mongod"])
