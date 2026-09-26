# Copyright (c) 2024-2026 Yago Lopez Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
Tests for :mod:`wasm.managers.diagnose`.

Every probe is scripted through :class:`~wasm.core.runner.FakeRunner`, never a
real process, and the HTTP and disk probes are given fakes directly, never a
real socket or a real filesystem: that is the whole point of the injectable
seams this module exposes.

Two kinds of test live here: one per ordered rule in ``_decide`` (a crash
loop, a port mismatch, an expired certificate, ...), matching the "each rule
tested" requirement, and a handful for the isolation guarantee itself - a
probe that raises must not take any other probe down with it.
"""

from __future__ import annotations

from collections import namedtuple
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from wasm.core.exceptions import ValidationError
from wasm.core.runner import FakeRunner
from wasm.core.store import App, DeploymentRecord, Service
from wasm.managers import diagnose as diagnose_module
from wasm.managers.diagnose import Check, Diagnosis, diagnose
from wasm.managers.service_manager import ServiceManager

DOMAIN = "example.com"
APP_NAME = "example-com"
UNIT_FILE = "example-com.service"
FRAGMENT_PATH = f"/etc/systemd/system/{UNIT_FILE}"

#: A path nothing ever writes to, so the nginx log check deterministically skips
#: unless a test points NGINX_ERROR_LOG somewhere else.
NONEXISTENT_LOG = Path("/nonexistent/wasm-test-nginx-error.log")

_DiskUsage = namedtuple("_DiskUsage", "total used free")


class FakeStore:
    """
    Stands in for both the store ``diagnose`` reads directly and the one
    ServiceManager reads internally, so a test can hand it a service and an
    app record in one place.
    """

    def __init__(
        self,
        app: App | None = None,
        deployments: list[DeploymentRecord] | None = None,
        services: list[Service] | None = None,
    ) -> None:
        self._app = app
        self._deployments = deployments or []
        self._services = services or []

    def get_app(self, _domain: str) -> App | None:
        return self._app

    def list_deployments(self, _domain: str, limit: int = 50) -> list[DeploymentRecord]:
        return self._deployments[:limit]

    def list_services(self, **_kwargs: Any) -> list[Service]:
        return self._services

    def list_apps(self, **_kwargs: Any) -> list[App]:
        return []


def _app(**overrides: Any) -> App:
    fields: dict[str, Any] = {
        "domain": DOMAIN,
        "port": 3000,
        "is_static": False,
        "ssl_enabled": True,
    }
    fields.update(overrides)
    return App(**fields)


def _http(
    direct: tuple[int | None, str | None] = (200, None),
    nginx: tuple[int | None, str | None] = (200, None),
) -> Callable[[str, dict], tuple[int | None, str | None]]:
    """Build a fake http_get answering differently for the Host-header probe."""

    def _get(_url: str, headers: dict) -> tuple[int | None, str | None]:
        return nginx if headers.get("Host") else direct

    return _get


def _disk(percent_used: float, total: int = 1_000_000_000) -> Callable[[str], _DiskUsage]:
    free = int(total * (1 - percent_used / 100))
    return lambda _path: _DiskUsage(total=total, used=total - free, free=free)


def _fragment(runner: FakeRunner) -> None:
    runner.script(
        ["systemctl", "show", "-p", "FragmentPath", UNIT_FILE],
        stdout=f"FragmentPath={FRAGMENT_PATH}\n",
    )


def _unit_state(
    runner: FakeRunner,
    *,
    active: str = "active",
    sub: str = "running",
    result: str = "success",
    exec_status: str = "0",
    restarts: str = "0",
) -> None:
    runner.script(
        [
            "systemctl",
            "show",
            "-p",
            "ActiveState,SubState,Result,ExecMainStatus,NRestarts",
            UNIT_FILE,
        ],
        stdout=(
            f"ActiveState={active}\n"
            f"SubState={sub}\n"
            f"Result={result}\n"
            f"ExecMainStatus={exec_status}\n"
            f"NRestarts={restarts}\n"
        ),
    )


def _listening(runner: FakeRunner, port: int, pid: str = "100", process: str = "node") -> None:
    runner.script(
        ["ss", "-ltnpH"],
        stdout=(
            f"LISTEN 0      511          127.0.0.1:{port}       0.0.0.0:*    "
            f'users:(("{process}",pid={pid},fd=18))\n'
        ),
    )


def _main_pid(runner: FakeRunner, pid: str) -> None:
    runner.script(["systemctl", "show", "-p", "MainPID", UNIT_FILE], stdout=f"MainPID={pid}\n")


def _certbot_output(expiry_date: str) -> str:
    return (
        "Found the following certs:\n"
        f"  Certificate Name: {DOMAIN}\n"
        f"    Domains: {DOMAIN}\n"
        f"    Expiry Date: {expiry_date} 00:00:00+00:00 (VALID: 30 days)\n"
        f"    Certificate Path: /etc/letsencrypt/live/{DOMAIN}/fullchain.pem\n"
        f"    Private Key Path: /etc/letsencrypt/live/{DOMAIN}/privkey.pem\n"
    )


def _no_findings(runner: FakeRunner) -> None:
    """Script every read-only probe to report nothing of interest."""
    runner.script(
        ["journalctl", "-u", UNIT_FILE, "-n", "50", "--no-pager", "-o", "short-iso"], stdout=""
    )
    runner.script(
        ["journalctl", "-k", "--since", "-7d", "--grep", "oom", "-o", "short-iso"], stdout=""
    )
    runner.script(["certbot", "certificates"], stdout="")


@pytest.fixture
def runner() -> FakeRunner:
    return FakeRunner()


def _diagnose(
    monkeypatch: pytest.MonkeyPatch,
    runner: FakeRunner,
    *,
    app: App | None = None,
    deployments: list[DeploymentRecord] | None = None,
    services: list[Service] | None = None,
    now: datetime = datetime(2026, 1, 1, tzinfo=timezone.utc),
    http_get: Callable[[str, dict], tuple[int | None, str | None]] | None = None,
    disk_usage: Callable[[str], _DiskUsage] | None = None,
    nginx_log_path: Path | None = None,
) -> Diagnosis:
    """Wire a diagnosis with every seam under the test's control."""
    if services is None:
        services = [Service(name=APP_NAME)]
    store = FakeStore(app=app, deployments=deployments, services=services)
    monkeypatch.setattr("wasm.managers.service_manager.get_store", lambda: store)
    monkeypatch.setattr(diagnose_module, "NGINX_ERROR_LOG", nginx_log_path or NONEXISTENT_LOG)

    return diagnose(
        DOMAIN,
        runner=runner,
        store=store,
        now=lambda: now,
        http_get=http_get or _http(),
        disk_usage=disk_usage or _disk(10.0),
    )


def _check(diagnosis: Diagnosis, name: str) -> Check:
    return next(c for c in diagnosis.checks if c.name == name)


# -- Healthy ------------------------------------------------------------------


def test_healthy_app_is_reported_healthy(
    monkeypatch: pytest.MonkeyPatch, runner: FakeRunner
) -> None:
    _fragment(runner)
    _unit_state(runner)
    _listening(runner, 3000)
    _no_findings(runner)
    runner.script(["certbot", "certificates"], stdout=_certbot_output("2026-06-01"))

    result = _diagnose(
        monkeypatch,
        runner,
        app=_app(),
        deployments=[DeploymentRecord(domain=DOMAIN, status="success")],
        disk_usage=_disk(10.0),
    )

    assert result.verdict == "healthy"
    assert result.probable_cause is None
    assert all(c.status in ("ok", "skip") for c in result.checks)
    assert _check(result, "unit").status == "ok"
    assert _check(result, "port").status == "ok"
    assert _check(result, "http_direct").status == "ok"
    assert _check(result, "http_nginx").status == "ok"


# -- Ordered rules, each tested -----------------------------------------------


def test_unit_missing_reports_down(monkeypatch: pytest.MonkeyPatch, runner: FakeRunner) -> None:
    # No FragmentPath scripted: systemd reports nothing, so the unit does not exist.
    _no_findings(runner)

    result = _diagnose(monkeypatch, runner, app=_app())

    assert result.verdict == "down"
    assert result.probable_cause is not None
    assert "No systemd unit found" in result.probable_cause
    assert _check(result, "unit").status == "fail"


def test_crash_loop_with_oom_blames_the_kernel(
    monkeypatch: pytest.MonkeyPatch, runner: FakeRunner
) -> None:
    _fragment(runner)
    _unit_state(runner, active="activating", sub="auto-restart", exec_status="137", restarts="6")
    runner.script(
        ["journalctl", "-u", UNIT_FILE, "-n", "50", "--no-pager", "-o", "short-iso"], stdout=""
    )
    runner.script(
        ["journalctl", "-k", "--since", "-7d", "--grep", "oom", "-o", "short-iso"],
        stdout="2026-01-01T00:00:00+00:00 host kernel: Out of memory: Killed process 100 (node)\n",
    )
    runner.script(["certbot", "certificates"], stdout="")

    result = _diagnose(monkeypatch, runner, app=_app())

    assert result.verdict == "down"
    assert (
        result.probable_cause
        == "Killed by the kernel for running out of memory (limit or machine)."
    )
    assert _check(result, "unit").status == "fail"
    assert _check(result, "oom").status == "warn"


def test_crash_loop_without_oom_reports_exit_status(
    monkeypatch: pytest.MonkeyPatch, runner: FakeRunner
) -> None:
    _fragment(runner)
    _unit_state(runner, active="activating", sub="auto-restart", exec_status="1", restarts="4")
    _no_findings(runner)

    result = _diagnose(monkeypatch, runner, app=_app())

    assert result.verdict == "down"
    assert result.probable_cause == (
        "The process exits with status 1 at start; see the last journal lines below."
    )


def test_unit_failed_without_crash_loop_reports_result_and_exit_status(
    monkeypatch: pytest.MonkeyPatch, runner: FakeRunner
) -> None:
    _fragment(runner)
    _unit_state(
        runner, active="failed", sub="failed", result="exit-code", exec_status="1", restarts="0"
    )
    _no_findings(runner)

    result = _diagnose(monkeypatch, runner, app=_app())

    assert result.verdict == "down"
    assert result.probable_cause == (
        "The unit failed to start (Result=exit-code, exit status 1); see the last journal lines below."
    )


def test_port_not_listening_names_the_recorded_port(
    monkeypatch: pytest.MonkeyPatch, runner: FakeRunner
) -> None:
    _fragment(runner)
    _unit_state(runner)
    runner.script(["ss", "-ltnpH"], stdout="")
    _main_pid(runner, "100")
    _no_findings(runner)

    result = _diagnose(monkeypatch, runner, app=_app(port=3000))

    assert result.verdict == "down"
    assert result.probable_cause == "The process is running but not listening on port 3000."
    assert _check(result, "port").status == "fail"


def test_port_mismatch_names_both_ports(
    monkeypatch: pytest.MonkeyPatch, runner: FakeRunner
) -> None:
    _fragment(runner)
    _unit_state(runner)
    _listening(runner, 4000, pid="100")
    _main_pid(runner, "100")
    _no_findings(runner)

    result = _diagnose(monkeypatch, runner, app=_app(port=3000))

    assert result.verdict == "down"
    assert result.probable_cause == "Listening on 4000, WASM routes to 3000."


def test_nginx_cannot_reach_a_healthy_app(
    monkeypatch: pytest.MonkeyPatch, runner: FakeRunner
) -> None:
    _fragment(runner)
    _unit_state(runner)
    _listening(runner, 3000)
    _no_findings(runner)

    result = _diagnose(
        monkeypatch,
        runner,
        app=_app(),
        http_get=_http(direct=(200, None), nginx=(502, None)),
    )

    assert result.verdict == "down"
    assert result.probable_cause == (
        "The app answers directly, but nginx returns 502 for example.com; nginx cannot reach the app."
    )
    assert _check(result, "http_direct").status == "ok"
    assert _check(result, "http_nginx").status == "fail"


def test_expired_certificate_is_the_probable_cause(
    monkeypatch: pytest.MonkeyPatch, runner: FakeRunner
) -> None:
    _fragment(runner)
    _unit_state(runner)
    _listening(runner, 3000)
    runner.script(
        ["journalctl", "-u", UNIT_FILE, "-n", "50", "--no-pager", "-o", "short-iso"], stdout=""
    )
    runner.script(
        ["journalctl", "-k", "--since", "-7d", "--grep", "oom", "-o", "short-iso"], stdout=""
    )
    runner.script(["certbot", "certificates"], stdout=_certbot_output("2025-01-01"))

    result = _diagnose(monkeypatch, runner, app=_app())

    assert result.verdict == "down"
    assert result.probable_cause == "The TLS certificate for example.com expired on 2025-01-01."
    assert _check(result, "certificate").status == "fail"


def test_disk_almost_full_is_degraded(monkeypatch: pytest.MonkeyPatch, runner: FakeRunner) -> None:
    _fragment(runner)
    _unit_state(runner)
    _listening(runner, 3000)
    _no_findings(runner)
    runner.script(["certbot", "certificates"], stdout=_certbot_output("2026-06-01"))

    result = _diagnose(monkeypatch, runner, app=_app(), disk_usage=_disk(97.0))

    assert result.verdict == "degraded"
    assert result.probable_cause == "The disk holding the apps directory is 97% full."
    assert _check(result, "disk").status == "fail"


def test_fallback_degraded_when_only_a_warning_check_fires(
    monkeypatch: pytest.MonkeyPatch, runner: FakeRunner, tmp_path: Path
) -> None:
    _fragment(runner)
    _unit_state(runner)
    _listening(runner, 3000)
    runner.script(
        ["journalctl", "-u", UNIT_FILE, "-n", "50", "--no-pager", "-o", "short-iso"], stdout=""
    )
    runner.script(
        ["journalctl", "-k", "--since", "-7d", "--grep", "oom", "-o", "short-iso"], stdout=""
    )
    runner.script(["certbot", "certificates"], stdout=_certbot_output("2026-06-01"))

    log_path = tmp_path / "error.log"
    log_path.write_text(
        f'2026-01-01 upstream timed out while reading response header from upstream, host: "{DOMAIN}"\n'
    )

    result = _diagnose(monkeypatch, runner, app=_app(), nginx_log_path=log_path)

    assert result.verdict == "degraded"
    assert result.probable_cause is None
    assert _check(result, "nginx_log").status == "warn"


# -- Static sites ---------------------------------------------------------


def test_static_site_skips_unit_and_port_and_direct_probe(
    monkeypatch: pytest.MonkeyPatch, runner: FakeRunner
) -> None:
    _no_findings(runner)

    result = _diagnose(monkeypatch, runner, app=_app(is_static=True, port=None))

    assert _check(result, "unit").status == "skip"
    assert _check(result, "port").status == "skip"
    assert _check(result, "http_direct").status == "skip"
    assert _check(result, "journal").status == "skip"


# -- Isolation: a probe raising never hides the others ------------------------


def test_a_raising_probe_becomes_skip_without_affecting_others(
    monkeypatch: pytest.MonkeyPatch, runner: FakeRunner
) -> None:
    _fragment(runner)
    _unit_state(runner)
    _listening(runner, 3000)
    _no_findings(runner)
    runner.script(["certbot", "certificates"], stdout=_certbot_output("2026-06-01"))

    def _raising_disk(_path: str) -> _DiskUsage:
        raise OSError("no such directory")

    def _raising_http(_url: str, _headers: dict) -> tuple[int | None, str | None]:
        raise ValueError("boom")

    result = _diagnose(
        monkeypatch,
        runner,
        app=_app(),
        disk_usage=_raising_disk,
        http_get=_raising_http,
    )

    disk_check = _check(result, "disk")
    assert disk_check.status == "skip"
    assert "no such directory" in disk_check.evidence

    for name in ("http_direct", "http_nginx"):
        check = _check(result, name)
        assert check.status == "skip"
        assert "boom" in check.evidence

    # Everything that did not raise still ran normally.
    assert _check(result, "unit").status == "ok"
    assert _check(result, "port").status == "ok"
    assert _check(result, "certificate").status == "ok"
    assert len(result.checks) == 10


def test_probe_that_raises_wasm_error_becomes_skip(
    monkeypatch: pytest.MonkeyPatch, runner: FakeRunner
) -> None:
    _no_findings(runner)

    # A ServiceManager that cannot resolve the unit at all raises a
    # ValidationError (a WASMError). unit, port and journal all depend on that
    # resolution and must each degrade to a skip independently.
    def _raise(self: ServiceManager, _name: str):
        raise ValidationError("bad name")

    monkeypatch.setattr(ServiceManager, "inspect_unit", _raise)

    result = _diagnose(monkeypatch, runner, app=_app())

    for name in ("unit", "port", "journal"):
        check = _check(result, name)
        assert check.status == "skip"
        assert "bad name" in check.evidence

    # Checks that do not depend on unit resolution still ran.
    assert _check(result, "disk").status == "ok"
    assert _check(result, "last_deployment").status == "skip"


# -- Supporting checks, not just the verdict -----------------------------


def test_last_deployment_failure_is_reported(
    monkeypatch: pytest.MonkeyPatch, runner: FakeRunner
) -> None:
    _fragment(runner)
    _unit_state(runner)
    _listening(runner, 3000)
    _no_findings(runner)
    runner.script(["certbot", "certificates"], stdout=_certbot_output("2026-06-01"))

    result = _diagnose(
        monkeypatch,
        runner,
        app=_app(),
        deployments=[
            DeploymentRecord(domain=DOMAIN, status="failed", error="npm install exited 1")
        ],
    )

    last_deploy = _check(result, "last_deployment")
    assert last_deploy.status == "fail"
    assert "npm install exited 1" in last_deploy.summary
