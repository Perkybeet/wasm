# Copyright (c) 2024-2026 Yago Lopez Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
Tests for deploying an application through ``POST /api/apps``.

Ported from the deleted server-rendered deploy form, which used to be the
only caller of this endpoint anywhere in the interface - an operator could
look at everything on the machine and create nothing. The form's own
presentation concerns (the type list drawn from the deployer registry, a
refusal that re-opens the form with what was typed, escaping a hostile
domain in rendered markup) have no JSON equivalent and are gone with it; the
monorepo/compose option forwarding this file used to cover has its own home
now in tests/test_web_deploy_options_api.py.

What is defended here:

- **A refusal is an answer.** A domain that is already deployed and a source
  that is not acceptable each come back with the reason, not a bare 500.
- **A port left unset is assigned, not sent as nothing.** The request model
  makes ``port`` optional; the endpoint must still hand the job a real one.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from wasm.core.store import App, WASMStore
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
def anonymous(app: FastAPI) -> TestClient:
    """
    Args:
        app: The application.

    Returns:
        A client with no session.
    """
    return TestClient(app, client=("testclient", 50000), follow_redirects=False)


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


PAYLOAD = {
    "domain": "app.example.com",
    "source": "https://github.com/you/app",
    "app_type": "nextjs",
    "branch": "main",
    "ssl": True,
}


# ---------------------------------------------------------------------------
# Submitting it
# ---------------------------------------------------------------------------


def test_creating_an_application_demands_a_session(anonymous: TestClient) -> None:
    """Deploying is not a hole in the fence."""
    response = anonymous.post("/api/apps", json=PAYLOAD)

    assert response.status_code in (401, 403)


def test_a_submitted_deployment_queues_the_job(client: TestClient, queued: list[Any]) -> None:
    """
    Args:
        client: A signed-in client.
        queued: Captured job requests.
    """
    response = client.post("/api/apps", json=PAYLOAD)

    assert response.status_code == 202, response.text
    assert len(queued) == 1


def test_the_deployment_carries_what_was_submitted(client: TestClient, queued: list[Any]) -> None:
    """
    Args:
        client: A signed-in client.
        queued: Captured job requests.
    """
    client.post("/api/apps", json=PAYLOAD)

    kwargs = queued[0]["kwargs"]
    assert kwargs["domain"] == "app.example.com"
    assert kwargs["source"] == "https://github.com/you/app"
    assert kwargs["app_type"] == "nextjs"
    assert kwargs["branch"] == "main"
    assert kwargs["ssl"] is True


def test_an_unset_port_is_assigned_rather_than_left_empty(
    client: TestClient, queued: list[Any]
) -> None:
    """
    Args:
        client: A signed-in client.
        queued: Captured job requests.
    """
    client.post("/api/apps", json=PAYLOAD)

    assert isinstance(queued[0]["kwargs"]["port"], int)


# ---------------------------------------------------------------------------
# Being refused
# ---------------------------------------------------------------------------


def test_a_domain_that_is_already_deployed_is_refused_with_409(
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

    response = client.post("/api/apps", json=PAYLOAD)

    assert response.status_code == 409
    assert "already exists" in response.text


def test_a_domain_that_is_not_acceptable_is_refused(client: TestClient) -> None:
    """
    The domain reaches nginx configuration and a certificate request, so it is
    validated before a job is queued rather than by whatever fails first.

    Args:
        client: A signed-in client.
    """
    response = client.post("/api/apps", json={**PAYLOAD, "domain": "not a domain"})

    assert response.status_code == 400


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

        last_deployment_id = None

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


def test_deploy_job_hands_its_own_id_to_the_deployer(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    The deployment history links back to the job that started it.

    :class:`~wasm.deployers.recorder.DeploymentRecorder` records ``job_id`` at
    the start of the deploy, read off the deployer :func:`recorder_for` is
    built from - so the job has to hand its id to the deployer before
    ``deploy()`` runs, not after.

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

        last_deployment_id = 42

    monkeypatch.setattr("wasm.deployers.get_deployer", lambda *a, **k: FakeDeployer())

    result = deploy_app_job(
        "app.example.com",
        "https://github.com/you/app",
        "nodejs",
        job_context=_job_context(),
    )

    assert captured["job_id"] == "job-test"
    assert result["deployment_id"] == 42


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
