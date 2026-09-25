# Copyright (c) 2024-2026 Yago Lopez Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

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

import json
from pathlib import Path

import pytest

from wasm.core.exceptions import DeploymentError, SourceError
from wasm.core.runner import FakeRunner
from wasm.core.store import WASMStore
from wasm.deployers import inspect as inspect_module
from wasm.deployers.inspect import inspect_source

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

    # The prefix requested for the throwaway checkout, and it must be gone
    # once inspection has returned.
    assert created, "mkdtemp was not called"
    assert created[0].name.startswith("wasm-inspect-")
    assert not created[0].exists()


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
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, store: WASMStore
) -> None:
    """
    Detection raising leaves nothing behind.

    An empty directory matches no registered application type, so
    ``inspect_source`` raises instead of returning a nonsensical result -
    and the checkout it made to find that out is still removed.
    """
    created = _spy_on_mkdtemp(monkeypatch)

    empty_project = tmp_path / "empty"
    empty_project.mkdir()

    with pytest.raises(DeploymentError) as exc_info:
        inspect_source(str(empty_project))

    assert exc_info.value.details, "the error should say what to do about it"

    assert created, "mkdtemp was not called"
    assert not created[0].exists()


def test_inspect_raises_for_an_empty_source(
    monkeypatch: pytest.MonkeyPatch, store: WASMStore
) -> None:
    """An empty source is refused before anything is fetched, and cleaned up."""
    created = _spy_on_mkdtemp(monkeypatch)

    with pytest.raises(SourceError):
        inspect_source("")

    assert created, "mkdtemp was not called"
    assert not created[0].exists()


def test_inspect_raises_an_actionable_error_on_clone_failure(
    runner: FakeRunner, monkeypatch: pytest.MonkeyPatch, store: WASMStore
) -> None:
    """
    A repository git cannot reach fails the inspection, not silently.

    Nothing here talks to a real network: ``git`` is faked to fail exactly
    as it would on an unreachable or nonexistent repository, and the
    checkout directory made before the clone was attempted is still cleaned
    up.
    """
    created = _spy_on_mkdtemp(monkeypatch)
    runner.script(
        ["git"],
        exit_code=128,
        stderr="fatal: repository 'https://example.invalid/owner/repo.git' not found",
    )

    with pytest.raises(SourceError) as exc_info:
        inspect_source("https://example.invalid/owner/repo.git")

    assert "not found" in str(exc_info.value)

    assert created, "mkdtemp was not called"
    assert not created[0].exists()
