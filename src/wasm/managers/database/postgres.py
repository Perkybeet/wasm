# Copyright (c) 2024-2025 Yago López Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
PostgreSQL manager.

Statements WASM builds are fed to ``psql`` on stdin, never with ``-c``: a
CREATE ROLE carries a password, and everything in argv is visible in ``ps``.
The console's statement is the one exception, and goes the other way for a
reason: psql reads stdin as a script and runs its meta-commands (``\\!`` is a
shell) wherever they appear, while a ``-c`` string is sent to the server as it
is. See :meth:`PostgresManager._console_argv`. Dumps are streamed to disk by
the runner, so a dump containing quotes or binary bytes arrives intact.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from pathlib import Path

from wasm.core.exceptions import (
    DatabaseBackupError,
    DatabaseError,
    DatabaseExistsError,
    DatabaseNotFoundError,
    DatabaseQueryError,
    DatabaseUserError,
)
from wasm.core.runner import runuser_prefix
from wasm.managers.database.base import (
    QUERY_TIMEOUT,
    TRANSFER_TIMEOUT,
    BackupInfo,
    BaseDatabaseManager,
    DatabaseInfo,
    StructuredQueryResult,
    UserInfo,
    console_statement,
    format_size,
    parse_tabular_query_output,
    quote_identifier,
    validate_name,
)
from wasm.managers.database.registry import DatabaseRegistry

#: Privileges PostgreSQL accepts on a DATABASE object.
DATABASE_PRIVILEGES = frozenset(
    {
        "ALL",
        "ALL PRIVILEGES",
        "CONNECT",
        "CREATE",
        "TEMP",
        "TEMPORARY",
    }
)

#: Privileges PostgreSQL accepts on a TABLE object.
TABLE_PRIVILEGES = frozenset(
    {
        "ALL",
        "ALL PRIVILEGES",
        "DELETE",
        "INSERT",
        "MAINTAIN",
        "REFERENCES",
        "SELECT",
        "TRIGGER",
        "TRUNCATE",
        "UPDATE",
    }
)

#: pg_dump output formats that can be streamed to stdout. "directory" cannot.
DUMP_FORMATS = frozenset({"plain", "custom", "tar"})

#: Prefix of the least-privilege role the read-only console connects as.
_READ_ONLY_ROLE_PREFIX = "wasm_ro_"

#: PostgreSQL's NAMEDATALEN limit for an identifier, minus the terminator.
_IDENTIFIER_MAX_LENGTH = 63


def _read_only_role_name(database: str) -> str:
    """
    Build the deterministic, length-safe role name for a database's console.

    A role assembled by simple concatenation collides silently once it
    overflows PostgreSQL's 63-character identifier limit: the server accepts
    the CREATE ROLE statement and truncates, so two long, similarly prefixed
    database names could end up sharing one read-only role without either
    creation failing. A short hash of the full name keeps every role
    distinct even when it has to be shortened.

    Args:
        database: The database the role is scoped to.

    Returns:
        ``wasm_ro_<database>``, unchanged when it fits within the limit;
        otherwise truncated and suffixed with an 8-character digest of the
        full database name.
    """
    candidate = f"{_READ_ONLY_ROLE_PREFIX}{database}"
    if len(candidate) <= _IDENTIFIER_MAX_LENGTH:
        return candidate
    digest = hashlib.sha256(database.encode()).hexdigest()[:8]
    budget = _IDENTIFIER_MAX_LENGTH - len(_READ_ONLY_ROLE_PREFIX) - len(digest) - 1
    return f"{_READ_ONLY_ROLE_PREFIX}{database[:budget]}_{digest}"


class PostgresManager(BaseDatabaseManager):
    """Manager for PostgreSQL databases."""

    ENGINE_NAME = "postgresql"
    DISPLAY_NAME = "PostgreSQL"
    DEFAULT_PORT = 5432
    SERVICE_NAME = "postgresql"
    PACKAGE_NAMES = ("postgresql", "postgresql-contrib")
    CLIENT_BINARY = "psql"
    VERSION_ARGV = ("psql", "--version")
    VERSION_PATTERN = r"(\d+\.\d+)"
    PURGE_PATHS = ("/var/lib/postgresql", "/etc/postgresql")
    MAX_DATABASE_NAME_LENGTH = 63
    MAX_USER_NAME_LENGTH = 63
    VALID_PRIVILEGES = DATABASE_PRIVILEGES | TABLE_PRIVILEGES
    DEFAULT_PRIVILEGES = ("ALL PRIVILEGES",)
    SUPPORTS_STRUCTURED_QUERY = True

    #: The account that owns the cluster and can authenticate by peer.
    SUPERUSER = "postgres"

    #: Databases that belong to the cluster, not to a user.
    SYSTEM_DATABASES = frozenset({"postgres", "template0", "template1"})

    # ==================== SQL text ====================

    @staticmethod
    def _escape_identifier(value: str) -> str:
        """
        Quote an identifier the way PostgreSQL does.

        Args:
            value: Raw identifier.

        Returns:
            The identifier in double quotes, with embedded double quotes doubled.
        """
        return quote_identifier(value, '"')

    @staticmethod
    def _escape_literal(value: str) -> str:
        """
        Quote a string literal the way PostgreSQL does.

        Args:
            value: Raw string.

        Returns:
            The value in single quotes, with embedded single quotes doubled.
        """
        return "'" + value.replace("'", "''") + "'"

    def _psql_argv(self, database: str, *tail: str) -> list[str]:
        """
        Build a psql invocation that fails loudly and prints only data.

        Runs as :data:`SUPERUSER`, applied by the caller through ``_exec``'s
        ``user=`` rather than baked in here: PostgreSQL's peer authentication
        only accepts a connection from the OS account of the same name, and
        WASM runs as root, not ``postgres``.

        Args:
            database: Database to connect to.
            *tail: Arguments describing where the SQL comes from.

        Returns:
            The argument vector.
        """
        return [
            "psql",
            "-v",
            "ON_ERROR_STOP=1",
            "-d",
            database,
            "-t",
            "-A",
            # No psqlrc: a startup file is a script psql would run first.
            "-X",
            *tail,
        ]

    def _execute_sql(
        self,
        sql: str,
        database: str = "postgres",
        *,
        secrets: Sequence[str] = (),
        timeout: int = QUERY_TIMEOUT,
        env: Mapping[str, str] | None = None,
    ) -> tuple[bool, str]:
        """
        Run SQL as the cluster superuser, passing the statement on stdin.

        Args:
            sql: The statement.
            database: Database to connect to.
            secrets: Values the statement carries that must not be logged.
            timeout: Deadline in seconds.
            env: Extra environment for psql, such as PGOPTIONS.

        Returns:
            Whether psql succeeded, and its output or its error text.
        """
        result = self._exec(
            self._psql_argv(database, "-f", "-"),
            input=sql,
            timeout=timeout,
            secrets=secrets,
            env=env,
            user=self.SUPERUSER,
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
            owner: Role that owns the database.
            encoding: Character encoding, UTF8 by default.
            **kwargs: Accepts ``template``.

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

        encoding = encoding or "UTF8"
        template = kwargs.get("template", "template0")

        sql = (
            f"CREATE DATABASE {self._escape_identifier(name)} "
            f"ENCODING {self._escape_literal(encoding)} "
            f"TEMPLATE {self._escape_identifier(template)}"
        )
        if owner:
            self.validate_user_name(owner)
            sql += f" OWNER {self._escape_identifier(owner)}"
        sql += ";"

        success, output = self._execute_sql(sql)
        if not success:
            raise DatabaseError(f"Failed to create database '{name}'", details=output.strip())

        self.logger.info(f"Created database: {name}")
        return self.get_database_info(name)

    def drop_database(self, name: str, force: bool = False) -> None:
        """
        Drop a database.

        Args:
            name: Database name.
            force: Terminate open connections first, and accept a missing
                database as success.

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
                details="Run 'wasm db list --engine postgresql' to see the databases.",
            )

        if force:
            self._execute_sql(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "  # noqa: S608 - quoted literal, not interpolated data
                f"WHERE datname = {self._escape_literal(name)};"
            )

        success, output = self._execute_sql(f"DROP DATABASE {self._escape_identifier(name)};")
        if not success:
            raise DatabaseError(
                f"Failed to drop database '{name}'",
                details=output.strip() or "Retry with --force to close open connections.",
            )

        self.logger.info(f"Dropped database: {name}")

    def database_exists(self, name: str) -> bool:
        """
        Report whether a database exists.

        Args:
            name: Database name.

        Returns:
            True when pg_database holds the name.
        """
        success, output = self._execute_sql(
            f"SELECT 1 FROM pg_database WHERE datname = {self._escape_literal(name)};"  # noqa: S608 - quoted literal, not interpolated data
        )
        return success and output.strip() == "1"

    def list_databases(self) -> list[DatabaseInfo]:
        """
        List the databases that do not belong to the cluster itself.

        Owner comes from the same round trip as size and encoding - the
        ``pg_roles`` join :meth:`get_database_info` already uses for one
        database, applied here to every row instead of a call per database.

        Returns:
            One entry per user database.
        """
        success, output = self._execute_sql(
            "SELECT datname, pg_encoding_to_char(encoding), pg_database_size(datname), r.rolname "
            "FROM pg_database d JOIN pg_roles r ON d.datdba = r.oid "
            "WHERE datistemplate = false;"
        )
        if not success:
            return []

        databases = []
        for line in output.strip().splitlines():
            if not line:
                continue
            parts = line.split("|")
            name = parts[0]
            if name in self.SYSTEM_DATABASES:
                continue

            size = None
            if len(parts) >= 3 and parts[2].isdigit():
                size = format_size(int(parts[2]))

            databases.append(
                DatabaseInfo(
                    name=name,
                    engine=self.ENGINE_NAME,
                    encoding=parts[1] if len(parts) > 1 else None,
                    size=size,
                    owner=parts[3] if len(parts) > 3 else None,
                )
            )
        return databases

    def get_database_info(self, name: str) -> DatabaseInfo:
        """
        Describe one database.

        Args:
            name: Database name.

        Returns:
            Size, owner, encoding and table count.

        Raises:
            DatabaseNotFoundError: When the database does not exist.
        """
        if not self.database_exists(name):
            raise DatabaseNotFoundError(f"Database '{name}' does not exist")

        literal = self._escape_literal(name)
        success, output = self._execute_sql(
            "SELECT datname, pg_encoding_to_char(encoding), pg_database_size(datname), r.rolname "  # noqa: S608 - quoted literal, not interpolated data
            "FROM pg_database d JOIN pg_roles r ON d.datdba = r.oid "
            f"WHERE datname = {literal};"
        )

        encoding: str | None = None
        size: str | None = None
        owner: str | None = None
        if success and output.strip():
            parts = output.strip().split("|")
            if len(parts) >= 4:
                encoding = parts[1]
                if parts[2].isdigit():
                    size = format_size(int(parts[2]))
                owner = parts[3]

        success, output = self._execute_sql(
            "SELECT COUNT(*) FROM information_schema.tables "  # noqa: S608 - quoted literal, not interpolated data
            f"WHERE table_schema = 'public' AND table_catalog = {literal};",
            database=name,
        )
        tables = int(output.strip()) if success and output.strip().isdigit() else 0

        return DatabaseInfo(
            name=name,
            engine=self.ENGINE_NAME,
            size=size,
            tables=tables,
            owner=owner,
            encoding=encoding,
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
        Create a login role.

        Args:
            username: Role name.
            password: Password. Generated when omitted.
            host: Ignored; PostgreSQL restricts hosts in pg_hba.conf.
            **kwargs: Accepts ``superuser``, ``createdb`` and ``createrole``.

        Returns:
            The role and its password.

        Raises:
            DatabaseUserError: When the role exists or creation fails.
        """
        self.validate_user_name(username)
        if self.user_exists(username):
            raise DatabaseUserError(
                f"User '{username}' already exists",
                details="Drop the role first, or pick another name.",
            )

        password = password or self.generate_password()

        options = ["LOGIN"]
        if kwargs.get("superuser"):
            options.append("SUPERUSER")
        if kwargs.get("createdb"):
            options.append("CREATEDB")
        if kwargs.get("createrole"):
            options.append("CREATEROLE")

        sql = (
            f"CREATE ROLE {self._escape_identifier(username)} WITH {' '.join(options)} "
            f"PASSWORD {self._escape_literal(password)};"
        )
        success, output = self._execute_sql(sql, secrets=(password,))
        if not success:
            raise DatabaseUserError(f"Failed to create user '{username}'", details=output.strip())

        self.logger.info(f"Created user: {username}")
        return UserInfo(username=username, engine=self.ENGINE_NAME, host=host), password

    def drop_user(self, username: str, host: str = "localhost") -> None:
        """
        Drop a role.

        Args:
            username: Role name.
            host: Ignored; PostgreSQL restricts hosts in pg_hba.conf.

        Raises:
            DatabaseUserError: When the role is missing or still owns objects.
        """
        self.validate_user_name(username)
        if not self.user_exists(username):
            raise DatabaseUserError(
                f"User '{username}' does not exist",
                details="Run 'wasm db users --engine postgresql' to see the roles.",
            )

        success, output = self._execute_sql(f"DROP ROLE {self._escape_identifier(username)};")
        if not success:
            raise DatabaseUserError(
                f"Failed to drop user '{username}'",
                details=(
                    output.strip()
                    or "The role may still own objects; reassign them with REASSIGN OWNED."
                ),
            )

        self.logger.info(f"Dropped user: {username}")

    def user_exists(self, username: str, host: str = "localhost") -> bool:
        """
        Report whether a role exists.

        Args:
            username: Role name.
            host: Ignored; PostgreSQL restricts hosts in pg_hba.conf.

        Returns:
            True when pg_roles holds the name.
        """
        success, output = self._execute_sql(
            f"SELECT 1 FROM pg_roles WHERE rolname = {self._escape_literal(username)};"  # noqa: S608 - quoted literal, not interpolated data
        )
        return success and output.strip() == "1"

    def list_users(self) -> list[UserInfo]:
        """
        List the roles that are not internal to PostgreSQL.

        Which databases a role can connect to comes from the same query, one
        row per role rather than one query per role: ``has_database_privilege``
        is evaluated for every role/database pair and folded into a single
        comma-separated column with ``array_agg`` ... ``FILTER``, so a cluster
        with a hundred roles still costs one round trip, not a hundred.

        Returns:
            One entry per role, with its cluster-wide attributes and the
            databases it may connect to.
        """
        success, output = self._execute_sql(
            "SELECT r.rolname, r.rolsuper, r.rolcreatedb, r.rolcreaterole, "
            "COALESCE(array_to_string(array_agg(d.datname) "
            "FILTER (WHERE has_database_privilege(r.rolname, d.datname, 'CONNECT')), ','), '') "
            "FROM pg_roles r CROSS JOIN pg_database d "
            "WHERE r.rolname NOT LIKE 'pg\\_%' AND d.datistemplate = false "
            "GROUP BY r.rolname, r.rolsuper, r.rolcreatedb, r.rolcreaterole "
            "ORDER BY r.rolname;"
        )
        if not success:
            return []

        users = []
        for line in output.strip().splitlines():
            if not line:
                continue
            parts = line.split("|")
            attributes = []
            for index, name in ((1, "SUPERUSER"), (2, "CREATEDB"), (3, "CREATEROLE")):
                if len(parts) > index and parts[index] == "t":
                    attributes.append(name)
            databases = parts[4].split(",") if len(parts) > 4 and parts[4] else []
            users.append(
                UserInfo(
                    username=parts[0],
                    engine=self.ENGINE_NAME,
                    privileges=attributes,
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
        Grant whitelisted privileges on a database and on its public schema.

        A privilege is applied where PostgreSQL accepts it: CONNECT and CREATE
        on the database, SELECT and friends on the tables.

        Args:
            username: Role name.
            database: Database name.
            privileges: Privileges to grant. ALL PRIVILEGES when omitted.
            host: Ignored; PostgreSQL restricts hosts in pg_hba.conf.

        Raises:
            DatabaseUserError: When a privilege is not whitelisted or the grant
                fails.
        """
        self.validate_user_name(username)
        self.validate_database_name(database)
        granted = self.validate_privileges(privileges)

        role = self._escape_identifier(username)
        for statement, target_database in self._privilege_statements(
            "GRANT", granted, database, role, "TO"
        ):
            success, output = self._execute_sql(statement, database=target_database)
            if not success:
                raise DatabaseUserError(
                    f"Failed to grant privileges on '{database}' to '{username}'",
                    details=output.strip(),
                )

        self.logger.info(f"Granted {', '.join(granted)} on {database} to {username}")

    def revoke_privileges(
        self,
        username: str,
        database: str,
        privileges: Sequence[str] | None = None,
        host: str = "localhost",
    ) -> None:
        """
        Revoke whitelisted privileges on a database and on its public schema.

        Args:
            username: Role name.
            database: Database name.
            privileges: Privileges to revoke. ALL PRIVILEGES when omitted.
            host: Ignored; PostgreSQL restricts hosts in pg_hba.conf.

        Raises:
            DatabaseUserError: When a privilege is not whitelisted or the revoke
                fails.
        """
        self.validate_user_name(username)
        self.validate_database_name(database)
        revoked = self.validate_privileges(privileges)

        role = self._escape_identifier(username)
        for statement, target_database in self._privilege_statements(
            "REVOKE", revoked, database, role, "FROM"
        ):
            success, output = self._execute_sql(statement, database=target_database)
            if not success:
                raise DatabaseUserError(
                    f"Failed to revoke privileges on '{database}' from '{username}'",
                    details=output.strip(),
                )

        self.logger.info(f"Revoked {', '.join(revoked)} on {database} from {username}")

    def _privilege_statements(
        self,
        verb: str,
        privileges: Sequence[str],
        database: str,
        role: str,
        preposition: str,
    ) -> list[tuple[str, str]]:
        """
        Split privileges over the object types that accept them.

        Args:
            verb: ``GRANT`` or ``REVOKE``.
            privileges: Already whitelisted privileges.
            database: Database name.
            role: Quoted role identifier.
            preposition: ``TO`` for a grant, ``FROM`` for a revoke.

        Returns:
            Pairs of statement and the database it must run against.
        """
        statements = []
        database_privileges = [p for p in privileges if p in DATABASE_PRIVILEGES]
        table_privileges = [p for p in privileges if p in TABLE_PRIVILEGES]

        if database_privileges:
            statements.append(
                (
                    f"{verb} {', '.join(database_privileges)} ON DATABASE "
                    f"{self._escape_identifier(database)} {preposition} {role};",
                    "postgres",
                )
            )
        if table_privileges:
            statements.append(
                (
                    f"{verb} {', '.join(table_privileges)} ON ALL TABLES IN SCHEMA public "
                    f"{preposition} {role};",
                    database,
                )
            )
        return statements

    # ==================== Backup & Restore ====================

    def backup(
        self,
        database: str,
        output_path: Path | None = None,
        compress: bool = True,
        **kwargs,
    ) -> BackupInfo:
        """
        Dump a database with pg_dump.

        Args:
            database: Database name.
            output_path: Custom destination.
            compress: Pipe the dump through gzip.
            **kwargs: Accepts ``format`` (plain, custom or tar) and ``schemas``.

        Returns:
            Information about the backup.

        Raises:
            DatabaseNotFoundError: When the database does not exist.
            DatabaseBackupError: When the dump fails.
        """
        self.validate_database_name(database)
        if not self.database_exists(database):
            raise DatabaseNotFoundError(f"Database '{database}' does not exist")

        schemas = kwargs.get("schemas")
        if schemas:
            backups = [
                self.backup_schema(database, schema, compress=compress) for schema in schemas
            ]
            return backups[-1]

        dump_format = kwargs.get("format", "plain")
        if dump_format not in DUMP_FORMATS:
            raise DatabaseBackupError(
                f"Unsupported pg_dump format: {dump_format}",
                details=f"Use one of: {', '.join(sorted(DUMP_FORMATS))}.",
            )

        destination = self._backup_path(database, output_path, compress)
        return self._dump_to_file(
            self._pg_dump_argv(database, dump_format),
            destination,
            database=database,
            compress=compress,
            user=self.SUPERUSER,
        )

    def _pg_dump_argv(
        self, database: str, dump_format: str, schema: str | None = None
    ) -> list[str]:
        """
        Build a pg_dump invocation that writes to stdout.

        Runs as :data:`SUPERUSER`, applied by the caller through
        ``_dump_to_file``'s ``user=``; see :meth:`_psql_argv` for why.

        Args:
            database: Database name.
            dump_format: One of :data:`DUMP_FORMATS`.
            schema: Restrict the dump to this schema.

        Returns:
            The argument vector.
        """
        argv = [
            "pg_dump",
            "--no-password",
            f"--format={dump_format}",
        ]
        if schema is not None:
            argv.append(f"--schema={schema}")
        argv.append(database)
        return argv

    def list_schemas(self, database: str) -> list[str]:
        """
        List the schemas a database holds, excluding the system ones.

        Args:
            database: Database name.

        Returns:
            Schema names, sorted.

        Raises:
            DatabaseNotFoundError: When the database does not exist.
        """
        if not self.database_exists(database):
            raise DatabaseNotFoundError(f"Database '{database}' does not exist")

        success, output = self._execute_sql(
            "SELECT schema_name FROM information_schema.schemata "
            "WHERE schema_name NOT LIKE 'pg\\_%' AND schema_name != 'information_schema' "
            "ORDER BY schema_name;",
            database=database,
        )
        if not success:
            return []
        return [line.strip() for line in output.strip().splitlines() if line.strip()]

    def backup_schema(
        self,
        database: str,
        schema: str,
        output_path: Path | None = None,
        compress: bool = True,
    ) -> BackupInfo:
        """
        Dump a single schema.

        Args:
            database: Database name.
            schema: Schema to dump.
            output_path: Custom destination.
            compress: Pipe the dump through gzip.

        Returns:
            Information about the backup.

        Raises:
            DatabaseNotFoundError: When the database does not exist.
            DatabaseBackupError: When the dump fails.
        """
        self.validate_database_name(database)
        validate_name(schema, kind="schema", engine=self.DISPLAY_NAME, max_length=63)
        if not self.database_exists(database):
            raise DatabaseNotFoundError(f"Database '{database}' does not exist")

        destination = self._backup_path(
            database, output_path, compress, label=f"{database}-{schema}"
        )
        info = self._dump_to_file(
            self._pg_dump_argv(database, "plain", schema=schema),
            destination,
            database=database,
            compress=compress,
            user=self.SUPERUSER,
        )
        info.database = f"{database}/{schema}"
        return info

    def backup_all_schemas(
        self,
        database: str,
        output_dir: Path | None = None,
        compress: bool = True,
    ) -> list[BackupInfo]:
        """
        Dump every non-system schema of a database into its own file.

        Args:
            database: Database name.
            output_dir: Directory for the backup files.
            compress: Pipe each dump through gzip.

        Returns:
            One entry per schema that was dumped.

        Raises:
            DatabaseNotFoundError: When the database does not exist.
        """
        schemas = self.list_schemas(database)
        if not schemas:
            self.logger.info(f"No user schemas found in {database}")
            return []

        backups = []
        for schema in schemas:
            destination = None
            if output_dir is not None:
                base = self._backup_path(database, None, compress, label=f"{database}-{schema}")
                destination = Path(output_dir) / base.name
            try:
                backups.append(
                    self.backup_schema(database, schema, output_path=destination, compress=compress)
                )
            except DatabaseBackupError as exc:
                self.logger.warning(f"Failed to backup schema {schema}: {exc}")
        return backups

    def restore(
        self,
        database: str,
        backup_path: Path,
        drop_existing: bool = False,
        **kwargs,
    ) -> None:
        """
        Restore a database from a plain or gzipped dump.

        Args:
            database: Target database name.
            backup_path: Path to the backup file.
            drop_existing: Drop and recreate the database first.
            **kwargs: Accepts ``format`` (plain or custom).

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

        dump_format = kwargs.get("format", "plain")
        staged_name = f"{self.ENGINE_NAME}-restore-{database}{self.BACKUP_SUFFIX}"

        # psql and pg_restore open the file themselves, as the postgres account,
        # so the 0600 root-owned backup has to be staged for that account first.
        with self._staged_backup(backup_path, staged_name, owner=self.SUPERUSER) as staged:
            if dump_format == "custom":
                argv = [
                    "pg_restore",
                    "--no-password",
                    "-d",
                    database,
                    str(staged),
                ]
            else:
                argv = self._psql_argv(database, "-f", str(staged))
            result = self._exec(argv, timeout=TRANSFER_TIMEOUT, user=self.SUPERUSER)

        if not result.success:
            raise DatabaseBackupError(
                f"Failed to restore database '{database}'",
                details=result.stderr.strip() or "The dump may have been taken in another format.",
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
            read_only: Refuse anything that would change data. Enforcement is
                the server's, not a keyword allowlist: PostgreSQL lets a
                data-modifying CTE hide behind a leading ``WITH``, so
                ``WITH x AS (DELETE FROM t RETURNING *) SELECT * FROM x`` reads
                as a SELECT to any parser simple enough to be trustworthy.
            **kwargs: Unused.

        Returns:
            Success and the statement's output.

        Raises:
            DatabaseNotFoundError: When the database does not exist.
            DatabaseQueryError: When the statement is refused before it runs
                (see :meth:`_console_argv`) or fails.
        """
        argv, env = self._console_argv(database, query, read_only=read_only, csv=False)
        result = self._exec(argv, env=env, timeout=QUERY_TIMEOUT, user=self.SUPERUSER)
        if not result.success:
            raise DatabaseQueryError("Query failed", details=result.stderr.strip())
        return True, result.stdout

    def _console_argv(
        self, database: str, query: str, *, read_only: bool, csv: bool
    ) -> tuple[list[str], dict[str, str] | None]:
        """
        Build the psql invocation that runs an operator's console statement.

        psql parses stdin, ``-f`` files and psqlrc as scripts, running a
        meta-command wherever one appears: ``\\! id`` is a shell as the
        ``postgres`` account even in the middle of a line, ``\\o`` writes a
        file, ``\\i`` reads one. A ``-c`` string is different - psql sends it
        to the server as it is, with no meta-command or variable handling -
        unless its very first character is a backslash, in which case it is
        one meta-command. So the statement travels as its own ``-c``, a
        leading backslash is refused, and ``-X`` keeps psqlrc out.

        The trade-off is that the statement is in argv, visible in ``ps`` for
        as long as it runs. That is acceptable for the operator's own query,
        which is not a credential: the connection itself still authenticates
        by peer as :data:`SUPERUSER`, with nothing secret on the command line.
        A statement that embeds a password (``ALTER ROLE ... PASSWORD``) is
        better sent through ``wasm db user``, which keeps it on stdin.

        Read mode wraps the statement in a read-only transaction under the
        least-privilege role, one ``-c`` per statement rather than one string:
        psql before 15 prints only the last result of a multi-statement
        ``-c``, which would be COMMIT's empty one instead of the rows.

        A read-only transaction rejects INSERT, UPDATE, DELETE, DDL and
        data-modifying CTEs alike, and cannot be escalated from inside because
        SET TRANSACTION READ WRITE is refused once the session default is
        read-only. It does not, by itself, stop a read: the cluster superuser
        this connects as could still run ``SELECT pg_read_file(...)`` or
        ``pg_ls_dir('/')``, which are reads as far as the transaction mode is
        concerned. SET ROLE closes that for the rest of the session, leaving
        only what ``wasm_ro_<database>`` was granted - which is also why read
        mode is held to a single statement by :func:`console_statement`.

        Args:
            database: Database to connect to.
            query: The operator's statement.
            read_only: Wrap it in the read-only transaction and role.
            csv: Print CSV with a header row, for the structured result.

        Returns:
            The argument vector and the extra environment for psql.

        Raises:
            DatabaseNotFoundError: When the database does not exist.
            DatabaseQueryError: When the statement is refused, or the
                read-only role cannot be provisioned.
        """
        statement = console_statement(query, read_only=read_only)
        if statement.lstrip().startswith("\\"):
            raise DatabaseQueryError(
                "psql client commands are not accepted by the console",
                details=(
                    "A statement starting with a backslash is a psql meta-command "
                    "(\\! runs a shell, \\o writes a file), not SQL. Send SQL only; "
                    "use 'wasm db connect' for an interactive psql session."
                ),
            )
        if not self.database_exists(database):
            raise DatabaseNotFoundError(f"Database '{database}' does not exist")

        if read_only:
            role = self._ensure_read_only_role(database)
            commands = [
                "BEGIN READ ONLY",
                f"SET ROLE {self._escape_identifier(role)}",
                statement,
                "COMMIT",
            ]
            env: dict[str, str] | None = {"PGOPTIONS": "-c default_transaction_read_only=on"}
        else:
            commands, env = [statement], None

        tail = [arg for command in commands for arg in ("-c", command)]
        if csv:
            return self._psql_csv_argv(database, *tail), env
        # -q drops the BEGIN/SET/COMMIT status lines the wrapper would
        # otherwise print around the rows.
        quiet = ["-q"] if read_only else []
        return self._psql_argv(database, *quiet, *tail), env

    def _psql_csv_argv(self, database: str, *tail: str) -> list[str]:
        """
        Build a psql invocation that prints CSV with a header row.

        Every other invocation in this module uses :meth:`_psql_argv`'s
        ``-t -A`` (tuples only, unaligned): headerless, which is exactly
        wrong for the console's structured result - it needs the header
        ``--csv`` prints to know what to call each column.

        Args:
            database: Database to connect to.
            *tail: Arguments describing where the SQL comes from.

        Returns:
            The argument vector.
        """
        return ["psql", "-v", "ON_ERROR_STOP=1", "-d", database, "--csv", "-q", "-X", *tail]

    def execute_query_structured(
        self,
        database: str,
        query: str,
        *,
        read_only: bool = False,
        max_rows: int = 1000,
    ) -> StructuredQueryResult:
        """
        Run a statement once and parse its CSV output into columns and rows.

        One execution, not two: the plain-text ``output`` this returns is the
        same client invocation's stdout, in CSV form. Running the query a
        second time to also produce the older unaligned format would apply a
        write statement twice, which is not a trade a console is allowed to
        make for a nicer legacy text field.

        Args:
            database: Database name.
            query: The statement.
            read_only: Same enforcement as :meth:`execute_query`: a
                least-privilege role via ``SET ROLE`` inside a read-only
                transaction, provisioned by :meth:`_ensure_read_only_role`.
            max_rows: Data rows kept before the rest are dropped.

        Returns:
            The parsed result.

        Raises:
            DatabaseNotFoundError: When the database does not exist.
            DatabaseQueryError: When the statement is refused before it runs
                (see :meth:`_console_argv`) or fails.
        """
        argv, env = self._console_argv(database, query, read_only=read_only, csv=True)
        result = self._exec(argv, timeout=QUERY_TIMEOUT, env=env, user=self.SUPERUSER)
        if not result.success:
            raise DatabaseQueryError(
                "Query failed", details=(result.stderr or result.stdout).strip()
            )

        columns, rows, truncated = parse_tabular_query_output(
            result.stdout, delimiter=",", max_rows=max_rows
        )
        return StructuredQueryResult(
            output=result.stdout,
            columns=columns,
            rows=rows,
            row_count=len(rows),
            duration_ms=result.duration * 1000,
            truncated=truncated,
        )

    def _ensure_read_only_role(self, database: str) -> str:
        """
        Create, idempotently, the role the read-only console runs as.

        Closes ``SELECT pg_read_file('/etc/shadow')``,
        ``SELECT * FROM pg_ls_dir('/')`` and every other
        ``pg_read_server_files``-gated function or superuser-only catalog
        being reachable from a "read-only" console query: the role is
        ``NOSUPERUSER`` and is never logged into directly - the console
        always authenticates as :data:`SUPERUSER` over the local peer
        socket, the same as every other operation here, and only reaches
        this role with ``SET ROLE`` inside the read-only transaction, which
        a superuser may do to any role regardless of membership and which
        drops every superuser privilege for the rest of the session. The
        role itself holds nothing but ``CONNECT`` on this database,
        ``USAGE`` on its schemas and ``SELECT`` on its tables - re-granted on
        every call, so a table created after the role's first use is covered
        by the next read instead of needing a separate migration step.

        Args:
            database: The database the role is scoped to.

        Returns:
            The role name.

        Raises:
            DatabaseQueryError: When the role cannot be created or granted.
        """
        role = _read_only_role_name(database)
        role_literal = self._escape_literal(role)
        role_identifier = self._escape_identifier(role)
        provision = (
            "DO $wasm_ro_role$\n"  # noqa: S608 - quoted literal, not interpolated data
            "BEGIN\n"
            f"  IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = {role_literal}) "
            "THEN\n"
            f"    CREATE ROLE {role_identifier} "
            "NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOLOGIN;\n"
            "  END IF;\n"
            "END\n"
            "$wasm_ro_role$;\n"
            f"GRANT CONNECT ON DATABASE {self._escape_identifier(database)} TO {role_identifier};\n"
            "DO $wasm_ro_grants$\n"
            "DECLARE\n"
            "  schema_name text;\n"
            "BEGIN\n"
            "  FOR schema_name IN\n"
            "    SELECT nspname FROM pg_catalog.pg_namespace\n"
            "    WHERE nspname NOT IN ('pg_catalog', 'information_schema')\n"
            "      AND nspname NOT LIKE 'pg\\_%'\n"
            "  LOOP\n"
            f"    EXECUTE format('GRANT USAGE ON SCHEMA %I TO %I', schema_name, {role_literal});\n"
            "    EXECUTE format('GRANT SELECT ON ALL TABLES IN SCHEMA %I TO %I', "
            f"schema_name, {role_literal});\n"
            "  END LOOP;\n"
            "END\n"
            "$wasm_ro_grants$;\n"
        )
        success, output = self._execute_sql(provision, database=database)
        if not success:
            raise DatabaseQueryError(
                f"Could not provision the read-only console role for '{database}'",
                details=output.strip(),
            )
        return role

    def get_connection_string(
        self,
        database: str,
        username: str,
        password: str,
        host: str = "localhost",
    ) -> str:
        """
        Build a libpq URI.

        Args:
            database: Database name.
            username: Role name.
            password: Password.
            host: Host to connect to.

        Returns:
            The connection string.
        """
        return f"postgresql://{username}:{password}@{host}:{self.DEFAULT_PORT}/{database}"

    def get_interactive_command(
        self,
        database: str | None = None,
        username: str | None = None,
    ) -> list[str]:
        """
        Build the command that opens a psql session.

        Args:
            database: Database to connect to.
            username: Role to connect as.

        Returns:
            The argument vector. This one is handed to ``os.execvp`` directly
            instead of going through the runner - an interactive client needs
            the real terminal - so the ``runuser`` prefix that peer
            authentication requires is applied here rather than by ``user=``.
        """
        argv = [*runuser_prefix(self.SUPERUSER), "psql"]
        if database:
            argv.extend(["-d", database])
        if username:
            argv.extend(["-U", username])
        return argv


DatabaseRegistry.register(PostgresManager, aliases=["postgres", "pg", "pgsql"])
