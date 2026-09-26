# Copyright (c) 2024-2026 Yago Lopez Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
One operation at a time on each application.

Nothing used to stop two operations on the same application from running at
once: two updates, an update and a rollback, a deploy and a migration, a push
webhook firing while an operator updated by hand. Each of them moves or
rebuilds the tree, re-points ``current`` or rewrites the unit, and two of them
interleaved leave a tree neither would have produced: a release pruned while
another activates it, a migration renaming the directory an update is
building in.

Every operation that changes an application's code or where it runs from takes
:func:`app_lock` for as long as it runs: deploy, update, release activation,
migration, restore, deletion and resource limits. A second one fails at once
with :class:`AppBusyError`, which names the operation holding the lock and
since when, so the console, the webhook and the CLI can say "an update is
already running" instead of waiting silently or corrupting the tree.

How it works, and why:

- ``fcntl.flock`` on ``<state dir>/locks/<domain>.lock``, next to the store
  and never inside the application tree, which a migration or a restore moves
  and which a repository could plant a link in. The kernel releases the lock
  when the holding process exits, however it exits, so a crash never leaves a
  stale lock for someone to remove by hand.
- The holder writes what it is doing into the file, which is how the refused
  caller can name it.
- Reentrant within one thread: an update takes a backup, a rollback restores
  one, an automatic deploy hands over to the deployer it chose, and each of
  those inner operations takes the lock the outer one already holds. Another
  thread of the same process (the console serving a request while its job
  worker updates) is refused like another process: ``flock`` locks belong to
  an open file description, and each acquisition opens its own.
- Under ``--dry-run`` nothing is created: a rehearsal changes nothing, and a
  lock file is a change.
"""

from __future__ import annotations

import errno
import fcntl
import json
import os
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from wasm.core.exceptions import WASMError
from wasm.core.fs import get_fs, is_rehearsal

#: Directory, next to the store's database, that holds one lock file per application.
LOCKS_DIR = "locks"

#: Only root reads what is running on which application.
_LOCKS_DIR_MODE = 0o700
_LOCK_FILE_MODE = 0o600

#: Largest holder record read back; a record is a few dozen bytes.
_MAX_RECORD = 4096


@dataclass(frozen=True)
class LockHolder:
    """
    What holds an application's lock, as its holder recorded it.

    Attributes:
        operation: What it is doing: ``update``, ``deploy``, ``migration``...
        pid: The process holding it.
        started_at: When it took the lock, ISO 8601 in UTC.
    """

    operation: str
    pid: int
    started_at: str


class AppBusyError(WASMError):
    """
    Another operation is running on the application.

    Attributes:
        domain: The application.
        operation: What was refused.
        holder: What holds the lock, when it could be read.
    """

    def __init__(self, domain: str, operation: str, holder: LockHolder | None) -> None:
        """
        Args:
            domain: The application.
            operation: What was refused.
            holder: What holds the lock, when it could be read.
        """
        self.domain = domain
        self.operation = operation
        self.holder = holder
        if holder is not None:
            running = f"{holder.operation} started at {holder.started_at} (pid {holder.pid})"
        else:
            running = "another WASM operation"
        super().__init__(
            f"Cannot start the {operation} of {domain}: {running} is still running on it",
            details="Only one deploy, update, rollback, migration, restore or deletion runs on "
            "an application at a time. Wait for that one to finish, then retry. The lock is "
            "released as soon as its process exits, so there is nothing to remove by hand.",
        )


@dataclass
class _Held:
    """A lock this thread holds, and how many nested operations are inside it."""

    fd: int
    operation: str
    depth: int = 1


_local = threading.local()


def _held() -> dict[str, _Held]:
    """
    Return the locks the calling thread holds.

    Returns:
        Lock key to held lock.
    """
    held: dict[str, _Held] | None = getattr(_local, "held", None)
    if held is None:
        held = {}
        _local.held = held
    return held


def _key(domain: str) -> str:
    """
    Turn a domain into the lock's name.

    Args:
        domain: The application's domain.

    Returns:
        The lower-cased domain.

    Raises:
        ValueError: It could not be a file name: empty, a path, or hidden.
    """
    key = domain.strip().lower().rstrip(".")
    if not key or "/" in key or "\0" in key or key.startswith("."):
        raise ValueError(f"not a domain: {domain!r}")
    return key


def locks_directory() -> Path:
    """
    Return where the lock files are kept.

    Returns:
        ``locks/`` beside the store's database: ``/var/lib/wasm/locks`` on a
        server, and inside the test's own directory in a test.
    """
    # Imported here: the store imports a great deal, and this module is
    # imported by modules the store's own imports reach.
    from wasm.core.store import get_store

    return get_store().db_path.parent / LOCKS_DIR


def lock_path(domain: str) -> Path:
    """
    Return the lock file of an application.

    Args:
        domain: The application's domain.

    Returns:
        The path of its lock file.
    """
    return locks_directory() / f"{_key(domain)}.lock"


def is_held_here(domain: str) -> bool:
    """
    Tell whether the calling thread holds an application's lock.

    Args:
        domain: The application's domain.

    Returns:
        True inside :func:`app_lock` for it, in this thread.
    """
    return _key(domain) in _held()


@contextmanager
def app_lock(domain: str, operation: str) -> Iterator[None]:
    """
    Hold an application's lock for the duration of an operation.

    Args:
        domain: The application's domain.
        operation: What is about to run, as the refused caller will read it:
            ``update``, ``deploy``, ``rollback``, ``migration``...

    Yields:
        Nothing; the lock is held until the block exits.

    Raises:
        AppBusyError: Another process, or another thread of this one, holds
            the lock. Raised at once, never after waiting.
    """
    key = _key(domain)
    held = _held()
    entry = held.get(key)
    if entry is not None:
        # An operation inside one that already holds it: a backup taken by
        # an update, the restore a rollback runs.
        entry.depth += 1
        try:
            yield
        finally:
            entry.depth -= 1
        return

    if is_rehearsal():
        yield
        return

    fd = _acquire(key, operation)
    held[key] = _Held(fd=fd, operation=operation)
    try:
        yield
    finally:
        del held[key]
        _release(fd)


def _acquire(key: str, operation: str) -> int:
    """
    Take the lock, or fail at once naming what holds it.

    Args:
        key: The lock's name.
        operation: What is about to run.

    Returns:
        The descriptor holding the lock.

    Raises:
        AppBusyError: Something else holds it.
        OSError: The lock file could not be opened.
    """
    path = locks_directory() / f"{key}.lock"
    get_fs().make_dir(path.parent, mode=_LOCKS_DIR_MODE)
    # O_NOFOLLOW: the directory is root's, but a link planted in it must not
    # turn "record who holds the lock" into a write somewhere else.
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, _LOCK_FILE_MODE)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as error:
        if error.errno not in (errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES):
            os.close(fd)
            raise
        holder = _read_holder(fd)
        os.close(fd)
        raise AppBusyError(key, operation, holder) from None

    record = json.dumps(
        {
            "operation": operation,
            "pid": os.getpid(),
            "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
    ).encode("utf-8")
    try:
        os.ftruncate(fd, 0)
        os.pwrite(fd, record, 0)
    except OSError:
        # The record is only for the message a refused caller shows; the lock
        # itself is held either way.
        pass
    return fd


def _release(fd: int) -> None:
    """
    Clear the holder record and release the lock.

    Args:
        fd: The descriptor holding it.
    """
    try:
        # Cleared before unlocking, so a caller that reads it after taking
        # the lock never sees the previous holder's record.
        os.ftruncate(fd, 0)
    except OSError:
        pass
    finally:
        # Closing the descriptor releases the lock even if unlocking failed.
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _read_holder(fd: int) -> LockHolder | None:
    """
    Read what the holder of a lock recorded.

    Args:
        fd: A descriptor of the lock file.

    Returns:
        The holder, or None when nothing readable was recorded yet: the
        holder may have taken the lock an instant ago.
    """
    try:
        raw = os.pread(fd, _MAX_RECORD, 0)
        record = json.loads(raw.decode("utf-8"))
        return LockHolder(
            operation=str(record["operation"]),
            pid=int(record["pid"]),
            started_at=str(record["started_at"]),
        )
    except (OSError, ValueError, KeyError, TypeError):
        return None
