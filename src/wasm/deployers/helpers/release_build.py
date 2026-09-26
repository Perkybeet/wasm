# Copyright (c) 2024-2026 Yago Lopez Prado
# SPDX-License-Identifier: AGPL-3.0-or-later

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

import json
import os
from dataclasses import dataclass
from pathlib import Path

from wasm.core.exceptions import WASMError
from wasm.core.fs import FileSystem
from wasm.core.logger import Logger
from wasm.core.runner import CommandRunner
from wasm.deployers.helpers.health import failure_output
from wasm.deployers.helpers.package_manager import PackageManagerHelper
from wasm.deployers.releases import REPO_CACHE_DIR, Release, ReleaseManager
from wasm.managers.source_manager import SourceManager
from wasm.validators.source import validate_source

__all__ = [
    "DEPENDENCY_DIRS",
    "LOCKFILES",
    "REPO_CACHE_DIR",
    "RUNTIME_STAMP_FILE",
    "StagedRelease",
    "discard_release",
    "lockfiles_match",
    "reuse_dependencies",
    "stage_release",
    "stamp_installed_dependencies",
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

#: A version command reports in well under a second; this only guards a hang.
VERSION_TIMEOUT = 15

#: Written inside a dependency directory once it is installed, so a copy of it
#: carries its own runtime key wherever it is reused. Dot-prefixed so it is
#: never mistaken for a package and, for a static site's node_modules-adjacent
#: build output, never served: nothing serves dotfiles from a build directory.
RUNTIME_STAMP_FILE = ".wasm-runtime.json"


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
    commit: str | None = None,
) -> StagedRelease:
    """
    Create a new release directory and put the source in it.

    Args:
        source: Git URL, archive URL or local directory.
        branch: Git branch, for git sources.
        releases: Manager of the application's releases.
        source_manager: Manager the fetch goes through.
        logger: Where progress is reported.
        commit: For a git source, a full commit id already in the repository
            cache (see :meth:`SourceManager.resolve_commit`) to export instead
            of the head of the branch: a rebuild of that exact commit.

    Returns:
        The staged release.

    Raises:
        WASMError: If the source cannot be fetched or exported.
    """
    source_type, _ = validate_source(source)
    cache = releases.app_path / REPO_CACHE_DIR
    if source_type != "git":
        commit = None
    elif commit is None:
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


def _current_runtime_key(
    name: str, release: Path, *, runner: CommandRunner
) -> tuple[dict[str, str] | None, str | None]:
    """
    The runtime a dependency directory's install is bound to right now.

    ``node_modules`` holds native addons built against a specific Node ABI,
    and is installed by whichever package manager the release's lockfile
    names; a venv's interpreter decides the ``.so`` files under it. Both are
    machine-wide facts, not release-specific ones, so this asks the runner
    what runs right now rather than reading anything from a release tree
    (except which Node package manager applies, decided by the lockfile).

    Args:
        name: One of DEPENDENCY_DIRS.
        release: The release whose lockfile decides the Node package manager.
            Only its presence is read, never a version out of it.
        runner: Runner the version commands go through.

    Returns:
        The key naming every component that matters and its version, or None
        with why when a version command failed - either way the caller must
        not reuse.
    """
    if name == "node_modules":
        node = runner.run(["node", "--version"], timeout=VERSION_TIMEOUT)
        if not node.success:
            return None, "node --version failed"
        package_manager = PackageManagerHelper().detect(release)
        manager = runner.run([package_manager, "--version"], timeout=VERSION_TIMEOUT)
        if not manager.success:
            return None, f"{package_manager} --version failed"
        return {"node": node.output, package_manager: manager.output}, None

    # ".venv" and "venv": PythonDeployer.pre_install always creates a plain
    # venv with "python3 -m venv" - the only interpreter that ever builds one.
    python = runner.run(["python3", "--version"], timeout=VERSION_TIMEOUT)
    if not python.success:
        return None, "python3 --version failed"
    return {"python3": python.output}, None


def _read_runtime_stamp(directory: Path) -> dict[str, str] | None:
    """
    Read the runtime a dependency directory was installed under.

    A read, not a mutation, so it does not go through the fs seam.

    Args:
        directory: A dependency directory copied or kept from a release.

    Returns:
        The stamp, or None when there is none, it is not valid JSON, or it is
        not a flat mapping of strings. Every one of those reads exactly like a
        release built before this stamp existed: it must not be reused.
    """
    try:
        data = json.loads((directory / RUNTIME_STAMP_FILE).read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in data.items()
    ):
        return None
    return data


def _describe_runtime_change(old: dict[str, str] | None, new: dict[str, str]) -> str:
    """
    Explain why a dependency directory cannot be reused for its runtime.

    Args:
        old: The active release's stamp, or None when it has none.
        new: The runtime a fresh install would be built with now.

    Returns:
        "no runtime stamp" for a release built before this change, otherwise
        which component moved and between which versions.
    """
    if old is None:
        return "no runtime stamp"
    changed = [f"{k} {old.get(k, 'missing')} -> {v}" for k, v in new.items() if old.get(k) != v]
    return f"the runtime changed ({', '.join(changed)})" if changed else "the runtime changed"


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

    Only when the lockfiles are byte-identical and the active release's
    dependencies were built under the runtime that is current right now -
    read from the ``.wasm-runtime.json`` :func:`stamp_installed_dependencies`
    leaves inside each dependency directory it installs. A release built
    before that stamp existed, or one whose stamp names a different Node or
    Python, is never reused: reusing it would hand the new release native
    modules built for an ABI nothing on the machine still is.

    ``cp -a --reflink=auto`` shares the blocks on filesystems that can (btrfs,
    XFS) and copies them elsewhere; either way the new release owns its copy,
    so pruning the old one later cannot break it.

    Args:
        release: The release being built.
        active: The active release, or None when there is none.
        runner: Runner the version checks and the copies go through.
        fs: Filesystem a partial copy is removed through.
        logger: Where a failed copy, or a runtime mismatch, is reported.

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

        current_key, reason = _current_runtime_key(name, release, runner=runner)
        if current_key is None:
            logger.substep(f"Dependencies reinstalled: {reason}")
            return None
        stamp = _read_runtime_stamp(source)
        if stamp != current_key:
            logger.substep(
                f"Dependencies reinstalled: {_describe_runtime_change(stamp, current_key)}"
            )
            return None

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


def stamp_installed_dependencies(
    release: Path, *, runner: CommandRunner, fs: FileSystem, logger: Logger
) -> None:
    """
    Record the runtime a fresh install was just built with.

    Written inside each dependency directory the install actually produced,
    so a later deploy that copies it with ``cp -a`` carries the stamp along
    and :func:`reuse_dependencies` can tell whether reusing it is still safe.
    A no-op under ``--dry-run``: ``fs`` is a :class:`DryRunFileSystem` there,
    and nothing was really installed for this to describe.

    Args:
        release: The release whose dependencies were just installed.
        runner: Runner the version commands go through.
        fs: Filesystem the stamp is written through.
        logger: Where a version command that failed is noted; never fatal,
            since a deploy must not fail over a stamp that only affects the
            next one.
    """
    for name in DEPENDENCY_DIRS:
        directory = release / name
        if directory.is_symlink() or not directory.is_dir():
            continue
        key, reason = _current_runtime_key(name, release, runner=runner)
        if key is None:
            logger.debug(f"No runtime stamp written for {name}: {reason}")
            continue
        fs.write_text(directory / RUNTIME_STAMP_FILE, json.dumps(key, sort_keys=True))
