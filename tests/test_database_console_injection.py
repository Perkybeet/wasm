"""
The SQL console never lets a database client interpret the operator's text as
a client script.

``psql`` and ``mysql`` both have client-side commands that run on the WASM host,
not in the database: ``\\! id`` (a shell), ``\\o``/``tee`` (write a file),
``\\i``/``source`` (read a file), ``pager`` (another shell). Fed on stdin as a
script, they execute wherever they appear - psql honours ``\\!`` even in the
middle of a line - so a "read-only" console was a root shell. These tests pin
the argv and stdin the managers build for hostile input: either the text is
refused before any client runs, or it reaches the client only as data the
server will parse (psql ``-c``, mysql ``--binary-mode``).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import re
import stat
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest

from wasm.core.exceptions import DatabaseQueryError, SecurityError
from wasm.core.runner import CommandResult, FakeRunner
from wasm.core.store import WASMStore
from wasm.managers.database.mysql import MySQLManager
from wasm.managers.database.postgres import PostgresManager, _scram_sha256_verifier

PSQL = ("runuser", "-u", "postgres", "--", "psql")

#: Statements whose only purpose is a client-side escape.
PSQL_CLIENT_COMMANDS = [
    "\\! id",
    "  \\! id",
    "\\o /tmp/x",
    "\\i /etc/shadow",
    "\\copy t to '/tmp/x'",
]
MYSQL_CLIENT_COMMANDS = [
    "\\! id",
    "SELECT 1 \\! id",
    "system id",
    "  SYSTEM id",
    "\\o /tmp/x",
    "source /etc/shadow",
    "\\. /etc/shadow",
    "tee /tmp/x",
    "pager id",
    "SELECT 1;\nsystem id",
]


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
def postgres(runner: FakeRunner) -> Iterator[PostgresManager]:
    """
    Provide a PostgreSQL manager whose ``app`` database exists.

    Args:
        runner: The fake runner installed process-wide.

    Yields:
        The manager.
    """
    runner.script(list(PSQL), stdout="1\n")
    WASMStore.reset_instance()
    manager = PostgresManager()
    manager.config = StubConfig({})
    try:
        yield manager
    finally:
        WASMStore.reset_instance()


@pytest.fixture
def mysql(runner: FakeRunner) -> MySQLManager:
    """
    Provide a MySQL manager whose ``app`` database exists.

    Args:
        runner: The fake runner installed process-wide.

    Returns:
        The manager.
    """
    runner.only_knows("mysql", "mysqldump")
    runner.script(["mysql"], stdout="app\n")
    manager = MySQLManager()
    manager.config = StubConfig({})
    return manager


def _is_psql(call: tuple[str, ...]) -> bool:
    """
    Report whether a recorded call runs psql, as postgres or directly.

    Args:
        call: A recorded argv.

    Returns:
        True for the superuser's ``runuser -u postgres -- psql`` and for the
        read-only console's own ``psql``.
    """
    return call[: len(PSQL)] == PSQL or call[:1] == ("psql",)


def _psql_console_calls(runner: FakeRunner) -> list[tuple[str, ...]]:
    """
    Return the psql invocations that carried a console statement.

    Args:
        runner: The fake runner.

    Returns:
        Every psql call made with ``-c``.
    """
    return [call for call in runner.calls if _is_psql(call) and "-c" in call]


def _command_strings(call: tuple[str, ...]) -> list[str]:
    """
    Extract the values of every ``-c`` option in an argv.

    Args:
        call: A recorded argv.

    Returns:
        The command strings, in order.
    """
    return [call[i + 1] for i, arg in enumerate(call[:-1]) if arg == "-c"]


def _stdin_mentions(runner: FakeRunner, needle: str) -> bool:
    """
    Report whether any process received ``needle`` on stdin.

    Args:
        runner: The fake runner.
        needle: Text to look for.

    Returns:
        True when some call's stdin contained it.
    """
    return any(needle in (sent or "") for sent in runner.inputs)


class TestPsqlNeverParsesTheConsoleText:
    """psql gets the operator's statement as a ``-c`` string, never as a script."""

    @pytest.mark.parametrize("read_only", [True, False])
    @pytest.mark.parametrize("structured", [True, False])
    @pytest.mark.parametrize("statement", PSQL_CLIENT_COMMANDS)
    def test_a_leading_backslash_is_refused_before_psql_runs(
        self, postgres, runner, statement, read_only, structured
    ):
        # -c would hand a string that starts with a backslash to psql's own
        # meta-command parser, so it is the one shape -c cannot carry safely.
        before = len(runner.calls)
        run = postgres.execute_query_structured if structured else postgres.execute_query

        with pytest.raises(DatabaseQueryError, match="client command"):
            run(database="app", query=statement, read_only=read_only)

        assert not any("-c" in call for call in runner.calls[before:])
        assert not _stdin_mentions(runner, statement.strip())

    @pytest.mark.parametrize("read_only", [True, False])
    @pytest.mark.parametrize("structured", [True, False])
    @pytest.mark.parametrize(
        "statement", ["SELECT 1 \\! id", "SELECT 1 \\o /tmp/x", "system id", "SELECT '\\! id'"]
    )
    def test_a_mid_line_backslash_reaches_psql_only_as_data(
        self, postgres, runner, statement, read_only, structured
    ):
        run = postgres.execute_query_structured if structured else postgres.execute_query

        run(database="app", query=statement, read_only=read_only)

        (call,) = _psql_console_calls(runner)
        assert statement in _command_strings(call)
        # Nothing about the console statement was read as a script.
        assert "-f" not in call
        assert not _stdin_mentions(runner, statement)

    @pytest.mark.parametrize("structured", [True, False])
    def test_psqlrc_is_never_read(self, postgres, runner, structured):
        run = postgres.execute_query_structured if structured else postgres.execute_query

        run(database="app", query="SELECT 1", read_only=True)

        psql_calls = [call for call in runner.calls if _is_psql(call)]
        assert psql_calls
        assert all("-X" in call for call in psql_calls)

    @pytest.mark.parametrize("structured", [True, False])
    def test_read_mode_wraps_the_statement_in_separate_command_strings(
        self, postgres, runner, structured
    ):
        # One -c per statement, not one string: before PostgreSQL 15, psql
        # prints only the last result of a multi-statement -c, which would be
        # COMMIT's (nothing) instead of the query's rows.
        run = postgres.execute_query_structured if structured else postgres.execute_query

        run(database="app", query="SELECT n FROM t", read_only=True)

        (call,) = _psql_console_calls(runner)
        assert _command_strings(call) == ["BEGIN READ ONLY", "SELECT n FROM t", "COMMIT"]
        assert "-q" in call

    @pytest.mark.parametrize("structured", [True, False])
    def test_read_mode_refuses_a_second_statement_at_the_manager(
        self, postgres, runner, structured
    ):
        # "SELECT 1; COMMIT; DELETE FROM t" would close the read-only
        # transaction and write. The role the session signs in as cannot write
        # either, but the API and the CLI refuse ';' too, and the guarantee
        # belongs to the one place every caller goes through.
        run = postgres.execute_query_structured if structured else postgres.execute_query

        with pytest.raises(DatabaseQueryError, match="one statement"):
            run(
                database="app",
                query="SELECT 1; COMMIT; RESET ROLE; COPY (SELECT 1) TO PROGRAM 'id'",
                read_only=True,
            )

        assert not _psql_console_calls(runner)

    def test_read_mode_accepts_one_trailing_semicolon(self, postgres, runner):
        postgres.execute_query(database="app", query="SELECT 1;  ", read_only=True)

        (call,) = _psql_console_calls(runner)
        assert "SELECT 1" in _command_strings(call)

    def test_write_mode_still_accepts_a_batch(self, postgres, runner):
        postgres.execute_query(
            database="app", query="DELETE FROM a; DELETE FROM b", read_only=False
        )

        (call,) = _psql_console_calls(runner)
        assert _command_strings(call) == ["DELETE FROM a; DELETE FROM b"]


class TestMysqlNeverRunsClientCommands:
    """mysql runs the console text with client commands switched off."""

    @pytest.mark.parametrize("read_only", [True, False])
    @pytest.mark.parametrize("structured", [True, False])
    @pytest.mark.parametrize("statement", MYSQL_CLIENT_COMMANDS)
    def test_client_commands_are_refused_before_mysql_runs(
        self, mysql, runner, statement, read_only, structured
    ):
        run = mysql.execute_query_structured if structured else mysql.execute_query
        before = len(runner.calls)

        with pytest.raises(DatabaseQueryError):
            run(database="app", query=statement, read_only=read_only)

        # The read-only account may be provisioned first; the statement itself
        # never reaches a client, on stdin or in argv.
        assert not _stdin_mentions(runner, statement.strip().splitlines()[-1])
        assert not any(statement in " ".join(call) for call in runner.calls[before:])

    @pytest.mark.parametrize("read_only", [True, False])
    @pytest.mark.parametrize("structured", [True, False])
    def test_the_console_statement_runs_in_binary_mode(self, mysql, runner, read_only, structured):
        run = mysql.execute_query_structured if structured else mysql.execute_query

        run(database="app", query="SELECT status FROM orders", read_only=read_only)

        index = next(
            i for i, sent in enumerate(runner.inputs) if "SELECT status FROM orders" in (sent or "")
        )
        assert "--binary-mode" in runner.calls[index]
        # Passed as data on stdin, never on the command line.
        assert not any("SELECT status" in arg for arg in runner.calls[index])

    def test_a_column_named_like_a_client_command_is_not_refused(self, mysql, runner):
        # The client only treats a command word at the start of a statement
        # as a command, so neither does the guard.
        mysql.execute_query(
            database="app", query="SELECT id,\n  status,\n  source\nFROM t", read_only=True
        )

    def test_the_read_only_wrapper_survives_a_trailing_comment(self, mysql, runner):
        mysql.execute_query(database="app", query="SELECT 1 -- note", read_only=True)

        sent = next(s for s in runner.inputs if s and "SELECT 1 -- note" in s)
        # The statement's terminator is on its own line, where the comment
        # cannot swallow it.
        assert "SELECT 1 -- note\n;\nCOMMIT;" in sent

    def test_read_mode_refuses_a_second_statement_at_the_manager(self, mysql, runner):
        with pytest.raises(DatabaseQueryError, match="one statement"):
            mysql.execute_query(
                database="app", query="SELECT 1; COMMIT; DROP TABLE t", read_only=True
            )

    @pytest.mark.parametrize("structured", [True, False])
    def test_read_mode_forces_the_read_only_account_on_the_command_line(
        self, mysql, runner, monkeypatch, structured
    ):
        # --defaults-extra-file is read before ~/.my.cnf, so root's own option
        # file could otherwise swap the read-only account for root. The user
        # name on argv beats every option file, and HOME points away from
        # root's anyway.
        seen: list[tuple[list[str], Mapping[str, str] | None]] = []
        original = mysql._exec

        def recording_exec(argv, **kwargs) -> CommandResult:
            seen.append((list(argv), kwargs.get("env")))
            return original(argv, **kwargs)

        monkeypatch.setattr(mysql, "_exec", recording_exec)
        run = mysql.execute_query_structured if structured else mysql.execute_query

        run(database="app", query="SELECT 1", read_only=True)

        argv, env = next((a, e) for a, e in seen if "--binary-mode" in a)
        assert argv[1].startswith("--defaults-extra-file=")
        assert "--user=wasm_ro_app" in argv
        assert "root" not in " ".join(argv)
        assert env is not None
        assert env["HOME"] != str(Path.home())
        assert Path(env["HOME"]) == Path(argv[1].split("=", 1)[1]).parent


#: The escape an earlier build left open: its session user was the superuser
#: and only SET ROLE stood in the way, which one SELECT can undo.
ROLE_ESCAPE = (
    "SELECT set_config('role', 'postgres', true), "
    "query_to_xml('select pg_read_file(''/etc/passwd'')', true, true, '')"
)

#: What libpq prints when pg_hba.conf has no line for a password login.
HBA_REFUSAL = (
    'psql: error: connection to server at "127.0.0.1", port 5432 failed: FATAL:  '
    'no pg_hba.conf entry for host "127.0.0.1", user "wasm_ro_app", database "app", '
    "no encryption\n"
)

#: What libpq prints when the server rejects the password itself.
PASSWORD_REJECTED = (
    'psql: error: connection to server at "127.0.0.1", port 5432 failed: FATAL:  '
    'password authentication failed for user "wasm_ro_app"\n'
)


class ConsoleRunner(FakeRunner):
    """
    A FakeRunner that also records environments and answers direct psql calls in turn.

    The read-only console's password travels in ``env``, which FakeRunner does
    not record, and a rejected sign-in followed by a retry needs two different
    answers to the same argv prefix.
    """

    def __init__(self) -> None:
        """Start with no queued answers."""
        super().__init__()
        self.envs: list[Mapping[str, str] | None] = []
        self.redacted: list[tuple[str, ...]] = []
        self.sign_ins: list[CommandResult] = []

    def run(self, argv, **kwargs) -> CommandResult:
        """
        Record the call's environment and secrets, then answer it.

        Args:
            argv: The argument vector.
            **kwargs: The runner's keyword arguments.

        Returns:
            The next queued answer for a direct psql call, else the scripted one.
        """
        self.envs.append(kwargs.get("env"))
        self.redacted.append(tuple(kwargs.get("secrets") or ()))
        result = super().run(argv, **kwargs)
        if argv[0] == "psql" and kwargs.get("user") is None and self.sign_ins:
            return self.sign_ins.pop(0)
        return result


@pytest.fixture
def console_runner() -> ConsoleRunner:
    """
    Install a :class:`ConsoleRunner` as the process-wide runner.

    Yields:
        The runner.
    """
    from wasm.core.runner import set_runner

    fake = ConsoleRunner()
    fake.script(list(PSQL), stdout="1\n")
    set_runner(fake)
    try:
        yield fake
    finally:
        set_runner(None)


@pytest.fixture
def read_only_postgres(console_runner: ConsoleRunner) -> Iterator[PostgresManager]:
    """
    Provide a PostgreSQL manager on the console runner, with no configured port.

    The store is reopened around the test: role passwords are kept beside it,
    and the process-wide singleton would otherwise point at another test's
    directory.

    Args:
        console_runner: The runner, installed process-wide.

    Yields:
        The manager.
    """
    WASMStore.reset_instance()
    manager = PostgresManager()
    manager.config = StubConfig({})
    try:
        yield manager
    finally:
        WASMStore.reset_instance()


def _password_file(manager: PostgresManager, role: str = "wasm_ro_app") -> Path:
    """
    Return where the manager keeps a role's password.

    Args:
        manager: The manager.
        role: The read-only role.

    Returns:
        The password file's path.
    """
    return manager._read_only_password_file(role)


def _failure(stderr: str, exit_code: int = 2) -> CommandResult:
    """
    Build a failed psql result.

    Args:
        stderr: What psql printed.
        exit_code: Its exit status; 2 is psql's EXIT_BADCONN.

    Returns:
        The result.
    """
    return CommandResult(argv=("psql",), exit_code=exit_code, stderr=stderr)


class TestPostgresReadModeSignsInAsTheRole:
    """
    Read mode's limit is the login, held by the server.

    A superuser session that ran ``SET ROLE`` could put the superuser back
    from inside one SELECT, so read mode never opens a superuser session at
    all: it signs in over TCP as ``wasm_ro_<database>``.
    """

    @pytest.mark.parametrize("structured", [True, False])
    def test_read_mode_signs_in_as_the_role_over_the_loopback(
        self, read_only_postgres, console_runner, structured
    ):
        run = (
            read_only_postgres.execute_query_structured
            if structured
            else read_only_postgres.execute_query
        )

        run(database="app", query="SELECT n FROM t", read_only=True)

        (call,) = _psql_console_calls(console_runner)
        assert call[0] == "psql"
        assert "runuser" not in call
        assert call[call.index("-h") + 1] == "127.0.0.1"
        assert call[call.index("-p") + 1] == "5432"
        assert call[call.index("-U") + 1] == "wasm_ro_app"
        assert call[call.index("-d") + 1] == "app"
        # Never prompt, never read psqlrc, stop at the first error.
        assert {"-w", "-X", "ON_ERROR_STOP=1"} <= set(call)
        assert _command_strings(call) == ["BEGIN READ ONLY", "SELECT n FROM t", "COMMIT"]

    def test_the_password_travels_in_the_environment_only(self, read_only_postgres, console_runner):
        read_only_postgres.execute_query(database="app", query="SELECT 1", read_only=True)

        index = console_runner.calls.index(_psql_console_calls(console_runner)[0])
        env = console_runner.envs[index]
        stored = _password_file(read_only_postgres).read_text().strip()
        assert env is not None
        assert env["PGPASSWORD"] == stored
        assert env["PGOPTIONS"] == "-c default_transaction_read_only=on"
        assert not any(stored in arg for call in console_runner.calls for arg in call)
        # Handed to the runner as a secret, so nothing it records shows it.
        assert stored in console_runner.redacted[index]

    @pytest.mark.parametrize("structured", [True, False])
    def test_the_role_escape_only_ever_reaches_a_session_signed_in_as_the_role(
        self, read_only_postgres, console_runner, structured
    ):
        run = (
            read_only_postgres.execute_query_structured
            if structured
            else read_only_postgres.execute_query
        )

        run(database="app", query=ROLE_ESCAPE, read_only=True)

        carrying = [
            call for call in console_runner.calls if any(ROLE_ESCAPE in arg for arg in call)
        ]
        assert carrying
        for call in carrying:
            assert call[0] == "psql"
            assert call[call.index("-U") + 1] == "wasm_ro_app"
        # Nor on stdin of any session, the superuser's provisioning included.
        assert not _stdin_mentions(console_runner, "pg_read_file")

    def test_provisioning_grants_login_with_a_scram_verifier_and_stores_the_password(
        self, read_only_postgres, console_runner
    ):
        read_only_postgres.execute_query(database="app", query="SELECT 1", read_only=True)

        provisioning = next(s for s in console_runner.inputs if s and "wasm_ro_role" in s)
        path = _password_file(read_only_postgres)
        password = path.read_text().strip()
        assert re.search(
            r'ALTER ROLE "wasm_ro_app" NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT '
            r"NOREPLICATION NOBYPASSRLS LOGIN PASSWORD 'SCRAM-SHA-256\$4096:[^']+';",
            provisioning,
        )
        assert 'ALTER ROLE "wasm_ro_app" SET default_transaction_read_only = on;' in provisioning
        # The server is sent a verifier, never the password.
        assert password not in provisioning
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
        assert len(password) >= 32

    def test_the_verifier_is_the_one_postgresql_would_compute(self):
        # RFC 5802/7677, checked the way the server checks a client proof.
        verifier = _scram_sha256_verifier("pencil")
        match = re.fullmatch(r"SCRAM-SHA-256\$4096:([^$]+)\$([^:]+):(.+)", verifier)
        assert match
        salt, stored_key, server_key = (base64.b64decode(part) for part in match.groups())
        salted = hashlib.pbkdf2_hmac("sha256", b"pencil", salt, 4096)
        client_key = hmac.new(salted, b"Client Key", hashlib.sha256).digest()
        assert hashlib.sha256(client_key).digest() == stored_key
        assert hmac.new(salted, b"Server Key", hashlib.sha256).digest() == server_key

    def test_a_stored_password_is_reused_not_rotated(self, read_only_postgres, console_runner):
        read_only_postgres.execute_query(database="app", query="SELECT 1", read_only=True)
        first = _password_file(read_only_postgres).read_text()

        read_only_postgres.execute_query(database="app", query="SELECT 2", read_only=True)

        provisionings = [s for s in console_runner.inputs if s and "wasm_ro_role" in s]
        assert "PASSWORD" in provisionings[0]
        assert "PASSWORD" not in provisionings[1]
        # Still LOGIN, still re-granted, so new tables are covered.
        assert "LOGIN;" in provisionings[1]
        assert "GRANT SELECT ON ALL TABLES" in provisionings[1]
        assert _password_file(read_only_postgres).read_text() == first

    def test_a_role_from_an_earlier_build_gets_a_password(self, read_only_postgres, console_runner):
        # 2.0 builds created the role NOLOGIN and stored nothing. No stored
        # password is exactly that case: the role is given LOGIN and one.
        assert not _password_file(read_only_postgres).exists()

        read_only_postgres.execute_query(database="app", query="SELECT 1", read_only=True)

        provisioning = next(s for s in console_runner.inputs if s and "wasm_ro_role" in s)
        assert "LOGIN PASSWORD 'SCRAM-SHA-256$" in provisioning
        assert _password_file(read_only_postgres).exists()

    def test_a_stored_password_that_is_not_private_is_replaced(
        self, read_only_postgres, console_runner
    ):
        path = _password_file(read_only_postgres)
        path.parent.mkdir(parents=True)
        path.write_text("A" * 43 + "\n")
        path.chmod(0o644)

        read_only_postgres.execute_query(database="app", query="SELECT 1", read_only=True)

        index = console_runner.calls.index(_psql_console_calls(console_runner)[0])
        assert console_runner.envs[index]["PGPASSWORD"] != "A" * 43
        assert stat.S_IMODE(path.stat().st_mode) == 0o600

    def test_a_symlinked_password_file_is_never_followed(
        self, read_only_postgres, console_runner, tmp_path
    ):
        target = tmp_path / "elsewhere"
        target.write_text("B" * 43 + "\n")
        target.chmod(0o600)
        path = _password_file(read_only_postgres)
        path.parent.mkdir(parents=True)
        path.symlink_to(target)

        with pytest.raises(SecurityError):
            read_only_postgres.execute_query(database="app", query="SELECT 1", read_only=True)

        assert target.read_text() == "B" * 43 + "\n"
        assert not _psql_console_calls(console_runner)

    def test_the_configured_port_is_used(self, read_only_postgres, console_runner):
        read_only_postgres.config = StubConfig(
            {"databases": {"credentials": {"postgresql": {"port": 5433}}}}
        )

        read_only_postgres.execute_query(database="app", query="SELECT 1", read_only=True)

        (call,) = _psql_console_calls(console_runner)
        assert call[call.index("-p") + 1] == "5433"

    @pytest.mark.parametrize("structured", [True, False])
    def test_a_refused_sign_in_is_actionable_and_never_falls_back_to_the_superuser(
        self, read_only_postgres, console_runner, structured
    ):
        console_runner.sign_ins = [_failure(HBA_REFUSAL)]
        run = (
            read_only_postgres.execute_query_structured
            if structured
            else read_only_postgres.execute_query
        )

        with pytest.raises(DatabaseQueryError) as caught:
            run(database="app", query="SELECT n FROM t", read_only=True)

        error = caught.value
        assert "could not sign in" in error.message
        assert "wasm_ro_app" in error.message
        assert "host app wasm_ro_app 127.0.0.1/32 scram-sha-256" in error.details
        assert "pg_hba.conf" in error.details
        assert "never falls back to the superuser" in error.details
        # psql's own words, verbatim.
        assert error.output == HBA_REFUSAL.strip()
        # One attempt, and the statement never reached a superuser session.
        assert len(_psql_console_calls(console_runner)) == 1
        assert not any(
            call[: len(PSQL)] == PSQL and "SELECT n FROM t" in call for call in console_runner.calls
        )

    def test_a_server_not_listening_on_tcp_says_so(self, read_only_postgres, console_runner):
        console_runner.sign_ins = [
            _failure(
                'psql: error: connection to server at "127.0.0.1", port 5432 failed: '
                "Connection refused\n\tIs the server running on that host and accepting "
                "TCP/IP connections?\n"
            )
        ]

        with pytest.raises(DatabaseQueryError) as caught:
            read_only_postgres.execute_query(database="app", query="SELECT 1", read_only=True)

        assert "listen_addresses" in caught.value.details
        assert "host app wasm_ro_app 127.0.0.1/32 scram-sha-256" in caught.value.details

    def test_a_rejected_password_is_rotated_once_and_retried(
        self, read_only_postgres, console_runner
    ):
        console_runner.sign_ins = [_failure(PASSWORD_REJECTED)]

        success, _ = read_only_postgres.execute_query(
            database="app", query="SELECT 1", read_only=True
        )

        assert success
        provisionings = [s for s in console_runner.inputs if s and "wasm_ro_role" in s]
        assert len(provisionings) == 2
        assert all("PASSWORD 'SCRAM-SHA-256$" in p for p in provisionings)
        # Both attempts have the same argv; only the environment differs.
        attempts = [
            index
            for index, call in enumerate(console_runner.calls)
            if call[:1] == ("psql",) and "-c" in call
        ]
        assert len(attempts) == 2
        first, second = (console_runner.envs[index]["PGPASSWORD"] for index in attempts)
        assert first != second
        assert _password_file(read_only_postgres).read_text().strip() == second

    def test_a_password_rejected_twice_is_reported_not_retried_forever(
        self, read_only_postgres, console_runner
    ):
        console_runner.sign_ins = [_failure(PASSWORD_REJECTED), _failure(PASSWORD_REJECTED)]

        with pytest.raises(DatabaseQueryError, match="could not sign in"):
            read_only_postgres.execute_query(database="app", query="SELECT 1", read_only=True)

        assert len(_psql_console_calls(console_runner)) == 2

    def test_a_failing_statement_is_a_failed_query_not_a_sign_in_problem(
        self, read_only_postgres, console_runner
    ):
        console_runner.sign_ins = [
            _failure('ERROR:  permission denied to set role "postgres"\n', exit_code=1)
        ]

        with pytest.raises(DatabaseQueryError, match="Query failed") as caught:
            read_only_postgres.execute_query(database="app", query=ROLE_ESCAPE, read_only=True)

        assert "permission denied" in caught.value.details
        assert len(_psql_console_calls(console_runner)) == 1

    def test_the_password_is_never_logged(self, read_only_postgres, console_runner, caplog):
        console_runner.sign_ins = [_failure(PASSWORD_REJECTED)]

        with caplog.at_level(logging.DEBUG):
            read_only_postgres.execute_query(database="app", query="SELECT 1", read_only=True)

        password = _password_file(read_only_postgres).read_text().strip()
        assert all(password not in record.getMessage() for record in caplog.records)

    def test_write_mode_still_runs_as_the_superuser(self, read_only_postgres, console_runner):
        read_only_postgres.execute_query(database="app", query="DELETE FROM t", read_only=False)

        (call,) = _psql_console_calls(console_runner)
        assert call[: len(PSQL)] == PSQL
        assert "-U" not in call
        assert not _password_file(read_only_postgres).exists()
