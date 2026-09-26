# Copyright (c) 2024-2026 Yago Lopez Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
Tests for the data GET /api/apps and GET /api/apps/{domain} carry.

The bug this closes: the endpoint used to derive ``status`` from ``active``
alone, so a unit systemd had given up on, or was restarting every few
seconds, read exactly like a stopped one - the console had no way to tell an
operator "this is broken" from "this was never running". It now goes through
:mod:`wasm.core.app_state`, the one place that decision is made, the same one
``wasm list`` and ``wasm health`` use.

Also covered: the fields the console needs and the API did not carry
(``webhook_enabled``, ``unit``, ``run_as``, ``last_deployment``), and that
listing many applications costs one batch of concurrent systemd queries and
one store query each for services, webhooks and deployment history - not one
of each per application.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from wasm.core.store import App, Service, WASMStore
from wasm.core.utils import domain_to_app_name
from wasm.web.api import apps as apps_api
from wasm.web.api.auth import get_current_session
from wasm.web.api.deps import require_elevated

DOMAIN = "shop.example.com"
OTHER_DOMAIN = "blog.example.com"


class _FakeServiceManager:
    """A systemd that answers whatever the test scripted, and counts asks."""

    def __init__(self, statuses: dict[str, dict[str, Any]]) -> None:
        """
        Args:
            statuses: Unit name to the status mapping to report.
        """
        self.statuses = statuses
        self.asked: list[str] = []

    def app_units(self, app: Any) -> list[str]:
        """
        Args:
            app: The application.

        Returns:
            The unit it is named after, as ServiceManager.app_units names it.
        """
        return [] if app.is_static else [domain_to_app_name(app.domain)]

    def get_status(self, name: str) -> dict[str, Any]:
        """
        Args:
            name: Service name.

        Returns:
            The scripted status, or an absent unit when nothing was scripted.
        """
        self.asked.append(name)
        return self.statuses.get(name, {"exists": False, "active": False})


def _active(**extra: Any) -> dict[str, Any]:
    status = {
        "exists": True,
        "active": True,
        "enabled": True,
        "active_state": "active",
        "sub_state": "running",
        "restarts": "0",
        "pid": "4242",
    }
    status.update(extra)
    return status


@pytest.fixture
def store(tmp_path: Path):
    """
    Yields:
        A store the endpoints under test read, sandboxed per test.
    """
    WASMStore.reset_instance()
    instance = WASMStore(tmp_path / "wasm.db")
    try:
        yield instance
    finally:
        instance.close()
        WASMStore.reset_instance()


@pytest.fixture
def client(store: WASMStore) -> TestClient:
    """
    Returns:
        A client for the applications router, already authenticated.
    """
    app = FastAPI()
    app.include_router(apps_api.router, prefix="/api/apps")
    session = {"sid": "test", "scope": "admin"}
    app.dependency_overrides[get_current_session] = lambda: session
    app.dependency_overrides[require_elevated] = lambda: session
    return TestClient(app, raise_server_exceptions=False)


def seed_app(
    store: WASMStore, domain: str = DOMAIN, *, is_static: bool = False, port: int | None = 3000
) -> App:
    """
    Returns:
        A stored application.
    """
    return store.create_app(
        App(
            domain=domain,
            app_type="nodejs",
            source="https://github.com/you/app",
            branch="main",
            port=port,
            app_path=f"/var/www/apps/{domain_to_app_name(domain)}",
            is_static=is_static,
        )
    )


def seed_service(store: WASMStore, app: App, *, user: str = "www-data") -> Service:
    """
    Returns:
        A stored service record for ``app``.
    """
    return store.create_service(
        Service(
            app_id=app.id,
            name=domain_to_app_name(app.domain),
            unit_file=f"/etc/systemd/system/{domain_to_app_name(app.domain)}.service",
            working_directory=app.app_path,
            command="node server.js",
            user=user,
            group="www-data",
            port=app.port,
        )
    )


def _install_fake_manager(
    monkeypatch: pytest.MonkeyPatch, statuses: dict[str, dict[str, Any]]
) -> _FakeServiceManager:
    """
    Args:
        monkeypatch: Patching helper.
        statuses: Scripted systemd answers, keyed by unit name.

    Returns:
        The fake, so the test can assert on how many times it was asked.
    """
    fake = _FakeServiceManager(statuses)
    monkeypatch.setattr(apps_api, "ServiceManager", lambda *a, **kw: fake)
    return fake


# ---------------------------------------------------------------------------
# Status vocabulary
# ---------------------------------------------------------------------------


def test_a_running_unit_reports_running(client: TestClient, store: WASMStore, monkeypatch) -> None:
    app = seed_app(store)
    seed_service(store, app)
    _install_fake_manager(monkeypatch, {"shop-example-com": _active()})

    body = client.get(f"/api/apps/{DOMAIN}").json()

    assert body["status"] == "running"
    assert body["active"] is True
    assert body["pid"] == 4242


def test_a_failed_unit_no_longer_reads_as_stopped(
    client: TestClient, store: WASMStore, monkeypatch
) -> None:
    """The bug this task closes: a unit systemd gave up on read as "stopped"."""
    app = seed_app(store)
    seed_service(store, app)
    _install_fake_manager(
        monkeypatch,
        {"shop-example-com": _active(active_state="failed", sub_state="failed")},
    )

    body = client.get(f"/api/apps/{DOMAIN}").json()

    assert body["status"] == "failed"


def test_a_crash_loop_reports_restarting(client: TestClient, store: WASMStore, monkeypatch) -> None:
    app = seed_app(store)
    seed_service(store, app)
    _install_fake_manager(
        monkeypatch,
        {
            "shop-example-com": _active(
                active_state="activating", sub_state="auto-restart", restarts="9"
            )
        },
    )

    body = client.get(f"/api/apps/{DOMAIN}").json()

    assert body["status"] == "restarting"


def test_a_unit_up_but_unreachable_reports_no_answer(
    client: TestClient, store: WASMStore, monkeypatch, ports
) -> None:
    app = seed_app(store, port=59123)
    seed_service(store, app)
    _install_fake_manager(monkeypatch, {"shop-example-com": _active()})
    ports.closed.add(59123)

    body = client.get(f"/api/apps/{DOMAIN}").json()

    assert body["status"] == "no_answer"


def test_an_inactive_unit_reports_stopped(
    client: TestClient, store: WASMStore, monkeypatch
) -> None:
    app = seed_app(store)
    seed_service(store, app)
    _install_fake_manager(monkeypatch, {"shop-example-com": {"exists": True, "active": False}})

    body = client.get(f"/api/apps/{DOMAIN}").json()

    assert body["status"] == "stopped"


def test_a_static_site_reports_static_and_is_never_queried(
    client: TestClient, store: WASMStore, monkeypatch
) -> None:
    seed_app(store, is_static=True, port=None)
    fake = _install_fake_manager(monkeypatch, {})

    body = client.get(f"/api/apps/{DOMAIN}").json()

    assert body["status"] == "static"
    assert fake.asked == []


# ---------------------------------------------------------------------------
# webhook_enabled
# ---------------------------------------------------------------------------


def test_webhook_enabled_is_false_with_no_secret(
    client: TestClient, store: WASMStore, monkeypatch
) -> None:
    seed_app(store)
    _install_fake_manager(monkeypatch, {})

    body = client.get(f"/api/apps/{DOMAIN}").json()

    assert body["webhook_enabled"] is False


def test_webhook_enabled_is_true_once_a_secret_is_set_and_never_reveals_it(
    client: TestClient, store: WASMStore, monkeypatch
) -> None:
    seed_app(store)
    store.set_webhook_secret(DOMAIN, "s3cret-value")
    _install_fake_manager(monkeypatch, {})

    response = client.get(f"/api/apps/{DOMAIN}")
    body = response.json()

    assert body["webhook_enabled"] is True
    assert "s3cret-value" not in response.text
    assert "webhook_secret" not in response.text


# ---------------------------------------------------------------------------
# unit / run_as
# ---------------------------------------------------------------------------


def test_unit_and_run_as_come_from_the_service_record(
    client: TestClient, store: WASMStore, monkeypatch
) -> None:
    app = seed_app(store)
    seed_service(store, app, user="shop-runtime")
    _install_fake_manager(monkeypatch, {"shop-example-com": _active()})

    body = client.get(f"/api/apps/{DOMAIN}").json()

    assert body["unit"] == "shop-example-com"
    assert body["run_as"] == "shop-runtime"


def test_unit_and_run_as_are_none_for_a_static_site(
    client: TestClient, store: WASMStore, monkeypatch
) -> None:
    seed_app(store, is_static=True, port=None)
    _install_fake_manager(monkeypatch, {})

    body = client.get(f"/api/apps/{DOMAIN}").json()

    assert body["unit"] is None
    assert body["run_as"] is None


# ---------------------------------------------------------------------------
# last_deployment
# ---------------------------------------------------------------------------


def test_last_deployment_is_none_with_no_history(
    client: TestClient, store: WASMStore, monkeypatch
) -> None:
    seed_app(store)
    _install_fake_manager(monkeypatch, {})

    body = client.get(f"/api/apps/{DOMAIN}").json()

    assert body["last_deployment"] is None


def test_last_deployment_is_the_newest_one_with_an_offset_on_its_timestamp(
    client: TestClient, store: WASMStore, monkeypatch
) -> None:
    seed_app(store)
    _install_fake_manager(monkeypatch, {})
    first = store.record_deployment_start(DOMAIN, "cli", git_commit="aaa0000")
    store.finish_deployment(first, "success")
    second = store.record_deployment_start(DOMAIN, "panel", git_commit="bbb1111")
    store.finish_deployment(second, "failed", error="boom")

    body = client.get(f"/api/apps/{DOMAIN}").json()

    last = body["last_deployment"]
    assert last is not None
    assert last["id"] == second
    assert last["status"] == "failed"
    assert last["git_commit"] == "bbb1111"
    assert last["finished_at"] is not None
    # A naive store timestamp must come back with an explicit offset, not
    # bare, so the browser is not left guessing which zone it is in.
    parsed = datetime.fromisoformat(last["finished_at"])
    assert parsed.tzinfo is not None


# ---------------------------------------------------------------------------
# Listing: batching
# ---------------------------------------------------------------------------


def test_listing_several_apps_queries_systemd_once_per_app_not_more(
    client: TestClient, store: WASMStore, monkeypatch
) -> None:
    a = seed_app(store, DOMAIN)
    b = seed_app(store, OTHER_DOMAIN)
    seed_service(store, a)
    seed_service(store, b)
    fake = _install_fake_manager(
        monkeypatch,
        {
            "shop-example-com": _active(),
            "blog-example-com": _active(),
        },
    )

    response = client.get("/api/apps")

    assert response.status_code == 200
    assert sorted(fake.asked) == ["blog-example-com", "shop-example-com"]


def test_listing_several_apps_costs_one_deployment_query_not_n(
    client: TestClient, store: WASMStore, monkeypatch
) -> None:
    a = seed_app(store, DOMAIN)
    b = seed_app(store, OTHER_DOMAIN)
    seed_service(store, a)
    seed_service(store, b)
    _install_fake_manager(
        monkeypatch, {"shop-example-com": _active(), "blog-example-com": _active()}
    )
    store.finish_deployment(store.record_deployment_start(DOMAIN, "cli"), "success")

    calls: list[Any] = []
    original = WASMStore.get_latest_deployments

    def counting(self: WASMStore, domains: Any) -> Any:
        calls.append(list(domains))
        return original(self, domains)

    monkeypatch.setattr(WASMStore, "get_latest_deployments", counting)

    body = client.get("/api/apps").json()

    assert len(calls) == 1
    by_domain = {item["domain"]: item for item in body["apps"]}
    assert by_domain[DOMAIN]["last_deployment"] is not None
    assert by_domain[OTHER_DOMAIN]["last_deployment"] is None


def test_listing_several_apps_costs_one_webhook_query_not_n(
    client: TestClient, store: WASMStore, monkeypatch
) -> None:
    seed_app(store, DOMAIN)
    seed_app(store, OTHER_DOMAIN)
    store.set_webhook_secret(DOMAIN, "s3cret-value")
    _install_fake_manager(monkeypatch, {})

    calls: list[Any] = []
    original = WASMStore.list_webhook_flags

    def counting(self: WASMStore, domains: Any = None) -> Any:
        calls.append(domains)
        return original(self, domains)

    monkeypatch.setattr(WASMStore, "list_webhook_flags", counting)

    body = client.get("/api/apps").json()

    assert len(calls) == 1
    by_domain = {item["domain"]: item for item in body["apps"]}
    assert by_domain[DOMAIN]["webhook_enabled"] is True
    assert by_domain[OTHER_DOMAIN]["webhook_enabled"] is False


def test_a_missing_app_is_404(client: TestClient) -> None:
    response = client.get("/api/apps/nothing.example.com")

    assert response.status_code == 404


# ---------------------------------------------------------------------------
# source, branch, keep_releases, build_command and start_command
# ---------------------------------------------------------------------------


def test_the_list_carries_source_branch_and_keep_releases_for_free(
    client: TestClient, store: WASMStore, monkeypatch
) -> None:
    """These come straight off the stored row: no extra query, no N+1."""
    seed_app(store)
    _install_fake_manager(monkeypatch, {})

    body = client.get("/api/apps").json()

    entry = body["apps"][0]
    assert entry["source"] == "https://github.com/you/app"
    assert entry["branch"] == "main"
    assert entry["keep_releases"] == 5


def test_the_list_reports_the_deployed_start_command_from_the_service_row(
    client: TestClient, store: WASMStore, monkeypatch
) -> None:
    """start_command is what the deploy recorded on the unit, not recomputed."""
    app = seed_app(store)
    seed_service(store, app)
    _install_fake_manager(monkeypatch, {domain_to_app_name(DOMAIN): _active()})

    body = client.get("/api/apps").json()

    assert body["apps"][0]["start_command"] == "node server.js"


def test_a_static_app_has_no_start_command(
    client: TestClient, store: WASMStore, monkeypatch
) -> None:
    seed_app(store, is_static=True, port=None)
    _install_fake_manager(monkeypatch, {})

    body = client.get("/api/apps").json()

    assert body["apps"][0]["start_command"] is None


def test_the_list_never_computes_a_build_command_per_app(
    client: TestClient, store: WASMStore, monkeypatch
) -> None:
    """Listing is cheap: build_command is always empty here, never instantiates a deployer."""
    seed_app(store, DOMAIN)
    seed_app(store, OTHER_DOMAIN)
    _install_fake_manager(monkeypatch, {})

    def _must_not_be_called(app: App) -> list[str]:
        raise AssertionError("the list endpoint must not compute a build command")

    monkeypatch.setattr(apps_api, "_deployer_build_command", _must_not_be_called)

    response = client.get("/api/apps")

    assert response.status_code == 200, response.text
    body = response.json()
    assert all(item["build_command"] == [] for item in body["apps"])


def test_the_detail_endpoint_computes_the_build_command(
    client: TestClient, store: WASMStore, monkeypatch
) -> None:
    """Fetching one application may afford what the list must not."""
    seed_app(store)
    _install_fake_manager(monkeypatch, {})
    monkeypatch.setattr(apps_api, "_deployer_build_command", lambda app: ["npm", "run", "build"])

    body = client.get(f"/api/apps/{DOMAIN}").json()

    assert body["build_command"] == ["npm", "run", "build"]


# ---------------------------------------------------------------------------
# GET /api/apps/types
# ---------------------------------------------------------------------------


def test_app_types_lists_the_registry_not_a_hardcoded_copy(client: TestClient) -> None:
    """The wizard's type list comes from DeployerRegistry, the CLI's own source."""
    response = client.get("/api/apps/types")

    assert response.status_code == 200, response.text
    body = response.json()
    types = {entry["type"]: entry for entry in body["types"]}
    assert "nodejs" in types
    assert types["nodejs"]["name"] == "Node.js"
    assert types["nodejs"]["default_port"] == 3000
    assert "static" in types
    assert "auto" in types


# ---------------------------------------------------------------------------
# POST /api/apps/inspect: a bad source is the operator's mistake, not a crash
# ---------------------------------------------------------------------------


def test_an_unreachable_source_answers_400_not_500(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A repository WASM cannot fetch is a validation problem, not a server fault."""
    from wasm.core.exceptions import SourceError

    def broken(source: str, *, branch: str | None = None) -> Any:
        raise SourceError(
            "Could not reach the repository", details="Check the URL and your network"
        )

    monkeypatch.setattr(apps_api, "inspect_source", broken)

    response = client.post("/api/apps/inspect", json={"source": "https://example.com/nope.git"})

    assert response.status_code == 400, response.text
    body = response.json()
    assert body["error"] == "sourceerror"
    assert "reach the repository" in body["detail"]
    assert body["hint"] == "Check the URL and your network"


def test_a_source_matching_no_app_type_answers_400_not_500(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    Nothing recognising the checkout is a validation problem too.

    ``inspect_source`` raises ``DeploymentError`` for this - right when a real
    deploy fails partway through, wrong for a wizard preview that never wrote
    anything - so the endpoint must translate it into the same 400 contract
    as any other bad input, with the manager's own details kept as the hint.
    """
    from wasm.core.exceptions import DeploymentError

    def unmatched(source: str, *, branch: str | None = None) -> Any:
        raise DeploymentError(
            f"Could not identify the application type at {source}",
            details="Choose a type explicitly instead of relying on auto-detection.",
        )

    monkeypatch.setattr(apps_api, "inspect_source", unmatched)

    response = client.post("/api/apps/inspect", json={"source": "https://example.com/mystery.git"})

    assert response.status_code == 400, response.text
    body = response.json()
    assert body["error"] == "validationerror"
    assert "Could not identify" in body["detail"]
    assert "Choose a type explicitly" in body["hint"]
