"""
Tests for the one property a backup has to have: it can bring the data back.

Each test here corresponds to a way the previous implementation lost data or
opened a hole:

- The archive was files only. ``includes_databases: true`` pointed at a dump
  living somewhere else on the machine, so a backup copied to another host
  restored an application with an empty database.
- ``restore()`` unpacked the archive with ``tar -xzf``, which happily writes
  through ``../`` and through symlinks.
- The scheduler had a second, unescaped unit renderer that interpolated the
  domain into a systemd unit.
- ``verify()`` reported "valid" for an archive it never read.
- ``delete()`` removed the two files it knew about and left every sidecar
  behind.
"""

from __future__ import annotations

import gzip
import json
import tarfile
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Any, ClassVar

import pytest

from wasm.core.exceptions import ValidationError
from wasm.core.runner import FakeRunner, get_runner
from wasm.managers.backup_manager import (
    DATABASES_DIR,
    MANIFEST_NAME,
    PAYLOAD_DIR,
    BackupError,
    BackupManager,
    BackupMetadata,
)
from wasm.managers.backup_scheduler import BackupSchedule, BackupScheduler


class FakeApp:
    """Minimal stand-in for a store application record."""

    def __init__(self, app_id: int, domain: str) -> None:
        """
        Args:
            app_id: Primary key the store would have assigned.
            domain: Domain the application answers on.
        """
        self.id = app_id
        self.domain = domain


class FakeDatabase:
    """Minimal stand-in for a store database record."""

    def __init__(self, name: str, engine: str) -> None:
        """
        Args:
            name: Database name.
            engine: Engine that owns it.
        """
        self.name = name
        self.engine = engine


class FakeStore:
    """Store double that answers the two queries the backup manager makes."""

    def __init__(self, app: FakeApp | None, databases: list[FakeDatabase]) -> None:
        """
        Args:
            app: Application record to return, or None when unknown.
            databases: Databases attached to that application.
        """
        self._app = app
        self._databases = databases

    def get_app(self, domain: str) -> FakeApp | None:
        """
        Args:
            domain: Domain being looked up.

        Returns:
            The configured application record.
        """
        return self._app

    def list_databases(self, app_id: int | None = None) -> list[FakeDatabase]:
        """
        Args:
            app_id: Application the databases belong to.

        Returns:
            The configured database records.
        """
        return self._databases


class FakeDatabaseManager:
    """
    Database engine double that really writes and really reads a dump file.

    It goes through the process-wide runner, so the argv a real engine would
    build is still asserted on, but the dump content is a plain marker string
    the test can follow from creation to restore.
    """

    restored: ClassVar[list[tuple[str, Path, str]]] = []

    def __init__(self, engine: str = "postgresql") -> None:
        """
        Args:
            engine: Engine name reported in metadata.
        """
        self.engine = engine
        self.BACKUP_SUFFIX = ".sql"

    def is_installed(self) -> bool:
        """
        Returns:
            Always True; installation is not what these tests exercise.
        """
        return True

    def backup(self, database: str, output_path: Path | None = None, compress: bool = True) -> Any:
        """
        Write a dump to ``output_path`` through the runner.

        Args:
            database: Database to dump.
            output_path: Destination chosen by the backup manager.
            compress: Whether the dump is gzipped.

        Returns:
            An object with the same attributes as ``BackupInfo``.
        """
        assert output_path is not None
        get_runner().capture_to_file(
            ["pg_dump", "--no-password", database],
            output_path,
            compress=compress,
            timeout=3600,
        )

        class _Info:
            path = output_path
            size = output_path.stat().st_size if output_path.exists() else 0
            created = datetime.now()

        return _Info()

    def restore(self, database: str, backup_path: Path) -> None:
        """
        Record the restore and read the dump back.

        Args:
            database: Target database.
            backup_path: Dump handed over by the backup manager.
        """
        content = Path(backup_path).read_text()
        FakeDatabaseManager.restored.append((database, Path(backup_path), content))
        get_runner().run(["psql", "--no-password", "-d", database], timeout=3600)


@pytest.fixture
def apps_dir(tmp_path: Path) -> Path:
    """
    Provide an applications root with one deployed application in it.

    Args:
        tmp_path: Per-test temporary directory.

    Returns:
        The applications root.
    """
    root = tmp_path / "apps"
    app = root / "shop-example-com"
    (app / "src").mkdir(parents=True)
    (app / "src" / "index.js").write_text("console.log('hello')\n")
    (app / ".env").write_text("SECRET=1\n")
    (app / "node_modules").mkdir()
    (app / "node_modules" / "junk.js").write_text("x" * 100)
    return root


@pytest.fixture
def manager(
    runner: FakeRunner, tmp_path: Path, apps_dir: Path, monkeypatch
) -> Iterator[BackupManager]:
    """
    Provide a backup manager wired to temporary directories and a fake runner.

    Args:
        runner: The FakeRunner fixture, installed process-wide.
        tmp_path: Per-test temporary directory.
        apps_dir: Applications root holding the deployed application.
        monkeypatch: Patching helper, scoped to the test.

    Returns:
        A backup manager that cannot touch the real machine.
    """
    from wasm.managers.database.registry import DatabaseRegistry

    FakeDatabaseManager.restored = []
    backup_manager = BackupManager(verbose=False, runner=runner)
    backup_manager.backup_dir = tmp_path / "backups"

    previous = backup_manager.config.get("apps_directory")
    backup_manager.config.set("apps_directory", str(apps_dir))

    monkeypatch.setattr(
        DatabaseRegistry,
        "get",
        classmethod(lambda cls, engine, verbose=False: FakeDatabaseManager(engine)),
    )

    yield backup_manager

    backup_manager.config.set("apps_directory", previous)


def _use_store(monkeypatch, app: FakeApp | None, databases: list[FakeDatabase]) -> None:
    """
    Point the backup manager at a store double.

    Args:
        monkeypatch: Patching helper, scoped to the test.
        app: Application record the store should return.
        databases: Databases attached to it.
    """
    store = FakeStore(app, databases)
    monkeypatch.setattr("wasm.managers.backup_manager.get_store", lambda: store)


class TestSelfContainedBackup:
    """A backup carries its own data or it is not a backup."""

    def test_moved_archive_still_restores_the_database(self, manager, monkeypatch, tmp_path):
        """A backup copied elsewhere restores the database it claims to carry."""
        _use_store(
            monkeypatch, FakeApp(1, "shop.example.com"), [FakeDatabase("shop", "postgresql")]
        )
        runner: FakeRunner = manager.runner
        runner.script(["pg_dump"], stdout="-- dump of shop\n")

        metadata = manager.create("shop.example.com", include_databases=True)

        assert metadata.includes_databases is True
        assert any(call[0] == "pg_dump" for call in runner.calls), runner.calls

        # The dump has to be inside the archive, not next to it.
        archive = manager.backup_dir / "shop-example-com" / f"{metadata.id}.tar.gz"
        with tarfile.open(archive) as tar:
            names = tar.getnames()
        assert f"{PAYLOAD_DIR}/{MANIFEST_NAME}" in names
        assert any(name.startswith(f"{PAYLOAD_DIR}/{DATABASES_DIR}/") for name in names)

        # Move the archive somewhere the metadata knows nothing about, which is
        # what happens when a backup is copied to another machine.
        elsewhere = tmp_path / "offsite"
        elsewhere.mkdir()
        moved = elsewhere / archive.name
        archive.rename(moved)

        manager.restore_archive(moved)

        assert FakeDatabaseManager.restored, "the database was never restored"
        database, dump_path, content = FakeDatabaseManager.restored[0]
        assert database == "shop"
        assert content == "-- dump of shop\n"
        assert elsewhere not in dump_path.parents
        assert any(call[0] == "psql" for call in runner.calls), runner.calls

        app_path = manager.config.apps_directory / "shop-example-com"
        assert (app_path / "src" / "index.js").read_text() == "console.log('hello')\n"
        assert not (app_path / "node_modules").exists()

    def test_restore_by_id_puts_the_database_back(self, manager, monkeypatch):
        """The metadata-driven path restores the databases the archive carries."""
        _use_store(
            monkeypatch, FakeApp(1, "shop.example.com"), [FakeDatabase("shop", "postgresql")]
        )
        runner: FakeRunner = manager.runner
        runner.script(["pg_dump"], stdout="-- dump of shop\n")

        metadata = manager.create("shop.example.com", include_databases=True)
        app_path = manager.config.apps_directory / "shop-example-com"
        (app_path / "src" / "index.js").write_text("broken\n")

        assert manager.restore(metadata.id) is True

        assert (app_path / "src" / "index.js").read_text() == "console.log('hello')\n"
        assert [entry[0] for entry in FakeDatabaseManager.restored] == ["shop"]

    def test_legacy_archive_without_manifest_still_restores_files(self, manager, tmp_path):
        """A 1.x archive, application tree at the top level, still comes back."""
        legacy_tree = tmp_path / "legacy" / "shop-example-com"
        legacy_tree.mkdir(parents=True)
        (legacy_tree / "index.html").write_text("<h1>old</h1>")

        archive = tmp_path / "legacy.tar.gz"
        with tarfile.open(archive, "w:gz") as tar:
            tar.add(legacy_tree, arcname="shop-example-com")

        assert manager.restore_archive(archive, target_domain="shop.example.com") is True

        app_path = manager.config.apps_directory / "shop-example-com"
        assert (app_path / "index.html").read_text() == "<h1>old</h1>"

    def test_metadata_never_claims_a_database_it_does_not_carry(self, manager, monkeypatch):
        """An application with no databases produces includes_databases false."""
        _use_store(monkeypatch, FakeApp(1, "shop.example.com"), [])

        metadata = manager.create("shop.example.com", include_databases=True)

        assert metadata.includes_databases is False
        assert metadata.database_backups == []


class TestSafeExtraction:
    """Restoring is unpacking an archive: it must not write outside its root."""

    def test_traversal_member_is_refused(self, manager, tmp_path, monkeypatch):
        """A member named ``../`` never lands outside the extraction root."""
        _use_store(monkeypatch, None, [])
        victim = tmp_path / "pwned.txt"
        payload = tmp_path / "payload.txt"
        payload.write_text("owned")

        archive = tmp_path / "malicious.tar.gz"
        with tarfile.open(archive, "w:gz") as tar:
            tar.add(payload, arcname="../../pwned.txt")
            tar.add(payload, arcname="shop-example-com/keep.txt")

        with pytest.raises(BackupError) as exc_info:
            manager.restore_archive(archive, target_domain="shop.example.com")

        assert "escapes" in str(exc_info.value) or "outside" in str(exc_info.value)
        assert not victim.exists()

    def test_absolute_member_is_refused(self, manager, tmp_path, monkeypatch):
        """A member with an absolute path is refused instead of overwriting it."""
        _use_store(monkeypatch, None, [])
        payload = tmp_path / "payload.txt"
        payload.write_text("owned")

        # tarfile.add() strips the leading slash, so the member has to be built
        # by hand, exactly as a crafted archive would carry it.
        archive = tmp_path / "absolute.tar.gz"
        with tarfile.open(archive, "w:gz") as tar:
            info = tarfile.TarInfo("/etc/wasm-pwned.conf")
            info.size = payload.stat().st_size
            with payload.open("rb") as handle:
                tar.addfile(info, handle)

        with pytest.raises(BackupError):
            manager.restore_archive(archive, target_domain="shop.example.com")

        assert not Path("/etc/wasm-pwned.conf").exists()


class TestVerify:
    """verify() has to read the archive, not just look at the file name."""

    def test_corrupted_archive_is_detected(self, manager, monkeypatch):
        """Flipping bytes in a stored archive makes verify() fail."""
        _use_store(monkeypatch, None, [])
        metadata = manager.create("shop.example.com")
        archive = manager.backup_dir / "shop-example-com" / f"{metadata.id}.tar.gz"

        assert manager.verify(metadata.id)["valid"] is True

        raw = bytearray(archive.read_bytes())
        raw[len(raw) // 2] ^= 0xFF
        archive.write_bytes(bytes(raw))

        result = manager.verify(metadata.id)

        assert result["valid"] is False
        assert any("checksum" in error.lower() for error in result["errors"])

    def test_truncated_archive_with_matching_checksum_is_detected(self, manager, monkeypatch):
        """An unreadable archive fails even when its recorded checksum matches."""
        _use_store(monkeypatch, None, [])
        metadata = manager.create("shop.example.com")
        app_dir = manager.backup_dir / "shop-example-com"
        archive = app_dir / f"{metadata.id}.tar.gz"

        archive.write_bytes(gzip.compress(b"this is not a tar file"))
        stored = json.loads((app_dir / f"{metadata.id}.json").read_text())
        stored["checksum"] = manager._calculate_checksum(archive)
        (app_dir / f"{metadata.id}.json").write_text(json.dumps(stored))

        result = manager.verify(metadata.id)

        assert result["valid"] is False
        assert any("corrupt" in error.lower() for error in result["errors"])


class TestRetention:
    """Deleting a backup deletes everything that belongs to it."""

    def test_delete_removes_sidecars(self, manager, monkeypatch):
        """Legacy sidecar files recorded in metadata are deleted with the backup."""
        _use_store(monkeypatch, None, [])
        metadata = manager.create("shop.example.com")
        app_dir = manager.backup_dir / "shop-example-com"

        sidecar = app_dir / f"volume-data-{metadata.id}.tar.gz"
        sidecar.write_bytes(b"volume")
        stored = json.loads((app_dir / f"{metadata.id}.json").read_text())
        stored["docker_volume_backups"] = [{"volume": "data", "path": str(sidecar)}]
        (app_dir / f"{metadata.id}.json").write_text(json.dumps(stored))

        manager.delete(metadata.id)

        assert list(app_dir.iterdir()) == [], "delete() left orphans behind"

    def test_delete_ignores_paths_outside_the_backup_directory(
        self, manager, monkeypatch, tmp_path
    ):
        """A metadata file cannot make delete() remove an unrelated path."""
        _use_store(monkeypatch, None, [])
        metadata = manager.create("shop.example.com")
        app_dir = manager.backup_dir / "shop-example-com"

        outsider = tmp_path / "important.conf"
        outsider.write_text("keep me")
        stored = json.loads((app_dir / f"{metadata.id}.json").read_text())
        stored["docker_volume_backups"] = [{"volume": "data", "path": str(outsider)}]
        (app_dir / f"{metadata.id}.json").write_text(json.dumps(stored))

        manager.delete(metadata.id)

        assert outsider.exists()

    def test_rotation_removes_metadata_and_archive(self, manager, monkeypatch):
        """Rotating by count leaves no metadata without an archive."""
        _use_store(monkeypatch, None, [])
        app_dir = manager.backup_dir / "shop-example-com"
        created = []
        for index in range(4):
            metadata = BackupMetadata(
                id=f"shop-example-com_2026010{index}_000000",
                domain="shop.example.com",
                app_name="shop-example-com",
                created_at=f"2026-01-0{index + 1}T00:00:00",
                size_bytes=1,
                app_type="static",
                version="2.0.0",
                description="",
                includes_env=False,
                includes_node_modules=False,
            )
            app_dir.mkdir(parents=True, exist_ok=True)
            (app_dir / f"{metadata.id}.tar.gz").write_bytes(b"x")
            (app_dir / f"{metadata.id}.json").write_text(json.dumps(metadata.to_dict()))
            created.append(metadata.id)

        deleted = manager.rotate_by_policy("shop-example-com", max_count=2)

        assert deleted == 2
        remaining = sorted(path.name for path in app_dir.iterdir())
        assert remaining == sorted(
            [
                f"{created[3]}.tar.gz",
                f"{created[3]}.json",
                f"{created[2]}.tar.gz",
                f"{created[2]}.json",
            ]
        )

    def test_rotation_prunes_metadata_left_without_an_archive(self, manager, monkeypatch):
        """A sidecar whose archive vanished is invisible to list(); rotation clears it."""
        _use_store(monkeypatch, None, [])
        metadata = manager.create("shop.example.com")
        app_dir = manager.backup_dir / "shop-example-com"

        (app_dir / f"{metadata.id}.tar.gz").unlink()
        assert manager.list_backups(app_name="shop-example-com") == []

        manager.rotate_by_policy("shop-example-com", max_count=5)

        assert list(app_dir.iterdir()) == [], "rotation left an orphan metadata file"


class TestScheduler:
    """The scheduler writes root-owned unit files; a domain must stay data."""

    @pytest.fixture
    def scheduler(self, runner: FakeRunner, tmp_path: Path) -> BackupScheduler:
        """
        Provide a scheduler writing units into a temporary directory.

        Args:
            runner: The FakeRunner fixture.
            tmp_path: Per-test temporary directory.

        Returns:
            The scheduler under test.
        """
        instance = BackupScheduler(verbose=False, runner=runner)
        instance.SYSTEMD_DIR = tmp_path / "systemd"
        instance.SYSTEMD_DIR.mkdir()
        return instance

    def test_newline_in_domain_cannot_inject_a_directive(self, scheduler):
        """A domain carrying a newline is refused, not rendered into the unit."""
        schedule = BackupSchedule(
            domain="shop.example.com\nExecStartPre=/bin/rm -rf /",
            app_name="shop-example-com",
            schedule="daily",
        )

        with pytest.raises((BackupError, ValidationError)):
            scheduler.create_schedule(schedule)

        assert list(scheduler.SYSTEMD_DIR.iterdir()) == []

    def test_renderer_escapes_newlines(self, scheduler):
        """Even called directly, the renderer never emits a second directive."""
        schedule = BackupSchedule(
            domain="shop.example.com\nExecStartPre=/bin/rm -rf /",
            app_name="shop-example-com",
            schedule="daily",
        )

        service = scheduler.render_service(schedule)

        assert "\nExecStartPre=" not in service
        assert len([line for line in service.splitlines() if line.startswith("ExecStart")]) == 1

    def test_calendar_expression_cannot_inject_a_directive(self, scheduler):
        """A crafted schedule expression cannot append a Timer directive."""
        schedule = BackupSchedule(
            domain="shop.example.com",
            app_name="shop-example-com",
            schedule="daily\nOnBootSec=1s",
        )

        with pytest.raises((BackupError, ValidationError)):
            scheduler.create_schedule(schedule)

    def test_create_schedule_writes_units_and_enables_timer(self, scheduler):
        """The happy path writes both units and enables the timer through the runner."""
        schedule = BackupSchedule(
            domain="shop.example.com",
            app_name="shop-example-com",
            schedule="daily",
        )

        assert scheduler.create_schedule(schedule) is True

        timer = scheduler.SYSTEMD_DIR / "wasm-backup-shop-example-com.timer"
        service = scheduler.SYSTEMD_DIR / "wasm-backup-shop-example-com.service"
        assert "OnCalendar=*-*-* 02:00:00" in timer.read_text()
        assert "shop.example.com" in service.read_text()

        runner: FakeRunner = scheduler.runner
        assert runner.ran("systemctl", "daemon-reload")
        assert runner.ran("systemctl", "enable", "--now", "wasm-backup-shop-example-com.timer")
        assert not any(call[0] == "sudo" for call in runner.calls), runner.calls
