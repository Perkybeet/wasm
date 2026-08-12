# Copyright (c) 2024-2026 Yago Lopez Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
The seam through which WASM changes the filesystem.

:mod:`wasm.core.runner` made ``--dry-run`` true for anything WASM *executes*.
It was not true for anything WASM *writes*, and an adversarial review proved
it: ``wasm --dry-run backup delete <id> --force`` printed "no changes will be
made to this machine" and then deleted the archive, because the deletion is a
``Path.unlink`` and never went near a subprocess.

That is the same defect the flag was supposed to fix, one layer down. A
rehearsal that quietly performs half the operation is worse than no rehearsal,
because the operator now trusts it.

So mutations go through a :class:`FileSystem`, and ``--dry-run`` installs one
that refuses. Reads are not routed here: they change nothing, they are on every
hot path, and making every ``Path.exists()`` go through an object would buy
nothing but noise.
"""

from __future__ import annotations

import os
import shutil
from abc import ABC, abstractmethod
from collections.abc import Callable
from pathlib import Path

#: Mode for anything that holds a credential: config, tokens, .env files,
#: database dumps and the store.
SECRET_MODE = 0o600

#: Mode for a directory that holds secrets.
SECRET_DIR_MODE = 0o700


class FileSystem(ABC):
    """Changes the filesystem. The only thing in the codebase that may."""

    @abstractmethod
    def write_text(self, path: Path, content: str, *, mode: int = 0o644) -> None:
        """
        Write a text file, replacing it atomically.

        A partially written unit file or nginx config is worse than none: the
        next daemon-reload or reload picks it up. Writing to a temporary file
        in the same directory and renaming makes the change all-or-nothing.

        Args:
            path: File to write.
            content: What to write.
            mode: Permissions to create it with. Use SECRET_MODE for anything
                holding a credential; the mode is applied at creation, not
                afterwards, so the file is never briefly world-readable.
        """

    @abstractmethod
    def make_dir(self, path: Path, *, mode: int = 0o755, parents: bool = True) -> None:
        """
        Create a directory.

        Args:
            path: Directory to create.
            mode: Permissions, applied to every level this call creates.
            parents: Create missing parents.
        """

    @abstractmethod
    def remove(self, path: Path, *, missing_ok: bool = True) -> None:
        """
        Delete a file.

        Args:
            path: File to delete.
            missing_ok: Do not complain when it is already gone.
        """

    @abstractmethod
    def remove_tree(self, path: Path) -> None:
        """
        Delete a directory and everything under it.

        Args:
            path: Directory to delete.
        """

    @abstractmethod
    def move(self, source: Path, destination: Path) -> None:
        """
        Move a file or directory.

        Args:
            source: What to move.
            destination: Where to move it.
        """

    @abstractmethod
    def copy_tree(self, source: Path, destination: Path) -> None:
        """
        Copy a directory recursively.

        Args:
            source: Directory to copy.
            destination: Where to copy it.
        """

    @abstractmethod
    def chmod(self, path: Path, mode: int) -> None:
        """
        Change permissions.

        Args:
            path: What to change.
            mode: New mode.
        """

    @abstractmethod
    def symlink(self, target: Path, link: Path) -> None:
        """
        Create a symbolic link, replacing one that is already there.

        Args:
            target: What the link points at.
            link: The link to create.
        """


class RealFileSystem(FileSystem):
    """Actually changes the filesystem."""

    def write_text(self, path: Path, content: str, *, mode: int = 0o644) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.wasm-tmp")
        # The mode is set at creation. A chmod afterwards leaves a window in
        # which a file holding a database password is world-readable.
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise

    def make_dir(self, path: Path, *, mode: int = 0o755, parents: bool = True) -> None:
        if not parents:
            path.mkdir(mode=mode, exist_ok=True)
            return
        # pathlib applies the mode only to the leaf and creates the parents
        # with the process umask, which is how a 0700 secrets directory ends up
        # under a 0755 one.
        missing = []
        current = path
        while not current.exists() and current != current.parent:
            missing.append(current)
            current = current.parent
        for directory in reversed(missing):
            directory.mkdir(mode=mode)

    def remove(self, path: Path, *, missing_ok: bool = True) -> None:
        path.unlink(missing_ok=missing_ok)

    def remove_tree(self, path: Path) -> None:
        shutil.rmtree(path)

    def move(self, source: Path, destination: Path) -> None:
        shutil.move(str(source), str(destination))

    def copy_tree(self, source: Path, destination: Path) -> None:
        # symlinks=True: a link in the source tree is copied as a link rather
        # than followed, so a source containing a link to /etc/passwd does not
        # deposit its contents inside the deployment.
        shutil.copytree(source, destination, symlinks=True, dirs_exist_ok=True)

    def chmod(self, path: Path, mode: int) -> None:
        path.chmod(mode)

    def symlink(self, target: Path, link: Path) -> None:
        link.unlink(missing_ok=True)
        link.symlink_to(target)


class DryRunFileSystem(FileSystem):
    """
    Records what would have happened and does nothing.

    This is the other half of ``--dry-run``. Without it the flag is a claim the
    program cannot keep, which is worse than not offering it.
    """

    def __init__(self, on_skip: Callable[[str], None] | None = None):
        """
        Args:
            on_skip: Called with a description of each change not made.
        """
        self._on_skip = on_skip
        self.skipped: list[str] = []

    def _skip(self, description: str) -> None:
        """
        Record a change that a real run would have made.

        Args:
            description: What would have happened.
        """
        self.skipped.append(description)
        if self._on_skip is not None:
            self._on_skip(description)

    def write_text(self, path: Path, content: str, *, mode: int = 0o644) -> None:
        self._skip(f"would write {path} ({len(content)} bytes, mode {mode:o})")

    def make_dir(self, path: Path, *, mode: int = 0o755, parents: bool = True) -> None:
        self._skip(f"would create directory {path} (mode {mode:o})")

    def remove(self, path: Path, *, missing_ok: bool = True) -> None:
        self._skip(f"would delete {path}")

    def remove_tree(self, path: Path) -> None:
        self._skip(f"would delete directory {path} and everything under it")

    def move(self, source: Path, destination: Path) -> None:
        self._skip(f"would move {source} to {destination}")

    def copy_tree(self, source: Path, destination: Path) -> None:
        self._skip(f"would copy {source} to {destination}")

    def chmod(self, path: Path, mode: int) -> None:
        self._skip(f"would set {path} to mode {mode:o}")

    def symlink(self, target: Path, link: Path) -> None:
        self._skip(f"would link {link} to {target}")


class RecordingFileSystem(RealFileSystem):
    """
    A real filesystem that also records what it did.

    Used in tests that want the changes to happen inside ``tmp_path`` and also
    want to assert on them.
    """

    def __init__(self) -> None:
        self.changes: list[tuple[str, Path]] = []

    def write_text(self, path: Path, content: str, *, mode: int = 0o644) -> None:
        self.changes.append(("write", path))
        super().write_text(path, content, mode=mode)

    def make_dir(self, path: Path, *, mode: int = 0o755, parents: bool = True) -> None:
        self.changes.append(("mkdir", path))
        super().make_dir(path, mode=mode, parents=parents)

    def remove(self, path: Path, *, missing_ok: bool = True) -> None:
        self.changes.append(("remove", path))
        super().remove(path, missing_ok=missing_ok)

    def remove_tree(self, path: Path) -> None:
        self.changes.append(("remove_tree", path))
        super().remove_tree(path)

    def move(self, source: Path, destination: Path) -> None:
        self.changes.append(("move", source))
        super().move(source, destination)

    def copy_tree(self, source: Path, destination: Path) -> None:
        self.changes.append(("copy_tree", source))
        super().copy_tree(source, destination)

    def chmod(self, path: Path, mode: int) -> None:
        self.changes.append(("chmod", path))
        super().chmod(path, mode)

    def symlink(self, target: Path, link: Path) -> None:
        self.changes.append(("symlink", link))
        super().symlink(target, link)


_default_fs: FileSystem | None = None


def get_fs() -> FileSystem:
    """
    Return the process-wide filesystem, creating the real one on first use.

    Returns:
        The active filesystem.
    """
    global _default_fs
    if _default_fs is None:
        _default_fs = RealFileSystem()
    return _default_fs


def set_fs(filesystem: FileSystem | None) -> None:
    """
    Replace the process-wide filesystem.

    Args:
        filesystem: The filesystem to install, or None to reset to the real one.
    """
    global _default_fs
    _default_fs = filesystem
