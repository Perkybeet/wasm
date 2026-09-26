# Copyright (c) 2024-2026 Yago Lopez Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
Tests for ``wasm diagnose``.

The command itself does no correlation - that is
:mod:`wasm.managers.diagnose`'s job, covered in ``tests/test_diagnose.py`` -
so what is pinned here is the presentation contract: the verdict line, the
probable cause in bold above the checks, each check's evidence printed
verbatim and indented underneath it, ``--json`` (both the command's own flag
and the global one) emitting the dataclasses as JSON, and the exit code
signalling "down" the way ``wasm health`` signals an issue.

Every probe this pulls in is faked: :class:`~wasm.core.runner.FakeRunner` for
everything that would otherwise shell out, a fake HTTP getter so no probe
opens a socket, and a fake ``shutil.disk_usage`` so no probe reads the real
disk.
"""

from __future__ import annotations

import json
from collections import namedtuple
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner, Result

from wasm.cli.app import cli as root_cli
from wasm.core.logger import Logger
from wasm.core.runner import FakeRunner
from wasm.core.store import App, DeploymentRecord, Service
from wasm.managers import diagnose as diagnose_module

DOMAIN = "example.com"
APP_NAME = "example-com"
UNIT_FILE = "example-com.service"
FRAGMENT_PATH = f"/etc/systemd/system/{UNIT_FILE}"

#: A path nothing ever writes to, so the nginx log check deterministically skips.
NONEXISTENT_LOG = Path("/nonexistent/wasm-cli-test-nginx-error.log")

_DiskUsage = namedtuple("_DiskUsage", "total used free")


class FakeStore:
    """Serves both the store the command reads and the one ServiceManager reads."""

    def __init__(
        self,
        app: App | None = None,
        deployments: list[DeploymentRecord] | None = None,
        services: list[Service] | None = None,
    ) -> None:
        self._app = app
        self._deployments = deployments or []
        self._services = services if services is not None else [Service(name=APP_NAME)]

    def get_app(self, _domain: str) -> App | None:
        return self._app

    def list_deployments(self, _domain: str, limit: int = 50) -> list[DeploymentRecord]:
        return self._deployments[:limit]

    def list_services(self, **_kwargs: Any) -> list[Service]:
        return self._services

    def list_apps(self, **_kwargs: Any) -> list[App]:
        return []


def _app(**overrides: Any) -> App:
    # ssl_enabled defaults to False here: these tests are about the CLI's
    # presentation of a diagnosis, not certificates, and a plain HTTP app
    # keeps the "healthy" scenario from needing certbot scripted too.
    fields: dict[str, Any] = {
        "domain": DOMAIN,
        "port": 3000,
        "is_static": False,
        "ssl_enabled": False,
    }
    fields.update(overrides)
    return App(**fields)


@pytest.fixture
def log(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """
    Collect what the command reports to the operator.

    :class:`~wasm.core.logger.Logger` binds ``sys.stdout`` dynamically, but
    the write itself is intercepted here rather than read back from the
    stream, matching the pattern the rest of the CLI test suite uses.
    """
    lines: list[str] = []
    monkeypatch.setattr(Logger, "_write", lambda self, message, newline=True: lines.append(message))
    return lines


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> FakeStore:
    """Install a fake store as both the diagnose module and ServiceManager see it."""
    fake = FakeStore()
    monkeypatch.setattr("wasm.managers.diagnose.get_store", lambda: fake)
    monkeypatch.setattr("wasm.managers.service_manager.get_store", lambda: fake)
    return fake


@pytest.fixture(autouse=True)
def _no_real_probes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the command's own default seams off the real machine."""
    monkeypatch.setattr(diagnose_module, "_default_http_get", lambda _url, _headers: (200, None))
    monkeypatch.setattr(
        "shutil.disk_usage",
        lambda _path: _DiskUsage(total=1_000_000_000, used=100_000_000, free=900_000_000),
    )
    monkeypatch.setattr(diagnose_module, "NGINX_ERROR_LOG", NONEXISTENT_LOG)


def _script_healthy(runner: FakeRunner) -> None:
    runner.script(
        ["systemctl", "show", "-p", "FragmentPath", UNIT_FILE],
        stdout=f"FragmentPath={FRAGMENT_PATH}\n",
    )
    runner.script(
        [
            "systemctl",
            "show",
            "-p",
            "ActiveState,SubState,Result,ExecMainStatus,NRestarts",
            UNIT_FILE,
        ],
        stdout="ActiveState=active\nSubState=running\nResult=success\nExecMainStatus=0\nNRestarts=0\n",
    )
    runner.script(
        ["ss", "-ltnpH"],
        stdout='LISTEN 0 511 127.0.0.1:3000 0.0.0.0:* users:(("node",pid=100,fd=18))\n',
    )
    runner.script(
        ["journalctl", "-u", UNIT_FILE, "-n", "50", "--no-pager", "-o", "short-iso"],
        stdout="2026-01-01T00:00:00+00:00 host node[100]: listening on 3000\n",
    )
    runner.script(
        ["journalctl", "-k", "--since", "-7d", "--grep", "oom", "-o", "short-iso"], stdout=""
    )
    runner.script(["certbot", "certificates"], stdout="")


def _invoke(args: list[str], **kwargs: Any) -> Result:
    # Not standalone_mode=False: a Click command's return value is not what
    # sets the process exit code (see cli/commands/diagnose.py's comment on
    # ctx.exit); only a real SystemExit, which only standalone mode raises,
    # is what CliRunner turns into result.exit_code.
    return CliRunner().invoke(root_cli, args, **kwargs)


def test_diagnose_prints_the_verdict_and_the_cause_in_that_order(
    log: list[str], runner: FakeRunner, store: FakeStore
) -> None:
    # Nothing is scripted: FragmentPath reports nothing, so the unit does not
    # exist and the diagnosis is unambiguous without any further setup.
    store._app = _app()

    result = _invoke(["diagnose", DOMAIN])

    assert result.exit_code == 1
    joined = "\n".join(log)
    assert "Verdict" in joined
    assert "down" in joined
    assert "No systemd unit found for example-com" in joined
    # The probable cause is printed before the per-check listing.
    cause_index = joined.index("No systemd unit found for example-com")
    unit_check_index = joined.index("unit:")
    assert cause_index < unit_check_index


def test_diagnose_prints_evidence_verbatim_and_indented(
    log: list[str], runner: FakeRunner, store: FakeStore
) -> None:
    _script_healthy(runner)
    store._app = _app()

    result = _invoke(["diagnose", DOMAIN])

    assert result.exit_code == 0
    assert any("listening on 3000" in line for line in log)
    # Evidence is indented well past the check line itself, and printed
    # verbatim: the journal's own timestamp and unit prefix are untouched.
    evidence_line = next(line for line in log if "listening on 3000" in line)
    assert evidence_line.startswith("        ")
    assert "2026-01-01T00:00:00+00:00 host node[100]: listening on 3000" in evidence_line


def test_diagnose_healthy_exits_zero(log: list[str], runner: FakeRunner, store: FakeStore) -> None:
    _script_healthy(runner)
    store._app = _app()

    result = _invoke(["diagnose", DOMAIN])

    assert result.exit_code == 0
    assert any("healthy" in line for line in log)


def test_diagnose_local_json_flag(runner: FakeRunner, store: FakeStore) -> None:
    _script_healthy(runner)
    store._app = _app()

    result = _invoke(["diagnose", DOMAIN, "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["domain"] == DOMAIN
    assert payload["verdict"] == "healthy"
    assert payload["probable_cause"] is None
    names = {c["name"] for c in payload["checks"]}
    assert {
        "unit",
        "port",
        "http_direct",
        "http_nginx",
        "journal",
        "nginx_log",
        "certificate",
        "last_deployment",
        "oom",
        "disk",
    } == names
    for check in payload["checks"]:
        assert set(check) == {"name", "status", "summary", "evidence"}


def test_diagnose_global_json_flag(runner: FakeRunner, store: FakeStore) -> None:
    _script_healthy(runner)
    store._app = _app()

    result = _invoke(["--json", "diagnose", DOMAIN])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["verdict"] == "healthy"


def test_diagnose_down_exits_nonzero(runner: FakeRunner, store: FakeStore) -> None:
    store._app = _app()
    # No unit scripted: the diagnosis is "down" before any further setup.

    result = _invoke(["diagnose", DOMAIN, "--json"])

    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["verdict"] == "down"


def test_diagnose_requires_a_domain(store: FakeStore) -> None:
    result = _invoke(["diagnose"])

    assert result.exit_code != 0
