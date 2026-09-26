# Copyright (c) 2024-2026 Yago Lopez Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
Tests for the application endpoints the deploy engine v2 adds or changes.

- The environment editor reads and writes ``shared/.env`` on the release
  layout, not a stray ``<app>/.env`` the application never sees.
- Releases are listed and one is activated through the same lifecycle
  function the CLI calls, behind the same health gate as a deploy.
- A migration is planned and run through :mod:`wasm.deployers.migrate`.
- Resource limits are validated here and applied by the service manager.

Each endpoint is a thin translation of HTTP to a manager call, so these tests
check the translation (status codes, models, what reached the manager) and
leave the behaviour to the tests of the functions they call.
"""

from __future__ import annotations

import os
import stat
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from wasm.core.runner import FakeRunner
from wasm.core.store import App, WASMStore
from wasm.deployers.helpers import app_env as app_env_module
from wasm.web.api import apps as apps_api
from wasm.web.api.auth import get_current_session
from wasm.web.api.deps import require_elevated

DOMAIN = "rel.example.com"
RELEASE_A = "20260925-120000-aaaaaaa"
RELEASE_B = "20260925-130000-bbbbbbb"


@pytest.fixture
def store(tmp_path: Path) -> Iterator[WASMStore]:
    """
    Give the API a store of its own.

    Args:
        tmp_path: Per-test temporary directory.

    Yields:
        The store the endpoints read.
    """
    WASMStore.reset_instance()
    instance = WASMStore(tmp_path / "wasm.db")
    try:
        yield instance
    finally:
        instance.close()
        WASMStore.reset_instance()


@pytest.fixture
def client(store: WASMStore, runner: FakeRunner, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """
    Build a client for the applications router, already signed in and elevated.

    Args:
        store: The sandboxed store.
        runner: Fake runner, so no manager reaches a real process.
        monkeypatch: Patching helper.

    Returns:
        The client.
    """
    monkeypatch.setattr(
        app_env_module,
        "Config",
        lambda: SimpleNamespace(service_user="www-data", service_group="www-data"),
    )
    app = FastAPI()
    app.include_router(apps_api.router, prefix="/api/apps")
    session = {"sid": "test", "scope": "admin"}
    app.dependency_overrides[get_current_session] = lambda: session
    app.dependency_overrides[require_elevated] = lambda: session
    return TestClient(app, raise_server_exceptions=False)


def release_app(
    store: WASMStore, root: Path, *, releases: tuple[str, ...] = (RELEASE_A,), **fields: Any
) -> App:
    """
    Put an application on the release layout, on disk and in the store.

    Args:
        store: Where the row goes.
        root: The application directory.
        releases: Release ids to create, oldest first; the last one is active.
        **fields: Extra App fields.

    Returns:
        The stored row.
    """
    for release_id in releases:
        (root / "releases" / release_id).mkdir(parents=True)
    (root / "current").symlink_to(Path("releases") / releases[-1])
    (root / "shared").mkdir(parents=True)
    return store.create_app(
        App(
            domain=DOMAIN,
            app_type="nodejs",
            port=3100,
            app_path=str(root),
            layout="releases",
            **fields,
        )
    )


# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------


def test_get_env_reads_shared_on_the_release_layout(
    client: TestClient, store: WASMStore, tmp_path: Path
) -> None:
    """<app>/.env is not the release layout's .env; reading it returned nothing."""
    root = tmp_path / "rel"
    release_app(store, root)
    (root / "shared" / ".env").write_text("NODE_ENV=production\n", encoding="utf-8")

    response = client.get(f"/api/apps/{DOMAIN}/env")

    assert response.status_code == 200, response.text
    assert response.json()["variables"] == {"NODE_ENV": "production"}


def test_put_env_writes_shared_hands_it_over_and_links_it(
    client: TestClient, store: WASMStore, tmp_path: Path, runner: FakeRunner
) -> None:
    """The write lands where every release reads it, owned by the service account."""
    root = tmp_path / "rel"
    release_app(store, root)

    response = client.put(f"/api/apps/{DOMAIN}/env", json={"variables": {"API_KEY": "new"}})

    assert response.status_code == 200, response.text
    shared_env = root / "shared" / ".env"
    assert shared_env.read_text(encoding="utf-8") == "API_KEY=new\n"
    assert stat.S_IMODE(shared_env.stat().st_mode) == 0o600
    assert not os.path.lexists(root / ".env"), "a stray .env the application never reads"
    assert os.readlink(root / "releases" / RELEASE_A / ".env") == "../../shared/.env"
    assert ("chown", "www-data:www-data", str(shared_env)) in runner.calls


def test_put_env_in_place_still_writes_the_app_directory(
    client: TestClient, store: WASMStore, tmp_path: Path
) -> None:
    """The in-place layout keeps its .env where it always was."""
    root = tmp_path / "inplace"
    root.mkdir()
    store.create_app(App(domain=DOMAIN, app_type="nodejs", app_path=str(root)))

    response = client.put(f"/api/apps/{DOMAIN}/env", json={"variables": {"A": "1"}})

    assert response.status_code == 200, response.text
    assert (root / ".env").read_text(encoding="utf-8") == "A=1\n"
    assert not (root / "shared").exists()


# ---------------------------------------------------------------------------
# Releases
# ---------------------------------------------------------------------------


def test_releases_are_listed_newest_first(
    client: TestClient, store: WASMStore, tmp_path: Path
) -> None:
    """The listing is lifecycle.list_releases, translated."""
    release_app(store, tmp_path / "rel", releases=(RELEASE_A, RELEASE_B))

    response = client.get(f"/api/apps/{DOMAIN}/releases")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] == 2
    assert [(r["id"], r["active"], r["commit"]) for r in body["items"]] == [
        (RELEASE_B, True, "bbbbbbb"),
        (RELEASE_A, False, "aaaaaaa"),
    ]


def test_an_in_place_app_has_no_releases_to_list(
    client: TestClient, store: WASMStore, tmp_path: Path
) -> None:
    """409 with the way forward, not an empty list that reads like a bug."""
    store.create_app(App(domain=DOMAIN, app_type="nodejs", app_path=str(tmp_path)))

    response = client.get(f"/api/apps/{DOMAIN}/releases")

    assert response.status_code == 409
    assert "migrate" in response.text


def test_activating_a_release_goes_through_the_lifecycle_as_the_panel(
    client: TestClient, store: WASMStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The endpoint is a translation: the health gate and the history are the lifecycle's."""
    root = tmp_path / "rel"
    release_app(store, root, releases=(RELEASE_A, RELEASE_B))
    calls: list[tuple[str, str | None, str]] = []

    def activate(domain: str, release_id: str | None = None, *, trigger: str) -> Any:
        calls.append((domain, release_id, trigger))
        from wasm.deployers.lifecycle import ReleaseActivation
        from wasm.deployers.releases import ReleaseManager

        listed = {r.id: r for r in ReleaseManager(root).list()}
        return ReleaseActivation(
            domain=domain,
            release=listed[RELEASE_A],
            previous=listed[RELEASE_B],
            changed=True,
            went_back=True,
            deployment_id=7,
        )

    monkeypatch.setattr(apps_api, "activate_release", activate)

    response = client.post(f"/api/apps/{DOMAIN}/releases/{RELEASE_A}/activate")

    assert response.status_code == 200, response.text
    assert calls == [(DOMAIN, RELEASE_A, "panel")]
    assert response.json() == {
        "domain": DOMAIN,
        "release_id": RELEASE_A,
        "previous_id": RELEASE_B,
        "changed": True,
        "rolled_back": True,
        "deployment_id": 7,
    }


@pytest.mark.parametrize(
    ("release_id", "status"),
    [("a..b", 400), ("not-a-release", 400), ("20260101-000000-ccccccc", 404)],
)
def test_activating_something_that_is_not_a_release_on_disk_is_refused(
    client: TestClient,
    store: WASMStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    release_id: str,
    status: int,
) -> None:
    """Nothing reaches the lifecycle for a name that is not a release on disk."""
    release_app(store, tmp_path / "rel")
    monkeypatch.setattr(
        apps_api, "activate_release", lambda *a, **k: pytest.fail("reached the lifecycle")
    )

    response = client.post(f"/api/apps/{DOMAIN}/releases/{release_id}/activate")

    assert response.status_code == status, response.text


def test_a_deploy_scope_is_enough_to_activate_a_release_and_nothing_near_it() -> None:
    """The pattern names exactly the activation, anchored at both ends."""
    from wasm.web.auth import required_scope

    path = f"/api/apps/{DOMAIN}/releases/{RELEASE_A}/activate"
    assert required_scope("POST", path) == "deploy"
    assert required_scope("GET", f"/api/apps/{DOMAIN}/releases") == "read"
    assert required_scope("DELETE", path) == "admin"
    assert required_scope("POST", f"{path}/again") == "admin"
    assert required_scope("POST", f"/api/apps/{DOMAIN}/x/releases/{RELEASE_A}/activate") == "admin"
    assert required_scope("POST", f"/api/apps/{DOMAIN}/migrate") == "admin"
    assert required_scope("PATCH", f"/api/apps/{DOMAIN}/limits") == "admin"


# ---------------------------------------------------------------------------
# Migration
# ---------------------------------------------------------------------------


def _plan(**overrides: Any) -> Any:
    """A migration plan as plan_migration returns one."""
    from wasm.deployers.migrate import MigrationPlan, TreeCount

    fields: dict[str, Any] = {
        "domain": DOMAIN,
        "app_path": "/var/www/apps/rel-example-com",
        "release_id": RELEASE_A,
        "commit": "a" * 40,
        "persistent": ("uploads",),
        "persistent_source": "git",
        "env_files": (".env",),
        "unit": "rel-example-com",
        "unit_rewrite": True,
        "site_rewrite": False,
        "untracked_files": ("notes.txt",),
        "warnings": ("1 untracked file(s) stay in the first release only",),
        "count": TreeCount(files=12, bytes=3400, links=1),
    }
    fields.update(overrides)
    return MigrationPlan(**fields)


def test_the_migration_plan_is_plan_migration_translated(
    client: TestClient, store: WASMStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Repeated persist parameters reach the planner as a list."""
    store.create_app(App(domain=DOMAIN, app_type="nodejs", app_path=str(tmp_path)))
    asked: list[Any] = []
    monkeypatch.setattr(
        apps_api, "plan_migration", lambda domain, persist: asked.append(persist) or _plan()
    )

    response = client.get(
        f"/api/apps/{DOMAIN}/migrate/plan", params=[("persist", "uploads"), ("persist", "data")]
    )

    assert response.status_code == 200, response.text
    assert asked == [["uploads", "data"]]
    body = response.json()
    assert (body["persistent"], body["files"], body["unit_rewrite"]) == (["uploads"], 12, True)


def test_migrating_is_queued_as_a_job(
    client: TestClient, store: WASMStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    A migration moves the whole tree and waits for a health check: a job, not a request.

    Checked here, before queueing: that the application can be migrated and
    that the named paths are valid, so a bad request fails at once.
    """
    from wasm.web.jobs import JobType, migrate_app_job

    store.create_app(App(domain=DOMAIN, app_type="nodejs", app_path=str(tmp_path)))
    planned: list[Any] = []
    monkeypatch.setattr(
        apps_api, "plan_migration", lambda domain, persist: planned.append(persist) or _plan()
    )
    queued: list[dict[str, Any]] = []

    class Queued:
        id = "job-7"
        status = SimpleNamespace(value="pending")

        def to_dict(self) -> dict[str, Any]:
            return {"id": self.id, "type": "migrate"}

    def create_job(**kwargs: Any) -> Queued:
        queued.append(kwargs)
        return Queued()

    monkeypatch.setattr(apps_api, "get_job_manager", lambda: SimpleNamespace(create_job=create_job))

    response = client.post(f"/api/apps/{DOMAIN}/migrate", json={"persist": ["uploads"]})

    assert response.status_code == 202, response.text
    assert response.json()["job_id"] == "job-7"
    assert planned == [["uploads"]], "validated before it is queued"
    (job,) = queued
    assert job["job_type"] is JobType.MIGRATE
    assert job["func"] is migrate_app_job
    assert job["kwargs"] == {"domain": DOMAIN, "persist": ["uploads"]}
    assert job["metadata"] == {"domain": DOMAIN}


def test_the_migration_job_plans_again_and_migrates_as_the_panel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """What runs is planned from the disk when the job runs, never earlier."""
    from wasm.deployers import migrate as migrate_module
    from wasm.deployers.migrate import MigrationResult, TreeCount
    from wasm.web.jobs import Job, JobContext, migrate_app_job

    plan = _plan()
    calls: list[Any] = []
    monkeypatch.setattr(
        migrate_module, "plan_migration", lambda domain, persist: calls.append(persist) or plan
    )

    def run(domain: str, given: Any, *, trigger: str, logger: Any = None) -> Any:
        calls.append((domain, given, trigger))
        count = TreeCount(files=12, bytes=3400, links=1)
        return MigrationResult(
            domain=domain,
            release_id=RELEASE_A,
            persistent=("uploads",),
            env_files=(".env",),
            before=count,
            after=TreeCount(files=12, bytes=3400, links=4),
            unit_rewritten=True,
            site_rewritten=False,
            rehearsed=False,
            deployment_id=9,
        )

    monkeypatch.setattr(migrate_module, "migrate", run)
    job = Job(id="job-7", type=apps_api.JobType.MIGRATE, name="migrate", description="")

    result = migrate_app_job(DOMAIN, ["uploads"], job_context=JobContext(job, lambda _j: None))

    assert calls == [["uploads"], (DOMAIN, plan, "panel")]
    assert (result["files_before"], result["files_after"], result["release_id"]) == (
        12,
        12,
        RELEASE_A,
    )
    assert result["deployment_id"] == 9
    assert job.metadata["domain"] == DOMAIN
    assert any("1 untracked file(s)" in entry.message for entry in job.logs)


def test_migrating_needs_sudo_mode_and_an_in_place_app(
    client: TestClient, store: WASMStore, tmp_path: Path
) -> None:
    """The route declares elevation; an app on releases already is a conflict."""
    from fastapi.routing import APIRoute

    route = next(
        r
        for r in apps_api.router.routes
        if isinstance(r, APIRoute) and r.path == "/{domain}/migrate" and "POST" in r.methods
    )
    assert any(dep.call is require_elevated for dep in route.dependant.dependencies)

    release_app(store, tmp_path / "rel")
    assert client.post(f"/api/apps/{DOMAIN}/migrate", json={}).status_code == 409
    assert client.get(f"/api/apps/{DOMAIN}/migrate/plan").status_code == 409


# ---------------------------------------------------------------------------
# Resource limits
# ---------------------------------------------------------------------------


def test_limits_are_set_as_a_whole_through_the_lifecycle(
    client: TestClient, store: WASMStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A field left out removes that limit; restart is only what was asked."""
    from wasm.deployers.lifecycle import LimitsChange
    from wasm.managers.service_manager import ResourceLimits

    store.create_app(App(domain=DOMAIN, app_type="nodejs", app_path=str(tmp_path)))
    calls: list[Any] = []

    def apply(domain: str, limits: ResourceLimits, *, restart: bool) -> LimitsChange:
        calls.append((domain, limits, restart))
        return LimitsChange(
            domain=domain, limits=limits, units=("rel-example-com",), restarted=restart
        )

    monkeypatch.setattr(apps_api, "set_resource_limits", apply)

    response = client.patch(
        f"/api/apps/{DOMAIN}/limits", json={"memory_max_mb": 512, "cpu_quota_percent": 50}
    )

    assert response.status_code == 200, response.text
    assert calls == [(DOMAIN, ResourceLimits(memory_max_mb=512, cpu_quota_percent=50), False)]
    assert response.json() == {
        "domain": DOMAIN,
        "memory_max_mb": 512,
        "cpu_quota_percent": 50,
        "tasks_max": None,
        "units": ["rel-example-com"],
        "restarted": False,
        "restart_required": True,
    }


def test_a_limit_out_of_range_is_a_400_with_the_range(
    client: TestClient, store: WASMStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The service manager's refusal, through the error contract, nothing written."""
    from wasm.deployers import lifecycle

    store.create_app(App(domain=DOMAIN, app_type="nodejs", app_path=str(tmp_path)))
    monkeypatch.setattr(lifecycle, "ServiceManager", lambda **kw: pytest.fail("a unit was touched"))

    response = client.patch(f"/api/apps/{DOMAIN}/limits", json={"memory_max_mb": 32})

    assert response.status_code == 400, response.text
    assert "64M" in response.text


def test_setting_limits_needs_sudo_mode() -> None:
    """It rewrites a unit, like editing one by hand does."""
    from fastapi.routing import APIRoute

    route = next(
        r
        for r in apps_api.router.routes
        if isinstance(r, APIRoute) and r.path == "/{domain}/limits"
    )
    assert "PATCH" in route.methods
    assert any(dep.call is require_elevated for dep in route.dependant.dependencies)


def test_the_application_shows_its_layout_and_limits(
    client: TestClient, store: WASMStore, tmp_path: Path
) -> None:
    """What the console needs to show the release tab and the limit fields."""
    release_app(store, tmp_path / "rel", memory_max_mb=256, tasks_max=64)

    body = client.get(f"/api/apps/{DOMAIN}").json()

    assert (
        body["layout"],
        body["memory_max_mb"],
        body["cpu_quota_percent"],
        body["tasks_max"],
    ) == (
        "releases",
        256,
        None,
        64,
    )
