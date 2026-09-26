"""
Credentials that work more often than they should.

Three gaps of the same kind: a second-factor code that could be used again
inside its own thirty seconds, a lockout that counted guesses on channels it
never refused (the session cookie, the session endpoint), and a session that
could renew itself into several live successors at once during the grace a
rotation leaves its predecessor. Each test here uses one credential the
number of times it is supposed to be usable, and then once more.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.test_web_auth import build_client, enable_totp, login, make_config
from wasm.core import totp
from wasm.web import auth as auth_module
from wasm.web.auth import CSRF_HEADER_NAME, SESSION_COOKIE_NAME, TokenManager
from wasm.web.server import get_brute_force, get_token_manager

# ------------------------------------------------------------ TOTP replay


def test_a_totp_code_opens_one_login_and_not_a_second(sandbox: Path) -> None:
    """RFC 6238 5.2: a verifier must not accept the same code twice."""
    client = build_client(sandbox)
    token = get_token_manager().generate_master_token()
    csrf = login(client, token)["csrf_token"]
    secret, _codes = enable_totp(client, csrf, token)
    code = totp.totp_now(secret)

    first = client.post("/api/auth/login", json={"token": token, "totp_code": code})
    replay = client.post("/api/auth/login", json={"token": token, "totp_code": code})

    assert first.status_code == 200, first.text
    assert replay.status_code == 401, replay.text
    assert replay.json()["error"] == "invalid_totp"


def test_an_elevation_code_cannot_be_replayed(sandbox: Path) -> None:
    """Sudo mode is the same proof of presence, with the same one-use rule."""
    client = build_client(sandbox)
    token = get_token_manager().generate_master_token()
    csrf = login(client, token)["csrf_token"]
    secret, _codes = enable_totp(client, csrf, token)
    code = totp.totp_now(secret)
    headers = {CSRF_HEADER_NAME: csrf}

    first = client.post("/api/auth/elevate", json={"code": code}, headers=headers)
    replay = client.post("/api/auth/elevate", json={"code": code}, headers=headers)

    assert first.status_code == 200, first.text
    assert replay.status_code == 401, replay.text


def test_one_code_may_sign_in_and_then_confirm_once_each(sandbox: Path) -> None:
    """
    Reuse is refused per purpose, not across them.

    Signing in and immediately confirming a destructive action is the normal
    path; making the operator wait up to thirty seconds between the two would
    buy nothing, since each is still a one-time use of the code.
    """
    client = build_client(sandbox)
    token = get_token_manager().generate_master_token()
    csrf = login(client, token)["csrf_token"]
    secret, _codes = enable_totp(client, csrf, token)
    code = totp.totp_now(secret)

    signed_in = client.post("/api/auth/login", json={"token": token, "totp_code": code})
    assert signed_in.status_code == 200, signed_in.text
    csrf = signed_in.json()["csrf_token"]

    elevated = client.post(
        "/api/auth/elevate", json={"code": code}, headers={CSRF_HEADER_NAME: csrf}
    )
    assert elevated.status_code == 200, elevated.text


def test_an_older_code_is_refused_once_a_newer_one_was_accepted(
    sandbox: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    The drift window accepts the previous step, but not after the current one.

    Otherwise a code shoulder-surfed thirty seconds ago would still open a
    login that the operator already made with a fresh one.
    """
    manager = TokenManager(make_config(sandbox))
    secret = manager.begin_totp_enrollment()
    now = 1_900_000_000.0
    monkeypatch.setattr(auth_module, "_now", lambda: now)
    assert manager.confirm_totp_enrollment(totp.totp_now(secret, t=now)) is not None

    previous = totp.totp_now(secret, t=now - totp.PERIOD)
    current = totp.totp_now(secret, t=now)

    assert manager.verify_second_factor(current, purpose="login") is True
    assert manager.verify_second_factor(previous, purpose="login") is False
    # The previous step is still fresh for a purpose that has not used any.
    assert manager.verify_second_factor(previous, purpose="elevate") is True
    manager.sessions.close()


def test_the_matched_step_is_reported() -> None:
    """The step is what replay protection remembers, so it has to be exact."""
    secret = totp.generate_secret()
    now = 1_900_000_000.0
    step = int(now // totp.PERIOD)

    assert totp.matched_step(secret, totp.totp_now(secret, t=now), t=now) == step
    assert totp.matched_step(secret, totp.totp_now(secret, t=now - totp.PERIOD), t=now) == step - 1
    assert totp.matched_step(secret, "not-a-code", t=now) is None


# ---------------------------------------------------------------- lockout


def test_a_locked_out_address_is_refused_with_a_valid_session_cookie(sandbox: Path) -> None:
    """
    The cookie is a channel like any other.

    It used to be exempt so an attacker could not lock the operator out, but
    the same exemption let a locked-out address keep presenting cookies - and
    a cookie is also checked against the master token.
    """
    client = build_client(sandbox, max_failed_attempts=3, lockout_duration=60)
    token = get_token_manager().generate_master_token()
    login(client, token)

    for _ in range(3):
        get_brute_force().record_failure("testclient")

    response = client.get("/api/auth/sessions")

    assert response.status_code == 429, response.text
    assert response.json()["error"] == "locked_out"


def test_the_cookie_is_not_an_unlimited_master_token_oracle(sandbox: Path) -> None:
    """Guessing the master token through the cookie ends in the same lockout."""
    client = build_client(sandbox, max_failed_attempts=3, lockout_duration=60)
    get_token_manager().generate_master_token()

    statuses = []
    for index in range(6):
        client.cookies.set(SESSION_COOKIE_NAME, f"wasm_guess{index}")
        statuses.append(client.get("/api/apps").status_code)

    assert statuses[0] == 401, statuses
    assert statuses[-1] == 429, statuses


@pytest.mark.parametrize("channel", ["bearer", "cookie"])
def test_failed_checks_at_the_session_endpoint_count(sandbox: Path, channel: str) -> None:
    """``GET /api/auth/session`` checks a credential, so a wrong one is a guess."""
    client = build_client(sandbox, max_failed_attempts=3, lockout_duration=60)
    token = get_token_manager().generate_master_token()

    for index in range(3):
        if channel == "bearer":
            answer = client.get(
                "/api/auth/session", headers={"Authorization": f"Bearer wasm_guess{index}"}
            )
        else:
            client.cookies.set(SESSION_COOKIE_NAME, f"wasm_guess{index}")
            answer = client.get("/api/auth/session")
        assert answer.status_code == 200
        assert answer.json()["authenticated"] is False
    client.cookies.clear()

    refused = client.post("/api/auth/login", json={"token": token})

    assert refused.status_code == 429, refused.text


def test_a_dead_session_cookie_is_not_a_guess(sandbox: Path) -> None:
    """
    A browser that outlived its session keeps sending the cookie; that is not an attack.

    The token is signed with the server's key, so it cannot have been guessed,
    and counting it would lock the operator out of the sign-in page they were
    just sent to.
    """
    client = build_client(sandbox, max_failed_attempts=3, lockout_duration=60)
    token = get_token_manager().generate_master_token()
    body = login(client, token)
    dead_cookie = client.cookies.get(SESSION_COOKIE_NAME)
    assert client.post("/api/auth/logout", headers={CSRF_HEADER_NAME: body["csrf_token"]})
    assert dead_cookie

    for _ in range(6):
        client.cookies.set(SESSION_COOKIE_NAME, dead_cookie)
        answer = client.get("/api/auth/session")
        assert answer.status_code == 200, answer.text
        assert answer.json()["authenticated"] is False
        assert client.get("/api/apps").status_code == 401
    client.cookies.clear()

    assert client.post("/api/auth/login", json={"token": token}).status_code == 200


# ------------------------------------------------------- renewal fan-out


def age_sessions(sandbox: Path, seconds: int) -> None:
    """
    Make every session look older than it is, so renewal is due.

    Args:
        sandbox: Per-test temporary directory.
        seconds: How far back to move ``issued_at``.
    """
    db = sqlite3.connect(sandbox / "state" / "web-sessions.db")
    with db:
        db.execute("UPDATE sessions SET issued_at = issued_at - ?", (seconds,))
    db.close()


def live_sessions(sandbox: Path) -> int:
    """
    Args:
        sandbox: Per-test temporary directory.

    Returns:
        How many session rows are unrevoked and unexpired.
    """
    db = sqlite3.connect(sandbox / "state" / "web-sessions.db")
    try:
        (count,) = db.execute(
            "SELECT COUNT(*) FROM sessions WHERE revoked = 0 AND expires_at > strftime('%s','now')"
        ).fetchone()
    finally:
        db.close()
    return int(count)


def test_a_retired_session_renews_into_one_successor_only(sandbox: Path) -> None:
    """
    Every request still carrying the old cookie used to mint a session of its own.

    The grace a rotation leaves exists for requests already in flight; each
    of them renewing again turned one login into as many live sessions as the
    dashboard had open requests.
    """
    manager = TokenManager(make_config(sandbox, token_expiration_hours=1))
    session = manager.create_session("10.0.0.1")
    age_sessions(sandbox, 3500)
    payload = manager.verify_session_token(session.token, "10.0.0.1")
    assert payload is not None

    first = manager.renew_session(payload)
    again = manager.renew_session(payload)
    third = manager.renew_session(payload)

    assert first is not None and again is not None and third is not None
    assert again.session_id == first.session_id == third.session_id
    assert again.token == first.token
    assert again.csrf_token == first.csrf_token
    assert live_sessions(sandbox) == 2, "the retired session and exactly one successor"
    manager.sessions.close()


def test_concurrent_requests_with_the_old_cookie_get_the_same_new_cookie(sandbox: Path) -> None:
    """Over HTTP: every response during the grace hands out the one successor."""
    client = build_client(sandbox, token_expiration_hours=1)
    token = get_token_manager().generate_master_token()
    login(client, token)
    old_cookie = client.cookies.get(SESSION_COOKIE_NAME)
    age_sessions(sandbox, 3500)

    issued: set[str] = set()
    for _ in range(3):
        anon = TestClient(client.app, client=("testclient", 50000))
        anon.cookies.set(SESSION_COOKIE_NAME, str(old_cookie))
        response = anon.get("/api/auth/verify")
        assert response.status_code == 200, response.text
        issued.update(
            value.split(";", 1)[0]
            for key, value in response.headers.multi_items()
            if key.lower() == "set-cookie" and value.startswith(f"{SESSION_COOKIE_NAME}=")
        )

    assert len(issued) == 1, issued
    assert live_sessions(sandbox) == 2


def test_logging_out_retires_the_whole_lineage(sandbox: Path) -> None:
    """The predecessor left alive for in-flight requests dies with its successor."""
    manager = TokenManager(make_config(sandbox, token_expiration_hours=1))
    session = manager.create_session("10.0.0.1")
    age_sessions(sandbox, 3500)
    payload = manager.verify_session_token(session.token, "10.0.0.1")
    assert payload is not None
    renewed = manager.renew_session(payload)
    assert renewed is not None

    manager.revoke_session(renewed.session_id)

    assert manager.verify_session_token(session.token, "10.0.0.1") is None
    assert manager.verify_session_token(renewed.token, "10.0.0.1") is None
    manager.sessions.close()
