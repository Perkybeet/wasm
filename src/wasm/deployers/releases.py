# Copyright (c) 2024-2026 Yago Lopez Prado
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Release directories: the layout that makes a deploy atomic and a rollback instant.

An application on the release layout looks like this::

    /var/www/apps/{app}/
      releases/20260925-143012-a1b2c3d/   one complete build, immutable once active
      releases/20260924-101500-9f8e7d6/
      current -> releases/20260925-143012-a1b2c3d
      shared/.env                         secrets, outside every release
      shared/<persistent paths>           uploads, storage... linked into each release

The in-place layout builds over the tree the running service reads from, so a
failed build leaves a half-updated application and a rollback means restoring
a backup. Here every build gets its own directory, the service and the web
server only ever see ``current``, and going back is re-pointing one link.

:class:`ReleaseManager` is the only code that knows this layout. The pipeline,
the migration of in-place apps, the API and the CLI all go through it, so the
rules below hold for every caller:

- ``current`` is swapped with a rename, never removed and recreated, so it
  resolves at every instant of an activation.
- Every link is relative, so the whole application directory can be moved or
  restored from a backup somewhere else and still work.
- Nothing is written through a symlink found inside a release or under
  ``shared/``: a repository is untrusted input, and a tracked link to ``/etc``
  must not become a place WASM writes to as root.
- Every change goes through the :mod:`wasm.core.fs` seam, so ``--dry-run``
  rehearses all of it and changes nothing.
"""

from __future__ import annotations

import builtins
import dataclasses
import os
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from wasm.core.exceptions import DeploymentError
from wasm.core.fs import FileSystem, get_fs
from wasm.core.logger import Logger
from wasm.core.runner import CommandRunner, get_runner

#: Directory, inside the application directory, that holds one build per entry.
RELEASES_DIR = "releases"

#: The link the unit and the web server point at.
CURRENT_LINK = "current"

#: Directory for what outlives a release: secrets and persistent data.
SHARED_DIR = "shared"

#: Linked into every release whenever ``shared/`` holds one.
ENV_FILE = ".env"

#: The repository cache: a clone releases are exported from.
REPO_CACHE_DIR = "repo"

#: Stands in for the commit when the source is not a git checkout.
NO_COMMIT = "nogit"

#: How much of the commit goes into a release id, as ``git log --oneline`` shows it.
SHORT_COMMIT_LENGTH = 7

#: UTC, second resolution. UTC because local time repeats an hour every
#: autumn, and ids must sort in creation order.
_STAMP_FORMAT = "%Y%m%d-%H%M%S"

#: A release id: stamp, short commit, and a sequence for the second and later
#: releases created within the same second.
_RELEASE_ID = re.compile(
    r"^(?P<stamp>\d{8}-\d{6})-(?P<commit>[0-9a-f]{7}|nogit)(?:-(?P<sequence>[1-9][0-9]*))?$"
)

#: A full or abbreviated commit id. 64 covers SHA-256 repositories.
_COMMIT = re.compile(r"^[0-9a-f]{7,64}$")

#: How many times to retry a release name another process took first.
_MAX_NAME_ATTEMPTS = 100


@dataclass(frozen=True)
class Release:
    """
    One build of an application, as found on disk.

    Attributes:
        id: Directory name, ``YYYYMMDD-HHMMSS-<short commit>`` with ``-N``
            appended for the Nth release created within the same second.
        path: The release directory.
        commit: Short commit the release was built from, or None when the
            source was not a git checkout.
        created_at: Creation time from the id, ISO 8601 in UTC.
        active: Whether ``current`` points at it.
    """

    id: str
    path: Path
    commit: str | None
    created_at: str
    active: bool


@dataclass(frozen=True)
class SharedLinks:
    """
    What :meth:`ReleaseManager.link_shared` did.

    Attributes:
        linked: Relative paths that now point into ``shared/``, including the
            ones that already did.
        conflicts: Relative paths left alone because the release already has
            something there, usually a file tracked in the repository. The
            tracked copy wins; the operator decides whether it should.
    """

    linked: tuple[str, ...]
    conflicts: tuple[str, ...]


def _utc_now() -> datetime:
    """
    Return the current time in UTC.

    Returns:
        An aware datetime.
    """
    return datetime.now(timezone.utc)


def _short_commit(commit: str | None) -> str:
    """
    Turn a commit into the part of a release id that names it.

    Args:
        commit: Full or abbreviated commit id, or None for a non-git source.

    Returns:
        The first seven hex digits, or ``nogit``.

    Raises:
        DeploymentError: The commit is not a hex commit id. It becomes part of
            a directory name, so anything else could be a path.
    """
    if not commit:
        return NO_COMMIT
    normalized = commit.strip().lower()
    if not _COMMIT.match(normalized):
        raise DeploymentError(
            f"{commit!r} is not a commit id",
            details="Pass the full or abbreviated hash from `git rev-parse HEAD`, "
            "or None for a source that is not a git checkout.",
        )
    return normalized[:SHORT_COMMIT_LENGTH]


def release_order_key(release_id: str) -> tuple[str, int, str]:
    """
    Sort key that puts release ids in creation order.

    The sequence decides between releases created in the same second, whatever
    their commits: plain string order would put ``-aaaaaaa-2`` before
    ``-bbbbbbb`` even when it was created after it.

    Args:
        release_id: A valid release id.

    Returns:
        (stamp, sequence, id).

    Raises:
        ValueError: If it is not a release id.
    """
    match = _RELEASE_ID.match(release_id)
    if match is None:
        raise ValueError(f"not a release id: {release_id!r}")
    return match["stamp"], int(match["sequence"] or 1), release_id


def is_release_id(value: str) -> bool:
    """
    Tell whether a string is shaped like a release id.

    Args:
        value: Candidate, such as a path parameter.

    Returns:
        True for ``YYYYMMDD-HHMMSS-<commit>[-N]``. Nothing else can name a
        release, so nothing else needs to reach the disk.
    """
    return _RELEASE_ID.match(value) is not None


def persistent_path(raw: str) -> PurePosixPath:
    """
    Validate a persistent path from the app configuration.

    Args:
        raw: Path relative to the release root, such as ``storage/app/public``.

    Returns:
        The normalized relative path.

    Raises:
        DeploymentError: The path is empty, absolute or climbs with ``..``, so
            it could name something outside the release or outside ``shared/``.
    """
    candidate = PurePosixPath(raw)
    if (
        not raw
        or "\x00" in raw
        or candidate.is_absolute()
        or ".." in candidate.parts
        or not candidate.parts
    ):
        raise DeploymentError(
            f"Persistent path {raw!r} is not a path inside the application",
            details="Persistent paths are relative to the application root, such as "
            "'storage' or 'public/uploads', and may not be absolute or contain '..'.",
        )
    return candidate


def first_obstacle(root: Path, relative: PurePosixPath) -> Path | None:
    """
    Find the first ancestor of ``root/relative`` that cannot be walked into safely.

    A symlink in the way means anything created below it lands wherever the
    link points, and a relative link placed there would resolve from that other
    place too. A file in the way means a directory cannot be made there.

    Args:
        root: Directory the path is relative to. Not checked itself.
        relative: The path. Its last component is not checked either.

    Returns:
        The offending ancestor, or None when every existing one is a real
        directory.
    """
    current = root
    for part in relative.parts[:-1]:
        current = current / part
        if current.is_symlink():
            return current
        if not os.path.lexists(current):
            return None
        if not current.is_dir():
            return current
    return None


class ReleaseManager:
    """Creates, activates, rolls back and prunes the releases of one application."""

    def __init__(
        self,
        app_path: Path,
        *,
        fs: FileSystem | None = None,
        runner: CommandRunner | None = None,
        clock: Callable[[], datetime] | None = None,
        logger: Logger | None = None,
    ) -> None:
        """
        Initialize the manager.

        Args:
            app_path: The application directory, such as ``/var/www/apps/example``.
            fs: Filesystem every change goes through. Defaults to the
                process-wide one at the time of each change, which is what
                makes ``--dry-run`` apply.
            runner: Command runner for the commands that populate a release.
                Defaults to the process-wide one, for the same reason.
            clock: Returns the current time; release ids are stamped with it.
                Injectable so tests can create releases within one second.
            logger: Where conflicts are reported. Pass the deployment's logger
                so they end up in the captured deploy log.
        """
        self.app_path = Path(app_path)
        self.releases_dir = self.app_path / RELEASES_DIR
        self.shared_dir = self.app_path / SHARED_DIR
        self.current_link = self.app_path / CURRENT_LINK
        self._fs = fs
        self._runner = runner
        self._clock = clock if clock is not None else _utc_now
        self.logger = logger if logger is not None else Logger()
        # Ids this manager handed out. Under --dry-run the directory is never
        # created, so without this a second rehearsed release in the same
        # second would be told the same name as the first, and a rehearsed
        # activation would be refused for a release that "does not exist".
        self._issued: set[str] = set()

    @property
    def fs(self) -> FileSystem:
        """The filesystem every change goes through."""
        return self._fs if self._fs is not None else get_fs()

    @property
    def runner(self) -> CommandRunner:
        """The command runner for commands run inside a release."""
        return self._runner if self._runner is not None else get_runner()

    # ------------------------------------------------------------------
    # Reading the layout
    # ------------------------------------------------------------------

    def list(self) -> builtins.list[Release]:
        """
        List the releases on disk.

        Only real directories named like a release count. A stray file, a
        symlink or a directory someone made by hand is not a release, and
        nothing here will activate or delete it.

        Returns:
            Releases, newest first.
        """
        active = self._active_id()
        return [
            self._release(release_id, active=release_id == active)
            for release_id in sorted(self._ids_on_disk(), key=release_order_key, reverse=True)
        ]

    def current(self) -> Release | None:
        """
        Return the active release.

        Returns:
            The release ``current`` points at, or None when there is no link,
            it is dangling, or it points somewhere that is not a release.
        """
        active = self._active_id()
        return None if active is None else self._release(active, active=True)

    # ------------------------------------------------------------------
    # Changing it
    # ------------------------------------------------------------------

    def next_release_id(self, commit: str | None) -> str:
        """
        Name the release :meth:`new_release_dir` would create now, without creating it.

        For plans shown before anything changes. The name is only a forecast:
        another release created in the meantime moves the real one along.

        Args:
            commit: Commit the release would be built from, or None.

        Returns:
            The release id.

        Raises:
            DeploymentError: The commit is not a commit id.
        """
        stamp, short, sequence = self._next_name(commit)
        return f"{stamp}-{short}" if sequence == 1 else f"{stamp}-{short}-{sequence}"

    def new_release_dir(self, commit: str | None) -> Path:
        """
        Create the directory for a new release.

        Args:
            commit: Commit the release will be built from, or None for a source
                that is not a git checkout.

        Returns:
            The new, empty release directory.

        Raises:
            DeploymentError: The commit is not a commit id, or no free name
                could be claimed.
        """
        stamp, short, sequence = self._next_name(commit)

        self.fs.make_dir(self.releases_dir)
        for _ in range(_MAX_NAME_ATTEMPTS):
            release_id = f"{stamp}-{short}" if sequence == 1 else f"{stamp}-{short}-{sequence}"
            path = self.releases_dir / release_id
            try:
                # Claimed exclusively: a concurrent deploy that scanned the
                # directory at the same moment must build somewhere else.
                self.fs.make_dir(path, parents=False, exist_ok=False)
            except FileExistsError:
                sequence += 1
                continue
            self._issued.add(release_id)
            return path

        raise DeploymentError(
            f"Could not claim a release directory in {self.releases_dir}",
            details=f"{_MAX_NAME_ATTEMPTS} names for {stamp} were taken. Check for "
            "a runaway process creating releases, then retry.",
        )

    def link_shared(self, release: Path, persistent: Sequence[str]) -> SharedLinks:
        """
        Link a release to what it shares with every other release.

        ``.env`` is linked whenever ``shared/.env`` exists. Each persistent
        path becomes a relative link to ``shared/<path>``, and the directory in
        ``shared/`` is created when it is missing. Nothing already in the
        release is moved or replaced: a tracked copy wins, is left exactly as
        it is and is reported as a conflict.

        Args:
            release: Release directory, as returned by :meth:`new_release_dir`.
            persistent: Paths relative to the application root that must
                survive a deploy, such as ``storage`` or ``public/uploads``.

        Returns:
            What was linked and what was left alone.

        Raises:
            DeploymentError: The release is not one of this application's, a
                persistent path could escape the release or ``shared/``, or
                creating it in ``shared/`` would write through a symlink.
        """
        root = self.releases_dir / self._existing_release_id(release)

        # Validate every path before touching anything, so one bad entry does
        # not leave a release half linked.
        paths: builtins.list[PurePosixPath] = []
        if os.path.lexists(self.shared_dir / ENV_FILE):
            paths.append(PurePosixPath(ENV_FILE))
        for raw in persistent:
            path = persistent_path(raw)
            # .env follows its own rule above: linked only when the shared copy
            # exists, never created as a directory for the lack of one.
            if path not in paths and path != PurePosixPath(ENV_FILE):
                paths.append(path)
        for path in paths:
            shared = self.shared_dir / path
            if os.path.lexists(shared):
                continue
            blocked = first_obstacle(self.shared_dir, path)
            if blocked is not None:
                raise DeploymentError(
                    f"Refusing to create {shared}: {blocked} is not a plain directory",
                    details=f"A symlink or file at {blocked} would make WASM create "
                    f"{path} somewhere outside {self.shared_dir}. Replace it with a "
                    "directory, or create the shared path yourself.",
                )

        linked: builtins.list[str] = []
        conflicts: builtins.list[str] = []
        for path in paths:
            link = root / path
            # Relative, so the application directory can be moved or restored
            # elsewhere: up out of the path's own parents, out of releases/<id>,
            # then down into shared/.
            target = Path(*[".."] * (len(path.parts) + 1), SHARED_DIR, path)

            if link.is_symlink() and Path(os.readlink(link)) == target:
                linked.append(str(path))
                continue
            blocked = first_obstacle(root, path)
            if blocked is not None or os.path.lexists(link):
                conflicts.append(str(path))
                self.logger.warning(
                    f"{path} is already in release {root.name}; it was not linked to "
                    f"{SHARED_DIR}/{path}. Untrack it or drop it from the persistent paths."
                )
                continue

            shared = self.shared_dir / path
            if not os.path.lexists(shared):
                self.fs.make_dir(shared)
            if link.parent != root and not link.parent.is_dir():
                self.fs.make_dir(link.parent)
            self.fs.symlink(target, link)
            linked.append(str(path))

        return SharedLinks(linked=tuple(linked), conflicts=tuple(conflicts))

    def activate(self, release: Path) -> Release | None:
        """
        Point ``current`` at a release, atomically.

        The new link is created as ``current.tmp-<random>`` next to the old one
        and renamed over it, so ``current`` resolves at every instant: before
        the rename to the old release, after it to the new one. Restarting the
        service is the caller's job.

        Args:
            release: Release directory to activate.

        Returns:
            The release that was active before, or None when there was none.
            When the release was already active nothing changes and it is
            returned as it is.

        Raises:
            DeploymentError: The path is not one of this application's
                releases, it does not exist, ``current`` is a real directory
                (the in-place layout), or the link could not be swapped, in
                which case the previous release is still active.
        """
        release_id = self._existing_release_id(release)
        if os.path.lexists(self.current_link) and not self.current_link.is_symlink():
            # The in-place layout is never converted by accident: only the
            # explicit migration replaces a real tree with a link.
            raise DeploymentError(
                f"{self.current_link} is a real directory, not a release link",
                details="This application is not on the release layout. Migrate it "
                "explicitly before activating releases.",
            )

        previous = self.current()
        if previous is not None and previous.id == release_id:
            return previous

        try:
            self.fs.symlink(Path(RELEASES_DIR) / release_id, self.current_link)
        except OSError as error:
            raise DeploymentError(
                f"Could not point {self.current_link} at release {release_id}",
                details=f"{error}. Nothing changed: "
                + (
                    f"release {previous.id} is still active."
                    if previous is not None
                    else "there was no active release before."
                ),
            ) from error
        return None if previous is None else dataclasses.replace(previous, active=False)

    def rollback(self, to: str | None = None) -> Release:
        """
        Activate an earlier release.

        Args:
            to: Id of the release to go back to. None means the one created
                just before the active one.

        Returns:
            The release now active.

        Raises:
            DeploymentError: There is no active release, no earlier one, or
                the requested one does not exist.
        """
        releases = self.list()
        active_index = next((i for i, r in enumerate(releases) if r.active), None)

        if to is None:
            if active_index is None:
                raise DeploymentError(
                    "There is no active release to roll back from",
                    details=f"{self.current_link} does not point at a release. "
                    + self._available_hint(releases)
                    + " Name the one to activate explicitly.",
                )
            if active_index + 1 >= len(releases):
                raise DeploymentError(
                    f"Release {releases[active_index].id} is the oldest one; there is "
                    "nothing earlier to roll back to",
                    details="Older releases were pruned or never existed. Deploy a "
                    "known-good commit instead, or restore a backup.",
                )
            target = releases[active_index + 1]
        else:
            found = next((r for r in releases if r.id == to), None)
            if found is None:
                raise DeploymentError(
                    f"Release {to!r} does not exist",
                    details=self._available_hint(releases),
                )
            target = found

        self.activate(target.path)
        return dataclasses.replace(target, active=True)

    def prune(self, keep: int) -> builtins.list[Release]:
        """
        Delete old releases.

        The newest ``keep`` survive, and so does the active one wherever it is
        in the history: after a rollback it can be older than all of them. So
        does the release just before the active one, which is where a
        rollback goes: pruning it would leave the release serving with
        nothing to go back to, even when ``keep`` is 1.

        Args:
            keep: How many of the newest releases to keep. At least 1.

        Returns:
            The releases removed, oldest first.

        Raises:
            DeploymentError: keep is less than 1.
        """
        if keep < 1:
            raise DeploymentError(
                f"Cannot keep {keep} releases",
                details="Keep at least 1 release. A release is the application's "
                "code; pruning them all would leave nothing to run or roll back to.",
            )

        releases = self.list()
        survivors = {r.id for r in releases[:keep]}
        for index, release in enumerate(releases):
            if release.active:
                survivors.add(release.id)
                # The rollback target, as rollback() picks it: the next older
                # release on disk.
                if index + 1 < len(releases):
                    survivors.add(releases[index + 1].id)
        removed: builtins.list[Release] = []
        # Oldest first, so an interruption leaves the newest history in place.
        for release in reversed(releases):
            if release.id in survivors:
                continue
            self.fs.remove_tree(release.path)
            removed.append(release)
        return removed

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _next_name(self, commit: str | None) -> tuple[str, str, int]:
        """
        Work out the parts of the next release id.

        Args:
            commit: Commit the release is built from, or None.

        Returns:
            (stamp, short commit, sequence within the second).

        Raises:
            DeploymentError: The commit is not a commit id.
        """
        short = _short_commit(commit)
        known = self._known_ids()
        stamp = self._now_stamp()
        # A clock that moved backwards (NTP, a VM restored from a snapshot)
        # would stamp the new release older than the active one, and rollback
        # would then treat the newest build as history. Never go back.
        newest = max((release_order_key(i)[0] for i in known), default=stamp)
        stamp = max(stamp, newest)
        sequence = 1 + max(
            (release_order_key(i)[1] for i in known if i.startswith(f"{stamp}-")),
            default=0,
        )
        return stamp, short, sequence

    def _now_stamp(self) -> str:
        """
        Stamp the current time for a release id.

        Returns:
            ``YYYYMMDD-HHMMSS`` in UTC. A naive clock is taken to be UTC.
        """
        now = self._clock()
        if now.tzinfo is not None:
            now = now.astimezone(timezone.utc)
        return now.strftime(_STAMP_FORMAT)

    def _ids_on_disk(self) -> builtins.list[str]:
        """
        Return the ids of the release directories present.

        Returns:
            Ids, unordered.
        """
        if not self.releases_dir.is_dir():
            return []
        return [
            entry.name
            for entry in self.releases_dir.iterdir()
            if _RELEASE_ID.match(entry.name) and entry.is_dir() and not entry.is_symlink()
        ]

    def _known_ids(self) -> set[str]:
        """
        Return every id taken, on disk or handed out by this manager.

        Returns:
            Ids.
        """
        return set(self._ids_on_disk()) | self._issued

    def _active_id(self) -> str | None:
        """
        Read which release ``current`` points at.

        Returns:
            The id, or None when there is no usable link.
        """
        if not self.current_link.is_symlink():
            return None
        target = PurePosixPath(os.readlink(self.current_link))
        # WASM only ever writes releases/<id>. Anything else was put there by
        # hand, and guessing what it means is how a rollback lands somewhere
        # unexpected.
        if target.is_absolute() or len(target.parts) != 2 or target.parts[0] != RELEASES_DIR:
            return None
        release_id = target.parts[1]
        if release_id not in self._ids_on_disk():
            return None
        return release_id

    def _release_id(self, release: Path) -> str:
        """
        Check that a path is one of this application's release directories.

        Args:
            release: Path to check.

        Returns:
            Its release id.

        Raises:
            DeploymentError: The path is not directly inside ``releases/``, is
                not named like a release, or is a symlink.
        """
        release = Path(release)
        in_place = (
            _RELEASE_ID.match(release.name) is not None
            and release.parent.resolve() == self.releases_dir.resolve()
            and not release.is_symlink()
        )
        if not in_place:
            raise DeploymentError(
                f"{release} is not a release of {self.app_path}",
                details=f"Releases are directories in {self.releases_dir} named like "
                "20260925-143012-a1b2c3d, created by a deploy.",
            )
        return release.name

    def _existing_release_id(self, release: Path) -> str:
        """
        Check that a path is one of this application's releases and exists.

        A release this manager created in this process also counts: under
        ``--dry-run`` its directory was never made, and the rest of the
        rehearsal must still be able to refer to it.

        Args:
            release: Path to check.

        Returns:
            Its release id.

        Raises:
            DeploymentError: The path is not a release of this application,
                or it does not exist. Carrying on would create it: a link or a
                directory made inside a mistyped release would turn it into a
                "release" that holds nothing but links.
        """
        release_id = self._release_id(release)
        if not (self.releases_dir / release_id).is_dir() and release_id not in self._issued:
            raise DeploymentError(
                f"Release {release_id} does not exist",
                details=self._available_hint(),
            )
        return release_id

    def _release(self, release_id: str, *, active: bool) -> Release:
        """
        Describe a release from its id.

        Args:
            release_id: A valid release id.
            active: Whether it is the active one.

        Returns:
            The release.
        """
        match = _RELEASE_ID.match(release_id)
        if match is None:
            raise ValueError(f"not a release id: {release_id!r}")
        created = datetime.strptime(match["stamp"], _STAMP_FORMAT).replace(tzinfo=timezone.utc)
        commit = match["commit"]
        return Release(
            id=release_id,
            path=self.releases_dir / release_id,
            commit=None if commit == NO_COMMIT else commit,
            created_at=created.isoformat(),
            active=active,
        )

    def _available_hint(self, releases: Sequence[Release] | None = None) -> str:
        """
        Describe the releases there are, for an error's details.

        Args:
            releases: Releases already listed, to avoid reading the disk twice.

        Returns:
            A sentence naming them, newest first.
        """
        ids = [r.id for r in (self.list() if releases is None else releases)]
        if not ids:
            return f"There are no releases in {self.releases_dir}."
        return f"Releases present, newest first: {', '.join(ids)}."
