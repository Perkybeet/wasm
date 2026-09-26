"""
Tests for the one implementation of "update a deployed application".

The CLI, the panel's update job and the git webhook used to update in three
different ways. The panel and the webhook re-ran the whole deploy pipeline,
whose fetch step deletes the application directory and clones it again: the
``.env`` edited in the panel, every uploaded file and the generated secrets
went with it on every push. These tests pin the shared behaviour down.
"""

from __future__ import annotations

import stat
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from wasm.core.exceptions import WASMError
from wasm.core.fs import SECRET_MODE, RealFileSystem, set_fs
from wasm.core.store import App, Service, WASMStore
from wasm.deployers import lifecycle
from wasm.deployers.interface import UpdateResult

DOMAIN = "example.com"


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> WASMStore:
    """A store in the test's directory, installed where the module looks for it."""
    WASMStore.reset_instance()
    instance = WASMStore(tmp_path / "wasm.db")
    monkeypatch.setattr(lifecycle, "get_store", lambda: instance)
    yield instance
    WASMStore.reset_instance()


@pytest.fixture
def real_fs() -> Any:
    """The real filesystem, restored to the default afterwards."""
    set_fs(RealFileSystem())
    yield
    set_fs(None)


class Recorder:
    """Everything the update asked of its collaborators, in order."""

    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self.configured: dict[str, Any] = {}
        self.service_exists = True
        self.service_active = True


class FakeDeployer:
    """A deployer that records instead of building."""

    def __init__(self, recorder: Recorder, *, is_static: bool = False) -> None:
        self.recorder = recorder
        self.is_static = is_static

    def configure(self, **kwargs: Any) -> None:
        self.recorder.configured = kwargs

    def update(self, on_step: Any = None) -> UpdateResult:
        self.recorder.calls.append(("update",))
        if on_step:
            on_step("Building")
        return UpdateResult(
            package_manager="npm",
            prisma_updated=False,
            is_static=self.is_static,
            start_command="" if self.is_static else "/usr/bin/npm run start",
        )

    def deploy(self) -> bool:
        self.recorder.calls.append(("deploy",))
        return True


@pytest.fixture
def recorder(monkeypatch: pytest.MonkeyPatch) -> Recorder:
    """Replace every collaborator of the update with a recording fake."""
    rec = Recorder()

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
            fetch=lambda source, path, branch=None, force=False, clean=True: rec.calls.append(
                ("fetch", source, path, force, clean)
            ),
        ),
    )

    def restart(name: str) -> bool:
        rec.calls.append(("restart", name))
        return True

    monkeypatch.setattr(
        lifecycle,
        "ServiceManager",
        lambda verbose=False: SimpleNamespace(
            get_status=lambda name: {"exists": rec.service_exists, "active": rec.service_active},
            restart=restart,
            get_service_config=lambda name: None,
        ),
    )
    monkeypatch.setattr(lifecycle.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(
        lifecycle, "get_deployer", lambda app_type, verbose=False: FakeDeployer(rec)
    )
    return rec


def make_app(store: WASMStore, app_path: Path, app_type: str = "nextjs") -> App:
    """Register an application whose tree lives at ``app_path``."""
    (app_path / ".git").mkdir(parents=True, exist_ok=True)
    return store.create_app(
        App(
            domain=DOMAIN,
            app_type=app_type,
            source="https://github.com/example/app.git",
            branch="main",
            port=3000,
            app_path=str(app_path),
        )
    )


def test_update_never_deletes_what_is_not_in_the_repository(
    store: WASMStore, recorder: Recorder, tmp_path: Path
) -> None:
    """The panel and the webhook wiped the tree; uploads and .env must survive."""
    app_path = tmp_path / "apps" / "example-com"
    make_app(store, app_path)
    (app_path / ".env").write_text("SECRET=kept\n")
    upload = app_path / "public" / "uploads" / "photo.png"
    upload.parent.mkdir(parents=True)
    upload.write_bytes(b"\x89PNG")

    lifecycle.update_app(DOMAIN, trigger="webhook")

    assert (app_path / ".env").read_text() == "SECRET=kept\n"
    assert upload.exists()
    assert ("deploy",) not in recorder.calls
    assert ("update",) in recorder.calls
    assert ("pull", app_path, None) in recorder.calls


def test_update_backs_up_then_pulls_then_rebuilds_then_restarts(
    store: WASMStore, recorder: Recorder, tmp_path: Path
) -> None:
    """The order is what makes a broken build leave the old one serving."""
    make_app(store, tmp_path / "apps" / "example-com")

    outcome = lifecycle.update_app(DOMAIN, branch="release")

    kinds = [call[0] for call in recorder.calls]
    assert kinds == ["backup", "pull", "update", "restart"]
    assert recorder.calls[1][2] == "release"
    assert outcome.restarted == ("example-com",)
    assert outcome.active is True


def test_the_trigger_reaches_the_deployment_history(
    store: WASMStore, recorder: Recorder, tmp_path: Path
) -> None:
    """History must say whether the operator, the panel or a push did it."""
    make_app(store, tmp_path / "apps" / "example-com")

    lifecycle.update_app(DOMAIN, trigger="panel", package_manager="pnpm")

    assert recorder.configured["trigger"] == "panel"
    assert recorder.configured["package_manager"] == "pnpm"
    assert recorder.configured["app_path"] == tmp_path / "apps" / "example-com"


def test_a_new_source_keeps_the_env_file_private(
    store: WASMStore,
    recorder: Recorder,
    tmp_path: Path,
    real_fs: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A forced fetch replaces the tree; the secrets come back owner-only."""
    app_path = tmp_path / "apps" / "example-com"
    make_app(store, app_path)
    (app_path / ".env").write_text("SECRET=kept\n")

    def wiping_fetch(
        source: str, path: Path, branch: str | None = None, force: bool = False, clean: bool = True
    ) -> None:
        recorder.calls.append(("fetch", source, path, force, clean))
        (path / ".env").unlink()

    monkeypatch.setattr(
        lifecycle,
        "SourceManager",
        lambda verbose=False: SimpleNamespace(fetch=wiping_fetch, pull=None),
    )

    lifecycle.update_app(DOMAIN, source="https://github.com/example/other.git")

    env_file = app_path / ".env"
    assert env_file.read_text() == "SECRET=kept\n"
    assert stat.S_IMODE(env_file.stat().st_mode) == SECRET_MODE
    assert ("fetch", "https://github.com/example/other.git", app_path, True, True) in recorder.calls


def test_a_static_application_is_not_restarted(
    store: WASMStore, recorder: Recorder, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing runs for a static site; the web server serves the new files."""
    make_app(store, tmp_path / "apps" / "example-com", app_type="static")
    monkeypatch.setattr(
        lifecycle,
        "get_deployer",
        lambda app_type, verbose=False: FakeDeployer(recorder, is_static=True),
    )

    outcome = lifecycle.update_app(DOMAIN)

    assert not any(call[0] == "restart" for call in recorder.calls)
    assert outcome.is_static is True


def test_a_missing_unit_is_reported_not_restarted(
    store: WASMStore, recorder: Recorder, tmp_path: Path
) -> None:
    """The build is done; there is just nothing to restart it into."""
    make_app(store, tmp_path / "apps" / "example-com")
    recorder.service_exists = False

    outcome = lifecycle.update_app(DOMAIN)

    assert outcome.restarted == ()
    assert outcome.active is False
    assert not any(call[0] == "restart" for call in recorder.calls)


def test_a_monorepo_restarts_every_unit_of_the_application(
    store: WASMStore, recorder: Recorder, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each workspace runs as its own unit; all of them run the new build."""
    app = make_app(store, tmp_path / "apps" / "example-com", app_type="monorepo")
    for name in ("example-com-web", "example-com-api"):
        store.create_service(Service(name=name, app_id=app.id, unit_file=f"/x/{name}.service"))
    monkeypatch.setattr(lifecycle, "MonorepoDeployer", lambda verbose=False: FakeDeployer(recorder))

    outcome = lifecycle.update_app(DOMAIN)

    assert set(outcome.restarted) == {"example-com-web", "example-com-api"}
    assert ("update",) in recorder.calls


def test_docker_compose_goes_through_its_deployer(
    store: WASMStore, recorder: Recorder, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The CLI reached into the deployer's private methods with its own timeout."""
    make_app(store, tmp_path / "apps" / "example-com", app_type="docker-compose")
    monkeypatch.setattr(
        lifecycle,
        "DockerComposeDeployer",
        lambda verbose=False: FakeDeployer(recorder, is_static=True),
    )

    outcome = lifecycle.update_app(DOMAIN)

    assert ("update",) in recorder.calls
    assert not any(call[0] == "restart" for call in recorder.calls)
    assert outcome.is_static is True


def test_an_unknown_application_is_a_clear_error(
    store: WASMStore, recorder: Recorder, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing to update is said as such, with the command that would deploy it."""
    monkeypatch.setattr(
        lifecycle, "Config", lambda: SimpleNamespace(apps_directory=tmp_path / "nowhere")
    )

    with pytest.raises(WASMError, match=r"Application not found: example\.com") as exc:
        lifecycle.update_app(DOMAIN)

    assert exc.value.details and "wasm create" in exc.value.details


def test_phases_are_reported_with_their_position(
    store: WASMStore, recorder: Recorder, tmp_path: Path
) -> None:
    """Callers number the steps; the web job turns them into progress."""
    make_app(store, tmp_path / "apps" / "example-com")
    phases: list[tuple[int, int, str]] = []

    lifecycle.update_app(DOMAIN, on_phase=lambda i, n, message: phases.append((i, n, message)))

    assert [p[0] for p in phases] == [1, 2, 3, 4, 5]
    assert all(p[1] == 5 for p in phases)
    assert phases[1][2] == "Pulling latest changes"


def test_the_panel_update_job_runs_the_shared_update(
    store: WASMStore, recorder: Recorder, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The panel's Update used to redeploy from scratch, wiping the tree."""
    from wasm.web import jobs
    from wasm.web.jobs import Job, JobContext, JobType, update_app_job

    monkeypatch.setattr(jobs, "get_store", lambda: store)
    app_path = tmp_path / "apps" / "example-com"
    make_app(store, app_path)
    (app_path / ".env").write_text("SECRET=kept\n")
    progress: list[int] = []
    job = Job(id="j1", type=JobType.UPDATE, name="update", description="update")

    result = update_app_job(
        DOMAIN, job_context=JobContext(job, lambda updated: progress.append(updated.progress))
    )

    assert result["trigger"] == "panel"
    assert recorder.configured["trigger"] == "panel"
    assert ("deploy",) not in recorder.calls
    assert (app_path / ".env").read_text() == "SECRET=kept\n"
    assert progress[-1] == 100


def test_a_tree_that_is_not_a_checkout_fetches_its_recorded_source(
    store: WASMStore, recorder: Recorder, tmp_path: Path, real_fs: Any
) -> None:
    """An archive or a local directory has no remote to pull from."""
    app_path = tmp_path / "apps" / "example-com"
    make_app(store, app_path)
    (app_path / ".git").rmdir()
    (app_path / ".env").write_text("SECRET=kept\n")

    lifecycle.update_app(DOMAIN)

    assert not any(call[0] == "pull" for call in recorder.calls)
    # Copied over the tree, never wiping it: there is no checkout to reset.
    assert ("fetch", "https://github.com/example/app.git", app_path, False, False) in recorder.calls
    assert (app_path / ".env").read_text() == "SECRET=kept\n"


def test_a_monorepo_with_a_unit_that_failed_to_restart_is_not_active(
    store: WASMStore, recorder: Recorder, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two workspaces down and one up must not read as "Running"."""
    from wasm.core.exceptions import ServiceError

    app = make_app(store, tmp_path / "apps" / "example-com", app_type="monorepo")
    for name in ("example-com-web", "example-com-api"):
        store.create_service(Service(name=name, app_id=app.id, unit_file=f"/x/{name}.service"))
    monkeypatch.setattr(lifecycle, "MonorepoDeployer", lambda verbose=False: FakeDeployer(recorder))

    def restart(name: str) -> bool:
        if name == "example-com-api":
            raise ServiceError(f"Failed to restart {name}")
        return True

    monkeypatch.setattr(
        lifecycle,
        "ServiceManager",
        lambda verbose=False: SimpleNamespace(
            get_status=lambda name: {"exists": True, "active": True}, restart=restart
        ),
    )

    outcome = lifecycle.update_app(DOMAIN)

    assert outcome.restarted == ("example-com-web",)
    assert outcome.active is False


def test_updating_from_a_local_directory_keeps_what_the_app_wrote(
    store: WASMStore,
    recorder: Recorder,
    tmp_path: Path,
    real_fs: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    The integration harness caught this: a re-fetch deleted every upload.

    An app deployed from a directory has no checkout to pull, so the update
    fetches the directory again. That fetch used to wipe the tree first; the
    new source must be copied over it instead, leaving the app's own files.
    """
    from wasm.managers.source_manager import SourceManager

    source = tmp_path / "src-app"
    source.mkdir()
    (source / "server.js").write_text("v2")
    (source / ".env").write_text("DEV=1\n")
    app_path = tmp_path / "apps" / "example-com"
    app = make_app(store, app_path)
    app.source = str(source)
    store.update_app(app)
    (app_path / ".git").rmdir()
    (app_path / "server.js").write_text("v1")
    (app_path / ".env").write_text("SECRET=prod\n")
    upload = app_path / "uploads" / "photo.png"
    upload.parent.mkdir()
    upload.write_bytes(b"\x89PNG")
    monkeypatch.setattr(lifecycle, "SourceManager", SourceManager)

    lifecycle.update_app(DOMAIN)

    assert (app_path / "server.js").read_text() == "v2"
    assert upload.read_bytes() == b"\x89PNG"
    assert (app_path / ".env").read_text() == "SECRET=prod\n"


def test_a_forced_git_update_never_deletes_untracked_files(tmp_path: Path) -> None:
    """git clean -fd removed every untracked, unignored file: the uploads."""
    from wasm.core.runner import FakeRunner
    from wasm.managers.source_manager import SourceManager

    runner = FakeRunner()
    repo = tmp_path / "app"
    (repo / ".git").mkdir(parents=True)

    SourceManager(runner=runner).fetch(
        "https://github.com/example/app.git", repo, branch="main", force=True
    )

    assert any("reset" in call for call in runner.calls)
    assert not any("clean" in call for call in runner.calls)


def test_npm_without_a_lockfile_installs_instead_of_failing(tmp_path: Path) -> None:
    """npm ci refuses to run without package-lock.json, with a useless message."""
    from wasm.core.runner import FakeRunner
    from wasm.deployers.helpers.package_manager import PackageManagerHelper

    helper = PackageManagerHelper(runner=FakeRunner())

    assert helper.get_install_command("npm", tmp_path) == ["npm", "install"]
    (tmp_path / "package-lock.json").write_text("{}")
    assert helper.get_install_command("npm", tmp_path) == ["npm", "ci"]
    assert helper.get_install_command("pnpm", tmp_path) == ["pnpm", "install", "--frozen-lockfile"]
