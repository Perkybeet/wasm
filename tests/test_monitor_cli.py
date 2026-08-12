"""
Tests for the callers of the monitor: the CLI and the web API.

The monitor package was rewritten from an antivirus into observability, and its
two callers were left addressing the old model. ``wasm monitor scan`` built a
``MonitorConfig`` with ``auto_terminate`` and ``use_ai``, so it raised a
TypeError on every invocation; the web API did the same and read ``ThreatStore``
from a package that no longer exports it, so every request returned 500. Both
were caught only by running them, because a blanket ``except Exception``
rendered the crash as a one-line message.

So these tests execute every action and every endpoint. Nothing is mocked
except the machine underneath: psutil, systemd through the FakeRunner, SMTP.
"""

from __future__ import annotations

import functools
import io
import types
from argparse import Namespace
from pathlib import Path
from typing import Any

import psutil
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from wasm.cli.commands import monitor as cli_monitor
from wasm.core.logger import Logger
from wasm.web.api import monitor as monitor_api
from wasm.web.api.auth import get_current_session

#: Settings that stopped existing when the monitor stopped being an antivirus.
#: A caller that still mentions one of them is a caller that will raise.
REMOVED_SETTINGS = (
    "auto_terminate",
    "terminate_malicious_only",
    "use_ai",
    "threat_level",
    "ThreatStore",
    "action_taken",
)


class _FakeProcess:
    """Stand-in for ``psutil.Process`` as returned by ``process_iter``."""

    def __init__(self, **info: Any) -> None:
        """
        Args:
            info: The fields ``process_iter`` would expose.
        """
        self.info = info
        self.pid = info["pid"]


def _fake_process(**overrides: Any) -> _FakeProcess:
    """
    Build a fake psutil process.

    Args:
        overrides: Fields to replace in the defaults.

    Returns:
        The fake process.
    """
    info: dict[str, Any] = {
        "pid": 4242,
        "name": "xmrig",
        "username": "nobody",
        "cpu_percent": 99.0,
        "memory_percent": 12.0,
        "cmdline": ["./xmrig", "--donate-level", "1"],
        "create_time": 0.0,
        "ppid": 0,
        "status": "running",
        "num_threads": 4,
        "cwd": "/var/empty",
    }
    info.update(overrides)
    return _FakeProcess(**info)


class _FakeNotifier:
    """Records what would have been mailed."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """
        Args:
            args: Ignored, kept for signature compatibility.
            kwargs: Ignored, kept for signature compatibility.
        """
        self.recipients: list[str] = ["ops@example.com"]
        self.smtp_config = types.SimpleNamespace(host="smtp.example.com")
        self.sent = 0

    def send_test_email(self) -> bool:
        """Record a test email instead of opening a socket."""
        self.sent += 1
        return True

    def send_observation_alert(self, observations: list[Any]) -> bool:
        """Record a report instead of opening a socket."""
        self.sent += 1
        return True


@pytest.fixture
def monitor_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    sandbox: Path,
    runner: Any,
) -> Any:
    """
    Put the monitor on a machine made of fixtures.

    psutil returns one fake process, systemd answers through the FakeRunner,
    the systemd unit directory and the observation database live under the
    sandbox, and SMTP is a recorder.

    Args:
        monkeypatch: Patching helper, scoped to the test.
        tmp_path: Per-test temporary directory.
        sandbox: Fixture pointing HOME at a throwaway directory.
        runner: FakeRunner installed as the process-wide runner.

    Returns:
        A namespace with the runner and the notifier the code will use.
    """
    from wasm.monitor import process_monitor as process_monitor_module

    monkeypatch.setattr(
        psutil,
        "process_iter",
        lambda attrs=None, *args, **kwargs: iter([_fake_process()]),
    )
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
    monkeypatch.setattr(cli_monitor, "EmailNotifier", _FakeNotifier)
    monkeypatch.setattr(monitor_api, "EmailNotifier", _FakeNotifier)
    monkeypatch.setattr(process_monitor_module, "EmailNotifier", _FakeNotifier)

    runner.script(["systemctl", "is-active"], stdout="active\n")
    runner.script(["systemctl", "is-enabled"], stdout="enabled\n")
    runner.script(["systemctl", "show"], stdout="MainPID=123\nActiveState=active\n")

    return types.SimpleNamespace(runner=runner, unit_dir=unit_dir, home=sandbox)


@pytest.fixture
def cli_output(monkeypatch: pytest.MonkeyPatch) -> io.StringIO:
    """
    Collect what the handlers print.

    ``Logger`` binds ``sys.stdout`` as a default argument value at import time,
    so neither capsys nor capfd sees its output; giving it an explicit stream is
    the only reliable way to read it back.

    Args:
        monkeypatch: Patching helper, scoped to the test.

    Returns:
        The buffer the CLI writes into.
    """
    buffer = io.StringIO()
    monkeypatch.setattr(
        cli_monitor,
        "Logger",
        functools.partial(Logger, stream=buffer),
    )
    return buffer


def _args(action: str, **extra: Any) -> Namespace:
    """
    Build the namespace the parser hands to the monitor handler.

    Args:
        action: Subcommand name.
        extra: Extra attributes the subparser would set.

    Returns:
        The namespace.
    """
    namespace = Namespace(action=action, verbose=False, no_color=True, dry_run=False)
    for key, value in extra.items():
        setattr(namespace, key, value)
    return namespace


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("action", sorted(cli_monitor.ACTIONS))
def test_every_monitor_action_runs(
    action: str,
    monitor_env: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every subcommand of ``wasm monitor`` must execute, not raise."""
    if action == "run":
        # The daemon loop would never return; one iteration is the interesting part.
        monkeypatch.setattr(
            "wasm.monitor.process_monitor.ProcessMonitor.run",
            lambda self: None,
        )
    if action in ("disable", "uninstall"):
        # Both act on an installed unit, so install one first.
        assert cli_monitor.handle_monitor(_args("install")) == 0

    exit_code = cli_monitor.handle_monitor(_args(action))

    assert exit_code == 0, f"'wasm monitor {action}' exited {exit_code}"


def test_an_unknown_action_is_rejected_without_raising(monitor_env: Any) -> None:
    """A typo is an exit code, not a traceback."""
    assert cli_monitor.handle_monitor(_args("nonsense")) == 1


def test_scan_reports_the_flagged_process_and_persists_it(monitor_env: Any) -> None:
    """The scan that used to raise now produces observations and stores them."""
    from wasm.monitor import ObservationStore

    assert cli_monitor.handle_monitor(_args("scan")) == 0

    stored = ObservationStore().recent()
    assert len(stored) == 1
    assert stored[0]["process_name"] == "xmrig"
    assert stored[0]["signal"] == "name-pattern"


def test_scan_accepts_the_removed_ai_flags_and_says_they_do_nothing(
    monitor_env: Any,
    cli_output: io.StringIO,
) -> None:
    """The parser still offers --force-ai; a script passing it must not crash."""
    assert cli_monitor.handle_monitor(_args("scan", force_ai=True, all=True)) == 0

    output = cli_output.getvalue()
    assert "--force-ai" in output
    assert "--all" in output


def test_status_states_what_the_monitor_will_not_do(
    monitor_env: Any,
    cli_output: io.StringIO,
) -> None:
    """An operator reading the status learns the tool's limits from the tool."""
    assert cli_monitor.handle_monitor(_args("status")) == 0

    output = cli_output.getvalue()
    assert "signals, terminates or restarts a process" in output
    assert "Retention" in output


def test_install_writes_the_unit_through_the_runner(monitor_env: Any) -> None:
    """Installing goes through the audited execution seam."""
    assert cli_monitor.handle_monitor(_args("install")) == 0

    unit = monitor_env.unit_dir / "wasm-monitor.service"
    assert unit.exists()
    assert "ExecStart=" in unit.read_text()
    assert ("systemctl", "daemon-reload") in monitor_env.runner.calls


def test_actions_that_touch_systemd_require_root(
    monitor_env: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without root the error names the problem instead of failing on a write."""
    monkeypatch.setattr(cli_monitor, "check_root", lambda: False)

    for action in ("install", "enable", "disable", "uninstall"):
        assert cli_monitor.handle_monitor(_args(action)) == 1

    assert not (monitor_env.unit_dir / "wasm-monitor.service").exists()


def test_the_cli_does_not_mention_any_removed_setting() -> None:
    """The regression was a caller addressing a model that no longer exists."""
    source = Path(cli_monitor.__file__).read_text()

    offences = [
        name
        for name in REMOVED_SETTINGS
        # The module documents why the flags are gone, so only real uses count:
        # anything written as an attribute access or a keyword argument.
        if f"{name}=" in source or f".{name}" in source
    ]
    assert offences == [], f"CLI still speaks the antivirus vocabulary: {offences}"


# ---------------------------------------------------------------------------
# Web API
# ---------------------------------------------------------------------------


@pytest.fixture
def client(monitor_env: Any, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """
    Build a test client for the monitor router with authentication stubbed out.

    Args:
        monitor_env: Fixture that fakes the machine underneath.
        monkeypatch: Patching helper, scoped to the test.

    Returns:
        A client whose requests are already authenticated.
    """
    _install_psutil_metrics(monkeypatch)

    app = FastAPI()
    app.include_router(monitor_api.router, prefix="/api/monitor")
    app.dependency_overrides[get_current_session] = lambda: {"session_id": "test"}
    return TestClient(app, raise_server_exceptions=False)


def _install_psutil_metrics(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Replace the psutil calls the resource collector makes.

    Args:
        monkeypatch: Patching helper, scoped to the test.
    """
    monkeypatch.setattr(psutil, "cpu_percent", lambda interval=None: 12.5)
    monkeypatch.setattr(psutil, "cpu_count", lambda logical=True: 4)
    monkeypatch.setattr(psutil, "getloadavg", lambda: (0.5, 0.4, 0.3))
    monkeypatch.setattr(
        psutil,
        "virtual_memory",
        lambda: types.SimpleNamespace(total=8_000, used=2_000, available=6_000, percent=25.0),
    )
    monkeypatch.setattr(
        psutil,
        "swap_memory",
        lambda: types.SimpleNamespace(total=1_000, used=100, percent=10.0),
    )
    monkeypatch.setattr(psutil, "disk_partitions", lambda all=False: [])
    monkeypatch.setattr(
        psutil,
        "net_io_counters",
        lambda: types.SimpleNamespace(bytes_sent=1, bytes_recv=2),
    )
    monkeypatch.setattr(psutil, "boot_time", lambda: 1_000.0)
    monkeypatch.setattr(psutil, "pids", lambda: [1, 2, 3])


def test_status_endpoint_answers_with_the_unit_state(client: TestClient) -> None:
    """The endpoint that used to 500 on ProcessMonitor's new model."""
    response = client.get("/api/monitor/status")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["installed"] is False
    assert body["scope"], "the panel is not told what the monitor refuses to do"


def test_config_endpoint_returns_only_settings_that_exist(client: TestClient) -> None:
    """auto_terminate and use_ai are gone from the wire, not just from the code."""
    response = client.get("/api/monitor/config")

    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {
        "scan_interval",
        "cpu_threshold",
        "memory_threshold",
        "retention_days",
        "max_observations",
        "notify",
        "watch_units",
    }


def test_scan_endpoint_returns_observations(client: TestClient) -> None:
    """POST /scan used to raise a TypeError before it read a single process."""
    response = client.post("/api/monitor/scan")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["count"] == 1
    assert body["observations"][0]["process_name"] == "xmrig"
    assert body["observations"][0]["severity"] == "warning"
    assert "terminated" not in body


def test_processes_endpoint_lists_the_process_table(client: TestClient) -> None:
    """The panel's process list goes through the monitor's collector."""
    response = client.get("/api/monitor/processes?limit=10&sort_by=cpu")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] == 1
    assert body["processes"][0]["name"] == "xmrig"


def test_processes_endpoint_rejects_an_unknown_sort_key(client: TestClient) -> None:
    """A sort key is a closed set, validated before it reaches a lookup."""
    assert client.get("/api/monitor/processes?sort_by=; DROP").status_code == 422


def test_metrics_endpoint_reports_resources(client: TestClient) -> None:
    """Resources are read live and never stored."""
    response = client.get("/api/monitor/metrics")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["cpu_percent"] == 12.5
    assert body["memory_percent"] == 25.0


def test_observations_endpoint_reads_and_acknowledges(client: TestClient) -> None:
    """The replacement for /threats/history, which imported a deleted class."""
    assert client.post("/api/monitor/scan").status_code == 200

    listed = client.get("/api/monitor/observations")
    assert listed.status_code == 200, listed.text
    body = listed.json()
    assert body["count"] == 1
    observation_id = body["observations"][0]["id"]

    acknowledged = client.post(f"/api/monitor/observations/{observation_id}/acknowledge")
    assert acknowledged.status_code == 200, acknowledged.text
    assert client.get("/api/monitor/observations").json()["count"] == 0

    missing = client.post("/api/monitor/observations/999999/acknowledge")
    assert missing.status_code == 404


@pytest.mark.parametrize(
    "action",
    ["install", "uninstall", "enable", "disable", "start", "stop"],
)
def test_service_endpoints_drive_systemd_through_the_runner(
    client: TestClient,
    monitor_env: Any,
    action: str,
) -> None:
    """Every service endpoint must reach systemd, and none may raise."""
    response = client.post(f"/api/monitor/{action}")

    assert response.status_code == 200, response.text
    assert any(call[0] == "systemctl" for call in monitor_env.runner.calls)


def test_test_email_endpoint_sends_through_the_notifier(client: TestClient) -> None:
    """The mail path answers instead of 500ing on a missing module."""
    response = client.post("/api/monitor/test-email")

    assert response.status_code == 200, response.text
    assert response.json()["success"] is True


def test_endpoints_are_synchronous_so_they_do_not_block_the_event_loop() -> None:
    """
    systemctl and /proc are blocking calls; ``async def`` would freeze the panel.

    FastAPI sends plain ``def`` handlers to its threadpool, which is exactly
    what a handler that shells out needs.
    """
    import inspect

    offenders = [
        route.name
        for route in monitor_api.router.routes
        if inspect.iscoroutinefunction(getattr(route, "endpoint", None))
    ]

    assert offenders == [], f"async handlers that block the event loop: {offenders}"


def test_the_web_api_does_not_mention_any_removed_setting() -> None:
    """Same regression, other caller."""
    source = Path(monitor_api.__file__).read_text()

    offences = [name for name in REMOVED_SETTINGS if f"{name}=" in source or f".{name}" in source]
    assert offences == [], f"web API still speaks the antivirus vocabulary: {offences}"
