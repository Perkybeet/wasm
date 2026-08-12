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
- ``verify()`` reported "valid" for an archive it never read, and later for an
  archive whose members ``restore()`` refuses on sight.
- ``delete()`` removed the two files it knew about and left every sidecar
  behind.
- ``wasm --dry-run backup delete <id> --force`` printed "no changes will be made
  to this machine" and then deleted the archive, because a deletion is a
  ``Path.unlink`` and never goes near a subprocess.
- Every backup of a Python application was unrestorable: the archive carried
  ``venv``, and a virtualenv is a tree of absolute symlinks that the hardened
  extractor refuses.
"""

from __future__ import annotations

import ast
import gzip
import json
import tarfile
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Any, ClassVar

import pytest

import wasm.managers.backup_manager as backup_manager_module
import wasm.managers.backup_scheduler as backup_scheduler_module
from wasm.core.exceptions import ValidationError
from wasm.core.fs import DryRunFileSystem, RecordingFileSystem, set_fs
from wasm.core.runner import FakeRunner, get_runner, set_runner
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


@pytest.fixture
def rehearsal() -> Iterator[DryRunFileSystem]:
    """
    Provide a rehearsing filesystem, and undo its installation afterwards.

    The test installs it with ``set_fs`` at the point where the rehearsal
    starts, usually after arranging a real backup to act on. Managers read the
    filesystem from :func:`wasm.core.fs.get_fs` exactly as they read the runner,
    so this exercises the wiring ``--dry-run`` uses rather than a test-only
    injection point.

    Returns:
        The filesystem that records changes instead of making them.
    """
    filesystem = DryRunFileSystem()
    try:
        yield filesystem
    finally:
        set_fs(None)


def _tree_snapshot(root: Path) -> set[str]:
    """
    Record every path under a directory.

    Args:
        root: Directory to walk.

    Returns:
        Paths relative to ``root``, as strings.
    """
    return {str(path.relative_to(root)) for path in root.rglob("*")}


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


class TestRehearsalChangesNothing:
    """
    ``--dry-run`` has to be true for what WASM writes, not only for what it runs.

    The bug these tests exist for was reproduced on a real machine:
    ``wasm --dry-run backup delete <id> --force`` printed "Dry run: no changes
    will be made to this machine" and then deleted the archive.
    """

    def test_delete_keeps_the_archive_and_its_metadata(self, manager, monkeypatch, rehearsal):
        """The reproduced bug: a rehearsed delete must delete nothing."""
        _use_store(monkeypatch, None, [])
        metadata = manager.create("shop.example.com")
        app_dir = manager.backup_dir / "shop-example-com"
        before = _tree_snapshot(manager.backup_dir)

        set_fs(rehearsal)
        assert manager.delete(metadata.id) is True

        assert (app_dir / f"{metadata.id}.tar.gz").is_file(), "the rehearsal deleted the archive"
        assert (app_dir / f"{metadata.id}.json").is_file()
        assert _tree_snapshot(manager.backup_dir) == before
        assert any("would delete" in entry for entry in rehearsal.skipped), rehearsal.skipped

    def test_delete_of_a_sidecar_is_rehearsed_too(self, manager, monkeypatch, rehearsal):
        """The extra files delete() cleans up survive a rehearsal as well."""
        _use_store(monkeypatch, None, [])
        metadata = manager.create("shop.example.com")
        app_dir = manager.backup_dir / "shop-example-com"

        sidecar = app_dir / f"volume-data-{metadata.id}.tar.gz"
        sidecar.write_bytes(b"volume")
        stored = json.loads((app_dir / f"{metadata.id}.json").read_text())
        stored["docker_volume_backups"] = [{"volume": "data", "path": str(sidecar)}]
        (app_dir / f"{metadata.id}.json").write_text(json.dumps(stored))

        set_fs(rehearsal)
        manager.delete(metadata.id)

        assert sidecar.is_file()

    def test_rotation_keeps_every_backup(self, manager, monkeypatch, rehearsal):
        """Retention is a bulk delete; rehearsing it must not free a single byte."""
        _use_store(monkeypatch, None, [])
        app_dir = manager.backup_dir / "shop-example-com"
        app_dir.mkdir(parents=True)
        for index in range(3):
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
            (app_dir / f"{metadata.id}.tar.gz").write_bytes(b"x")
            (app_dir / f"{metadata.id}.json").write_text(json.dumps(metadata.to_dict()))
        before = _tree_snapshot(manager.backup_dir)

        set_fs(rehearsal)
        manager.rotate_by_policy("shop-example-com", max_count=1)

        assert _tree_snapshot(manager.backup_dir) == before

    def test_pruning_an_orphan_sidecar_is_rehearsed(self, manager, monkeypatch, rehearsal):
        """A metadata file with no archive is still there after a rehearsal."""
        _use_store(monkeypatch, None, [])
        metadata = manager.create("shop.example.com")
        app_dir = manager.backup_dir / "shop-example-com"
        (app_dir / f"{metadata.id}.tar.gz").unlink()

        set_fs(rehearsal)
        assert manager._prune_orphan_metadata("shop-example-com") == 1

        assert (app_dir / f"{metadata.id}.json").is_file()

    def test_create_writes_no_archive_and_no_metadata(self, manager, monkeypatch, rehearsal):
        """A rehearsed backup leaves nothing behind, in the backup dir or in /tmp."""
        _use_store(monkeypatch, None, [])
        manager.backup_dir.mkdir(parents=True)
        before = _tree_snapshot(manager.backup_dir)

        set_fs(rehearsal)
        metadata = manager.create("shop.example.com")

        assert metadata.size_bytes == 0
        assert metadata.checksum is None
        assert _tree_snapshot(manager.backup_dir) == before
        assert manager.list_backups(app_name="shop-example-com") == []
        assert any("would write" in entry for entry in rehearsal.skipped), rehearsal.skipped

    def test_create_dumps_no_database(self, manager, monkeypatch, rehearsal):
        """A rehearsal does not put production load on the database either."""
        _use_store(
            monkeypatch, FakeApp(1, "shop.example.com"), [FakeDatabase("shop", "postgresql")]
        )
        manager.backup_dir.mkdir(parents=True)
        runner: FakeRunner = manager.runner

        set_fs(rehearsal)
        manager.create("shop.example.com", include_databases=True)

        assert not any(call[0] == "pg_dump" for call in runner.calls), runner.calls

    def test_restore_does_not_replace_the_deployed_tree(self, manager, monkeypatch, rehearsal):
        """The destructive half of a restore is the rmtree; it must not happen."""
        _use_store(monkeypatch, None, [])
        metadata = manager.create("shop.example.com")
        app_path = manager.config.apps_directory / "shop-example-com"
        (app_path / "src" / "index.js").write_text("live version\n")
        (app_path / "keep-me.txt").write_text("added after the backup\n")

        set_fs(rehearsal)
        assert manager.restore(metadata.id) is True

        assert (app_path / "src" / "index.js").read_text() == "live version\n"
        assert (app_path / "keep-me.txt").is_file(), "the rehearsal deleted the deployed tree"

    def test_restore_keeps_the_deployed_env_file(self, manager, monkeypatch, rehearsal):
        """Rehearsing a restore with --no-restore-env does not rewrite the .env."""
        _use_store(monkeypatch, None, [])
        metadata = manager.create("shop.example.com")
        app_path = manager.config.apps_directory / "shop-example-com"
        (app_path / ".env").write_text("SECRET=live\n")

        set_fs(rehearsal)
        manager.restore(metadata.id, restore_env=False)

        assert (app_path / ".env").read_text() == "SECRET=live\n"

    def test_schedule_delete_keeps_the_unit_files(self, runner, tmp_path, rehearsal):
        """Removing a schedule unlinks two root-owned unit files; rehearse it."""
        scheduler = BackupScheduler(verbose=False, runner=runner)
        scheduler.SYSTEMD_DIR = tmp_path / "systemd"
        scheduler.SYSTEMD_DIR.mkdir()
        timer = scheduler.SYSTEMD_DIR / "wasm-backup-shop-example-com.timer"
        service = scheduler.SYSTEMD_DIR / "wasm-backup-shop-example-com.service"
        timer.write_text("[Timer]\n")
        service.write_text("[Service]\n")

        set_fs(rehearsal)
        assert scheduler.remove_schedule("shop.example.com") is True

        assert timer.is_file(), "the rehearsal deleted a systemd unit"
        assert service.is_file()

    def test_schedule_create_writes_no_unit(self, runner, tmp_path, rehearsal):
        """A rehearsed schedule leaves the systemd directory empty."""
        scheduler = BackupScheduler(verbose=False, runner=runner)
        scheduler.SYSTEMD_DIR = tmp_path / "systemd"
        scheduler.SYSTEMD_DIR.mkdir()

        set_fs(rehearsal)
        scheduler.create_schedule(
            BackupSchedule(domain="shop.example.com", app_name="shop-example-com", schedule="daily")
        )

        assert list(scheduler.SYSTEMD_DIR.iterdir()) == []

    def test_the_filesystem_can_be_injected(self, runner, tmp_path):
        """Like the runner, the filesystem is a constructor argument."""
        recorder = RecordingFileSystem()
        scheduler = BackupScheduler(verbose=False, runner=runner, fs=recorder)
        scheduler.SYSTEMD_DIR = tmp_path / "systemd"

        scheduler.create_schedule(
            BackupSchedule(domain="shop.example.com", app_name="shop-example-com", schedule="daily")
        )

        assert scheduler.fs is recorder
        assert [change[0] for change in recorder.changes] == ["write", "write"]
        assert BackupManager(runner=runner, fs=recorder).fs is recorder


class TestSecretsAreNotReadableByAnyone:
    """An archive holds the .env and the database dumps: it is a secret."""

    def test_archive_and_metadata_are_owner_only(self, manager, monkeypatch):
        """The stored archive, its sidecar and their directory are 0600/0700."""
        _use_store(monkeypatch, None, [])
        metadata = manager.create("shop.example.com")
        app_dir = manager.backup_dir / "shop-example-com"

        assert (app_dir / f"{metadata.id}.tar.gz").stat().st_mode & 0o777 == 0o600
        assert (app_dir / f"{metadata.id}.json").stat().st_mode & 0o777 == 0o600
        assert app_dir.stat().st_mode & 0o777 == 0o700
        assert manager.backup_dir.stat().st_mode & 0o777 == 0o700


def _write_python_app(apps_dir: Path) -> Path:
    """
    Deploy a Python application, virtualenv included, as PythonDeployer does.

    Args:
        apps_dir: Applications root.

    Returns:
        The application directory.
    """
    app = apps_dir / "shop-example-com"
    (app / "requirements.txt").write_text("flask\n")
    venv_bin = app / "venv" / "bin"
    venv_bin.mkdir(parents=True)
    (app / "venv" / "pyvenv.cfg").write_text("home = /usr/bin\n")
    # This is what makes a venv unrestorable: the interpreter is an absolute
    # symlink into the machine that created it.
    (venv_bin / "python").symlink_to("/usr/bin/python3")
    (venv_bin / "flask").write_text("#!/bin/sh\n")
    return app


def _register_archive(manager: BackupManager, archive: Path, backup_id: str, domain: str) -> None:
    """
    Put an archive and a matching sidecar where the manager expects them.

    Args:
        manager: Manager whose backup directory receives the pair.
        archive: Archive to move into place.
        backup_id: Identifier the backup is known by.
        domain: Domain the backup belongs to.
    """
    app_dir = manager.backup_dir / "shop-example-com"
    app_dir.mkdir(parents=True, exist_ok=True)
    stored = app_dir / f"{backup_id}.tar.gz"
    stored.write_bytes(archive.read_bytes())
    metadata = BackupMetadata(
        id=backup_id,
        domain=domain,
        app_name="shop-example-com",
        created_at="2026-01-01T00:00:00",
        size_bytes=stored.stat().st_size,
        app_type="python",
        version="2.0.0",
        description="",
        includes_env=True,
        includes_node_modules=False,
        checksum=manager._calculate_checksum(stored),
    )
    (app_dir / f"{backup_id}.json").write_text(json.dumps(metadata.to_dict()))


class TestVirtualenvIsNeverArchived:
    """
    A backup of a Python application used to be unrestorable, every time.

    ``PythonDeployer`` creates ``<app>/venv``; a virtualenv is a tree of
    absolute symlinks; the extractor a restore uses refuses absolute symlinks,
    and correctly so. Excluding the venv is the fix, not relaxing the extractor:
    a venv records the paths of the machine that built it and is rebuilt by
    ``install_dependencies()`` anyway.
    """

    def test_backup_of_a_python_app_leaves_the_venv_out(self, manager, monkeypatch, apps_dir):
        """The archive carries the source and not the virtualenv."""
        _use_store(monkeypatch, None, [])
        _write_python_app(apps_dir)

        metadata = manager.create("shop.example.com")

        archive = manager.backup_dir / "shop-example-com" / f"{metadata.id}.tar.gz"
        with tarfile.open(archive) as tar:
            names = tar.getnames()
        assert "shop-example-com/requirements.txt" in names
        assert not any("/venv" in name for name in names), names

    def test_backup_of_a_python_app_can_be_restored(self, manager, monkeypatch, apps_dir):
        """The property that matters: create() then restore() actually works."""
        _use_store(monkeypatch, None, [])
        app_path = _write_python_app(apps_dir)

        metadata = manager.create("shop.example.com")
        (app_path / "requirements.txt").write_text("broken\n")

        assert manager.restore(metadata.id) is True
        assert (app_path / "requirements.txt").read_text() == "flask\n"

    def test_verify_rejects_an_archive_the_restore_would_refuse(self, manager, monkeypatch):
        """verify() and restore() have to agree; they used to disagree always."""
        _use_store(monkeypatch, None, [])
        payload = manager.backup_dir / "pyvenv.cfg"
        payload.parent.mkdir(parents=True, exist_ok=True)
        payload.write_text("home = /usr/bin\n")

        archive = manager.backup_dir / "legacy.tar.gz"
        with tarfile.open(archive, "w:gz") as tar:
            tar.add(payload, arcname="shop-example-com/venv/pyvenv.cfg")
            link = tarfile.TarInfo("shop-example-com/venv/bin/python")
            link.type = tarfile.SYMTYPE
            link.linkname = "/usr/bin/python3"
            tar.addfile(link)
        backup_id = "shop-example-com_20260101_000000"
        _register_archive(manager, archive, backup_id, "shop.example.com")
        archive.unlink()
        payload.unlink()

        result = manager.verify(backup_id)

        assert result["valid"] is False
        assert result["extractable"] is False
        assert any("cannot be restored" in error for error in result["errors"]), result

        # The same archive, through the real restore path: same verdict.
        with pytest.raises(BackupError):
            manager.restore(backup_id)

    def test_a_shallow_verification_says_so(self, manager, monkeypatch):
        """deep=False is the old, weaker check; it must not claim more than it did."""
        _use_store(monkeypatch, None, [])
        metadata = manager.create("shop.example.com")

        assert manager.verify(metadata.id)["extractable"] is True
        assert "extractable" not in manager.verify(metadata.id, deep=False)


#: Calls that change the filesystem and have no innocent namesake. ``remove``
#: and ``replace`` are deliberately absent: ``list.remove`` and ``str.replace``
#: are everywhere, so those two are caught by module below instead.
_MUTATING_CALLS = frozenset(
    {
        "chmod",
        "copy2",
        "copyfile",
        "copystat",
        "copytree",
        "hardlink_to",
        "lchmod",
        "link_to",
        "makedirs",
        "mkdir",
        "mkdtemp",
        "mkstemp",
        "removedirs",
        "rename",
        "rmdir",
        "rmtree",
        "symlink_to",
        "touch",
        "unlink",
        "write_bytes",
        "write_text",
    }
)

#: Module-qualified mutations. ``shutil.disk_usage`` and ``os.path.*`` read and
#: are not listed.
_MUTATING_MODULE_CALLS = frozenset(
    {
        "os.chmod",
        "os.chown",
        "os.link",
        "os.makedirs",
        "os.mkdir",
        "os.open",
        "os.remove",
        "os.removedirs",
        "os.rename",
        "os.replace",
        "os.rmdir",
        "os.symlink",
        "os.truncate",
        "os.unlink",
        "shutil.chown",
        "shutil.copy",
        "shutil.copy2",
        "shutil.copyfile",
        "shutil.copytree",
        "shutil.move",
        "shutil.rmtree",
        "shutil.unpack_archive",
        "tempfile.mkdtemp",
        "tempfile.mkstemp",
    }
)

#: The same mutations imported by name, as ``from shutil import rmtree`` does.
_BARE_MUTATING_CALLS = frozenset(
    {
        "copy2",
        "copyfile",
        "copytree",
        "makedirs",
        "mkdtemp",
        "mkstemp",
        "move",
        "rmtree",
        "symlink",
        "unlink",
    }
)

#: Modules whose ``open`` takes the file name first and the mode second, unlike
#: :meth:`pathlib.Path.open`.
_FILE_MODULES = frozenset({"bz2", "gzip", "io", "lzma", "os", "tarfile", "zipfile"})

#: Expressions a mutating call may be made on: the seam itself.
_SEAM_RECEIVERS = frozenset({"fs", "self.fs", "self._fs"})

#: The one write that cannot go through the seam, because a tar stream cannot.
#: It is allowed in exactly one function, whose destination is always a file in
#: a self-cleaning :class:`tempfile.TemporaryDirectory` that a rehearsal never
#: reaches. Anywhere else it is a hole in --dry-run.
_STAGED_ARCHIVE_WRITER = "_write_archive"


def _expression(node: ast.AST) -> str:
    """
    Render a dotted expression the way it is written in the source.

    Args:
        node: Expression node, typically the receiver of a call.

    Returns:
        The dotted name, or "" for anything that is not a plain attribute chain.
    """
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _expression(node.value)
        return f"{prefix}.{node.attr}" if prefix else ""
    return ""


def _is_write_mode(call: ast.Call, position: int) -> bool:
    """
    Decide whether an ``open``-style call was asked for a writable handle.

    An unknown mode is treated as writable: the check is a guard, and a guard
    that assumes the safe case is not a guard.

    Args:
        call: The call node.
        position: Index of the positional mode argument. ``Path.open`` takes it
            first, ``open`` and ``tarfile.open`` take it second.

    Returns:
        True when the call may create or truncate a file.
    """
    mode: ast.AST | None = call.args[position] if len(call.args) > position else None
    for keyword in call.keywords:
        if keyword.arg == "mode":
            mode = keyword.value
    if mode is None:
        return False
    if isinstance(mode, ast.Constant) and isinstance(mode.value, str):
        return any(char in mode.value for char in "wax+")
    return True


def _enclosing_functions(tree: ast.AST) -> dict[ast.AST, str]:
    """
    Map every node to the name of the function that contains it.

    Args:
        tree: Parsed module.

    Returns:
        Node to function name, for the nodes inside a function.
    """
    owners: dict[ast.AST, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            for child in ast.walk(node):
                owners.setdefault(child, node.name)
    return owners


def _direct_mutations(source: str) -> list[str]:
    """
    Find filesystem mutations that bypass :mod:`wasm.core.fs`.

    Args:
        source: Module source code.

    Returns:
        One description per offending call, with its line number.
    """
    tree = ast.parse(source)
    owners = _enclosing_functions(tree)
    offenders: list[str] = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _expression(node.func)
        attribute = node.func.attr if isinstance(node.func, ast.Attribute) else None
        receiver = _expression(node.func.value) if isinstance(node.func, ast.Attribute) else ""
        owner = owners.get(node, "<module>")

        if attribute in _MUTATING_CALLS and receiver not in _SEAM_RECEIVERS:
            offenders.append(f"line {node.lineno} in {owner}(): {name}(...)")
        elif name in _MUTATING_MODULE_CALLS or name in _BARE_MUTATING_CALLS:
            offenders.append(f"line {node.lineno} in {owner}(): {name}(...)")
        elif attribute == "open" or name in {"open", "ZipFile", "zipfile.ZipFile"}:
            # ``Path.open`` takes the mode first; ``open`` and the archive
            # modules take a file name first and the mode second.
            position = 0 if attribute == "open" and receiver not in _FILE_MODULES else 1
            # tarfile.open is the one writer that cannot go through the seam,
            # and only where the destination is the staging directory.
            if _is_write_mode(node, position) and owner != _STAGED_ARCHIVE_WRITER:
                offenders.append(f"line {node.lineno} in {owner}(): {name}(...) for writing")

    return offenders


class TestEveryMutationGoesThroughTheSeam:
    """
    The seam only holds if nothing walks around it.

    A reviewer can miss one ``Path.unlink`` in two thousand lines; this cannot,
    and it is what stops ``--dry-run`` from quietly becoming a lie again the
    next time somebody adds a cleanup step.
    """

    @pytest.mark.parametrize(
        "module",
        [backup_manager_module, backup_scheduler_module],
        ids=lambda module: module.__name__,
    )
    def test_no_direct_filesystem_mutation(self, module):
        """No module in this area writes, deletes or chmods on its own."""
        source = Path(module.__file__).read_text()

        offenders = _direct_mutations(source)

        assert offenders == [], (
            f"{module.__name__} changes the filesystem without going through wasm.core.fs, "
            f"so --dry-run is a lie for these calls:\n  " + "\n  ".join(offenders)
        )

    def test_the_guard_catches_a_reintroduced_deletion(self):
        """The guard itself is tested, or it is decoration."""
        offenders = _direct_mutations(
            "from pathlib import Path\n"
            "from shutil import rmtree\n"
            "def delete(path: Path) -> None:\n"
            "    path.unlink(missing_ok=True)\n"
            "    Path('/etc/x').write_text('x')\n"
            "    shutil.rmtree(path)\n"
            "    rmtree(path)\n"
            "    os.replace(path, path)\n"
            "    os.remove(path)\n"
            "    path.chmod(0o777)\n"
            "    path.parent.mkdir(parents=True)\n"
            "    open('/etc/y', 'w')\n"
            "    path.open('w')\n"
            "    tarfile.open(path, 'w:gz')\n"
            "    tempfile.mkdtemp()\n"
        )

        assert len(offenders) == 12, offenders
        assert all("delete()" in offender for offender in offenders)

    def test_the_guard_accepts_the_seam(self):
        """Calls on the injected filesystem are not violations."""
        assert (
            _direct_mutations(
                "def delete(self, path):\n"
                "    self.fs.remove(path)\n"
                "    self.fs.write_text(path, 'x')\n"
                "    self.fs.chmod(path, 0o600)\n"
                "    self.fs.make_dir(path)\n"
                "    excludes.remove('node_modules')\n"
                "    name.replace('.', '-')\n"
                "    open(path, 'rb')\n"
                "    tarfile.open(path, 'r:gz')\n"
            )
            == []
        )


class TestTheReproducedCommand:
    """
    The exact invocation an adversarial review ran, end to end.

    ``wasm --dry-run backup delete <id> --force`` printed "Dry run: nothing on
    this machine will be changed" and deleted the archive. This drives the real
    command tree, so it also covers the wiring between the flag and the seam,
    not only the manager.
    """

    def test_dry_run_backup_delete_force_deletes_nothing(self, manager, monkeypatch):
        """The archive, its sidecar and the directory are all still there."""
        from click.testing import CliRunner

        from wasm.cli.app import cli

        _use_store(monkeypatch, None, [])
        metadata = manager.create("shop.example.com")
        before = _tree_snapshot(manager.backup_dir)

        # The command builds its own manager, so the temporary backup directory
        # has to reach it through the default rather than through this instance.
        # Config is a process-wide singleton, so it is left alone.
        monkeypatch.setattr(BackupManager, "DEFAULT_BACKUP_DIR", manager.backup_dir)
        try:
            result = CliRunner().invoke(
                cli, ["--dry-run", "backup", "delete", metadata.id, "--force"]
            )
        finally:
            # The command tree installs both seams process-wide; put them back
            # before the next test inherits a rehearsal.
            set_fs(None)
            set_runner(None)

        # What the command printed is asserted at the manager level, through the
        # rehearsing filesystem's own record; the Logger binds its stream when it
        # is built, which puts its output out of reach of both CliRunner and
        # capsys. What matters here is the disk.
        assert result.exit_code == 0, result.exception
        assert _tree_snapshot(manager.backup_dir) == before
        assert (manager.backup_dir / "shop-example-com" / f"{metadata.id}.tar.gz").is_file()


class TestARestoreNeverDestroysWhatItCannotReplace:
    """
    The deployed tree is deleted to make room for the archive's copy. If the
    safety copy of it could not be made, deleting it is unrecoverable: there is
    nothing to roll back to and the application is gone.
    """

    def test_a_failed_safety_copy_aborts_before_anything_is_deleted(
        self, manager, tmp_path, monkeypatch
    ):
        deployed = tmp_path / "apps" / "example-com"
        deployed.mkdir(parents=True)
        (deployed / "server.js").write_text("the running application")
        extracted = tmp_path / "extracted"
        extracted.mkdir()
        (extracted / "server.js").write_text("the archived application")
        workspace = tmp_path / "work"
        workspace.mkdir()

        def no_space(*_args, **_kwargs):
            raise OSError(28, "No space left on device")

        monkeypatch.setattr(manager.fs, "copy_tree", no_space)

        with pytest.raises(BackupError) as exc:
            manager._swap_in_tree(extracted, deployed, workspace)

        assert "could not be copied aside" in str(exc.value)
        assert (deployed / "server.js").read_text() == "the running application", (
            "the deployed application was destroyed by a restore that could not protect it"
        )

    def test_the_refusal_says_how_to_proceed(self, manager, tmp_path, monkeypatch):
        deployed = tmp_path / "apps" / "example-com"
        deployed.mkdir(parents=True)
        (deployed / "x").write_text("x")
        workspace = tmp_path / "work"
        workspace.mkdir()
        monkeypatch.setattr(
            manager.fs,
            "copy_tree",
            lambda *a, **k: (_ for _ in ()).throw(OSError(13, "Permission denied")),
        )

        with pytest.raises(BackupError) as exc:
            manager._swap_in_tree(tmp_path / "extracted", deployed, workspace)

        assert exc.value.details
        assert "restore again" in exc.value.details
