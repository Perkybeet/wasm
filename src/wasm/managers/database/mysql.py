# Copyright (c) 2024-2025 Yago López Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
MySQL and MariaDB manager.

Two habits from the previous implementation are gone here:

- statements travel on stdin, so a ``CREATE USER ... IDENTIFIED BY`` no longer
  shows the password to every ``ps`` on the machine,
- dumps are streamed to disk by the runner instead of being pushed through
  ``bash -c "... | gzip > file"``.

Credentials reach the client through a 0600 option file, which is what MySQL
documents as the way to authenticate without a command line password.
"""

from __future__ import annotations

import hashlib
import os
import secrets as secrets_module
import tempfile
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path

from wasm.core.exceptions import (
    DatabaseBackupError,
    DatabaseError,
    DatabaseExistsError,
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
    StructuredQueryResult,
    UserInfo,
    format_size,
    parse_tabular_query_output,
    quote_identifier,
    validate_name,
    validate_path,
)
from wasm.managers.database.registry import DatabaseRegistry

#: Static privileges MySQL 8 and MariaDB accept in a GRANT. Anything outside
#: this set is rejected before a statement is built.
MYSQL_PRIVILEGES = frozenset(
    {
        "ALL",
        "ALL PRIVILEGES",
        "ALTER",
        "ALTER ROUTINE",
        "CREATE",
        "CREATE ROLE",
        "CREATE ROUTINE",
        "CREATE TABLESPACE",
        "CREATE TEMPORARY TABLES",
        "CREATE USER",
        "CREATE VIEW",
        "DELETE",
        "DROP",
        "DROP ROLE",
        "EVENT",
        "EXECUTE",
        "FILE",
        "GRANT OPTION",
        "INDEX",
        "INSERT",
        "LOCK TABLES",
        "PROCESS",
        "PROXY",
        "REFERENCES",
        "RELOAD",
        "REPLICATION CLIENT",
        "REPLICATION SLAVE",
        "SELECT",
        "SHOW DATABASES",
        "SHOW VIEW",
        "SHUTDOWN",
        "SUPER",
        "TRIGGER",
        "UPDATE",
        "USAGE",
    }
)


#: Escapes MySQL's option file reader turns back into characters. The manual
#: documents ``\b \t \n \r \\ \s``; the reader (mysys/my_default.cc) also
#: accepts ``\"`` and ``\'``, and its comment stripper skips a quote that
#: follows a backslash. Escaping both quote characters is therefore what keeps a
#: '#' inside a password from being taken for a comment and truncating it.
OPTION_FILE_ESCAPES = str.maketrans(
    {
        "\\": "\\\\",
        '"': '\\"',
        "'": "\\'",
        "\n": "\\n",
        "\r": "\\r",
        "\t": "\\t",
        "\b": "\\b",
        # A quoted value keeps its inner spaces, but the reader trims the raw
        # line first; \s survives both and costs nothing.
        " ": "\\s",
    }
)


def escape_option_file_value(value: str) -> str:
    """
    Render a value so a MySQL option file reads it back unchanged.

    An option file is parsed line by line, so a raw newline in a password does
    not corrupt the value: it ends the record and starts a new directive inside
    the ``[client]`` section. ``socket=`` or ``plugin-dir=`` placed there
    redirects every connection WASM makes afterwards. The escapes below are the
    ones the client applies after the line split, so a newline arrives as data.

    Args:
        value: The raw value, such as a password taken from the configuration.

    Returns:
        The value, escaped and enclosed in double quotes.

    Raises:
        DatabaseError: When the value contains a NUL byte, which the reader
            would silently truncate at rather than misparse.
    """
    if "\x00" in value:
        raise DatabaseError(
            "A MySQL credential contains a NUL byte",
            details=(
                "MySQL option files are read as C strings and would silently use only the "
                "part before the NUL. Change the credential in /etc/wasm/config.yaml."
            ),
        )
    return '"' + value.translate(OPTION_FILE_ESCAPES) + '"'


#: Prefix of the least-privilege account the read-only console connects as.
_READ_ONLY_USER_PREFIX = "wasm_ro_"

#: MySQL and MariaDB both cap a user name at 32 characters.
_USER_NAME_MAX_LENGTH = 32


def _read_only_user_name(database: str) -> str:
    """
    Build the deterministic, length-safe user name for a database's console.

    A name assembled by simple concatenation collides silently once it
    overflows the 32-character limit MySQL and MariaDB both enforce: two
    long, similarly prefixed database names could end up sharing one
    read-only account. A short hash of the full name keeps every account
    distinct even when it has to be shortened.

    Args:
        database: The database the account is scoped to.

    Returns:
        ``wasm_ro_<database>``, unchanged when it fits within the limit;
        otherwise truncated and suffixed with an 8-character digest of the
        full database name.
    """
    candidate = f"{_READ_ONLY_USER_PREFIX}{database}"
    if len(candidate) <= _USER_NAME_MAX_LENGTH:
        return candidate
    digest = hashlib.sha256(database.encode()).hexdigest()[:8]
    budget = _USER_NAME_MAX_LENGTH - len(_READ_ONLY_USER_PREFIX) - len(digest) - 1
    return f"{_READ_ONLY_USER_PREFIX}{database[:budget]}_{digest}"


class MySQLManager(BaseDatabaseManager):
    """Manager for MySQL and MariaDB, whichever is installed."""

    ENGINE_NAME = "mysql"
    DISPLAY_NAME = "MySQL/MariaDB"
    DEFAULT_PORT = 3306
    SERVICE_NAME = "mysql"
    PACKAGE_NAMES = ("mysql-server",)
    MARIADB_PACKAGES = ("mariadb-server",)
    CLIENT_BINARY = "mysql"
    VERSION_ARGV = ("mysql", "--version")
    PURGE_PATHS = ("/var/lib/mysql", "/etc/mysql")
    MAX_DATABASE_NAME_LENGTH = 64
    MAX_USER_NAME_LENGTH = 32
    VALID_PRIVILEGES = MYSQL_PRIVILEGES
    DEFAULT_PRIVILEGES = ("ALL PRIVILEGES",)
    SUPPORTS_STRUCTURED_QUERY = True

    #: Schemas that belong to the server, not to a user.
    SYSTEM_DATABASES = frozenset({"information_schema", "mysql", "performance_schema", "sys"})

    def __init__(self, verbose: bool = False):
        """
        Args:
            verbose: Enable verbose logging.
        """
        super().__init__(verbose=verbose)
        self._detect_variant()

    def _detect_variant(self) -> None:
        """Name the service after the flavour that is actually installed."""
        if self.runner.exists("mariadb") or self.runner.exists("mariadbd"):
            self.SERVICE_NAME = "mariadb"
            self.DISPLAY_NAME = "MariaDB"

    def _package_sets(self) -> tuple[list[str], ...]:
        """
        Prefer MariaDB, which is what current Debian and Ubuntu ship.

        Returns:
            The MariaDB packages first, the MySQL ones as a fallback.
        """
        return (list(self.MARIADB_PACKAGES), list(self.PACKAGE_NAMES))

    def _on_packages_installed(self, packages: Sequence[str]) -> None:
        """
        Record which flavour got installed.

        Args:
            packages: The package names that installed successfully.
        """
        if list(packages) == list(self.MARIADB_PACKAGES):
            self.SERVICE_NAME = "mariadb"
            self.DISPLAY_NAME = "MariaDB"

    def _post_install(self) -> None:
        """Drop the anonymous accounts and the test database a fresh install ships."""
        self._execute_sql("DELETE FROM mysql.user WHERE User='';")
        self._execute_sql("DROP DATABASE IF EXISTS test;")
        self._execute_sql("FLUSH PRIVILEGES;")

    # ==================== SQL text ====================

    @staticmethod
    def _escape_identifier(value: str) -> str:
        """
        Quote an identifier the way MySQL does.

        Args:
            value: Raw identifier.

        Returns:
            The identifier in backticks, with embedded backticks doubled.
        """
        return quote_identifier(value, "`")

    @staticmethod
    def _escape_literal(value: str) -> str:
        """
        Quote a string literal the way MySQL does.

        Args:
            value: Raw string.

        Returns:
            The value in single quotes, with backslashes and single quotes
            escaped, which covers both NO_BACKSLASH_ESCAPES settings.
        """
        return "'" + value.replace("\\", "\\\\").replace("'", "''") + "'"

    @classmethod
    def validate_host(cls, host: str) -> str:
        """
        Check a host restriction.

        Args:
            host: Host pattern such as ``localhost``, ``%`` or ``10.0.0.%``.

        Returns:
            The host, unchanged.

        Raises:
            DatabaseUserError: When the host contains anything but letters,
                digits, dot, dash, colon, underscore or the ``%`` wildcard.
        """
        if (
            not host
            or len(host) > 255
            or not set(host)
            <= set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.:-_%")
        ):
            raise DatabaseUserError(
                f"Invalid MySQL host: {host!r}",
                details="Use a host name, an address or a pattern such as 'localhost' or '10.0.0.%'.",
            )
        return host

    # ==================== Credentials ====================

    @contextmanager
    def _credentials(self) -> Iterator[list[str]]:
        """
        Provide the client arguments that authenticate, without a password in argv.

        MySQL reads credentials from an option file, which is the mechanism it
        documents for scripts; the file is created 0600 and removed afterwards.

        Yields:
            Arguments to place immediately after the program name.

        Raises:
            DatabaseError: When a credential cannot be written to an option file
                without changing its value.
        """
        credentials = self.config.get("databases", {}).get("credentials", {}).get("mysql", {})
        user = credentials.get("user")
        password = credentials.get("password")

        if not password:
            yield ["-u", user] if user else []
            return

        # The values are escaped before the file is created, so a credential
        # that cannot be represented fails without leaving a file behind.
        content = "[client]\n"
        if user:
            content += f"user={escape_option_file_value(str(user))}\n"
        content += f"password={escape_option_file_value(str(password))}\n"

        # mkstemp creates the file 0600, so the password is never briefly
        # world readable the way a write-then-chmod would leave it.
        fd, path = tempfile.mkstemp(prefix="wasm_mysql_", suffix=".cnf")
        try:
            with os.fdopen(fd, "w") as handle:
                handle.write(content)
            # This option is only honoured as the client's first argument.
            yield [f"--defaults-extra-file={path}"]
        finally:
            Path(path).unlink(missing_ok=True)

    def _ensure_read_only_user(self, database: str) -> tuple[str, str]:
        """
        Create, idempotently, the account the read-only console connects as.

        Closes ``SELECT LOAD_FILE('/etc/shadow')`` and
        ``SELECT ... INTO OUTFILE`` being reachable from a "read-only"
        console query: ``START TRANSACTION READ ONLY`` refuses a write to a
        table, but not a call to ``LOAD_FILE`` or a clause that writes to the
        filesystem instead of a table, and the account the write path
        connects as typically carries ``FILE`` (needed for restores) and
        often more. ``wasm_ro_<database>`` is a dedicated account, granted
        nothing but ``SELECT`` on this one database - no ``FILE``, no
        ``SUPER``, no ``PROCESS``, no access to any other schema - with a
        password rotated on every call and never persisted.
        ``REVOKE ALL PRIVILEGES, GRANT OPTION FROM`` is the one MySQL and
        MariaDB statement that succeeds even when the account already holds
        nothing, which is what keeps this idempotent and also keeps a
        privilege granted by an older version of this method from surviving
        an upgrade.

        Args:
            database: The database the account is scoped to.

        Returns:
            The user name and a freshly generated password.

        Raises:
            DatabaseQueryError: When the account cannot be provisioned.
        """
        username = _read_only_user_name(database)
        password = secrets_module.token_urlsafe(24)
        account = f"{self._escape_literal(username)}@{self._escape_literal('localhost')}"
        provision = (
            f"CREATE USER IF NOT EXISTS {account} IDENTIFIED BY "
            f"{self._escape_literal(password)};\n"
            f"ALTER USER {account} IDENTIFIED BY {self._escape_literal(password)};\n"
            f"REVOKE ALL PRIVILEGES, GRANT OPTION FROM {account};\n"
            f"GRANT SELECT ON {self._escape_identifier(database)}.* TO {account};\n"
            "FLUSH PRIVILEGES;\n"
        )
        success, output = self._execute_sql(provision, secrets=(password,))
        if not success:
            raise DatabaseQueryError(
                f"Could not provision the read-only console account for '{database}'",
                details=output.strip(),
            )
        return username, password

    @contextmanager
    def _read_only_credentials(self, database: str) -> Iterator[list[str]]:
        """
        Provision and hand back the read-only console's own credentials.

        Args:
            database: The database the account is scoped to.

        Yields:
            Arguments to place immediately after the program name, exactly
            like :meth:`_credentials`.

        Raises:
            DatabaseQueryError: When the account cannot be provisioned.
        """
        username, password = self._ensure_read_only_user(database)

        content = (
            "[client]\n"
            f"user={escape_option_file_value(username)}\n"
            f"password={escape_option_file_value(password)}\n"
        )
        fd, path = tempfile.mkstemp(prefix="wasm_mysql_ro_", suffix=".cnf")
        try:
            with os.fdopen(fd, "w") as handle:
                handle.write(content)
            yield [f"--defaults-extra-file={path}"]
        finally:
            Path(path).unlink(missing_ok=True)

    def _client_argv(
        self, credentials: Sequence[str], database: str | None = None, *, headers: bool = False
    ) -> list[str]:
        """
        Build a mysql invocation that prints machine readable output.

        Args:
            credentials: Arguments from :meth:`_credentials`.
            database: Database to select.
            headers: Keep the column name header row ``-B`` alone prints.
                Every caller except the structured console query drops it
                with ``-N``, because they parse a single value or a line at a
                time and a header would be just another line to skip.

        Returns:
            The argument vector.
        """
        argv = ["mysql", *credentials]
        if not headers:
            argv.append("-N")
        argv.append("-B")
        if database:
            argv.extend(["-D", database])
        return argv

    def _execute_sql(
        self,
        sql: str,
        database: str | None = None,
        *,
        secrets: Sequence[str] = (),
        timeout: int = QUERY_TIMEOUT,
    ) -> tuple[bool, str]:
        """
        Run SQL, passing the statement on stdin.

        Args:
            sql: The statement.
            database: Database to select.
            secrets: Values the statement carries that must not be logged.
            timeout: Deadline in seconds.

        Returns:
            Whether the client succeeded, and its output or its error text.
        """
        with self._credentials() as credentials:
            result = self._exec(
                self._client_argv(credentials, database),
                input=sql,
                timeout=timeout,
                secrets=secrets,
            )
        return result.success, result.stdout if result.success else result.stderr

    # ==================== Database Management ====================

    def create_database(
        self,
        name: str,
        owner: str | None = None,
        encoding: str | None = None,
        **kwargs,
    ) -> DatabaseInfo:
        """
        Create a database.

        Args:
            name: Database name.
            owner: User to grant privileges to once the database exists.
            encoding: Character set, utf8mb4 by default.
            **kwargs: Accepts ``collation``.

        Returns:
            Information about the new database.

        Raises:
            DatabaseExistsError: When the database already exists.
            DatabaseError: When a name is invalid or creation fails.
        """
        self.validate_database_name(name)
        if self.database_exists(name):
            raise DatabaseExistsError(
                f"Database '{name}' already exists",
                details="Drop it first, or pick another name.",
            )

        charset = validate_name(
            encoding or "utf8mb4", kind="character set", engine=self.DISPLAY_NAME, max_length=64
        )
        collation = validate_name(
            kwargs.get("collation", "utf8mb4_unicode_ci"),
            kind="collation",
            engine=self.DISPLAY_NAME,
            max_length=64,
        )

        success, output = self._execute_sql(
            f"CREATE DATABASE {self._escape_identifier(name)} "
            f"CHARACTER SET {charset} COLLATE {collation};"
        )
        if not success:
            raise DatabaseError(f"Failed to create database '{name}'", details=output.strip())

        if owner:
            self.grant_privileges(owner, name)

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
                details="Run 'wasm db list --engine mysql' to see the databases.",
            )

        success, output = self._execute_sql(f"DROP DATABASE {self._escape_identifier(name)};")
        if not success:
            raise DatabaseError(f"Failed to drop database '{name}'", details=output.strip())

        self.logger.info(f"Dropped database: {name}")

    def database_exists(self, name: str) -> bool:
        """
        Report whether a schema exists.

        Args:
            name: Database name.

        Returns:
            True when INFORMATION_SCHEMA holds the name.
        """
        success, output = self._execute_sql(
            "SELECT SCHEMA_NAME FROM INFORMATION_SCHEMA.SCHEMATA "  # noqa: S608 - quoted literal, not interpolated data
            f"WHERE SCHEMA_NAME = {self._escape_literal(name)};"
        )
        return success and output.strip() == name

    def list_databases(self) -> list[DatabaseInfo]:
        """
        List the schemas that do not belong to the server.

        Size comes from the same query, one row per schema rather than one
        query per database: ``INFORMATION_SCHEMA.TABLES`` is joined and
        summed per schema instead of :meth:`get_database_info`'s separate
        call, so a server with a hundred databases still costs one round
        trip.

        Owner is not filled: MySQL and MariaDB have no catalog concept of a
        database owner, only per-account grants, which :meth:`list_users`
        reports instead.

        Returns:
            One entry per user database.
        """
        success, output = self._execute_sql(
            "SELECT s.SCHEMA_NAME, s.DEFAULT_CHARACTER_SET_NAME, "
            "SUM(t.DATA_LENGTH + t.INDEX_LENGTH) "
            "FROM INFORMATION_SCHEMA.SCHEMATA s "
            "LEFT JOIN INFORMATION_SCHEMA.TABLES t ON t.TABLE_SCHEMA = s.SCHEMA_NAME "
            "GROUP BY s.SCHEMA_NAME, s.DEFAULT_CHARACTER_SET_NAME;"
        )
        if not success:
            return []

        databases = []
        for line in output.strip().splitlines():
            if not line:
                continue
            parts = line.split("\t")
            name = parts[0]
            if name in self.SYSTEM_DATABASES:
                continue
            size = None
            if len(parts) > 2 and parts[2].isdigit():
                size = format_size(int(parts[2]))
            databases.append(
                DatabaseInfo(
                    name=name,
                    engine=self.ENGINE_NAME,
                    encoding=parts[1] if len(parts) > 1 else None,
                    size=size,
                )
            )
        return databases

    def get_database_info(self, name: str) -> DatabaseInfo:
        """
        Describe one database.

        Args:
            name: Database name.

        Returns:
            Size, table count and character set.

        Raises:
            DatabaseNotFoundError: When the database does not exist.
        """
        if not self.database_exists(name):
            raise DatabaseNotFoundError(f"Database '{name}' does not exist")

        literal = self._escape_literal(name)
        success, output = self._execute_sql(
            "SELECT SUM(DATA_LENGTH + INDEX_LENGTH), COUNT(*) FROM INFORMATION_SCHEMA.TABLES "  # noqa: S608 - quoted literal, not interpolated data
            f"WHERE TABLE_SCHEMA = {literal};"
        )

        size: str | None = None
        tables = 0
        if success and output.strip():
            parts = output.strip().split("\t")
            if len(parts) >= 2:
                size = format_size(int(parts[0]) if parts[0].isdigit() else 0)
                tables = int(parts[1]) if parts[1].isdigit() else 0

        success, output = self._execute_sql(
            "SELECT DEFAULT_CHARACTER_SET_NAME FROM INFORMATION_SCHEMA.SCHEMATA "  # noqa: S608 - quoted literal, not interpolated data
            f"WHERE SCHEMA_NAME = {literal};"
        )

        return DatabaseInfo(
            name=name,
            engine=self.ENGINE_NAME,
            size=size,
            tables=tables,
            encoding=output.strip() if success else None,
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
        Create a user.

        Args:
            username: User name.
            password: Password. Generated when omitted.
            host: Host the user may connect from.
            **kwargs: Unused.

        Returns:
            The user and its password.

        Raises:
            DatabaseUserError: When the user exists or creation fails.
        """
        self.validate_user_name(username)
        self.validate_host(host)
        if self.user_exists(username, host):
            raise DatabaseUserError(
                f"User '{username}'@'{host}' already exists",
                details="Drop the user first, or pick another name.",
            )

        password = password or self.generate_password()

        success, output = self._execute_sql(
            f"CREATE USER {self._escape_literal(username)}@{self._escape_literal(host)} "
            f"IDENTIFIED BY {self._escape_literal(password)};",
            secrets=(password,),
        )
        if not success:
            raise DatabaseUserError(
                f"Failed to create user '{username}'@'{host}'", details=output.strip()
            )

        self._execute_sql("FLUSH PRIVILEGES;")
        self.logger.info(f"Created user: {username}@{host}")
        return UserInfo(username=username, engine=self.ENGINE_NAME, host=host), password

    def drop_user(self, username: str, host: str = "localhost") -> None:
        """
        Drop a user.

        Args:
            username: User name.
            host: Host the user connects from.

        Raises:
            DatabaseUserError: When the user is missing or the drop fails.
        """
        self.validate_user_name(username)
        self.validate_host(host)
        if not self.user_exists(username, host):
            raise DatabaseUserError(
                f"User '{username}'@'{host}' does not exist",
                details="Run 'wasm db users --engine mysql' to see the users.",
            )

        success, output = self._execute_sql(
            f"DROP USER {self._escape_literal(username)}@{self._escape_literal(host)};"
        )
        if not success:
            raise DatabaseUserError(
                f"Failed to drop user '{username}'@'{host}'", details=output.strip()
            )

        self._execute_sql("FLUSH PRIVILEGES;")
        self.logger.info(f"Dropped user: {username}@{host}")

    def user_exists(self, username: str, host: str = "localhost") -> bool:
        """
        Report whether a user exists.

        Args:
            username: User name.
            host: Host the user connects from.

        Returns:
            True when mysql.user holds the pair.
        """
        success, output = self._execute_sql(
            "SELECT User FROM mysql.user "  # noqa: S608 - quoted literals, not interpolated data
            f"WHERE User = {self._escape_literal(username)} "
            f"AND Host = {self._escape_literal(host)};"
        )
        return success and output.strip() == username

    def list_users(self) -> list[UserInfo]:
        """
        List the server's users.

        Databases come from the same query, one row per user rather than one
        query per user: ``mysql.db`` (database-level grants) is left-joined
        and aggregated with ``GROUP_CONCAT`` per account, so a server with a
        hundred users still costs one round trip. Table- and column-level
        grants (``mysql.tables_priv``, ``mysql.columns_priv``) are not
        included - they would need a second join per privilege scope for
        information this listing does not otherwise need, so
        :meth:`grant_privileges` remains the source of truth for exactly
        what an account can do.

        Returns:
            One entry per user and host pair, with the databases it has
            database-level grants on.
        """
        success, output = self._execute_sql(
            "SELECT u.User, u.Host, COALESCE(GROUP_CONCAT(DISTINCT d.Db SEPARATOR ','), '') "
            "FROM mysql.user u "
            "LEFT JOIN mysql.db d ON d.User = u.User AND d.Host = u.Host "
            "WHERE u.User != '' "
            "GROUP BY u.User, u.Host "
            "ORDER BY u.User;"
        )
        if not success:
            return []

        users = []
        for line in output.strip().splitlines():
            parts = line.split("\t")
            if len(parts) >= 2:
                databases = parts[2].split(",") if len(parts) > 2 and parts[2] else []
                users.append(
                    UserInfo(
                        username=parts[0],
                        engine=self.ENGINE_NAME,
                        host=parts[1],
                        databases=databases,
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
        Grant whitelisted privileges on a database to a user.

        Args:
            username: User name.
            database: Database name.
            privileges: Privileges to grant. ALL PRIVILEGES when omitted.
            host: Host the user connects from.

        Raises:
            DatabaseUserError: When a privilege is not whitelisted or the grant
                fails.
        """
        self.validate_user_name(username)
        self.validate_database_name(database)
        self.validate_host(host)
        granted = self.validate_privileges(privileges)

        success, output = self._execute_sql(
            f"GRANT {', '.join(granted)} ON {self._escape_identifier(database)}.* "
            f"TO {self._escape_literal(username)}@{self._escape_literal(host)};"
        )
        if not success:
            raise DatabaseUserError(
                f"Failed to grant privileges on '{database}' to '{username}'@'{host}'",
                details=output.strip(),
            )

        self._execute_sql("FLUSH PRIVILEGES;")
        self.logger.info(f"Granted {', '.join(granted)} on {database} to {username}@{host}")

    def revoke_privileges(
        self,
        username: str,
        database: str,
        privileges: Sequence[str] | None = None,
        host: str = "localhost",
    ) -> None:
        """
        Revoke whitelisted privileges on a database from a user.

        Args:
            username: User name.
            database: Database name.
            privileges: Privileges to revoke. ALL PRIVILEGES when omitted.
            host: Host the user connects from.

        Raises:
            DatabaseUserError: When a privilege is not whitelisted or the revoke
                fails.
        """
        self.validate_user_name(username)
        self.validate_database_name(database)
        self.validate_host(host)
        revoked = self.validate_privileges(privileges)

        success, output = self._execute_sql(
            f"REVOKE {', '.join(revoked)} ON {self._escape_identifier(database)}.* "
            f"FROM {self._escape_literal(username)}@{self._escape_literal(host)};"
        )
        if not success:
            raise DatabaseUserError(
                f"Failed to revoke privileges on '{database}' from '{username}'@'{host}'",
                details=output.strip(),
            )

        self._execute_sql("FLUSH PRIVILEGES;")
        self.logger.info(f"Revoked {', '.join(revoked)} on {database} from {username}@{host}")

    # ==================== Backup & Restore ====================

    def backup(
        self,
        database: str,
        output_path: Path | None = None,
        compress: bool = True,
        **kwargs,
    ) -> BackupInfo:
        """
        Dump a database with mysqldump.

        Args:
            database: Database name.
            output_path: Custom destination.
            compress: Pipe the dump through gzip.
            **kwargs: Unused.

        Returns:
            Information about the backup.

        Raises:
            DatabaseNotFoundError: When the database does not exist.
            DatabaseBackupError: When the dump fails.
        """
        self.validate_database_name(database)
        if not self.database_exists(database):
            raise DatabaseNotFoundError(f"Database '{database}' does not exist")

        destination = self._backup_path(database, output_path, compress)
        with self._credentials() as credentials:
            argv = [
                "mysqldump",
                *credentials,
                "--single-transaction",
                "--routines",
                "--triggers",
                database,
            ]
            return self._dump_to_file(argv, destination, database=database, compress=compress)

    def restore(
        self,
        database: str,
        backup_path: Path,
        drop_existing: bool = False,
        **kwargs,
    ) -> None:
        """
        Restore a database from a plain or gzipped dump.

        The client has no argv option for "read this file", so the staged dump is
        named in a ``source`` command sent on stdin. The path is checked against
        a conservative character set first, because that command is parsed by the
        client.

        Args:
            database: Target database name.
            backup_path: Path to the backup file.
            drop_existing: Drop and recreate the database first.
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
        if not self.database_exists(database):
            self.create_database(database)

        staged_name = f"{self.ENGINE_NAME}-restore-{database}{self.BACKUP_SUFFIX}"
        with self._staged_backup(backup_path, staged_name) as staged:
            validate_path(staged, purpose="a MySQL restore")
            with self._credentials() as credentials:
                result = self._exec(
                    self._client_argv(credentials, database),
                    input=f"source {staged}\n",
                    timeout=TRANSFER_TIMEOUT,
                )

        if not result.success:
            raise DatabaseBackupError(
                f"Failed to restore database '{database}'",
                details=result.stderr.strip() or "The dump may be truncated.",
            )

        self.logger.info(f"Restored database: {database} from {backup_path}")

    # ==================== Query Execution ====================

    def execute_query(
        self,
        database: str,
        query: str,
        *,
        read_only: bool = False,
        **kwargs,
    ) -> tuple[bool, str]:
        """
        Run an arbitrary statement against a database.

        Args:
            database: Database name.
            query: The statement.
            read_only: Refuse anything that would change data. The server
                enforces it, not a keyword allowlist, because a leading
                keyword does not tell you what a statement does. Enforced by
                connecting as a dedicated, least-privilege account: see
                :meth:`_ensure_read_only_user`.
            **kwargs: Unused.

        Returns:
            Success and the statement's output.

        Raises:
            DatabaseNotFoundError: When the database does not exist.
            DatabaseQueryError: When the statement fails, or the read-only
                account cannot be provisioned.
        """
        if not self.database_exists(database):
            raise DatabaseNotFoundError(f"Database '{database}' does not exist")

        if read_only:
            sql = f"START TRANSACTION READ ONLY;\n{query}\nCOMMIT;\n"
            with self._read_only_credentials(database) as credentials:
                result = self._exec(
                    self._client_argv(credentials, database),
                    input=sql,
                    timeout=QUERY_TIMEOUT,
                )
            success, output = result.success, result.stdout if result.success else result.stderr
        else:
            success, output = self._execute_sql(query, database=database)
        if not success:
            raise DatabaseQueryError("Query failed", details=output.strip())
        return success, output

    def execute_query_structured(
        self,
        database: str,
        query: str,
        *,
        read_only: bool = False,
        max_rows: int = 1000,
    ) -> StructuredQueryResult:
        """
        Run a statement once and parse its batch output into columns and rows.

        One execution, not two, for the reason :meth:`PostgresManager
        <wasm.managers.database.postgres.PostgresManager.execute_query_structured>`
        gives: a second run to also produce the older headerless format
        would apply a write statement twice.

        Args:
            database: Database name.
            query: The statement.
            read_only: Same enforcement as :meth:`execute_query`: a
                dedicated, least-privilege account provisioned by
                :meth:`_ensure_read_only_user`.
            max_rows: Data rows kept before the rest are dropped.

        Returns:
            The parsed result.

        Raises:
            DatabaseNotFoundError: When the database does not exist.
            DatabaseQueryError: When the statement fails, or the read-only
                account cannot be provisioned.
        """
        if not self.database_exists(database):
            raise DatabaseNotFoundError(f"Database '{database}' does not exist")

        if read_only:
            sql = f"START TRANSACTION READ ONLY;\n{query}\nCOMMIT;\n"
            with self._read_only_credentials(database) as credentials:
                result = self._exec(
                    self._client_argv(credentials, database, headers=True),
                    input=sql,
                    timeout=QUERY_TIMEOUT,
                )
        else:
            with self._credentials() as credentials:
                result = self._exec(
                    self._client_argv(credentials, database, headers=True),
                    input=query,
                    timeout=QUERY_TIMEOUT,
                )

        if not result.success:
            raise DatabaseQueryError(
                "Query failed", details=(result.stderr or result.stdout).strip()
            )

        columns, rows, truncated = parse_tabular_query_output(
            result.stdout, delimiter="\t", max_rows=max_rows
        )
        return StructuredQueryResult(
            output=result.stdout,
            columns=columns,
            rows=rows,
            row_count=len(rows),
            duration_ms=result.duration * 1000,
            truncated=truncated,
        )

    def get_connection_string(
        self,
        database: str,
        username: str,
        password: str,
        host: str = "localhost",
    ) -> str:
        """
        Build a MySQL URI.

        Args:
            database: Database name.
            username: User name.
            password: Password.
            host: Host to connect to.

        Returns:
            The connection string.
        """
        return f"mysql://{username}:{password}@{host}:{self.DEFAULT_PORT}/{database}"

    def get_interactive_command(
        self,
        database: str | None = None,
        username: str | None = None,
    ) -> list[str]:
        """
        Build the command that opens a mysql session.

        Args:
            database: Database to select.
            username: User to connect as.

        Returns:
            The argument vector. The client prompts for the password itself.
        """
        argv = ["mysql"]
        if username:
            argv.extend(["-u", username, "-p"])
        if database:
            argv.append(database)
        return argv


DatabaseRegistry.register(MySQLManager, aliases=["mariadb", "maria"])
