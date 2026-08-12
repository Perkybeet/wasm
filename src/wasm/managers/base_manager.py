# Copyright (c) 2024-2026 Yago Lopez Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
Infrastructure shared by every manager.

Two things live here, and both exist to remove a defect class rather than to
save typing.

:class:`BaseManager` gives every adapter two ways to reach the system, and only
those two: the injectable :class:`~wasm.core.runner.CommandRunner` for anything
it executes, and the injectable :class:`~wasm.core.fs.FileSystem` for anything it
writes. The previous version wrapped ``core.utils.run_command``, which split
strings into argv, accepted ``shell=`` and defaulted to no timeout at all; three
separate ways for a domain name to become a command. It also carried
``_run_sudo``. That is gone: WASM requires root (decision D6 of the v1 design),
so a manager that re-elevates is either redundant or hiding the fact that it is
running unprivileged.

The filesystem seam is here for the same reason the runner is. These managers
write systemd units and web server configurations into ``/etc`` and delete them
again; before the seam existed a ``--dry-run`` deploy printed "no changes will
be made to this machine" and then unlinked the unit file, because a deletion is
a ``Path.unlink`` and never goes near a subprocess.

:class:`MappingRecord` is the bridge that lets a manager return a typed record
where it used to return a bare dict. The contract that matters is the field
names: ``wasm health`` spent several releases reading ``cert["expires"]`` from a
mapping that only ever contained ``expiry``, and a plain dict answered that with
``None`` instead of an error.
"""

from __future__ import annotations

import dataclasses
from abc import ABC, abstractmethod
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any

from wasm.core.config import Config
from wasm.core.fs import FileSystem, get_fs
from wasm.core.logger import Logger
from wasm.core.runner import DEFAULT_TIMEOUT, CommandResult, CommandRunner, get_runner


class MappingRecord:
    """
    Mixin that lets a dataclass be read like the dict it replaces.

    Managers used to hand dicts across module boundaries, so their readers index
    and ``.get()`` them. Making the replacement records answer to that protocol
    keeps those readers working while the field names become part of a type.

    The one deliberate difference from a dict: an unknown key raises
    :class:`KeyError` from :meth:`get` as well as from ``[]``. A key that is not
    a field is a typo or a stale name, never a missing value, and returning the
    default for it is exactly how the ``expires``/``expiry`` bug survived.
    """

    def _field_names(self) -> tuple[str, ...]:
        """
        List the fields of the concrete record.

        Returns:
            The field names, in declaration order.

        Raises:
            TypeError: When the mixin is used on a class that is not a
                dataclass.
        """
        if not dataclasses.is_dataclass(self):
            raise TypeError(f"{type(self).__name__} must be a dataclass to use MappingRecord")
        return tuple(f.name for f in dataclasses.fields(self))

    def _check_key(self, key: str) -> str:
        """
        Reject a key that is not a field of this record.

        Args:
            key: Key as written by the caller.

        Returns:
            The key, once it is known to name a field.

        Raises:
            KeyError: When the key is not a field.
        """
        if key not in self._field_names():
            raise KeyError(
                f"{type(self).__name__} has no field {key!r}. "
                f"Available fields: {', '.join(self._field_names())}."
            )
        return key

    def __getitem__(self, key: str) -> Any:
        """
        Read a field by name.

        Args:
            key: Field name.

        Returns:
            The field value.

        Raises:
            KeyError: When the key is not a field.
        """
        return getattr(self, self._check_key(key))

    def __setitem__(self, key: str, value: Any) -> None:
        """
        Write a field by name.

        Args:
            key: Field name.
            value: New value.

        Raises:
            KeyError: When the key is not a field.
        """
        setattr(self, self._check_key(key), value)

    def get(self, key: str, default: Any = None) -> Any:
        """
        Read a field, falling back to a default when it is unset.

        Args:
            key: Field name.
            default: Returned when the field holds None or an empty value.

        Returns:
            The field value, or the default.

        Raises:
            KeyError: When the key is not a field of this record.
        """
        value = getattr(self, self._check_key(key))
        if value is None or value == "" or value == []:
            return default
        return value

    def keys(self) -> tuple[str, ...]:
        """
        List the field names.

        Returns:
            The field names, in declaration order.
        """
        return self._field_names()

    def __iter__(self) -> Iterator[str]:
        """
        Iterate over field names, as a mapping does.

        Returns:
            An iterator over the field names.
        """
        return iter(self._field_names())

    def __contains__(self, key: object) -> bool:
        """
        Report whether a name is a field of this record.

        Args:
            key: Candidate field name.

        Returns:
            True when the record has that field.
        """
        return isinstance(key, str) and key in self._field_names()

    def to_dict(self) -> dict[str, Any]:
        """
        Convert the record to a plain dictionary.

        Returns:
            A mapping of field name to value.
        """
        return {name: getattr(self, name) for name in self._field_names()}


class BaseManager(ABC):
    """
    Abstract base class for all managers.

    Provides configuration access, logging, and the two seams through which a
    manager may touch the system: one for what it executes, one for what it
    writes.
    """

    def __init__(
        self,
        verbose: bool = False,
        runner: CommandRunner | None = None,
        fs: FileSystem | None = None,
    ) -> None:
        """
        Initialize the manager.

        Args:
            verbose: Enable verbose logging.
            runner: Command runner to execute with. Defaults to the process-wide
                runner, which is what makes ``--dry-run`` and the test fake work
                without every call site knowing about them.
            fs: Filesystem to write through. Defaults to the process-wide one,
                for the same reason.
        """
        self.config = Config()
        self.logger = Logger(verbose=verbose)
        self.verbose = verbose
        self._runner = runner
        self._fs = fs

    @property
    def runner(self) -> CommandRunner:
        """
        The command runner used for every external process.

        Returns:
            The injected runner, or the process-wide one.
        """
        return self._runner or get_runner()

    @property
    def fs(self) -> FileSystem:
        """
        The filesystem used for every change this manager makes to disk.

        Returns:
            The injected filesystem, or the process-wide one.
        """
        return self._fs or get_fs()

    def _run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        timeout: int = DEFAULT_TIMEOUT,
        stdin: str | None = None,
        user: str | None = None,
        check: bool = False,
        secrets: Sequence[str] = (),
    ) -> CommandResult:
        """
        Execute a command through the shared runner.

        Args:
            argv: Program and arguments. Never a shell string: passing one is a
                :class:`ValueError` from the runner, not a silent split.
            cwd: Working directory.
            env: Extra environment variables, merged over the current one.
            timeout: Deadline in seconds. There is no way to disable it; a
                manager that waits forever is a manager that hangs a deploy.
            stdin: Data written to the process stdin, then closed. This is how a
                secret reaches a program without appearing in ``ps``.
            user: Run as this account instead of root.
            check: Raise CommandError instead of returning a failed result.
            secrets: Literal values to redact from logs and from the result.

        Returns:
            The command outcome.

        Raises:
            CommandError: When check is True and the command failed.
        """
        result = self.runner.run(
            argv,
            cwd=cwd,
            env=env,
            timeout=timeout,
            input=stdin,
            user=user,
            check=check,
            secrets=secrets,
        )
        self.logger.debug(f"Ran: {result.command}")
        self.logger.command_output(result.stdout, result.stderr)
        return result

    @abstractmethod
    def is_installed(self) -> bool:
        """
        Check if the managed service or tool is installed.

        Returns:
            True if installed.
        """

    @abstractmethod
    def get_version(self) -> str | None:
        """
        Get the version of the managed service or tool.

        Returns:
            Version string, or None when it cannot be determined.
        """
