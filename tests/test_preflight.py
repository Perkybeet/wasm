"""
Tests for the pre-flight checks.

The repository probe is the first thing a deploy does and the first thing an
operator sees fail, so its error message is the one that has to be true.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from wasm.core.runner import FakeRunner
from wasm.deployers.helpers import preflight


class TestRepositoryProbe:
    """`git ls-remote` has to run somewhere that exists and report what git said."""

    def test_runs_from_a_directory_that_exists(self, runner: FakeRunner, monkeypatch):
        """
        After `wasm delete` the operator's shell is often still inside the
        directory that was just removed. A child inheriting that cwd makes git
        abort with "Unable to read current working directory", which used to
        surface as an opaque "Repository not accessible" even when the
        credentials were fine.
        """
        seen: dict[str, Path | None] = {}

        def record(argv, **kwargs):
            seen["cwd"] = kwargs.get("cwd")
            return FakeRunner().run(argv)

        monkeypatch.setattr(runner, "run", record)

        preflight.repository_unreachable(runner, "git@github.com:user/repo.git")

        assert seen["cwd"] is not None, "the probe inherited the caller's working directory"
        assert seen["cwd"].exists()

    def test_reports_what_git_actually_said(self, runner: FakeRunner):
        runner.script(
            ["git", "ls-remote"],
            exit_code=128,
            stderr="ERROR: Repository not found.\nfatal: Could not read from remote repository.",
        )

        issues = preflight.repository_unreachable(runner, "git@github.com:user/gone.git")

        assert "ERROR: Repository not found." in issues[0], (
            "git's own first line beats a summary that fits half a dozen causes"
        )

    def test_points_at_the_ssh_check_when_that_is_the_problem(self, runner: FakeRunner):
        runner.script(
            ["git", "ls-remote"], exit_code=128, stderr="git@github.com: Permission denied"
        )

        issues = preflight.repository_unreachable(runner, "git@github.com:user/repo.git")

        assert any("wasm setup ssh --test" in issue for issue in issues)

    def test_a_reachable_repository_reports_nothing(self, runner: FakeRunner):
        runner.script(["git", "ls-remote"], stdout="abc123\tHEAD")

        assert preflight.repository_unreachable(runner, "https://example.com/x.git") == []

    @pytest.mark.parametrize("source", ["/var/www/local", "./relative", "~/somewhere"])
    def test_a_local_path_is_not_probed(self, runner: FakeRunner, source: str):
        assert preflight.repository_unreachable(runner, source) == []
        assert runner.calls == []
