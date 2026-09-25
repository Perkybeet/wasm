# Copyright (c) 2024-2026 Yago Lopez Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
Tests for ``GET /api/apps/{domain}/diagnose``.

The route is a thin translation of :func:`wasm.managers.diagnose.diagnose` to
HTTP - the correlation rules are pinned in ``tests/test_diagnose.py`` and are
not repeated here. What this module owns: the endpoint calls the shared
function with the validated domain, requires a session, and serialises the
``Diagnosis`` it gets back field for field, the same shape ``wasm diagnose
--json`` prints.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.test_web_auth import build_client
from wasm.core.runner import FakeRunner
from wasm.managers.diagnose import Check, Diagnosis
from wasm.web.api import diagnose as diagnose_api
from wasm.web.api.auth import get_current_session


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """
    Build a client for the diagnose router alone, authentication stubbed.

    Args:
        monkeypatch: Patching helper, scoped to the test.

    Returns:
        A client whose requests are already authenticated.
    """
    app = FastAPI()
    app.include_router(diagnose_api.router, prefix="/api/apps")
    app.dependency_overrides[get_current_session] = lambda: {"session_id": "test", "scope": "read"}
    return TestClient(app)


def _stub(monkeypatch: pytest.MonkeyPatch, diagnosis: Diagnosis) -> list[str]:
    """
    Replace :func:`wasm.managers.diagnose.diagnose` with a stub.

    Args:
        monkeypatch: Patching helper, scoped to the test.
        diagnosis: The value the stub returns.

    Returns:
        The domains the stub was called with, in call order.
    """
    calls: list[str] = []

    def fake_diagnose(domain: str, **_kwargs: Any) -> Diagnosis:
        calls.append(domain)
        return diagnosis

    monkeypatch.setattr(diagnose_api, "diagnose", fake_diagnose)
    return calls


def test_the_route_serves_the_diagnosis_the_cli_would_print(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The JSON shape matches ``Diagnosis`` field for field, like ``--json`` does."""
    diagnosis = Diagnosis(
        domain="example.com",
        verdict="down",
        probable_cause="The systemd unit is not running.",
        checks=(
            Check(name="unit", status="fail", summary="Unit is inactive", evidence="inactive\n"),
            Check(name="port", status="skip", summary="Could not complete this check"),
        ),
    )
    calls = _stub(monkeypatch, diagnosis)

    response = client.get("/api/apps/example.com/diagnose")

    assert response.status_code == 200, response.text
    assert calls == ["example.com"]
    assert response.json() == {
        "domain": "example.com",
        "verdict": "down",
        "probable_cause": "The systemd unit is not running.",
        "checks": [
            {
                "name": "unit",
                "status": "fail",
                "summary": "Unit is inactive",
                "evidence": "inactive\n",
            },
            {
                "name": "port",
                "status": "skip",
                "summary": "Could not complete this check",
                "evidence": "",
            },
        ],
    }


def test_a_clean_diagnosis_has_no_probable_cause(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``None`` survives the round trip as JSON ``null``, not a missing key or empty string."""
    diagnosis = Diagnosis(domain="example.com", verdict="healthy", probable_cause=None, checks=())
    _stub(monkeypatch, diagnosis)

    response = client.get("/api/apps/example.com/diagnose")

    assert response.status_code == 200
    assert response.json()["probable_cause"] is None
    assert response.json()["checks"] == []


def test_an_unknown_domain_is_not_a_404(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    Exactly like the CLI: an app the store has never heard of is not refused,
    the probes report it as the likely cause instead.
    """
    diagnosis = Diagnosis(
        domain="ghost.example.com",
        verdict="down",
        probable_cause="No application is deployed at this domain.",
        checks=(),
    )
    _stub(monkeypatch, diagnosis)

    response = client.get("/api/apps/ghost.example.com/diagnose")

    assert response.status_code == 200
    assert response.json()["probable_cause"] == "No application is deployed at this domain."


def test_the_domain_is_validated_before_it_reaches_diagnose(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A traversal payload never reaches the probes."""
    calls = _stub(
        monkeypatch, Diagnosis(domain="x", verdict="healthy", probable_cause=None, checks=())
    )

    response = client.get("/api/apps/..%2f..%2fetc%2fpasswd/diagnose")

    assert response.status_code in (400, 404)
    assert calls == []


def test_the_route_requires_a_session(sandbox: Path, runner: FakeRunner) -> None:
    """
    Through the real application, not the bare router: only ``create_app``
    initialises the token manager ``require_auth`` needs to tell "no
    credential" apart from "server not configured".
    """
    client = build_client(sandbox)

    response = client.get("/api/apps/example.com/diagnose")

    assert response.status_code == 401
