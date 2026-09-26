# Copyright (c) 2024-2026 Yago Lopez Prado
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Tests for ``GET /api/system/health``.

The endpoint is a thin translation of
:func:`wasm.managers.health.collect_health_report` to HTTP - the checks
themselves (disk space, web servers, applications, certificates, memory) are
pinned once, against ``wasm health``, in ``tests/test_cli_health.py``. What
this module owns: the route calls the shared function and serialises
:class:`~wasm.managers.health.HealthReport` field for field, so the console's
server card can never disagree with what the CLI reports.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.test_web_auth import build_client
from wasm.core.runner import FakeRunner
from wasm.managers.health import HealthCheck, HealthReport
from wasm.web.api import system as system_api
from wasm.web.api.auth import get_current_session


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """
    Build a client for the system router alone, authentication stubbed.

    Args:
        monkeypatch: Patching helper, scoped to the test.

    Returns:
        A client whose requests are already authenticated.
    """
    app = FastAPI()
    app.include_router(system_api.router, prefix="/api/system")
    app.dependency_overrides[get_current_session] = lambda: {"session_id": "test", "scope": "read"}
    return TestClient(app)


def _stub(monkeypatch: pytest.MonkeyPatch, report: HealthReport) -> None:
    """
    Replace :func:`wasm.managers.health.collect_health_report` with a stub.

    Args:
        monkeypatch: Patching helper, scoped to the test.
        report: The value the stub returns.
    """
    monkeypatch.setattr(system_api, "collect_health_report", lambda **_kwargs: report)


def test_a_healthy_server_reports_the_healthy_verdict(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    report = HealthReport(
        disk=HealthCheck("Disk Space", "150.0GB free / 200.0GB total (25% used)", "ok"),
        nginx=HealthCheck("Nginx", "Running", "ok"),
        apache=HealthCheck("Apache", "Not installed", "info"),
        applications=HealthCheck("Applications", "No applications deployed", "info"),
        certificates=HealthCheck("SSL Certificates", "None configured", "info"),
        memory=HealthCheck("Memory", "12.0GB free / 16.0GB total (25% used)", "ok"),
    )
    _stub(monkeypatch, report)

    response = client.get("/api/system/health")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["verdict"] == "healthy"
    assert body["issues"] == []
    assert body["warnings"] == []
    assert {"name": "Nginx", "value": "Running", "status": "ok"} in body["checks"]
    assert len(body["checks"]) == 6


def test_the_disk_check_can_be_absent(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """Disk usage that could not be read at all drops out rather than lying."""
    report = HealthReport(
        disk=None,
        nginx=HealthCheck("Nginx", "Running", "ok"),
        apache=HealthCheck("Apache", "Not installed", "info"),
        applications=HealthCheck("Applications", "No applications deployed", "info"),
        certificates=HealthCheck("SSL Certificates", "None configured", "info"),
        memory=HealthCheck("Memory", "12.0GB free / 16.0GB total (25% used)", "ok"),
        warnings=["Could not check disk space: [Errno 13] Permission denied"],
    )
    _stub(monkeypatch, report)

    response = client.get("/api/system/health")

    body = response.json()
    assert len(body["checks"]) == 5
    assert not any(check["name"] == "Disk Space" for check in body["checks"])
    assert body["verdict"] == "warning"


def test_an_issue_reports_the_error_verdict(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    report = HealthReport(
        disk=HealthCheck("Disk Space", "0.5GB free / 200.0GB total (99% used)", "error"),
        nginx=HealthCheck("Nginx", "Stopped", "error"),
        apache=HealthCheck("Apache", "Not installed", "info"),
        applications=HealthCheck("Applications", "No applications deployed", "info"),
        certificates=HealthCheck("SSL Certificates", "None configured", "info"),
        memory=HealthCheck("Memory", "12.0GB free / 16.0GB total (25% used)", "ok"),
        issues=["Low disk space: 0.5GB free", "Nginx is installed but not running"],
    )
    _stub(monkeypatch, report)

    response = client.get("/api/system/health")

    body = response.json()
    assert body["verdict"] == "error"
    assert body["issues"] == ["Low disk space: 0.5GB free", "Nginx is installed but not running"]


def test_the_route_requires_a_session(sandbox: Path, runner: FakeRunner) -> None:
    """Through the real application: an anonymous request must be refused."""
    client = build_client(sandbox)

    response = client.get("/api/system/health")

    assert response.status_code == 401
