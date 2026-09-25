# Copyright (c) 2024-2026 Yago Lopez Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
Filling a new release: its source, and the dependencies it can take over.

:class:`~wasm.deployers.releases.ReleaseManager` knows the layout; this module
knows what goes into a release before it is built. Both the deploy pipeline
and :class:`~wasm.deployers.auto.AutoDeployer`, which has to look at the source
before it knows which deployer builds it, stage a release through here, so
there is one way a release gets its code.

Git sources go through a repository cache, ``repo/`` in the application
directory: an ordinary clone that is fetched and reset on every deploy, and
from which the target commit is exported without ``.git``. Local directories
and archives are copied or extracted straight into the release.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from wasm.core.exceptions import WASMError
from wasm.core.fs import FileSystem
from wasm.core.logger import Logger
from wasm.core.runner import CommandRunner
from wasm.deployers.helpers.health import failure_output
from wasm.deployers.releases import REPO_CACHE_DIR, Release, ReleaseManager
from wasm.managers.source_manager import SourceManager
from wasm.validators.source import validate_source

__all__ = [
    "DEPENDENCY_DIRS",
    "LOCKFILES",
    "REPO_CACHE_DIR",
    "StagedRelease",
    "discard_release",
    "lockfiles_match",
    "reuse_dependencies",
    "stage_release",
]

#: Files whose bytes decide the installed dependencies. When every one of them
#: is identical to the active release's, the install would produce what the
#: active release already has.
LOCKFILES: tuple[str, ...] = (
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "bun.lockb",
    "requirements.txt",
    "poetry.lock",
    "uv.lock",
    "Pipfile.lock",
)

#: What an install produces, and what is copied instead of reinstalled.
DEPENDENCY_DIRS: tuple[str, ...] = ("node_modules", ".venv", "venv")

#: Copying a large node_modules is quick with reflinks and slow without them.
COPY_TIMEOUT = 900


@dataclass(frozen=True)
class StagedRelease:
    """
    A release directory that holds its source and is ready to be built.

    Attributes:
        path: The release directory.
        commit: Full commit id it was exported from, or None when the source
            is not a git repository.
        manager: The manager that created it. Kept with the release because
            under ``--dry-run`` the directory is never made, and only the
            manager that issued the id can still refer to it.
    """

    path: Path
    commit: str | None
    manager: ReleaseManager

    @property
    def id(self) -> str:
        """The release id, which is the directory name."""
        return self.path.name

    @property
    def short_commit(self) -> str | None:
        """The commit as ``git log --oneline`` shows it, or None."""
        return self.commit[:7] if self.commit else None


def stage_release(
    source: str,
    branch: str | None,
    *,
    releases: ReleaseManager,
    source_manager: SourceManager,
    logger: Logger,
) -> StagedRelease:
    """
    Create a new release directory and put the source in it.

    Args:
        source: Git URL, archive URL or local directory.
        branch: Git branch, for git sources.
        releases: Manager of the application's releases.
        source_manager: Manager the fetch goes through.
        logger: Where progress is reported.

    Returns:
        The staged release.

    Raises:
        WASMError: If the source cannot be fetched or exported.
    """
    source_type, _ = validate_source(source)
    cache = releases.app_path / REPO_CACHE_DIR
    commit: str | None = None
    if source_type == "git":
        logger.substep(f"Repository cache: {cache}")
        commit = source_manager.sync_cache(source, cache, branch)

    release = releases.new_release_dir(commit)
    try:
        if source_type != "git":
            # clean=False: the directory was just claimed, and removing it to
            # fetch into it again would hand the name back to a concurrent
            # deploy.
            source_manager.fetch(source, release, branch=branch, clean=False)
        elif commit is not None:
            source_manager.export_commit(cache, commit, release)
    except (WASMError, OSError):
        # A half-filled directory named like a release is a release to
        # everything that lists them, rollback included.
        discard_release(release, releases=releases, logger=logger)
        raise

    logger.substep(f"Release: {release.name}")
    return StagedRelease(path=release, commit=commit, manager=releases)


def discard_release(release: Path, *, releases: ReleaseManager, logger: Logger) -> None:
    """
    Delete a release that never became active.

    Args:
        release: The release directory.
        releases: Manager of the application's releases.
        logger: Where a failure to delete is reported.
    """
    active = releases.current()
    if active is not None and active.path.name == release.name:
        return
    if not release.is_dir():
        return
    try:
        releases.fs.remove_tree(release)
    except OSError as exc:
        logger.warning(f"Could not remove the unfinished release {release}: {exc}")


def lockfiles_match(active: Path, release: Path) -> bool:
    """
    Tell whether two trees would install the same dependencies.

    Args:
        active: The active release.
        release: The release being built.

    Returns:
        True when at least one lockfile exists and every lockfile is present
        in both trees or in neither, with identical bytes. A lockfile that is
        a symlink never matches: its content is not the release's own.
    """
    found = False
    for name in LOCKFILES:
        old, new = active / name, release / name
        if old.is_symlink() or new.is_symlink():
            return False
        if old.is_file() != new.is_file():
            return False
        if not old.is_file():
            continue
        found = True
        if old.read_bytes() != new.read_bytes():
            return False
    return found


def reuse_dependencies(
    release: Path,
    active: Release | None,
    *,
    runner: CommandRunner,
    fs: FileSystem,
    logger: Logger,
) -> str | None:
    """
    Copy the installed dependencies of the active release into a new one.

    Only when the lockfiles are byte-identical, and only what the active
    release really has. ``cp -a --reflink=auto`` shares the blocks on
    filesystems that can (btrfs, XFS) and copies them elsewhere; either way
    the new release owns its copy, so pruning the old one later cannot break
    it.

    Args:
        release: The release being built.
        active: The active release, or None when there is none.
        runner: Runner the copies go through.
        fs: Filesystem a partial copy is removed through.
        logger: Where a failed copy is reported.

    Returns:
        The id of the release the dependencies came from, or None when
        nothing was reused and the install must run.
    """
    if active is None or not lockfiles_match(active.path, release):
        return None

    copied: list[str] = []
    for name in DEPENDENCY_DIRS:
        source, target = active.path / name, release / name
        if source.is_symlink() or not source.is_dir() or os.path.lexists(target):
            continue
        result = runner.run(
            ["cp", "-a", "--reflink=auto", str(source), str(target)], timeout=COPY_TIMEOUT
        )
        if not result.success:
            logger.warning(
                f"Could not reuse {name} from release {active.id}; installing instead: "
                f"{failure_output(result)}"
            )
            if os.path.lexists(target):
                fs.remove_tree(target)
            return None
        copied.append(name)

    return active.id if copied else None
