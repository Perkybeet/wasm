# Copyright (c) 2024-2026 Yago Lopez Prado
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Tests for going back to a deployment, and the API of the deployment page.

An in-place update backs the tree up before it touches it, and that tree is
exactly what the previous deployment produced. The backup is recorded as that
deployment's snapshot, which is what makes "roll back to this deployment" a
one-step operation in place, as activating a release is on releases. What is
pinned:

- **The snapshot is linked only when it is true**: to the most recent
  finished deployment, only if it succeeded and its commit matches, only in
  place, never in a rehearsal.
- **Going back** restores the snapshot in place and activates the release on
  releases; a deployment with nothing to go back to is refused with why.
- **The API** exposes ``snapshot_backup`` and ``rollback_available``, queues a
  rebuild of a deployment's exact commit and a rollback to it, refuses an
  update that would rebuild the live commit with ``409 nothing_new`` unless
  forced, and asks the ``deploy`` scope for all of it.
"""

# The pipeline fixtures are imported rather than replicated, so there stays
# one definition of the fake machine.
# ruff: noqa: F811

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.test_rebuild_commit import git  # noqa: F401  (pytest resolves fixtures by name)
from tests.test_release_activation import two_releases  # noqa: F401
from tests.test_release_pipeline import (  # noqa: F401
    DOMAIN,
    active_id,
    machine,
    root,
    store,
)
from wasm.core.exceptions import DeploymentError, WASMError
from wasm.core.store import App, DeploymentStatus, WASMStore
from wasm.deployers import lifecycle
from wasm.managers import backup_manager as backup_module
from wasm.managers.backup_manager import BackupMetadata, RollbackManager
from wasm.web import auth as web_auth
from wasm.web.api import deployments as deployments_api
from wasm.web.api import jobs as jobs_api
from wasm.web.api.auth import get_current_session
from wasm.web.api.deps import install_error_handlers
from wasm.web.jobs import Job, JobStatus

INPLACE = "shop.example.com"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def backup(backup_id: str = "shop-example-com-20260926-120000", commit: str | None = None) -> Any:
    """A backup's metadata, as BackupManager.create returns it."""
    return BackupMetadata(
        id=backup_id,
        domain=INPLACE,
        app_name="shop-example-com",
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


def deployment(
    store: WASMStore,
    status: str,
    *,
    domain: str = INPLACE,
    commit: str | None = "abc1234",
    release_id: str | None = None,
) -> int:
    """Write a finished (or running) deployment row."""
    deployment_id = store.record_deployment_start(domain, "cli", git_commit=commit)
    if status != DeploymentStatus.RUNNING.value:
        store.finish_deployment(deployment_id, status)
    if release_id is not None:
        store.annotate_deployment(deployment_id, release_id=release_id)
    return deployment_id


def inplace_app(store: WASMStore, tmp_path: Path) -> App:
    """An in-place application with its directory."""
    app_path = tmp_path / "apps" / "shop-example-com"
    app_path.mkdir(parents=True)
    return store.create_app(
        App(
            domain=INPLACE,
            app_type="nodejs",
            source="https://github.com/example/shop.git",
            branch="main",
            port=3000,
            app_path=str(app_path),
        )
    )


@pytest.fixture
def rollback_manager(
    tmp_path: Path, store: WASMStore, monkeypatch: pytest.MonkeyPatch
) -> RollbackManager:
    """A RollbackManager whose backups are faked and whose store is the test's."""
    inplace_app(store, tmp_path)
    monkeypatch.setattr(backup_module, "get_store", lambda: store)
    manager = RollbackManager()
    manager.config = SimpleNamespace(apps_directory=tmp_path / "apps")  # type: ignore[assignment]
    made: list[Any] = [backup(commit="abc1234def56")]
    manager.backup_manager.create = lambda **kwargs: made[0]  # type: ignore[method-assign]
    manager.made = made  # type: ignore[attr-defined]
    return manager


# ---------------------------------------------------------------------------
# The snapshot link
# ---------------------------------------------------------------------------


def test_a_pre_update_backup_is_the_snapshot_of_the_previous_deployment(
    store: WASMStore, rollback_manager: RollbackManager
) -> None:
    """The tree backed up is what the last successful deployment produced."""
    older = deployment(store, "success")
    previous = deployment(store, "success")

    taken = rollback_manager.create_pre_deploy_backup(INPLACE)

    assert taken is not None
    assert store.get_deployment(previous).snapshot_backup == taken.id
    assert store.get_deployment(older).snapshot_backup is None


def test_the_operation_taking_the_backup_is_skipped_while_it_runs(
    store: WASMStore, rollback_manager: RollbackManager
) -> None:
    """A rollback records before its safety backup; the row it replaces gets it."""
    previous = deployment(store, "success")
    running = deployment(store, "running")

    rollback_manager.create_pre_deploy_backup(INPLACE)

    assert store.get_deployment(previous).snapshot_backup == rollback_manager.made[0].id
    assert store.get_deployment(running).snapshot_backup is None


def test_after_a_failed_deployment_the_tree_is_nobodys_snapshot(
    store: WASMStore, rollback_manager: RollbackManager
) -> None:
    """A failed build left the tree half-changed; linking it would lie."""
    good = deployment(store, "success")
    deployment(store, "failed")

    rollback_manager.create_pre_deploy_backup(INPLACE)

    assert store.get_deployment(good).snapshot_backup is None


def test_a_tree_on_another_commit_is_not_the_deployments_snapshot(
    store: WASMStore, rollback_manager: RollbackManager
) -> None:
    """Changed by hand since: the commits disagree, so no link."""
    previous = deployment(store, "success", commit="abc1234")
    rollback_manager.made[0] = backup(commit="fff9999aaaa")

    rollback_manager.create_pre_deploy_backup(INPLACE)

    assert store.get_deployment(previous).snapshot_backup is None


def test_a_matching_commit_of_another_length_is_linked(
    store: WASMStore, rollback_manager: RollbackManager
) -> None:
    """The backup records twelve characters and the history seven."""
    previous = deployment(store, "success", commit="abc1234")
    rollback_manager.made[0] = backup(commit="abc1234def56")

    rollback_manager.create_pre_deploy_backup(INPLACE)

    assert store.get_deployment(previous).snapshot_backup == "shop-example-com-20260926-120000"


def test_a_release_application_gets_no_snapshot(
    tmp_path: Path, store: WASMStore, rollback_manager: RollbackManager
) -> None:
    """On releases the previous build stays on disk; that is its way back."""
    previous = deployment(store, "success")
    app_path = tmp_path / "apps" / "shop-example-com"
    (app_path / "releases" / "r1").mkdir(parents=True)
    (app_path / "current").symlink_to(Path("releases") / "r1")

    rollback_manager.create_pre_deploy_backup(INPLACE)

    assert store.get_deployment(previous).snapshot_backup is None


# ---------------------------------------------------------------------------
# Availability and going back
# ---------------------------------------------------------------------------


@pytest.fixture
def backups(monkeypatch: pytest.MonkeyPatch) -> set[str]:
    """Backup ids that exist, as the lifecycle's BackupManager sees them."""
    existing: set[str] = set()
    monkeypatch.setattr(
        lifecycle,
        "BackupManager",
        lambda: SimpleNamespace(get_backup=lambda backup_id: backup_id in existing or None),
    )
    return existing


def test_in_place_without_history_a_snapshot_is_restored_through_the_rollback_manager(
    tmp_path: Path, store: WASMStore, backups: set[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Keeping .git and the .env, rebuilt as the recorded type, behind the gate."""
    inplace_app(store, tmp_path)
    target = deployment(store, "success")
    store.set_deployment_snapshot(target, "snap-1")
    backups.add("snap-1")
    restored: list[dict[str, Any]] = []

    class Manager:
        logger = None
        last_deployment_id = 42

        def __init__(self, verbose: bool = False) -> None:
            pass

        def rollback(self, **kw: Any) -> bool:
            restored.append(kw)
            return True

    monkeypatch.setattr(lifecycle, "RollbackManager", Manager)

    assert lifecycle.rollback_availability([store.get_deployment(target)]) == {target: None}
    outcome = lifecycle.rollback_to_deployment(INPLACE, target, trigger="panel")

    (call,) = restored
    assert {k: call[k] for k in ("domain", "backup_id", "trigger", "restore_env", "keep")} == {
        "domain": INPLACE,
        "backup_id": "snap-1",
        "trigger": "panel",
        "restore_env": False,
        "keep": (".git",),
    }
    assert call["app_type"] == "nodejs"
    assert callable(call["gate"])
    assert (outcome.backup_id, outcome.release_id, outcome.history_id) == ("snap-1", None, 42)


def test_in_place_without_a_snapshot_the_refusal_says_to_rebuild(
    tmp_path: Path, store: WASMStore, backups: set[str]
) -> None:
    """The live deployment has no snapshot until the next update takes one."""
    inplace_app(store, tmp_path)
    target = deployment(store, "success")

    with pytest.raises(DeploymentError, match="No backup holds") as refusal:
        lifecycle.rollback_to_deployment(INPLACE, target)
    assert "--commit abc1234" in refusal.value.details


def test_a_snapshot_rotated_away_is_not_offered(
    tmp_path: Path, store: WASMStore, backups: set[str]
) -> None:
    """The link outlives the backup; availability checks the backup."""
    inplace_app(store, tmp_path)
    target = deployment(store, "success")
    store.set_deployment_snapshot(target, "gone")

    reason = lifecycle.rollback_availability([store.get_deployment(target)])[target]
    assert reason is not None and "no longer exists" in reason


def test_a_failed_deployment_is_not_something_to_go_back_to(
    tmp_path: Path, store: WASMStore, backups: set[str]
) -> None:
    """Only what served can be put back."""
    inplace_app(store, tmp_path)
    target = deployment(store, "failed")
    store.set_deployment_snapshot(target, "snap-1")
    backups.add("snap-1")

    assert lifecycle.rollback_availability([store.get_deployment(target)])[target] is not None


def test_a_deployment_of_another_application_is_not_found(tmp_path: Path, store: WASMStore) -> None:
    """The id alone is not enough: it must be this application's."""
    inplace_app(store, tmp_path)
    other = deployment(store, "success", domain="other.example.com")

    with pytest.raises(WASMError, match="not found"):
        lifecycle.rollback_to_deployment(INPLACE, other)


def test_on_releases_going_back_to_a_deployment_activates_its_release(
    root: Path, store: WASMStore, machine: SimpleNamespace, two_releases: tuple[str, str]
) -> None:
    """The deployment that built the first release, activated behind the gate."""
    first, second = two_releases
    rows = {row.release_id: row for row in store.list_deployments(DOMAIN)}
    target = rows[first]
    assert target.id is not None

    availability = lifecycle.rollback_availability(list(rows.values()))
    assert availability[target.id] is None
    assert availability[rows[second].id] == f"Release {second} is already live"

    outcome = lifecycle.rollback_to_deployment(DOMAIN, target.id, trigger="panel")

    assert active_id(root) == first
    assert outcome.release_id == first
    assert store.get_deployment(outcome.history_id).triggered_by == "panel"


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------


class Jobs:
    """Stands in for the job manager, keeping what was queued."""

    def __init__(self) -> None:
        self.created: list[dict[str, Any]] = []

    def create_job(self, **kwargs: Any) -> Job:
        self.created.append(kwargs)
        return Job(
            id="ab12cd34",
            type=kwargs["job_type"],
            name=kwargs["name"],
            description=kwargs["description"],
            status=JobStatus.PENDING,
            metadata=kwargs.get("metadata") or {},
        )


@pytest.fixture
def jobs(monkeypatch: pytest.MonkeyPatch) -> Jobs:
    """The job manager every endpoint queues through."""
    fake = Jobs()
    monkeypatch.setattr(deployments_api, "get_job_manager", lambda: fake)
    monkeypatch.setattr(jobs_api, "get_job_manager", lambda: fake)
    return fake


@pytest.fixture
def client(store: WASMStore, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """The deployments, deployment actions and jobs routers, authenticated."""
    monkeypatch.setattr(deployments_api, "get_store", lambda: store)
    app = FastAPI()
    install_error_handlers(app)
    app.include_router(deployments_api.router, prefix="/api/deployments")
    app.include_router(deployments_api.app_router, prefix="/api/apps")
    app.include_router(jobs_api.router, prefix="/api")
    app.dependency_overrides[get_current_session] = lambda: {"session_id": "test"}
    return TestClient(app, raise_server_exceptions=False)


def upstream(has_new: bool) -> lifecycle.UpstreamState:
    """An upstream comparison that has, or has not, something new."""
    return lifecycle.UpstreamState(
        domain=INPLACE,
        branch="main",
        live_commit="abc1234",
        remote_commit=("fff0000" if has_new else "abc1234") + "0" * 33,
    )


def test_an_update_with_nothing_new_is_refused_with_why_and_when_it_is_worth_it(
    client: TestClient, jobs: Jobs, monkeypatch: pytest.MonkeyPatch
) -> None:
    """409 nothing_new; no job."""
    monkeypatch.setattr(jobs_api, "check_upstream", lambda domain: upstream(False))

    response = client.post("/api/jobs/update", json={"domain": INPLACE})

    assert response.status_code == 409
    body = response.json()
    assert body["error"] == "nothing_new"
    assert body["detail"] == "No new commits on main since abc1234, which is live"
    assert "environment or the dependencies changed" in body["hint"]
    assert "last build broke" in body["hint"]
    assert jobs.created == []


def test_a_forced_update_is_not_compared(
    client: TestClient, jobs: Jobs, monkeypatch: pytest.MonkeyPatch
) -> None:
    """force: true rebuilds the same commit, without asking the remote."""

    def never(domain: str) -> None:
        raise AssertionError("a forced update does not ask")

    monkeypatch.setattr(jobs_api, "check_upstream", never)

    response = client.post("/api/jobs/update", json={"domain": INPLACE, "force": True})

    assert response.status_code == 202
    assert jobs.created[0]["kwargs"] == {"domain": INPLACE}


@pytest.mark.parametrize("answer", [None, "new"])
def test_an_update_with_news_or_no_answer_is_queued(
    client: TestClient, jobs: Jobs, monkeypatch: pytest.MonkeyPatch, answer: str | None
) -> None:
    """Something new, or a source that is not git: the update goes ahead."""
    monkeypatch.setattr(
        jobs_api, "check_upstream", lambda domain: upstream(True) if answer else None
    )

    assert client.post("/api/jobs/update", json={"domain": INPLACE}).status_code == 202
    assert len(jobs.created) == 1


def test_rebuilding_a_deployment_queues_the_update_with_its_commit(
    tmp_path: Path, client: TestClient, jobs: Jobs, store: WASMStore
) -> None:
    """The update job, given the deployment's own commit; no nothing-new check."""
    inplace_app(store, tmp_path)
    target = deployment(store, "success", commit="abc1234")

    response = client.post(f"/api/apps/{INPLACE}/deployments/{target}/rebuild")

    assert response.status_code == 202, response.text
    queued = jobs.created[0]
    assert queued["func"] is deployments_api.update_app_job
    assert queued["kwargs"] == {"domain": INPLACE, "commit": "abc1234"}
    assert queued["metadata"]["deployment_id"] == target


def test_a_deployment_without_a_commit_cannot_be_rebuilt_exactly(
    tmp_path: Path, client: TestClient, jobs: Jobs, store: WASMStore
) -> None:
    """A local source recorded no commit: 409 with the way forward."""
    inplace_app(store, tmp_path)
    target = deployment(store, "success", commit=None)

    response = client.post(f"/api/apps/{INPLACE}/deployments/{target}/rebuild")

    assert response.status_code == 409
    assert response.json()["error"] == "no_commit"
    assert jobs.created == []


def test_a_deployment_of_another_domain_is_404_on_either_action(
    tmp_path: Path, client: TestClient, jobs: Jobs, store: WASMStore
) -> None:
    """The domain in the path is checked against the row."""
    inplace_app(store, tmp_path)
    other = deployment(store, "success", domain="other.example.com")

    for action in ("rebuild", "rollback"):
        response = client.post(f"/api/apps/{INPLACE}/deployments/{other}/{action}")
        assert response.status_code == 404
    assert jobs.created == []


def test_rolling_back_to_a_deployment_is_queued_when_available(
    tmp_path: Path, client: TestClient, jobs: Jobs, store: WASMStore, backups: set[str]
) -> None:
    """A snapshot that exists: the rollback job, for that deployment."""
    inplace_app(store, tmp_path)
    target = deployment(store, "success")
    store.set_deployment_snapshot(target, "snap-1")
    backups.add("snap-1")

    response = client.post(f"/api/apps/{INPLACE}/deployments/{target}/rollback")

    assert response.status_code == 202, response.text
    assert jobs.created[0]["func"] is deployments_api.rollback_deployment_job
    assert jobs.created[0]["kwargs"] == {"domain": INPLACE, "deployment_id": target}


def test_rolling_back_to_a_deployment_without_a_snapshot_is_409_with_why(
    tmp_path: Path, client: TestClient, jobs: Jobs, store: WASMStore, backups: set[str]
) -> None:
    """rollback_unavailable carries the reason and the alternative."""
    inplace_app(store, tmp_path)
    target = deployment(store, "success")

    response = client.post(f"/api/apps/{INPLACE}/deployments/{target}/rollback")

    assert response.status_code == 409
    body = response.json()
    assert body["error"] == "rollback_unavailable"
    assert "No backup holds" in body["detail"]
    assert body["hint"] == "Rebuild its commit instead"
    assert jobs.created == []


def test_the_history_says_which_deployments_can_be_gone_back_to(
    tmp_path: Path, client: TestClient, store: WASMStore, backups: set[str]
) -> None:
    """snapshot_backup, rollback_available and the reason when not."""
    inplace_app(store, tmp_path)
    older = deployment(store, "success")
    store.set_deployment_snapshot(older, "snap-1")
    backups.add("snap-1")
    live = deployment(store, "success")

    items = {
        item["id"]: item
        for item in client.get("/api/deployments", params={"domain": INPLACE}).json()["items"]
    }

    assert items[older]["snapshot_backup"] == "snap-1"
    assert items[older]["rollback_available"] is True
    assert items[older]["rollback_unavailable_reason"] is None
    assert items[live]["snapshot_backup"] is None
    assert items[live]["rollback_available"] is False
    assert "No backup holds" in items[live]["rollback_unavailable_reason"]

    one = client.get(f"/api/deployments/{older}").json()
    assert one["rollback_available"] is True


@pytest.mark.parametrize("action", ["rebuild", "rollback"])
def test_both_actions_need_the_deploy_scope_like_an_update(action: str) -> None:
    """Stated once, in the scope policy, for every surface."""
    path = f"/api/apps/{INPLACE}/deployments/12/{action}"
    assert web_auth.required_scope("POST", path) == "deploy"
    assert web_auth.required_scope("POST", path + "/x") == "admin"
    assert web_auth.required_scope("POST", f"/api/apps/{INPLACE}/deployments/x/{action}") == (
        "admin"
    )


def test_on_releases_a_release_that_failed_its_gate_is_not_offered(
    store: WASMStore, machine: SimpleNamespace, two_releases: tuple[str, str]
) -> None:
    """Still on disk after a failed activation, and still not something to go back to."""
    first, _ = two_releases
    app = store.get_app(DOMAIN)
    assert app is not None and app.id is not None
    store.set_release_status(app.id, first, "failed")
    target = {row.release_id: row for row in store.list_deployments(DOMAIN)}[first]
    assert target.id is not None

    reason = lifecycle.rollback_availability([target])[target.id]

    assert reason is not None and "did not pass its health check" in reason
