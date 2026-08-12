# Copyright (c) 2024-2026 Yago Lopez Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
Tests for the ``wasm web`` Click group.

Three things are being defended here:

- The frozen command surface. Every command and option the argparse tree
  offered still parses, because they are in operators' scripts.
- The refusal to expose a root panel. ``--host 0.0.0.0`` without TLS or a
  whitelist has to fail before a socket is bound, not warn.
- The separation between global flags and command options. A subcommand may
  accept ``--verbose``, but it must never receive it as a parameter of its own.
"""

from __future__ import annotations

import inspect
import json
import os
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import click
import pytest
from click.testing import CliRunner

from wasm.cli.commands import web
from wasm.core.exceptions import SecurityError
from wasm.core.runner import FakeRunner

CONTRACT = json.loads(
    (Path(__file__).parent / "contracts" / "cli_surface.json").read_text(encoding="utf-8")
)

#: Every ``web`` entry of the frozen surface, keyed by the subcommand name. The
#: group itself is keyed by the empty string.
WEB_CONTRACT = {
    ("" if key == "web" else key[len("web ") :]): value
    for key, value in CONTRACT.items()
    if key == "web" or key.startswith("web ")
}

SUBCOMMANDS = sorted(name for name in WEB_CONTRACT if name)

#: Flags owned by the root group. A subcommand may spell them; it may not own
#: a value for them.
GLOBAL_OPTIONS = {"-v", "--verbose", "--dry-run", "--json", "--no-color"}
GLOBAL_PARAMETER_NAMES = {"verbose", "dry_run", "json_output", "json", "no_color"}

ALL_INTERFACES = "0.0.0.0"  # noqa: S104 - the address under test, never bound


@pytest.fixture(autouse=True)
def logger_follows_the_current_stdout(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Make logger output visible to the Click test runner.

    ``Logger`` binds its default stream when the module is imported, long
    before ``CliRunner`` redirects stdout, so without this the assertions in
    this file would silently pass against empty output.

    Args:
        monkeypatch: Patching helper, scoped to the test.
    """
    real_logger = web.Logger

    class StdoutLogger(real_logger):  # type: ignore[valid-type, misc]
        """A logger that resolves stdout when it is built, not when imported."""

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            """
            Args:
                *args: Passed to Logger.
                **kwargs: Passed to Logger.
            """
            kwargs.setdefault("stream", sys.stdout)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(web, "Logger", StdoutLogger)


@pytest.fixture
def cli_runner() -> CliRunner:
    """
    Provide a Click test runner.

    Returns:
        A runner that captures stdout and stderr together.
    """
    return CliRunner()


@pytest.fixture
def deps_present(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Report every web dependency as installed.

    Args:
        monkeypatch: Patching helper, scoped to the test.
    """
    monkeypatch.setattr(web, "_check_dependencies", lambda: (True, [], []))


@pytest.fixture
def pid_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """
    Point the PID file at a temporary directory.

    Args:
        tmp_path: Per-test temporary directory.
        monkeypatch: Patching helper, scoped to the test.

    Returns:
        The path the commands will read and write.
    """
    path = tmp_path / "web.pid"
    monkeypatch.setattr(web, "get_pid_file", lambda: path)
    return path


@pytest.fixture
def started(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """
    Capture the configuration the server would be started with.

    Args:
        monkeypatch: Patching helper, scoped to the test.

    Returns:
        A dict filled in with ``config`` and ``mode`` when a start happens.
    """
    captured: dict[str, Any] = {}

    def foreground(config: Any) -> int:
        captured["config"] = config
        captured["mode"] = "foreground"
        return 0

    def daemon(config: Any, verbose: bool) -> int:
        captured["config"] = config
        captured["mode"] = "daemon"
        captured["verbose"] = verbose
        return 0

    monkeypatch.setattr(web, "_start_foreground", foreground)
    monkeypatch.setattr(web, "_start_daemon", daemon)
    return captured


def _commands() -> list[tuple[str, click.Command]]:
    """
    List the group and every command under it.

    Returns:
        Pairs of name and command, the group itself included.
    """
    ctx = click.Context(web.cli)
    found: list[tuple[str, click.Command]] = [("web", web.cli)]
    for name in web.cli.list_commands(ctx):
        command = web.cli.get_command(ctx, name)
        assert command is not None
        found.append((f"web {name}", command))
    return found


# ---------------------------------------------------------------------------
# The frozen surface
# ---------------------------------------------------------------------------


def test_the_contract_lists_the_commands_this_file_checks() -> None:
    """A typo in the contract key prefix would silently test nothing."""
    assert SUBCOMMANDS == ["install", "restart", "start", "status", "stop", "token"]


@pytest.mark.parametrize("name", SUBCOMMANDS)
def test_every_contract_command_exists_and_documents_itself(
    cli_runner: CliRunner, name: str
) -> None:
    """Each frozen subcommand answers --help with a description."""
    result = cli_runner.invoke(web.cli, [name, "--help"])

    assert result.exit_code == 0, result.output
    assert f"web {name}" in result.output or name in result.output
    assert result.output.strip()


def test_the_group_itself_documents_itself(cli_runner: CliRunner) -> None:
    """``wasm web --help`` lists every subcommand."""
    result = cli_runner.invoke(web.cli, ["--help"])

    assert result.exit_code == 0
    for name in SUBCOMMANDS:
        assert name in result.output


@pytest.mark.parametrize("name", [None, *SUBCOMMANDS])
def test_no_frozen_option_was_dropped(name: str | None) -> None:
    """Every option the argparse tree accepted still parses."""
    key = "web" if name is None else f"web {name}"
    command = dict(_commands())[key]
    declared = {
        opt
        for param in command.params
        if isinstance(param, click.Option)
        for opt in param.opts + param.secondary_opts
    }

    missing = set(WEB_CONTRACT["" if name is None else name]["options"]) - declared
    assert not missing, f"{key} no longer accepts {sorted(missing)}"


def test_the_group_declares_no_local_aliases() -> None:
    """The frozen surface gives ``web`` no alternative spellings to preserve."""
    assert all(not entry["aliases"] for entry in WEB_CONTRACT.values())


# ---------------------------------------------------------------------------
# Global flags are re-exposed, never redeclared
# ---------------------------------------------------------------------------


def test_no_command_redeclares_a_global_flag() -> None:
    """
    A global flag may be spelled after a subcommand, but never owned by it.

    Owning it is the argparse defect this migration removes: the subcommand's
    default overwrote whatever the user typed before the subcommand name.
    """
    for key, command in _commands():
        for param in command.params:
            if not isinstance(param, click.Option):
                continue
            if set(param.opts) & GLOBAL_OPTIONS:
                assert param.expose_value is False, (
                    f"{key} owns a value for {param.opts}; it must be expose_value=False "
                    "so the shared context stays authoritative"
                )

        if command.callback is None:
            continue
        parameters = set(inspect.signature(command.callback).parameters)
        assert not parameters & GLOBAL_PARAMETER_NAMES, (
            f"{key} takes {sorted(parameters & GLOBAL_PARAMETER_NAMES)} as a parameter"
        )


def test_a_late_verbose_flag_reaches_the_shared_context(
    cli_runner: CliRunner, deps_present: None, pid_file: Path, started: dict[str, Any]
) -> None:
    """``web start --daemon --verbose`` still turns verbosity on."""
    result = cli_runner.invoke(web.cli, ["start", "--daemon", "--verbose"])

    assert result.exit_code == 0, result.output
    assert started["verbose"] is True


# ---------------------------------------------------------------------------
# Refusing to put a root panel on the network
# ---------------------------------------------------------------------------


def test_binding_to_every_interface_without_protection_is_refused(
    cli_runner: CliRunner, deps_present: None, pid_file: Path, started: dict[str, Any]
) -> None:
    """The panel is a root shell; it does not reach the network by accident."""
    result = cli_runner.invoke(web.cli, ["start", "--host", ALL_INTERFACES])

    assert result.exit_code != 0
    assert isinstance(result.exception, SecurityError)
    assert "--allow-ip" in result.exception.details
    assert "--require-https" in result.exception.details
    assert "config" not in started


#: Every way of asking a socket for every interface. The first one is the
#: reported regression: an earlier version kept a set of loopback strings with
#: "" in it, so an empty host read as loopback and bound INADDR_ANY.
ALL_INTERFACES_SPELLINGS = ["", "0.0.0.0", "::", "*", "0", "[::]", " 0.0.0.0 "]  # noqa: S104


@pytest.mark.parametrize("spelling", ALL_INTERFACES_SPELLINGS)
def test_no_spelling_of_every_interface_escapes_the_refusal(
    cli_runner: CliRunner,
    deps_present: None,
    pid_file: Path,
    started: dict[str, Any],
    spelling: str,
) -> None:
    """
    The refusal is about the address bound, not about the string typed.

    Args:
        spelling: One way of writing "every interface".
    """
    result = cli_runner.invoke(web.cli, ["start", "--host", spelling])

    assert isinstance(result.exception, SecurityError), result.output
    assert "--allow-ip" in result.exception.details
    assert "config" not in started


def test_a_name_that_resolves_to_every_interface_is_refused(
    cli_runner: CliRunner,
    deps_present: None,
    pid_file: Path,
    started: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A line in /etc/hosts is not a way past the refusal."""
    monkeypatch.setattr(
        web.socket,
        "getaddrinfo",
        lambda *args, **kwargs: [(2, 1, 6, "", (ALL_INTERFACES, 0))],
    )

    result = cli_runner.invoke(web.cli, ["start", "--host", "panel.internal"])

    assert isinstance(result.exception, SecurityError), result.output
    assert "config" not in started


def test_a_host_that_cannot_be_resolved_is_treated_as_exposed(
    cli_runner: CliRunner,
    deps_present: None,
    pid_file: Path,
    started: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unknown is not loopback: guessing the other way publishes a root shell."""

    def unresolvable(*args: Any, **kwargs: Any) -> list[Any]:
        raise OSError("Name or service not known")

    monkeypatch.setattr(web.socket, "getaddrinfo", unresolvable)

    result = cli_runner.invoke(web.cli, ["start", "--host", "panel.invalid"])

    assert isinstance(result.exception, SecurityError), result.output
    assert "could not resolve" in result.exception.details
    assert "config" not in started


@pytest.mark.parametrize("spelling", ["127.0.0.1", "::1", "localhost", "127.1", "[::1]"])
def test_every_spelling_of_loopback_still_starts_unprotected(
    cli_runner: CliRunner,
    deps_present: None,
    pid_file: Path,
    started: dict[str, Any],
    spelling: str,
) -> None:
    """
    Nothing beyond this machine can reach these, so no flags are demanded.

    Args:
        spelling: One way of writing an address only this machine answers on.
    """
    result = cli_runner.invoke(web.cli, ["start", "--host", spelling])

    assert result.exit_code == 0, result.output
    assert started["config"].allowed_hosts != []


def test_an_empty_host_is_reported_as_the_address_it_would_bind(
    cli_runner: CliRunner, deps_present: None, pid_file: Path, started: dict[str, Any]
) -> None:
    """The message names an address, so the operator can act on it."""
    result = cli_runner.invoke(web.cli, ["start", "--host", ""])

    assert isinstance(result.exception, SecurityError)
    assert ALL_INTERFACES in str(result.exception)


def test_a_wildcard_host_is_normalised_before_it_is_bound(
    cli_runner: CliRunner, deps_present: None, pid_file: Path, started: dict[str, Any]
) -> None:
    """What was checked and what is served have to be the same address."""
    result = cli_runner.invoke(web.cli, ["start", "--host", "", "--allow-ip", "10.0.0.0/24"])

    assert result.exit_code == 0, result.output
    assert started["config"].host == ALL_INTERFACES


def test_a_refused_exposure_changes_nothing_first(
    cli_runner: CliRunner,
    deps_present: None,
    pid_file: Path,
    started: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The refusal comes before the stale PID file is cleared, not after."""
    pid_file.write_text("4321")

    def gone(pid: int, sig: int) -> None:
        raise ProcessLookupError

    monkeypatch.setattr(os, "kill", gone)

    result = cli_runner.invoke(web.cli, ["start", "--host", ALL_INTERFACES])

    assert isinstance(result.exception, SecurityError)
    assert pid_file.read_text() == "4321"


def test_missing_dependencies_are_reported_before_the_panel_is_configured(
    cli_runner: CliRunner,
    pid_file: Path,
    started: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    SecurityConfig lives behind fastapi.

    Building it before the dependency check would answer a missing package with
    an ImportError traceback instead of the line that says how to install it.
    """
    monkeypatch.setattr(
        web, "_check_dependencies", lambda: (False, ["python3-fastapi"], ["fastapi>=0.109.0"])
    )
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)

    result = cli_runner.invoke(web.cli, ["start"])

    assert result.exit_code == 1
    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert "python3-fastapi" in result.output


def test_require_https_without_material_is_refused(
    cli_runner: CliRunner, deps_present: None, pid_file: Path, started: dict[str, Any]
) -> None:
    """Asking for TLS with no key fails before the port is bound."""
    result = cli_runner.invoke(web.cli, ["start", "--require-https"])

    assert isinstance(result.exception, SecurityError)
    assert "--tls-cert" in result.exception.details
    assert "config" not in started


def test_a_whitelist_justifies_the_exposure(
    cli_runner: CliRunner, deps_present: None, pid_file: Path, started: dict[str, Any]
) -> None:
    """Declaring who may connect is one of the ways to open the panel up."""
    result = cli_runner.invoke(
        web.cli,
        ["start", "--host", ALL_INTERFACES, "--allow-ip", "10.0.0.0/24", "--allow-ip", "10.1.0.1"],
    )

    assert result.exit_code == 0, result.output
    assert started["config"].ip_whitelist == ["10.0.0.0/24", "10.1.0.1"]
    assert started["config"].allowed_hosts == []


def test_tls_material_opens_the_panel_up(
    cli_runner: CliRunner,
    deps_present: None,
    pid_file: Path,
    started: dict[str, Any],
    tmp_path: Path,
) -> None:
    """Terminating TLS in the panel is the other way to justify the exposure."""
    cert = tmp_path / "fullchain.pem"
    key = tmp_path / "privkey.pem"
    cert.write_text("cert")
    key.write_text("key")

    result = cli_runner.invoke(
        web.cli,
        [
            "start",
            "--host",
            ALL_INTERFACES,
            "--require-https",
            "--tls-cert",
            str(cert),
            "--tls-key",
            str(key),
        ],
    )

    assert result.exit_code == 0, result.output
    assert started["config"].require_https is True
    assert started["config"].ssl_certfile == str(cert)
    assert started["config"].ssl_keyfile == str(key)


def test_a_trusted_proxy_is_carried_into_the_configuration(
    cli_runner: CliRunner, deps_present: None, pid_file: Path, started: dict[str, Any]
) -> None:
    """The nginx-in-front deployment can declare its proxy."""
    result = cli_runner.invoke(web.cli, ["start", "--trusted-proxy", "10.9.9.1"])

    assert result.exit_code == 0, result.output
    assert started["config"].trusted_proxies == ["10.9.9.1"]


def test_loopback_needs_no_flags(
    cli_runner: CliRunner, deps_present: None, pid_file: Path, started: dict[str, Any]
) -> None:
    """The safe default stays usable with nothing typed."""
    result = cli_runner.invoke(web.cli, ["start"])

    assert result.exit_code == 0, result.output
    assert started["config"].host == "127.0.0.1"
    assert started["config"].port == 8080
    assert started["mode"] == "foreground"


# ---------------------------------------------------------------------------
# Values are rejected before anything is touched
# ---------------------------------------------------------------------------


def test_a_non_numeric_port_is_a_usage_error(
    cli_runner: CliRunner, deps_present: None, pid_file: Path, started: dict[str, Any]
) -> None:
    """Click rejects the value; no traceback and nothing started."""
    result = cli_runner.invoke(web.cli, ["start", "--port", "http"])

    assert result.exit_code == 2
    assert "Invalid value" in result.output
    assert "config" not in started


def test_a_missing_certificate_is_a_usage_error(
    cli_runner: CliRunner, deps_present: None, pid_file: Path, started: dict[str, Any]
) -> None:
    """A path that is not there is caught by the parser, not by uvicorn."""
    result = cli_runner.invoke(web.cli, ["start", "--tls-cert", "/nonexistent/fullchain.pem"])

    assert result.exit_code == 2
    assert "config" not in started


def test_an_unknown_option_is_a_usage_error(cli_runner: CliRunner) -> None:
    """Typos exit 2 rather than being ignored."""
    result = cli_runner.invoke(web.cli, ["start", "--allow-everyone"])

    assert result.exit_code == 2


def test_an_unknown_subcommand_is_a_usage_error(cli_runner: CliRunner) -> None:
    """The group refuses an action it does not have."""
    result = cli_runner.invoke(web.cli, ["reboot"])

    assert result.exit_code == 2


def test_install_refuses_both_package_managers(cli_runner: CliRunner) -> None:
    """--apt and --pip contradict each other, so it is a usage error."""
    result = cli_runner.invoke(web.cli, ["install", "--apt", "--pip"])

    assert result.exit_code == 2
    assert "--apt" in result.output


# ---------------------------------------------------------------------------
# A rehearsal that changes nothing
# ---------------------------------------------------------------------------


@pytest.fixture
def seams() -> Iterator[None]:
    """
    Put the process-wide runner and filesystem back after the test.

    ``--dry-run`` swaps both, and they are module globals: a test that turns it
    on and does not put them back leaks a filesystem that refuses to write into
    every test that follows.

    Yields:
        None.
    """
    from wasm.core.fs import set_fs
    from wasm.core.runner import set_runner

    try:
        yield
    finally:
        set_fs(None)
        set_runner(None)


@pytest.mark.parametrize("argv", [["start", "--dry-run"], ["--dry-run", "start"]])
def test_a_rehearsed_start_binds_nothing(
    cli_runner: CliRunner,
    deps_present: None,
    pid_file: Path,
    started: dict[str, Any],
    seams: None,
    argv: list[str],
) -> None:
    """
    Serving is neither a subprocess nor a write, so only the command may stop it.

    Args:
        argv: The invocation, with the flag typed before and after the command.
    """
    result = cli_runner.invoke(web.cli, argv)

    assert result.exit_code == 0, result.output
    assert "config" not in started
    assert not pid_file.exists()
    assert "would serve the panel" in result.output


def test_a_rehearsed_start_installs_the_filesystem_seam(
    cli_runner: CliRunner,
    deps_present: None,
    pid_file: Path,
    started: dict[str, Any],
    seams: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A stale PID file is a ``Path.unlink``: only the fs seam holds it back.

    Wiring just the command runner is what let ``--dry-run`` announce that
    nothing would change and then delete a file.
    """
    pid_file.write_text("4321")

    def gone(pid: int, sig: int) -> None:
        raise ProcessLookupError

    monkeypatch.setattr(os, "kill", gone)

    result = cli_runner.invoke(web.cli, ["start", "--dry-run"])

    assert result.exit_code == 0, result.output
    assert pid_file.read_text() == "4321"


def test_a_rehearsed_stop_signals_nothing(
    cli_runner: CliRunner, pid_file: Path, seams: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The panel is still running afterwards, and its PID file is still there."""
    pid_file.write_text("4321")
    signalled: list[tuple[int, int]] = []
    monkeypatch.setattr(os, "kill", lambda pid, sig: signalled.append((pid, sig)))

    result = cli_runner.invoke(web.cli, ["stop", "--dry-run"])

    assert result.exit_code == 0, result.output
    assert signalled == []
    assert pid_file.read_text() == "4321"
    assert "would send SIGTERM to PID 4321" in result.output


def test_a_rehearsed_rotation_keeps_the_token_in_use(
    cli_runner: CliRunner, deps_present: None, state_dir: Path, seams: None
) -> None:
    """The signing key and the token hash are written outside both seams."""
    cli_runner.invoke(web.cli, ["token", "--new", "--yes"])
    token_before = (state_dir / "web-token").read_text()
    key_before = (state_dir / "web-secret").read_text()

    result = cli_runner.invoke(web.cli, ["token", "--regenerate", "--yes", "--dry-run"])

    assert result.exit_code == 0, result.output
    assert (state_dir / "web-token").read_text() == token_before
    assert (state_dir / "web-secret").read_text() == key_before
    assert "would rotate the signing key" in result.output


def test_a_rehearsal_announces_itself_once(
    cli_runner: CliRunner,
    deps_present: None,
    pid_file: Path,
    started: dict[str, Any],
    seams: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    The flag before and after the command name is one rehearsal, not two.

    The announcement comes from the shared context's logger, which binds its
    stream when it is built, so that logger is redirected too.
    """
    from wasm.cli import app as cli_app

    real_logger = cli_app.Logger
    monkeypatch.setattr(
        cli_app, "Logger", lambda **kwargs: real_logger(stream=sys.stdout, **kwargs)
    )

    state = cli_app.Context()
    result = cli_runner.invoke(web.cli, ["--dry-run", "start", "--dry-run"], obj=state)

    assert result.exit_code == 0, result.output
    assert state.dry_run_active is True
    assert result.output.count("Dry run:") == 1


# ---------------------------------------------------------------------------
# Stop and status
# ---------------------------------------------------------------------------


def test_stop_signals_the_recorded_process(
    cli_runner: CliRunner, pid_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``web stop`` sends SIGTERM to the PID it wrote and clears the file."""
    pid_file.write_text("4321")
    signalled: list[tuple[int, int]] = []
    monkeypatch.setattr(os, "kill", lambda pid, sig: signalled.append((pid, sig)))

    result = cli_runner.invoke(web.cli, ["stop"])

    assert result.exit_code == 0, result.output
    assert signalled == [(4321, 15)]
    assert not pid_file.exists()


def test_stop_is_quiet_when_nothing_runs(cli_runner: CliRunner, pid_file: Path) -> None:
    """Stopping a stopped panel succeeds."""
    result = cli_runner.invoke(web.cli, ["stop"])

    assert result.exit_code == 0
    assert "not running" in result.output


def test_status_reports_a_stopped_panel(cli_runner: CliRunner, pid_file: Path) -> None:
    """``web status`` says so rather than failing."""
    result = cli_runner.invoke(web.cli, ["status"])

    assert result.exit_code == 0
    assert "not running" in result.output


def test_restart_stops_then_starts(
    cli_runner: CliRunner,
    deps_present: None,
    pid_file: Path,
    started: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Restart is a stop and a start, with the new options applied."""
    order: list[str] = []

    def record_stop(verbose: bool, *, dry_run: bool = False) -> int:
        order.append("stop")
        return 0

    monkeypatch.setattr(web, "_stop", record_stop)
    monkeypatch.setattr(web.time, "sleep", lambda seconds: None)

    result = cli_runner.invoke(web.cli, ["restart", "--port", "9443"])

    assert result.exit_code == 0, result.output
    assert order == ["stop"]
    assert started["config"].port == 9443


# ---------------------------------------------------------------------------
# Tokens
# ---------------------------------------------------------------------------


@pytest.fixture
def state_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """
    Keep secrets, sessions and the audit log inside the test's directory.

    Args:
        tmp_path: Per-test temporary directory.
        monkeypatch: Patching helper, scoped to the test.

    Returns:
        The state directory the panel will use.
    """
    from wasm.web.auth import STATE_DIR_ENV

    directory = tmp_path / "state"
    directory.mkdir()
    monkeypatch.setenv(STATE_DIR_ENV, str(directory))
    return directory


def test_a_bare_token_command_does_not_touch_the_token_in_use(
    cli_runner: CliRunner, deps_present: None, state_dir: Path
) -> None:
    """
    The reported defect: ``wasm web token`` silently revoked the root credential.

    Operators run it to look the token up, so a bare invocation reports and
    changes nothing. Rotating is what needs to be typed out.
    """
    cli_runner.invoke(web.cli, ["token", "--new", "--yes"])
    before = (state_dir / "web-token").read_text()

    result = cli_runner.invoke(web.cli, ["token"])

    assert result.exit_code == 0, result.output
    assert (state_dir / "web-token").read_text() == before
    assert "Access Token: wasm_" not in result.output
    assert "wasm web token --new" in result.output


def test_the_status_report_says_when_the_token_was_issued(
    cli_runner: CliRunner, deps_present: None, state_dir: Path
) -> None:
    """Showing is the default, so it has to be worth reading."""
    cli_runner.invoke(web.cli, ["token", "--new", "--yes"])

    result = cli_runner.invoke(web.cli, ["token"])

    assert result.exit_code == 0, result.output
    assert "web-token" in result.output
    assert "salted hash" in result.output


def test_the_status_report_says_when_no_token_exists_yet(
    cli_runner: CliRunner, deps_present: None, state_dir: Path
) -> None:
    """A fresh install is a state the report has to name, not a crash."""
    result = cli_runner.invoke(web.cli, ["token"])

    assert result.exit_code == 0, result.output
    assert "no token" in result.output
    assert not (state_dir / "web-token").exists()


@pytest.mark.parametrize("flag", ["--new", "--rotate"])
def test_issuing_a_token_prints_it(
    cli_runner: CliRunner, deps_present: None, state_dir: Path, flag: str
) -> None:
    """
    Both spellings of the explicit request issue a token.

    Args:
        flag: The spelling under test.
    """
    result = cli_runner.invoke(web.cli, ["token", flag])

    assert result.exit_code == 0, result.output
    assert "Access Token: wasm_" in result.output
    assert (state_dir / "web-token").exists()


def test_token_is_verified_by_the_manager_that_issued_it(
    cli_runner: CliRunner, deps_present: None, state_dir: Path
) -> None:
    """The printed token is the one the panel will accept at the login form."""
    from wasm.web.auth import SecurityConfig, TokenManager

    result = cli_runner.invoke(web.cli, ["token", "--new"])
    printed = next(
        line.split("Access Token:", 1)[1].strip()
        for line in result.output.splitlines()
        if "Access Token:" in line
    )

    assert TokenManager(SecurityConfig()).verify_master_token(printed) is True


def test_regenerate_rotates_the_signing_key(
    cli_runner: CliRunner, deps_present: None, state_dir: Path
) -> None:
    """--regenerate replaces the key, which is what revokes every session."""
    cli_runner.invoke(web.cli, ["token", "--new", "--yes"])
    first_key = (state_dir / "web-secret").read_text()

    result = cli_runner.invoke(web.cli, ["token", "--regenerate", "--yes"])

    assert result.exit_code == 0, result.output
    assert "revoked" in result.output
    assert (state_dir / "web-secret").read_text() != first_key


def test_replacing_a_token_in_use_asks_first(
    cli_runner: CliRunner, deps_present: None, state_dir: Path
) -> None:
    """The prompt names the file and the consequence, and declining changes nothing."""
    cli_runner.invoke(web.cli, ["token", "--new", "--yes"])
    before = (state_dir / "web-token").read_text()

    result = cli_runner.invoke(web.cli, ["token", "--new"], input="n\n")

    assert result.exit_code != 0
    assert "web-token" in result.output
    assert "stops working" in result.output
    assert (state_dir / "web-token").read_text() == before


def test_regenerating_names_the_sessions_it_would_close(
    cli_runner: CliRunner, deps_present: None, state_dir: Path
) -> None:
    """The consequence an operator cannot undo has to be in the question."""
    cli_runner.invoke(web.cli, ["token", "--new", "--yes"])
    before = (state_dir / "web-secret").read_text()

    result = cli_runner.invoke(web.cli, ["token", "--regenerate"], input="n\n")

    assert result.exit_code != 0
    assert "logged out" in result.output
    assert (state_dir / "web-secret").read_text() == before


def test_the_first_token_is_not_worth_a_prompt(
    cli_runner: CliRunner, deps_present: None, state_dir: Path
) -> None:
    """There is nothing to invalidate before a token exists."""
    result = cli_runner.invoke(web.cli, ["token", "--new"], input="")

    assert result.exit_code == 0, result.output
    assert "Access Token: wasm_" in result.output


def test_token_reports_missing_dependencies_instead_of_crashing(
    cli_runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without fastapi there is no panel, and the message says how to get one."""
    monkeypatch.setattr(
        web, "_check_dependencies", lambda: (False, ["python3-fastapi"], ["fastapi>=0.109.0"])
    )

    result = cli_runner.invoke(web.cli, ["token"])

    assert result.exit_code == 1
    assert "python3-fastapi" in result.output
    assert "wasm web install" in result.output


# ---------------------------------------------------------------------------
# Installing dependencies
# ---------------------------------------------------------------------------


def test_install_with_pip_builds_the_exact_argv(
    cli_runner: CliRunner, runner: FakeRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The command that ends up running is the one under test."""
    monkeypatch.setattr(
        web, "_check_dependencies", lambda: (False, ["python3-fastapi"], ["fastapi>=0.109.0"])
    )
    monkeypatch.setattr(web, "_is_externally_managed", lambda: False)

    result = cli_runner.invoke(web.cli, ["install", "--pip"])

    assert runner.calls == [(sys.executable, "-m", "pip", "install", "--user", "fastapi>=0.109.0")]
    assert result.exit_code == 1  # the module is still missing afterwards


def test_install_with_apt_builds_the_exact_argv(
    cli_runner: CliRunner, runner: FakeRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """apt is driven through the runner, never through a shell."""
    monkeypatch.setattr(
        web, "_check_dependencies", lambda: (False, ["python3-fastapi"], ["fastapi>=0.109.0"])
    )

    cli_runner.invoke(web.cli, ["install", "--apt"])

    assert runner.calls[0] == ("apt-get", "install", "-y", "python3-fastapi")


def test_install_is_a_no_op_when_everything_is_present(
    cli_runner: CliRunner, deps_present: None, runner: FakeRunner
) -> None:
    """Nothing is installed when nothing is missing."""
    result = cli_runner.invoke(web.cli, ["install"])

    assert result.exit_code == 0
    assert runner.calls == []


# ---------------------------------------------------------------------------
# Dependency list hygiene
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("module", ["passlib", "aiofiles"])
def test_unused_dependencies_are_not_demanded(module: str) -> None:
    """
    The panel imports neither, so requiring them only sends operators shopping.

    Args:
        module: Import name that must not appear in the dependency table.
    """
    assert module not in web.WEB_DEPENDENCIES


def test_every_declared_dependency_is_actually_imported() -> None:
    """A dependency nobody imports cannot be a reason to refuse to start."""
    sources = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (Path(web.__file__).parent.parent.parent / "web").rglob("*.py")
    )

    for module in web.WEB_DEPENDENCIES:
        assert f"import {module}" in sources or f"from {module}" in sources


def test_the_module_does_not_reach_for_subprocess() -> None:
    """Process execution goes through the runner, so there is a seam to fake."""
    source = Path(web.__file__).read_text(encoding="utf-8")

    assert "import subprocess" not in source
