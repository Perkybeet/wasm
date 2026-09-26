# Copyright (c) 2024-2026 Yago Lopez Prado
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Tests for three defects in the restore path of :mod:`wasm.managers.backup_manager`.

- A restore used to hand the whole tree over to the service account, Docker
  Compose applications included. Their bind mounts and named volumes carry the
  uids their containers chose (postgres's data directory is 999, for
  instance), and a recursive chown to ``www-data`` breaks every one of them.
- The safety copy of the tree a restore is about to replace lived under
  :func:`tempfile.gettempdir`, which is often tmpfs or a different filesystem
  from the application directory: a large copy could fill RAM, moving it back
  on failure was a second copy rather than an atomic rename, and it vanished
  on reboot.
- Nothing stopped a deploy, an update or another restore from running on the
  same application at the same time as a restore.
"""

from __future__ import annotations

import tempfile
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from wasm.core.applock import AppBusyError, app_lock
from wasm.core.fs import DryRunFileSystem, set_fs
from wasm.core.runner import FakeRunner
from wasm.core.store import WASMStore
from wasm.managers.backup_manager import BackupError, BackupManager

DOMAIN = "shop.example.com"
APP_NAME = "shop-example-com"


class FakeApp:
    """Minimal stand-in for a store application record."""

    def __init__(self, app_type: str = "nodejs", layout: str | None = None) -> None:
        """
        Args:
            app_type: Type the store records for the application.
            layout: Layout the store records, when a mismatch check matters.
        """
        self.app_type = app_type
        self.layout = layout


class FakeStore:
    """Store double answering the one query these tests need: the app row."""

    def __init__(self, app: FakeApp | None) -> None:
        """
        Args:
            app: Application record to return, or None for an unknown domain.
        """
        self._app = app

    def get_app(self, domain: str) -> FakeApp | None:
        """
        Args:
            domain: Domain being looked up.

        Returns:
            The configured application record.
        """
        return self._app

    def list_databases(self, app_id: int | None = None) -> list[object]:
        """
        Args:
            app_id: Application the databases belong to.

        Returns:
            No databases: these tests do not exercise database restore.
        """
        return []


def _use_store(monkeypatch: pytest.MonkeyPatch, app: FakeApp | None) -> None:
    """
    Point the backup manager's own store lookups at a double.

    This does not affect :mod:`wasm.core.applock`, which reads the real,
    process-wide store to find where lock files live; tests that exercise the
    lock provide one of their own.

    Args:
        monkeypatch: Patching helper, scoped to the test.
        app: Application record the store should return.
    """
    monkeypatch.setattr("wasm.managers.backup_manager.get_store", lambda: FakeStore(app))


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
    app = root / APP_NAME
    (app / "src").mkdir(parents=True)
    (app / "src" / "index.js").write_text("console.log('original')\n")
    (app / ".env").write_text("SECRET=1\n")
    return root


@pytest.fixture
def manager(runner: FakeRunner, tmp_path: Path, apps_dir: Path) -> Iterator[BackupManager]:
    """
    Provide a backup manager wired to temporary directories and a fake runner.

    Args:
        runner: The FakeRunner fixture, installed process-wide.
        tmp_path: Per-test temporary directory.
        apps_dir: Applications root holding the deployed application.

    Returns:
        A backup manager that cannot touch the real machine.
    """
    backup_manager = BackupManager(verbose=False, runner=runner)
    backup_manager.backup_dir = tmp_path / "backups"
    previous = backup_manager.config.get("apps_directory")
    backup_manager.config.set("apps_directory", str(apps_dir))
    yield backup_manager
    backup_manager.config.set("apps_directory", previous)


class TestComposeRestoreKeepsOwnership:
    """A Compose tree's volumes and bind mounts have their own uids."""

    def test_a_compose_restore_does_not_chown_the_tree(
        self, manager: BackupManager, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The whole point: no recursive chown on a Docker Compose application."""
        _use_store(monkeypatch, FakeApp(app_type="docker-compose"))
        metadata = manager.create(DOMAIN)
        runner: FakeRunner = manager.runner
        runner.calls.clear()

        assert manager.restore(metadata.id) is True

        assert not any(call[:2] == ("chown", "-R") for call in runner.calls), runner.calls

    def test_an_ordinary_node_app_still_gets_handed_over(
        self, manager: BackupManager, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The chown is not gone; it is refused only for Docker Compose."""
        _use_store(monkeypatch, FakeApp(app_type="nodejs"))
        metadata = manager.create(DOMAIN)
        runner: FakeRunner = manager.runner
        runner.calls.clear()

        assert manager.restore(metadata.id) is True

        app_path = manager.config.apps_directory / APP_NAME
        assert any(
            call[:2] == ("chown", "-R") and call[3] == str(app_path) for call in runner.calls
        ), runner.calls

    def test_an_unknown_application_still_gets_handed_over(
        self, manager: BackupManager, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No store row (a restore into a domain nothing has deployed) is not Compose."""
        _use_store(monkeypatch, None)
        metadata = manager.create(DOMAIN)
        runner: FakeRunner = manager.runner
        runner.calls.clear()

        assert manager.restore(metadata.id) is True

        app_path = manager.config.apps_directory / APP_NAME
        assert any(
            call[:2] == ("chown", "-R") and call[3] == str(app_path) for call in runner.calls
        )


class TestRestoreSafetyCopyLocation:
    """The previous tree is kept beside the application, not under /tmp."""

    def test_the_workspace_is_a_sibling_of_the_app_directory(
        self, manager: BackupManager, apps_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A failure that keeps the workspace proves where it lives."""
        _use_store(monkeypatch, None)
        metadata = manager.create(DOMAIN)

        def boom(*args: object, **kwargs: object) -> None:
            raise BackupError("the post-restore step failed")

        monkeypatch.setattr(manager, "_restore_databases", boom)

        with pytest.raises(BackupError):
            manager.restore(metadata.id)

        workspaces = list(apps_dir.glob(f".wasm-restore-{APP_NAME}-*"))
        assert len(workspaces) == 1, workspaces
        workspace = workspaces[0]
        # Beside the application directory - not a fresh directory dropped
        # straight into tempfile.gettempdir(), which is what the old
        # TemporaryDirectory(prefix="wasm-restore-") produced.
        assert workspace.parent == apps_dir
        assert workspace.parent != Path(tempfile.gettempdir())

    def test_it_is_removed_after_a_successful_restore(
        self, manager: BackupManager, apps_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Nothing is left behind once the restore has fully succeeded."""
        _use_store(monkeypatch, None)
        metadata = manager.create(DOMAIN)

        assert manager.restore(metadata.id) is True

        assert list(apps_dir.glob(".wasm-restore-*")) == []

    def test_a_failure_after_the_swap_keeps_the_operator_whole(
        self, manager: BackupManager, apps_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        The tree was already replaced by the time a database fails to come
        back: the previous state is put back, or - if that itself fails -
        kept at the sibling workspace, and either way the error names it.
        """
        _use_store(monkeypatch, None)
        metadata = manager.create(DOMAIN)
        app_path = apps_dir / APP_NAME
        (app_path / "src" / "index.js").write_text("console.log('live, before the restore')\n")

        def boom(*args: object, **kwargs: object) -> None:
            raise BackupError("the post-restore step failed")

        monkeypatch.setattr(manager, "_restore_databases", boom)

        with pytest.raises(BackupError) as excinfo:
            manager.restore(metadata.id)

        workspaces = list(apps_dir.glob(f".wasm-restore-{APP_NAME}-*"))
        assert workspaces, "the workspace was not kept"
        workspace = workspaces[0]

        put_back = (
            app_path / "src" / "index.js"
        ).read_text() == "console.log('live, before the restore')\n"
        kept = (workspace / "previous" / "src" / "index.js").is_file()
        assert put_back or kept, "the previous tree was neither put back nor kept"
        assert str(workspace) in str(excinfo.value)

    def test_dry_run_creates_no_workspace(
        self, manager: BackupManager, apps_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A rehearsal never reaches the filesystem, workspace included."""
        _use_store(monkeypatch, None)
        metadata = manager.create(DOMAIN)

        set_fs(DryRunFileSystem())
        try:
            assert manager.restore(metadata.id) is True
        finally:
            set_fs(None)

        assert list(apps_dir.glob(".wasm-restore-*")) == []


class Holder:
    """Holds an application's lock in another thread until told to let go."""

    def __init__(self, domain: str, operation: str) -> None:
        """
        Args:
            domain: The application's domain.
            operation: What the holder claims to be doing.
        """
        self.domain = domain
        self.operation = operation
        self.acquired = threading.Event()
        self.release = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        with app_lock(self.domain, self.operation):
            self.acquired.set()
            self.release.wait(timeout=10)

    def __enter__(self) -> Holder:
        self.thread.start()
        assert self.acquired.wait(timeout=5)
        return self

    def __exit__(self, *exc: object) -> None:
        self.release.set()
        self.thread.join(timeout=5)


class TestRestoreIsLocked:
    """A restore must not interleave with another operation on the same app."""

    @pytest.fixture(autouse=True)
    def real_store(self, tmp_path: Path) -> Iterator[WASMStore]:
        """
        Provide a real, isolated store: the lock lives beside its database.

        Args:
            tmp_path: Per-test temporary directory.

        Yields:
            The store instance.
        """
        WASMStore.reset_instance()
        instance = WASMStore(tmp_path / "state" / "wasm.db")
        yield instance
        WASMStore.reset_instance()

    def test_a_restore_is_refused_while_another_operation_holds_the_lock(
        self, manager: BackupManager, apps_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The archive and the deployed tree are untouched by the refusal."""
        _use_store(monkeypatch, None)
        metadata = manager.create(DOMAIN)
        app_path = apps_dir / APP_NAME
        original = (app_path / "src" / "index.js").read_text()
        archive = manager.backup_dir / APP_NAME / f"{metadata.id}.tar.gz"
        archive_before = archive.read_bytes()

        with Holder(DOMAIN, "update"):
            with pytest.raises(AppBusyError):
                manager.restore(metadata.id)

        assert (app_path / "src" / "index.js").read_text() == original
        assert archive.read_bytes() == archive_before
        assert list(apps_dir.glob(".wasm-restore-*")) == []

    def test_a_rollback_is_also_refused(
        self, manager: BackupManager, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """RollbackManager.rollback locks too, under the ``rollback`` operation."""
        from wasm.managers.backup_manager import RollbackManager

        _use_store(monkeypatch, None)
        manager.create(DOMAIN)
        rollback = RollbackManager(verbose=False, runner=manager.runner)
        rollback.backup_manager = manager

        with Holder(DOMAIN, "deploy"):
            with pytest.raises(AppBusyError):
                rollback.rollback(DOMAIN)
