# Copyright (c) 2024-2026 Yago Lopez Prado
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Tests for ``GET /api/system/version``.

``updates.check`` is the switch an operator on an airgapped or tightly
firewalled server needs: without it, every load of the panel's version
display made a GitHub request that could only time out. Disabled, the
endpoint must say so plainly rather than quietly reporting "no update found",
which looks identical to a check that actually ran and found nothing.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from wasm import __version__
from wasm.core.config import Config
from wasm.core.update_checker import UpdateChecker
from wasm.web.api import system as system_api
from wasm.web.api.auth import get_current_session


@pytest.fixture
def config_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """
    Point the configuration singleton at a sandbox file.

    Args:
        tmp_path: Per-test temporary directory.
        monkeypatch: Patching helper, scoped to the test.

    Returns:
        The path the singleton reads from and writes to.
    """
    path = tmp_path / "etc" / "wasm" / "config.yaml"
    monkeypatch.setattr("wasm.core.config.DEFAULT_CONFIG_PATH", path)
    Config.reset_instance()
    yield path
    Config.reset_instance()


@pytest.fixture
def client(config_path: Path) -> TestClient:
    """
    Build a client for the system router alone, authentication stubbed.

    Args:
        config_path: Fixture redirecting configuration reads into the sandbox.

    Returns:
        A client whose requests are already authenticated.
    """
    app = FastAPI()
    app.include_router(system_api.router, prefix="/api/system")
    app.dependency_overrides[get_current_session] = lambda: {"session_id": "test", "scope": "read"}
    return TestClient(app)


def test_disabled_reports_status_disabled_and_makes_no_request(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The endpoint must not even try GitHub when the operator turned it off."""
    Config().set("updates.check", False)

    def _boom() -> str | None:
        raise AssertionError("the update check must not run a request while disabled")

    monkeypatch.setattr(UpdateChecker, "_fetch_latest_version", classmethod(lambda cls: _boom()))

    response = client.get("/api/system/version")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "disabled"
    assert body["current_version"] == __version__
    assert body["has_update"] is False
    assert body["latest_version"] is None
    assert body["update_command"] is None


def test_enabled_reports_status_checked(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The ordinary case - checking left on - is unaffected and reports 'checked'."""
    monkeypatch.setattr(UpdateChecker, "CACHE_FILE", tmp_path / "version_check.json")
    monkeypatch.setattr(
        UpdateChecker, "_fetch_latest_version", classmethod(lambda cls: __version__)
    )

    response = client.get("/api/system/version")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "checked"
    assert body["has_update"] is False
