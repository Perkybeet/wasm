# Copyright (c) 2024-2026 Yago Lopez Prado
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Tests for repository inspection: the backend of the new-app wizard.

``inspect_source`` fetches a repository into a throwaway checkout, detects
what it is, and reports the commands, port and environment variables a
deployment would use, all without creating an application. These tests cover
the local-directory source the wizard preview form exercises, the secret
heuristics applied to discovered environment variables, and that the
checkout is always removed, success or failure.
"""

from __future__ import annotations

import fnmatch
import json
import os
import shutil
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from wasm.core.exceptions import DeploymentError, SourceError
from wasm.core.runner import (
    CommandCancelled,
    CommandResult,
    FakeRunner,
    set_runner,
)
from wasm.core.store import WASMStore
from wasm.deployers import inspect as inspect_module
from wasm.deployers.docker_compose import COMPOSE_FILE_PRIORITY, DockerComposeDeployer
from wasm.deployers.inspect import (
    STALE_CHECKOUT_AGE,
    inspect_source,
    remove_stale_checkouts,
    sparse_patterns,
)
from wasm.deployers.registry import DeployerRegistry, _import_deployers
from wasm.managers.source_manager import GIT_AUTH_FAILURE_MESSAGE

NEXTJS_PACKAGE_JSON = json.dumps(
    {
        "name": "example-app",
        "version": "1.0.0",
        "scripts": {"build": "next build", "start": "next start"},
        "dependencies": {"next": "^14.0.0", "react": "^18.0.0"},
    }
)

#: A package.json that satisfies NodeJSDeployer.detect() (a "start" script,
#: no framework dependency) without satisfying NextJSDeployer's own
#: package.json check, so the tree is recognised through next.config.js
#: alone. Used to prove two deployers can match one tree.
GENERIC_START_PACKAGE_JSON = json.dumps(
    {"name": "example-app", "version": "1.0.0", "scripts": {"start": "node server.js"}}
)

ENV_EXAMPLE = "\n".join(
    [
        "# Payment provider",
        "STRIPE_SECRET=",
        "SOME_API_KEY=abc123",
        "AUTH_TOKEN=",
        "DB_PASSWORD=hunter2",
        "SSH_PRIVATE_KEY_PATH=",
        "ADMIN_PASS=",
        "PUBLIC_URL=https://example.com",
        "APP_NAME=",
        "",
    ]
)


@pytest.fixture
def store(tmp_path: Path):
    """
    Provide an isolated store, installed as the process-wide singleton.

    Every deployer constructed during detection reads ``get_store()`` in its
    ``__init__``, so a test that inspects a repository must not touch
    whatever database happens to be configured on the machine running the
    suite.

    Args:
        tmp_path: Per-test temporary directory.

    Yields:
        The store instance installed for the duration of the test.
    """
    WASMStore.reset_instance()
    instance = WASMStore(tmp_path / "wasm.db")
    yield instance
    WASMStore.reset_instance()


def _spy_on_mkdtemp(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    """
    Record every scratch directory ``inspect_source`` creates during a test.

    Args:
        monkeypatch: Patching helper, scoped to the test.

    Returns:
        The list every created path is appended to, in call order.
    """
    created: list[Path] = []
    real = inspect_module.tempfile.TemporaryDirectory

    def spy(*args: object, **kwargs: object) -> object:
        scratch = real(*args, **kwargs)  # type: ignore[call-overload]
        created.append(Path(scratch.name))
        return scratch

    monkeypatch.setattr(inspect_module.tempfile, "TemporaryDirectory", spy)
    return created


def test_inspect_detects_nextjs_from_a_local_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, store: WASMStore
) -> None:
    """The canonical wizard scenario: a Next.js repo pasted as a local path."""
    created = _spy_on_mkdtemp(monkeypatch)

    project = tmp_path / "project"
    project.mkdir()
    (project / "next.config.js").write_text("module.exports = {}\n")
    (project / "package.json").write_text(NEXTJS_PACKAGE_JSON)
    (project / "package-lock.json").write_text("{}\n")
    (project / ".env.example").write_text(ENV_EXAMPLE)

    result = inspect_source(str(project))

    assert result.app_type == "nextjs"
    assert result.detected_types[0] == "nextjs"
    assert result.package_manager == "npm"
    assert result.install_command == ["npm", "ci"]
    assert result.build_command == ["npm", "run", "build"]
    assert result.start_command == "npm run start"
    assert result.default_port == 3000
    assert result.branch == ""
    assert result.commit == ""
    assert result.compatible is True

    # A local directory is read where it is: detection only reads, so there
    # is nothing to copy and nothing to clean up.
    assert created == []


def test_inspect_flags_env_keys_by_broad_secret_heuristics(
    tmp_path: Path, store: WASMStore
) -> None:
    """
    Secret detection is broader here than EnvManager's own defaults.

    ``SOME_API_KEY`` and ``SSH_PRIVATE_KEY_PATH`` are flagged on the generic
    ``KEY``/``PRIVATE`` markers, not on the narrower compounds
    (``API_KEY``, ``PRIVATE_KEY``) EnvManager.SECRET_PATTERNS looks for; a
    false positive in a pre-deploy form only pre-selects a password field the
    operator can still see and edit.
    """
    project = tmp_path / "project"
    project.mkdir()
    (project / "next.config.js").write_text("module.exports = {}\n")
    (project / ".env.example").write_text(ENV_EXAMPLE)

    result = inspect_source(str(project))

    keys = {key.name: key for key in result.env_keys}

    assert keys["STRIPE_SECRET"].secret is True
    assert keys["STRIPE_SECRET"].required is True
    assert keys["STRIPE_SECRET"].default is None

    assert keys["SOME_API_KEY"].secret is True
    assert keys["SOME_API_KEY"].required is False
    assert keys["SOME_API_KEY"].default == "abc123"

    assert keys["AUTH_TOKEN"].secret is True
    assert keys["DB_PASSWORD"].secret is True
    assert keys["SSH_PRIVATE_KEY_PATH"].secret is True
    assert keys["ADMIN_PASS"].secret is True

    assert keys["PUBLIC_URL"].secret is False
    assert keys["PUBLIC_URL"].required is False
    assert keys["PUBLIC_URL"].default == "https://example.com"

    assert keys["APP_NAME"].secret is False
    assert keys["APP_NAME"].required is True
    assert keys["APP_NAME"].default is None


def test_inspect_detects_package_manager_from_the_lock_file(
    tmp_path: Path, store: WASMStore
) -> None:
    """A pnpm-lock.yaml means pnpm, not the npm default."""
    project = tmp_path / "project"
    project.mkdir()
    (project / "next.config.js").write_text("module.exports = {}\n")
    (project / "package.json").write_text(NEXTJS_PACKAGE_JSON)
    (project / "pnpm-lock.yaml").write_text("lockfileVersion: '6.0'\n")

    result = inspect_source(str(project))

    assert result.package_manager == "pnpm"
    assert result.install_command == ["pnpm", "install", "--frozen-lockfile"]
    assert result.build_command == ["pnpm", "run", "build"]
    assert result.start_command == "pnpm run start"


def test_inspect_reports_every_matching_type_in_priority_order(
    tmp_path: Path, store: WASMStore
) -> None:
    """
    A tree that satisfies two detectors reports both, most specific first.

    ``next.config.js`` alone identifies the project as Next.js; a
    ``package.json`` with a plain start script and no framework dependency
    also satisfies NodeJSDeployer's own detection. NextJS outranks NodeJS
    (70 vs. 40), so it is chosen, but both are reported.
    """
    project = tmp_path / "project"
    project.mkdir()
    (project / "next.config.js").write_text("module.exports = {}\n")
    (project / "package.json").write_text(GENERIC_START_PACKAGE_JSON)

    result = inspect_source(str(project))

    assert result.detected_types == ["nextjs", "nodejs"]
    assert result.app_type == "nextjs"


def test_inspect_reports_the_requested_branch_verbatim_when_not_git(
    tmp_path: Path, store: WASMStore
) -> None:
    """A local directory has no branch; the requested one is echoed back."""
    project = tmp_path / "project"
    project.mkdir()
    (project / "next.config.js").write_text("module.exports = {}\n")

    result = inspect_source(str(project), branch="release/2.0")

    assert result.branch == "release/2.0"
    assert result.commit == ""


def test_inspect_static_site_has_no_commands_or_package_manager(
    tmp_path: Path, store: WASMStore
) -> None:
    """A plain static site has nothing to install, build or run."""
    project = tmp_path / "project"
    project.mkdir()
    (project / "index.html").write_text("<html></html>\n")

    result = inspect_source(str(project))

    assert result.app_type == "static"
    assert result.package_manager is None
    assert result.install_command == []
    assert result.build_command == []
    assert result.start_command == ""
    assert result.default_port == 80


def test_inspect_removes_the_temp_dir_even_when_nothing_matches(
    monkeypatch: pytest.MonkeyPatch, remote: FakeRemote
) -> None:
    """
    Detection raising leaves nothing behind.

    An empty repository matches no registered application type, so
    ``inspect_source`` raises instead of returning a nonsensical result -
    and the checkout it made to find that out is still removed.
    """
    created = _spy_on_mkdtemp(monkeypatch)

    with pytest.raises(DeploymentError) as exc_info:
        inspect_source(REMOTE_URL)

    assert exc_info.value.details, "the error should say what to do about it"

    assert created, "mkdtemp was not called"
    assert not created[0].exists()


def test_inspect_raises_for_an_empty_source(
    monkeypatch: pytest.MonkeyPatch, store: WASMStore
) -> None:
    """An empty source is refused before anything is fetched or created."""
    created = _spy_on_mkdtemp(monkeypatch)

    with pytest.raises(SourceError):
        inspect_source("")

    assert created == []


# ---------------------------------------------------------------------------
# Git sources: ls-remote first, then only the files the detectors read
# ---------------------------------------------------------------------------

REMOTE_URL = "https://example.com/owner/repo.git"
REMOTE_COMMIT = "0123456789abcdef0123456789abcdef01234567"


def _git_subcommand(argv: Sequence[str]) -> tuple[str, list[str]]:
    """
    Split a git argv into its subcommand and that subcommand's arguments.

    Args:
        argv: ``git -c ... <subcommand> args...``.

    Returns:
        The subcommand and the arguments after it.
    """
    rest = list(argv[1:])
    while rest and rest[0] == "-c":
        rest = rest[2:]
    return rest[0], rest[1:]


def _matches(path: str, patterns: Sequence[str]) -> bool:
    """
    Decide whether git's non-cone sparse checkout would materialise a file.

    Every pattern WASM hands git is anchored (``/name``) and uses ``*`` only
    as a whole path segment, so matching segment by segment is exactly what
    git does with them.

    Args:
        path: A file of the repository, relative, POSIX separators.
        patterns: The sparse-checkout patterns.

    Returns:
        True when the file would be checked out.
    """
    parts = path.split("/")
    for pattern in patterns:
        wanted = pattern.lstrip("/").split("/")
        if len(wanted) == len(parts) and all(
            fnmatch.fnmatchcase(part, glob) for part, glob in zip(parts, wanted, strict=True)
        ):
            return True
    return False


class FakeRemote(FakeRunner):
    """
    git, answered from a directory that stands in for the remote repository.

    ``ls-remote`` reports one branch; ``clone`` creates the checkout, full or
    empty depending on ``--no-checkout``; ``sparse-checkout set`` records the
    patterns and ``checkout`` copies exactly the files they match, the way git
    would; ``ls-tree`` lists every file without reading one. Any subcommand in
    :attr:`failures` answers that result instead.
    """

    def __init__(self, tree: Path, *, branch: str = "main") -> None:
        """
        Args:
            tree: The remote repository's files. Created when missing.
            branch: Its only (and default) branch.
        """
        super().__init__()
        tree.mkdir(parents=True, exist_ok=True)
        self.tree = tree
        self.branch = branch
        self.patterns: list[str] | None = None
        self.failures: dict[str, CommandResult] = {}
        self.before: dict[str, Callable[[list[str], Path | None], None]] = {}

    def files(self) -> list[str]:
        """Every file of the remote tree, relative and sorted."""
        return sorted(
            str(path.relative_to(self.tree)) for path in self.tree.rglob("*") if path.is_file()
        )

    def run(  # type: ignore[override]
        self,
        argv: Sequence[str],
        *,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        check: bool = False,
        **_: Any,
    ) -> CommandResult:
        recorded = self._lookup(argv, None, env)
        sub, args = _git_subcommand(argv)
        if sub in self.before:
            self.before[sub](args, cwd)
        if sub in self.failures:
            result = self.failures[sub]
        else:
            result = self._answer(sub, args, cwd)
        return CommandResult(
            argv=recorded, exit_code=result.exit_code, stdout=result.stdout, stderr=result.stderr
        )

    def _answer(self, sub: str, args: list[str], cwd: Path | None) -> CommandResult:
        ok = CommandResult(argv=(), exit_code=0)
        if sub == "ls-remote":
            if "--symref" in args:
                out = f"ref: refs/heads/{self.branch}\tHEAD\n{REMOTE_COMMIT}\tHEAD\n"
                return CommandResult(argv=(), exit_code=0, stdout=out)
            asked = args[-1]
            if asked == f"refs/heads/{self.branch}":
                return CommandResult(argv=(), exit_code=0, stdout=f"{REMOTE_COMMIT}\t{asked}\n")
            return CommandResult(argv=(), exit_code=2)
        if sub == "clone":
            if "--branch" in args and args[args.index("--branch") + 1] != self.branch:
                wanted = args[args.index("--branch") + 1]
                return CommandResult(
                    argv=(),
                    exit_code=128,
                    stderr=f"fatal: Remote branch {wanted} not found in upstream origin\n",
                )
            destination = Path(args[-1])
            (destination / ".git").mkdir(parents=True)
            if "--no-checkout" not in args:
                self._copy(destination, lambda _path: True)
            return ok
        if sub == "sparse-checkout":
            self.patterns = [a for a in args[1:] if not a.startswith("--")]
            return ok
        if sub == "checkout":
            assert cwd is not None and self.patterns is not None
            patterns = self.patterns
            self._copy(cwd, lambda path: _matches(path, patterns))
            return ok
        if sub == "ls-tree":
            return CommandResult(
                argv=(), exit_code=0, stdout="".join(f"{f}\0" for f in self.files())
            )
        if sub == "rev-parse" and "--abbrev-ref" in args:
            return CommandResult(argv=(), exit_code=0, stdout=f"{self.branch}\n")
        if sub == "rev-parse":
            return CommandResult(argv=(), exit_code=0, stdout=f"{REMOTE_COMMIT[:7]}\n")
        if sub == "config":
            return CommandResult(argv=(), exit_code=0, stdout=f"{REMOTE_URL}\n")
        return ok

    def _copy(self, destination: Path, wanted: Callable[[str], bool]) -> None:
        for name in self.files():
            if wanted(name):
                target = destination / name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(self.tree / name, target)

    def subcommands(self) -> list[str]:
        """The git subcommands run, in order."""
        return [_git_subcommand(call)[0] for call in self.calls if call[0] == "git"]


@pytest.fixture
def remote(tmp_path: Path, store: WASMStore) -> FakeRemote:
    """
    A fake remote, installed as the process-wide runner.

    Yields:
        The remote; write its files under ``remote.tree``.
    """
    fake = FakeRemote(tmp_path / "remote")
    set_runner(fake)
    try:
        yield fake
    finally:
        set_runner(None)


def _write(tree: Path, files: Mapping[str, str]) -> None:
    for name, content in files.items():
        path = tree / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)


def test_a_git_source_is_probed_then_fetched_sparse_and_blobless(remote: FakeRemote) -> None:
    """The fast path, in order, with the flags that make it fast."""
    _write(
        remote.tree,
        {"next.config.js": "module.exports = {}\n", "package.json": NEXTJS_PACKAGE_JSON},
    )

    result = inspect_source(REMOTE_URL)

    assert result.app_type == "nextjs"
    assert result.branch == "main"
    assert result.commit == REMOTE_COMMIT[:7]
    subcommands = remote.subcommands()
    # No ls-remote first: git never prompts, so the clone itself fails fast, and a
    # separate round trip made every small repository slower than a plain clone.
    assert subcommands[:3] == ["clone", "sparse-checkout", "checkout"]
    clone = next(call for call in remote.calls if "clone" in call)
    for flag in ("--filter=blob:none", "--no-checkout"):
        assert flag in clone
    assert clone[clone.index("--depth") + 1] == "1"
    assert "--branch" not in clone  # the repository's default
    sparse = next(call for call in remote.calls if "sparse-checkout" in call)
    assert sparse[sparse.index("sparse-checkout") + 1 : sparse.index("sparse-checkout") + 3] == (
        "set",
        "--no-cone",
    )
    # Submodules never decide an application's type.
    assert not any("--recursive" in call for call in remote.calls)


def test_the_sparse_checkout_fetches_only_what_the_detectors_read(remote: FakeRemote) -> None:
    """Source code, assets and docs stay on the remote."""
    _write(
        remote.tree,
        {
            "package.json": NEXTJS_PACKAGE_JSON,
            "src/app/page.tsx": "export default function Page() {}\n",
            "public/logo.png": "png",
            "README.md": "# app\n",
        },
    )
    fetched: list[str] = []

    def spy(args: list[str], cwd: Path | None) -> None:
        assert remote.patterns is not None
        fetched.extend(f for f in remote.files() if _matches(f, remote.patterns))

    remote.before["checkout"] = spy

    inspect_source(REMOTE_URL)

    assert fetched == ["package.json"]


def test_the_sparse_file_list_comes_from_the_detectors() -> None:
    """
    Every file a detector checks is in the list, read off the deployers.

    A detection file added to a deployer reaches the fast path without anyone
    remembering to add it here.
    """
    _import_deployers()
    patterns = sparse_patterns()

    for deployer_class in DeployerRegistry.in_detection_order():
        for name in deployer_class.DETECTION_FILES:
            assert f"/{name}" in patterns, f"{deployer_class.APP_TYPE} reads {name}"
    for name in [*COMPOSE_FILE_PRIORITY, *DockerComposeDeployer.FRAMEWORK_CONFIG_FILES]:
        assert f"/{name}" in patterns
    # Read by detect() and the helpers beyond DETECTION_FILES.
    for name in ["/package.json", "/pnpm-lock.yaml", "/package-lock.json", "/.env.example"]:
        assert name in patterns
    assert "/apps/*/package.json" in patterns
    # Anchored, so a nested package.json three levels down is never fetched.
    assert all(pattern.startswith("/") for pattern in patterns)


#: Trees that exercise every detector and helper inspection runs, each
#: inspected twice: from the full directory and through the sparse fast path.
EQUIVALENCE_TREES: dict[str, dict[str, str]] = {
    "nextjs": {
        "next.config.mjs": "export default {}\n",
        "package.json": NEXTJS_PACKAGE_JSON,
        "pnpm-lock.yaml": "lockfileVersion: '6.0'\n",
        ".env.example": ENV_EXAMPLE,
        "src/index.ts": "",
    },
    "vite": {
        "package.json": json.dumps({"devDependencies": {"vite": "^5"}}),
        "yarn.lock": "",
        "src/main.ts": "",
    },
    "nodejs": {
        "package.json": json.dumps({"main": "server.js", "scripts": {"build": "tsc"}}),
        "package-lock.json": "{}",
        "server.js": "",
    },
    "python": {"requirements.txt": "django\n", "manage.py": "", "app/wsgi.py": ""},
    "static": {"index.html": "<html></html>\n", "css/site.css": ""},
    "static-with-go-is-not-static": {"index.html": "", "go.mod": "module x\n"},
    "compose": {"docker-compose.yml": "services: {}\n", ".env.sample": "PORT=3000\n"},
    "compose-for-local-dev": {
        "docker-compose.yml": "services: {}\n",
        "vite.config.ts": "",
        "package.json": json.dumps({"dependencies": {"vite": "^5"}}),
    },
    "monorepo": {
        "turbo.json": "{}",
        "pnpm-workspace.yaml": "packages: ['apps/*']\n",
        "package.json": json.dumps({"name": "root", "private": True}),
        "apps/web/package.json": NEXTJS_PACKAGE_JSON,
        "apps/web/.env.example": "NEXT_PUBLIC_API=\n",
        "apps/api/package.json": json.dumps({"main": "index.js"}),
        "packages/db/.env.template": "DATABASE_URL=\n",
        "services/worker/.env.example": "QUEUE=\n",
        "apps/web/src/page.tsx": "",
    },
    # Compose counts every directory under apps/, with or without a
    # package.json; only the sparse path's directory skeleton keeps that.
    "compose-in-turbo-repo": {
        "docker-compose.yml": "services: {}\n",
        "turbo.json": "{}",
        "apps/one/main.go": "",
        "apps/two/main.go": "",
    },
}


@pytest.mark.parametrize("name", sorted(EQUIVALENCE_TREES))
def test_the_fast_path_answers_exactly_what_a_full_checkout_does(
    name: str, tmp_path: Path, remote: FakeRemote
) -> None:
    """
    The same inspection from the whole tree and from the sparse checkout.

    The local directory is read in full; the git source only gets the files
    the sparse patterns match. Any file a detector reads that the patterns
    miss shows up here as a different answer.
    """
    files = EQUIVALENCE_TREES[name]
    full = tmp_path / "full"
    _write(full, files)
    _write(remote.tree, files)

    def outcome(source: str) -> object:
        try:
            result = inspect_source(source)
        except DeploymentError as exc:
            return ("unmatched", exc.message)
        return (
            result.app_type,
            result.detected_types,
            result.package_manager,
            result.install_command,
            result.build_command,
            result.start_command,
            result.default_port,
            result.env_keys,
            result.compatible,
            result.verdict,
        )

    assert outcome(REMOTE_URL) == outcome(str(full))


def test_an_unreachable_repository_fails_at_the_clone_with_the_actionable_error(
    remote: FakeRemote, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    A private repository fails in the time one clone round trip takes, says how
    to grant access, and leaves no scratch directory behind.
    """
    created = _spy_on_mkdtemp(monkeypatch)
    remote.failures["clone"] = CommandResult(
        argv=(),
        exit_code=128,
        stderr="fatal: could not read Username for 'https://example.com': "
        "terminal prompts disabled\n",
    )

    with pytest.raises(SourceError) as exc_info:
        inspect_source(REMOTE_URL)

    assert exc_info.value.message == GIT_AUTH_FAILURE_MESSAGE
    assert "deploy key" in exc_info.value.details
    assert "terminal prompts disabled" in (exc_info.value.output or "")
    assert remote.subcommands() == ["clone"]
    assert all(not directory.exists() for directory in created)


def test_a_branch_the_remote_does_not_have_is_named_in_the_error(remote: FakeRemote) -> None:
    """A typo in the branch fails the clone and the error names it."""
    with pytest.raises(SourceError) as exc_info:
        inspect_source(REMOTE_URL, branch="mian")

    assert "mian" in f"{exc_info.value.message} {exc_info.value.details} {exc_info.value.output}"
    assert "sparse-checkout" not in remote.subcommands()


def test_the_requested_branch_is_cloned(remote: FakeRemote) -> None:
    """A branch that exists goes to the clone as asked, and is reported back."""
    remote.branch = "release"
    _write(remote.tree, {"index.html": "<html></html>\n"})

    result = inspect_source(REMOTE_URL, branch="release")

    clone = next(call for call in remote.calls if "clone" in call)
    assert clone[clone.index("--branch") + 1] == "release"
    assert result.branch == "release"


@pytest.mark.parametrize("failing", ["sparse-checkout", "checkout"])
def test_a_git_without_sparse_checkout_falls_back_to_a_shallow_clone(
    failing: str, remote: FakeRemote
) -> None:
    """
    When this git cannot do the sparse steps, inspection still answers.

    The fallback is the old shallow clone, minus ``--recursive``.
    """
    _write(
        remote.tree,
        {"next.config.js": "module.exports = {}\n", "package.json": NEXTJS_PACKAGE_JSON},
    )
    remote.failures[failing] = CommandResult(
        argv=(), exit_code=129, stderr="error: unknown option `no-cone'\n"
    )

    result = inspect_source(REMOTE_URL)

    assert result.app_type == "nextjs"
    assert result.commit == REMOTE_COMMIT[:7]
    clones = [call for call in remote.calls if "clone" in call]
    assert len(clones) == 2
    fallback = clones[-1]
    assert "--filter=blob:none" not in fallback
    assert "--recursive" not in fallback
    assert fallback[fallback.index("--depth") + 1] == "1"
    assert "--branch" not in fallback


def test_a_refused_credential_during_the_blob_fetch_is_not_hidden_by_the_fallback(
    remote: FakeRemote,
) -> None:
    """The fallback is for an old git, not for a remote that says no."""
    _write(remote.tree, {"index.html": ""})
    remote.failures["checkout"] = CommandResult(
        argv=(), exit_code=128, stderr="fatal: Authentication failed for 'https://example.com'\n"
    )

    with pytest.raises(SourceError) as exc_info:
        inspect_source(REMOTE_URL)

    assert exc_info.value.message == GIT_AUTH_FAILURE_MESSAGE
    assert [s for s in remote.subcommands() if s == "clone"] == ["clone"]


# ---------------------------------------------------------------------------
# Cancel
# ---------------------------------------------------------------------------


def test_cancelling_stops_git_and_removes_the_checkout_at_once(
    remote: FakeRemote, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    The client went away mid-clone: the clone is stopped and its half-written
    directory is gone before ``inspect_source`` returns.

    The fake clone writes part of a repository, then the cancel arrives and
    the runner does what the real one does once it has killed the process
    tree (``tests/test_runner.py`` proves that part with real processes):
    raise :class:`CommandCancelled`.
    """
    created = _spy_on_mkdtemp(monkeypatch)
    cancel = threading.Event()
    _write(remote.tree, {"index.html": ""})

    def interrupted_clone(args: list[str], cwd: Path | None) -> None:
        partial = Path(args[-1]) / ".git" / "objects" / "pack"
        partial.mkdir(parents=True)
        (partial / "tmp_pack_1").write_text("half a pack")
        cancel.set()
        raise CommandCancelled("Cancelled: git clone")

    remote.before["clone"] = interrupted_clone

    with pytest.raises(CommandCancelled):
        inspect_source(REMOTE_URL, cancel=cancel)

    assert remote.subcommands() == ["clone"]
    assert created and not created[0].exists()


def test_a_cancel_before_the_first_command_runs_nothing(remote: FakeRemote) -> None:
    """A client that left before the work started costs no git at all."""
    cancel = threading.Event()
    cancel.set()

    with pytest.raises(CommandCancelled):
        inspect_source(REMOTE_URL, cancel=cancel)

    assert remote.calls == []


def test_a_cancel_between_commands_stops_before_the_next(remote: FakeRemote) -> None:
    """The scope covers every command of the inspection, not only the clone."""
    cancel = threading.Event()
    _write(remote.tree, {"index.html": ""})
    remote.before["clone"] = lambda args, cwd: cancel.set()

    with pytest.raises(CommandCancelled):
        inspect_source(REMOTE_URL, cancel=cancel)

    assert remote.subcommands() == ["clone"]


# ---------------------------------------------------------------------------
# Stale checkouts
# ---------------------------------------------------------------------------


def _age(path: Path, seconds: float) -> None:
    then = time.time() - seconds
    os.utime(path, (then, then))


def test_stale_checkouts_are_removed_and_fresh_ones_kept(tmp_path: Path) -> None:
    """
    A console killed mid-inspection leaves its checkout; the next start
    removes it. One still being used by a running inspection stays.
    """
    stale = tmp_path / "wasm-inspect-abc123"
    (stale / "source" / ".git").mkdir(parents=True)
    (stale / "source" / "package.json").write_text("{}")
    _age(stale, STALE_CHECKOUT_AGE + 60)
    fresh = tmp_path / "wasm-inspect-def456"
    fresh.mkdir()
    unrelated = tmp_path / "something-else"
    unrelated.mkdir()
    _age(unrelated, STALE_CHECKOUT_AGE + 60)

    removed = remove_stale_checkouts(tmp_path)

    assert removed == [stale]
    assert not stale.exists()
    assert fresh.exists()
    assert unrelated.exists()


def test_stale_cleanup_never_follows_a_link_or_touches_another_users_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    /tmp is shared: anyone can create a ``wasm-inspect-*`` there. A link is
    never followed (root would delete its target), and a directory that
    belongs to another account is not ours to remove.
    """
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "keep").write_text("important")
    link = tmp_path / "wasm-inspect-link"
    link.symlink_to(victim)
    others = tmp_path / "wasm-inspect-other"
    others.mkdir()
    for path in (victim, others):
        _age(path, STALE_CHECKOUT_AGE + 60)
    os.utime(link, (0, 0), follow_symlinks=False)
    real_uid = os.geteuid()

    def owner(path: Path) -> int:
        return real_uid + 1 if path == others else real_uid

    monkeypatch.setattr(inspect_module, "_owner", owner)

    assert remove_stale_checkouts(tmp_path) == []
    assert (victim / "keep").read_text() == "important"
    assert link.is_symlink()
    assert others.exists()


# ---------------------------------------------------------------------------
# Compatibility verdict
# ---------------------------------------------------------------------------


def test_a_deployable_repository_says_so(remote: FakeRemote) -> None:
    _write(remote.tree, {"next.config.js": "", "package.json": NEXTJS_PACKAGE_JSON})

    result = inspect_source(REMOTE_URL)

    assert result.compatible is True
    assert "Next.js" in result.verdict
    assert result.suggestion is None


def test_a_missing_runtime_makes_it_not_deployable_here(
    remote: FakeRemote,
) -> None:
    """Recognised, but this server cannot build it yet: say what to install."""
    _write(remote.tree, {"requirements.txt": "flask\n"})
    remote.only_knows("git")

    result = inspect_source(REMOTE_URL)

    assert result.app_type == "python"
    assert result.compatible is False
    assert "python3" in result.verdict
    assert result.suggestion and "wasm setup init" in result.suggestion


def test_a_dockerfile_alone_suggests_a_compose_file(remote: FakeRemote) -> None:
    _write(remote.tree, {"Dockerfile": "FROM node:22\n", "src/index.js": ""})

    with pytest.raises(DeploymentError) as exc_info:
        inspect_source(REMOTE_URL)

    assert "Dockerfile" in exc_info.value.message
    assert "compose.yaml" in exc_info.value.details
    assert "build: ." in exc_info.value.details


def test_projects_in_subdirectories_are_named(remote: FakeRemote) -> None:
    """Nothing at the root, a frontend and a backend below it."""
    _write(
        remote.tree,
        {
            "frontend/package.json": NEXTJS_PACKAGE_JSON,
            "backend/requirements.txt": "fastapi\n",
            "README.md": "",
        },
    )

    with pytest.raises(DeploymentError) as exc_info:
        inspect_source(REMOTE_URL)

    message = exc_info.value.message
    assert "frontend/" in message and "backend/" in message
    assert "root" in message
    assert "compose" in exc_info.value.details.lower()


def test_a_near_miss_monorepo_says_what_the_monorepo_type_needs(remote: FakeRemote) -> None:
    """turbo.json and apps/, but only one app: close to a monorepo, not one."""
    _write(
        remote.tree,
        {
            "turbo.json": "{}",
            "pnpm-workspace.yaml": "packages: ['apps/*']\n",
            "apps/web/package.json": NEXTJS_PACKAGE_JSON,
        },
    )

    with pytest.raises(DeploymentError) as exc_info:
        inspect_source(REMOTE_URL)

    assert "apps/web/" in exc_info.value.message
    assert "two" in exc_info.value.details and "apps/" in exc_info.value.details


def test_an_unsupported_language_is_named(tmp_path: Path, store: WASMStore) -> None:
    project = tmp_path / "project"
    _write(project, {"go.mod": "module example.com/app\n", "main.go": ""})

    with pytest.raises(DeploymentError) as exc_info:
        inspect_source(str(project))

    assert "Go" in exc_info.value.message
    assert "Dockerfile" in exc_info.value.details


def test_a_package_json_without_a_start_script_says_to_add_one(
    tmp_path: Path, store: WASMStore
) -> None:
    project = tmp_path / "project"
    _write(project, {"package.json": json.dumps({"name": "lib", "scripts": {"test": "jest"}})})

    with pytest.raises(DeploymentError) as exc_info:
        inspect_source(str(project))

    assert "start" in exc_info.value.details
