# Copyright (c) 2024-2025 Yago López Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
Version and changelog display commands.
"""

import re
from pathlib import Path
from typing import Optional

from wasm import __version__


def get_current_version_changelog() -> Optional[str]:
    """
    Extract changelog for the current version from debian.changelog.

    Returns:
        Changelog text for current version, or None if not found.
    """
    try:
        # Try to find the changelog file
        # When installed, it might be in different locations
        changelog_paths = [
            # Development/source
            Path(__file__).parent.parent.parent.parent.parent / "obs" / "debian.changelog",
            # Installed via pip/setuptools
            Path(__file__).parent.parent.parent / "obs" / "debian.changelog",
            # System-wide install
            Path("/usr/share/doc/wasm/changelog.gz"),
            Path("/usr/share/doc/wasm/changelog"),
        ]

        changelog_content = None
        for changelog_path in changelog_paths:
            if changelog_path.exists():
                if changelog_path.suffix == ".gz":
                    import gzip
                    with gzip.open(changelog_path, "rt") as f:
                        changelog_content = f.read()
                else:
                    changelog_content = changelog_path.read_text()
                break

        if not changelog_content:
            return None

        # Parse debian changelog format
        # Format: wasm (VERSION-REVISION) ...; urgency=...
        #   * Change 1
        #   * Change 2
        # -- Author <email>  Date

        version_pattern = rf"wasm \({re.escape(__version__)}-\d+\)"
        lines = changelog_content.split("\n")

        changelog_lines = []
        in_version = False

        for i, line in enumerate(lines):
            if re.match(version_pattern, line):
                in_version = True
                changelog_lines.append(line)
                continue

            if in_version:
                # Stop at the author line or next version
                if line.strip().startswith("--") or (line.strip() and re.match(r"wasm \(\d+\.\d+\.\d+-\d+\)", line)):
                    changelog_lines.append(line)
                    break
                changelog_lines.append(line)

        if changelog_lines:
            return "\n".join(changelog_lines).strip()

        return None

    except Exception:
        return None


def show_changelog() -> None:
    """Display the changelog for the current version."""
    print(f"WASM v{__version__} - Changelog\n")

    changelog = get_current_version_changelog()

    if changelog:
        # Parse and format the changelog nicely
        lines = changelog.split("\n")
        for line in lines:
            if line.strip().startswith("*"):
                # Change item
                print(f"  {line.strip()}")
            elif line.strip().startswith("--"):
                # Author line
                print(f"\n{line.strip()}")
            elif "wasm (" in line:
                # Version header
                print(f"\n{line.strip()}")
            elif line.strip():
                # Other content
                print(f"  {line.strip()}")
        print()
    else:
        # Fallback: show release URL
        print("Changelog not available locally.")
        print(f"View release notes at:")
        print(f"https://github.com/Perkybeet/wasm/releases/tag/v{__version__}\n")
