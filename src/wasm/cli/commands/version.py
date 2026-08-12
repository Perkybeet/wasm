# Copyright (c) 2024-2025 Yago López Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
What changed in the release that is installed.

``wasm --changelog`` reads the Debian changelog that ships with the package
rather than calling home, so it answers on a server with no outbound network.
The flag lives on the root group in :mod:`wasm.cli.app`; this module only knows
how to find the file and print the entry for the running version.
"""

from __future__ import annotations

import gzip
import re
from pathlib import Path

import click

from wasm import __version__

#: Where the changelog ends up, in the order the packagings put it there: the
#: source checkout, a pip install, then a distribution package.
CHANGELOG_PATHS: tuple[Path, ...] = (
    Path(__file__).parent.parent.parent.parent.parent / "obs" / "debian.changelog",
    Path(__file__).parent.parent.parent / "obs" / "debian.changelog",
    Path("/usr/share/doc/wasm/changelog.gz"),
    Path("/usr/share/doc/wasm/changelog"),
)

#: Start of any release stanza, used to know where the current one ends.
_ANY_VERSION = re.compile(r"wasm \(\d+\.\d+\.\d+-\d+\)")


def _read_changelog() -> str | None:
    """
    Read the first changelog file that exists.

    Returns:
        The file's contents, or None if no packaging installed one or it could
        not be read.
    """
    for path in CHANGELOG_PATHS:
        if not path.exists():
            continue
        try:
            if path.suffix == ".gz":
                with gzip.open(path, "rt", encoding="utf-8") as handle:
                    return handle.read()
            return path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            # A changelog that cannot be read is a cosmetic loss, and the caller
            # falls back to the release URL.
            return None
    return None


def get_current_version_changelog() -> str | None:
    """
    Extract the changelog stanza for the running version.

    Returns:
        The stanza for :data:`wasm.__version__`, or None if the changelog is
        absent or does not mention this version.
    """
    content = _read_changelog()
    if not content:
        return None

    # Debian format: "wasm (VERSION-REVISION) ...", then the bullet list, then
    # the trailer line that starts with "--".
    version_pattern = re.compile(rf"wasm \({re.escape(__version__)}-\d+\)")

    stanza: list[str] = []
    in_version = False

    for line in content.split("\n"):
        if version_pattern.match(line):
            in_version = True
            stanza.append(line)
            continue

        if in_version:
            stanza.append(line)
            if line.strip().startswith("--") or (line.strip() and _ANY_VERSION.match(line)):
                break

    if stanza:
        return "\n".join(stanza).strip()

    return None


def show_changelog() -> None:
    """Print what changed in the installed release."""
    click.echo(f"WASM v{__version__} - Changelog\n")

    changelog = get_current_version_changelog()

    if not changelog:
        click.echo("Changelog not available locally.")
        click.echo("View release notes at:")
        click.echo(f"https://github.com/Perkybeet/wasm/releases/tag/v{__version__}\n")
        return

    for line in changelog.split("\n"):
        stripped = line.strip()
        if stripped.startswith("--") or "wasm (" in line:
            # The stanza header and the maintainer trailer frame the entry.
            click.echo(f"\n{stripped}")
        elif stripped:
            click.echo(f"  {stripped}")
    click.echo()
