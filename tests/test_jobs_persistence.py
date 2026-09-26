# Copyright (c) 2024-2026 Yago Lopez Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
Tests for Task 1.7: jobs survive a panel restart.

Before this, the whole job system lived in a Python dict inside one process.
Restarting the panel - a deploy, a crash, `systemctl restart wasm-panel` -
erased every job in flight along with the answer to "did it finish?", and a
job that was genuinely still running when the process died stayed forever
"running" in a UI that never comes back to correct it.

What is defended here:

- **An interrupted job is never a lie.** A job ``pending`` or ``running`` when
  the process starts was orphaned by whatever ran before, and startup marks it
  ``failed`` with an honest reason rather than leaving a "running" row nothing
  will ever finish.
- **History outlives the process.** ``GET /api/jobs`` and
  ``GET /api/jobs/{id}`` read the store, not the in-memory queue, so a job
  created in an earlier process is still there.
- **The build output is not memory-only.** Every log line a job reports is
  appended to a file on disk as it happens, readable through
  ``GET /api/jobs/{id}/log`` after the process that ran the job is gone.
- **There is one deploy route.** ``POST /api/jobs/deploy`` duplicated
  ``POST /api/apps`` and has been removed.
"""

from __future__ import annotations

import json
import stat
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from wasm.core.fs import SECRET_DIR_MODE, SECRET_MODE, set_fs
from wasm.core.store import JobRecord, WASMStore
from wasm.web.auth import CSRF_HEADER_NAME, SecurityConfig
from wasm.web.jobs import (
    INTERRUPTED_REASON,
    Job,
    JobContext,
    JobManager,
    JobStatus,
    JobType,
    get_job_manager,
)
from wasm.web.server import create_app as build_app
from wasm.web.server import get_token_manager


@pytest.fixture(autouse=True)
def real_filesystem() -> Any:
    """
    Run every test in this file against the real filesystem seam.

    A CLI test elsewhere in the suite that rehearses ``--dry-run`` installs
    the refusing filesystem process-wide; this file asserts on log files it
    really writes under ``tmp_path``, so it must not inherit that state.

    Yields:
        Nothing; the seam is reset on both sides.
    """
    set_fs(None)
    yield
    set_fs(None)


@pytest.fixture
def store(tmp_path: Path) -> Any:
    """
    Give this file a store of its own, backed by a temporary database.

    Args:
        tmp_path: Per-test temporary directory.

    Yields:
        The store the job manager under test persists to.
    """
    WASMStore.reset_instance()
    instance = WASMStore(tmp_path / "wasm.db")
    try:
        yield instance
    finally:
        instance.close()
        WASMStore.reset_instance()


@pytest.fixture(autouse=True)
def fresh_job_manager(store: Any) -> Any:
    """
    Give every test a job manager singleton of its own, wired to ``store``.

    Args:
        store: The store fixture, so the manager's startup check has
            something real to read.

    Yields:
        Nothing; the singleton is reset before and after the test.
    """
    JobManager.reset_instance()
    yield
    JobManager.reset_instance()


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


def _run_job_synchronously(
    manager: JobManager,
    job_type: JobType,
    name: str,
    description: str,
    func: Callable[..., Any],
    kwargs: dict[str, Any] | None = None,
) -> Job:
    """
    Queue and execute a job on the calling thread, not the worker's.

    ``JobManager.create_job`` hands the job to the background worker thread,
    whose scheduling a test should not have to depend on. This drives exactly
    the same path - opening the log, the "queued" notification,
    ``_execute_job`` - without a second thread's timing in the way.

    Args:
        manager: The job manager under test.
        job_type: Type of job.
        name: Short name.
        description: Detailed description.
        func: Function to execute; it must accept a ``job_context`` keyword.
        kwargs: Keyword arguments for the function.

    Returns:
        The finished job.
    """
    job_id = str(uuid.uuid4())[:8]
    job = Job(id=job_id, type=job_type, name=name, description=description)
    manager._jobs[job_id] = job
    manager._open_log(job_id)
    manager._notify_subscribers(job)
    manager._execute_job(job_id, func, (), kwargs or {})
    return job


def _tiny_job(job_context: JobContext | None = None) -> dict[str, Any]:
    """
    A minimal job function: two progress steps and a log line, then done.

    Args:
        job_context: Injected by the job manager.

    Returns:
        A small, JSON-serialisable result.
    """
    assert job_context is not None
    job_context.update("Doing the one thing", 50)
    job_context.log("halfway there", "info")
    job_context.update("Done", 100)
    return {"ok": True}


def _failing_job(job_context: JobContext | None = None) -> dict[str, Any]:
    """
    A job function that always raises, to exercise the failure path.

    Args:
        job_context: Injected by the job manager.

    Raises:
        RuntimeError: Always.
    """
    assert job_context is not None
    job_context.log("about to explode", "info")
    raise RuntimeError("the build tool exploded")


# ---------------------------------------------------------------------------
# An interrupted job is never a lie
# ---------------------------------------------------------------------------


def test_a_job_running_when_the_panel_stops_is_failed_on_restart(store: Any) -> None:
    """The review focus of Task 1.7: never vanish, never stay running forever."""
    store.create_job(
        JobRecord(
            id="j1",
            type="update",
            name="Update example.com",
            status="running",
            domain="example.com",
        )
    )

    JobManager.reset_instance()
    JobManager()  # simulates the panel starting back up

    job = store.get_job("j1")
    assert job is not None
    assert job.status == "failed"
    assert job.error == INTERRUPTED_REASON


def test_a_pending_job_is_also_failed_on_restart(store: Any) -> None:
    """A job that was only queued, never started, is just as orphaned."""
    store.create_job(JobRecord(id="j2", type="backup", name="Backup example.com", status="pending"))

    JobManager.reset_instance()
    JobManager()

    assert store.get_job("j2").status == "failed"


def test_a_job_that_already_finished_is_left_alone_on_restart(store: Any) -> None:
    """The real outcome of a completed job must never be overwritten."""
    store.create_job(
        JobRecord(id="j3", type="backup", name="Backup example.com", status="completed")
    )

    JobManager.reset_instance()
    JobManager()

    job = store.get_job("j3")
    assert job.status == "completed"
    assert job.error is None


def test_startup_with_no_interrupted_jobs_changes_nothing(store: Any) -> None:
    """The common case: a clean start has no history to correct."""
    store.create_job(JobRecord(id="j4", type="backup", name="Backup", status="completed"))

    JobManager.reset_instance()
    JobManager()

    assert store.get_job("j4").status == "completed"


# ---------------------------------------------------------------------------
# History outlives the process
# ---------------------------------------------------------------------------


def test_history_lists_jobs_from_before_the_restart(client: TestClient, store: Any) -> None:
    """
    A job this process never queued - created directly in the store, the way
    a job from a previous process would already be there - still shows up.
    """
    store.create_job(
        JobRecord(
            id="past1",
            type="deploy",
            name="Deploy old.example.com",
            description="Deploying a nextjs application to old.example.com",
            status="completed",
            progress=100,
            domain="old.example.com",
        )
    )

    response = client.get("/api/jobs")

    assert response.status_code == 200, response.text
    body = response.json()
    ids = [job["id"] for job in body["jobs"]]
    assert "past1" in ids
    entry = next(job for job in body["jobs"] if job["id"] == "past1")
    assert entry["status"] == "completed"
    assert entry["type"] == "deploy"
    assert entry["metadata"] == {"domain": "old.example.com"}


def test_a_single_job_from_before_the_restart_can_be_fetched(
    client: TestClient, store: Any
) -> None:
    """GET /api/jobs/{id} must not depend on the in-memory queue either."""
    store.create_job(
        JobRecord(
            id="cafebeef",
            type="update",
            name="Update old.example.com",
            status="failed",
            error="nginx: [emerg] duplicate listen options",
            domain="old.example.com",
        )
    )

    response = client.get("/api/jobs/cafebeef")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["id"] == "cafebeef"
    assert body["status"] == "failed"
    assert body["error"] == "nginx: [emerg] duplicate listen options"


def test_a_persisted_deploy_jobs_result_exposes_its_deployment_id(
    client: TestClient, store: Any
) -> None:
    """
    A completed deploy job's result carries ``deployment_id``; the API lifts it
    onto the job itself, so the console can link straight to the deployment's
    log without parsing the free-form result.
    """
    store.create_job(
        JobRecord(
            id="deadbeef",
            type="deploy",
            name="Deploy old.example.com",
            status="completed",
            domain="old.example.com",
            result_json=json.dumps(
                {"domain": "old.example.com", "status": "deployed", "deployment_id": 17}
            ),
        )
    )

    response = client.get("/api/jobs/deadbeef")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["deployment_id"] == 17
    assert body["result"]["deployment_id"] == 17


def test_a_job_with_no_deployment_id_in_its_result_answers_none(
    client: TestClient, store: Any
) -> None:
    """A job that never deployed anything (a backup, say) has nothing to link."""
    store.create_job(
        JobRecord(
            id="cafebabe",
            type="backup",
            name="Back up old.example.com",
            status="completed",
            domain="old.example.com",
            result_json=json.dumps({"domain": "old.example.com"}),
        )
    )

    response = client.get("/api/jobs/cafebabe")

    assert response.status_code == 200, response.text
    assert response.json()["deployment_id"] is None


def test_a_live_deploy_jobs_result_also_exposes_its_deployment_id() -> None:
    """The same lift happens for a job still in the in-memory queue, not just a persisted one."""
    from wasm.web.api.jobs import _to_response
    from wasm.web.jobs import Job, JobType

    job = Job(
        id="live1",
        type=JobType.DEPLOY,
        name="Deploy new.example.com",
        description="",
        result={"domain": "new.example.com", "status": "deployed", "deployment_id": 99},
    )

    assert _to_response(job).deployment_id == 99


def test_an_unknown_job_id_is_404(client: TestClient) -> None:
    """A job id nothing ever created is not found, not an empty success."""
    response = client.get("/api/jobs/deadbeef")

    assert response.status_code == 404


def test_the_list_can_be_filtered_by_status(client: TestClient, store: Any) -> None:
    """The activity screen needs to narrow the list without fetching everything."""
    store.create_job(JobRecord(id="a", type="backup", name="a", status="failed"))
    store.create_job(JobRecord(id="b", type="backup", name="b", status="completed"))

    response = client.get("/api/jobs", params={"status": "failed"})

    assert response.status_code == 200
    ids = [job["id"] for job in response.json()["jobs"]]
    assert ids == ["a"]


def test_the_list_can_be_filtered_by_domain(client: TestClient, store: Any) -> None:
    """A per-app history reuses the same endpoint the activity screen does."""
    store.create_job(
        JobRecord(id="a", type="update", name="a", status="completed", domain="a.example.com")
    )
    store.create_job(
        JobRecord(id="b", type="update", name="b", status="completed", domain="b.example.com")
    )

    response = client.get("/api/jobs", params={"domain": "a.example.com"})

    assert [job["id"] for job in response.json()["jobs"]] == ["a"]


def test_an_unknown_status_filter_is_a_validation_error(client: TestClient) -> None:
    """A typo in the filter should say so, not silently return everything."""
    response = client.get("/api/jobs", params={"status": "sideways"})

    assert response.status_code == 400


# ---------------------------------------------------------------------------
# The build output is not memory-only
# ---------------------------------------------------------------------------


def test_job_log_lines_are_readable_after_completion(
    client: TestClient, store: Any, runner: object
) -> None:
    """The log file, not the queue's memory, is what the endpoint reads."""
    manager = get_job_manager()
    job = _run_job_synchronously(manager, JobType.CUSTOM, "Tiny job", "A minimal job", _tiny_job)
    assert job.status == JobStatus.COMPLETED

    response = client.get(f"/api/jobs/{job.id}/log")

    assert response.status_code == 200, response.text
    body = response.json()
    assert "Doing the one thing" in body["content"]
    assert "halfway there" in body["content"]
    assert "Done" in body["content"]
    assert body["truncated"] is False


def test_a_failed_job_s_log_carries_the_failure(
    client: TestClient, store: Any, runner: object
) -> None:
    """A build's own words, verbatim, survive the process that ran it."""
    manager = get_job_manager()
    job = _run_job_synchronously(
        manager, JobType.CUSTOM, "Doomed job", "A job that fails", _failing_job
    )
    assert job.status == JobStatus.FAILED

    response = client.get(f"/api/jobs/{job.id}/log")

    assert "about to explode" in response.json()["content"]

    detail = client.get(f"/api/jobs/{job.id}")
    assert "the build tool exploded" in detail.json()["error"]


def test_the_tail_parameter_limits_how_much_comes_back(
    client: TestClient, store: Any, runner: object
) -> None:
    """A long build log must not have to be downloaded in full to check the end."""

    def chatty_job(job_context: JobContext | None = None) -> dict[str, Any]:
        assert job_context is not None
        for step in range(20):
            job_context.log(f"line {step}", "info")
        return {}

    manager = get_job_manager()
    job = _run_job_synchronously(manager, JobType.CUSTOM, "Chatty", "Logs a lot", chatty_job)

    response = client.get(f"/api/jobs/{job.id}/log", params={"tail": 3})

    assert response.status_code == 200
    body = response.json()
    assert body["truncated"] is True
    lines = [line for line in body["content"].splitlines() if line]
    # The manager's own "job started"/"completed" lines bracket the 20 the
    # job function reported; the last three are the tail of the whole log,
    # not just of what the job itself logged.
    assert len(lines) == 3
    assert "line 19" in lines[-2]
    assert "completed successfully" in lines[-1].lower()


def test_a_job_with_no_log_path_reports_an_empty_log(client: TestClient, store: Any) -> None:
    """A job recorded without a log file - the seam could not create one - is not a 500."""
    store.create_job(JobRecord(id="cafefeed", type="backup", name="Backup", status="completed"))

    response = client.get("/api/jobs/cafefeed/log")

    assert response.status_code == 200
    assert response.json() == {"content": "", "truncated": False}


def test_the_log_file_lives_in_a_sibling_of_deploy_logs(store: Any, runner: object) -> None:
    """Deploy logs and job logs are found the same way: next to the database."""
    manager = get_job_manager()
    job = _run_job_synchronously(manager, JobType.CUSTOM, "Tiny", "A tiny job", _tiny_job)

    expected = store.db_path.parent / "job-logs" / f"{job.id}.log"
    assert expected.is_file()


def test_the_log_file_and_its_directory_are_owner_only(store: Any, runner: object) -> None:
    """Job output can echo secrets a deploy printed; it is not world readable."""
    manager = get_job_manager()
    job = _run_job_synchronously(manager, JobType.CUSTOM, "Tiny", "A tiny job", _tiny_job)

    path = store.db_path.parent / "job-logs" / f"{job.id}.log"
    assert stat.S_IMODE(path.stat().st_mode) == SECRET_MODE
    assert stat.S_IMODE(path.parent.stat().st_mode) == SECRET_DIR_MODE


def test_the_stored_row_points_at_the_log_file(store: Any, runner: object) -> None:
    """The API's log endpoint follows ``log_path``; it has to actually be set."""
    manager = get_job_manager()
    job = _run_job_synchronously(manager, JobType.CUSTOM, "Tiny", "A tiny job", _tiny_job)

    record = store.get_job(job.id)
    assert record is not None
    assert record.log_path == str(store.db_path.parent / "job-logs" / f"{job.id}.log")


# ---------------------------------------------------------------------------
# Every transition is persisted, not just the outcome
# ---------------------------------------------------------------------------


def test_queueing_a_job_creates_its_row_before_it_runs(store: Any, runner: object) -> None:
    """The very first notification - queued, not yet started - creates the row."""
    manager = get_job_manager()
    job_id = str(uuid.uuid4())[:8]
    job = Job(id=job_id, type=JobType.CUSTOM, name="Slow", description="")
    manager._jobs[job_id] = job
    manager._open_log(job_id)

    manager._notify_subscribers(job)  # queued, never executed

    record = store.get_job(job_id)
    assert record is not None
    assert record.status == "pending"


def test_a_completed_job_s_result_is_persisted_as_json(store: Any, runner: object) -> None:
    """The result a job returns must survive the process that produced it."""
    manager = get_job_manager()
    job = _run_job_synchronously(manager, JobType.CUSTOM, "Tiny", "A tiny job", _tiny_job)

    record = store.get_job(job.id)
    assert record is not None
    assert record.result_json is not None
    assert '"ok": true' in record.result_json


def test_the_full_pipeline_persists_a_job_queued_through_the_http_api(
    client: TestClient, store: Any, runner: object
) -> None:
    """
    The end-to-end path: a real HTTP request reaches the store.

    Every piece is tested in isolation above; this is the one that would fail
    if the wiring between ``POST /api/jobs/update``, the job manager's worker
    thread and the store were wrong, without depending on the thread's exact
    timing: updating an application that does not exist fails immediately.
    """
    response = client.post("/api/jobs/update", json={"domain": "nope.example.com"})
    assert response.status_code == 202, response.text
    job_id = response.json()["job"]["id"]

    manager = get_job_manager()
    deadline = time.monotonic() + 2.0
    record = None
    while time.monotonic() < deadline:
        record = store.get_job(job_id)
        if record is not None and record.status in ("completed", "failed"):
            break
        time.sleep(0.02)
    else:
        pytest.fail("the job never reached a terminal state")

    assert record.status == "failed"
    assert record.error is not None
    assert "not found" in record.error.lower()
    assert manager.get_job(job_id) is not None


# ---------------------------------------------------------------------------
# There is one deploy route
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Jobs record who queued them
# ---------------------------------------------------------------------------


def test_a_job_queued_through_the_api_records_its_actor(client: TestClient, store: Any) -> None:
    """
    The cookie session queuing a job is recorded, not lost.

    The client fixture signs in through ``POST /api/auth/login``, which
    issues a cookie session with its own opaque id - see
    :func:`wasm.web.auth.actor_label` for why a job records a 12 character
    prefix of it rather than the id in full.
    """
    response = client.post("/api/jobs/update", json={"domain": "nope.example.com"})
    assert response.status_code == 202, response.text
    actor = response.json()["job"]["actor"]
    assert actor is not None
    assert len(actor) == 12

    record = store.get_job(response.json()["job"]["id"])
    assert record is not None
    assert record.actor == actor


def test_a_job_fetched_after_a_restart_still_carries_its_actor(
    client: TestClient, store: Any
) -> None:
    """A restart reads the job from the store; the actor must not be lost with it."""
    store.create_job(
        JobRecord(
            id="ac704811", type="backup", name="Backup", status="completed", actor="a1b2c3d4e5f6"
        )
    )

    response = client.get("/api/jobs/ac704811")

    assert response.status_code == 200, response.text
    assert response.json()["actor"] == "a1b2c3d4e5f6"


def test_post_jobs_deploy_is_gone(client: TestClient) -> None:
    """
    POST /api/apps is the one route that queues a deployment now.

    Nothing is registered for a POST to ``/jobs/deploy`` any more; "deploy"
    falls through to the ``/{job_id}`` pattern instead (GET and POST cancel
    only), so the honest answer from routing is 405, not a 404 that would
    suggest the path itself is unknown.
    """
    response = client.post(
        "/api/jobs/deploy",
        json={"domain": "app.example.com", "source": "https://github.com/you/app"},
    )

    assert response.status_code in (404, 405)
    assert response.status_code != 202


# ---------------------------------------------------------------------------
# Two threads recording one job
# ---------------------------------------------------------------------------


def test_queueing_and_starting_a_job_at_once_loses_no_write(
    store: Any, runner: object, caplog: pytest.LogCaptureFixture
) -> None:
    """
    The production failure: "Could not persist job faf6fc7b: UNIQUE constraint failed".

    The request thread records the job it queued while the worker records it
    starting. Every write must land, and the row must end at the newest state
    whichever thread got there first.
    """
    import threading
    from datetime import datetime

    manager = get_job_manager()
    barrier = threading.Barrier(2)

    for _ in range(100):
        job_id = str(uuid.uuid4())[:8]
        job = Job(id=job_id, type=JobType.CUSTOM, name="Race", description="")
        manager._jobs[job_id] = job

        def queue_it(job: Job = job) -> None:
            barrier.wait()
            manager._notify_subscribers(job)

        def start_it(job: Job = job) -> None:
            barrier.wait()
            job.status = JobStatus.RUNNING
            job.started_at = datetime.now()
            manager._notify_subscribers(job)

        threads = [threading.Thread(target=queue_it), threading.Thread(target=start_it)]
        with caplog.at_level("WARNING", logger="wasm.web.jobs"):
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=30)

        record = store.get_job(job_id)
        assert record is not None
        assert record.status == "running"
        assert record.started_at is not None

    assert [r.getMessage() for r in caplog.records if "Could not persist" in r.getMessage()] == []


# ---------------------------------------------------------------------------
# A failed job is logged at the weight its failure deserves
# ---------------------------------------------------------------------------


def _dns_refused_renewal(job_context: JobContext | None = None) -> None:
    """
    A job that fails the way a renewal does when DNS points elsewhere.

    Args:
        job_context: Injected by the job manager.

    Raises:
        CertificateError: Always, with certbot's output attached.
    """
    from wasm.core.exceptions import CertificateError

    raise CertificateError(
        "Could not renew the certificate for shop.example.com",
        details="shop.example.com does not resolve to this server.\nPoint its A record here.",
        output="certbot: Challenge failed for domain shop.example.com",
    )


def test_an_expected_failure_is_logged_as_one_line_without_a_traceback(
    store: Any, runner: object, caplog: pytest.LogCaptureFixture
) -> None:
    """
    A WASMError already says what happened and how to fix it; a traceback
    through the job worker only buries that.
    """
    manager = get_job_manager()

    with caplog.at_level("DEBUG", logger="wasm.web.jobs"):
        job = _run_job_synchronously(manager, JobType.CERT_RENEW, "Renew", "", _dns_refused_renewal)

    assert job.status == JobStatus.FAILED
    failures = [r for r in caplog.records if r.levelname == "ERROR"]
    assert len(failures) == 1
    record = failures[0]
    assert record.exc_info is None
    message = record.getMessage()
    assert "\n" not in message
    assert f"Job {job.id} failed" in message
    assert "Could not renew the certificate for shop.example.com" in message
    assert "Point its A record here." in message
    # The tool's own output is kept, at debug, not in the one line.
    assert "Challenge failed" not in message
    debug = [r.getMessage() for r in caplog.records if r.levelname == "DEBUG"]
    assert any("Challenge failed for domain shop.example.com" in line for line in debug)


def test_an_unexpected_failure_keeps_its_traceback(
    store: Any, runner: object, caplog: pytest.LogCaptureFixture
) -> None:
    """Anything that is not a WASMError is a defect, and finding it needs the trace."""
    manager = get_job_manager()

    with caplog.at_level("ERROR", logger="wasm.web.jobs"):
        job = _run_job_synchronously(manager, JobType.CUSTOM, "Boom", "", _failing_job)

    assert job.status == JobStatus.FAILED
    failures = [r for r in caplog.records if r.levelname == "ERROR"]
    assert len(failures) == 1
    assert failures[0].exc_info is not None
    assert failures[0].exc_info[0] is RuntimeError
