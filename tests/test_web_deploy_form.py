# Copyright (c) 2024-2026 Yago Lopez Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
Tests for the htmx deployment form and the git webhook section.

The form moved from ``views/router.py`` into ``views/deploy_form.py`` and
grew per-field checks and an Advanced section. What is defended here:

- **The move changed no address.** ``/apps/new`` answers at the same URL with
  the same methods, still declared before ``/apps/{domain}``, and the old
  handlers are gone from the aggregate router rather than duplicated in it.
- **A field's refusal is inline and early.** An invalid domain is reported
  under the field as it is typed, through the same validators the chokepoint
  uses; the guard itself stays in ``create_app``.
- **The advanced options travel whole.** What the operator types under
  Advanced reaches the queued job and the deployer's ``configure`` under
  exactly the keyword names the monorepo and docker-compose deployers already
  accept. An option that silently stops halfway is a form that lies.
- **The webhook secret is shown once.** The minting response is the only
  place it ever appears; a re-render of the section can say "enabled" and no
  more. Rotating replaces it, disabling discards it.
"""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from wasm.core.store import App, WASMStore
from wasm.web.auth import CSRF_HEADER_NAME, SecurityConfig
from wasm.web.server import create_app as build_app
from wasm.web.server import get_token_manager


@pytest.fixture
def store(tmp_path: Path) -> Any:
    """
    Give the panel a store of its own.

    Args:
        tmp_path: Per-test temporary directory.

    Yields:
        The store the pages read.
    """
    WASMStore.reset_instance()
    instance = WASMStore(tmp_path / "wasm.db")
    try:
        yield instance
    finally:
        instance.close()
        WASMStore.reset_instance()


@pytest.fixture
def app(tmp_path: Path, store: Any, runner: object) -> FastAPI:
    """
    Args:
        tmp_path: Per-test temporary directory.
        store: The store fixture.
        runner: The fake command runner, so no manager reaches a real process.

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
        A signed-in client carrying the CSRF header.
    """
    signed_in = TestClient(app, client=("testclient", 50000), follow_redirects=False)
    token = get_token_manager().generate_master_token()
    response = signed_in.post("/api/auth/login", json={"token": token})
    assert response.status_code == 200, response.text
    signed_in.headers[CSRF_HEADER_NAME] = response.json()["csrf_token"]
    return signed_in


@pytest.fixture
def queued(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """
    Capture the deployment instead of queueing a real job.

    Args:
        monkeypatch: Patching helper, scoped to the test.

    Returns:
        The requests that reached the job manager.
    """
    captured: list[Any] = []

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
    monkeypatch.setattr("wasm.web.api.apps.get_job_manager", lambda: manager)
    return captured


def deploy(store: Any, domain: str = "app.example.com") -> App:
    """
    Record an application, as a deploy would.

    Args:
        store: The store to write to.
        domain: The application's domain.

    Returns:
        The stored application.
    """
    return store.create_app(
        App(
            domain=domain,
            app_type="nextjs",
            source="https://github.com/you/app",
            port=3000,
            app_path=f"/var/www/apps/{domain}",
            status="running",
        )
    )


FORM = {
    "domain": "app.example.com",
    "source": "https://github.com/you/app",
    "app_type": "nextjs",
    "branch": "main",
    "port": "",
    "webserver": "nginx",
    "ssl": "yes",
}


# ---------------------------------------------------------------------------
# The move
# ---------------------------------------------------------------------------


def test_the_form_still_renders_at_its_old_address(client: TestClient) -> None:
    """
    The route moved modules, not URLs, and it must still be matched before
    ``/apps/{domain}`` or the panel goes looking for an application called
    "new".

    Args:
        client: A signed-in client.
    """
    response = client.get("/apps/new")

    assert response.status_code == 200
    assert "Deploy an application" in response.text
    assert "Advanced" in response.text
    assert 'name="env"' in response.text


def test_the_aggregate_router_no_longer_owns_the_form(
    client: TestClient, queued: list[Any]
) -> None:
    """
    The handlers moved; a copy left behind in router.py would be a second
    implementation waiting to drift.

    Args:
        client: A signed-in client.
        queued: Captured job requests.
    """
    aggregate = importlib.import_module("wasm.web.views.router")
    assert not hasattr(aggregate, "deploy_submit")

    moved = importlib.import_module("wasm.web.views.deploy_form")
    assert hasattr(moved, "deploy_submit")
    assert hasattr(moved, "deploy_form")

    response = client.post("/apps/new", data=FORM)
    assert response.status_code == 303
    assert response.headers["location"] == "/activity"
    assert len(queued) == 1


# ---------------------------------------------------------------------------
# Per-field checks
# ---------------------------------------------------------------------------


def test_an_invalid_domain_is_refused_inline(client: TestClient) -> None:
    """
    Args:
        client: A signed-in client.
    """
    response = client.post("/apps/new/validate/domain", data={"domain": "not a domain"})

    assert response.status_code == 200, "htmx does not swap an error status"
    assert 'id="check-domain"' in response.text
    assert "problem" in response.text, "the refusal should render as a problem block"


def test_a_deployed_domain_is_reported_inline(client: TestClient, store: Any) -> None:
    """
    Args:
        client: A signed-in client.
        store: The store, holding the application already.
    """
    deploy(store)

    body = client.post("/apps/new/validate/domain", data={"domain": "app.example.com"}).text

    assert "already exists" in body


def test_a_sound_domain_clears_the_refusal(client: TestClient) -> None:
    """
    The verdict fragment swaps whole, so "fine" has to come back as an empty
    check element rather than nothing at all.

    Args:
        client: A signed-in client.
    """
    body = client.post("/apps/new/validate/domain", data={"domain": "fine.example.com"}).text

    assert 'id="check-domain"' in body
    assert "problem" not in body


def test_a_malformed_environment_is_refused_inline(client: TestClient) -> None:
    """
    Args:
        client: A signed-in client.
    """
    body = client.post("/apps/new/validate/env", data={"env": "BAD KEY=value"}).text

    assert 'id="check-env"' in body
    assert "problem" in body


def test_a_malformed_subdomain_mapping_is_refused_inline(client: TestClient) -> None:
    """
    Args:
        client: A signed-in client.
    """
    body = client.post("/apps/new/validate/subdomains", data={"subdomains": "no-colon-here"}).text

    assert "workspace:subdomain" in body


def test_a_field_nobody_validates_is_not_reported_fine(client: TestClient) -> None:
    """
    A template pointing a check at a field this module does not know must fail
    loudly, not answer "fine" about a value nothing looked at.

    Args:
        client: A signed-in client.
    """
    assert client.post("/apps/new/validate/nonsense", data={}).status_code == 404


def test_the_checks_demand_a_session(app: FastAPI) -> None:
    """
    Args:
        app: The application.
    """
    anonymous = TestClient(app, client=("testclient", 50000), follow_redirects=False)

    response = anonymous.post("/apps/new/validate/domain", data={"domain": "a.example.com"})
    assert response.status_code == 303


# ---------------------------------------------------------------------------
# The type-specific options fragment
# ---------------------------------------------------------------------------


def test_monorepo_options_appear_for_the_monorepo_type(client: TestClient) -> None:
    """
    Args:
        client: A signed-in client.
    """
    body = client.get("/apps/new/options", params={"app_type": "monorepo"}).text

    assert 'name="subdomains"' in body
    assert 'name="workspaces"' in body
    assert 'name="no_database"' in body
    assert 'name="compose_file"' not in body


def test_compose_options_appear_for_the_compose_type(client: TestClient) -> None:
    """
    Args:
        client: A signed-in client.
    """
    body = client.get("/apps/new/options", params={"app_type": "docker-compose"}).text

    assert 'name="compose_file"' in body
    assert 'name="compose_profiles"' in body
    assert 'name="subdomains"' not in body


def test_other_types_ask_no_extra_questions(client: TestClient) -> None:
    """
    Args:
        client: A signed-in client.
    """
    body = client.get("/apps/new/options", params={"app_type": "nextjs"}).text

    assert 'name="subdomains"' not in body
    assert 'name="compose_file"' not in body


# ---------------------------------------------------------------------------
# The advanced options travel whole
# ---------------------------------------------------------------------------


def test_a_monorepo_submission_carries_its_options_to_the_job(
    client: TestClient, queued: list[Any]
) -> None:
    """
    Args:
        client: A signed-in client.
        queued: Captured job requests.
    """
    response = client.post(
        "/apps/new",
        data={
            **FORM,
            "app_type": "monorepo",
            "env": "FOO=bar\n# a comment\nBAZ=qux",
            "subdomains": "web:app\napi:api",
            "workspaces": "web, api",
            "no_database": "yes",
        },
    )

    assert response.status_code == 303
    kwargs = queued[0]["kwargs"]
    assert kwargs["env_vars"] == {"FOO": "bar", "BAZ": "qux"}
    assert kwargs["subdomain_overrides"] == {"web": "app", "api": "api"}
    assert kwargs["workspace_filter"] == ["web", "api"]
    assert kwargs["skip_database"] is True
    assert kwargs["compose_file"] is None
    assert kwargs["compose_profiles"] is None


def test_a_compose_submission_carries_its_options_to_the_job(
    client: TestClient, queued: list[Any]
) -> None:
    """
    Args:
        client: A signed-in client.
        queued: Captured job requests.
    """
    response = client.post(
        "/apps/new",
        data={
            **FORM,
            "app_type": "docker-compose",
            "compose_file": "deploy/compose.yml",
            "compose_profiles": "web, worker",
        },
    )

    assert response.status_code == 303
    kwargs = queued[0]["kwargs"]
    assert kwargs["compose_file"] == "deploy/compose.yml"
    assert kwargs["compose_profiles"] == ["web", "worker"]
    assert kwargs["subdomain_overrides"] == {}
    assert kwargs["skip_database"] is False


def test_the_job_hands_the_options_to_the_deployer(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    The last hop: the job function must forward the options to ``configure``
    under exactly the keyword names the deployers read.

    Args:
        monkeypatch: Patching helper, scoped to the test.
    """
    from wasm.web.jobs import Job, JobContext, JobType, deploy_app_job

    captured: dict[str, Any] = {}

    class FakeDeployer:
        """Records how it was configured and deploys nothing."""

        def configure(self, **kwargs: Any) -> None:
            captured.update(kwargs)

        def deploy(self) -> bool:
            return True

    monkeypatch.setattr("wasm.deployers.get_deployer", lambda *a, **k: FakeDeployer())

    job = Job(id="job-test", type=JobType.DEPLOY, name="deploy", description="")
    deploy_app_job(
        "mono.example.com",
        "https://github.com/you/mono",
        "monorepo",
        env_vars={"FOO": "bar"},
        subdomain_overrides={"web": "app"},
        workspace_filter=["web"],
        skip_database=True,
        compose_file="deploy/compose.yml",
        compose_profiles=["web"],
        job_context=JobContext(job, lambda _job: None),
    )

    assert captured["subdomain_overrides"] == {"web": "app"}
    assert captured["workspace_filter"] == ["web"]
    assert captured["skip_database"] is True
    assert captured["compose_file"] == "deploy/compose.yml"
    assert captured["compose_profiles"] == ["web"]
    assert captured["env_vars"] == {"FOO": "bar"}
    assert captured["trigger"] == "panel"


def test_a_malformed_mapping_refuses_the_submission_with_the_form(
    client: TestClient, queued: list[Any]
) -> None:
    """
    A mapping the deployer would never see must stop the submission, with the
    reason on the form and everything still typed in.

    Args:
        client: A signed-in client.
        queued: Captured job requests.
    """
    response = client.post(
        "/apps/new",
        data={**FORM, "app_type": "monorepo", "subdomains": "just-a-word"},
    )

    assert response.status_code == 400
    assert "just-a-word" in response.text, "what was typed should come back"
    assert "https://github.com/you/app" in response.text
    assert not queued, "nothing may be queued from a refused submission"


def test_an_htmx_refusal_answers_200(client: TestClient, store: Any) -> None:
    """
    htmx does not swap an error status: a boosted submission answered 400
    would leave the operator on a form that looks like it did nothing.

    Args:
        client: A signed-in client.
        store: The store, holding the application already.
    """
    deploy(store)

    response = client.post("/apps/new", data=FORM, headers={"HX-Request": "true"})

    assert response.status_code == 200
    assert "already exists" in response.text


# ---------------------------------------------------------------------------
# The webhook section
# ---------------------------------------------------------------------------


def test_enabling_shows_the_secret_exactly_once(client: TestClient, store: Any) -> None:
    """
    The minting response is the only place the secret ever appears. A later
    render of the section may say "enabled" and no more.

    Args:
        client: A signed-in client.
        store: The store, holding the application.
    """
    deploy(store)

    minted = client.post("/apps/app.example.com/webhook/enable")
    assert minted.status_code == 200

    secret = store.get_webhook_secret("app.example.com")
    assert secret, "enabling must store a secret"
    assert secret in minted.text
    assert "/hooks/deploy/app.example.com" in minted.text
    assert "copy it now" in minted.text
    assert "GitHub" in minted.text
    assert "GitLab" in minted.text
    assert "Gitea" in minted.text

    section = client.get("/apps/app.example.com/webhook/section")
    assert section.status_code == 200
    assert secret not in section.text, "the secret must never render again"
    assert "/hooks/deploy/app.example.com" in section.text
    assert "Regenerate" in section.text
    assert "Disable" in section.text


def test_regenerating_rotates_the_secret(client: TestClient, store: Any) -> None:
    """
    Rotation replaces the stored secret in the same motion, so the old one
    stops verifying the moment the new one exists.

    Args:
        client: A signed-in client.
        store: The store, holding the application.
    """
    deploy(store)

    client.post("/apps/app.example.com/webhook/enable")
    first = store.get_webhook_secret("app.example.com")

    rotated = client.post("/apps/app.example.com/webhook/enable")
    second = store.get_webhook_secret("app.example.com")

    assert first and second and first != second
    assert second in rotated.text
    assert first not in rotated.text


def test_disabling_discards_the_secret(client: TestClient, store: Any) -> None:
    """
    Args:
        client: A signed-in client.
        store: The store, holding the application.
    """
    deploy(store)
    client.post("/apps/app.example.com/webhook/enable")

    response = client.post("/apps/app.example.com/webhook/disable")

    assert response.status_code == 200
    assert store.get_webhook_secret("app.example.com") is None
    assert "Enable auto-deploy" in response.text


def test_enabling_for_an_unknown_domain_is_refused_inline(client: TestClient) -> None:
    """
    Args:
        client: A signed-in client.
    """
    response = client.post("/apps/gone.example.com/webhook/enable")

    assert response.status_code == 200, "htmx does not swap an error status"
    assert "not found" in response.text.lower()
    assert "Enable auto-deploy" in response.text, "the section should stay usable"


def test_the_section_lists_only_webhook_deliveries(client: TestClient, store: Any) -> None:
    """
    The recent deliveries table answers "what has the robot done", so a deploy
    an operator ran from the panel or the CLI does not belong in it.

    Args:
        client: A signed-in client.
        store: The store, holding the history.
    """
    deploy(store)
    hook = store.record_deployment_start("app.example.com", "webhook", git_commit="cafe123")
    store.finish_deployment(hook, "success")
    manual = store.record_deployment_start("app.example.com", "cli", git_commit="beef456")
    store.finish_deployment(manual, "success")

    body = client.get("/apps/app.example.com/webhook/section").text

    assert "cafe123" in body
    assert "beef456" not in body


def test_the_webhook_section_demands_a_session(app: FastAPI, store: Any) -> None:
    """
    Args:
        app: The application.
        store: The store, holding the application.
    """
    deploy(store)
    anonymous = TestClient(app, client=("testclient", 50000), follow_redirects=False)

    assert anonymous.get("/apps/app.example.com/webhook/section").status_code == 303
    assert anonymous.post("/apps/app.example.com/webhook/enable").status_code == 303
