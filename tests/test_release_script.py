"""
Tests for the release script.

This is the one script that must not break at release time, and it did: the
replacement templates used ``\\1`` back-references, so setting version 1.0.0
produced ``\\1`` followed by ``1`` and the regex engine read it as group 11.
The failure only appears for versions starting with a digit that follows a
back-reference, which is to say the first major release.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "release_script", Path(__file__).parent.parent / "scripts/release.py"
)
release = importlib.util.module_from_spec(SPEC)
# Registered before execution: the module defines a dataclass, and dataclasses
# resolves its annotations through sys.modules at class creation time.
sys.modules["release_script"] = release
SPEC.loader.exec_module(release)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """
    Build a miniature repository with every file the script rewrites.

    Args:
        tmp_path: Per-test temporary directory.
        monkeypatch: Patching helper, scoped to the test.

    Returns:
        The repository root.
    """
    (tmp_path / "src/wasm").mkdir(parents=True)
    (tmp_path / "rpm").mkdir()
    (tmp_path / "obs").mkdir()

    (tmp_path / "pyproject.toml").write_text('[project]\nname = "wasm-cli"\nversion = "0.15.8"\n')
    (tmp_path / "setup.py").write_text('setup(\n    version="0.15.8",\n)\n')
    (tmp_path / "src/wasm/__init__.py").write_text('_FALLBACK_VERSION = "0.15.8"\n')
    (tmp_path / "rpm/wasm.spec").write_text(
        "Name:           wasm-cli\nVersion:        0.15.8\n%changelog\n"
    )
    (tmp_path / "obs/wasm.dsc").write_text(
        "Version: 0.15.8-1\nFiles:\n 00000000000000000000000000000000 0 wasm-0.15.8.tar.gz\n"
    )
    (tmp_path / "obs/debian.changelog").write_text(
        "wasm (0.15.8-1) unstable; urgency=medium\n\n  * Something\n\n -- A B <a@b>  Fri, 20 Mar 2026 14:00:00 +0000\n"
    )

    monkeypatch.setattr(release, "ROOT", tmp_path)
    monkeypatch.setattr(release, "_current_tag", lambda: None)
    return tmp_path


@pytest.mark.parametrize("version", ["1.0.0", "1.2.3", "10.0.0", "0.16.0", "2.11.11"])
def test_every_file_gets_the_version(repo, version):
    """A version starting with a digit must not be read as a group reference."""
    assert release.bump(version, ["Released"]) == 0

    assert f'version = "{version}"' in (repo / "pyproject.toml").read_text()
    assert f'version="{version}"' in (repo / "setup.py").read_text()
    assert f'_FALLBACK_VERSION = "{version}"' in (repo / "src/wasm/__init__.py").read_text()
    assert f"Version:        {version}" in (repo / "rpm/wasm.spec").read_text()

    dsc = (repo / "obs/wasm.dsc").read_text()
    assert f"Version: {version}-1" in dsc
    assert f"wasm-{version}.tar.gz" in dsc


def test_the_indentation_of_setup_py_survives(repo):
    """The captured group is the indentation, and it has to come back."""
    release.bump("1.0.0", ["Released"])

    assert '    version="1.0.0",' in (repo / "setup.py").read_text()


def test_the_check_passes_after_a_bump(repo):
    """Whatever bump writes, check has to accept."""
    release.bump("1.0.0", ["Something changed"])

    assert release.check() == 0


def test_the_check_fails_on_drift(repo):
    """A file left behind is the failure this gate exists for."""
    release.bump("1.0.0", ["Something changed"])
    (repo / "setup.py").write_text('setup(\n    version="0.9.9",\n)\n')

    assert release.check() == 1


def test_a_changelog_entry_lands_at_the_top(repo):
    """Debian reads the newest entry first."""
    release.bump("1.0.0", ["First thing", "Second thing"])

    first_line = (repo / "obs/debian.changelog").read_text().splitlines()[0]
    assert first_line.startswith("wasm (1.0.0-1)")
    assert "First thing" in (repo / "obs/debian.changelog").read_text()
    assert "- First thing" in (repo / "rpm/wasm.spec").read_text()


def test_a_version_that_is_not_semver_is_refused(repo):
    """A tag like 'v1.0' would produce packages nobody can order."""
    assert release.bump("1.0", ["Released"]) == 1
    assert 'version = "0.15.8"' in (repo / "pyproject.toml").read_text()
