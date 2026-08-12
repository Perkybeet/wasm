#!/usr/bin/env python3
"""
Propagate the project version from pyproject.toml to every packaging file.

The version used to live as a hand-edited literal in six places, kept in step
by a checklist in CLAUDE.md. Checklists fail, and this one already caused
corrective releases. Here ``[project].version`` in pyproject.toml is the single
source of truth and everything else is derived from it.

Usage:
    scripts/release.py --check          Verify every file agrees. Used by CI.
    scripts/release.py 1.0.0            Set the version everywhere.
    scripts/release.py 1.0.0 -m "..."   Set it and write changelog entries.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - only on 3.10
    import tomli as tomllib

ROOT = Path(__file__).resolve().parent.parent

MAINTAINER = "Yago Lopez Prado"
MAINTAINER_EMAIL = "yago.lopez.adeje@gmail.com"

SEMVER = re.compile(r"^\d+\.\d+\.\d+$")


@dataclass(frozen=True)
class Site:
    """
    One place where the version literal appears.

    Attributes:
        path: File relative to the repository root.
        pattern: Regex with a single group capturing the version.
        template: Replacement string, with ``{version}`` substituted.
        label: Human-readable name for error messages.
    """

    path: str
    pattern: str
    template: str
    label: str

    def read(self) -> str | None:
        """
        Return the version currently recorded in this file.

        Returns:
            The version string, or None when the file or pattern is missing.
        """
        file = ROOT / self.path
        if not file.exists():
            return None
        match = re.search(self.pattern, file.read_text(encoding="utf-8"), re.MULTILINE)
        return match.group(1) if match else None

    def write(self, version: str) -> bool:
        """
        Rewrite this file to carry the given version.

        Args:
            version: The version to record.

        Returns:
            True when the file changed.
        """
        file = ROOT / self.path
        text = file.read_text(encoding="utf-8")
        updated, count = re.subn(
            self.pattern, self.template.format(version=version), text, count=1, flags=re.MULTILINE
        )
        if count == 0:
            raise SystemExit(f"Could not find the version pattern in {self.path}")
        if updated == text:
            return False
        file.write_text(updated, encoding="utf-8")
        return True


SITES: tuple[Site, ...] = (
    Site(
        path="setup.py",
        pattern=r'^(\s*)version="([^"]+)"',
        template=r'\1version="{version}"',
        label="setup.py",
    ),
    Site(
        path="src/wasm/__init__.py",
        pattern=r'^_FALLBACK_VERSION = "([^"]+)"',
        template='_FALLBACK_VERSION = "{version}"',
        label="package fallback",
    ),
    Site(
        path="rpm/wasm.spec",
        pattern=r"^Version:(\s+)(\S+)",
        template=r"Version:\1{version}",
        label="RPM spec",
    ),
    Site(
        path="obs/wasm.dsc",
        pattern=r"^Version: (\S+)-\d+",
        template="Version: {version}-1",
        label="Debian source control",
    ),
    Site(
        path="obs/wasm.dsc",
        pattern=r"^ (\w{32}) (\d+) wasm-(\S+)\.tar\.gz",
        template=r" \1 \2 wasm-{version}.tar.gz",
        label="Debian source tarball name",
    ),
)

# setup.py's pattern has the indentation as group 1, so the version is group 2.
_GROUP_OVERRIDES = {"setup.py": 2, "rpm/wasm.spec": 2}
_TARBALL_GROUP = 3


def _read_version(site: Site) -> str | None:
    """
    Read a version from a site, accounting for patterns with extra groups.

    Args:
        site: The site to inspect.

    Returns:
        The recorded version, or None when it cannot be found.
    """
    file = ROOT / site.path
    if not file.exists():
        return None
    match = re.search(site.pattern, file.read_text(encoding="utf-8"), re.MULTILINE)
    if not match:
        return None
    if site.label == "Debian source tarball name":
        return match.group(_TARBALL_GROUP)
    return match.group(_GROUP_OVERRIDES.get(site.path, 1))


def source_of_truth() -> str:
    """
    Return the version declared in pyproject.toml.

    Returns:
        The canonical project version.
    """
    with (ROOT / "pyproject.toml").open("rb") as handle:
        return tomllib.load(handle)["project"]["version"]


def check() -> int:
    """
    Verify that every packaging file agrees with pyproject.toml.

    Returns:
        Process exit code: 0 when consistent, 1 otherwise.
    """
    expected = source_of_truth()
    problems: list[str] = []

    for site in SITES:
        found = _read_version(site)
        if found is None:
            problems.append(f"{site.label} ({site.path}): version not found")
        elif found != expected:
            problems.append(f"{site.label} ({site.path}): {found}, expected {expected}")

    changelog = ROOT / "obs/debian.changelog"
    if changelog.exists():
        first = changelog.read_text(encoding="utf-8").splitlines()[0]
        match = re.match(r"wasm \((\S+?)-\d+\)", first)
        if not match:
            problems.append("obs/debian.changelog: top entry is not parseable")
        elif match.group(1) != expected:
            problems.append(
                f"obs/debian.changelog: top entry is {match.group(1)}, expected {expected}"
            )

    spec = ROOT / "rpm/wasm.spec"
    if spec.exists() and f"- {expected}-1" not in spec.read_text(encoding="utf-8"):
        problems.append(f"rpm/wasm.spec: no %changelog entry for {expected}")

    tag = _current_tag()
    if tag and tag.lstrip("v") != expected:
        problems.append(f"git tag {tag} does not match pyproject version {expected}")

    if problems:
        print(f"Version inconsistency (pyproject.toml says {expected}):")
        for problem in problems:
            print(f"  - {problem}")
        return 1

    print(f"All packaging files agree on version {expected}")
    return 0


def _current_tag() -> str | None:
    """
    Return the tag pointing at HEAD, if any.

    Returns:
        The tag name, or None when HEAD is not tagged or git is unavailable.
    """
    try:
        result = subprocess.run(  # noqa: S603 - fixed argv, developer tool
            ["git", "describe", "--exact-match", "--tags", "HEAD"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() or None


def _prepend_debian_changelog(version: str, entries: list[str]) -> None:
    """
    Add a Debian changelog entry at the top of the file.

    Args:
        version: The version being released.
        entries: Bullet lines describing the release.
    """
    changelog = ROOT / "obs/debian.changelog"
    stamp = datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S +0000")
    body = "\n".join(f"  * {entry}" for entry in entries)
    block = (
        f"wasm ({version}-1) unstable; urgency=medium\n\n"
        f"{body}\n\n"
        f" -- {MAINTAINER} <{MAINTAINER_EMAIL}>  {stamp}\n\n"
    )
    changelog.write_text(block + changelog.read_text(encoding="utf-8"), encoding="utf-8")


def _prepend_rpm_changelog(version: str, entries: list[str]) -> None:
    """
    Add an RPM %changelog entry directly below the %changelog directive.

    Args:
        version: The version being released.
        entries: Bullet lines describing the release.
    """
    spec = ROOT / "rpm/wasm.spec"
    text = spec.read_text(encoding="utf-8")
    stamp = datetime.now(timezone.utc).strftime("%a %b %d %Y")
    body = "\n".join(f"- {entry}" for entry in entries)
    block = f"%changelog\n* {stamp} {MAINTAINER} <{MAINTAINER_EMAIL}> - {version}-1\n{body}\n"
    spec.write_text(text.replace("%changelog\n", block, 1), encoding="utf-8")


def bump(version: str, entries: list[str]) -> int:
    """
    Set the version everywhere and optionally write changelog entries.

    Args:
        version: The new version.
        entries: Bullet lines for the changelogs. Empty to skip them.

    Returns:
        Process exit code.
    """
    if not SEMVER.match(version):
        print(f"Version must look like X.Y.Z, got {version!r}")
        return 1

    pyproject = ROOT / "pyproject.toml"
    text = pyproject.read_text(encoding="utf-8")
    updated, count = re.subn(
        r'^version = "[^"]+"', f'version = "{version}"', text, count=1, flags=re.MULTILINE
    )
    if count == 0:
        print("Could not find the version field in pyproject.toml")
        return 1
    pyproject.write_text(updated, encoding="utf-8")

    for site in SITES:
        if site.write(version):
            print(f"  updated {site.label}")

    if entries:
        _prepend_debian_changelog(version, entries)
        _prepend_rpm_changelog(version, entries)
        print("  updated changelogs")

    print(f"\nVersion set to {version}. Now:")
    print(f"  git commit -am 'v{version}: <summary>'")
    print(f"  git tag -a v{version} -m 'Release v{version}'")
    print(f"  git push && git push origin v{version}")
    return check()


def main(argv: list[str] | None = None) -> int:
    """
    Command line entry point.

    Args:
        argv: Arguments, defaulting to sys.argv.

    Returns:
        Process exit code.
    """
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    parser.add_argument("version", nargs="?", help="new version, as X.Y.Z")
    parser.add_argument(
        "--check", action="store_true", help="verify consistency without writing"
    )
    parser.add_argument(
        "-m",
        "--message",
        action="append",
        default=[],
        help="changelog bullet, repeatable",
    )
    args = parser.parse_args(argv)

    if args.check or not args.version:
        return check()
    return bump(args.version, args.message)


if __name__ == "__main__":
    raise SystemExit(main())
