# Copyright (c) 2024-2026 Yago Lopez Prado
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Tests for what the API's error boundary deliberately does not catch.

Ported from the deleted server-rendered panel's own failure-boundary tests
(tests/test_web_failure.py), which exercised a page-level boundary that no
longer exists. The JSON API has always had its own, ``WASMErrorRoute`` (see
tests/test_web_errors.py for the shape of what it *does* catch); what matters
here is that it only ever catches :class:`~wasm.core.exceptions.WASMError`.
An ``AttributeError`` is a bug in the endpoint, not something a manager or a
system tool reported, and this project's position on those is that they stay
loud: catching them to answer a polite JSON error is precisely the mechanism
by which five calls to methods that did not exist shipped for entire
releases (see CLAUDE.md rule 2).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from wasm.web.auth import SecurityConfig
from wasm.web.server import create_app, get_token_manager


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


def test_a_bug_in_an_endpoint_is_not_dressed_up_as_a_system_error(
    app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    The boundary catches WASMError and nothing else, on purpose.

    An AttributeError here is a bug in the endpoint, not something the
    machine did; it must propagate rather than come back as a tidy
    ``{"error": ...}`` body, or the boundary would be exactly the kind of
    broad except CLAUDE.md rule 2 forbids.
    """

    def broken() -> None:
        raise AttributeError("'NoneType' object has no attribute 'domain'")

    monkeypatch.setattr("wasm.web.api.apps.get_store", broken)

    client = TestClient(app, client=("testclient", 50000), raise_server_exceptions=True)
    token = get_token_manager().generate_master_token()
    assert client.post("/api/auth/login", json={"token": token}).status_code == 200

    with pytest.raises(AttributeError):
        client.get("/api/apps")


def test_a_working_endpoint_is_untouched_by_the_boundary(app: FastAPI) -> None:
    """A net that catches everything would pass the test above and break the API."""
    client = TestClient(app, client=("testclient", 50000), follow_redirects=False)
    token = get_token_manager().generate_master_token()
    assert client.post("/api/auth/login", json={"token": token}).status_code == 200

    response = client.get("/api/apps")

    assert response.status_code == 200
