"""
Tests for how the server hands out the console.

The console is a static Vite build committed to ``src/wasm/web/static``; the
server renders no pages. What is defended here is the seam between the two:

- **Every console address answers with the console.** A reload on a deep link
  is a GET the server has never heard of, and the client router can only draw
  it if the server returns ``index.html`` for it.
- **No machine path is ever shadowed.** A script probing ``/api/nope`` must
  get a JSON error, not an HTML page with a 200 that reads as success.
- **Caching is right in both directions.** Hashed assets are immutable for a
  year; ``index.html`` is never stored, because it names the current build's
  assets and a cached copy would pin a browser to files that no longer exist.
- **The policy allows no inline code.** The strict CSP is the reason the
  console exists in this shape.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from wasm.web import server as server_module
from wasm.web.auth import SecurityConfig
from wasm.web.server import (
    CONTENT_SECURITY_POLICY,
    STATIC_DIR,
    create_app,
    is_machine_path,
)

#: The policy the plan fixes, character for character.
STRICT_POLICY = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "font-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; "
    "form-action 'self'; object-src 'none'"
)


@pytest.fixture
def app(tmp_path: Path, runner: object) -> FastAPI:
    """
    Build the panel with its state in a temporary directory.

    Args:
        tmp_path: Per-test temporary directory.
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
        A client with no session: the console itself needs none.
    """
    return TestClient(app, client=("testclient", 50000), follow_redirects=False)


def a_built_asset(suffix: str) -> str:
    """
    Name one file of the committed build.

    Args:
        suffix: The extension wanted, such as ``.js``.

    Returns:
        The file name under ``static/assets``.
    """
    return next((STATIC_DIR / "assets").glob(f"*{suffix}")).name


# ---------------------------------------------------------------------------
# Console addresses
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "/",
        "/apps",
        "/apps/example.com/deployments",
        "/settings/security",
        "/login",
        "/login?next=/apps&reason=expired",
        "/a/path/the/console/does/not/know",
        # Only whole segments are machine prefixes.
        "/apis",
        "/assetsx",
    ],
)
def test_client_routes_get_the_console(client: TestClient, path: str) -> None:
    """
    Args:
        client: An anonymous client.
        path: A console address.
    """
    response = client.get(path)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert '<div id="root">' in response.text
    assert response.headers["cache-control"] == "no-store"


def test_the_console_answers_head_too(client: TestClient) -> None:
    """Uptime probes and ``curl -I`` send HEAD, and a 405 reads as broken."""
    response = client.head("/apps")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")


def test_the_console_is_the_built_index(client: TestClient) -> None:
    """What is served is the committed build, byte for byte."""
    response = client.get("/")

    assert response.content == (STATIC_DIR / "index.html").read_bytes()


def test_a_root_file_of_the_build_is_served_as_itself(client: TestClient) -> None:
    """The favicon lives at the root, where the built index.html points."""
    response = client.get("/favicon.svg")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("image/svg+xml")
    assert response.content == (STATIC_DIR / "favicon.svg").read_bytes()


@pytest.mark.parametrize("path", ["/..%2Fserver.py", "/%2e%2e/server.py", "/assets/../server.py"])
def test_no_path_reaches_outside_the_build(client: TestClient, path: str) -> None:
    """
    A root file is looked up by name in a fixed set, never joined onto a path.

    Args:
        client: An anonymous client.
        path: A traversal attempt.
    """
    response = client.get(path)

    assert "def create_app" not in response.text


def test_there_is_no_form_sign_in_any_more(client: TestClient) -> None:
    """
    Sign-in is ``POST /api/auth/login``; the form route died with the pages.

    A second sign-in implementation is a second lockout to keep in step.
    """
    assert client.post("/login", data={"token": "x"}).status_code == 405


def test_a_missing_build_explains_itself(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    A package built without the console must still say what is wrong.

    Args:
        client: An anonymous client.
        tmp_path: Per-test temporary directory.
        monkeypatch: Points the index at a file that does not exist.
    """
    monkeypatch.setattr(server_module, "INDEX_HTML", tmp_path / "missing.html")

    response = client.get("/apps")

    assert response.status_code == 503
    assert "npm run build" in response.text
    assert "<script" not in response.text


# ---------------------------------------------------------------------------
# Machine paths
# ---------------------------------------------------------------------------


def test_machine_paths_are_never_shadowed(client: TestClient) -> None:
    """An unknown API address is a JSON error in the contract, not a page."""
    response = client.get("/api/does-not-exist")

    assert response.status_code in (401, 404)
    assert response.headers["content-type"].startswith("application/json")
    assert set(response.json()) >= {"error", "detail"}


@pytest.mark.parametrize(
    "path", ["/assets/missing.js", "/events/nope", "/ws/nope", "/hooks", "/health/nope"]
)
def test_every_machine_prefix_answers_json_when_nothing_matches(
    client: TestClient, path: str
) -> None:
    """
    Args:
        client: An anonymous client.
        path: An address under a machine prefix that no route answers.
    """
    response = client.get(path)

    assert response.status_code in (404, 405)
    assert response.headers["content-type"].startswith("application/json")
    assert '<div id="root">' not in response.text


def test_a_missing_asset_is_not_cached(client: TestClient) -> None:
    """A 404 cached for a year would outlive the deploy that ships the file."""
    response = client.get("/assets/missing.js")

    assert response.status_code == 404
    assert response.headers["cache-control"] == "no-store"


def test_the_health_probe_is_not_the_console(client: TestClient) -> None:
    """A load balancer reads this body; HTML here would be a false healthy."""
    response = client.get("/health")

    assert response.json() == {"status": "healthy", "service": "wasm-web"}


def test_the_event_stream_demands_a_session(client: TestClient) -> None:
    """
    The feed names every application and what is happening to it.

    It answers the way the API does, a 401, and never a redirect to a sign-in
    page: an EventSource cannot follow one to anything useful.
    """
    response = client.get("/events")

    assert response.status_code == 401
    assert "location" not in response.headers


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/api", True),
        ("/api/apps", True),
        ("/assets/index.js", True),
        ("/events", True),
        ("/ws/logs/example.com", True),
        ("/hooks/example.com", True),
        ("/health", True),
        ("/", False),
        ("/apps", False),
        ("/apis", False),
        ("/assetsx/a.js", False),
        ("/healthz", False),
    ],
)
def test_machine_prefixes_match_whole_segments(path: str, expected: bool) -> None:
    """
    Args:
        path: A request path.
        expected: Whether it belongs to a machine.
    """
    assert is_machine_path(path) is expected


# ---------------------------------------------------------------------------
# Caching
# ---------------------------------------------------------------------------


def test_hashed_assets_are_cached_forever(client: TestClient) -> None:
    """Vite names every asset by its content, so a stale one cannot exist."""
    asset = a_built_asset(".js")

    response = client.get(f"/assets/{asset}")

    assert response.status_code == 200
    assert "immutable" in response.headers["cache-control"]
    assert "javascript" in response.headers["content-type"]


def test_the_stylesheet_is_an_asset_too(client: TestClient) -> None:
    """Styles live in a file: the policy forbids them inline."""
    response = client.get(f"/assets/{a_built_asset('.css')}")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/css")
    assert "immutable" in response.headers["cache-control"]


# ---------------------------------------------------------------------------
# Content Security Policy
# ---------------------------------------------------------------------------


def test_the_policy_allows_no_inline_code(client: TestClient) -> None:
    """No inline script, no inline style, no third-party origin."""
    csp = client.get("/").headers["content-security-policy"]

    assert "'unsafe-inline'" not in csp
    assert "'unsafe-eval'" not in csp
    assert "script-src 'self';" in csp
    assert "style-src 'self';" in csp


def test_the_policy_is_the_one_the_plan_fixes(client: TestClient) -> None:
    """
    Character for character, plus Trusted Types when the E2E proved it safe.

    ``connect-src`` is ``'self'`` alone: CSP Level 3 matches the page's own
    ``ws:``/``wss:`` against it, so the old ``ws: wss:`` - any host - is gone.
    """
    csp = client.get("/apps").headers["content-security-policy"]

    assert csp == CONTENT_SECURITY_POLICY
    assert csp.startswith(STRICT_POLICY)
    assert " ws:" not in csp and " wss:" not in csp


def test_dom_sinks_require_trusted_types(client: TestClient) -> None:
    """
    No string reaches innerHTML, eval or a script URL unless a policy made it.

    Kept because the whole E2E suite (``panel/e2e``: every page, both themes,
    the phone viewport) runs clean with it: React, TanStack and Base UI write
    the DOM through properties, never through a string sink. A dependency that
    starts needing one fails the E2E with the violation and its source file.
    """
    csp = client.get("/").headers["content-security-policy"]

    assert csp.endswith("; require-trusted-types-for 'script'")


def test_the_policy_covers_the_api_and_the_assets_too(client: TestClient) -> None:
    """A policy on the document alone would leave a sniffed JSON body open."""
    for path in ("/health", f"/assets/{a_built_asset('.js')}", "/api/does-not-exist"):
        assert client.get(path).headers["content-security-policy"] == CONTENT_SECURITY_POLICY


def test_the_built_index_carries_no_inline_code() -> None:
    """
    The policy would block an inline script or style, silently in production.

    Vite can inline a module preload polyfill or a small stylesheet; the build
    is configured not to, and this is what notices if that ever changes.
    """
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")

    assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>", html), "an inline <script>"
    assert "<style" not in html, "an inline <style>"
    assert not re.search(r"\sstyle=", html), "a style attribute"
    assert not re.search(r"\son[a-z]+=", html), "an inline event handler"


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------


def test_loading_the_console_does_not_spend_the_rate_budget(tmp_path: Path, runner: object) -> None:
    """
    The console loads some forty hashed chunks per page load.

    Counted against the per-client limit, a couple of hard reloads would lock
    the operator out of a blank page; the API behind it must still count.

    Args:
        tmp_path: Per-test temporary directory.
        runner: The fake command runner.
    """
    app = create_app(SecurityConfig(state_dir=tmp_path / "state", rate_limit_requests=5))
    client = TestClient(app, client=("testclient", 50000), follow_redirects=False)
    asset = f"/assets/{a_built_asset('.js')}"

    for _ in range(20):
        assert client.get(asset).status_code == 200

    statuses = [client.get("/health").status_code for _ in range(6)]
    assert statuses[:5] == [200] * 5
    assert statuses[5] == 429
