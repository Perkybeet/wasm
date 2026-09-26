# Copyright (c) 2024-2026 Yago Lopez Prado
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Tests that audit lines name the real actor, never "unknown".

A session payload carries its id under ``sid`` (see
:func:`wasm.web.auth.require_auth`); the databases, cron and backup schedule
audit lines used to read ``session.get("session_id", "unknown")``, a key no
session payload has ever carried, so every one of those lines printed
``session=unknown`` regardless of who was signed in.
:func:`wasm.web.auth.actor_label` is the one place a session payload becomes
a short, safe label, and this module drives one destructive action per area
through the real ``wasm.audit`` logger to prove the label is now correct -
for a cookie session it is the first twelve characters of the session id,
which ``GET /api/auth/sessions`` also reports unmasked as ``current_session``
for the caller's own session; for the master token presented as a Bearer
credential it is the literal ``master``.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.test_web_databases_api import make_engine, wire
from wasm.core.runner import FakeRunner
from wasm.core.store import WASMStore
from wasm.managers.backup_scheduler import BackupScheduler
from wasm.managers.cron_manager import CronManager
from wasm.web.auth import CSRF_HEADER_NAME, SecurityConfig
from wasm.web.server import create_app, get_token_manager

#: Root every "no offending pattern remains" guard test scans.
WEB_PACKAGE_ROOT = Path(__file__).resolve().parent.parent / "src" / "wasm" / "web"


@pytest.fixture
def store(tmp_path: Path):
    """
    Give the panel a store of its own.

    Args:
        tmp_path: Per-test temporary directory.

    Yields:
        The store the API reads and writes.
    """
    WASMStore.reset_instance()
    instance = WASMStore(tmp_path / "wasm.db")
    try:
        yield instance
    finally:
        instance.close()
        WASMStore.reset_instance()


@pytest.fixture
def app(tmp_path: Path, store: WASMStore, runner: FakeRunner) -> FastAPI:
    """
    Args:
        tmp_path: Per-test temporary directory.
        store: The store fixture, so the application and the test share one.
        runner: The fake command runner, so no manager reaches a real process.

    Returns:
        The configured application.
    """
    return create_app(SecurityConfig(state_dir=tmp_path / "state", rate_limit_requests=5000))


@pytest.fixture
def master_token(app: FastAPI) -> str:
    """
    Returns:
        A freshly generated master token for the application under test.
    """
    return get_token_manager().generate_master_token()


@pytest.fixture
def client(app: FastAPI, master_token: str) -> TestClient:
    """
    A cookie session signed in with the master token.

    Args:
        app: The application.
        master_token: The credential to log in with.

    Returns:
        A client carrying a session cookie and the matching CSRF header.
    """
    signed_in = TestClient(app, client=("testclient", 50000), follow_redirects=False)
    response = signed_in.post("/api/auth/login", json={"token": master_token})
    assert response.status_code == 200, response.text
    signed_in.headers[CSRF_HEADER_NAME] = response.json()["csrf_token"]
    return signed_in


def elevate(client: TestClient, master_token: str) -> None:
    """
    Confirm sudo mode for the signed-in client.

    Args:
        client: A signed-in client.
        master_token: The credential the session was signed in with.
    """
    response = client.post("/api/auth/elevate", json={"token": master_token})
    assert response.status_code == 200, response.text


def current_sid(client: TestClient) -> str:
    """
    Read the caller's own session id, unmasked.

    ``GET /api/auth/sessions`` is the one place a session's own id leaves the
    server whole - every other session in the listing only ever gets a
    prefix - which is what makes it useful here: the test can compute the
    exact label :func:`~wasm.web.auth.actor_label` should have logged.

    Args:
        client: A signed-in client.

    Returns:
        The session id.
    """
    response = client.get("/api/auth/sessions")
    assert response.status_code == 200, response.text
    sid = response.json()["current_session"]
    assert sid
    return sid


def audit_line(caplog: pytest.LogCaptureFixture, marker: str) -> str:
    """
    Find the one ``wasm.audit`` record carrying a marker, and return its text.

    Args:
        caplog: The captured log records.
        marker: Substring identifying the audit line under test.

    Returns:
        The record's rendered message.
    """
    matches = [
        record.message
        for record in caplog.records
        if record.name == "wasm.audit" and marker in record.message
    ]
    assert len(matches) == 1, f"expected exactly one {marker!r} audit line, got {matches}"
    return matches[0]


@pytest.fixture
def cron_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """
    Point the cron manager's unit directory into the sandbox.

    Args:
        tmp_path: Per-test temporary directory.
        monkeypatch: Patching helper, scoped to the test.

    Returns:
        The directory unit files land in.
    """
    path = tmp_path / "cron-systemd"
    path.mkdir()
    monkeypatch.setattr(CronManager, "SYSTEMD_DIR", path)
    return path


def write_owned_cron_job(cron_dir: Path) -> None:
    """
    Write a cron unit pair the way the manager itself would, marker included.

    Args:
        cron_dir: The patched unit directory.
    """
    (cron_dir / "wasm-cron-cleanup.timer").write_text(
        "# Generated by WASM\n[Timer]\nOnCalendar=*-*-* 02:00:00\n"
    )
    (cron_dir / "wasm-cron-cleanup.service").write_text(
        "# Generated by WASM\n[Service]\nUser=www-data\nExecStart=/usr/bin/true\n"
    )


def test_a_sql_query_names_the_cookie_session_actor(
    client: TestClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A statement run from a cookie session is audited under that session, not "unknown"."""
    wire(monkeypatch, [make_engine(tmp_path / "dumps")])
    sid = current_sid(client)

    with caplog.at_level(logging.INFO, logger="wasm.audit"):
        response = client.post(
            "/api/databases/query",
            json={"database": "appdb", "engine": "postgresql", "query": "SELECT 1"},
        )

    assert response.status_code == 200, response.text
    line = audit_line(caplog, "query engine=")
    assert f"session={sid[:12]}" in line
    assert "session=unknown" not in line


def test_a_sql_query_names_the_master_token_actor(
    app: FastAPI,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A statement run with the master token as a Bearer credential is audited as "master"."""
    wire(monkeypatch, [make_engine(tmp_path / "dumps")])
    token = get_token_manager().generate_master_token()
    anon = TestClient(app, client=("testclient", 50000), follow_redirects=False)

    with caplog.at_level(logging.INFO, logger="wasm.audit"):
        response = anon.post(
            "/api/databases/query",
            json={"database": "appdb", "engine": "postgresql", "query": "SELECT 1"},
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 200, response.text
    line = audit_line(caplog, "query engine=")
    assert "session=master" in line
    assert "session=unknown" not in line


def test_deleting_a_cron_job_names_the_cookie_session_actor(
    client: TestClient,
    master_token: str,
    cron_dir: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Deleting a cron job is audited under the elevated cookie session, not "unknown"."""
    write_owned_cron_job(cron_dir)
    sid = current_sid(client)
    elevate(client, master_token)

    with caplog.at_level(logging.INFO, logger="wasm.audit"):
        response = client.delete("/api/cron/cleanup")

    assert response.status_code == 200, response.text
    line = audit_line(caplog, "delete_cron_job")
    assert f"session={sid[:12]}" in line
    assert "session=unknown" not in line


def test_deleting_a_backup_schedule_names_the_cookie_session_actor(
    client: TestClient,
    master_token: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Deleting a backup schedule is audited under the elevated cookie session, not "unknown"."""
    monkeypatch.setattr(BackupScheduler, "SYSTEMD_DIR", tmp_path / "backup-systemd")
    sid = current_sid(client)
    elevate(client, master_token)

    with caplog.at_level(logging.INFO, logger="wasm.audit"):
        response = client.delete("/api/backup-schedules/example.com")

    assert response.status_code == 200, response.text
    line = audit_line(caplog, "delete_backup_schedule")
    assert f"session={sid[:12]}" in line
    assert "session=unknown" not in line


def test_no_audit_call_site_under_web_reads_the_nonexistent_session_id_key() -> None:
    """
    Guard against the regression coming back.

    A session payload has never carried a ``session_id`` key - only ``sid`` -
    so any code under ``src/wasm/web`` that reads it is dead code shaped
    exactly like the bug this file exists to catch.
    """
    offenders = [
        str(path.relative_to(WEB_PACKAGE_ROOT.parent.parent.parent))
        for path in WEB_PACKAGE_ROOT.rglob("*.py")
        if 'session.get("session_id"' in path.read_text(encoding="utf-8")
    ]
    assert offenders == [], f"still reading the nonexistent session_id key: {offenders}"
