# Copyright (c) 2024-2026 Yago Lopez Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
Tests for taking and checking backups from the panel.

The panel could restore a backup and delete one, and could neither take one nor
check that one was sound. Both matter more than restoring does: the moment an
operator wants a backup is immediately before doing something risky, and the
moment they find out an archive is corrupt must not be the moment they need it.

The load-bearing test here is the one about a failed verification. The API
answers 200 with ``valid: false`` for a corrupt archive, which is right for a
JSON client and wrong for a button: reported through the panel's success path
it would say "is sound", in the same green it uses for a restart that worked,
about an archive that cannot be restored. A backup nobody can restore is worse
than no backup, because it is the one people are counting on.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from wasm.core.store import App, WASMStore
from wasm.web.auth import CSRF_HEADER_NAME, SecurityConfig
from wasm.web.server import create_app as build_app
from wasm.web.server import get_token_manager


@pytest.fixture
def store(tmp_path: Path) -> Any:
    """
    Args:
        tmp_path: Per-test temporary directory.

    Yields:
        A store of this test's own.
    """
    WASMStore.reset_instance()
    instance = WASMStore(tmp_path / "wasm.db")
    instance.create_app(
        App(
            domain="example.com",
            app_type="nextjs",
            source="https://github.com/you/app",
            port=3000,
            app_path="/var/www/apps/example.com",
            status="running",
        )
    )
    try:
        yield instance
    finally:
        instance.close()
        WASMStore.reset_instance()


@pytest.fixture
def app(tmp_path: Path, store: Any, runner: object) -> FastAPI:
    """
    Args:
        tmp_path: Per-test temporary directory.
        store: The store fixture.
        runner: The fake command runner.

    Returns:
        The application.
    """
    return build_app(SecurityConfig(state_dir=tmp_path / "state", rate_limit_requests=5000))


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


@pytest.fixture
def queued(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """
    Capture the backup job instead of running one.

    Args:
        monkeypatch: Patching helper, scoped to the test.

    Returns:
        The jobs that were queued.
    """
    captured: list[dict[str, Any]] = []

    def create_job(**kwargs: Any) -> Any:
        """
        Args:
            **kwargs: The job description.

        Returns:
            Something shaped like a queued job.
        """
        captured.append(kwargs)
        return type(
            "Queued",
            (),
            {
                "id": "job-1",
                "status": type("S", (), {"value": "pending"})(),
                "to_dict": lambda self: {"id": "job-1"},
            },
        )()

    monkeypatch.setattr(
        "wasm.web.api.backups.get_job_manager",
        lambda: type("M", (), {"create_job": staticmethod(create_job)})(),
    )
    return captured


def verification(monkeypatch: pytest.MonkeyPatch, **result: Any) -> None:
    """
    Make the backup checker report a fixed verdict.

    Args:
        monkeypatch: Patching helper, scoped to the test.
        **result: Fields of the verdict to report.
    """
    from wasm.web.api.backups import VerifyBackupResponse

    verdict = {"valid": True, "checksum_ok": True, "files_ok": True, "errors": [], "warnings": []}
    verdict.update(result)

    def verify(backup_id: str, session: Any) -> VerifyBackupResponse:
        """
        Stand in for the API endpoint the adapter calls.

        The adapter is what is under test: whether a verdict of "not valid"
        reaches the operator as a failure rather than through the success path.

        Args:
            backup_id: The archive being checked.
            session: The authenticated session.

        Returns:
            The verdict, in the shape the endpoint returns it.
        """
        return VerifyBackupResponse(backup_id=backup_id, **verdict)

    monkeypatch.setattr("wasm.web.api.backups.verify_backup", verify)


# ---------------------------------------------------------------------------
# Taking one
# ---------------------------------------------------------------------------


def test_an_application_can_be_backed_up_from_its_row(
    client: TestClient, queued: list[dict[str, Any]]
) -> None:
    """The panel could restore and delete backups, and never make one."""
    response = client.post("/apps/example.com/backup")

    assert response.status_code == 204
    assert len(queued) == 1


def test_the_backup_names_the_application_it_is_of(
    client: TestClient, queued: list[dict[str, Any]]
) -> None:
    """
    Args:
        client: A signed-in client.
        queued: Captured jobs.
    """
    client.post("/apps/example.com/backup")

    assert queued[0]["kwargs"]["domain"] == "example.com"


def test_the_applications_screen_offers_the_backup(client: TestClient) -> None:
    """
    The moment an operator wants a backup is immediately before something
    risky, which is when they are looking at the application.

    Args:
        client: A signed-in client.
    """
    body = client.get("/apps").text

    assert "/apps/example.com/backup" in body
    assert "Back up" in body


def test_taking_a_backup_demands_a_session(app: FastAPI) -> None:
    """
    Args:
        app: The application.
    """
    anonymous = TestClient(app, client=("testclient", 50000), follow_redirects=False)

    assert anonymous.post("/apps/example.com/backup").status_code in (303, 401, 403)


# ---------------------------------------------------------------------------
# Checking one
# ---------------------------------------------------------------------------


def test_a_sound_archive_verifies_quietly(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    Args:
        client: A signed-in client.
        monkeypatch: Patching helper, scoped to the test.
    """
    verification(monkeypatch, valid=True)

    response = client.post("/backups/example-com_20260101_120000/verify")

    assert response.status_code == 204


def test_a_corrupt_archive_is_not_reported_as_sound(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    The test this file exists for.

    The API answers 200 with valid: false, and the panel's success path would
    have said "is sound" in green about an archive that cannot be restored.
    """
    verification(monkeypatch, valid=False, checksum_ok=False, errors=["checksum mismatch"])

    response = client.post("/backups/example-com_20260101_120000/verify")

    assert response.status_code >= 400, "a corrupt archive was reported as a success"


def test_a_failed_verification_says_what_was_found(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    "Verification failed" sends the operator to a terminal. The checker's own
    finding is what tells them whether the archive is worth anything.

    Args:
        client: A signed-in client.
        monkeypatch: Patching helper, scoped to the test.
    """
    verification(monkeypatch, valid=False, errors=["checksum mismatch on payload.tar.gz"])

    body = client.post("/backups/example-com_20260101_120000/verify").text

    assert "checksum mismatch on payload.tar.gz" in body


def test_a_failed_verification_names_the_archive(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    An operator checking several archives needs to know which one failed.

    Args:
        client: A signed-in client.
        monkeypatch: Patching helper, scoped to the test.
    """
    verification(monkeypatch, valid=False, errors=["truncated"])

    body = client.post("/backups/example-com_20260101_120000/verify").text

    assert "example-com_20260101_120000" in body


def test_an_archive_with_only_warnings_still_fails_loudly(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    ``valid`` is the verdict. A checker that says "not valid" while listing
    only warnings is still saying the archive cannot be trusted.

    Args:
        client: A signed-in client.
        monkeypatch: Patching helper, scoped to the test.
    """
    verification(monkeypatch, valid=False, errors=[], warnings=["missing manifest entry"])

    response = client.post("/backups/example-com_20260101_120000/verify")

    assert response.status_code >= 400
    assert "missing manifest entry" in response.text
