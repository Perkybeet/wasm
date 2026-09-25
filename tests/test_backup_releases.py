# Copyright (c) 2024-2026 Yago Lopez Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
Backups of applications on the release layout.

What such an application cannot lose lives in ``shared/``: its ``.env`` and
the uploads every release links to. An archive that carried the tree but got
``shared/`` wrong - or restored the ``.env`` to ``<app>/.env``, where no
release reads it - would be a backup that loses exactly the data it exists
for. The archive also must not bring back releases that could never start
(an archive never carries dependencies) nor hand the application directory
itself to the service account.
"""

from __future__ import annotations

import os
import tarfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from wasm.core.runner import FakeRunner
from wasm.managers.backup_manager import BackupManager

DOMAIN = "rel.example.com"
OLD = "20260925-100000-aaaaaaa"
ACTIVE = "20260925-110000-bbbbbbb"


@pytest.fixture
def root(tmp_path: Path) -> Path:
    """
    An application on the release layout with two releases, uploads and a .env.

    Returns:
        The application directory.
    """
    root = tmp_path / "apps" / "rel-example-com"
    for release_id, version in ((OLD, "1"), (ACTIVE, "2")):
        release = root / "releases" / release_id
        release.mkdir(parents=True)
        (release / "package.json").write_text('{"name": "app"}')
        (release / "VERSION").write_text(version)
        (release / "node_modules").mkdir()
        (release / "node_modules" / "dep.js").write_text("x")
        (release / ".env").symlink_to(Path("../../shared/.env"))
        (release / "uploads").symlink_to(Path("../../shared/uploads"))
    (root / "current").symlink_to(Path("releases") / ACTIVE)
    (root / "shared" / "uploads").mkdir(parents=True)
    (root / "shared" / "uploads" / "photo.jpg").write_text("user data")
    (root / "shared" / ".env").write_text("SECRET=archived\n")
    (root / "repo" / ".git").mkdir(parents=True)
    (root / "repo" / "package.json").write_text("{}")
    return root


@pytest.fixture
def manager(
    runner: FakeRunner, tmp_path: Path, root: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[BackupManager]:
    """
    A backup manager over the temporary apps directory, with a fake runner.

    Yields:
        The manager.
    """
    monkeypatch.setattr("wasm.managers.backup_manager.get_store", lambda: _NoStore())
    backup_manager = BackupManager(verbose=False, runner=runner)
    backup_manager.backup_dir = tmp_path / "backups"
    previous = backup_manager.config.get("apps_directory")
    backup_manager.config.set("apps_directory", str(root.parent))
    yield backup_manager
    backup_manager.config.set("apps_directory", previous)


class _NoStore:
    """A store that knows no application and no database."""

    def get_app(self, domain: str) -> None:
        return None

    def list_databases(self, app_id: int | None = None) -> list[object]:
        return []


def members(manager: BackupManager, backup_id: str) -> set[str]:
    """Names in an archive, relative to the application tree."""
    archive = manager.backup_dir / "rel-example-com" / f"{backup_id}.tar.gz"
    with tarfile.open(archive) as tar:
        return {
            name.split("/", 1)[1] for name in tar.getnames() if name.startswith("rel-example-com/")
        }


def test_the_archive_carries_shared_the_active_release_and_current(
    manager: BackupManager,
) -> None:
    """The data and the code that serves; not the cache nor releases that cannot start."""
    metadata = manager.create(DOMAIN)

    names = members(manager, metadata.id)
    assert {"current", "shared/.env", "shared/uploads/photo.jpg"} <= names
    assert f"releases/{ACTIVE}/VERSION" in names
    assert not any(name.startswith(f"releases/{OLD}") for name in names)
    assert not any(name == "repo" or name.startswith("repo/") for name in names)
    assert not any("node_modules" in name for name in names)
    # The release is described by what it serves, not by the bare directory.
    assert metadata.git_commit == "bbbbbbb"
    assert metadata.app_type == "nodejs"


def test_leaving_the_env_out_leaves_shared_env_out(manager: BackupManager) -> None:
    """--no-env means the secrets in shared/ too."""
    metadata = manager.create(DOMAIN, include_env=False)

    names = members(manager, metadata.id)
    assert "shared/.env" not in names
    assert "shared/uploads/photo.jpg" in names


def test_a_restore_brings_shared_back_and_hands_over_only_what_serves(
    manager: BackupManager, root: Path, runner: FakeRunner
) -> None:
    """Uploads and .env come back; the application directory stays root's."""
    metadata = manager.create(DOMAIN)
    (root / "shared" / "uploads" / "photo.jpg").unlink()
    (root / "shared" / ".env").write_text("SECRET=changed\n")

    assert manager.restore(metadata.id) is True

    assert (root / "shared" / "uploads" / "photo.jpg").read_text() == "user data"
    assert (root / "shared" / ".env").read_text() == "SECRET=archived\n"
    assert os.readlink(root / "current") == f"releases/{ACTIVE}"
    assert (root / "current" / "uploads" / "photo.jpg").read_text() == "user data"
    chowns = [call for call in runner.calls if call[:2] == ("chown", "-R")]
    assert {call[3] for call in chowns} == {
        str(root / "releases" / ACTIVE),
        str(root / "shared"),
    }


def test_a_restore_without_the_env_keeps_the_deployed_shared_env(
    manager: BackupManager, root: Path
) -> None:
    """The .env kept is shared/.env, and the restored release sees it again."""
    metadata = manager.create(DOMAIN, include_env=False)
    (root / "shared" / ".env").write_text("SECRET=live\n")

    assert manager.restore(metadata.id, restore_env=False) is True

    assert (root / "shared" / ".env").read_text() == "SECRET=live\n"
    assert not os.path.lexists(root / ".env"), "a stray .env no release reads"
    assert (root / "current" / ".env").read_text() == "SECRET=live\n"


def test_in_place_archives_still_carry_the_whole_tree(
    manager: BackupManager, tmp_path: Path
) -> None:
    """The anchored patterns are only the release layout's; in place is unchanged."""
    root = tmp_path / "apps" / "inplace-example-com"
    (root / "repo").mkdir(parents=True)
    (root / "repo" / "kept.txt").write_text("a directory the app calls repo")
    (root / ".env").write_text("A=1\n")

    metadata = manager.create("inplace.example.com")

    archive = manager.backup_dir / "inplace-example-com" / f"{metadata.id}.tar.gz"
    with tarfile.open(archive) as tar:
        names = tar.getnames()
    assert "inplace-example-com/repo/kept.txt" in names
    assert "inplace-example-com/.env" in names


def test_a_rollback_by_backup_does_not_rebuild_a_release_app_in_place(
    manager: BackupManager, root: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Building in the application directory is building where no release is."""
    from wasm.core.store import WASMStore
    from wasm.managers.backup_manager import RollbackManager

    metadata = manager.create(DOMAIN)
    WASMStore.reset_instance()
    store = WASMStore(tmp_path / "wasm.db")
    monkeypatch.setattr("wasm.managers.backup_manager.get_store", lambda: store)
    detected: list[Path] = []
    monkeypatch.setattr(
        "wasm.deployers.detect_app_type", lambda path, verbose=False: detected.append(path)
    )
    rollback = RollbackManager(verbose=False, runner=manager.runner)
    rollback.backup_manager = manager
    monkeypatch.setattr(rollback, "create_pre_deploy_backup", lambda *a, **k: None)
    monkeypatch.setattr(
        rollback.service_manager, "get_status", lambda name: {"exists": False, "active": False}
    )

    try:
        assert rollback.rollback(DOMAIN, backup_id=metadata.id) is True
    finally:
        WASMStore.reset_instance()

    assert detected == [], "nothing is rebuilt in the application directory"
    assert os.readlink(root / "current") == f"releases/{ACTIVE}"
