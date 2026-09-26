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
                return {
                    "id": self.id,
                    "type": "delete",
                    "name": "Delete example.com",
                    "description": "",
                    "status": "pending",
                    "progress": 0,
                    "total_steps": 100,
                    "current_step": "",
                    "created_at": "2026-01-01T00:00:00",
                }

        return Queued()

    manager = type("FakeJobs", (), {"create_job": staticmethod(create_job)})()
    monkeypatch.setattr("wasm.web.api.apps.get_job_manager", lambda: manager)
    monkeypatch.setattr("wasm.web.api.jobs.get_job_manager", lambda: manager)
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


# --------------------------------------------------------------------------
# POST /api/jobs/delete: the same deletion, queued through a second route
# --------------------------------------------------------------------------


def test_jobs_delete_requires_elevation_the_same_as_the_app_route(
    client: TestClient, seeded_app: None, queued: list[dict[str, Any]]
) -> None:
    """
    ``POST /api/jobs/delete`` runs the identical ``delete_app_job``
    ``DELETE /api/apps/{domain}`` queues, so a fresh, unelevated cookie
    session must be refused here too - not only on the route the console
    actually calls today.
    """
    response = client.post("/api/jobs/delete", json={"domain": "example.com"})

    assert response.status_code == 403, response.text
    assert response.json()["error"] == "elevation_required"
    assert not queued


def test_jobs_delete_succeeds_once_elevated(
    client: TestClient,
    seeded_app: None,
    queued: list[dict[str, Any]],
    master_token: str,
) -> None:
    """Elevating opens the same window for ``POST /api/jobs/delete`` as for the app route."""
    elevate(client, token=master_token)

    response = client.post("/api/jobs/delete", json={"domain": "example.com"})

    assert response.status_code == 202, response.text
    assert queued


# --------------------------------------------------------------------------
# Certificates: revoking and deleting are as destructive as deleting an app
# --------------------------------------------------------------------------


@pytest.fixture
def fake_cert_manager(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    """
    Stand in for ``CertManager`` so revoke/delete never reach certbot.

    Args:
        monkeypatch: Patching helper, scoped to the test.

    Returns:
        The calls the endpoint made - empty when a request never got past the
        elevation gate.
    """
    calls: list[tuple[str, str]] = []

    class FakeCertManager:
        """Records what it was asked, does nothing to the machine."""

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            """
            Args:
                *args: Ignored, kept for signature compatibility.
                **kwargs: Ignored, kept for signature compatibility.
            """

        def revoke(self, domain: str) -> bool:
            """
            Args:
                domain: Certificate name.

            Returns:
                Always True.
            """
            calls.append(("revoke", domain))
            return True

        def delete(self, domain: str) -> bool:
            """
            Args:
                domain: Certificate name.

            Returns:
                Always True.
            """
            calls.append(("delete", domain))
            return True

    monkeypatch.setattr("wasm.web.api.certs.CertManager", FakeCertManager)
    return calls


def test_revoking_a_certificate_requires_elevation(
    client: TestClient, fake_cert_manager: list[tuple[str, str]]
) -> None:
    """A fresh cookie session is refused before certbot is ever asked."""
    response = client.post("/api/certs/example.com/revoke")

    assert response.status_code == 403, response.text
    assert response.json()["error"] == "elevation_required"
    assert fake_cert_manager == []


def test_revoking_a_certificate_succeeds_once_elevated(
    client: TestClient, fake_cert_manager: list[tuple[str, str]], master_token: str
) -> None:
    """Elevating opens the same window revoking a certificate needs."""
    elevate(client, token=master_token)

    response = client.post("/api/certs/example.com/revoke")

    assert response.status_code == 200, response.text
    assert fake_cert_manager == [("revoke", "example.com")]


def test_deleting_a_certificate_requires_elevation(
    client: TestClient, fake_cert_manager: list[tuple[str, str]]
) -> None:
    """Deleting the certificate files is refused the same way revoking is."""
    response = client.delete("/api/certs/example.com")

    assert response.status_code == 403, response.text
    assert response.json()["error"] == "elevation_required"
    assert fake_cert_manager == []


def test_deleting_a_certificate_succeeds_once_elevated(
    client: TestClient, fake_cert_manager: list[tuple[str, str]], master_token: str
) -> None:
    """Elevating opens the same window deleting a certificate needs."""
    elevate(client, token=master_token)

    response = client.delete("/api/certs/example.com")

    assert response.status_code == 200, response.text
    assert fake_cert_manager == [("delete", "example.com")]


def test_the_master_token_bearer_is_exempt_for_certificate_revocation(
    client: TestClient, fake_cert_manager: list[tuple[str, str]], master_token: str
) -> None:
    """Automation presenting the master token needs no elevation either."""
    anon = TestClient(client.app, client=("testclient", 50000))

    response = anon.post(
        "/api/certs/example.com/revoke", headers={"Authorization": f"Bearer {master_token}"}
    )

    assert response.status_code == 200, response.text
    assert fake_cert_manager == [("revoke", "example.com")]


# --------------------------------------------------------------------------
# A session is a session on every channel
# --------------------------------------------------------------------------


@pytest.fixture
def bearer_session(app: FastAPI, master_token: str) -> tuple[TestClient, str, str]:
    """
    A session signed in with ``bearer: true``: the token travels in a header.

    Returns:
        A cookie-less client, the session token and its CSRF token.
    """
    login = TestClient(app, client=("testclient", 50000))
    response = login.post("/api/auth/login", json={"token": master_token, "bearer": True})
    assert response.status_code == 200, response.text
    body = response.json()
    anon = TestClient(app, client=("testclient", 50000))
    return anon, body["session_token"], body["csrf_token"]


def test_a_session_token_presented_as_bearer_must_still_confirm(
    bearer_session: tuple[TestClient, str, str],
    seeded_app: None,
    queued: list[dict[str, Any]],
) -> None:
    """
    The exemption belongs to the credential, not to the header it came in.

    Sudo mode used to exempt anything that arrived as ``Authorization:
    Bearer``, and a session token is accepted there: moving the same session
    from the cookie to the header skipped the confirmation entirely.
    """
    anon, session_token, csrf = bearer_session

    response = anon.delete(
        "/api/apps/example.com",
        headers={"Authorization": f"Bearer {session_token}", CSRF_HEADER_NAME: csrf},
    )

    assert response.status_code == 403, response.text
    assert response.json()["error"] == "elevation_required"
    assert not queued


def test_a_bearer_session_can_confirm_like_a_cookie_session(
    bearer_session: tuple[TestClient, str, str],
    master_token: str,
    seeded_app: None,
    queued: list[dict[str, Any]],
) -> None:
    """The way out of the refusal above works on the same channel."""
    anon, session_token, csrf = bearer_session
    headers = {"Authorization": f"Bearer {session_token}", CSRF_HEADER_NAME: csrf}

    elevated = anon.post("/api/auth/elevate", json={"token": master_token}, headers=headers)
    assert elevated.status_code == 200, elevated.text

    response = anon.delete("/api/apps/example.com", headers=headers)
    assert response.status_code == 202, response.text
    assert queued


def test_a_session_token_presented_as_bearer_needs_the_csrf_header(
    bearer_session: tuple[TestClient, str, str],
) -> None:
    """CSRF is the session's, so it applies to the session wherever it is presented."""
    anon, session_token, csrf = bearer_session

    without = anon.post("/api/auth/ws-ticket", headers={"Authorization": f"Bearer {session_token}"})
    assert without.status_code == 403, without.text

    with_csrf = anon.post(
        "/api/auth/ws-ticket",
        headers={"Authorization": f"Bearer {session_token}", CSRF_HEADER_NAME: csrf},
    )
    assert with_csrf.status_code == 200, with_csrf.text


def test_a_payload_without_a_credential_type_is_not_exempt() -> None:
    """Fail closed: only the master token and API tokens skip the confirmation."""
    from fastapi import HTTPException
    from starlette.requests import Request

    from wasm.web.api.deps import ensure_elevated

    request = Request({"type": "http", "method": "DELETE", "path": "/x", "headers": []})

    for exempt in ({"type": "master"}, {"type": "api_token", "scope": "admin"}):
        ensure_elevated(request, exempt)

    for refused in ({}, {"type": "session"}, {"type": "session", "source": "bearer"}):
        with pytest.raises(HTTPException) as caught:
            ensure_elevated(request, refused)
        assert caught.value.status_code == 403


# --------------------------------------------------------------------------
# Two-factor enrolment
# --------------------------------------------------------------------------


def test_enrolling_two_factor_needs_sudo_mode(client: TestClient, master_token: str) -> None:
    """
    Enrolling binds the second factor that guards every later confirmation.

    A hijacked, unconfirmed session that could enrol its own authenticator
    would own sudo mode from then on.
    """
    enroll = client.post("/api/auth/2fa/enroll")
    assert enroll.status_code == 403, enroll.text
    assert enroll.json()["error"] == "elevation_required"

    confirm = client.post("/api/auth/2fa/confirm", json={"code": "123456"})
    assert confirm.status_code == 403, confirm.text
    assert confirm.json()["error"] == "elevation_required"

    elevate(client, token=master_token)
    assert client.post("/api/auth/2fa/enroll").status_code == 200


# --------------------------------------------------------------------------
# Standing root: services, cron jobs and backup schedules
# --------------------------------------------------------------------------


@pytest.fixture
def recorded_units(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """
    Stand in for ``ServiceManager`` so a create never reaches systemd.

    Returns:
        The names the endpoint created - empty when refused at the gate.
    """
    created: list[str] = []

    class FakeServiceManager:
        """Records what it was asked, does nothing to the machine."""

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            """
            Args:
                *args: Ignored.
                **kwargs: Ignored.
            """

        def create_from_unit(self, name: str, content: str) -> None:
            created.append(name)

        def create_service(self, **kwargs: Any) -> None:
            created.append(str(kwargs["name"]))

        def enable(self, name: str) -> bool:
            return True

    monkeypatch.setattr("wasm.web.api.services.ServiceManager", FakeServiceManager)
    return created


@pytest.mark.parametrize(
    "body",
    [
        {"name": "worker", "raw_content": "[Service]\nExecStart=/bin/true\n"},
        {"name": "worker", "command": "/bin/true"},
    ],
)
def test_creating_a_service_needs_sudo_mode(
    client: TestClient, master_token: str, recorded_units: list[str], body: dict[str, Any]
) -> None:
    """A unit is a command systemd runs as root for as long as the machine is up."""
    refused = client.post("/api/services", json=body)
    assert refused.status_code == 403, refused.text
    assert refused.json()["error"] == "elevation_required"
    assert recorded_units == []

    elevate(client, token=master_token)
    accepted = client.post("/api/services", json=body)
    assert accepted.status_code == 200, accepted.text
    assert recorded_units == ["worker"]


def test_creating_or_rewriting_a_cron_job_needs_sudo_mode(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``POST /api/cron`` creates a job or rewrites one: a root command on a timer either way."""
    reached: list[str] = []

    class FakeCron:
        """Records the job, touches no timer."""

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            """
            Args:
                *args: Ignored.
                **kwargs: Ignored.
            """

        def create_job(self, job: Any) -> Any:
            reached.append(job.name)
            return job

        def get_job(self, name: str) -> None:
            return None

    monkeypatch.setattr("wasm.web.api.cron.CronManager", FakeCron)

    response = client.post(
        "/api/cron", json={"name": "cleanup", "command": "/bin/true", "schedule": "daily"}
    )

    assert response.status_code == 403, response.text
    assert response.json()["error"] == "elevation_required"
    assert reached == []


def test_scheduling_backups_needs_sudo_mode(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A schedule writes a root timer and decides how many backups survive retention."""
    reached: list[str] = []

    class FakeScheduler:
        """Records the schedule, touches no timer."""

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            """
            Args:
                *args: Ignored.
                **kwargs: Ignored.
            """

        def create_schedule(self, schedule: Any) -> None:
            reached.append(schedule.domain)

    monkeypatch.setattr("wasm.web.api.backup_schedules.BackupScheduler", FakeScheduler)

    response = client.post(
        "/api/backup-schedules", json={"domain": "example.com", "schedule": "daily"}
    )

    assert response.status_code == 403, response.text
    assert response.json()["error"] == "elevation_required"
    assert reached == []
