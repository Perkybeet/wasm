# Copyright (c) 2024-2026 Yago Lopez Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
Tests for deploys built as releases and activated behind a health gate.

Every test builds on a real temporary tree: releases, ``current`` and
``shared/`` are real directories and links, and what a release holds is read
back from disk. Only the machine is faked - git, systemd, nginx and the HTTP
probe - through the same seams the deployers use in production.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from jinja2 import Environment, PackageLoader

from wasm.core.exceptions import DeploymentError, SourceError
from wasm.core.fs import DryRunFileSystem, set_fs
from wasm.core.logger import Logger
from wasm.core.runner import DryRunRunner, FakeRunner, is_read_only
from wasm.core.store import App, ReleaseStatus, WASMStore
from wasm.deployers import base as base_module
from wasm.deployers import lifecycle
from wasm.deployers.auto import AutoDeployer
from wasm.deployers.helpers.health_gate import collapse_attempts as _collapse_attempts
from wasm.deployers.helpers.layout import (
    CONFIGURED,
    INPLACE,
    RELEASES,
    choose_layout,
    configured_layout,
)
from wasm.deployers.helpers.release_build import (
    StagedRelease,
    lockfiles_match,
    stage_release,
)
from wasm.deployers.nodejs import NodeJSDeployer
from wasm.deployers.python import PythonDeployer
from wasm.deployers.releases import ReleaseManager
from wasm.deployers.static import StaticDeployer
from wasm.deployers.vite import ViteDeployer
from wasm.managers.nginx_manager import NginxManager
from wasm.managers.source_manager import SourceManager

DOMAIN = "rel.example.com"
#: Not any deployer's default, so a probe of the wrong port shows.
PORT = 3100
GIT_URL = "https://github.com/example/app.git"

PACKAGE_JSON = '{"name": "app", "scripts": {"start": "node server.js"}}\n'
LOCKFILE = '{"name": "app", "lockfileVersion": 3, "packages": {}}\n'
GOOD_SERVER = "require('http').createServer((q, s) => s.end('ok')).listen(3000);\n"
BROKEN_SERVER = "throw new Error('boom');\n"


# ---------------------------------------------------------------------------
# Fakes for the machine
# ---------------------------------------------------------------------------


class TreeRunner(FakeRunner):
    """
    A FakeRunner whose copies and installs leave real trees behind.

    Attributes:
        where: Every command with the directory it ran in, in order.
    """

    def __init__(self) -> None:
        super().__init__()
        self.where: list[tuple[tuple[str, ...], Path | None]] = []

    def run(self, argv: Any, *, cwd: Path | None = None, **kwargs: Any) -> Any:
        result = super().run(argv, cwd=cwd, **kwargs)
        self.where.append((tuple(argv), cwd))
        if list(argv[:3]) == ["cp", "-a", "--reflink=auto"] and result.success:
            shutil.copytree(argv[3], argv[4], symlinks=True)
        elif list(argv[:3]) == ["python3", "-m", "venv"] and result.success:
            Path(argv[3]).mkdir(parents=True, exist_ok=True)
        return result

    def stream(self, argv: Any, *, on_line: Any, cwd: Path | None = None, **kwargs: Any) -> Any:
        result = super().stream(argv, on_line=on_line, cwd=cwd, **kwargs)
        self.where.append((tuple(argv), cwd))
        if tuple(argv) in {("npm", "ci"), ("npm", "install")} and cwd is not None:
            marker = Path(cwd) / "node_modules" / ".installed-by"
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text(Path(cwd).name)
        return result

    def cwd_of(self, *argv: str) -> list[Path | None]:
        """
        Args:
            *argv: The command to look for.

        Returns:
            The directories it ran in, in order.
        """
        return [cwd for call, cwd in self.where if call == argv]


class FakeGit:
    """
    Stands in for SourceManager for git sources: a commit is a directory.

    Attributes:
        commits: Directory holding the tree of each commit id.
        head: The commit the branch points at.
        calls: What was asked of it.
    """

    def __init__(self) -> None:
        self.commits: dict[str, Path] = {}
        self.head: str | None = None
        self.calls: list[tuple[Any, ...]] = []

    def publish(self, tree: Path) -> str:
        """
        Make a tree the new tip of the branch.

        Args:
            tree: Directory with the files of the commit.

        Returns:
            The commit id.
        """
        commit = hashlib.sha256(f"{len(self.commits)}:{tree}".encode()).hexdigest()[:40]
        self.commits[commit] = tree
        self.head = commit
        return commit

    def sync_cache(self, source: str, cache: Path, branch: str | None = None) -> str | None:
        self.calls.append(("sync", source, cache, branch))
        (cache / ".git").mkdir(parents=True, exist_ok=True)
        return self.head

    def export_commit(self, repository: Path, commit: str, destination: Path) -> None:
        self.calls.append(("export", commit, destination))
        shutil.copytree(self.commits[commit], destination, dirs_exist_ok=True)

    def get_repo_info(self, path: Path) -> dict[str, Any]:
        return {"branch": "main", "commit": None}

    def fetch(self, *args: Any, **kwargs: Any) -> bool:
        raise AssertionError("a git source must go through the repository cache")


class FakeServices:
    """
    Stands in for ServiceManager.

    Attributes:
        units: Units created, with what they were created with.
        restarts: Every restart, with the release ``current`` pointed at.
    """

    def __init__(self, app_root: Path) -> None:
        self.app_root = app_root
        self.units: dict[str, dict[str, Any]] = {}
        self.restarts: list[str] = []

    def create_service(self, name: str, **kwargs: Any) -> None:
        self.units[name] = kwargs

    def enable(self, name: str) -> bool:
        return True

    def start(self, name: str) -> None:
        raise AssertionError("a release is activated with a restart, not a start")

    def restart(self, name: str) -> None:
        link = self.app_root / "current"
        self.restarts.append(os.readlink(link) if link.is_symlink() else "<no current>")

    def stop(self, name: str) -> bool:
        return True

    def get_status(self, name: str) -> dict[str, Any]:
        return {"exists": name in self.units, "active": True}

    def delete_service(self, name: str) -> None:
        self.units.pop(name, None)

    def logs(self, name: str, lines: int = 50) -> str:
        return "Error: boom\n    at Object.<anonymous> (server.js:1:7)"


class FakeWeb:
    """Stands in for NginxManager/ApacheManager, keeping what it was asked to render."""

    def __init__(self) -> None:
        self.sites: dict[str, dict[str, Any]] = {}

    def is_running(self) -> bool:
        return True

    def site_exists(self, domain: str) -> bool:
        return domain in self.sites

    def create_site(self, domain: str, template: str, context: dict[str, Any]) -> bool:
        self.sites[domain] = {"template": template, "context": context}
        return True

    update_site = create_site

    def enable_site(self, domain: str) -> bool:
        return True

    def disable_site(self, domain: str) -> bool:
        return True

    def delete_site(self, domain: str) -> bool:
        self.sites.pop(domain, None)
        return True

    def reload(self) -> bool:
        return True


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """A store in the test's directory, installed wherever it is looked up."""
    WASMStore.reset_instance()
    instance = WASMStore(tmp_path / "wasm.db")
    monkeypatch.setattr(lifecycle, "get_store", lambda: instance)
    yield instance
    WASMStore.reset_instance()


@pytest.fixture
def root(tmp_path: Path) -> Path:
    """The application directory."""
    return tmp_path / "apps" / "rel-example-com"


@pytest.fixture
def git() -> FakeGit:
    """The fake repository."""
    return FakeGit()


@pytest.fixture
def machine(root: Path, git: FakeGit, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """
    Everything a deploy talks to, faked, plus a probe that reads the active release.

    The probe answers like the real one would for the fixture server: a
    release whose server.js throws never answers.
    """
    services = FakeServices(root)
    web = FakeWeb()
    runner = TreeRunner()
    probes: list[str] = []

    def probe(url: str, **kwargs: Any) -> bool:
        probes.append(url)
        server = root / "current" / "server.js"
        if server.is_file() and "throw" in server.read_text():
            on_attempt = kwargs.get("on_attempt")
            if on_attempt is not None:
                on_attempt("Health check attempt 1 failed: [Errno 111] Connection refused")
            return False
        return True

    monkeypatch.setattr(base_module, "wait_until_healthy", probe)
    return SimpleNamespace(services=services, web=web, runner=runner, git=git, probes=probes)


def write_tree(directory: Path, files: dict[str, str]) -> Path:
    """
    Materialise a project tree.

    Args:
        directory: Where to create it.
        files: Relative path to content.

    Returns:
        The directory.
    """
    for relative, content in files.items():
        target = directory / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    return directory


def node_tree(directory: Path, *, server: str = GOOD_SERVER, lockfile: str = LOCKFILE) -> Path:
    """A Node project with a lockfile, a server and an .env.example."""
    return write_tree(
        directory,
        {
            "package.json": PACKAGE_JSON,
            "package-lock.json": lockfile,
            "server.js": server,
            ".env.example": "SESSION_SECRET=\n",
        },
    )


def python_tree(directory: Path, *, requirements: str = "flask\n") -> Path:
    """A Python project with a requirements.txt lockfile."""
    return write_tree(directory, {"requirements.txt": requirements, "app.py": "app = None\n"})


def wire(deployer: Any, machine: SimpleNamespace) -> Any:
    """
    Point a deployer at the fakes.

    Args:
        deployer: The deployer.
        machine: The fakes.

    Returns:
        The deployer.
    """
    deployer.source_manager = machine.git
    deployer.service_manager = machine.services
    deployer.cert_manager = SimpleNamespace(obtain=lambda *a, **k: True)
    deployer._webserver_manager = lambda: machine.web
    deployer.pre_flight_check = lambda: True
    return deployer


def deploy_new(
    root: Path,
    machine: SimpleNamespace,
    *,
    deployer_class: type = NodeJSDeployer,
    layout: str | None = RELEASES,
    persistent: list[str] | None = None,
    source: str = GIT_URL,
) -> Any:
    """
    Deploy a new application.

    Returns:
        The deployer, after a successful deploy.
    """
    deployer = wire(deployer_class(verbose=False, runner=machine.runner), machine)
    deployer.configure(
        DOMAIN,
        source,
        port=PORT,
        ssl=False,
        app_path=root,
        layout=layout,
        persistent_paths=persistent,
    )
    assert deployer.deploy() is True
    return deployer


def update(
    machine: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    *,
    deployer_class: type = NodeJSDeployer,
    **kwargs: Any,
) -> Any:
    """
    Run the shared update sequence, with deployers wired to the fakes.

    Returns:
        What update_app returned.
    """
    monkeypatch.setattr(
        lifecycle,
        "get_deployer",
        lambda app_type, verbose=False: wire(
            deployer_class(verbose=False, runner=machine.runner), machine
        ),
    )
    return lifecycle.update_app(DOMAIN, **kwargs)


def active_id(root: Path) -> str:
    """The release ``current`` points at."""
    return Path(os.readlink(root / "current")).name


# ---------------------------------------------------------------------------
# A new application
# ---------------------------------------------------------------------------


def test_a_new_app_deploys_as_a_release(
    tmp_path: Path, root: Path, store: WASMStore, machine: SimpleNamespace
) -> None:
    """The source lands in releases/<id>, current points at it, shared holds the .env."""
    commit = machine.git.publish(node_tree(tmp_path / "v1"))

    deploy_new(root, machine, persistent=["uploads"])

    releases = ReleaseManager(root).list()
    assert [r.commit for r in releases] == [commit[:7]]
    release = releases[0].path
    assert active_id(root) == release.name
    assert (release / "server.js").read_text() == GOOD_SERVER
    assert not (release / ".git").exists()
    assert (root / "repo" / ".git").is_dir()

    # Shared before install and build, and linked relatively.
    assert os.readlink(release / ".env") == "../../shared/.env"
    assert stat.S_IMODE((root / "shared" / ".env").stat().st_mode) == 0o600
    assert os.readlink(release / "uploads") == "../../shared/uploads"
    assert (root / "shared" / "uploads").is_dir()

    # Installed where it was built, activated with a restart, recorded.
    assert machine.runner.cwd_of("npm", "ci") == [release]
    assert machine.services.restarts == [f"releases/{release.name}"]
    app = store.get_app(DOMAIN)
    assert app is not None
    assert app.layout == RELEASES
    assert app.persistent_paths == ["uploads"]
    assert [(r.id, r.status, r.git_commit) for r in store.list_releases(app.id)] == [
        (release.name, ReleaseStatus.ACTIVE.value, commit[:7])
    ]
    deployment = store.list_deployments(DOMAIN)[0]
    assert deployment.status == "success"
    assert deployment.git_commit == commit[:7]
    assert deployment.release_id == release.name


def test_the_unit_and_the_site_are_written_against_current(
    tmp_path: Path, root: Path, store: WASMStore, machine: SimpleNamespace
) -> None:
    """Activation must not need the unit or the site to be rewritten."""
    machine.git.publish(node_tree(tmp_path / "v1"))

    deploy_new(root, machine)

    unit = machine.services.units["rel-example-com"]
    assert unit["working_directory"] == str(root / "current")
    rendered = (
        Environment(  # noqa: S701 - unit files, escaped by their own macros
            loader=PackageLoader("wasm", "templates/systemd"), trim_blocks=True, lstrip_blocks=True
        )
        .get_template("app.service.j2")
        .render(name="wasm-rel-example-com", user="www-data", **unit)
    )
    assert f"WorkingDirectory={root}/current\n" in rendered
    assert machine.web.sites[DOMAIN]["context"]["app_path"] == str(root / "current")
    service = store.get_service("rel-example-com")
    assert service is not None
    assert service.working_directory == str(root / "current")


def test_a_static_build_is_served_through_current(
    tmp_path: Path, root: Path, store: WASMStore, machine: SimpleNamespace
) -> None:
    """nginx's root follows current, so activating a release needs no reload."""
    machine.git.publish(
        write_tree(
            tmp_path / "site",
            {
                "vite.config.ts": "export default {}",
                "package.json": '{"devDependencies": {"vite": "5"}}',
                "package-lock.json": LOCKFILE,
            },
        )
    )
    # The build writes dist/ into the release it runs in.
    original_stream = machine.runner.stream

    def build(argv: Any, **kwargs: Any) -> Any:
        if tuple(argv) == ("npm", "run", "build"):
            write_tree(Path(kwargs["cwd"]), {"dist/index.html": "<h1>ok</h1>"})
        return original_stream(argv, **kwargs)

    machine.runner.stream = build

    deploy_new(root, machine, deployer_class=ViteDeployer)

    site = machine.web.sites[DOMAIN]
    assert site["template"] == "static"
    assert site["context"]["static_dir"] == str(root / "current" / "dist")
    rendered = NginxManager().render_config(DOMAIN, "static", site["context"])
    assert f"root {root}/current/dist;" in rendered
    assert (root / "current" / "dist" / "index.html").is_file()
    assert store.get_site(DOMAIN).document_root == str(root / "current")


def test_a_static_site_deploys_as_a_release_without_a_unit(
    tmp_path: Path, root: Path, store: WASMStore, machine: SimpleNamespace
) -> None:
    """The static pipeline builds releases too, and never asks systemd for anything."""
    machine.git.publish(write_tree(tmp_path / "site", {"public/index.html": "<h1>hi</h1>"}))

    deploy_new(root, machine, deployer_class=StaticDeployer)

    assert machine.services.units == {}
    assert machine.web.sites[DOMAIN]["context"]["static_dir"] == str(root / "current" / "public")
    assert (root / "current" / "public" / "index.html").is_file()


def test_a_first_release_that_does_not_answer_leaves_nothing_behind(
    tmp_path: Path, root: Path, store: WASMStore, machine: SimpleNamespace
) -> None:
    """With nothing to go back to, the pipeline's undo removes everything the deploy made."""
    machine.git.publish(node_tree(tmp_path / "v1", server=BROKEN_SERVER))
    deployer = wire(NodeJSDeployer(verbose=False, runner=machine.runner), machine)
    deployer.configure(DOMAIN, GIT_URL, port=3000, ssl=False, app_path=root, layout=RELEASES)

    with pytest.raises(DeploymentError, match="did not pass its health check") as failure:
        deployer.deploy()

    assert "Connection refused" in failure.value.details
    assert "Error: boom" in failure.value.details
    assert not root.exists()
    assert store.get_app(DOMAIN) is None
    assert machine.services.units == {}
    assert machine.web.sites == {}


# ---------------------------------------------------------------------------
# Updating an application on releases
# ---------------------------------------------------------------------------


def test_an_update_builds_a_new_release_and_keeps_shared_data(
    tmp_path: Path,
    root: Path,
    store: WASMStore,
    machine: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The new release gets the same .env and the uploads the old one wrote."""
    machine.git.publish(node_tree(tmp_path / "v1"))
    deploy_new(root, machine, persistent=["uploads"])
    first = active_id(root)
    (root / "current" / "uploads" / "photo.jpg").write_text("user data")
    env_before = (root / "shared" / ".env").read_text()

    commit = machine.git.publish(node_tree(tmp_path / "v2", server=GOOD_SERVER + "// v2\n"))
    outcome = update(machine, monkeypatch)

    second = active_id(root)
    assert second != first
    assert Path(os.readlink(root / "current")) == Path("releases") / second
    assert second.split("-")[2] == commit[:7]
    assert (root / "current" / "uploads" / "photo.jpg").read_text() == "user data"
    assert (root / "shared" / ".env").read_text() == env_before
    assert outcome.active is True
    assert outcome.restarted == ("rel-example-com",)
    app = store.get_app(DOMAIN)
    statuses = {r.id: r.status for r in store.list_releases(app.id)}
    assert statuses == {first: "superseded", second: "active"}


def test_an_unhealthy_release_rolls_back_to_the_previous_one(
    tmp_path: Path,
    root: Path,
    store: WASMStore,
    machine: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The previous release is active and restarted again, and the failure says why."""
    machine.git.publish(node_tree(tmp_path / "v1"))
    deploy_new(root, machine)
    first = active_id(root)

    machine.git.publish(node_tree(tmp_path / "v2", server=BROKEN_SERVER))
    with pytest.raises(DeploymentError, match=f"{first} is active again") as failure:
        update(machine, monkeypatch)

    assert active_id(root) == first
    # Deploy, the broken release, then the previous one again.
    assert machine.services.restarts[0] == f"releases/{first}"
    assert machine.services.restarts[-1] == f"releases/{first}"
    broken = Path(machine.services.restarts[1]).name
    assert broken != first
    assert not (root / "releases" / broken).exists()

    app = store.get_app(DOMAIN)
    statuses = {r.id: r.status for r in store.list_releases(app.id)}
    assert statuses == {first: "active", broken: "failed"}

    deployment = store.list_deployments(DOMAIN)[0]
    assert deployment.status == "failed"
    # The health output, verbatim: the probe and the process's own words.
    assert "Connection refused" in deployment.error
    assert "Error: boom" in deployment.error
    assert "Connection refused" in failure.value.details
    # The gate asked this application, not whatever holds the default port.
    assert set(machine.probes) == {f"http://127.0.0.1:{PORT}/"}


def test_an_identical_lockfile_reuses_the_installed_dependencies(
    tmp_path: Path,
    root: Path,
    store: WASMStore,
    machine: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No install runs; node_modules is copied from the active release and the log says so."""
    machine.git.publish(node_tree(tmp_path / "v1"))
    deploy_new(root, machine)
    first = active_id(root)

    machine.git.publish(node_tree(tmp_path / "v2", server=GOOD_SERVER + "// v2\n"))
    update(machine, monkeypatch)
    second = active_id(root)

    assert machine.runner.cwd_of("npm", "ci") == [root / "releases" / first]
    assert (
        "cp",
        "-a",
        "--reflink=auto",
        str(root / "releases" / first / "node_modules"),
        str(root / "releases" / second / "node_modules"),
    ) in machine.runner.calls
    # Its own copy: the file says which release installed it.
    marker = root / "releases" / second / "node_modules" / ".installed-by"
    assert marker.read_text() == first
    log = Path(store.list_deployments(DOMAIN)[0].log_path).read_text()
    assert f"Dependencies reused from {first}" in log


def test_a_changed_lockfile_installs_again(
    tmp_path: Path,
    root: Path,
    store: WASMStore,
    machine: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reuse is only for byte-identical lockfiles."""
    machine.git.publish(node_tree(tmp_path / "v1"))
    deploy_new(root, machine)

    machine.git.publish(node_tree(tmp_path / "v2", lockfile=LOCKFILE.replace("3", "4")))
    update(machine, monkeypatch)

    assert len(machine.runner.cwd_of("npm", "ci")) == 2
    assert not machine.runner.ran("cp")


# ---------------------------------------------------------------------------
# Dependency reuse must key on the runtime, not only the lockfile
# ---------------------------------------------------------------------------


def test_a_stamp_is_written_after_a_real_install(
    tmp_path: Path, root: Path, store: WASMStore, machine: SimpleNamespace
) -> None:
    """A fresh install records the runtime it was built with, so the next deploy can trust it."""
    machine.runner.script(["node", "--version"], stdout="v20.11.0\n")
    machine.runner.script(["npm", "--version"], stdout="10.2.4\n")
    machine.git.publish(node_tree(tmp_path / "v1"))
    deploy_new(root, machine)

    stamp = json.loads(
        (root / "releases" / active_id(root) / "node_modules" / ".wasm-runtime.json").read_text()
    )
    assert stamp == {"node": "v20.11.0", "npm": "10.2.4"}


def test_matching_runtime_reuses_dependencies(
    tmp_path: Path,
    root: Path,
    store: WASMStore,
    machine: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The runtime is queried and found unchanged, so the copy still happens."""
    machine.runner.script(["node", "--version"], stdout="v20.11.0\n")
    machine.runner.script(["npm", "--version"], stdout="10.2.4\n")
    machine.git.publish(node_tree(tmp_path / "v1"))
    deploy_new(root, machine)
    first = active_id(root)

    machine.git.publish(node_tree(tmp_path / "v2", server=GOOD_SERVER + "// v2\n"))
    update(machine, monkeypatch)
    second = active_id(root)

    assert machine.runner.cwd_of("npm", "ci") == [root / "releases" / first]
    assert (
        "cp",
        "-a",
        "--reflink=auto",
        str(root / "releases" / first / "node_modules"),
        str(root / "releases" / second / "node_modules"),
    ) in machine.runner.calls
    log = Path(store.list_deployments(DOMAIN)[0].log_path).read_text()
    assert f"Dependencies reused from {first}" in log


def test_a_node_version_change_reinstalls_dependencies(
    tmp_path: Path,
    root: Path,
    store: WASMStore,
    machine: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """apt upgrading Node must not hand a new release native modules built for the old ABI."""
    machine.runner.script(["node", "--version"], stdout="v20.11.0\n")
    machine.git.publish(node_tree(tmp_path / "v1"))
    deploy_new(root, machine)

    machine.runner.script(["node", "--version"], stdout="v22.3.0\n")
    machine.git.publish(node_tree(tmp_path / "v2", server=GOOD_SERVER + "// v2\n"))
    update(machine, monkeypatch)

    assert len(machine.runner.cwd_of("npm", "ci")) == 2
    assert not machine.runner.ran("cp")
    log = Path(store.list_deployments(DOMAIN)[0].log_path).read_text()
    assert "Dependencies reinstalled: the runtime changed (node v20.11.0 -> v22.3.0)" in log


def test_a_package_manager_version_change_reinstalls_dependencies(
    tmp_path: Path,
    root: Path,
    store: WASMStore,
    machine: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A different npm on the same Node still produces a different install."""
    machine.runner.script(["npm", "--version"], stdout="10.2.4\n")
    machine.git.publish(node_tree(tmp_path / "v1"))
    deploy_new(root, machine)

    machine.runner.script(["npm", "--version"], stdout="10.8.1\n")
    machine.git.publish(node_tree(tmp_path / "v2", server=GOOD_SERVER + "// v2\n"))
    update(machine, monkeypatch)

    assert len(machine.runner.cwd_of("npm", "ci")) == 2
    assert not machine.runner.ran("cp")
    log = Path(store.list_deployments(DOMAIN)[0].log_path).read_text()
    assert "Dependencies reinstalled: the runtime changed (npm 10.2.4 -> 10.8.1)" in log


def test_a_release_without_a_runtime_stamp_reinstalls(
    tmp_path: Path,
    root: Path,
    store: WASMStore,
    machine: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A release built before this change carries no stamp and must not be reused blindly."""
    machine.git.publish(node_tree(tmp_path / "v1"))
    deploy_new(root, machine)
    first = active_id(root)
    (root / "releases" / first / "node_modules" / ".wasm-runtime.json").unlink()

    machine.git.publish(node_tree(tmp_path / "v2", server=GOOD_SERVER + "// v2\n"))
    update(machine, monkeypatch)

    assert len(machine.runner.cwd_of("npm", "ci")) == 2
    assert not machine.runner.ran("cp")
    log = Path(store.list_deployments(DOMAIN)[0].log_path).read_text()
    assert "Dependencies reinstalled: no runtime stamp" in log


def test_a_python_interpreter_change_reinstalls_the_venv(
    tmp_path: Path,
    root: Path,
    store: WASMStore,
    machine: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Python upgrade must not hand a new release a venv built for the old interpreter."""
    machine.runner.script(["python3", "--version"], stdout="Python 3.11.6\n")
    machine.git.publish(python_tree(tmp_path / "v1"))
    deploy_new(root, machine, deployer_class=PythonDeployer)

    machine.runner.script(["python3", "--version"], stdout="Python 3.12.1\n")
    machine.git.publish(python_tree(tmp_path / "v2"))
    update(machine, monkeypatch, deployer_class=PythonDeployer)

    installs = [call for call in machine.runner.calls if "requirements.txt" in call]
    assert len(installs) == 2
    assert not machine.runner.ran("cp")
    log = Path(store.list_deployments(DOMAIN)[0].log_path).read_text()
    assert (
        "Dependencies reinstalled: the runtime changed (python3 Python 3.11.6 -> Python 3.12.1)"
        in log
    )


def test_old_releases_are_pruned_to_the_retention(
    tmp_path: Path,
    root: Path,
    store: WASMStore,
    machine: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """keep_releases survive on disk and in the store; older ones go from both."""
    machine.git.publish(node_tree(tmp_path / "v1"))
    deploy_new(root, machine)
    app = store.get_app(DOMAIN)
    app.keep_releases = 2
    store.update_app(app)

    for version in (2, 3):
        machine.git.publish(node_tree(tmp_path / f"v{version}", server=f"{GOOD_SERVER}//{version}"))
        update(machine, monkeypatch)

    on_disk = [r.id for r in ReleaseManager(root).list()]
    assert len(on_disk) == 2
    assert on_disk[0] == active_id(root)
    assert sorted(r.id for r in store.list_releases(app.id)) == sorted(on_disk)


# ---------------------------------------------------------------------------
# In-place applications stay in place
# ---------------------------------------------------------------------------


def test_an_in_place_app_updated_by_v2_stays_in_place(
    tmp_path: Path, root: Path, store: WASMStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    Review Focus: the release engine is never applied to a 1.x app implicitly.

    The server default for new applications is releases; an application
    already deployed in place must keep updating in place anyway, with no
    releases/, current, shared/ or repo/ appearing in its tree.
    """
    assert configured_layout() == RELEASES
    node_tree(root)
    (root / ".git").mkdir()
    (root / "uploads").mkdir()
    (root / "uploads" / "photo.jpg").write_text("user data")
    store.create_app(
        App(domain=DOMAIN, app_type="nodejs", source=GIT_URL, port=3000, app_path=str(root))
    )
    calls: list[tuple[Any, ...]] = []
    monkeypatch.setattr(
        lifecycle,
        "RollbackManager",
        lambda verbose=False: SimpleNamespace(
            create_pre_deploy_backup=lambda **kw: calls.append(("backup",))
        ),
    )
    monkeypatch.setattr(
        lifecycle,
        "SourceManager",
        lambda verbose=False: SimpleNamespace(
            pull=lambda path, branch=None: calls.append(("pull", path))
        ),
    )
    monkeypatch.setattr(
        lifecycle,
        "ServiceManager",
        lambda verbose=False: SimpleNamespace(
            get_status=lambda name: {"exists": True, "active": True},
            restart=lambda name: calls.append(("restart", name)),
        ),
    )
    monkeypatch.setattr(lifecycle.time, "sleep", lambda seconds: None)
    runner = TreeRunner()

    def make_deployer(app_type: str, verbose: bool = False) -> NodeJSDeployer:
        deployer = NodeJSDeployer(verbose=False, runner=runner)
        deployer.source_manager = SourceManager(runner=runner)
        return deployer

    monkeypatch.setattr(lifecycle, "get_deployer", make_deployer)

    outcome = lifecycle.update_app(DOMAIN)

    for created in ("releases", "current", "shared", "repo"):
        assert not os.path.lexists(root / created), created
    assert calls == [("backup",), ("pull", root), ("restart", "rel-example-com")]
    assert runner.cwd_of("npm", "ci") == [root]
    assert (root / "uploads" / "photo.jpg").read_text() == "user data"
    assert store.get_app(DOMAIN).layout == INPLACE
    assert outcome.active is True


def test_a_redeploy_never_changes_the_layout_it_was_not_asked_to() -> None:
    """The server default is for new applications; an existing one keeps its own."""
    inplace = App(domain=DOMAIN, layout=INPLACE)
    releases = App(domain=DOMAIN, layout=RELEASES)

    assert choose_layout(inplace, CONFIGURED, app_type="nodejs", supports_releases=True) == INPLACE
    assert choose_layout(releases, None, app_type="nodejs", supports_releases=True) == RELEASES
    with pytest.raises(DeploymentError, match="does not change it"):
        choose_layout(inplace, RELEASES, app_type="nodejs", supports_releases=True)


def test_a_new_app_gets_the_configured_layout_unless_it_asks() -> None:
    """None is the pre-2.0 programmatic default; CONFIGURED is what create sends."""
    inplace_config = SimpleNamespace(get=lambda key, default=None: INPLACE)

    assert choose_layout(None, None, app_type="nodejs", supports_releases=True) == INPLACE
    assert choose_layout(None, CONFIGURED, app_type="nodejs", supports_releases=True) == RELEASES
    assert (
        choose_layout(
            None,
            CONFIGURED,
            app_type="nodejs",
            supports_releases=True,
            config=inplace_config,  # type: ignore[arg-type]
        )
        == INPLACE
    )


def test_types_without_a_release_pipeline_stay_in_place() -> None:
    """The default is a preference for them; an explicit request is an error."""
    assert choose_layout(None, CONFIGURED, app_type="monorepo", supports_releases=False) == INPLACE
    with pytest.raises(DeploymentError, match="cannot use the releases layout"):
        choose_layout(None, RELEASES, app_type="monorepo", supports_releases=False)


# ---------------------------------------------------------------------------
# --type auto
# ---------------------------------------------------------------------------


def test_auto_detects_inside_the_release_and_hands_it_over(
    tmp_path: Path, root: Path, store: WASMStore, machine: SimpleNamespace
) -> None:
    """Detection needs the source; the release it was fetched into is the one built."""
    machine.git.publish(node_tree(tmp_path / "v1"))
    auto = AutoDeployer()
    auto.configure(DOMAIN, GIT_URL, app_path=root, layout=RELEASES)
    auto.source_manager = machine.git

    delegate = auto.resolve()

    assert delegate.APP_TYPE == "nodejs"
    assert delegate.source_already_fetched is True
    assert delegate.build_path.parent == root / "releases"
    assert (delegate.build_path / "server.js").is_file()
    assert len([c for c in machine.git.calls if c[0] == "export"]) == 1


def test_auto_leaves_a_monorepo_in_place_under_the_default(
    tmp_path: Path, root: Path, store: WASMStore, machine: SimpleNamespace
) -> None:
    """A monorepo found in a release is fetched again in place; nothing of the release stays."""
    source = write_tree(
        tmp_path / "mono",
        {
            "turbo.json": "{}",
            "pnpm-workspace.yaml": "packages:\n  - apps/*\n",
            "apps/web/package.json": PACKAGE_JSON,
            "apps/api/package.json": PACKAGE_JSON,
        },
    )
    auto = AutoDeployer()
    auto.configure(DOMAIN, str(source), app_path=root, layout=CONFIGURED)
    auto.source_manager = SourceManager(runner=FakeRunner())

    delegate = auto.resolve()

    assert delegate.APP_TYPE == "monorepo"
    assert (root / "turbo.json").is_file()
    assert not (root / "releases").exists()


def test_auto_leaves_nothing_behind_when_the_source_is_empty(
    tmp_path: Path, root: Path, store: WASMStore, machine: SimpleNamespace
) -> None:
    """A new application that turned out to be nothing does not keep a cache or a release."""
    machine.git.publish((tmp_path / "empty").resolve())
    (tmp_path / "empty").mkdir()
    auto = AutoDeployer()
    auto.configure(DOMAIN, GIT_URL, app_path=root, layout=RELEASES)
    auto.source_manager = machine.git

    with pytest.raises(DeploymentError, match="Nothing to deploy"):
        auto.resolve()

    assert not root.exists()


def test_a_release_that_could_not_be_filled_is_removed(
    tmp_path: Path, root: Path, machine: SimpleNamespace
) -> None:
    """A half-exported directory named like a release would be a rollback target."""
    machine.git.publish(node_tree(tmp_path / "v1"))

    def broken_export(repository: Path, commit: str, destination: Path) -> None:
        (destination / "partial").write_text("")
        raise SourceError("git archive failed", details="fatal: not a tree object")

    machine.git.export_commit = broken_export

    with pytest.raises(SourceError, match="git archive failed"):
        stage_release(
            GIT_URL,
            None,
            releases=ReleaseManager(root),
            source_manager=machine.git,  # type: ignore[arg-type]
            logger=Logger(),
        )

    assert list((root / "releases").iterdir()) == []


def test_repeated_probe_failures_read_as_one_line() -> None:
    """Fifteen identical refusals are one fact; the journal below them is the news."""
    attempts = [f"Health check attempt {n} failed: refused" for n in range(1, 16)]

    assert _collapse_attempts([*attempts, "Health check attempt 16 failed: timed out"]) == [
        "Health check attempts 1-15 failed: refused",
        "Health check attempt 16 failed: timed out",
    ]


# ---------------------------------------------------------------------------
# Python
# ---------------------------------------------------------------------------


def test_python_on_releases_runs_through_current_and_python_m(
    tmp_path: Path, store: WASMStore
) -> None:
    """A reused venv's scripts name the release that made them; python -m does not."""
    app_root = tmp_path / "app"
    release = app_root / "releases" / "20260925-143012-a1b2c3d"
    write_tree(release, {"requirements.txt": "flask\n", "app.py": "app = Flask(__name__)\n"})
    deployer = PythonDeployer(verbose=False, runner=FakeRunner())
    deployer.configure(DOMAIN, str(tmp_path), port=8000, app_path=app_root)
    deployer._layout = RELEASES
    deployer._staged = StagedRelease(release, None, ReleaseManager(app_root))
    deployer.pre_install()

    assert deployer.get_install_command() == [
        str(release / "venv" / "bin" / "python"),
        "-m",
        "pip",
        "install",
        "-r",
        "requirements.txt",
    ]
    assert deployer.get_start_command().startswith(
        f"{app_root}/current/venv/bin/python -m gunicorn "
    )


def test_python_in_place_commands_are_unchanged(tmp_path: Path, store: WASMStore) -> None:
    """The in-place layout keeps the exact argv it always had."""
    write_tree(tmp_path / "app", {"requirements.txt": "flask\n"})
    deployer = PythonDeployer(verbose=False, runner=FakeRunner())
    deployer.configure(DOMAIN, str(tmp_path), port=8000, app_path=tmp_path / "app")
    deployer.pre_install()

    venv = tmp_path / "app" / "venv"
    assert deployer.get_install_command() == [
        str(venv / "bin" / "pip"),
        "install",
        "-r",
        "requirements.txt",
    ]
    assert deployer.get_start_command().startswith(f"{venv}/bin/gunicorn ")


# ---------------------------------------------------------------------------
# Lockfiles
# ---------------------------------------------------------------------------


def test_lockfiles_match_only_byte_for_byte(tmp_path: Path) -> None:
    """Presence and content of every lockfile must agree, and there must be one."""
    a = write_tree(tmp_path / "a", {"package-lock.json": LOCKFILE})
    b = write_tree(tmp_path / "b", {"package-lock.json": LOCKFILE})
    c = write_tree(tmp_path / "c", {"package-lock.json": LOCKFILE, "yarn.lock": "x"})
    d = write_tree(tmp_path / "d", {"server.js": ""})
    e = write_tree(tmp_path / "e", {"server.js": ""})

    assert lockfiles_match(a, b)
    assert not lockfiles_match(a, c)
    assert not lockfiles_match(d, e)


# ---------------------------------------------------------------------------
# The repository cache
# ---------------------------------------------------------------------------


def test_the_cache_is_fetched_and_reset_never_cleaned(tmp_path: Path) -> None:
    """An update of the cache touches tracked files only."""
    cache = tmp_path / "repo"
    (cache / ".git").mkdir(parents=True)
    runner = FakeRunner()
    runner.script(
        [
            "git",
            "-c",
            "protocol.ext.allow=never",
            "-c",
            "protocol.file.allow=never",
            "remote",
            "get-url",
            "origin",
        ],
        stdout=GIT_URL + "\n",
    )
    runner.script(
        [
            "git",
            "-c",
            "protocol.ext.allow=never",
            "-c",
            "protocol.file.allow=never",
            "rev-parse",
            "--abbrev-ref",
            "HEAD",
        ],
        stdout="main\n",
    )
    runner.script(
        [
            "git",
            "-c",
            "protocol.ext.allow=never",
            "-c",
            "protocol.file.allow=never",
            "rev-parse",
            "HEAD",
        ],
        stdout="a" * 40 + "\n",
    )

    commit = SourceManager(runner=runner).sync_cache(GIT_URL, cache, "main")

    git_calls = [call[5:] for call in runner.calls_to("git")]
    assert commit == "a" * 40
    assert ("fetch", "origin", "+refs/heads/main:refs/remotes/origin/main") in git_calls
    assert ("reset", "--hard", "origin/main") in git_calls
    assert not any(call and call[0] == "clean" for call in git_calls)


def test_a_commit_is_exported_with_two_commands_and_no_shell(tmp_path: Path) -> None:
    """git archive to a file, then tar out of it; the archive does not stay behind."""
    release = tmp_path / "releases" / "20260925-143012-aaaaaaa"
    release.mkdir(parents=True)
    runner = FakeRunner()

    SourceManager(runner=runner).export_commit(tmp_path / "repo", "a" * 40, release)

    archive = tmp_path / "releases" / f".{release.name}.tar"
    assert runner.calls[0][5:] == ("archive", "--format=tar", f"--output={archive}", "a" * 40)
    assert runner.calls[1] == ("tar", "-xf", str(archive), "-C", str(release))
    assert not archive.exists()


def test_export_refuses_something_that_is_not_a_commit(tmp_path: Path) -> None:
    """The commit becomes an argument; a leading dash would be an option."""
    with pytest.raises(SourceError, match="not a commit id"):
        SourceManager(runner=FakeRunner()).export_commit(tmp_path, "--output=/etc/x", tmp_path)


# ---------------------------------------------------------------------------
# --dry-run
# ---------------------------------------------------------------------------


def test_a_rehearsed_release_deploy_changes_nothing(
    tmp_path: Path, root: Path, store: WASMStore, machine: SimpleNamespace
) -> None:
    """No directory, no link, no row, no mutating command."""
    source = node_tree(tmp_path / "src")
    inner = FakeRunner()
    set_fs(DryRunFileSystem())
    deployer = wire(NodeJSDeployer(verbose=False, runner=DryRunRunner(inner)), machine)
    deployer.source_manager = SourceManager(runner=DryRunRunner(inner))
    deployer.configure(DOMAIN, str(source), port=3000, ssl=False, app_path=root, layout=RELEASES)

    assert deployer.deploy() is True

    assert not os.path.lexists(root)
    assert store.get_app(DOMAIN) is None
    assert store.list_deployments(DOMAIN) == []
    # Only probes reached the machine.
    assert all(is_read_only(call) for call in inner.calls), inner.calls
