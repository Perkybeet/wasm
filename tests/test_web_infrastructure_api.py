# Copyright (c) 2024-2026 Yago Lopez Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
Tests for gaps the deleted infrastructure pages left in the JSON API's own
coverage (tests/test_web_infrastructure_pages.py).

Most of what that file exercised through ``/services/new`` and
``/sites/new`` htmx forms is already pinned directly against ``/api/services``
and ``/api/sites`` in tests/test_web_services_api.py and
tests/test_web_api_siblings.py. Three things were not:

- **Advanced-mode service creation** (``raw_content``) - the simple-mode path
  is well covered, but nothing exercised the "paste a whole unit" path or its
  WASM-marker refusal, and that used to write straight to disk before this
  endpoint routed it through the manager.
- **A duplicate site is a 409, not a silent overwrite.**
- **``POST /api/sites/reload`` tests before it reloads**, and a broken live
  configuration must not be followed by a reload that takes the site down.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from wasm.managers.service_manager import WASM_UNIT_MARKER, ServiceManager
from wasm.web.api import services as services_api
from wasm.web.api import sites as sites_api
from wasm.web.api.auth import get_current_session

RAW_UNIT = (
    f"# {WASM_UNIT_MARKER}\n[Unit]\nDescription=Raw queue worker\n\n"
    "[Service]\nExecStart=/usr/bin/true\n"
)


def _client(*routers: Any) -> TestClient:
    """
    Args:
        *routers: ``(router, prefix)`` pairs to mount.

    Returns:
        A client whose requests are already authenticated.
    """
    app = FastAPI()
    for router, prefix in routers:
        app.include_router(router, prefix=prefix)
    app.dependency_overrides[get_current_session] = lambda: {"session_id": "test"}
    return TestClient(app, raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# Advanced-mode (raw_content) service creation
# ---------------------------------------------------------------------------


@pytest.fixture
def unit_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """
    Args:
        tmp_path: Per-test temporary directory.
        monkeypatch: Patching helper, scoped to the test.

    Returns:
        The directory unit files are expected to be written into.
    """
    directory = tmp_path / "etc" / "systemd" / "system"
    directory.mkdir(parents=True)
    monkeypatch.setattr(services_api, "SYSTEMD_UNIT_DIR", directory, raising=False)
    return directory


@pytest.fixture
def services_client(unit_dir: Path, monkeypatch: pytest.MonkeyPatch, runner: object) -> TestClient:
    """
    A client over the real ServiceManager, pinned to the sandbox.

    The real manager is used rather than a stub because what is under test
    is whether the endpoint delegates the write to it, which is exactly the
    guarantee a stub cannot prove.

    Args:
        unit_dir: The sandbox unit directory.
        monkeypatch: Patching helper, scoped to the test.
        runner: The FakeRunner fixture, so systemctl is never invoked for real.

    Returns:
        The client.
    """
    monkeypatch.setattr(ServiceManager, "SYSTEMD_DIR", unit_dir)
    monkeypatch.setattr(ServiceManager, "UNIT_SEARCH_DIRS", (unit_dir,))
    monkeypatch.setattr(services_api, "ServiceManager", ServiceManager, raising=False)
    return _client((services_api.router, "/api/services"))


def test_creating_a_service_via_raw_content_writes_it_verbatim(
    services_client: TestClient, unit_dir: Path
) -> None:
    """Advanced mode is a promise: the file on disk is exactly what was sent."""
    response = services_client.post(
        "/api/services", json={"name": "infra-raw", "raw_content": RAW_UNIT}
    )

    assert response.status_code == 200, response.text
    assert (unit_dir / "infra-raw.service").read_text() == RAW_UNIT


def test_creating_a_service_via_raw_content_without_the_marker_is_refused(
    services_client: TestClient, unit_dir: Path
) -> None:
    """The manager's rule applies to creation too, not only to an edit."""
    body = "[Service]\nExecStart=/usr/bin/true\n"

    response = services_client.post(
        "/api/services", json={"name": "infra-naked", "raw_content": body}
    )

    assert 400 <= response.status_code < 500, response.text
    assert not (unit_dir / "infra-naked.service").exists()


# ---------------------------------------------------------------------------
# Sites: duplicates and reload
# ---------------------------------------------------------------------------


@pytest.fixture
def site_dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """
    Point the sites API at an nginx manager bound to a throwaway tree.

    Args:
        tmp_path: Per-test temporary directory.
        monkeypatch: Patching helper, scoped to the test.

    Returns:
        The sites-available and sites-enabled directories.
    """
    from wasm.managers.nginx_manager import NginxManager
    from wasm.managers.webserver import NGINX_BACKEND

    available = tmp_path / "etc" / "nginx" / "sites-available"
    enabled = tmp_path / "etc" / "nginx" / "sites-enabled"
    available.mkdir(parents=True)
    enabled.mkdir(parents=True)
    backend = dataclasses.replace(NGINX_BACKEND, sites_available=available, sites_enabled=enabled)

    def make(verbose: bool = False, **kwargs: Any) -> NginxManager:
        """
        Args:
            verbose: Ignored, kept for signature compatibility.
            **kwargs: Ignored, kept for signature compatibility.

        Returns:
            The manager, pinned to the sandbox tree.
        """
        return NginxManager(verbose=verbose, backend=backend)

    monkeypatch.setattr(sites_api, "MANAGERS", {"nginx": make})
    monkeypatch.setattr(sites_api, "detect_webserver", lambda: "nginx")
    return available, enabled


@pytest.fixture
def sites_client(site_dirs: tuple[Path, Path], runner: object) -> TestClient:
    """
    Args:
        site_dirs: Fixture redirecting nginx writes into the sandbox.
        runner: The FakeRunner fixture, installed process-wide.

    Returns:
        The client.
    """
    return _client((sites_api.router, "/api/sites"))


def test_creating_a_duplicate_site_is_refused_with_409(sites_client: TestClient) -> None:
    """The second create for the same domain must not silently overwrite the first."""
    first = sites_client.post("/api/sites", json={"domain": "panel.example.com"})
    assert first.status_code == 200, first.text

    second = sites_client.post("/api/sites", json={"domain": "panel.example.com"})

    assert second.status_code == 409, second.text
    assert "already exists" in second.text


def test_the_reload_endpoint_tests_then_reloads(sites_client: TestClient, runner: Any) -> None:
    """The reload runs the syntax check first, then reloads the live unit."""
    response = sites_client.post("/api/sites/reload")

    assert response.status_code == 200, response.text
    assert ("nginx", "-t") in runner.calls, "the reload skipped the configuration test"
    assert ("systemctl", "reload", "nginx") in runner.calls


def test_a_failed_config_test_blocks_the_reload(sites_client: TestClient, runner: Any) -> None:
    """A broken live configuration must never be followed by a reload."""
    runner.script(["nginx", "-t"], stderr="nginx: configuration file test failed", exit_code=1)

    response = sites_client.post("/api/sites/reload")

    assert response.status_code >= 400, response.text
    assert ("systemctl", "reload", "nginx") not in runner.calls
