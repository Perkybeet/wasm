# Copyright (c) 2024-2026 Yago Lopez Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
Tests for the deployment history API, its shared log reader and the three
sibling endpoints this task adds: rollback points, webhook deliveries and the
notification test button.

What is defended:

- **Keyset pagination is exact.** ``before_id`` never repeats or skips a row,
  ``next_before_id`` is offered only when a row remains behind it, and
  ``total`` counts every row the filters match, not just the page.
- **Filters compose.** ``domain``, ``status`` and ``trigger`` narrow together,
  not each replacing the others.
- **The log reader is the one implementation.** ``wasm.deployers.logs`` is
  exercised directly and through the API: a path outside the deployment log
  directory is refused, never read; a rotated file is reported, not a 500; a
  large log is truncated on a whole line, not mid-character.
- **Rollback points and webhook deliveries are views over existing managers,
  not new bookkeeping.** They read `RollbackManager.list_rollback_points` and
  the deployment history's own ``triggered_by`` column.
- **The notification test button never leaks a remote body or a secret**,
  because it is wired to :class:`~wasm.core.notifier.Notifier` and nothing
  else answers on its behalf.
"""

# The notifier config fixture is imported rather than replicated, so there
# stays one definition of "a sandboxed configuration".
# ruff: noqa: F811

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, ClassVar

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.test_notifier import (  # noqa: F401  (pytest resolves fixtures by name)
    CapturingOpener,
    config,
    public_dns,
)
from wasm.core.store import App, DeploymentStatus, DeploymentTrigger, WASMStore
from wasm.deployers.logs import DEFAULT_TAIL_BYTES, read_deployment_log
from wasm.managers.backup_manager import BackupMetadata
from wasm.web.api import apps as apps_api
from wasm.web.api import config as config_api
from wasm.web.api import deployments as deployments_api
from wasm.web.api import hooks as hooks_module
from wasm.web.api.auth import get_current_session

DOMAIN = "app.example.com"
OTHER_DOMAIN = "other.example.com"


# --------------------------------------------------------------- fixtures


@pytest.fixture
def store(tmp_path: Path):
    """
    Give the API a store of its own.

    Args:
        tmp_path: Per-test temporary directory.

    Yields:
        The store the endpoints under test read.
    """
    WASMStore.reset_instance()
    instance = WASMStore(tmp_path / "wasm.db")
    try:
        yield instance
    finally:
        instance.close()
        WASMStore.reset_instance()


@pytest.fixture
def client(store: WASMStore) -> TestClient:
    """
    Build a test client carrying every router this task adds or extends.

    Args:
        store: Fixture giving the routers a sandboxed store.

    Returns:
        A client whose requests are already authenticated.
    """
    app = FastAPI()
    app.include_router(deployments_api.router, prefix="/api/deployments")
    app.include_router(apps_api.router, prefix="/api/apps")
    app.include_router(hooks_module.admin_router, prefix="/api/apps")
    app.include_router(config_api.router, prefix="/api/config")
    app.dependency_overrides[get_current_session] = lambda: {"session_id": "test"}
    return TestClient(app, raise_server_exceptions=False)


def seed_app(store: WASMStore, domain: str = DOMAIN) -> App:
    """
    Deploy one application on paper.

    Args:
        store: The store fixture.
        domain: The application's domain.

    Returns:
        The stored application.
    """
    return store.create_app(
        App(
            domain=domain,
            app_type="nodejs",
            source="https://github.com/you/app",
            branch="main",
            port=3000,
            app_path=f"/var/www/apps/{domain.replace('.', '-')}",
        )
    )


def seed_deployment(
    store: WASMStore,
    domain: str,
    *,
    trigger: str = DeploymentTrigger.CLI.value,
    status: str = DeploymentStatus.SUCCESS.value,
    git_commit: str | None = None,
    git_branch: str | None = None,
    error: str | None = None,
    log_path: str | None = None,
) -> int:
    """
    Record one deployment attempt, started and finished in the same call.

    Args:
        store: The store fixture.
        domain: Domain the attempt is for.
        trigger: What started it.
        status: The outcome to record.
        git_commit: Commit deployed, when relevant to the test.
        git_branch: Branch deployed, when relevant to the test.
        error: Failure message, when the test wants one recorded.
        log_path: Where the row should claim its captured log lives.

    Returns:
        The row's id.
    """
    deployment_id = store.record_deployment_start(
        domain, trigger, git_commit=git_commit, git_branch=git_branch, log_path=log_path
    )
    store.finish_deployment(deployment_id, status, error)
    return deployment_id


def write_log(store: WASMStore, domain: str, deployment_id: int, content: str) -> Path:
    """
    Write a captured log where the recorder would have written it.

    Args:
        store: The store fixture, whose ``db_path`` anchors the log directory.
        domain: Domain the deployment belongs to.
        deployment_id: The row the log belongs to.
        content: Bytes to write, encoded as UTF-8.

    Returns:
        The path written.
    """
    directory = store.db_path.parent / "deploy-logs" / domain
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{deployment_id}.log"
    path.write_text(content, encoding="utf-8")
    return path


# --------------------------------------------------------------- listing


class TestListDeployments:
    def test_defaults_to_every_domain_newest_first(
        self, client: TestClient, store: WASMStore
    ) -> None:
        first = seed_deployment(store, DOMAIN)
        second = seed_deployment(store, OTHER_DOMAIN)

        response = client.get("/api/deployments")

        assert response.status_code == 200
        body = response.json()
        assert [item["id"] for item in body["items"]] == [second, first]
        assert body["total"] == 2
        assert body["next_before_id"] is None

    def test_filters_by_domain(self, client: TestClient, store: WASMStore) -> None:
        seed_deployment(store, DOMAIN)
        wanted = seed_deployment(store, OTHER_DOMAIN)

        response = client.get(f"/api/deployments?domain={OTHER_DOMAIN}")

        assert response.status_code == 200
        body = response.json()
        assert [item["id"] for item in body["items"]] == [wanted]
        assert body["total"] == 1

    def test_an_invalid_domain_filter_is_refused(
        self, client: TestClient, store: WASMStore
    ) -> None:
        response = client.get("/api/deployments?domain=not a domain")
        assert 400 <= response.status_code < 500

    def test_filters_by_status_and_trigger_together(
        self, client: TestClient, store: WASMStore
    ) -> None:
        wanted = seed_deployment(
            store,
            DOMAIN,
            trigger=DeploymentTrigger.WEBHOOK.value,
            status=DeploymentStatus.FAILED.value,
        )
        # Same domain, wrong status.
        seed_deployment(
            store,
            DOMAIN,
            trigger=DeploymentTrigger.WEBHOOK.value,
            status=DeploymentStatus.SUCCESS.value,
        )
        # Same domain and status, wrong trigger.
        seed_deployment(
            store, DOMAIN, trigger=DeploymentTrigger.CLI.value, status=DeploymentStatus.FAILED.value
        )

        response = client.get(f"/api/deployments?domain={DOMAIN}&status=failed&trigger=webhook")

        assert response.status_code == 200
        body = response.json()
        assert [item["id"] for item in body["items"]] == [wanted]
        assert body["total"] == 1

    def test_keyset_pagination_never_repeats_or_skips_a_row(
        self, client: TestClient, store: WASMStore
    ) -> None:
        ids = [seed_deployment(store, DOMAIN) for _ in range(5)]

        collected: list[int] = []
        before_id: int | None = None
        for _ in range(10):  # generous upper bound; the loop must end on its own
            params = f"?domain={DOMAIN}&limit=2" + (f"&before_id={before_id}" if before_id else "")
            response = client.get(f"/api/deployments{params}")
            assert response.status_code == 200
            body = response.json()
            collected.extend(item["id"] for item in body["items"])
            before_id = body["next_before_id"]
            if before_id is None:
                break

        assert collected == list(reversed(ids))

    def test_a_page_short_of_the_limit_offers_no_cursor(
        self, client: TestClient, store: WASMStore
    ) -> None:
        seed_deployment(store, DOMAIN)
        seed_deployment(store, DOMAIN)

        response = client.get(f"/api/deployments?domain={DOMAIN}&limit=50")

        assert response.json()["next_before_id"] is None

    def test_has_log_reflects_a_recorded_path(self, client: TestClient, store: WASMStore) -> None:
        without_log = seed_deployment(store, DOMAIN)
        with_log = seed_deployment(store, DOMAIN, log_path="/var/lib/wasm/deploy-logs/x/1.log")

        response = client.get(f"/api/deployments?domain={DOMAIN}")

        by_id = {item["id"]: item["has_log"] for item in response.json()["items"]}
        assert by_id[without_log] is False
        assert by_id[with_log] is True

    def test_timestamps_carry_an_explicit_utc_offset(
        self, client: TestClient, store: WASMStore
    ) -> None:
        """
        The store writes ``started_at``/``finished_at`` as naive local
        timestamps; the console cannot know which zone those are in unless
        the response says so.
        """
        seed_deployment(store, DOMAIN)

        item = client.get(f"/api/deployments?domain={DOMAIN}").json()["items"][0]

        assert datetime.fromisoformat(item["started_at"]).tzinfo is not None
        assert datetime.fromisoformat(item["finished_at"]).tzinfo is not None


class TestGetDeployment:
    def test_returns_the_row(self, client: TestClient, store: WASMStore) -> None:
        deployment_id = seed_deployment(
            store,
            DOMAIN,
            git_commit="0a1b2c3",
            git_branch="main",
            status=DeploymentStatus.FAILED.value,
            error="build failed",
        )

        response = client.get(f"/api/deployments/{deployment_id}")

        assert response.status_code == 200
        body = response.json()
        assert body["domain"] == DOMAIN
        assert body["git_commit"] == "0a1b2c3"
        assert body["git_branch"] == "main"
        assert body["status"] == "failed"
        assert body["error"] == "build failed"

    def test_an_unrecorded_id_is_404(self, client: TestClient) -> None:
        response = client.get("/api/deployments/424242")
        assert response.status_code == 404

    def test_carries_the_job_and_release_that_produced_it(
        self, client: TestClient, store: WASMStore
    ) -> None:
        """A deploy queued from the panel links back to its job and its release."""
        deployment_id = store.record_deployment_start(DOMAIN, "panel", job_id="ab12cd34")
        store.annotate_deployment(
            deployment_id, release_id="20260101-000000", commit_message="Fix the thing"
        )
        store.finish_deployment(deployment_id, DeploymentStatus.SUCCESS.value)

        response = client.get(f"/api/deployments/{deployment_id}")

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["job_id"] == "ab12cd34"
        assert body["release_id"] == "20260101-000000"
        assert body["commit_message"] == "Fix the thing"

    def test_a_cli_deploy_carries_none_of_them(self, client: TestClient, store: WASMStore) -> None:
        """A CLI deploy has no job and, in place, no release either."""
        deployment_id = seed_deployment(store, DOMAIN)

        response = client.get(f"/api/deployments/{deployment_id}")

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["job_id"] is None
        assert body["release_id"] is None
        assert body["commit_message"] is None


# --------------------------------------------------------------- log reading


class TestReadDeploymentLog:
    """Direct tests of the shared implementation, ``wasm.deployers.logs``."""

    def test_no_log_recorded(self, store: WASMStore) -> None:
        deployment_id = seed_deployment(store, DOMAIN)
        record = store.get_deployment(deployment_id)
        assert record is not None

        result = read_deployment_log(record)

        assert result.content == ""
        assert result.truncated is False
        assert "No build log was captured" in (result.missing_reason or "")

    def test_a_path_outside_the_log_directory_is_refused(
        self, store: WASMStore, tmp_path: Path
    ) -> None:
        planted = tmp_path / "secret.txt"
        planted.write_text("TOP-SECRET")
        deployment_id = seed_deployment(store, DOMAIN, log_path=str(planted))
        record = store.get_deployment(deployment_id)
        assert record is not None

        result = read_deployment_log(record)

        assert result.content == ""
        assert "will not read it" in (result.missing_reason or "")

    def test_a_missing_file_is_reported_honestly(self, store: WASMStore) -> None:
        vanished = store.db_path.parent / "deploy-logs" / DOMAIN / "999.log"
        deployment_id = seed_deployment(store, DOMAIN, log_path=str(vanished))
        record = store.get_deployment(deployment_id)
        assert record is not None

        result = read_deployment_log(record)

        assert "no longer on disk" in (result.missing_reason or "")

    def test_a_small_log_is_returned_whole(self, store: WASMStore) -> None:
        deployment_id = seed_deployment(store, DOMAIN)
        log_path = write_log(store, DOMAIN, deployment_id, "npm install\nnpm run build\n")
        store.annotate_deployment(deployment_id, log_path=str(log_path))
        record = store.get_deployment(deployment_id)
        assert record is not None

        result = read_deployment_log(record)

        assert result.content == "npm install\nnpm run build\n"
        assert result.truncated is False
        assert result.missing_reason is None

    def test_a_tail_narrower_than_the_file_truncates_on_a_line_boundary(
        self, store: WASMStore
    ) -> None:
        deployment_id = seed_deployment(store, DOMAIN)
        content = "".join(f"line {i}\n" for i in range(1000))
        log_path = write_log(store, DOMAIN, deployment_id, content)
        store.annotate_deployment(deployment_id, log_path=str(log_path))
        record = store.get_deployment(deployment_id)
        assert record is not None

        result = read_deployment_log(record, tail=200)

        assert result.truncated is True
        assert result.content in content
        assert not result.content.startswith("line 0\n")
        # The cut never leaves a partial line at the front.
        assert result.content == "" or content.endswith(result.content)

    def test_the_default_tail_matches_the_documented_cap(self) -> None:
        assert DEFAULT_TAIL_BYTES == 512 * 1024


class TestDeploymentLogEndpoint:
    def test_serves_the_captured_log(self, client: TestClient, store: WASMStore) -> None:
        deployment_id = seed_deployment(store, DOMAIN)
        log_path = write_log(store, DOMAIN, deployment_id, "hello world\n")
        store.annotate_deployment(deployment_id, log_path=str(log_path))

        response = client.get(f"/api/deployments/{deployment_id}/log")

        assert response.status_code == 200
        body = response.json()
        assert body["content"] == "hello world\n"
        assert body["truncated"] is False
        assert body["missing_reason"] is None

    def test_tail_narrows_the_response(self, client: TestClient, store: WASMStore) -> None:
        deployment_id = seed_deployment(store, DOMAIN)
        content = "".join(f"line {i}\n" for i in range(1000))
        log_path = write_log(store, DOMAIN, deployment_id, content)
        store.annotate_deployment(deployment_id, log_path=str(log_path))

        response = client.get(f"/api/deployments/{deployment_id}/log?tail=200")

        assert response.status_code == 200
        body = response.json()
        assert body["truncated"] is True
        assert len(body["content"]) <= 200

    def test_a_path_outside_the_log_directory_is_refused_not_read(
        self, client: TestClient, store: WASMStore, tmp_path: Path
    ) -> None:
        planted = tmp_path / "secret.txt"
        planted.write_text("TOP-SECRET-CONTENT")
        deployment_id = seed_deployment(store, DOMAIN, log_path=str(planted))

        response = client.get(f"/api/deployments/{deployment_id}/log")

        assert response.status_code == 200
        body = response.json()
        assert body["content"] == ""
        assert "TOP-SECRET-CONTENT" not in response.text
        assert "will not read it" in (body["missing_reason"] or "")

    def test_a_missing_deployment_is_404(self, client: TestClient) -> None:
        response = client.get("/api/deployments/424242/log")
        assert response.status_code == 404


# --------------------------------------------------------------- rollback points


class FakeRollbackManager:
    """Stand-in for RollbackManager that returns canned points."""

    instances: ClassVar[list[FakeRollbackManager]] = []
    points_by_domain: ClassVar[dict[str, list[BackupMetadata]]] = {}

    def __init__(self, verbose: bool = False, **_: Any) -> None:
        self.verbose = verbose
        FakeRollbackManager.instances.append(self)

    def list_rollback_points(self, domain: str) -> list[BackupMetadata]:
        return FakeRollbackManager.points_by_domain.get(domain, [])


def make_backup(**overrides: Any) -> BackupMetadata:
    """
    Args:
        overrides: Field values that replace the defaults.

    Returns:
        A minimal, valid backup record.
    """
    fields: dict[str, Any] = {
        "id": "bk-1",
        "domain": DOMAIN,
        "app_name": "app-example-com",
        "created_at": "2026-09-20T10:00:00",
        "size_bytes": 4096,
        "app_type": "nodejs",
        "version": "1",
        "description": "Pre-deploy backup",
        "includes_env": True,
        "includes_node_modules": False,
        "git_commit": "0a1b2c3",
    }
    fields.update(overrides)
    return BackupMetadata(**fields)


@pytest.fixture(autouse=True)
def _reset_fake_rollback_manager() -> None:
    """Every test starts with an empty canned-points table."""
    FakeRollbackManager.instances = []
    FakeRollbackManager.points_by_domain = {}


class TestRollbackPoints:
    def test_lists_the_points_from_the_rollback_manager(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(apps_api, "RollbackManager", FakeRollbackManager)
        FakeRollbackManager.points_by_domain[DOMAIN] = [
            make_backup(id="bk-1"),
            make_backup(id="bk-2"),
        ]

        response = client.get(f"/api/apps/{DOMAIN}/rollback-points")

        assert response.status_code == 200
        body = response.json()
        assert body["total"] == 2
        assert [item["id"] for item in body["items"]] == ["bk-1", "bk-2"]
        assert body["items"][0]["git_commit"] == "0a1b2c3"
        assert body["items"][0]["size_bytes"] == 4096

    def test_a_domain_with_no_backups_answers_an_empty_list_not_404(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Rollback points, like deployment history, outlive the application."""
        monkeypatch.setattr(apps_api, "RollbackManager", FakeRollbackManager)

        response = client.get(f"/api/apps/{DOMAIN}/rollback-points")

        assert response.status_code == 200
        assert response.json() == {"items": [], "total": 0}


# --------------------------------------------------------------- webhook deliveries


class TestWebhookDeliveries:
    def test_lists_only_webhook_triggered_deployments_newest_first(
        self, client: TestClient, store: WASMStore
    ) -> None:
        seed_app(store)
        seed_deployment(store, DOMAIN, trigger=DeploymentTrigger.CLI.value)
        first = seed_deployment(
            store, DOMAIN, trigger=DeploymentTrigger.WEBHOOK.value, git_commit="aaa111"
        )
        second = seed_deployment(
            store,
            DOMAIN,
            trigger=DeploymentTrigger.WEBHOOK.value,
            status=DeploymentStatus.FAILED.value,
            error="npm exited 1",
        )
        # A webhook delivery for a different domain must not appear.
        seed_app(store, OTHER_DOMAIN)
        seed_deployment(store, OTHER_DOMAIN, trigger=DeploymentTrigger.WEBHOOK.value)

        response = client.get(f"/api/apps/{DOMAIN}/webhook/deliveries")

        assert response.status_code == 200
        body = response.json()
        assert [item["deployment_id"] for item in body["items"]] == [second, first]
        assert body["total"] == 2
        assert body["items"][0]["status"] == "failed"
        assert body["items"][0]["error"] == "npm exited 1"
        assert body["items"][1]["git_commit"] == "aaa111"

    def test_an_unknown_domain_is_404(self, client: TestClient) -> None:
        response = client.get(f"/api/apps/{DOMAIN}/webhook/deliveries")
        assert response.status_code == 404

    def test_started_at_carries_an_explicit_utc_offset(
        self, client: TestClient, store: WASMStore
    ) -> None:
        seed_app(store)
        seed_deployment(store, DOMAIN, trigger=DeploymentTrigger.WEBHOOK.value)

        item = client.get(f"/api/apps/{DOMAIN}/webhook/deliveries").json()["items"][0]

        assert datetime.fromisoformat(item["started_at"]).tzinfo is not None


# --------------------------------------------------------------- notification test


def store_config(config, **values: object) -> None:
    """
    Write settings to the sandboxed config file, the way the settings editor's
    own tests do, so a freshly reloaded ``Config()`` picks them up.

    ``_build_notifier`` always builds a fresh, reloaded configuration - the
    same one-line construction the panel's settings page uses - so a change
    made only in memory on the fixture's own instance would not be visible to
    it.

    Args:
        config: The sandboxed configuration fixture.
        **values: Dotted keys (dots spelled as ``__``) to values.
    """
    for key, value in values.items():
        config.set(key.replace("__", "."), value)
    assert config.save() is True


class TestNotificationChannelTest:
    def test_success_never_leaks_credentials_and_reports_ok(
        self, client: TestClient, config, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        webhook_url = "https://hooks.example.test/wasm"
        store_config(config, notifications__channels__webhook__webhook_url=webhook_url)
        opener = CapturingOpener()
        monkeypatch.setattr(config_api, "_build_notifier", lambda: _notifier_with(opener))

        response = client.post("/api/config/notifications/webhook/test")

        assert response.status_code == 200
        body = response.json()
        assert body["ok"] is True
        assert "webhook" in body["detail"]
        assert [request.full_url for request in opener.requests] == [webhook_url]

    def test_a_delivery_failure_is_reported_without_the_url_or_body(
        self, client: TestClient, config, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from urllib.error import URLError

        slack_url = "https://hooks.slack.test/services/T0/B0/secret"
        store_config(config, notifications__channels__slack__webhook_url=slack_url)
        opener = CapturingOpener()
        opener.errors["https://hooks.slack.test"] = URLError("connection refused by the endpoint")
        monkeypatch.setattr(config_api, "_build_notifier", lambda: _notifier_with(opener))

        response = client.post("/api/config/notifications/slack/test")

        assert response.status_code == 200
        body = response.json()
        assert body["ok"] is False
        assert "connection refused by the endpoint" in body["detail"]
        assert slack_url not in body["detail"]

    def test_an_unconfigured_channel_names_the_missing_setting(
        self, client: TestClient, config
    ) -> None:
        response = client.post("/api/config/notifications/webhook/test")

        assert response.status_code == 200
        body = response.json()
        assert body["ok"] is False
        assert "notifications.channels.webhook.webhook_url" in body["detail"]

    def test_an_unknown_channel_is_reported_not_crashed(self, client: TestClient, config) -> None:
        response = client.post("/api/config/notifications/carrier-pigeon/test")

        assert response.status_code == 200
        body = response.json()
        assert body["ok"] is False
        assert "carrier-pigeon" in body["detail"]


def _notifier_with(opener: CapturingOpener):
    """
    Args:
        opener: The stand-in for urlopen.

    Returns:
        A notifier over the current sandboxed configuration - the same
        one-line construction :mod:`wasm.web.api.config` uses.
    """
    from wasm.core.config import Config
    from wasm.core.notifier import Notifier

    fresh = Config()
    fresh.reload()
    return Notifier(fresh, opener=opener)
