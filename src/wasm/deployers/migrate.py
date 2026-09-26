# Copyright (c) 2024-2026 Yago Lopez Prado
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Moving an in-place application onto the release layout, without losing a byte.

An application deployed by 1.x runs from the tree every update rebuilds. The
release layout needs that tree to become the first release, its ``.env`` and
everything it wrote for itself (uploads, storage) to move to ``shared/``, and
its unit and site to run from ``current``. Nothing here ever happens by
itself: :func:`plan_migration` says what would be done, and only
:func:`migrate`, which ``wasm app migrate`` and ``POST /api/apps/{d}/migrate``
call when the operator asks, does it.

The rules, and why:

- **Nothing is deleted.** Every change is a rename inside the application
  directory, or the creation of a directory or a link. The regular files and
  their bytes are counted before and after, ``shared/`` included, and a
  difference fails the migration.
- **What the application wrote for itself is found, not guessed.** In a git
  checkout that is every untracked directory, ignored or not, minus build
  output (``git status --ignored``: ``git ls-files --others
  --exclude-standard`` hides exactly the directories that matter, because an
  uploads directory is almost always in ``.gitignore``). Without git the
  operator names them (``--persist``), or a conservative list of the usual
  upload directories that exist is used.
- **Nothing moves under a running process.** The unit is stopped before the
  first rename and started again on the release layout, or on the tree it had
  after an undo. A Next.js server left running writes into ``.next`` while it
  moves, and a reversal then finds its source taken and cannot put it back.
- **A migration that does not come up is undone exactly.** The unit and the
  site are rewritten, the application restarted and put through the same
  health gate as a deploy. If it does not answer, every rename is reversed,
  the unit and site are put back byte for byte and the application restarted
  on the tree it had. Every reversal is attempted even when an earlier one
  fails, and the error names each thing that could not be put back and where
  it is now.
- **A SQLite database is data, not code.** One found in the tree moves to
  ``shared/`` with the ``-wal`` and ``-shm`` files beside it, which hold
  committed transactions until the next checkpoint. Left in the first
  release, the next deploy would start from an empty database.
- **``--dry-run`` changes nothing.** The plan only reads, and a rehearsed
  migration announces its renames through the filesystem seam and stops.
"""

from __future__ import annotations

import os
import re
import secrets
import sqlite3
import stat
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from wasm.core.applock import app_lock
from wasm.core.config import Config
from wasm.core.exceptions import DeploymentError, ValidationError, WASMError
from wasm.core.fs import FileSystem, get_fs, is_rehearsal
from wasm.core.logger import Logger
from wasm.core.runner import CommandRunner, get_runner
from wasm.core.store import (
    App,
    DeploymentTrigger,
    ReleaseRecord,
    ReleaseStatus,
    Service,
    WASMStore,
    get_store,
)
from wasm.deployers.helpers.layout import RELEASES, app_root
from wasm.deployers.helpers.permissions import hand_over_file
from wasm.deployers.lifecycle import health_gate_for
from wasm.deployers.recorder import CapturingLogger, DeploymentRecorder, recording
from wasm.deployers.registry import get_deployer
from wasm.deployers.releases import (
    CURRENT_LINK,
    ENV_FILE,
    RELEASES_DIR,
    SHARED_DIR,
    ReleaseManager,
    first_obstacle,
    persistent_path,
)
from wasm.managers.apache_manager import ApacheManager
from wasm.managers.nginx_manager import NginxManager
from wasm.managers.service_manager import ServiceManager
from wasm.validators.domain import validate_domain

#: What a build or an install produces. Never persistent: shared across
#: releases, a build output would be written by every build into the one
#: directory the serving release reads.
BUILD_OUTPUTS = frozenset(
    {
        ".cache",
        ".git",
        ".next",
        ".nuxt",
        ".output",
        ".parcel-cache",
        ".pytest_cache",
        ".svelte-kit",
        ".turbo",
        ".venv",
        "__pycache__",
        "build",
        "coverage",
        "dist",
        "node_modules",
        "out",
        "venv",
    }
)

#: Where applications usually keep what users upload. Used only for a tree
#: that is not a git checkout and only when the operator named nothing, and
#: only those of them that exist.
COMMON_PERSISTENT: tuple[str, ...] = ("uploads", "public/uploads", "storage", "data")

#: WASM's own inventory of the variables, written next to the ``.env``.
ENV_INVENTORY = ".wasm"

#: Names a SQLite database is given. Only files named like this are opened to
#: read their header, so planning does not read a large tree file by file.
SQLITE_SUFFIXES = (".sqlite", ".sqlite3", ".db", ".db3", ".s3db", ".sl3")

#: The first 16 bytes of every SQLite 3 database.
_SQLITE_HEADER = b"SQLite format 3\x00"

#: What SQLite keeps beside a database: the write-ahead log holds committed
#: transactions until they are checkpointed into the database, the rollback
#: journal an interrupted one. Always next to the file SQLite resolved, so
#: next to the real database in shared/, never next to a release's link.
SQLITE_COMPANIONS = ("-journal", "-shm", "-wal")

#: How the persistent paths of a plan were chosen.
FROM_GIT = "git"
FROM_OPERATOR = "explicit"
FROM_COMMON_NAMES = "common"

#: Console scripts of a virtual environment the in-place Python unit runs.
#: A script's first line names the interpreter by the absolute path it was
#: installed under, which the migration moves; ``python -m`` through
#: ``current`` does not, and is how the release layout runs them.
_VENV_SCRIPTS = ("gunicorn", "uvicorn")

#: Mode of the directories the migration creates. Readable by the web server,
#: which serves static builds straight out of the release.
_DIR_MODE = 0o755

#: Deadline for the git commands that read the tree.
_GIT_TIMEOUT = 60


@dataclass(frozen=True)
class TreeCount:
    """
    What a directory holds, to prove a migration kept all of it.

    Attributes:
        files: Regular files.
        bytes: Their total size.
        links: Symbolic links.
    """

    files: int
    bytes: int
    links: int


@dataclass(frozen=True)
class MigrationPlan:
    """
    What migrating an application would do.

    Attributes:
        domain: The application's domain.
        app_path: Its directory.
        release_id: The name the first release would get. A forecast: the
            real name is claimed when the migration runs.
        commit: The commit the tree is checked out at, when it is a checkout.
        persistent: Paths that move to ``shared/`` and are linked into every
            release from now on.
        persistent_source: How they were chosen: ``git``, ``explicit`` or
            ``common``.
        env_files: The ``.env`` and WASM's inventory of it, which move to
            ``shared/``.
        unit: The unit that runs the application, or None for a site.
        unit_rewrite: Whether the unit names the application directory and is
            rewritten to name ``current``.
        site_rewrite: Whether the site configuration names it, likewise.
        untracked_files: Files git does not track that are not in a
            persistent path. They stay in the first release only: the next
            deploy will not have them. Name one with ``--persist`` to keep it.
        warnings: Anything the operator should know before going ahead,
            the downtime included.
        count: What the directory holds now.
        companions: The ``-wal``, ``-shm`` and ``-journal`` files of the
            SQLite databases in ``persistent``, which move to ``shared/``
            with them and are not linked: SQLite finds them beside the real
            file.
    """

    domain: str
    app_path: str
    release_id: str
    commit: str | None
    persistent: tuple[str, ...]
    persistent_source: str
    env_files: tuple[str, ...]
    unit: str | None
    unit_rewrite: bool
    site_rewrite: bool
    untracked_files: tuple[str, ...]
    warnings: tuple[str, ...]
    count: TreeCount
    companions: tuple[str, ...] = ()


@dataclass(frozen=True)
class MigrationResult:
    """
    What :func:`migrate` did.

    Attributes:
        domain: The application's domain.
        release_id: The first release; empty for a rehearsal.
        persistent: What moved to ``shared/`` and is linked into every release.
        env_files: The environment files that moved to ``shared/``.
        before: What the directory held before.
        after: What it holds now, ``shared/`` included. Files and bytes are
            the same; the links are more by the ones the layout needs.
        unit_rewritten: Whether the unit was rewritten.
        site_rewritten: Whether the site configuration was rewritten.
        rehearsed: True under ``--dry-run``: nothing changed.
        deployment_id: The history row that records it, when one was written.
    """

    domain: str
    release_id: str
    persistent: tuple[str, ...]
    env_files: tuple[str, ...]
    before: TreeCount
    after: TreeCount
    unit_rewritten: bool
    site_rewritten: bool
    rehearsed: bool
    deployment_id: int | None


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------


def plan_migration(
    domain: str,
    persist: Sequence[str] | None = None,
    *,
    runner: CommandRunner | None = None,
) -> MigrationPlan:
    """
    Work out what migrating an in-place application would do. Changes nothing.

    Args:
        domain: The application's domain.
        persist: Paths to keep in ``shared/``, relative to the application.
            Given, they replace what would be detected.
        runner: Runner for the read-only git commands.

    Returns:
        The plan.

    Raises:
        WASMError: The application is unknown.
        DeploymentError: It is already on releases, its type cannot build
            releases, or its directory is missing.
        ValidationError: A path in ``persist`` is not a path inside it.
        DeploymentError: The paths named in ``persist`` leave a SQLite
            database out.
    """
    app = _inplace_app(validate_domain(domain))
    root = app_root(app)
    run = runner or get_runner()
    warnings: list[str] = []

    commit = _head_commit(root, run)
    untracked_dirs, untracked_files = _untracked(root, run) if commit else ([], [])

    if persist is not None:
        persistent = _explicit(persist)
        source = FROM_OPERATOR
    elif commit:
        persistent = _detected(untracked_dirs, untracked_files)
        source = FROM_GIT
    else:
        persistent = [p for p in COMMON_PERSISTENT if _is_real_dir(root / p)]
        source = FROM_COMMON_NAMES
        warnings.append(
            f"{root} is not a git checkout, so what the application wrote for itself cannot be "
            "told apart from its code. "
            + (
                f"Keeping the usual upload directories that exist: {', '.join(persistent)}. "
                if persistent
                else "None of the usual upload directories exist. "
            )
            + "Name every other one with --persist."
        )

    persistent, companions = _with_databases(
        root, persistent, source, commit, untracked_dirs, untracked_files, warnings
    )

    for path in persistent:
        blocked = first_obstacle(root, PurePosixPath(path))
        if blocked is not None or (root / path).is_symlink():
            raise DeploymentError(
                f"Cannot keep {path} in shared/: {blocked or root / path} is a symlink or a file",
                details="Only real directories and files move to shared/. Resolve the link "
                "or leave the path out with --persist.",
            )

    env_files = tuple(
        name
        for name in (ENV_FILE, ENV_INVENTORY)
        if os.path.lexists(root / name) and not (root / name).is_symlink()
    )
    if (root / ENV_FILE).is_symlink():
        warnings.append(
            f"{root / ENV_FILE} is a symlink; it stays where it is and is not moved to shared/."
        )

    kept = {PurePosixPath(p) for p in persistent}
    left = tuple(
        f
        for f in untracked_files
        if f not in env_files
        and f not in companions
        and not any(PurePosixPath(f).is_relative_to(k) for k in kept)
    )
    if left:
        warnings.append(
            f"{len(left)} untracked file(s) stay in the first release only and will not be in "
            "the next one: " + ", ".join(left[:10]) + (" ..." if len(left) > 10 else "")
        )

    services = ServiceManager()
    # app_units is the one mapping from an application to its unit, legacy
    # prefix included; a static application has none.
    unit = next(iter(services.app_units(app)), None)
    unit_text = services.get_service_config(unit) if unit else None
    site_text = _webserver_for(app).get_site_config(app.domain)
    warnings.append(_downtime(unit))

    return MigrationPlan(
        domain=app.domain,
        app_path=str(root),
        release_id=ReleaseManager(root).next_release_id(commit),
        commit=commit,
        persistent=tuple(persistent),
        persistent_source=source,
        env_files=env_files,
        unit=unit,
        unit_rewrite=unit_text is not None and _names(unit_text, root),
        site_rewrite=site_text is not None and _names(site_text, root),
        untracked_files=left,
        warnings=tuple(warnings),
        count=count_tree(root),
        companions=companions,
    )


def _downtime(unit: str | None) -> str:
    """
    Say how long the application is unavailable while it migrates.

    Args:
        unit: The unit that runs it, or None for a site the web server serves.

    Returns:
        The sentence for the plan.
    """
    if unit is None:
        return (
            "The site answers with errors while its files move and the site is reloaded to "
            "serve current: usually under a second."
        )
    return (
        f"{unit} is stopped before anything moves and started again on the release layout, "
        "where it must pass its health check: the application is down from the stop until it "
        "answers, usually a few seconds and at most about half a minute. If it does not answer, "
        "the tree is put back and it is started again as it was."
    )


def _with_databases(
    root: Path,
    persistent: Sequence[str],
    source: str,
    commit: str | None,
    untracked_dirs: Sequence[str],
    untracked_files: Sequence[str],
    warnings: list[str],
) -> tuple[list[str], tuple[str, ...]]:
    """
    Keep every SQLite database of the tree in ``shared/``, with its WAL.

    A database left in the first release is not in the next one: the next
    deploy would start from an empty database, and the data would go with
    the first release when it is pruned.

    Args:
        root: The application directory.
        persistent: The paths chosen so far.
        source: How they were chosen.
        commit: The commit the tree is at, when it is a git checkout.
        untracked_dirs: What git does not track, directories.
        untracked_files: What git does not track, files.
        warnings: Where what the operator should know is added.

    Returns:
        The persistent paths, databases added, and the companions of the
        databases that are persistent paths of their own.

    Raises:
        DeploymentError: The operator named the persistent paths and left a
            database out; it is not guessed at.
    """
    databases = _sqlite_databases(root)
    chosen = [PurePosixPath(p) for p in persistent]
    uncovered = [
        db for db in databases if not any(PurePosixPath(db).is_relative_to(k) for k in chosen)
    ]
    if uncovered and source == FROM_OPERATOR:
        raise DeploymentError(
            f"SQLite database(s) outside the paths named with --persist: {', '.join(uncovered)}",
            details="A database left in the first release is not in the next one: the next "
            "deploy would start from an empty database. Name each one, or the directory that "
            "holds it, too: " + " ".join(_persist_hint(db) for db in uncovered),
        )
    if uncovered:
        warnings.append(
            f"SQLite database(s) move to shared/ with their -wal and -shm files, and every release "
            f"links them: {', '.join(uncovered)}."
        )
    result = [*persistent, *(db for db in uncovered if db not in persistent)]

    if commit:
        tracked = [
            db
            for db in databases
            if db not in untracked_files
            and not any(PurePosixPath(db).is_relative_to(d) for d in untracked_dirs)
        ]
        for db in tracked:
            warnings.append(
                f"{db} is a SQLite database tracked by git: it moves to shared/, but a deploy "
                "uses the repository's copy instead of the shared one for as long as the "
                f"repository tracks it. Untrack it (git rm --cached {db}) before the next deploy."
            )

    companions = tuple(
        sorted(
            f"{db}{suffix}"
            for db in databases
            if db in result
            for suffix in SQLITE_COMPANIONS
            if os.path.lexists(root / f"{db}{suffix}") and not (root / f"{db}{suffix}").is_symlink()
        )
    )
    return result, companions


def _persist_hint(database: str) -> str:
    """
    Suggest the ``--persist`` that keeps a database.

    Args:
        database: Its path, relative to the application.

    Returns:
        Its directory, or the file itself at the top of the tree.
    """
    parent = PurePosixPath(database).parent
    return f"--persist {database}" if parent == PurePosixPath(".") else f"--persist {parent}"


def _sqlite_databases(root: Path) -> list[str]:
    """
    Find the SQLite databases in an application tree.

    Build output and installed dependencies are not looked into: a database
    there is a fixture or a cache the next build makes again.

    Args:
        root: The application directory.

    Returns:
        Their paths, relative to the root.
    """
    found: list[str] = []
    for directory, subdirectories, names in os.walk(root, followlinks=False):
        subdirectories[:] = sorted(d for d in subdirectories if d not in BUILD_OUTPUTS)
        for name in names:
            if not name.lower().endswith(SQLITE_SUFFIXES):
                continue
            path = Path(directory) / name
            if path.is_symlink() or not path.is_file():
                continue
            try:
                with path.open("rb") as handle:
                    header = handle.read(len(_SQLITE_HEADER))
            except OSError:
                continue
            if header == _SQLITE_HEADER:
                found.append(path.relative_to(root).as_posix())
    return sorted(found)


def _inplace_app(domain: str) -> App:
    """
    Read the row of an application that can be migrated.

    Args:
        domain: A validated domain.

    Returns:
        The row.

    Raises:
        WASMError: The application is unknown.
        DeploymentError: It is on releases already, its type cannot build
            releases, or its directory is missing.
    """
    app = get_store().get_app(domain)
    if app is None:
        raise WASMError(
            f"Application not found: {domain}", details="Run 'wasm list' to see what is deployed."
        )
    if app.layout == RELEASES:
        raise DeploymentError(
            f"{domain} is on the release layout already",
            details=f"There is nothing to migrate. See its releases with: wasm releases list "
            f"{domain}",
        )
    try:
        deployer = get_deployer(app.app_type) if app.app_type else None
    except ValueError:
        deployer = None
    if deployer is None or not getattr(deployer, "SUPPORTS_RELEASES", False):
        raise DeploymentError(
            f"{app.app_type or 'unknown'} applications cannot use the release layout yet",
            details=f"{domain} keeps updating in place, which is what it has always done.",
        )
    root = app_root(app)
    if not root.is_dir() or root.is_symlink():
        raise DeploymentError(
            f"{root} is not a directory", details="Redeploy the application before migrating it."
        )
    return app


def _git(root: Path, *args: str) -> list[str]:
    """
    Build a git command that reads the application's checkout.

    The tree belongs to the service account and WASM runs as root, which git
    refuses as "dubious ownership" unless the directory is declared safe; on
    the command line, so no global configuration is changed to read it.

    Args:
        root: The checkout.
        *args: The git subcommand and its arguments.

    Returns:
        The argv.
    """
    return ["git", "-c", f"safe.directory={root}", "--no-optional-locks", *args]


def _head_commit(root: Path, runner: CommandRunner) -> str | None:
    """
    Read the commit a checkout is at.

    Args:
        root: The application directory.
        runner: Runner for git.

    Returns:
        The full commit id, or None when it is not a git checkout.
    """
    if not (root / ".git").exists():
        return None
    result = runner.run(_git(root, "rev-parse", "HEAD"), cwd=root, timeout=_GIT_TIMEOUT)
    commit = result.stdout.strip()
    return commit if result.success and re.fullmatch(r"[0-9a-f]{7,64}", commit) else None


def _untracked(root: Path, runner: CommandRunner) -> tuple[list[str], list[str]]:
    """
    List what git does not track in a checkout, ignored or not.

    Args:
        root: The checkout.
        runner: Runner for git.

    Returns:
        Untracked directories and untracked files, relative to the root.
        A directory git lists whole is one entry; nothing inside it is.

    Raises:
        DeploymentError: git could not read the checkout. Guessing would
            leave the uploads out of shared/.
    """
    result = runner.run(
        _git(
            root,
            "status",
            "--porcelain=v1",
            "-z",
            "--ignored=traditional",
            "--untracked-files=normal",
        ),
        cwd=root,
        timeout=_GIT_TIMEOUT,
    )
    if not result.success:
        raise DeploymentError(
            f"git could not list what is untracked in {root}",
            details=(result.stderr or result.stdout).strip()
            + "\nName the paths to keep with --persist instead.",
        )
    directories: list[str] = []
    files: list[str] = []
    for entry in result.stdout.split("\0"):
        if len(entry) < 4 or entry[:2] not in ("??", "!!"):
            continue
        path = entry[3:]
        if path.endswith("/"):
            directories.append(path.rstrip("/"))
        else:
            files.append(path)
    return directories, files


def _detected(directories: Sequence[str], files: Sequence[str]) -> list[str]:
    """
    Choose what is persistent from what git does not track.

    Args:
        directories: Untracked directories.
        files: Untracked files.

    Returns:
        Every untracked directory that is not build output nor the
        environment, plus the untracked ``.env.*`` files at the top level:
        ``.env.local`` or ``.env.production`` are read at build time, and a
        release exported from git would not have them.
    """
    chosen: list[str] = []
    for directory in directories:
        parts = PurePosixPath(directory).parts
        if not parts or any(part in BUILD_OUTPUTS for part in parts):
            continue
        if parts[0] == ENV_INVENTORY:
            continue
        chosen.append(directory)
    for file in files:
        if "/" not in file and file.startswith(f"{ENV_FILE}.") and file not in chosen:
            chosen.append(file)
    return sorted(chosen)


def _explicit(paths: Sequence[str]) -> list[str]:
    """
    Validate the paths the operator named.

    Args:
        paths: Paths relative to the application.

    Returns:
        The normalized paths, without duplicates.

    Raises:
        ValidationError: A path is not inside the application, is the
            ``.env`` (which always moves), or one path is inside another.
    """
    normalized: list[PurePosixPath] = []
    for raw in paths:
        try:
            path = persistent_path(raw)
        except DeploymentError as exc:
            raise ValidationError(exc.message, details=exc.details) from exc
        if path.parts[0] in (ENV_FILE, ENV_INVENTORY):
            raise ValidationError(
                f"{raw} does not need --persist",
                details="The .env always moves to shared/, and every release links it.",
            )
        if path not in normalized:
            normalized.append(path)
    for path in normalized:
        for other in normalized:
            if path != other and path.is_relative_to(other):
                raise ValidationError(
                    f"{path} is inside {other}",
                    details="Name only the outer one; everything under it moves with it.",
                )
    return [str(path) for path in normalized]


def _is_real_dir(path: Path) -> bool:
    """
    Tell whether a path is a directory and not a link to one.

    Args:
        path: The path.

    Returns:
        True for a real directory.
    """
    return path.is_dir() and not path.is_symlink()


def _webserver_for(app: App) -> NginxManager | ApacheManager:
    """
    Return the manager of the web server that serves an application.

    Args:
        app: The application.

    Returns:
        Its manager.
    """
    return ApacheManager() if app.webserver == "apache" else NginxManager()


def _path_pattern(root: Path) -> re.Pattern[str]:
    """
    Match the application directory as a whole path, not as a prefix.

    ``/var/www/apps/shop`` must not match inside ``/var/www/apps/shop-2``.
    A virtual environment's console script started directly is matched
    whole, with the groups ``venv`` and ``script``.

    Args:
        root: The application directory.

    Returns:
        The pattern.
    """
    scripts = "|".join(_VENV_SCRIPTS)
    return re.compile(
        re.escape(str(root))
        + rf"(?:/(?P<venv>\.?venv)/bin/(?P<script>{scripts})(?=\s|$)|(?=$|[/\s;\"']))"
    )


def _names(text: str, root: Path) -> bool:
    """
    Tell whether a unit or a site names the application directory.

    Args:
        text: The file.
        root: The application directory.

    Returns:
        True when it does.
    """
    return _path_pattern(root).search(text) is not None


def relocate(text: str, root: Path) -> str:
    """
    Point a unit or a site at ``current`` instead of the application directory.

    Every path under the application now lives under ``current``. The one
    exception is a virtual environment's console script started directly: it
    is started as ``python -m`` instead, because its first line names the
    interpreter by the path the migration just moved. One pass, so nothing
    it writes is rewritten again.

    Args:
        text: The file.
        root: The application directory.

    Returns:
        The rewritten file.
    """
    current = root / CURRENT_LINK

    def replacement(match: re.Match[str]) -> str:
        if match.group("venv"):
            return f"{current}/{match.group('venv')}/bin/python -m {match.group('script')}"
        return str(current)

    return _path_pattern(root).sub(replacement, text)


def count_tree(root: Path) -> TreeCount:
    """
    Count what a directory holds, without following links.

    Args:
        root: The directory.

    Returns:
        Its regular files, their bytes, and its links.
    """
    files = size = links = 0
    for directory, subdirectories, names in os.walk(root, followlinks=False):
        for name in names:
            info = os.lstat(os.path.join(directory, name))
            if stat.S_ISLNK(info.st_mode):
                links += 1
            elif stat.S_ISREG(info.st_mode):
                files += 1
                size += info.st_size
        # os.walk lists a link to a directory with the directories and does
        # not descend into it.
        links += sum(1 for name in subdirectories if os.path.islink(os.path.join(directory, name)))
    return TreeCount(files=files, bytes=size, links=links)


# ---------------------------------------------------------------------------
# Migrating
# ---------------------------------------------------------------------------


class _Journal:
    """
    Every change a migration made, in order, with what reverses it.

    Undoing runs the reversals newest first and carries on past one that
    fails, so as much as possible is put back; what could not be is reported
    by path, never silently left behind.
    """

    def __init__(self, fs: FileSystem, logger: Logger) -> None:
        """
        Args:
            fs: The filesystem the reversals go through.
            logger: Where each failed reversal is reported.
        """
        self._fs = fs
        self._logger = logger
        self._entries: list[tuple[str, Callable[[], None]]] = []
        self.failures: list[str] = []

    def record(self, description: str, undo: Callable[[], None]) -> None:
        """
        Remember a change.

        Args:
            description: What was done, for a failed reversal's report.
            undo: What reverses it.
        """
        self._entries.append((description, undo))

    def moved(self, source: Path, destination: Path) -> None:
        """
        Move something and remember where it came from.

        Args:
            source: Where it is.
            destination: Where it goes. Must not exist.

        Raises:
            DeploymentError: The destination exists; moving onto it would
                put one inside the other.
        """
        if os.path.lexists(destination):
            raise DeploymentError(
                f"Cannot move {source} to {destination}: something is already there",
                details="Nothing was overwritten. Move it out of the way and retry.",
            )
        self._fs.move(source, destination)

        def undo() -> None:
            if os.path.lexists(source):
                raise OSError(f"something was created at {source} in the meantime")
            self._fs.move(destination, source)

        # Worded as where it is if it cannot be put back: that is what the
        # operator needs from the error.
        self.record(f"{source} is still at {destination}", undo)

    def made_dir(self, path: Path) -> None:
        """
        Create a directory, and remember to remove it only if it is empty then.

        Args:
            path: The directory, whose parent exists.
        """
        self._fs.make_dir(path, mode=_DIR_MODE, parents=False, exist_ok=False)
        self.created_dir(path)

    def created_dir(self, path: Path) -> None:
        """
        Remember a directory something else created.

        Args:
            path: The directory.
        """
        self.record(f"{path} was created by the migration", lambda: self.remove_empty(path))

    def created_link(self, path: Path) -> None:
        """
        Remember a link something else created.

        Args:
            path: The link.
        """

        def undo() -> None:
            if path.is_symlink():
                self._fs.remove(path)

        self.record(f"{path} is a link the migration made", undo)

    def undo(self) -> None:
        """
        Reverse every change, newest first, whatever each reversal raises.

        One reversal that fails must not leave the ones after it undone: a
        renamed build directory is no less worth putting back because the
        unit before it could not be rewritten. Each failure is logged and
        kept in :attr:`failures`, for the error the migration raises.
        """
        while self._entries:
            description, undo = self._entries.pop()
            try:
                undo()
            # The undo is an error boundary: it runs after something already
            # failed, and every reversal after this one must still be tried.
            # What went wrong is logged and reported in the migration's error.
            except Exception as exc:
                self.failures.append(f"{description}: {exc}")
                self._logger.error(f"Could not undo ({description}): {exc}")

    def report(self) -> str:
        """
        Say what the undo could not put back, or that it put everything back.

        Returns:
            A paragraph for an error's details.
        """
        if not self.failures:
            return "Everything was put back as it was."
        return "\n".join(["Not put back:", *(f"  - {failure}" for failure in self.failures)])

    def remove_empty(self, path: Path) -> None:
        """
        Remove a directory the migration created, only if nothing is in it.

        Args:
            path: The directory.

        Raises:
            OSError: It is not empty. Whatever is in it is left alone.
        """
        if not path.is_dir() or path.is_symlink():
            return
        if any(path.iterdir()):
            raise OSError(f"{path} is not empty; it was left in place")
        # Checked empty just above: this removes one empty directory, and
        # the seam has no narrower call for it.
        self._fs.remove_tree(path)


def migrate(
    domain: str,
    plan: MigrationPlan,
    *,
    trigger: str = DeploymentTrigger.CLI.value,
    logger: Logger | None = None,
) -> MigrationResult:
    """
    Move an in-place application onto the release layout, as planned.

    Args:
        domain: The application's domain.
        plan: What :func:`plan_migration` said would be done.
        trigger: Who asked, recorded in the deployment history.
        logger: Where progress is reported; captured into the history row's
            log when it is a :class:`CapturingLogger`.

    Returns:
        What was done.

    Raises:
        WASMError: The application is unknown.
        DeploymentError: The plan is not for this application as it is now,
            a step failed, or the application did not pass the health gate
            on the new layout. In each case everything was put back, and the
            details say what, if anything, could not be, and where it is.
        ServiceError: The unit could not be stopped; nothing was moved.
        AppBusyError: Another operation is running on the application.
    """
    log = logger if logger is not None else CapturingLogger()
    domain = validate_domain(domain)
    # Nothing else may run on the application while its tree moves: an
    # update building in it, a restore replacing it, a second migration.
    with app_lock(domain, "migration"):
        return _migrate(domain, plan, trigger=trigger, log=log)


def _migrate(domain: str, plan: MigrationPlan, *, trigger: str, log: Logger) -> MigrationResult:
    """
    Migrate an application whose lock the caller holds.

    Args:
        domain: A validated domain.
        plan: What :func:`plan_migration` said would be done.
        trigger: Who asked, recorded in the deployment history.
        log: Where progress is reported.

    Returns:
        What was done.

    Raises:
        DeploymentError: See :func:`migrate`.
    """
    app = _inplace_app(domain)
    root = app_root(app)
    if plan.domain != app.domain or Path(plan.app_path) != root:
        raise DeploymentError(
            f"The plan is for {plan.domain} at {plan.app_path}, not {app.domain} at {root}",
            details="Plan again and migrate with the new plan.",
        )

    before = count_tree(root)
    if is_rehearsal():
        return _rehearse(app, root, plan, before, log)

    store = get_store()
    fs = get_fs()
    journal = _Journal(fs, log)
    releases = ReleaseManager(root, fs=fs, logger=log)
    services = ServiceManager()
    webserver = _webserver_for(app)
    recorder = DeploymentRecorder(
        store,
        app.domain,
        trigger,
        logger=log,
        git_info=lambda: (plan.commit[:7] if plan.commit else None, app.branch),
    )

    def put_back(error: BaseException) -> None:
        log.warning(f"Migration failed, restoring the in-place layout: {error}")
        journal.undo()
        if plan.unit:
            # Started again whatever the undo managed: it was stopped for the
            # migration, and the tree it runs from is as close to what it had
            # as could be made.
            try:
                services.restart(plan.unit)
            except WASMError as exc:
                journal.failures.append(f"{plan.unit} did not start again in place: {exc}")
        restored = count_tree(root)
        if restored != before:
            journal.failures.append(
                f"{root} holds {restored} after the undo and held {before} before"
            )
        if journal.failures and isinstance(error, WASMError):
            # The operator must learn from the error itself, not from a log
            # line above it, that the tree is not exactly as it was.
            error.details = "\n\n".join([error.details or "", journal.report()]).strip()

    try:
        with recording(recorder, git_branch=app.branch, on_failure=put_back):
            if plan.unit:
                # Before the first rename: a process running from the tree
                # writes into it while it moves (Next.js recreates .next), and
                # the undo of a rename whose source was recreated cannot run.
                log.substep(f"Stopping {plan.unit} while its tree moves")
                services.stop(plan.unit)
            staging, release = _move_into_release(root, plan, releases, journal, log)
            created_dirs = _share(root, release, plan, releases, journal, log)

            # Counted before anything runs on the new layout, so an upload that
            # arrives once it is serving is not mistaken for a file gained.
            after = count_tree(root)
            added_links = (ENV_FILE in plan.env_files) + len(plan.persistent) + 1
            if (after.files, after.bytes) != (before.files, before.bytes) or (
                after.links != before.links + added_links
            ):
                raise DeploymentError(
                    f"{root} held {before} before the migration and {after} after it",
                    details="Every file must still be there, in the release or in shared/. The "
                    "migration was undone.",
                )

            _hand_over([release, *created_dirs], log)
            unit_rewritten = _rewrite_unit(app, root, plan, services, store, journal, log)
            site_rewritten = _rewrite_site(app, root, webserver, store, journal, log)

            log.substep("Restarting on the release layout")
            healthy, evidence = health_gate_for(app, store, log).restart_and_probe()
            if not healthy:
                raise DeploymentError(
                    f"{app.domain} did not pass its health check on the release layout; "
                    "the in-place layout was put back",
                    details=evidence,
                )

            # The staging directory is empty once everything is in the release.
            journal.remove_empty(staging)
            _record(store, app, release, plan)
            _keep_the_directory_root_owned(root, log)
    except (OSError, sqlite3.Error) as error:
        # Not a WASM error, so it carries no details to put the undo's report
        # in: one error that says what failed and what the undo managed.
        raise DeploymentError(
            f"The migration of {app.domain} failed and was undone: {error}",
            details=journal.report(),
        ) from error

    log.substep(f"{app.domain} runs from release {release.name}")
    return MigrationResult(
        domain=app.domain,
        release_id=release.name,
        persistent=plan.persistent,
        env_files=plan.env_files,
        before=before,
        after=after,
        unit_rewritten=unit_rewritten,
        site_rewritten=site_rewritten,
        rehearsed=False,
        deployment_id=recorder.deployment_id,
    )


def _rehearse(
    app: App, root: Path, plan: MigrationPlan, before: TreeCount, log: Logger
) -> MigrationResult:
    """
    Announce what a migration would change, and change nothing.

    Args:
        app: The application.
        root: Its directory.
        plan: The plan.
        before: What the directory holds.
        log: Where the rehearsal is reported.

    Returns:
        A result marked as rehearsed.
    """
    # Announced through the seam, which records each change and makes none;
    # the targets are named for the operator, not computed as links.
    fs = get_fs()
    release = root / RELEASES_DIR / plan.release_id
    fs.move(root, release)
    for name in plan.env_files:
        fs.move(release / name, root / SHARED_DIR / name)
    for path in plan.persistent:
        fs.move(release / path, root / SHARED_DIR / path)
        fs.symlink(root / SHARED_DIR / path, release / path)
    for name in plan.companions:
        fs.move(release / name, root / SHARED_DIR / name)
    fs.symlink(Path(RELEASES_DIR) / plan.release_id, root / CURRENT_LINK)
    if plan.unit_rewrite and plan.unit:
        log.info(f"Would rewrite the unit {plan.unit} to run from {root / CURRENT_LINK}")
    if plan.site_rewrite:
        log.info(f"Would rewrite the site of {app.domain} to serve {root / CURRENT_LINK}")
    return MigrationResult(
        domain=app.domain,
        release_id="",
        persistent=plan.persistent,
        env_files=plan.env_files,
        before=before,
        after=before,
        unit_rewritten=False,
        site_rewritten=False,
        rehearsed=True,
        deployment_id=None,
    )


def _move_into_release(
    root: Path,
    plan: MigrationPlan,
    releases: ReleaseManager,
    journal: _Journal,
    log: Logger,
) -> tuple[Path, Path]:
    """
    Make the live tree the first release, by renaming, never copying.

    Everything moves into a staging directory first, so a name the
    application uses itself (``releases``, ``shared``, ``current``) is out of
    the way before the layout creates its own.

    Args:
        root: The application directory.
        plan: The plan.
        releases: The application's releases.
        journal: Where every change is recorded.
        log: Where progress is reported.

    Returns:
        The staging directory, empty by now, and the first release.
    """
    staging = root / f".wasm-migrating-{secrets.token_hex(4)}"
    entries = sorted(os.listdir(root))
    journal.made_dir(staging)
    for name in entries:
        journal.moved(root / name, staging / name)

    release = releases.new_release_dir(plan.commit)
    journal.created_dir(releases.releases_dir)
    journal.created_dir(release)
    for name in entries:
        journal.moved(staging / name, release / name)
    log.substep(f"The live tree is release {release.name}")
    return staging, release


def _share(
    root: Path,
    release: Path,
    plan: MigrationPlan,
    releases: ReleaseManager,
    journal: _Journal,
    log: Logger,
) -> list[Path]:
    """
    Move the environment and the persistent paths to ``shared/`` and link them.

    Args:
        root: The application directory.
        release: The first release.
        plan: The plan.
        releases: The application's releases.
        journal: Where every change is recorded.
        log: Where progress is reported.

    Returns:
        The directories created, which the service account must own.

    Raises:
        DeploymentError: A persistent path cannot be moved, or the release
            could not be activated.
    """
    shared = root / SHARED_DIR
    journal.made_dir(shared)
    created = [shared]

    for name in plan.env_files:
        source = release / name
        if os.path.lexists(source) and not source.is_symlink():
            journal.moved(source, shared / name)

    for raw in plan.persistent:
        path = PurePosixPath(raw)
        blocked = first_obstacle(release, path)
        if blocked is not None or (release / path).is_symlink():
            raise DeploymentError(
                f"Cannot move {path} to shared/: {blocked or release / path} is a symlink",
                details="Nothing was moved. Plan again.",
            )
        # What is missing on either side is created by the move or by the
        # link below; remembered top-down so the undo removes it bottom-up.
        for parent in reversed(path.parents[:-1]):
            if not os.path.lexists(shared / parent):
                journal.made_dir(shared / parent)
                created.append(shared / parent)
            if not os.path.lexists(release / parent):
                journal.made_dir(release / parent)
        if os.path.lexists(release / path):
            journal.moved(release / path, shared / path)
        else:
            journal.made_dir(shared / path)
            created.append(shared / path)

    # Beside their database, never linked: SQLite resolves the release's link
    # and keeps these next to the real file in shared/.
    for name in plan.companions:
        source = release / name
        if os.path.lexists(source) and not source.is_symlink():
            journal.moved(source, shared / name)

    links = releases.link_shared(release, plan.persistent)
    for linked in links.linked:
        journal.created_link(release / linked)
    if links.conflicts:
        raise DeploymentError(
            f"Could not link {', '.join(links.conflicts)} into release {release.name}",
            details="Something is still in the release where the link must go.",
        )
    for linked in links.linked:
        log.substep(f"Linked {linked} to shared/{linked}")

    if releases.activate(release) is not None:
        raise DeploymentError(
            f"{root / CURRENT_LINK} pointed at a release already",
            details="An in-place application has none. Check the directory by hand.",
        )
    journal.created_link(root / CURRENT_LINK)
    return created


def _hand_over(directories: Sequence[Path], log: Logger) -> None:
    """
    Give the directories the migration created to the service account.

    Only those: everything that moved keeps its owner and mode, which is
    what lets an undo put the tree back exactly.

    Args:
        directories: What was created.
        log: Where a failed hand-over is reported.
    """
    config = Config()
    for directory in directories:
        hand_over_file(
            directory,
            user=config.service_user,
            group=config.service_group,
            mode=_DIR_MODE,
            runner=get_runner(),
            logger=log,
        )


def _keep_the_directory_root_owned(root: Path, log: Logger) -> None:
    """
    Take the application directory back from the service account.

    In place the service owned it; on releases it must not, or it could
    re-point ``current`` at whatever it likes. A deploy on releases never
    hands it over, and a migrated application ends the same way.

    Args:
        root: The application directory.
        log: Where a failure is reported.
    """
    hand_over_file(root, user="root", group="root", mode=_DIR_MODE, runner=get_runner(), logger=log)


def _rewrite_unit(
    app: App,
    root: Path,
    plan: MigrationPlan,
    services: ServiceManager,
    store: WASMStore,
    journal: _Journal,
    log: Logger,
) -> bool:
    """
    Point the unit at ``current``, keeping the old body to put back.

    Args:
        app: The application.
        root: Its directory.
        plan: The plan.
        services: The service manager.
        store: The store.
        journal: Where the change is recorded.
        log: Where progress is reported.

    Returns:
        Whether the unit was rewritten.
    """
    if not plan.unit:
        return False
    body = services.get_service_config(plan.unit)
    if body is None or not _names(body, root):
        return False
    previous = services.update_config(plan.unit, relocate(body, root))
    row = store.get_service(plan.unit)

    def undo() -> None:
        services.update_config(plan.unit or "", previous)
        if row is not None:
            store.update_service(row)

    journal.record(f"rewrote the unit {plan.unit}", undo)
    if row is not None:
        store.update_service(_relocated_service(row, root))
    log.substep(f"The unit {plan.unit} runs from {root / CURRENT_LINK}")
    return True


def _relocated_service(row: Service, root: Path) -> Service:
    """
    Describe a service row as the rewritten unit runs it.

    Args:
        row: The stored row.
        root: The application directory.

    Returns:
        The row with its command, working directory and environment relocated.
    """
    return replace(
        row,
        command=relocate(row.command, root),
        working_directory=relocate(row.working_directory, root),
        environment={k: relocate(v, root) for k, v in row.environment.items()},
    )


def _rewrite_site(
    app: App,
    root: Path,
    webserver: NginxManager | ApacheManager,
    store: WASMStore,
    journal: _Journal,
    log: Logger,
) -> bool:
    """
    Point the site at ``current``, when it names the application directory.

    A proxied site only names a port and is left alone. A static one serves
    files from the directory and is rewritten, validated by the web server
    before it is installed, and reloaded.

    Args:
        app: The application.
        root: Its directory.
        webserver: The manager of the web server that serves it.
        store: The store.
        journal: Where the change is recorded.
        log: Where progress is reported.

    Returns:
        Whether the site was rewritten.
    """
    previous = webserver.get_site_config(app.domain)
    if previous is None or not _names(previous, root):
        return False
    row = store.get_site(app.domain)
    webserver.replace_site_config(app.domain, relocate(previous, root))

    def undo() -> None:
        webserver.replace_site_config(app.domain, previous)
        webserver.reload()
        if row is not None:
            store.update_site(row)

    journal.record(f"rewrote the site of {app.domain}", undo)
    if row is not None and row.document_root:
        store.update_site(replace(row, document_root=relocate(row.document_root, root)))
    webserver.reload()
    log.substep(f"The site of {app.domain} serves {root / CURRENT_LINK}")
    return True


def _record(store: WASMStore, app: App, release: Path, plan: MigrationPlan) -> None:
    """
    Record the application on releases, with its first release active.

    Both rows or neither: an application row on releases without its release
    reads as an application with nothing active, and one still in place
    would have the next update build over a tree that is not there. A
    failure here fails the migration, which is then undone.

    Args:
        store: The store.
        app: The application.
        release: The first release.
        plan: The plan.
    """
    store.update_app(replace(app, layout=RELEASES, persistent_paths=list(plan.persistent)))
    if app.id is None:
        return
    store.record_release(
        ReleaseRecord(
            id=release.name,
            app_id=app.id,
            git_commit=plan.commit[:7] if plan.commit else None,
            created_at=datetime.now(timezone.utc).isoformat(),
            status=ReleaseStatus.BUILT.value,
            path=str(release),
        )
    )
    store.mark_release_active(app.id, release.name)
