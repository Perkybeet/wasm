# Copyright (c) 2024-2026 Yago Lopez Prado
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
The console's rate limit: strict for strangers, roomy for its own operator.

120 requests a minute per client address was reached by the console on its
own - navigating, polling, following a job - and over an SSH tunnel every
request is 127.0.0.1, so every tab and script shared that one budget. A reload
counted the console's ``index.html`` too, and the operator was shown the raw
429 JSON where the console should have been.

What is defended here:

- A request without a valid credential keeps the strict per-address budget.
- A request with one is counted per credential, against a much larger budget,
  so two credentials behind one address do not share it.
- The console's shell and build assets spend no budget at all.
- A 429 is the API's error contract, with a ``Retry-After`` that says when.
- Credential guessing is the lockout's job and still is: a wrong credential
  buys nothing from the per-credential budget.
- The limiter is not a credential oracle. It only looks at a credential where
  an endpoint would (``/api``, ``/events``, ``/ws``), a wrong one it looked at
  is counted against the lockout exactly once even when no endpoint checks it,
  and an address already over the anonymous budget gets no credential check.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from wasm.web import auth
from wasm.web.auth import RateLimiter, SecurityConfig
from wasm.web.server import create_app, get_brute_force, get_token_manager


def build_client(sandbox: Path, **overrides: object) -> TestClient:
    """
    Create an application with a small anonymous budget, and a client for it.

    Args:
        sandbox: Per-test temporary directory.
        **overrides: Security configuration overrides.

    Returns:
        A test client, every request from one address like an SSH tunnel's.
    """
    params: dict[str, object] = {
        "state_dir": sandbox / "state",
        "rate_limit_requests": 3,
        "rate_limit_authenticated_requests": 10,
        "rate_limit_window": 60,
    }
    params.update(overrides)
    app = create_app(SecurityConfig(**params))  # type: ignore[arg-type]
    return TestClient(app, client=("127.0.0.1", 50000), follow_redirects=False)


def bearer(token: str) -> dict[str, str]:
    """
    Args:
        token: A credential.

    Returns:
        The header that presents it.
    """
    return {"Authorization": f"Bearer {token}"}


def test_an_anonymous_client_keeps_the_strict_budget(sandbox: Path) -> None:
    """Nothing about this change widens what a client without a credential gets."""
    client = build_client(sandbox)

    statuses = [client.get("/api/auth/session").status_code for _ in range(4)]

    assert statuses[:3] == [200, 200, 200]
    assert statuses[3] == 429


def test_a_429_keeps_the_error_contract_and_says_when_to_retry(sandbox: Path) -> None:
    """The console backs off by Retry-After; a script parses the body like any error."""
    client = build_client(sandbox)
    for _ in range(3):
        client.get("/api/auth/session")

    response = client.get("/api/auth/session")

    assert response.status_code == 429
    body = response.json()
    assert body["error"] == "rate_limited"
    assert body["detail"]
    assert set(body) == {"error", "detail", "hint", "fields"}
    retry_after = int(response.headers["Retry-After"])
    assert 1 <= retry_after <= 60
    assert response.headers["X-RateLimit-Limit"] == "3"
    assert response.headers["X-RateLimit-Remaining"] == "0"


def test_an_authenticated_client_is_counted_against_its_own_larger_budget(
    sandbox: Path,
) -> None:
    """Polling, navigation and job following all fit; a runaway client still stops."""
    client = build_client(sandbox)
    token = get_token_manager().generate_master_token()

    statuses = [
        client.get("/api/auth/verify", headers=bearer(token)).status_code for _ in range(11)
    ]

    assert statuses[:10] == [200] * 10
    assert statuses[10] == 429


def test_the_headers_report_the_budget_the_request_was_counted_in(sandbox: Path) -> None:
    """An authenticated response must not advertise the anonymous numbers."""
    client = build_client(sandbox)
    token = get_token_manager().generate_master_token()

    response = client.get("/api/auth/verify", headers=bearer(token))

    assert response.headers["X-RateLimit-Limit"] == "10"
    assert response.headers["X-RateLimit-Remaining"] == "9"


def test_authenticated_traffic_does_not_spend_the_anonymous_budget(sandbox: Path) -> None:
    """Behind a tunnel, a busy console must not lock the sign-in page out."""
    client = build_client(sandbox)
    token = get_token_manager().generate_master_token()

    for _ in range(10):
        assert client.get("/api/auth/verify", headers=bearer(token)).status_code == 200

    anonymous = [client.get("/api/auth/session").status_code for _ in range(3)]
    assert anonymous == [200, 200, 200]


def test_two_credentials_behind_one_address_have_a_budget_each(sandbox: Path) -> None:
    """A script's API token and the operator's browser, both through 127.0.0.1."""
    client = build_client(sandbox)
    manager = get_token_manager()
    master = manager.generate_master_token()
    api_token = manager.create_api_token("ci", "read")["token"]

    for _ in range(10):
        assert client.get("/api/auth/verify", headers=bearer(master)).status_code == 200
    assert client.get("/api/auth/verify", headers=bearer(master)).status_code == 429

    assert client.get("/api/auth/verify", headers=bearer(api_token)).status_code == 200


def test_a_signed_in_browser_is_counted_by_its_session(sandbox: Path) -> None:
    """The console's cookie is a credential like the header, with its own budget."""
    client = build_client(sandbox, rate_limit_authenticated_requests=5)
    token = get_token_manager().generate_master_token()
    # The sign-in itself carries no credential yet: it spends the anonymous budget.
    assert client.post("/api/auth/login", json={"token": token}).status_code == 200

    statuses = [client.get("/api/auth/verify").status_code for _ in range(6)]

    assert statuses[:5] == [200] * 5
    assert statuses[5] == 429


@pytest.mark.parametrize("path", ["/", "/apps/example.com/deployments", "/settings"])
def test_the_console_shell_spends_no_budget(sandbox: Path, path: str) -> None:
    """Reloading the console must never answer with the raw 429 JSON."""
    client = build_client(sandbox)

    statuses = {client.get(path).status_code for _ in range(10)}

    assert 429 not in statuses
    assert statuses <= {200, 503}


def test_a_machine_path_still_counts(sandbox: Path) -> None:
    """The exemption is the shell, not everything the fallback route sees."""
    client = build_client(sandbox)

    statuses = [client.get("/api/does-not-exist").status_code for _ in range(4)]

    assert statuses[3] == 429


def test_a_wrong_credential_is_counted_as_anonymous(sandbox: Path) -> None:
    """Presenting garbage does not buy the authenticated budget."""
    client = build_client(sandbox, max_failed_attempts=1000)

    statuses = [
        client.get("/api/auth/verify", headers=bearer("wasm_guess")).status_code for _ in range(4)
    ]

    assert statuses[:3] == [401, 401, 401]
    assert statuses[3] == 429


def test_the_lockout_still_stops_credential_guessing(sandbox: Path) -> None:
    """
    Brute force is the lockout's job, separate from the rate limit.

    A generous anonymous budget does not matter: after the failures the
    lockout allows, the address is refused - and a locked-out address is not
    given the per-credential budget even for a valid credential.
    """
    client = build_client(
        sandbox, rate_limit_requests=1000, max_failed_attempts=3, lockout_duration=60
    )
    token = get_token_manager().generate_master_token()

    guesses = [
        client.get("/api/auth/verify", headers=bearer(f"wasm_guess{n}")).status_code
        for n in range(5)
    ]

    assert guesses[:3] == [401, 401, 401]
    assert guesses[3:] == [429, 429]
    locked = client.get("/api/auth/verify", headers=bearer(token))
    assert locked.status_code == 429
    assert locked.json()["error"] == "locked_out"


def spy_on_credential_checks(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """
    Record every credential the server checks, still checking it.

    Args:
        monkeypatch: Patching helper, scoped to the test.

    Returns:
        The list the checked credentials are appended to.
    """
    checked: list[str] = []
    real = auth.check_credential

    def spy(credential: str, client_ip: str) -> dict[str, object] | None:
        checked.append(credential)
        return real(credential, client_ip)

    monkeypatch.setattr(auth, "check_credential", spy)
    return checked


@pytest.mark.parametrize(
    ("method", "path"),
    [("GET", "/health"), ("POST", "/hooks/deploy/example.com")],
)
def test_a_path_that_checks_no_credential_counts_by_address_whatever_it_carries(
    sandbox: Path, monkeypatch: pytest.MonkeyPatch, method: str, path: str
) -> None:
    """
    Neither /health nor a forge webhook verifies a bearer token, so neither may
    tell a valid one from a guess: before, the valid one landed in the roomy
    budget and X-RateLimit-Limit said so, while the guess was never recorded.
    """
    client = build_client(sandbox)
    token = get_token_manager().generate_master_token()
    checked = spy_on_credential_checks(monkeypatch)

    valid = client.request(method, path, headers=bearer(token), json={})
    wrong = client.request(method, path, headers=bearer("wasm_guess"), json={})

    assert checked == []
    assert valid.headers["X-RateLimit-Limit"] == "3"
    assert wrong.headers["X-RateLimit-Limit"] == "3"
    assert (valid.headers["X-RateLimit-Remaining"], wrong.headers["X-RateLimit-Remaining"]) == (
        "2",
        "1",
    )
    assert get_brute_force().get_attempts_remaining("127.0.0.1") == 5


def test_a_wrong_credential_no_endpoint_checks_still_counts_towards_the_lockout(
    sandbox: Path,
) -> None:
    """The limiter checked it, so the guess is paid for even on a 404."""
    client = build_client(
        sandbox, rate_limit_requests=1000, max_failed_attempts=2, lockout_duration=60
    )
    token = get_token_manager().generate_master_token()

    first = client.get("/api/does-not-exist", headers=bearer("wasm_guess1"))
    assert first.status_code == 404
    assert get_brute_force().get_attempts_remaining("127.0.0.1") == 1
    assert client.get("/api/does-not-exist", headers=bearer("wasm_guess2")).status_code == 404

    locked = client.get("/api/auth/verify", headers=bearer(token))
    assert locked.status_code == 429
    assert locked.json()["error"] == "locked_out"


@pytest.mark.parametrize("path", ["/api/auth/verify", "/api/auth/session"])
def test_a_wrong_credential_an_endpoint_checks_counts_once(sandbox: Path, path: str) -> None:
    """The limiter and the endpoint both look at it; the lockout hears once."""
    client = build_client(sandbox, rate_limit_requests=1000, max_failed_attempts=5)

    client.get(path, headers=bearer("wasm_guess"))

    assert get_brute_force().get_attempts_remaining("127.0.0.1") == 4


def test_a_websocket_guess_counts_once(sandbox: Path) -> None:
    """The handshake is checked by the limiter and by the handshake itself."""
    from starlette.websockets import WebSocketDisconnect

    from wasm.web.websockets.router import WS_SUBPROTOCOL, WS_TOKEN_PREFIX

    client = build_client(sandbox, rate_limit_requests=1000, max_failed_attempts=5)

    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(
            "/ws/events", subprotocols=[WS_SUBPROTOCOL, f"{WS_TOKEN_PREFIX}wasm_guess"]
        ):
            pass

    assert get_brute_force().get_attempts_remaining("127.0.0.1") == 4


def test_a_signed_in_browser_with_an_expired_session_is_not_counted(sandbox: Path) -> None:
    """A cookie this server signed is not a guess, for the limiter either."""
    client = build_client(sandbox, rate_limit_requests=1000, max_failed_attempts=2)
    token = get_token_manager().generate_master_token()
    assert client.post("/api/auth/login", json={"token": token}).status_code == 200
    session = client.cookies["wasm_session"]
    csrf = {"X-WASM-CSRF": client.cookies["wasm_csrf"]}
    assert client.post("/api/auth/logout", headers=csrf).status_code in (200, 204)
    client.cookies.set("wasm_session", session)

    for _ in range(3):
        client.get("/api/does-not-exist")

    assert get_brute_force().get_attempts_remaining("127.0.0.1") == 2


def test_an_address_over_the_anonymous_budget_gets_no_credential_check(
    sandbox: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A flood that is refused anyway must not cost a SQLite query and a file read each."""
    client = build_client(sandbox)
    token = get_token_manager().generate_master_token()
    for _ in range(3):
        client.get("/api/auth/session")
    checked = spy_on_credential_checks(monkeypatch)

    response = client.get("/api/auth/verify", headers=bearer(token))

    assert response.status_code == 429
    assert response.json()["error"] == "rate_limited"
    assert response.headers["X-RateLimit-Limit"] == "3"
    assert checked == []


def test_retry_after_is_when_the_oldest_request_leaves_the_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Not the whole window: the console should wait exactly as long as it must."""
    now = [1000.0]
    monkeypatch.setattr("wasm.web.auth.time.time", lambda: now[0])
    limiter = RateLimiter(max_requests=2, window=60)

    assert limiter.is_allowed("k")
    assert limiter.retry_after("k") == 0
    now[0] = 1010.0
    assert limiter.is_allowed("k")
    now[0] = 1020.0
    assert not limiter.is_allowed("k")

    assert limiter.retry_after("k") == 40
