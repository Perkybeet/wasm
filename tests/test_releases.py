"""
Tests for the release layout: releases/<id>, current, shared/.

These run on real trees in ``tmp_path`` because the properties that matter are
properties of the filesystem: that ``current`` resolves at every instant of an
activation, that links are relative and land where they should, and that a
rehearsal leaves the tree byte-identical. A mock would only prove the code
calls what it calls.
"""

from __future__ import annotations

import io
import os
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from wasm.core.exceptions import DeploymentError
from wasm.core.fs import DryRunFileSystem, RealFileSystem
from wasm.core.logger import Logger
from wasm.deployers.releases import Release, ReleaseManager

SHA_A = "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678"
SHA_B = "b2c3d4e5f60718293a4b5c6d7e8f901234567890"
SHA_C = "c3d4e5f60718293a4b5c6d7e8f90123456789012"
START = datetime(2026, 9, 25, 14, 30, 12, tzinfo=timezone.utc)


class Clock:
    """A clock the test moves by hand."""

    def __init__(self, now: datetime = START) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def tick(self, seconds: int = 1) -> None:
        self.now += timedelta(seconds=seconds)


@pytest.fixture
def app(tmp_path: Path) -> Path:
    """An application directory."""
    path = tmp_path / "apps" / "example"
    path.mkdir(parents=True)
    return path


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def output() -> io.StringIO:
    """What the manager's logger printed."""
    return io.StringIO()


@pytest.fixture
def manager(app: Path, clock: Clock, output: io.StringIO) -> ReleaseManager:
    return ReleaseManager(
        app,
        fs=RealFileSystem(),
        clock=clock,
        logger=Logger(no_color=True, stream=output),
    )


def build(manager: ReleaseManager, clock: Clock, commit: str | None = SHA_A) -> Path:
    """Create a release with something in it, as a deploy would, one second later."""
    clock.tick()
    path = manager.new_release_dir(commit)
    (path / "index.html").write_text(f"built from {commit}")
    return path


def snapshot(root: Path) -> dict[str, tuple[object, ...]]:
    """
    Describe a tree well enough that any change to it shows.

    Directory mtimes are included, so an entry created and removed again (a
    temporary link cleaned up behind the rehearsal's back) is caught too.
    """
    state: dict[str, tuple[object, ...]] = {}
    for directory, dirs, files in os.walk(root, followlinks=False):
        for name in dirs + files:
            path = Path(directory) / name
            info = path.lstat()
            key = str(path.relative_to(root))
            if path.is_symlink():
                state[key] = ("link", os.readlink(path), info.st_mtime_ns)
            elif path.is_dir():
                state[key] = ("dir", stat.S_IMODE(info.st_mode), info.st_mtime_ns)
            else:
                content = path.read_bytes()
                state[key] = ("file", content, stat.S_IMODE(info.st_mode), info.st_mtime_ns)
    state["."] = ("dir", stat.S_IMODE(root.lstat().st_mode), root.lstat().st_mtime_ns)
    return state


# ---------------------------------------------------------------------------
# Release ids
# ---------------------------------------------------------------------------


class TestReleaseIds:
    """Ids name the build and sort in the order the builds were made."""

    def test_stamp_and_short_commit(self, manager: ReleaseManager, app: Path):
        path = manager.new_release_dir(SHA_A)

        assert path == app / "releases" / "20260925-143012-a1b2c3d"
        assert path.is_dir()
        assert list(path.iterdir()) == []

    def test_a_source_without_git(self, manager: ReleaseManager):
        assert manager.new_release_dir(None).name == "20260925-143012-nogit"

    def test_the_clock_is_read_in_utc(self, app: Path):
        madrid = timezone(timedelta(hours=2))
        manager = ReleaseManager(
            app, fs=RealFileSystem(), clock=lambda: datetime(2026, 9, 25, 16, 30, 12, tzinfo=madrid)
        )

        assert manager.new_release_dir(SHA_A).name == "20260925-143012-a1b2c3d"

    def test_uppercase_commits_are_normalized(self, manager: ReleaseManager):
        assert manager.new_release_dir(SHA_A.upper()).name.endswith("-a1b2c3d")

    @pytest.mark.parametrize("commit", ["main", "../../etc", "a1b2c3", "a1b2c3d/x", "g" * 40])
    def test_something_that_is_not_a_commit_is_refused(
        self, manager: ReleaseManager, app: Path, commit: str
    ):
        """The commit becomes a directory name; anything but hex could be a path."""
        with pytest.raises(DeploymentError, match="not a commit id"):
            manager.new_release_dir(commit)

        assert not (app / "releases").exists()

    def test_unique_within_one_second(self, manager: ReleaseManager):
        ids = [manager.new_release_dir(SHA_A).name for _ in range(3)]

        assert ids == [
            "20260925-143012-a1b2c3d",
            "20260925-143012-a1b2c3d-2",
            "20260925-143012-a1b2c3d-3",
        ]

    def test_the_sequence_orders_different_commits_within_one_second(self, manager: ReleaseManager):
        """By string, a...-2 would sort before b..., though it was made after it."""
        first = manager.new_release_dir(SHA_B)
        second = manager.new_release_dir(SHA_A)

        assert second.name == "20260925-143012-a1b2c3d-2"
        assert [r.id for r in manager.list()] == [second.name, first.name]

    def test_a_name_taken_by_a_concurrent_deploy_is_skipped(self, app: Path, clock: Clock):
        """Two deploys scanning at the same moment must not build in one directory."""

        class Racer(RealFileSystem):
            """Another process claims the first name between the scan and the mkdir."""

            raced = False

            def make_dir(self, path, *, mode=0o755, parents=True, exist_ok=True):
                if not exist_ok and not self.raced:
                    self.raced = True
                    path.mkdir()
                super().make_dir(path, mode=mode, parents=parents, exist_ok=exist_ok)

        manager = ReleaseManager(app, fs=Racer(), clock=clock)

        path = manager.new_release_dir(SHA_A)

        assert path.name == "20260925-143012-a1b2c3d-2"
        assert (app / "releases" / "20260925-143012-a1b2c3d").is_dir()

    def test_a_clock_that_went_backwards_still_makes_the_newest_release(
        self, manager: ReleaseManager, clock: Clock
    ):
        """Otherwise rollback would treat the build just made as history."""
        first = manager.new_release_dir(SHA_A)
        clock.tick(-3600)

        second = manager.new_release_dir(SHA_B)

        assert manager.list()[0].path == second
        assert second.name > first.name


# ---------------------------------------------------------------------------
# Listing
# ---------------------------------------------------------------------------


class TestListAndCurrent:
    def test_nothing_before_the_first_release(self, manager: ReleaseManager):
        assert manager.list() == []
        assert manager.current() is None

    def test_newest_first_with_the_active_one_marked(
        self, manager: ReleaseManager, clock: Clock, app: Path
    ):
        old = build(manager, clock, SHA_A)
        new = build(manager, clock, None)
        manager.activate(old)

        releases = manager.list()

        assert releases == [
            Release(
                id=new.name,
                path=app / "releases" / new.name,
                commit=None,
                created_at="2026-09-25T14:30:14+00:00",
                active=False,
            ),
            Release(
                id=old.name,
                path=app / "releases" / old.name,
                commit="a1b2c3d",
                created_at="2026-09-25T14:30:13+00:00",
                active=True,
            ),
        ]
        assert manager.current() == releases[1]

    def test_only_release_directories_count(
        self, manager: ReleaseManager, clock: Clock, app: Path, tmp_path: Path
    ):
        """Nothing else in releases/ can be activated or pruned."""
        real = build(manager, clock)
        releases = app / "releases"
        (releases / "20260101-000000-aaaaaaa").write_text("a file named like a release")
        (releases / "20260101-000001-bbbbbbb").symlink_to(tmp_path)
        (releases / "old-copy").mkdir()

        assert [r.id for r in manager.list()] == [real.name]

    def test_a_dangling_current_means_no_active_release(
        self, manager: ReleaseManager, clock: Clock, app: Path
    ):
        build(manager, clock)
        (app / "current").symlink_to("releases/20200101-000000-deadbee")

        assert manager.current() is None

    def test_a_link_wasm_did_not_write_is_not_trusted(
        self, manager: ReleaseManager, clock: Clock, app: Path
    ):
        release = build(manager, clock)
        (app / "current").symlink_to(release)  # absolute, not releases/<id>

        assert manager.current() is None


# ---------------------------------------------------------------------------
# Activation
# ---------------------------------------------------------------------------


class TestActivate:
    def test_the_first_activation(self, manager: ReleaseManager, clock: Clock, app: Path):
        release = build(manager, clock)

        previous = manager.activate(release)

        assert previous is None
        assert os.readlink(app / "current") == f"releases/{release.name}"
        assert (app / "current" / "index.html").read_text() == f"built from {SHA_A}"

    def test_returns_the_release_it_replaced(self, manager: ReleaseManager, clock: Clock):
        old = build(manager, clock, SHA_A)
        new = build(manager, clock, SHA_B)
        manager.activate(old)

        previous = manager.activate(new)

        assert previous is not None
        assert previous.id == old.name
        assert previous.active is False
        current = manager.current()
        assert current is not None and current.id == new.name

    def test_current_resolves_at_every_instant_of_the_swap(
        self, manager: ReleaseManager, clock: Clock, app: Path, monkeypatch
    ):
        """
        The swap is one rename of a fully formed link over the old one. At the
        moment of the rename ``current`` still serves the old release, and the
        link about to replace it already serves the new one.
        """
        old = build(manager, clock, SHA_A)
        new = build(manager, clock, SHA_B)
        manager.activate(old)
        current = app / "current"
        renames: list[str] = []
        real_replace = os.replace
        real_unlink = Path.unlink

        def spy(source, destination):
            assert Path(destination) == current
            assert (current / "index.html").read_text() == f"built from {SHA_A}"
            assert Path(source).name.startswith("current.tmp-")
            assert (Path(source) / "index.html").read_text() == f"built from {SHA_B}"
            renames.append(Path(source).name)
            real_replace(source, destination)

        def guarded_unlink(self, *args, **kwargs):
            assert self != current, "current was removed: it did not resolve for a moment"
            real_unlink(self, *args, **kwargs)

        monkeypatch.setattr(os, "replace", spy)
        monkeypatch.setattr(Path, "unlink", guarded_unlink)

        manager.activate(new)

        assert len(renames) == 1
        assert (current / "index.html").read_text() == f"built from {SHA_B}"

    def test_a_failed_swap_keeps_the_old_release_and_leaves_no_temporary_link(
        self, manager: ReleaseManager, clock: Clock, app: Path, monkeypatch
    ):
        old = build(manager, clock, SHA_A)
        new = build(manager, clock, SHA_B)
        manager.activate(old)

        def failing_rename(*_args, **_kwargs):
            raise OSError(30, "Read-only file system")

        monkeypatch.setattr(os, "replace", failing_rename)

        with pytest.raises(DeploymentError) as raised:
            manager.activate(new)

        assert f"release {old.name} is still active" in raised.value.details
        assert os.readlink(app / "current") == f"releases/{old.name}"
        assert sorted(p.name for p in app.iterdir()) == ["current", "releases"]

    def test_activating_the_active_release_changes_nothing(
        self, manager: ReleaseManager, clock: Clock, app: Path
    ):
        release = build(manager, clock)
        manager.activate(release)
        before = snapshot(app)

        again = manager.activate(release)

        assert again is not None and again.id == release.name and again.active
        assert snapshot(app) == before

    def test_a_path_outside_releases_is_refused(
        self, manager: ReleaseManager, app: Path, tmp_path: Path
    ):
        elsewhere = tmp_path / "20260925-143012-a1b2c3d"
        elsewhere.mkdir()

        with pytest.raises(DeploymentError, match="is not a release"):
            manager.activate(elsewhere)

        assert not os.path.lexists(app / "current")

    def test_a_release_that_does_not_exist_is_refused(
        self, manager: ReleaseManager, clock: Clock, app: Path
    ):
        real = build(manager, clock)

        with pytest.raises(DeploymentError, match="does not exist") as raised:
            manager.activate(app / "releases" / "20200101-000000-deadbee")

        assert real.name in raised.value.details
        assert not os.path.lexists(app / "current")

    def test_a_symlinked_release_is_refused(
        self, manager: ReleaseManager, app: Path, tmp_path: Path
    ):
        (app / "releases").mkdir()
        planted = app / "releases" / "20260925-143012-a1b2c3d"
        planted.symlink_to(tmp_path)

        with pytest.raises(DeploymentError, match="is not a release"):
            manager.activate(planted)

    def test_an_in_place_tree_is_never_replaced(
        self, manager: ReleaseManager, clock: Clock, app: Path
    ):
        """The release engine must not be applied to an in-place app implicitly."""
        release = build(manager, clock)
        live = app / "current"
        live.mkdir()
        (live / "server.js").write_text("the running app")

        with pytest.raises(DeploymentError, match="real directory"):
            manager.activate(release)

        assert (live / "server.js").read_text() == "the running app"
        assert not live.is_symlink()


# ---------------------------------------------------------------------------
# Shared paths
# ---------------------------------------------------------------------------


class TestLinkShared:
    def test_env_is_linked_when_shared_has_one(
        self, manager: ReleaseManager, clock: Clock, app: Path
    ):
        (app / "shared").mkdir()
        (app / "shared" / ".env").write_text("SECRET=1\n")
        release = build(manager, clock)

        result = manager.link_shared(release, [])

        assert result.linked == (".env",)
        assert result.conflicts == ()
        assert os.readlink(release / ".env") == "../../shared/.env"
        assert (release / ".env").read_text() == "SECRET=1\n"

    def test_env_is_not_linked_when_shared_has_none(self, manager: ReleaseManager, clock: Clock):
        release = build(manager, clock)

        result = manager.link_shared(release, [".env"])

        assert result.linked == ()
        assert not os.path.lexists(release / ".env")

    def test_a_nested_path_gets_a_relative_link_and_a_shared_directory(
        self, manager: ReleaseManager, clock: Clock, app: Path
    ):
        release = build(manager, clock)

        result = manager.link_shared(release, ["storage/app/public"])

        link = release / "storage" / "app" / "public"
        assert result.linked == ("storage/app/public",)
        assert os.readlink(link) == "../../../../shared/storage/app/public"
        assert (app / "shared" / "storage" / "app" / "public").is_dir()
        assert link.resolve() == (app / "shared" / "storage" / "app" / "public").resolve()
        # The parents inside the release are real directories, not links.
        assert (release / "storage").is_dir() and not (release / "storage").is_symlink()

    def test_the_links_survive_moving_the_application(
        self, manager: ReleaseManager, clock: Clock, app: Path, tmp_path: Path
    ):
        """Relative links are why a backup restored elsewhere still works."""
        (app / "shared" / "uploads").mkdir(parents=True)
        (app / "shared" / "uploads" / "photo.jpg").write_text("jpeg")
        release = build(manager, clock)
        manager.link_shared(release, ["uploads"])
        manager.activate(release)

        moved = tmp_path / "restored"
        app.rename(moved)

        assert (moved / "current" / "uploads" / "photo.jpg").read_text() == "jpeg"

    def test_existing_shared_data_is_linked_as_it_is(
        self, manager: ReleaseManager, clock: Clock, app: Path
    ):
        (app / "shared" / "uploads").mkdir(parents=True)
        (app / "shared" / "uploads" / "photo.jpg").write_text("jpeg")
        release = build(manager, clock)

        manager.link_shared(release, ["uploads"])

        assert (release / "uploads" / "photo.jpg").read_text() == "jpeg"

    def test_a_tracked_copy_wins_and_is_reported(
        self, manager: ReleaseManager, clock: Clock, app: Path, output: io.StringIO
    ):
        (app / "shared" / "uploads").mkdir(parents=True)
        (app / "shared" / ".env").write_text("SECRET=shared\n")
        release = build(manager, clock)
        (release / "uploads").mkdir()
        (release / "uploads" / "logo.png").write_text("tracked")
        (release / ".env").write_text("SECRET=committed\n")

        result = manager.link_shared(release, ["uploads", "storage"])

        assert result.conflicts == (".env", "uploads")
        assert result.linked == ("storage",)
        assert (release / "uploads" / "logo.png").read_text() == "tracked"
        assert not (release / "uploads").is_symlink()
        assert (release / ".env").read_text() == "SECRET=committed\n"
        assert "uploads is already in release" in output.getvalue()
        assert ".env is already in release" in output.getvalue()

    def test_linking_twice_is_harmless(self, manager: ReleaseManager, clock: Clock, app: Path):
        (app / "shared").mkdir()
        (app / "shared" / ".env").write_text("A=1\n")
        release = build(manager, clock)
        manager.link_shared(release, ["uploads"])
        before = snapshot(app)

        result = manager.link_shared(release, ["uploads"])

        assert result.linked == (".env", "uploads")
        assert result.conflicts == ()
        assert snapshot(app) == before

    @pytest.mark.parametrize(
        "escape", ["/etc", "../outside", "uploads/../../outside", "", ".", "./", "a\x00b"]
    )
    def test_a_path_that_could_escape_is_refused_before_anything_changes(
        self, manager: ReleaseManager, clock: Clock, app: Path, escape: str
    ):
        release = build(manager, clock)
        before = snapshot(app)

        with pytest.raises(DeploymentError, match="not a path inside the application"):
            manager.link_shared(release, ["uploads", escape])

        assert snapshot(app) == before

    def test_a_tracked_symlink_is_never_written_through(
        self, manager: ReleaseManager, clock: Clock, tmp_path: Path
    ):
        """A repository is untrusted: its link to /etc must not become a target."""
        outside = tmp_path / "outside"
        outside.mkdir()
        release = build(manager, clock)
        (release / "storage").symlink_to(outside)

        result = manager.link_shared(release, ["storage/app"])

        assert result.conflicts == ("storage/app",)
        assert list(outside.iterdir()) == []

    def test_shared_is_never_created_through_a_symlink(
        self, manager: ReleaseManager, clock: Clock, app: Path, tmp_path: Path
    ):
        """The app user can write to shared/; root must not follow its links."""
        outside = tmp_path / "outside"
        outside.mkdir()
        (app / "shared").mkdir()
        (app / "shared" / "data").symlink_to(outside)
        release = build(manager, clock)

        with pytest.raises(DeploymentError, match="not a plain directory"):
            manager.link_shared(release, ["data/cache"])

        assert list(outside.iterdir()) == []
        assert not os.path.lexists(release / "data")

    def test_an_existing_shared_symlink_is_linked_to_not_through(
        self, manager: ReleaseManager, clock: Clock, app: Path, tmp_path: Path
    ):
        """An operator may keep uploads on another disk; linking to it writes nothing there."""
        disk = tmp_path / "bigdisk"
        disk.mkdir()
        (app / "shared").mkdir()
        (app / "shared" / "uploads").symlink_to(disk)
        release = build(manager, clock)

        result = manager.link_shared(release, ["uploads"])

        assert result.linked == ("uploads",)
        assert os.readlink(release / "uploads") == "../../shared/uploads"
        assert list(disk.iterdir()) == []

    def test_a_release_that_does_not_exist_is_not_created(self, manager: ReleaseManager, app: Path):
        missing = app / "releases" / "20260925-143012-a1b2c3d"

        with pytest.raises(DeploymentError, match="does not exist"):
            manager.link_shared(missing, ["storage/logs"])

        assert not missing.exists()
        assert not (app / "shared").exists()


# ---------------------------------------------------------------------------
# Rollback
# ---------------------------------------------------------------------------


class TestRollback:
    def test_goes_to_the_release_before_the_active_one(
        self, manager: ReleaseManager, clock: Clock, app: Path
    ):
        first = build(manager, clock, SHA_A)
        second = build(manager, clock, SHA_B)
        manager.activate(second)

        restored = manager.rollback()

        assert restored.id == first.name
        assert restored.active is True
        assert os.readlink(app / "current") == f"releases/{first.name}"

    def test_rolling_back_twice_goes_further_back(self, manager: ReleaseManager, clock: Clock):
        first = build(manager, clock, SHA_A)
        build(manager, clock, SHA_B)
        third = build(manager, clock, SHA_C)
        manager.activate(third)

        manager.rollback()
        restored = manager.rollback()

        assert restored.id == first.name

    def test_to_a_named_release(self, manager: ReleaseManager, clock: Clock):
        first = build(manager, clock, SHA_A)
        build(manager, clock, SHA_B)
        third = build(manager, clock, SHA_C)
        manager.activate(first)

        restored = manager.rollback(to=third.name)

        assert restored.id == third.name
        current = manager.current()
        assert current is not None and current.id == third.name

    def test_without_an_earlier_release_it_says_what_to_do(
        self, manager: ReleaseManager, clock: Clock, app: Path
    ):
        only = build(manager, clock)
        manager.activate(only)

        with pytest.raises(DeploymentError, match="nothing earlier") as raised:
            manager.rollback()

        assert "Deploy a known-good commit" in raised.value.details
        assert os.readlink(app / "current") == f"releases/{only.name}"

    def test_without_an_active_release_it_names_the_ones_there_are(
        self, manager: ReleaseManager, clock: Clock
    ):
        release = build(manager, clock)

        with pytest.raises(DeploymentError, match="no active release") as raised:
            manager.rollback()

        assert release.name in raised.value.details

    @pytest.mark.parametrize("missing", ["20200101-000000-deadbee", "../../etc", ""])
    def test_a_release_that_does_not_exist_is_refused(
        self, manager: ReleaseManager, clock: Clock, app: Path, missing: str
    ):
        first = build(manager, clock, SHA_A)
        second = build(manager, clock, SHA_B)
        manager.activate(second)

        with pytest.raises(DeploymentError, match="does not exist") as raised:
            manager.rollback(to=missing)

        assert first.name in raised.value.details
        assert os.readlink(app / "current") == f"releases/{second.name}"

    def test_a_pruned_release_cannot_be_rolled_back_to(self, manager: ReleaseManager, clock: Clock):
        first = build(manager, clock, SHA_A)
        second = build(manager, clock, SHA_B)
        manager.activate(second)
        manager.prune(keep=1)

        with pytest.raises(DeploymentError, match="does not exist"):
            manager.rollback(to=first.name)
        with pytest.raises(DeploymentError, match="nothing earlier"):
            manager.rollback()


# ---------------------------------------------------------------------------
# Retention
# ---------------------------------------------------------------------------


class TestPrune:
    def test_keeps_the_newest(self, manager: ReleaseManager, clock: Clock):
        paths = [build(manager, clock) for _ in range(5)]
        manager.activate(paths[-1])

        removed = manager.prune(keep=2)

        assert [r.id for r in removed] == [p.name for p in paths[:3]]
        assert [r.id for r in manager.list()] == [paths[4].name, paths[3].name]
        assert not any(p.exists() for p in paths[:3])

    def test_never_removes_the_active_release(self, manager: ReleaseManager, clock: Clock):
        """After a rollback the active release can be older than every kept one."""
        paths = [build(manager, clock) for _ in range(4)]
        manager.activate(paths[0])

        removed = manager.prune(keep=1)

        assert [r.id for r in removed] == [paths[1].name, paths[2].name]
        assert paths[0].is_dir()
        assert paths[3].is_dir()
        current = manager.current()
        assert current is not None and current.id == paths[0].name

    def test_nothing_to_remove(self, manager: ReleaseManager, clock: Clock):
        build(manager, clock)

        assert manager.prune(keep=5) == []

    @pytest.mark.parametrize("keep", [0, -1])
    def test_keep_must_be_at_least_one(self, manager: ReleaseManager, clock: Clock, keep: int):
        release = build(manager, clock)

        with pytest.raises(DeploymentError, match="Cannot keep"):
            manager.prune(keep=keep)

        assert release.is_dir()

    def test_shared_data_survives_pruning_the_releases_linked_to_it(
        self, manager: ReleaseManager, clock: Clock, app: Path
    ):
        (app / "shared" / "uploads").mkdir(parents=True)
        (app / "shared" / "uploads" / "photo.jpg").write_text("jpeg")
        old = build(manager, clock)
        manager.link_shared(old, ["uploads"])
        new = build(manager, clock)
        manager.activate(new)

        manager.prune(keep=1)

        assert not old.exists()
        assert (app / "shared" / "uploads" / "photo.jpg").read_text() == "jpeg"

    def test_what_is_not_a_release_is_never_removed(
        self, manager: ReleaseManager, clock: Clock, app: Path
    ):
        stray = app / "releases" / "manual-backup"
        build(manager, clock)
        stray.mkdir()
        latest = build(manager, clock)
        manager.activate(latest)

        manager.prune(keep=1)

        assert stray.is_dir()


# ---------------------------------------------------------------------------
# Rehearsal
# ---------------------------------------------------------------------------


class TestDryRun:
    """--dry-run walks the whole release flow and leaves the tree byte-identical."""

    @pytest.fixture
    def deployed(self, manager: ReleaseManager, clock: Clock, app: Path) -> Path:
        """An application with history, an active release and shared data."""
        (app / "shared" / "uploads").mkdir(parents=True)
        (app / "shared" / "uploads" / "photo.jpg").write_text("jpeg")
        (app / "shared" / ".env").write_text("SECRET=1\n")
        for commit in (SHA_A, SHA_B, SHA_C):
            release = build(manager, clock, commit)
            manager.link_shared(release, ["uploads"])
        manager.activate(release)
        return app

    def test_the_whole_flow_changes_nothing(self, deployed: Path, clock: Clock):
        dry = DryRunFileSystem()
        manager = ReleaseManager(deployed, fs=dry, clock=clock, logger=Logger(stream=io.StringIO()))
        before = snapshot(deployed.parent)
        clock.tick()

        release = manager.new_release_dir(SHA_A)
        linked = manager.link_shared(release, ["uploads", "storage/logs"])
        previous = manager.activate(release)
        rolled_back = manager.rollback()
        pruned = manager.prune(keep=1)

        assert snapshot(deployed.parent) == before
        assert not release.exists()
        # The rehearsal still reports what a real run would have done.
        assert linked.linked == (".env", "uploads", "storage/logs")
        assert previous is not None and previous.id.endswith("-c3d4e5f")
        assert rolled_back.id.endswith("-b2c3d4e")
        assert [r.id for r in pruned] == [r.id for r in manager.list()[1:][::-1]]
        assert any(f"would link {deployed / 'current'}" in line for line in dry.skipped)
        assert any("would delete directory" in line for line in dry.skipped)

    def test_rehearsed_releases_in_one_second_get_distinct_names(self, app: Path, clock: Clock):
        manager = ReleaseManager(app, fs=DryRunFileSystem(), clock=clock)

        first = manager.new_release_dir(SHA_A)
        second = manager.new_release_dir(SHA_A)

        assert first != second
        assert not (app / "releases").exists()

    def test_a_first_deploy_creates_nothing(self, app: Path, clock: Clock):
        manager = ReleaseManager(app, fs=DryRunFileSystem(), clock=clock)
        before = snapshot(app)

        release = manager.new_release_dir(None)
        manager.link_shared(release, ["uploads"])
        assert manager.activate(release) is None

        assert snapshot(app) == before

    def test_the_process_wide_filesystem_is_honoured(
        self, deployed: Path, clock: Clock, monkeypatch
    ):
        """No fs passed: --dry-run installs the seam globally and it must apply."""
        from wasm.core import fs as fs_module

        monkeypatch.setattr(fs_module, "_default_fs", DryRunFileSystem())
        manager = ReleaseManager(deployed, clock=clock, logger=Logger(stream=io.StringIO()))
        before = snapshot(deployed)

        manager.rollback()
        manager.prune(keep=1)

        assert snapshot(deployed) == before
