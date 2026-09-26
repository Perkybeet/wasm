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

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from wasm.core.exceptions import DatabaseQueryError
from wasm.core.runner import CommandResult, FakeRunner
from wasm.managers.database.mysql import MySQLManager
from wasm.managers.database.postgres import PostgresManager

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
def postgres(runner: FakeRunner) -> PostgresManager:
    """
    Provide a PostgreSQL manager whose ``app`` database exists.

    Args:
        runner: The fake runner installed process-wide.

    Returns:
        The manager.
    """
    runner.script(list(PSQL), stdout="1\n")
    return PostgresManager()


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


def _psql_console_calls(runner: FakeRunner) -> list[tuple[str, ...]]:
    """
    Return the psql invocations that carried a console statement.

    Args:
        runner: The fake runner.

    Returns:
        Every psql call made with ``-c``.
    """
    return [call for call in runner.calls if call[: len(PSQL)] == PSQL and "-c" in call]


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

        psql_calls = [call for call in runner.calls if call[: len(PSQL)] == PSQL]
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
        assert _command_strings(call) == [
            "BEGIN READ ONLY",
            'SET ROLE "wasm_ro_app"',
            "SELECT n FROM t",
            "COMMIT",
        ]
        assert "-q" in call

    @pytest.mark.parametrize("structured", [True, False])
    def test_read_mode_refuses_a_second_statement_at_the_manager(
        self, postgres, runner, structured
    ):
        # "SELECT 1; COMMIT; RESET ROLE; COPY (SELECT 1) TO PROGRAM 'id'" is a
        # shell as the postgres account once the read-only transaction and the
        # role are closed. The API and the CLI refuse ';' too, but the
        # guarantee belongs to the one place every caller goes through.
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
