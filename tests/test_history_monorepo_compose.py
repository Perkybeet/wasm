# Copyright (c) 2024-2026 Yago Lopez Prado
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Monorepo and docker-compose deployments join the deployment history.

Both deployers are their own pipelines rather than subclasses of the base, and
both used to leave no history at all: a push that rebuilt a monorepo or a
compose stack was invisible in the panel. They now record through the same
construction as the base (:func:`wasm.deployers.recorder.recorder_for`), with
the trigger and the captured log; and they write their application row
through the same registrar, so a redeploy keeps the settings it does not know
about (the v5 columns) instead of rebuilding the row from scratch.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from wasm.core.exceptions import DeploymentError
from wasm.core.runner import FakeRunner
from wasm.core.store import App, WASMStore
from wasm.deployers import lifecycle
from wasm.deployers.docker_compose import DockerComposeDeployer
from wasm.deployers.monorepo import MonorepoDeployer

DOMAIN = "mono.example.com"


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[WASMStore]:
    """A store in the test directory, where the lifecycle and the deployers look."""
    WASMStore.reset_instance()
    instance = WASMStore(tmp_path / "wasm.db")
    monkeypatch.setattr(lifecycle, "get_store", lambda: instance)
    monkeypatch.setattr("wasm.deployers.monorepo.get_store", lambda: instance)
    monkeypatch.setattr("wasm.deployers.docker_compose.get_store", lambda: instance)
    yield instance
    WASMStore.reset_instance()


def existing_app(store: WASMStore, app_type: str, root: Path) -> App:
    """An application whose row carries settings no deploy knows about."""
    app = store.create_app(App(domain=DOMAIN, app_type=app_type, app_path=str(root)))
    app.persistent_paths = ["uploads"]
    app.memory_max_mb, app.cpu_quota_percent, app.tasks_max = 512, 50, 256
    store.update_app(app)
    # The retention has a setter of its own; a full-row write leaves it alone.
    store.set_keep_releases(DOMAIN, 3)
    refreshed = store.get_app(DOMAIN)
    assert refreshed is not None
    return refreshed


def v5_columns(app: App | None) -> tuple[Any, ...]:
    """The settings a redeploy must carry over."""
    assert app is not None
    return (
        app.layout,
        app.keep_releases,
        app.persistent_paths,
        app.memory_max_mb,
        app.cpu_quota_percent,
        app.tasks_max,
    )


def monorepo(root: Path, runner: FakeRunner) -> MonorepoDeployer:
    """A monorepo deployer over a tree with one app workspace."""
    (root / "apps" / "web").mkdir(parents=True)
    (root / "apps" / "web" / "package.json").write_text('{"name": "web"}')
    (root / "package.json").write_text('{"name": "mono", "workspaces": ["apps/*"]}')
    (root / "pnpm-workspace.yaml").write_text("packages:\n  - apps/*\n")
    deployer = MonorepoDeployer(verbose=False, runner=runner)
    deployer.configure(DOMAIN, str(root), app_path=root, trigger="webhook")
    return deployer


def test_a_monorepo_update_writes_a_history_row_with_its_trigger_and_log(
    tmp_path: Path, store: WASMStore, runner: FakeRunner
) -> None:
    """The row says who asked and how it ended; the log holds the build."""
    runner.script(["pnpm", "install"], stdout="Packages: +42\nDone in 3.1s")

    monorepo(tmp_path, runner).update()

    row = store.list_deployments(DOMAIN)[0]
    assert (row.status, row.triggered_by) == ("success", "webhook")
    assert row.log_path is not None
    assert "Packages: +42" in Path(row.log_path).read_text()


def test_a_failed_monorepo_update_is_recorded_with_its_error(
    tmp_path: Path, store: WASMStore, runner: FakeRunner
) -> None:
    """pnpm's own words end up in the row."""
    runner.script(["pnpm", "install"], stderr="ERR_PNPM_OUTDATED_LOCKFILE", exit_code=1)

    with pytest.raises(DeploymentError):
        monorepo(tmp_path, runner).update()

    row = store.list_deployments(DOMAIN)[0]
    assert row.status == "failed"
    assert "ERR_PNPM_OUTDATED_LOCKFILE" in (row.error or "")


def test_update_app_passes_its_trigger_to_a_monorepo(
    tmp_path: Path, store: WASMStore, runner: FakeRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The panel's and the webhook's updates are told apart in the history."""
    root = tmp_path / "mono"
    root.mkdir()
    store.create_app(App(domain=DOMAIN, app_type="monorepo", app_path=str(root)))
    seen: list[str] = []

    def update(self: MonorepoDeployer, on_step: Any = None) -> Any:
        seen.append(self.trigger)
        return lifecycle.UpdateResult(
            package_manager="pnpm", prisma_updated=False, is_static=True, start_command=""
        )

    monkeypatch.setattr(MonorepoDeployer, "update", update)
    monkeypatch.setattr(
        lifecycle,
        "RollbackManager",
        lambda verbose=False: type("R", (), {"create_pre_deploy_backup": lambda *a, **k: None})(),
    )
    monkeypatch.setattr(
        lifecycle,
        "SourceManager",
        lambda verbose=False: type("S", (), {"pull": lambda *a, **k: True})(),
    )

    lifecycle.update_app(DOMAIN, trigger="panel")

    assert seen == ["panel"]


def test_a_monorepo_redeploy_keeps_the_settings_of_its_row(
    tmp_path: Path, store: WASMStore, runner: FakeRunner
) -> None:
    """The row is updated through the registrar, not rebuilt from scratch."""
    before = v5_columns(existing_app(store, "monorepo", tmp_path))

    monorepo(tmp_path, runner)._register_app_in_store("deploying")

    assert v5_columns(store.get_app(DOMAIN)) == before


def compose(root: Path, runner: FakeRunner) -> DockerComposeDeployer:
    """A compose deployer over a tree with a compose file."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "docker-compose.yml").write_text("services:\n  web:\n    image: nginx\n")
    deployer = DockerComposeDeployer(verbose=False, runner=runner)
    deployer.configure(DOMAIN, str(root), app_path=root, trigger="panel")
    return deployer


def test_a_compose_update_writes_a_history_row(
    tmp_path: Path, store: WASMStore, runner: FakeRunner
) -> None:
    """Rebuild and recreate, recorded with trigger and log."""
    runner.script(["docker", "compose"], stdout="Container mono-web-1  Started")

    compose(tmp_path / "stack", runner).update()

    row = store.list_deployments(DOMAIN)[0]
    assert (row.status, row.triggered_by) == ("success", "panel")
    log = Path(row.log_path or "").read_text()
    assert "Images built successfully" in log
    assert "Container mono-web-1  Started" in log


def test_a_compose_redeploy_updates_its_row_and_keeps_its_settings(
    tmp_path: Path, store: WASMStore, runner: FakeRunner
) -> None:
    """It used to insert a second row, fail on the unique domain, and warn."""
    root = tmp_path / "stack"
    before = v5_columns(existing_app(store, "docker-compose", root))
    deployer = compose(root, runner)
    deployer._discover_compose_file()
    deployer._parse_compose_services()

    deployer._register_app()

    app = store.get_app(DOMAIN)
    assert v5_columns(app) == before
    assert app is not None and app.status == "running" and app.app_type == "docker-compose"


def test_a_failed_compose_redeploy_leaves_the_stack_that_was_serving(
    tmp_path: Path, store: WASMStore, runner: FakeRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Undoing a redeploy would delete the directory and the row of a live stack."""
    root = tmp_path / "stack"
    existing_app(store, "docker-compose", root)
    deployer = compose(root, runner)
    # Redeploying over the live stack is only done when asked (--force).
    deployer.replace_existing = True
    monkeypatch.setattr(deployer, "_fetch_source", lambda: None)
    runner.script(["docker", "compose"], stderr="build failed", exit_code=1)

    with pytest.raises(Exception, match="Failed to build"):
        deployer.deploy()

    assert (root / "docker-compose.yml").is_file()
    app = store.get_app(DOMAIN)
    assert app is not None and app.status == "failed"
    assert store.list_deployments(DOMAIN)[0].status == "failed"
