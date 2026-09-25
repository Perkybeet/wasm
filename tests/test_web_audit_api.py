"""
Tests for GET /api/audit: the append-only audit log, exposed to admins.

The log itself is :class:`wasm.web.auth.AuditLogger`, JSON lines written by
every privileged action the panel performs (see tests/test_web_auth.py for
what gets audited). This module tests the read side: an admin-only endpoint
that returns it newest first, filterable and keyset-paginated.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from wasm.web.auth import CSRF_HEADER_NAME, SecurityConfig, get_audit_logger
from wasm.web.server import create_app, get_token_manager


def make_config(sandbox: Path, **overrides) -> SecurityConfig:
    """
    Args:
        sandbox: Per-test temporary directory.
        **overrides: Fields to override on the configuration.

    Returns:
        A security configuration whose state lives inside the sandbox.
    """
    params: dict[str, object] = {
        "state_dir": sandbox / "state",
        "rate_limit_requests": 5000,
    }
    params.update(overrides)
    return SecurityConfig(**params)


def build_client(sandbox: Path) -> TestClient:
    """
    Args:
        sandbox: Per-test temporary directory.

    Returns:
        A test client bound to a freshly created application.
    """
    app = create_app(make_config(sandbox))
    return TestClient(app, client=("testclient", 50000))


def login_admin(client: TestClient) -> str:
    """
    Log in with a freshly generated master token, which carries admin scope.

    Args:
        client: The client to sign in.

    Returns:
        The CSRF token for the new session, already installed on the client.
    """
    token = get_token_manager().generate_master_token()
    response = client.post("/api/auth/login", json={"token": token})
    assert response.status_code == 200, response.text
    csrf = response.json()["csrf_token"]
    client.headers[CSRF_HEADER_NAME] = csrf
    return csrf


def test_unauthenticated_is_refused(sandbox: Path) -> None:
    """Anonymous access must not reach the audit log."""
    client = build_client(sandbox)
    response = client.get("/api/audit")
    assert response.status_code == 401


def test_a_read_scoped_token_is_refused(sandbox: Path) -> None:
    """Audit is sensitive: even a valid, lesser-scoped credential is refused."""
    client = build_client(sandbox)
    login_admin(client)
    reader = get_token_manager().create_api_token("reader", "read")["token"]

    response = client.get("/api/audit", headers={"Authorization": f"Bearer {reader}"})
    assert response.status_code == 403


def test_an_admin_token_is_accepted(sandbox: Path) -> None:
    """An admin-scoped Bearer credential, not only a cookie session, may read it."""
    client = build_client(sandbox)
    login_admin(client)
    admin_token = get_token_manager().create_api_token("automation", "admin")["token"]

    response = client.get("/api/audit", headers={"Authorization": f"Bearer {admin_token}"})
    assert response.status_code == 200


def test_lists_entries_newest_first_with_every_field(sandbox: Path) -> None:
    """The model carries the fields the console's activity view needs."""
    client = build_client(sandbox)
    login_admin(client)
    audit = get_audit_logger()
    assert audit is not None
    for index in range(3):
        audit.record(
            action="test.action",
            result="success",
            client_ip="10.0.0.1",
            actor=f"actor{index}",
            resource="/x",
            detail=f"n{index}",
        )

    response = client.get("/api/audit?limit=50")
    assert response.status_code == 200
    items = response.json()["items"]

    ours = [item for item in items if item["action"] == "test.action"]
    assert [item["actor"] for item in ours] == ["actor2", "actor1", "actor0"]
    assert {"timestamp", "action", "result", "actor", "client_ip", "resource", "detail"} <= set(
        ours[0]
    )
    assert ours[0]["detail"] == "n2"
    assert ours[0]["client_ip"] == "10.0.0.1"


def test_filters_by_action_result_and_actor(sandbox: Path) -> None:
    """Each filter narrows the listing independently."""
    client = build_client(sandbox)
    login_admin(client)
    audit = get_audit_logger()
    assert audit is not None
    audit.record(
        action="apps.delete", result="success", client_ip="10.0.0.1", actor="sid1", resource="/a"
    )
    audit.record(
        action="apps.delete", result="denied", client_ip="10.0.0.1", actor="sid2", resource="/b"
    )
    audit.record(
        action="auth.login", result="success", client_ip="10.0.0.1", actor="sid1", resource="/c"
    )

    by_action = client.get("/api/audit?action=apps.delete").json()["items"]
    assert [item["result"] for item in by_action] == ["denied", "success"]

    by_result = client.get("/api/audit?result=denied").json()["items"]
    assert len(by_result) == 1
    assert by_result[0]["actor"] == "sid2"

    by_actor = client.get("/api/audit?actor=sid1").json()["items"]
    assert {item["action"] for item in by_actor} == {"apps.delete", "auth.login"}


def test_keyset_pagination_walks_every_entry_exactly_once(sandbox: Path) -> None:
    """``before`` from one page is the cursor for the next, until it ends."""
    client = build_client(sandbox)
    login_admin(client)
    audit = get_audit_logger()
    assert audit is not None
    for index in range(5):
        audit.record(
            action="paged", result="success", client_ip="10.0.0.1", actor=str(index), resource="/x"
        )

    first = client.get("/api/audit?limit=2&action=paged").json()
    assert [item["actor"] for item in first["items"]] == ["4", "3"]
    assert first["next_before"] is not None

    second = client.get(f"/api/audit?limit=2&action=paged&before={first['next_before']}").json()
    assert [item["actor"] for item in second["items"]] == ["2", "1"]
    assert second["next_before"] is not None

    third = client.get(f"/api/audit?limit=2&action=paged&before={second['next_before']}").json()
    assert [item["actor"] for item in third["items"]] == ["0"]
    assert third["next_before"] is None
