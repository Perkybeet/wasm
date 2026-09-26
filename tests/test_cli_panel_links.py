# Copyright (c) 2024-2026 Yago Lopez Prado
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Every panel deep link the CLI builds must land on a route that exists.

``wasm cert list --open`` used to print a link to ``/certificates``, a page
that has never existed in the console - certificates are a tab of
``/domains``. Rather than pin that one fix, this walks every
``open_in_panel(...)`` call under :mod:`wasm.cli.commands` and checks the path
it builds against ``panel/src/routeTree.gen.ts``, TanStack Router's own
generated map of every route the console actually serves, so the next dead
link is caught here instead of by an operator's browser.

Both sides are read as text - the CLI source with :mod:`ast`, the route tree
with a regular expression - rather than imported, so this has no dependency
on either the panel's toolchain or a running application.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
CLI_COMMANDS_DIR = REPO_ROOT / "src" / "wasm" / "cli" / "commands"
ROUTE_TREE = REPO_ROOT / "panel" / "src" / "routeTree.gen.ts"

#: What an interpolated segment - a CLI f-string value or a router "$param" -
#: is normalised to before two paths are compared segment by segment.
WILDCARD = "*"


def _segments(path: str) -> tuple[str, ...]:
    """
    Split a path into comparable segments.

    A query string plays no part in routing, and a trailing slash is exactly
    the difference between an index route's own declared path and the
    ``open_in_panel`` call that means the same page without one - both are
    dropped here so neither shape has to be special-cased by every caller.

    Args:
        path: An absolute path, with or without a query string.

    Returns:
        The path's segments, the root's single empty segment included.
    """
    path = path.split("?", 1)[0]
    if path != "/" and path.endswith("/"):
        path = path[:-1]
    return tuple(path.split("/"))


def _route_paths() -> list[str]:
    """
    Read every absolute path TanStack Router will match, from its own output.

    Returns:
        Every ``fullPath`` the generated route tree declares.

    Raises:
        AssertionError: If nothing was found, which means the format of the
            generated file changed and this parser needs to change with it -
            silently checking nothing would be worse than failing loudly.
    """
    text = ROUTE_TREE.read_text(encoding="utf-8")
    paths = sorted(set(re.findall(r'fullPath:\s*"([^"]*)"', text)))
    assert paths, f"No 'fullPath' entries found in {ROUTE_TREE}; has its format changed?"
    return paths


def _route_matches(built: tuple[str, ...], route: tuple[str, ...]) -> bool:
    """
    Check whether a CLI-built path's segments could resolve to a route's.

    Args:
        built: Segments of a path an ``open_in_panel`` call constructs.
        route: Segments of one route TanStack Router declares.

    Returns:
        True when every literal segment matches and the two have the same
        shape; a wildcard segment on either side matches anything.
    """
    if len(built) != len(route):
        return False
    return all(
        built_part == WILDCARD or route_part.startswith("$") or built_part == route_part
        for built_part, route_part in zip(built, route, strict=True)
    )


def _template_of(node: ast.expr) -> str | None:
    """
    Turn the first argument of an ``open_in_panel`` call into a path template.

    Args:
        node: The argument expression.

    Returns:
        The literal path, with every interpolated value replaced by
        :data:`WILDCARD`, or None when the argument is not a string or
        f-string this can read statically.
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        parts: list[str] = []
        for value in node.values:
            if isinstance(value, ast.Constant):
                parts.append(str(value.value))
            elif isinstance(value, ast.FormattedValue):
                parts.append(WILDCARD)
            else:  # pragma: no cover - no such node kind appears in an f-string
                return None
        return "".join(parts)
    return None


def _open_in_panel_calls() -> list[tuple[str, int, str]]:
    """
    Find every ``open_in_panel(...)`` call under :mod:`wasm.cli.commands`.

    Returns:
        ``(file name, line number, path template)`` for every call whose
        first argument is a plain string or an f-string.
    """
    calls: list[tuple[str, int, str]] = []
    for path in sorted(CLI_COMMANDS_DIR.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
            if name != "open_in_panel" or not node.args:
                continue
            template = _template_of(node.args[0])
            if template is not None:
                calls.append((path.name, node.lineno, template))
    return calls


_ROUTES = [_segments(p) for p in _route_paths()]
_CALLS = _open_in_panel_calls()


@pytest.mark.parametrize(
    ("file_name", "line", "template"), _CALLS, ids=[f"{f}:{n}" for f, n, _ in _CALLS]
)
def test_every_panel_link_the_cli_builds_exists_in_the_console(
    file_name: str, line: int, template: str
) -> None:
    """A path ``open_in_panel`` builds must resolve to a route the console serves."""
    built = _segments(template)

    assert any(_route_matches(built, route) for route in _ROUTES), (
        f"{file_name}:{line} opens {template!r}, which matches no route in "
        f"{ROUTE_TREE.relative_to(REPO_ROOT)}"
    )


def test_the_route_tree_was_actually_parsed() -> None:
    """A change to the generated file's shape must not make every check above vacuous."""
    assert len(_ROUTES) > 10


def test_open_in_panel_calls_were_actually_found() -> None:
    """A rename of open_in_panel must not silently stop this file from checking anything."""
    assert len(_CALLS) >= 7
