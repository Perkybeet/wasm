"""
Tests for the WebSocket surface of the web panel.

``/ws/*`` streams the root journal and the machine's metrics, so it is exactly
as privileged as the REST API and has to be defended by the same middleware.
These tests are written as attacks against the handshake: connect from an
address that is not whitelisted, guess the master token forever, flood the
handshake to fill the audit log, and open a socket from a sibling subdomain the
way a cross-site WebSocket hijack would.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from starlette.websockets import WebSocketDisconnect

from tests.test_web_auth import build_client, iter_routes, login, make_config
from wasm.web.auth import (
    CSRF_HEADER_NAME,
    WS_CLOSE_FORBIDDEN,
    WS_CLOSE_RATE_LIMITED,
    WS_CLOSE_UNAUTHORIZED,
    AuditLogger,
)
from wasm.web.server import create_app, get_token_manager
from wasm.web.websockets.router import WS_SUBPROTOCOL, WS_TOKEN_PREFIX


def token_subprotocols(token: str) -> list[str]:
    """
    Build the subprotocol list that carries a credential.

    Args:
        token: The credential to present.

    Returns:
        The value of ``Sec-WebSocket-Protocol``, split into entries.
    """
    return [WS_SUBPROTOCOL, f"{WS_TOKEN_PREFIX}{token}"]


def connect_code(client, url: str, **kwargs) -> int | None:
    """
    Try to open a WebSocket and report the close code of a refusal.

    Args:
        client: The test client.
        url: The WebSocket path.
        **kwargs: Extra arguments for ``websocket_connect``.

    Returns:
        The close code, or None when the handshake was accepted.
    """
    try:
        with client.websocket_connect(url, **kwargs):
            return None
    except WebSocketDisconnect as exc:
        return exc.code


def test_websocket_from_a_non_whitelisted_ip_is_rejected(sandbox: Path) -> None:
    """The IP whitelist has to cover the WebSocket surface, not only HTTP."""
    client = build_client(sandbox, client_host="10.0.0.9", ip_whitelist=["10.0.0.5"])
    token = get_token_manager().generate_master_token()

    code = connect_code(client, "/ws/system", subprotocols=token_subprotocols(token))

    assert code == WS_CLOSE_FORBIDDEN, "a valid credential must not defeat the IP whitelist"


def test_websocket_from_a_whitelisted_ip_is_served(sandbox: Path) -> None:
    """The whitelist must not break the legitimate operator."""
    client = build_client(sandbox, client_host="10.0.0.5", ip_whitelist=["10.0.0.0/24"])
    token = get_token_manager().generate_master_token()

    with client.websocket_connect("/ws/system", subprotocols=token_subprotocols(token)) as ws:
        assert ws.receive_json()["type"] == "connected"


def test_websocket_master_token_guesses_trigger_the_lockout(sandbox: Path) -> None:
    """The handshake must not be an unlimited, unlogged guessing oracle."""
    client = build_client(sandbox, max_failed_attempts=3, lockout_duration=60)

    codes = [
        connect_code(client, "/ws/system", subprotocols=token_subprotocols(f"wasm_guess{index}"))
        for index in range(6)
    ]

    assert codes[0] == WS_CLOSE_UNAUTHORIZED, codes
    assert WS_CLOSE_RATE_LIMITED in codes, f"guessing was never locked out: {codes}"
    assert codes[-1] == WS_CLOSE_RATE_LIMITED


def test_websocket_failures_count_towards_the_http_lockout(sandbox: Path) -> None:
    """One counter for every credential channel: WebSocket failures lock HTTP."""
    client = build_client(sandbox, max_failed_attempts=3, lockout_duration=60)

    for index in range(3):
        connect_code(client, "/ws/system", subprotocols=token_subprotocols(f"wasm_guess{index}"))

    response = client.post("/api/auth/login", json={"token": "wasm_guess"})
    assert response.status_code == 429, response.text


def test_websocket_handshakes_are_rate_limited(sandbox: Path) -> None:
    """An anonymous client must not be able to hammer the handshake."""
    client = build_client(
        sandbox,
        rate_limit_requests=4,
        rate_limit_window=60,
        max_failed_attempts=1000,
    )

    codes = [connect_code(client, "/ws/system") for _ in range(8)]

    assert codes[-1] == WS_CLOSE_RATE_LIMITED, codes


def test_cross_site_origin_is_rejected(sandbox: Path) -> None:
    """A sibling subdomain is same-site, so SameSite alone does not protect us."""
    client = build_client(sandbox)
    token = get_token_manager().generate_master_token()

    code = connect_code(
        client,
        "/ws/system",
        subprotocols=token_subprotocols(token),
        headers={"Origin": "https://evil.testserver"},
    )

    assert code == WS_CLOSE_FORBIDDEN


def test_same_origin_handshake_is_accepted(sandbox: Path) -> None:
    """The panel's own page must still be able to open a socket."""
    client = build_client(sandbox)
    token = get_token_manager().generate_master_token()

    with client.websocket_connect(
        "/ws/system",
        subprotocols=token_subprotocols(token),
        headers={"Origin": "http://testserver"},
    ) as ws:
        assert ws.receive_json()["type"] == "connected"


def test_unauthenticated_handshake_is_refused_on_every_websocket_route(sandbox: Path) -> None:
    """No WebSocket route may serve an anonymous client."""
    app = create_app(make_config(sandbox, max_failed_attempts=10_000))
    from fastapi.testclient import TestClient

    client = TestClient(app, client=("testclient", 50000))

    websocket_paths = [
        path
        for path, route in iter_routes(app.routes)
        if route.__class__.__name__.endswith("WebSocketRoute")
    ]
    assert len(websocket_paths) >= 5, f"route discovery is broken: {websocket_paths}"

    served = []
    for path in websocket_paths:
        url = path.replace("{", "").replace("}", "")
        if connect_code(client, url) is None:
            served.append(path)

    assert not served, f"websocket routes reachable without credentials: {served}"


def test_rejected_handshakes_cannot_fill_the_disk(sandbox: Path) -> None:
    """The audit log is bounded: a flood of denials rotates instead of growing."""
    path = sandbox / "audit.log"
    audit = AuditLogger(path, enabled=True, max_bytes=2048, backups=1)

    for index in range(500):
        audit.record(
            action="ws.connect",
            result="denied",
            client_ip=f"10.0.0.{index % 256}",
            resource="/ws/system",
            detail="no valid credential",
        )

    files = list(sandbox.glob("audit.log*"))
    total = sum(item.stat().st_size for item in files)

    assert len(files) <= 2, [item.name for item in files]
    assert total <= 2048 * 3, total
    # The most recent events survive the rotation.
    assert "10.0.0." in path.read_text()


def test_forwarded_header_from_a_trusted_proxy_reaches_the_websocket(sandbox: Path) -> None:
    """A declared proxy can still identify the real client on a handshake."""
    client = build_client(
        sandbox,
        client_host="10.9.9.1",
        trusted_proxies=["10.9.9.1"],
        ip_whitelist=["203.0.113.7"],
    )
    token = get_token_manager().generate_master_token()

    allowed = connect_code(
        client,
        "/ws/system",
        subprotocols=token_subprotocols(token),
        headers={"X-Forwarded-For": "203.0.113.7"},
    )
    denied = connect_code(
        client,
        "/ws/system",
        subprotocols=token_subprotocols(token),
        headers={"X-Forwarded-For": "203.0.113.8"},
    )

    assert allowed is None
    assert denied == WS_CLOSE_FORBIDDEN


def test_websocket_requires_https_when_configured(sandbox: Path, tmp_path: Path) -> None:
    """A cleartext handshake against a TLS-only panel is refused."""
    cert = tmp_path / "web.crt"
    key = tmp_path / "web.key"
    cert.write_text("certificate")
    key.write_text("key")

    client = build_client(sandbox, require_https=True, ssl_certfile=str(cert), ssl_keyfile=str(key))
    token = get_token_manager().generate_master_token()

    code = connect_code(client, "/ws/system", subprotocols=token_subprotocols(token))

    assert code == WS_CLOSE_FORBIDDEN


def test_log_stream_refuses_a_domain_that_is_not_a_unit_name(sandbox: Path) -> None:
    """A domain that cannot be a systemd unit never reaches journalctl."""
    client = build_client(sandbox)
    token = get_token_manager().generate_master_token()

    # '*' is a journalctl unit pattern, not a domain: it must never be spawned.
    with client.websocket_connect("/ws/logs/*", subprotocols=token_subprotocols(token)) as ws:
        message = ws.receive_json()

    assert message["type"] == "error"
    assert "invalid domain" in message["message"].lower()


def test_ticket_authenticates_exactly_one_handshake(sandbox: Path) -> None:
    """The browser path: a ticket opens one socket and is then spent."""
    client = build_client(sandbox)
    token = get_token_manager().generate_master_token()
    body = login(client, token)
    ticket = client.post(
        "/api/auth/ws-ticket", headers={CSRF_HEADER_NAME: body["csrf_token"]}
    ).json()["ticket"]

    client.cookies.clear()
    with client.websocket_connect(f"/ws/system?ticket={ticket}") as ws:
        assert ws.receive_json()["type"] == "connected"

    assert connect_code(client, f"/ws/system?ticket={ticket}") == WS_CLOSE_UNAUTHORIZED


@pytest.mark.parametrize("path", ["/ws/system", "/ws/events", "/ws/jobs"])
def test_master_token_subprotocol_is_accepted(sandbox: Path, path: str) -> None:
    """Automation without a cookie jar authenticates with the subprotocol."""
    client = build_client(sandbox)
    token = get_token_manager().generate_master_token()

    with client.websocket_connect(path, subprotocols=token_subprotocols(token)) as ws:
        assert ws.receive_json()["type"] == "connected"
