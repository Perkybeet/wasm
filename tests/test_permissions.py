# Copyright (c) 2024-2026 Yago López Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
Tests for the ownership hand-over helpers.

:func:`~wasm.deployers.helpers.permissions.hand_over_tree` covers a deployed
application tree and only warns on failure, because the build is already good
and an app that never writes to its own directory runs fine regardless.
:func:`~wasm.deployers.helpers.permissions.hand_over_file` exists for the
single files a restore or a database engine hands to another account - a
Redis snapshot, a staged database dump - where the caller cannot shrug off a
failure: reporting "restored" over a file still owned by root is exactly the
silent failure CLAUDE.md rule 2 exists to remove.
"""

from __future__ import annotations

import io
from pathlib import Path

from wasm.core.logger import Logger
from wasm.core.runner import FakeRunner
from wasm.deployers.helpers.permissions import hand_over_file


def test_hand_over_file_succeeds_when_both_commands_succeed(tmp_path: Path) -> None:
    """A clean chown and chmod report success, in that order."""
    runner = FakeRunner()
    target = tmp_path / "dump.rdb"
    target.write_text("x")

    ok = hand_over_file(
        target,
        user="redis",
        group="redis",
        mode=0o640,
        runner=runner,
        logger=Logger(no_color=True, stream=io.StringIO()),
    )

    assert ok is True
    assert runner.calls == [
        ("chown", "redis:redis", str(target)),
        ("chmod", "640", str(target)),
    ]


def test_hand_over_file_reports_a_failed_chown(tmp_path: Path) -> None:
    """A failed chown must not be reported as a completed hand-over."""
    runner = FakeRunner().script(["chown"], exit_code=1, stderr="chown: invalid group")
    out = io.StringIO()
    target = tmp_path / "dump.rdb"
    target.write_text("x")

    ok = hand_over_file(
        target,
        user="redis",
        group="redis",
        mode=0o640,
        runner=runner,
        logger=Logger(no_color=True, stream=out),
    )

    assert ok is False
    assert "invalid group" in out.getvalue()
    # A file still owned by the wrong account must not have its mode changed
    # as though the hand-over had gone through.
    assert not runner.ran("chmod")


def test_hand_over_file_reports_a_failed_chmod(tmp_path: Path) -> None:
    """A chown that succeeds does not hide a chmod that fails."""
    runner = FakeRunner().script(["chmod"], exit_code=1, stderr="chmod: read-only file system")
    out = io.StringIO()
    target = tmp_path / "dump.rdb"
    target.write_text("x")

    ok = hand_over_file(
        target,
        user="redis",
        group="redis",
        mode=0o640,
        runner=runner,
        logger=Logger(no_color=True, stream=out),
    )

    assert ok is False
    assert "read-only file system" in out.getvalue()
