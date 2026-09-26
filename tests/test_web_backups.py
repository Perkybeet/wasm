# Copyright (c) 2024-2026 Yago Lopez Prado
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Tests for taking and checking backups through the JSON API.

The panel could restore a backup and delete one, and could neither take one nor
check that one was sound. Both matter more than restoring does: the moment an
operator wants a backup is immediately before doing something risky, and the
moment they find out an archive is corrupt must not be the moment they need it.

The test that matters most is the one about a failed verification: the API
answers 200 with ``valid: false`` for a corrupt archive, on purpose, so a JSON
client can branch on the field - but the field itself has to carry the true
verdict, or a corrupt archive reads no differently from a sound one. A backup
nobody can restore is worse than no backup, because it is the one people are
counting on.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
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
def master_token(app: FastAPI) -> str:
    """
    Args:
        app: The application.

    Returns:
        A freshly generated master token for the application under test.
    """
    return get_token_manager().generate_master_token()


@pytest.fixture
def client(app: FastAPI, master_token: str) -> TestClient:
    """
    Args:
        app: The application.
        master_token: The credential to log in with.

    Returns:
        A signed-in client carrying the CSRF header, not yet elevated.
    """
    signed_in = TestClient(app, client=("testclient", 50000), follow_redirects=False)
    response = signed_in.post("/api/auth/login", json={"token": master_token})
    assert response.status_code == 200, response.text
    signed_in.headers[CSRF_HEADER_NAME] = response.json()["csrf_token"]
    return signed_in


def elevate(client: TestClient, master_token: str) -> None:
    """
    Confirm sudo mode on an already signed-in client.

    Args:
        client: A signed-in client.
        master_token: The same credential the client logged in with; two-factor
            authentication is never enabled in a test that calls this.
    """
    response = client.post("/api/auth/elevate", json={"token": master_token})
    assert response.status_code == 200, response.text


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
    Make the backup checker report a fixed verdict for any identifier asked.

    Replaces :class:`~wasm.web.api.backups.BackupManager` itself rather than
    the endpoint function: the endpoint is reached through a real request
    here, and FastAPI already holds its own reference to the original
    function by the time a test runs, so patching the module attribute the
    route was built from would have no effect on it.

    Args:
        monkeypatch: Patching helper, scoped to the test.
        **result: Fields of the verdict to report.
    """
    verdict = {"valid": True, "checksum_ok": True, "files_ok": True, "errors": [], "warnings": []}
    verdict.update(result)

    class FakeBackup:
        """Stands in for the metadata :meth:`get_backup` would load."""

        def __init__(self, backup_id: str) -> None:
            self.id = backup_id

    class FakeManager:
        """Stands in for the manager, so no real archive has to exist on disk."""

        def __init__(self, verbose: bool = False) -> None:
            pass

        def get_backup(self, backup_id: str) -> FakeBackup:
            return FakeBackup(backup_id)

        def verify(self, backup_id: str) -> dict[str, Any]:
            return verdict

    monkeypatch.setattr("wasm.web.api.backups.BackupManager", FakeManager)


# ---------------------------------------------------------------------------
# Taking one
# ---------------------------------------------------------------------------


def test_creating_a_backup_queues_the_job(client: TestClient, queued: list[dict[str, Any]]) -> None:
    """The panel could restore and delete backups, and never make one."""
    response = client.post("/api/backups", json={"domain": "example.com"})

    assert response.status_code == 202, response.text
    assert len(queued) == 1


def test_the_queued_backup_names_the_application_it_is_of(
    client: TestClient, queued: list[dict[str, Any]]
) -> None:
    """
    Args:
        client: A signed-in client.
        queued: Captured jobs.
    """
    client.post("/api/backups", json={"domain": "example.com"})

    assert queued[0]["kwargs"]["domain"] == "example.com"


def test_taking_a_backup_demands_a_session(app: FastAPI) -> None:
    """
    Args:
        app: The application.
    """
    anonymous = TestClient(app, client=("testclient", 50000), follow_redirects=False)

    response = anonymous.post("/api/backups", json={"domain": "example.com"})

    assert response.status_code in (401, 403)


# ---------------------------------------------------------------------------
# Checking one
# ---------------------------------------------------------------------------


def test_a_sound_archive_verifies_as_valid(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    Args:
        client: A signed-in client.
        monkeypatch: Patching helper, scoped to the test.
    """
    verification(monkeypatch, valid=True)

    response = client.post("/api/backups/example-com_20260101_120000/verify")

    assert response.status_code == 200, response.text
    assert response.json()["valid"] is True


def test_a_corrupt_archive_is_reported_as_invalid(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    ``valid: false`` is a 200 for a JSON client to branch on - unlike the
    deleted panel's own button, which translated it into an HTTP failure so
    it would not report through the same success path as a restart that
    worked. That translation was the page's job; a JSON client reads the
    field directly, and the field itself is what this guards.
    """
    verification(monkeypatch, valid=False, checksum_ok=False, errors=["checksum mismatch"])

    response = client.post("/api/backups/example-com_20260101_120000/verify")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["valid"] is False
    assert body["checksum_ok"] is False
    assert body["backup_id"] == "example-com_20260101_120000"
    assert "checksum mismatch" in body["errors"]


def backup_manager_stub(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, ...]]:
    """
    Install a fake BackupManager for the restore/delete elevation tests.

    Args:
        monkeypatch: Patching helper, scoped to the test.

    Returns:
        The calls the fake manager's mutating methods recorded.
    """
    calls: list[tuple[str, ...]] = []

    class FakeBackup:
        """Stands in for the metadata :meth:`get_backup` would load."""

        def __init__(self, backup_id: str) -> None:
            self.id = backup_id
            self.domain = "example.com"

    class FakeManager:
        """Stands in for the manager, so no real archive has to exist on disk."""

        def __init__(self, verbose: bool = False) -> None:
            pass

        def get_backup(self, backup_id: str) -> FakeBackup:
            return FakeBackup(backup_id)

        def delete(self, backup_id: str) -> None:
            calls.append(("delete", backup_id))

    monkeypatch.setattr("wasm.web.api.backups.BackupManager", FakeManager)
    return calls


# ---------------------------------------------------------------------------
# Restoring and deleting need sudo mode from a cookie session
# ---------------------------------------------------------------------------


def test_restore_from_a_fresh_cookie_session_is_refused(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, queued: list[dict[str, Any]]
) -> None:
    """Restoring overwrites the target app: D5 treats it like deleting one."""
    backup_manager_stub(monkeypatch)

    response = client.post("/api/backups/example-com_20260101_120000/restore")

    assert response.status_code == 403, response.text
    assert response.json()["error"] == "elevation_required"
    assert queued == []


def test_restore_after_elevation_is_queued(
    client: TestClient,
    master_token: str,
    monkeypatch: pytest.MonkeyPatch,
    queued: list[dict[str, Any]],
) -> None:
    """Once confirmed, the same cookie session may queue the restore."""
    backup_manager_stub(monkeypatch)
    elevate(client, master_token)

    response = client.post("/api/backups/example-com_20260101_120000/restore")

    assert response.status_code == 202, response.text
    assert len(queued) == 1


def test_delete_from_a_fresh_cookie_session_is_refused(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Deletion is irreversible: it needs the same confirmation as restoring."""
    calls = backup_manager_stub(monkeypatch)

    response = client.delete("/api/backups/example-com_20260101_120000")

    assert response.status_code == 403, response.text
    assert response.json()["error"] == "elevation_required"
    assert calls == []


def test_delete_after_elevation_succeeds(
    client: TestClient, master_token: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Once confirmed, the same cookie session may delete the backup."""
    calls = backup_manager_stub(monkeypatch)
    elevate(client, master_token)

    response = client.delete("/api/backups/example-com_20260101_120000")

    assert response.status_code == 200, response.text
    assert ("delete", "example-com_20260101_120000") in calls


def test_an_admin_api_token_is_not_asked_to_elevate_to_restore(
    app: FastAPI, monkeypatch: pytest.MonkeyPatch, queued: list[dict[str, Any]]
) -> None:
    """An explicit automation credential is exempt, the same as everywhere else."""
    backup_manager_stub(monkeypatch)
    admin_token = get_token_manager().create_api_token("automation", "admin")["token"]
    bearer = TestClient(app, client=("testclient", 50000), follow_redirects=False)

    response = bearer.post(
        "/api/backups/example-com_20260101_120000/restore",
        headers={"Authorization": f"Bearer {admin_token}"},
    )

    assert response.status_code == 202, response.text
    assert len(queued) == 1


def test_an_archive_with_only_warnings_is_still_reported_as_invalid(
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

    response = client.post("/api/backups/example-com_20260101_120000/verify")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["valid"] is False
    assert body["warnings"] == ["missing manifest entry"]


# ---------------------------------------------------------------------------
# Where they are
# ---------------------------------------------------------------------------


def test_storage_lists_only_backup_directories_and_points_at_misplaced_ones(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    The console listed /root's .ssh, .docker and .claude as applications.

    ``backup.directory: ''`` sent every backup to the working directory, so the
    storage page walked /root. It now counts only directories holding WASM
    backups, and names where misplaced ones are so they can be imported.
    """
    from tests.test_backup_placement import plant_backup, plant_home_clutter
    from wasm.core.config import Config
    from wasm.managers.backup_manager import BackupManager

    configured = tmp_path / "backups"
    plant_backup(configured, "example.com")
    plant_home_clutter(configured)
    home = tmp_path / "root"
    plant_backup(home, "example.com", stamp="20260803_000000")
    config_file = tmp_path / "etc" / "config.yaml"
    config_file.parent.mkdir()
    config_file.write_text(f"backup:\n  directory: {configured}\n")
    monkeypatch.setattr("wasm.core.config.DEFAULT_CONFIG_PATH", config_file)
    monkeypatch.setattr(BackupManager, "MISPLACED_BACKUP_ROOTS", (home,))
    Config.reset_instance()
    try:
        response = client.get("/api/backups/storage")
    finally:
        Config.reset_instance()

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["path"] == str(configured)
    assert body["domains"] == ["example-com", "orphan-example-com"]
    assert body["misplaced"] == [
        {"directory": str(home), "count": 1, "command": f"wasm backup import {home}"}
    ]


def _storage_with_directory(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, directory: Path
) -> dict[str, Any]:
    """
    Read ``/api/backups/storage`` with ``backup.directory`` set to ``directory``.

    Args:
        client: A signed-in client.
        tmp_path: Per-test temporary directory, for the configuration file.
        monkeypatch: Patching helper, scoped to the test.
        directory: The backup directory to configure.

    Returns:
        The decoded answer.
    """
    from wasm.core.config import Config
    from wasm.managers.backup_manager import BackupManager

    config_file = tmp_path / "etc" / "config.yaml"
    config_file.parent.mkdir(exist_ok=True)
    config_file.write_text(f"backup:\n  directory: {directory}\n")
    monkeypatch.setattr("wasm.core.config.DEFAULT_CONFIG_PATH", config_file)
    monkeypatch.setattr(BackupManager, "MISPLACED_BACKUP_ROOTS", ())
    Config.reset_instance()
    try:
        response = client.get("/api/backups/storage")
    finally:
        Config.reset_instance()
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def test_storage_reports_the_filesystem_the_backup_directory_is_on(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    The bar read "X of <disk total>" against the root disk, whichever disk the
    backups were on. The size and free space are now the backup directory's own.
    """
    import wasm.web.api.backups as backups_api

    configured = tmp_path / "backups"
    configured.mkdir()
    measured: list[Path] = []

    def disk_usage(path: Path) -> Any:
        measured.append(Path(path))
        return SimpleNamespace(total=500 * 1024**3, used=200 * 1024**3, free=300 * 1024**3)

    monkeypatch.setattr(backups_api.shutil, "disk_usage", disk_usage)
    body = _storage_with_directory(client, tmp_path, monkeypatch, configured)

    assert measured == [configured]
    assert body["filesystem_total"] == 500 * 1024**3
    assert body["filesystem_free"] == 300 * 1024**3


def test_storage_measures_a_missing_backup_directory_at_its_nearest_parent(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Before the first backup the directory does not exist; its parent is where it will land."""
    import wasm.web.api.backups as backups_api

    missing = tmp_path / "not-yet" / "backups"
    measured: list[Path] = []

    def disk_usage(path: Path) -> Any:
        measured.append(Path(path))
        return SimpleNamespace(total=100, used=40, free=60)

    monkeypatch.setattr(backups_api.shutil, "disk_usage", disk_usage)
    body = _storage_with_directory(client, tmp_path, monkeypatch, missing)

    assert measured == [tmp_path]
    assert (body["filesystem_total"], body["filesystem_free"]) == (100, 60)


def test_storage_answers_null_when_the_filesystem_cannot_be_read(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unreadable filesystem is reported as unknown, not as an empty disk."""
    import wasm.web.api.backups as backups_api

    configured = tmp_path / "backups"
    configured.mkdir()

    def disk_usage(path: Path) -> Any:
        raise PermissionError(13, "Permission denied", str(path))

    monkeypatch.setattr(backups_api.shutil, "disk_usage", disk_usage)
    body = _storage_with_directory(client, tmp_path, monkeypatch, configured)

    assert body["filesystem_total"] is None
    assert body["filesystem_free"] is None
