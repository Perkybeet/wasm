# Copyright (c) 2024-2026 Yago Lopez Prado
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Tests for "rebuild this exact commit" and "nothing new to deploy".

The deployment page's Redeploy used to pull the head of the branch, which is
an update, not a redeploy of what the page shows. An update now takes a
commit, and the console and the CLI ask the remote, before rebuilding,
whether the branch has anything the live build does not. What is pinned:

- **The git side goes through SourceManager**, with argv the tests read and
  the non-interactive environment: a commit is resolved locally first and
  fetched only when missing (a full id by name, an abbreviation by fetching
  the history, unshallowing a shallow clone); an ambiguous or unknown id is
  an error that says what to do. ``ls-remote`` answers the head of a branch
  without downloading anything.
- **In place**, the checkout is detached at the commit, the branch it was on
  is remembered, and the next plain pull goes back to it.
- **On releases**, a release of that commit on disk and not active is
  activated (nothing is built); otherwise the commit is exported from the
  repository cache into a new release and built.
- **The upstream check** compares the remote head with the live commit on
  either layout, and stays out of the way when it cannot tell.
"""

# The pipeline fixtures are imported rather than replicated, so there stays
# one definition of the fake machine.
# ruff: noqa: F811

from __future__ import annotations

import os
import shutil
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from tests.test_lifecycle import (
    DOMAIN as INPLACE_DOMAIN,
)
from tests.test_lifecycle import (
    FakeDeployer,
    Recorder,
    make_app,
)
from tests.test_lifecycle import store as inplace_store  # noqa: F401
from tests.test_release_activation import two_releases  # noqa: F401
from tests.test_release_pipeline import (  # noqa: F401
    DOMAIN,
    GIT_URL,
    GOOD_SERVER,
    FakeGit,
    active_id,
    machine,
    node_tree,
    root,
    store,
)
from wasm.core.exceptions import SourceError, ValidationError
from wasm.core.logger import Logger
from wasm.core.runner import CommandResult, FakeRunner
from wasm.core.store import WASMStore
from wasm.deployers import lifecycle
from wasm.managers.source_manager import (
    FOLLOW_BRANCH_KEY,
    GIT_AUTH_FAILURE_MESSAGE,
    RemoteHead,
    SourceManager,
)

#: What every git WASM runs starts with.
GIT = ("git", "-c", "protocol.ext.allow=never", "-c", "protocol.file.allow=never")

FULL = "0123456789abcdef0123456789abcdef01234567"


# ---------------------------------------------------------------------------
# A git that answers per subcommand
# ---------------------------------------------------------------------------


class GitRunner(FakeRunner):
    """
    A FakeRunner whose git answers come from a function of the arguments.

    Attributes:
        answer: Called with the arguments after ``git`` and its safe config;
            returns ``(exit_code, stdout, stderr)`` or None for the default.
    """

    def __init__(self, answer: Callable[[tuple[str, ...]], tuple[int, str, str] | None]) -> None:
        super().__init__()
        self.answer = answer

    def run(self, argv: Any, **kwargs: Any) -> CommandResult:
        result = super().run(argv, **kwargs)
        if tuple(argv[: len(GIT)]) == GIT:
            custom = self.answer(tuple(argv[len(GIT) :]))
            if custom is not None:
                code, out, err = custom
                return CommandResult(argv=tuple(argv), exit_code=code, stdout=out, stderr=err)
        return result

    def git(self) -> list[tuple[str, ...]]:
        """Every git call, without the common prefix."""
        return [call[len(GIT) :] for call in self.calls if call[: len(GIT)] == GIT]


def git_envs(runner: GitRunner) -> list[Any]:
    """The environment of every git call."""
    return [
        env for call, env in zip(runner.calls, runner.envs, strict=True) if call[: len(GIT)] == GIT
    ]


# ---------------------------------------------------------------------------
# SourceManager: resolving and checking out a commit
# ---------------------------------------------------------------------------


def test_a_commit_the_clone_has_is_resolved_without_the_network(tmp_path: Path) -> None:
    """rev-parse answers; nothing is fetched."""

    def answer(args: tuple[str, ...]) -> tuple[int, str, str] | None:
        if args[:2] == ("rev-parse", "--verify"):
            return 0, FULL + "\n", ""
        return None

    runner = GitRunner(answer)
    assert SourceManager(runner=runner).resolve_commit(tmp_path, "0123ABC") == FULL
    assert ("rev-parse", "--verify", "0123abc^{commit}") in runner.git()
    assert not any(call[0] == "fetch" for call in runner.git())


def test_an_abbreviation_missing_from_a_shallow_clone_fetches_the_history(
    tmp_path: Path,
) -> None:
    """Only the client expands an abbreviation: every branch, unshallowed."""
    fetched: list[bool] = []

    def answer(args: tuple[str, ...]) -> tuple[int, str, str] | None:
        if args[:2] == ("rev-parse", "--verify"):
            return (0, FULL + "\n", "") if fetched else (128, "", "fatal: Needed a single revision")
        if args == ("rev-parse", "--is-shallow-repository"):
            return 0, "true\n", ""
        if args[0] == "fetch":
            fetched.append(True)
        return None

    runner = GitRunner(answer)
    assert SourceManager(runner=runner).resolve_commit(tmp_path, "0123abc") == FULL
    assert (
        "fetch",
        "--unshallow",
        "--tags",
        "origin",
        "+refs/heads/*:refs/remotes/origin/*",
    ) in runner.git()
    # An abbreviation is never asked of the server by name.
    assert ("fetch", "origin", "0123abc") not in runner.git()
    assert all(env["GIT_TERMINAL_PROMPT"] == "0" for env in git_envs(runner))


def test_a_full_id_is_fetched_by_name_first(tmp_path: Path) -> None:
    """Every forge serves a commit asked for by its full id: one small fetch."""
    fetched: list[tuple[str, ...]] = []

    def answer(args: tuple[str, ...]) -> tuple[int, str, str] | None:
        if args[:2] == ("rev-parse", "--verify"):
            return (0, FULL + "\n", "") if fetched else (128, "", "fatal: bad revision")
        if args[0] == "fetch":
            fetched.append(args)
        return None

    runner = GitRunner(answer)
    assert SourceManager(runner=runner).resolve_commit(tmp_path, FULL) == FULL
    assert fetched == [("fetch", "origin", FULL)]


def test_an_ambiguous_abbreviation_asks_for_more_characters(tmp_path: Path) -> None:
    """Exactly one commit, or an error; never whichever git picked."""

    def answer(args: tuple[str, ...]) -> tuple[int, str, str] | None:
        if args[:2] == ("rev-parse", "--verify"):
            return 128, "", "error: short object ID 0123 is ambiguous\n"
        return None

    with pytest.raises(SourceError, match="more than one commit") as failure:
        SourceManager(runner=GitRunner(answer)).resolve_commit(tmp_path, "0123")
    assert "more characters" in failure.value.details


def test_a_commit_nowhere_in_the_repository_is_an_error(tmp_path: Path) -> None:
    """Fetched and still unknown: said so, with what it may mean."""

    def answer(args: tuple[str, ...]) -> tuple[int, str, str] | None:
        if args[:2] == ("rev-parse", "--verify"):
            return 128, "", "fatal: Needed a single revision"
        return None

    with pytest.raises(SourceError, match="does not exist in the repository"):
        SourceManager(runner=GitRunner(answer)).resolve_commit(tmp_path, "deadbeef")


@pytest.mark.parametrize("bad", ["", "abc", "HEAD", "--output=/etc/x", "main", "0123abcg"])
def test_something_that_is_not_a_commit_id_never_reaches_git(tmp_path: Path, bad: str) -> None:
    """A commit becomes an argument; only hex can, so nothing else is tried."""
    runner = GitRunner(lambda args: None)
    with pytest.raises(SourceError, match="Not a commit id"):
        SourceManager(runner=runner).resolve_commit(tmp_path, bad)
    assert runner.git() == []


def test_checking_out_a_commit_detaches_and_remembers_the_branch(tmp_path: Path) -> None:
    """Detached, forced over tracked files only, the branch kept for the next pull."""
    (tmp_path / ".git").mkdir()

    def answer(args: tuple[str, ...]) -> tuple[int, str, str] | None:
        if args[:2] == ("rev-parse", "--verify"):
            return 0, FULL + "\n", ""
        if args[:3] == ("symbolic-ref", "--quiet", "--short"):
            return 0, "main\n", ""
        return None

    runner = GitRunner(answer)
    assert SourceManager(runner=runner).checkout_commit(tmp_path, "0123456") == FULL

    calls = runner.git()
    remember = calls.index(("config", FOLLOW_BRANCH_KEY, "main"))
    checkout = calls.index(("checkout", "--force", "--detach", FULL))
    assert remember < checkout
    assert not any(call[0] in {"clean", "reset"} for call in calls)


def test_a_second_rebuild_keeps_the_branch_the_first_remembered(tmp_path: Path) -> None:
    """Already detached, there is no branch to remember; the first one stands."""
    (tmp_path / ".git").mkdir()

    def answer(args: tuple[str, ...]) -> tuple[int, str, str] | None:
        if args[:2] == ("rev-parse", "--verify"):
            return 0, FULL + "\n", ""
        if args[0] == "symbolic-ref":
            return 1, "", ""
        return None

    runner = GitRunner(answer)
    SourceManager(runner=runner).checkout_commit(tmp_path, FULL)
    assert not any(call[0] == "config" and FOLLOW_BRANCH_KEY in call for call in runner.git())


def test_a_plain_pull_on_a_detached_checkout_goes_back_to_the_branch(tmp_path: Path) -> None:
    """The next update without a commit follows the branch again."""
    (tmp_path / ".git").mkdir()

    def answer(args: tuple[str, ...]) -> tuple[int, str, str] | None:
        if args == ("symbolic-ref", "--quiet", "HEAD"):
            return 1, "", ""
        if args == ("config", "--get", FOLLOW_BRANCH_KEY):
            return 0, "production\n", ""
        return None

    runner = GitRunner(answer)
    assert SourceManager(runner=runner).pull(tmp_path) is True

    calls = runner.git()
    assert calls.index(("checkout", "production")) < calls.index(("pull", "--rebase"))


def test_a_detached_checkout_with_nothing_remembered_follows_the_default_branch(
    tmp_path: Path,
) -> None:
    """origin/HEAD, the branch a clone starts on, is the fallback."""
    (tmp_path / ".git").mkdir()

    def answer(args: tuple[str, ...]) -> tuple[int, str, str] | None:
        if args == ("symbolic-ref", "--quiet", "HEAD"):
            return 1, "", ""
        if args == ("config", "--get", FOLLOW_BRANCH_KEY):
            return 1, "", ""
        if args == ("symbolic-ref", "--short", "refs/remotes/origin/HEAD"):
            return 0, "origin/main\n", ""
        return None

    runner = GitRunner(answer)
    SourceManager(runner=runner).pull(tmp_path)
    assert ("checkout", "main") in runner.git()


def test_a_detached_checkout_reports_the_branch_it_follows(tmp_path: Path) -> None:
    """The history records the branch, not the word HEAD."""
    (tmp_path / ".git").mkdir()

    def answer(args: tuple[str, ...]) -> tuple[int, str, str] | None:
        if args == ("rev-parse", "--abbrev-ref", "HEAD"):
            return 0, "HEAD\n", ""
        if args == ("config", "--get", FOLLOW_BRANCH_KEY):
            return 0, "main\n", ""
        return None

    info = SourceManager(runner=GitRunner(answer)).get_repo_info(tmp_path)
    assert info["branch"] == "main"


# ---------------------------------------------------------------------------
# SourceManager: the head of a remote branch
# ---------------------------------------------------------------------------


def test_the_head_of_a_branch_is_read_with_ls_remote(tmp_path: Path) -> None:
    """No clone, no fetch: one ls-remote, non-interactive."""

    def answer(args: tuple[str, ...]) -> tuple[int, str, str] | None:
        if args[0] == "ls-remote":
            return 0, f"{FULL}\trefs/heads/main\n", ""
        return None

    runner = GitRunner(answer)
    head = SourceManager(runner=runner).remote_head(GIT_URL, "main")

    assert head == RemoteHead(branch="main", commit=FULL)
    assert runner.git() == [("ls-remote", "--exit-code", "--", GIT_URL, "refs/heads/main")]
    assert git_envs(runner)[0]["GIT_TERMINAL_PROMPT"] == "0"


def test_the_default_branch_is_read_from_the_remote_head(tmp_path: Path) -> None:
    """Without a branch, --symref names the default one and its commit."""

    def answer(args: tuple[str, ...]) -> tuple[int, str, str] | None:
        if args[0] == "ls-remote":
            return 0, f"ref: refs/heads/trunk\tHEAD\n{FULL}\tHEAD\n", ""
        return None

    runner = GitRunner(answer)
    head = SourceManager(runner=runner).remote_head(GIT_URL)
    assert head == RemoteHead(branch="trunk", commit=FULL)
    assert runner.git() == [("ls-remote", "--symref", "--", GIT_URL, "HEAD")]


def test_a_branch_the_remote_does_not_have_is_named(tmp_path: Path) -> None:
    """--exit-code's 2 is "no such ref", not a network failure."""

    def answer(args: tuple[str, ...]) -> tuple[int, str, str] | None:
        return (2, "", "") if args[0] == "ls-remote" else None

    with pytest.raises(SourceError, match="Branch gone does not exist"):
        SourceManager(runner=GitRunner(answer)).remote_head(GIT_URL, "gone")


def test_a_refused_credential_says_how_to_grant_access(tmp_path: Path) -> None:
    """The same actionable error as a clone of a private repository."""

    def answer(args: tuple[str, ...]) -> tuple[int, str, str] | None:
        if args[0] == "ls-remote":
            return 128, "", "fatal: could not read Username for 'https://github.com'"
        return None

    with pytest.raises(SourceError) as failure:
        SourceManager(runner=GitRunner(answer)).remote_head(GIT_URL, "main")
    assert failure.value.message == GIT_AUTH_FAILURE_MESSAGE


def test_a_local_directory_has_no_remote_head(tmp_path: Path) -> None:
    """Not git: refused before anything runs."""
    runner = GitRunner(lambda args: None)
    with pytest.raises(SourceError, match="Not a git source"):
        SourceManager(runner=runner).remote_head(str(tmp_path))
    assert runner.git() == []


# ---------------------------------------------------------------------------
# In place: update_app with a commit
# ---------------------------------------------------------------------------


@pytest.fixture
def inplace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, inplace_store: WASMStore) -> Any:
    """An in-place application, and every collaborator of its update faked."""
    rec = Recorder()
    app_path = tmp_path / "example-com"
    make_app(inplace_store, app_path)

    def checkout_commit(path: Path, commit: str) -> str:
        rec.calls.append(("checkout", path, commit))
        return FULL

    monkeypatch.setattr(
        lifecycle,
        "RollbackManager",
        lambda verbose=False: SimpleNamespace(
            create_pre_deploy_backup=lambda **kw: rec.calls.append(("backup", kw["domain"]))
        ),
    )
    monkeypatch.setattr(
        lifecycle,
        "SourceManager",
        lambda verbose=False: SimpleNamespace(
            pull=lambda path, branch=None: rec.calls.append(("pull", path, branch)),
            checkout_commit=checkout_commit,
        ),
    )
    monkeypatch.setattr(
        lifecycle,
        "ServiceManager",
        lambda verbose=False: SimpleNamespace(
            get_status=lambda name: {"exists": True, "active": True},
            restart=lambda name: rec.calls.append(("restart", name)),
        ),
    )
    monkeypatch.setattr(lifecycle.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(
        lifecycle, "get_deployer", lambda app_type, verbose=False: FakeDeployer(rec)
    )
    return SimpleNamespace(recorder=rec, app_path=app_path)


def test_in_place_a_commit_is_checked_out_instead_of_pulled(inplace: Any) -> None:
    """Backup, the commit, the rebuild, the restart: the same update otherwise."""
    lifecycle.update_app(INPLACE_DOMAIN, commit="0123ABC")

    assert inplace.recorder.calls == [
        ("backup", INPLACE_DOMAIN),
        ("checkout", inplace.app_path, "0123abc"),
        ("update",),
        ("restart", "example-com"),
    ]


def test_in_place_a_tree_without_history_is_refused_before_the_backup(
    inplace: Any,
) -> None:
    """A directory deployed from a path has no commit to go to."""
    shutil.rmtree(inplace.app_path / ".git")

    with pytest.raises(SourceError, match="not a git checkout"):
        lifecycle.update_app(INPLACE_DOMAIN, commit="0123abc")
    assert inplace.recorder.calls == []


@pytest.mark.parametrize("extra", [{"source": GIT_URL}, {"branch": "main"}])
def test_a_commit_with_a_source_or_a_branch_is_refused(inplace: Any, extra: dict[str, str]) -> None:
    """A commit says exactly what to build; anything else would be ignored silently."""
    with pytest.raises(ValidationError, match="does not apply"):
        lifecycle.update_app(INPLACE_DOMAIN, commit="0123abc", **extra)
    assert inplace.recorder.calls == []


# ---------------------------------------------------------------------------
# Releases: update_app with a commit
# ---------------------------------------------------------------------------


class HistoryGit(FakeGit):
    """FakeGit that resolves any commit it published and answers ls-remote."""

    def __init__(self) -> None:
        super().__init__()
        self.timeouts: list[int | None] = []

    def resolve_commit(self, repository: Path, commit: str) -> str:
        self.calls.append(("resolve", commit))
        matches = [c for c in self.commits if c.startswith(commit.lower())]
        if len(matches) != 1:
            raise SourceError(f"Commit {commit} does not exist in the repository")
        return matches[0]

    def remote_head(
        self, source: str, branch: str | None = None, *, timeout: int | None = None
    ) -> RemoteHead:
        self.calls.append(("ls-remote", source, branch))
        self.timeouts.append(timeout)
        assert self.head is not None
        return RemoteHead(branch=branch or "main", commit=self.head)


@pytest.fixture
def git() -> HistoryGit:
    """The fake repository, with history."""
    return HistoryGit()


def commit_of(machine: SimpleNamespace, release_id: str) -> str:
    """The full commit a release was built from."""
    short = release_id.split("-")[2]
    return next(c for c in machine.git.commits if c.startswith(short))


def test_a_commit_whose_release_is_on_disk_is_activated_not_built(
    root: Path,
    store: WASMStore,
    machine: SimpleNamespace,
    two_releases: tuple[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Instant: current moves, the gate passes, nothing is installed or built."""
    first, second = two_releases
    runs_before = list(machine.runner.calls)
    git_before = len(machine.git.calls)
    releases_before = sorted(p.name for p in (root / "releases").iterdir())

    outcome = lifecycle.update_app(DOMAIN, commit=commit_of(machine, first)[:9], trigger="panel")

    assert active_id(root) == first
    assert machine.runner.calls == runs_before, "an activation runs no install or build"
    assert sorted(p.name for p in (root / "releases").iterdir()) == releases_before
    assert [c[0] for c in machine.git.calls[git_before:]] == ["resolve"]
    history = store.list_deployments(DOMAIN)
    assert history[0].id == outcome.deployment_id
    assert (history[0].status, history[0].triggered_by) == ("success", "panel")
    assert history[0].git_commit == first.split("-")[2]
    assert outcome.active and outcome.restarted == ("rel-example-com",)


def test_the_commit_that_is_live_is_built_again_as_a_new_release(
    root: Path,
    store: WASMStore,
    machine: SimpleNamespace,
    two_releases: tuple[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rebuilding what serves is a real build: the environment may have changed."""
    first, second = two_releases
    full = commit_of(machine, second)
    exports_before = [c for c in machine.git.calls if c[0] == "export"]

    lifecycle.update_app(DOMAIN, commit=full[:7])

    rebuilt = active_id(root)
    assert rebuilt not in {first, second}
    assert rebuilt.split("-")[2] == full[:7]
    exports = [c for c in machine.git.calls if c[0] == "export"]
    assert len(exports) == len(exports_before) + 1
    assert exports[-1][1] == full
    # The recorded subject is the rebuilt commit's, not the cache's HEAD's.
    assert ("git", "log", "-1", "--format=%s", full) in [c[:5] for c in machine.runner.calls]


def test_a_commit_whose_release_was_pruned_is_built_from_the_cache(
    tmp_path: Path,
    root: Path,
    store: WASMStore,
    machine: SimpleNamespace,
    two_releases: tuple[str, str],
) -> None:
    """The cache has the commit; it is exported without moving the branch."""
    first, second = two_releases
    shutil.rmtree(root / "releases" / first)
    machine.git.publish(node_tree(tmp_path / "v3", server=GOOD_SERVER + "// v3\n"))
    syncs_before = [c for c in machine.git.calls if c[0] == "sync"]

    lifecycle.update_app(DOMAIN, commit=first.split("-")[2])

    rebuilt = active_id(root)
    assert rebuilt.split("-")[2] == first.split("-")[2]
    assert rebuilt != first
    # The cache existed: it was not synced to the head, which is v3 now.
    assert [c for c in machine.git.calls if c[0] == "sync"] == syncs_before
    assert (root / "current" / "server.js").read_text() == GOOD_SERVER


def test_an_unknown_commit_fails_before_anything_changes(
    root: Path, store: WASMStore, machine: SimpleNamespace, two_releases: tuple[str, str]
) -> None:
    """Nothing activated, nothing staged."""
    first, second = two_releases
    releases_before = sorted(p.name for p in (root / "releases").iterdir())

    with pytest.raises(SourceError, match="does not exist"):
        lifecycle.update_app(DOMAIN, commit="ffffffff")

    assert active_id(root) == second
    assert sorted(p.name for p in (root / "releases").iterdir()) == releases_before


def test_a_release_app_from_a_directory_has_no_commit_to_rebuild(
    tmp_path: Path, store: WASMStore, machine: SimpleNamespace, two_releases: tuple[str, str]
) -> None:
    """A local source is refused, with how to update it instead."""
    app = store.get_app(DOMAIN)
    assert app is not None
    app.source = str(tmp_path)
    store.update_app(app)

    with pytest.raises(SourceError, match="not deployed from git"):
        lifecycle.update_app(DOMAIN, commit="0123abc")


# ---------------------------------------------------------------------------
# Is there anything new
# ---------------------------------------------------------------------------


def test_releases_with_nothing_new_compare_equal(
    store: WASMStore, machine: SimpleNamespace, two_releases: tuple[str, str]
) -> None:
    """The branch head is the active release's commit."""
    first, second = two_releases

    upstream = lifecycle.check_upstream(DOMAIN)

    assert upstream is not None
    assert not upstream.has_new_commits
    assert upstream.summary == (
        f"No new commits on main since {second.split('-')[2]}, which is live"
    )
    assert ("ls-remote", GIT_URL, "main") in machine.git.calls


def test_releases_with_a_new_commit_compare_different(
    tmp_path: Path, store: WASMStore, machine: SimpleNamespace, two_releases: tuple[str, str]
) -> None:
    """A push since the live release is news."""
    machine.git.publish(node_tree(tmp_path / "v3", server=GOOD_SERVER + "// v3\n"))

    upstream = lifecycle.check_upstream(DOMAIN)

    assert upstream is not None and upstream.has_new_commits


def test_releases_after_a_rollback_have_something_new(
    store: WASMStore, machine: SimpleNamespace, two_releases: tuple[str, str]
) -> None:
    """Live is the older release again, so the head of the branch is news."""
    lifecycle.activate_release(DOMAIN)

    upstream = lifecycle.check_upstream(DOMAIN)

    assert upstream is not None and upstream.has_new_commits


def inplace_git(
    monkeypatch: pytest.MonkeyPatch, *, commit: str | None, remote: Any
) -> list[tuple[Any, ...]]:
    """Fake the SourceManager the in-place check asks."""
    asked: list[tuple[Any, ...]] = []

    def remote_head(
        source: str, branch: str | None = None, *, timeout: int | None = None
    ) -> RemoteHead:
        asked.append((source, branch, timeout))
        if isinstance(remote, Exception):
            raise remote
        return RemoteHead(branch=branch or "main", commit=remote)

    monkeypatch.setattr(
        lifecycle,
        "SourceManager",
        lambda verbose=False: SimpleNamespace(
            get_repo_info=lambda path: {"commit": commit, "branch": "main", "remote": GIT_URL},
            remote_head=remote_head,
        ),
    )
    return asked


def test_in_place_with_nothing_new_compares_the_checkout(
    tmp_path: Path, inplace_store: WASMStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """HEAD of the checkout against the head of its branch."""
    make_app(inplace_store, tmp_path / "example-com")
    asked = inplace_git(monkeypatch, commit=FULL[:7], remote=FULL)

    upstream = lifecycle.check_upstream(INPLACE_DOMAIN)

    assert upstream is not None and not upstream.has_new_commits
    assert asked == [("https://github.com/example/app.git", "main", lifecycle.UPSTREAM_TIMEOUT)]


def test_in_place_a_tree_that_is_not_a_checkout_is_not_compared(
    tmp_path: Path, inplace_store: WASMStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A local directory source: nothing to ask, the update goes ahead."""
    make_app(inplace_store, tmp_path / "example-com")
    shutil.rmtree(tmp_path / "example-com" / ".git")
    asked = inplace_git(monkeypatch, commit=FULL[:7], remote=FULL)

    assert lifecycle.check_upstream(INPLACE_DOMAIN) is None
    assert asked == []


def test_a_remote_that_cannot_be_asked_is_reported_and_skipped(
    tmp_path: Path, inplace_store: WASMStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The check is advice; the update itself will say what is wrong."""
    make_app(inplace_store, tmp_path / "example-com")
    inplace_git(monkeypatch, commit=FULL[:7], remote=SourceError("Cannot read the branches"))
    warnings: list[str] = []
    logger = Logger()
    monkeypatch.setattr(logger, "warning", warnings.append)

    assert lifecycle.check_upstream(INPLACE_DOMAIN, logger=logger) is None
    assert warnings and "Cannot read the branches" in warnings[0]


def test_an_unknown_application_is_not_compared(inplace_store: WASMStore) -> None:
    """The update will say it does not exist."""
    assert lifecycle.check_upstream("nothing.example.com") is None


def test_the_live_release_link_is_what_is_compared(
    root: Path, store: WASMStore, machine: SimpleNamespace, two_releases: tuple[str, str]
) -> None:
    """current on disk is the truth, whatever the rows say."""
    first, _ = two_releases
    os.remove(root / "current")
    os.symlink(Path("releases") / first, root / "current")

    upstream = lifecycle.check_upstream(DOMAIN)

    assert upstream is not None and upstream.live_commit == first.split("-")[2]


def test_in_place_after_a_failed_build_the_new_commit_is_still_news(
    tmp_path: Path, inplace_store: WASMStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The checkout moved to the new commit; the old build serves; there is something new."""
    make_app(inplace_store, tmp_path / "example-com")
    good = inplace_store.record_deployment_start(INPLACE_DOMAIN, "cli", git_commit="aaaaaaa")
    inplace_store.finish_deployment(good, "success")
    broken = inplace_store.record_deployment_start(INPLACE_DOMAIN, "cli", git_commit=FULL[:7])
    inplace_store.finish_deployment(broken, "failed")
    inplace_git(monkeypatch, commit=FULL[:7], remote=FULL)

    upstream = lifecycle.check_upstream(INPLACE_DOMAIN)

    assert upstream is not None and upstream.has_new_commits
    assert upstream.live_commit == "aaaaaaa"


def test_in_place_the_last_successful_deployment_is_what_is_live(
    tmp_path: Path, inplace_store: WASMStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Its commit, not HEAD, is compared: the head of the branch built fine."""
    make_app(inplace_store, tmp_path / "example-com")
    good = inplace_store.record_deployment_start(INPLACE_DOMAIN, "cli", git_commit=FULL[:7])
    inplace_store.finish_deployment(good, "success")
    inplace_git(monkeypatch, commit="bbbbbbb", remote=FULL)

    upstream = lifecycle.check_upstream(INPLACE_DOMAIN)

    assert upstream is not None and not upstream.has_new_commits


def test_one_question_per_application_at_a_time(
    tmp_path: Path, inplace_store: WASMStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two requests at once share one ls-remote; the second waits for its answer."""
    import threading

    make_app(inplace_store, tmp_path / "example-com")
    asked = inplace_git(monkeypatch, commit=FULL[:7], remote=FULL)
    release = threading.Event()
    started = threading.Event()
    fake = lifecycle.SourceManager()
    answer = fake.remote_head

    def slow(source: str, branch: str | None = None, *, timeout: int | None = None) -> Any:
        started.set()
        release.wait(5)
        return answer(source, branch, timeout=timeout)

    fake.remote_head = slow
    monkeypatch.setattr(lifecycle, "SourceManager", lambda verbose=False: fake)
    results: list[Any] = []
    threads = [
        threading.Thread(target=lambda: results.append(lifecycle.check_upstream(INPLACE_DOMAIN)))
        for _ in range(2)
    ]
    threads[0].start()
    assert started.wait(5)
    threads[1].start()
    release.set()
    for thread in threads:
        thread.join(5)

    assert len(asked) == 1
    assert len(results) == 2 and results[0] == results[1]


def test_an_answer_is_reused_for_a_few_seconds_then_asked_again(
    tmp_path: Path, inplace_store: WASMStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A burst of clicks is one ls-remote; a later click asks again."""
    make_app(inplace_store, tmp_path / "example-com")
    asked = inplace_git(monkeypatch, commit=FULL[:7], remote=FULL)
    now = [1000.0]
    monkeypatch.setattr(lifecycle.time, "monotonic", lambda: now[0])

    lifecycle.check_upstream(INPLACE_DOMAIN)
    now[0] += lifecycle.UPSTREAM_CACHE_SECONDS - 1
    lifecycle.check_upstream(INPLACE_DOMAIN)
    assert len(asked) == 1

    now[0] += 2
    lifecycle.check_upstream(INPLACE_DOMAIN)
    assert len(asked) == 2


def test_an_update_forgets_the_answer(inplace: Any) -> None:
    """What is live changed; the next question goes to the remote."""
    lifecycle._upstream_answers[(INPLACE_DOMAIN, None)] = (lifecycle.time.monotonic(), None)

    lifecycle.update_app(INPLACE_DOMAIN)

    assert (INPLACE_DOMAIN, None) not in lifecycle._upstream_answers


def test_a_release_that_failed_its_gate_is_rebuilt_not_activated_again(
    root: Path, store: WASMStore, machine: SimpleNamespace, two_releases: tuple[str, str]
) -> None:
    """A failed activation leaves the release on disk; the commit is built afresh."""
    first, second = two_releases
    app = store.get_app(DOMAIN)
    assert app is not None and app.id is not None
    store.set_release_status(app.id, first, "failed")

    lifecycle.update_app(DOMAIN, commit=commit_of(machine, first)[:9])

    rebuilt = active_id(root)
    assert rebuilt not in {first, second}
    assert rebuilt.split("-")[2] == first.split("-")[2]
