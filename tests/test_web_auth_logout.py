# Copyright (c) 2024-2026 Yago Lopez Prado
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Tests for ``POST /api/auth/logout``.

Ported from the deleted server-rendered panel, which only ever exercised the
page's own sign-out button (a thin client of this same endpoint) and never
the JSON route directly - the gap this leaves once the button is gone.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from wasm.web.auth import CSRF_COOKIE_NAME, CSRF_HEADER_NAME, SESSION_COOKIE_NAME, SecurityConfig
from wasm.web.server import create_app, get_token_manager


@pytest.fixture
def app(tmp_path: Path, runner: object) -> FastAPI:
    """
    Args:
        tmp_path: Per-test temporary directory.
        runner: The fake command runner.

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
        A signed-in client carrying the CSRF header.
    """
    signed_in = TestClient(app, client=("testclient", 50000), follow_redirects=False)
    token = get_token_manager().generate_master_token()
    response = signed_in.post("/api/auth/login", json={"token": token})
    assert response.status_code == 200, response.text
    signed_in.headers[CSRF_HEADER_NAME] = response.json()["csrf_token"]
    return signed_in


def test_logout_revokes_the_session_and_clears_its_cookies(client: TestClient) -> None:
    """The button in the shell has to actually revoke, and leave nothing behind."""
    assert client.get("/api/apps").status_code == 200

    response = client.post("/api/auth/logout")

    assert response.status_code == 200, response.text
    assert response.json()["success"] is True

    cleared = " ".join(response.headers.get_list("set-cookie"))
    assert f"{SESSION_COOKIE_NAME}=" in cleared
    assert f"{CSRF_COOKIE_NAME}=" in cleared

    assert client.get("/api/apps").status_code == 401


def test_logout_demands_a_session(app: FastAPI) -> None:
    """An anonymous client cannot revoke a session it does not have."""
    anonymous = TestClient(app, client=("testclient", 50000), follow_redirects=False)

    response = anonymous.post("/api/auth/logout")

    assert response.status_code == 401


def test_logout_still_demands_the_csrf_header(app: FastAPI) -> None:
    """A mutation is a mutation: the cookie session alone is not enough."""
    signed_in = TestClient(app, client=("testclient", 50000), follow_redirects=False)
    token = get_token_manager().generate_master_token()
    login = signed_in.post("/api/auth/login", json={"token": token})
    assert login.status_code == 200, login.text

    response = signed_in.post("/api/auth/logout")

    assert response.status_code == 403
