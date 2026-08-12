# Copyright (c) 2024-2025 Yago López Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
Tests for ``wasm store``.

The store is the inventory WASM answers every other question from, so what is
pinned here is that each subcommand still resolves, that a failure to open the
database is an actionable error rather than a traceback, and that a dump which
can contain service credentials is never written world readable.
"""

from __future__ import annotations

import io
import json
import sqlite3
import stat
from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import click
import pytest
from click.testing import CliRunner

from wasm.cli import app as app_module
from wasm.cli.commands import store as store_module
from wasm.core.exceptions import ConfigError
from wasm.core.logger import Logger

#: Flags the root group owns. A subcommand that declares one of them again is
#: the shadowing defect the Click migration exists to remove.
GLOBAL_FLAGS = frozenset({"-v", "--verbose", "--dry-run", "--json", "--no-color"})

#: Every command the frozen surface promises under ``store``.
CONTRACT_NAMES = ["init", "stats", "import", "export", "sync", "path"]

STATISTICS: dict[str, Any] = {
    "total_apps": 2,
    "running_apps": 1,
    "total_sites": 3,
    "total_services": 4,
    "total_databases": 5,
    "apps_by_type": {"nextjs": 2},
    "databases_by_engine": {"postgresql": 1},
}


class _Record:
    """A stored row that knows how to serialise itself."""

    def __init__(self, **fields: Any) -> None:
        """
        Args:
            **fields: The row's columns.
        """
        self._fields = fields
        for key, value in fields.items():
            setattr(self, key, value)

    def to_dict(self) -> dict[str, Any]:
        """
        Serialise the row.

        Returns:
            The row's columns.
        """
        return dict(self._fields)


class _FakeStore:
    """A store that answers from memory instead of SQLite."""

    def __init__(self, db_path: Path) -> None:
        """
        Args:
            db_path: Where the real store would keep its file.
        """
        self.db_path = db_path
        self.services: list[Any] = []
        self.apps: list[Any] = []
        self.status_updates: list[tuple[str, bool, bool]] = []
        self.app_updates: list[tuple[str, str]] = []

    def get_statistics(self) -> dict[str, Any]:
        """
        Returns:
            Fixed counts.
        """
        return dict(STATISTICS)

    def list_apps(self) -> list[Any]:
        """
        Returns:
            Every recorded application.
        """
        return self.apps

    def list_sites(self) -> list[Any]:
        """
        Returns:
            Every recorded site.
        """
        return []

    def list_services(self) -> list[Any]:
        """
        Returns:
            Every recorded service.
        """
        return self.services

    def list_databases(self) -> list[Any]:
        """
        Returns:
            Every recorded database.
        """
        return []

    def get_app_by_id(self, app_id: int) -> Any:
        """
        Args:
            app_id: Primary key.

        Returns:
            The application, or None.
        """
        return next((app for app in self.apps if app.id == app_id), None)

    def update_service_status(self, name: str, active: bool, enabled: bool) -> None:
        """
        Args:
            name: Service name.
            active: Whether systemd reports it running.
            enabled: Whether systemd reports it enabled.
        """
        self.status_updates.append((name, active, enabled))

    def update_app_status(self, domain: str, status: str) -> None:
        """
        Args:
            domain: Application domain.
            status: New status.
        """
        self.app_updates.append((domain, status))


@pytest.fixture
def cli_runner() -> CliRunner:
    """
    Provide a Click test runner.

    Returns:
        A runner that invokes the real root group.
    """
    return CliRunner()


@pytest.fixture
def logged(monkeypatch: pytest.MonkeyPatch) -> io.StringIO:
    """
    Capture what the commands print.

    :class:`~wasm.core.logger.Logger` binds ``sys.stdout`` as a default argument
    at import time, so pytest's own capture never sees it. Handing the module a
    logger bound to a buffer is what makes the output assertable.

    Args:
        monkeypatch: Patching helper, scoped to the test.

    Returns:
        The buffer every Logger built by the store module writes to.
    """
    buffer = io.StringIO()
    monkeypatch.setattr(store_module, "Logger", lambda **kwargs: Logger(stream=buffer, **kwargs))
    return buffer


@pytest.fixture
def fake_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _FakeStore:
    """
    Install a store that never touches SQLite.

    Args:
        tmp_path: Per-test temporary directory.
        monkeypatch: Patching helper, scoped to the test.

    Returns:
        The store every command in the test will see.
    """
    import wasm.core.store as real_store

    store = _FakeStore(tmp_path / "wasm.db")
    monkeypatch.setattr(real_store, "get_store", lambda *args, **kwargs: store)
    return store


# ---------------------------------------------------------------------------
# Command surface
# ---------------------------------------------------------------------------


def test_the_group_answers_help(cli_runner: CliRunner) -> None:
    """``wasm store --help`` lists every action."""
    result = cli_runner.invoke(app_module.cli, ["store", "--help"])

    assert result.exit_code == 0, result.output
    for name in CONTRACT_NAMES:
        assert name in result.output


@pytest.mark.parametrize("name", CONTRACT_NAMES)
def test_every_frozen_command_answers_help(cli_runner: CliRunner, name: str) -> None:
    """Each command in the frozen surface still resolves."""
    result = cli_runner.invoke(app_module.cli, ["store", name, "--help"])

    assert result.exit_code == 0, result.output


def test_an_unknown_action_is_a_usage_error(cli_runner: CliRunner) -> None:
    """A typo names itself instead of reporting nothing."""
    result = cli_runner.invoke(app_module.cli, ["store", "statz"])

    assert result.exit_code == 2
    assert "statz" in result.output


def test_export_rejects_a_directory_before_touching_anything(
    cli_runner: CliRunner, fake_store: _FakeStore, tmp_path: Path
) -> None:
    """An output path that is a directory fails validation, not the write."""
    target = tmp_path / "adirectory"
    target.mkdir()

    result = cli_runner.invoke(app_module.cli, ["store", "export", "-o", str(target)])

    assert result.exit_code == 2
    assert list(target.iterdir()) == []


def test_no_store_command_redeclares_a_global_flag() -> None:
    """
    Regression guard for the defect that motivated the migration.

    ``--dry-run`` declared on the root parser and again on a subparser meant the
    subparser default overwrote what the user asked for.
    """
    offenders: list[str] = []
    group = store_module.cli

    for name, command in [("store", group), *group.commands.items()]:
        for param in command.params:
            if not isinstance(param, click.Option):
                continue
            clash = GLOBAL_FLAGS.intersection(param.opts + param.secondary_opts)
            if clash:
                offenders.append(f"{name}: {sorted(clash)}")

    assert offenders == []


# ---------------------------------------------------------------------------
# Behaviour
# ---------------------------------------------------------------------------


def test_path_prints_the_database_location(cli_runner: CliRunner, fake_store: _FakeStore) -> None:
    """The path is on stdout alone, so it can be used in a shell substitution."""
    result = cli_runner.invoke(app_module.cli, ["store", "path"])

    assert result.exit_code == 0, result.output
    assert result.output.strip() == str(fake_store.db_path)


def test_stats_reports_the_counts(fake_store: _FakeStore, logged: io.StringIO) -> None:
    """Every counter the store exposes reaches the report."""
    assert store_module._store_stats(json_output=False, verbose=False) == 0

    output = logged.getvalue()
    assert str(fake_store.db_path) in output
    assert "nextjs" in output
    assert "postgresql" in output


def test_stats_honours_the_global_json_flag(cli_runner: CliRunner, fake_store: _FakeStore) -> None:
    """``wasm --json store stats`` emits the payload and nothing else."""
    result = cli_runner.invoke(app_module.cli, ["--json", "store", "stats"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == STATISTICS


def test_stats_turns_an_unreadable_database_into_advice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing database says how to create one instead of raising sqlite3."""
    import wasm.core.store as real_store

    def _broken(*args: Any, **kwargs: Any) -> Any:
        raise sqlite3.OperationalError("unable to open database file")

    monkeypatch.setattr(real_store, "get_store", _broken)

    with pytest.raises(ConfigError) as excinfo:
        store_module._store_stats(json_output=False, verbose=False)

    assert "wasm store init" in excinfo.value.details


def test_init_turns_a_broken_database_into_an_exit_code(
    cli_runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The operator gets an actionable message, not a stack trace."""
    import wasm.core.store as real_store

    monkeypatch.setattr(real_store.WASMStore, "reset_instance", classmethod(lambda cls: None))

    def _broken(*args: Any, **kwargs: Any) -> Any:
        raise sqlite3.OperationalError("unable to open database file")

    monkeypatch.setattr(real_store, "get_store", _broken)

    result = cli_runner.invoke(app_module.cli, ["store", "init"])

    assert result.exit_code == 1
    assert isinstance(result.exception, ConfigError)
    assert "write" in result.exception.details


def test_export_writes_an_owner_only_file(
    fake_store: _FakeStore, tmp_path: Path, logged: io.StringIO
) -> None:
    """A dump carries the environment a service runs with, credentials included."""
    fake_store.apps = [_Record(domain="example.com", app_type="nextjs")]
    fake_store.services = [_Record(name="example-com", environment={"DB_PASSWORD": "hunter2"})]
    target = tmp_path / "dump.json"

    assert store_module._store_export(str(target), verbose=False) == 0

    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    data = json.loads(target.read_text(encoding="utf-8"))
    assert data["apps"][0]["domain"] == "example.com"
    assert data["statistics"] == STATISTICS


def test_export_without_a_destination_goes_to_stdout(
    cli_runner: CliRunner, fake_store: _FakeStore
) -> None:
    """Piping the dump into another tool must keep working."""
    fake_store.apps = [_Record(domain="example.com")]

    result = cli_runner.invoke(app_module.cli, ["store", "export"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["apps"] == [{"domain": "example.com"}]


def test_sync_writes_back_only_what_changed(
    fake_store: _FakeStore, monkeypatch: pytest.MonkeyPatch, logged: io.StringIO
) -> None:
    """A service already in the recorded state costs no write."""
    fake_store.apps = [_Record(id=1, domain="example.com", status="stopped")]
    fake_store.services = [
        _Record(name="example-com", status="stopped", enabled=False, app_id=1),
        _Record(name="other-com", status="active", enabled=True, app_id=None),
    ]

    states = {
        "example-com": {"active": True, "enabled": True},
        "other-com": {"active": True, "enabled": True},
    }

    class _FakeServiceManager:
        """A service manager that reports scripted systemd states."""

        def __init__(self, verbose: bool = False) -> None:
            """
            Args:
                verbose: Ignored; present to match the real signature.
            """
            self.verbose = verbose

        def get_status(self, name: str) -> dict[str, bool]:
            """
            Args:
                name: Service name.

            Returns:
                The state this test scripted for that service.
            """
            return states[name]

    import wasm.managers.service_manager as service_module

    monkeypatch.setattr(service_module, "ServiceManager", _FakeServiceManager)

    assert store_module._store_sync(verbose=False) == 0

    assert fake_store.status_updates == [("example-com", True, True)]
    assert fake_store.app_updates == [("example.com", "running")]


def test_import_reports_nothing_to_do_on_a_clean_server(
    fake_store: _FakeStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, logged: io.StringIO
) -> None:
    """With no vhosts and no units, the import is a no-op that says so."""
    import wasm.core.config as config_module

    absent = tmp_path / "absent"
    monkeypatch.setattr(config_module, "NGINX_SITES_AVAILABLE", absent)
    monkeypatch.setattr(config_module, "NGINX_SITES_ENABLED", absent)
    monkeypatch.setattr(config_module, "APACHE_SITES_AVAILABLE", absent)
    monkeypatch.setattr(config_module, "SYSTEMD_DIR", absent)
    monkeypatch.setattr(
        store_module, "Config", lambda: SimpleNamespace(apps_directory=tmp_path / "apps")
    )

    assert store_module._store_import(verbose=False) == 0

    output = logged.getvalue()
    assert "No Nginx sites found" in output
    assert "No Apache sites found" in output


# ---------------------------------------------------------------------------
# Project detection
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("files", "expected"),
    [
        ({"next.config.js": ""}, "nextjs"),
        ({"package.json": '{"dependencies": {"next": "14"}}'}, "nextjs"),
        ({"package.json": '{"devDependencies": {"vite": "5"}}'}, "vite"),
        ({"vite.config.ts": ""}, "vite"),
        ({"package.json": "{}"}, "nodejs"),
        ({"pyproject.toml": ""}, "python"),
        ({"index.html": ""}, "static"),
        ({"README.md": ""}, "unknown"),
    ],
)
def test_detect_app_type(tmp_path: Path, files: dict[str, str], expected: str) -> None:
    """The marker files map to the deployer that handles them."""
    for name, content in files.items():
        (tmp_path / name).write_text(content, encoding="utf-8")

    assert store_module._detect_app_type(tmp_path) == expected


def test_detect_app_type_survives_a_broken_package_json(tmp_path: Path) -> None:
    """Malformed JSON costs precision, not the whole import."""
    (tmp_path / "package.json").write_text("{not json", encoding="utf-8")

    assert store_module._detect_app_type(tmp_path) == "nodejs"


# ---------------------------------------------------------------------------
# The argparse path, until the parser is retired
# ---------------------------------------------------------------------------


def test_handle_store_still_routes_path(
    fake_store: _FakeStore, capfd: pytest.CaptureFixture[str]
) -> None:
    """Both entry points run the same function."""
    assert store_module.handle_store(Namespace(action="path", verbose=False)) == 0
    assert str(fake_store.db_path) in capfd.readouterr().out


def test_handle_store_still_honours_its_own_json_flag(
    fake_store: _FakeStore, capfd: pytest.CaptureFixture[str]
) -> None:
    """``wasm store stats --json`` keeps working from the legacy parser."""
    assert store_module.handle_store(Namespace(action="stats", json=True, verbose=False)) == 0
    assert json.loads(capfd.readouterr().out) == STATISTICS


def test_handle_store_turns_a_broken_database_into_an_exit_code(
    monkeypatch: pytest.MonkeyPatch, logged: io.StringIO
) -> None:
    """The legacy path reports the error rather than raising through argparse."""
    import wasm.core.store as real_store

    def _broken(*args: Any, **kwargs: Any) -> Any:
        raise sqlite3.OperationalError("unable to open database file")

    monkeypatch.setattr(real_store, "get_store", _broken)

    assert store_module.handle_store(Namespace(action="stats", json=False, verbose=False)) == 1
    assert "wasm store init" in logged.getvalue()


def test_handle_store_without_an_action_explains_itself(logged: io.StringIO) -> None:
    """An action is required, and the message says where to look."""
    assert store_module.handle_store(Namespace(verbose=False)) == 1
    assert "wasm store --help" in logged.getvalue()


def test_handle_store_rejects_an_unknown_action(logged: io.StringIO) -> None:
    """An unrecognised action names itself."""
    assert store_module.handle_store(Namespace(action="frobnicate", verbose=False)) == 1
    assert "frobnicate" in logged.getvalue()
