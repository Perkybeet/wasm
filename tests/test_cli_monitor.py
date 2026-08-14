"""
Tests for the ``wasm monitor`` command surface after the move to Click.

The migration has one job beyond "it still runs": the command line a user typed
last week has to keep working. So these tests are written against
``tests/contracts/cli_surface.json`` rather than against the code, and they
check the two things argparse got wrong here: the local alias ``monitor info``,
which the root alias table cannot resolve because it only rewrites the first
word, and the global flags, which used to be redeclared on every subparser and
therefore overwrote the value the user set before the subcommand name.

The machine underneath is fixtures: psutil returns one process, systemd answers
through the FakeRunner, and the unit directory lives in a temporary directory.
"""

from __future__ import annotations

import functools
import io
import json
import types
from pathlib import Path
from typing import Any

import click
import psutil
import pytest
import yaml
from click.testing import CliRunner, Result

from wasm.cli.commands import monitor as cli_monitor
from wasm.core.exceptions import MonitorError
from wasm.core.logger import Logger

#: Flags that belong to the root command and to no other. A subcommand that
#: declares one of them is the shadowing defect the migration exists to remove.
GLOBAL_FLAGS = frozenset({"-v", "--verbose", "--dry-run", "--json", "--no-color"})

CONTRACT = Path(__file__).parent / "contracts" / "cli_surface.json"


def contract_subcommands() -> list[str]:
    """
    Read the frozen ``wasm monitor`` subcommand names.

    Returns:
        Every action the argparse tree offered, in sorted order.
    """
    surface = json.loads(CONTRACT.read_text(encoding="utf-8"))
    return sorted(key.split(" ", 1)[1] for key in surface if key.startswith("monitor "))


class _FakeProcess:
    """Stand-in for ``psutil.Process`` as returned by ``process_iter``."""

    def __init__(self, **info: Any) -> None:
        """
        Args:
            info: The fields ``process_iter`` would expose.
        """
        self.info = info
        self.pid = info["pid"]


@pytest.fixture
def monitor_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    sandbox: Path,
    runner: Any,
) -> Any:
    """
    Put the monitor on a machine made of fixtures.

    Args:
        monkeypatch: Patching helper, scoped to the test.
        tmp_path: Per-test temporary directory.
        sandbox: Fixture pointing HOME at a throwaway directory.
        runner: FakeRunner installed as the process-wide runner.

    Returns:
        A namespace with the runner and the systemd unit directory.
    """
    from wasm.monitor import process_monitor as process_monitor_module

    process = _FakeProcess(
        pid=4242,
        name="xmrig",
        username="nobody",
        cpu_percent=99.0,
        memory_percent=12.0,
        cmdline=["./xmrig"],
        create_time=0.0,
        ppid=0,
        status="running",
        num_threads=4,
        cwd="/var/empty",
    )
    monkeypatch.setattr(psutil, "process_iter", lambda attrs=None, *a, **kw: iter([process]))
    # A one-shot scan samples CPU over a real window; tests do not need to wait.
    monkeypatch.setattr("wasm.monitor.metrics.time.sleep", lambda seconds: None)

    unit_dir = tmp_path / "systemd"
    unit_dir.mkdir()
    monkeypatch.setattr(process_monitor_module, "SYSTEMD_DIR", unit_dir)

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    wasm_bin = bin_dir / "wasm"
    wasm_bin.write_text("#!/bin/sh\n")
    wasm_bin.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir))

    monkeypatch.setattr(cli_monitor, "check_root", lambda: True)

    runner.script(["systemctl", "is-active"], stdout="active\n")
    runner.script(["systemctl", "is-enabled"], stdout="enabled\n")
    runner.script(["systemctl", "show"], stdout="MainPID=123\nActiveState=active\n")

    return types.SimpleNamespace(runner=runner, unit_dir=unit_dir, home=sandbox)


@pytest.fixture
def cli_output(monkeypatch: pytest.MonkeyPatch) -> io.StringIO:
    """
    Collect what the commands print through the logger.

    ``Logger`` binds ``sys.stdout`` as a default argument value at import time,
    so neither capsys nor the CliRunner sees its output; giving it an explicit
    stream is the only reliable way to read it back.

    Args:
        monkeypatch: Patching helper, scoped to the test.

    Returns:
        The buffer the commands write into.
    """
    buffer = io.StringIO()
    monkeypatch.setattr(cli_monitor, "Logger", functools.partial(Logger, stream=buffer))
    return buffer


def invoke(*args: str, **kwargs: Any) -> Result:
    """
    Run ``wasm monitor`` with the given arguments.

    Args:
        args: Arguments after ``monitor``.
        kwargs: Passed to ``CliRunner.invoke``.

    Returns:
        The result of the invocation.
    """
    return CliRunner().invoke(cli_monitor.cli, list(args), **kwargs)


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


# ---------------------------------------------------------------------------
# The frozen surface
# ---------------------------------------------------------------------------


def test_every_frozen_subcommand_still_exists() -> None:
    """The contract is the list; the group may not quietly be shorter than it."""
    ctx = click.Context(cli_monitor.cli)

    assert sorted(cli_monitor.cli.list_commands(ctx)) == contract_subcommands()


@pytest.mark.parametrize("name", contract_subcommands())
def test_every_subcommand_documents_itself(name: str) -> None:
    """--help is the documentation, so every command has to answer it."""
    result = invoke(name, "--help")

    assert result.exit_code == 0, result.output
    assert name in result.output
    # A help screen that says nothing is a help screen that was not written.
    assert len(result.output.splitlines()) > 3


def test_monitor_info_still_reaches_status() -> None:
    """
    ``monitor info`` is an alias inside the group, not at the root.

    The root alias table only rewrites the first word of the command line, so
    losing this mapping would break a spelling that has always worked.
    """
    ctx = click.Context(cli_monitor.cli)

    assert cli_monitor.cli.get_command(ctx, "info") is cli_monitor.cli.get_command(ctx, "status")


def test_the_alias_reports_the_name_it_resolves_to() -> None:
    """``monitor info --help`` documents status rather than failing."""
    result = invoke("info", "--help")

    assert result.exit_code == 0, result.output
    assert "watching" in result.output


def test_the_root_group_reaches_monitor_through_its_alias() -> None:
    """``wasm mon scan`` is in scripts; the root alias table must still map it."""
    from wasm.cli.app import cli as root

    result = CliRunner().invoke(root, ["mon", "scan", "--help"])

    assert result.exit_code == 0, result.output
    assert "stands out" in result.output


def test_the_group_without_an_action_lists_the_actions() -> None:
    """
    argparse answered "the following arguments are required: <action>".

    The exit code stays 2, because that is what scripts branch on, but the
    output is now the list of things the user could have typed.
    """
    result = invoke()

    assert result.exit_code == 2
    for action in contract_subcommands():
        assert action in result.output


# ---------------------------------------------------------------------------
# Global flags
# ---------------------------------------------------------------------------


def test_no_subcommand_redeclares_a_global_flag() -> None:
    """
    Global state lives on the context, not on nine copies of the same flag.

    ``wasm --dry-run monitor scan`` used to run a real scan because argparse
    copied the subparser's default over the value the root parser had parsed.
    A subcommand that declares the flag again can reintroduce exactly that.
    """
    ctx = click.Context(cli_monitor.cli)
    commands = [cli_monitor.cli] + [
        cli_monitor.cli.get_command(ctx, name) for name in cli_monitor.cli.list_commands(ctx)
    ]

    offenders = {
        command.name: sorted(GLOBAL_FLAGS.intersection(option.opts))
        for command in commands
        if command is not None
        for option in command.params
        if isinstance(option, click.Option) and GLOBAL_FLAGS.intersection(option.opts)
    }

    assert offenders == {}, f"commands that shadow a global flag: {offenders}"


def test_verbose_is_read_from_the_context_not_from_the_command(
    monitor_env: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    The command honours a flag it does not declare.

    ``wasm --verbose monitor config`` has to reach the logger the command
    builds, which is the half of the shadowing bug that stayed silent.
    """
    from wasm.cli.app import Context

    asked: list[bool] = []
    buffer = io.StringIO()

    def _record(*args: Any, verbose: bool = False, **kwargs: Any) -> Logger:
        asked.append(verbose)
        return Logger(*args, verbose=verbose, stream=buffer, **kwargs)

    monkeypatch.setattr(cli_monitor, "Logger", _record)

    result = CliRunner().invoke(
        cli_monitor.cli,
        ["config"],
        obj=Context(verbose=True),
        standalone_mode=False,
    )

    assert result.return_value == 0, result.output
    assert asked == [True]


# ---------------------------------------------------------------------------
# Usage errors
# ---------------------------------------------------------------------------


def test_an_unknown_action_is_a_usage_error(monitor_env: Any) -> None:
    """A typo exits 2 and touches nothing, rather than printing a traceback."""
    result = invoke("nonsense")

    assert result.exit_code == 2
    assert monitor_env.runner.calls == []


def test_an_unknown_option_is_rejected_before_anything_runs(monitor_env: Any) -> None:
    """The scan is expensive; an unparsable command line must not start it."""
    result = invoke("scan", "--threshold=high")

    assert result.exit_code == 2
    assert monitor_env.runner.calls == []


def test_an_extra_argument_is_a_usage_error(monitor_env: Any) -> None:
    """None of these actions takes a positional argument."""
    result = invoke("status", "wasm-monitor")

    assert result.exit_code == 2
    assert monitor_env.runner.calls == []


# ---------------------------------------------------------------------------
# What the commands actually do
# ---------------------------------------------------------------------------


def test_scan_reports_the_process_it_noticed(monitor_env: Any, cli_output: io.StringIO) -> None:
    """The scan reads the process table and prints what stood out."""
    result = invoke("scan", standalone_mode=False)

    assert result.return_value == 0, result.output
    assert "xmrig" in cli_output.getvalue()


def test_scan_promises_nothing_it_no_longer_does(
    monitor_env: Any,
    cli_output: io.StringIO,
) -> None:
    """The monitor stopped being an antivirus; the output has to say so."""
    assert invoke("scan", standalone_mode=False).return_value == 0

    output = cli_output.getvalue()
    assert "no process was signalled and no file was touched" in output


def test_the_retired_ai_flags_are_accepted_and_explain_themselves(
    monitor_env: Any,
    cli_output: io.StringIO,
) -> None:
    """
    A script still passing --force-ai gets an explanation, not a usage error.

    The flags are in the frozen surface, so they are still parsed; what they
    used to do no longer exists, so the command says which flag it ignored.
    """
    result = invoke("scan", "--force-ai", "--all", standalone_mode=False)

    assert result.return_value == 0, result.output
    output = cli_output.getvalue()
    assert "--force-ai is ignored" in output
    assert "--all is ignored" in output


def test_the_scan_help_does_not_promise_ai_or_termination() -> None:
    """The help text is documentation, and the documentation was out of date."""
    output = invoke("scan", "--help").output.lower()

    assert "retired" in output
    assert "kill" not in output
    assert "terminate" not in output


def test_status_states_what_the_monitor_will_not_do(
    monitor_env: Any,
    cli_output: io.StringIO,
) -> None:
    """An operator reading the status learns the tool's limits from the tool."""
    assert invoke("status", standalone_mode=False).return_value == 0

    output = cli_output.getvalue()
    assert "signals, terminates or restarts a process" in output


def test_status_open_prints_the_configured_panel_url_without_a_display(
    monitor_env: Any,
    cli_output: io.StringIO,
    isolated_panel_config: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--open`` prints the panel URL and never touches xdg-open without a display."""
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    _configure_panel(isolated_panel_config)

    result = invoke("status", "--open", standalone_mode=False)

    assert result.return_value == 0, result.output
    assert "http://127.0.0.1:8080/" in cli_output.getvalue()
    assert not monitor_env.runner.calls_to("xdg-open")


def test_status_open_launches_xdg_open_when_a_display_is_present(
    monitor_env: Any,
    cli_output: io.StringIO,
    isolated_panel_config: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With a display available, ``--open`` hands the dashboard URL to xdg-open."""
    monkeypatch.setenv("DISPLAY", ":0")
    _configure_panel(isolated_panel_config)

    result = invoke("status", "--open", standalone_mode=False)

    assert result.return_value == 0, result.output
    assert monitor_env.runner.calls_to("xdg-open") == [("xdg-open", "http://127.0.0.1:8080/")]


def test_status_open_without_a_configured_panel_warns_and_exits_clean(
    monitor_env: Any,
    cli_output: io.StringIO,
    isolated_panel_config: Path,
) -> None:
    """The panel is off by default, so ``--open`` warns instead of guessing a URL."""
    result = invoke("status", "--open", standalone_mode=False)

    assert result.return_value == 0, result.output
    assert "not configured" in cli_output.getvalue()
    assert not monitor_env.runner.calls_to("xdg-open")


def test_enable_drives_systemd_through_the_runner(monitor_env: Any) -> None:
    """Enabling reaches systemd with the exact argv, through the audited seam."""
    result = invoke("enable", standalone_mode=False)

    assert result.return_value == 0, result.output
    assert ("systemctl", "enable", "--now", "wasm-monitor") in monitor_env.runner.calls


def test_install_writes_the_unit_and_reloads_systemd(monitor_env: Any) -> None:
    """Installing writes one file and tells systemd about it."""
    result = invoke("install", standalone_mode=False)

    assert result.return_value == 0, result.output
    assert (monitor_env.unit_dir / "wasm-monitor.service").exists()
    assert ("systemctl", "daemon-reload") in monitor_env.runner.calls


def test_disable_reports_a_service_that_was_never_installed(monitor_env: Any) -> None:
    """Nothing to disable is an answer, and a non-zero one."""
    result = invoke("disable", standalone_mode=False)

    assert result.return_value == 1
    assert monitor_env.runner.calls == []


def test_config_lists_the_settings_that_exist(
    monitor_env: Any,
    cli_output: io.StringIO,
) -> None:
    """The settings screen names the database and the retention window."""
    assert invoke("config", standalone_mode=False).return_value == 0

    output = cli_output.getvalue()
    assert "Retention" in output
    assert "Row cap" in output


def test_run_starts_the_loop_and_stops_on_interrupt(
    monitor_env: Any,
    monkeypatch: pytest.MonkeyPatch,
    cli_output: io.StringIO,
) -> None:
    """The foreground loop returns cleanly instead of propagating Ctrl+C."""

    def _interrupt(self: Any) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr("wasm.monitor.process_monitor.ProcessMonitor.run", _interrupt)
    monkeypatch.setattr("wasm.monitor.process_monitor.ProcessMonitor.stop", lambda self: None)

    result = invoke("run", standalone_mode=False)

    assert result.return_value == 0, result.output
    assert "Stopped" in cli_output.getvalue()


def test_test_email_refuses_when_no_recipient_is_configured(
    monitor_env: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A mail command with nowhere to send says so instead of pretending."""

    class _NoRecipients:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            """
            Args:
                args: Ignored, kept for signature compatibility.
                kwargs: Ignored, kept for signature compatibility.
            """
            self.recipients: list[str] = []

    monkeypatch.setattr(cli_monitor, "EmailNotifier", _NoRecipients)

    assert invoke("test-email", standalone_mode=False).return_value == 1


def test_test_email_sends_through_the_notifier(
    monitor_env: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The command's whole purpose is one send; assert the send happened."""
    sent: list[bool] = []

    class _Recorder:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            """
            Args:
                args: Ignored, kept for signature compatibility.
                kwargs: Ignored, kept for signature compatibility.
            """
            self.recipients = ["ops@example.com"]

        def send_test_email(self) -> bool:
            """Record the send instead of opening a socket."""
            sent.append(True)
            return True

    monkeypatch.setattr(cli_monitor, "EmailNotifier", _Recorder)

    assert invoke("test-email", standalone_mode=False).return_value == 0
    assert sent == [True]


# ---------------------------------------------------------------------------
# Root and confirmation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("action", ["install", "enable", "disable", "uninstall"])
def test_actions_that_touch_systemd_require_root(
    action: str,
    monitor_env: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without root the error names the problem instead of failing on a write."""
    monkeypatch.setattr(cli_monitor, "check_root", lambda: False)

    # uninstall confirms first; --yes gets the test past the prompt to the check.
    args = [action, "--yes"] if action == "uninstall" else [action]
    result = CliRunner().invoke(cli_monitor.cli, args, standalone_mode=False)

    assert isinstance(result.exception, MonitorError)
    assert "needs root" in str(result.exception)
    assert not (monitor_env.unit_dir / "wasm-monitor.service").exists()


def test_uninstall_names_the_unit_and_the_consequence(monitor_env: Any) -> None:
    """A destructive prompt has to say what it removes and what stops working."""
    assert invoke("install", standalone_mode=False).return_value == 0

    result = invoke("uninstall", input="n\n", standalone_mode=False)

    assert result.return_value == 0
    assert "wasm-monitor" in result.output
    assert "watch this server" in result.output
    assert (monitor_env.unit_dir / "wasm-monitor.service").exists(), "declining still removed it"


def test_uninstall_removes_the_unit_once_confirmed(monitor_env: Any) -> None:
    """Answering yes does the thing the prompt described."""
    assert invoke("install", standalone_mode=False).return_value == 0

    result = invoke("uninstall", input="y\n", standalone_mode=False)

    assert result.return_value == 0, result.output
    assert not (monitor_env.unit_dir / "wasm-monitor.service").exists()


def test_uninstall_yes_skips_the_prompt(monitor_env: Any) -> None:
    """A script has no terminal to answer with, so it has --yes."""
    assert invoke("install", standalone_mode=False).return_value == 0

    result = invoke("uninstall", "--yes", standalone_mode=False)

    assert result.return_value == 0, result.output
    assert not (monitor_env.unit_dir / "wasm-monitor.service").exists()


# ---------------------------------------------------------------------------
# The two entry points
# ---------------------------------------------------------------------------


def test_the_argparse_handler_and_the_click_group_share_one_action_table() -> None:
    """
    ``wasm.cli.parser`` still calls ``handle_monitor``; it may not drift.

    Both entry points dispatch through ACTIONS, so a command added to one is a
    command added to the other.
    """
    ctx = click.Context(cli_monitor.cli)
    click_names = set(cli_monitor.cli.list_commands(ctx)) | set(cli_monitor.LOCAL_ALIASES)

    assert click_names == set(cli_monitor.ACTIONS)
