# Copyright (c) 2024-2026 Yago Lopez Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
Tests for the console as a systemd service: ``wasm web enable`` and friends.

The owner of a VPS did not know three things, and each is defended here:

- the console binds to 127.0.0.1, so it needs ``ssh -L`` - the tunnel line is
  printed by every start, and set apart so it is not missed;
- Ctrl+C stops a foreground console - the banner says so, and names
  ``wasm web enable``;
- ``wasm web start -d`` does not survive a reboot - ``wasm web enable`` writes
  ``wasm-web.service``, which does.

And one rule that must never break: the token is printed to the operator's
terminal by ``enable``, never to the journal by the service.
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from wasm.cli.commands import web
from wasm.core.exceptions import SecurityError, ServiceError
from wasm.core.runner import FakeRunner
from wasm.managers.service_manager import WASM_UNIT_MARKER, ServiceManager

ALL_INTERFACES = "0.0.0.0"  # noqa: S104 - the address under test, never bound

#: Where the tests pretend the wasm entry point is installed.
WASM_BIN = "/usr/bin/wasm"

#: What systemd answers for a console that came up.
RUNNING = "ActiveState=active\nSubState=running\nMainPID=4321\nNRestarts=0\nResult=success\n"

#: What systemd answers for a console that exited on start.
CRASHED = "ActiveState=failed\nSubState=failed\nMainPID=0\nNRestarts=5\nResult=exit-code\n"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def logger_follows_the_current_stdout(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Make logger output visible to the Click test runner.

    Args:
        monkeypatch: Patching helper, scoped to the test.
    """
    real_logger = web.Logger

    class StdoutLogger(real_logger):  # type: ignore[valid-type, misc]
        """A logger that resolves stdout when it is built, not when imported."""

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            kwargs.setdefault("stream", sys.stdout)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(web, "Logger", StdoutLogger)


@pytest.fixture(autouse=True)
def isolated_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """
    Point the layered configuration at a per-test file.

    Args:
        tmp_path: Per-test temporary directory.
        monkeypatch: Patching helper, scoped to the test.

    Yields:
        None.
    """
    from wasm.core import config as config_module

    monkeypatch.setattr(config_module, "DEFAULT_CONFIG_PATH", tmp_path / "config.yaml")
    config_module.Config.reset_instance()
    yield
    config_module.Config.reset_instance()


@pytest.fixture(autouse=True)
def state_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """
    Keep the token hash and the signing key inside the test's directory.

    Args:
        tmp_path: Per-test temporary directory.
        monkeypatch: Patching helper, scoped to the test.

    Returns:
        The state directory the console uses.
    """
    from wasm.web.auth import STATE_DIR_ENV

    directory = tmp_path / "state"
    directory.mkdir()
    monkeypatch.setenv(STATE_DIR_ENV, str(directory))
    return directory


@pytest.fixture(autouse=True)
def unit_dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """
    Point every unit directory systemd would read at a temporary tree.

    Args:
        tmp_path: Per-test temporary directory.
        monkeypatch: Patching helper, scoped to the test.

    Returns:
        The managed directory under ``managed``, the distribution's under
        ``distro``.
    """
    managed = tmp_path / "etc/systemd/system"
    distro = tmp_path / "usr/lib/systemd/system"
    for directory in (managed, distro):
        directory.mkdir(parents=True)
    monkeypatch.setattr(ServiceManager, "SYSTEMD_DIR", managed)
    monkeypatch.setattr(ServiceManager, "UNIT_SEARCH_DIRS", (managed, distro))
    return {"managed": managed, "distro": distro}


@pytest.fixture(autouse=True)
def pid_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """
    Point the daemon's PID file at a temporary directory.

    Args:
        tmp_path: Per-test temporary directory.
        monkeypatch: Patching helper, scoped to the test.

    Returns:
        The path the commands read and write.
    """
    path = tmp_path / "web.pid"
    monkeypatch.setattr(web, "get_pid_file", lambda: path)
    return path


@pytest.fixture(autouse=True)
def deps_present(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Report every web dependency as installed.

    Args:
        monkeypatch: Patching helper, scoped to the test.
    """
    monkeypatch.setattr(web, "_check_dependencies", lambda: (True, [], []))


@pytest.fixture(autouse=True)
def wasm_on_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Answer ``shutil.which("wasm")`` with a fixed absolute path.

    Args:
        monkeypatch: Patching helper, scoped to the test.
    """
    monkeypatch.setattr(web.shutil, "which", lambda name: WASM_BIN if name == "wasm" else None)


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Make the start-up wait instantaneous.

    Args:
        monkeypatch: Patching helper, scoped to the test.
    """
    monkeypatch.setattr(web.time, "sleep", lambda seconds: None)


@pytest.fixture(autouse=True)
def seams() -> Iterator[None]:
    """
    Put the process-wide filesystem back after a rehearsal installed its own.

    Yields:
        None.
    """
    from wasm.core.fs import set_fs

    try:
        yield
    finally:
        set_fs(None)


@pytest.fixture
def cli_runner() -> CliRunner:
    """
    Provide a Click test runner.

    Returns:
        A runner that captures stdout and stderr together.
    """
    return CliRunner()


@pytest.fixture
def listening(runner: FakeRunner, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """
    Pretend the port is free until systemd starts the console, then held.

    Args:
        runner: The fake command runner installed process-wide.
        monkeypatch: Patching helper, scoped to the test.

    Returns:
        A switch: set ``other`` to True to have something else hold the port
        from the start.
    """
    state: dict[str, Any] = {"other": False}

    def in_use(host: str, port: int) -> bool:
        return state["other"] or runner.ran("systemctl", "restart", "wasm-web.service")

    monkeypatch.setattr(web, "_port_in_use", in_use)
    return state


@pytest.fixture
def systemd_up(runner: FakeRunner) -> FakeRunner:
    """
    Script systemd to report the console unit as running.

    Args:
        runner: The fake command runner installed process-wide.

    Returns:
        The runner.
    """
    runner.script(["systemctl", "is-active"], stdout="active\n")
    runner.script(["systemctl", "is-enabled"], stdout="enabled\n")
    runner.script(["systemctl", "show", "wasm-web.service", "--no-pager"], stdout=RUNNING)
    return runner


def _installed_unit(unit_dirs: dict[str, Path], body: str | None = None) -> Path:
    """
    Put a console unit in the managed directory, as ``enable`` would have.

    Args:
        unit_dirs: The temporary unit directories.
        body: Unit body; a marked minimal unit by default.

    Returns:
        The unit file.
    """
    path = unit_dirs["managed"] / "wasm-web.service"
    path.write_text(
        body
        or (
            f"# {WASM_UNIT_MARKER}\n[Service]\n"
            f"ExecStart={WASM_BIN} web start --under-systemd --host 127.0.0.1 --port 8080\n"
        )
    )
    return path


def _printed_token(output: str) -> str:
    """
    Pull the access token out of a banner.

    Args:
        output: Everything the command printed.

    Returns:
        The token.
    """
    return next(
        line.split("Access Token:", 1)[1].strip()
        for line in output.splitlines()
        if "Access Token:" in line
    )


# ---------------------------------------------------------------------------
# wasm web enable
# ---------------------------------------------------------------------------


def test_enable_writes_a_marked_unit_that_runs_the_console_in_the_foreground(
    cli_runner: CliRunner,
    systemd_up: FakeRunner,
    listening: dict[str, Any],
    unit_dirs: dict[str, Path],
) -> None:
    """ExecStart is the absolute binary, running ``web start`` without ``-d``."""
    result = cli_runner.invoke(web.cli, ["enable", "--port", "9090"])

    assert result.exit_code == 0, result.output
    unit = (unit_dirs["managed"] / "wasm-web.service").read_text()
    assert WASM_UNIT_MARKER in unit
    exec_start = next(line for line in unit.splitlines() if line.startswith("ExecStart="))
    assert exec_start == (
        f"ExecStart={WASM_BIN} web start --under-systemd --host 127.0.0.1 --port 9090"
    )
    assert "--daemon" not in unit
    assert "Restart=on-failure" in unit
    assert "[Install]" in unit
    assert "WantedBy=multi-user.target" in unit


def test_the_unit_is_hardened_without_breaking_what_the_console_does_as_root(
    cli_runner: CliRunner,
    systemd_up: FakeRunner,
    listening: dict[str, Any],
    unit_dirs: dict[str, Path],
) -> None:
    """
    The console installs packages, writes /etc and deploys into /var/www:
    nothing may make those read-only, and every directive carries its reason.
    """
    cli_runner.invoke(web.cli, ["enable"])
    unit = (unit_dirs["managed"] / "wasm-web.service").read_text()
    directives = {
        line.split("=", 1)[0]: line.split("=", 1)[1]
        for line in unit.splitlines()
        if "=" in line and not line.startswith("#")
    }

    assert directives.get("NoNewPrivileges") == "true"
    assert directives.get("User") == "root"
    for forbidden in ("ProtectSystem", "ProtectHome", "ReadOnlyPaths", "PrivateDevices"):
        assert forbidden not in directives, f"{forbidden} would break root operations"
    assert "MemoryDenyWriteExecute" not in directives, "Node builds need a JIT"


def test_enable_turns_the_unit_on_through_systemd(
    cli_runner: CliRunner,
    systemd_up: FakeRunner,
    listening: dict[str, Any],
) -> None:
    """The unit is reloaded, enabled at boot, and (re)started now."""
    result = cli_runner.invoke(web.cli, ["enable"])

    assert result.exit_code == 0, result.output
    calls = systemd_up.calls
    reload_at = calls.index(("systemctl", "daemon-reload"))
    enable_at = calls.index(("systemctl", "enable", "wasm-web.service"))
    restart_at = calls.index(("systemctl", "restart", "wasm-web.service"))
    assert reload_at < enable_at < restart_at


def test_enable_prints_the_token_the_service_serves(
    cli_runner: CliRunner,
    systemd_up: FakeRunner,
    listening: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    The token is printed by ``enable``, in the banner a foreground start
    prints, with the SSH tunnel line on loopback.
    """
    from wasm.web.auth import SecurityConfig, TokenManager

    monkeypatch.setattr("wasm.core.net.server_address", lambda: "198.51.100.7")
    monkeypatch.setattr("wasm.core.net._current_user", lambda: "root")

    result = cli_runner.invoke(web.cli, ["enable"])

    assert result.exit_code == 0, result.output
    token = _printed_token(result.output)
    assert TokenManager(SecurityConfig()).verify_master_token(token) is True
    assert "ssh -L 8080:127.0.0.1:8080 root@198.51.100.7" in result.output
    assert "wasm-web.service" in result.output
    assert "wasm web disable" in result.output


def test_the_token_never_reaches_the_unit_file(
    cli_runner: CliRunner,
    systemd_up: FakeRunner,
    listening: dict[str, Any],
    unit_dirs: dict[str, Path],
) -> None:
    """A unit file is world-readable; the token lives only on the terminal."""
    result = cli_runner.invoke(web.cli, ["enable"])

    token = _printed_token(result.output)
    assert token not in (unit_dirs["managed"] / "wasm-web.service").read_text()


def test_enable_refuses_what_start_refuses(
    cli_runner: CliRunner,
    runner: FakeRunner,
    listening: dict[str, Any],
    unit_dirs: dict[str, Path],
) -> None:
    """The same rule, not a copy of it: every interface without TLS is refused."""
    result = cli_runner.invoke(web.cli, ["enable", "--host", ALL_INTERFACES])

    assert isinstance(result.exception, SecurityError)
    assert "--self-signed" in result.exception.details
    assert not (unit_dirs["managed"] / "wasm-web.service").exists()
    assert runner.calls == []


def test_enable_carries_tls_options_as_absolute_paths(
    cli_runner: CliRunner,
    systemd_up: FakeRunner,
    listening: dict[str, Any],
    unit_dirs: dict[str, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A path typed relative to the operator's shell means nothing to systemd,
    and a path with a space in it has to stay one argument.
    """
    folder = tmp_path / "my certs"
    folder.mkdir()
    (folder / "panel.crt").write_text("cert")
    (folder / "panel.key").write_text("key")
    monkeypatch.chdir(folder)
    monkeypatch.setattr("wasm.web.server.verify_tls_material", lambda config: ("", ""))

    result = cli_runner.invoke(
        web.cli,
        [
            "enable",
            "--host",
            ALL_INTERFACES,
            "--tls-cert",
            "panel.crt",
            "--tls-key",
            "panel.key",
            "--allow-ip",
            "10.0.0.0/8",
            "--trusted-proxy",
            "127.0.0.1",
        ],
    )

    assert result.exit_code == 0, result.output
    unit = (unit_dirs["managed"] / "wasm-web.service").read_text()
    assert f'--tls-cert "{folder}/panel.crt"' in unit
    assert f'--tls-key "{folder}/panel.key"' in unit
    assert "--allow-ip 10.0.0.0/8" in unit
    assert "--trusted-proxy 127.0.0.1" in unit
    assert f"--host {ALL_INTERFACES}" in unit


def test_enable_with_self_signed_mints_the_pair_before_the_service_starts(
    cli_runner: CliRunner,
    systemd_up: FakeRunner,
    listening: dict[str, Any],
    unit_dirs: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A minting failure is shown to the operator, not buried in the journal."""
    minted: list[str] = []
    monkeypatch.setattr(
        "wasm.managers.cert_manager.CertManager.generate_self_signed",
        lambda self, host, cert, key: minted.append(host) or True,
    )

    result = cli_runner.invoke(web.cli, ["enable", "--host", ALL_INTERFACES, "--self-signed"])

    assert result.exit_code == 0, result.output
    assert minted
    assert "--self-signed" in (unit_dirs["managed"] / "wasm-web.service").read_text()
    assert "https://" in result.output


def test_enable_refuses_while_a_background_console_runs(
    cli_runner: CliRunner,
    runner: FakeRunner,
    listening: dict[str, Any],
    pid_file: Path,
    unit_dirs: dict[str, Path],
) -> None:
    """Two consoles cannot hold one port; the daemon has to be stopped first."""
    pid_file.write_text(str(os.getpid()))

    result = cli_runner.invoke(web.cli, ["enable"])

    assert result.exit_code == 1
    assert "wasm web stop" in result.output
    assert "Access Token" not in result.output
    assert not (unit_dirs["managed"] / "wasm-web.service").exists()


def test_enable_refuses_a_port_something_else_holds(
    cli_runner: CliRunner,
    runner: FakeRunner,
    listening: dict[str, Any],
    unit_dirs: dict[str, Path],
) -> None:
    """The service would only crash-loop on it."""
    listening["other"] = True

    result = cli_runner.invoke(web.cli, ["enable"])

    assert result.exit_code == 1
    assert "already listening on 127.0.0.1:8080" in result.output
    assert not (unit_dirs["managed"] / "wasm-web.service").exists()


def test_enable_without_a_wasm_binary_on_path_says_so(
    cli_runner: CliRunner,
    runner: FakeRunner,
    listening: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    unit_dirs: dict[str, Path],
) -> None:
    """systemd has no PATH of the operator's; a relative ExecStart never starts."""
    monkeypatch.setattr(web.shutil, "which", lambda name: None)

    result = cli_runner.invoke(web.cli, ["enable"])

    assert isinstance(result.exception, ServiceError)
    assert not (unit_dirs["managed"] / "wasm-web.service").exists()


def test_a_console_that_does_not_come_up_shows_the_journal_and_no_token(
    cli_runner: CliRunner,
    runner: FakeRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A token for a console that is not running is a credential for nothing."""
    monkeypatch.setattr(web, "_port_in_use", lambda host, port: False)
    runner.script(["systemctl", "is-active"], stdout="failed\n")
    runner.script(["systemctl", "show", "wasm-web.service", "--no-pager"], stdout=CRASHED)
    runner.script(["journalctl", "-u", "wasm-web.service"], stdout="OSError: [Errno 98] in use")

    result = cli_runner.invoke(web.cli, ["enable"])

    assert isinstance(result.exception, ServiceError)
    assert "OSError: [Errno 98] in use" in result.exception.details
    assert "Access Token" not in result.output


def test_enable_twice_replaces_the_options_and_restarts(
    cli_runner: CliRunner,
    systemd_up: FakeRunner,
    listening: dict[str, Any],
    unit_dirs: dict[str, Path],
) -> None:
    """Re-running enable is how the options of a running service change."""
    unit = _installed_unit(unit_dirs)

    result = cli_runner.invoke(web.cli, ["enable", "--port", "9443"])

    assert result.exit_code == 0, result.output
    assert "--port 9443" in unit.read_text()
    assert systemd_up.ran("systemctl", "restart", "wasm-web.service")


def test_enable_will_not_eclipse_a_unit_the_system_ships(
    cli_runner: CliRunner,
    runner: FakeRunner,
    listening: dict[str, Any],
    unit_dirs: dict[str, Path],
) -> None:
    """Ownership rules apply: a same-named distribution unit is not WASM's."""
    (unit_dirs["distro"] / "wasm-web.service").write_text("[Service]\nExecStart=/bin/true\n")

    result = cli_runner.invoke(web.cli, ["enable"])

    assert isinstance(result.exception, ServiceError)
    assert not (unit_dirs["managed"] / "wasm-web.service").exists()
    assert not runner.ran("systemctl", "enable")


def test_a_rehearsed_enable_writes_nothing_and_issues_no_token(
    cli_runner: CliRunner,
    runner: FakeRunner,
    listening: dict[str, Any],
    unit_dirs: dict[str, Path],
    state_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nothing is written, enabled, started or issued."""
    # The rehearsal runs read-only probes for real; here, against the fake.
    monkeypatch.setattr("wasm.cli.app.SubprocessRunner", lambda: runner)

    result = cli_runner.invoke(web.cli, ["enable", "--dry-run"])

    assert result.exit_code == 0, result.output
    assert not (unit_dirs["managed"] / "wasm-web.service").exists()
    assert not (state_dir / "web-token").exists()
    assert "Access Token" not in result.output
    assert "would enable and start wasm-web.service" in result.output
    assert not runner.ran("systemctl", "enable")
    assert not runner.ran("systemctl", "restart")


# ---------------------------------------------------------------------------
# wasm web disable
# ---------------------------------------------------------------------------


def test_disable_stops_disables_and_removes_the_unit(
    cli_runner: CliRunner, systemd_up: FakeRunner, unit_dirs: dict[str, Path]
) -> None:
    """Afterwards nothing brings the console back at boot."""
    unit = _installed_unit(unit_dirs)

    result = cli_runner.invoke(web.cli, ["disable"])

    assert result.exit_code == 0, result.output
    assert systemd_up.ran("systemctl", "stop", "wasm-web.service")
    assert systemd_up.ran("systemctl", "disable", "wasm-web.service")
    assert systemd_up.ran("systemctl", "daemon-reload")
    assert not unit.exists()


def test_disable_without_the_service_touches_nothing(
    cli_runner: CliRunner, runner: FakeRunner, unit_dirs: dict[str, Path]
) -> None:
    """
    ServiceManager falls back from ``wasm-X`` to ``X`` when only ``X`` is
    installed; an application called ``web`` must never be stopped for it.
    """
    (unit_dirs["managed"] / "web.service").write_text(f"# {WASM_UNIT_MARKER}\n")

    result = cli_runner.invoke(web.cli, ["disable"])

    assert result.exit_code == 0, result.output
    assert "not installed" in result.output
    assert runner.calls == []
    assert (unit_dirs["managed"] / "web.service").exists()


# ---------------------------------------------------------------------------
# Status, stop, start and restart next to the service
# ---------------------------------------------------------------------------


def test_status_reports_a_console_running_as_the_service(
    cli_runner: CliRunner, systemd_up: FakeRunner, unit_dirs: dict[str, Path]
) -> None:
    """It says how it runs, because that decides whether it survives a reboot."""
    _installed_unit(unit_dirs)

    result = cli_runner.invoke(web.cli, ["status"])

    assert result.exit_code == 0, result.output
    assert "running" in result.output
    assert "wasm-web.service" in result.output
    assert "4321" in result.output


def test_status_json_names_the_mode(
    cli_runner: CliRunner, systemd_up: FakeRunner, unit_dirs: dict[str, Path]
) -> None:
    """Scripts can tell the service from the daemon."""
    _installed_unit(unit_dirs)

    result = cli_runner.invoke(web.cli, ["status", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["status"] == "running"
    assert payload["mode"] == "service"
    assert payload["pid"] == 4321
    assert payload["service"]["installed"] is True
    assert payload["service"]["enabled"] is True


def test_status_json_reports_the_daemon_as_the_daemon(
    cli_runner: CliRunner, runner: FakeRunner, pid_file: Path
) -> None:
    """A background console is labelled as one: it does not survive a reboot."""
    pid_file.write_text(str(os.getpid()))

    result = cli_runner.invoke(web.cli, ["status", "--json"])

    payload = json.loads(result.output)
    assert payload["status"] == "running"
    assert payload["mode"] == "daemon"
    assert payload["service"]["installed"] is False
    assert runner.calls == []


def test_status_reports_an_installed_service_that_is_down(
    cli_runner: CliRunner, runner: FakeRunner, unit_dirs: dict[str, Path]
) -> None:
    """A failed service is where the operator has to look, so it is named."""
    _installed_unit(unit_dirs)
    runner.script(["systemctl", "is-active"], stdout="failed\n")
    runner.script(["systemctl", "show", "wasm-web.service", "--no-pager"], stdout=CRASHED)

    result = cli_runner.invoke(web.cli, ["status"])

    assert result.exit_code == 0, result.output
    assert "not running" in result.output
    assert "journalctl -u wasm-web" in result.output


def test_start_refuses_a_second_console_while_the_service_runs(
    cli_runner: CliRunner, systemd_up: FakeRunner, unit_dirs: dict[str, Path]
) -> None:
    """
    A second start would print a new token and retire the one the service's
    operator holds, and then fail to bind anyway.
    """
    _installed_unit(unit_dirs)

    result = cli_runner.invoke(web.cli, ["start"])

    assert isinstance(result.exception, ServiceError)
    assert "wasm-web.service" in str(result.exception)
    assert "wasm web disable" in result.exception.details
    assert "Access Token" not in result.output


def test_restart_refuses_while_the_service_runs(
    cli_runner: CliRunner, systemd_up: FakeRunner, unit_dirs: dict[str, Path]
) -> None:
    """Restarting is the service's job: 'wasm web enable' with the new options."""
    _installed_unit(unit_dirs)

    result = cli_runner.invoke(web.cli, ["restart"])

    assert isinstance(result.exception, ServiceError)
    assert "wasm web enable" in result.exception.details


def test_stop_names_the_service_instead_of_claiming_nothing_runs(
    cli_runner: CliRunner, systemd_up: FakeRunner, unit_dirs: dict[str, Path]
) -> None:
    """'Web server is not running' next to a running service is a lie."""
    _installed_unit(unit_dirs)

    result = cli_runner.invoke(web.cli, ["stop"])

    assert result.exit_code == 1
    assert "wasm-web.service" in result.output
    assert "wasm web disable" in result.output
    assert not systemd_up.ran("systemctl", "stop")


# ---------------------------------------------------------------------------
# What the service itself runs
# ---------------------------------------------------------------------------


@pytest.fixture
def served(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """
    Capture the call that would bind the socket.

    Args:
        monkeypatch: Patching helper, scoped to the test.

    Returns:
        The keyword arguments ``run_server`` was called with.
    """
    captured: dict[str, Any] = {}

    def run_server(**kwargs: Any) -> None:
        captured.update(kwargs)

    monkeypatch.setattr("wasm.web.server.run_server", run_server)
    return captured


def test_the_service_start_never_prints_a_token(
    cli_runner: CliRunner,
    systemd_up: FakeRunner,
    unit_dirs: dict[str, Path],
    served: dict[str, Any],
    state_dir: Path,
    pid_file: Path,
) -> None:
    """
    Its stdout is the journal, which every member of systemd-journal or adm
    reads. It serves the token ``enable`` printed and issues none.
    """
    from wasm.web.auth import SecurityConfig, TokenManager

    issued = TokenManager(SecurityConfig()).generate_master_token()
    _installed_unit(unit_dirs)

    result = cli_runner.invoke(web.cli, ["start", "--under-systemd"])

    assert result.exit_code == 0, result.output
    assert served["show_token"] is False
    assert "Access Token" not in result.output
    assert issued not in result.output
    assert TokenManager(SecurityConfig()).verify_master_token(issued) is True
    assert not pid_file.exists(), "systemd tracks the process; a PID file invites kill"


def test_the_service_start_cannot_be_backgrounded(cli_runner: CliRunner) -> None:
    """systemd supervises the process it started; a fork would orphan it."""
    result = cli_runner.invoke(web.cli, ["start", "--under-systemd", "--daemon"])

    assert result.exit_code == 2
    assert "--under-systemd" in result.output


def test_under_systemd_is_not_advertised(cli_runner: CliRunner) -> None:
    """It is the unit's switch, not the operator's."""
    result = cli_runner.invoke(web.cli, ["start", "--help"])

    assert "--under-systemd" not in result.output


# ---------------------------------------------------------------------------
# The foreground banner
# ---------------------------------------------------------------------------


def test_the_foreground_banner_says_how_to_stop_it_and_keep_it(
    served: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """
    Ctrl+C is the stop button, and 'wasm web enable' - with the options this
    console was started with - is how it survives a reboot.
    """
    from wasm.web.auth import SecurityConfig

    monkeypatch.setattr("wasm.core.net.server_address", lambda: "198.51.100.7")
    monkeypatch.setattr("wasm.core.net._current_user", lambda: "root")

    options = web.StartOptions(port=8081, trusted_proxy=("127.0.0.1",))
    web._start_foreground(
        SecurityConfig(host="127.0.0.1", port=8081), options=options, insecure_http=False
    )

    output = capsys.readouterr().out
    lines = output.splitlines()
    stop_line = next(line for line in lines if "Ctrl+C" in line)
    assert "wasm web enable --port 8081 --trusted-proxy 127.0.0.1" in stop_line
    assert "reboot" in stop_line
    assert served["show_token"] is False

    tunnel = next(i for i, line in enumerate(lines) if "ssh -L 8081:127.0.0.1:8081" in line)
    block_start = next(i for i in range(tunnel, -1, -1) if "answers on the server" in lines[i])
    assert lines[block_start - 1] == "", "the tunnel instructions are set apart"


def test_the_foreground_banner_token_is_the_one_served(
    served: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    """Issued in the CLI and served by the server, never issued twice."""
    from wasm.web.auth import SecurityConfig, TokenManager

    web._start_foreground(
        SecurityConfig(host="127.0.0.1", port=8081),
        options=web.StartOptions(port=8081),
        insecure_http=False,
    )

    token = _printed_token(capsys.readouterr().out)
    assert TokenManager(SecurityConfig()).verify_master_token(token) is True


def test_the_daemon_banner_says_it_does_not_survive_a_reboot(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The background mode is exactly the one that was mistaken for a service."""
    from wasm.web.auth import SecurityConfig

    monkeypatch.setattr(web.os, "fork", lambda: 4321)

    web._start_daemon(SecurityConfig(host="127.0.0.1", port=8081), verbose=False)

    output = capsys.readouterr().out
    assert "wasm web enable" in output
    assert "reboot" in output


# ---------------------------------------------------------------------------
# ServiceManager.install_unit
# ---------------------------------------------------------------------------


def test_install_unit_keeps_the_wasm_prefix(runner: FakeRunner, unit_dirs: dict[str, Path]) -> None:
    """
    create_service drops ``wasm-`` from application names; WASM's own units
    keep it, because the prefix is what marks them as WASM's.
    """
    path = ServiceManager().install_unit(
        "wasm-web", "wasm-web", {"exec_start": f"{WASM_BIN} web start --under-systemd"}
    )

    assert path == unit_dirs["managed"] / "wasm-web.service"
    assert WASM_UNIT_MARKER in path.read_text()
    assert runner.ran("systemctl", "daemon-reload")


def test_install_unit_refuses_a_unit_systemd_loads_from_elsewhere(
    runner: FakeRunner, unit_dirs: dict[str, Path]
) -> None:
    """The authority is systemd's FragmentPath, as for every other operation."""
    runner.script(
        ["systemctl", "show", "-p", "FragmentPath"],
        stdout="FragmentPath=/usr/lib/systemd/system/wasm-web.service\n",
    )

    with pytest.raises(ServiceError):
        ServiceManager().install_unit("wasm-web", "wasm-web", {"exec_start": "/bin/true"})

    assert not (unit_dirs["managed"] / "wasm-web.service").exists()


def test_install_unit_refuses_to_replace_a_file_wasm_did_not_write(
    runner: FakeRunner, unit_dirs: dict[str, Path]
) -> None:
    """Replacing is for WASM's own units only."""
    foreign = unit_dirs["managed"] / "nginx.service"
    foreign.write_text("[Service]\nExecStart=/usr/sbin/nginx\n")

    with pytest.raises(ServiceError):
        ServiceManager().install_unit("nginx", "wasm-web", {"exec_start": "/bin/true"})

    assert "wasm" not in foreign.read_text()
