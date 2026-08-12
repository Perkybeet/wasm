"""
Tests for the database engine managers.

The properties pinned down here are the ones that used to break in production:
the exact argv every operation builds, that a dump reaches disk byte for byte,
that no shell is ever involved, that privileges come from a whitelist and that
no password is ever visible in ``ps``.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pytest

from wasm.core.exceptions import (
    DatabaseError,
    DatabaseUserError,
)
from wasm.core.runner import FakeRunner
from wasm.managers.database.base import (
    quote_identifier,
    validate_name,
    validate_path,
)
from wasm.managers.database.mongodb import MongoDBManager
from wasm.managers.database.mysql import MySQLManager
from wasm.managers.database.postgres import PostgresManager
from wasm.managers.database.redis import RedisManager
from wasm.managers.database.registry import DatabaseRegistry, get_db_manager

# A dump that breaks every naive "echo '...' > file" implementation: single
# quotes, shell metacharacters and a run of non-ASCII bytes.
NASTY_DUMP = (
    "INSERT INTO t VALUES ('O''Brien; rm -rf /');\n"
    "INSERT INTO t VALUES ('$(touch /tmp/pwned)');\n"
    "-- binary: \x01\x02\x03\xff\n"
)

PASSWORD = "s3cr3t'; DROP TABLE users; --"

PSQL_PREFIX = ("sudo", "-u", "postgres", "psql")


class StubConfig:
    """A configuration that answers with whatever the test put in it."""

    def __init__(self, data: dict[str, Any]):
        """
        Args:
            data: Values keyed by top level configuration key.
        """
        self._data = data

    def get(self, key: str, default: Any = None) -> Any:
        """
        Read a configuration key.

        Args:
            key: Configuration key.
            default: Value returned when the key is absent.

        Returns:
            The configured value or the default.
        """
        return self._data.get(key, default)


@pytest.fixture
def postgres(runner: FakeRunner, tmp_path: Path) -> PostgresManager:
    """
    Provide a PostgreSQL manager whose backups land in a temporary directory.

    Args:
        runner: The fake runner installed process-wide.
        tmp_path: Per-test temporary directory.

    Returns:
        A configured manager.
    """
    manager = PostgresManager()
    manager.BACKUP_DIR = tmp_path / "backups"
    return manager


@pytest.fixture
def mysql(runner: FakeRunner, tmp_path: Path) -> MySQLManager:
    """
    Provide a MySQL manager whose backups land in a temporary directory.

    Args:
        runner: The fake runner installed process-wide.
        tmp_path: Per-test temporary directory.

    Returns:
        A configured manager.
    """
    runner.only_knows("mysql", "mysqldump")
    manager = MySQLManager()
    manager.BACKUP_DIR = tmp_path / "backups"
    # The host's own /etc/wasm/config.yaml must not leak into the argv tables.
    manager.config = StubConfig({})
    return manager


@pytest.fixture
def mongodb(runner: FakeRunner, tmp_path: Path) -> MongoDBManager:
    """
    Provide a MongoDB manager whose backups land in a temporary directory.

    Args:
        runner: The fake runner installed process-wide.
        tmp_path: Per-test temporary directory.

    Returns:
        A configured manager.
    """
    runner.only_knows("mongosh", "mongod")
    manager = MongoDBManager()
    manager.BACKUP_DIR = tmp_path / "backups"
    return manager


@pytest.fixture
def redis(runner: FakeRunner, tmp_path: Path) -> RedisManager:
    """
    Provide a Redis manager whose backups land in a temporary directory.

    Args:
        runner: The fake runner installed process-wide.
        tmp_path: Per-test temporary directory.

    Returns:
        A configured manager.
    """
    manager = RedisManager()
    manager.BACKUP_DIR = tmp_path / "backups"
    manager.DATA_DIR = tmp_path / "redis"
    manager.PERSISTENCE_POLLS = 1
    return manager


def existing_database(manager, exists: bool = True) -> None:
    """
    Pin what the manager believes about a database, without scripting SQL.

    Args:
        manager: The manager to patch.
        exists: What ``database_exists`` should answer.
    """
    manager.database_exists = lambda name, _exists=exists: _exists


def existing_user(manager, exists: bool = True) -> None:
    """
    Pin what the manager believes about a user.

    Args:
        manager: The manager to patch.
        exists: What ``user_exists`` should answer.
    """
    manager.user_exists = lambda username, host="localhost", _exists=exists: _exists


# ==================== The dump must survive ====================


def test_postgres_backup_writes_the_dump_verbatim(
    postgres: PostgresManager, runner: FakeRunner
) -> None:
    """A dump full of quotes and odd bytes must reach disk unchanged."""
    runner.script(PSQL_PREFIX, stdout="1")
    runner.script(["sudo", "-u", "postgres", "pg_dump"], stdout=NASTY_DUMP)

    info = postgres.backup("shop", compress=False)

    assert info.path.read_text() == NASTY_DUMP
    assert info.size == len(NASTY_DUMP.encode())


def test_no_manager_operation_ever_invokes_a_shell(
    postgres: PostgresManager,
    mysql: MySQLManager,
    mongodb: MongoDBManager,
    redis: RedisManager,
    runner: FakeRunner,
    tmp_path: Path,
) -> None:
    """No recorded command may name a shell or hand it a script."""
    runner.script(PSQL_PREFIX, stdout="1")
    runner.script(["sudo", "-u", "postgres", "pg_dump"], stdout=NASTY_DUMP)
    runner.script(["mysql"], stdout="shop")
    runner.script(["mysqldump"], stdout=NASTY_DUMP)
    runner.script(["mongosh"], stdout='["shop"]')
    runner.script(["redis-cli", "-n", "0", "INFO"], stdout="rdb_bgsave_in_progress:0")

    dump = tmp_path / "restore-me.sql"
    dump.write_text(NASTY_DUMP)

    postgres.backup("shop", compress=True)
    postgres.restore("shop", dump)
    mysql.backup("shop", compress=True)
    mysql.restore("shop", dump)
    mongo_archive = tmp_path / "mongo.tar.gz"
    mongo_archive.write_bytes(b"archive")
    mongodb.backup("shop", compress=True, output_path=mongo_archive)
    redis.backup("all", compress=True)
    redis.restore("all", dump)

    for call in runner.calls:
        assert "bash" not in call
        assert "sh" not in call
        assert "-c" not in call
        assert not any(part.endswith("/bash") or part.endswith("/sh") for part in call)


# ==================== argv tables ====================


def test_postgres_service_argv(postgres: PostgresManager, runner: FakeRunner) -> None:
    """Service verbs address the unit by name and nothing else."""
    postgres.start()
    postgres.stop()
    postgres.restart()
    postgres.enable()
    postgres.disable()
    postgres.is_running()

    assert runner.calls == [
        ("systemctl", "start", "postgresql"),
        ("systemctl", "stop", "postgresql"),
        ("systemctl", "restart", "postgresql"),
        ("systemctl", "enable", "postgresql"),
        ("systemctl", "disable", "postgresql"),
        ("systemctl", "is-active", "postgresql"),
    ]


def test_postgres_statement_argv_is_stdin_only(
    postgres: PostgresManager, runner: FakeRunner
) -> None:
    """Every statement is read from stdin, so nothing SQL reaches argv."""
    runner.script(PSQL_PREFIX, stdout="1")

    postgres.database_exists("shop")

    assert runner.calls[-1] == (
        "sudo",
        "-u",
        "postgres",
        "psql",
        "-v",
        "ON_ERROR_STOP=1",
        "-d",
        "postgres",
        "-t",
        "-A",
        "-f",
        "-",
    )
    assert runner.inputs[-1] == "SELECT 1 FROM pg_database WHERE datname = 'shop';"


@pytest.mark.parametrize(
    ("compress", "dump_format", "expected"),
    [
        (
            False,
            "plain",
            ("sudo", "-u", "postgres", "pg_dump", "--no-password", "--format=plain", "shop"),
        ),
        (
            True,
            "plain",
            ("sudo", "-u", "postgres", "pg_dump", "--no-password", "--format=plain", "shop"),
        ),
        (
            False,
            "custom",
            ("sudo", "-u", "postgres", "pg_dump", "--no-password", "--format=custom", "shop"),
        ),
    ],
)
def test_postgres_backup_argv(
    postgres: PostgresManager,
    runner: FakeRunner,
    compress: bool,
    dump_format: str,
    expected: tuple[str, ...],
) -> None:
    """pg_dump is invoked with the format asked for and nothing else."""
    runner.script(PSQL_PREFIX, stdout="1")
    runner.script(["sudo", "-u", "postgres", "pg_dump"], stdout="dump")

    postgres.backup("shop", compress=compress, format=dump_format)

    assert runner.calls[-1] == expected


def test_postgres_restore_stages_the_dump_for_the_postgres_account(
    postgres: PostgresManager, runner: FakeRunner, tmp_path: Path
) -> None:
    """A 0600 root-owned backup is copied and handed to postgres before psql reads it."""
    runner.script(PSQL_PREFIX, stdout="1")
    dump = tmp_path / "dump.sql"
    dump.write_text(NASTY_DUMP)

    postgres.restore("shop", dump)

    staged = str(postgres.BACKUP_DIR / ".staging" / "postgresql-restore-shop.sql")
    assert runner.calls[-3] == ("cp", str(dump), staged)
    assert runner.calls[-2] == ("chown", "postgres", staged)
    assert runner.calls[-1] == (
        "sudo",
        "-u",
        "postgres",
        "psql",
        "-v",
        "ON_ERROR_STOP=1",
        "-d",
        "shop",
        "-t",
        "-A",
        "-f",
        staged,
    )
    assert not Path(staged).exists()


def test_postgres_failed_dump_is_reported_not_swallowed(
    postgres: PostgresManager, runner: FakeRunner
) -> None:
    """A dump that fails must not leave the caller thinking it has a backup."""
    runner.script(PSQL_PREFIX, stdout="1")
    runner.script(["sudo", "-u", "postgres", "pg_dump"], stderr="permission denied", exit_code=1)

    with pytest.raises(Exception) as excinfo:
        postgres.backup("shop", compress=False)

    assert "permission denied" in str(excinfo.value)


def test_postgres_grant_defaults_to_all_privileges(
    postgres: PostgresManager, runner: FakeRunner
) -> None:
    """Omitting the privileges keeps the previous default, not an empty grant."""
    postgres.grant_privileges("app", "shop")

    assert runner.inputs[-2] == 'GRANT ALL PRIVILEGES ON DATABASE "shop" TO "app";'


def test_postgres_gzipped_restore_decompresses_without_a_pipe(
    postgres: PostgresManager, runner: FakeRunner, tmp_path: Path
) -> None:
    """Decompression is a program with arguments, not a shell redirection."""
    runner.script(PSQL_PREFIX, stdout="1")
    dump = tmp_path / "dump.sql.gz"
    dump.write_bytes(b"gzipped")

    postgres.restore("shop", dump)

    staged = str(postgres.BACKUP_DIR / ".staging" / "postgresql-restore-shop.sql")
    assert runner.calls[-3] == ("gzip", "-dc", str(dump))
    assert runner.calls[-1][-1] == staged


def test_mysql_statement_argv_puts_credentials_first(
    mysql: MySQLManager, runner: FakeRunner
) -> None:
    """MySQL only honours --defaults-extra-file as the very first argument."""
    mysql.config = StubConfig(
        {"databases": {"credentials": {"mysql": {"user": "root", "password": PASSWORD}}}}
    )
    runner.script(["mysql"], stdout="shop")

    mysql.database_exists("shop")

    call = runner.calls[-1]
    assert call[0] == "mysql"
    assert call[1].startswith("--defaults-extra-file=")
    assert call[2:] == ("-N", "-B")
    assert PASSWORD not in " ".join(call)
    # The option file lives only as long as the command that reads it.
    assert not Path(call[1].split("=", 1)[1]).exists()


def test_mysql_backup_argv(mysql: MySQLManager, runner: FakeRunner) -> None:
    """mysqldump streams to the runner, with no shell and no redirection."""
    existing_database(mysql)
    runner.script(["mysqldump"], stdout=NASTY_DUMP)

    info = mysql.backup("shop", compress=False)

    assert runner.calls[-1] == (
        "mysqldump",
        "--single-transaction",
        "--routines",
        "--triggers",
        "shop",
    )
    assert info.path.read_text() == NASTY_DUMP


def test_mysql_restore_reads_the_staged_file_through_the_client(
    mysql: MySQLManager, runner: FakeRunner, tmp_path: Path
) -> None:
    """The dump is named in a source command on stdin, never in argv."""
    existing_database(mysql)
    dump = tmp_path / "dump.sql"
    dump.write_text(NASTY_DUMP)

    mysql.restore("shop", dump)

    staged = mysql.BACKUP_DIR / ".staging" / "mysql-restore-shop.sql"
    assert runner.calls[-1] == ("mysql", "-N", "-B", "-D", "shop")
    assert runner.inputs[-1] == f"source {staged}\n"


def test_mysql_install_argv(mysql: MySQLManager, runner: FakeRunner) -> None:
    """Installation prefers MariaDB and hardens the result."""
    mysql.install()

    assert runner.calls[0] == ("apt-get", "update")
    assert runner.calls[1] == ("apt-get", "install", "-y", "mariadb-server")
    assert runner.calls[2] == ("systemctl", "enable", "mariadb")
    assert runner.calls[3] == ("systemctl", "start", "mariadb")
    assert runner.inputs[-3:] == [
        "DELETE FROM mysql.user WHERE User='';",
        "DROP DATABASE IF EXISTS test;",
        "FLUSH PRIVILEGES;",
    ]


def test_mongodb_statement_argv(mongodb: MongoDBManager, runner: FakeRunner) -> None:
    """Scripts are piped into the shell, not passed with --eval."""
    runner.script(["mongosh"], stdout='["shop"]')

    mongodb.database_exists("shop")

    assert runner.calls[-1] == ("mongosh", "admin", "--quiet")
    assert runner.inputs[-1] == (
        "EJSON.stringify(db.adminCommand('listDatabases').databases.map(d => d.name))"
    )


def test_mongodb_backup_argv(mongodb: MongoDBManager, runner: FakeRunner, tmp_path: Path) -> None:
    """mongodump writes a tree, which tar then packs; neither needs a shell."""
    existing_database(mongodb)
    archive = tmp_path / "mongo.tar.gz"
    archive.write_bytes(b"archive")

    mongodb.backup("shop", compress=True, output_path=archive)

    dump_call, tar_call = runner.calls[-2], runner.calls[-1]
    assert dump_call[:4] == ("mongodump", "--db", "shop", "--out")
    assert dump_call[-1] == "--gzip"
    assert tar_call[0] == "tar"
    assert tar_call[1] == "-czf"
    assert tar_call[-1] == "shop"


def test_redis_argv_table(redis: RedisManager, runner: FakeRunner) -> None:
    """Redis commands are argv, addressed at an explicit slot."""
    runner.script(["redis-cli", "-n", "0", "INFO"], stdout="rdb_bgsave_in_progress:0")

    redis.drop_database("3")
    redis.flush_all()
    redis.backup("all", compress=False)

    assert runner.calls[0] == ("redis-cli", "-n", "3", "FLUSHDB")
    assert runner.calls[1] == ("redis-cli", "-n", "0", "FLUSHALL")
    assert runner.calls[2] == ("redis-cli", "-n", "0", "BGSAVE")
    assert runner.calls[-1] == ("cat", str(redis.DATA_DIR / "dump.rdb"))


def test_redis_restore_installs_the_snapshot_and_fixes_ownership(
    redis: RedisManager, runner: FakeRunner, tmp_path: Path
) -> None:
    """A restore stops the server, replaces dump.rdb and starts it again."""
    snapshot = tmp_path / "snapshot.rdb"
    snapshot.write_bytes(b"\x00\x01binary")

    redis.restore("all", snapshot)

    rdb = str(redis.DATA_DIR / "dump.rdb")
    assert runner.calls[0] == ("systemctl", "stop", "redis-server")
    assert runner.calls[1] == ("cp", str(snapshot), rdb)
    assert runner.calls[2] == ("chown", "redis:redis", rdb)
    assert runner.calls[-1] == ("systemctl", "start", "redis-server")


# ==================== Privilege whitelist ====================


@pytest.mark.parametrize(
    "privilege",
    [
        "SELECT; DROP DATABASE shop",
        "SELECT -- comment",
        "SELECT UNION SELECT password FROM mysql.user",
        "ALL PRIVILEGES, FILE",
        "GRANT OPTION; FLUSH PRIVILEGES",
        "SELECT/*x*/",
        "'SELECT'",
        "",
        "   ",
        "DELETE\nDROP",
        "NOT_A_PRIVILEGE",
    ],
)
def test_mysql_rejects_privileges_outside_the_whitelist(
    mysql: MySQLManager, runner: FakeRunner, privilege: str
) -> None:
    """Anything that is not a bare whitelisted keyword is refused."""
    with pytest.raises(DatabaseUserError) as excinfo:
        mysql.grant_privileges("app", "shop", privileges=[privilege])

    assert "Allowed" in str(excinfo.value)
    assert runner.calls == []


@pytest.mark.parametrize(
    "privilege",
    [
        "SELECT; DROP DATABASE shop",
        "CONNECT -- comment",
        "ALL PRIVILEGES; GRANT ALL ON DATABASE postgres TO app",
        "SELECT, INSERT",
        "SUPERUSER",
        "",
    ],
)
def test_postgres_rejects_privileges_outside_the_whitelist(
    postgres: PostgresManager, runner: FakeRunner, privilege: str
) -> None:
    """The PostgreSQL whitelist refuses the same class of input."""
    with pytest.raises(DatabaseUserError):
        postgres.grant_privileges("app", "shop", privileges=[privilege])

    assert runner.calls == []


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        (["select"], "SELECT"),
        (["  Insert  "], "INSERT"),
        (["all privileges"], "ALL PRIVILEGES"),
        (["ALL   PRIVILEGES"], "ALL PRIVILEGES"),
    ],
)
def test_mysql_accepts_whitelisted_privileges_in_any_case(
    mysql: MySQLManager, runner: FakeRunner, given: list[str], expected: str
) -> None:
    """Case and padding are normalised; the statement carries the keyword."""
    runner.script(["mysql"], stdout="")

    mysql.grant_privileges("app", "shop", privileges=given)

    assert runner.inputs[-2] == f"GRANT {expected} ON `shop`.* TO 'app'@'localhost';"


def test_mysql_grant_deduplicates_and_keeps_order(mysql: MySQLManager, runner: FakeRunner) -> None:
    """Repeated privileges collapse, and the order the caller chose is kept."""
    mysql.grant_privileges("app", "shop", privileges=["INSERT", "select", "INSERT"])

    assert runner.inputs[-2] == "GRANT INSERT, SELECT ON `shop`.* TO 'app'@'localhost';"


def test_postgres_grant_splits_privileges_by_object_type(
    postgres: PostgresManager, runner: FakeRunner
) -> None:
    """CONNECT is a database privilege and SELECT a table one; each goes where it is legal."""
    postgres.grant_privileges("app", "shop", privileges=["CONNECT", "SELECT"])

    assert runner.inputs[-2] == 'GRANT CONNECT ON DATABASE "shop" TO "app";'
    assert runner.inputs[-1] == 'GRANT SELECT ON ALL TABLES IN SCHEMA public TO "app";'
    assert runner.calls[-1][7] == "shop"


def test_postgres_revoke_uses_the_same_whitelist(
    postgres: PostgresManager, runner: FakeRunner
) -> None:
    """A revoke is a grant in reverse, with the same validation."""
    postgres.revoke_privileges("app", "shop", privileges=["ALL PRIVILEGES"])

    assert runner.inputs[-2] == 'REVOKE ALL PRIVILEGES ON DATABASE "shop" FROM "app";'
    assert runner.inputs[-1] == 'REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM "app";'


@pytest.mark.parametrize("rule", ["+@all; FLUSHALL", ">password", "~* nopass", "+@all\n-@admin"])
def test_redis_rejects_acl_rules_that_are_not_rules(
    redis: RedisManager, runner: FakeRunner, rule: str
) -> None:
    """A Redis ACL rule may not smuggle another command or a password."""
    existing_user(redis)

    with pytest.raises(DatabaseUserError):
        redis.grant_privileges("app", "0", privileges=[rule])


@pytest.mark.parametrize("role", ["readWrite; db.dropDatabase()", "read'", "root --", ""])
def test_mongodb_rejects_roles_that_are_not_roles(
    mongodb: MongoDBManager, runner: FakeRunner, role: str
) -> None:
    """A MongoDB role name is an identifier, not a fragment of script."""
    existing_user(mongodb)

    with pytest.raises(DatabaseUserError):
        mongodb.grant_privileges("app", "shop", privileges=[role])


# ==================== Names and identifiers ====================


@pytest.mark.parametrize(
    "name",
    ['shop"; DROP DATABASE postgres; --', "shop`", "shop;", "shop name", "-shop", "", "a" * 64],
)
def test_postgres_refuses_impossible_database_names(postgres: PostgresManager, name: str) -> None:
    """A name that would need quoting to be safe is refused outright."""
    with pytest.raises(DatabaseError):
        postgres.validate_database_name(name)


def test_postgres_accepts_the_names_wasm_generates(postgres: PostgresManager) -> None:
    """Generated names, dashes included, stay valid."""
    assert postgres.validate_database_name("my-app_2") == "my-app_2"


@pytest.mark.parametrize(
    ("value", "quote", "expected"),
    [
        ('we"ird', '"', '"we""ird"'),
        ('a""b', '"', '"a""""b"'),
        ("we`ird", "`", "`we``ird`"),
        ("`", "`", "````"),
    ],
)
def test_identifier_quoting_doubles_the_engine_quote(value: str, quote: str, expected: str) -> None:
    """The quote character inside an identifier is doubled, never dropped."""
    assert quote_identifier(value, quote) == expected


def test_engine_escapers_use_their_own_quote_character() -> None:
    """PostgreSQL quotes with double quotes, MySQL with backticks."""
    assert PostgresManager._escape_identifier('we"ird') == '"we""ird"'
    assert MySQLManager._escape_identifier("we`ird") == "`we``ird`"
    assert PostgresManager._escape_literal("O'Brien") == "'O''Brien'"
    assert MySQLManager._escape_literal("O'Brien\\") == "'O''Brien\\\\'"


def test_validate_name_reports_the_limit_it_enforces() -> None:
    """The error says what is allowed, not just that something was wrong."""
    with pytest.raises(DatabaseError) as excinfo:
        validate_name("x" * 65, kind="database", engine="MySQL", max_length=64)

    assert "at most 64" in str(excinfo.value)


def test_validate_path_refuses_a_path_a_client_would_reparse() -> None:
    """A path handed to a client command may not carry quoting or separators."""
    with pytest.raises(Exception) as excinfo:
        validate_path(Path("/backups/dump';DROP.sql"), purpose="a MySQL restore")

    assert "Unsafe path" in str(excinfo.value)


# ==================== Passwords stay out of argv ====================


def test_no_password_reaches_argv_in_any_engine(
    postgres: PostgresManager,
    mysql: MySQLManager,
    mongodb: MongoDBManager,
    redis: RedisManager,
    runner: FakeRunner,
) -> None:
    """Creating a user must never put the password on a command line."""
    for manager in (postgres, mysql, mongodb, redis):
        existing_user(manager, exists=False)
        manager.create_user("app", password=PASSWORD)

    for call in runner.calls:
        assert PASSWORD not in " ".join(call)


def test_postgres_create_user_sends_the_statement_on_stdin(
    postgres: PostgresManager, runner: FakeRunner
) -> None:
    """The CREATE ROLE statement, password included, travels on stdin."""
    existing_user(postgres, exists=False)

    _, password = postgres.create_user("app", password=PASSWORD)

    assert password == PASSWORD
    assert runner.inputs[-1] == (
        "CREATE ROLE \"app\" WITH LOGIN PASSWORD 's3cr3t''; DROP TABLE users; --';"
    )
    assert runner.calls[-1][-2:] == ("-f", "-")


def test_mysql_create_user_sends_the_statement_on_stdin(
    mysql: MySQLManager, runner: FakeRunner
) -> None:
    """MySQL gets the statement on stdin too, so ps shows only 'mysql -N -B'."""
    existing_user(mysql, exists=False)

    mysql.create_user("app", password=PASSWORD)

    assert runner.inputs[-2] == (
        "CREATE USER 'app'@'localhost' IDENTIFIED BY 's3cr3t''; DROP TABLE users; --';"
    )
    assert runner.calls[-2] == ("mysql", "-N", "-B")


def test_redis_stores_the_password_as_a_digest(redis: RedisManager, runner: FakeRunner) -> None:
    """Redis takes a SHA-256 in ACL SETUSER, which keeps the secret out of argv."""
    existing_user(redis, exists=False)

    redis.create_user("app", password=PASSWORD)

    digest = hashlib.sha256(PASSWORD.encode()).hexdigest()
    assert runner.calls[-1] == (
        "redis-cli",
        "-n",
        "0",
        "ACL",
        "SETUSER",
        "app",
        "on",
        f"#{digest}",
        "+@all",
        "~*",
    )


def test_redis_sets_requirepass_over_stdin(redis: RedisManager, runner: FakeRunner) -> None:
    """The instance password is hex escaped and sent on stdin."""
    redis.set_password(PASSWORD)

    assert runner.calls[0] == ("redis-cli",)
    assert PASSWORD not in runner.inputs[0]
    assert runner.inputs[0].startswith('CONFIG SET requirepass "\\x73\\x33\\x63')


def test_redis_authenticates_through_the_environment(
    redis: RedisManager, runner: FakeRunner
) -> None:
    """Once a password is known, it travels in REDISCLI_AUTH, not in argv."""
    redis.set_password(PASSWORD)
    runner.calls.clear()

    redis.flush_all()

    assert runner.calls[-1] == ("redis-cli", "-n", "0", "FLUSHALL")


def test_mongodb_create_user_sends_the_script_on_stdin(
    mongodb: MongoDBManager, runner: FakeRunner
) -> None:
    """The createUser script is piped in, and its values are JSON literals."""
    existing_user(mongodb, exists=False)

    mongodb.create_user("app", password=PASSWORD, database="shop")

    assert runner.calls[-1] == ("mongosh", "admin", "--quiet")
    script = runner.inputs[-1]
    assert '"s3cr3t\'; DROP TABLE users; --"' in script
    assert '[{"role": "readWrite", "db": "shop"}]' in script


# ==================== Housekeeping ====================


def test_failed_service_action_raises_with_a_next_step(
    postgres: PostgresManager, runner: FakeRunner
) -> None:
    """A failure explains itself instead of returning False."""
    runner.script(["systemctl", "start"], stderr="Job failed", exit_code=1)

    with pytest.raises(Exception) as excinfo:
        postgres.start()

    assert "journalctl -u postgresql" in str(excinfo.value)


def test_backup_listing_handles_database_names_with_dashes(
    postgres: PostgresManager,
) -> None:
    """The timestamp anchors the parse, so a dashed name survives it."""
    postgres.BACKUP_DIR.mkdir(parents=True)
    (postgres.BACKUP_DIR / "postgresql-my-app-20240102_030405.sql.gz").write_text("x")
    (postgres.BACKUP_DIR / "postgresql-other-20240102_030405.sql").write_text("y")
    (postgres.BACKUP_DIR / "unrelated.txt").write_text("z")

    backups = postgres.list_backups()

    assert {backup.database for backup in backups} == {"my-app", "other"}
    assert postgres.list_backups(database="my-app")[0].compressed is True


def test_registry_still_resolves_every_engine() -> None:
    """The registry contract the CLI and the API depend on is unchanged."""
    assert set(DatabaseRegistry.list_engines()) == {"mysql", "postgresql", "redis", "mongodb"}
    assert isinstance(get_db_manager("pg"), PostgresManager)
    assert isinstance(get_db_manager("mariadb"), MySQLManager)
    assert isinstance(get_db_manager("mongo"), MongoDBManager)
    assert isinstance(get_db_manager("redis-server"), RedisManager)
    assert get_db_manager("nosuchengine") is None
