# Copyright (c) 2024-2026 Yago Lopez Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
Tests for the deployment history recording.

Every deploy, update and rollback must leave a row in the ``deployments``
table with its build log captured to a file, whoever triggered it. The
recording lives in the pipeline and the rollback manager, not in the callers,
so these tests drive the deployers and the rollback manager and read the store,
never a recording API of their own.

The other property defended here is the inverse: recording must never take a
deployment down with it. A history database on fire is a warning, not a failed
deploy.
"""

from __future__ import annotations

import stat
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from tests.test_deployers import build_deployer
from wasm.core.exceptions import BackupError, BuildError
from wasm.core.fs import DryRunFileSystem, set_fs
from wasm.core.runner import FakeRunner
from wasm.core.store import DeploymentStatus, StoreError, WASMStore
from wasm.deployers.nodejs import NodeJSDeployer
from wasm.deployers.recorder import CapturingLogger, DeploymentRecorder
from wasm.managers.backup_manager import BackupMetadata, RollbackManager

DOMAIN = "app.example.com"

#: The safety flags SourceManager puts between ``git`` and the subcommand.
GIT = ("git", "-c", "protocol.ext.allow=never", "-c", "protocol.file.allow=never")


@pytest.fixture(autouse=True)
def real_filesystem():
    """
    Run every test in this file against the real filesystem seam.

    A CLI test that rehearses ``--dry-run`` installs the refusing filesystem
    process-wide and cannot know when the rehearsal is over. This file asserts
    on files the recorder really writes, so it must not inherit that state,
    whatever ran before it.
    """
    set_fs(None)
    yield
    set_fs(None)


@pytest.fixture
def store(tmp_path: Path):
    """
    Provide an isolated store, installed as the process-wide singleton.

    Args:
        tmp_path: Per-test temporary directory.

    Yields:
        The store the recording under test writes to.
    """
    WASMStore.reset_instance()
    instance = WASMStore(tmp_path / "wasm.db")
    yield instance
    WASMStore.reset_instance()


def happy_deployer(tmp_path: Path) -> Any:
    """
    Build a deployer whose every pipeline step succeeds.

    Args:
        tmp_path: Directory the application is deployed into.

    Returns:
        The deployer, ready to deploy.
    """
    deployer = build_deployer(NodeJSDeployer, tmp_path)
    (tmp_path / "app").mkdir(parents=True, exist_ok=True)
    deployer.fetch_source = lambda: True
    deployer.install_dependencies = lambda: True
    deployer.build = lambda: True
    deployer.health_check = lambda retries=5, delay=2.0: True
    return deployer


# ---------------------------------------------------------------------------
# Deploys are recorded
# ---------------------------------------------------------------------------


def test_successful_deploy_records_success_with_captured_log(
    tmp_path: Path, store: WASMStore
) -> None:
    """The happy path leaves one success row whose log holds the pipeline lines."""
    deployer = happy_deployer(tmp_path)

    assert deployer.deploy() is True

    records = store.list_deployments(DOMAIN)
    assert len(records) == 1
    record = records[0]
    assert record.status == DeploymentStatus.SUCCESS.value
    assert record.triggered_by == "cli", "the CLI is the default trigger"
    assert record.started_at is not None
    assert record.finished_at is not None
    assert record.duration_s is not None

    assert record.log_path is not None
    log = Path(record.log_path)
    assert log == tmp_path / "deploy-logs" / DOMAIN / f"{record.id}.log"
    assert log.exists()

    content = log.read_text()
    assert "Fetching source code" in content
    assert "Starting application" in content

    assert stat.S_IMODE(log.stat().st_mode) == 0o640
    assert stat.S_IMODE(log.parent.stat().st_mode) == 0o750


def test_streamed_build_output_is_captured_without_verbose(
    tmp_path: Path, store: WASMStore
) -> None:
    """
    The runner streams install output through ``logger.debug``, which a quiet
    console drops. The captured log must hold it anyway: it is the part the
    operator reads after a failure.
    """
    fake = FakeRunner()
    fake.script(["npm", "ci"], stdout="added 42 packages in 3s\n")

    deployer = build_deployer(NodeJSDeployer, tmp_path)
    deployer._runner = fake
    (tmp_path / "app").mkdir(parents=True, exist_ok=True)
    deployer.fetch_source = lambda: True
    deployer.pre_install = lambda: True
    deployer.post_install = lambda: True
    deployer.package_manager = "npm"
    deployer.build = lambda: True
    deployer.health_check = lambda retries=5, delay=2.0: True

    assert deployer.deploy() is True

    record = store.list_deployments(DOMAIN)[0]
    assert record.log_path is not None
    assert "added 42 packages in 3s" in Path(record.log_path).read_text()


def test_failed_deploy_records_the_error_verbatim(tmp_path: Path, store: WASMStore) -> None:
    """A failure closes the row as failed, holding the build tool's own words."""
    deployer = happy_deployer(tmp_path)
    error = BuildError("Build failed", details="npm ERR! missing script: build")

    def explode() -> None:
        raise error

    deployer.build = explode

    with pytest.raises(BuildError):
        deployer.deploy()

    record = store.list_deployments(DOMAIN)[0]
    assert record.status == DeploymentStatus.FAILED.value
    assert record.error is not None
    assert "Build failed" in record.error
    assert "npm ERR! missing script: build" in record.error

    assert record.log_path is not None
    assert "Deployment failed" in Path(record.log_path).read_text()


def test_deploy_records_git_commit_and_branch(
    tmp_path: Path, store: WASMStore, runner: FakeRunner
) -> None:
    """A deployed git checkout records what was actually checked out."""
    runner.script([*GIT, "rev-parse", "--abbrev-ref", "HEAD"], stdout="main\n")
    runner.script([*GIT, "rev-parse", "--short", "HEAD"], stdout="abc1234\n")

    deployer = happy_deployer(tmp_path)
    (tmp_path / "app" / ".git").mkdir(parents=True)

    assert deployer.deploy() is True

    record = store.list_deployments(DOMAIN)[0]
    assert record.git_commit == "abc1234"
    assert record.git_branch == "main"


def test_trigger_is_recorded_per_caller(tmp_path: Path, store: WASMStore) -> None:
    """The trigger flows from configure() into the history row."""
    deployer = happy_deployer(tmp_path)
    deployer.configure(
        DOMAIN,
        "https://github.com/example/app.git",
        port=3000,
        ssl=False,
        app_path=tmp_path / "app",
        trigger="panel",
    )

    assert deployer.deploy() is True

    assert store.list_deployments(DOMAIN)[0].triggered_by == "panel"


# ---------------------------------------------------------------------------
# Updates are recorded
# ---------------------------------------------------------------------------


def test_update_records_history(tmp_path: Path, store: WASMStore) -> None:
    """An in-place update leaves the same kind of row a deploy does."""
    deployer = build_deployer(NodeJSDeployer, tmp_path)
    deployer.pre_install = lambda: True
    deployer.install_dependencies = lambda: True
    deployer.build = lambda: True
    deployer.get_start_command = lambda: "node server.js"

    deployer.update()

    record = store.list_deployments(DOMAIN)[0]
    assert record.status == DeploymentStatus.SUCCESS.value
    assert record.triggered_by == "cli"
    assert record.log_path is not None
    assert Path(record.log_path).exists()


def test_failed_update_records_the_failure(tmp_path: Path, store: WASMStore) -> None:
    """A broken build during update is recorded as failed, error verbatim."""
    deployer = build_deployer(NodeJSDeployer, tmp_path)
    deployer.pre_install = lambda: True
    deployer.install_dependencies = lambda: True

    def explode() -> None:
        raise BuildError("Build failed", details="tsc: 12 errors")

    deployer.build = explode

    with pytest.raises(BuildError):
        deployer.update()

    record = store.list_deployments(DOMAIN)[0]
    assert record.status == DeploymentStatus.FAILED.value
    assert record.error is not None
    assert "tsc: 12 errors" in record.error


# ---------------------------------------------------------------------------
# Rotation
# ---------------------------------------------------------------------------


def test_history_rotates_rows_and_log_files(tmp_path: Path, store: WASMStore) -> None:
    """Pruning the oldest rows also deletes the log files they pointed at."""
    log_root = tmp_path / "deploy-logs"
    ids: list[int] = []

    for _ in range(5):
        recorder = DeploymentRecorder(
            store, DOMAIN, "cli", logger=CapturingLogger(), log_root=log_root, keep=3
        )
        recorder.start()
        assert recorder.deployment_id is not None
        ids.append(recorder.deployment_id)
        recorder.finish_success()

    surviving = [record.id for record in store.list_deployments(DOMAIN)]
    assert surviving == [ids[4], ids[3], ids[2]]
    assert store.get_deployment(ids[0]) is None
    assert store.get_deployment(ids[1]) is None

    remaining_files = sorted(int(f.stem) for f in (log_root / DOMAIN).glob("*.log"))
    assert remaining_files == sorted(surviving)


# ---------------------------------------------------------------------------
# Recording never takes the deployment down
# ---------------------------------------------------------------------------


def test_a_store_failure_does_not_abort_the_deploy(
    tmp_path: Path, store: WASMStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A history table on fire is a warning, not a failed deploy."""

    def explode(*args: Any, **kwargs: Any) -> int:
        raise StoreError("history table is on fire")

    monkeypatch.setattr(store, "record_deployment_start", explode)

    deployer = happy_deployer(tmp_path)

    assert deployer.deploy() is True
    assert store.list_deployments(DOMAIN) == []


def test_a_failing_finish_does_not_abort_the_deploy(
    tmp_path: Path, store: WASMStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The deploy already happened; a finish that cannot be written stays a warning."""

    def explode(*args: Any, **kwargs: Any) -> None:
        raise StoreError("disk full")

    monkeypatch.setattr(store, "finish_deployment", explode)

    deployer = happy_deployer(tmp_path)

    assert deployer.deploy() is True

    record = store.list_deployments(DOMAIN)[0]
    assert record.status == DeploymentStatus.RUNNING.value, "the row simply never closed"


def test_a_rehearsal_records_nothing(tmp_path: Path, store: WASMStore) -> None:
    """A dry run that left history rows behind would have changed the machine."""
    recorder = DeploymentRecorder(
        store,
        DOMAIN,
        "cli",
        logger=CapturingLogger(),
        fs=DryRunFileSystem(),
        log_root=tmp_path / "deploy-logs",
    )

    recorder.start()
    recorder.finish_success()

    assert recorder.deployment_id is None
    assert store.list_deployments(DOMAIN) == []
    assert not (tmp_path / "deploy-logs").exists()


# ---------------------------------------------------------------------------
# Rollbacks are recorded
# ---------------------------------------------------------------------------


def test_rollback_is_recorded_and_marks_the_reverted_deployment(
    tmp_path: Path, store: WASMStore, runner: FakeRunner
) -> None:
    """
    A rollback is its own history row; the build it discards turns rolled_back.

    The reverted row keeps its original timing: reclassifying a deployment
    days later must not rewrite how long it took.
    """
    previous_id = store.record_deployment_start(DOMAIN, "cli")
    store.finish_deployment(previous_id, DeploymentStatus.SUCCESS.value)
    before = store.get_deployment(previous_id)
    assert before is not None

    metadata = BackupMetadata(
        id="backup-1",
        domain=DOMAIN,
        app_name="app-example-com",
        created_at=datetime.now().isoformat(),
        size_bytes=123,
        app_type="nodejs",
        version="2.0.0",
        description="manual",
        includes_env=True,
        includes_node_modules=False,
        git_commit="abc1234",
        git_branch="main",
    )

    class FakeBackups:
        """Answers for the one backup the test rolls back to."""

        def get_backup(self, backup_id: str) -> BackupMetadata:
            return metadata

        def restore(self, **kwargs: Any) -> bool:
            return True

    class FakeServices:
        """A machine with no unit for the application."""

        def get_status(self, name: str) -> dict[str, Any]:
            return {"exists": False}

    manager = RollbackManager(verbose=False)
    manager.backup_manager = FakeBackups()  # type: ignore[assignment]
    manager.service_manager = FakeServices()  # type: ignore[assignment]

    assert manager.rollback(DOMAIN, backup_id="backup-1", rebuild=False, trigger="panel") is True

    records = store.list_deployments(DOMAIN)
    assert len(records) == 2

    rollback_record = records[0]
    assert rollback_record.status == DeploymentStatus.SUCCESS.value
    assert rollback_record.triggered_by == "panel"
    assert rollback_record.git_commit == "abc1234"
    assert rollback_record.log_path is not None
    assert "Rolling back to: backup-1" in Path(rollback_record.log_path).read_text()

    reverted = store.get_deployment(previous_id)
    assert reverted is not None
    assert reverted.status == DeploymentStatus.ROLLED_BACK.value
    assert reverted.finished_at == before.finished_at
    assert reverted.duration_s == before.duration_s


def test_a_refused_rollback_records_nothing(tmp_path: Path, store: WASMStore) -> None:
    """No backup to roll back to means nothing ran, so nothing is recorded."""

    class EmptyBackups:
        """A machine with no backups at all."""

        def list_backups(self, domain: str | None = None) -> list[BackupMetadata]:
            return []

    manager = RollbackManager(verbose=False)
    manager.backup_manager = EmptyBackups()  # type: ignore[assignment]

    with pytest.raises(BackupError, match="No backups found"):
        manager.rollback(DOMAIN)

    assert store.list_deployments(DOMAIN) == []


# ---------------------------------------------------------------------------
# The capturing logger
# ---------------------------------------------------------------------------


def test_capturing_logger_mirrors_suppressed_detail(capsys: pytest.CaptureFixture) -> None:
    """Verbose-only lines reach the sink even when the console drops them."""
    logger = CapturingLogger(verbose=False)
    lines: list[str] = []
    logger.attach_sink(lines.append)

    logger.step(1, 2, "Building application")
    logger.debug("hidden debug line")
    logger.substep("hidden substep")
    logger.command_output("stdout line\n", "stderr line\n")

    captured = "\n".join(lines)
    assert "Building application" in captured
    assert "hidden debug line" in captured
    assert "hidden substep" in captured
    assert "stdout line" in captured
    assert "stderr line" in captured

    console = capsys.readouterr().out
    assert "hidden debug line" not in console, "the console keeps its quiet default"

    logger.detach_sink()
    logger.debug("after detach")
    assert "after detach" not in "\n".join(lines)


# ---------------------------------------------------------------------------
# The store's side of the recording
# ---------------------------------------------------------------------------


def test_annotate_deployment_fills_only_the_given_fields(store: WASMStore) -> None:
    """Annotation adds facts without disturbing what is already recorded."""
    deployment_id = store.record_deployment_start(DOMAIN, "cli", git_branch="main")

    assert store.annotate_deployment(deployment_id, log_path="/tmp/x.log") is True
    assert store.annotate_deployment(deployment_id, git_commit="abc1234") is True
    assert store.annotate_deployment(deployment_id) is False, "nothing to update"

    record = store.get_deployment(deployment_id)
    assert record is not None
    assert record.log_path == "/tmp/x.log"
    assert record.git_commit == "abc1234"
    assert record.git_branch == "main"
    assert store.annotate_deployment(999_999, log_path="/nope") is False


def test_mark_deployment_rolled_back_preserves_timing(store: WASMStore) -> None:
    """Reclassifying a finished deployment must not rewrite its duration."""
    deployment_id = store.record_deployment_start(DOMAIN, "cli")
    store.finish_deployment(deployment_id, DeploymentStatus.SUCCESS.value)
    before = store.get_deployment(deployment_id)
    assert before is not None

    assert store.mark_deployment_rolled_back(deployment_id) is True

    after = store.get_deployment(deployment_id)
    assert after is not None
    assert after.status == DeploymentStatus.ROLLED_BACK.value
    assert after.finished_at == before.finished_at
    assert after.duration_s == before.duration_s
    assert store.mark_deployment_rolled_back(999_999) is False
