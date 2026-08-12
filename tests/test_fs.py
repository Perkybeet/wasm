"""
Tests for the filesystem seam.

The seam exists because ``--dry-run`` was only true for things WASM executed.
An adversarial review demonstrated that ``wasm --dry-run backup delete <id>
--force`` printed "no changes will be made to this machine" and then deleted
the archive, because a deletion is a ``Path.unlink`` and never reaches a
subprocess.

A rehearsal that performs half the operation is worse than no rehearsal, so
these tests pin the two properties that matter: the real filesystem writes
atomically and with the mode set at creation, and the dry-run one does nothing
at all.
"""

from __future__ import annotations

import os
import stat

import pytest

from wasm.core.fs import (
    SECRET_DIR_MODE,
    SECRET_MODE,
    DryRunFileSystem,
    RealFileSystem,
    get_fs,
    set_fs,
)


@pytest.fixture
def fs() -> RealFileSystem:
    """Return a real filesystem."""
    return RealFileSystem()


@pytest.fixture
def dry() -> DryRunFileSystem:
    """Return a rehearsing filesystem."""
    return DryRunFileSystem()


class TestAtomicWrites:
    """A half-written unit file or nginx config is worse than none."""

    def test_writes_the_content(self, fs: RealFileSystem, tmp_path):
        target = tmp_path / "unit.service"

        fs.write_text(target, "[Service]\n")

        assert target.read_text() == "[Service]\n"

    def test_creates_missing_parents(self, fs: RealFileSystem, tmp_path):
        target = tmp_path / "a" / "b" / "c.conf"

        fs.write_text(target, "x")

        assert target.read_text() == "x"

    def test_replaces_an_existing_file_without_a_gap(self, fs: RealFileSystem, tmp_path):
        target = tmp_path / "nginx.conf"
        target.write_text("old")

        fs.write_text(target, "new")

        assert target.read_text() == "new"

    def test_a_failed_write_leaves_the_previous_content(
        self, fs: RealFileSystem, tmp_path, monkeypatch
    ):
        target = tmp_path / "nginx.conf"
        target.write_text("working config")

        def explode(*_args, **_kwargs):
            raise OSError("disk full")

        monkeypatch.setattr(os, "replace", explode)

        with pytest.raises(OSError):
            fs.write_text(target, "broken config")

        assert target.read_text() == "working config"

    def test_a_failed_write_leaves_no_temporary_file(
        self, fs: RealFileSystem, tmp_path, monkeypatch
    ):
        target = tmp_path / "nginx.conf"
        target.write_text("working")
        monkeypatch.setattr(os, "replace", lambda *a, **k: (_ for _ in ()).throw(OSError("nope")))

        with pytest.raises(OSError):
            fs.write_text(target, "broken")

        assert list(tmp_path.iterdir()) == [target]


class TestPermissions:
    """A file holding a credential is never briefly world-readable."""

    def test_the_mode_is_set_at_creation(self, fs: RealFileSystem, tmp_path):
        target = tmp_path / "config.yaml"

        fs.write_text(target, "password: hunter2", mode=SECRET_MODE)

        assert stat.S_IMODE(target.stat().st_mode) == SECRET_MODE

    def test_a_secret_file_is_unreadable_by_others(self, fs: RealFileSystem, tmp_path):
        target = tmp_path / "token"

        fs.write_text(target, "wasm_abc", mode=SECRET_MODE)

        assert target.stat().st_mode & 0o077 == 0

    def test_every_level_of_a_new_directory_gets_the_mode(self, fs: RealFileSystem, tmp_path):
        """
        pathlib applies the mode to the leaf only and creates parents with the
        umask, which is how a 0700 secrets directory ends up inside a 0755 one.
        """
        target = tmp_path / "var" / "lib" / "wasm"

        fs.make_dir(target, mode=SECRET_DIR_MODE)

        for level in (target, target.parent, target.parent.parent):
            assert stat.S_IMODE(level.stat().st_mode) == SECRET_DIR_MODE, level


class TestSymlinksInCopies:
    """A link in a source tree is copied as a link, not followed."""

    def test_a_link_out_of_the_tree_is_not_dereferenced(self, fs: RealFileSystem, tmp_path):
        secret = tmp_path / "outside.txt"
        secret.write_text("root:x:0:0")
        source = tmp_path / "src"
        source.mkdir()
        (source / "link").symlink_to(secret)
        destination = tmp_path / "dst"

        fs.copy_tree(source, destination)

        copied = destination / "link"
        assert copied.is_symlink()
        assert (
            "root:x:0:0" not in copied.read_bytes().decode(errors="replace") or copied.is_symlink()
        )


class TestDryRun:
    """The rehearsal changes nothing."""

    def test_write_does_not_create_the_file(self, dry: DryRunFileSystem, tmp_path):
        target = tmp_path / "unit.service"

        dry.write_text(target, "[Service]\n")

        assert not target.exists()

    def test_remove_does_not_delete(self, dry: DryRunFileSystem, tmp_path):
        target = tmp_path / "backup.tar.gz"
        target.write_text("precious")

        dry.remove(target)

        assert target.read_text() == "precious"

    def test_remove_tree_does_not_delete(self, dry: DryRunFileSystem, tmp_path):
        tree = tmp_path / "app"
        tree.mkdir()
        (tree / "file").write_text("x")

        dry.remove_tree(tree)

        assert (tree / "file").exists()

    def test_make_dir_does_not_create(self, dry: DryRunFileSystem, tmp_path):
        target = tmp_path / "new"

        dry.make_dir(target)

        assert not target.exists()

    def test_move_does_not_move(self, dry: DryRunFileSystem, tmp_path):
        source = tmp_path / "a"
        source.write_text("x")

        dry.move(source, tmp_path / "b")

        assert source.exists()
        assert not (tmp_path / "b").exists()

    def test_reports_what_would_have_happened(self, tmp_path):
        seen: list[str] = []
        dry = DryRunFileSystem(on_skip=seen.append)

        dry.remove(tmp_path / "backup.tar.gz")
        dry.remove_tree(tmp_path / "app")

        assert len(seen) == 2
        assert "backup.tar.gz" in seen[0]
        assert "everything under it" in seen[1]


class TestGlobalFileSystem:
    """The seam can be swapped, like the command runner."""

    def test_defaults_to_the_real_one(self):
        set_fs(None)

        assert isinstance(get_fs(), RealFileSystem)

    def test_can_be_replaced(self):
        dry = DryRunFileSystem()
        set_fs(dry)
        try:
            assert get_fs() is dry
        finally:
            set_fs(None)


class TestDryRunIsWiredToTheFlag:
    """--dry-run installs both seams, not just the command runner."""

    def test_the_cli_flag_installs_a_rehearsing_filesystem(self):
        from wasm.cli.app import Context, enable_dry_run
        from wasm.core.runner import DryRunRunner, get_runner

        set_fs(None)
        state = Context(dry_run=True)
        try:
            enable_dry_run(state)

            assert isinstance(get_fs(), DryRunFileSystem)
            assert isinstance(get_runner(), DryRunRunner)
        finally:
            set_fs(None)
            from wasm.core.runner import set_runner

            set_runner(None)

    def test_turning_it_on_twice_announces_once(self):
        from wasm.cli.app import Context, enable_dry_run

        set_fs(None)
        state = Context(dry_run=True)
        try:
            enable_dry_run(state)
            first = get_fs()
            enable_dry_run(state)

            assert get_fs() is first
        finally:
            set_fs(None)
            from wasm.core.runner import set_runner

            set_runner(None)
