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
- **Every screen is exercised with data in it.** An empty machine renders
  almost nothing, so a suite that only ever looks at empty screens is a suite
  that has not looked at the product. Reverting the databases delete URL to a
  route that does not exist used to leave every test green, because no test
  ever put a database in the store.
- **Every address a screen emits resolves, with the method it is emitted
  with.** A button that deletes through an address the router does not answer
  for is worse than no button.
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
from wasm.core.store import App, Database, Service, Site, WASMStore
from wasm.web.auth import CSRF_HEADER_NAME, SESSION_COOKIE_NAME, SecurityConfig
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

#: Every screen, including the ones that need a resource named in the URL. The
#: parametrised page is where a list's links actually land, so it belongs in
#: every sweep the flat pages get.
SCREENS = (*PAGES, "/apps/example.com")

#: How an address reaches the browser, and the method the browser will use with
#: it. Checking the method matters: a delete button aimed at a path that only
#: answers GET is still a dead button.
URL_ATTRIBUTES = {
    "href": "GET",
    "hx-get": "GET",
    "action": "POST",
    "hx-post": "POST",
    "hx-delete": "DELETE",
    "data-url": "WEBSOCKET",
}

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


@pytest.fixture
def populated(
    store: WASMStore, config_file: Path, sandbox: Path, monkeypatch: pytest.MonkeyPatch
) -> dict[str, Any]:
    """
    Put something on every screen the panel has.

    An empty machine renders almost nothing, so a suite that only ever looks at
    empty screens has not looked at the product. This is what caught the
    databases list offering a delete button aimed at a route that does not
    exist: nothing here ever created a database, so the row was never drawn.

    Args:
        store: The store the pages read.
        config_file: The configuration fixture, so settings has something.
        sandbox: Isolated filesystem root, where the backup archive lands.
        monkeypatch: Patching helper, scoped to the test.

    Returns:
        What was created, for a test that wants to name it.
    """
    deploy(store, domain="example.com")
    deploy(store, domain="broken.example.com", status="failed", ssl_enabled=False)

    store.create_database(
        Database(name="app_production", engine="postgresql", port=5432, username="app")
    )
    store.create_database(Database(name="cache", engine="mysql", port=3306))

    write_backup(sandbox, "example.com")

    jobs = [
        make_job("job-running", JobStatus.RUNNING),
        make_job("job-done", JobStatus.COMPLETED),
        make_job("job-broken", JobStatus.FAILED, error="nginx: [emerg] duplicate listen"),
    ]
    monkeypatch.setattr("wasm.web.jobs.get_job_manager", lambda: FakeJobs(jobs))

    config = Config()
    config.set("databases.credentials.mysql.password", CONFIG_SECRET)
    config.set("apps.directory", "/var/www/apps")

    return {
        "domains": ["example.com", "broken.example.com"],
        "databases": [("postgresql", "app_production"), ("mysql", "cache")],
        "jobs": jobs,
    }


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
    client: TestClient, populated: dict[str, Any]
) -> None:
    """A missing context key renders a marker; none may reach a browser."""
    for path in SCREENS:
        page = body_of(client, path)
        assert MISSING_MARKER not in page, f"{path} rendered an undefined variable"


def test_the_sign_in_page_leaves_no_template_variable_unresolved(anonymous: TestClient) -> None:
    """It renders outside the shell, so the sweep above never reaches it."""
    for response in (anonymous.get("/login"), anonymous.post("/login", data={"token": "wrong"})):
        assert MISSING_MARKER not in response.text


def addresses_on(client: TestClient, path: str) -> set[tuple[str, str]]:
    """
    Collect every address a screen hands the browser, with its method.

    Args:
        client: A signed-in client.
        path: The screen to read.

    Returns:
        Method and path pairs.
    """
    page = body_of(client, path)
    found: set[tuple[str, str]] = set()
    for attribute, method in URL_ATTRIBUTES.items():
        for target in re.findall(rf'(?:^|\s){attribute}="(/[^"#]*)"', page):
            found.add((method, target))
    return found


def test_every_address_on_every_populated_screen_resolves(
    app: FastAPI, client: TestClient, populated: dict[str, Any]
) -> None:
    """
    Every link, form and htmx endpoint resolves, with the method it is sent with.

    This is the test that stops a button pointing at nothing. It is run with
    the machine full rather than empty, because a row is where the action
    endpoints live and an empty screen has no rows: the databases list spent
    its whole life offering a delete aimed at ``/api/databases/{name}``, which
    matches no route at all, and every test passed because no test had ever
    created a database.
    """
    targets: set[tuple[str, str]] = set()
    per_screen: dict[str, int] = {}
    for path in (*SCREENS, "/apps/broken.example.com"):
        found = addresses_on(client, path)
        per_screen[path] = len(found)
        targets |= found

    empty = sorted(path for path, count in per_screen.items() if count == 0)
    assert not empty, f"these screens emitted no addresses, so they were not exercised: {empty}"

    # A net that catches nothing passes silently. These are the addresses that
    # only exist once the machine has something on it.
    assert ("DELETE", "/api/databases/databases/postgresql/app_production") in targets
    assert ("DELETE", "/api/apps/example.com") in targets
    assert ("POST", "/api/backups/example-com_20260101_120000/restore") in targets
    assert ("WEBSOCKET", "/ws/logs/example.com") in targets
    assert len(targets) >= 25, f"only {len(targets)} addresses were checked"

    unresolved = sorted(target for target in targets if not _resolves(app, *target))
    assert not unresolved, f"screens point at routes that do not exist: {unresolved}"


def test_the_sign_in_page_posts_to_a_route_that_exists(app: FastAPI, anonymous: TestClient) -> None:
    """The form is outside the shell, so it is swept separately or not at all."""
    response = anonymous.get("/login")
    assert response.status_code == 200

    targets: set[tuple[str, str]] = set()
    for attribute, method in URL_ATTRIBUTES.items():
        for target in re.findall(rf'(?:^|\s){attribute}="(/[^"#]*)"', response.text):
            targets.add((method, target))

    assert ("POST", "/login") in targets, "the form no longer posts to the login route"
    unresolved = sorted(target for target in targets if not _resolves(app, *target))
    assert not unresolved, f"the sign-in page points at routes that do not exist: {unresolved}"


def _resolves(app: FastAPI, method: str, path: str) -> bool:
    """
    Check whether the application would route a path with a given method.

    The application's own matching is used rather than a regex of our own, so
    this keeps working whatever shape the router assembles routes into. Only a
    full match counts: Starlette reports a path that exists under a different
    method as a partial one, and accepting that would pass a delete button
    aimed at a read-only route.

    Args:
        app: The application.
        method: HTTP method, or ``WEBSOCKET`` for a handshake.
        path: An absolute path taken from a rendered page.

    Returns:
        True when some route, mount or WebSocket fully answers for it.
    """
    scope: dict[str, Any] = {"path": path, "root_path": "", "headers": []}
    if method == "WEBSOCKET":
        scope["type"] = "websocket"
    else:
        scope["type"] = "http"
        scope["method"] = method

    return any(route.matches(scope)[0] is Match.FULL for route in app.routes)


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


def shell_headers(page: str) -> dict[str, str]:
    """
    Read the headers the shell tells htmx to send with every request.

    Args:
        page: A rendered page.

    Returns:
        The parsed ``hx-headers`` object.
    """
    match = re.search(r"hx-headers='([^']*)'", page)
    assert match is not None, "the shell no longer sets hx-headers"
    return dict(json.loads(match.group(1)))


def test_the_shell_carries_the_session_csrf_token(client: TestClient) -> None:
    """
    Without it every mutation the panel offers is refused.

    The token was read off the session payload with an attribute lookup while
    the payload is a mapping, so it was always empty and every restart, delete
    and sign-out came back 403.
    """
    headers = shell_headers(body_of(client, "/apps"))
    assert headers, "the shell no longer sends a CSRF header"
    assert all(headers.values()), "the CSRF token reaching the browser is empty"


def test_the_header_the_shell_sends_is_the_one_the_server_reads(
    app: FastAPI, store: WASMStore
) -> None:
    """
    Spelling the header twice, in two files, is not a thing care can maintain.

    The shell sent ``X-CSRF-Token`` and :mod:`wasm.web.auth` reads
    ``X-WASM-CSRF``, so every mutation a browser made came back 403: restart,
    delete and the sign-out button included. Only the exact headers the page
    hands the browser are used here, so this cannot pass by a test setting the
    right header itself.
    """
    deploy(store)
    browser = TestClient(app, client=("testclient", 50000), follow_redirects=False)
    token = get_token_manager().generate_master_token()
    assert browser.post("/login", data={"token": token}).status_code == 303

    headers = shell_headers(browser.get("/apps").text)

    # Established first, or the test below proves nothing: the mutation has to
    # be one that is genuinely refused without the header.
    assert browser.post("/logout").status_code == 403

    response = browser.post("/logout", headers=headers)
    assert response.status_code == 200, (
        f"the shell sends {sorted(headers)}, which the server does not accept: {response.text}"
    )
    assert response.headers["HX-Redirect"] == "/login"


def test_signing_out_ends_the_session(client: TestClient) -> None:
    """The button in the shell has to actually revoke, and lead somewhere."""
    assert client.get("/apps").status_code == 200

    response = client.post("/logout")
    assert response.status_code == 200
    assert response.headers["HX-Redirect"] == "/login"

    # The cookies go too, so a browser that ignores the redirect is not left
    # holding something that looks like a session.
    cleared = response.headers.get_list("set-cookie")
    assert any(SESSION_COOKIE_NAME in header for header in cleared)

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


# ------------------------------------------------------------------- session


def test_the_sign_in_page_is_reachable_without_a_session(anonymous: TestClient) -> None:
    """It is the one page that must answer an anonymous browser."""
    response = anonymous.get("/login")
    assert response.status_code == 200
    assert "Access token" in response.text


def test_the_form_posts_the_field_the_server_reads(anonymous: TestClient) -> None:
    """
    The page and the handler have to agree on the body.

    The form used to post to a route that did not exist at all; a field name
    that drifts is the same failure one letter smaller, and it is invisible
    until someone tries to sign in.
    """
    page = anonymous.get("/login").text
    assert 'method="post"' in page
    assert 'action="/login"' in page
    assert 'name="token"' in page
    # Nothing is accepted in a URL: a query string is written to browser
    # history, proxy logs and access logs.
    assert 'method="get"' not in page.lower()


def test_a_token_typed_into_the_form_opens_a_session(anonymous: TestClient) -> None:
    """The whole point of the page: type the token, land on the panel."""
    token = get_token_manager().generate_master_token()

    response = anonymous.post("/login", data={"token": token})

    # 303 so the browser follows with GET; a 302 after a POST may repeat the
    # POST, which would replay the credential.
    assert response.status_code == 303, response.text
    assert response.headers["location"] == "/"
    assert SESSION_COOKIE_NAME in anonymous.cookies

    landing = anonymous.get("/")
    assert landing.status_code == 200
    assert MISSING_MARKER not in landing.text


def test_a_token_the_server_does_not_accept_comes_back_to_the_form(
    anonymous: TestClient,
) -> None:
    """A refusal says what happened and how many tries are left, and sets nothing."""
    get_token_manager().generate_master_token()

    response = anonymous.post("/login", data={"token": "not-the-token"})

    assert response.status_code == 401
    assert "attempts remaining" in response.text
    assert 'name="token"' in response.text
    assert SESSION_COOKIE_NAME not in anonymous.cookies
    assert anonymous.get("/").status_code == 303


def test_a_body_with_no_token_at_all_is_refused_rather_than_crashing(
    anonymous: TestClient,
) -> None:
    """The handler parses the body itself, so an empty one has to be survivable."""
    get_token_manager().generate_master_token()
    response = anonymous.post("/login", content=b"", headers={"content-type": "text/plain"})
    assert response.status_code == 401
    assert SESSION_COOKIE_NAME not in anonymous.cookies


def test_the_session_cookie_is_not_readable_by_script(anonymous: TestClient) -> None:
    """The panel is root over the machine; an XSS must not be able to lift it."""
    token = get_token_manager().generate_master_token()
    response = anonymous.post("/login", data={"token": token})

    cookies = " ".join(response.headers.get_list("set-cookie"))
    assert SESSION_COOKIE_NAME in cookies
    assert "HttpOnly" in cookies
    assert "SameSite" in cookies


# ------------------------------------------------------ screens with data in


def test_every_screen_renders_with_the_machine_full(
    client: TestClient, populated: dict[str, Any]
) -> None:
    """
    The counterpart to the empty sweep, and the one that has teeth.

    An empty screen draws a heading and an invitation. Everything that can be
    wrong about a row, an endpoint or a shaped value only exists once there is
    something to draw.
    """
    for path in (*SCREENS, "/apps/broken.example.com"):
        response = client.get(path)
        assert response.status_code == 200, f"{path} answered {response.status_code}"
        assert MISSING_MARKER not in response.text


def test_the_databases_screen_shows_what_the_store_holds(
    client: TestClient, populated: dict[str, Any]
) -> None:
    """
    The screen the suite had never rendered with a row in it.

    Its delete button is the reason: the endpoint lives under the databases
    module's own collection, two segments deep, and the row used to point one
    segment short of it at an address the router answers for with nothing.
    """
    page = body_of(client, "/databases")

    assert "app_production" in page
    assert "cache" in page
    assert "engine postgresql" in page
    assert "/api/databases/databases/postgresql/app_production" in page
    assert "/api/databases/databases/mysql/cache" in page
    # The one-segment address is not merely absent from the routes; it must be
    # absent from the page.
    assert 'hx-delete="/api/databases/app_production"' not in page


def test_the_databases_screen_invites_when_there_is_nothing_on_it(client: TestClient) -> None:
    """An empty screen names the next thing to do and gives the command for it."""
    page = body_of(client, "/databases")
    assert "No databases yet" in page
    assert "wasm db create" in page


def test_the_certificates_screen_admits_when_certbot_cannot_be_asked(
    sandbox: Path, store: WASMStore, config_file: Path, runner
) -> None:
    """
    "Every domain is covered" is the worst possible thing to say here.

    With certbot missing, nothing on this machine can be issued or renewed and
    the panel knows nothing about what is covered. Reporting that as "all
    covered" is a confident answer given at exactly the moment there is none.
    """
    runner.only_knows()
    app = create_app(SecurityConfig(state_dir=sandbox / "state", rate_limit_requests=5000))
    signed_in = TestClient(app, client=("testclient", 50000), follow_redirects=False)
    token = get_token_manager().generate_master_token()
    signed_in.post("/login", data={"token": token})
    deploy(store, domain="bare.example.com", ssl_enabled=False)

    page = signed_in.get("/certificates").text

    assert "certbot is not installed" in page
    assert "Every domain this machine serves is covered" not in page
    assert "cannot say which" in page
    assert MISSING_MARKER not in page


# -------------------------------------------------------------- hostile input


def test_no_screen_lets_markup_out_of_an_attribute_or_a_text_node(
    client: TestClient,
    store: WASMStore,
    sandbox: Path,
    config_file: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Every value on these screens comes off the machine, and the machine lies.

    A domain, a database name, a configuration value and an error message from
    a tool all reach the browser, and any of them can carry markup. An injected
    script in a panel that runs systemd as root is a root shell.
    """
    deploy(store, domain=XSS_DOMAIN)
    store.create_database(Database(name=XSS_DOMAIN, engine="postgresql"))
    Config().set("apps.directory", XSS_ERROR)
    monkeypatch.setattr(
        "wasm.web.jobs.get_job_manager",
        lambda: FakeJobs([make_job("job-x", JobStatus.FAILED, error=XSS_ERROR)]),
    )

    for path in PAGES:
        page = body_of(client, path)
        assert "<script>alert" not in page, f"{path} let a script tag through"
        assert '"><script' not in page, f"{path} let a value close an attribute"
        assert "</script>alert" not in page


def test_the_hostile_values_actually_reached_the_pages(
    client: TestClient, store: WASMStore, config_file: Path
) -> None:
    """
    The escaping test above is worthless if nothing under test was rendered.

    A page that dropped the value entirely would pass it silently.
    """
    deploy(store, domain=XSS_DOMAIN)
    store.create_database(Database(name=XSS_DOMAIN, engine="postgresql"))
    Config().set("apps.directory", XSS_ERROR)

    assert "boom-7" in body_of(client, "/apps")
    assert "boom-7" in body_of(client, "/databases")
    assert "boom-9" in body_of(client, "/settings")


# ------------------------------------------------------------ secrets, again


def test_no_screen_echoes_the_master_token(client: TestClient, populated: dict[str, Any]) -> None:
    """
    The token is root on this machine and belongs only on the terminal that
    printed it.
    """
    token = get_token_manager().generate_master_token()
    for path in SCREENS:
        assert token not in body_of(client, path), f"{path} printed the master token"


def test_no_screen_echoes_the_session_cookie(
    app: FastAPI, store: WASMStore, populated: dict[str, Any]
) -> None:
    """A page that renders its own session cookie hands it to anything that can read the DOM."""
    browser = TestClient(app, client=("testclient", 50000), follow_redirects=False)
    token = get_token_manager().generate_master_token()
    browser.post("/login", data={"token": token})
    cookie = browser.cookies[SESSION_COOKIE_NAME]

    for path in SCREENS:
        assert cookie not in browser.get(path).text, f"{path} printed the session cookie"


def test_the_stored_secret_stays_hidden_on_every_screen(
    client: TestClient, populated: dict[str, Any]
) -> None:
    """Not only on the settings screen: nothing may print it anywhere."""
    for path in SCREENS:
        assert CONFIG_SECRET not in body_of(client, path), f"{path} printed a stored secret"


# ---------------------------------------------------------------- the floor


def test_every_page_can_be_reached_by_keyboard_alone(
    client: TestClient, populated: dict[str, Any]
) -> None:
    """
    Navigation is anchors, so it works with no pointer and with no JavaScript.

    The skip link and the landmark it targets are what make the sidebar
    skippable rather than something to tab through on every screen.
    """
    for path in PAGES:
        if path.startswith("/fragments/"):
            continue
        page = body_of(client, path)
        assert 'href="#main"' in page, f"{path} has no skip link"
        assert 'id="main"' in page, f"{path} has nothing for the skip link to reach"
        assert '<html lang="en"' in page


def test_the_log_drawer_is_operated_by_a_real_button(client: TestClient) -> None:
    """
    It used to be a div with role="button" wrapping two more buttons.

    That is invalid nesting, its keyboard handling was hand-rolled, and its
    label was empty until Alpine had run. A native button is focusable,
    answers to Enter and Space, and is announced without any of it.
    """
    page = body_of(client, "/apps")

    assert 'role="button"' not in page, "a control is imitating a button again"
    assert 'aria-controls="log-drawer-body"' in page
    assert 'id="log-drawer-body"' in page
    assert 'aria-expanded="false"' in page, "the collapsed state is not announced before Alpine"
    assert ">Show</button>" in page, "the toggle has no label until JavaScript runs"


def test_no_row_encodes_its_state_in_colour_alone(
    client: TestClient, populated: dict[str, Any]
) -> None:
    """
    The rail is the panel's signature, and a colour is not a message.

    Every row carries its state in words as well: visibly, as a badge, where
    the record has a state worth naming, and for a screen reader in every case.
    """
    for path in SCREENS:
        page = body_of(client, path)
        fragments = page.split('<div class="row row--')[1:]
        assert fragments or path in ("/settings", "/fragments/machine"), f"{path} drew no rows"
        for fragment in fragments:
            row = fragment.split('<div class="row row--')[0]
            assert "visually-hidden" in row or "badge badge--" in row, (
                f"a row on {path} says its state only in colour"
            )


def test_no_screen_ships_a_control_that_submits_by_accident(
    client: TestClient, populated: dict[str, Any], anonymous: TestClient
) -> None:
    """
    A button with no type is a submit button.

    Every action in the panel is an htmx request, and one of them ending up
    inside a form would fire twice: once through htmx and once as a
    submission. The machine has to be full for this to see anything: the
    action buttons live in rows, and an empty screen has no rows.
    """
    pages = [body_of(client, path) for path in SCREENS]
    pages.append(anonymous.get("/login").text)

    assert sum(page.count("<button") for page in pages) >= 10, "no buttons were examined"

    bare = [found for page in pages for found in re.findall(r"<button(?![^>]*\btype=)[^>]*>", page)]
    assert not bare, f"buttons without an explicit type: {bare}"


# ------------------------------------------------------------------ the seam


def test_the_presentation_layer_never_changes_the_filesystem() -> None:
    """
    A view renders. It does not write, and it does not delete.

    ``--dry-run`` is only honest while every mutation goes through
    :mod:`wasm.core.fs`. A page handler that reaches for ``Path.unlink``
    because it is convenient is how the flag stops being true, and the panel
    is the layer where that temptation looks most harmless.
    """
    import ast

    views = Path(__file__).resolve().parent.parent / "src" / "wasm" / "web" / "views"
    forbidden = {
        "write_text",
        "write_bytes",
        "mkdir",
        "makedirs",
        "unlink",
        "rmtree",
        "copytree",
        "rename",
        "touch",
        "symlink_to",
        "chmod",
    }

    offenders: list[str] = []
    for module in sorted(views.glob("*.py")):
        tree = ast.parse(module.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                name = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
                if name in forbidden or name == "open":
                    offenders.append(f"{module.name}:{node.lineno} {name}")

    assert not offenders, f"the presentation layer is changing the filesystem: {offenders}"
