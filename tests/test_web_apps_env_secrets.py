# Copyright (c) 2024-2026 Yago Lopez Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
Tests for the secrecy contract of ``GET``/``PUT /api/apps/{domain}/env``.

Ported from the panel's deleted server-rendered views, which exercised this
same endpoint through a page rather than directly: the environment editor was
a thin client of it, and the file-layout tests in
tests/test_web_app_engine.py cover where the ``.env`` lands. What is defended
here is what a browser or any other client of the JSON API is allowed to see:

- A secret-looking name (``PASSWORD``, ``TOKEN``, ``KEY``, ...) is masked by
  default, and only comes back in clear for ``?unmask=true`` from an elevated
  admin session - audited, but never with the value itself in the trail.
- A write that would put an unsafe name into a systemd unit is refused before
  anything reaches disk, and the audit line names which keys changed, never
  what they changed to.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from wasm.core.store import App, WASMStore
from wasm.web.auth import CSRF_HEADER_NAME, SecurityConfig
from wasm.web.server import create_app, get_token_manager

#: A secret planted in a value, to prove it never reaches a masked response.
ENV_SECRET = "sk-live-openai-hunter2"


@pytest.fixture
def store(tmp_path: Path) -> Iterator[WASMStore]:
    """
    Args:
        tmp_path: Per-test temporary directory.

    Yields:
        A store of this test's own.
    """
    WASMStore.reset_instance()
    instance = WASMStore(tmp_path / "wasm.db")
    try:
        yield instance
    finally:
        instance.close()
        WASMStore.reset_instance()


@pytest.fixture
def app(tmp_path: Path, store: WASMStore, runner: object) -> FastAPI:
    """
    Args:
        tmp_path: Per-test temporary directory.
        store: The store fixture.
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
        A signed-in client carrying the CSRF header.
    """
    signed_in = TestClient(app, client=("testclient", 50000), follow_redirects=False)
    token = get_token_manager().generate_master_token()
    response = signed_in.post("/api/auth/login", json={"token": token})
    assert response.status_code == 200, response.text
    signed_in.headers[CSRF_HEADER_NAME] = response.json()["csrf_token"]
    return signed_in


def elevate(client: TestClient) -> None:
    """
    Confirm sudo mode for the signed-in client.

    Unmasking an environment and writing one both require elevation; see
    tests/test_web_sudo.py for the gate itself. This module is about the
    endpoint's own behaviour, so it confirms once per test that needs it.

    Args:
        client: A signed-in client.
    """
    response = client.post(
        "/api/auth/elevate", json={"token": get_token_manager().generate_master_token()}
    )
    assert response.status_code == 200, response.text


def deployed_env(
    store: WASMStore, tmp_path: Path, domain: str = "example.com", env_text: str = ""
) -> Path:
    """
    Deploy an application whose ``.env`` file lives inside the sandbox.

    On the default (in-place) layout the environment endpoint reads and
    writes ``<app_path>/.env`` directly, so the application directory has to
    exist on disk for the round trip to mean anything.

    Args:
        store: The store to write the application record to.
        tmp_path: Per-test temporary directory.
        domain: The application's domain.
        env_text: Content to write to its ``.env`` file, when not empty.

    Returns:
        The application's directory.
    """
    app_dir = tmp_path / "apps" / domain
    app_dir.mkdir(parents=True, exist_ok=True)
    if env_text:
        (app_dir / ".env").write_text(env_text, encoding="utf-8")
    store.create_app(
        App(
            domain=domain,
            app_type="nextjs",
            source="https://github.com/you/app",
            port=3000,
            app_path=str(app_dir),
            status="running",
        )
    )
    return app_dir


def read_audit(tmp_path: Path) -> list[dict[str, Any]]:
    """
    Read the audit log written during a test.

    Args:
        tmp_path: Per-test temporary directory, where the panel's state lives.

    Returns:
        One dict per audit line, oldest first. Empty when nothing was audited.
    """
    import json

    path = tmp_path / "state" / "web-audit.log"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def test_get_app_env_redacts_secret_looking_keys_by_default(
    client: TestClient, store: WASMStore, tmp_path: Path
) -> None:
    """SECRET/TOKEN/PASSWORD/KEY-shaped names come back as the fixed placeholder."""
    deployed_env(
        store,
        tmp_path,
        env_text=(
            f"API_KEY={ENV_SECRET}\n"
            "AUTH_TOKEN=t0ken-value\n"
            "ADMIN_PASSWORD=correcthorse\n"
            "APP_SECRET=hunter2\n"
            "PORT=3000\n"
        ),
    )

    response = client.get("/api/apps/example.com/env")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["unmasked"] is False
    variables = body["variables"]
    assert variables["API_KEY"] == "***"
    assert variables["AUTH_TOKEN"] == "***"
    assert variables["ADMIN_PASSWORD"] == "***"
    assert variables["APP_SECRET"] == "***"
    # A harmless value stays readable, or the endpoint is useless.
    assert variables["PORT"] == "3000"


def test_get_app_env_unmask_returns_clear_values_and_is_audited(
    client: TestClient, store: WASMStore, tmp_path: Path
) -> None:
    """``?unmask=true`` is the explicit request, and it leaves a trail."""
    deployed_env(store, tmp_path, env_text=f"API_KEY={ENV_SECRET}\n")
    elevate(client)

    response = client.get("/api/apps/example.com/env", params={"unmask": "true"})

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["unmasked"] is True
    assert body["variables"]["API_KEY"] == ENV_SECRET

    reveals = [e for e in read_audit(tmp_path) if e["action"] == "apps.env.reveal"]
    assert reveals, "reading the environment in clear must be audited"
    assert reveals[0]["result"] == "success"
    assert ENV_SECRET not in (tmp_path / "state" / "web-audit.log").read_text()


def test_put_app_env_rewrites_the_file_and_reports_restart_required(
    client: TestClient, store: WASMStore, tmp_path: Path
) -> None:
    """A full roundtrip: the file on disk becomes exactly what was sent."""
    import stat

    app_dir = deployed_env(store, tmp_path, env_text="API_KEY=old\n")
    elevate(client)

    response = client.put(
        "/api/apps/example.com/env",
        json={"variables": {"API_KEY": "new", "PORT": "3000"}},
    )

    assert response.status_code == 200, response.text
    assert response.json() == {"domain": "example.com", "restart_required": True}
    assert (app_dir / ".env").read_text(encoding="utf-8") == "API_KEY=new\nPORT=3000\n"
    assert stat.S_IMODE((app_dir / ".env").stat().st_mode) == 0o600


def test_put_app_env_rejects_an_invalid_name_with_422(
    client: TestClient, store: WASMStore, tmp_path: Path
) -> None:
    """A name that could inject a systemd directive is refused before any write."""
    app_dir = deployed_env(store, tmp_path, env_text="API_KEY=old\n")
    elevate(client)

    response = client.put("/api/apps/example.com/env", json={"variables": {"BAD NAME": "x"}})

    assert response.status_code == 422, response.text
    assert "Invalid environment variable name" in response.text
    assert (app_dir / ".env").read_text(encoding="utf-8") == "API_KEY=old\n"


def test_put_app_env_never_writes_a_value_to_the_audit_log(
    client: TestClient, store: WASMStore, tmp_path: Path
) -> None:
    """The audit line names the changed keys; the values never appear anywhere near it."""
    deployed_env(store, tmp_path, env_text="API_KEY=old\n")
    elevate(client)

    response = client.put(
        "/api/apps/example.com/env",
        json={"variables": {"API_KEY": ENV_SECRET, "NEW_ONE": "another-secret-value"}},
    )
    assert response.status_code == 200, response.text

    raw_audit = (tmp_path / "state" / "web-audit.log").read_text()
    assert ENV_SECRET not in raw_audit
    assert "another-secret-value" not in raw_audit

    updates = [e for e in read_audit(tmp_path) if e["action"] == "apps.env.update"]
    assert updates, "saving the environment must be audited"
    assert "API_KEY" in updates[0]["detail"]
    assert "NEW_ONE" in updates[0]["detail"]
