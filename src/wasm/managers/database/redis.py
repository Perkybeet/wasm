# Copyright (c) 2024-2025 Yago López Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
Redis manager.

Redis is a key-value store: its "databases" are numbered slots and its users are
ACL entries, so several operations that make sense elsewhere are refused here
with an explanation instead of being emulated.

Passwords never reach argv. ``ACL SETUSER`` receives the SHA-256 form Redis
documents for exactly this reason, ``requirepass`` is set over stdin, and the
client authenticates through ``REDISCLI_AUTH``.
"""

from __future__ import annotations

import hashlib
import re
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from wasm.core.exceptions import (
    DatabaseBackupError,
    DatabaseError,
    DatabaseNotFoundError,
    DatabaseQueryError,
    DatabaseUserError,
)
from wasm.managers.database.base import (
    QUERY_TIMEOUT,
    TRANSFER_TIMEOUT,
    BackupInfo,
    BaseDatabaseManager,
    DatabaseInfo,
    UserInfo,
)
from wasm.managers.database.registry import DatabaseRegistry

#: ACL rules that are bare keywords.
ACL_KEYWORDS = frozenset(
    {
        "allchannels",
        "allcommands",
        "allkeys",
        "clearselectors",
        "nocommands",
        "nokeys",
        "off",
        "on",
        "resetchannels",
        "resetkeys",
        "sanitize-payload",
        "skip-sanitize-payload",
    }
)

# Both rules end in \Z rather than '$': in Python '$' also matches before a
# final newline, so a '$'-anchored rule would accept "+@all\n" and hand a value
# with a line break to the server as if it were a bare keyword.

#: Command and category rules: ``+get``, ``-@admin``, ``+client|list``.
ACL_COMMAND_PATTERN = re.compile(r"\A[+-]@?[A-Za-z0-9_|-]+\Z")

#: Key and channel patterns: ``~*``, ``~cache:*``, ``&events.*``.
ACL_PATTERN_RULE = re.compile(r"\A(?:%(?:R|W|RW))?[~&][A-Za-z0-9_.:*?{}\[\]-]*\Z")

#: Default number of one-second polls spent waiting for persistence to finish.
PERSISTENCE_POLL_SECONDS = 60


class RedisManager(BaseDatabaseManager):
    """Manager for a Redis instance."""

    ENGINE_NAME = "redis"
    DISPLAY_NAME = "Redis"
    DEFAULT_PORT = 6379
    SERVICE_NAME = "redis-server"
    PACKAGE_NAMES = ("redis-server",)
    CLIENT_BINARY = "redis-cli"
    VERSION_ARGV = ("redis-server", "--version")
    VERSION_PATTERN = r"v=(\d+\.\d+\.\d+)"
    PURGE_PATHS = ("/var/lib/redis", "/etc/redis")
    BACKUP_SUFFIX = ".rdb"

    #: Where the server keeps its RDB and AOF files.
    DATA_DIR = Path("/var/lib/redis")
    #: Account the server runs as, and therefore the owner of its data files.
    DATA_OWNER = "redis"
    #: Number of database slots a default configuration exposes.
    DEFAULT_DATABASE_COUNT = 16
    #: One-second polls spent waiting for a save or an AOF rewrite to finish.
    PERSISTENCE_POLLS = PERSISTENCE_POLL_SECONDS

    def __init__(self, verbose: bool = False):
        """
        Args:
            verbose: Enable verbose logging.
        """
        super().__init__(verbose=verbose)
        self._password: str | None = None

    # ==================== Client ====================

    def _client_env(self) -> Mapping[str, str] | None:
        """
        Build the environment that authenticates the client.

        Returns:
            REDISCLI_AUTH carrying the password, or None when there is none.
            The password never goes in argv, where ``ps`` would show it.
        """
        return {"REDISCLI_AUTH": self._password} if self._password else None

    def _execute_redis(self, *args: str, db: int = 0) -> tuple[bool, str]:
        """
        Run a Redis command.

        Args:
            *args: Command and arguments, already free of secrets.
            db: Database slot to select.

        Returns:
            Whether the command succeeded, and its output or its error text.
        """
        result = self._exec(
            ["redis-cli", "-n", str(db), *args],
            env=self._client_env(),
            timeout=QUERY_TIMEOUT,
            secrets=(self._password,) if self._password else (),
        )
        output = result.stdout if result.success else result.stderr
        if result.success and result.stdout.lstrip().startswith(("(error)", "ERR ")):
            return False, result.stdout
        return result.success, output

    @staticmethod
    def _quote_for_stdin(value: str) -> str:
        """
        Render a value as a redis-cli string literal.

        Every byte becomes a hex escape, so nothing in a password can be read as
        quoting or as a separator by the client's argument parser.

        Args:
            value: The raw value.

        Returns:
            A double quoted, fully escaped literal.
        """
        return '"' + "".join(f"\\x{byte:02x}" for byte in value.encode()) + '"'

    def _execute_redis_with_secret(self, command: str, secret: str) -> tuple[bool, str]:
        """
        Run a command whose text carries a secret, over stdin.

        Args:
            command: The complete command line, secret already quoted.
            secret: The secret it carries, kept out of the logs.

        Returns:
            Whether the command succeeded, and its output.
        """
        result = self._exec(
            ["redis-cli"],
            input=f"{command}\n",
            env=self._client_env(),
            timeout=QUERY_TIMEOUT,
            secrets=(secret,),
        )
        if result.success and result.stdout.lstrip().startswith(("(error)", "ERR ")):
            return False, result.stdout
        return result.success, result.stdout if result.success else result.stderr

    # ==================== Validation ====================

    @classmethod
    def validate_privileges(cls, privileges: Sequence[str] | None) -> tuple[str, ...]:
        """
        Check ACL rules before they reach ``ACL SETUSER``.

        Redis rules are not SQL keywords, so they get their own grammar. Password
        rules (``>secret``, ``<secret``) are refused: a password must not travel
        through this path, where it would end up in argv.

        Args:
            privileges: Rules requested by the caller, or None for the default.

        Returns:
            The rules, unchanged and without repeats.

        Raises:
            DatabaseUserError: When a rule is not a recognised ACL rule.
        """
        requested = list(privileges) if privileges else ["+@all", "~*"]

        rules: list[str] = []
        for rule in requested:
            if not isinstance(rule, str) or not (
                rule in ACL_KEYWORDS
                or ACL_COMMAND_PATTERN.match(rule)
                or ACL_PATTERN_RULE.match(rule)
            ):
                raise DatabaseUserError(
                    f"Invalid Redis ACL rule: {rule!r}",
                    details=(
                        "Use rules such as '+@all', '-@admin', '+get', '~cache:*' or 'allkeys'. "
                        "Passwords are set with create_user, not with an ACL rule."
                    ),
                )
            if rule not in rules:
                rules.append(rule)
        return tuple(rules)

    @staticmethod
    def _database_number(name: str) -> int:
        """
        Parse a Redis database slot.

        Args:
            name: Slot number as text.

        Returns:
            The slot number.

        Raises:
            DatabaseError: When the value is not a number.
        """
        try:
            return int(name)
        except (TypeError, ValueError) as exc:
            raise DatabaseError(
                f"Invalid Redis database number: {name!r}",
                details="Redis databases are numbered; pass a number such as 0.",
            ) from exc

    def get_status(self) -> dict[str, Any]:
        """
        Summarise the instance, adding mode, clients and memory when running.

        Returns:
            A dictionary describing the instance.
        """
        status = super().get_status()
        if not status["running"]:
            return status

        success, output = self._execute_redis("INFO", "server")
        if success:
            for line in output.splitlines():
                if ":" not in line:
                    continue
                key, value = line.strip().split(":", 1)
                if key == "redis_mode":
                    status["mode"] = value
                elif key == "connected_clients" and value.isdigit():
                    status["clients"] = int(value)
                elif key == "used_memory_human":
                    status["memory"] = value
        return status

    # ==================== Database Management ====================

    def create_database(
        self,
        name: str,
        owner: str | None = None,
        encoding: str | None = None,
        **kwargs,
    ) -> DatabaseInfo:
        """
        Refuse to create a database.

        Args:
            name: Ignored.
            owner: Ignored.
            encoding: Ignored.
            **kwargs: Ignored.

        Returns:
            Never returns.

        Raises:
            DatabaseError: Always; Redis slots are fixed by configuration.
        """
        raise DatabaseError(
            "Redis uses numbered databases (0-15 by default)",
            details=(
                "Select a slot with SELECT <number>, or raise 'databases' in redis.conf to "
                "expose more."
            ),
        )

    def drop_database(self, name: str, force: bool = False) -> None:
        """
        Delete every key in a database slot.

        Args:
            name: Slot number.
            force: Ignored; a flush is unconditional.

        Raises:
            DatabaseError: When the slot is not a number or the flush fails.
        """
        db_number = self._database_number(name)
        success, output = self._execute_redis("FLUSHDB", db=db_number)
        if not success:
            raise DatabaseError(f"Failed to flush database {db_number}", details=output.strip())
        self.logger.info(f"Flushed database: {db_number}")

    def _database_count(self) -> int:
        """
        Read how many database slots the server exposes.

        Returns:
            The configured slot count, or the default when it cannot be read.
        """
        success, output = self._execute_redis("CONFIG", "GET", "databases")
        if success:
            parts = output.strip().splitlines()
            if len(parts) >= 2 and parts[1].strip().isdigit():
                return int(parts[1].strip())
        return self.DEFAULT_DATABASE_COUNT

    def database_exists(self, name: str) -> bool:
        """
        Report whether a slot number is within range.

        Args:
            name: Slot number.

        Returns:
            True when the slot exists.
        """
        try:
            db_number = int(name)
        except (TypeError, ValueError):
            return False
        return 0 <= db_number < self._database_count()

    def list_databases(self) -> list[DatabaseInfo]:
        """
        List the slots that hold keys, plus slot 0.

        Returns:
            One entry per listed slot, with its key count.
        """
        keyspace: dict[int, int] = {}
        success, output = self._execute_redis("INFO", "keyspace")
        if success:
            for line in output.splitlines():
                match = re.match(r"db(\d+):keys=(\d+)", line)
                if match:
                    keyspace[int(match.group(1))] = int(match.group(2))

        databases = []
        for db_number in range(self._database_count()):
            keys = keyspace.get(db_number, 0)
            if keys > 0 or db_number == 0:
                databases.append(
                    DatabaseInfo(
                        name=str(db_number),
                        engine=self.ENGINE_NAME,
                        tables=keys,
                        extra={"keys": keys},
                    )
                )
        return databases

    def get_database_info(self, name: str) -> DatabaseInfo:
        """
        Describe one slot.

        Args:
            name: Slot number.

        Returns:
            Key count and the instance's memory usage.

        Raises:
            DatabaseNotFoundError: When the slot is out of range.
        """
        db_number = self._database_number(name)
        if not self.database_exists(name):
            raise DatabaseNotFoundError(
                f"Database {db_number} does not exist",
                details="Raise 'databases' in redis.conf to expose more slots.",
            )

        keys = 0
        success, output = self._execute_redis("DBSIZE", db=db_number)
        if success:
            match = re.search(r"(\d+)", output)
            if match:
                keys = int(match.group(1))

        memory = None
        success, output = self._execute_redis("INFO", "memory")
        if success:
            for line in output.splitlines():
                if line.startswith("used_memory_human:"):
                    memory = line.split(":", 1)[1].strip()
                    break

        return DatabaseInfo(
            name=str(db_number),
            engine=self.ENGINE_NAME,
            size=memory,
            tables=keys,
            extra={"keys": keys},
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
        Create an ACL user (Redis 6 and later).

        The password is stored as the SHA-256 digest Redis accepts with the ``#``
        prefix, so the plain text never appears in a command line.

        Args:
            username: User name.
            password: Password. Generated when omitted.
            host: Recorded for the caller; Redis has no per-host users.
            **kwargs: Accepts ``permissions``, a list of ACL rules.

        Returns:
            The user and its password.

        Raises:
            DatabaseUserError: When the user exists or the server refuses the ACL.
        """
        self.validate_user_name(username)
        if self.user_exists(username):
            raise DatabaseUserError(
                f"User '{username}' already exists",
                details="Delete the user first, or pick another name.",
            )

        password = password or self.generate_password()
        rules = self.validate_privileges(kwargs.get("permissions"))
        digest = hashlib.sha256(password.encode()).hexdigest()

        success, output = self._execute_redis(
            "ACL", "SETUSER", username, "on", f"#{digest}", *rules
        )
        if not success:
            if "unknown command" in output.lower():
                raise DatabaseUserError(
                    "This Redis server has no ACL support",
                    details="ACL users require Redis 6.0 or later.",
                )
            raise DatabaseUserError(f"Failed to create user '{username}'", details=output.strip())

        self.logger.info(f"Created user: {username}")
        user = UserInfo(
            username=username,
            engine=self.ENGINE_NAME,
            host=host,
            privileges=list(rules),
        )
        return user, password

    def drop_user(self, username: str, host: str = "localhost") -> None:
        """
        Delete an ACL user.

        Args:
            username: User name.
            host: Ignored; Redis has no per-host users.

        Raises:
            DatabaseUserError: When the user is missing or the deletion fails.
        """
        self.validate_user_name(username)
        if not self.user_exists(username):
            raise DatabaseUserError(
                f"User '{username}' does not exist",
                details="Run 'wasm db users --engine redis' to see the ACL users.",
            )

        success, output = self._execute_redis("ACL", "DELUSER", username)
        if not success:
            raise DatabaseUserError(f"Failed to delete user '{username}'", details=output.strip())

        self.logger.info(f"Deleted user: {username}")

    def user_exists(self, username: str, host: str = "localhost") -> bool:
        """
        Report whether an ACL user exists.

        Args:
            username: User name.
            host: Ignored; Redis has no per-host users.

        Returns:
            True when ACL LIST mentions the user.
        """
        success, output = self._execute_redis("ACL", "LIST")
        if not success:
            return False
        return any(line.startswith(f"user {username} ") for line in output.splitlines())

    def list_users(self) -> list[UserInfo]:
        """
        List the ACL users.

        Returns:
            One entry per user, with its rules.
        """
        success, output = self._execute_redis("ACL", "LIST")
        if not success:
            return []

        users = []
        for line in output.splitlines():
            if not line.startswith("user "):
                continue
            parts = line.split()
            if len(parts) >= 2:
                users.append(
                    UserInfo(
                        username=parts[1],
                        engine=self.ENGINE_NAME,
                        privileges=parts[2:],
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
        Add ACL rules to a user.

        Args:
            username: User name.
            database: Ignored; Redis ACLs are instance wide.
            privileges: ACL rules. Full access when omitted.
            host: Ignored; Redis has no per-host users.

        Raises:
            DatabaseUserError: When a rule is invalid or the server refuses it.
        """
        self.validate_user_name(username)
        if not self.user_exists(username):
            raise DatabaseUserError(
                f"User '{username}' does not exist",
                details="Create the user before granting rules to it.",
            )

        rules = self.validate_privileges(privileges)
        success, output = self._execute_redis("ACL", "SETUSER", username, *rules)
        if not success:
            raise DatabaseUserError(
                f"Failed to grant privileges to '{username}'", details=output.strip()
            )

        self.logger.info(f"Granted {' '.join(rules)} to {username}")

    def revoke_privileges(
        self,
        username: str,
        database: str,
        privileges: Sequence[str] | None = None,
        host: str = "localhost",
    ) -> None:
        """
        Take every command and key away from a user.

        Args:
            username: User name.
            database: Ignored; Redis ACLs are instance wide.
            privileges: Ignored; Redis revokes by resetting the rules.
            host: Ignored; Redis has no per-host users.

        Raises:
            DatabaseUserError: When the user is missing or the server refuses.
        """
        self.validate_user_name(username)
        if not self.user_exists(username):
            raise DatabaseUserError(
                f"User '{username}' does not exist",
                details="Run 'wasm db users --engine redis' to see the ACL users.",
            )

        success, output = self._execute_redis("ACL", "SETUSER", username, "nocommands", "resetkeys")
        if not success:
            raise DatabaseUserError(
                f"Failed to revoke privileges from '{username}'", details=output.strip()
            )

        self.logger.info(f"Revoked all rules from {username}")

    # ==================== Backup & Restore ====================

    def backup(
        self,
        database: str,
        output_path: Path | None = None,
        compress: bool = True,
        **kwargs,
    ) -> BackupInfo:
        """
        Copy the instance's RDB snapshot, or its AOF file.

        Redis persists the whole instance, so the database argument is only used
        to label the backup.

        Args:
            database: Ignored; kept for interface compatibility.
            output_path: Custom destination.
            compress: Pipe the file through gzip.
            **kwargs: Accepts ``method``: ``rdb`` (default) or ``aof``.

        Returns:
            Information about the backup.

        Raises:
            DatabaseBackupError: When the save or the copy fails.
        """
        if kwargs.get("method") == "aof":
            return self.backup_aof(output_path=output_path, compress=compress)

        success, output = self._execute_redis("BGSAVE")
        if not success:
            success, output = self._execute_redis("SAVE")
            if not success:
                raise DatabaseBackupError(
                    "Failed to save the Redis dataset",
                    details=output.strip() or "Check that the server can write to its data dir.",
                )
        self._wait_for("rdb_bgsave_in_progress:0")

        destination = self._backup_path(database, output_path, compress, label="dump")
        return self._dump_to_file(
            ["cat", str(self.DATA_DIR / "dump.rdb")],
            destination,
            database="all",
            compress=compress,
        )

    def backup_aof(
        self,
        output_path: Path | None = None,
        compress: bool = True,
    ) -> BackupInfo:
        """
        Rewrite the append-only file and copy the result.

        Args:
            output_path: Custom destination.
            compress: Pipe the file through gzip.

        Returns:
            Information about the backup.

        Raises:
            DatabaseBackupError: When the rewrite or the copy fails.
        """
        success, output = self._execute_redis("BGREWRITEAOF")
        if not success:
            raise DatabaseBackupError(
                "Failed to trigger an AOF rewrite",
                details=output.strip() or "Enable appendonly in redis.conf first.",
            )
        self._wait_for("aof_rewrite_in_progress:0")

        destination = self._backup_path("all", output_path, compress, label="aof", suffix=".aof")
        return self._dump_to_file(
            ["cat", str(self._aof_path())],
            destination,
            database="all",
            compress=compress,
        )

    def _wait_for(self, marker: str) -> None:
        """
        Wait until a persistence counter reports the operation has finished.

        Args:
            marker: The INFO persistence line that means "done".
        """
        for _ in range(self.PERSISTENCE_POLLS):
            success, output = self._execute_redis("INFO", "persistence")
            if success and marker in output:
                return
            time.sleep(1)
        self.logger.warning(f"Timed out waiting for Redis persistence marker {marker}")

    def _aof_path(self) -> Path:
        """
        Locate the append-only file, honouring the Redis 7 directory layout.

        Returns:
            The path of the AOF file to copy.
        """
        success, output = self._execute_redis("CONFIG", "GET", "appendfilename")
        parts = output.strip().splitlines() if success else []
        filename = parts[1].strip() if len(parts) >= 2 and parts[1].strip() else "appendonly.aof"

        success, output = self._execute_redis("CONFIG", "GET", "appenddirname")
        dir_parts = output.strip().splitlines() if success else []
        if len(dir_parts) >= 2 and dir_parts[1].strip():
            candidate = self.DATA_DIR / dir_parts[1].strip() / filename
            if candidate.exists():
                return candidate
        return self.DATA_DIR / filename

    def restore(
        self,
        database: str,
        backup_path: Path,
        drop_existing: bool = False,
        **kwargs,
    ) -> None:
        """
        Replace the instance's RDB snapshot with a backup and restart.

        Args:
            database: Ignored; a Redis snapshot covers the whole instance.
            backup_path: Path to the backup file, plain or gzipped.
            drop_existing: Ignored; the snapshot replaces everything.
            **kwargs: Unused.

        Raises:
            DatabaseBackupError: When the file is missing or cannot be installed.
        """
        backup_path = Path(backup_path)
        if not backup_path.exists():
            raise DatabaseBackupError(
                f"Backup file not found: {backup_path}",
                details="Run 'wasm db backups' to list the backups WASM knows about.",
            )

        rdb_file = self.DATA_DIR / "dump.rdb"
        self.stop()
        try:
            if backup_path.suffix == ".gz":
                result = self.runner.capture_to_file(
                    ["gzip", "-dc", str(backup_path)],
                    rdb_file,
                    timeout=TRANSFER_TIMEOUT,
                )
            else:
                result = self._exec(
                    ["cp", str(backup_path), str(rdb_file)], timeout=TRANSFER_TIMEOUT
                )
            if not result.success:
                raise DatabaseBackupError(
                    "Failed to install the backup file",
                    details=result.stderr.strip() or f"Could not write {rdb_file}.",
                )
            self._exec(["chown", f"{self.DATA_OWNER}:{self.DATA_OWNER}", str(rdb_file)])
            self._exec(["chmod", "660", str(rdb_file)])
        finally:
            self.start()

        self.logger.info(f"Restored Redis from: {backup_path}")

    # ==================== Query Execution ====================

    def execute_query(
        self,
        database: str,
        query: str,
        **kwargs,
    ) -> tuple[bool, str]:
        """
        Run a Redis command given as text.

        Args:
            database: Slot number.
            query: The command, split on whitespace.
            **kwargs: Unused.

        Returns:
            Success and the command's output.

        Raises:
            DatabaseQueryError: When the command is empty or fails.
        """
        try:
            db_number = int(database)
        except (TypeError, ValueError):
            db_number = 0

        parts = query.split()
        if not parts:
            raise DatabaseQueryError(
                "Empty Redis command", details="Pass a command such as 'INFO server'."
            )

        success, output = self._execute_redis(*parts, db=db_number)
        if not success:
            raise DatabaseQueryError("Command failed", details=output.strip())
        return success, output

    def get_connection_string(
        self,
        database: str,
        username: str,
        password: str,
        host: str = "localhost",
    ) -> str:
        """
        Build a Redis URI.

        Args:
            database: Slot number.
            username: ACL user, or ``default``.
            password: Password.
            host: Host to connect to.

        Returns:
            The connection string.
        """
        try:
            db_number = int(database)
        except (TypeError, ValueError):
            db_number = 0

        if username and username != "default":
            return f"redis://{username}:{password}@{host}:{self.DEFAULT_PORT}/{db_number}"
        return f"redis://:{password}@{host}:{self.DEFAULT_PORT}/{db_number}"

    def get_interactive_command(
        self,
        database: str | None = None,
        username: str | None = None,
    ) -> list[str]:
        """
        Build the command that opens a redis-cli session.

        Args:
            database: Slot to select.
            username: ACL user to connect as.

        Returns:
            The argument vector.
        """
        argv = ["redis-cli"]
        if database is not None and str(database).isdigit():
            argv.extend(["-n", str(int(database))])
        if username:
            argv.extend(["--user", username])
        return argv

    # ==================== Redis-specific ====================

    def set_password(self, password: str) -> None:
        """
        Set ``requirepass`` and persist it to the configuration file.

        The password is sent on stdin, hex escaped, so it appears neither in argv
        nor in the client's own parsing edge cases.

        Args:
            password: The new password.

        Raises:
            DatabaseError: When the server refuses the change.
        """
        success, output = self._execute_redis_with_secret(
            f"CONFIG SET requirepass {self._quote_for_stdin(password)}", password
        )
        if not success:
            raise DatabaseError("Failed to set the Redis password", details=output.strip())

        self._password = password
        self._execute_redis("CONFIG", "REWRITE")

    def get_memory_stats(self) -> dict[str, str]:
        """
        Read the memory section of INFO.

        Returns:
            The section as key-value pairs.
        """
        success, output = self._execute_redis("INFO", "memory")
        if not success:
            return {}
        stats = {}
        for line in output.splitlines():
            if ":" in line:
                key, value = line.strip().split(":", 1)
                stats[key] = value
        return stats

    def flush_all(self) -> None:
        """
        Delete every key in every slot.

        Raises:
            DatabaseError: When the server refuses.
        """
        success, output = self._execute_redis("FLUSHALL")
        if not success:
            raise DatabaseError("Failed to flush all databases", details=output.strip())


DatabaseRegistry.register(RedisManager, aliases=["redis-server"])
