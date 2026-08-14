# Copyright (c) 2024-2026 Yago Lopez Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
Tests for the metrics history API.

The charts load their past from here and their present from the ``/events``
stream, so this surface is deliberately small: a list of metric names and a
window of points per metric. What is defended:

- **The endpoints demand a session.** Metric names alone reveal every
  application on the machine.
- **The window vocabulary is closed.** The store's retention tiers are fixed;
  a window it cannot honour is refused with a validation error, not answered
  with misleadingly sparse data.
- **No data is an empty chart, not a 404.** Every metric starts with no rows.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from wasm.monitor.timeseries import MetricsStore
from wasm.web import metrics_collector
from wasm.web.api.auth import get_current_session
from wasm.web.api.metrics import WINDOWS
from wasm.web.api.metrics import router as metrics_router

#: A fixed "now" the store's injected clock reports.
NOW = 1_700_002_800


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> MetricsStore:
    """
    Put the process-wide metrics store on a throwaway database.

    Args:
        tmp_path: Per-test temporary directory.
        monkeypatch: Patching helper, scoped to the test.

    Returns:
        The store the API under test will read.
    """
    instance = MetricsStore(tmp_path / "metrics.db", clock=lambda: NOW)
    monkeypatch.setattr(metrics_collector, "get_metrics_store", lambda: instance)
    return instance


@pytest.fixture
def client(store: MetricsStore) -> TestClient:
    """
    Build a client for the metrics router with authentication stubbed.

    Args:
        store: The seeded store fixture.

    Returns:
        A client whose requests are already authenticated.
    """
    app = FastAPI()
    app.include_router(metrics_router, prefix="/api/metrics")
    app.dependency_overrides[get_current_session] = lambda: {"session_id": "test"}
    return TestClient(app)


def test_the_metric_list_names_what_has_data(client: TestClient, store: MetricsStore) -> None:
    """GET /api/metrics answers with the names the collector has recorded."""
    store.record("cpu.percent", 12.5)
    store.record("app.example.com.mem.bytes", 4096.0)

    response = client.get("/api/metrics")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["metrics"] == ["app.example.com.mem.bytes", "cpu.percent"]
    assert set(body["windows"]) == set(WINDOWS)


def test_a_metric_window_comes_back_as_timestamped_points(
    client: TestClient, store: MetricsStore
) -> None:
    """The shape the charts consume: [ts, value] pairs, oldest first."""
    store.record("cpu.percent", 10.0, ts=NOW - 20)
    store.record("cpu.percent", 30.0, ts=NOW - 10)

    response = client.get("/api/metrics/cpu.percent?window=1h")

    assert response.status_code == 200, response.text
    assert response.json() == {
        "metric": "cpu.percent",
        "window": "1h",
        "points": [[NOW - 20, 10.0], [NOW - 10, 30.0]],
    }


def test_the_window_defaults_to_an_hour(client: TestClient, store: MetricsStore) -> None:
    """A client that says nothing gets the raw tier."""
    store.record("cpu.percent", 10.0, ts=NOW - 10)

    response = client.get("/api/metrics/cpu.percent")

    assert response.status_code == 200, response.text
    assert response.json()["window"] == "1h"


@pytest.mark.parametrize("window", ["7d", "2h", "60", "", "1h; DROP TABLE samples"])
def test_a_window_outside_the_vocabulary_is_refused(client: TestClient, window: str) -> None:
    """
    The retention tiers are fixed, so the windows are too.

    Args:
        window: A window name the store cannot honour.
    """
    response = client.get("/api/metrics/cpu.percent", params={"window": window})

    assert response.status_code == 422, response.text


def test_a_metric_with_no_data_is_an_empty_chart_not_a_404(client: TestClient) -> None:
    """Every metric starts empty; that is a normal state, not a missing resource."""
    response = client.get("/api/metrics/app.example.com.cpu.percent?window=24h")

    assert response.status_code == 200, response.text
    assert response.json()["points"] == []


def test_every_window_maps_onto_a_retention_tier() -> None:
    """The vocabulary and the store's tiers must not drift apart."""
    from wasm.monitor.timeseries import (
        HOUR_RETENTION_SECONDS,
        MINUTE_RETENTION_SECONDS,
        RAW_RETENTION_SECONDS,
    )

    assert WINDOWS["1h"] == RAW_RETENTION_SECONDS
    assert WINDOWS["24h"] == MINUTE_RETENTION_SECONDS
    assert WINDOWS["30d"] == HOUR_RETENTION_SECONDS


def test_the_endpoints_demand_a_session(tmp_path: Path) -> None:
    """Metric names alone reveal every application on the machine."""
    from wasm.web.auth import SecurityConfig
    from wasm.web.server import create_app

    app = create_app(SecurityConfig(state_dir=tmp_path / "state"))
    anonymous = TestClient(app, client=("testclient", 50000))

    assert anonymous.get("/api/metrics").status_code == 401
    assert anonymous.get("/api/metrics/cpu.percent").status_code == 401
