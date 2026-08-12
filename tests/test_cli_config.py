# Copyright (c) 2024-2026 Yago Lopez Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
Tests for the ``wasm config`` command group after the move to Click.

These commands are what a provisioning script calls after an upgrade, so the
things pinned here are the ones a script depends on: the surface the argparse
tree published, an exit code that reflects whether the file was really written,
and output that stays parseable.
"""

from __future__ import annotations

import json
import sys
from argparse import Namespace
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import click
import pytest
import yaml
from click.testing import CliRunner

from wasm.cli import app as app_module
from wasm.cli.app import cli as root_cli
from wasm.cli.commands import config as config_cmd
from wasm.core.logger import Logger
from wasm.core.runner import DryRunRunner, FakeRunner, get_runner

CONTRACT = json.loads(
    (Path(__file__).parent / "contracts/cli_surface.json").read_text(encoding="utf-8")
)

#: Command paths the frozen surface promises under this subtree.
CONTRACT_COMMANDS = sorted(key for key in CONTRACT if key == "config" or key.startswith("config "))

#: Flags the root group owns. A subcommand may re-offer them, never own them.
GLOBAL_OPTS = {"-v", "--verbose", "--dry-run", "--no-color", "--json"}


@dataclass
class Invocation:
    """
    What one run of the CLI produced.

    Attributes:
        exit_code: The status the process would have exited with.
        output: Everything the user would have seen.
        exception: The exception that escaped, if any.
    """

    exit_code: int
    output: str
    exception: BaseException | None


#: Type of the ``wasm`` fixture, for the signatures below.
Wasm = Callable[..., Invocation]


class _TestLogger(Logger):
    """
    A logger that writes wherever stdout points when it is built.

    :class:`Logger` binds its stream as a default argument, so every instance
    writes to the interpreter's stdout as it was at import time. Under a test
    that is neither the stream Click's runner reads back nor the one pytest
    hands to capsys, which would make every assertion on output vacuous.
    """

    def __init__(
        self,
        verbose: bool = False,
        no_color: bool = False,
        log_file: Path | None = None,
        stream: Any = None,
    ):
        """
        Args:
            verbose: Show debug messages.
            no_color: Disable colour.
            log_file: Optional file to mirror output into.
            stream: Where to write; defaults to stdout as it is right now.
        """
        super().__init__(
            verbose=verbose,
            no_color=no_color,
            log_file=log_file,
            stream=stream if stream is not None else sys.stdout,
        )


@pytest.fixture(autouse=True)
def _readable_output(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Make everything a command prints readable by the test.

    Args:
        monkeypatch: Patching helper, scoped to the test.
    """
    monkeypatch.setattr(app_module, "Logger", _TestLogger)
    monkeypatch.setattr(config_cmd, "Logger", _TestLogger)


@pytest.fixture
def wasm() -> Wasm:
    """
    Run the real command tree the way the console script does.

    Returns:
        A callable taking the arguments after the program name.
    """
    cli_runner = CliRunner()

    def invoke(*args: str) -> Invocation:
        result = cli_runner.invoke(root_cli, list(args))
        return Invocation(
            exit_code=result.exit_code,
            output=result.output,
            exception=result.exception,
        )

    return invoke


@pytest.fixture
def fake_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, Any]:
    """
    Replace the configuration singleton and point the path at a sandbox.

    Args:
        monkeypatch: Patching helper, scoped to the test.
        tmp_path: Per-test temporary directory.

    Returns:
        A dict the test mutates to script what the upgrade reports, and reads
        to see what was asked for.
    """
    state: dict[str, Any] = {
        "upgrade_result": {"upgraded": False, "added_keys": [], "removed_keys": []},
        "values": {"webserver": "nginx", "ssl": {"email": "ops@example.com"}},
        "upgrade_calls": 0,
    }

    class _FakeConfig:
        """Stands in for the real singleton, which reads and writes /etc."""

        def upgrade(self) -> dict[str, Any]:
            """
            Report the scripted upgrade outcome.

            Returns:
                The same shape the real upgrade returns.
            """
            state["upgrade_calls"] += 1
            return dict(state["upgrade_result"])

        def to_dict(self) -> dict[str, Any]:
            """
            Report the configuration in effect.

            Returns:
                A plain dictionary.
            """
            return dict(state["values"])

    monkeypatch.setattr(config_cmd, "Config", _FakeConfig)
    monkeypatch.setattr(config_cmd, "DEFAULT_CONFIG_PATH", tmp_path / "etc/wasm/config.yaml")
    return state


# ---------------------------------------------------------------------------
# Command surface
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("command", CONTRACT_COMMANDS)
def test_every_contract_command_answers_help(wasm: Wasm, command: str) -> None:
    """Every command the argparse tree published still exists and documents itself."""
    result = wasm(*command.split(" "), "--help")

    assert result.exit_code == 0, result.output
    assert "Usage:" in result.output


@pytest.mark.parametrize("command", CONTRACT_COMMANDS)
def test_every_contract_option_survived(wasm: Wasm, command: str) -> None:
    """No option disappeared: they are in scripts that must keep running."""
    node: click.Command | None = root_cli
    ctx = click.Context(root_cli)
    for name in command.split(" "):
        assert isinstance(node, click.Group)
        node = node.get_command(ctx, name)
        assert node is not None, f"wasm {command} is gone"

    assert node is not None
    declared = {
        opt
        for param in node.params
        if isinstance(param, click.Option)
        for opt in param.opts + param.secondary_opts
    }
    assert set(CONTRACT[command]["options"]) <= declared


def test_no_subcommand_owns_a_global_flag() -> None:
    """
    A subcommand may re-offer a global flag; it may never hold the value.

    This is the defect the migration exists to remove: argparse let a subparser
    reset the ``--dry-run`` the user asked for before the subcommand name.
    """
    ctx = click.Context(config_cmd.cli)
    offenders: list[str] = []

    for name in config_cmd.cli.list_commands(ctx):
        command = config_cmd.cli.get_command(ctx, name)
        assert command is not None
        for param in command.params:
            if not isinstance(param, click.Option):
                continue
            if not GLOBAL_OPTS.intersection(param.opts):
                continue
            if param.expose_value or not param.is_eager:
                offenders.append(f"config {name}: {param.opts}")

    assert offenders == []


def test_no_config_command_offers_json(wasm: Wasm) -> None:
    """--json is not offered where nothing builds a structured payload."""
    ctx = click.Context(config_cmd.cli)
    for name in config_cmd.cli.list_commands(ctx):
        command = config_cmd.cli.get_command(ctx, name)
        assert command is not None
        assert "--json" not in {opt for param in command.params for opt in param.opts}


def test_unknown_option_is_a_usage_error(wasm: Wasm) -> None:
    """A typo gets a usage message and exit code 2, never a traceback."""
    result = wasm("config", "show", "--wat")

    assert result.exit_code == 2
    assert "no such option" in result.output.lower()


def test_unknown_subcommand_is_a_usage_error(wasm: Wasm) -> None:
    """`wasm config nonsense` fails as a usage error."""
    assert wasm("config", "nonsense").exit_code == 2


def test_upgrade_rejects_a_stray_argument(wasm: Wasm, fake_config: dict[str, Any]) -> None:
    """An argument nobody declared is refused before the file is touched."""
    result = wasm("config", "upgrade", "extra")

    assert result.exit_code == 2
    assert fake_config["upgrade_calls"] == 0


def test_bare_config_lists_its_commands(wasm: Wasm) -> None:
    """`wasm config` with no action shows what it can do."""
    result = wasm("config")

    assert "upgrade" in result.output
    assert "show" in result.output
    assert "path" in result.output


# ---------------------------------------------------------------------------
# config upgrade
# ---------------------------------------------------------------------------


def test_upgrade_reports_the_keys_it_added(wasm: Wasm, fake_config: dict[str, Any]) -> None:
    """The operator is told exactly which options are new."""
    fake_config["upgrade_result"] = {
        "upgraded": True,
        "added_keys": ["ssl.email", "backup.retention_days"],
        "removed_keys": [],
    }

    result = wasm("config", "upgrade")

    assert result.exit_code == 0, result.output
    assert "ssl.email" in result.output
    assert "backup.retention_days" in result.output


def test_upgrade_summarises_a_long_list(wasm: Wasm, fake_config: dict[str, Any]) -> None:
    """A large upgrade stays readable instead of scrolling off the screen."""
    fake_config["upgrade_result"] = {
        "upgraded": True,
        "added_keys": [f"key.{n}" for n in range(25)],
        "removed_keys": [],
    }

    result = wasm("config", "upgrade")

    assert result.exit_code == 0
    assert "key.0" in result.output
    assert "and 15 more" in result.output


def test_upgrade_says_when_nothing_changed(wasm: Wasm, fake_config: dict[str, Any]) -> None:
    """An up to date file is reported as such, not as an upgrade."""
    result = wasm("config", "upgrade")

    assert result.exit_code == 0
    assert "already up to date" in result.output


def test_upgrade_failure_exits_non_zero(wasm: Wasm, fake_config: dict[str, Any]) -> None:
    """A provisioning script must be able to tell that the file is stale."""
    fake_config["upgrade_result"] = {"error": "permission denied"}

    result = wasm("config", "upgrade")

    assert result.exit_code == 1
    assert "permission denied" in result.output


def test_quiet_upgrade_says_nothing_on_success(wasm: Wasm, fake_config: dict[str, Any]) -> None:
    """--quiet is for scripts, which only read the exit status."""
    fake_config["upgrade_result"] = {
        "upgraded": True,
        "added_keys": ["ssl.email"],
        "removed_keys": [],
    }

    result = wasm("config", "upgrade", "--quiet")

    assert result.exit_code == 0
    assert result.output.strip() == ""


def test_quiet_upgrade_still_reports_a_failure(wasm: Wasm, fake_config: dict[str, Any]) -> None:
    """
    Quiet means quiet about success, never about failure.

    A silent failure here leaves the next command reading options that are not
    in the file.
    """
    fake_config["upgrade_result"] = {"error": "disk full"}

    result = wasm("config", "upgrade", "-q")

    assert result.exit_code == 1
    assert "disk full" in result.output


# ---------------------------------------------------------------------------
# config show and config path
# ---------------------------------------------------------------------------


def test_show_emits_parseable_yaml(wasm: Wasm, fake_config: dict[str, Any]) -> None:
    """The dump is what an operator copies back into the file, so it must load."""
    result = wasm("config", "show")

    assert result.exit_code == 0
    body = result.output[result.output.index("webserver:") :]
    assert yaml.safe_load(body)["webserver"] == "nginx"


def test_path_reports_where_the_file_is(wasm: Wasm, fake_config: dict[str, Any]) -> None:
    """The path is printed whether or not the file exists yet."""
    result = wasm("config", "path")

    assert result.exit_code == 0
    assert "config.yaml" in result.output
    assert "No" in result.output


def test_path_notices_an_existing_file(
    wasm: Wasm, fake_config: dict[str, Any], tmp_path: Path
) -> None:
    """An existing file is reported as existing."""
    path = tmp_path / "etc/wasm/config.yaml"
    path.parent.mkdir(parents=True)
    path.write_text("webserver: nginx\n", encoding="utf-8")

    result = wasm("config", "path")

    assert result.exit_code == 0
    assert "Yes" in result.output


# ---------------------------------------------------------------------------
# Global flags
# ---------------------------------------------------------------------------


def test_dry_run_after_the_subcommand_still_rehearses(
    wasm: Wasm, runner: FakeRunner, fake_config: dict[str, Any]
) -> None:
    """`wasm config path --dry-run` rehearses, it does not just parse."""
    wasm("config", "path", "--dry-run")

    assert isinstance(get_runner(), DryRunRunner)


def test_dry_run_before_the_subcommand_is_not_undone(
    wasm: Wasm, runner: FakeRunner, fake_config: dict[str, Any]
) -> None:
    """The flag survives the subcommand, which is the whole point of the move."""
    wasm("--dry-run", "config", "path")

    assert isinstance(get_runner(), DryRunRunner)


def test_verbose_after_the_subcommand_reaches_the_shared_context(
    wasm: Wasm, fake_config: dict[str, Any]
) -> None:
    """--verbose is accepted after the subcommand name, as it always was."""
    assert wasm("config", "path", "--verbose").exit_code == 0
    assert wasm("config", "path", "-v").exit_code == 0


# ---------------------------------------------------------------------------
# The argparse entry point still works while it is still wired up
# ---------------------------------------------------------------------------


def test_handle_config_still_dispatches(fake_config: dict[str, Any]) -> None:
    """Both entry points call the same implementation, so neither drifts."""
    assert config_cmd.handle_config(Namespace(action="path", verbose=False)) == 0
    assert config_cmd.handle_config(Namespace(action="upgrade", verbose=False, quiet=True)) == 0
    assert fake_config["upgrade_calls"] == 1


def test_handle_config_without_an_action_prints_the_summary(
    capsys: pytest.CaptureFixture[str], fake_config: dict[str, Any]
) -> None:
    """`wasm config` with no action keeps listing its commands."""
    assert config_cmd.handle_config(Namespace(action=None, verbose=False)) == 0
    assert "upgrade" in capsys.readouterr().out
