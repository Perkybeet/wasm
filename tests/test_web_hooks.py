# Copyright (c) 2024-2026 Yago Lopez Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
Tests for git webhook auto-deploy.

``POST /hooks/deploy/{domain}`` is the panel's only deliberately
unauthenticated mutation, so these tests are written as attacks first: a
delivery without a signature, with a wrong signature, against a domain that
has no secret, a replayed delivery id, and a flood that must still meet the
rate limiter. Only then do they check that a correctly signed push from
GitHub, GitLab or Gitea queues the one update job the panel already has,
recorded as webhook-triggered.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from wasm.core.store import App, WASMStore
from wasm.web.api import hooks as hooks_module
from wasm.web.api.hooks import mint_webhook_secret, webhook_update_job
from wasm.web.auth import CSRF_HEADER_NAME, SecurityConfig
from wasm.web.jobs import Job, JobContext, JobType
from wasm.web.server import create_app as build_app
from wasm.web.server import get_token_manager

DOMAIN = "app.example.com"

#: A push to the branch the seeded application tracks.
PUSH_MAIN = json.dumps({"ref": "refs/heads/main"}).encode()


def github_signature(secret: str, body: bytes) -> str:
    """
    Sign a body the way GitHub does.

    Args:
        secret: The webhook secret in clear.
        body: The raw request body.

    Returns:
        The ``X-Hub-Signature-256`` header value.
    """
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def gitea_signature(secret: str, body: bytes) -> str:
    """
    Sign a body the way Gitea does.

    Args:
        secret: The webhook secret in clear.
        body: The raw request body.

    Returns:
        The ``X-Gitea-Signature`` header value.
    """
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def github_headers(secret: str, body: bytes, delivery: str | None = None) -> dict[str, str]:
    """
    Build the headers of a GitHub push delivery.

    Args:
        secret: The webhook secret in clear.
        body: The raw request body.
        delivery: Delivery id, generated when omitted.

    Returns:
        The request headers.
    """
    return {
        "X-Hub-Signature-256": github_signature(secret, body),
        "X-GitHub-Delivery": delivery or str(uuid.uuid4()),
        "Content-Type": "application/json",
    }


@pytest.fixture(autouse=True)
def fresh_delivery_cache() -> None:
    """The replay cache is process-wide; no test may inherit another's ids."""
    hooks_module._deliveries.clear()


@pytest.fixture
def store(tmp_path: Path) -> Any:
    """
    Give the panel a store of its own.

    Args:
        tmp_path: Per-test temporary directory.

    Yields:
        The store the hook reads.
    """
    WASMStore.reset_instance()
    instance = WASMStore(tmp_path / "wasm.db")
    try:
        yield instance
    finally:
        instance.close()
        WASMStore.reset_instance()


@pytest.fixture
def seeded(store: WASMStore) -> App:
    """
    Deploy one application on paper.

    Args:
        store: The store fixture.

    Returns:
        The stored application, tracking the ``main`` branch.
    """
    return store.create_app(
        App(
            domain=DOMAIN,
            app_type="nodejs",
            source="https://github.com/you/app",
            branch="main",
            port=3000,
            app_path=f"/var/www/apps/{DOMAIN.replace('.', '-')}",
        )
    )


@pytest.fixture
def secret(store: WASMStore, seeded: App) -> str:
    """
    Enable webhooks for the seeded application.

    Args:
        store: The store fixture.
        seeded: The seeded application.

    Returns:
        The webhook secret in clear.
    """
    return mint_webhook_secret(DOMAIN)


@pytest.fixture
def app(tmp_path: Path, store: WASMStore) -> FastAPI:
    """
    Args:
        tmp_path: Per-test temporary directory.
        store: The store fixture, so the panel never opens a real database.

    Returns:
        The application.
    """
    return build_app(SecurityConfig(state_dir=tmp_path / "state", rate_limit_requests=5000))


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    """
    Args:
        app: The application.

    Returns:
        An anonymous client: a git forge holds no session.
    """
    return TestClient(app, client=("testclient", 50000))


@pytest.fixture
def admin(app: FastAPI) -> TestClient:
    """
    Args:
        app: The application.

    Returns:
        A signed-in client carrying the CSRF header.
    """
    signed_in = TestClient(app, client=("testclient", 50000), follow_redirects=False)
    token = get_token_manager().generate_master_token()
    response = signed_in.post("/api/auth/login", json={"token": token})
    assert response.status_code == 200, response.text
    signed_in.headers[CSRF_HEADER_NAME] = response.json()["csrf_token"]
    return signed_in


@pytest.fixture
def queued(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """
    Capture the update instead of queueing a real job.

    Args:
        monkeypatch: Patching helper, scoped to the test.

    Returns:
        The job descriptions that reached the job manager.
    """
    captured: list[dict[str, Any]] = []

    def create_job(**kwargs: Any) -> Any:
        """
        Args:
            **kwargs: The job description.

        Returns:
            An object shaped like a queued job.
        """
        captured.append(kwargs)

        class Queued:
            """A job that was accepted but never run."""

            id = "job-1"
            status = type("Status", (), {"value": "pending"})()

            def to_dict(self) -> dict[str, Any]:
                """
                Returns:
                    The job as JSON-serialisable data.
                """
                return {"id": self.id}

        return Queued()

    manager = type("FakeJobs", (), {"create_job": staticmethod(create_job)})()
    monkeypatch.setattr("wasm.web.api.hooks.get_job_manager", lambda: manager)
    return captured


def read_audit(tmp_path: Path) -> str:
    """
    Read the audit log written during a test.

    Args:
        tmp_path: Per-test temporary directory.

    Returns:
        The whole log, one JSON object per line.
    """
    path = tmp_path / "state" / "web-audit.log"
    return path.read_text() if path.exists() else ""


def audit_entries(tmp_path: Path) -> list[dict[str, Any]]:
    """
    Parse the audit log written during a test.

    Args:
        tmp_path: Per-test temporary directory.

    Returns:
        One dict per audit line.
    """
    return [json.loads(line) for line in read_audit(tmp_path).splitlines() if line.strip()]


def hook_outcomes(tmp_path: Path) -> set[str]:
    """
    Collect the recorded results of every hook delivery.

    Args:
        tmp_path: Per-test temporary directory.

    Returns:
        The distinct ``result`` values recorded for ``hooks.deploy``.
    """
    return {
        entry["result"] for entry in audit_entries(tmp_path) if entry["action"] == "hooks.deploy"
    }


# ---------------------------------------------------------------------------
# Signed deliveries queue the update job
# ---------------------------------------------------------------------------


def test_a_valid_github_signature_queues_a_webhook_update(
    client: TestClient, secret: str, queued: list[dict[str, Any]]
) -> None:
    """A correctly signed push answers 202 with the job id."""
    response = client.post(
        f"/hooks/deploy/{DOMAIN}", content=PUSH_MAIN, headers=github_headers(secret, PUSH_MAIN)
    )

    assert response.status_code == 202, response.text
    assert response.json()["job_id"] == "job-1"
    assert len(queued) == 1
    assert queued[0]["func"] is webhook_update_job
    assert queued[0]["kwargs"] == {"domain": DOMAIN}
    assert queued[0]["job_type"] == JobType.UPDATE
    assert queued[0]["metadata"]["trigger"] == "webhook"
    assert queued[0]["metadata"]["provider"] == "github"


def test_a_valid_gitlab_token_queues_a_webhook_update(
    client: TestClient, secret: str, queued: list[dict[str, Any]]
) -> None:
    """GitLab authenticates with a shared token rather than a signature."""
    response = client.post(
        f"/hooks/deploy/{DOMAIN}",
        content=PUSH_MAIN,
        headers={
            "X-Gitlab-Token": secret,
            "X-Gitlab-Event-UUID": str(uuid.uuid4()),
            "Content-Type": "application/json",
        },
    )

    assert response.status_code == 202, response.text
    assert len(queued) == 1
    assert queued[0]["metadata"]["provider"] == "gitlab"


def test_a_valid_gitea_signature_queues_a_webhook_update(
    client: TestClient, secret: str, queued: list[dict[str, Any]]
) -> None:
    """Gitea signs like GitHub but without the ``sha256=`` prefix."""
    response = client.post(
        f"/hooks/deploy/{DOMAIN}",
        content=PUSH_MAIN,
        headers={
            "X-Gitea-Signature": gitea_signature(secret, PUSH_MAIN),
            "X-Gitea-Delivery": str(uuid.uuid4()),
            "Content-Type": "application/json",
        },
    )

    assert response.status_code == 202, response.text
    assert len(queued) == 1
    assert queued[0]["metadata"]["provider"] == "gitea"


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------


def test_a_bad_signature_is_refused_without_details(
    client: TestClient, secret: str, queued: list[dict[str, Any]]
) -> None:
    """A wrong signature answers 401 and explains nothing."""
    response = client.post(
        f"/hooks/deploy/{DOMAIN}",
        content=PUSH_MAIN,
        headers={
            "X-Hub-Signature-256": github_signature("not-the-secret", PUSH_MAIN),
            "X-GitHub-Delivery": str(uuid.uuid4()),
        },
    )

    assert response.status_code == 401
    assert queued == []
    # No hint about what was wrong: not the provider, not the expected shape.
    assert response.json() == {"detail": "Unauthorized"}


def test_a_delivery_without_any_credential_is_refused(
    client: TestClient, secret: str, queued: list[dict[str, Any]]
) -> None:
    """No signature header at all is a 401, not a 404: the hook exists."""
    response = client.post(f"/hooks/deploy/{DOMAIN}", content=PUSH_MAIN)

    assert response.status_code == 401
    assert queued == []


def test_a_wrong_gitlab_token_is_refused(
    client: TestClient, secret: str, queued: list[dict[str, Any]]
) -> None:
    """A GitLab token that is not the secret answers 401."""
    response = client.post(
        f"/hooks/deploy/{DOMAIN}",
        content=PUSH_MAIN,
        headers={"X-Gitlab-Token": "not-the-secret"},
    )

    assert response.status_code == 401
    assert queued == []


def test_a_domain_without_a_secret_is_a_generic_404(
    client: TestClient, seeded: App, queued: list[dict[str, Any]]
) -> None:
    """An app with webhooks disabled and an unknown domain are told apart by nothing."""
    disabled = client.post(
        f"/hooks/deploy/{DOMAIN}",
        content=PUSH_MAIN,
        headers=github_headers("whatever", PUSH_MAIN),
    )
    unknown = client.post(
        "/hooks/deploy/nothing.example.com",
        content=PUSH_MAIN,
        headers=github_headers("whatever", PUSH_MAIN),
    )
    not_a_domain = client.post(
        "/hooks/deploy/not_a_domain!",
        content=PUSH_MAIN,
        headers=github_headers("whatever", PUSH_MAIN),
    )

    assert disabled.status_code == 404
    assert unknown.status_code == 404
    assert not_a_domain.status_code == 404
    assert disabled.json() == unknown.json() == not_a_domain.json()
    assert queued == []


# ---------------------------------------------------------------------------
# Branch filter and replay protection
# ---------------------------------------------------------------------------


def test_a_push_to_another_branch_is_ignored_without_a_job(
    client: TestClient, secret: str, queued: list[dict[str, Any]]
) -> None:
    """The app tracks main; a push to develop is acknowledged and dropped."""
    body = json.dumps({"ref": "refs/heads/develop"}).encode()

    response = client.post(
        f"/hooks/deploy/{DOMAIN}", content=body, headers=github_headers(secret, body)
    )

    assert response.status_code == 200
    assert response.json() == {"status": "ignored", "reason": "branch"}
    assert queued == []


def test_a_payload_without_a_ref_is_ignored_when_a_branch_is_tracked(
    client: TestClient, secret: str, queued: list[dict[str, Any]]
) -> None:
    """A ping event carries no ref and must not deploy a branch-tracking app."""
    body = json.dumps({"zen": "Anything added dilutes everything else."}).encode()

    response = client.post(
        f"/hooks/deploy/{DOMAIN}", content=body, headers=github_headers(secret, body)
    )

    assert response.status_code == 200
    assert response.json() == {"status": "ignored", "reason": "branch"}
    assert queued == []


def test_a_replayed_delivery_id_is_ignored(
    client: TestClient, secret: str, queued: list[dict[str, Any]]
) -> None:
    """The same delivery id twice queues exactly one job."""
    headers = github_headers(secret, PUSH_MAIN, delivery="delivery-1")

    first = client.post(f"/hooks/deploy/{DOMAIN}", content=PUSH_MAIN, headers=headers)
    second = client.post(f"/hooks/deploy/{DOMAIN}", content=PUSH_MAIN, headers=headers)

    assert first.status_code == 202
    assert second.status_code == 200
    assert second.json() == {"status": "ignored", "reason": "duplicate"}
    assert len(queued) == 1


# ---------------------------------------------------------------------------
# The middleware still stands in front of the hook
# ---------------------------------------------------------------------------


def test_rate_limiting_applies_to_the_hook_surface(tmp_path: Path, store: WASMStore) -> None:
    """The hook is exempt from sessions, not from the rate limiter."""
    app = build_app(
        SecurityConfig(state_dir=tmp_path / "state", rate_limit_requests=3, rate_limit_window=60)
    )
    client = TestClient(app, client=("testclient", 50000))

    statuses = [
        client.post("/hooks/deploy/nothing.example.com", content=b"{}").status_code
        for _ in range(6)
    ]

    assert statuses[0] == 404
    assert 429 in statuses, f"the rate limiter never engaged: {statuses}"
    assert statuses[-1] == 429


def test_the_ip_whitelist_applies_to_the_hook_surface(tmp_path: Path, store: WASMStore) -> None:
    """An address outside the whitelist never reaches the signature check."""
    app = build_app(SecurityConfig(state_dir=tmp_path / "state", ip_whitelist=["10.0.0.5"]))
    client = TestClient(app, client=("testclient", 50000))

    response = client.post(f"/hooks/deploy/{DOMAIN}", content=b"{}")

    assert response.status_code == 403


# ---------------------------------------------------------------------------
# Secret management: authenticated, admin, shown once
# ---------------------------------------------------------------------------


def test_minting_a_secret_requires_a_session(
    client: TestClient, seeded: App, store: WASMStore
) -> None:
    """Anonymous clients cannot mint or destroy webhook secrets."""
    assert client.post(f"/api/apps/{DOMAIN}/webhook-secret").status_code == 401
    assert client.delete(f"/api/apps/{DOMAIN}/webhook-secret").status_code == 401


def test_mint_and_delete_roundtrip(
    admin: TestClient,
    client: TestClient,
    store: WASMStore,
    seeded: App,
    queued: list[dict[str, Any]],
) -> None:
    """The minted secret verifies real deliveries until it is deleted."""
    minted = admin.post(f"/api/apps/{DOMAIN}/webhook-secret")
    assert minted.status_code == 200, minted.text
    body = minted.json()
    assert body["hook_url"].endswith(f"/hooks/deploy/{DOMAIN}")
    secret = body["secret"]
    assert store.get_webhook_secret(DOMAIN) == secret

    delivery = client.post(
        f"/hooks/deploy/{DOMAIN}", content=PUSH_MAIN, headers=github_headers(secret, PUSH_MAIN)
    )
    assert delivery.status_code == 202

    disabled = admin.delete(f"/api/apps/{DOMAIN}/webhook-secret")
    assert disabled.status_code == 200
    assert store.get_webhook_secret(DOMAIN) is None

    after = client.post(
        f"/hooks/deploy/{DOMAIN}", content=PUSH_MAIN, headers=github_headers(secret, PUSH_MAIN)
    )
    assert after.status_code == 404


def test_minting_again_replaces_the_secret(
    admin: TestClient, store: WASMStore, seeded: App
) -> None:
    """Regeneration invalidates the old secret in the same motion."""
    first = admin.post(f"/api/apps/{DOMAIN}/webhook-secret").json()["secret"]
    second = admin.post(f"/api/apps/{DOMAIN}/webhook-secret").json()["secret"]

    assert first != second
    assert store.get_webhook_secret(DOMAIN) == second


def test_minting_for_an_unknown_domain_is_404(admin: TestClient, store: WASMStore) -> None:
    """The authenticated surface may say so plainly."""
    assert admin.post("/api/apps/nothing.example.com/webhook-secret").status_code == 404
    assert admin.delete("/api/apps/nothing.example.com/webhook-secret").status_code == 404


# ---------------------------------------------------------------------------
# The secret never leaks
# ---------------------------------------------------------------------------


def test_the_secret_never_appears_in_audit_or_hook_responses(
    tmp_path: Path,
    admin: TestClient,
    client: TestClient,
    store: WASMStore,
    seeded: App,
    queued: list[dict[str, Any]],
) -> None:
    """Every delivery is audited; no record or hook response carries the secret."""
    secret = admin.post(f"/api/apps/{DOMAIN}/webhook-secret").json()["secret"]

    accepted = client.post(
        f"/hooks/deploy/{DOMAIN}", content=PUSH_MAIN, headers=github_headers(secret, PUSH_MAIN)
    )
    bad_signature = github_signature("not-the-secret", PUSH_MAIN)
    refused = client.post(
        f"/hooks/deploy/{DOMAIN}",
        content=PUSH_MAIN,
        headers={"X-Hub-Signature-256": bad_signature},
    )
    audit = read_audit(tmp_path)

    assert secret not in accepted.text
    assert secret not in refused.text
    assert secret not in audit
    assert bad_signature.removeprefix("sha256=") not in audit
    # Every outcome leaves a trace: the mint, the accepted delivery, the refusal.
    assert any(entry["action"] == "hooks.secret.mint" for entry in audit_entries(tmp_path))
    assert {"accepted", "denied"} <= hook_outcomes(tmp_path)


def test_ignored_deliveries_are_audited_too(
    tmp_path: Path, client: TestClient, secret: str, queued: list[dict[str, Any]]
) -> None:
    """A branch mismatch is an outcome, and outcomes are recorded."""
    body = json.dumps({"ref": "refs/heads/develop"}).encode()
    client.post(f"/hooks/deploy/{DOMAIN}", content=body, headers=github_headers(secret, body))

    assert "ignored" in hook_outcomes(tmp_path)


# ---------------------------------------------------------------------------
# The job records webhook provenance
# ---------------------------------------------------------------------------


def test_webhook_update_job_deploys_with_webhook_trigger(
    store: WASMStore, seeded: App, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The deployment history must say a robot did it, not the panel."""
    configured: dict[str, Any] = {}

    class FakeDeployer:
        """Records its configuration instead of deploying."""

        def configure(self, **kwargs: Any) -> None:
            configured.update(kwargs)

        def deploy(self) -> bool:
            return True

    class FakeRollback:
        """Stands in for the pre-deploy backup."""

        def __init__(self, verbose: bool = False) -> None:
            pass

        def create_pre_deploy_backup(self, domain: str) -> None:
            configured["backup_domain"] = domain

    monkeypatch.setattr("wasm.deployers.get_deployer", lambda *a, **k: FakeDeployer())
    monkeypatch.setattr("wasm.managers.backup_manager.RollbackManager", FakeRollback)

    job = Job(id="j1", type=JobType.UPDATE, name="update", description="update")
    result = webhook_update_job(DOMAIN, job_context=JobContext(job, lambda _job: None))

    assert result["status"] == "updated"
    assert configured["trigger"] == "webhook"
    assert configured["backup_domain"] == DOMAIN
    assert configured["domain"] == DOMAIN
    assert configured["branch"] == "main"
