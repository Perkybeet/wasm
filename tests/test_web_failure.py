# Copyright (c) 2024-2026 Yago Lopez Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
Tests for what the panel does when a screen cannot be rendered.

The API has had an error boundary since the beginning, as ``WASMErrorRoute``.
The pages never did, so a manager raising anywhere in a page handler reached
Starlette's default and produced the words "Internal Server Error" as plain
text on a white page: no navigation, no machine strip, and none of what nginx
or systemd actually said.

That last part is the one that matters. The design direction is explicit that a
system error is never paraphrased: it is shown verbatim, in mono, with the fix
above it. A panel that swallows certbot's output and replaces it with a generic
sentence has taken away the only thing the operator can act on.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from wasm.core.exceptions import DeploymentError
from wasm.web.auth import SecurityConfig
from wasm.web.server import create_app, get_token_manager

#: What certbot says when it cannot answer a challenge. It reaches the screen
#: unedited, so it is also a test that the boundary escapes what it prints.
CERTBOT_FAILURE = (
    "Certbot failed to authenticate some domains (authenticator: nginx). "
    "Domain: <script>alert(1)</script>example.com"
)


@pytest.fixture
def app(tmp_path: Path, runner: object) -> FastAPI:
    """
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
        A signed-in client.
    """
    signed_in = TestClient(app, client=("testclient", 50000), follow_redirects=False)
    token = get_token_manager().generate_master_token()
    response = signed_in.post("/api/auth/login", json={"token": token})
    assert response.status_code == 200, response.text
    return signed_in


@pytest.fixture
def broken_backups(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Make the backups screen raise the way a failing tool does.

    Args:
        monkeypatch: Patching helper, scoped to the test.
    """

    def raise_it(*args: Any, **kwargs: Any) -> None:
        """
        Raises:
            DeploymentError: Always.
        """
        raise DeploymentError(CERTBOT_FAILURE, details="Check that the domain resolves here.")

    monkeypatch.setattr("wasm.web.views.resources.backup_rows", raise_it)


@pytest.fixture
def crashing_backups(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Make the backups screen raise something nobody planned for.

    Args:
        monkeypatch: Patching helper, scoped to the test.
    """

    def raise_it(*args: Any, **kwargs: Any) -> None:
        """
        Raises:
            AttributeError: Always.
        """
        raise AttributeError("'NoneType' object has no attribute 'domain'")

    monkeypatch.setattr("wasm.web.views.resources.backup_rows", raise_it)


def test_a_failing_screen_answers_with_a_page_not_plain_text(
    client: TestClient, broken_backups: None
) -> None:
    """
    Args:
        client: A signed-in client.
        broken_backups: The backups screen, raising.
    """
    response = client.get("/backups")

    assert response.status_code == 500
    assert response.headers["content-type"].startswith("text/html")
    assert "Internal Server Error" not in response.text


def test_the_tool_s_own_words_reach_the_screen(client: TestClient, broken_backups: None) -> None:
    """
    The rule: a system error is never paraphrased.

    Args:
        client: A signed-in client.
        broken_backups: The backups screen, raising.
    """
    body = client.get("/backups").text

    assert "Certbot failed to authenticate some domains" in body
    assert "problem__output" in body, "the output is not in the mono block"


def test_the_error_carries_its_fix(client: TestClient, broken_backups: None) -> None:
    """
    A WASMError's details are how to recover; they belong above the output.

    Args:
        client: A signed-in client.
        broken_backups: The backups screen, raising.
    """
    body = client.get("/backups").text

    assert "Check that the domain resolves here." in body


def test_a_failing_screen_still_has_the_panel_around_it(
    client: TestClient, broken_backups: None
) -> None:
    """
    One screen failed, not the whole panel. The operator can navigate away.

    Args:
        client: A signed-in client.
        broken_backups: The backups screen, raising.
    """
    body = client.get("/backups").text

    assert 'class="sidebar"' in body
    assert 'href="/apps"' in body


def test_markup_inside_an_error_message_comes_out_escaped(
    client: TestClient, broken_backups: None
) -> None:
    """
    The output is printed verbatim, which is exactly why it must be escaped.

    A domain name reaches this string from the store, and an injected script in
    this panel is a root shell.

    Args:
        client: A signed-in client.
        broken_backups: The backups screen, raising.
    """
    body = client.get("/backups").text

    assert "<script>alert(1)</script>" not in body
    assert "&lt;script&gt;" in body


def test_a_bug_in_the_panel_is_not_dressed_up_as_a_system_error(
    app: FastAPI, crashing_backups: None
) -> None:
    """
    The boundary catches WASMError and nothing else, on purpose.

    An AttributeError here is a bug in the panel, not something the machine
    did, and this project's position on those is that they stay loud: catching
    them to render a polite screen is precisely the mechanism by which five
    calls to methods that did not exist shipped for entire releases. It also
    keeps the boundary out of the blind-except ratchet.

    Args:
        app: The application.
        crashing_backups: The backups screen, raising an AttributeError.
    """
    client = TestClient(
        app, client=("testclient", 50000), follow_redirects=False, raise_server_exceptions=True
    )
    token = get_token_manager().generate_master_token()
    assert client.post("/api/auth/login", json={"token": token}).status_code == 200

    with pytest.raises(AttributeError):
        client.get("/backups")


def test_a_working_screen_is_untouched_by_the_boundary(client: TestClient) -> None:
    """
    A net that catches everything would pass every test above and break the
    panel.

    Args:
        client: A signed-in client.
    """
    response = client.get("/backups")

    assert response.status_code == 200


def test_a_redirect_is_not_treated_as_a_failure(app: FastAPI) -> None:
    """
    Signing in is an answer, not a crash. The boundary re-raises HTTPException
    so an expired session still reaches the sign-in page instead of rendering
    "this screen could not be rendered".

    Args:
        app: The application.
    """
    anonymous = TestClient(app, client=("testclient", 50000), follow_redirects=False)

    response = anonymous.get("/backups")

    assert response.status_code == 303
    assert response.headers["location"] == "/login"
