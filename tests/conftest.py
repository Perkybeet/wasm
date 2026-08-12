"""
Shared test fixtures.

The important thing in this file is :func:`forbid_real_subprocess`. It is
autouse, so every test in the suite runs with real process execution disabled.
Any code path that shells out without going through an injected
:class:`~wasm.core.runner.CommandRunner` fails loudly instead of silently
touching the developer's machine.

Tests that genuinely need to spawn a process, such as the runner's own tests,
opt out with ``@pytest.mark.allow_subprocess``.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from wasm.core.runner import FakeRunner, set_runner


class RealSubprocessAttempted(AssertionError):
    """Raised when a test tries to execute a real process."""


def pytest_configure(config: pytest.Config) -> None:
    """
    Register the markers this suite uses.

    Args:
        config: The pytest configuration object.
    """
    config.addinivalue_line(
        "markers",
        "allow_subprocess: permit this test to execute real processes",
    )
    config.addinivalue_line(
        "markers",
        "allow_sockets: permit this test to open real network connections",
    )


@pytest.fixture(autouse=True)
def forbid_real_subprocess(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch):
    """
    Make real process execution fail for the duration of a test.

    Args:
        request: The pytest request, used to honour the opt-out marker.
        monkeypatch: Patching helper, scoped to the test.
    """
    if request.node.get_closest_marker("allow_subprocess"):
        return

    def _blocked(*args, **kwargs):
        argv = args[0] if args else kwargs.get("args")
        raise RealSubprocessAttempted(
            f"This test tried to execute a real process: {argv!r}. "
            "Route the call through a CommandRunner and inject a FakeRunner, "
            "or mark the test with @pytest.mark.allow_subprocess."
        )

    for name in ("run", "Popen", "call", "check_call", "check_output", "getoutput"):
        monkeypatch.setattr(subprocess, name, _blocked, raising=False)


class PortProbe:
    """
    Stands in for a connection attempt against a port.

    Attributes:
        closed: Ports that refuse connections. Everything else answers, which
            keeps "systemd says active" meaning "running" for the tests that
            are not about the probe.
        taken: Ports something else already holds, so the panel cannot bind
            them. Empty by default: a test machine is not required to have
            8080 free for the suite to pass.
        asked: Every port that was checked, in order.
    """

    def __init__(self) -> None:
        self.closed: set[int] = set()
        self.taken: set[int] = set()
        self.asked: list[int] = []

    def in_use(self, host: str, port: int) -> bool:
        """
        Answer whether the panel's address is already bound.

        Args:
            host: Ignored.
            port: The port asked about.

        Returns:
            True when the test declared this port taken.
        """
        self.asked.append(port)
        return port in self.taken

    def __call__(self, port: int, host: str = "127.0.0.1", timeout: float = 0.0) -> bool:
        """
        Answer whether a port accepts connections.

        Args:
            port: The port asked about.
            host: Ignored; recorded by the caller's signature only.
            timeout: Ignored.

        Returns:
            True unless the test declared this port closed.
        """
        self.asked.append(port)
        return port not in self.closed


@pytest.fixture(autouse=True)
def ports(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> PortProbe | None:
    """
    Stop the application state probe from opening real connections.

    :func:`~wasm.core.app_state.resolve_state` asks the port whether anything
    answers, because a systemd unit can be active while the application behind
    it is refusing every request. In a test that would reach the developer's
    own machine and give a different answer depending on what happens to be
    listening, so it is replaced here.

    Args:
        request: The pytest request, used to honour the opt-out marker.
        monkeypatch: Patching helper, scoped to the test.

    Returns:
        The probe, so a test can declare which ports refuse connections.
    """
    if request.node.get_closest_marker("allow_sockets"):
        return None

    probe = PortProbe()
    monkeypatch.setattr("wasm.core.app_state.port_answers", probe)
    monkeypatch.setattr("wasm.cli.commands.web._port_in_use", probe.in_use)
    return probe


@pytest.fixture
def runner() -> FakeRunner:
    """
    Provide a FakeRunner installed as the process-wide runner.

    Returns:
        The fake runner, for scripting responses and asserting on calls.
    """
    fake = FakeRunner()
    set_runner(fake)
    try:
        yield fake
    finally:
        set_runner(None)


@pytest.fixture
def sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """
    Provide an isolated filesystem root with HOME pointed at it.

    Args:
        tmp_path: Per-test temporary directory.
        monkeypatch: Patching helper, scoped to the test.

    Returns:
        The sandbox root.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    return tmp_path
