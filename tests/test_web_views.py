# Copyright (c) 2024-2026 Yago Lopez Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
Tests for the panel's server-rendered pages.

The panel runs as root, so these are written as attacks and as audits rather
than as happy paths:

- **Every page demands a session.** A screen that lists domains, ports and
  configuration is not a public page, and the one that forgets is the one an
  attacker finds.
- **No page ships an unresolved variable.** The Jinja environment renders a
  missing name as ``[missing: x]`` instead of an empty string, so a typo in a
  context key is caught here rather than by a user asking where their data
  went.
- **Server data is escaped.** A domain name and an error message from certbot
  or systemd both reach the browser, and both can carry markup. An injected
  script in this panel is a root shell.
- **Secrets do not leave the server.** The settings screen and an
  application's environment are the two places a credential could be printed.
- **No handler is a coroutine.** These handlers block on the store and on
  managers; declared ``async`` they would run on the event loop and a single
  deploy would freeze every other request and every WebSocket.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.routing import Match

from wasm.core.config import Config
from wasm.core.store import App, Service, Site, WASMStore
from wasm.web.auth import CSRF_HEADER_NAME, SecurityConfig
from wasm.web.jobs import Job, JobLogEntry, JobStatus, JobType
from wasm.web.server import create_app, get_token_manager
from wasm.web.views import rendering, resources
from wasm.web.views import router as views_router

#: Every page a browser can reach, with no parameters to fill in.
PAGES = (
    "/",
    "/apps",
    "/services",
    "/sites",
    "/databases",
    "/certificates",
    "/backups",
    "/activity",
    "/settings",
    "/fragments/machine",
)

#: What the loud Undefined renders. Finding it in a page means a context key is
#: missing or misspelled.
MISSING_MARKER = "[missing:"

#: A domain that closes an attribute and opens a script tag.
XSS_DOMAIN = 'evil.com"><script>alert("boom-7")</script>'

#: An error message shaped like the output of a tool that has been tampered
#: with. It is shown verbatim, which is exactly why it has to be escaped.
XSS_ERROR = '<script>alert("boom-9")</script>'

#: Secrets planted where the panel could print them.
CONFIG_SECRET = "mysql-hunter2"
ENV_SECRET = "sk-live-openai-hunter2"
ENV_URL_SECRET = "postgres://app:url-hunter2@localhost:5432/app"

#: What certbot prints. Parsed by the manager, so the page is exercised through
#: the same path production uses.
CERTBOT_OUTPUT = """
Found the following certs:
  Certificate Name: good.example.com
    Domains: good.example.com www.good.example.com
    Expiry Date: {far} (VALID: 89 days)
    Certificate Path: /etc/letsencrypt/live/good.example.com/fullchain.pem
    Private Key Path: /etc/letsencrypt/live/good.example.com/privkey.pem
  Certificate Name: soon.example.com
    Domains: soon.example.com
    Expiry Date: {near} (VALID: 5 days)
    Certificate Path: /etc/letsencrypt/live/soon.example.com/fullchain.pem
    Private Key Path: /etc/letsencrypt/live/soon.example.com/privkey.pem
  Certificate Name: gone.example.com
    Domains: gone.example.com
    Expiry Date: {past} (INVALID: EXPIRED)
    Certificate Path: /etc/letsencrypt/live/gone.example.com/fullchain.pem
    Private Key Path: /etc/letsencrypt/live/gone.example.com/privkey.pem
"""


def certbot_output() -> str:
    """
    Render certbot's listing with dates relative to today.

    Returns:
        The text ``certbot certificates`` would print.
    """
    today = datetime.now().date()
    return CERTBOT_OUTPUT.format(
        far=(today + timedelta(days=89)).isoformat(),
        near=(today + timedelta(days=5)).isoformat(),
        past=(today - timedelta(days=3)).isoformat(),
    )


class FakeJobs:
    """A job manager that reports a fixed set of jobs."""

    def __init__(self, jobs: list[Job]) -> None:
        """
        Args:
            jobs: The jobs to report, newest first.
        """
        self._jobs = jobs

    def get_active_jobs(self) -> list[Job]:
        """
        Returns:
            The jobs that are queued or running.
        """
        return [job for job in self._jobs if job.status in (JobStatus.PENDING, JobStatus.RUNNING)]

    def get_all_jobs(self, limit: int = 50) -> list[Job]:
        """
        Args:
            limit: Most jobs to return.

        Returns:
            The jobs, newest first.
        """
        return self._jobs[:limit]


def make_job(
    job_id: str = "job-1",
    status: JobStatus = JobStatus.COMPLETED,
    error: str | None = None,
    domain: str = "example.com",
) -> Job:
    """
    Build a job the way the job manager records one.

    Args:
        job_id: Identifier.
        status: Status to report.
        error: Failure message, verbatim from the tool.
        domain: Resource the job acted on.

    Returns:
        The job.
    """
    now = datetime.now()
    job = Job(
        id=job_id,
        type=JobType.DEPLOY,
        name=f"Deploy {domain}",
        description=f"Deploying a nextjs application to {domain}",
        status=status,
        progress=40,
        current_step="Building",
        created_at=now - timedelta(minutes=5),
        started_at=now - timedelta(minutes=4),
        completed_at=None if status in (JobStatus.PENDING, JobStatus.RUNNING) else now,
        error=error,
        metadata={"domain": domain},
    )
    job.logs.append(JobLogEntry(timestamp=now, level="info", message="npm run build", step=40))
    return job


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
    Give the panel a store of its own.

    Args:
        sandbox: Isolated filesystem root.

    Yields:
        The store the pages read.
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
    runner.script(("sudo", "certbot", "certificates"), stdout=certbot_output())
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


def deploy(store: WASMStore, domain: str = "example.com", **overrides: Any) -> App:
    """
    Record an application, its unit and its site, as a deploy would.

    Args:
        store: The store to write to.
        domain: The application's domain.
        **overrides: Fields to override on the application record.

    Returns:
        The stored application.
    """
    fields: dict[str, Any] = {
        "domain": domain,
        "app_type": "nextjs",
        "source": "https://github.com/you/app",
        "branch": "main",
        "port": 3000,
        "app_path": f"/var/www/apps/{domain}",
        "status": "running",
        "ssl_enabled": True,
        "ssl_certificate": f"/etc/letsencrypt/live/{domain}/fullchain.pem",
        "deployed_at": datetime.now().isoformat(),
    }
    fields.update(overrides)
    app = store.create_app(App(**fields))
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


def test_every_page_redirects_to_the_sign_in_form_without_a_session(
    anonymous: TestClient,
) -> None:
    """A page must send a browser to the form, not answer with data."""
    for path in PAGES:
        response = anonymous.get(path)
        assert response.status_code == 303, f"{path} answered {response.status_code}"
        assert response.headers["location"] == "/login"


def test_the_application_page_also_demands_a_session(
    anonymous: TestClient, store: WASMStore
) -> None:
    """The parametrised page is not a hole in the fence."""
    deploy(store)
    response = anonymous.get("/apps/example.com")
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_every_page_answers_with_a_session(client: TestClient, store: WASMStore) -> None:
    """Each screen in the navigation exists and renders."""
    deploy(store)
    for path in (*PAGES, "/apps/example.com"):
        assert client.get(path).status_code == 200, path


def test_pages_render_when_the_machine_holds_nothing(client: TestClient) -> None:
    """An empty machine is the first thing an operator sees, not an edge case."""
    for path in PAGES:
        response = client.get(path)
        assert response.status_code == 200, path
        assert MISSING_MARKER not in response.text


def test_a_domain_that_is_not_deployed_answers_404(client: TestClient) -> None:
    """A page that reports something missing says so in its status too."""
    response = client.get("/apps/nothing-here.example.com")
    assert response.status_code == 404
    assert "nothing-here.example.com" in response.text


# ------------------------------------------------------------- template hygiene


def test_no_page_leaves_a_template_variable_unresolved(
    client: TestClient, store: WASMStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A missing context key renders a marker; none may reach a browser."""
    deploy(store)
    monkeypatch.setattr(
        "wasm.web.jobs.get_job_manager",
        lambda: FakeJobs([make_job(), make_job("job-2", JobStatus.RUNNING)]),
    )

    for path in (*PAGES, "/apps/example.com"):
        page = body_of(client, path)
        assert MISSING_MARKER not in page, f"{path} rendered an undefined variable"


def test_navigation_never_points_at_a_route_that_does_not_exist(
    app: FastAPI, client: TestClient, store: WASMStore
) -> None:
    """
    Every link and every htmx endpoint on every page resolves.

    A dead link in a panel that holds root over the machine costs more trust
    than a screen that is missing, because the operator stops believing the
    rest of it.
    """
    deploy(store)
    targets: set[str] = set()
    for path in (*PAGES, "/apps/example.com"):
        page = body_of(client, path)
        for attribute in ("href", "action", "hx-get", "hx-post", "hx-delete", "data-url"):
            targets.update(re.findall(rf'{attribute}="(/[^"#]*)"', page))

    unresolved = sorted(target for target in targets if not _resolves(app, target))
    assert not unresolved, f"pages link to routes that do not exist: {unresolved}"


def _resolves(app: FastAPI, path: str) -> bool:
    """
    Check whether the application would route a path.

    The application's own matching is used rather than a regex of our own, so
    this keeps working whatever shape the router assembles routes into.

    Args:
        app: The application.
        path: An absolute path taken from a rendered page.

    Returns:
        True when some route, mount or WebSocket answers for it.
    """
    for scope_type, method in (("http", "GET"), ("http", "POST"), ("websocket", None)):
        scope: dict[str, Any] = {
            "type": scope_type,
            "path": path,
            "root_path": "",
            "headers": [],
        }
        if method is not None:
            scope["method"] = method
        for route in app.routes:
            match, _ = route.matches(scope)
            if match is not Match.NONE:
                return True
    return False


def test_no_page_handler_is_a_coroutine() -> None:
    """
    A blocking handler declared async freezes the event loop.

    These handlers read the store and call managers. Run on the loop, one
    deploy takes every other request, every WebSocket and the heartbeat with
    it.
    """
    import inspect

    handlers = [route.endpoint for route in views_router.routes if hasattr(route, "endpoint")]
    assert len(handlers) >= 12, "route discovery is broken, this net would pass blindly"

    coroutines = [handler.__name__ for handler in handlers if inspect.iscoroutinefunction(handler)]
    assert not coroutines, f"page handlers must be synchronous: {coroutines}"


# --------------------------------------------------------------------- escaping


def test_a_domain_carrying_markup_comes_out_escaped(client: TestClient, store: WASMStore) -> None:
    """A domain reaches the page inside an attribute and inside text."""
    deploy(store, domain=XSS_DOMAIN)

    page = body_of(client, "/apps")
    assert "boom-7" in page, "the domain under test never reached the page"
    assert "<script>alert" not in page
    assert '"><script>' not in page


def test_an_error_from_a_tool_comes_out_escaped(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A message is shown verbatim, which is why it must be escaped."""
    monkeypatch.setattr(
        "wasm.web.jobs.get_job_manager",
        lambda: FakeJobs([make_job("job-3", JobStatus.FAILED, error=XSS_ERROR)]),
    )

    page = body_of(client, "/activity")
    assert "boom-9" in page, "the error under test never reached the page"
    assert "<script>alert" not in page


# ---------------------------------------------------------------------- secrets


def test_settings_never_shows_a_stored_secret(client: TestClient, config_file: Path) -> None:
    """The configuration screen is one request away from a credential dump."""
    config = Config()
    config.set("databases.credentials.mysql.password", CONFIG_SECRET)
    assert config.save() is True
    Config.reset_instance()

    page = body_of(client, "/settings")
    assert CONFIG_SECRET not in page
    assert "databases.credentials.mysql.password" in page
    # A redacted field has to read as hidden. Blank would say "nothing is set"
    # and invite someone to overwrite a working credential with an empty string.
    assert "***" in page
    assert "Redacted" in page


def test_an_application_environment_hides_its_credentials(
    client: TestClient, store: WASMStore
) -> None:
    """Both the secret-looking key and the password inside a URL are masked."""
    deploy(
        store,
        env_vars={
            "OPENAI_API_KEY": ENV_SECRET,
            "DATABASE_URL": ENV_URL_SECRET,
            "NODE_ENV": "production",
        },
    )

    page = body_of(client, "/apps/example.com")
    assert ENV_SECRET not in page
    assert "url-hunter2" not in page
    assert "OPENAI_API_KEY" in page
    assert "DATABASE_URL" in page
    # Non-secrets stay readable, or the screen is useless.
    assert "production" in page


# ------------------------------------------------------------------ behaviour


def test_the_shell_carries_the_session_csrf_token(client: TestClient) -> None:
    """
    Without it every mutation the panel offers is refused.

    The token was read off the session payload with an attribute lookup while
    the payload is a mapping, so it was always empty and every restart, delete
    and sign-out came back 403.
    """
    page = body_of(client, "/apps")
    match = re.search(r"hx-headers='\{\"X-CSRF-Token\": \"([^\"]*)\"\}'", page)
    assert match is not None, "the shell no longer sends a CSRF header"
    assert match.group(1), "the CSRF token reaching the browser is empty"


def test_signing_out_ends_the_session(client: TestClient) -> None:
    """The button in the shell has to actually revoke, and lead somewhere."""
    assert client.get("/apps").status_code == 200

    response = client.post("/logout")
    assert response.status_code == 200
    assert response.headers["HX-Redirect"] == "/login"

    assert client.get("/apps").status_code == 303


def test_certificates_are_railed_by_how_long_is_left(client: TestClient) -> None:
    """Green while there is time, amber inside the renewal window, red once expired."""
    page = body_of(client, "/certificates")

    assert 'id="row-good.example.com"' in page
    assert "row row--active" in page
    assert "row row--busy" in page
    assert "row row--failed" in page
    assert "/api/certs/soon.example.com/renew" in page


def test_a_domain_without_a_certificate_can_be_issued_one(
    client: TestClient, store: WASMStore
) -> None:
    """Issuing needs no free text field: the panel knows every domain it serves."""
    deploy(store, domain="bare.example.com", ssl_enabled=False)

    page = body_of(client, "/certificates")
    assert "/api/certs/bare.example.com" in page
    assert "bare.example.com" in page


def test_restoring_a_backup_names_the_domain_and_the_overwrite(
    client: TestClient, config_file: Path, sandbox: Path
) -> None:
    """The most destructive action in the panel asks a question worth reading."""
    write_backup(sandbox, "example.com")

    page = body_of(client, "/backups")
    assert "/api/backups/example-com_20260101_120000/restore" in page
    assert "overwrites the files currently deployed at example.com" in page
    assert "cannot be recovered" in page


def write_backup(sandbox: Path, domain: str) -> Path:
    """
    Write an archive and its sidecar where the backup manager looks.

    Args:
        sandbox: Isolated filesystem root.
        domain: Domain the backup belongs to.

    Returns:
        The backup directory.
    """
    app_name = domain.replace(".", "-")
    directory = sandbox / "backups" / app_name
    directory.mkdir(parents=True)

    config = Config()
    config.set("backup.directory", str(sandbox / "backups"))

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
    }
    (directory / f"{backup_id}.json").write_text(json.dumps(metadata))
    (directory / f"{backup_id}.tar.gz").write_bytes(b"not really a tarball")
    return directory


# ------------------------------------------------------------------- shaping


def test_a_timestamp_without_an_offset_is_read_as_local_time() -> None:
    """
    The store, the job manager and the backup manager all use datetime.now().

    That has no offset attached. Read as UTC, a deploy that finished a minute
    ago is reported as happening an hour into the future on any machine that is
    not in London.
    """
    just_now = datetime.now()
    assert rendering.since(just_now) in ("0s ago", "1s ago", "2s ago")
    assert rendering.since(just_now.isoformat()) in ("0s ago", "1s ago", "2s ago")
    assert rendering.since(None) == "—"
    # An unreadable timestamp is shown as it was stored rather than invented.
    assert rendering.since("not a timestamp") == "not a timestamp"


def test_a_deployment_time_is_not_reported_as_the_future(
    client: TestClient, store: WASMStore
) -> None:
    """The regression the local time reading fixes, on the page that showed it."""
    deploy(store)
    page = body_of(client, "/apps/example.com")
    assert "in 59m" not in page
    assert "ago" in page


def test_days_remaining_decide_the_rail_colour() -> None:
    """One mapping, in one place, for every screen that shows a certificate."""
    assert resources.certificate_state(90) == "active"
    assert resources.certificate_state(resources.CERT_WARNING_DAYS) == "busy"
    assert resources.certificate_state(0) == "failed"
    assert resources.certificate_state(-3) == "failed"
    assert resources.certificate_state(None) == "idle"
