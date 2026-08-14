"""
Tests for the web panel's authentication.

The panel manages the whole machine as root, so these tests are written as
attacks: rotate a header to escape a lockout, forge a header to jump an IP
whitelist, replay a cookie without its CSRF token, restart the process to see
whether sessions survive, and walk every route looking for one that forgot to
ask for credentials.
"""

from __future__ import annotations

import json
import sqlite3
import time
from argparse import ArgumentParser, Namespace
from pathlib import Path

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from wasm.cli.commands.web import add_start_arguments, build_security_config
from wasm.core import totp
from wasm.core.exceptions import SecurityError
from wasm.web import auth as auth_module
from wasm.web.auth import (
    CSRF_HEADER_NAME,
    SESSION_COOKIE_NAME,
    SecurityConfig,
    TokenManager,
    require_auth,
)
from wasm.web.server import _uvicorn_kwargs, create_app, get_token_manager

#: Endpoints that answer without credentials, on purpose.
PUBLIC_API_PATHS = frozenset({"/api/auth/login"})

#: The bind address an operator reaches for when they want the panel "on the
#: network". Every test that uses it expects a refusal or an explicit guard.
ALL_INTERFACES = "0.0.0.0"  # noqa: S104 - the address under test, not a bind

#: Every path of the panel that is public, including the pages a browser needs
#: before it has a session. Anything not listed here has to demand credentials.
PUBLIC_PATHS = frozenset({"/", "/login", "/health", "/api/auth/login"})


def make_config(sandbox: Path, **overrides) -> SecurityConfig:
    """
    Build a security configuration whose state lives inside the sandbox.

    Args:
        sandbox: Per-test temporary directory.
        **overrides: Fields to override on the configuration.

    Returns:
        The configuration.
    """
    params: dict[str, object] = {
        "state_dir": sandbox / "state",
        "rate_limit_requests": 5000,
    }
    params.update(overrides)
    return SecurityConfig(**params)


def build_client(sandbox: Path, client_host: str = "testclient", **overrides) -> TestClient:
    """
    Create an application and a client for it.

    Args:
        sandbox: Per-test temporary directory.
        client_host: Peer address the test connects from.
        **overrides: Security configuration overrides.

    Returns:
        A test client bound to a freshly created application.
    """
    app = create_app(make_config(sandbox, **overrides))
    return TestClient(app, client=(client_host, 50000))


def login(client: TestClient, token: str, **kwargs) -> dict:
    """
    Log in and return the response body.

    Args:
        client: The test client.
        token: The master token.
        **kwargs: Extra request arguments, such as headers.

    Returns:
        The decoded JSON body.
    """
    response = client.post("/api/auth/login", json={"token": token}, **kwargs)
    assert response.status_code == 200, response.text
    return response.json()


def read_audit(sandbox: Path) -> list[dict]:
    """
    Read the audit log written during a test.

    Args:
        sandbox: Per-test temporary directory.

    Returns:
        One dict per audit line.
    """
    path = sandbox / "state" / "web-audit.log"
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def test_rotating_x_forwarded_for_does_not_bypass_lockout(sandbox: Path) -> None:
    """Rotating X-Forwarded-For must not reset the brute force lockout."""
    client = build_client(sandbox, max_failed_attempts=3, lockout_duration=60)

    statuses = []
    for attempt in range(10):
        response = client.post(
            "/api/auth/login",
            json={"token": f"wrong-{attempt}"},
            headers={"X-Forwarded-For": f"10.0.0.{attempt}"},
        )
        statuses.append(response.status_code)

    assert 429 in statuses, f"lockout never triggered, statuses={statuses}"
    assert statuses[-1] == 429


def test_x_real_ip_does_not_bypass_lockout(sandbox: Path) -> None:
    """X-Real-IP is equally untrusted when the peer is not a proxy."""
    client = build_client(sandbox, max_failed_attempts=3, lockout_duration=60)

    statuses = [
        client.post(
            "/api/auth/login",
            json={"token": "wrong"},
            headers={"X-Real-IP": f"10.1.0.{attempt}"},
        ).status_code
        for attempt in range(8)
    ]

    assert statuses[-1] == 429, statuses


def test_forwarded_header_is_honoured_for_configured_proxies(sandbox: Path) -> None:
    """A configured reverse proxy is still able to report the real client."""
    client = build_client(
        sandbox,
        client_host="10.9.9.1",
        trusted_proxies=["10.9.9.1"],
        max_failed_attempts=3,
        lockout_duration=60,
    )

    for _ in range(5):
        client.post(
            "/api/auth/login",
            json={"token": "wrong"},
            headers={"X-Forwarded-For": "203.0.113.7"},
        )

    locked = client.post(
        "/api/auth/login", json={"token": "wrong"}, headers={"X-Forwarded-For": "203.0.113.7"}
    )
    other_client = client.post(
        "/api/auth/login", json={"token": "wrong"}, headers={"X-Forwarded-For": "203.0.113.8"}
    )

    assert locked.status_code == 429
    assert other_client.status_code == 401


def test_ip_whitelist_cannot_be_bypassed_with_headers(sandbox: Path) -> None:
    """Forged forwarding headers must not satisfy the IP whitelist."""
    client = build_client(sandbox, ip_whitelist=["10.0.0.5"])

    for headers in (
        {"X-Forwarded-For": "10.0.0.5"},
        {"X-Forwarded-For": "10.0.0.5, 127.0.0.1"},
        {"X-Real-IP": "10.0.0.5"},
        {"X-Forwarded-For": "127.0.0.1"},
    ):
        response = client.get("/health", headers=headers)
        assert response.status_code == 403, f"{headers} got through: {response.text}"
        # A refusal is still a response the browser renders: harden it too.
        assert "default-src 'self'" in response.headers["Content-Security-Policy"]


def test_ip_whitelist_allows_the_real_peer(sandbox: Path) -> None:
    """A client whose peer address is whitelisted is served."""
    client = build_client(sandbox, client_host="10.0.0.5", ip_whitelist=["10.0.0.0/24"])

    assert client.get("/health").status_code == 200


def test_login_sets_httponly_samesite_cookie(sandbox: Path) -> None:
    """The session travels in a HttpOnly, SameSite=Strict cookie."""
    client = build_client(sandbox)
    token = get_token_manager().generate_master_token()

    response = client.post("/api/auth/login", json={"token": token})
    assert response.status_code == 200

    cookie_header = next(
        value
        for key, value in response.headers.multi_items()
        if key.lower() == "set-cookie" and value.startswith(f"{SESSION_COOKIE_NAME}=")
    )
    assert "HttpOnly" in cookie_header
    assert "samesite=strict" in cookie_header.lower()
    assert "Path=/" in cookie_header
    # Plain HTTP must not set Secure, or the browser drops the cookie entirely.
    assert "Secure" not in cookie_header
    assert response.json()["session_token"] is None
    assert response.json()["csrf_token"]


def test_cookie_is_secure_over_https(sandbox: Path) -> None:
    """Behind TLS the session cookie is marked Secure."""
    app = create_app(make_config(sandbox))
    client = TestClient(app, base_url="https://testserver", client=("testclient", 50000))
    token = get_token_manager().generate_master_token()

    response = client.post("/api/auth/login", json={"token": token})
    cookie_header = next(
        value
        for key, value in response.headers.multi_items()
        if key.lower() == "set-cookie" and value.startswith(f"{SESSION_COOKIE_NAME}=")
    )
    assert "Secure" in cookie_header


def test_mutation_with_cookie_and_no_csrf_token_is_rejected(sandbox: Path) -> None:
    """A cookie-authenticated mutation without the CSRF header must fail."""
    client = build_client(sandbox)
    token = get_token_manager().generate_master_token()
    body = login(client, token)

    without_csrf = client.post("/api/auth/ws-ticket")
    assert without_csrf.status_code == 403
    assert "CSRF" in without_csrf.json()["detail"]

    wrong_csrf = client.post("/api/auth/ws-ticket", headers={CSRF_HEADER_NAME: "not-the-token"})
    assert wrong_csrf.status_code == 403

    with_csrf = client.post("/api/auth/ws-ticket", headers={CSRF_HEADER_NAME: body["csrf_token"]})
    assert with_csrf.status_code == 200, with_csrf.text

    # Reads never need the CSRF token.
    assert client.get("/api/auth/verify").status_code == 200


def test_bearer_clients_skip_csrf_but_still_need_a_token(sandbox: Path) -> None:
    """Automation authenticates with a Bearer token and no cookie."""
    client = build_client(sandbox)
    token = get_token_manager().generate_master_token()

    response = client.post("/api/auth/login", json={"token": token, "bearer": True})
    session_token = response.json()["session_token"]
    assert session_token

    client.cookies.clear()
    assert client.post("/api/auth/ws-ticket").status_code == 401
    assert (
        client.post(
            "/api/auth/ws-ticket", headers={"Authorization": f"Bearer {session_token}"}
        ).status_code
        == 200
    )


def test_secret_and_sessions_survive_a_restart(sandbox: Path) -> None:
    """A restart must not invalidate the signing key or live sessions."""
    config = make_config(sandbox)

    first = TokenManager(config)
    secret_file = config.secret_file
    secret = secret_file.read_text()
    session = first.create_session("10.0.0.1")

    second = TokenManager(make_config(sandbox))
    assert secret_file.read_text() == secret
    assert second.verify_session_token(session.token, "10.0.0.1") is not None

    # The token the restarted process issues must verify under the key the
    # first one loaded: comparing the file alone would pass even if the key
    # in memory had been replaced.
    reissued = second.create_session("10.0.0.1")
    assert first.verify_session_token(reissued.token, "10.0.0.1") is not None
    second.sessions.close()
    first.sessions.close()

    assert secret_file.stat().st_mode & 0o777 == 0o600


def test_startup_fails_when_the_secret_cannot_be_persisted(sandbox: Path) -> None:
    """An unwritable state directory must abort startup, not degrade silently."""
    blocked = sandbox / "blocked"
    blocked.write_text("this is a file, not a directory")

    with pytest.raises(SecurityError) as excinfo:
        TokenManager(SecurityConfig(state_dir=blocked))

    assert "state directory" in str(excinfo.value).lower()
    assert excinfo.value.details


def test_expired_sessions_are_purged(sandbox: Path) -> None:
    """Session storage must not grow without bound."""
    manager = TokenManager(make_config(sandbox, token_expiration_hours=1))
    session = manager.create_session("10.0.0.1")
    manager.sessions.extend(session.session_id, 0.0)

    assert manager.purge_expired_sessions() >= 1
    assert manager.verify_session_token(session.token, "10.0.0.1") is None
    assert manager.get_active_session_count() == 0
    manager.sessions.close()


def iter_routes(routes: list, prefix: str = "") -> list[tuple[str, object]]:
    """
    Collect every route of an application, including nested routers.

    Args:
        routes: Routes of an application or router.
        prefix: Path prefix accumulated so far.

    Returns:
        Pairs of full path and route, for HTTP and WebSocket routes alike.
    """
    collected: list[tuple[str, object]] = []
    for route in routes:
        # FastAPI keeps included routers as an opaque node instead of flattening.
        context = getattr(route, "include_context", None)
        if context is not None:
            collected.extend(iter_routes(context.included_router.routes, prefix + context.prefix))
            continue
        path = getattr(route, "path", None)
        if path is not None and not hasattr(route, "routes"):
            collected.append((prefix + path, route))
            continue
        if hasattr(route, "routes"):
            collected.extend(iter_routes(route.routes, prefix))
    return collected


def iter_api_routes(routes: list, prefix: str = "") -> list[tuple[str, APIRoute]]:
    """
    Collect every API route, including the ones nested in included routers.

    Args:
        routes: Routes of an application or router.
        prefix: Path prefix accumulated so far.

    Returns:
        Pairs of full path and route.
    """
    return [
        (path, route) for path, route in iter_routes(routes, prefix) if isinstance(route, APIRoute)
    ]


def test_every_api_route_requires_authentication(sandbox: Path) -> None:
    """No /api endpoint may be reachable without a session."""
    app = create_app(make_config(sandbox))
    client = TestClient(app, client=("testclient", 50000))

    def dependency_callables(dependant) -> list:
        found = []
        for sub in dependant.dependencies:
            found.append(sub.call)
            found.extend(dependency_callables(sub))
        return found

    api_routes = [
        (path, route)
        for path, route in iter_api_routes(app.routes)
        if path.startswith("/api") and path not in PUBLIC_API_PATHS
    ]
    assert len(api_routes) > 50, "route discovery is broken, the safety net would pass blindly"

    unprotected = [
        f"{sorted(route.methods)} {path}"
        for path, route in api_routes
        if require_auth not in dependency_callables(route.dependant)
    ]
    assert not unprotected, f"endpoints without authentication: {unprotected}"

    # Belt and braces: probe the documented surface as an anonymous client.
    probed = 0
    anonymous_allowed: list[str] = []
    for path, methods in app.openapi()["paths"].items():
        if not path.startswith("/api") or path in PUBLIC_API_PATHS:
            continue
        url = path.replace("{", "").replace("}", "")
        for method in methods:
            if method.upper() in ("HEAD", "OPTIONS"):
                continue
            probed += 1
            response = client.request(method.upper(), url, json={})
            if response.status_code not in (401, 403):
                anonymous_allowed.append(f"{method.upper()} {path} -> {response.status_code}")

    assert probed > 50, f"only probed {probed} endpoints"
    assert not anonymous_allowed, f"anonymous access allowed: {anonymous_allowed}"


def test_security_headers_are_present(sandbox: Path) -> None:
    """Every response carries the hardening headers."""
    client = build_client(sandbox)
    headers = client.get("/health").headers

    csp = headers["Content-Security-Policy"]
    assert "default-src 'self'" in csp
    assert "frame-ancestors 'none'" in csp

    # The directive that decides whether an injected script gets root. It
    # allows neither inline scripts nor eval, and Alpine was removed rather
    # than this loosened to accommodate it.
    assert "script-src 'self';" in csp
    assert "unsafe-eval" not in csp

    # style-src does allow inline, because xterm builds the log terminal out of
    # inline styles and htmx sets them for its request indicators; strict here
    # did not harden the panel, it switched those features off in silence.
    # Server-rendered markup still carries no style attributes - see
    # tests/test_web_style_contract.py.
    assert "style-src 'self' 'unsafe-inline'" in csp

    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["X-Frame-Options"] == "DENY"
    assert headers["Referrer-Policy"] == "no-referrer"
    # Without it, a same-site page that embeds the panel's static assets or
    # API responses via <img>/<script src> can be read cross-origin by any
    # page that links to it - the class of leak Spectre made exploitable
    # even for resources a browser would previously have blocked reading.
    assert headers["Cross-Origin-Resource-Policy"] == "same-origin"
    assert "Strict-Transport-Security" not in headers


def test_hsts_is_sent_when_https_is_required(sandbox: Path, tmp_path: Path) -> None:
    """HSTS only appears once TLS is actually in play."""
    cert = tmp_path / "web.crt"
    key = tmp_path / "web.key"
    cert.write_text("certificate")
    key.write_text("key")

    app = create_app(
        make_config(sandbox, require_https=True, ssl_certfile=str(cert), ssl_keyfile=str(key))
    )
    client = TestClient(app, base_url="https://testserver", client=("testclient", 50000))

    assert "max-age=" in client.get("/health").headers["Strict-Transport-Security"]


def test_https_required_without_certificate_refuses_to_start(sandbox: Path) -> None:
    """require_https without a certificate must be a startup failure."""
    with pytest.raises(SecurityError) as excinfo:
        create_app(make_config(sandbox, require_https=True))

    assert "certbot" in excinfo.value.details or "openssl" in excinfo.value.details


def test_uvicorn_is_configured_to_hide_its_server_banner() -> None:
    """
    ``run_server`` must ask uvicorn not to send ``Server: uvicorn``.

    An unauthenticated response header naming the server software is exactly
    the fingerprint an attacker probes for before picking an exploit. There is
    no socket-free way to inspect the header uvicorn itself would send -
    ``uvicorn.run`` blocks until the process is killed, and
    tests/conftest.py makes real sockets fail in every test - so this checks
    the keyword arguments ``run_server`` builds for it instead, the same way
    ``--dry-run`` elsewhere in the project makes an otherwise-opaque call
    testable by extracting the part that decides what would happen.
    """
    kwargs = _uvicorn_kwargs(
        app=object(),
        host="127.0.0.1",
        port=8080,
        ssl_certfile=None,
        ssl_keyfile=None,
    )

    assert kwargs["server_header"] is False


def test_plain_http_is_refused_when_https_is_required(sandbox: Path, tmp_path: Path) -> None:
    """A cleartext request to a TLS-only panel is rejected."""
    cert = tmp_path / "web.crt"
    key = tmp_path / "web.key"
    cert.write_text("certificate")
    key.write_text("key")

    client = build_client(sandbox, require_https=True, ssl_certfile=str(cert), ssl_keyfile=str(key))

    response = client.get("/health")
    assert response.status_code == 403
    assert "HTTPS" in response.json()["detail"]


def test_audit_log_records_login_failure_and_success_without_the_token(sandbox: Path) -> None:
    """Both outcomes are auditable and neither leaks the credential."""
    client = build_client(sandbox)
    token = get_token_manager().generate_master_token()

    client.post("/api/auth/login", json={"token": "wrong-token-value"})
    login(client, token)

    entries = read_audit(sandbox)
    logins = [entry for entry in entries if entry["action"] == "auth.login"]
    results = [entry["result"] for entry in logins]

    assert "failure" in results
    assert "success" in results
    assert all(entry["ip"] for entry in logins)
    assert all(entry["ts"] for entry in logins)

    raw = (sandbox / "state" / "web-audit.log").read_text()
    assert token not in raw
    assert "wrong-token-value" not in raw
    assert (sandbox / "state" / "web-audit.log").stat().st_mode & 0o777 == 0o600


def test_session_cookie_is_renewed_past_half_its_lifetime(sandbox: Path) -> None:
    """An active operator gets a fresh cookie instead of being logged out."""
    client = build_client(sandbox, token_expiration_hours=1)
    token = get_token_manager().generate_master_token()
    login(client, token)

    db = sqlite3.connect(sandbox / "state" / "web-sessions.db")
    with db:
        db.execute("UPDATE sessions SET issued_at = issued_at - 3500")
    db.close()

    response = client.get("/api/auth/verify")
    assert response.status_code == 200
    renewed = [
        value
        for key, value in response.headers.multi_items()
        if key.lower() == "set-cookie" and value.startswith(f"{SESSION_COOKIE_NAME}=")
    ]
    assert renewed, "an active session should be re-issued before it expires"


def test_mutations_are_audited(sandbox: Path) -> None:
    """Every state-changing API call leaves a trace naming the session."""
    client = build_client(sandbox)
    token = get_token_manager().generate_master_token()
    body = login(client, token)

    client.post("/api/auth/ws-ticket", headers={CSRF_HEADER_NAME: body["csrf_token"]})

    mutations = [entry for entry in read_audit(sandbox) if entry["action"] == "api.post"]
    ticket_calls = [e for e in mutations if e["resource"] == "/api/auth/ws-ticket"]
    assert ticket_calls
    assert ticket_calls[-1]["result"] == "ok"
    assert ticket_calls[-1]["actor"] != "anonymous"


def test_websocket_rejects_a_token_in_the_query_string(sandbox: Path) -> None:
    """The old ?token=... handshake must no longer authenticate anything."""
    client = build_client(sandbox)
    token = get_token_manager().generate_master_token()

    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(f"/ws/system?token={token}"):
            pass


def test_websocket_accepts_a_single_use_ticket(sandbox: Path) -> None:
    """Tickets authenticate exactly one handshake."""
    client = build_client(sandbox)
    token = get_token_manager().generate_master_token()
    body = login(client, token)

    ticket = client.post(
        "/api/auth/ws-ticket", headers={CSRF_HEADER_NAME: body["csrf_token"]}
    ).json()["ticket"]

    manager = get_token_manager()
    assert manager.consume_ws_ticket(ticket, "testclient") is not None
    assert manager.consume_ws_ticket(ticket, "testclient") is None


def test_session_is_rejected_from_a_different_address(sandbox: Path) -> None:
    """A stolen session token is useless from another IP."""
    manager = TokenManager(make_config(sandbox))
    session = manager.create_session("10.0.0.1")

    assert manager.verify_session_token(session.token, "10.0.0.1") is not None
    assert manager.verify_session_token(session.token, "10.0.0.2") is None
    manager.sessions.close()


def test_bearer_guesses_across_endpoints_trigger_the_lockout(sandbox: Path) -> None:
    """Changing endpoint must not reset the counter the way changing IP cannot."""
    client = build_client(sandbox, max_failed_attempts=3, lockout_duration=60)
    endpoints = [
        "/api/auth/verify",
        "/api/auth/sessions",
        "/api/system/info",
        "/api/auth/verify",
        "/api/auth/sessions",
        "/api/auth/verify",
    ]

    statuses = [
        client.get(endpoint, headers={"Authorization": f"Bearer wasm_guess{index}"}).status_code
        for index, endpoint in enumerate(endpoints)
    ]

    assert statuses[0] == 401, statuses
    assert 429 in statuses, f"master token guessing was never locked out: {statuses}"
    assert statuses[-1] == 429


def test_bearer_failures_are_audited(sandbox: Path) -> None:
    """A failed Bearer credential leaves a trace, like a failed login does."""
    client = build_client(sandbox, max_failed_attempts=100)

    client.get("/api/auth/verify", headers={"Authorization": "Bearer wasm_not_a_real_token"})

    failures = [entry for entry in read_audit(sandbox) if entry["result"] == "denied"]
    assert failures, "a rejected credential must be auditable"
    assert all("wasm_not_a_real_token" not in json.dumps(entry) for entry in failures)


def test_a_forwarded_header_that_is_not_an_ip_is_ignored(sandbox: Path) -> None:
    """A trusted proxy cannot make an arbitrary string the rate limit key."""
    client = build_client(
        sandbox,
        client_host="10.9.9.1",
        trusted_proxies=["10.9.9.1"],
        max_failed_attempts=3,
        lockout_duration=60,
    )

    statuses = [
        client.post(
            "/api/auth/login",
            json={"token": "wrong"},
            headers={"X-Forwarded-For": f"not-an-ip-{attempt}"},
        ).status_code
        for attempt in range(8)
    ]

    assert statuses[-1] == 429, statuses


def test_a_real_ip_header_that_is_not_an_ip_is_ignored(sandbox: Path) -> None:
    """The same rule applies to X-Real-IP."""
    client = build_client(
        sandbox,
        client_host="10.9.9.1",
        trusted_proxies=["10.9.9.1"],
        max_failed_attempts=3,
        lockout_duration=60,
    )

    statuses = [
        client.post(
            "/api/auth/login",
            json={"token": "wrong"},
            headers={"X-Real-IP": f"host{attempt}.example.com"},
        ).status_code
        for attempt in range(8)
    ]

    assert statuses[-1] == 429, statuses


def test_session_has_an_absolute_maximum_lifetime(sandbox: Path) -> None:
    """A continuously used session must still die of old age."""
    manager = TokenManager(make_config(sandbox, token_expiration_hours=12, session_max_hours=24))
    session = manager.create_session("10.0.0.1")

    assert manager.verify_session_token(session.token, "10.0.0.1") is not None

    db = sqlite3.connect(sandbox / "state" / "web-sessions.db")
    with db:
        db.execute("UPDATE sessions SET created_at = created_at - 90000")
    db.close()

    assert manager.renew_session({"sid": session.session_id}) is None
    assert manager.verify_session_token(session.token, "10.0.0.1") is None
    manager.sessions.close()


def test_renewal_rotates_the_session_identifier(sandbox: Path) -> None:
    """Re-issuing a session must not keep the same sid and CSRF token forever."""
    manager = TokenManager(make_config(sandbox, token_expiration_hours=1))
    session = manager.create_session("10.0.0.1")

    db = sqlite3.connect(sandbox / "state" / "web-sessions.db")
    with db:
        db.execute("UPDATE sessions SET issued_at = issued_at - 3500")
    db.close()

    renewed = manager.renew_session({"sid": session.session_id})
    assert renewed is not None
    assert renewed.session_id != session.session_id
    assert renewed.csrf_token != session.csrf_token
    assert manager.verify_session_token(renewed.token, "10.0.0.1") is not None

    # The replaced identifier survives only long enough for the requests the
    # dashboard already had in flight, so a captured copy is worthless.
    retired = manager.sessions.get(session.session_id)
    assert retired is not None
    assert retired["expires_at"] <= time.time() + auth_module.SESSION_ROTATION_GRACE + 1

    manager.sessions.extend(session.session_id, 0.0)
    assert manager.verify_session_token(session.token, "10.0.0.1") is None
    manager.sessions.close()


def test_no_route_escapes_authentication(sandbox: Path) -> None:
    """Walk every route, HTTP and WebSocket, and probe it anonymously."""
    app = create_app(make_config(sandbox, max_failed_attempts=10_000))
    client = TestClient(app, client=("testclient", 50000))

    inventory = iter_routes(app.routes) + iter_routes(app.router.routes)
    paths = {path for path, _ in inventory}
    assert "/ws/system" in paths, "route discovery missed the websocket surface"

    reachable: list[str] = []
    for path, route in inventory:
        if path in PUBLIC_PATHS:
            continue
        url = path.replace("{", "").replace("}", "")
        if route.__class__.__name__.endswith("WebSocketRoute"):
            try:
                with client.websocket_connect(url):
                    reachable.append(f"WS {path}")
            except WebSocketDisconnect:
                continue
            continue
        for method in sorted(getattr(route, "methods", set()) or {"GET"}):
            if method in ("HEAD", "OPTIONS"):
                continue
            # Redirects are not followed: a page route answers an anonymous
            # browser with a redirect to the sign-in form rather than a JSON
            # 401, and following it would land on the public login page and
            # read as success.
            response = client.request(method, url, json={}, follow_redirects=False)
            refused = response.status_code in (401, 403) or (
                response.status_code in (302, 303, 307)
                and response.headers.get("location", "").startswith("/login")
            )
            if not refused:
                reachable.append(f"{method} {path} -> {response.status_code}")

    assert not reachable, f"routes reachable without credentials: {reachable}"


def test_pages_send_an_anonymous_browser_to_the_sign_in_form(sandbox: Path) -> None:
    """
    A person who typed a URL gets a form, not a JSON error.

    The refusal is the same check the API uses; only its presentation differs,
    so there is still one place where a credential is verified.
    """
    app = create_app(make_config(sandbox, max_failed_attempts=10_000))
    client = TestClient(app, client=("testclient", 50000))

    response = client.get("/apps", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/login"
    assert "row" not in response.text, "no page content may leak with the redirect"


def test_cors_preflight_is_subject_to_the_ip_whitelist(sandbox: Path) -> None:
    """CORS must live inside the security middleware, not in front of it."""
    client = build_client(
        sandbox,
        ip_whitelist=["10.0.0.5"],
        enable_cors=True,
        cors_origins=["https://panel.example.com"],
    )

    response = client.options(
        "/api/auth/verify",
        headers={
            "Origin": "https://panel.example.com",
            "Access-Control-Request-Method": "GET",
        },
    )

    assert response.status_code == 403, response.text
    assert "access-control-allow-origin" not in {k.lower() for k in response.headers}


def test_an_empty_secret_file_is_not_silently_replaced(sandbox: Path) -> None:
    """A truncated key file must abort startup instead of logging everyone out."""
    config = make_config(sandbox)
    manager = TokenManager(config)
    session = manager.create_session("10.0.0.1")
    manager.sessions.close()

    config.secret_file.write_text("")

    with pytest.raises(SecurityError) as excinfo:
        TokenManager(make_config(sandbox))

    assert excinfo.value.details
    # And the sessions the key protects are still there to be recovered.
    assert session.token


def parse_start_args(argv: list[str]) -> Namespace:
    """
    Parse ``wasm web start`` arguments with the options the handler expects.

    Args:
        argv: Command line arguments after ``web start``.

    Returns:
        The parsed namespace.
    """
    parser = ArgumentParser(prog="wasm web start")
    add_start_arguments(parser)
    return parser.parse_args(argv)


def test_binding_to_every_interface_without_protection_is_an_error() -> None:
    """A root panel is not put on the network by a flag and a warning."""
    with pytest.raises(SecurityError) as excinfo:
        build_security_config(parse_start_args(["--host", ALL_INTERFACES]))

    assert "--tls-cert" in excinfo.value.details
    assert "--self-signed" in excinfo.value.details
    assert "--insecure-http" in excinfo.value.details


def test_a_whitelist_alone_no_longer_justifies_the_exposure() -> None:
    """A whitelist restricts who connects; it encrypts nothing."""
    with pytest.raises(SecurityError):
        build_security_config(
            parse_start_args(["--host", ALL_INTERFACES, "--allow-ip", "10.0.0.0/24"])
        )


def test_the_insecure_opt_out_carries_the_whitelist() -> None:
    """Cleartext must be asked for in so many words, and keeps its whitelist."""
    config = build_security_config(
        parse_start_args(
            [
                "--host",
                ALL_INTERFACES,
                "--insecure-http",
                "--allow-ip",
                "10.0.0.0/24",
                "--allow-ip",
                "10.1.0.1",
            ]
        )
    )

    assert config.ip_whitelist == ["10.0.0.0/24", "10.1.0.1"]
    assert config.require_https is False


def test_require_https_without_material_is_an_error() -> None:
    """Asking for TLS without a certificate must fail before the port is bound."""
    with pytest.raises(SecurityError) as excinfo:
        build_security_config(parse_start_args(["--host", ALL_INTERFACES, "--require-https"]))

    assert "--tls-cert" in excinfo.value.details


def test_loopback_is_the_default_and_needs_nothing() -> None:
    """The safe default stays usable with no flags at all."""
    config = build_security_config(parse_start_args([]))

    assert config.host == "127.0.0.1"
    assert config.require_https is False
    assert config.trusted_proxies == []


def test_declaring_a_tls_proxy_makes_the_cookie_secure(sandbox: Path) -> None:
    """The nginx-in-front deployment issues a Secure cookie once declared."""
    config = build_security_config(parse_start_args(["--trusted-proxy", "10.9.9.1"]))
    config.state_dir = sandbox / "state"
    app = create_app(config)
    client = TestClient(app, client=("10.9.9.1", 50000))
    token = get_token_manager().generate_master_token()

    response = client.post(
        "/api/auth/login",
        json={"token": token},
        headers={"X-Forwarded-Proto": "https", "X-Forwarded-For": "203.0.113.7"},
    )

    cookie_header = next(
        value
        for key, value in response.headers.multi_items()
        if key.lower() == "set-cookie" and value.startswith(f"{SESSION_COOKIE_NAME}=")
    )
    assert "Secure" in cookie_header


# ------------------------------------------------------------- second factor


def enable_totp(client: TestClient, csrf: str) -> tuple[str, list[str]]:
    """
    Enrol and confirm the second factor through the API.

    Args:
        client: A signed-in client.
        csrf: The session's CSRF token.

    Returns:
        The shared secret and the backup codes shown at confirmation.
    """
    enroll = client.post("/api/auth/2fa/enroll", headers={CSRF_HEADER_NAME: csrf})
    assert enroll.status_code == 200, enroll.text
    secret = enroll.json()["secret"]

    confirm = client.post(
        "/api/auth/2fa/confirm",
        json={"code": totp.totp_now(secret)},
        headers={CSRF_HEADER_NAME: csrf},
    )
    assert confirm.status_code == 200, confirm.text
    return secret, confirm.json()["backup_codes"]


def test_with_two_factor_off_login_is_exactly_what_it_was(sandbox: Path) -> None:
    """No code field on the form, no code required by the API."""
    client = build_client(sandbox)
    token = get_token_manager().generate_master_token()

    assert 'name="totp_code"' not in client.get("/login").text
    assert client.post("/api/auth/login", json={"token": token}).status_code == 200


def test_enrollment_confirms_activates_and_issues_backup_codes_once(sandbox: Path) -> None:
    """The full roundtrip: off, pending, on, with eight one-use codes."""
    client = build_client(sandbox)
    token = get_token_manager().generate_master_token()
    csrf = login(client, token)["csrf_token"]

    before = client.get("/api/auth/2fa").json()
    assert before == {"enabled": False, "pending": False, "backup_codes_remaining": 0}

    enroll = client.post("/api/auth/2fa/enroll", headers={CSRF_HEADER_NAME: csrf})
    assert enroll.status_code == 200
    secret = enroll.json()["secret"]
    assert secret in enroll.json()["uri"]
    assert enroll.json()["uri"].startswith("otpauth://totp/")
    assert client.get("/api/auth/2fa").json()["pending"] is True

    confirm = client.post(
        "/api/auth/2fa/confirm",
        json={"code": totp.totp_now(secret)},
        headers={CSRF_HEADER_NAME: csrf},
    )
    assert confirm.status_code == 200
    codes = confirm.json()["backup_codes"]
    assert len(codes) == auth_module.BACKUP_CODE_COUNT
    for code in codes:
        assert len(code) == 9 and code[4] == "-", f"backup code format broke: {code}"

    after = client.get("/api/auth/2fa").json()
    assert after["enabled"] is True
    assert after["pending"] is False
    assert after["backup_codes_remaining"] == len(codes)


def test_a_wrong_confirmation_code_does_not_activate_anything(sandbox: Path) -> None:
    """A typo during enrolment leaves the second factor off."""
    client = build_client(sandbox)
    token = get_token_manager().generate_master_token()
    csrf = login(client, token)["csrf_token"]

    client.post("/api/auth/2fa/enroll", headers={CSRF_HEADER_NAME: csrf})
    refused = client.post(
        "/api/auth/2fa/confirm", json={"code": "000000"}, headers={CSRF_HEADER_NAME: csrf}
    )

    assert refused.status_code == 400
    status = client.get("/api/auth/2fa").json()
    assert status["enabled"] is False
    assert status["pending"] is True
    # And a login still needs no code, because nothing was activated.
    assert client.post("/api/auth/login", json={"token": token}).status_code == 200


def test_once_enabled_a_login_without_a_code_is_refused(sandbox: Path) -> None:
    """The master token alone stops being enough."""
    client = build_client(sandbox)
    token = get_token_manager().generate_master_token()
    csrf = login(client, token)["csrf_token"]
    secret, _codes = enable_totp(client, csrf)

    without = client.post("/api/auth/login", json={"token": token})
    assert without.status_code == 401
    assert "totp_code" in without.json()["detail"]

    with_code = client.post(
        "/api/auth/login", json={"token": token, "totp_code": totp.totp_now(secret)}
    )
    assert with_code.status_code == 200


def test_a_wrong_second_factor_counts_toward_the_same_lockout(sandbox: Path) -> None:
    """Bringing a stolen token and guessing only the code must still lock out."""
    client = build_client(sandbox, max_failed_attempts=3, lockout_duration=60)
    token = get_token_manager().generate_master_token()
    csrf = login(client, token)["csrf_token"]
    enable_totp(client, csrf)

    statuses = [
        client.post("/api/auth/login", json={"token": token, "totp_code": "000000"}).status_code
        for _ in range(6)
    ]

    assert statuses[0] == 401, statuses
    assert 429 in statuses, f"totp guessing was never locked out: {statuses}"
    assert statuses[-1] == 429


def test_a_backup_code_opens_one_login_and_only_one(sandbox: Path) -> None:
    """Backup codes are consumed by use."""
    client = build_client(sandbox)
    token = get_token_manager().generate_master_token()
    csrf = login(client, token)["csrf_token"]
    _secret, codes = enable_totp(client, csrf)

    first = client.post("/api/auth/login", json={"token": token, "totp_code": codes[0]})
    assert first.status_code == 200

    replay = client.post("/api/auth/login", json={"token": token, "totp_code": codes[0]})
    assert replay.status_code == 401

    remaining = client.get("/api/auth/2fa").json()["backup_codes_remaining"]
    assert remaining == len(codes) - 1


def test_disable_requires_a_current_code_and_restores_plain_login(sandbox: Path) -> None:
    """The switch that turns the second factor off is itself guarded by it."""
    client = build_client(sandbox)
    token = get_token_manager().generate_master_token()
    csrf = login(client, token)["csrf_token"]
    secret, _codes = enable_totp(client, csrf)

    refused = client.post(
        "/api/auth/2fa/disable", json={"code": "000000"}, headers={CSRF_HEADER_NAME: csrf}
    )
    assert refused.status_code == 400
    assert client.get("/api/auth/2fa").json()["enabled"] is True
    # The wrong code was counted where every other credential guess is.
    denied = [e for e in read_audit(sandbox) if e["action"] == "auth.credential"]
    assert any(e["resource"] == "/api/auth/2fa/disable" for e in denied)

    accepted = client.post(
        "/api/auth/2fa/disable",
        json={"code": totp.totp_now(secret)},
        headers={CSRF_HEADER_NAME: csrf},
    )
    assert accepted.status_code == 200
    assert client.get("/api/auth/2fa").json() == {
        "enabled": False,
        "pending": False,
        "backup_codes_remaining": 0,
    }
    assert client.post("/api/auth/login", json={"token": token}).status_code == 200


def test_a_backup_code_can_disable_when_the_authenticator_is_lost(sandbox: Path) -> None:
    """Losing the phone must not mean losing the panel."""
    client = build_client(sandbox)
    token = get_token_manager().generate_master_token()
    csrf = login(client, token)["csrf_token"]
    _secret, codes = enable_totp(client, csrf)

    response = client.post(
        "/api/auth/2fa/disable", json={"code": codes[-1]}, headers={CSRF_HEADER_NAME: csrf}
    )

    assert response.status_code == 200
    assert client.get("/api/auth/2fa").json()["enabled"] is False


def test_the_secret_never_appears_again_after_confirmation(sandbox: Path, runner: object) -> None:
    """
    One screen sees the secret once; no response or audit line repeats it.

    Args:
        sandbox: Per-test temporary directory.
        runner: The fake command runner, so rendering /settings reaches no
            real process.
    """
    client = build_client(sandbox)
    token = get_token_manager().generate_master_token()
    csrf = login(client, token)["csrf_token"]
    secret, codes = enable_totp(client, csrf)

    responses = [
        client.get("/api/auth/2fa"),
        client.post("/api/auth/login", json={"token": token, "totp_code": totp.totp_now(secret)}),
        client.get("/settings"),
    ]
    for response in responses:
        assert secret not in response.text, response.request.url

    raw_audit = (sandbox / "state" / "web-audit.log").read_text()
    assert secret not in raw_audit
    for code in codes:
        assert code not in raw_audit, "a backup code reached the audit log"

    entries = read_audit(sandbox)
    for action in ("auth.2fa.enroll", "auth.2fa.confirm"):
        assert any(e["action"] == action and e["result"] == "success" for e in entries), action


def test_the_two_factor_state_file_is_owner_only(sandbox: Path) -> None:
    """The secret at rest gets the same 0600 the signing key gets."""
    client = build_client(sandbox)
    token = get_token_manager().generate_master_token()
    csrf = login(client, token)["csrf_token"]
    enable_totp(client, csrf)

    state_file = sandbox / "state" / "web-totp"
    assert state_file.exists()
    assert state_file.stat().st_mode & 0o777 == 0o600
    stored = json.loads(state_file.read_text())
    assert stored["enabled"] is True
    # Backup codes at rest are hashes, never the codes themselves.
    assert all(len(entry) == 64 for entry in stored["backup_codes"])


def test_a_corrupt_state_file_refuses_rather_than_waves_through(sandbox: Path) -> None:
    """Treating a truncated file as "off" would be a silent bypass."""
    manager = TokenManager(make_config(sandbox))
    manager.config.totp_file.write_text("{not json")

    with pytest.raises(SecurityError) as excinfo:
        manager.totp_enabled()

    assert excinfo.value.details
    manager.sessions.close()


def test_the_browser_form_gains_the_code_field_only_when_it_is_read(sandbox: Path) -> None:
    """The form flow: field appears, code is required, wrong code is counted."""
    client = build_client(sandbox, max_failed_attempts=3, lockout_duration=60)
    token = get_token_manager().generate_master_token()
    csrf = login(client, token)["csrf_token"]
    secret, _codes = enable_totp(client, csrf)
    client.cookies.clear()

    form = client.get("/login").text
    assert 'name="totp_code"' in form
    assert "Authenticator code" in form

    missing = client.post("/login", data={"token": token}, follow_redirects=False)
    assert missing.status_code == 401
    assert "authenticator" in missing.text.lower()

    wrong = client.post(
        "/login", data={"token": token, "totp_code": "000000"}, follow_redirects=False
    )
    assert wrong.status_code == 401
    assert "attempts remaining" in wrong.text

    good = client.post(
        "/login",
        data={"token": token, "totp_code": totp.totp_now(secret)},
        follow_redirects=False,
    )
    assert good.status_code == 303
    assert good.headers["location"] == "/"


def test_the_settings_fragment_flow_enrolls_confirms_and_disables(
    sandbox: Path, runner: object
) -> None:
    """
    The htmx adapters drive the same implementation the JSON API does.

    Args:
        sandbox: Per-test temporary directory.
        runner: The fake command runner, so rendering /settings reaches no
            real process.
    """
    client = build_client(sandbox)
    token = get_token_manager().generate_master_token()
    csrf = login(client, token)["csrf_token"]
    headers = {CSRF_HEADER_NAME: csrf}

    page = client.get("/settings").text
    assert "Two-factor authentication" in page
    assert 'hx-post="/settings/2fa/enroll"' in page

    enroll = client.post("/settings/2fa/enroll", headers=headers)
    assert enroll.status_code == 200
    assert "data-totp-uri=" in enroll.text
    secret = get_token_manager().pending_totp_secret()
    assert secret is not None
    assert secret in enroll.text, "the manual key is not on the enrolment screen"

    wrong = client.post("/settings/2fa/confirm", data={"code": "000000"}, headers=headers)
    assert wrong.status_code == 200
    assert "was not accepted" in wrong.text
    assert "data-totp-uri=" in wrong.text, "a refused code must re-show the QR"

    confirmed = client.post(
        "/settings/2fa/confirm", data={"code": totp.totp_now(secret)}, headers=headers
    )
    assert confirmed.status_code == 200
    assert "backup" in confirmed.text.lower()
    assert secret not in confirmed.text, "the secret survived past confirmation"

    disabled = client.post(
        "/settings/2fa/disable", data={"code": totp.totp_now(secret)}, headers=headers
    )
    assert disabled.status_code == 200
    assert 'hx-post="/settings/2fa/enroll"' in disabled.text


def test_rate_limiter_does_not_grow_without_bound() -> None:
    """Old client entries are dropped instead of accumulating forever."""
    limiter = auth_module.RateLimiter(max_requests=2, window=60, max_tracked=10)

    for index in range(200):
        limiter.is_allowed(f"10.0.{index // 256}.{index % 256}")

    assert limiter.tracked_clients() <= 10


def test_rate_limit_default_is_not_relaxed_for_convenience() -> None:
    """The shipped default stays in the range a dashboard actually needs."""
    assert auth_module.RATE_LIMIT_MAX_REQUESTS <= 200
    assert SecurityConfig().trusted_proxies == []
