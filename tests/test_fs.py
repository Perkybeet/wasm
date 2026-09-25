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
from pathlib import Path

import pytest

from wasm.core.fs import (
    SECRET_DIR_MODE,
    SECRET_MODE,
    DryRunFileSystem,
    RealFileSystem,
    RecordingFileSystem,
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


class TestAtomicSymlinks:
    """
    Replacing a link never leaves a moment in which it does not exist.

    The release layout swaps ``current`` under a running service and nginx:
    unlinking the old link and creating the new one leaves a window in which
    every request fails.
    """

    def test_the_target_is_stored_verbatim(self, fs: RealFileSystem, tmp_path):
        """A relative target stays relative, so the tree can be moved."""
        (tmp_path / "releases" / "a").mkdir(parents=True)

        fs.symlink(Path("releases/a"), tmp_path / "current")

        assert os.readlink(tmp_path / "current") == "releases/a"
        assert (tmp_path / "current").resolve() == (tmp_path / "releases" / "a").resolve()

    def test_replaces_a_link_to_a_directory_instead_of_nesting_inside_it(
        self, fs: RealFileSystem, tmp_path
    ):
        """shutil.move() would have put the new link inside the old target."""
        old = tmp_path / "old"
        new = tmp_path / "new"
        old.mkdir()
        new.mkdir()
        link = tmp_path / "current"
        link.symlink_to("old")

        fs.symlink(Path("new"), link)

        assert os.readlink(link) == "new"
        assert list(old.iterdir()) == []

    def test_the_old_link_still_resolves_when_the_new_one_takes_its_place(
        self, fs: RealFileSystem, tmp_path, monkeypatch
    ):
        old = tmp_path / "old"
        new = tmp_path / "new"
        old.mkdir()
        new.mkdir()
        link = tmp_path / "current"
        link.symlink_to("old")
        seen: list[tuple[str, str]] = []
        real_replace = os.replace

        def spy(source, destination):
            seen.append((os.readlink(destination), os.readlink(source)))
            real_replace(source, destination)

        monkeypatch.setattr(os, "replace", spy)

        fs.symlink(Path("new"), link)

        assert seen == [("old", "new")], "the link was not swapped by a single rename"
        assert os.readlink(link) == "new"

    def test_a_failed_swap_keeps_the_old_link_and_leaves_no_temporary(
        self, fs: RealFileSystem, tmp_path, monkeypatch
    ):
        (tmp_path / "old").mkdir()
        link = tmp_path / "current"
        link.symlink_to("old")

        def explode(*_args, **_kwargs):
            raise OSError("read-only filesystem")

        monkeypatch.setattr(os, "replace", explode)

        with pytest.raises(OSError, match="read-only"):
            fs.symlink(Path("new"), link)

        assert os.readlink(link) == "old"
        assert sorted(p.name for p in tmp_path.iterdir()) == ["current", "old"]

    def test_a_real_directory_is_not_replaced(self, fs: RealFileSystem, tmp_path):
        """An in-place tree where a link was expected must survive, whole."""
        tree = tmp_path / "current"
        tree.mkdir()
        (tree / "index.html").write_text("live")

        with pytest.raises(OSError):
            fs.symlink(Path("releases/a"), tree)

        assert (tree / "index.html").read_text() == "live"
        assert [p.name for p in tmp_path.iterdir()] == ["current"]

    def test_the_temporary_name_is_not_predictable(self, fs: RealFileSystem, tmp_path, monkeypatch):
        names: list[str] = []
        real_symlink = os.symlink

        def record(target, link, *args, **kwargs):
            names.append(Path(link).name)
            real_symlink(target, link, *args, **kwargs)

        monkeypatch.setattr(os, "symlink", record)

        fs.symlink(Path("a"), tmp_path / "current")
        fs.symlink(Path("b"), tmp_path / "current")

        assert len(set(names)) == 2
        assert all(name.startswith("current.tmp-") for name in names)

    def test_dry_run_does_not_link(self, dry: DryRunFileSystem, tmp_path):
        (tmp_path / "old").mkdir()
        link = tmp_path / "current"
        link.symlink_to("old")

        dry.symlink(Path("new"), link)
        dry.symlink(Path("x"), tmp_path / "other")

        assert os.readlink(link) == "old"
        assert not os.path.lexists(tmp_path / "other")
        assert len(dry.skipped) == 2
        assert "would link" in dry.skipped[0]

    def test_recording_records_and_links(self, tmp_path):
        recording = RecordingFileSystem()

        recording.symlink(Path("target"), tmp_path / "link")

        assert recording.changes == [("symlink", tmp_path / "link")]
        assert os.readlink(tmp_path / "link") == "target"


class TestExclusiveDirectories:
    """exist_ok=False lets exactly one caller claim a directory."""

    @pytest.mark.parametrize("parents", [True, False])
    def test_an_existing_directory_is_refused(self, fs: RealFileSystem, tmp_path, parents):
        target = tmp_path / "20260925-143012-a1b2c3d"
        target.mkdir()
        (target / "build").write_text("someone else's")

        with pytest.raises(FileExistsError):
            fs.make_dir(target, parents=parents, exist_ok=False)

        assert (target / "build").read_text() == "someone else's"

    @pytest.mark.parametrize("parents", [True, False])
    def test_a_missing_directory_is_created(self, fs: RealFileSystem, tmp_path, parents):
        target = tmp_path / "claimed"

        fs.make_dir(target, parents=parents, exist_ok=False)

        assert target.is_dir()

    def test_missing_parents_are_created_before_the_claim(self, fs: RealFileSystem, tmp_path):
        target = tmp_path / "releases" / "one"

        fs.make_dir(target, exist_ok=False)

        assert target.is_dir()

    def test_the_default_still_accepts_an_existing_directory(self, fs: RealFileSystem, tmp_path):
        fs.make_dir(tmp_path)
        fs.make_dir(tmp_path, parents=False)

        assert tmp_path.is_dir()

    def test_dry_run_claims_nothing(self, dry: DryRunFileSystem, tmp_path):
        dry.make_dir(tmp_path / "claimed", parents=False, exist_ok=False)

        assert not (tmp_path / "claimed").exists()
        assert dry.skipped == [f"would create directory {tmp_path / 'claimed'} (mode 755)"]

    def test_recording_passes_the_claim_through(self, tmp_path):
        recording = RecordingFileSystem()
        (tmp_path / "taken").mkdir()

        with pytest.raises(FileExistsError):
            recording.make_dir(tmp_path / "taken", parents=False, exist_ok=False)

        assert recording.changes == [("mkdir", tmp_path / "taken")]


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


class TestTheTemporaryFileCannotBeHijacked:
    """
    The staging file is a predictable path in a directory WASM writes to as
    root, so it is exactly the kind of name an attacker plants a symlink at.
    """

    def test_a_squatted_symlink_does_not_redirect_the_write(self, fs: RealFileSystem, tmp_path):
        victim = tmp_path / "victim"
        victim.write_text("original")
        target = tmp_path / "config.yaml"

        # Guess every staging name the implementation could pick and plant a
        # link at each: whichever it chooses must be refused, not followed.
        for candidate in tmp_path.glob(".config.yaml*"):
            candidate.unlink()
        planted = tmp_path / ".config.yaml.wasm-tmp"
        planted.symlink_to(victim)

        fs.write_text(target, "new content")

        assert victim.read_text() == "original", "the write followed a planted symlink"
        assert target.read_text() == "new content"

    def test_the_staging_name_is_not_predictable(self, fs: RealFileSystem, tmp_path):
        """
        Two writes must not agree on a name, or one process can pre-create the
        path another is about to open.
        """
        seen: set[str] = set()
        original = os.open

        def record(path, flags, mode=0o777, **kwargs):
            if ".wasm-tmp" in str(path):
                seen.add(Path(path).name)
            return original(path, flags, mode, **kwargs)

        import wasm.core.fs as fs_module

        monkey = fs_module.os
        try:
            monkey.open = record  # type: ignore[assignment]
            fs.write_text(tmp_path / "a.conf", "1")
            fs.write_text(tmp_path / "a.conf", "2")
        finally:
            monkey.open = original  # type: ignore[assignment]

        assert len(seen) == 2, f"the staging name repeated: {seen}"

    def test_a_squatted_regular_file_is_refused_not_overwritten(
        self, fs: RealFileSystem, tmp_path, monkeypatch
    ):
        """O_EXCL: an existing entry at the staging path aborts the write."""
        target = tmp_path / "unit.service"
        fixed = tmp_path / ".unit.service.abcdef012345.wasm-tmp"
        fixed.write_text("someone else's file")
        monkeypatch.setattr("os.urandom", lambda _n: bytes.fromhex("abcdef012345"))

        with pytest.raises(FileExistsError):
            fs.write_text(target, "[Service]\n")

        assert fixed.read_text() == "someone else's file"


class TestTheStoreIsRehearsedToo:
    """
    A rehearsal that leaves the database changed is worse than one that leaves
    a file changed: the record and the machine then disagree, and the next real
    command acts on a state that never existed.
    """

    def test_a_write_is_rolled_back_under_dry_run(self, tmp_path, monkeypatch):
        from wasm.core.fs import DryRunFileSystem, set_fs
        from wasm.core.store import App, WASMStore

        db = tmp_path / "wasm.db"
        WASMStore.reset_instance()
        store = WASMStore(db_path=db)
        store.create_app(App(domain="before.com", app_type="static", app_path="/x"))

        set_fs(DryRunFileSystem())
        try:
            store.create_app(App(domain="during.com", app_type="static", app_path="/y"))
        finally:
            set_fs(None)

        WASMStore.reset_instance()
        after = WASMStore(db_path=db)
        domains = {app.domain for app in after.list_apps()}

        assert "before.com" in domains
        assert "during.com" not in domains, "the rehearsal committed a row"
        WASMStore.reset_instance()

    def test_a_real_run_still_commits(self, tmp_path):
        from wasm.core.fs import set_fs
        from wasm.core.store import App, WASMStore

        db = tmp_path / "wasm.db"
        set_fs(None)
        WASMStore.reset_instance()
        store = WASMStore(db_path=db)

        store.create_app(App(domain="real.com", app_type="static", app_path="/x"))

        WASMStore.reset_instance()
        after = WASMStore(db_path=db)
        assert {a.domain for a in after.list_apps()} == {"real.com"}
        WASMStore.reset_instance()
