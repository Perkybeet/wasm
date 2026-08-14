# Copyright (c) 2024-2026 Yago Lopez Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
Tests for deploying an application from the panel.

The panel exists to deploy web applications and, until this screen, could not
deploy one: ``POST /api/apps`` had no caller anywhere in the interface. An
operator could look at everything on the machine and create nothing.

What is defended here:

- **One implementation.** The form calls the same ``create_app`` the JSON API
  calls. A second path that builds a deployment job is how the panel would come
  to disagree with the CLI about what a deployment is.
- **A refusal is an answer, not a dead end.** A domain that is already
  deployed, a port that is taken, a source that is not acceptable: each comes
  back with the reason and with what was typed still in the fields.
- **The type list is not hand-written.** It comes from the deployer registry,
  so adding a deployer reaches the panel without anyone remembering to edit it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from wasm.core.store import App, WASMStore
from wasm.deployers.registry import available_types
from wasm.web.auth import CSRF_HEADER_NAME, SecurityConfig
from wasm.web.server import create_app as build_app
from wasm.web.server import get_token_manager


@pytest.fixture
def store(tmp_path: Path) -> Any:
    """
    Give the panel a store of its own.

    Args:
        tmp_path: Per-test temporary directory.

    Yields:
        The store the pages read.
    """
    WASMStore.reset_instance()
    instance = WASMStore(tmp_path / "wasm.db")
    try:
        yield instance
    finally:
        instance.close()
        WASMStore.reset_instance()


@pytest.fixture
def app(tmp_path: Path, store: Any, runner: object) -> FastAPI:
    """
    Args:
        tmp_path: Per-test temporary directory.
        store: The store fixture.
        runner: The fake command runner, so no manager reaches a real process.

    Returns:
        The application.
    """
    return build_app(SecurityConfig(state_dir=tmp_path / "state", rate_limit_requests=5000))


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    """
    Args:
        app: The application.

    Returns:
        A signed-in client carrying the CSRF header.
    """
    signed_in = TestClient(app, client=("testclient", 50000), follow_redirects=False)
    token = get_token_manager().generate_master_token()
    response = signed_in.post("/api/auth/login", json={"token": token})
    assert response.status_code == 200, response.text
    signed_in.headers[CSRF_HEADER_NAME] = response.json()["csrf_token"]
    return signed_in


@pytest.fixture
def queued(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """
    Capture the deployment instead of queueing a real job.

    Args:
        monkeypatch: Patching helper, scoped to the test.

    Returns:
        The requests that reached the job manager.
    """
    captured: list[Any] = []

    def create_job(**kwargs: Any) -> Any:
        """
        Args:
            **kwargs: The job description.

        Returns:
            An object shaped like a queued job.
        """
        captured.append(kwargs)

        class Queued:
            """A job that was accepted but never run."""

            id = "job-1"
            status = type("Status", (), {"value": "pending"})()

            def to_dict(self) -> dict[str, Any]:
                """
                Returns:
                    The job as JSON-serialisable data.
                """
                return {"id": self.id}

        return Queued()

    manager = type("FakeJobs", (), {"create_job": staticmethod(create_job)})()
    monkeypatch.setattr("wasm.web.api.apps.get_job_manager", lambda: manager)
    return captured


FORM = {
    "domain": "app.example.com",
    "source": "https://github.com/you/app",
    "app_type": "nextjs",
    "branch": "main",
    "port": "",
    "webserver": "nginx",
    "ssl": "yes",
}


# ---------------------------------------------------------------------------
# The form
# ---------------------------------------------------------------------------


def test_the_deployment_screen_demands_a_session(app: FastAPI) -> None:
    """
    Args:
        app: The application.
    """
    anonymous = TestClient(app, client=("testclient", 50000), follow_redirects=False)

    assert anonymous.get("/apps/new").status_code == 303


def test_the_deployment_screen_is_not_read_as_an_application_name(client: TestClient) -> None:
    """
    ``/apps/new`` has to be declared before ``/apps/{domain}``, or the panel
    goes looking for an application called "new" and answers 404.
    """
    response = client.get("/apps/new")

    assert response.status_code == 200
    assert "Deploy an application" in response.text


def test_the_form_offers_every_type_the_registry_knows(client: TestClient) -> None:
    """
    A hand-written list is a list that goes stale the first time a deployer is
    added, and the CLI's copy already had.

    Args:
        client: A signed-in client.
    """
    body = client.get("/apps/new").text

    known = available_types()
    assert len(known) >= 8, "the registry was not populated, this test would pass blindly"
    for entry in known:
        assert f'value="{entry["type"]}"' in body, f"{entry['type']} is not offered"


def test_the_applications_screen_offers_a_way_to_deploy(client: TestClient) -> None:
    """The empty state invited the operator to a terminal and nowhere else."""
    body = client.get("/apps").text

    assert 'href="/apps/new"' in body


# ---------------------------------------------------------------------------
# Submitting it
# ---------------------------------------------------------------------------


def test_a_submitted_form_queues_the_deployment(client: TestClient, queued: list[Any]) -> None:
    """
    Args:
        client: A signed-in client.
        queued: Captured job requests.
    """
    response = client.post("/apps/new", data=FORM)

    assert response.status_code == 303
    assert response.headers["location"] == "/activity"
    assert len(queued) == 1


def test_the_deployment_carries_what_was_typed(client: TestClient, queued: list[Any]) -> None:
    """
    Args:
        client: A signed-in client.
        queued: Captured job requests.
    """
    client.post("/apps/new", data=FORM)

    kwargs = queued[0]["kwargs"]
    assert kwargs["domain"] == "app.example.com"
    assert kwargs["source"] == "https://github.com/you/app"
    assert kwargs["app_type"] == "nextjs"
    assert kwargs["branch"] == "main"
    assert kwargs["ssl"] is True


def test_an_unticked_certificate_box_means_no_certificate(
    client: TestClient, queued: list[Any]
) -> None:
    """
    An unchecked checkbox is absent from the body rather than false, which is
    the classic way for a form to silently mean the opposite of what it shows.

    Args:
        client: A signed-in client.
        queued: Captured job requests.
    """
    client.post("/apps/new", data={**FORM, "ssl": None})

    assert queued[0]["kwargs"]["ssl"] is False


def test_an_empty_port_is_assigned_rather_than_sent_as_empty(
    client: TestClient, queued: list[Any]
) -> None:
    """
    Args:
        client: A signed-in client.
        queued: Captured job requests.
    """
    client.post("/apps/new", data=FORM)

    assert isinstance(queued[0]["kwargs"]["port"], int)


def test_a_deployment_reaches_the_same_function_the_api_uses(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    One implementation. A second path that builds a deployment job is how the
    panel would come to disagree with the CLI about what a deployment is.

    Args:
        client: A signed-in client.
        monkeypatch: Patching helper, scoped to the test.
    """
    called: list[Any] = []
    import wasm.web.api.apps as api

    original = api.create_app

    def spy(body: Any, session: Any) -> Any:
        """
        Args:
            body: The deployment request.
            session: The authenticated session.

        Returns:
            Whatever the real endpoint returns.
        """
        called.append(body)
        raise AssertionError("stop here; reaching this proves the call was made")

    monkeypatch.setattr(api, "create_app", spy)
    assert original is not spy

    with pytest.raises(AssertionError):
        client.post("/apps/new", data=FORM)

    assert called, "the form did not call the API's own deployment function"


# ---------------------------------------------------------------------------
# Being refused
# ---------------------------------------------------------------------------


def test_a_domain_that_is_already_deployed_is_refused_with_the_reason(
    client: TestClient, store: Any
) -> None:
    """
    Args:
        client: A signed-in client.
        store: The store, holding the application already.
    """
    store.create_app(
        App(
            domain="app.example.com",
            app_type="nextjs",
            source="https://github.com/you/app",
            port=3000,
            app_path="/var/www/apps/app.example.com",
            status="running",
        )
    )

    response = client.post("/apps/new", data=FORM)

    assert response.status_code == 400
    assert "already exists" in response.text


def test_a_refusal_keeps_what_was_typed(client: TestClient, store: Any) -> None:
    """
    Emptying the form on a refusal makes the operator retype a URL to fix a
    single character.

    Args:
        client: A signed-in client.
        store: The store, holding the application already.
    """
    store.create_app(
        App(
            domain="app.example.com",
            app_type="nextjs",
            source="https://github.com/you/app",
            port=3000,
            app_path="/var/www/apps/app.example.com",
            status="running",
        )
    )

    body = client.post("/apps/new", data=FORM).text

    assert "https://github.com/you/app" in body
    assert "app.example.com" in body


def test_a_domain_that_is_not_acceptable_is_refused(client: TestClient) -> None:
    """
    The domain reaches nginx configuration and a certificate request, so it is
    validated before a job is queued rather than by whatever fails first.

    Args:
        client: A signed-in client.
    """
    response = client.post("/apps/new", data={**FORM, "domain": "not a domain"})

    assert response.status_code == 400
    assert "Deploy an application" in response.text, "the form should come back"


def test_a_refusal_does_not_leave_the_panel(client: TestClient) -> None:
    """
    Args:
        client: A signed-in client.
    """
    body = client.post("/apps/new", data={**FORM, "domain": ""}).text

    assert 'class="sidebar"' in body


def test_markup_typed_into_the_form_comes_back_escaped(client: TestClient) -> None:
    """
    Everything typed here is echoed into the form again on a refusal, and this
    panel runs as root.

    Args:
        client: A signed-in client.
    """
    body = client.post("/apps/new", data={**FORM, "domain": '"><script>alert(1)</script>'}).text

    assert "<script>alert(1)</script>" not in body


# ---------------------------------------------------------------------------
# The jobs say who triggered them
# ---------------------------------------------------------------------------


def _job_context() -> Any:
    """
    Build a job context outside the job manager, discarding notifications.

    Returns:
        A context the job functions accept.
    """
    from wasm.web.jobs import Job, JobContext, JobType

    job = Job(id="job-test", type=JobType.DEPLOY, name="deploy", description="")
    return JobContext(job, lambda _job: None)


def test_deploy_job_hands_the_panel_trigger_to_the_deployer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    The deployment history records who initiated each run, and the recording
    lives in the deployer. The job's whole contribution is the word "panel";
    losing it would file every panel deploy as a CLI one.

    Args:
        monkeypatch: Patching helper, scoped to the test.
    """
    from wasm.web.jobs import deploy_app_job

    captured: dict[str, Any] = {}

    class FakeDeployer:
        """Records how it was configured and deploys nothing."""

        def configure(self, **kwargs: Any) -> None:
            captured.update(kwargs)

        def deploy(self) -> bool:
            return True

    monkeypatch.setattr("wasm.deployers.get_deployer", lambda *a, **k: FakeDeployer())

    deploy_app_job(
        "app.example.com",
        "https://github.com/you/app",
        "nodejs",
        job_context=_job_context(),
    )

    assert captured["trigger"] == "panel"


def test_rollback_job_hands_the_panel_trigger_to_the_rollback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Args:
        monkeypatch: Patching helper, scoped to the test.
    """
    from wasm.web.jobs import rollback_app_job

    captured: dict[str, Any] = {}

    class FakeRollbackManager:
        """Records the rollback request instead of restoring anything."""

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def rollback(self, **kwargs: Any) -> bool:
            captured.update(kwargs)
            return True

    monkeypatch.setattr("wasm.managers.backup_manager.RollbackManager", FakeRollbackManager)

    rollback_app_job("app.example.com", backup_id="backup-1", job_context=_job_context())

    assert captured["trigger"] == "panel"
