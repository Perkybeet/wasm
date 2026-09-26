"""
Who may make the panel build code, and from where.

Every build runs as root: ``npm install`` executes whatever lifecycle scripts
the repository declares. Creating an application or inspecting a source is
therefore an admin act, not something a ``deploy`` token handed to CI may do,
and deploying from a *local path* - any directory on the machine, ``/root``
included - is further reserved to an operator who proved it is still them:
the master token, or a console session in sudo mode. An API token never.

The stored source is shown back on every application read, so a credential
an older release stored inside a clone URL must not travel back out with it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.test_web_auth import bearer, issue_token
from wasm.core.store import App, WASMStore
from wasm.web.auth import CSRF_HEADER_NAME, SecurityConfig, required_scope
from wasm.web.server import create_app, get_token_manager

LOCAL_SOURCES = ("/srv/app", "./app", "~/app", "/")


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
def master(app: FastAPI) -> str:
    """
    Returns:
        A freshly generated master token.
    """
    return get_token_manager().generate_master_token()


@pytest.fixture
def session(app: FastAPI, master: str) -> tuple[TestClient, str]:
    """
    A cookie session, not elevated.

    Returns:
        The signed-in client and its CSRF token.
    """
    client = TestClient(app, client=("testclient", 50000))
    response = client.post("/api/auth/login", json={"token": master})
    assert response.status_code == 200, response.text
    csrf = response.json()["csrf_token"]
    client.headers[CSRF_HEADER_NAME] = csrf
    return client, csrf


@pytest.fixture
def queued(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """
    Capture queued jobs instead of running a build.

    Returns:
        The keyword arguments of every job an endpoint queued.
    """
    captured: list[dict[str, Any]] = []

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
                "type": "deploy",
                "name": "Deploy",
                "description": "",
                "status": "pending",
                "progress": 0,
                "total_steps": 100,
                "current_step": "",
                "created_at": "2026-01-01T00:00:00",
            }

    def create_job(**kwargs: Any) -> Queued:
        captured.append(kwargs)
        return Queued()

    manager = type("FakeJobs", (), {"create_job": staticmethod(create_job)})()
    monkeypatch.setattr("wasm.web.api.apps.get_job_manager", lambda: manager)
    monkeypatch.setattr("wasm.web.api.jobs.get_job_manager", lambda: manager)
    return captured


@pytest.fixture
def inspected(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """
    Record what ``POST /api/apps/inspect`` would have fetched, fetching nothing.

    Returns:
        The sources handed to ``inspect_source``.
    """
    seen: list[str] = []

    def fake_inspect(source: str, *, branch: str | None = None) -> Any:
        seen.append(source)
        raise AssertionError("the test only checks whether the fetch was reached")

    monkeypatch.setattr("wasm.web.api.apps.inspect_source", fake_inspect)
    return seen


def new_app(source: str, domain: str = "app.example.com") -> dict[str, Any]:
    """
    Args:
        source: The source to deploy.
        domain: The application's domain.

    Returns:
        A ``POST /api/apps`` body with a fixed port, so no port probe runs.
    """
    return {"domain": domain, "source": source, "port": 4000}


# --------------------------------------------------------------------- scope


def test_creating_and_inspecting_an_application_is_admin_scope() -> None:
    """The policy table: a build runs as root, so starting one is admin."""
    assert required_scope("POST", "/api/apps") == "admin"
    assert required_scope("POST", "/api/apps/inspect") == "admin"
    # What deploy is for: moving an existing application between releases.
    assert required_scope("POST", "/api/jobs/update") == "deploy"
    assert required_scope("POST", "/api/jobs/rollback") == "deploy"
    assert required_scope("POST", "/api/apps/app.example.com/releases/r1/activate") == "deploy"


def test_a_deploy_token_cannot_create_or_inspect_an_application(
    session: tuple[TestClient, str],
    master: str,
    queued: list[dict[str, Any]],
    inspected: list[str],
) -> None:
    """A CI token that can update an app cannot make the machine build a new one."""
    client, csrf = session
    token = issue_token(client, csrf, master, name="ci", scope="deploy")["token"]
    anon = TestClient(client.app, client=("testclient", 50000))

    created = anon.post(
        "/api/apps", headers=bearer(token), json=new_app("https://github.com/you/app")
    )
    inspect = anon.post(
        "/api/apps/inspect", headers=bearer(token), json={"source": "https://github.com/you/app"}
    )

    assert created.status_code == 403, created.text
    assert "admin" in created.json()["detail"]
    assert inspect.status_code == 403, inspect.text
    assert queued == []
    assert inspected == []


def test_a_deploy_token_still_queues_an_update(
    session: tuple[TestClient, str],
    master: str,
    store: WASMStore,
    queued: list[dict[str, Any]],
) -> None:
    """Narrowing creation must not take away what the scope exists for."""
    store.create_app(
        App(domain="app.example.com", app_type="static", app_path="/var/www/apps/app-example-com")
    )
    client, csrf = session
    token = issue_token(client, csrf, master, name="ci", scope="deploy")["token"]
    anon = TestClient(client.app, client=("testclient", 50000))

    response = anon.post(
        "/api/jobs/update", headers=bearer(token), json={"domain": "app.example.com"}
    )

    assert response.status_code == 202, response.text
    assert queued


# ---------------------------------------------------------------- local paths


@pytest.mark.parametrize("source", LOCAL_SOURCES)
def test_an_admin_api_token_can_never_deploy_a_local_path(
    session: tuple[TestClient, str],
    master: str,
    queued: list[dict[str, Any]],
    source: str,
) -> None:
    """A standing token is a script; a script does not get to build /root as root."""
    client, csrf = session
    token = issue_token(client, csrf, master, name="ops", scope="admin")["token"]
    anon = TestClient(client.app, client=("testclient", 50000))

    response = anon.post("/api/apps", headers=bearer(token), json=new_app(source))

    assert response.status_code == 403, response.text
    body = response.json()
    assert body["error"] == "forbidden"
    assert "local path" in body["detail"].lower()
    assert queued == []


def test_an_admin_api_token_cannot_inspect_a_local_path(
    session: tuple[TestClient, str], master: str, inspected: list[str]
) -> None:
    """Inspecting copies the directory and reads it back: the same reach as deploying."""
    client, csrf = session
    token = issue_token(client, csrf, master, name="ops", scope="admin")["token"]
    anon = TestClient(client.app, client=("testclient", 50000))

    response = anon.post("/api/apps/inspect", headers=bearer(token), json={"source": "/root"})

    assert response.status_code == 403, response.text
    assert inspected == []


def test_an_admin_api_token_still_deploys_from_a_repository(
    session: tuple[TestClient, str],
    master: str,
    store: WASMStore,
    queued: list[dict[str, Any]],
) -> None:
    """The refusal is about local paths, not about API tokens creating applications."""
    client, csrf = session
    token = issue_token(client, csrf, master, name="ops", scope="admin")["token"]
    anon = TestClient(client.app, client=("testclient", 50000))

    response = anon.post(
        "/api/apps", headers=bearer(token), json=new_app("https://github.com/you/app.git")
    )

    assert response.status_code == 202, response.text
    assert queued[0]["kwargs"]["source"] == "https://github.com/you/app.git"


def test_a_console_session_must_confirm_before_deploying_a_local_path(
    session: tuple[TestClient, str],
    master: str,
    store: WASMStore,
    queued: list[dict[str, Any]],
) -> None:
    """An operator may deploy a directory, after proving it is still them."""
    client, _csrf = session

    refused = client.post("/api/apps", json=new_app("/srv/app"))
    assert refused.status_code == 403, refused.text
    assert refused.json()["error"] == "elevation_required"
    assert queued == []

    elevated = client.post("/api/auth/elevate", json={"token": master})
    assert elevated.status_code == 200, elevated.text

    accepted = client.post("/api/apps", json=new_app("/srv/app"))
    assert accepted.status_code == 202, accepted.text
    assert queued


def test_the_master_token_deploys_a_local_path(
    app: FastAPI, master: str, store: WASMStore, queued: list[dict[str, Any]]
) -> None:
    """The master token is root by definition; nothing is gained by refusing it."""
    anon = TestClient(app, client=("testclient", 50000))

    response = anon.post("/api/apps", headers=bearer(master), json=new_app("/srv/app"))

    assert response.status_code == 202, response.text
    assert queued


# ------------------------------------------------------------------ redaction


@pytest.mark.parametrize(
    ("stored", "leaked"),
    [
        ("https://deploy:ghp_s3cr3tvalue@github.com/you/app.git", "ghp_s3cr3tvalue"),
        ("https://ghp_s3cr3tvalue@github.com/you/app.git", "ghp_s3cr3tvalue"),
        ("https://oauth2:glpat-s3cr3t@gitlab.com/you/app.git", "glpat-s3cr3t"),
    ],
)
def test_a_credential_stored_in_a_clone_url_never_comes_back_out(
    app: FastAPI, master: str, store: WASMStore, runner: object, stored: str, leaked: str
) -> None:
    """
    Older releases stored whatever URL they were given, token included.

    Args:
        runner: The fake command runner, so the status read reaches no process.
    """
    store.create_app(
        App(
            domain="app.example.com",
            app_type="static",
            app_path="/var/www/apps/app-example-com",
            source=stored,
        )
    )
    anon = TestClient(app, client=("testclient", 50000))

    one = anon.get("/api/apps/app.example.com", headers=bearer(master))
    listing = anon.get("/api/apps", headers=bearer(master))

    assert one.status_code == 200, one.text
    assert listing.status_code == 200, listing.text
    assert leaked not in one.text
    assert leaked not in listing.text
    assert one.json()["source"].endswith("github.com/you/app.git") or one.json()["source"].endswith(
        "gitlab.com/you/app.git"
    )
    assert "***" in one.json()["source"]


def test_an_ssh_login_name_is_not_mistaken_for_a_credential(
    app: FastAPI, master: str, store: WASMStore, runner: object
) -> None:
    """``git@`` is who ssh logs in as, not a secret; redacting it would lie."""
    store.create_app(
        App(
            domain="app.example.com",
            app_type="static",
            app_path="/var/www/apps/app-example-com",
            source="git@github.com:you/app.git",
        )
    )
    anon = TestClient(app, client=("testclient", 50000))

    one = anon.get("/api/apps/app.example.com", headers=bearer(master))

    assert one.json()["source"] == "git@github.com:you/app.git"
