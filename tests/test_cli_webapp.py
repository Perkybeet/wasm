# Copyright (c) 2024-2026 Yago Lopez Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
Tests for the nine commands that act on a deployed application.

They exercise the Click layer of :mod:`wasm.cli.commands.webapp`: the surface
(every command, every alias, every option the contract froze), the validation
Click now does instead of the handlers, and what each command actually calls.
Nothing here reaches systemd, nginx or the store: the managers are spied on and
process execution goes through the :class:`~wasm.core.runner.FakeRunner`, so an
argv assertion is an assertion about the exact command that would have run.
"""

from __future__ import annotations

import functools
import inspect
import io
from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import click
import pytest
import yaml
from click.testing import CliRunner, Result

from wasm.cli import app as cli_app
from wasm.cli.app import ALIASES, Context
from wasm.cli.app import cli as root_cli
from wasm.cli.commands import webapp
from wasm.core.logger import Logger
from wasm.core.runner import DryRunRunner, FakeRunner, get_runner

#: The commands this module owns, as the user types them.
COMMANDS = (
    "create",
    "delete",
    "list",
    "logs",
    "restart",
    "start",
    "status",
    "stop",
    "update",
)

#: Alternative spellings the contract froze, and what each one resolves to.
COMMAND_ALIASES = {
    "deploy": "create",
    "new": "create",
    "ls": "list",
    "info": "status",
    "upgrade": "update",
    "remove": "delete",
    "rm": "delete",
}

#: Flags that belong to the root group. A command that bound one of these to a
#: parameter of its own would shadow what the user typed before the command
#: name, which is the defect the Click migration exists to remove.
GLOBAL_FLAG_NAMES = frozenset({"verbose", "dry_run", "json_output", "no_color"})


def shown(result: Result, console: io.StringIO) -> str:
    """
    Everything the user saw during an invocation.

    Click prompts go through the CliRunner's buffer while everything the
    commands report goes through the logger, so both halves are returned.

    Args:
        result: What the CliRunner returned.
        console: Buffer the patched logger wrote into.

    Returns:
        The Click output followed by the logger output.
    """
    return result.output + console.getvalue()


class ServiceSpy:
    """
    A stand-in for :class:`~wasm.managers.service_manager.ServiceManager`.

    Records what the command asked for instead of talking to systemd.

    Attributes:
        calls: Method name and service name, in call order.
        exists: What :meth:`service_exists` reports.
        status: What :meth:`get_status` reports.
        journal: What :meth:`logs` returns.
        verbose: Verbosity the command constructed it with.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.exists: bool = True
        self.status: dict[str, Any] = {"exists": True, "active": True, "enabled": True}
        #: Per-unit overrides, for the commands that ask about several at once.
        self.statuses: dict[str, dict[str, Any]] = {}
        self.journal: str = "a log line"
        self.verbose: bool = False

    def service_exists(self, name: str) -> bool:
        """
        Args:
            name: Service name.

        Returns:
            The scripted answer.
        """
        self.calls.append(("service_exists", name))
        return self.exists

    def start(self, name: str) -> None:
        """
        Args:
            name: Service name.
        """
        self.calls.append(("start", name))

    def stop(self, name: str) -> None:
        """
        Args:
            name: Service name.
        """
        self.calls.append(("stop", name))

    def restart(self, name: str) -> None:
        """
        Args:
            name: Service name.
        """
        self.calls.append(("restart", name))

    def delete_service(self, name: str) -> None:
        """
        Args:
            name: Service name.
        """
        self.calls.append(("delete_service", name))

    def get_status(self, name: str) -> dict[str, Any]:
        """
        Args:
            name: Service name.

        Returns:
            The scripted status.
        """
        self.calls.append(("get_status", name))
        return self.statuses.get(name, self.status)

    def logs(self, name: str, lines: int = 50) -> str:
        """
        Args:
            name: Service name.
            lines: How many lines were asked for.

        Returns:
            The scripted journal.
        """
        self.calls.append(("logs", f"{name}:{lines}"))
        return self.journal

    def _resolve_service_name(self, name: str) -> str:
        """
        Args:
            name: Base service name.

        Returns:
            The name unchanged; no legacy unit exists in a test.
        """
        return name


class StoreSpy:
    """
    A stand-in for the SQLite store.

    Attributes:
        apps: Applications keyed by domain.
        relations: What :meth:`get_app_with_relations` returns per domain.
        services: Every service row.
        deleted: Names passed to the delete methods, in call order.
    """

    def __init__(self) -> None:
        self.apps: dict[str, Any] = {}
        self.relations: dict[str, Any] = {}
        self.services: list[Any] = []
        self.deleted: list[tuple[str, str]] = []

    def list_apps(self) -> list[Any]:
        """
        Returns:
            Every registered application.
        """
        return list(self.apps.values())

    def list_services(self) -> list[Any]:
        """
        Returns:
            Every registered service.
        """
        return list(self.services)

    def get_app(self, domain: str) -> Any:
        """
        Args:
            domain: Application domain.

        Returns:
            The application row, or None.
        """
        return self.apps.get(domain)

    def get_app_with_relations(self, domain: str) -> Any:
        """
        Args:
            domain: Application domain.

        Returns:
            The application and its site, service and databases, or None.
        """
        return self.relations.get(domain)

    def delete_app(self, domain: str) -> None:
        """
        Args:
            domain: Application domain.
        """
        self.deleted.append(("app", domain))

    def delete_site(self, domain: str) -> None:
        """
        Args:
            domain: Site domain.
        """
        self.deleted.append(("site", domain))

    def delete_service(self, name: str) -> None:
        """
        Args:
            name: Service name.
        """
        self.deleted.append(("service", name))


class DeployerSpy:
    """
    A stand-in for a deployer.

    Attributes:
        configured: The keyword arguments ``configure`` received.
        deployed: Whether ``deploy`` was called.
        updated: Whether ``update`` was called.
        steps: Step descriptions reported during ``update``.
    """

    def __init__(self, verbose: bool = False) -> None:
        """
        Args:
            verbose: Verbosity the command constructed it with.
        """
        self.verbose = verbose
        self.configured: dict[str, Any] = {}
        self.deployed = False
        self.updated = False
        self.steps: list[str] = []

    def configure(self, **kwargs: Any) -> None:
        """
        Args:
            **kwargs: Whatever the command passed.
        """
        self.configured = kwargs

    def deploy(self) -> bool:
        """
        Returns:
            Always True.
        """
        self.deployed = True
        return True

    def update(self, on_step: Any = None) -> Any:
        """
        Args:
            on_step: Progress reporter.

        Returns:
            A result describing a plain, non-static build.
        """
        self.updated = True
        if on_step:
            for message in ("Installing dependencies", "Building"):
                self.steps.append(message)
                on_step(message)
        return SimpleNamespace(
            package_manager="pnpm",
            prisma_updated=False,
            is_static=False,
            start_command="/usr/bin/node server.js",
        )


def make_app(
    domain: str = "example.com",
    *,
    app_type: str = "nextjs",
    app_path: str = "/var/www/apps/example-com",
    is_static: bool = False,
) -> SimpleNamespace:
    """
    Build a store row for an application.

    Args:
        domain: Application domain.
        app_type: Application type.
        app_path: Directory the application lives in.
        is_static: Whether the application has no service.

    Returns:
        Something shaped like a store App row.
    """
    return SimpleNamespace(
        id=1,
        domain=domain,
        app_type=app_type,
        app_path=app_path,
        is_static=is_static,
        status="running",
        port=3000,
        ssl_enabled=True,
        source="https://github.com/user/repo",
        branch="main",
        deployed_at="2026-01-01T00:00:00",
    )


@pytest.fixture
def cli_runner() -> CliRunner:
    """
    Provide a Click test runner.

    Returns:
        A runner that keeps stderr in the captured output.
    """
    return CliRunner()


@pytest.fixture
def console(monkeypatch: pytest.MonkeyPatch) -> io.StringIO:
    """
    Collect what the commands print.

    ``Logger`` binds ``sys.stdout`` as a default argument value at import time,
    so neither capsys nor the CliRunner ever sees its output; giving it an
    explicit stream is the only reliable way to read it back. Both the shared
    context logger and the one the argparse entry point builds are redirected.

    Args:
        monkeypatch: Patching helper, scoped to the test.

    Returns:
        The buffer the commands write into.
    """
    buffer = io.StringIO()
    patched = functools.partial(Logger, stream=buffer)
    monkeypatch.setattr(cli_app, "Logger", patched)
    monkeypatch.setattr(webapp, "Logger", patched)
    return buffer


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> StoreSpy:
    """
    Replace the SQLite store with a spy for the duration of a test.

    Args:
        monkeypatch: Patching helper.

    Returns:
        The spy the commands will use.
    """
    spy = StoreSpy()
    monkeypatch.setattr(webapp, "get_store", lambda: spy)
    return spy


@pytest.fixture
def services(monkeypatch: pytest.MonkeyPatch) -> ServiceSpy:
    """
    Replace the service manager with a spy for the duration of a test.

    Args:
        monkeypatch: Patching helper.

    Returns:
        The spy the commands will use.
    """
    spy = ServiceSpy()

    def _build(verbose: bool = False) -> ServiceSpy:
        spy.verbose = verbose
        return spy

    monkeypatch.setattr(webapp, "ServiceManager", _build)
    return spy


@pytest.fixture
def deployer(monkeypatch: pytest.MonkeyPatch) -> DeployerSpy:
    """
    Replace the deployer registry with a spy, and clear the readiness gate.

    Args:
        monkeypatch: Patching helper.

    Returns:
        The spy the commands will use.
    """
    spy = DeployerSpy()
    monkeypatch.setattr(webapp, "get_deployer", lambda app_type, verbose=False: spy)
    monkeypatch.setattr(
        webapp,
        "check_deployment_ready",
        lambda app_type, package_manager, verbose: (True, [], []),
    )
    return spy


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
# Surface: the contract
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", COMMANDS)
def test_every_command_answers_help(cli_runner: CliRunner, name: str) -> None:
    """
    Args:
        cli_runner: Click test runner.
        name: Command under test.
    """
    result = cli_runner.invoke(root_cli, [name, "--help"])

    assert result.exit_code == 0, result.output
    assert f"Usage: cli {name}" in result.output


@pytest.mark.parametrize(("alias", "target"), sorted(COMMAND_ALIASES.items()))
def test_every_alias_resolves_to_its_command(
    cli_runner: CliRunner, alias: str, target: str
) -> None:
    """
    Args:
        cli_runner: Click test runner.
        alias: Alternative spelling.
        target: Command it must reach.
    """
    assert ALIASES[alias] == target

    result = cli_runner.invoke(root_cli, [alias, "--help"])

    assert result.exit_code == 0, result.output
    assert f"Usage: cli {target}" in result.output


def test_the_group_exposes_exactly_the_nine_commands() -> None:
    """The lazy loader in app.py looks these up by name."""
    assert sorted(webapp.cli.commands) == sorted(COMMANDS)


@pytest.mark.parametrize("name", COMMANDS)
def test_no_command_redeclares_a_global_flag(name: str) -> None:
    """
    Args:
        name: Command under test.
    """
    command = webapp.cli.commands[name]

    bound = {param.name for param in command.params if param.expose_value}
    assert not bound & GLOBAL_FLAG_NAMES

    assert command.callback is not None
    signature = inspect.signature(command.callback)
    assert not set(signature.parameters) & GLOBAL_FLAG_NAMES


# ---------------------------------------------------------------------------
# Validation: Click refuses before anything runs
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "argv",
    [
        ["create"],
        ["create", "-d", "example.com"],
        ["create", "-s", "https://github.com/user/repo"],
        ["status"],
        ["start"],
        ["stop"],
        ["restart"],
        ["update"],
        ["delete"],
        ["logs"],
    ],
    ids=lambda argv: " ".join(argv),
)
def test_a_missing_argument_is_a_usage_error(
    cli_runner: CliRunner, runner: FakeRunner, argv: list[str]
) -> None:
    """
    Args:
        cli_runner: Click test runner.
        runner: Fake process runner, asserted to stay untouched.
        argv: Incomplete command line.
    """
    result = cli_runner.invoke(root_cli, argv)

    assert result.exit_code == 2, result.output
    assert "Traceback" not in result.output
    assert "Missing" in result.output
    assert runner.calls == []


@pytest.mark.parametrize(
    "argv",
    [
        ["create", "-d", "example.com", "-s", ".", "-p", "abc"],
        ["create", "-d", "example.com", "-s", ".", "-t", "cobol"],
        ["create", "-d", "example.com", "-s", ".", "-w", "iis"],
        ["create", "-d", "example.com", "-s", ".", "--pm", "yarn"],
        ["create", "-d", "example.com", "-s", ".", "--env-file", "/nowhere/.env"],
        ["update", "example.com", "--package-manager", "yarn"],
        ["logs", "example.com", "-n", "lots"],
    ],
    ids=lambda argv: " ".join(argv),
)
def test_an_invalid_value_is_rejected_before_anything_runs(
    cli_runner: CliRunner, runner: FakeRunner, argv: list[str]
) -> None:
    """
    Args:
        cli_runner: Click test runner.
        runner: Fake process runner, asserted to stay untouched.
        argv: Command line with one bad value.
    """
    result = cli_runner.invoke(root_cli, argv)

    assert result.exit_code == 2, result.output
    assert "Traceback" not in result.output
    assert "Invalid value" in result.output
    assert runner.calls == []


# ---------------------------------------------------------------------------
# Global flags reach the shared context from either side of the command name
# ---------------------------------------------------------------------------


def test_verbose_after_the_command_name_reaches_the_context(
    cli_runner: CliRunner, store: StoreSpy
) -> None:
    """
    Args:
        cli_runner: Click test runner.
        store: Store spy, empty.
    """
    state = Context()

    result = cli_runner.invoke(webapp.cli.commands["list"], ["-v"], obj=state)

    assert result.exit_code == 0, result.output
    assert state.verbose is True


def test_dry_run_before_the_command_name_survives_it(
    cli_runner: CliRunner, store: StoreSpy, runner: FakeRunner
) -> None:
    """
    Args:
        cli_runner: Click test runner.
        store: Store spy, empty.
        runner: Fake process runner, replaced by the dry-run wrapper.
    """
    state = Context()

    result = cli_runner.invoke(root_cli, ["--dry-run", "list"], obj=state)

    assert result.exit_code == 0, result.output
    assert state.dry_run is True
    assert isinstance(get_runner(), DryRunRunner)


# ---------------------------------------------------------------------------
# create
# ---------------------------------------------------------------------------


def test_create_configures_the_deployer_and_deploys(
    cli_runner: CliRunner, deployer: DeployerSpy, tmp_path: Path
) -> None:
    """
    Args:
        cli_runner: Click test runner.
        deployer: Deployer spy.
        tmp_path: Temporary directory holding the environment file.
    """
    env_file = tmp_path / ".env"
    env_file.write_text('DATABASE_URL="postgres://localhost/app"\n# comment\nBROKEN\n')

    result = cli_runner.invoke(
        webapp.cli.commands["create"],
        [
            "-d",
            "example.com",
            "-s",
            "https://github.com/user/repo",
            "-t",
            "nextjs",
            "-p",
            "3100",
            "-b",
            "main",
            "--www",
            "--env-file",
            str(env_file),
            "--pm",
            "pnpm",
        ],
    )

    assert result.exit_code == 0, result.output
    assert deployer.deployed is True
    assert deployer.configured == {
        "domain": "example.com",
        "source": "https://github.com/user/repo",
        "port": 3100,
        "webserver": "nginx",
        "ssl": True,
        "branch": "main",
        # The quotes are stripped here so they never reach systemd's Environment=.
        "env_vars": {"DATABASE_URL": "postgres://localhost/app"},
        "package_manager": "pnpm",
        "include_www": True,
    }


def test_create_without_ssl_asks_the_deployer_for_no_certificate(
    cli_runner: CliRunner, deployer: DeployerSpy
) -> None:
    """
    Args:
        cli_runner: Click test runner.
        deployer: Deployer spy.
    """
    result = cli_runner.invoke(
        webapp.cli.commands["create"],
        ["-d", "example.com", "-s", ".", "-p", "3100", "--no-ssl", "-w", "apache"],
    )

    assert result.exit_code == 0, result.output
    assert deployer.configured["ssl"] is False
    assert deployer.configured["webserver"] == "apache"


def test_create_stops_when_the_system_is_not_ready(
    cli_runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    deployer: DeployerSpy,
    console: io.StringIO,
) -> None:
    """
    Args:
        cli_runner: Click test runner.
        monkeypatch: Patching helper.
        deployer: Deployer spy, asserted to stay untouched.
        console: Buffer holding what the logger printed.
    """
    monkeypatch.setattr(
        webapp,
        "check_deployment_ready",
        lambda app_type, package_manager, verbose: (False, ["node is not installed"], []),
    )

    result = cli_runner.invoke(
        webapp.cli.commands["create"], ["-d", "example.com", "-s", ".", "-p", "3100"]
    )

    assert result.exit_code == 1
    assert "node is not installed" in shown(result, console)
    assert deployer.deployed is False


def test_create_maps_monorepo_subdomains(
    cli_runner: CliRunner, monkeypatch: pytest.MonkeyPatch, deployer: DeployerSpy
) -> None:
    """
    Args:
        cli_runner: Click test runner.
        monkeypatch: Patching helper.
        deployer: Deployer spy, replacing the monorepo deployer.
    """
    monkeypatch.setattr(webapp, "MonorepoDeployer", lambda verbose=False: deployer)

    result = cli_runner.invoke(
        webapp.cli.commands["create"],
        [
            "-d",
            "example.com",
            "-s",
            ".",
            "-p",
            "3100",
            "-t",
            "monorepo",
            "--subdomains",
            "erp-backend:api",
            "--subdomains",
            "web-gateway:app",
            "--workspaces",
            "erp-backend",
            "--no-database",
        ],
    )

    assert result.exit_code == 0, result.output
    assert deployer.configured["subdomain_overrides"] == {
        "erp-backend": "api",
        "web-gateway": "app",
    }
    assert deployer.configured["workspace_filter"] == ["erp-backend"]
    assert deployer.configured["skip_database"] is True


def test_create_passes_compose_settings_through(
    cli_runner: CliRunner, monkeypatch: pytest.MonkeyPatch, deployer: DeployerSpy
) -> None:
    """
    Args:
        cli_runner: Click test runner.
        monkeypatch: Patching helper.
        deployer: Deployer spy, replacing the compose deployer.
    """
    monkeypatch.setattr(webapp, "DockerComposeDeployer", lambda verbose=False: deployer)

    result = cli_runner.invoke(
        webapp.cli.commands["create"],
        [
            "-d",
            "example.com",
            "-s",
            ".",
            "-p",
            "3100",
            "-t",
            "docker-compose",
            "--compose-file",
            "docker-compose.prod.yml",
            "--compose-profiles",
            "web",
            "--compose-profiles",
            "worker",
        ],
    )

    assert result.exit_code == 0, result.output
    assert deployer.configured["compose_file"] == "docker-compose.prod.yml"
    assert deployer.configured["compose_profiles"] == ["web", "worker"]


# ---------------------------------------------------------------------------
# list and status
# ---------------------------------------------------------------------------


def test_list_reports_every_deployed_app(
    cli_runner: CliRunner, store: StoreSpy, services: ServiceSpy, console: io.StringIO
) -> None:
    """
    Args:
        cli_runner: Click test runner.
        store: Store spy.
        services: Service manager spy.
        console: Buffer holding what the logger printed.
    """
    store.apps["example.com"] = make_app()

    result = cli_runner.invoke(webapp.cli.commands["list"], [])
    output = shown(result, console)

    assert result.exit_code == 0, output
    assert "example.com" in output
    assert "Total: 1 apps" in output


def test_list_reports_the_live_state_not_the_stored_one(
    cli_runner: CliRunner, store: StoreSpy, services: ServiceSpy, console: io.StringIO
) -> None:
    """
    The regression an operator hit: list said Running, health said stopped.

    The status column is written at deploy time and never again, so list was
    reporting what had been true once. systemd is the authority.
    """
    store.apps["example.com"] = make_app()
    assert store.apps["example.com"].status == "running", "the stored value must disagree"
    services.status = {"exists": True, "active": False, "enabled": True}

    result = cli_runner.invoke(webapp.cli.commands["list"], [])
    output = shown(result, console)

    assert result.exit_code == 0, output
    assert "Stopped" in output
    assert "Running" not in output
    assert ("get_status", "example-com") in services.calls


def test_list_does_not_call_a_static_site_stopped(
    cli_runner: CliRunner, store: StoreSpy, services: ServiceSpy, console: io.StringIO
) -> None:
    """
    A static site has no unit, so asking systemd about it always says stopped.

    Args:
        cli_runner: Click test runner.
        store: Store spy.
        services: Service manager spy.
        console: Buffer holding what the logger printed.
    """
    store.apps["example.com"] = make_app(is_static=True)

    result = cli_runner.invoke(webapp.cli.commands["list"], [])
    output = shown(result, console)

    assert result.exit_code == 0, output
    assert "Static" in output
    assert "need attention" not in output
    assert not [call for call in services.calls if call[0] == "get_status"], (
        "a static site has no unit to ask about"
    )


def test_list_reports_a_crash_loop_rather_than_running(
    cli_runner: CliRunner, store: StoreSpy, services: ServiceSpy, console: io.StringIO
) -> None:
    """
    A service systemd restarts every few seconds reads as active in between.

    Args:
        cli_runner: Click test runner.
        store: Store spy.
        services: Service manager spy.
        console: Buffer holding what the logger printed.
    """
    store.apps["example.com"] = make_app()
    services.status = {
        "exists": True,
        "active": True,
        "enabled": True,
        "active_state": "activating",
        "sub_state": "auto-restart",
        "restarts": "37",
    }

    result = cli_runner.invoke(webapp.cli.commands["list"], [])
    output = shown(result, console)

    assert result.exit_code == 0, output
    assert "Restarting" in output
    assert "restarted 37 times" in output


def test_list_reports_a_service_that_answers_nothing(
    cli_runner: CliRunner,
    store: StoreSpy,
    services: ServiceSpy,
    console: io.StringIO,
    ports: Any,
) -> None:
    """
    systemd being satisfied only means a process exists.

    Args:
        cli_runner: Click test runner.
        store: Store spy.
        services: Service manager spy.
        console: Buffer holding what the logger printed.
        ports: Port probe, told to refuse the application's port.
    """
    store.apps["example.com"] = make_app()
    ports.closed.add(store.apps["example.com"].port)
    services.status = {
        "exists": True,
        "active": True,
        "enabled": True,
        "active_state": "active",
        "sub_state": "running",
    }

    result = cli_runner.invoke(webapp.cli.commands["list"], [])
    output = shown(result, console)

    assert result.exit_code == 0, output
    assert "No answer" in output
    assert "nothing accepts connections" in output


def test_list_says_so_when_nothing_is_deployed(
    cli_runner: CliRunner, store: StoreSpy, console: io.StringIO
) -> None:
    """
    Args:
        cli_runner: Click test runner.
        store: Store spy, empty.
        console: Buffer holding what the logger printed.
    """
    result = cli_runner.invoke(webapp.cli.commands["list"], [])
    output = shown(result, console)

    assert result.exit_code == 0, output
    assert "No applications deployed" in output


def test_status_reports_the_stored_app_and_the_live_service(
    cli_runner: CliRunner,
    store: StoreSpy,
    services: ServiceSpy,
    console: io.StringIO,
) -> None:
    """
    Args:
        cli_runner: Click test runner.
        store: Store spy.
        services: Service manager spy.
        console: Buffer holding what the logger printed.
    """
    store.relations["example.com"] = {
        "app": make_app(),
        "site": SimpleNamespace(
            webserver="nginx",
            ssl_enabled=True,
            config_path="/etc/nginx/sites-available/example.com",
        ),
        "service": SimpleNamespace(name="example-com"),
        "databases": [],
    }

    result = cli_runner.invoke(webapp.cli.commands["status"], ["example.com"])
    output = shown(result, console)

    assert result.exit_code == 0, output
    assert "Status: example.com" in output
    assert ("get_status", "example-com") in services.calls


def test_status_of_an_unknown_app_fails(
    cli_runner: CliRunner,
    store: StoreSpy,
    services: ServiceSpy,
    console: io.StringIO,
) -> None:
    """
    Args:
        cli_runner: Click test runner.
        store: Store spy, empty.
        services: Service manager spy.
        console: Buffer holding what the logger printed.
    """
    services.status = {"exists": False, "active": False, "enabled": False, "name": "example-com"}

    result = cli_runner.invoke(webapp.cli.commands["status"], ["example.com"])

    assert result.exit_code == 1
    assert "Application not found: example.com" in shown(result, console)


# ---------------------------------------------------------------------------
# start, stop, restart
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "expected"),
    [("start", "started"), ("stop", "stopped"), ("restart", "restarted")],
)
def test_service_commands_drive_the_service_manager(
    cli_runner: CliRunner,
    store: StoreSpy,
    services: ServiceSpy,
    console: io.StringIO,
    name: str,
    expected: str,
) -> None:
    """
    Args:
        cli_runner: Click test runner.
        store: Store spy.
        services: Service manager spy.
        console: Buffer holding what the logger printed.
        name: Command under test.
        expected: Past participle the success message must use.
    """
    store.apps["example.com"] = make_app()

    result = cli_runner.invoke(webapp.cli.commands[name], ["example.com"])
    output = shown(result, console)

    assert result.exit_code == 0, output
    assert services.calls == [("service_exists", "example-com"), (name, "example-com")]
    assert f"Application {expected}: example.com" in output


@pytest.mark.parametrize("name", ["start", "stop", "restart"])
def test_service_commands_leave_a_static_site_alone(
    cli_runner: CliRunner,
    store: StoreSpy,
    services: ServiceSpy,
    console: io.StringIO,
    name: str,
) -> None:
    """
    Args:
        cli_runner: Click test runner.
        store: Store spy.
        services: Service manager spy, asserted to stay untouched.
        console: Buffer holding what the logger printed.
        name: Command under test.
    """
    store.apps["example.com"] = make_app(is_static=True)

    result = cli_runner.invoke(webapp.cli.commands[name], ["example.com"])
    output = shown(result, console)

    assert result.exit_code == 0, output
    assert services.calls == []
    assert f"no service to {name}" in output


def test_restart_fails_when_there_is_no_service(
    cli_runner: CliRunner,
    store: StoreSpy,
    services: ServiceSpy,
    console: io.StringIO,
) -> None:
    """
    Args:
        cli_runner: Click test runner.
        store: Store spy, empty.
        services: Service manager spy.
        console: Buffer holding what the logger printed.
    """
    services.exists = False

    result = cli_runner.invoke(webapp.cli.commands["restart"], ["example.com"])

    assert result.exit_code == 1
    assert "Service not found for: example.com" in shown(result, console)


# ---------------------------------------------------------------------------
# update
# ---------------------------------------------------------------------------


def test_update_delegates_the_rebuild_to_the_deployer(
    cli_runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    store: StoreSpy,
    services: ServiceSpy,
    deployer: DeployerSpy,
    tmp_path: Path,
) -> None:
    """
    Args:
        cli_runner: Click test runner.
        monkeypatch: Patching helper.
        store: Store spy.
        services: Service manager spy.
        deployer: Deployer spy.
        tmp_path: Directory standing in for the deployed application.
    """
    app_path = tmp_path / "example-com"
    app_path.mkdir()
    store.apps["example.com"] = make_app(app_path=str(app_path))

    pulls: list[tuple[Path, str | None]] = []
    monkeypatch.setattr(
        webapp,
        "SourceManager",
        lambda verbose=False: SimpleNamespace(
            pull=lambda path, branch=None: pulls.append((path, branch)),
            fetch=lambda *a, **kw: None,
        ),
    )
    monkeypatch.setattr(
        webapp,
        "RollbackManager",
        lambda verbose=False: SimpleNamespace(create_pre_deploy_backup=lambda **kw: None),
    )
    monkeypatch.setattr(webapp.time, "sleep", lambda seconds: None)

    result = cli_runner.invoke(
        webapp.cli.commands["update"], ["example.com", "-b", "release", "--pm", "pnpm"]
    )

    assert result.exit_code == 0, result.output
    assert pulls == [(app_path, "release")]
    assert deployer.updated is True
    # The command reports progress; it no longer drives the steps itself.
    assert deployer.steps == ["Installing dependencies", "Building"]
    assert deployer.configured["package_manager"] == "pnpm"
    assert ("restart", "example-com") in services.calls


def test_update_refuses_an_application_that_is_not_there(
    cli_runner: CliRunner, monkeypatch: pytest.MonkeyPatch, store: StoreSpy, tmp_path: Path
) -> None:
    """
    Args:
        cli_runner: Click test runner.
        monkeypatch: Patching helper.
        store: Store spy, empty.
        tmp_path: Empty applications directory.
    """
    monkeypatch.setattr(webapp, "Config", lambda: SimpleNamespace(apps_directory=tmp_path))

    result = cli_runner.invoke(webapp.cli.commands["update"], ["example.com"])

    assert result.exit_code == 1
    assert isinstance(result.exception, webapp.WASMError)
    assert "Application not found: example.com" in str(result.exception)


# ---------------------------------------------------------------------------
# delete
# ---------------------------------------------------------------------------


def test_delete_names_the_application_before_removing_it(
    cli_runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    store: StoreSpy,
    services: ServiceSpy,
    console: io.StringIO,
    tmp_path: Path,
) -> None:
    """
    Args:
        cli_runner: Click test runner.
        monkeypatch: Patching helper.
        store: Store spy.
        services: Service manager spy, asserted to stay untouched.
        console: Buffer holding what the logger printed.
        tmp_path: Applications directory.
    """
    monkeypatch.setattr(webapp, "Config", lambda: SimpleNamespace(apps_directory=tmp_path))
    store.apps["example.com"] = make_app()

    result = cli_runner.invoke(webapp.cli.commands["delete"], ["example.com"], input="n\n")
    output = shown(result, console)

    assert result.exit_code == 0, output
    # The prompt names the application and what goes with it, not "are you sure".
    assert "Delete the application example.com?" in output
    assert "the example-com service" in output
    assert "Aborted" in output
    assert store.deleted == []
    assert services.calls == []


def test_delete_with_force_removes_the_whole_deployment(
    cli_runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    store: StoreSpy,
    services: ServiceSpy,
    tmp_path: Path,
) -> None:
    """
    Args:
        cli_runner: Click test runner.
        monkeypatch: Patching helper.
        store: Store spy.
        services: Service manager spy.
        tmp_path: Applications directory.
    """
    monkeypatch.setattr(webapp, "Config", lambda: SimpleNamespace(apps_directory=tmp_path))
    monkeypatch.setattr(webapp, "NginxManager", lambda verbose=False: _AbsentSite())
    monkeypatch.setattr(webapp, "ApacheManager", lambda verbose=False: _AbsentSite())
    monkeypatch.setattr(
        webapp,
        "CertManager",
        lambda verbose=False: SimpleNamespace(
            is_installed=lambda: False, cert_exists=lambda d: False
        ),
    )
    removed: list[Path] = []
    monkeypatch.setattr(webapp, "remove_directory", lambda path, sudo=False: removed.append(path))
    (tmp_path / "example-com").mkdir()
    store.apps["example.com"] = make_app()

    result = cli_runner.invoke(webapp.cli.commands["delete"], ["example.com", "-y"])

    assert result.exit_code == 0, result.output
    assert ("delete_service", "example-com") in services.calls
    assert removed == [tmp_path / "example-com"]
    assert store.deleted == [
        ("site", "example.com"),
        ("service", "example-com"),
        ("app", "example.com"),
    ]


def test_delete_keeps_the_files_when_asked(
    cli_runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    store: StoreSpy,
    services: ServiceSpy,
    console: io.StringIO,
    tmp_path: Path,
) -> None:
    """
    Args:
        cli_runner: Click test runner.
        monkeypatch: Patching helper.
        store: Store spy.
        services: Service manager spy.
        console: Buffer holding what the logger printed.
        tmp_path: Applications directory.
    """
    monkeypatch.setattr(webapp, "Config", lambda: SimpleNamespace(apps_directory=tmp_path))
    monkeypatch.setattr(webapp, "NginxManager", lambda verbose=False: _AbsentSite())
    monkeypatch.setattr(webapp, "ApacheManager", lambda verbose=False: _AbsentSite())
    monkeypatch.setattr(
        webapp,
        "CertManager",
        lambda verbose=False: SimpleNamespace(
            is_installed=lambda: False, cert_exists=lambda d: False
        ),
    )
    removed: list[Path] = []
    monkeypatch.setattr(webapp, "remove_directory", lambda path, sudo=False: removed.append(path))
    (tmp_path / "example-com").mkdir()
    store.apps["example.com"] = make_app()

    result = cli_runner.invoke(
        webapp.cli.commands["delete"], ["example.com", "--force", "--keep-files"]
    )
    output = shown(result, console)

    assert result.exit_code == 0, output
    assert removed == []
    assert "Keeping application files" in output


def test_delete_under_dry_run_only_reports(
    cli_runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    store: StoreSpy,
    services: ServiceSpy,
    console: io.StringIO,
    tmp_path: Path,
) -> None:
    """
    Args:
        cli_runner: Click test runner.
        monkeypatch: Patching helper.
        store: Store spy.
        services: Service manager spy.
        console: Buffer holding what the logger printed.
        tmp_path: Applications directory.
    """
    monkeypatch.setattr(webapp, "Config", lambda: SimpleNamespace(apps_directory=tmp_path))
    monkeypatch.setattr(webapp, "NginxManager", lambda verbose=False: _AbsentSite())
    monkeypatch.setattr(webapp, "ApacheManager", lambda verbose=False: _AbsentSite())
    (tmp_path / "example-com").mkdir()
    store.apps["example.com"] = make_app()

    result = cli_runner.invoke(
        webapp.cli.commands["delete"], ["example.com", "--force"], obj=Context(dry_run=True)
    )
    output = shown(result, console)

    assert result.exit_code == 0, output
    assert "Would delete example.com" in output
    assert store.deleted == []
    assert ("delete_service", "example-com") not in services.calls


def test_delete_of_an_unknown_app_fails(
    cli_runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    store: StoreSpy,
    console: io.StringIO,
    tmp_path: Path,
) -> None:
    """
    Args:
        cli_runner: Click test runner.
        monkeypatch: Patching helper.
        store: Store spy, empty.
        console: Buffer holding what the logger printed.
        tmp_path: Empty applications directory.
    """
    monkeypatch.setattr(webapp, "Config", lambda: SimpleNamespace(apps_directory=tmp_path))

    result = cli_runner.invoke(webapp.cli.commands["delete"], ["example.com", "--force"])

    assert result.exit_code == 1
    assert "Application not found: example.com" in shown(result, console)


class _AbsentSite:
    """A web server manager that reports no site for any domain."""

    def site_exists(self, domain: str) -> bool:
        """
        Args:
            domain: Domain to look for.

        Returns:
            Always False.
        """
        return False


# ---------------------------------------------------------------------------
# logs
# ---------------------------------------------------------------------------


def test_logs_prints_the_journal(
    cli_runner: CliRunner, store: StoreSpy, services: ServiceSpy
) -> None:
    """
    Args:
        cli_runner: Click test runner.
        store: Store spy, empty.
        services: Service manager spy.
    """
    result = cli_runner.invoke(webapp.cli.commands["logs"], ["example.com", "-n", "10"])

    assert result.exit_code == 0, result.output
    assert services.calls == [("logs", "example-com:10")]
    assert "a log line" in result.output


def test_logs_follow_runs_journalctl(
    cli_runner: CliRunner, store: StoreSpy, services: ServiceSpy, runner: FakeRunner
) -> None:
    """
    Args:
        cli_runner: Click test runner.
        store: Store spy, empty.
        services: Service manager spy.
        runner: Fake process runner.
    """
    result = cli_runner.invoke(webapp.cli.commands["logs"], ["example.com", "--follow", "-n", "25"])

    assert result.exit_code == 0, result.output
    assert runner.calls == [
        ("journalctl", "-u", "example-com.service", "-f", "-n", "25"),
    ]


def test_logs_of_a_compose_app_asks_docker(
    cli_runner: CliRunner, store: StoreSpy, runner: FakeRunner, tmp_path: Path
) -> None:
    """
    Args:
        cli_runner: Click test runner.
        store: Store spy.
        runner: Fake process runner.
        tmp_path: Directory standing in for the deployed application.
    """
    app_path = tmp_path / "example-com"
    app_path.mkdir()
    compose_file = app_path / "docker-compose.yml"
    compose_file.write_text("services: {}\n")
    store.apps["example.com"] = make_app(app_type="docker-compose", app_path=str(app_path))

    result = cli_runner.invoke(webapp.cli.commands["logs"], ["example.com"])

    assert result.exit_code == 0, result.output
    assert runner.calls == [
        ("docker", "compose", "-f", str(compose_file), "logs", "--tail", "50"),
    ]


# ---------------------------------------------------------------------------
# --open: deep links into the panel
# ---------------------------------------------------------------------------


def _status_relations() -> dict[str, Any]:
    """
    Returns:
        A store relations payload that ``status --open`` can succeed against.
    """
    return {
        "app": make_app(),
        "site": SimpleNamespace(
            webserver="nginx",
            ssl_enabled=True,
            config_path="/etc/nginx/sites-available/example.com",
        ),
        "service": SimpleNamespace(name="example-com"),
        "databases": [],
    }


@pytest.mark.parametrize(
    ("command", "args", "path"),
    [
        ("status", ["example.com"], "/apps/example.com"),
        ("list", [], "/apps"),
        ("logs", ["example.com"], "/apps/example.com"),
    ],
)
def test_open_prints_the_configured_panel_url_without_a_display(
    cli_runner: CliRunner,
    store: StoreSpy,
    services: ServiceSpy,
    console: io.StringIO,
    isolated_panel_config: Path,
    runner: FakeRunner,
    monkeypatch: pytest.MonkeyPatch,
    command: str,
    args: list[str],
    path: str,
) -> None:
    """
    ``--open`` prints the panel URL, and never touches xdg-open without a display.

    Args:
        cli_runner: Click test runner.
        store: Store spy.
        services: Service manager spy.
        console: Buffer holding what the logger printed.
        isolated_panel_config: Per-test configuration file.
        runner: Fake process runner.
        monkeypatch: Patching helper, scoped to the test.
        command: Command under test.
        args: Positional arguments the command needs.
        path: Panel path the command is expected to print.
    """
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    _configure_panel(isolated_panel_config)
    store.relations["example.com"] = _status_relations()
    store.apps["example.com"] = make_app()

    result = cli_runner.invoke(webapp.cli.commands[command], [*args, "--open"])
    output = shown(result, console)

    assert result.exit_code == 0, output
    assert f"http://127.0.0.1:8080{path}" in output
    assert not runner.calls_to("xdg-open")


@pytest.mark.parametrize(
    ("command", "args", "path"),
    [
        ("status", ["example.com"], "/apps/example.com"),
        ("list", [], "/apps"),
        ("logs", ["example.com"], "/apps/example.com"),
    ],
)
def test_open_launches_xdg_open_when_a_display_is_present(
    cli_runner: CliRunner,
    store: StoreSpy,
    services: ServiceSpy,
    console: io.StringIO,
    isolated_panel_config: Path,
    runner: FakeRunner,
    monkeypatch: pytest.MonkeyPatch,
    command: str,
    args: list[str],
    path: str,
) -> None:
    """
    With a display available, ``--open`` hands the URL to xdg-open.

    Args:
        cli_runner: Click test runner.
        store: Store spy.
        services: Service manager spy.
        console: Buffer holding what the logger printed.
        isolated_panel_config: Per-test configuration file.
        runner: Fake process runner.
        monkeypatch: Patching helper, scoped to the test.
        command: Command under test.
        args: Positional arguments the command needs.
        path: Panel path the command is expected to open.
    """
    monkeypatch.setenv("DISPLAY", ":0")
    _configure_panel(isolated_panel_config)
    store.relations["example.com"] = _status_relations()
    store.apps["example.com"] = make_app()

    result = cli_runner.invoke(webapp.cli.commands[command], [*args, "--open"])

    assert result.exit_code == 0, result.output
    assert runner.calls_to("xdg-open") == [("xdg-open", f"http://127.0.0.1:8080{path}")]


@pytest.mark.parametrize(
    ("command", "args"),
    [
        ("status", ["example.com"]),
        ("list", []),
        ("logs", ["example.com"]),
    ],
)
def test_open_without_a_configured_panel_warns_and_exits_clean(
    cli_runner: CliRunner,
    store: StoreSpy,
    services: ServiceSpy,
    console: io.StringIO,
    isolated_panel_config: Path,
    runner: FakeRunner,
    command: str,
    args: list[str],
) -> None:
    """
    The panel is off by default, so ``--open`` warns instead of guessing a URL.

    Args:
        cli_runner: Click test runner.
        store: Store spy.
        services: Service manager spy.
        console: Buffer holding what the logger printed.
        isolated_panel_config: Per-test configuration file, left unwritten.
        runner: Fake process runner.
        command: Command under test.
        args: Positional arguments the command needs.
    """
    store.relations["example.com"] = _status_relations()
    store.apps["example.com"] = make_app()

    result = cli_runner.invoke(webapp.cli.commands[command], [*args, "--open"])
    output = shown(result, console)

    assert result.exit_code == 0, output
    assert "not configured" in output
    assert not runner.calls_to("xdg-open")


# ---------------------------------------------------------------------------
# The argparse entry point still works: wasm.cli.parser calls it
# ---------------------------------------------------------------------------


def test_the_argparse_handler_shares_the_click_implementation(
    store: StoreSpy, services: ServiceSpy
) -> None:
    """
    Args:
        store: Store spy.
        services: Service manager spy.
    """
    store.apps["example.com"] = make_app()
    args = Namespace(action="restart", domain="example.com", verbose=False)

    assert webapp.handle_webapp(args) == 0
    assert services.calls == [("service_exists", "example-com"), ("restart", "example-com")]


def test_the_argparse_handler_still_reports_unknown_actions() -> None:
    """An action the dispatch table does not know is an error, not a crash."""
    args = Namespace(action="teleport", verbose=False)

    assert webapp.handle_webapp(args) == 1


def test_click_commands_are_real_commands() -> None:
    """Guards against a decorator ordering mistake that silently loses one."""
    for name, command in webapp.cli.commands.items():
        assert isinstance(command, click.Command), name
        assert command.help, f"{name} has no help text"
