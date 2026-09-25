#!/usr/bin/env python3
"""
Export the API's OpenAPI schema to panel/openapi.json.

``openapi_url`` stays ``None`` on the application: the schema of an API that
runs systemd as root is a map for an attacker, so it is only ever served to an
authenticated caller, at ``GET /api/openapi.json``. That leaves the schema
with no address a build step can fetch from, which is exactly what this
script is for - it builds the same application ``create_app`` builds for the
test suite and calls ``app.openapi()`` directly, in process, with no server
listening anywhere.

``panel/openapi.json`` is the only input of ``openapi-typescript``
(``npm run gen:api``), which is how the console gets types for every endpoint
instead of hand-maintained ones that drift from what the server actually
returns.

Usage:
    scripts/export_openapi.py            Write panel/openapi.json.
    scripts/export_openapi.py --check    Fail if it is out of date. CI runs this.
"""

from __future__ import annotations

import argparse
import difflib
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT = ROOT / "panel" / "openapi.json"


def _build_schema() -> dict[str, Any]:
    """
    Build the application the way the test suite does, and read its schema.

    A throwaway state directory is used so nothing this touches - secrets,
    the audit log, session storage - is left behind on disk, and a generous
    rate limit keeps a slow CI runner from tripping it while FastAPI walks
    every route to build the schema.

    Returns:
        The OpenAPI document, exactly as ``GET /api/openapi.json`` serves it.
    """
    from wasm.web.auth import SecurityConfig
    from wasm.web.server import create_app

    with tempfile.TemporaryDirectory(prefix="wasm-openapi-") as tmp_dir:
        config = SecurityConfig(state_dir=Path(tmp_dir) / "state", rate_limit_requests=5000)
        app = create_app(config)
        return app.openapi()


def _render(schema: dict[str, Any]) -> str:
    """
    Render a schema deterministically.

    Args:
        schema: The OpenAPI document.

    Returns:
        Sorted, indented JSON with a trailing newline, so the committed file
        diffs cleanly and two exports of the same API never disagree byte for
        byte.
    """
    return json.dumps(schema, sort_keys=True, indent=2) + "\n"


def export_openapi(destination: Path) -> Path:
    """
    Write the current schema to ``destination``.

    Args:
        destination: File to write. Parent directories are created.

    Returns:
        ``destination``, for chaining.
    """
    text = _render(_build_schema())
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(text, encoding="utf-8")
    return destination


def _diff_summary(current: str, fresh: str, *, path: Path) -> str:
    """
    Build a short summary of what changed, for a failed ``--check``.

    Args:
        current: Content on disk.
        fresh: Content a fresh export would write.
        path: File being compared, used in the diff header.

    Returns:
        A unified diff, capped so a full schema rewrite does not flood the log.
    """
    diff = list(
        difflib.unified_diff(
            current.splitlines(keepends=True),
            fresh.splitlines(keepends=True),
            fromfile=f"{path} (committed)",
            tofile=f"{path} (current API)",
        )
    )
    cap = 60
    if len(diff) > cap:
        diff = [*diff[:cap], f"... {len(diff) - cap} more lines\n"]
    return "".join(diff)


def main(argv: list[str] | None = None) -> int:
    """
    Command line entry point.

    Args:
        argv: Arguments, defaulting to ``sys.argv``.

    Returns:
        Process exit code: 0 on success, 1 when ``--check`` finds the
        committed file stale or missing.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="verify panel/openapi.json is current without writing it",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help="file to write (default: panel/openapi.json)",
    )
    args = parser.parse_args(argv)

    fresh = _render(_build_schema())

    if args.check:
        if not args.output.exists():
            print(f"{args.output} does not exist. Run scripts/export_openapi.py.", file=sys.stderr)
            return 1
        current = args.output.read_text(encoding="utf-8")
        if current != fresh:
            print(
                f"{args.output} is out of date. Run scripts/export_openapi.py to refresh it.",
                file=sys.stderr,
            )
            print(_diff_summary(current, fresh, path=args.output), file=sys.stderr)
            return 1
        print(f"{args.output} matches the API")
        return 0

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(fresh, encoding="utf-8")
    print(f"Wrote {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
