# Copyright (c) 2024-2026 Yago Lopez Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
Tests for ``/api/apps/{domain}/domains``.

The router is a translation of :mod:`wasm.deployers.domains` to HTTP - the
rules are pinned in ``tests/test_domains.py`` and ``tests/test_store.py`` and
not repeated here. What this module owns: which function each route calls and
with what, the response shapes, the status of each refusal, that certbot is
queued as a job instead of running on the request path, and that removing a
domain needs sudo mode.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from wasm.core.exceptions import DomainConflictError, DomainError, ValidationError
from wasm.core.store import App, DomainRecord, WASMStore
from wasm.deployers.domains import DnsCheck, DomainChange
from wasm.web.api import domains as domains_api
from wasm.web.api.deps import install_error_handlers
from wasm.web.auth import require_auth

PRIMARY = DomainRecord(id=1, app_id=1, domain="example.com", kind="primary", created_at="t0")
ALIAS = DomainRecord(id=2, app_id=1, domain="shop.example.com", kind="alias", created_at="t1")


@pytest.fixture
def store(tmp_path: Any) -> Any:
    """A store with example.com deployed."""
    WASMStore.reset_instance()
    instance = WASMStore(tmp_path / "wasm.db")
    instance.create_app(App(domain="example.com", app_path="/var/www/apps/example.com"))
    yield instance
    WASMStore.reset_instance()


def build(session: dict[str, Any]) -> TestClient:
    """
    A client for the domains router alone, with a fixed session.

    Args:
        session: What ``require_auth`` resolves every request to.

    Returns:
        The client.
    """
    app = FastAPI()
    install_error_handlers(app)
    app.include_router(domains_api.router, prefix="/api/apps")
    app.dependency_overrides[require_auth] = lambda: session
    return TestClient(app)


@pytest.fixture
def client(store: Any) -> TestClient:
    """A client presenting an admin Bearer credential, which sudo mode does not ask."""
    return build({"sid": "token", "scope": "admin", "source": "bearer", "type": "api_token"})


class Calls:
    """Records what the stubbed operations were called with."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    def stub(self, name: str, result: Any = None, error: Exception | None = None) -> Any:
        """Build a stand-in for one operation."""

        def fake(*args: Any, **kwargs: Any) -> Any:
            self.calls.append((name, args, kwargs))
            if error is not None:
                raise error
            return result

        return fake


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> Calls:
    """Stub every operation and the job manager, recording the calls."""
    recorder = Calls()
    change = DomainChange(app="example.com", domains=(PRIMARY, ALIAS), tls=False)
    monkeypatch.setattr(domains_api, "list_domains", recorder.stub("list", [PRIMARY, ALIAS]))
    monkeypatch.setattr(domains_api, "add_domain", recorder.stub("add", change))
    monkeypatch.setattr(domains_api, "remove_domain", recorder.stub("remove", change))
    return recorder


class FakeJobs:
    """Stands in for the job manager."""

    def __init__(self) -> None:
        self.created: list[dict[str, Any]] = []

    def create_job(self, **kwargs: Any) -> Any:
        self.created.append(kwargs)
        return type("Job", (), {"id": "job-1"})()


@pytest.fixture
def jobs(monkeypatch: pytest.MonkeyPatch) -> FakeJobs:
    fake = FakeJobs()
    monkeypatch.setattr(domains_api, "get_job_manager", lambda: fake)
    return fake


def test_listing_returns_every_domain_primary_first(client: TestClient, calls: Calls) -> None:
    response = client.get("/api/apps/example.com/domains")

    assert response.status_code == 200, response.text
    assert response.json() == {
        "app": "example.com",
        "domains": [
            {"domain": "example.com", "kind": "primary", "created_at": "t0"},
            {"domain": "shop.example.com", "kind": "alias", "created_at": "t1"},
        ],
    }
    assert calls.calls == [("list", ("example.com",), {})]


def test_an_unknown_application_is_a_404(client: TestClient, calls: Calls) -> None:
    for response in (
        client.get("/api/apps/ghost.example.com/domains"),
        client.post("/api/apps/ghost.example.com/domains", json={"domain": "a.example.com"}),
        client.delete("/api/apps/ghost.example.com/domains/a.example.com"),
        client.get("/api/apps/ghost.example.com/domains/a.example.com/dns"),
    ):
        assert response.status_code == 404, response.text
        assert response.json()["error"] == "not_found"
    assert calls.calls == []


def test_adding_never_runs_certbot_on_the_request_path(
    client: TestClient, calls: Calls, jobs: FakeJobs
) -> None:
    response = client.post(
        "/api/apps/example.com/domains", json={"domain": "shop.example.com", "kind": "redirect"}
    )

    assert response.status_code == 201, response.text
    assert calls.calls == [
        ("add", ("example.com", "shop.example.com", "redirect"), {"issue_cert": False})
    ]
    assert response.json()["certificate_job_id"] is None
    assert jobs.created == []


def test_adding_to_a_tls_site_queues_the_certificate(
    client: TestClient, calls: Calls, jobs: FakeJobs, monkeypatch: pytest.MonkeyPatch
) -> None:
    change = DomainChange(
        app="example.com", domains=(PRIMARY, ALIAS), tls=True, adopted=("www.example.com",)
    )
    monkeypatch.setattr(domains_api, "add_domain", calls.stub("add", change))

    response = client.post("/api/apps/example.com/domains", json={"domain": "shop.example.com"})

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["certificate_job_id"] == "job-1"
    assert body["tls"] is True
    assert body["adopted"] == ["www.example.com"]
    assert calls.calls[0][1] == ("example.com", "shop.example.com", "alias")
    (job,) = jobs.created
    assert job["func"] is domains_api.certificate_job
    assert job["kwargs"] == {"app_domain": "example.com"}


def test_the_certificate_job_covers_every_domain(monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = Calls()
    change = DomainChange(app="example.com", domains=(PRIMARY, ALIAS), tls=True)
    monkeypatch.setattr(domains_api, "issue_certificate", recorder.stub("issue", change))

    result = domains_api.certificate_job("example.com")

    assert recorder.calls == [("issue", ("example.com",), {})]
    assert result == {"domain": "example.com", "domains": ["example.com", "shop.example.com"]}


@pytest.mark.parametrize(
    ("error", "status"),
    [
        (DomainConflictError("shop.example.com is already an alias of other.com"), 409),
        (DomainError("Invalid domain"), 400),
        (ValidationError("Unknown domain kind 'mirror'"), 400),
        (ValidationError("nginx rejected the new configuration", details="[emerg] x"), 400),
    ],
)
def test_refusals_keep_their_status_and_the_servers_words(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, error: Exception, status: int
) -> None:
    monkeypatch.setattr(domains_api, "add_domain", Calls().stub("add", error=error))

    response = client.post("/api/apps/example.com/domains", json={"domain": "shop.example.com"})

    assert response.status_code == status, response.text
    assert response.json()["detail"] == error.message  # type: ignore[attr-defined]
    assert response.json()["hint"] == (error.details or None)  # type: ignore[attr-defined]


@pytest.mark.parametrize("hostile", ["shop.example.com/../x", "https://shop.example.com"])
def test_a_domain_that_only_becomes_one_after_stripping_is_refused(
    client: TestClient, calls: Calls, hostile: str
) -> None:
    response = client.post("/api/apps/example.com/domains", json={"domain": hostile})

    assert response.status_code == 400, response.text
    assert calls.calls == []


def test_removing_calls_the_operation(client: TestClient, calls: Calls) -> None:
    response = client.delete("/api/apps/example.com/domains/shop.example.com")

    assert response.status_code == 200, response.text
    assert calls.calls == [("remove", ("example.com", "shop.example.com"), {})]
    assert response.json()["domains"][0]["domain"] == "example.com"


def test_removing_from_a_cookie_session_needs_sudo_mode(store: Any, calls: Calls) -> None:
    client = build({"sid": "browser", "scope": "admin", "source": "cookie", "type": "session"})

    response = client.delete("/api/apps/example.com/domains/shop.example.com")

    assert response.status_code == 403, response.text
    assert response.json()["error"] == "elevation_required"
    assert calls.calls == []


def test_removing_from_an_elevated_cookie_session_is_allowed(store: Any, calls: Calls) -> None:
    client = build(
        {
            "sid": "browser",
            "scope": "admin",
            "source": "cookie",
            "type": "session",
            "elevated_until": 4e9,
        }
    )

    response = client.delete("/api/apps/example.com/domains/shop.example.com")

    assert response.status_code == 200, response.text


def test_the_dns_check_is_served_as_the_operation_answers_it(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder = Calls()
    check = DnsCheck(
        domain="shop.example.com",
        expected_addresses=("203.0.113.5",),
        resolved_addresses=("198.51.100.7",),
        points_here=False,
    )
    monkeypatch.setattr(domains_api, "check_dns", recorder.stub("dns", check))

    response = client.get("/api/apps/example.com/domains/shop.example.com/dns")

    assert response.status_code == 200, response.text
    assert recorder.calls == [("dns", ("shop.example.com",), {})]
    assert response.json() == {
        "domain": "shop.example.com",
        "expected_addresses": ["203.0.113.5"],
        "resolved_addresses": ["198.51.100.7"],
        "points_here": False,
    }


def build_bare(session: dict[str, Any]) -> TestClient:
    """
    A client for the app-less DNS check alone, with a fixed session.

    Args:
        session: What ``require_auth`` resolves every request to.

    Returns:
        The client.
    """
    app = FastAPI()
    install_error_handlers(app)
    app.include_router(domains_api.dns_router, prefix="/api/domains")
    app.dependency_overrides[require_auth] = lambda: session
    return TestClient(app)


def test_the_bare_dns_check_needs_no_application(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The wizard can check a domain before anything is deployed at it."""
    recorder = Calls()
    check = DnsCheck(
        domain="new-app.example.com",
        expected_addresses=("203.0.113.5",),
        resolved_addresses=("203.0.113.5",),
        points_here=True,
    )
    monkeypatch.setattr(domains_api, "check_dns", recorder.stub("dns", check))
    client = build_bare({"sid": "token", "scope": "admin", "source": "bearer", "type": "api_token"})

    response = client.get("/api/domains/dns", params={"name": "new-app.example.com"})

    assert response.status_code == 200, response.text
    assert recorder.calls == [("dns", ("new-app.example.com",), {})]
    assert response.json() == {
        "domain": "new-app.example.com",
        "expected_addresses": ["203.0.113.5"],
        "resolved_addresses": ["203.0.113.5"],
        "points_here": True,
    }


def test_the_bare_dns_check_uses_the_same_operation_as_the_app_scoped_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One implementation of "resolve a domain", asked with or without an app."""
    calls: list[str] = []

    def fake_check_dns(name: str) -> DnsCheck:
        calls.append(name)
        return DnsCheck(
            domain=name, expected_addresses=(), resolved_addresses=(), points_here=False
        )

    monkeypatch.setattr(domains_api, "check_dns", fake_check_dns)
    client = build_bare({"sid": "token", "scope": "admin", "source": "bearer", "type": "api_token"})

    client.get("/api/domains/dns", params={"name": "a.example.com"})

    assert calls == ["a.example.com"]
