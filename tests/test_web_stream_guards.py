"""
Streams that outlive the reason they were allowed.

A WebSocket or an event stream is authenticated once, at the handshake, and
then stays open for hours. Three things used to follow from that: the log
stream followed whichever unit its path named - the root journal of sshd
included - for any valid credential; one credential could open sockets, and
``journalctl -f`` processes, without limit; and revoking a token, rotating the
master token or signing out left every stream already open with it running.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from tests.test_web_auth import build_client, issue_token, login
from tests.test_web_websockets import connect_code, token_subprotocols
from wasm.managers.service_manager import WASM_UNIT_MARKER, ServiceManager
from wasm.web import events as events_module
from wasm.web.auth import (
    CSRF_HEADER_NAME,
    WS_CLOSE_RATE_LIMITED,
    WS_CLOSE_UNAUTHORIZED,
    credential_is_current,
)
from wasm.web.server import get_token_manager

# The package re-exports the APIRouter under the module's own name, so the
# module is fetched from sys.modules to patch its constants.
ws_module = importlib.import_module("wasm.web.websockets.router")


@pytest.fixture
def fast_recheck(monkeypatch: pytest.MonkeyPatch) -> None:
    """Re-check credentials every few milliseconds instead of every thirty seconds."""
    monkeypatch.setattr(ws_module, "WS_RECHECK_SECONDS", 0.02)
    monkeypatch.setattr(events_module, "CREDENTIAL_RECHECK_SECONDS", 0.02)


def close_code_after(ws: Any, seconds: float = 5.0) -> int | None:
    """
    Read from a socket until the server closes it.

    Bounded by time, not by frames: a fast machine answers hundreds of pings before a
    deadline of a tenth of a second has passed, which made a frame budget flaky.

    Args:
        ws: An open test WebSocket.
        seconds: How long to wait for the server to close at most.

    Returns:
        The close code, or None when the server never closed in time.
    """
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            ws.send_json({"type": "ping"})
            ws.receive_json()
        except WebSocketDisconnect as exc:
            return exc.code
    return None


# ------------------------------------------------------------ unit ownership


@pytest.fixture
def unit_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """
    Point the service manager at a private unit directory.

    Returns:
        The directory WASM's units live in for the test.
    """
    directory = tmp_path / "units"
    directory.mkdir()
    monkeypatch.setattr(ServiceManager, "SYSTEMD_DIR", directory)
    monkeypatch.setattr(ServiceManager, "UNIT_SEARCH_DIRS", (directory,))
    return directory


@pytest.fixture
def spawned(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, ...]]:
    """
    Stand in for journalctl: record the argv, stream one line, then end.

    Returns:
        Every argv the log stream tried to spawn.
    """
    calls: list[tuple[str, ...]] = []

    class FakeProcess:
        """A journal follow that has one line to say."""

        def __init__(self) -> None:
            self.returncode: int | None = None
            self.stdout = asyncio.StreamReader()
            self.stdout.feed_data(b"2026-09-26T10:00:00 host app[1]: hello\n")
            self.stderr = asyncio.StreamReader()
            self.stderr.feed_eof()

        def terminate(self) -> None:
            self.returncode = 0

        def kill(self) -> None:
            self.returncode = -9

        async def wait(self) -> int:
            return 0

    async def fake_exec(*argv: str, **_kwargs: Any) -> FakeProcess:
        calls.append(tuple(argv))
        return FakeProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")
    return calls


def test_the_log_stream_refuses_a_unit_wasm_does_not_manage(
    sandbox: Path, runner: object, unit_dir: Path, spawned: list[tuple[str, ...]]
) -> None:
    """
    ``/ws/logs/ssh`` used to follow sshd's journal as root for any credential.

    The same ownership rule ``GET /api/services/{name}/logs`` goes through
    applies: a unit WASM did not create is not WASM's to show.
    """
    (unit_dir / "ssh.service").write_text("[Service]\nExecStart=/usr/sbin/sshd -D\n")
    client = build_client(sandbox)
    token = get_token_manager().generate_master_token()

    with client.websocket_connect("/ws/logs/ssh", subprotocols=token_subprotocols(token)) as ws:
        message = ws.receive_json()

    assert message["type"] == "error"
    assert "does not manage" in message["message"]
    assert spawned == []


def test_the_log_stream_refuses_a_unit_that_does_not_exist(
    sandbox: Path, runner: object, unit_dir: Path, spawned: list[tuple[str, ...]]
) -> None:
    """Nothing to follow is not a reason to follow whatever systemd resolves the name to."""
    client = build_client(sandbox)
    token = get_token_manager().generate_master_token()

    with client.websocket_connect(
        "/ws/logs/nothing.example.com", subprotocols=token_subprotocols(token)
    ) as ws:
        message = ws.receive_json()

    assert message["type"] == "error"
    assert spawned == []


def test_the_log_stream_follows_a_unit_wasm_manages(
    sandbox: Path, runner: object, unit_dir: Path, spawned: list[tuple[str, ...]]
) -> None:
    """An application's own unit still streams, which is what the log tab is for."""
    (unit_dir / "app-example-com.service").write_text(
        f"# {WASM_UNIT_MARKER}\n[Service]\nExecStart=/usr/bin/node server.js\n"
    )
    client = build_client(sandbox)
    token = get_token_manager().generate_master_token()

    with client.websocket_connect(
        "/ws/logs/app.example.com", subprotocols=token_subprotocols(token)
    ) as ws:
        connected = ws.receive_json()
        line = ws.receive_json()

    assert connected == {
        "type": "connected",
        "domain": "app.example.com",
        "service": "app-example-com",
    }
    assert line["type"] == "log"
    assert spawned and spawned[0][:3] == ("journalctl", "-u", "app-example-com.service")


# ------------------------------------------------------ one budget per credential


def test_one_credential_cannot_open_sockets_without_limit(sandbox: Path) -> None:
    """
    Every log socket is a ``journalctl -f`` running as root.

    A credential gets a fixed number of concurrent sockets; the next one is
    refused at the handshake, and closing one frees its place.
    """
    client = build_client(sandbox, ws_max_per_credential=2)
    token = get_token_manager().generate_master_token()
    protocols = token_subprotocols(token)

    with contextlib.ExitStack() as stack:
        for _ in range(2):
            ws = stack.enter_context(client.websocket_connect("/ws/jobs", subprotocols=protocols))
            assert ws.receive_json()["type"] == "connected"

        assert connect_code(client, "/ws/jobs", subprotocols=protocols) == WS_CLOSE_RATE_LIMITED

    with client.websocket_connect("/ws/jobs", subprotocols=protocols) as ws:
        assert ws.receive_json()["type"] == "connected"


def test_the_budget_is_per_credential(sandbox: Path) -> None:
    """One script at its limit does not lock the operator's console out of streaming."""
    client = build_client(sandbox, ws_max_per_credential=1)
    master = get_token_manager().generate_master_token()
    csrf = login(client, master)["csrf_token"]
    reader = issue_token(client, csrf, master, name="dashboard", scope="read")["token"]
    client.cookies.clear()

    with client.websocket_connect("/ws/jobs", subprotocols=token_subprotocols(master)) as ws:
        assert ws.receive_json()["type"] == "connected"
        with client.websocket_connect("/ws/jobs", subprotocols=token_subprotocols(reader)) as other:
            assert other.receive_json()["type"] == "connected"


# --------------------------------------------------- credentials re-checked


def test_revoking_an_api_token_closes_its_open_sockets(sandbox: Path, fast_recheck: None) -> None:
    """``wasm token revoke`` has to end what the token already opened, not only what it opens next."""
    client = build_client(sandbox)
    master = get_token_manager().generate_master_token()
    csrf = login(client, master)["csrf_token"]
    issued = issue_token(client, csrf, master, name="dashboard", scope="read")
    client.cookies.clear()

    with client.websocket_connect(
        "/ws/jobs", subprotocols=token_subprotocols(issued["token"])
    ) as ws:
        assert ws.receive_json()["type"] == "connected"
        get_token_manager().revoke_api_token(int(issued["id"]))
        code = close_code_after(ws)

    assert code == WS_CLOSE_UNAUTHORIZED


def test_rotating_the_master_token_closes_the_sockets_it_opened(
    sandbox: Path, fast_recheck: None
) -> None:
    """A leaked master token is retired with ``wasm web token --new``, streams included."""
    client = build_client(sandbox)
    token = get_token_manager().generate_master_token()

    with client.websocket_connect("/ws/jobs", subprotocols=token_subprotocols(token)) as ws:
        assert ws.receive_json()["type"] == "connected"
        get_token_manager().generate_master_token()
        code = close_code_after(ws)

    assert code == WS_CLOSE_UNAUTHORIZED


def test_signing_out_closes_a_socket_opened_with_the_session_s_ticket(
    sandbox: Path, fast_recheck: None
) -> None:
    """The console's own path: a ticket redeems as the session, and dies with it."""
    client = build_client(sandbox)
    token = get_token_manager().generate_master_token()
    csrf = login(client, token)["csrf_token"]
    ticket = client.post("/api/auth/ws-ticket", headers={CSRF_HEADER_NAME: csrf}).json()["ticket"]
    browser = TestClient(client.app, client=("testclient", 50000))

    with browser.websocket_connect(f"/ws/jobs?ticket={ticket}") as ws:
        assert ws.receive_json()["type"] == "connected"
        assert client.post("/api/auth/logout", headers={CSRF_HEADER_NAME: csrf}).status_code == 200
        code = close_code_after(ws)

    assert code == WS_CLOSE_UNAUTHORIZED


def test_a_renewed_session_keeps_its_open_sockets(sandbox: Path, fast_recheck: None) -> None:
    """
    Renewal replaces the session id; the sign-in is the same one.

    Closing with 4401 here would sign the operator out of a console they are
    actively using, every time their session renews.
    """
    client = build_client(sandbox, token_expiration_hours=1)
    token = get_token_manager().generate_master_token()
    csrf = login(client, token)["csrf_token"]
    ticket = client.post("/api/auth/ws-ticket", headers={CSRF_HEADER_NAME: csrf}).json()["ticket"]
    browser = TestClient(client.app, client=("testclient", 50000))
    manager = get_token_manager()

    with browser.websocket_connect(f"/ws/jobs?ticket={ticket}") as ws:
        assert ws.receive_json()["type"] == "connected"
        (old_sid,) = [row["sid"] for row in manager.sessions.list_active()]
        manager.sessions._conn.execute(
            "UPDATE sessions SET issued_at = issued_at - 3500 WHERE sid = ?", (old_sid,)
        )
        manager.sessions._conn.commit()
        renewed = manager.renew_session({"sid": old_sid})
        assert renewed is not None
        manager.sessions.extend(old_sid, 0.0)

        for _ in range(20):
            ws.send_json({"type": "ping"})
            assert ws.receive_json() == {"type": "pong"}


def test_a_socket_does_not_outlive_its_maximum_lifetime(
    sandbox: Path, monkeypatch: pytest.MonkeyPatch, fast_recheck: None
) -> None:
    """A token that never expires still does not hold one socket open forever."""
    monkeypatch.setattr(ws_module, "WS_MAX_LIFETIME_SECONDS", 0.1)
    client = build_client(sandbox)
    token = get_token_manager().generate_master_token()

    with client.websocket_connect("/ws/jobs", subprotocols=token_subprotocols(token)) as ws:
        assert ws.receive_json()["type"] == "connected"
        code = close_code_after(ws)

    assert code == ws_module.WS_CLOSE_LIFETIME
    # Not 4401: the console reads that as "signed out" and would not reconnect.
    assert code != WS_CLOSE_UNAUTHORIZED


# ----------------------------------------------------------------- the event stream


class ConnectedRequest:
    """A request whose client never goes away; see tests/test_web_events.py."""

    async def is_disconnected(self) -> bool:
        """
        Returns:
            Always False.
        """
        return False


def test_the_event_stream_ends_when_its_credential_is_revoked(
    sandbox: Path, fast_recheck: None
) -> None:
    """``/events`` re-checks on the same cadence and stops feeding a revoked token."""
    client = build_client(sandbox)
    master = get_token_manager().generate_master_token()
    csrf = login(client, master)["csrf_token"]
    issued = issue_token(client, csrf, master, name="wallboard", scope="read")
    payload = get_token_manager().verify_api_token(issued["token"], "testclient")
    assert payload is not None and credential_is_current(payload)

    async def exercise() -> list[str]:
        stream = events_module._stream(ConnectedRequest(), payload)
        frames = [await stream.__anext__()]
        get_token_manager().revoke_api_token(int(issued["id"]))
        with contextlib.suppress(StopAsyncIteration):
            while True:
                frames.append(await asyncio.wait_for(stream.__anext__(), timeout=5))
        return frames

    frames = asyncio.run(exercise())

    assert frames[0].startswith(":")
    assert not credential_is_current(payload)


def test_a_live_credential_keeps_the_event_stream_open(sandbox: Path, fast_recheck: None) -> None:
    """The re-check must not end a stream whose credential is fine."""
    build_client(sandbox)
    master = get_token_manager().generate_master_token()

    from wasm.web.auth import check_credential

    payload = check_credential(master, "testclient")
    assert payload is not None

    async def exercise() -> str:
        stream = events_module._stream(ConnectedRequest(), payload)
        await stream.__anext__()
        try:
            # Many re-check periods; nothing else is due in this window.
            await asyncio.wait_for(stream.__anext__(), timeout=0.3)
        except asyncio.TimeoutError:
            return "open"
        except StopAsyncIteration:
            return "ended"
        return "frame"

    assert asyncio.run(exercise()) in ("open", "frame")
