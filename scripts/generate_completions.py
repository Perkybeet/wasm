#!/usr/bin/env python3
"""
Generate the shell completion scripts Click provides.

They are committed rather than built, which is the point of this script
existing instead of a line in debian/rules.

Generating them during the distribution package build meant the package had to
be importable at build time, so every runtime import became a build dependency
on every distribution and every architecture. One missing entry failed all
twenty-two OBS targets at once.

Committing them costs nothing, because Click's scripts contain no command
names: each is a fixed shim that asks ``wasm`` what to complete when the
operator presses tab. They cannot drift from the command tree the way the
2,295 hand-written lines they replaced did, and a new subcommand needs no
regeneration at all. Only a Click upgrade can change them, which is what
``--check`` is for.

Usage:
    scripts/generate_completions.py            Write the scripts.
    scripts/generate_completions.py --check    Fail if they are stale. CI runs this.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
COMPLETIONS_DIR = ROOT / "src/wasm/completions"

#: Shell to the file name each distribution expects.
SHELLS = {
    "bash": "wasm.bash",
    "zsh": "_wasm",
    "fish": "wasm.fish",
}


#: The name the completion binds to. Fixed rather than taken from argv0: asking
#: the CLI through `python -m wasm` names the function after the module and
#: produces a different file from the one the installed `wasm` script produces,
#: so the check would fail depending on how it was invoked.
PROGRAM = "wasm"
COMPLETE_VAR = "_WASM_COMPLETE"


def render(shell: str) -> str:
    """
    Ask Click for a shell's completion script.

    Args:
        shell: One of bash, zsh or fish.

    Returns:
        The script, with a trailing newline.

    Raises:
        SystemExit: When Click does not know the shell.
    """
    from click.shell_completion import get_completion_class

    from wasm.cli.app import cli

    completion_class = get_completion_class(shell)
    if completion_class is None:
        raise SystemExit(f"Click does not support {shell} completion")

    source = completion_class(cli, {}, PROGRAM, COMPLETE_VAR).source()
    return source if source.endswith("\n") else source + "\n"


def main(argv: list[str] | None = None) -> int:
    """
    Command line entry point.

    Args:
        argv: Arguments, defaulting to sys.argv.

    Returns:
        Process exit code.
    """
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    parser.add_argument("--check", action="store_true", help="fail if the scripts are stale")
    args = parser.parse_args(argv)

    stale = []
    for shell, filename in SHELLS.items():
        target = COMPLETIONS_DIR / filename
        content = render(shell)

        if args.check:
            if not target.exists() or target.read_text(encoding="utf-8") != content:
                stale.append(filename)
            continue

        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        print(f"  wrote {target.relative_to(ROOT)}")

    if args.check:
        if stale:
            print("These completion scripts are out of date. Run scripts/generate_completions.py:")
            for filename in stale:
                print(f"  {filename}")
            return 1
        print("Completion scripts match what Click generates")

    return 0


if __name__ == "__main__":
    sys.exit(main())
