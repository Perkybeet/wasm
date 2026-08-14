# Copyright (c) 2024-2026 Yago Lopez Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
Tests for the deployment history pages and the rollback section.

Written to the same standard as the rest of the view tests: every screen is
exercised with real rows in the real store, the captured log is a real file in
the sandbox, and the attacks are attempted rather than assumed away:

- **Every page demands a session.** History names domains, commits and error
  output; it is not a public record.
- **The log path stored in the database is data, not an instruction.** A row
  pointing outside the deployment log directory must not be read, must not
  become a 500, and must not leak a byte of what it points at.
- **The captured log and a recorded error are shown verbatim, escaped.** Build
  output can carry markup, and an injected script in this panel is a root
  shell.
- **The rollback confirmation is server-side.** The right name queues the
  existing job; the wrong name changes nothing and says so inline at 200.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from wasm.core.config import Config
from wasm.core.store import App, Service, Site, WASMStore
from wasm.web.auth import CSRF_HEADER_NAME, SecurityConfig
from wasm.web.jobs import Job, JobStatus, JobType
from wasm.web.server import create_app, get_token_manager
from wasm.web.views import deployments as deployment_views

#: What the loud Undefined renders. Finding it in a page means a context key is
#: missing or misspelled.
MISSING_MARKER = "[missing:"

#: A log line that closes an attribute and opens a script tag. It must reach
#: the page escaped, never executable.
XSS_LOG_LINE = '</pre><script>alert("boom-log")</script>'

#: An error shaped like tampered tool output, shown verbatim on the detail.
XSS_ERROR = '<script>alert("boom-deploy")</script> build failed'

#: Content planted outside the log directory. It must never reach a page.
PLANTED_SECRET = "TOP-SECRET-77-root-password"


@pytest.fixture
def config_file(sandbox: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """
    Point the configuration singleton at the sandbox.

    Args:
        sandbox: Isolated filesystem root.
        monkeypatch: Patching helper, scoped to the test.

    Yields:
        The path the panel reads configuration from.
    """
    path = sandbox / "etc" / "wasm" / "config.yaml"
    monkeypatch.setattr("wasm.core.config.DEFAULT_CONFIG_PATH", path)
    Config.reset_instance()
    try:
        yield path
    finally:
        Config.reset_instance()


@pytest.fixture
def store(sandbox: Path) -> Iterator[WASMStore]:
    """
    Give the panel a store of its own, inside the sandbox.

    Args:
        sandbox: Isolated filesystem root.

    Yields:
        The store the pages read. Its ``deploy-logs`` sibling is where the
        detail page is allowed to read captured logs from.
    """
    WASMStore.reset_instance()
    instance = WASMStore(sandbox / "wasm.db")
    try:
        yield instance
    finally:
        instance.close()
        WASMStore.reset_instance()


@pytest.fixture
def app(sandbox: Path, store: WASMStore, config_file: Path, runner) -> FastAPI:
    """
    Build the panel with its state inside the sandbox.

    Args:
        sandbox: Isolated filesystem root.
        store: The store fixture, so pages read test data.
        config_file: The configuration fixture.
        runner: The fake command runner, so no manager reaches a real process.

    Returns:
        The application.
    """
    return create_app(SecurityConfig(state_dir=sandbox / "state", rate_limit_requests=5000))


@pytest.fixture
def anonymous(app: FastAPI) -> TestClient:
    """
    A client with no session.

    Args:
        app: The application.

    Returns:
        The client.
    """
    return TestClient(app, client=("testclient", 50000), follow_redirects=False)


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    """
    A signed-in client, carrying the CSRF header htmx sends.

    Args:
        app: The application.

    Returns:
        The client.
    """
    signed_in = TestClient(app, client=("testclient", 50000), follow_redirects=False)
    token = get_token_manager().generate_master_token()
    response = signed_in.post("/api/auth/login", json={"token": token})
    assert response.status_code == 200, response.text
    signed_in.headers[CSRF_HEADER_NAME] = response.json()["csrf_token"]
    return signed_in


def deploy(store: WASMStore, domain: str = "example.com") -> App:
    """
    Record an application, its unit and its site, as a deploy would.

    Args:
        store: The store to write to.
        domain: The application's domain.

    Returns:
        The stored application.
    """
    app = store.create_app(
        App(
            domain=domain,
            app_type="nextjs",
            source="https://github.com/you/app",
            branch="main",
            port=3000,
            app_path=f"/var/www/apps/{domain}",
            status="running",
            deployed_at=datetime.now().isoformat(),
        )
    )
    store.create_service(
        Service(
            app_id=app.id,
            name=f"wasm-{domain.replace('.', '-')}",
            unit_file=f"/etc/systemd/system/wasm-{domain}.service",
            working_directory=f"/var/www/apps/{domain}",
            command="/usr/bin/node server.js",
            status="active",
            port=3000,
        )
    )
    store.create_site(
        Site(
            app_id=app.id,
            domain=domain,
            config_path=f"/etc/nginx/sites-available/{domain}",
            proxy_port=3000,
            ssl_enabled=True,
        )
    )
    return app


def record(
    store: WASMStore,
    domain: str = "example.com",
    *,
    status: str = "success",
    trigger: str = "panel",
    commit: str | None = "0a1b2c3",
    branch: str | None = "main",
    error: str | None = None,
    log_path: str | None = None,
) -> int:
    """
    Record one deployment attempt the way the recorder does.

    Args:
        store: The store to write to.
        domain: Domain the attempt belongs to.
        status: Final status; ``running`` leaves the row open.
        trigger: What initiated it.
        commit: Short hash annotated onto the row.
        branch: Branch annotated onto the row.
        error: The failure, verbatim, when there was one.
        log_path: Where the captured log claims to live.

    Returns:
        The row id.
    """
    deployment_id = store.record_deployment_start(domain, trigger, git_branch=branch)
    if commit or log_path:
        store.annotate_deployment(deployment_id, git_commit=commit, log_path=log_path)
    if status != "running":
        store.finish_deployment(deployment_id, status, error=error)
    return deployment_id


def write_log(store: WASMStore, domain: str, deployment_id: int, text: str) -> Path:
    """
    Write a captured log where the recorder would have put it.

    Args:
        store: The store, which anchors the log directory.
        domain: Domain the log belongs to.
        deployment_id: Row the log belongs to, which names the file.
        text: The log content.

    Returns:
        The log file's path.
    """
    directory = store.db_path.parent / "deploy-logs" / domain
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{deployment_id}.log"
    path.write_text(text)
    return path


def write_rollback_point(sandbox: Path, domain: str) -> str:
    """
    Write an archive and its sidecar where the backup manager looks.

    Args:
        sandbox: Isolated filesystem root.
        domain: Domain the backup belongs to.

    Returns:
        The backup id.
    """
    app_name = domain.replace(".", "-")
    directory = sandbox / "backups" / app_name
    directory.mkdir(parents=True, exist_ok=True)

    Config().set("backup.directory", str(sandbox / "backups"))

    backup_id = f"{app_name}_20260101_120000"
    metadata = {
        "id": backup_id,
        "domain": domain,
        "app_name": app_name,
        "created_at": datetime.now().isoformat(),
        "size_bytes": 4096,
        "app_type": "nextjs",
        "version": "2.0.0",
        "description": "Before upgrading Next.js",
        "includes_env": True,
        "includes_databases": True,
        "tags": ["pre-deploy"],
    }
    (directory / f"{backup_id}.json").write_text(json.dumps(metadata))
    (directory / f"{backup_id}.tar.gz").write_bytes(b"not really a tarball")
    return backup_id


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


def body_of(client: TestClient, path: str) -> str:
    """
    Fetch a page and return its markup.

    Args:
        client: A signed-in client.
        path: The page to fetch.

    Returns:
        The rendered page.
    """
    response = client.get(path)
    assert response.status_code == 200, f"{path} answered {response.status_code}"
    return response.text


# ------------------------------------------------------------ reachability


def test_every_history_surface_demands_a_session(anonymous: TestClient, store: WASMStore) -> None:
    """History names domains, commits and errors; it is not a public record."""
    deployment_id = record(store)
    for path in (
        "/deployments",
        f"/deployments/{deployment_id}",
        "/apps/example.com/deployments",
        "/apps/example.com/deployments/recent",
        "/apps/example.com/rollback/section",
    ):
        response = anonymous.get(path)
        assert response.status_code == 303, f"{path} answered {response.status_code}"
        assert response.headers["location"] == "/login"


def test_no_handler_is_a_coroutine() -> None:
    """These handlers block on the store; on the event loop they would freeze it."""
    for route in deployment_views.router.routes:
        endpoint = getattr(route, "endpoint", None)
        assert endpoint is not None
        assert not asyncio.iscoroutinefunction(endpoint), f"{route.path} is async"


# ------------------------------------------------------------ history pages


def test_the_global_history_lists_every_domain(client: TestClient, store: WASMStore) -> None:
    """One machine, one record: every domain's attempts on one screen."""
    record(store, "example.com", status="success", trigger="panel", commit="0a1b2c3")
    record(
        store,
        "other.example.com",
        status="failed",
        trigger="cli",
        commit="9f8e7d6",
        error="npm ERR! Build failed",
    )

    page = body_of(client, "/deployments")
    assert MISSING_MARKER not in page
    assert "example.com" in page
    assert "other.example.com" in page
    assert "0a1b2c3" in page
    assert "9f8e7d6" in page
    # The failed row already says what broke, verbatim.
    assert "npm ERR! Build failed" in page


def test_a_domains_history_only_lists_that_domain(client: TestClient, store: WASMStore) -> None:
    """The filtered page must not mix another domain's attempts in."""
    record(store, "example.com", commit="aaa1111")
    record(store, "other.example.com", commit="bbb2222")

    page = body_of(client, "/apps/example.com/deployments")
    assert MISSING_MARKER not in page
    assert "aaa1111" in page
    assert "bbb2222" not in page
    assert "other.example.com" not in page


def test_history_survives_the_application_being_gone(client: TestClient, store: WASMStore) -> None:
    """
    The record outlives the app on purpose.

    The history of a deleted application matters most at exactly the moment
    the app is gone, so the page renders the rows rather than answering 404
    about a record that exists.
    """
    record(store, "gone.example.com", status="failed", error="the reason it was deleted")
    page = body_of(client, "/apps/gone.example.com/deployments")
    assert "gone.example.com" in page
    assert "the reason it was deleted" in page


def test_an_empty_history_is_an_empty_state_not_a_blank(client: TestClient) -> None:
    """Both history pages render honestly when nothing was ever recorded."""
    for path in ("/deployments", "/apps/example.com/deployments"):
        page = body_of(client, path)
        assert MISSING_MARKER not in page
        assert "No deployments recorded" in page


def test_a_hostile_domain_and_error_reach_the_history_escaped(
    client: TestClient, store: WASMStore
) -> None:
    """Server data can carry markup; an injected script here is a root shell."""
    record(store, 'evil.com"><script>alert("boom-7")</script>', status="failed", error=XSS_ERROR)
    page = body_of(client, "/deployments")
    assert "<script>alert(" not in page
    assert "&lt;script&gt;" in page


# ------------------------------------------------------------ the detail


def test_the_detail_shows_the_captured_log_verbatim_and_escaped(
    client: TestClient, store: WASMStore
) -> None:
    """The log is the product of this screen: exact, complete and inert."""
    deployment_id = record(store, "example.com", status="success")
    log_path = write_log(
        store,
        "example.com",
        deployment_id,
        f"[2026-08-13 10:00:00] npm install\n[2026-08-13 10:00:05] {XSS_LOG_LINE}\n",
    )
    store.annotate_deployment(deployment_id, log_path=str(log_path))

    page = body_of(client, f"/deployments/{deployment_id}")
    assert MISSING_MARKER not in page
    assert "npm install" in page
    # The hostile line is present, as text, not as markup.
    assert "<script>alert(" not in page
    assert "&lt;script&gt;alert(" in page
    # The facts are on the page too.
    assert "example.com" in page
    assert "0a1b2c3" in page
    assert "panel" in page


def test_a_failed_deployment_shows_its_error_above_the_facts(
    client: TestClient, store: WASMStore
) -> None:
    """The failure is answered first, verbatim, escaped."""
    deployment_id = record(store, "example.com", status="failed", error=XSS_ERROR)
    page = body_of(client, f"/deployments/{deployment_id}")
    assert "The deployment failed" in page
    assert "build failed" in page
    assert "<script>alert(" not in page
    assert "&lt;script&gt;" in page


def test_a_log_path_outside_the_log_directory_is_refused_not_read(
    client: TestClient, store: WASMStore, sandbox: Path
) -> None:
    """
    The stored path is data, not an instruction.

    A row pointing at an arbitrary file must not become a 500 and must not
    leak a byte of what it points at, whether the path is plainly outside the
    directory or climbs out of it with dot segments.
    """
    planted = sandbox / "secret.txt"
    planted.write_text(PLANTED_SECRET)
    escape_routes = (
        str(planted),
        str(store.db_path.parent / "deploy-logs" / ".." / "secret.txt"),
    )

    for log_path in escape_routes:
        deployment_id = record(store, "example.com", status="success", log_path=log_path)
        response = client.get(f"/deployments/{deployment_id}")
        assert response.status_code == 200, log_path
        assert PLANTED_SECRET not in response.text, log_path
        assert "will not read it" in response.text, log_path


def test_a_missing_log_file_is_reported_honestly(client: TestClient, store: WASMStore) -> None:
    """A rotated or deleted log is a fact, stated as one, not an error."""
    vanished = store.db_path.parent / "deploy-logs" / "example.com" / "999.log"
    deployment_id = record(store, "example.com", log_path=str(vanished))
    page = body_of(client, f"/deployments/{deployment_id}")
    assert "no longer on disk" in page


def test_a_deployment_with_no_log_recorded_says_so(client: TestClient, store: WASMStore) -> None:
    """No path in the row means no capture happened; the page says exactly that."""
    deployment_id = record(store, "example.com")
    page = body_of(client, f"/deployments/{deployment_id}")
    assert "No build log was captured" in page


def test_an_unrecorded_deployment_answers_404(client: TestClient) -> None:
    """A page that reports something missing says so in its status too."""
    response = client.get("/deployments/424242")
    assert response.status_code == 404
    assert "No such deployment" in response.text


# --------------------------------------------------- the application page


def test_the_app_page_offers_both_sections(client: TestClient, store: WASMStore) -> None:
    """The sections load over htmx; the page must hand the browser their URLs."""
    deploy(store)
    page = body_of(client, "/apps/example.com")
    assert 'hx-get="/apps/example.com/deployments/recent"' in page
    assert 'hx-get="/apps/example.com/rollback/section"' in page


def test_the_recent_fragment_lists_at_most_five_newest_first(
    client: TestClient, store: WASMStore
) -> None:
    """The page section is a tail; the full record is one click away."""
    ids = [record(store, "example.com", commit=f"c{n}00000") for n in range(7)]

    page = body_of(client, "/apps/example.com/deployments/recent")
    assert MISSING_MARKER not in page
    for kept in ids[-5:]:
        assert f'id="deployment-{kept}"' in page
    for dropped in ids[:2]:
        assert f'id="deployment-{dropped}"' not in page
    assert "View all deployments of example.com" in page
    assert 'href="/apps/example.com/deployments"' in page


def test_the_recent_fragment_is_honest_when_nothing_was_recorded(
    client: TestClient, store: WASMStore
) -> None:
    """An app deployed before recording existed has an empty, honest section."""
    deploy(store)
    page = body_of(client, "/apps/example.com/deployments/recent")
    assert "No deployment has been recorded for example.com" in page


# ------------------------------------------------------------- rollback


def test_the_rollback_section_lists_the_points(
    client: TestClient, store: WASMStore, sandbox: Path
) -> None:
    """Each point names when it was taken, how big it is and what it is."""
    deploy(store)
    backup_id = write_rollback_point(sandbox, "example.com")
    page = body_of(client, "/apps/example.com/rollback/section")
    assert MISSING_MARKER not in page
    assert backup_id in page
    assert "4.0 KiB" in page
    assert "Roll back" in page
    assert f'hx-get="/apps/example.com/rollback/confirm/{backup_id}"' in page


def test_the_rollback_section_is_honest_when_there_are_no_points(
    client: TestClient, store: WASMStore, sandbox: Path
) -> None:
    """No archive means nothing to return to, said plainly."""
    deploy(store)
    Config().set("backup.directory", str(sandbox / "backups"))
    page = body_of(client, "/apps/example.com/rollback/section")
    assert "No rollback points exist for example.com" in page


def test_the_confirmation_asks_for_the_domain_by_name(
    client: TestClient, store: WASMStore, sandbox: Path
) -> None:
    """The form names the point, the consequence, and demands the domain typed."""
    deploy(store)
    backup_id = write_rollback_point(sandbox, "example.com")
    page = body_of(client, f"/apps/example.com/rollback/confirm/{backup_id}")
    assert 'name="confirm"' in page
    assert f'value="{backup_id}"' in page
    assert "Type example.com to confirm" in page
    assert "cannot be undone" in page


def test_rollback_with_the_right_name_queues_the_existing_job(
    client: TestClient, store: WASMStore, sandbox: Path, queued_jobs: RecordingJobs
) -> None:
    """The panel queues the same job the JSON API does, and says it did."""
    deploy(store)
    backup_id = write_rollback_point(sandbox, "example.com")

    response = client.post(
        "/apps/example.com/rollback",
        data={"backup_id": backup_id, "confirm": "example.com"},
    )
    assert response.status_code == 200, response.text
    assert len(queued_jobs.created) == 1
    queued = queued_jobs.created[0]
    assert queued["kwargs"]["domain"] == "example.com"
    assert queued["kwargs"]["backup_id"] == backup_id
    assert "queued" in response.text


def test_rollback_with_the_wrong_name_changes_nothing(
    client: TestClient, store: WASMStore, sandbox: Path, queued_jobs: RecordingJobs
) -> None:
    """A wrong name is a refusal at 200, inline, with the form still there."""
    deploy(store)
    backup_id = write_rollback_point(sandbox, "example.com")

    response = client.post(
        "/apps/example.com/rollback",
        data={"backup_id": backup_id, "confirm": "examp1e.com"},
    )
    assert response.status_code == 200
    assert queued_jobs.created == []
    assert "Type example.com exactly" in response.text
    # The operator is left on the form, able to correct themselves.
    assert 'name="confirm"' in response.text
