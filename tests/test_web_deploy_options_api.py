# Copyright (c) 2024-2026 Yago Lopez Prado
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Tests that the monorepo and docker-compose deploy options travel whole.

Ported from the deleted server-rendered deploy form, which used to parse
"web:app\\napi:api" and "web, api" out of textareas by hand before this
JSON API existed; ``CreateAppRequest`` now carries the same options as
structured fields (a dict, a list), so there is no parsing left to test at
the API layer - only that ``POST /api/apps`` forwards them into the queued
job's kwargs under exactly the names the deployers read, and that the job
function forwards them again into ``configure()``. An option that silently
stops halfway between the request and the deployer is a form that lies, JSON
body or not.

tests/test_web_deploy.py already covers the deploy job's own "panel" trigger
and the plain happy path; this file is only about the monorepo/compose
options, which nothing else exercises end to end.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from wasm.core.store import WASMStore
from wasm.web.auth import CSRF_HEADER_NAME, SecurityConfig
from wasm.web.server import create_app, get_token_manager


@pytest.fixture
def store(tmp_path: Path) -> Iterator[WASMStore]:
    """
    Args:
        tmp_path: Per-test temporary directory.

    Yields:
        A store of this test's own.
    """
    WASMStore.reset_instance()
    instance = WASMStore(tmp_path / "wasm.db")
    try:
        yield instance
    finally:
        instance.close()
        WASMStore.reset_instance()


@pytest.fixture
def app(tmp_path: Path, store: WASMStore, runner: object) -> FastAPI:
    """
    Args:
        tmp_path: Per-test temporary directory.
        store: The store fixture.
        runner: The fake command runner, so no manager reaches a real process.

    Returns:
        The application.
    """
    return create_app(SecurityConfig(state_dir=tmp_path / "state", rate_limit_requests=5000))


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
def queued(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """
    Capture the deployment instead of queueing a real job.

    Args:
        monkeypatch: Patching helper, scoped to the test.

    Returns:
        The requests that reached the job manager.
    """
    captured: list[dict[str, Any]] = []

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
}


def test_a_monorepo_request_carries_its_options_to_the_job(
    client: TestClient, queued: list[dict[str, Any]]
) -> None:
    """The structured monorepo fields reach the queued job's kwargs unchanged."""
    response = client.post(
        "/api/apps",
        json={
            **FORM,
            "app_type": "monorepo",
            "env_vars": {"FOO": "bar", "BAZ": "qux"},
            "subdomain_overrides": {"web": "app", "api": "api"},
            "workspace_filter": ["web", "api"],
            "skip_database": True,
        },
    )

    assert response.status_code == 202, response.text
    kwargs = queued[0]["kwargs"]
    assert kwargs["env_vars"] == {"FOO": "bar", "BAZ": "qux"}
    assert kwargs["subdomain_overrides"] == {"web": "app", "api": "api"}
    assert kwargs["workspace_filter"] == ["web", "api"]
    assert kwargs["skip_database"] is True
    assert kwargs["compose_file"] is None
    assert kwargs["compose_profiles"] is None


def test_a_compose_request_carries_its_options_to_the_job(
    client: TestClient, queued: list[dict[str, Any]]
) -> None:
    """The structured compose fields reach the queued job's kwargs unchanged."""
    response = client.post(
        "/api/apps",
        json={
            **FORM,
            "app_type": "docker-compose",
            "compose_file": "deploy/compose.yml",
            "compose_profiles": ["web", "worker"],
        },
    )

    assert response.status_code == 202, response.text
    kwargs = queued[0]["kwargs"]
    assert kwargs["compose_file"] == "deploy/compose.yml"
    assert kwargs["compose_profiles"] == ["web", "worker"]
    assert kwargs["subdomain_overrides"] == {}
    assert kwargs["skip_database"] is False


def test_the_job_hands_the_options_to_the_deployer(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    The last hop: the job function must forward the options to ``configure``
    under exactly the keyword names the deployers read.

    Args:
        monkeypatch: Patching helper, scoped to the test.
    """
    from wasm.web.jobs import Job, JobContext, JobType, deploy_app_job

    captured: dict[str, Any] = {}

    class FakeDeployer:
        """Records how it was configured and deploys nothing."""

        last_deployment_id = None

        def configure(self, **kwargs: Any) -> None:
            captured.update(kwargs)

        def deploy(self) -> bool:
            return True

    monkeypatch.setattr("wasm.deployers.get_deployer", lambda *a, **k: FakeDeployer())

    job = Job(id="job-test", type=JobType.DEPLOY, name="deploy", description="")
    deploy_app_job(
        "mono.example.com",
        "https://github.com/you/mono",
        "monorepo",
        env_vars={"FOO": "bar"},
        subdomain_overrides={"web": "app"},
        workspace_filter=["web"],
        skip_database=True,
        compose_file="deploy/compose.yml",
        compose_profiles=["web"],
        job_context=JobContext(job, lambda _job: None),
    )

    assert captured["subdomain_overrides"] == {"web": "app"}
    assert captured["workspace_filter"] == ["web"]
    assert captured["skip_database"] is True
    assert captured["compose_file"] == "deploy/compose.yml"
    assert captured["compose_profiles"] == ["web"]
    assert captured["env_vars"] == {"FOO": "bar"}
    assert captured["trigger"] == "panel"


def test_a_request_with_www_paths_and_limits_carries_them_to_the_job(
    client: TestClient, queued: list[dict[str, Any]]
) -> None:
    """include_www, persistent_paths and the three unit limits reach the queued job."""
    response = client.post(
        "/api/apps",
        json={
            **FORM,
            "include_www": True,
            "persistent_paths": ["storage", "uploads"],
            "memory_max_mb": 512,
            "cpu_quota_percent": 150,
            "tasks_max": 200,
        },
    )

    assert response.status_code == 202, response.text
    kwargs = queued[0]["kwargs"]
    assert kwargs["include_www"] is True
    assert kwargs["persistent_paths"] == ["storage", "uploads"]
    assert kwargs["memory_max_mb"] == 512
    assert kwargs["cpu_quota_percent"] == 150
    assert kwargs["tasks_max"] == 200


def test_a_limit_below_the_minimum_is_refused_before_queueing(
    client: TestClient, queued: list[dict[str, Any]]
) -> None:
    """The same range PATCH .../limits enforces applies at creation too."""
    response = client.post("/api/apps", json={**FORM, "memory_max_mb": 1})

    assert response.status_code == 400, response.text
    assert response.json()["error"] == "validationerror"
    assert not queued


def test_the_job_hands_www_paths_and_limits_to_the_deployer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """deploy_app_job forwards include_www, persistent_paths and the limits to configure()."""
    from wasm.web.jobs import Job, JobContext, JobType, deploy_app_job

    captured: dict[str, Any] = {}

    class FakeDeployer:
        """Records how it was configured and deploys nothing."""

        last_deployment_id = None

        def configure(self, **kwargs: Any) -> None:
            captured.update(kwargs)

        def deploy(self) -> bool:
            return True

    monkeypatch.setattr("wasm.deployers.get_deployer", lambda *a, **k: FakeDeployer())

    job = Job(id="job-test", type=JobType.DEPLOY, name="deploy", description="")
    deploy_app_job(
        "limited.example.com",
        "https://github.com/you/app",
        "nodejs",
        include_www=True,
        persistent_paths=["storage"],
        memory_max_mb=256,
        cpu_quota_percent=50,
        tasks_max=64,
        job_context=JobContext(job, lambda _job: None),
    )

    assert captured["include_www"] is True
    assert captured["persistent_paths"] == ["storage"]
    assert captured["memory_max_mb"] == 256
    assert captured["cpu_quota_percent"] == 50
    assert captured["tasks_max"] == 64
    assert captured["resource_limits_given"] is True


def test_a_package_manager_request_carries_it_to_the_job(
    client: TestClient, queued: list[dict[str, Any]]
) -> None:
    """
    PACKAGE_MANAGERS in the CLI's --pm used to lack yarn even though
    PackageManagerHelper fully supports it; the API had no field for it at
    all. Both now derive from the same list.
    """
    response = client.post("/api/apps", json={**FORM, "package_manager": "yarn"})

    assert response.status_code == 202, response.text
    assert queued[0]["kwargs"]["package_manager"] == "yarn"


def test_an_unsupported_package_manager_is_refused_before_queueing(
    client: TestClient, queued: list[dict[str, Any]]
) -> None:
    """A name PackageManagerHelper does not know is refused, not queued to fail later."""
    response = client.post("/api/apps", json={**FORM, "package_manager": "cobol-pm"})

    assert response.status_code == 400, response.text
    assert response.json()["error"] == "validationerror"
    assert not queued


def test_the_job_hands_the_package_manager_to_the_deployer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """deploy_app_job forwards package_manager to configure(), defaulting to auto."""
    from wasm.web.jobs import Job, JobContext, JobType, deploy_app_job

    captured: dict[str, Any] = {}

    class FakeDeployer:
        """Records how it was configured and deploys nothing."""

        last_deployment_id = None

        def configure(self, **kwargs: Any) -> None:
            captured.update(kwargs)

        def deploy(self) -> bool:
            return True

    monkeypatch.setattr("wasm.deployers.get_deployer", lambda *a, **k: FakeDeployer())

    job = Job(id="job-test", type=JobType.DEPLOY, name="deploy", description="")
    deploy_app_job(
        "pm.example.com",
        "https://github.com/you/app",
        "nodejs",
        package_manager="yarn",
        job_context=JobContext(job, lambda _job: None),
    )

    assert captured["package_manager"] == "yarn"


def test_the_job_defaults_the_package_manager_to_auto(monkeypatch: pytest.MonkeyPatch) -> None:
    """Omitting package_manager entirely must not hand configure() a bare None."""
    from wasm.web.jobs import Job, JobContext, JobType, deploy_app_job

    captured: dict[str, Any] = {}

    class FakeDeployer:
        """Records how it was configured and deploys nothing."""

        last_deployment_id = None

        def configure(self, **kwargs: Any) -> None:
            captured.update(kwargs)

        def deploy(self) -> bool:
            return True

    monkeypatch.setattr("wasm.deployers.get_deployer", lambda *a, **k: FakeDeployer())

    job = Job(id="job-test", type=JobType.DEPLOY, name="deploy", description="")
    deploy_app_job(
        "pm2.example.com",
        "https://github.com/you/app",
        "nodejs",
        job_context=JobContext(job, lambda _job: None),
    )

    assert captured["package_manager"] == "auto"
