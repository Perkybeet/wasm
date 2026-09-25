# Copyright (c) 2024-2026 Yago Lopez Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
Every response model that carries a timestamp applies the same conversion.

:func:`wasm.web.pydantic_compat.iso_offset_validator` is exercised once,
generically, in ``tests/test_pydantic_compat.py``. What is worth pinning
here, model by model, is that each response class actually wired it up to
the right fields - a typo'd field name or a forgotten class attribute would
otherwise ship silently, since a model with no validator at all is still a
valid model.

Endpoint-level coverage for the same mechanism lives with each endpoint's own
tests (``test_web_apps_api.py``, ``test_web_deployments_api.py``); this file
is for the response classes that are otherwise only reachable through a lot
of fixture setup to build one HTTP response.
"""

from __future__ import annotations

from datetime import datetime

import pytest

NAIVE = datetime(2026, 6, 15, 9, 30, 0)


def _has_offset(value: str | None) -> bool:
    """
    Args:
        value: A timestamp field's value after model construction.

    Returns:
        Whether it parses as an aware datetime with the naive wall clock
        preserved.
    """
    assert value is not None
    parsed = datetime.fromisoformat(value)
    return parsed.tzinfo is not None and parsed.replace(tzinfo=None) == NAIVE


@pytest.mark.parametrize("field", ["created_at", "activated_at"])
def test_release_out(field: str) -> None:
    from wasm.web.api.apps import ReleaseOut

    values = {"created_at": NAIVE.isoformat(), "activated_at": NAIVE.isoformat()}
    model = ReleaseOut(
        id="20260615-093000-abc1234",
        commit="abc1234",
        status="active",
        active=True,
        on_disk=True,
        **values,
    )

    assert _has_offset(getattr(model, field))


def test_rollback_point_out() -> None:
    from wasm.web.api.apps import RollbackPointOut

    model = RollbackPointOut(
        id="app_20260615_093000",
        created_at=NAIVE.isoformat(),
        description="pre-deploy",
        size_bytes=1024,
    )

    assert _has_offset(model.created_at)


def test_last_deployment_out() -> None:
    from wasm.web.api.apps import LastDeploymentOut

    model = LastDeploymentOut(id=1, status="success", finished_at=NAIVE.isoformat())

    assert _has_offset(model.finished_at)


def test_last_deployment_out_stays_none_while_running() -> None:
    from wasm.web.api.apps import LastDeploymentOut

    model = LastDeploymentOut(id=1, status="running", finished_at=None)

    assert model.finished_at is None


@pytest.mark.parametrize("field", ["created_at", "started_at", "completed_at"])
def test_job_response(field: str) -> None:
    from wasm.web.api.jobs import JobResponse

    values = {
        "created_at": NAIVE.isoformat(),
        "started_at": NAIVE.isoformat(),
        "completed_at": NAIVE.isoformat(),
    }
    model = JobResponse(
        id="abc123",
        type="deploy",
        name="Deploy shop.example.com",
        description="",
        status="completed",
        progress=100,
        total_steps=100,
        current_step="",
        **values,
    )

    assert _has_offset(getattr(model, field))


def test_app_domain() -> None:
    from wasm.web.api.domains import AppDomain

    model = AppDomain(domain="shop.example.com", kind="primary", created_at=NAIVE.isoformat())

    assert _has_offset(model.created_at)


def test_backup_info() -> None:
    from wasm.web.api.backups import BackupInfo

    model = BackupInfo(
        backup_id="shop-example-com_20260615_093000",
        domain="shop.example.com",
        timestamp=NAIVE.isoformat(),
        size=2048,
        size_human="2.0 KB",
        age="1 hour ago",
    )

    assert _has_offset(model.timestamp)


def test_database_backup_info_response() -> None:
    from wasm.web.api.databases import BackupInfoResponse

    model = BackupInfoResponse(
        path="/var/backups/wasm/db/shop.sql.gz",
        database="shop",
        engine="postgres",
        size=4096,
        size_human="4.0 KB",
        created=NAIVE.isoformat(),
        compressed=True,
    )

    assert _has_offset(model.created)


def test_webhook_delivery_out() -> None:
    from wasm.web.api.hooks import WebhookDeliveryOut

    model = WebhookDeliveryOut(deployment_id=1, status="success", started_at=NAIVE.isoformat())

    assert _has_offset(model.started_at)


def test_deployment_out() -> None:
    from wasm.web.api.deployments import DeploymentOut

    model = DeploymentOut(
        id=1,
        domain="shop.example.com",
        status="success",
        triggered_by="cli",
        started_at=NAIVE.isoformat(),
        finished_at=NAIVE.isoformat(),
        has_log=False,
    )

    assert _has_offset(model.started_at)
    assert _has_offset(model.finished_at)
