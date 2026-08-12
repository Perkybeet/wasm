# Copyright (c) 2024-2026 Yago Lopez Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
Validation of the identifiers WASM turns into paths, unit names and SQL.

Every name here eventually leaves Python: a service name becomes
``/etc/systemd/system/<name>.service``, an app name becomes a directory under
``/var/www/apps``, a database name is interpolated into DDL. WASM runs as root,
so a name that carries a path separator, a NUL byte or a newline is not a
cosmetic problem: it is arbitrary file write, arbitrary unit installation or
statement injection.

**Why an allowlist and never a denylist.** A denylist has to enumerate every
dangerous form: ``..``, ``%2e%2e``, backslashes, NUL truncation, Unicode
look-alikes, control characters, overlong UTF-8, Windows device names. Every
encoding layer added later (a URL decode, a JSON unescape, a shell) invents new
ones, so the list is always one trick behind the attacker. An allowlist inverts
the burden: only characters known to be inert in a path, a unit file and an SQL
identifier are accepted, and everything else - including forms nobody has
thought of yet - is refused by default. The regexes below are anchored and
deliberately narrow; widening one is a security decision.

The other half of the job is :func:`resolve_within`, which contains a path to a
base directory even when the name itself is clean but the filesystem is not, for
example when a symlink inside the base directory points out of it.
"""

from __future__ import annotations

import re
from pathlib import Path

from wasm.core.exceptions import SecurityError, ValidationError

#: systemd allows longer unit names, but nothing WASM manages needs more and a
#: short cap keeps names readable in journalctl and in the panel.
MAX_SERVICE_NAME_LENGTH = 64

MAX_APP_NAME_LENGTH = 64

#: PostgreSQL truncates identifiers at 63 bytes; MySQL allows 64. The stricter
#: limit is the portable one.
MAX_DATABASE_NAME_LENGTH = 63

MAX_DATABASE_USER_LENGTH = 63

#: Common limit of a single path component on ext4, xfs and btrfs.
MAX_FILENAME_LENGTH = 255

#: Unit names: letters, digits and the punctuation systemd itself uses. The
#: leading character must be alphanumeric, which is what rules out ``.`` and
#: ``..`` as whole names.
SERVICE_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@-]*$")

APP_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

#: SQL identifiers are used unquoted in DDL, so no dots, hyphens or spaces.
DATABASE_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

FILENAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

#: Names that are not files even when they look like one. ``.`` and ``..`` are
#: the traversal primitives; the rest are DOS device names that some tooling and
#: some filesystems still treat specially.
RESERVED_NAMES = frozenset(
    {".", ".."}
    | {"con", "prn", "aux", "nul"}
    | {f"com{i}" for i in range(1, 10)}
    | {f"lpt{i}" for i in range(1, 10)}
)


def _validate_identifier(
    value: str,
    *,
    kind: str,
    pattern: re.Pattern[str],
    max_length: int,
    allowed: str,
) -> str:
    """
    Apply the shared allowlist checks to a candidate identifier.

    Args:
        value: The candidate name, exactly as it arrived from the caller.
        kind: Human-readable name of what is being validated, used in messages.
        pattern: Anchored allowlist the whole name must match.
        max_length: Maximum accepted length in characters.
        allowed: Description of the accepted alphabet, shown to the user.

    Returns:
        The name, unchanged, once it is known to be safe.

    Raises:
        ValidationError: When the name is empty, too long or contains anything
            outside the allowlist.
    """
    if not isinstance(value, str):
        raise ValidationError(
            f"{kind} must be a string",
            details=f"Received {type(value).__name__}.",
        )

    if not value:
        raise ValidationError(
            f"{kind} is required",
            details=f"Provide a non-empty name using {allowed}.",
        )

    if len(value) > max_length:
        raise ValidationError(
            f"{kind} is too long: {len(value)} characters",
            details=f"Use at most {max_length} characters.",
        )

    if not pattern.match(value):
        # The rejected value is echoed with repr so control characters and NUL
        # bytes are visible in the panel instead of mangling the message.
        raise ValidationError(
            f"Invalid {kind.lower()}: {value!r}",
            details=(
                f"Only {allowed} are allowed, and the name must start with an "
                "alphanumeric character. Path separators, '..', spaces, "
                "newlines and NUL bytes are rejected."
            ),
        )

    if value.lower() in RESERVED_NAMES:
        raise ValidationError(
            f"Reserved {kind.lower()}: {value!r}",
            details="Choose a different name; this one has a special meaning.",
        )

    return value


def validate_service_name(name: str) -> str:
    """
    Validate a systemd unit name.

    The name is used to build ``<unit dir>/<name>.service`` and is passed to
    systemctl, so it must be a single, inert path component.

    Args:
        name: Candidate unit name, without the ``.service`` suffix.

    Returns:
        The validated name.

    Raises:
        ValidationError: When the name is not a safe unit name.
    """
    return _validate_identifier(
        name,
        kind="Service name",
        pattern=SERVICE_NAME_PATTERN,
        max_length=MAX_SERVICE_NAME_LENGTH,
        allowed="letters, digits, '.', '_', '-' and '@'",
    )


def validate_app_name(name: str) -> str:
    """
    Validate an application name.

    The name becomes a directory under the applications root and part of a unit
    name, so it follows the same single-component rule.

    Args:
        name: Candidate application name.

    Returns:
        The validated name.

    Raises:
        ValidationError: When the name is not a safe application name.
    """
    return _validate_identifier(
        name,
        kind="Application name",
        pattern=APP_NAME_PATTERN,
        max_length=MAX_APP_NAME_LENGTH,
        allowed="letters, digits, '.', '_' and '-'",
    )


def validate_database_name(name: str) -> str:
    """
    Validate a database name.

    Database names reach the engine inside statements that cannot be
    parameterised, so the alphabet is narrower than for paths: no dots, no
    hyphens, no quotes, nothing that could terminate an identifier.

    Args:
        name: Candidate database name.

    Returns:
        The validated name.

    Raises:
        ValidationError: When the name is not a safe SQL identifier.
    """
    return _validate_identifier(
        name,
        kind="Database name",
        pattern=DATABASE_IDENTIFIER_PATTERN,
        max_length=MAX_DATABASE_NAME_LENGTH,
        allowed="letters, digits and '_'",
    )


def validate_database_user(name: str) -> str:
    """
    Validate a database user name.

    Args:
        name: Candidate database user name.

    Returns:
        The validated name.

    Raises:
        ValidationError: When the name is not a safe SQL identifier.
    """
    return _validate_identifier(
        name,
        kind="Database user",
        pattern=DATABASE_IDENTIFIER_PATTERN,
        max_length=MAX_DATABASE_USER_LENGTH,
        allowed="letters, digits and '_'",
    )


def validate_filename(name: str) -> str:
    """
    Validate a single file or directory name.

    The result is safe to join to a trusted directory: it is one path component,
    it is not ``.`` or ``..``, and it carries no separator, control character or
    NUL byte. It is not, on its own, proof that the final path stays inside that
    directory - use :func:`resolve_within` for that.

    Args:
        name: Candidate file or directory name.

    Returns:
        The validated name.

    Raises:
        ValidationError: When the name is not a safe path component.
    """
    validated = _validate_identifier(
        name,
        kind="File name",
        pattern=FILENAME_PATTERN,
        max_length=MAX_FILENAME_LENGTH,
        allowed="letters, digits, '.', '_' and '-'",
    )

    # A trailing dot or a run of dots is legal on Linux but is a well known
    # source of surprises once a name crosses to another filesystem or tool.
    if ".." in validated or validated.endswith("."):
        raise ValidationError(
            f"Invalid file name: {name!r}",
            details="File names must not contain '..' or end with a dot.",
        )

    return validated


def resolve_within(base: Path, candidate: str) -> Path:
    """
    Resolve a candidate path inside a base directory, refusing to escape it.

    Both sides are resolved before they are compared, which is the only order
    that works: ``base / candidate`` may contain ``..`` segments or traverse a
    symlink, so a comparison made on the unresolved path would be checking a
    string that does not describe where the write will land.

    Args:
        base: Directory the result must stay inside.
        candidate: Relative path to resolve within ``base``. An absolute path or
            one that climbs out of ``base`` is an error, not a silent override.

    Returns:
        The resolved absolute path, guaranteed to be strictly inside the
        resolved base.

    Raises:
        ValidationError: When the candidate is empty or contains a NUL byte.
        SecurityError: When the resolved path falls outside ``base``, or is
            ``base`` itself.
    """
    if not candidate:
        raise ValidationError(
            "Path is required",
            details="Provide a relative path inside the base directory.",
        )

    if "\x00" in candidate or "\x00" in str(base):
        raise ValidationError(
            f"Path contains a NUL byte: {candidate!r}",
            details="NUL bytes truncate paths at the system call boundary.",
        )

    resolved_base = Path(base).resolve()
    # Path.__truediv__ discards the left operand when the right one is absolute,
    # so '/etc/shadow' arrives here as itself and is caught by the check below.
    target = (resolved_base / candidate).resolve()

    # Resolving to the base itself means the candidate named a directory where a
    # path inside it was expected, so it is refused rather than silently used.
    if target == resolved_base or not target.is_relative_to(resolved_base):
        raise SecurityError(
            f"Path escapes the allowed directory: {candidate!r}",
            details=(
                f"Resolved to {target}, which is outside {resolved_base}. "
                "Use a plain name without '..', leading '/' or symlinks that "
                "point out of the directory."
            ),
        )

    return target
