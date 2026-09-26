# Copyright (c) 2024-2026 Yago Lopez Prado
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Going back to a deployment of an in-place application, through the real paths.

2.1's first cut restored the deployment's snapshot backup for every in-place
type. A backup leaves out ``.git``, ``dist``, ``build`` and ``*.log`` wherever
they are, and the restore replaced the whole tree: the application stopped
being a git checkout, a tracked ``build/`` source folder was lost, uploads
and SQLite files went back in time, a rotated ``.env`` came back, a failed
rebuild was a warning and nothing probed the result. What is pinned:

- **A git checkout goes back by commit**: ``update_app`` with the
  deployment's commit, which keeps the tree, restarts behind the health gate
  and records the operation as a rollback. Monorepos and stacks go through
  their own deployers, which handle their units and images.
- **The snapshot restore is what is left for a tree without history**, and
  there it keeps ``.git``, keeps the ``.env`` unless asked, rebuilds strictly
  and passes the same health gate; a failed rebuild or gate is a failed row.
- **Availability says the same**, and a snapshot is only linked when both
  sides know the commit and it is the deployment's.

The key tests here run the real ``RollbackManager``, ``BackupManager``,
``SourceManager`` and deployer over a fake runner: nothing is monkeypatched
away but the process runner, the unit directory and the HTTP probe.
"""

from __future__ import annotations

import tarfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from wasm.core.config import Config
from wasm.core.exceptions import DeploymentError
from wasm.core.runner import CommandResult, FakeRunner, set_runner
from wasm.core.store import App, DeploymentStatus, WASMStore
from wasm.deployers import base as base_module
from wasm.deployers import lifecycle
from wasm.deployers.interface import UpdateResult
from wasm.managers.backup_manager import BackupManager, BackupMetadata, RollbackManager
from wasm.managers.service_manager import WASM_UNIT_MARKER, ServiceManager

DOMAIN = "shop.example.com"
APP = "shop-example-com"
PORT = 3100

OLD = "1111111aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
NEW = "2222222bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"

PACKAGE_JSON = '{"name": "shop", "scripts": {"start": "node server.js", "build": "true"}}\n'


# ---------------------------------------------------------------------------
# The machine
# ---------------------------------------------------------------------------


class Machine(FakeRunner):
    """
    git, npm and systemd as an in-place checkout sees them.

    A checkout of a commit rewrites the tracked files of the tree, exactly as
    ``git checkout --force`` would, and leaves everything else alone.

    Attributes:
        root: The application directory.
        head: The commit checked out.
        trees: The tracked files of each commit.
        build_fails: ``npm run build`` fails.
    """

    def __init__(self, root: Path) -> None:
        super().__init__()
        self.root = root
        self.head = NEW
        self.detached = False
        self.build_fails = False
        self.trees: dict[str, dict[str, str]] = {
            OLD: {"package.json": PACKAGE_JSON, "server.js": "// v1\n"},
            NEW: {"package.json": PACKAGE_JSON, "server.js": "// v2\n"},
        }

    def _git(self, args: tuple[str, ...]) -> tuple[int, str, str] | None:
        if args[:2] == ("rev-parse", "--verify"):
            wanted = args[2].split("^")[0]
            matches = [c for c in self.trees if c.startswith(wanted)]
            return (0, matches[0] + "\n", "") if len(matches) == 1 else (128, "", "fatal")
        if args == ("rev-parse", "HEAD"):
            return 0, self.head + "\n", ""
        if args == ("rev-parse", "--short", "HEAD"):
            return 0, self.head[:7] + "\n", ""
        if args == ("rev-parse", "--abbrev-ref", "HEAD"):
            return 0, ("HEAD" if self.detached else "main") + "\n", ""
        if args[:3] == ("symbolic-ref", "--quiet", "--short"):
            return (1, "", "") if self.detached else (0, "main\n", "")
        if args == ("config", "--get", "wasm.branch"):
            return 0, "main\n", ""
        if args[:3] == ("checkout", "--force", "--detach"):
            self.head = args[3]
            self.detached = True
            for name, content in self.trees[self.head].items():
                (self.root / name).write_text(content)
            return 0, "", ""
        if args[:2] == ("log", "-1"):
            return 0, f"commit {self.head[:7]}\n", ""
        return None

    def _answer(self, argv: Any, result: CommandResult) -> CommandResult:
        args = tuple(argv)
        if args and args[0] == "git":
            rest = list(args[1:])
            while rest[:1] == ["-c"]:
                rest = rest[2:]
            custom = self._git(tuple(rest))
            if custom is not None:
                code, out, err = custom
                return CommandResult(argv=args, exit_code=code, stdout=out, stderr=err)
        if args[:3] == ("npm", "run", "build") and self.build_fails:
            return CommandResult(argv=args, exit_code=1, stderr="Error: build broke")
        return result

    def run(self, argv: Any, **kwargs: Any) -> CommandResult:
        return self._answer(argv, super().run(argv, **kwargs))

    def stream(self, argv: Any, **kwargs: Any) -> CommandResult:
        return self._answer(argv, super().stream(argv, **kwargs))

    def ran(self, *prefix: str) -> list[tuple[str, ...]]:
        """Every call whose argv contains ``prefix`` in order, contiguously."""
        size = len(prefix)
        return [
            call
            for call in self.calls
            if any(tuple(call[i : i + size]) == prefix for i in range(len(call) - size + 1))
        ]


class Probe:
    """The HTTP probe: answers until told not to."""

    def __init__(self) -> None:
        self.urls: list[str] = []
        self.healthy = True

    def __call__(self, url: str, **kwargs: Any) -> bool:
        self.urls.append(url)
        if not self.healthy and kwargs.get("on_attempt") is not None:
            kwargs["on_attempt"]("Health check attempt 1 failed: HTTP 502")
        return self.healthy


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    """
    An in-place Node application, its store, its unit, its backups directory.

    Yields:
        A namespace with the store, the root, the machine and the probe.
    """
    apps = tmp_path / "apps"
    root = apps / APP
    root.mkdir(parents=True)

    config = Config()
    previous = {key: config.get(key) for key in ("apps_directory", "backup.directory")}
    config.set("apps_directory", str(apps))
    config.set("backup.directory", str(tmp_path / "backups"))

    units = tmp_path / "systemd"
    units.mkdir()
    (units / f"{APP}.service").write_text(f"# {WASM_UNIT_MARKER}\n[Service]\n")
    monkeypatch.setattr(ServiceManager, "SYSTEMD_DIR", units)
    monkeypatch.setattr(ServiceManager, "UNIT_SEARCH_DIRS", (units,))

    WASMStore.reset_instance()
    store = WASMStore(tmp_path / "wasm.db")
    machine = Machine(root)
    set_runner(machine)
    probe = Probe()
    monkeypatch.setattr(base_module, "wait_until_healthy", probe)
    monkeypatch.setattr(lifecycle, "wait_until_healthy", probe)
    monkeypatch.setattr(lifecycle.time, "sleep", lambda seconds: None)
    # Ids have a resolution of one second, and a test takes the snapshot and
    # the rollback's safety backup within the same one.
    taken = iter(range(100000, 999999))
    monkeypatch.setattr(
        BackupManager,
        "_generate_backup_id",
        lambda self, domain: f"{APP}_20260926_{next(taken)}",
    )

    class World:
        pass

    w: Any = World()
    w.store, w.root, w.machine, w.probe, w.tmp = store, root, machine, probe, tmp_path
    try:
        yield w
    finally:
        set_runner(None)
        WASMStore.reset_instance()
        for key, value in previous.items():
            config.set(key, value if value is not None else "")


def register(store: WASMStore, root: Path, *, app_type: str = "nodejs", git: bool = True) -> App:
    """The application's row and its tree at NEW, as the last update left it."""
    for name, content in Machine(root).trees[NEW].items():
        (root / name).write_text(content)
    if git:
        (root / ".git").mkdir(exist_ok=True)
        (root / ".git" / "HEAD").write_text(f"{NEW}\n")
    return store.create_app(
        App(
            domain=DOMAIN,
            app_type=app_type,
            source="https://github.com/example/shop.git" if git else "/srv/src/shop",
            branch="main",
            port=PORT,
            app_path=str(root),
            status="running",
        )
    )


def finished(store: WASMStore, status: str, commit: str | None) -> int:
    """A finished deployment row."""
    deployment_id = store.record_deployment_start(DOMAIN, "cli", git_commit=commit)
    store.finish_deployment(deployment_id, status)
    return deployment_id


# ---------------------------------------------------------------------------
# A git checkout goes back by commit
# ---------------------------------------------------------------------------


def test_going_back_rebuilds_the_commit_in_the_checkout_behind_the_gate(world: Any) -> None:
    """The tree stays a checkout; uploads, .env and tracked build/ are where they were."""
    register(world.store, world.root)
    (world.root / ".env").write_text("SECRET=rotated\n")
    (world.root / "public" / "uploads").mkdir(parents=True)
    (world.root / "public" / "uploads" / "photo.png").write_bytes(b"\x89PNG")
    (world.root / "build").mkdir()
    (world.root / "build" / "tool.py").write_text("tracked source\n")
    (world.root / "data.sqlite").write_bytes(b"SQLite format 3\x00new rows")
    target = finished(world.store, "success", OLD[:7])
    live = finished(world.store, "success", NEW[:7])

    assert lifecycle.rollback_availability([world.store.get_deployment(target)]) == {target: None}
    outcome = lifecycle.rollback_to_deployment(DOMAIN, target, trigger="panel")

    assert world.machine.ran("checkout", "--force", "--detach", OLD)
    assert (world.root / "server.js").read_text() == "// v1\n"
    assert (world.root / ".git").is_dir()
    assert (world.root / ".env").read_text() == "SECRET=rotated\n"
    assert (world.root / "public" / "uploads" / "photo.png").exists()
    assert (world.root / "build" / "tool.py").read_text() == "tracked source\n"
    assert (world.root / "data.sqlite").read_bytes().endswith(b"new rows")
    # Rebuilt, restarted and probed on the application's own port.
    assert world.machine.ran("npm", "run", "build")
    assert world.machine.ran("systemctl", "restart", f"{APP}.service")
    assert world.probe.urls == [f"http://127.0.0.1:{PORT}/"]

    row = world.store.get_deployment(outcome.history_id)
    assert (row.status, row.triggered_by, row.git_commit) == ("success", "panel", OLD[:7])
    assert outcome.backup_id is None and outcome.release_id is None
    # Recorded as a rollback: the build it replaced is rolled back.
    assert world.store.get_deployment(live).status == DeploymentStatus.ROLLED_BACK.value
    assert world.store.get_deployment(target).status == DeploymentStatus.SUCCESS.value
    # The update's safety backup is the snapshot of what served before.
    assert world.store.get_deployment(live).snapshot_backup is not None


def test_a_rollback_that_does_not_answer_is_a_failed_row_with_the_evidence(world: Any) -> None:
    """No warning and a success row: the operation failed, and says why."""
    register(world.store, world.root)
    target = finished(world.store, "success", OLD[:7])
    live = finished(world.store, "success", NEW[:7])
    world.probe.healthy = False

    with pytest.raises(DeploymentError, match="did not pass its health check") as caught:
        lifecycle.rollback_to_deployment(DOMAIN, target)

    assert "HTTP 502" in (caught.value.details or "")
    newest = world.store.list_deployments(DOMAIN)[0]
    assert newest.id not in (target, live)
    assert newest.status == DeploymentStatus.FAILED.value
    assert world.store.get_deployment(live).status == DeploymentStatus.SUCCESS.value


def test_a_rollback_whose_build_fails_restarts_nothing(world: Any) -> None:
    """The previous build keeps serving; the row is failed."""
    register(world.store, world.root)
    target = finished(world.store, "success", OLD[:7])
    finished(world.store, "success", NEW[:7])
    world.machine.build_fails = True

    with pytest.raises(DeploymentError):
        lifecycle.rollback_to_deployment(DOMAIN, target)

    assert not world.machine.ran("systemctl", "restart")
    assert world.store.list_deployments(DOMAIN)[0].status == DeploymentStatus.FAILED.value


@pytest.mark.parametrize("app_type", ["monorepo", "docker-compose"])
def test_monorepos_and_stacks_go_back_through_their_own_deployer(
    world: Any, monkeypatch: pytest.MonkeyPatch, app_type: str
) -> None:
    """They restart their workspaces and images; the commit is checked out first."""
    register(world.store, world.root, app_type=app_type)
    target = finished(world.store, "success", OLD[:7])
    live = finished(world.store, "success", NEW[:7])
    seen: dict[str, Any] = {}

    class Deployer:
        compose_file = None

        def __init__(self, verbose: bool = False) -> None:
            pass

        def update(self, on_step: Any = None) -> UpdateResult:
            seen["previous"] = self.previous_commit  # type: ignore[attr-defined]
            seen["head"] = world.machine.head
            row = world.store.record_deployment_start(DOMAIN, self.trigger, git_commit=OLD[:7])  # type: ignore[attr-defined]
            world.store.finish_deployment(row, "success")
            self.last_deployment_id = row
            return UpdateResult("pnpm", False, app_type == "docker-compose", "", restarted=())

    monkeypatch.setattr(lifecycle, "MonorepoDeployer", Deployer)
    monkeypatch.setattr(lifecycle, "DockerComposeDeployer", Deployer)

    outcome = lifecycle.rollback_to_deployment(DOMAIN, target, trigger="panel")

    assert seen == {"previous": NEW[:7], "head": OLD}
    assert not world.machine.ran("systemctl", "start", APP)
    assert world.store.get_deployment(live).status == DeploymentStatus.ROLLED_BACK.value
    assert outcome.history_id is not None


def test_in_a_checkout_the_live_deployment_is_not_something_to_go_back_to(world: Any) -> None:
    """The newest finished deployment serves; anything older can be rebuilt."""
    register(world.store, world.root)
    older = finished(world.store, "success", OLD[:7])
    live = finished(world.store, "success", NEW[:7])

    answers = lifecycle.rollback_availability(
        [world.store.get_deployment(older), world.store.get_deployment(live)]
    )

    assert answers[older] is None
    assert answers[live] == f"Deployment {live} is what is live"


def test_after_a_failed_update_the_last_good_deployment_can_be_rebuilt(world: Any) -> None:
    """The checkout is on the broken commit while the old build serves."""
    register(world.store, world.root)
    good = finished(world.store, "success", OLD[:7])
    finished(world.store, "failed", NEW[:7])

    assert lifecycle.rollback_availability([world.store.get_deployment(good)]) == {good: None}


# ---------------------------------------------------------------------------
# A tree without history: the snapshot, hardened
# ---------------------------------------------------------------------------


def snapshot_of_v1(world: Any) -> tuple[int, int, str]:
    """Back the tree up at v1, then move it to v2 as an update would."""
    (world.root / "server.js").write_text("// v1\n")
    (world.root / ".env").write_text("SECRET=old\n")
    backup = BackupManager().create(DOMAIN, include_env=True, tags=["pre-deploy", "auto"])
    assert backup is not None
    target = finished(world.store, "success", None)
    world.store.set_deployment_snapshot(target, backup.id)
    live = finished(world.store, "success", None)
    (world.root / "server.js").write_text("// v2\n")
    (world.root / ".env").write_text("SECRET=rotated\n")
    return target, live, backup.id


def test_a_snapshot_is_restored_rebuilt_and_gated_keeping_the_env(world: Any) -> None:
    """The files of the deployment come back; the .env the operator rotated stays."""
    register(world.store, world.root, git=False)
    target, live, backup_id = snapshot_of_v1(world)

    outcome = lifecycle.rollback_to_deployment(DOMAIN, target, trigger="panel")

    assert (world.root / "server.js").read_text() == "// v1\n"
    assert (world.root / ".env").read_text() == "SECRET=rotated\n"
    assert world.machine.ran("npm", "run", "build")
    assert world.machine.ran("systemctl", "restart", f"{APP}.service")
    assert world.probe.urls == [f"http://127.0.0.1:{PORT}/"]
    assert outcome.backup_id == backup_id
    row = world.store.get_deployment(outcome.history_id)
    assert (row.status, row.triggered_by) == ("success", "panel")
    assert world.store.get_deployment(live).status == DeploymentStatus.ROLLED_BACK.value


def test_the_env_of_the_snapshot_comes_back_only_when_asked(world: Any) -> None:
    """restore_env=True is the explicit way to get the archived secrets back."""
    register(world.store, world.root, git=False)
    target, _, _ = snapshot_of_v1(world)

    lifecycle.rollback_to_deployment(DOMAIN, target, restore_env=True)

    assert (world.root / ".env").read_text() == "SECRET=old\n"


def test_a_git_directory_survives_the_snapshot_restore(world: Any) -> None:
    """An archive never carries .git; the tree must not stop being a checkout."""
    register(world.store, world.root, git=False)
    target, _, _ = snapshot_of_v1(world)
    (world.root / ".git").mkdir()
    (world.root / ".git" / "HEAD").write_text("ref: refs/heads/main\n")

    lifecycle.rollback_to_deployment(DOMAIN, target)

    assert (world.root / ".git" / "HEAD").read_text() == "ref: refs/heads/main\n"
    assert (world.root / "server.js").read_text() == "// v1\n"


def test_a_snapshot_whose_rebuild_fails_is_a_failed_rollback(world: Any) -> None:
    """A failed rebuild used to be a warning under a success row."""
    register(world.store, world.root, git=False)
    target, live, _ = snapshot_of_v1(world)
    world.machine.build_fails = True

    with pytest.raises(DeploymentError, match="build"):
        lifecycle.rollback_to_deployment(DOMAIN, target)

    assert world.store.list_deployments(DOMAIN)[0].status == DeploymentStatus.FAILED.value
    assert world.store.get_deployment(live).status == DeploymentStatus.SUCCESS.value
    assert not world.machine.ran("systemctl", "restart")


def test_a_snapshot_that_does_not_answer_names_the_safety_backup(world: Any) -> None:
    """The gate's evidence, and where what served before the rollback is."""
    register(world.store, world.root, git=False)
    target, _, _ = snapshot_of_v1(world)
    world.probe.healthy = False

    with pytest.raises(DeploymentError, match="did not pass its health check") as caught:
        lifecycle.rollback_to_deployment(DOMAIN, target)

    details = caught.value.details or ""
    assert "HTTP 502" in details
    assert f"wasm rollback {DOMAIN} " in details
    assert world.store.list_deployments(DOMAIN)[0].status == DeploymentStatus.FAILED.value


@pytest.mark.parametrize("app_type", ["monorepo", "docker-compose"])
def test_a_snapshot_is_not_offered_for_types_that_cannot_rebuild_in_place(
    world: Any, app_type: str
) -> None:
    """Without history, a monorepo or a stack has no safe way back but a backup."""
    register(world.store, world.root, app_type=app_type, git=False)
    target = finished(world.store, "success", None)
    world.store.set_deployment_snapshot(target, "whatever")

    reason = lifecycle.rollback_availability([world.store.get_deployment(target)])[target]

    assert reason is not None and "wasm rollback" in reason


# ---------------------------------------------------------------------------
# The snapshot link
# ---------------------------------------------------------------------------


def backup_with(commit: str | None) -> BackupMetadata:
    """A backup's metadata, with the commit the tree was on."""
    return BackupMetadata(
        id="shop-example-com-20260926-120000",
        domain=DOMAIN,
        app_name=APP,
        created_at="2026-09-26T12:00:00",
        size_bytes=1,
        app_type="nodejs",
        version="2.1.0",
        description="Pre-update automatic backup",
        includes_env=True,
        includes_node_modules=False,
        git_commit=commit,
        tags=["pre-deploy", "auto"],
    )


@pytest.mark.parametrize(
    ("backup_commit", "record_commit"),
    [(None, "abc1234"), ("abc1234def56", None), (None, None)],
)
def test_a_snapshot_is_only_linked_when_both_sides_know_the_commit(
    world: Any,
    monkeypatch: pytest.MonkeyPatch,
    backup_commit: str | None,
    record_commit: str | None,
) -> None:
    """An unknown commit on either side could be a half-updated tree."""
    register(world.store, world.root)
    previous = finished(world.store, "success", record_commit)
    manager = RollbackManager()
    monkeypatch.setattr(manager.backup_manager, "create", lambda **kw: backup_with(backup_commit))

    manager.create_pre_deploy_backup(DOMAIN)

    assert world.store.get_deployment(previous).snapshot_backup is None


def test_the_archive_of_an_in_place_tree_leaves_git_out(world: Any) -> None:
    """Why the snapshot restore has to carry .git across: the archive never has it."""
    register(world.store, world.root)
    backup = BackupManager().create(DOMAIN)
    assert backup is not None

    archive = world.tmp / "backups" / APP / f"{backup.id}.tar.gz"
    with tarfile.open(archive) as tar:
        assert not any("/.git" in name for name in tar.getnames())
