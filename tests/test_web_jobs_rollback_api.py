# Copyright (c) 2024-2026 Yago Lopez Prado
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Tests for ``POST /api/jobs/rollback``.

Ported from the deleted panel's rollback section, which queued through this
same endpoint. The page's own "type the domain to confirm" double-check was a
page-level safety net with no JSON equivalent - the API trusts the session and
the CSRF token, same as every other job it queues - so that part of the old
coverage does not survive. What does: the request reaches the job manager as
a ``RESTORE`` job carrying exactly the domain and backup id asked for, and the
endpoint is not a hole in the fence. Before this, ``POST /api/jobs/rollback``
had no test coverage at all.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from wasm.core.store import WASMStore
from wasm.web.auth import CSRF_HEADER_NAME, SecurityConfig
from wasm.web.jobs import Job, JobStatus, JobType
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
        runner: The fake command runner.

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
def anonymous(app: FastAPI) -> TestClient:
    """
    Args:
        app: The application.

    Returns:
        A client with no session.
    """
    return TestClient(app, client=("testclient", 50000), follow_redirects=False)


class RecordingJobs:
    """A job manager that records what was queued instead of running it."""

    def __init__(self) -> None:
        self.created: list[dict[str, Any]] = []

    def create_job(self, **kwargs: Any) -> Job:
        """
        Record the request and answer with a pending job.

        Args:
            **kwargs: Everything the caller queued with.

        Returns:
            The job, as the queue would report it.
        """
        self.created.append(kwargs)
        return Job(
            id="ab12cd34",
            type=kwargs.get("job_type", JobType.RESTORE),
            name=kwargs.get("name", "Rollback"),
            description=kwargs.get("description", ""),
            status=JobStatus.PENDING,
            created_at=datetime.now(),
            metadata=kwargs.get("metadata") or {},
        )


@pytest.fixture
def queued_jobs(monkeypatch: pytest.MonkeyPatch) -> RecordingJobs:
    """
    Replace the queue behind the jobs API with a recorder.

    Args:
        monkeypatch: Patching helper, scoped to the test.

    Returns:
        The recorder, for asserting on what was queued.
    """
    fake = RecordingJobs()
    monkeypatch.setattr("wasm.web.api.jobs.get_job_manager", lambda: fake)
    return fake


def test_rollback_queues_a_restore_job_with_the_domain_and_backup_id(
    client: TestClient, queued_jobs: RecordingJobs
) -> None:
    """The endpoint is a thin translation of the request into a queued job."""
    response = client.post(
        "/api/jobs/rollback", json={"domain": "example.com", "backup_id": "example-com_2026"}
    )

    assert response.status_code == 202, response.text
    assert len(queued_jobs.created) == 1
    queued = queued_jobs.created[0]
    assert queued["job_type"] == JobType.RESTORE
    assert queued["kwargs"]["domain"] == "example.com"
    assert queued["kwargs"]["backup_id"] == "example-com_2026"
    assert response.json()["job"]["id"] == "ab12cd34"


def test_rollback_without_a_backup_id_still_queues(
    client: TestClient, queued_jobs: RecordingJobs
) -> None:
    """A domain alone is a valid request: the job function picks the latest point."""
    response = client.post("/api/jobs/rollback", json={"domain": "example.com"})

    assert response.status_code == 202, response.text
    assert queued_jobs.created[0]["kwargs"]["backup_id"] is None


def test_rollback_demands_a_session(anonymous: TestClient) -> None:
    """Rolling an application back is not a hole in the fence."""
    response = anonymous.post(
        "/api/jobs/rollback", json={"domain": "example.com", "backup_id": "x"}
    )
    assert response.status_code in (401, 403)
