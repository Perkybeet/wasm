# Copyright (c) 2024-2026 Yago López Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
The ``wasm db`` command group, after the move to Click.

Three things are checked here, and they are the three ways a CLI migration
goes wrong:

- the published surface is intact, including the ``ls`` shorthand and the
  ``database`` spelling of the group;
- a missing or invalid argument is a usage error, not a traceback halfway
  through an operation;
- each command still reaches the manager it always reached, with the same
  arguments.
"""

from __future__ import annotations

import io
import json
from argparse import Namespace
from pathlib import Path
from typing import Any

import click
import pytest
import yaml
from click.testing import CliRunner

from wasm.cli import app as cli_app
from wasm.cli.commands import db as db_cli
from wasm.core.exceptions import WASMError
from wasm.core.logger import Logger

#: Every subcommand the frozen surface promises, with the arguments that make
#: it parse. Used to prove the command exists and does not explode on --help.
CONTRACT_COMMANDS = [
    "backup",
    "backups",
    "config",
    "connect",
    "connection-string",
    "create",
    "drop",
    "engines",
    "grant",
    "info",
    "install",
    "list",
    "query",
    "restart",
    "restore",
    "revoke",
    "start",
    "status",
    "stop",
    "uninstall",
    "user-create",
    "user-delete",
    "user-list",
]

#: Flags that belong to the root command. A subcommand that declares one of
#: these is the shadowing bug this migration exists to remove.
GLOBAL_FLAGS = {"--verbose", "-v", "--dry-run", "--json", "--no-color"}

#: Names those flags would arrive under if a command took them as parameters.
GLOBAL_PARAM_NAMES = {"verbose", "dry_run", "json_output", "no_color"}


class FakeBackupInfo:
    """The little the CLI reads back from a finished backup."""

    def __init__(self, path: Path):
        """
        Args:
            path: Where the backup was written.
        """
        self.path = path

    def to_dict(self) -> dict[str, Any]:
        """
        Returns:
            The payload the CLI prints the size from.
        """
        return {"path": str(self.path), "size_human": "1.2 MB"}


class FakeManager:
    """A database manager that records what the CLI asked it to do."""

    ENGINE_NAME = "postgresql"
    DISPLAY_NAME = "PostgreSQL"
    DEFAULT_PORT = 5432

    def __init__(
        self,
        *,
        installed: bool = True,
        running: bool = True,
        engine: str | None = None,
        display_name: str | None = None,
    ):
        """
        Args:
            installed: Report the engine as installed.
            running: Report the engine's service as active.
            engine: Override the engine name, to stand in for another backend.
            display_name: Override the name shown to the operator.
        """
        if engine:
            self.ENGINE_NAME = engine
        if display_name:
            self.DISPLAY_NAME = display_name
        self._installed = installed
        self._running = running
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    def _record(self, name: str, *args: Any, **kwargs: Any) -> None:
        self.calls.append((name, args, kwargs))

    def called(self, name: str) -> dict[str, Any] | None:
        """
        Return the keyword arguments of the first call to a method.

        Args:
            name: Method name.

        Returns:
            The keyword arguments, or None when it was never called.
        """
        for call, _args, kwargs in self.calls:
            if call == name:
                return kwargs
        return None

    def names_called(self) -> list[str]:
        """
        Returns:
            The methods that were called, in order.
        """
        return [call for call, _args, _kwargs in self.calls]

    def is_installed(self) -> bool:
        return self._installed

    def is_running(self) -> bool:
        return self._running

    def get_version(self) -> str:
        return "16.2"

    def get_status(self) -> dict[str, Any]:
        return {
            "installed": self._installed,
            "running": self._running,
            "version": "16.2",
            "display_name": self.DISPLAY_NAME,
            "port": self.DEFAULT_PORT,
            "service": "postgresql",
        }

    def drop_database(self, name: str, force: bool = False) -> None:
        self._record("drop_database", name, force=force)

    def grant_privileges(
        self,
        username: str,
        database: str,
        privileges: list[str] | None = None,
        host: str = "localhost",
    ) -> None:
        self._record("grant_privileges", username, database, privileges=privileges, host=host)

    def revoke_privileges(
        self,
        username: str,
        database: str,
        privileges: list[str] | None = None,
        host: str = "localhost",
    ) -> None:
        self._record("revoke_privileges", username, database, privileges=privileges, host=host)

    def drop_user(self, username: str, host: str = "localhost") -> None:
        self._record("drop_user", username, host=host)

    def execute_query(self, database: str, query: str, **kwargs: Any) -> tuple[bool, str]:
        self._record("execute_query", database, query, **kwargs)
        return True, "one row"

    def get_connection_string(
        self, database: str, username: str, password: str, host: str = "localhost"
    ) -> str:
        self._record("get_connection_string", database, username, password=password, host=host)
        return f"postgresql://{username}:{password}@{host}/{database}"

    def get_interactive_command(
        self, database: str | None = None, username: str | None = None
    ) -> list[str]:
        self._record("get_interactive_command", database=database, username=username)
        return ["sudo", "-u", "postgres", "psql"]

    def restore(self, database: str, backup_path: Path, drop_existing: bool = False) -> None:
        self._record("restore", database, backup_path=backup_path, drop_existing=drop_existing)

    def backup(
        self, database: str, output_path: Path | None = None, compress: bool = True
    ) -> FakeBackupInfo:
        self._record("backup", database, output_path=output_path, compress=compress)
        return FakeBackupInfo(output_path or Path("/var/backups/wasm/databases/shop.sql"))


def _guard_outcome(guard: Any, statement: str) -> str:
    """
    Run a single-statement guard and describe what it did.

    Args:
        guard: The function under test.
        statement: The text to hand it.

    Returns:
        The accepted statement, or "refused" when it raised.
    """
    try:
        return guard(statement)
    except WASMError:
        return "refused"


@pytest.fixture
def cli_runner() -> CliRunner:
    """
    Returns:
        A Click test runner.
    """
    return CliRunner()


@pytest.fixture
def logged(monkeypatch: pytest.MonkeyPatch) -> io.StringIO:
    """
    Capture what a command reports through the logger.

    :class:`~wasm.core.logger.Logger` binds ``sys.stdout`` as a default
    argument at import time, so neither the CliRunner nor capsys ever sees it.

    Args:
        monkeypatch: Patching helper, scoped to the test.

    Returns:
        The buffer the logger writes to.
    """
    buffer = io.StringIO()

    def factory(**kwargs: Any) -> Logger:
        return Logger(stream=buffer, **kwargs)

    monkeypatch.setattr(cli_app, "Logger", factory)
    monkeypatch.setattr(db_cli, "Logger", factory)
    return buffer


@pytest.fixture
def forgotten(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    """
    Stand in for the SQLite store and record what it was told to forget.

    Args:
        monkeypatch: Patching helper, scoped to the test.

    Returns:
        The (database, engine) pairs the CLI deleted from the store.
    """
    deleted: list[tuple[str, str]] = []

    class FakeStore:
        def delete_database(self, name: str, engine: str) -> None:
            deleted.append((name, engine))

    monkeypatch.setattr("wasm.core.store.get_store", lambda: FakeStore())
    return deleted


@pytest.fixture
def isolated_panel_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """
    Point the layered configuration at a per-test file.

    ``panel_url`` reads ``web.*`` through the Config singleton, which would
    otherwise read the developer's real ``/etc/wasm/config.yaml`` and make
    these tests depend on the machine they run on. The self-signed TLS pair
    path is pinned the same way, so a machine that has actually run
    ``wasm web start --self-signed`` does not turn "http" into "https" under
    a test.

    Args:
        tmp_path: Per-test temporary directory.
        monkeypatch: Patching helper, scoped to the test.

    Yields:
        The configuration file the test may write.
    """
    from wasm.cli.commands import web as web_module
    from wasm.core import config as config_module

    path = tmp_path / "config.yaml"
    monkeypatch.setattr(config_module, "DEFAULT_CONFIG_PATH", path)
    monkeypatch.setattr(web_module, "PANEL_TLS_CERT", tmp_path / "panel-tls" / "panel.crt")
    monkeypatch.setattr(web_module, "PANEL_TLS_KEY", tmp_path / "panel-tls" / "panel.key")
    config_module.Config.reset_instance()
    yield path
    config_module.Config.reset_instance()


def _configure_panel(path: Path, **settings: Any) -> None:
    """
    Declare a configured, reachable panel in the isolated config file.

    Args:
        path: The isolated configuration file.
        **settings: Overrides for the ``web`` section; ``enabled``, ``host``
            and ``port`` fall back to a plain local panel when not given.
    """
    from wasm.core.config import Config

    settings.setdefault("enabled", True)
    settings.setdefault("host", "127.0.0.1")
    settings.setdefault("port", 8080)
    path.write_text(yaml.safe_dump({"web": settings}), encoding="utf-8")
    Config.reset_instance()


@pytest.fixture
def manager(monkeypatch: pytest.MonkeyPatch) -> FakeManager:
    """
    Put a recording manager behind every engine name.

    Args:
        monkeypatch: Patching helper, scoped to the test.

    Returns:
        The manager the CLI will be handed.
    """
    fake = FakeManager()
    monkeypatch.setattr(db_cli, "get_db_manager", lambda engine, verbose=False: fake)
    return fake


class TestSurface:
    """The published command surface still exists."""

    @pytest.mark.parametrize("name", CONTRACT_COMMANDS)
    def test_every_contract_command_answers_help(self, cli_runner: CliRunner, name: str):
        result = cli_runner.invoke(cli_app.cli, ["db", name, "--help"])

        assert result.exit_code == 0, result.output
        assert f"db {name}" in result.output

    def test_group_answers_help(self, cli_runner: CliRunner):
        result = cli_runner.invoke(cli_app.cli, ["db", "--help"])

        assert result.exit_code == 0
        assert "user-create" in result.output

    def test_database_is_still_a_spelling_of_db(self, cli_runner: CliRunner):
        result = cli_runner.invoke(cli_app.cli, ["database", "--help"])

        assert result.exit_code == 0
        assert "user-create" in result.output

    def test_ls_is_still_a_spelling_of_list(self, cli_runner: CliRunner):
        aliased = cli_runner.invoke(cli_app.cli, ["db", "ls", "--help"])
        spelt_out = cli_runner.invoke(cli_app.cli, ["db", "list", "--help"])

        assert aliased.exit_code == 0
        # Only the usage line differs: it echoes the name that was typed.
        assert aliased.output.partition("\n")[2] == spelt_out.output.partition("\n")[2]

    def test_an_unknown_subcommand_is_a_usage_error(self, cli_runner: CliRunner):
        result = cli_runner.invoke(cli_app.cli, ["db", "nonesuch"])

        assert result.exit_code == 2

    def test_no_command_redeclares_a_global_flag(self):
        for name, command in db_cli.cli.commands.items():
            declared = {
                opt
                for param in command.params
                if isinstance(param, click.Option)
                for opt in param.opts + param.secondary_opts
            }
            assert not declared & GLOBAL_FLAGS, (
                f"'db {name}' redeclares {sorted(declared & GLOBAL_FLAGS)}. "
                "Global state belongs to wasm.cli.app.Context."
            )

            taken = {param.name for param in command.params}
            assert not taken & GLOBAL_PARAM_NAMES, (
                f"'db {name}' takes {sorted(taken & GLOBAL_PARAM_NAMES)} as a parameter, "
                "which is how a subcommand used to overwrite what the user asked for."
            )


class TestUsageErrors:
    """A missing or invalid argument costs a usage error, not an operation."""

    @pytest.mark.parametrize(
        "argv",
        [
            ["db", "install"],
            ["db", "create", "shop"],
            ["db", "drop", "shop"],
            ["db", "info", "shop"],
            ["db", "user-create", "app"],
            ["db", "user-list"],
            ["db", "grant", "app"],
            ["db", "query", "shop"],
            ["db", "connect"],
            ["db", "connection-string", "shop"],
            ["db", "config"],
            ["db", "backup", "shop"],
            ["db", "restore", "shop"],
        ],
    )
    def test_missing_arguments_are_usage_errors(self, cli_runner: CliRunner, argv: list[str]):
        result = cli_runner.invoke(cli_app.cli, argv)

        assert result.exit_code == 2, result.output
        assert "Traceback" not in result.output

    def test_an_unknown_engine_is_rejected_before_anything_runs(
        self, cli_runner: CliRunner, runner
    ):
        result = cli_runner.invoke(cli_app.cli, ["db", "install", "orable"])

        assert result.exit_code == 2
        assert "unknown database engine" in result.output
        assert runner.calls == []

    def test_an_unknown_engine_option_is_rejected(self, cli_runner: CliRunner, runner):
        result = cli_runner.invoke(cli_app.cli, ["db", "drop", "shop", "-e", "orable", "--force"])

        assert result.exit_code == 2
        assert runner.calls == []

    def test_restoring_from_a_missing_file_is_a_usage_error(self, cli_runner: CliRunner, runner):
        result = cli_runner.invoke(
            cli_app.cli,
            ["db", "restore", "shop", "/nowhere/shop.sql.gz", "-e", "postgresql", "--force"],
        )

        assert result.exit_code == 2
        assert runner.calls == []


class TestEngineCommands:
    """Engine commands reach systemd through the runner, with the exact argv."""

    def test_start_runs_systemctl_start(self, cli_runner: CliRunner, runner):
        result = cli_runner.invoke(cli_app.cli, ["db", "start", "postgresql"])

        assert result.exit_code == 0, result.output
        assert ("systemctl", "start", "postgresql") in runner.calls

    def test_stop_runs_systemctl_stop(self, cli_runner: CliRunner, runner):
        runner.script(["systemctl", "is-active", "postgresql"], stdout="active")

        result = cli_runner.invoke(cli_app.cli, ["db", "stop", "postgresql"])

        assert result.exit_code == 0, result.output
        assert ("systemctl", "stop", "postgresql") in runner.calls

    def test_restart_runs_systemctl_restart(self, cli_runner: CliRunner, runner):
        result = cli_runner.invoke(cli_app.cli, ["db", "restart", "postgresql"])

        assert result.exit_code == 0, result.output
        assert ("systemctl", "restart", "postgresql") in runner.calls

    def test_stopping_a_stopped_engine_touches_nothing(self, cli_runner: CliRunner, runner):
        result = cli_runner.invoke(cli_app.cli, ["db", "stop", "postgresql"])

        assert result.exit_code == 0
        assert ("systemctl", "stop", "postgresql") not in runner.calls

    def test_status_reports_json_when_the_root_asks_for_it(
        self, cli_runner: CliRunner, manager: FakeManager
    ):
        result = cli_runner.invoke(cli_app.cli, ["--json", "db", "status", "postgresql"])

        assert result.exit_code == 0, result.output
        assert json.loads(result.output)[0]["display_name"] == "PostgreSQL"


class TestOpenFlag:
    """``--open`` deep-links ``db list`` into the panel."""

    def test_prints_the_configured_panel_url_without_a_display(
        self,
        cli_runner: CliRunner,
        manager: FakeManager,
        logged: io.StringIO,
        isolated_panel_config: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """``--open`` prints the panel URL and never touches xdg-open without a display."""
        monkeypatch.delenv("DISPLAY", raising=False)
        monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
        _configure_panel(isolated_panel_config)

        result = cli_runner.invoke(cli_app.cli, ["db", "list", "--engine", "postgresql", "--open"])

        assert result.exit_code == 0, result.output
        output = logged.getvalue()
        assert "http://127.0.0.1:8080/databases" in output

    def test_launches_xdg_open_when_a_display_is_present(
        self,
        cli_runner: CliRunner,
        manager: FakeManager,
        logged: io.StringIO,
        isolated_panel_config: Path,
        runner,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """With a display available, ``--open`` hands the URL to xdg-open."""
        monkeypatch.setenv("DISPLAY", ":0")
        _configure_panel(isolated_panel_config)

        result = cli_runner.invoke(cli_app.cli, ["db", "list", "--engine", "postgresql", "--open"])

        assert result.exit_code == 0, result.output
        assert runner.calls_to("xdg-open") == [("xdg-open", "http://127.0.0.1:8080/databases")]

    def test_without_a_configured_panel_warns_and_exits_clean(
        self,
        cli_runner: CliRunner,
        manager: FakeManager,
        logged: io.StringIO,
        isolated_panel_config: Path,
        runner,
    ):
        """The panel is off by default, so ``--open`` warns instead of guessing a URL."""
        result = cli_runner.invoke(cli_app.cli, ["db", "list", "--engine", "postgresql", "--open"])

        assert result.exit_code == 0, result.output
        assert "not configured" in logged.getvalue()
        assert not runner.calls_to("xdg-open")


class TestDestructiveCommands:
    """Nothing irreversible happens without a question that names the resource."""

    def test_drop_asks_before_deleting_and_names_the_database(
        self, cli_runner: CliRunner, manager: FakeManager
    ):
        result = cli_runner.invoke(
            cli_app.cli, ["db", "drop", "shop", "-e", "postgresql"], input="n\n"
        )

        assert result.exit_code == 0
        assert "shop" in result.output
        assert "cannot be undone" in result.output
        assert manager.names_called() == []

    def test_drop_proceeds_when_the_operator_agrees(
        self, cli_runner: CliRunner, manager: FakeManager, forgotten: list[tuple[str, str]]
    ):
        result = cli_runner.invoke(
            cli_app.cli, ["db", "drop", "shop", "-e", "postgresql"], input="y\n"
        )

        assert result.exit_code == 0, result.output
        assert manager.called("drop_database") is not None
        assert forgotten == [("shop", "postgresql")]

    def test_force_skips_the_question(
        self, cli_runner: CliRunner, manager: FakeManager, forgotten: list[tuple[str, str]]
    ):
        result = cli_runner.invoke(
            cli_app.cli, ["db", "drop", "shop", "-e", "postgresql", "--force"]
        )

        assert result.exit_code == 0, result.output
        assert manager.called("drop_database") == {"force": True}

    def test_user_delete_asks_before_deleting(self, cli_runner: CliRunner, manager: FakeManager):
        result = cli_runner.invoke(
            cli_app.cli, ["db", "user-delete", "app", "-e", "postgresql"], input="n\n"
        )

        assert result.exit_code == 0
        assert "app" in result.output
        assert manager.names_called() == []

    def test_restore_names_the_file_and_the_database(
        self, cli_runner: CliRunner, manager: FakeManager, tmp_path: Path
    ):
        dump = tmp_path / "shop.sql"
        dump.write_text("-- dump")

        result = cli_runner.invoke(
            cli_app.cli,
            ["db", "restore", "shop", str(dump), "-e", "postgresql", "--drop"],
            input="n\n",
        )

        assert result.exit_code == 0
        assert "shop" in result.output
        assert str(dump) in result.output
        assert manager.names_called() == []


class TestPrivileges:
    """The comma-separated list is split here; the whitelist stays in the manager."""

    def test_grant_passes_the_privileges_as_a_list(
        self, cli_runner: CliRunner, manager: FakeManager
    ):
        result = cli_runner.invoke(
            cli_app.cli,
            ["db", "grant", "app", "shop", "-e", "postgresql", "--privileges", "SELECT, INSERT"],
        )

        assert result.exit_code == 0, result.output
        assert manager.called("grant_privileges") == {
            "privileges": ["SELECT", "INSERT"],
            "host": "localhost",
        }

    def test_no_privileges_leaves_the_default_to_the_manager(
        self, cli_runner: CliRunner, manager: FakeManager
    ):
        result = cli_runner.invoke(cli_app.cli, ["db", "revoke", "app", "shop", "-e", "postgresql"])

        assert result.exit_code == 0, result.output
        assert manager.called("revoke_privileges") == {"privileges": None, "host": "localhost"}


class TestBackup:
    """The backup options reach the manager as it expects them."""

    def test_backup_passes_the_path_and_compression(
        self, cli_runner: CliRunner, manager: FakeManager, tmp_path: Path
    ):
        destination = tmp_path / "shop.sql"

        result = cli_runner.invoke(
            cli_app.cli,
            ["db", "backup", "shop", "-e", "postgresql", "-o", str(destination), "--no-compress"],
        )

        assert result.exit_code == 0, result.output
        assert manager.called("backup") == {"output_path": destination, "compress": False}

    def test_backup_compresses_by_default(self, cli_runner: CliRunner, manager: FakeManager):
        result = cli_runner.invoke(cli_app.cli, ["db", "backup", "shop", "-e", "postgresql"])

        assert result.exit_code == 0, result.output
        assert manager.called("backup") == {"output_path": None, "compress": True}


class TestConfig:
    """Credentials are written through the configuration object, by key."""

    def test_config_stores_the_credentials_it_was_given(
        self, cli_runner: CliRunner, monkeypatch: pytest.MonkeyPatch
    ):
        written: dict[str, Any] = {}

        class FakeConfig:
            def set(self, key: str, value: Any) -> None:
                written[key] = value

            def save(self) -> bool:
                written["saved"] = True
                return True

        monkeypatch.setattr(db_cli, "Config", FakeConfig)

        result = cli_runner.invoke(
            cli_app.cli, ["db", "config", "-e", "mysql", "-u", "root", "-p", "hunter2"]
        )

        assert result.exit_code == 0, result.output
        assert written == {
            "databases.credentials.mysql.user": "root",
            "databases.credentials.mysql.password": "hunter2",
            "saved": True,
        }


class TestQuery:
    """The console is read-only until the operator says otherwise."""

    def test_a_statement_runs_read_only_by_default(
        self, cli_runner: CliRunner, manager: FakeManager
    ):
        result = cli_runner.invoke(
            cli_app.cli, ["db", "query", "shop", "SELECT 1", "-e", "postgresql"]
        )

        assert result.exit_code == 0, result.output
        assert manager.called("execute_query") == {"read_only": True}
        assert "one row" in result.output

    def test_write_opts_out_of_the_read_only_transaction(
        self, cli_runner: CliRunner, manager: FakeManager
    ):
        result = cli_runner.invoke(
            cli_app.cli, ["db", "query", "shop", "DELETE FROM t", "-e", "postgresql", "--write"]
        )

        assert result.exit_code == 0, result.output
        assert manager.called("execute_query") == {"read_only": False}

    def test_a_second_statement_cannot_ride_along_in_read_only_mode(
        self, cli_runner: CliRunner, manager: FakeManager, logged: io.StringIO
    ):
        """
        The reported bypass: MySQL commits the read-only transaction on a ';'.

        The manager wraps what it is given in START TRANSACTION READ ONLY, so
        'SELECT 1; COMMIT; DROP TABLE users' arrives at the engine with the
        COMMIT in the middle, and everything after it runs with write access.
        """
        result = cli_runner.invoke(
            cli_app.cli,
            ["db", "query", "shop", "SELECT 1; COMMIT; DROP TABLE users", "-e", "postgresql"],
        )

        assert result.exit_code == 1
        assert manager.names_called() == []
        assert "one statement" in logged.getvalue()
        assert "--write" in logged.getvalue()

    def test_a_trailing_semicolon_is_not_a_second_statement(
        self, cli_runner: CliRunner, manager: FakeManager
    ):
        """Typing SQL the way SQL is written must keep working."""
        result = cli_runner.invoke(
            cli_app.cli, ["db", "query", "shop", "  SELECT 1;  ", "-e", "postgresql"]
        )

        assert result.exit_code == 0, result.output
        sent = next(args for name, args, _ in manager.calls if name == "execute_query")
        assert sent == ("shop", "SELECT 1")

    def test_a_semicolon_inside_a_literal_is_refused_too(
        self, cli_runner: CliRunner, manager: FakeManager
    ):
        """
        Deliberately conservative.

        Telling a ';' that ends a statement from one inside a literal needs a
        parser for each engine's grammar. Refusing the rare legitimate query is
        recoverable with --write; getting it wrong the other way is not.
        """
        result = cli_runner.invoke(
            cli_app.cli, ["db", "query", "shop", "SELECT ';'", "-e", "postgresql"]
        )

        assert result.exit_code == 1
        assert manager.names_called() == []

    def test_write_mode_may_still_send_a_batch(self, cli_runner: CliRunner, manager: FakeManager):
        """--write is the operator accepting that the statements change data."""
        result = cli_runner.invoke(
            cli_app.cli,
            [
                "db",
                "query",
                "shop",
                "INSERT INTO t VALUES (1); DELETE FROM u",
                "-e",
                "postgresql",
                "--write",
            ],
        )

        assert result.exit_code == 0, result.output
        assert manager.called("execute_query") == {"read_only": False}

    def test_an_empty_statement_is_refused_before_the_engine_is_touched(
        self, cli_runner: CliRunner, manager: FakeManager, logged: io.StringIO
    ):
        """A blank argument is a mistake, not a query."""
        result = cli_runner.invoke(cli_app.cli, ["db", "query", "shop", "   ", "-e", "postgresql"])

        assert result.exit_code == 1
        assert manager.names_called() == []
        assert "Empty statement" in logged.getvalue()

    def test_an_oversized_statement_is_refused(
        self, cli_runner: CliRunner, manager: FakeManager, logged: io.StringIO
    ):
        """The console is not a file upload, and says where to put one instead."""
        oversized = "SELECT " + "a" * db_cli.MAX_QUERY_LENGTH

        result = cli_runner.invoke(
            cli_app.cli, ["db", "query", "shop", oversized, "-e", "postgresql"]
        )

        assert result.exit_code == 1
        assert manager.names_called() == []
        assert "wasm db connect" in logged.getvalue()

    def test_the_guard_matches_the_one_the_panel_applies(self):
        """
        The CLI and the web console have to agree on what one statement is.

        They are two front doors to the same root-level console; a rule that
        holds at one of them only is the rule not holding.
        """
        from wasm.web.api import databases as databases_api

        for statement in ("SELECT 1;", "  SELECT 1  ", "SELECT ';'", "SELECT 1; DROP TABLE t"):
            cli_result = _guard_outcome(db_cli._single_statement, statement)
            api_result = _guard_outcome(databases_api._reject_multiple_statements, statement)
            assert cli_result == api_result, statement

    def test_an_engine_that_cannot_be_held_read_only_refuses_to_pretend(
        self, cli_runner: CliRunner, monkeypatch: pytest.MonkeyPatch, logged: io.StringIO
    ):
        fake = FakeManager(engine="redis", display_name="Redis")
        monkeypatch.setattr(db_cli, "get_db_manager", lambda engine, verbose=False: fake)

        result = cli_runner.invoke(cli_app.cli, ["db", "query", "0", "KEYS *", "-e", "redis"])

        assert result.exit_code == 1
        # Redis has no read-only transaction, so the manager would accept the
        # flag and run the command anyway. Refusing is the honest answer.
        assert "--write" in logged.getvalue()
        assert fake.names_called() == []


class TestConnect:
    """The interactive client is handed the terminal, and only for real runs."""

    def test_connect_execs_the_client_the_manager_builds(
        self, cli_runner: CliRunner, manager: FakeManager, monkeypatch: pytest.MonkeyPatch
    ):
        opened: list[list[str]] = []
        monkeypatch.setattr(db_cli, "_open_client", lambda argv: opened.append(list(argv)))

        result = cli_runner.invoke(cli_app.cli, ["db", "connect", "-e", "postgresql", "-d", "shop"])

        assert result.exit_code == 0, result.output
        assert opened == [["sudo", "-u", "postgres", "psql"]]
        assert manager.called("get_interactive_command") == {
            "database": "shop",
            "username": None,
        }

    def test_a_dry_run_reports_the_client_instead_of_running_it(
        self,
        cli_runner: CliRunner,
        manager: FakeManager,
        monkeypatch: pytest.MonkeyPatch,
        logged: io.StringIO,
        runner,
    ):
        opened: list[list[str]] = []
        monkeypatch.setattr(db_cli, "_open_client", lambda argv: opened.append(list(argv)))

        result = cli_runner.invoke(cli_app.cli, ["--dry-run", "db", "connect", "-e", "postgresql"])

        assert result.exit_code == 0
        assert opened == []
        assert "would run: sudo -u postgres psql" in logged.getvalue()


class TestConnectionString:
    """A connection string is printed with a placeholder, never an empty password."""

    def test_a_missing_password_becomes_a_placeholder(
        self, cli_runner: CliRunner, manager: FakeManager
    ):
        result = cli_runner.invoke(
            cli_app.cli, ["db", "connection-string", "shop", "app", "-e", "postgresql"]
        )

        assert result.exit_code == 0, result.output
        assert db_cli.PASSWORD_PLACEHOLDER in result.output

    def test_a_given_password_is_used(self, cli_runner: CliRunner, manager: FakeManager):
        result = cli_runner.invoke(
            cli_app.cli,
            ["db", "connection-string", "shop", "app", "-e", "postgresql", "-p", "hunter2"],
        )

        assert result.exit_code == 0, result.output
        assert "hunter2" in result.output


class TestLegacyFrontEnd:
    """The argparse path still works, and shares the implementation."""

    def test_handle_db_routes_to_the_same_function(
        self, manager: FakeManager, forgotten: list[tuple[str, str]]
    ):
        args = Namespace(action="drop", name="shop", engine="postgresql", force=True, verbose=False)

        assert db_cli.handle_db(args) == 0
        assert manager.called("drop_database") == {"force": True}

    def test_an_unknown_action_is_reported(self):
        assert db_cli.handle_db(Namespace(action="teleport", verbose=False)) == 1

    def test_a_missing_action_is_reported(self):
        assert db_cli.handle_db(Namespace(verbose=False)) == 1

    def test_the_legacy_query_keeps_its_write_behaviour(self, manager: FakeManager):
        args = Namespace(
            action="query",
            database="shop",
            query="DELETE FROM t",
            engine="postgresql",
            verbose=False,
        )

        assert db_cli.handle_db(args) == 0
        # The old parser has no way to ask for a write, so making it read-only
        # would break every script that uses it.
        assert manager.called("execute_query") == {"read_only": False}
