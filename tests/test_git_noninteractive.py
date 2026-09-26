"""
Git never waits for a person.

The console's "Inspect source" on a private https repository once left
``git clone`` blocked for ten minutes: git-remote-https was asking for a
username on the terminal that had started ``wasm web start``, and the only way
out was the clone timeout. Deploys, updates, the release cache and webhooks all
run git with nobody at the keyboard, so every git WASM runs carries an
environment that makes a credential prompt fail at once, and the failure comes
back as an error that says how to give the server access.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

import pytest

from wasm.core.exceptions import SourceError
from wasm.core.runner import FakeRunner, SubprocessRunner
from wasm.deployers.helpers import preflight
from wasm.managers.source_manager import (
    GIT_AUTH_FAILURE_MESSAGE,
    SourceManager,
    git_environment,
    is_git_auth_failure,
)

HTTPS_URL = "https://github.com/owner/private-repo.git"
SSH_URL = "git@github.com:owner/private-repo.git"


def _assert_non_interactive(env: Mapping[str, str] | None) -> None:
    """
    Check that an environment makes git and ssh fail instead of prompting.

    Args:
        env: The environment a git call was given.
    """
    assert env is not None, "git ran with the caller's environment, free to prompt"
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert Path(env["GIT_ASKPASS"]).name == "false"
    assert env["SSH_ASKPASS_REQUIRE"] == "never"
    assert "BatchMode=yes" in env["GIT_SSH_COMMAND"]
    assert "StrictHostKeyChecking=accept-new" in env["GIT_SSH_COMMAND"]


def _git_calls(runner: FakeRunner) -> list[tuple[tuple[str, ...], Mapping[str, str] | None]]:
    """
    Pair every recorded git call with the environment it was given.

    Args:
        runner: The fake runner the code under test used.

    Returns:
        (argv, env) for each git call, in order.
    """
    return [
        (argv, env) for argv, env in zip(runner.calls, runner.envs, strict=True) if argv[0] == "git"
    ]


@pytest.fixture
def manager(runner: FakeRunner) -> SourceManager:
    """A source manager on the process-wide fake runner."""
    return SourceManager()


@pytest.fixture(autouse=True)
def _no_operator_ssh_command(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep a developer's own GIT_SSH_COMMAND out of these assertions."""
    monkeypatch.delenv("GIT_SSH_COMMAND", raising=False)


# The environment -----------------------------------------------------------


class TestGitEnvironment:
    def test_disables_every_way_git_and_ssh_can_prompt(self):
        _assert_non_interactive(git_environment())

    def test_the_askpass_program_exists_and_fails(self):
        askpass = Path(git_environment()["GIT_ASKPASS"])
        assert askpass.is_absolute() and os.access(askpass, os.X_OK), (
            "git runs GIT_ASKPASS by path; a missing one falls back to the terminal"
        )

    def test_an_operator_ssh_command_is_left_alone(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("GIT_SSH_COMMAND", "ssh -i /root/.ssh/deploy_key")
        assert "GIT_SSH_COMMAND" not in git_environment()

    def test_messages_are_in_english_so_they_can_be_recognised(self):
        env = git_environment()
        assert env["LC_ALL"] == "C.UTF-8"
        assert env["LANGUAGE"] == ""


# Every git network operation carries it -------------------------------------


class TestEveryGitCallIsNonInteractive:
    def test_clone(self, manager: SourceManager, runner: FakeRunner):
        manager.clone_git(HTTPS_URL, Path("/var/www/apps/x"))

        calls = _git_calls(runner)
        assert any("clone" in argv for argv, _ in calls)
        for _, env in calls:
            _assert_non_interactive(env)

    def test_forced_fetch(self, manager: SourceManager, runner: FakeRunner, tmp_path: Path):
        (tmp_path / ".git").mkdir()
        manager.fetch(HTTPS_URL, tmp_path, force=True)

        calls = _git_calls(runner)
        assert any("fetch" in argv for argv, _ in calls)
        for _, env in calls:
            _assert_non_interactive(env)

    def test_pull(self, manager: SourceManager, runner: FakeRunner, tmp_path: Path):
        (tmp_path / ".git").mkdir()
        runner.script(["git", "-c", "protocol.ext.allow=never"], exit_code=0)
        manager.pull(tmp_path, branch="main")

        calls = _git_calls(runner)
        assert any("pull" in argv for argv, _ in calls)
        for _, env in calls:
            _assert_non_interactive(env)

    def test_release_cache_first_clone(
        self, manager: SourceManager, runner: FakeRunner, tmp_path: Path
    ):
        manager.sync_cache(HTTPS_URL, tmp_path / "repo", branch="main")

        calls = _git_calls(runner)
        assert any("clone" in argv for argv, _ in calls)
        for _, env in calls:
            _assert_non_interactive(env)

    def test_release_cache_fetch(self, manager: SourceManager, runner: FakeRunner, tmp_path: Path):
        cache = tmp_path / "repo"
        (cache / ".git").mkdir(parents=True)
        runner.script(
            ["git", "-c", "protocol.ext.allow=never", "-c", "protocol.file.allow=never", "remote"],
            stdout=HTTPS_URL,
        )
        runner.script(
            [
                "git",
                "-c",
                "protocol.ext.allow=never",
                "-c",
                "protocol.file.allow=never",
                "rev-parse",
            ],
            stdout="main",
        )
        manager.sync_cache(HTTPS_URL, cache, branch="main")

        calls = _git_calls(runner)
        assert any("fetch" in argv for argv, _ in calls)
        for _, env in calls:
            _assert_non_interactive(env)

    def test_preflight_ls_remote(self, runner: FakeRunner):
        preflight.repository_unreachable(runner, HTTPS_URL)

        calls = _git_calls(runner)
        assert [argv[1] for argv, _ in calls] == ["ls-remote"]
        _assert_non_interactive(calls[0][1])

    def test_monorepo_ls_remote(self, runner: FakeRunner, monkeypatch):
        from wasm.deployers.monorepo import MonorepoDeployer

        monkeypatch.setattr("wasm.deployers.monorepo.get_store", lambda: None)
        deployer = MonorepoDeployer(runner=runner)
        deployer.source = HTTPS_URL
        deployer._pre_flight_check()

        calls = _git_calls(runner)
        assert [argv[1] for argv, _ in calls] == ["ls-remote"]
        _assert_non_interactive(calls[0][1])


# A refused credential is an actionable error, at once ----------------------

AUTH_FAILURES = [
    "Cloning into '/var/www/apps/x'...\n"
    "fatal: could not read Username for 'https://github.com': terminal prompts disabled",
    "remote: Invalid username or password.\n"
    "fatal: Authentication failed for 'https://github.com/owner/private-repo.git/'",
    "git@github.com: Permission denied (publickey).\n"
    "fatal: Could not read from remote repository.\n\n"
    "Please make sure you have the correct access rights\nand the repository exists.",
    "remote: Repository not found.\n"
    "fatal: repository 'https://github.com/owner/private-repo.git/' not found",
    "remote: HTTP Basic: Access denied\n"
    "fatal: Authentication failed for 'https://gitlab.com/owner/private-repo.git/'",
]


class TestAuthenticationFailures:
    @pytest.mark.parametrize("stderr", AUTH_FAILURES)
    def test_git_messages_are_recognised(self, stderr: str):
        assert is_git_auth_failure(stderr)

    @pytest.mark.parametrize(
        "stderr",
        [
            "fatal: unable to access 'https://github.com/o/r.git/': Could not resolve host",
            "fatal: Remote branch nope not found in upstream origin",
            "error: RPC failed; curl 56 GnuTLS recv error",
        ],
    )
    def test_other_failures_are_not_mistaken_for_credentials(self, stderr: str):
        assert not is_git_auth_failure(stderr)

    @pytest.mark.parametrize("stderr", AUTH_FAILURES)
    def test_clone_says_how_to_give_the_server_access(
        self, manager: SourceManager, runner: FakeRunner, stderr: str
    ):
        runner.script(["git"], stderr=stderr, exit_code=128)

        with pytest.raises(SourceError) as caught:
            manager.clone_git(HTTPS_URL, Path("/var/www/apps/x"))

        error = caught.value
        assert error.message == GIT_AUTH_FAILURE_MESSAGE
        assert "git@github.com:owner/private-repo.git" in error.details
        assert "wasm setup ssh" in error.details
        assert "token" in error.details
        assert error.output == stderr, "git's own words are shown verbatim, never paraphrased"

    def test_an_ssh_url_is_told_to_add_the_deploy_key(
        self, manager: SourceManager, runner: FakeRunner, monkeypatch
    ):
        monkeypatch.setattr(
            "wasm.managers.source_manager.ensure_ssh_setup", lambda *a, **k: (True, "", None)
        )
        runner.script(["git"], stderr=AUTH_FAILURES[2], exit_code=128)

        with pytest.raises(SourceError) as caught:
            manager.clone_git(SSH_URL, Path("/var/www/apps/x"))

        assert caught.value.message == GIT_AUTH_FAILURE_MESSAGE
        assert "deploy key" in caught.value.details
        assert "wasm setup ssh --show" in caught.value.details

    def test_release_cache_fetch_raises_it_too(
        self, manager: SourceManager, runner: FakeRunner, tmp_path: Path
    ):
        cache = tmp_path / "repo"
        (cache / ".git").mkdir(parents=True)
        prefix = ["git", "-c", "protocol.ext.allow=never", "-c", "protocol.file.allow=never"]
        runner.script([*prefix, "remote"], stdout=HTTPS_URL)
        runner.script([*prefix, "rev-parse"], stdout="main")
        runner.script([*prefix, "fetch"], stderr=AUTH_FAILURES[0], exit_code=128)

        with pytest.raises(SourceError) as caught:
            manager.sync_cache(HTTPS_URL, cache, branch="main")

        assert caught.value.message == GIT_AUTH_FAILURE_MESSAGE
        assert caught.value.output == AUTH_FAILURES[0]

    def test_pull_stops_at_the_refusal_instead_of_resetting(
        self, manager: SourceManager, runner: FakeRunner, tmp_path: Path
    ):
        (tmp_path / ".git").mkdir()
        prefix = ["git", "-c", "protocol.ext.allow=never", "-c", "protocol.file.allow=never"]
        runner.script([*prefix, "remote", "get-url"], stdout=HTTPS_URL)
        runner.script([*prefix, "pull"], stderr=AUTH_FAILURES[1], exit_code=1)

        with pytest.raises(SourceError) as caught:
            manager.pull(tmp_path)

        assert caught.value.message == GIT_AUTH_FAILURE_MESSAGE
        assert "git@github.com:owner/private-repo.git" in caught.value.details
        assert not any("reset" in argv for argv in runner.calls), (
            "a refused credential is not a diverged branch; resetting fixes nothing"
        )

    def test_an_unrelated_clone_failure_keeps_the_generic_error(
        self, manager: SourceManager, runner: FakeRunner
    ):
        runner.script(["git"], stderr="fatal: Could not resolve host: github.com", exit_code=128)

        with pytest.raises(SourceError) as caught:
            manager.clone_git(HTTPS_URL, Path("/var/www/apps/x"))

        assert caught.value.message != GIT_AUTH_FAILURE_MESSAGE

    def test_preflight_names_the_fix(self, runner: FakeRunner):
        runner.script(["git", "ls-remote"], exit_code=128, stderr=AUTH_FAILURES[0])

        issues = preflight.repository_unreachable(runner, HTTPS_URL)

        assert any("git@github.com:owner/private-repo.git" in issue for issue in issues)


# stdin is never the terminal ----------------------------------------------


@pytest.mark.allow_subprocess
class TestStdinIsNotInherited:
    """
    A child that inherits the terminal's stdin can sit reading it forever. Each
    test puts a pipe on this process's fd 0, so a child that inherited it would
    report a pipe rather than /dev/null.
    """

    PROBE = ["python3", "-c", "import os; print(os.path.realpath('/proc/self/fd/0'))"]

    @pytest.fixture(autouse=True)
    def _pipe_on_stdin(self):
        read_end, write_end = os.pipe()
        saved = os.dup(0)
        os.dup2(read_end, 0)
        try:
            yield
        finally:
            os.dup2(saved, 0)
            for fd in (saved, read_end, write_end):
                os.close(fd)

    def test_run(self):
        result = SubprocessRunner().run(self.PROBE, timeout=10)
        assert result.stdout.strip() == "/dev/null"

    def test_stream(self):
        lines: list[str] = []
        SubprocessRunner().stream(self.PROBE, on_line=lines.append, timeout=10)
        assert lines == ["/dev/null"]

    def test_capture_to_file(self, tmp_path: Path):
        destination = tmp_path / "out"
        SubprocessRunner().capture_to_file(self.PROBE, destination, timeout=10)
        assert destination.read_text().strip() == "/dev/null"

    def test_input_still_reaches_stdin(self):
        result = SubprocessRunner().run(
            ["python3", "-c", "import sys; print(sys.stdin.read())"], input="hello", timeout=10
        )
        assert result.stdout.strip() == "hello"
