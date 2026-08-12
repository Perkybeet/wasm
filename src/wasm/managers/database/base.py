# Copyright (c) 2024-2025 Yago López Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
Shared machinery for every database engine manager.

Service control, package install and removal, dump and restore plumbing,
privilege whitelisting and identifier quoting live here. A concrete backend only
declares its packages, its client binaries and the statements its engine speaks.

Three rules are enforced in this module and must not be relaxed by subclasses:

- **No shell.** Dumps reach disk through
  :meth:`~wasm.core.runner.CommandRunner.capture_to_file`. The contents of a
  database can never be reinterpreted as shell syntax, and a binary dump is
  never round-tripped through a string.
- **No secrets in argv.** Passwords travel through stdin, an environment
  variable or a 0600 option file. Anything on a command line is visible in
  ``ps`` to every account on the machine.
- **No unvalidated SQL fragments.** Privileges come from a per-engine whitelist
  and identifiers are quoted with the engine's own mechanism.
"""

from __future__ import annotations

import os
import re
import secrets
import string
from abc import abstractmethod
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from wasm.core.exceptions import (
    DatabaseBackupError,
    DatabaseEngineError,
    DatabaseError,
    DatabaseUserError,
)
from wasm.core.runner import CommandResult, CommandRunner, get_runner
from wasm.managers.base_manager import BaseManager

#: Deadline for a query or any other short-lived client invocation.
QUERY_TIMEOUT = 120

#: Deadline for a systemctl verb. Stopping a busy engine can take a while.
SERVICE_TIMEOUT = 120

#: Deadline for apt. Package downloads are slow and must not be cut short.
PACKAGE_TIMEOUT = 1800

#: Deadline for a dump, a restore or a decompression.
TRANSFER_TIMEOUT = 3600

#: apt must never stop to ask a question on a server.
APT_ENV: Mapping[str, str] = {"DEBIAN_FRONTEND": "noninteractive"}

#: Backups are readable only by root, and so is the directory holding them.
BACKUP_DIR_MODE = 0o750

#: Staging directory for restores: traversable so the engine account can open
#: the file it owns, unlistable so it cannot enumerate other backups.
STAGING_DIR_MODE = 0o711

# Every pattern below ends in \Z, never in $. In Python '$' also matches just
# before a final newline, so "shop\n" satisfies a '$'-anchored whitelist: the
# check would cover the first line and wave the rest through, which is the
# opposite of what an allowlist is for. \Z matches the end of the string and
# nothing else.

#: Database and user names accepted by WASM. Deliberately narrower than what the
#: engines accept: names come from HTTP requests and CLI arguments, and a name
#: that needs quoting to be safe is a name nobody wants to type.
NAME_PATTERN = re.compile(r"\A[A-Za-z0-9_][A-Za-z0-9_$-]*\Z")

#: Filesystem paths that may be handed to a client's own file-reading command.
SAFE_PATH_PATTERN = re.compile(r"\A[A-Za-z0-9_./-]+\Z")

#: Privileges are keywords, optionally multi-word ("ALL PRIVILEGES"). Anything
#: with punctuation is an injection attempt, not a privilege.
PRIVILEGE_PATTERN = re.compile(r"\A[A-Z]+(?: [A-Z]+)*\Z")


def quote_identifier(value: str, quote: str) -> str:
    """
    Quote a SQL identifier with the engine's quoting character.

    Args:
        value: Raw identifier, such as a database or user name.
        quote: The engine's identifier quote character (a backtick for MySQL, a
            double quote for PostgreSQL).

    Returns:
        The identifier wrapped in the quote character, with every embedded
        occurrence of that character doubled.
    """
    return f"{quote}{value.replace(quote, quote * 2)}{quote}"


def validate_name(value: str, *, kind: str, engine: str, max_length: int) -> str:
    """
    Check that a database or user name is one WASM is willing to handle.

    Quoting alone would be enough for the SQL layer, but names also end up in
    file names, service names and connection strings, so they are constrained
    once, here.

    Args:
        value: Candidate name.
        kind: What the name designates, used in the error message.
        engine: Engine name, used in the error message.
        max_length: Longest name the engine accepts.

    Returns:
        The name, unchanged.

    Raises:
        DatabaseError: When the name is empty, too long or contains a character
            outside ``[A-Za-z0-9_$-]``, a trailing newline included.
    """
    if not isinstance(value, str) or not value:
        raise DatabaseError(
            f"Empty {engine} {kind} name",
            details=f"Provide a {kind} name of 1 to {max_length} characters.",
        )
    if len(value) > max_length:
        raise DatabaseError(
            f"{engine} {kind} name is too long: {len(value)} characters",
            details=f"{engine} accepts at most {max_length} characters for a {kind} name.",
        )
    if not NAME_PATTERN.match(value):
        raise DatabaseError(
            f"Invalid {engine} {kind} name: {value!r}",
            details=(
                f"A {kind} name must start with a letter, a digit or an underscore and may "
                "only contain letters, digits and the characters _ $ -."
            ),
        )
    return value


def validate_privileges(
    privileges: Sequence[str] | None,
    *,
    allowed: frozenset[str],
    engine: str,
    default: Sequence[str],
) -> tuple[str, ...]:
    """
    Reduce a caller-supplied privilege list to a whitelisted, normalised tuple.

    Privileges are the one part of a GRANT that cannot be quoted: they are SQL
    keywords. The only safe treatment is an exact-match whitelist, which is what
    this function is.

    Args:
        privileges: Privileges requested by the caller, or None for the default.
        allowed: The engine's whitelist, upper case.
        engine: Engine name, used in the error message.
        default: Privileges to use when the caller supplied none.

    Returns:
        Normalised privileges, upper case, in the order given, without repeats.

    Raises:
        DatabaseUserError: When any entry is not a plain whitelisted keyword.
    """
    requested = list(privileges) if privileges else list(default)

    seen: list[str] = []
    for raw in requested:
        if not isinstance(raw, str):
            raise DatabaseUserError(
                f"Invalid {engine} privilege: {raw!r}",
                details=f"Privileges must be strings. Allowed: {', '.join(sorted(allowed))}.",
            )
        candidate = " ".join(raw.split()).upper()
        if not PRIVILEGE_PATTERN.match(candidate) or candidate not in allowed:
            raise DatabaseUserError(
                f"Invalid {engine} privilege: {raw!r}",
                details=(
                    f"Allowed {engine} privileges: {', '.join(sorted(allowed))}. "
                    "Pass one privilege per list entry, without punctuation."
                ),
            )
        if candidate not in seen:
            seen.append(candidate)

    if not seen:
        raise DatabaseUserError(
            f"No {engine} privileges given",
            details=f"Allowed {engine} privileges: {', '.join(sorted(allowed))}.",
        )
    return tuple(seen)


def validate_path(path: Path, *, purpose: str) -> Path:
    """
    Check that a path may be embedded in a client's own file-reading command.

    Some clients (the MySQL shell, for one) have no argv option for "read this
    file", only an in-band ``source`` command. Such a path must not contain
    anything that the client's parser could read as syntax.

    Args:
        path: Candidate path.
        purpose: What the path is for, used in the error message.

    Returns:
        The path, unchanged.

    Raises:
        DatabaseBackupError: When the path contains anything but letters,
            digits, dot, slash, dash or underscore. A newline anywhere, final
            one included, is what would end the command and start another.
    """
    if not SAFE_PATH_PATTERN.match(str(path)):
        raise DatabaseBackupError(
            f"Unsafe path for {purpose}: {path}",
            details=(
                "Move the file to a path made only of letters, digits and the characters "
                "._-/ before retrying."
            ),
        )
    return path


@dataclass
class DatabaseInfo:
    """Information about a database."""

    name: str
    engine: str
    size: str | None = None
    tables: int = 0
    owner: str | None = None
    encoding: str | None = None
    created: datetime | None = None
    connection_string: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """
        Render the database information as plain data.

        Returns:
            A JSON-serialisable dictionary.
        """
        return {
            "name": self.name,
            "engine": self.engine,
            "size": self.size,
            "tables": self.tables,
            "owner": self.owner,
            "encoding": self.encoding,
            "created": self.created.isoformat() if self.created else None,
            "connection_string": self.connection_string,
            **self.extra,
        }


@dataclass
class UserInfo:
    """Information about a database user."""

    username: str
    engine: str
    host: str = "localhost"
    databases: list[str] = field(default_factory=list)
    privileges: list[str] = field(default_factory=list)
    created: datetime | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """
        Render the user information as plain data.

        Returns:
            A JSON-serialisable dictionary.
        """
        return {
            "username": self.username,
            "engine": self.engine,
            "host": self.host,
            "databases": self.databases,
            "privileges": self.privileges,
            "created": self.created.isoformat() if self.created else None,
            **self.extra,
        }


@dataclass
class BackupInfo:
    """Information about a database backup."""

    path: Path
    database: str
    engine: str
    size: int
    created: datetime
    compressed: bool = False

    def to_dict(self) -> dict[str, Any]:
        """
        Render the backup information as plain data.

        Returns:
            A JSON-serialisable dictionary.
        """
        return {
            "path": str(self.path),
            "database": self.database,
            "engine": self.engine,
            "size": self.size,
            "size_human": format_size(self.size),
            "created": self.created.isoformat(),
            "compressed": self.compressed,
        }


def format_size(size: float) -> str:
    """
    Render a byte count in the largest unit that keeps it under 1024.

    Args:
        size: Number of bytes.

    Returns:
        A human readable size, such as ``"1.5 MB"``.
    """
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} PB"


class BaseDatabaseManager(BaseManager):
    """
    Base class for database engine managers.

    Subclasses declare the engine's packages, binaries and dialect; the workflow
    around them is implemented once, here.
    """

    #: Engine identifier used in the registry, in file names and in the API.
    ENGINE_NAME: str = ""
    #: Human readable engine name, used in messages.
    DISPLAY_NAME: str = ""
    #: Port the engine listens on by default.
    DEFAULT_PORT: int = 0
    #: systemd unit that runs the engine.
    SERVICE_NAME: str = ""
    #: Packages installed by :meth:`install`.
    PACKAGE_NAMES: tuple[str, ...] = ()
    #: Binary whose presence means the engine's client is installed.
    CLIENT_BINARY: str = ""
    #: Command that prints the engine version.
    VERSION_ARGV: tuple[str, ...] = ()
    #: Pattern whose first group is the version inside that command's output.
    VERSION_PATTERN: str = r"(\d+\.\d+\.\d+)"
    #: Paths removed by ``uninstall(purge=True)``.
    PURGE_PATHS: tuple[str, ...] = ()
    #: Extension given to a backup file before any ``.gz``.
    BACKUP_SUFFIX: str = ".sql"
    #: Longest database name the engine accepts.
    MAX_DATABASE_NAME_LENGTH: int = 63
    #: Longest user name the engine accepts.
    MAX_USER_NAME_LENGTH: int = 63
    #: Privileges :meth:`grant_privileges` and :meth:`revoke_privileges` accept.
    VALID_PRIVILEGES: frozenset[str] = frozenset()
    #: Privileges used when the caller names none.
    DEFAULT_PRIVILEGES: tuple[str, ...] = ()

    #: Where backups are written when the caller gives no path.
    BACKUP_DIR = Path("/var/backups/wasm/databases")

    # ==================== Process execution ====================

    @property
    def runner(self) -> CommandRunner:
        """The process runner. Resolved per call so tests can swap it in."""
        return get_runner()

    def _exec(
        self,
        argv: Sequence[str],
        *,
        input: str | None = None,
        env: Mapping[str, str] | None = None,
        timeout: int = QUERY_TIMEOUT,
        secrets: Sequence[str] = (),
    ) -> CommandResult:
        """
        Run a command through the audited runner.

        Args:
            argv: Program and arguments. Never a shell string.
            input: Data written to the process stdin. Statements carrying a
                password go here instead of into argv.
            env: Extra environment variables.
            timeout: Deadline in seconds.
            secrets: Values to keep out of the logs.

        Returns:
            The command outcome.
        """
        return self.runner.run(argv, input=input, env=env, timeout=timeout, secrets=secrets)

    # ==================== Passwords ====================

    @staticmethod
    def generate_password(length: int = 24) -> str:
        """
        Generate a secure random password.

        Args:
            length: Password length.

        Returns:
            A password containing at least one lower case letter, one upper case
            letter, one digit and one symbol.
        """
        alphabet = string.ascii_letters + string.digits + "!@#$%^&*"
        password = [
            secrets.choice(string.ascii_lowercase),
            secrets.choice(string.ascii_uppercase),
            secrets.choice(string.digits),
            secrets.choice("!@#$%^&*"),
        ]
        password += [secrets.choice(alphabet) for _ in range(length - 4)]
        secrets.SystemRandom().shuffle(password)
        return "".join(password)

    # ==================== Validation ====================

    @classmethod
    def validate_database_name(cls, name: str) -> str:
        """
        Check a database name against the engine's rules.

        Args:
            name: Candidate database name.

        Returns:
            The name, unchanged.

        Raises:
            DatabaseError: When the name is not acceptable.
        """
        return validate_name(
            name,
            kind="database",
            engine=cls.DISPLAY_NAME,
            max_length=cls.MAX_DATABASE_NAME_LENGTH,
        )

    @classmethod
    def validate_user_name(cls, username: str) -> str:
        """
        Check a user name against the engine's rules.

        Args:
            username: Candidate user name.

        Returns:
            The name, unchanged.

        Raises:
            DatabaseError: When the name is not acceptable.
        """
        return validate_name(
            username,
            kind="user",
            engine=cls.DISPLAY_NAME,
            max_length=cls.MAX_USER_NAME_LENGTH,
        )

    @classmethod
    def validate_privileges(cls, privileges: Sequence[str] | None) -> tuple[str, ...]:
        """
        Reduce requested privileges to the engine's whitelist.

        Args:
            privileges: Privileges requested by the caller, or None.

        Returns:
            Normalised, whitelisted privileges.

        Raises:
            DatabaseUserError: When a privilege is not whitelisted.
        """
        return validate_privileges(
            privileges,
            allowed=cls.VALID_PRIVILEGES,
            engine=cls.DISPLAY_NAME,
            default=cls.DEFAULT_PRIVILEGES,
        )

    # ==================== Engine Management ====================

    def is_installed(self) -> bool:
        """
        Report whether the engine's client is installed.

        Returns:
            True when the client binary is on PATH.
        """
        return bool(self.CLIENT_BINARY) and self.runner.exists(self.CLIENT_BINARY)

    def get_version(self) -> str | None:
        """
        Read the engine version from its own ``--version`` output.

        Returns:
            The version string, or None when the engine is absent or silent.
        """
        if not self.VERSION_ARGV:
            return None
        result = self._exec(list(self.VERSION_ARGV))
        if not result.success:
            return None
        match = re.search(self.VERSION_PATTERN, result.stdout)
        return match.group(1) if match else None

    def _package_sets(self) -> tuple[list[str], ...]:
        """
        Return the package sets to try, in order of preference.

        Returns:
            One list of package names per candidate flavour of the engine.
        """
        return (list(self.PACKAGE_NAMES),)

    def _pre_install(self) -> None:
        """Prepare apt sources. Engines outside the distro repos override this."""

    def _post_install(self) -> None:
        """Harden the fresh installation. Engines that need it override this."""

    def _on_packages_installed(self, packages: Sequence[str]) -> None:
        """
        React to the package set that actually installed.

        Args:
            packages: The package names that installed successfully.
        """

    def install(self) -> None:
        """
        Install the engine, enable its unit and start it.

        Raises:
            DatabaseEngineError: When apt or the unit fails.
        """
        self.logger.info(f"Installing {self.DISPLAY_NAME}...")

        self._pre_install()

        result = self._exec(["apt-get", "update"], env=APT_ENV, timeout=PACKAGE_TIMEOUT)
        if not result.success:
            raise DatabaseEngineError(
                "Failed to update the package list",
                details=result.stderr.strip() or "Check the apt sources in /etc/apt.",
            )

        failures: list[str] = []
        for packages in self._package_sets():
            result = self._exec(
                ["apt-get", "install", "-y", *packages],
                env=APT_ENV,
                timeout=PACKAGE_TIMEOUT,
            )
            if result.success:
                self._on_packages_installed(packages)
                break
            failures.append(f"{' '.join(packages)}: {result.stderr.strip()}")
        else:
            raise DatabaseEngineError(
                f"Failed to install {self.DISPLAY_NAME}",
                details="\n".join(failures) or "apt-get install returned no output.",
            )

        self.enable()
        self.start()
        self._post_install()

    def uninstall(self, purge: bool = False) -> None:
        """
        Remove the engine's packages, and its data when purging.

        Args:
            purge: Also delete the data and configuration directories.

        Raises:
            DatabaseEngineError: When every package set fails to be removed.
        """
        self.logger.info(f"Uninstalling {self.DISPLAY_NAME}...")

        try:
            self.stop()
        except DatabaseEngineError as exc:
            self.logger.warning(f"Could not stop {self.DISPLAY_NAME} before removal: {exc}")

        action = "purge" if purge else "remove"
        failures: list[str] = []
        for packages in self._package_sets():
            result = self._exec(
                ["apt-get", action, "-y", *packages],
                env=APT_ENV,
                timeout=PACKAGE_TIMEOUT,
            )
            if not result.success:
                failures.append(f"{' '.join(packages)}: {result.stderr.strip()}")

        if len(failures) == len(self._package_sets()):
            raise DatabaseEngineError(
                f"Failed to remove {self.DISPLAY_NAME}",
                details="\n".join(failures) or f"Run: apt-get {action} -y manually.",
            )

        if purge:
            for path in self.PURGE_PATHS:
                self._exec(["rm", "-rf", path], timeout=SERVICE_TIMEOUT)

    def _systemctl(self, action: str) -> CommandResult:
        """
        Apply a systemd verb to the engine's unit.

        Args:
            action: The systemctl verb.

        Returns:
            The command outcome.
        """
        return self._exec(["systemctl", action, self.SERVICE_NAME], timeout=SERVICE_TIMEOUT)

    def _service_action(self, action: str) -> None:
        """
        Apply a systemd verb and turn a failure into an actionable error.

        Args:
            action: The systemctl verb.

        Raises:
            DatabaseEngineError: When systemctl reports failure.
        """
        result = self._systemctl(action)
        if not result.success:
            raise DatabaseEngineError(
                f"Failed to {action} {self.DISPLAY_NAME}",
                details=(
                    f"{result.stderr.strip()}\n"
                    f"Inspect the unit with: journalctl -u {self.SERVICE_NAME} -n 50"
                ).strip(),
            )

    def start(self) -> None:
        """
        Start the engine's service.

        Raises:
            DatabaseEngineError: When the unit fails to start.
        """
        self._service_action("start")

    def stop(self) -> None:
        """
        Stop the engine's service.

        Raises:
            DatabaseEngineError: When the unit fails to stop.
        """
        self._service_action("stop")

    def restart(self) -> None:
        """
        Restart the engine's service.

        Raises:
            DatabaseEngineError: When the unit fails to restart.
        """
        self._service_action("restart")

    def enable(self) -> None:
        """
        Enable the engine's service at boot.

        Raises:
            DatabaseEngineError: When the unit cannot be enabled.
        """
        self._service_action("enable")

    def disable(self) -> None:
        """
        Disable the engine's service at boot.

        Raises:
            DatabaseEngineError: When the unit cannot be disabled.
        """
        self._service_action("disable")

    def is_running(self) -> bool:
        """
        Report whether the engine's service is active.

        Returns:
            True when systemd reports the unit as active.
        """
        return self._systemctl("is-active").output == "active"

    def get_status(self) -> dict[str, Any]:
        """
        Summarise the engine's state.

        Returns:
            A dictionary describing installation, version and service state.
        """
        installed = self.is_installed()
        return {
            "engine": self.ENGINE_NAME,
            "display_name": self.DISPLAY_NAME,
            "installed": installed,
            "version": self.get_version() if installed else None,
            "running": self.is_running() if installed else False,
            "port": self.DEFAULT_PORT,
            "service": self.SERVICE_NAME,
        }

    # ==================== Database Management ====================

    @abstractmethod
    def create_database(
        self,
        name: str,
        owner: str | None = None,
        encoding: str | None = None,
        **kwargs,
    ) -> DatabaseInfo:
        """
        Create a new database.

        Args:
            name: Database name.
            owner: Owner user, when the engine has the concept.
            encoding: Character encoding.
            **kwargs: Engine-specific options.

        Returns:
            Information about the new database.

        Raises:
            DatabaseExistsError: When the database already exists.
            DatabaseError: When creation fails.
        """

    @abstractmethod
    def drop_database(self, name: str, force: bool = False) -> None:
        """
        Drop a database.

        Args:
            name: Database name.
            force: Drop even if the database is in use, and stay silent when it
                does not exist.

        Raises:
            DatabaseNotFoundError: When the database does not exist.
            DatabaseError: When the drop fails.
        """

    @abstractmethod
    def database_exists(self, name: str) -> bool:
        """
        Report whether a database exists.

        Args:
            name: Database name.

        Returns:
            True when the database exists.
        """

    @abstractmethod
    def list_databases(self) -> list[DatabaseInfo]:
        """
        List the databases the engine holds.

        Returns:
            One entry per non-system database.
        """

    @abstractmethod
    def get_database_info(self, name: str) -> DatabaseInfo:
        """
        Describe one database.

        Args:
            name: Database name.

        Returns:
            Information about the database.

        Raises:
            DatabaseNotFoundError: When the database does not exist.
        """

    # ==================== User Management ====================

    @abstractmethod
    def create_user(
        self,
        username: str,
        password: str | None = None,
        host: str = "localhost",
        **kwargs,
    ) -> tuple[UserInfo, str]:
        """
        Create a database user.

        Args:
            username: User name.
            password: Password. Generated when omitted.
            host: Host restriction, for engines that have one.
            **kwargs: Engine-specific options.

        Returns:
            The user and its password.

        Raises:
            DatabaseUserError: When creation fails.
        """

    @abstractmethod
    def drop_user(self, username: str, host: str = "localhost") -> None:
        """
        Drop a database user.

        Args:
            username: User name.
            host: Host restriction, for engines that have one.

        Raises:
            DatabaseUserError: When the drop fails.
        """

    @abstractmethod
    def user_exists(self, username: str, host: str = "localhost") -> bool:
        """
        Report whether a user exists.

        Args:
            username: User name.
            host: Host restriction, for engines that have one.

        Returns:
            True when the user exists.
        """

    @abstractmethod
    def list_users(self) -> list[UserInfo]:
        """
        List the engine's users.

        Returns:
            One entry per user.
        """

    @abstractmethod
    def grant_privileges(
        self,
        username: str,
        database: str,
        privileges: Sequence[str] | None = None,
        host: str = "localhost",
    ) -> None:
        """
        Grant privileges on a database to a user.

        Args:
            username: User name.
            database: Database name.
            privileges: Privileges to grant. Engine default when omitted.
            host: Host restriction, for engines that have one.

        Raises:
            DatabaseUserError: When a privilege is not whitelisted or the grant
                fails.
        """

    @abstractmethod
    def revoke_privileges(
        self,
        username: str,
        database: str,
        privileges: Sequence[str] | None = None,
        host: str = "localhost",
    ) -> None:
        """
        Revoke privileges on a database from a user.

        Args:
            username: User name.
            database: Database name.
            privileges: Privileges to revoke. Engine default when omitted.
            host: Host restriction, for engines that have one.

        Raises:
            DatabaseUserError: When a privilege is not whitelisted or the revoke
                fails.
        """

    # ==================== Backup & Restore ====================

    def _ensure_directory(self, path: Path, mode: int = BACKUP_DIR_MODE) -> Path:
        """
        Create a directory the caller chose, without touching an existing one.

        This is for destinations WASM does not own, such as the parent of a
        ``--output`` path: an existing directory keeps its permissions, because
        chmod-ing ``/tmp`` or a user's home would be a worse bug than a lax
        backup directory. Directories WASM owns go through
        :meth:`_ensure_private_directory` instead.

        Args:
            path: Directory to create.
            mode: Permissions for directories this call creates.

        Returns:
            The directory path.

        Raises:
            DatabaseBackupError: When the directory cannot be created.
        """
        try:
            # mkdir applies the mode at creation, so the directory is never
            # briefly world readable the way a create-then-chmod leaves it.
            # exist_ok keeps an existing directory exactly as it was.
            path.mkdir(parents=True, exist_ok=True, mode=mode)
        except OSError as exc:
            raise DatabaseBackupError(
                f"Cannot create the backup directory {path}",
                details=f"{exc}. Check ownership and free space, then retry.",
            ) from exc
        return path

    def _ensure_private_directory(self, path: Path, mode: int = BACKUP_DIR_MODE) -> Path:
        """
        Create or adopt a directory WASM owns, with its mode enforced.

        Anything WASM writes as root into a directory it owns has to be sure the
        directory is really the one it means: not a symlink pointing somewhere
        else, not another account's, and not left group or world writable by an
        earlier version or by whoever got there first. The mode is applied
        through the open descriptor, so the inode that was inspected is the
        inode that is modified and then written to.

        Args:
            path: Directory to create or adopt.
            mode: Permissions the directory must end up with.

        Returns:
            The directory path.

        Raises:
            DatabaseBackupError: When the path is a symlink, is not a directory,
                belongs to another account, or cannot be created.
        """
        self._ensure_directory(path.parent)
        try:
            try:
                os.mkdir(path, mode)
            except FileExistsError:
                pass
            # O_NOFOLLOW turns "someone replaced this with a symlink" into an
            # error instead of a redirect; O_DIRECTORY does the same for a file.
            fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                owner = os.fstat(fd).st_uid
                if owner != os.geteuid():
                    raise DatabaseBackupError(
                        f"The directory {path} belongs to uid {owner}",
                        details=(
                            "WASM refuses to write backups into a directory it does not own, "
                            f"because whoever owns it decides who reads them. Remove {path} "
                            "and retry."
                        ),
                    )
                os.fchmod(fd, mode)
            finally:
                os.close(fd)
        except OSError as exc:
            raise DatabaseBackupError(
                f"Cannot use the directory {path}",
                details=(
                    f"{exc}. It must be a real directory owned by this account, "
                    "not a symlink or a file."
                ),
            ) from exc
        return path

    def _ensure_backup_directory(self, destination: Path) -> None:
        """
        Prepare the directory a backup is about to be written into.

        Args:
            destination: The backup file that is about to be created.
        """
        if destination.parent == self.BACKUP_DIR:
            self._ensure_private_directory(destination.parent, BACKUP_DIR_MODE)
        else:
            self._ensure_directory(destination.parent)

    def _backup_path(
        self,
        database: str,
        output_path: Path | None,
        compress: bool,
        *,
        label: str | None = None,
        suffix: str | None = None,
    ) -> Path:
        """
        Decide where a backup is written.

        Args:
            database: Database the backup belongs to.
            output_path: Caller-supplied destination, used verbatim when given.
            compress: Whether the file will be gzipped.
            label: Overrides the database name in the generated file name.
            suffix: Overrides :attr:`BACKUP_SUFFIX`.

        Returns:
            The destination path.
        """
        if output_path is not None:
            return Path(output_path)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = (
            f"{self.ENGINE_NAME}-{label or database}-{timestamp}{suffix or self.BACKUP_SUFFIX}"
        )
        if compress:
            filename += ".gz"
        return self.BACKUP_DIR / filename

    def _dump_to_file(
        self,
        argv: Sequence[str],
        destination: Path,
        *,
        database: str,
        compress: bool,
        env: Mapping[str, str] | None = None,
        secrets: Sequence[str] = (),
        timeout: int = TRANSFER_TIMEOUT,
    ) -> BackupInfo:
        """
        Run a dump command and stream its stdout straight into a file.

        The dump never passes through a shell and never through a Python string,
        which is what makes a binary dump or a dump containing quotes survive.

        Args:
            argv: The dump command.
            destination: File to write.
            database: Database being dumped, for messages and metadata.
            compress: Pipe the dump through gzip.
            env: Extra environment variables, for credentials.
            secrets: Values to keep out of the logs.
            timeout: Deadline in seconds.

        Returns:
            Information about the backup that was written.

        Raises:
            DatabaseBackupError: When the dump command fails or writes nothing.
        """
        self._ensure_backup_directory(destination)
        result = self.runner.capture_to_file(
            argv,
            destination,
            compress=compress,
            env=env,
            timeout=timeout,
            secrets=secrets,
        )
        if not result.success:
            raise DatabaseBackupError(
                f"Failed to back up '{database}'",
                details=(
                    result.stderr.strip()
                    or f"{argv[0]} exited with code {result.exit_code} and said nothing."
                ),
            )
        if not destination.exists():
            raise DatabaseBackupError(
                f"Backup of '{database}' produced no file",
                details=f"{argv[0]} reported success but {destination} does not exist.",
            )

        self.logger.info(f"Created backup: {destination}")
        return BackupInfo(
            path=destination,
            database=database,
            engine=self.ENGINE_NAME,
            size=destination.stat().st_size,
            created=datetime.now(),
            compressed=compress,
        )

    @contextmanager
    def _staged_backup(
        self,
        source: Path,
        staged_name: str,
        *,
        owner: str | None = None,
    ) -> Iterator[Path]:
        """
        Make a backup readable by the account that will restore it.

        Backups are written 0600 and owned by root, and a client such as psql
        opens the file itself, as the engine's own account. The file is therefore
        copied (or decompressed) into a traversable staging directory and handed
        to that account for the duration of the restore.

        The staging directory is adopted, never merely reused: it is traversable
        by design, so whoever gets there first must not be able to leave a
        symlink behind it or a symlink inside it and turn a root copy into a
        write of their choosing.

        Args:
            source: The backup file, plain or gzipped.
            staged_name: File name to use inside the staging directory.
            owner: Account that must be able to read the staged file.

        Yields:
            The path of the staged, plain-text copy.

        Raises:
            DatabaseBackupError: When staging fails.
        """
        self._ensure_private_directory(self.BACKUP_DIR, BACKUP_DIR_MODE)
        staging_dir = self._ensure_private_directory(self.BACKUP_DIR / ".staging", STAGING_DIR_MODE)
        staged = staging_dir / staged_name
        # Whatever is at the staged name is ours to remove: a leftover from a
        # crashed restore, or a symlink someone planted to catch the copy.
        try:
            staged.unlink(missing_ok=True)
        except OSError as exc:
            raise DatabaseBackupError(
                f"Cannot clear the staging path {staged}",
                details=f"{exc}. Remove it by hand and retry.",
            ) from exc
        try:
            if source.suffix == ".gz":
                result = self.runner.capture_to_file(
                    ["gzip", "-dc", str(source)],
                    staged,
                    timeout=TRANSFER_TIMEOUT,
                )
            else:
                result = self._exec(["cp", str(source), str(staged)], timeout=TRANSFER_TIMEOUT)
            if not result.success:
                raise DatabaseBackupError(
                    f"Failed to stage the backup {source}",
                    details=result.stderr.strip() or "Check free space in the backup directory.",
                )
            if owner:
                self._exec(["chown", owner, str(staged)], timeout=SERVICE_TIMEOUT)
            yield staged
        finally:
            staged.unlink(missing_ok=True)

    @abstractmethod
    def backup(
        self,
        database: str,
        output_path: Path | None = None,
        compress: bool = True,
        **kwargs,
    ) -> BackupInfo:
        """
        Back up a database.

        Args:
            database: Database name.
            output_path: Custom output path.
            compress: Compress the backup with gzip.
            **kwargs: Engine-specific options.

        Returns:
            Information about the backup.

        Raises:
            DatabaseBackupError: When the backup fails.
        """

    @abstractmethod
    def restore(
        self,
        database: str,
        backup_path: Path,
        drop_existing: bool = False,
        **kwargs,
    ) -> None:
        """
        Restore a database from a backup.

        Args:
            database: Target database name.
            backup_path: Path to the backup file.
            drop_existing: Drop the target database first.
            **kwargs: Engine-specific options.

        Raises:
            DatabaseBackupError: When the restore fails.
        """

    def list_backups(self, database: str | None = None) -> list[BackupInfo]:
        """
        List the backups this engine has written.

        Args:
            database: Only list backups of this database.

        Returns:
            Backups, newest first.
        """
        if not self.BACKUP_DIR.exists():
            return []

        # engine-database-YYYYmmdd_HHMMSS.ext, where the database name itself may
        # contain dashes, so the timestamp is what anchors the split.
        pattern = re.compile(
            rf"\A{re.escape(self.ENGINE_NAME)}-(?P<database>.+)-\d{{8}}_\d{{6}}(?P<ext>\..+)?\Z"
        )

        backups: list[BackupInfo] = []
        for path in sorted(self.BACKUP_DIR.glob(f"{self.ENGINE_NAME}-*")):
            match = pattern.match(path.name)
            if not match:
                continue
            db_name = match.group("database")
            if database and db_name != database:
                continue
            try:
                stat = path.stat()
            except OSError as exc:
                self.logger.debug(f"Skipping unreadable backup {path}: {exc}")
                continue
            backups.append(
                BackupInfo(
                    path=path,
                    database=db_name,
                    engine=self.ENGINE_NAME,
                    size=stat.st_size,
                    created=datetime.fromtimestamp(stat.st_mtime),
                    compressed=path.suffix == ".gz",
                )
            )

        return sorted(backups, key=lambda backup: backup.created, reverse=True)

    # ==================== Query Execution ====================

    @abstractmethod
    def execute_query(
        self,
        database: str,
        query: str,
        **kwargs,
    ) -> tuple[bool, str]:
        """
        Execute a statement against a database.

        Args:
            database: Database name.
            query: Statement to execute.
            **kwargs: Engine-specific options.

        Returns:
            Whether the statement succeeded, and its output.

        Raises:
            DatabaseQueryError: When the statement fails.
        """

    @abstractmethod
    def get_connection_string(
        self,
        database: str,
        username: str,
        password: str,
        host: str = "localhost",
    ) -> str:
        """
        Build a connection string for an application.

        Args:
            database: Database name.
            username: User name.
            password: Password.
            host: Host to connect to.

        Returns:
            The connection string.
        """

    def get_interactive_command(
        self,
        database: str | None = None,
        username: str | None = None,
    ) -> list[str]:
        """
        Build the command that opens an interactive client.

        Args:
            database: Database to connect to.
            username: User to connect as.

        Returns:
            The argument vector to execute.

        Raises:
            NotImplementedError: When the engine does not define one.
        """
        raise NotImplementedError("Subclass must implement get_interactive_command")
