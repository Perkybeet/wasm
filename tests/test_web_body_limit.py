"""
How much an anonymous client can make the panel read.

Every body used to be read to the end before anything looked at it - JSON
parsing happens before the route's credential dependency runs - so an
unauthenticated client could make a root process buffer as much as it cared
to send. The cap is enforced by the middleware, before authentication and
before routing, from the declared length when there is one and by counting
when the body is chunked.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.test_web_auth import build_client
from wasm.web.auth import MAX_BODY_BYTES, MAX_HOOK_BODY_BYTES
from wasm.web.server import get_token_manager

MIB = 1024 * 1024


def chunks(total: int, size: int = 64 * 1024) -> Iterator[bytes]:
    """
    Yield a body in pieces, so the client sends it chunked with no length.

    Args:
        total: Bytes to send.
        size: Bytes per piece.

    Yields:
        The pieces.
    """
    sent = 0
    while sent < total:
        piece = min(size, total - sent)
        sent += piece
        yield b"x" * piece


def assert_too_large(response) -> None:
    """
    Args:
        response: The answer to an oversized request.
    """
    assert response.status_code == 413, response.text
    body = response.json()
    assert body["error"] == "payload_too_large"
    assert body["detail"]
    assert set(body) >= {"error", "detail", "hint", "fields"}


def test_the_documented_caps() -> None:
    """One mebibyte for the API; the forge hook gets more, and still a limit."""
    assert MAX_BODY_BYTES == 1 * MIB
    assert MAX_HOOK_BODY_BYTES == 5 * MIB


def test_an_oversized_body_is_refused_before_authentication(sandbox: Path) -> None:
    """An anonymous client learns nothing but the limit, and the route never runs."""
    client = build_client(sandbox)

    response = client.post(
        "/api/apps",
        content=b"{" + b" " * MAX_BODY_BYTES + b"}",
        headers={"Content-Type": "application/json"},
    )

    assert_too_large(response)


def test_an_oversized_login_is_refused_and_is_not_a_guess(sandbox: Path) -> None:
    """The cap answers first; nothing reached the credential check to be counted."""
    client = build_client(sandbox, max_failed_attempts=1)
    token = get_token_manager().generate_master_token()

    response = client.post(
        "/api/auth/login",
        content=b'{"token": "' + b"a" * MAX_BODY_BYTES + b'"}',
        headers={"Content-Type": "application/json"},
    )

    assert_too_large(response)
    assert client.post("/api/auth/login", json={"token": token}).status_code == 200


def test_a_chunked_body_with_no_length_is_counted(sandbox: Path) -> None:
    """Leaving out Content-Length does not leave out the limit."""
    client = build_client(sandbox)

    response = client.post(
        "/api/auth/login",
        content=chunks(MAX_BODY_BYTES + 1),
        headers={"Content-Type": "application/json"},
    )

    assert_too_large(response)


def test_a_body_under_the_cap_is_delivered_whole(sandbox: Path) -> None:
    """The middleware reads the body to count it; the route must still get all of it."""
    client = build_client(sandbox)
    token = get_token_manager().generate_master_token()
    padding = " " * (MAX_BODY_BYTES - 200)

    chunked = client.post(
        "/api/auth/login",
        content=iter([b'{"token": "', token.encode(), b'"', padding.encode(), b"}"]),
        headers={"Content-Type": "application/json"},
    )
    declared = client.post(
        "/api/auth/login",
        content=('{"token": "' + token + '"' + padding + "}").encode(),
        headers={"Content-Type": "application/json"},
    )

    assert chunked.status_code == 200, chunked.text
    assert declared.status_code == 200, declared.text


@pytest.mark.parametrize(("size", "refused"), [(2 * MIB, False), (MAX_HOOK_BODY_BYTES + 1, True)])
def test_the_forge_hook_has_a_larger_cap_of_its_own(
    sandbox: Path, size: int, refused: bool
) -> None:
    """
    GitHub sends push payloads of several megabytes; they are not cut at the API's cap.

    Args:
        size: Body size to send.
        refused: Whether it is over the hook's own cap.
    """
    client = build_client(sandbox)

    response = client.post(
        "/hooks/deploy/app.example.com",
        content=b"x" * size,
        headers={"Content-Type": "application/json"},
    )

    if refused:
        assert_too_large(response)
    else:
        # Past the cap check; refused later for having no webhook configured.
        assert response.status_code == 404, response.text


def test_the_cap_is_configurable(sandbox: Path) -> None:
    """A deployment that wants a tighter limit states it once."""
    client = build_client(sandbox, max_body_bytes=1024)

    response = client.post("/api/auth/login", content=b"x" * 2048)

    assert_too_large(response)
