# Copyright (c) 2024-2026 Yago Lopez Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
Tests for Task 1.9: parity endpoints and single chokepoints.

Six chokepoints, six defects each one closes:

- **Service delete removes the store row.** The panel's DELETE endpoint used
  to stop, disable and unlink a unit itself, straight past
  ``ServiceManager``'s ownership guard and past the store, so a deleted
  service kept showing up in ``wasm service list``.
- **Site delete walks both engines and the certificate.**
  ``delete_site_completely`` is now the one place "delete a site" happens; the
  panel, ``wasm site delete`` and ``wasm app delete`` used to each write it
  again, and only one of the three checked apache as well as nginx.
- **Site create with SSL never renders a certificate that does not exist
  yet.** ``create_secured_site`` writes the vhost, then asks for the
  certificate, then rewrites the vhost with the certificate paths - in that
  order, always.
- **Rollback takes its own safety backup.** It used to be a step only the CLI
  performed before calling ``RollbackManager.rollback``, so a rollback
  triggered from the panel had no way back if the restore itself went wrong.
- **Every backup and restore option reaches the manager.** ``schemas``,
  ``include_docker_volumes``, ``redis_method``, ``restore_env`` and ``verify``
  were accepted by the CLI and silently dropped by the API.
- **Every certificate option reaches the manager.** SANs, the authentication
  method (including webroot and standalone, which the API had no way to
  select at all) and ``expand``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from wasm.core.fs import RecordingFileSystem, set_fs
from wasm.core.runner import FakeRunner
from wasm.core.store import App, Service, WASMStore
from wasm.managers.backup_manager import BackupManager, RollbackManager
from wasm.managers.service_manager import ServiceManager
from wasm.managers.webserver import (
    APACHE_BACKEND,
    NGINX_BACKEND,
    WebServerBackend,
    WebServerManager,
    create_secured_site,
    delete_site_completely,
)
from wasm.web.api import backups as backups_api
from wasm.web.api import certs as certs_api
from wasm.web.api import services as services_api
from wasm.web.api import sites as sites_api
from wasm.web.api.auth import get_current_session
from wasm.web.api.deps import require_elevated
from wasm.web.jobs import (
    Job,
    JobContext,
    JobType,
    backup_app_job,
    cert_create_job,
    delete_app_job,
    restore_backup_job,
)


@pytest.fixture
def store(tmp_path: Path):
    """
    Give this file its own store, backed by a temporary database.

    Args:
        tmp_path: Per-test temporary directory.

    Yields:
        The store the chokepoints under test read and write.
    """
    WASMStore.reset_instance()
    instance = WASMStore(tmp_path / "wasm.db")
    try:
        yield instance
    finally:
        instance.close()
        WASMStore.reset_instance()


def _job_context(job_type: JobType = JobType.CUSTOM) -> JobContext:
    """
    Build a job context a job function can report progress through, without
    the job manager's queue or worker thread.

    Args:
        job_type: Type recorded on the underlying job.

    Returns:
        A context whose notifications go nowhere.
    """
    job = Job(id=str(uuid4())[:8], type=job_type, name="test", description="test")
    return JobContext(job, notify=lambda _job: None)


def _client(router: Any, prefix: str, *, elevated: bool = False) -> TestClient:
    """
    Build a test client for one router, with authentication stubbed out.

    Args:
        router: The API router to mount.
        prefix: Path prefix to mount it under.
        elevated: Also bypass the sudo-mode dependency, for destructive
            endpoints. The mechanism itself is exercised in
            tests/test_web_sudo.py; these tests are about what happens once
            an operator has passed it.

    Returns:
        The client.
    """
    app = FastAPI()
    app.include_router(router, prefix=prefix)
    app.dependency_overrides[get_current_session] = lambda: {"session_id": "test", "scope": "admin"}
    if elevated:
        app.dependency_overrides[require_elevated] = lambda: {
            "session_id": "test",
            "scope": "admin",
        }
    return TestClient(app, raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# 1. Panel service delete removes the store row
# ---------------------------------------------------------------------------


class TestServiceDeleteRemovesTheStoreRow:
    """DELETE /api/services/{name} goes through ServiceManager.delete_service."""

    def test_delete_removes_the_unit_and_the_store_row(
        self, tmp_path: Path, store: WASMStore, runner: FakeRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        unit_dir = tmp_path / "systemd"
        unit_dir.mkdir()
        monkeypatch.setattr(services_api, "SYSTEMD_UNIT_DIR", unit_dir)
        monkeypatch.setattr(ServiceManager, "SYSTEMD_DIR", unit_dir)
        monkeypatch.setattr(ServiceManager, "UNIT_SEARCH_DIRS", (unit_dir,))

        unit_file = unit_dir / "my-app.service"
        unit_file.write_text("# Generated by WASM\n[Service]\nExecStart=/bin/true\n")
        store.create_service(Service(name="my-app", unit_file=str(unit_file)))
        assert store.get_service("my-app") is not None

        client = _client(services_api.router, "/api/services", elevated=True)
        response = client.delete("/api/services/my-app")

        assert response.status_code == 200, response.text
        assert not unit_file.exists()
        assert store.get_service("my-app") is None
        assert runner.ran("systemctl", "daemon-reload")


# ---------------------------------------------------------------------------
# 2. Panel site delete removes the cert and both engines' vhosts
# ---------------------------------------------------------------------------


def _sandbox_backend(base: WebServerBackend, tmp_path: Path, name: str) -> WebServerBackend:
    """Point a backend's directories at a throwaway tree."""
    import dataclasses

    available = tmp_path / name / "sites-available"
    enabled = tmp_path / name / "sites-enabled"
    available.mkdir(parents=True)
    enabled.mkdir(parents=True)
    return dataclasses.replace(base, sites_available=available, sites_enabled=enabled)


class _StubCertManager:
    """A certificate manager double that reports one installed certificate."""

    def __init__(self, *, has_cert: bool) -> None:
        self._has_cert = has_cert
        self.deleted: list[str] = []

    def is_installed(self) -> bool:
        return True

    def cert_exists(self, domain: str) -> bool:
        return self._has_cert

    def delete(self, domain: str) -> bool:
        self.deleted.append(domain)
        self._has_cert = False
        return True


class TestSiteDeleteWalksBothEnginesAndTheCertificate:
    """delete_site_completely is the one implementation every caller shares."""

    def test_both_backends_and_the_certificate_are_removed(
        self, tmp_path: Path, runner: FakeRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("wasm.managers.webserver.get_store", lambda: _FakeStore())

        nginx = WebServerManager(_sandbox_backend(NGINX_BACKEND, tmp_path, "nginx"))
        apache = WebServerManager(_sandbox_backend(APACHE_BACKEND, tmp_path, "apache"))
        nginx.create_site("example.com", context={"port": 3000, "ssl": False})
        nginx.enable_site("example.com")
        apache.create_site("example.com", context={"port": 3000, "ssl": False})
        apache.enable_site("example.com")
        certs = _StubCertManager(has_cert=True)

        deletion = delete_site_completely(
            "example.com", nginx=nginx, apache=apache, cert_manager=certs
        )

        assert deletion.nginx_removed is True
        assert deletion.apache_removed is True
        assert deletion.certificate_removed is True
        assert not nginx.site_exists("example.com")
        assert not apache.site_exists("example.com")
        assert certs.deleted == ["example.com"]

    def test_nothing_to_delete_reports_nothing_removed(
        self, tmp_path: Path, runner: FakeRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("wasm.managers.webserver.get_store", lambda: _FakeStore())
        nginx = WebServerManager(_sandbox_backend(NGINX_BACKEND, tmp_path, "nginx"))
        apache = WebServerManager(_sandbox_backend(APACHE_BACKEND, tmp_path, "apache"))

        deletion = delete_site_completely(
            "gone.example.com",
            nginx=nginx,
            apache=apache,
            cert_manager=_StubCertManager(has_cert=False),
        )

        assert deletion.removed_anything is False

    def test_the_web_api_deletes_through_the_chokepoint(
        self, tmp_path: Path, runner: FakeRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """DELETE /api/sites/{domain} calls delete_site_completely, not one manager."""
        calls: list[str] = []

        def fake_delete_site_completely(domain: str, **kwargs: Any):
            calls.append(domain)
            from wasm.managers.webserver import SiteDeletion

            return SiteDeletion(
                domain=domain, nginx_removed=True, apache_removed=True, certificate_removed=True
            )

        monkeypatch.setattr(sites_api, "delete_site_completely", fake_delete_site_completely)

        client = _client(sites_api.router, "/api/sites", elevated=True)
        response = client.delete("/api/sites/example.com")

        assert response.status_code == 200, response.text
        assert calls == ["example.com"]


class _FakeStore:
    """A store stand-in the sandboxed managers register sites with, and forget."""

    def get_site(self, domain: str) -> None:
        return None

    def create_site(self, site: Any) -> None:
        return None

    def update_site(self, site: Any) -> None:
        return None

    def delete_site(self, domain: str) -> bool:
        return True


# ---------------------------------------------------------------------------
# 3. Panel site create with ssl=true calls CertManager
# ---------------------------------------------------------------------------


class TestSiteCreateWithSslCallsCertManager:
    """create_secured_site never renders a certificate that was not asked for."""

    def test_ssl_true_obtains_a_certificate_before_rendering_it(
        self, tmp_path: Path, runner: FakeRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("wasm.managers.webserver.get_store", lambda: _FakeStore())
        manager = WebServerManager(_sandbox_backend(NGINX_BACKEND, tmp_path, "nginx"))

        outcome = create_secured_site("example.com", manager=manager, webserver="nginx", ssl=True)

        assert outcome.ssl_enabled is True
        assert any("certonly" in call for call in runner.calls), runner.calls
        config = manager.get_site_config("example.com") or ""
        assert "example.com/fullchain.pem" in config

    def test_ssl_false_never_touches_certbot(
        self, tmp_path: Path, runner: FakeRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("wasm.managers.webserver.get_store", lambda: _FakeStore())
        manager = WebServerManager(_sandbox_backend(NGINX_BACKEND, tmp_path, "nginx"))

        outcome = create_secured_site("example.com", manager=manager, webserver="nginx", ssl=False)

        assert outcome.ssl_enabled is False
        assert not any(call[0] == "certbot" for call in runner.calls)

    def test_the_web_api_creates_through_the_chokepoint(
        self, tmp_path: Path, runner: FakeRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """POST /api/sites with ssl=true reaches create_secured_site, not a bare render."""
        calls: list[dict[str, Any]] = []

        def fake_create_secured_site(domain: str, **kwargs: Any):
            calls.append({"domain": domain, **kwargs})
            from wasm.managers.webserver import SecuredSite

            return SecuredSite(
                domain=domain, webserver=kwargs["webserver"], ssl_requested=True, ssl_enabled=True
            )

        available = tmp_path / "sites-available"
        available.mkdir()
        monkeypatch.setattr(
            sites_api, "MANAGERS", {"nginx": lambda verbose=False: _StubManager(available)}
        )
        monkeypatch.setattr(sites_api, "create_secured_site", fake_create_secured_site)

        client = _client(sites_api.router, "/api/sites")
        response = client.post(
            "/api/sites", json={"domain": "example.com", "ssl": True, "webserver": "nginx"}
        )

        assert response.status_code == 200, response.text
        assert len(calls) == 1
        assert calls[0]["domain"] == "example.com"
        assert calls[0]["ssl"] is True


class _StubManager:
    """The minimal manager surface POST /api/sites needs before delegating."""

    def __init__(self, sites_available: Path) -> None:
        self._sites_available = sites_available

    def list_templates(self) -> list[str]:
        return ["proxy"]

    def site_exists(self, domain: str) -> bool:
        return False


# ---------------------------------------------------------------------------
# 4. Panel rollback leaves a safety backup
# ---------------------------------------------------------------------------


@pytest.fixture
def sandboxed_backups(tmp_path: Path, runner: FakeRunner):
    """
    A BackupManager whose backups and application directory both live under
    ``tmp_path``.

    ``Config`` is a process-wide singleton, so this mutates and restores it
    the same way ``tests/test_backup_integrity.py`` does; ``RollbackManager``
    reads the very same singleton, which is what lets a rollback built in one
    test see the sandboxed application directory without a second patch.

    Args:
        tmp_path: Per-test temporary directory.
        runner: The FakeRunner fixture, installed process-wide.

    Yields:
        The sandboxed manager.
    """
    manager = BackupManager(verbose=False, runner=runner)
    manager.backup_dir = tmp_path / "backups"
    previous = manager.config.get("apps_directory")
    manager.config.set("apps_directory", str(tmp_path / "apps"))
    try:
        yield manager
    finally:
        manager.config.set("apps_directory", previous)


class _NoService:
    """A ServiceManager stand-in for an application with no unit yet."""

    def get_status(self, name: str) -> dict[str, Any]:
        return {"exists": False}


class TestRollbackLeavesASafetyBackup:
    """RollbackManager.rollback takes the safety backup itself."""

    def test_rollback_creates_a_safety_backup_before_restoring(
        self, tmp_path: Path, store: WASMStore, sandboxed_backups: BackupManager
    ) -> None:
        app_path = tmp_path / "apps" / "example-com"
        app_path.mkdir(parents=True)
        (app_path / "server.js").write_text("// app\n")

        original = sandboxed_backups.create(
            domain="example.com", description="manual", include_env=False
        )

        # A spy on create(), not a second look at what is on disk afterwards:
        # BackupManager's id is second-resolution
        # (<domain>_<YYYYmmdd_HHMMSS>), so two backups of the same domain a
        # rollback takes milliseconds apart in a fast test can collide and
        # overwrite each other. The chokepoint under test is that rollback()
        # asks for a second backup, not that two distinct files survive it.
        create_calls: list[dict[str, Any]] = []
        real_create = sandboxed_backups.create

        def recording_create(**kwargs: Any) -> Any:
            create_calls.append(kwargs)
            return real_create(**kwargs)

        sandboxed_backups.create = recording_create  # type: ignore[method-assign]

        manager = RollbackManager(verbose=False)
        manager.backup_manager = sandboxed_backups
        manager.service_manager = _NoService()  # type: ignore[assignment]

        assert manager.rollback("example.com", backup_id=original.id, rebuild=False) is True

        assert any(
            call.get("description") == "Pre-rollback safety backup" for call in create_calls
        ), create_calls

    def test_a_backup_manager_that_cannot_take_the_safety_backup_does_not_abort_the_rollback(
        self, store: WASMStore
    ) -> None:
        """
        A failed safety backup is a warning, not a reason to refuse the
        rollback.

        No sandbox here on purpose: RollbackManager's own Config resolves the
        real, unsandboxed application directory, which this test's domain has
        nothing under - the same "nothing to back up yet" case
        create_pre_deploy_backup already treats as a no-op rather than a
        failure. This exercises the path where the safety backup genuinely
        cannot be taken (an explicit failure would be tested at the
        BackupManager level, not by making a double misbehave).
        """
        from wasm.core.store import DeploymentTrigger

        class _Restoring:
            def get_backup(self, backup_id: str) -> Any:
                from wasm.managers.backup_manager import BackupMetadata

                return BackupMetadata(
                    id=backup_id,
                    domain="no-app-here.example.com",
                    app_name="no-app-here-example-com",
                    created_at="2026-01-01T00:00:00",
                    size_bytes=1,
                    app_type="nodejs",
                    version="1",
                    description="manual",
                    includes_env=True,
                    includes_node_modules=False,
                )

            def restore(self, **kwargs: Any) -> bool:
                return True

        manager = RollbackManager(verbose=False)
        manager.backup_manager = _Restoring()  # type: ignore[assignment]
        manager.service_manager = _NoService()  # type: ignore[assignment]

        assert (
            manager.rollback(
                "no-app-here.example.com",
                backup_id="b1",
                rebuild=False,
                trigger=DeploymentTrigger.CLI.value,
            )
            is True
        )


# ---------------------------------------------------------------------------
# 5. Backup and restore options reach BackupManager
# ---------------------------------------------------------------------------


class _RecordingBackupManager:
    """A BackupManager stand-in that records exactly what it was called with."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.create_calls: list[dict[str, Any]] = []
        self.restore_calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> Any:
        self.create_calls.append(kwargs)
        from wasm.managers.backup_manager import BackupMetadata

        return BackupMetadata(
            id="b1",
            domain=kwargs["domain"],
            app_name="app",
            created_at="2026-01-01T00:00:00",
            size_bytes=1,
            app_type="nodejs",
            version="1",
            description="",
            includes_env=True,
            includes_node_modules=False,
        )

    def get_backup(self, backup_id: str) -> Any:
        from wasm.managers.backup_manager import BackupMetadata

        return BackupMetadata(
            id=backup_id,
            domain="example.com",
            app_name="app",
            created_at="2026-01-01T00:00:00",
            size_bytes=1,
            app_type="nodejs",
            version="1",
            description="",
            includes_env=True,
            includes_node_modules=False,
        )

    def restore(self, **kwargs: Any) -> bool:
        self.restore_calls.append(kwargs)
        return True


class TestBackupOptionsReachTheManager:
    """schemas, docker volumes, the redis method, restore_env and verify."""

    def test_create_forwards_the_full_option_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake = _RecordingBackupManager()
        monkeypatch.setattr("wasm.managers.backup_manager.BackupManager", lambda **kw: fake)

        backup_app_job(
            domain="example.com",
            include_databases=True,
            include_docker_volumes=True,
            schemas=["public"],
            redis_method="aof",
            job_context=_job_context(JobType.BACKUP),
        )

        assert fake.create_calls == [
            {
                "domain": "example.com",
                "description": "",
                "include_env": True,
                "include_node_modules": False,
                "include_build": False,
                "include_databases": True,
                "include_docker_volumes": True,
                "schemas": ["public"],
                "redis_method": "aof",
                "tags": [],
            }
        ]

    def test_restore_forwards_restore_env_and_verify(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake = _RecordingBackupManager()
        monkeypatch.setattr("wasm.managers.backup_manager.BackupManager", lambda **kw: fake)

        restore_backup_job(
            backup_id="b1",
            target_domain="example.com",
            restore_env=False,
            verify=False,
            job_context=_job_context(JobType.RESTORE),
        )

        assert fake.restore_calls == [
            {
                "backup_id": "b1",
                "target_domain": "example.com",
                "restore_env": False,
                "verify_checksum": False,
            }
        ]

    def test_the_create_endpoint_queues_the_full_option_set(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        queued: dict[str, Any] = {}

        class _FakeJob:
            id = "j1"
            status = type("S", (), {"value": "pending"})()

            def to_dict(self) -> dict[str, Any]:
                return {}

        class _FakeJobManager:
            def create_job(self, **kwargs: Any) -> _FakeJob:
                queued.update(kwargs)
                return _FakeJob()

        monkeypatch.setattr(backups_api, "get_job_manager", lambda: _FakeJobManager())

        client = _client(backups_api.router, "/api/backups")
        response = client.post(
            "/api/backups",
            json={
                "domain": "example.com",
                "include_docker_volumes": True,
                "schemas": ["public"],
                "redis_method": "aof",
            },
        )

        assert response.status_code == 202, response.text
        assert queued["kwargs"]["include_docker_volumes"] is True
        assert queued["kwargs"]["schemas"] == ["public"]
        assert queued["kwargs"]["redis_method"] == "aof"

    def test_the_restore_endpoint_queues_restore_env_and_verify(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        queued: dict[str, Any] = {}

        class _FakeJob:
            id = "j1"
            status = type("S", (), {"value": "pending"})()

            def to_dict(self) -> dict[str, Any]:
                return {}

        class _FakeJobManager:
            def create_job(self, **kwargs: Any) -> _FakeJob:
                queued.update(kwargs)
                return _FakeJob()

        monkeypatch.setattr(backups_api, "get_job_manager", lambda: _FakeJobManager())
        monkeypatch.setattr(backups_api, "_load_backup", lambda backup_id: (None, _backup_stub()))

        client = _client(backups_api.router, "/api/backups")
        response = client.post(
            "/api/backups/b1/restore", json={"restore_env": False, "verify": False}
        )

        assert response.status_code == 202, response.text
        assert queued["kwargs"]["restore_env"] is False
        assert queued["kwargs"]["verify"] is False


def _backup_stub() -> Any:
    from wasm.managers.backup_manager import BackupMetadata

    return BackupMetadata(
        id="b1",
        domain="example.com",
        app_name="app",
        created_at="2026-01-01T00:00:00",
        size_bytes=1,
        app_type="nodejs",
        version="1",
        description="",
        includes_env=True,
        includes_node_modules=False,
    )


# ---------------------------------------------------------------------------
# 6. Certificate options reach CertManager
# ---------------------------------------------------------------------------


class TestCertOptionsReachTheManager:
    """domains (SANs), method, webroot and expand."""

    def test_two_domains_build_dash_d_a_dash_d_b(self, runner: FakeRunner) -> None:
        cert_create_job(
            domain="a.example.com",
            domains=["b.example.com"],
            job_context=_job_context(JobType.CERT_CREATE),
        )

        [issue_call] = [c for c in runner.calls if "certonly" in c]
        assert "-d" in issue_call
        d_values = [issue_call[i + 1] for i, arg in enumerate(issue_call) if arg == "-d"]
        assert d_values == ["a.example.com", "b.example.com"]

    def test_method_webroot_uses_the_webroot_authenticator(self, runner: FakeRunner) -> None:
        cert_create_job(
            domain="a.example.com",
            method="webroot",
            webroot="/var/www/custom",
            job_context=_job_context(JobType.CERT_CREATE),
        )

        [issue_call] = [c for c in runner.calls if "certonly" in c]
        assert "--webroot" in issue_call
        assert "/var/www/custom" in issue_call

    def test_method_standalone_uses_the_standalone_authenticator(self, runner: FakeRunner) -> None:
        cert_create_job(
            domain="a.example.com",
            method="standalone",
            job_context=_job_context(JobType.CERT_CREATE),
        )

        [issue_call] = [c for c in runner.calls if "certonly" in c]
        assert "--standalone" in issue_call

    def test_the_create_endpoint_queues_domains_method_webroot_and_expand(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        queued: dict[str, Any] = {}

        class _FakeJob:
            id = "j1"
            status = type("S", (), {"value": "pending"})()

            def to_dict(self) -> dict[str, Any]:
                return {}

        class _FakeJobManager:
            def create_job(self, **kwargs: Any) -> _FakeJob:
                queued.update(kwargs)
                return _FakeJob()

        monkeypatch.setattr(certs_api, "get_job_manager", lambda: _FakeJobManager())

        client = _client(certs_api.router, "/api/certs")
        response = client.post(
            "/api/certs/a.example.com",
            json={
                "domains": ["b.example.com"],
                "method": "webroot",
                "webroot": "/var/www/custom",
                "expand": True,
            },
        )

        assert response.status_code == 202, response.text
        assert queued["kwargs"]["domains"] == ["b.example.com"]
        assert queued["kwargs"]["method"] == "webroot"
        assert queued["kwargs"]["webroot"] == "/var/www/custom"
        assert queued["kwargs"]["expand"] is True

    def test_an_unknown_method_is_a_validation_error(self) -> None:
        client = _client(certs_api.router, "/api/certs")

        response = client.post("/api/certs/a.example.com", json={"method": "ftp"})

        assert 400 <= response.status_code < 500, response.text


# ---------------------------------------------------------------------------
# delete_app_job: both engines, and files through the filesystem seam
# ---------------------------------------------------------------------------


class TestDeleteAppJobRoutesThroughTheChokepoints:
    def test_files_are_removed_through_the_filesystem_seam(
        self, tmp_path: Path, store: WASMStore, runner: FakeRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        app_path = tmp_path / "apps" / "example-com"
        app_path.mkdir(parents=True)
        (app_path / "server.js").write_text("// app\n")
        store.create_app(App(domain="example.com", app_path=str(app_path)))

        recording_fs = RecordingFileSystem()
        set_fs(recording_fs)
        try:
            delete_app_job(
                domain="example.com",
                remove_files=True,
                remove_ssl=True,
                job_context=_job_context(JobType.DELETE),
            )
        finally:
            set_fs(None)

        assert ("remove_tree", app_path) in recording_fs.changes
        assert not app_path.exists()

    def test_both_backends_are_checked(
        self, tmp_path: Path, store: WASMStore, runner: FakeRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        app_path = tmp_path / "apps" / "example-com"
        app_path.mkdir(parents=True)
        store.create_app(App(domain="example.com", app_path=str(app_path)))

        calls: list[str] = []

        def fake_delete_site_completely(domain: str, **kwargs: Any):
            calls.append(domain)
            assert kwargs["delete_certificate"] is True
            from wasm.managers.webserver import SiteDeletion

            return SiteDeletion(domain=domain)

        monkeypatch.setattr(
            "wasm.managers.webserver.delete_site_completely", fake_delete_site_completely
        )

        set_fs(RecordingFileSystem())
        try:
            delete_app_job(
                domain="example.com",
                remove_files=False,
                remove_ssl=True,
                job_context=_job_context(JobType.DELETE),
            )
        finally:
            set_fs(None)

        assert calls == ["example.com"]
