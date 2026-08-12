"""
Tests for ``wasm health`` after the move to Click.

``health`` is the command an operator runs when something looks wrong, so the
regression that mattered here was not a crash: it reported ``0/N running`` for
a server whose applications were all up, because it asked ServiceManager for a
``status`` method that never existed and counted the AttributeError as a
failure. The test that pins that is
:func:`test_a_running_application_is_reported_as_running`.

Everything the check reads is replaced by a fixture, so the numbers in the
output come from the test and not from the developer's machine.
"""

from __future__ import annotations

import functools
import io
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import click
import pytest
from click.testing import CliRunner, Result

from wasm.cli.commands import health as cli_health
from wasm.core.exceptions import ServiceError
from wasm.core.logger import Logger
from wasm.core.store import App

#: Flags that belong to the root command and to no other.
GLOBAL_FLAGS = frozenset({"-v", "--verbose", "--dry-run", "--json", "--no-color"})

#: One gigabyte, for readable fake disk figures.
_GB = 1024**3


class _FakeWebServer:
    """A web server that is whatever the test says it is."""

    def __init__(self, installed: bool = True, active: bool = True) -> None:
        """
        Args:
            installed: Whether the binary is on PATH.
            active: Whether the unit is running.
        """
        self._installed = installed
        self._active = active

    def is_installed(self) -> bool:
        """Whether the server is installed."""
        return self._installed

    def get_status(self) -> dict[str, Any]:
        """What the server is doing right now."""
        return {"active": self._active}


class _FakeServices:
    """A systemd that answers for the units the test set up."""

    def __init__(self, active: dict[str, bool], failing: tuple[str, ...] = ()) -> None:
        """
        Args:
            active: Unit name to whether it is running.
            failing: Units whose query raises, as an unmanaged unit would.
        """
        self.active = active
        self.failing = failing
        self.asked: list[str] = []

    def get_status(self, name: str) -> dict[str, Any]:
        """
        Report a unit's state.

        Args:
            name: Service name.

        Returns:
            The status mapping the real manager returns.

        Raises:
            ServiceError: When the test declared this unit unqueryable.
        """
        self.asked.append(name)
        if name in self.failing:
            raise ServiceError(f"{name} is not managed by WASM")
        return {"name": name, "exists": True, "active": self.active.get(name, False)}


class _FakeStore:
    """A store holding exactly the applications a test cares about."""

    def __init__(self, apps: list[App]) -> None:
        """
        Args:
            apps: Applications to report.
        """
        self._apps = apps

    def list_apps(self) -> list[App]:
        """Every deployed application."""
        return self._apps


class _FakeCerts:
    """A certificate manager with a fixed inventory."""

    def __init__(self, certs: list[dict[str, Any]]) -> None:
        """
        Args:
            certs: Certificate entries as certbot reports them.
        """
        self._certs = certs

    def list_certificates(self) -> list[dict[str, Any]]:
        """Every certificate on this machine."""
        return self._certs


@pytest.fixture
def server(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Any:
    """
    Replace every subsystem the health check reads.

    The default is a healthy server: nginx running, no applications, no
    certificates. A test changes only the part it is about.

    Args:
        monkeypatch: Patching helper, scoped to the test.
        tmp_path: Per-test temporary directory, used as the apps directory.

    Returns:
        A namespace whose attributes each test can rebind before invoking.
    """
    state = type(
        "ServerState",
        (),
        {
            "nginx": _FakeWebServer(installed=True, active=True),
            "apache": _FakeWebServer(installed=False, active=False),
            "services": _FakeServices({}),
            "store": _FakeStore([]),
            "certs": _FakeCerts([]),
        },
    )()

    monkeypatch.setattr(
        cli_health,
        "Config",
        lambda *a, **kw: type("FakeConfig", (), {"apps_directory": tmp_path})(),
    )
    # Disk and memory come from the machine running the suite, which would make
    # every assertion below depend on how full the developer's laptop is.
    monkeypatch.setattr(
        cli_health.shutil,
        "disk_usage",
        lambda path: SimpleNamespace(total=200 * _GB, used=50 * _GB, free=150 * _GB),
    )
    monkeypatch.setattr(
        cli_health,
        "_read_meminfo",
        lambda: {"MemTotal": 16 * 1024 * 1024, "MemAvailable": 12 * 1024 * 1024},
    )
    monkeypatch.setattr(cli_health, "NginxManager", lambda *a, **kw: state.nginx)
    monkeypatch.setattr(cli_health, "ApacheManager", lambda *a, **kw: state.apache)
    monkeypatch.setattr(cli_health, "ServiceManager", lambda *a, **kw: state.services)
    monkeypatch.setattr(cli_health, "CertManager", lambda *a, **kw: state.certs)
    monkeypatch.setattr(cli_health, "get_store", lambda: state.store)
    return state


@pytest.fixture
def logged(monkeypatch: pytest.MonkeyPatch) -> io.StringIO:
    """
    Collect what the check writes through the logger.

    ``Logger`` binds ``sys.stdout`` as a default argument at import time, so
    neither capsys nor the CliRunner sees it; an explicit stream does.

    Args:
        monkeypatch: Patching helper, scoped to the test.

    Returns:
        The buffer the command writes into.
    """
    buffer = io.StringIO()
    monkeypatch.setattr(cli_health, "Logger", functools.partial(Logger, stream=buffer))
    return buffer


def invoke(*args: str, **kwargs: Any) -> Result:
    """
    Run ``wasm health``.

    Args:
        args: Arguments after ``health``.
        kwargs: Passed to ``CliRunner.invoke``.

    Returns:
        The result of the invocation.
    """
    return CliRunner().invoke(cli_health.cli, list(args), **kwargs)


def _app(domain: str, *, is_static: bool = False) -> App:
    """
    Build an application record.

    Args:
        domain: The domain the app is served on.
        is_static: Whether it is served from a directory rather than by a
            systemd unit.

    Returns:
        The record the store would return.
    """
    return App(
        id=1,
        domain=domain,
        app_path=f"/var/www/apps/{domain}",
        is_static=is_static,
    )


# ---------------------------------------------------------------------------
# Surface
# ---------------------------------------------------------------------------


def test_health_documents_itself() -> None:
    """--help is the documentation, and it has to say what is checked."""
    result = invoke("--help")

    assert result.exit_code == 0, result.output
    assert "disk" in result.output.lower()
    assert "certificate" in result.output.lower()


def test_health_takes_no_arguments(server: Any) -> None:
    """A stray word is a usage error, not a silent full run."""
    result = invoke("nginx")

    assert result.exit_code == 2
    assert server.services.asked == []


def test_health_rejects_an_unknown_option(server: Any) -> None:
    """An option this command never had is refused before anything is read."""
    result = invoke("--check=disk")

    assert result.exit_code == 2
    assert server.services.asked == []


def test_health_declares_no_global_flag() -> None:
    """
    Global state lives on the context, not on a second copy of the flag.

    A redeclared ``--dry-run`` is what let ``wasm --dry-run <command>`` run for
    real under argparse.
    """
    offenders = sorted(
        opt
        for param in cli_health.cli.params
        if isinstance(param, click.Option)
        for opt in param.opts
        if opt in GLOBAL_FLAGS
    )

    assert offenders == []


def test_verbose_comes_from_the_context(server: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """``wasm --verbose health`` reaches the check without a local flag."""
    from wasm.cli.app import Context

    asked: list[bool] = []
    buffer = io.StringIO()

    def _record(*args: Any, verbose: bool = False, **kwargs: Any) -> Logger:
        asked.append(verbose)
        return Logger(*args, verbose=verbose, stream=buffer, **kwargs)

    monkeypatch.setattr(cli_health, "Logger", _record)

    result = CliRunner().invoke(
        cli_health.cli,
        [],
        obj=Context(verbose=True),
        standalone_mode=False,
    )

    assert result.return_value == 0, result.output
    assert asked == [True]


# ---------------------------------------------------------------------------
# What it reports
# ---------------------------------------------------------------------------


def test_a_running_application_is_reported_as_running(server: Any) -> None:
    """
    The regression: one deployed app, its unit active, reported as 0/1.

    The check asked ServiceManager for a method it does not have, so the
    AttributeError counted every application as unqueryable.
    """
    server.store = _FakeStore([_app("example.com")])
    server.services = _FakeServices({"example-com": True})

    result = invoke(standalone_mode=False)

    assert result.return_value == 0, result.output
    assert "Applications: 1/1 serving" in result.output
    assert server.services.asked == ["example-com"], "the unit name was not built from the domain"


def test_a_stopped_application_is_a_warning_not_a_running_count(server: Any) -> None:
    """A stopped app is counted as needing attention and named in the summary."""
    server.store = _FakeStore([_app("example.com")])
    server.services = _FakeServices({"example-com": False})

    result = invoke(standalone_mode=False)

    assert result.return_value == 0, result.output
    assert "Applications: 0/1 serving, 1 need attention" in result.output


def test_an_unqueryable_application_does_not_fail_the_whole_check(server: Any) -> None:
    """One unmanaged unit is a warning about that unit, not a crash."""
    server.store = _FakeStore([_app("example.com")])
    server.services = _FakeServices({}, failing=("example-com",))

    result = invoke(standalone_mode=False)

    assert result.return_value == 0, result.output
    assert "0/1 serving" in result.output


def test_a_static_site_is_not_reported_as_stopped(server: Any) -> None:
    """
    The false alarm an operator hit: five healthy static sites called stopped.

    A static site is served by nginx from a directory. There is no systemd unit
    for it, so asking systemd whether it is running can only ever say no.
    """
    server.store = _FakeStore([_app("example.com", is_static=True)])
    server.services = _FakeServices({})

    result = invoke(standalone_mode=False)

    assert result.return_value == 0, result.output
    assert "Applications: 1/1 serving (1 static)" in result.output
    assert "need attention" not in result.output
    assert server.services.asked == [], "a static site has no unit to ask about"


def test_list_and_health_agree_on_the_same_machine(server: Any) -> None:
    """
    The two commands contradicted each other, which is why this exists.

    They now share one resolver, so this asserts the seam rather than the
    wording: given the same store and the same systemd, both reach the same
    state for every application.
    """
    from wasm.core.app_state import RUNNING, STATIC, STOPPED, resolve_states

    apps = [
        _app("running.example.com"),
        _app("stopped.example.com"),
        _app("static.example.com", is_static=True),
    ]
    services = _FakeServices({"running-example-com": True, "stopped-example-com": False})

    states = resolve_states(apps, services)

    assert states["running.example.com"].label == RUNNING
    assert states["stopped.example.com"].label == STOPPED
    assert states["static.example.com"].label == STATIC


def test_a_stopped_web_server_is_an_issue_and_exits_one(server: Any) -> None:
    """An installed but stopped nginx is the definition of an unhealthy server."""
    server.nginx = _FakeWebServer(installed=True, active=False)

    result = invoke(standalone_mode=False)

    assert result.return_value == 1
    assert "Nginx: Stopped" in result.output


def test_no_web_server_at_all_is_an_issue(server: Any) -> None:
    """A server with neither nginx nor apache cannot serve anything."""
    server.nginx = _FakeWebServer(installed=False)
    server.apache = _FakeWebServer(installed=False)

    result = invoke(standalone_mode=False)

    assert result.return_value == 1


def test_a_certificate_near_expiry_is_reported(server: Any) -> None:
    """
    Certificates were read from an ``expires`` key that certbot calls ``expiry``.

    So nothing was ever close to renewal, right up to the outage.
    """
    soon = (datetime.now() + timedelta(days=3)).isoformat()
    server.certs = _FakeCerts([{"name": "example.com", "expiry": soon}])

    result = invoke(standalone_mode=False)

    assert result.return_value == 1
    assert "1 expiring soon" in result.output


def test_a_healthy_certificate_is_not_reported_as_expiring(server: Any) -> None:
    """A certificate renewed yesterday must not raise an alarm."""
    later = (datetime.now() + timedelta(days=80)).isoformat()
    server.certs = _FakeCerts([{"name": "example.com", "expiry": later}])

    result = invoke(standalone_mode=False)

    assert result.return_value == 0, result.output
    assert "all valid" in result.output


def test_a_healthy_server_says_so(server: Any, logged: io.StringIO) -> None:
    """The happy path has to be reachable, or the command is only ever noise."""
    result = invoke(standalone_mode=False)

    assert result.return_value == 0, result.output
    assert "All systems healthy" in logged.getvalue()


# ---------------------------------------------------------------------------
# The two entry points
# ---------------------------------------------------------------------------


def test_the_argparse_handler_runs_the_same_check(server: Any) -> None:
    """
    ``wasm.cli.parser`` still calls ``handle_health``; it shares the check.

    Both paths call :func:`run_health_check`, so a fix to one is a fix to both.
    """
    from argparse import Namespace

    server.store = _FakeStore([_app("example.com")])
    server.services = _FakeServices({"example-com": True})

    assert cli_health.handle_health(Namespace(verbose=False)) == 0
    assert server.services.asked == ["example-com"]
