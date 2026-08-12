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
from pathlib import Path

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from wasm.core.exceptions import SecurityError
from wasm.web import auth as auth_module
from wasm.web.auth import (
    CSRF_HEADER_NAME,
    SESSION_COOKIE_NAME,
    SecurityConfig,
    TokenManager,
    require_auth,
)
from wasm.web.server import create_app, get_token_manager

#: Endpoints that answer without credentials, on purpose.
PUBLIC_API_PATHS = frozenset({"/api/auth/login"})


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
    first.sessions.close()

    second = TokenManager(make_config(sandbox))
    assert secret_file.read_text() == secret
    assert second.verify_session_token(session.token, "10.0.0.1") is not None
    second.sessions.close()

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


def iter_api_routes(routes: list, prefix: str = "") -> list[tuple[str, APIRoute]]:
    """
    Collect every API route, including the ones nested in included routers.

    Args:
        routes: Routes of an application or router.
        prefix: Path prefix accumulated so far.

    Returns:
        Pairs of full path and route.
    """
    collected: list[tuple[str, APIRoute]] = []
    for route in routes:
        if isinstance(route, APIRoute):
            collected.append((prefix + route.path, route))
            continue
        # FastAPI keeps included routers as an opaque node instead of flattening.
        context = getattr(route, "include_context", None)
        if context is not None:
            collected.extend(
                iter_api_routes(context.included_router.routes, prefix + context.prefix)
            )
        elif hasattr(route, "routes"):
            collected.extend(iter_api_routes(route.routes, prefix))
    return collected


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
    assert "'unsafe-inline'" not in csp
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["X-Frame-Options"] == "DENY"
    assert headers["Referrer-Policy"] == "no-referrer"
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
