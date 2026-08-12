# Copyright (c) 2024-2026 Yago Lopez Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
Validation of values that end up inside a systemd unit file.

A systemd unit is a line-oriented INI file: a newline ends a directive and the
next line starts a new one. Everything WASM interpolates into a unit
(environment variables, the description, the command, the working directory)
arrives from user input, in the worst case straight from ``POST /api/apps``.
Before this module, a single ``\\n`` in an environment value appended arbitrary
directives to the unit, and because systemd keeps the *last* assignment of a
directive, a payload such as::

    x"\\nUser=root\\nExecStartPre=/bin/sh -c "id > /tmp/pwned"\\nEnvironment="Y=y

turned a ``www-data`` service into a root service running attacker shell on
every start.

Design decision
---------------
Two defences, both applied, because each covers what the other cannot:

1. **Reject at the boundary** (this module). Variable names must be POSIX
   environment identifiers (``[A-Za-z_][A-Za-z0-9_]*``); anything else is not
   an environment variable at all, it is a way to smuggle ``=`` or a newline
   into the directive. Values must contain no C0 control character and no
   ``DEL``. Rejecting newlines is the non-negotiable part; tab and the rest of
   the C0 range go with them because a unit file cannot represent them
   faithfully without C-escaping and no legitimate deployment needs them.
   Non-ASCII text is allowed: unit files are UTF-8 and ``café`` is a
   perfectly good value.

2. **Escape at render time** (:func:`escape_systemd_value`, applied by the
   Jinja macros in ``templates/systemd/_escape.j2``). Values are rendered
   inside double quotes, so ``"`` and ``\\`` are escaped, and ``%`` is doubled
   because systemd expands ``%`` specifiers in directive values before parsing
   them. Escaping is what keeps a caller that bypasses this module, or a future
   template, from reopening the hole.

Why not ``EnvironmentFile=``
----------------------------
An ``EnvironmentFile=`` at mode 0600 would keep secrets out of ``systemctl
show`` output and is the better long-term shape. It is not what this fix does,
for two reasons: the file has its own, differently broken quoting rules (it is
parsed shell-like, so an unvalidated value injects there too and still needs
this validation), and it moves secret material to a second file that every
backup, restore and delete path would have to learn about. Closing the
injection hole does not depend on that migration, so it is done here first.
"""

from __future__ import annotations

import re
from collections.abc import Mapping

from wasm.core.exceptions import ValidationError

#: POSIX environment variable name: a C identifier.
#: ``\A``/``\Z`` rather than ``^``/``$``: ``$`` also matches before a trailing
#: newline, which is exactly the character that must never get through.
ENV_NAME_PATTERN = re.compile(r"\A[A-Za-z_][A-Za-z0-9_]*\Z")

#: C0 control characters plus DEL. Any of these ends or corrupts a directive.
_FORBIDDEN_VALUE_CHARS = re.compile(r"[\x00-\x1f\x7f]")

#: Scalars the HTTP layer may hand over as JSON values and that map cleanly to
#: an environment variable.
_COERCIBLE_TYPES = (str, int, float, bool)

_CONTROL_CHAR_NAMES = {
    "\n": "newline",
    "\r": "carriage return",
    "\t": "tab",
    "\x00": "NUL",
}


class EnvironmentValidationError(ValidationError):
    """Raised when a value cannot be safely written into a systemd unit."""


def _describe(char: str) -> str:
    """
    Render a control character in a form a user can act on.

    Args:
        char: The offending character.

    Returns:
        A human readable name, falling back to the code point.
    """
    return _CONTROL_CHAR_NAMES.get(char, f"control character U+{ord(char):04X}")


def is_valid_env_name(name: str) -> bool:
    """
    Report whether a string is a usable environment variable name.

    Args:
        name: Candidate variable name.

    Returns:
        True when the name is a POSIX environment identifier.
    """
    return bool(ENV_NAME_PATTERN.match(name))


def validate_env_name(name: str) -> str:
    """
    Validate an environment variable name.

    Args:
        name: Candidate variable name.

    Returns:
        The name unchanged.

    Raises:
        EnvironmentValidationError: When the name is not a POSIX environment
            identifier.
    """
    if not isinstance(name, str) or not is_valid_env_name(name):
        raise EnvironmentValidationError(
            f"Invalid environment variable name: {name!r}",
            details=(
                "Names must start with a letter or underscore and contain only "
                "letters, digits and underscores (for example API_URL or _CACHE). "
                "Rename the variable and deploy again."
            ),
        )
    return name


def validate_env_value(value: object, *, name: str = "") -> str:
    """
    Validate a single environment variable value.

    Args:
        value: Candidate value. Strings, numbers and booleans are accepted;
            anything else has no unambiguous environment representation.
        name: Variable name, used only to make the error message actionable.

    Returns:
        The value as a string, unescaped.

    Raises:
        EnvironmentValidationError: When the value is not a scalar or contains a
            control character.
    """
    label = f"'{name}'" if name else "value"

    if not isinstance(value, _COERCIBLE_TYPES):
        raise EnvironmentValidationError(
            f"Environment variable {label} must be a string, number or boolean, "
            f"got {type(value).__name__}",
            details="Serialise structured values yourself, for example as JSON.",
        )

    text = str(value)
    match = _FORBIDDEN_VALUE_CHARS.search(text)
    if match:
        raise EnvironmentValidationError(
            f"Environment variable {label} contains a {_describe(match.group())}",
            details=(
                "A systemd unit is line oriented, so control characters would end the "
                "directive and let the rest of the value inject new ones. Remove them, "
                "or store multi-line data in a file and pass its path instead."
            ),
        )
    return text


def validate_environment(env: Mapping[str, object]) -> dict[str, str]:
    """
    Validate a whole environment mapping destined for a systemd unit.

    Args:
        env: Variable names mapped to values.

    Returns:
        A new mapping with validated names and stringified values. Values are
        returned unescaped, so they can be persisted and displayed as the user
        typed them; escaping happens when the unit is rendered.

    Raises:
        EnvironmentValidationError: When any name or value is unsafe.
    """
    return {
        validate_env_name(key): validate_env_value(value, name=str(key))
        for key, value in env.items()
    }


def validate_unit_value(value: str, *, field: str) -> str:
    """
    Validate a scalar directive value such as Description or ExecStart.

    Args:
        value: Candidate directive value.
        field: Directive name, used in the error message.

    Returns:
        The value unchanged.

    Raises:
        EnvironmentValidationError: When the value contains a control character.
    """
    text = str(value)
    match = _FORBIDDEN_VALUE_CHARS.search(text)
    if match:
        raise EnvironmentValidationError(
            f"Value for systemd directive {field}= contains a {_describe(match.group())}",
            details=(
                "Directives occupy a single line; a control character here would append "
                "arbitrary directives to the unit. Check the domain, path or command "
                f"used for {field}."
            ),
        )
    return text


def escape_systemd_value(value: str) -> str:
    """
    Escape a value for interpolation inside a double-quoted directive value.

    Args:
        value: Validated, unescaped value.

    Returns:
        The value with backslashes and double quotes C-escaped and ``%``
        doubled so systemd does not expand it as a specifier.
    """
    return str(value).replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
