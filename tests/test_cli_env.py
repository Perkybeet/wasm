# Copyright (c) 2024-2025 Yago López Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
Tests for ``wasm env``.

Two things are pinned here. The first is the command surface: every spelling
frozen in ``tests/contracts/cli_surface.json`` still resolves, and a missing
argument is a usage error rather than a traceback. The second is that the
command never prints a credential the operator did not ask for, which is the
only reason this group needs care at all.
"""

from __future__ import annotations

import io
import stat
from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace

import click
import pytest
from click.testing import CliRunner

from wasm.cli import app as app_module
from wasm.cli.commands import env as env_module
from wasm.core.config import REDACTED
from wasm.core.exceptions import EnvConfigError
from wasm.core.logger import Logger
from wasm.deployers.helpers.env_manager import EnvManager, EnvVariable

#: Flags the root group owns. A subcommand that declares one of them again is
#: the shadowing defect the Click migration exists to remove.
GLOBAL_FLAGS = frozenset({"-v", "--verbose", "--dry-run", "--json", "--no-color"})

#: Every command name and alias the frozen surface promises under ``env``.
CONTRACT_NAMES = ["configure", "config", "setup", "show", "list", "ls", "export"]


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

    :class:`~wasm.core.logger.Logger` binds ``sys.stdout`` as a default
    argument at import time, so pytest's own capture never sees it. Handing the
    module a logger bound to a buffer is what makes the output assertable.

    Args:
        monkeypatch: Patching helper, scoped to the test.

    Returns:
        The buffer every Logger built by the env module writes to.
    """
    buffer = io.StringIO()
    monkeypatch.setattr(env_module, "Logger", lambda **kwargs: Logger(stream=buffer, **kwargs))
    return buffer


@pytest.fixture
def deployed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """
    Provide an application deployed at example.com.

    Args:
        tmp_path: Per-test temporary directory.
        monkeypatch: Patching helper, scoped to the test.

    Returns:
        The application root.
    """
    apps = tmp_path / "apps"
    app_path = apps / "example-com"
    app_path.mkdir(parents=True)
    monkeypatch.setattr(env_module, "Config", lambda: SimpleNamespace(apps_directory=apps))
    return app_path


# ---------------------------------------------------------------------------
# Command surface
# ---------------------------------------------------------------------------


def test_the_group_answers_help(cli_runner: CliRunner) -> None:
    """``wasm env --help`` lists the three actions."""
    result = cli_runner.invoke(app_module.cli, ["env", "--help"])

    assert result.exit_code == 0, result.output
    for action in ("configure", "show", "export"):
        assert action in result.output


@pytest.mark.parametrize("name", CONTRACT_NAMES)
def test_every_frozen_name_answers_help(cli_runner: CliRunner, name: str) -> None:
    """Each command and each historical alias still resolves."""
    result = cli_runner.invoke(app_module.cli, ["env", name, "--help"])

    assert result.exit_code == 0, result.output
    assert "DOMAIN" in result.output


def test_the_group_help_names_the_aliases(cli_runner: CliRunner) -> None:
    """An alias that is not documented is an alias nobody finds."""
    result = cli_runner.invoke(app_module.cli, ["env", "--help"])

    for alias in ("config", "setup", "list", "ls"):
        assert alias in result.output


@pytest.mark.parametrize("name", ["configure", "config", "setup", "show", "ls", "export"])
def test_a_missing_domain_is_a_usage_error(cli_runner: CliRunner, name: str) -> None:
    """Click rejects the invocation; the handler never runs."""
    result = cli_runner.invoke(app_module.cli, ["env", name])

    assert result.exit_code == 2
    assert "Missing argument" in result.output
    assert "Traceback" not in result.output


def test_an_unknown_action_is_a_usage_error(cli_runner: CliRunner) -> None:
    """A typo names itself instead of reporting nothing."""
    result = cli_runner.invoke(app_module.cli, ["env", "shwo", "example.com"])

    assert result.exit_code == 2
    assert "shwo" in result.output


def test_export_rejects_a_directory_before_touching_anything(
    cli_runner: CliRunner, deployed: Path, tmp_path: Path
) -> None:
    """An output path that is a directory fails validation, not the write."""
    target = tmp_path / "adirectory"
    target.mkdir()

    result = cli_runner.invoke(app_module.cli, ["env", "export", "example.com", "-o", str(target)])

    assert result.exit_code == 2
    assert list(target.iterdir()) == []


def test_no_env_command_redeclares_a_global_flag() -> None:
    """
    Regression guard for the defect that motivated the migration.

    ``--dry-run`` declared on the root parser and again on a subparser meant the
    subparser default overwrote what the user asked for.
    """
    offenders: list[str] = []
    group = env_module.cli

    for name, command in [("env", group), *group.commands.items()]:
        for param in command.params:
            if not isinstance(param, click.Option):
                continue
            clash = GLOBAL_FLAGS.intersection(param.opts + param.secondary_opts)
            if clash:
                offenders.append(f"{name}: {sorted(clash)}")

    assert offenders == []


# ---------------------------------------------------------------------------
# Secrets
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "value", "leak"),
    [
        ("API_KEY", "ak_live_9f3c", "ak_live_9f3c"),
        ("DJANGO_SECRET_KEY", "abcdef123456", "abcdef123456"),
        ("SMTP_PASS", "hunter2", "hunter2"),
        ("AUTH_TOKEN", "t0ken-value", "t0ken-value"),
        ("ADMIN_PASSWORD", "correcthorse", "correcthorse"),
        ("DATABASE_URL", "postgres://app:s3cr3t@db.internal/app", "s3cr3t"),
        ("REDIS_URL", "redis://user:p4ss@cache:6379/0", "p4ss"),
    ],
)
def test_show_never_prints_a_secret_by_default(
    deployed: Path, logged: io.StringIO, name: str, value: str, leak: str
) -> None:
    """Secrets are replaced, not truncated: no prefix of the value survives."""
    (deployed / ".env").write_text(f"{name}={value}\n", encoding="utf-8")

    assert env_module._env_show("example.com", unmask=False, verbose=False) == 0

    output = logged.getvalue()
    assert name in output
    assert leak not in output
    assert REDACTED in output


def test_show_keeps_the_readable_part_of_a_connection_string(
    deployed: Path, logged: io.StringIO
) -> None:
    """Only the password is removed, so the value stays diagnosable."""
    (deployed / ".env").write_text(
        "DATABASE_URL=postgres://app:s3cr3t@db.internal:5432/app\n", encoding="utf-8"
    )

    env_module._env_show("example.com", unmask=False, verbose=False)

    output = logged.getvalue()
    assert "db.internal:5432/app" in output
    assert "s3cr3t" not in output


def test_show_leaves_a_harmless_value_alone(deployed: Path, logged: io.StringIO) -> None:
    """Redacting everything would make the command useless."""
    (deployed / ".env").write_text("PORT=3000\nNODE_ENV=production\n", encoding="utf-8")

    env_module._env_show("example.com", unmask=False, verbose=False)

    output = logged.getvalue()
    assert "3000" in output
    assert "production" in output


def test_show_reveals_only_when_asked_and_says_so(deployed: Path, logged: io.StringIO) -> None:
    """--unmask is the explicit request, and it warns before it obeys."""
    (deployed / ".env").write_text("API_KEY=ak_live_9f3c\n", encoding="utf-8")

    assert env_module._env_show("example.com", unmask=True, verbose=False) == 0

    output = logged.getvalue()
    assert "ak_live_9f3c" in output
    assert "clear" in output.lower()


def test_redact_hides_whether_a_secret_is_set() -> None:
    """An empty secret is redacted too, or its absence would be readable."""
    assert env_module._redact({"API_KEY": ""}) == {"API_KEY": REDACTED}


# ---------------------------------------------------------------------------
# Behaviour
# ---------------------------------------------------------------------------


def test_show_reports_an_application_that_is_not_deployed(deployed: Path) -> None:
    """The error names the domain and says how to find the right one."""
    with pytest.raises(EnvConfigError) as excinfo:
        env_module._env_show("nowhere.com", unmask=False, verbose=False)

    assert "nowhere.com" in excinfo.value.message
    assert "wasm list" in excinfo.value.details


def test_show_on_an_application_without_an_env_file_is_not_an_error(
    deployed: Path, logged: io.StringIO
) -> None:
    """Nothing configured yet is a state, not a failure."""
    assert env_module._env_show("example.com", unmask=False, verbose=False) == 0
    assert "No environment variables" in logged.getvalue()


def test_export_writes_an_owner_only_file(deployed: Path, tmp_path: Path) -> None:
    """The export carries secrets in clear, so it must not be world readable."""
    (deployed / ".env").write_text("API_KEY=ak_live_9f3c\nPORT=3000\n", encoding="utf-8")
    target = tmp_path / "exported.env"

    assert env_module._env_export("example.com", str(target), verbose=False) == 0

    assert target.read_text(encoding="utf-8") == "API_KEY=ak_live_9f3c\nPORT=3000\n"
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_configure_writes_an_owner_only_env_file(
    deployed: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The .env the application will run with is written 0600."""
    monkeypatch.setattr(EnvManager, "discover", lambda self, path: [EnvVariable(name="API_KEY")])
    monkeypatch.setattr(
        EnvManager,
        "prompt_variables",
        lambda self, variables, existing: {"API_KEY": "ak_live_9f3c"},
    )

    assert env_module._env_configure("example.com", verbose=False) == 0

    env_file = deployed / ".env"
    assert env_file.read_text(encoding="utf-8") == "API_KEY=ak_live_9f3c\n"
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600


def test_configure_stops_when_the_project_declares_nothing(
    deployed: Path, logged: io.StringIO
) -> None:
    """No .env.example means there is nothing to ask about."""
    assert env_module._env_configure("example.com", verbose=False) == 0

    assert "No .env.example files found" in logged.getvalue()
    assert not (deployed / ".env").exists()


def test_show_command_reaches_the_handler(
    cli_runner: CliRunner, deployed: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Click command passes the domain and the flag straight through."""
    seen: list[tuple[str, bool, bool]] = []
    monkeypatch.setattr(
        env_module,
        "_env_show",
        lambda domain, unmask, verbose: seen.append((domain, unmask, verbose)) or 0,
    )

    result = cli_runner.invoke(
        app_module.cli, ["--verbose", "env", "show", "example.com", "--unmask"]
    )

    assert result.exit_code == 0, result.output
    assert seen == [("example.com", True, True)]


def test_export_command_defaults_to_dot_env(
    cli_runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The documented default is still .env."""
    seen: list[tuple[str, str, bool]] = []
    monkeypatch.setattr(
        env_module,
        "_env_export",
        lambda domain, output, verbose: seen.append((domain, output, verbose)) or 0,
    )

    result = cli_runner.invoke(app_module.cli, ["env", "export", "example.com"])

    assert result.exit_code == 0, result.output
    assert seen == [("example.com", ".env", False)]


# ---------------------------------------------------------------------------
# The argparse path, until the parser is retired
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("action", ["configure", "config", "setup"])
def test_handle_env_still_routes_the_configure_aliases(
    action: str, deployed: Path, logged: io.StringIO
) -> None:
    """Both entry points run the same function."""
    args = Namespace(action=action, domain="example.com", verbose=False)

    assert env_module.handle_env(args) == 0
    assert "No .env.example files found" in logged.getvalue()


@pytest.mark.parametrize("action", ["show", "list", "ls"])
def test_handle_env_still_routes_the_show_aliases(
    action: str, deployed: Path, logged: io.StringIO
) -> None:
    """The show aliases keep working from the legacy parser."""
    (deployed / ".env").write_text("API_KEY=ak_live_9f3c\n", encoding="utf-8")
    args = Namespace(action=action, domain="example.com", unmask=False, verbose=False)

    assert env_module.handle_env(args) == 0
    assert "ak_live_9f3c" not in logged.getvalue()


def test_handle_env_turns_a_missing_application_into_an_exit_code(
    deployed: Path, logged: io.StringIO
) -> None:
    """The legacy path reports the error rather than raising through argparse."""
    args = Namespace(action="show", domain="nowhere.com", unmask=False, verbose=False)

    assert env_module.handle_env(args) == 1
    assert "nowhere.com" in logged.getvalue()


def test_handle_env_without_an_action_explains_itself(
    logged: io.StringIO,
) -> None:
    """An action is required, and the message says where to look."""
    assert env_module.handle_env(Namespace(verbose=False)) == 1
    assert "wasm env --help" in logged.getvalue()
