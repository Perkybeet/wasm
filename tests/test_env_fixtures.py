"""
One grammar for ``.env`` files, pinned by fixtures both parsers read.

The console's "Paste .env" dialog parses what the operator pastes in the
browser (``panel/src/features/app/environment/dotenv.ts``) and the backend
reads the file on disk with :meth:`EnvManager.read_env_file`. If the two ever
disagreed, the preview the operator approved would not be what WASM reads
back. Both test suites parse every ``tests/fixtures/env/*.env`` and compare
with the ``.json`` beside it, so a change to either parser that the other
does not follow fails one of them.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from wasm.deployers.helpers.env_manager import EnvManager

FIXTURES = Path(__file__).parent / "fixtures" / "env"

CASES = sorted(FIXTURES.glob("*.env"))


def test_there_are_fixtures_to_compare() -> None:
    """An empty directory would make every parametrised case vanish silently."""
    assert len(CASES) >= 9
    for case in CASES:
        assert case.with_suffix(".json").is_file(), f"{case.name} has no expected .json"


@pytest.mark.parametrize("case", CASES, ids=[case.stem for case in CASES])
def test_env_manager_reads_each_fixture_as_pinned(case: Path) -> None:
    """
    EnvManager parses the fixture into exactly the mapping the console expects.

    Args:
        case: The ``.env`` fixture.
    """
    expected = json.loads(case.with_suffix(".json").read_text(encoding="utf-8"))

    assert EnvManager().read_env_file(case) == expected


def test_line_endings_are_kept_in_the_fixtures() -> None:
    """The CRLF fixtures only prove something while their bytes stay CRLF."""
    assert b"\r\n" in (FIXTURES / "crlf.env").read_bytes()
    assert b"\r\n" in (FIXTURES / "messy.env").read_bytes()
