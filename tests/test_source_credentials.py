"""
A credential inside a source URL never reaches a command line or a message.

Older releases stored sources such as ``https://user:token@host/owner/repo``
and handed them to git as they were: ``git ls-remote -- URL``, ``git clone``,
``git remote set-url``. Everything on a command line is visible in ``ps`` to
every local user, and the same URL went into "Cannot read the branches of
{url}", which the console writes to its journal. Now the userinfo is taken
off the URL before git sees it and travels as an ``Authorization`` header in
git's environment, scoped to the scheme, host and port it was stored for.
"""

from __future__ import annotations

import base64
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from wasm.core.exceptions import SourceError
from wasm.core.runner import CommandResult, FakeRunner
from wasm.managers.source_manager import (
    GIT_NETWORK_TIMEOUT,
    SourceManager,
    redact_git_text,
    split_url_credentials,
)

TOKEN = "ghp_SuperSecretToken1234"
USER = "deployer"
CREDENTIALED = f"https://{USER}:{TOKEN}@github.com/owner/private.git"
TOKEN_ONLY = f"https://{TOKEN}@github.com/owner/private.git"
BARE = "https://github.com/owner/private.git"
HEADER_KEY = "http.https://github.com/.extraHeader"
COMMIT = "a" * 40


def _basic(user: str, password: str) -> str:
    """The header value git should be given for a user and password."""
    token = base64.b64encode(f"{user}:{password}".encode()).decode("ascii")
    return f"Authorization: Basic {token}"


def _auth_header(env: Mapping[str, str] | None) -> str | None:
    """
    Read the extra header a git call's environment configures.

    Args:
        env: The environment the call was given.

    Returns:
        The header value for :data:`HEADER_KEY`, or None when there is none.
    """
    if not env:
        return None
    count = int(env.get("GIT_CONFIG_COUNT", "0"))
    for index in range(count):
        if env.get(f"GIT_CONFIG_KEY_{index}") == HEADER_KEY:
            return env.get(f"GIT_CONFIG_VALUE_{index}")
    return None


def _assert_no_token_in_argv(runner: FakeRunner) -> None:
    """Fail when any recorded command line carries the token."""
    for argv in runner.calls:
        assert TOKEN not in " ".join(argv), f"credential on a command line: {argv}"


def _network_calls(runner: FakeRunner) -> list[tuple[tuple[str, ...], Mapping[str, str] | None]]:
    """Every git call that talks to the remote, with its environment."""
    verbs = {"clone", "fetch", "ls-remote", "pull"}
    return [
        (argv, env)
        for argv, env in zip(runner.calls, runner.envs, strict=True)
        if argv[0] == "git" and verbs & set(argv)
    ]


def _error_text(exc: SourceError) -> str:
    """Everything a SourceError would show anybody."""
    return " ".join(str(part) for part in (exc.message, exc.details, exc.output, exc) if part)


@pytest.fixture
def manager(runner: FakeRunner) -> SourceManager:
    """A verbose source manager, so every debug line is written and can be read."""
    return SourceManager(verbose=True)


@pytest.fixture(autouse=True)
def _no_operator_git_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep a developer's own GIT_CONFIG_COUNT out of these assertions."""
    monkeypatch.delenv("GIT_CONFIG_COUNT", raising=False)


# Splitting the URL ------------------------------------------------------------


class TestSplitUrlCredentials:
    def test_user_and_password_become_a_basic_header_scoped_to_the_host(self):
        url, env = split_url_credentials(CREDENTIALED)

        assert url == BARE
        assert env["GIT_CONFIG_COUNT"] == "1"
        assert env["GIT_CONFIG_KEY_0"] == HEADER_KEY
        assert env["GIT_CONFIG_VALUE_0"] == _basic(USER, TOKEN)

    def test_a_token_alone_is_the_user_with_an_empty_password(self):
        url, env = split_url_credentials(TOKEN_ONLY)

        assert url == BARE
        assert _auth_header(env) == _basic(TOKEN, "")

    def test_percent_encoding_is_decoded_the_way_git_would(self):
        url, env = split_url_credentials("https://me:p%40ss%3Aword@git.example.com/r.git")

        assert url == "https://git.example.com/r.git"
        assert env["GIT_CONFIG_VALUE_0"] == _basic("me", "p@ss:word")

    def test_the_port_is_part_of_the_scope(self):
        url, env = split_url_credentials(f"http://u:{TOKEN}@git.example.com:8443/r.git#main")

        assert url == "http://git.example.com:8443/r.git#main"
        assert env["GIT_CONFIG_KEY_0"] == "http.http://git.example.com:8443/.extraHeader"

    @pytest.mark.parametrize(
        "url",
        [
            BARE,
            "git@github.com:owner/repo.git",
            "ssh://git@github.com/owner/repo.git",
            "git://github.com/owner/repo.git",
        ],
    )
    def test_urls_without_an_http_credential_are_left_alone(self, url: str):
        assert split_url_credentials(url) == (url, {})

    def test_an_operator_git_config_count_is_extended_not_replaced(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setenv("GIT_CONFIG_COUNT", "2")

        _, env = split_url_credentials(CREDENTIALED)

        assert env["GIT_CONFIG_COUNT"] == "3"
        assert env["GIT_CONFIG_KEY_2"] == HEADER_KEY


class TestRedactGitText:
    @pytest.mark.parametrize("url", [CREDENTIALED, TOKEN_ONLY])
    def test_every_form_of_credential_is_redacted(self, url: str):
        text = f"fatal: unable to access '{url}/': The requested URL returned error: 403"

        redacted = redact_git_text(text)

        assert TOKEN not in redacted
        assert "github.com/owner/private.git" in redacted

    def test_an_ssh_user_is_not_a_secret(self):
        text = "Cloning git@github.com:owner/repo.git and ssh://git@github.com/owner/repo"
        assert redact_git_text(text) == text


# No credential in argv ----------------------------------------------------------


class TestNoCredentialInArgv:
    def test_ls_remote_of_a_branch(self, manager: SourceManager, runner: FakeRunner):
        runner.script(
            [
                "git",
                "-c",
                "protocol.ext.allow=never",
                "-c",
                "protocol.file.allow=never",
                "ls-remote",
            ],
            stdout=f"{COMMIT}\trefs/heads/main\n",
        )

        head = manager.remote_head(CREDENTIALED, "main")

        assert head.commit == COMMIT
        _assert_no_token_in_argv(runner)
        calls = _network_calls(runner)
        assert calls and all(BARE in argv for argv, _ in calls)
        assert all(_auth_header(env) == _basic(USER, TOKEN) for _, env in calls)

    def test_ls_remote_of_the_default_branch(self, manager: SourceManager, runner: FakeRunner):
        runner.script(
            [
                "git",
                "-c",
                "protocol.ext.allow=never",
                "-c",
                "protocol.file.allow=never",
                "ls-remote",
            ],
            stdout=f"ref: refs/heads/main\tHEAD\n{COMMIT}\tHEAD\n",
        )

        assert manager.remote_head(TOKEN_ONLY).branch == "main"
        _assert_no_token_in_argv(runner)
        assert all(_auth_header(env) == _basic(TOKEN, "") for _, env in _network_calls(runner))

    def test_clone(self, manager: SourceManager, runner: FakeRunner):
        manager.clone_git(CREDENTIALED + "#main", Path("/var/www/apps/x"))

        _assert_no_token_in_argv(runner)
        clone = next(argv for argv, _ in _network_calls(runner) if "clone" in argv)
        assert BARE in clone
        assert all(_auth_header(env) == _basic(USER, TOKEN) for _, env in _network_calls(runner))

    def test_forced_fetch_rewrites_the_remote_without_the_credential(
        self, manager: SourceManager, runner: FakeRunner, tmp_path: Path
    ):
        (tmp_path / ".git").mkdir()
        # The origin an older release wrote, credential and all.
        runner.script(
            ["git", "-c", "protocol.ext.allow=never", "-c", "protocol.file.allow=never", "remote"],
            stdout=CREDENTIALED + "\n",
        )

        manager.fetch(CREDENTIALED, tmp_path, force=True)

        _assert_no_token_in_argv(runner)
        set_url = next(argv for argv in runner.calls if "set-url" in argv)
        assert set_url[-1] == BARE
        fetches = _network_calls(runner)
        assert fetches and all(_auth_header(env) == _basic(USER, TOKEN) for _, env in fetches)

    def test_repository_cache_and_the_commit_lookup_after_it(
        self, manager: SourceManager, runner: FakeRunner, tmp_path: Path
    ):
        (tmp_path / ".git").mkdir()
        prefix = ["git", "-c", "protocol.ext.allow=never", "-c", "protocol.file.allow=never"]
        runner.script([*prefix, "rev-parse", "--abbrev-ref"], stdout="main\n")
        runner.script([*prefix, "rev-parse", "HEAD"], stdout=f"{COMMIT}\n")

        manager.sync_cache(CREDENTIALED, tmp_path, "main")
        # The rebuild of a commit asks the same cache, whose origin no longer
        # carries the credential; the fetch must still be authenticated.
        with pytest.raises(SourceError):
            manager.resolve_commit(tmp_path, "b" * 40)

        _assert_no_token_in_argv(runner)
        fetches = _network_calls(runner)
        assert len(fetches) >= 3
        assert all(_auth_header(env) == _basic(USER, TOKEN) for _, env in fetches)

    def test_sparse_clone_and_its_checkout(
        self, manager: SourceManager, runner: FakeRunner, tmp_path: Path
    ):
        destination = tmp_path / "checkout"

        manager.sparse_clone(CREDENTIALED, destination, patterns=["/package.json"])

        _assert_no_token_in_argv(runner)
        checkout = [
            env
            for argv, env in zip(runner.calls, runner.envs, strict=True)
            if "checkout" in argv and "sparse-checkout" not in argv
        ]
        assert checkout and _auth_header(checkout[0]) == _basic(USER, TOKEN)
        assert all(_auth_header(env) == _basic(USER, TOKEN) for _, env in _network_calls(runner))

    def test_a_url_without_credentials_gets_no_header(
        self, manager: SourceManager, runner: FakeRunner
    ):
        manager.clone_git(BARE, Path("/var/www/apps/x"))

        assert all(_auth_header(env) is None for _, env in _network_calls(runner))

    def test_the_header_does_not_follow_the_manager_to_another_repository(
        self, manager: SourceManager, runner: FakeRunner, tmp_path: Path
    ):
        manager.clone_git(CREDENTIALED, tmp_path / "one")
        runner.calls.clear()
        runner.envs.clear()

        manager.clone_git("https://gitlab.com/other/repo.git", tmp_path / "two")

        assert all(_auth_header(env) is None for _, env in _network_calls(runner))


# No credential in a message ------------------------------------------------------


class TestNoCredentialInMessages:
    def test_a_failed_ls_remote(self, manager: SourceManager, runner: FakeRunner):
        runner.script(["git"], stderr=f"fatal: unable to access '{CREDENTIALED}/'", exit_code=128)

        with pytest.raises(SourceError) as caught:
            manager.remote_head(CREDENTIALED, "main")

        assert "Cannot read the branches of" in caught.value.message
        assert TOKEN not in _error_text(caught.value)

    def test_a_refused_credential(self, manager: SourceManager, runner: FakeRunner):
        runner.script(
            ["git"],
            stderr=f"remote: Invalid username or password.\nfatal: Authentication failed for "
            f"'{CREDENTIALED}/'",
            exit_code=128,
        )

        with pytest.raises(SourceError) as caught:
            manager.remote_head(TOKEN_ONLY)

        assert TOKEN not in _error_text(caught.value)

    def test_a_missing_branch(self, manager: SourceManager, runner: FakeRunner):
        runner.script(["git"], exit_code=2)

        with pytest.raises(SourceError) as caught:
            manager.remote_head(CREDENTIALED, "gone")

        assert BARE in caught.value.message
        assert TOKEN not in _error_text(caught.value)

    def test_a_failed_clone(self, manager: SourceManager, runner: FakeRunner):
        runner.script(["git"], stderr=f"fatal: could not clone {CREDENTIALED}", exit_code=128)

        with pytest.raises(SourceError) as caught:
            manager.clone_git(CREDENTIALED, Path("/var/www/apps/x"))

        assert TOKEN not in _error_text(caught.value)

    def test_a_failed_pull_of_an_origin_written_by_an_older_release(
        self, manager: SourceManager, runner: FakeRunner, tmp_path: Path
    ):
        (tmp_path / ".git").mkdir()
        prefix = ["git", "-c", "protocol.ext.allow=never", "-c", "protocol.file.allow=never"]
        runner.script([*prefix, "remote", "get-url"], stdout=CREDENTIALED + "\n")
        runner.script(
            [*prefix, "pull"],
            stderr=f"fatal: Authentication failed for '{CREDENTIALED}/'",
            exit_code=128,
        )

        with pytest.raises(SourceError) as caught:
            manager.pull(tmp_path)

        assert TOKEN not in _error_text(caught.value)

    def test_nothing_verbose_logs_the_credential(
        self, manager: SourceManager, runner: FakeRunner, tmp_path: Path, capsys
    ):
        (tmp_path / ".git").mkdir()
        prefix = ["git", "-c", "protocol.ext.allow=never", "-c", "protocol.file.allow=never"]
        runner.script([*prefix, "remote", "get-url"], stdout=CREDENTIALED + "\n")
        runner.script([*prefix, "fetch"], stderr=f"From {CREDENTIALED}\n * branch main")
        runner.script([*prefix, "clone"], stderr=f"Cloning into '{tmp_path}'... {CREDENTIALED}")

        manager.fetch(CREDENTIALED, tmp_path, force=True)
        manager.clone_git(CREDENTIALED, tmp_path / "other")

        captured = capsys.readouterr()
        assert "github.com" in captured.out + captured.err, "verbose output was not captured"
        assert TOKEN not in captured.out + captured.err


# remote_head's deadline -----------------------------------------------------------


class TestRemoteHeadTimeout:
    def _record_timeouts(self, runner: FakeRunner, monkeypatch: pytest.MonkeyPatch) -> list[int]:
        timeouts: list[int] = []
        original = runner.run

        def run(argv: Sequence[str], **kwargs: Any) -> CommandResult:
            timeouts.append(kwargs["timeout"])
            return original(argv, **kwargs)

        monkeypatch.setattr(runner, "run", run)
        return timeouts

    def test_the_caller_can_shorten_it(
        self, manager: SourceManager, runner: FakeRunner, monkeypatch: pytest.MonkeyPatch
    ):
        timeouts = self._record_timeouts(runner, monkeypatch)
        runner.script(["git"], stdout=f"{COMMIT}\trefs/heads/main\n")

        manager.remote_head(BARE, "main", timeout=15)

        assert timeouts == [15]

    def test_it_defaults_to_the_network_timeout(
        self, manager: SourceManager, runner: FakeRunner, monkeypatch: pytest.MonkeyPatch
    ):
        timeouts = self._record_timeouts(runner, monkeypatch)
        runner.script(["git"], stdout=f"ref: refs/heads/main\tHEAD\n{COMMIT}\tHEAD\n")

        manager.remote_head(BARE)

        assert timeouts == [GIT_NETWORK_TIMEOUT]


# The forced reset keeps untracked files ----------------------------------------------


class TestForcedResetKeepsUntrackedFiles:
    def test_divergent_pull_resets_without_cleaning(
        self, manager: SourceManager, runner: FakeRunner, tmp_path: Path
    ):
        """
        uploads/ and a hand-written .env are untracked and not ignored in many
        applications; ``git clean -fd`` deleted them on every diverged update.
        """
        (tmp_path / ".git").mkdir()
        prefix = ["git", "-c", "protocol.ext.allow=never", "-c", "protocol.file.allow=never"]
        runner.script(
            [*prefix, "pull"],
            stderr="fatal: Need to specify how to reconcile divergent branches.",
            exit_code=128,
        )
        runner.script([*prefix, "rev-parse", "--abbrev-ref"], stdout="main\n")

        assert manager.pull(tmp_path) is True

        git = [argv[len(prefix) :] for argv in runner.calls if argv[0] == "git"]
        assert ("reset", "--hard", "origin/main") in git
        assert not any(argv[0] == "clean" for argv in git), git
