#!/usr/bin/env python3
"""
Compare the Click command tree against the frozen argparse surface.

``tests/contracts/cli_surface.json`` was generated from the argparse parser
before the migration and is the contract: every command, every alias and every
option that existed has to still exist, because they are in scripts, in muscle
memory and in the published documentation.

Usage:
    scripts/cli_parity.py            Check the whole tree.
    scripts/cli_parity.py site       Check one subtree, while migrating it.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import click

ROOT = Path(__file__).resolve().parent.parent
CONTRACT = ROOT / "tests/contracts/cli_surface.json"

#: Options the argparse tree declared and nothing ever read. They are not
#: carried over, and their absence is not a regression.
DROPPED_OPTIONS = frozenset()


def click_tree(command: click.Command, path: tuple[str, ...] = ()) -> dict[str, dict]:
    """
    Walk a Click command into the same shape as the contract.

    Args:
        command: Root command.
        path: Command names leading here.

    Returns:
        Mapping of space-joined command path to its options and aliases.
    """
    key = " ".join(path)
    options = sorted(
        {
            opt
            for param in command.params
            if isinstance(param, click.Option)
            for opt in param.opts + param.secondary_opts
            if opt.startswith("-") and opt not in ("-h", "--help")
        }
    )
    positionals = [p.name for p in command.params if isinstance(p, click.Argument)]
    tree = {key: {"options": options, "positionals": positionals, "aliases": []}}

    if isinstance(command, click.Group):
        ctx = click.Context(command)
        for name in command.list_commands(ctx):
            try:
                sub = command.get_command(ctx, name)
            except click.ClickException:
                # A group that has not been migrated yet. Reporting it as
                # missing is the point of running this while the work is in
                # progress.
                continue
            if sub is None:
                continue
            tree.update(click_tree(sub, path + (name,)))
    return tree


def compare(subtree: str | None) -> int:
    """
    Report differences between the contract and the current tree.

    Args:
        subtree: Only check commands under this top-level name.

    Returns:
        Process exit code.
    """
    from wasm.cli.app import ALIASES, cli

    contract = json.loads(CONTRACT.read_text(encoding="utf-8"))
    actual = click_tree(cli)

    def in_scope(key: str) -> bool:
        if subtree is None:
            return True
        return key == subtree or key.startswith(f"{subtree} ")

    problems: list[str] = []

    for key, expected in sorted(contract.items()):
        if not key or not in_scope(key):
            continue
        if key not in actual:
            problems.append(f"missing command: wasm {key}")
            continue
        missing_options = set(expected["options"]) - set(actual[key]["options"]) - DROPPED_OPTIONS
        if missing_options:
            problems.append(f"wasm {key}: options gone: {sorted(missing_options)}")
        for alias in expected["aliases"]:
            target = key.split(" ")[-1]
            if ALIASES.get(alias) != target and alias not in actual:
                problems.append(f"wasm {key}: alias '{alias}' no longer resolves")

    extra = {k for k in actual if k and in_scope(k)} - set(contract)
    if extra:
        print(f"New commands (fine, just noting): {sorted(extra)}")

    if problems:
        print(f"{len(problems)} differences from the frozen surface:\n")
        for problem in problems:
            print(f"  {problem}")
        return 1

    scope = f" under '{subtree}'" if subtree else ""
    print(f"Command surface{scope} matches the contract")
    return 0


def main(argv: list[str] | None = None) -> int:
    """
    Command line entry point.

    Args:
        argv: Arguments, defaulting to sys.argv.

    Returns:
        Process exit code.
    """
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    parser.add_argument("subtree", nargs="?", help="only check this top-level command")
    args = parser.parse_args(argv)
    return compare(args.subtree)


if __name__ == "__main__":
    sys.exit(main())
