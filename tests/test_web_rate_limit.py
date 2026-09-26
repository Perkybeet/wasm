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
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from wasm.web.auth import RateLimiter, SecurityConfig
from wasm.web.server import create_app, get_token_manager


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
