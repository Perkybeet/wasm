# Copyright (c) 2024-2026 Yago Lopez Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
What an application's health check may be told: where to probe, what counts
as up, and for how long to wait.

The store calls these before it writes the settings, so every caller - the
CLI, the API, a script - is held to the same rule, and the health gate never
meets a value it cannot use.
"""

from __future__ import annotations

import re

from wasm.core.exceptions import ValidationError

#: The fewest and the most seconds an application may be given to come up.
#: Below 5 a process that is merely starting fails the gate; past ten minutes
#: an update holds the application's lock for longer than anyone waits.
HEALTH_TIMEOUT_MIN = 5
HEALTH_TIMEOUT_MAX = 600

#: The HTTP status codes there are.
_STATUS_MIN = 100
_STATUS_MAX = 599

#: One item of an expectation: a status, or an inclusive range of them.
_ITEM = re.compile(r"(?P<low>[0-9]{3})(?:-(?P<high>[0-9]{3}))?")

#: Longer than any health endpoint; long enough to reject a pasted blob.
_PATH_MAX = 1024


def check_health_path(path: str) -> str:
    """
    Accept a path on the application, and nothing that could name another host.

    The gate requests ``http://127.0.0.1:<port>`` followed by this path, so
    it must begin with a single slash (``//host`` would be a host) and be
    printable ASCII: a space or a control character would split or corrupt
    the request line, and anything else has to be percent-encoded anyway.

    Args:
        path: What the operator asked for, such as ``/healthz``.

    Returns:
        The path, unchanged.

    Raises:
        ValidationError: It is not such a path.
    """
    how = "Give a path on the application, such as /healthz or /api/health?deep=1."
    if not isinstance(path, str) or not path.startswith("/") or path.startswith("//"):
        raise ValidationError(
            f"{path!r} is not a path on the application",
            details=f"{how} A scheme or a host is not accepted: the check always asks "
            "the application itself, on 127.0.0.1.",
        )
    if len(path) > _PATH_MAX:
        raise ValidationError(
            f"The health check path is longer than {_PATH_MAX} characters", details=how
        )
    if any(not ("\x21" <= char <= "\x7e") for char in path):
        raise ValidationError(
            f"{path!r} contains a space, a control character or a character outside ASCII",
            details=f"{how} Percent-encode anything else (%20 for a space).",
        )
    return path


def parse_health_expect(expect: str) -> tuple[tuple[int, int], ...]:
    """
    Read a list of statuses and ranges, such as ``200-399`` or ``200,204``.

    Args:
        expect: Comma-separated statuses (``204``) and inclusive ranges
            (``200-299``), each within 100-599.

    Returns:
        The ranges, a single status as a range of one.

    Raises:
        ValidationError: It is anything else.
    """
    how = (
        "Use statuses and ranges from 100 to 599, separated by commas: 200-399, or 200,204, "
        "or 200-299,301."
    )
    ranges: list[tuple[int, int]] = []
    items = expect.split(",") if isinstance(expect, str) else [""]
    for raw in items:
        item = raw.strip()
        match = _ITEM.fullmatch(item)
        if match is None:
            raise ValidationError(f"{expect!r} is not a list of HTTP statuses", details=how)
        low = int(match["low"])
        high = int(match["high"] or low)
        if not (_STATUS_MIN <= low <= high <= _STATUS_MAX):
            raise ValidationError(f"{item!r} is not a status range from 100 to 599", details=how)
        ranges.append((low, high))
    return tuple(ranges)


def check_health_expect(expect: str) -> str:
    """
    Accept a list of statuses and ranges, written the one way the store keeps it.

    Args:
        expect: See :func:`parse_health_expect`.

    Returns:
        The same list without spaces: ``200-299,301``.

    Raises:
        ValidationError: See :func:`parse_health_expect`.
    """
    return ",".join(
        str(low) if low == high else f"{low}-{high}" for low, high in parse_health_expect(expect)
    )


def check_health_timeout(timeout: int) -> int:
    """
    Accept how many seconds an application gets to come up.

    Args:
        timeout: Seconds.

    Returns:
        The same number.

    Raises:
        ValidationError: It is not a whole number from 5 to 600.
    """
    # bool is an int to Python, and True seconds is not something anyone meant.
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, int)
        or not HEALTH_TIMEOUT_MIN <= timeout <= HEALTH_TIMEOUT_MAX
    ):
        raise ValidationError(
            f"A health check timeout of {timeout!r} seconds is out of range",
            details=f"Give it from {HEALTH_TIMEOUT_MIN} to {HEALTH_TIMEOUT_MAX} seconds.",
        )
    return timeout
