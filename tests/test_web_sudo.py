"""
Tests for sudo mode: D5's elevation window over destructive API actions.

A cookie session is a browser tab left open on someone's desk; the master
token or an admin API token is a script someone chose to hand root to on
purpose. The first must re-confirm before deleting anything, revealing a
secret or rewriting configuration; the second is already an explicit,
narrow-purpose credential and is exempt. These tests attack that boundary
the way tests/test_web_auth.py attacks authentication itself: try the
destructive action fresh, try it elevated, try it after the window has
closed, and check that a wrong factor is not a free unlimited-attempts
guessing oracle.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from wasm.core.store import App, WASMStore
from wasm.web import auth as auth_module
from wasm.web.auth import CSRF_HEADER_NAME, SecurityConfig
from wasm.web.server import create_app, get_brute_force, get_token_manager


@pytest.fixture
def store(tmp_path: Path) -> Any:
    """
    Give the panel a store of its own.

    Args:
        tmp_path: Per-test temporary directory.

    Yields:
        The store the API reads and writes.
    """
    WASMStore.reset_instance()
    instance = WASMStore(tmp_path / "wasm.db")
    try:
        yield instance
    finally:
        instance.close()
        WASMStore.reset_instance()


@pytest.fixture
def app(tmp_path: Path, store: Any) -> FastAPI:
    """
    Args:
        tmp_path: Per-test temporary directory.
        store: The store fixture, so the application and the test share one.

    Returns:
        The configured application.
    """
    return create_app(SecurityConfig(state_dir=tmp_path / "state", rate_limit_requests=5000))


@pytest.fixture
def master_token(app: FastAPI) -> str:
    """
    Returns:
        A freshly generated master token for the application under test.
    """
    return get_token_manager().generate_master_token()


@pytest.fixture
def client(app: FastAPI, master_token: str) -> TestClient:
    """
    A cookie session signed in with the master token, not yet elevated.

    Args:
        app: The application.
        master_token: The credential to log in with.

    Returns:
        A client carrying a session cookie and the matching CSRF header.
    """
    signed_in = TestClient(app, client=("testclient", 50000))
    response = signed_in.post("/api/auth/login", json={"token": master_token})
    assert response.status_code == 200, response.text
    signed_in.headers[CSRF_HEADER_NAME] = response.json()["csrf_token"]
    return signed_in


@pytest.fixture
def anon_client(app: FastAPI) -> TestClient:
    """
    Returns:
        A client that has never authenticated.
    """
    return TestClient(app, client=("testclient", 50000))


@pytest.fixture
def seeded_app(store: Any) -> None:
    """Populate the store with two applications, so DELETE has something real to do."""
    for domain in ("example.com", "other.com"):
        store.create_app(
            App(
                domain=domain,
                app_type="static",
                app_path=f"/var/www/apps/{domain}",
                status="stopped",
            )
        )


@pytest.fixture
def queued(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """
    Capture app deletions instead of letting a real job run.

    Args:
        monkeypatch: Patching helper, scoped to the test.

    Returns:
        The keyword arguments of every job the endpoint tried to queue - empty
        when a request never got past the elevation gate.
    """
    captured: list[dict[str, Any]] = []

    def create_job(**kwargs: Any) -> Any:
        captured.append(kwargs)

        class Queued:
            """A job that was accepted but never run."""

            id = "job-1"
            status = type("Status", (), {"value": "pending"})()

            def to_dict(self) -> dict[str, Any]:
                """
                Returns:
                    The job as the API's response model expects it.
                """
                return {"id": self.id}

        return Queued()

    manager = type("FakeJobs", (), {"create_job": staticmethod(create_job)})()
    monkeypatch.setattr("wasm.web.api.apps.get_job_manager", lambda: manager)
    return captured


class FakeClock:
    """A monotonic-looking clock a test can push forward on demand."""

    def __init__(self) -> None:
        self._now = time.time()

    def __call__(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        """
        Args:
            seconds: How far to move the clock forward.
        """
        self._now += seconds


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> FakeClock:
    """
    Replace the elevation window's time source with a fake, controllable one.

    Only ``wasm.web.auth._now`` is patched, so session expiry - computed from
    the real wall clock elsewhere in the module - is untouched: advancing this
    clock past the ten minute elevation window must not also expire the
    session itself.

    Args:
        monkeypatch: Patching helper, scoped to the test.

    Returns:
        The fake clock, for the test to advance.
    """
    fake = FakeClock()
    monkeypatch.setattr(auth_module, "_now", fake)
    return fake


def elevate(client: TestClient, **body: Any) -> dict[str, Any]:
    """
    Call ``POST /api/auth/elevate`` and return its body.

    Args:
        client: A signed-in client.
        **body: ``token`` or ``code``.

    Returns:
        The decoded JSON body.
    """
    response = client.post("/api/auth/elevate", json=body)
    assert response.status_code == 200, response.text
    return response.json()


# --------------------------------------------------------------------------
# GET /api/auth/session
# --------------------------------------------------------------------------


def test_session_info_answers_before_login(anon_client: TestClient) -> None:
    """The console must be able to ask "am I signed in" without a credential."""
    response = anon_client.get("/api/auth/session")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["authenticated"] is False
    assert body["scope"] is None
    assert body["elevated_until"] is None
    assert body["hostname"]
    assert body["version"]
    assert body["csrf_header"] == CSRF_HEADER_NAME


def test_session_info_reports_scope_and_elevation(client: TestClient, master_token: str) -> None:
    """A signed-in, not-yet-elevated session reports its scope but no elevation."""
    fresh = client.get("/api/auth/session").json()
    assert fresh["authenticated"] is True
    assert fresh["scope"] == "admin"
    assert fresh["elevated_until"] is None

    elevate(client, token=master_token)

    confirmed = client.get("/api/auth/session").json()
    assert confirmed["elevated_until"] is not None


# --------------------------------------------------------------------------
# Sudo mode gating a destructive action
# --------------------------------------------------------------------------


def test_a_fresh_session_must_confirm_before_deleting(
    client: TestClient, seeded_app: None, queued: list[dict[str, Any]]
) -> None:
    """A cookie session that never elevated is refused, never reaching the job manager."""
    response = client.delete("/api/apps/example.com")

    assert response.status_code == 403, response.text
    assert response.json()["error"] == "elevation_required"
    assert not queued


def test_elevation_lasts_ten_minutes(
    client: TestClient,
    seeded_app: None,
    queued: list[dict[str, Any]],
    clock: FakeClock,
    master_token: str,
) -> None:
    """Elevating opens a ten minute window; the next request after it closes is refused again."""
    body = elevate(client, token=master_token)
    assert body["elevated_until"]

    ok = client.delete("/api/apps/example.com")
    assert ok.status_code == 202, ok.text
    assert queued

    clock.advance(601)

    blocked = client.delete("/api/apps/other.com")
    assert blocked.status_code == 403, blocked.text
    assert blocked.json()["error"] == "elevation_required"


def test_a_wrong_factor_counts_towards_the_lockout(client: TestClient) -> None:
    """A wrong master token at /elevate is a credential guess, not a free try."""
    protection = get_brute_force()
    before = protection.get_attempts_remaining("testclient")

    response = client.post("/api/auth/elevate", json={"token": "wasm_not-the-token"})

    assert response.status_code == 401, response.text
    assert response.json()["error"] == "invalid_token"
    after = protection.get_attempts_remaining("testclient")
    assert after < before


def test_admin_tokens_are_not_asked_to_elevate(
    client: TestClient, master_token: str, seeded_app: None, queued: list[dict[str, Any]]
) -> None:
    """
    An admin-scoped Bearer credential is explicit automation and is exempt.

    Minting the token itself still goes through sudo mode - it is a cookie
    session doing it, and issuing a standing credential is on D5's list. Using
    the finished token to delete something is a different request, presented
    on a different channel, and does not ask again.
    """
    elevate(client, token=master_token)
    issued = client.post("/api/auth/tokens", json={"name": "ci", "scope": "admin"})
    assert issued.status_code == 201, issued.text
    token = issued.json()["token"]

    anon = TestClient(client.app, client=("testclient", 50000))
    response = anon.delete("/api/apps/example.com", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 202, response.text
    assert queued


def test_issuing_a_token_itself_needs_sudo_mode(client: TestClient) -> None:
    """Minting a standing credential is the kind of action D5 gates, not just deleting."""
    response = client.post("/api/auth/tokens", json={"name": "ci", "scope": "admin"})

    assert response.status_code == 403, response.text
    assert response.json()["error"] == "elevation_required"


def test_a_deploy_scoped_token_stays_exempt_and_still_lacks_admin_scope(
    client: TestClient, master_token: str, seeded_app: None, queued: list[dict[str, Any]]
) -> None:
    """
    Issuing the token needs elevation; using it to delete does not, but fails on scope.

    This is the two chokepoints working together rather than against each
    other: sudo mode gates *minting* a standing credential, and the existing
    scope policy still gates *using* one that is not admin.
    """
    elevate(client, token=master_token)
    issued = client.post("/api/auth/tokens", json={"name": "deployer", "scope": "deploy"})
    assert issued.status_code == 201, issued.text
    token = issued.json()["token"]

    anon = TestClient(client.app, client=("testclient", 50000))
    refused = anon.delete("/api/apps/example.com", headers={"Authorization": f"Bearer {token}"})
    assert refused.status_code == 403
    assert not queued


def test_the_master_token_bearer_is_exempt(
    client: TestClient, master_token: str, seeded_app: None, queued: list[dict[str, Any]]
) -> None:
    """The master token presented as a Bearer credential needs no elevation either."""
    anon = TestClient(client.app, client=("testclient", 50000))
    response = anon.delete(
        "/api/apps/example.com", headers={"Authorization": f"Bearer {master_token}"}
    )
    assert response.status_code == 202, response.text
    assert queued
