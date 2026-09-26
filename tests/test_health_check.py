# Copyright (c) 2024-2026 Yago Lopez Prado
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Tests for the configurable health check.

An application may say where the health gate probes it, which statuses mean
it is up and how long it gets to come up. Pinned here:

- the settings are validated once, where they are stored, and a bad value
  never reaches the row;
- the gate honours each setting, whether a deploy, an activation or a limits
  restart builds it, and falls back to exactly the 2.0 rule without them;
- the diagnosis probes the same path with the same expectation.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from wasm.core.exceptions import DeploymentError, ValidationError, WASMError
from wasm.core.logger import Logger
from wasm.core.runner import FakeRunner
from wasm.core.store import App, WASMStore
from wasm.deployers import lifecycle
from wasm.deployers.helpers import health as health_module
from wasm.deployers.helpers.health_gate import (
    DEFAULT_HEALTH_TIMEOUT,
    HEALTH_GATE_ATTEMPTS,
    HEALTH_GATE_DELAY,
    HealthCheck,
    HealthGate,
)
from wasm.validators.health import (
    check_health_expect,
    check_health_path,
    check_health_timeout,
)

DOMAIN = "shop.example.com"


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """A store of the test's own, where the lifecycle looks for it."""
    WASMStore.reset_instance()
    instance = WASMStore(tmp_path / "wasm.db")
    monkeypatch.setattr(lifecycle, "get_store", lambda: instance)
    yield instance
    WASMStore.reset_instance()


def _register(store: WASMStore, **fields: Any) -> App:
    """Register the test application."""
    values: dict[str, Any] = {
        "domain": DOMAIN,
        "app_type": "nodejs",
        "port": 3100,
        "app_path": "/srv/shop",
    }
    values.update(fields)
    return store.create_app(App(**values))


class FakeUnits:
    """The part of the service manager the gate uses, recorded."""

    def __init__(self) -> None:
        self.restarts: list[str] = []

    def restart(self, name: str) -> None:
        self.restarts.append(name)

    def logs(self, name: str, lines: int = 50) -> str:
        return ""


class RecordingProbe:
    """Stands in for wait_until_healthy and keeps what it was asked."""

    def __init__(self, result: bool = True) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.result = result

    def __call__(self, url: str, **kwargs: Any) -> bool:
        self.calls.append((url, kwargs))
        return self.result


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["/", "/healthz", "/api/health?deep=1", "/_status/ready"])
def test_a_path_on_the_application_is_accepted(path: str) -> None:
    assert check_health_path(path) == path


@pytest.mark.parametrize(
    "path",
    [
        "",
        "healthz",
        "http://evil.example.com/",
        "//evil.example.com/healthz",
        "/health z",
        "/health\n",
        "/health\x00",
        "/salud/ñ",
        "/" + "a" * 2048,
    ],
)
def test_a_path_that_is_not_a_path_on_the_application_is_refused(path: str) -> None:
    """A scheme, a host, spaces or control characters would probe something else, or nothing."""
    with pytest.raises(ValidationError):
        check_health_path(path)


@pytest.mark.parametrize(
    ("expect", "normalised"),
    [
        ("200", "200"),
        ("200-399", "200-399"),
        ("200, 204", "200,204"),
        (" 200-299 ,301,302 ", "200-299,301,302"),
        ("100-599", "100-599"),
    ],
)
def test_an_expectation_is_a_list_of_statuses_and_ranges(expect: str, normalised: str) -> None:
    assert check_health_expect(expect) == normalised


@pytest.mark.parametrize(
    "expect",
    ["", "ok", "2xx", "99", "600", "200-", "-200", "399-200", "200,,204", "200-300-400", "2e2"],
)
def test_an_expectation_outside_100_599_or_malformed_is_refused(expect: str) -> None:
    with pytest.raises(ValidationError):
        check_health_expect(expect)


@pytest.mark.parametrize("timeout", [5, 30, 600])
def test_a_timeout_from_5_to_600_seconds_is_accepted(timeout: int) -> None:
    assert check_health_timeout(timeout) == timeout


@pytest.mark.parametrize("timeout", [0, 4, 601, -1, True])
def test_a_timeout_outside_5_to_600_seconds_is_refused(timeout: Any) -> None:
    with pytest.raises(ValidationError, match="5"):
        check_health_timeout(timeout)


def test_the_store_refuses_a_bad_setting_and_writes_nothing(store: WASMStore) -> None:
    """The store is the chokepoint: no caller can put an unusable value in the row."""
    _register(store)

    for bad in (
        {"path": "http://x/", "expect": None, "timeout": None},
        {"path": None, "expect": "700", "timeout": None},
        {"path": None, "expect": None, "timeout": 1},
    ):
        with pytest.raises(ValidationError):
            store.set_app_health(DOMAIN, **bad)

    app = store.get_app(DOMAIN)
    assert (app.health_path, app.health_expect, app.health_timeout) == (None, None, None)


def test_the_store_keeps_the_normalised_setting(store: WASMStore) -> None:
    _register(store)

    assert store.set_app_health(DOMAIN, path="/healthz", expect="200 , 204", timeout=60)
    app = store.get_app(DOMAIN)
    assert (app.health_path, app.health_expect, app.health_timeout) == ("/healthz", "200,204", 60)
    assert not store.set_app_health("other.example.com", path=None, expect=None, timeout=None)


def test_a_redeploy_keeps_the_health_settings(store: WASMStore) -> None:
    """A deploy rewrites the whole row; the operator's settings are not its to drop."""
    from wasm.deployers.helpers.registration import StoreRegistrar

    _register(store)
    store.set_app_health(DOMAIN, path="/healthz", expect="200", timeout=45)

    StoreRegistrar(store).register_app(
        domain=DOMAIN,
        app_type="nodejs",
        source="https://example.com/shop.git",
        branch="main",
        port=3100,
        app_path=Path("/srv/shop"),
        webserver="nginx",
        ssl_enabled=True,
        status="running",
        is_static=False,
        env_vars={},
    )

    app = store.get_app(DOMAIN)
    assert (app.health_path, app.health_expect, app.health_timeout) == ("/healthz", "200", 45)


# ---------------------------------------------------------------------------
# What the settings mean
# ---------------------------------------------------------------------------


def test_without_settings_the_check_is_exactly_the_2_0_rule() -> None:
    check = HealthCheck.for_app(App(domain=DOMAIN, port=3100))

    assert check.url(3100) == "http://127.0.0.1:3100/"
    assert check.accepts(404) and check.accepts(302) and not check.accepts(500)
    assert check.seconds == DEFAULT_HEALTH_TIMEOUT
    assert check.attempts(HEALTH_GATE_DELAY) == HEALTH_GATE_ATTEMPTS


def test_the_settings_of_the_application_are_the_check() -> None:
    app = App(
        domain=DOMAIN, port=3100, health_path="/healthz", health_expect="200,204", health_timeout=90
    )
    check = HealthCheck.for_app(app)

    assert check.url(3100) == "http://127.0.0.1:3100/healthz"
    assert check.accepts(204) and check.accepts(200)
    assert not check.accepts(302) and not check.accepts(404)
    assert check.attempts(HEALTH_GATE_DELAY) == 45


def test_a_deployer_path_is_the_default_the_application_can_override() -> None:
    assert HealthCheck.for_app(None, default_path="/ready").path == "/ready"
    app = App(domain=DOMAIN, health_path="/healthz")
    assert HealthCheck.for_app(app, default_path="/ready").path == "/healthz"


# ---------------------------------------------------------------------------
# The gate honours each setting
# ---------------------------------------------------------------------------


def _gate(check: HealthCheck | None, probe: RecordingProbe) -> HealthGate:
    return HealthGate(
        unit="shop-example-com",
        url=(check or HealthCheck()).url(3100),
        services=FakeUnits(),
        logger=Logger(),
        probe=probe,
        check=check,
    )


def test_the_gate_probes_the_path_and_judges_by_the_expectation() -> None:
    probe = RecordingProbe()
    check = HealthCheck(path="/healthz", expect="204", timeout=None)

    healthy, _ = _gate(check, probe).restart_and_probe()

    assert healthy
    url, kwargs = probe.calls[0]
    assert url == "http://127.0.0.1:3100/healthz"
    assert kwargs["accept"](204) and not kwargs["accept"](200)


def test_the_gate_waits_as_long_as_the_timeout_says() -> None:
    probe = RecordingProbe()

    _gate(HealthCheck(timeout=120), probe).restart_and_probe()
    _gate(HealthCheck(timeout=5), probe).restart_and_probe()
    _gate(None, probe).restart_and_probe()

    assert [kwargs["retries"] for _, kwargs in probe.calls] == [60, 3, HEALTH_GATE_ATTEMPTS]
    assert all(kwargs["delay"] == HEALTH_GATE_DELAY for _, kwargs in probe.calls)


def test_without_settings_the_gate_accepts_anything_below_500() -> None:
    probe = RecordingProbe()

    _gate(None, probe).restart_and_probe()

    accept = probe.calls[0][1]["accept"]
    assert accept(401) and accept(301) and not accept(502)


def test_an_expectation_can_refuse_a_200(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 200 was always healthy before the expectation was asked; now the expectation decides."""

    class Response:
        status = 200

        def __enter__(self) -> Response:
            return self

        def __exit__(self, *args: Any) -> None:
            return None

    class Opener:
        def open(self, url: str, timeout: float) -> Response:
            return Response()

    monkeypatch.setattr(health_module.urllib.request, "build_opener", lambda *a: Opener())
    monkeypatch.setattr(health_module.time, "sleep", lambda s: None)

    assert not health_module.wait_until_healthy(
        "http://127.0.0.1:1/", retries=2, delay=0, accept=lambda status: status == 204
    )
    assert health_module.wait_until_healthy("http://127.0.0.1:1/", retries=1, delay=0)


def test_an_activation_gate_is_built_from_the_row(
    store: WASMStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rollbacks, migrations and limits restarts go through health_gate_for: same settings."""
    app = _register(store, health_path="/healthz", health_expect="200-299", health_timeout=10)
    probe = RecordingProbe()
    monkeypatch.setattr(lifecycle, "wait_until_healthy", probe)
    monkeypatch.setattr(lifecycle, "ServiceManager", lambda **kw: FakeUnits())

    healthy, _ = lifecycle.health_gate_for(app, store, Logger()).restart_and_probe()

    assert healthy
    url, kwargs = probe.calls[0]
    assert url == "http://127.0.0.1:3100/healthz"
    assert kwargs["retries"] == 5
    assert kwargs["accept"](204) and not kwargs["accept"](302)


def test_a_deploy_gate_is_built_from_the_row(store: WASMStore, runner: FakeRunner) -> None:
    """The deployer's own gate reads the same settings as an activation's."""
    from wasm.deployers.nodejs import NodeJSDeployer

    deployer = NodeJSDeployer()
    deployer.domain = DOMAIN
    deployer.app_name = "shop-example-com"
    deployer.port = 3100
    deployer._app_record = App(
        domain=DOMAIN, port=3100, health_path="/up", health_expect="200", health_timeout=20
    )

    gate = deployer._health_gate()

    assert gate.url == "http://127.0.0.1:3100/up"
    assert gate.check is not None
    assert gate.check.attempts(HEALTH_GATE_DELAY) == 10
    assert not gate.check.accepts(301)


# ---------------------------------------------------------------------------
# Changing the settings
# ---------------------------------------------------------------------------


def test_set_health_check_records_the_settings(store: WASMStore) -> None:
    _register(store)

    app = lifecycle.set_health_check(DOMAIN, path="/healthz", expect="200-399", timeout=120)

    assert (app.health_path, app.health_expect, app.health_timeout) == ("/healthz", "200-399", 120)
    reset = lifecycle.set_health_check(DOMAIN, path=None, expect=None, timeout=None)
    assert (reset.health_path, reset.health_expect, reset.health_timeout) == (None, None, None)


def test_set_health_check_refuses_an_unknown_app_and_a_static_site(store: WASMStore) -> None:
    with pytest.raises(WASMError, match="not found"):
        lifecycle.set_health_check(DOMAIN, path="/healthz", expect=None, timeout=None)

    _register(store, app_type="static", is_static=True, port=None)
    with pytest.raises(DeploymentError, match="static"):
        lifecycle.set_health_check(DOMAIN, path="/healthz", expect=None, timeout=None)


# ---------------------------------------------------------------------------
# The diagnosis probes what the gate probes
# ---------------------------------------------------------------------------


def test_the_diagnosis_probes_the_configured_path_with_the_configured_expectation(
    monkeypatch: pytest.MonkeyPatch, runner: FakeRunner
) -> None:
    """A 200 from an application that promised 204 is what the gate would refuse too."""
    from tests import test_diagnose as d

    d._fragment(runner)
    d._unit_state(runner)
    d._listening(runner, 3000)
    d._no_findings(runner)
    asked: list[str] = []

    def http_get(url: str, headers: dict) -> tuple[int | None, str | None]:
        asked.append(url)
        return 200, None

    result = d._diagnose(
        monkeypatch,
        runner,
        app=d._app(health_path="/healthz", health_expect="204"),
        http_get=http_get,
    )

    assert asked == ["http://127.0.0.1:3000/healthz", "http://127.0.0.1:80/healthz"]
    direct = d._check(result, "http_direct")
    assert direct.status == "fail"
    assert "204" in direct.evidence
    assert d._check(result, "http_nginx").status == "fail"


def test_the_diagnosis_lets_nginx_redirect_to_https(
    monkeypatch: pytest.MonkeyPatch, runner: FakeRunner
) -> None:
    """Port 80 answering 301 to HTTPS is the site working, whatever the app promised."""
    from tests import test_diagnose as d

    d._fragment(runner)
    d._unit_state(runner)
    d._listening(runner, 3000)
    d._no_findings(runner)

    result = d._diagnose(
        monkeypatch,
        runner,
        app=d._app(health_path="/healthz", health_expect="200"),
        http_get=d._http(direct=(200, None), nginx=(301, None)),
    )

    assert d._check(result, "http_direct").status == "ok"
    assert d._check(result, "http_nginx").status == "ok"
