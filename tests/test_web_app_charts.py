# Copyright (c) 2024-2026 Yago Lopez Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
Tests for the application page's resource charts fragment.

The fragment is a contract with the same three parties as the dashboard band:
the server renders ``data-chart`` containers naming the collector's per-app
metrics, panel.js resolves those names to a spec by shape, and the deploy
marks travel as a JSON attribute the client paints as vertical lines. What is
defended here is the server's half plus the seam:

- The fragment renders a container per metric family, for the domain asked.
- The marks attribute is valid JSON holding only finished attempts, each with
  a numeric timestamp and a state tone the client's palette knows, oldest
  first - the client filters and paints, it never repairs.
- The application page loads the fragment lazily from an address that serves
  it, and the fragment renders for a domain with history but no application,
  because deployment history outlives the applications it describes.

The client's own half (the shape table, the getAttribute read) is pinned in
``tests/test_web_client_contract.py``.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.panel_factory import seed_panel_state
from wasm.core.config import Config
from wasm.core.store import WASMStore
from wasm.web.auth import SecurityConfig
from wasm.web.server import create_app, get_token_manager

WEB = Path(__file__).resolve().parents[1] / "src" / "wasm" / "web"
PANEL_JS = (WEB / "static" / "panel.js").read_text(encoding="utf-8")

#: The domain the tests deploy against; the factory's first seeded domain.
DOMAIN = "arennalabs.com"


@pytest.fixture
def store(sandbox: Path) -> Iterator[WASMStore]:
    """
    Give the panel a store of its own.

    Args:
        sandbox: Isolated filesystem root.

    Yields:
        The store the fragment reads.
    """
    WASMStore.reset_instance()
    instance = WASMStore(sandbox / "wasm.db")
    try:
        yield instance
    finally:
        instance.close()
        WASMStore.reset_instance()


@pytest.fixture
def config_file(sandbox: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """
    Point the configuration singleton at the sandbox.

    Args:
        sandbox: Isolated filesystem root.
        monkeypatch: Patching helper, scoped to the test.

    Yields:
        The path the panel reads configuration from.
    """
    path = sandbox / "etc" / "wasm" / "config.yaml"
    monkeypatch.setattr("wasm.core.config.DEFAULT_CONFIG_PATH", path)
    Config.reset_instance()
    try:
        yield path
    finally:
        Config.reset_instance()


@pytest.fixture
def app(sandbox: Path, store: WASMStore, config_file: Path, runner: Any) -> FastAPI:
    """
    Build the panel with a populated machine inside the sandbox.

    Args:
        sandbox: Isolated filesystem root.
        store: The store fixture, seeded so the application page exists.
        config_file: The configuration fixture.
        runner: The fake command runner, so no manager reaches a real process.

    Returns:
        The application.
    """
    seed_panel_state(store)
    return create_app(SecurityConfig(state_dir=sandbox / "state", rate_limit_requests=5000))


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    """
    A signed-in client.

    Args:
        app: The application.

    Returns:
        The client.
    """
    signed_in = TestClient(app, client=("testclient", 50000), follow_redirects=False)
    token = get_token_manager().generate_master_token()
    response = signed_in.post("/api/auth/login", json={"token": token})
    assert response.status_code == 200, response.text
    return signed_in


def charts_fragment(client: TestClient, domain: str = DOMAIN) -> str:
    """
    Fetch the fragment the application page loads.

    Args:
        client: A signed-in client.
        domain: The domain asked for.

    Returns:
        The rendered fragment.
    """
    response = client.get(f"/apps/{domain}/charts")
    assert response.status_code == 200, f"the charts fragment answered {response.status_code}"
    return response.text


def marks_of(fragment: str) -> list[list[dict[str, Any]]]:
    """
    Parse every data-chart-marks attribute out of a fragment.

    Args:
        fragment: The rendered fragment.

    Returns:
        One decoded list per container. Raises through ``json.loads`` when an
        attribute does not hold JSON, which is itself the defect.
    """
    found = re.findall(r"data-chart-marks='([^']*)'", fragment)
    assert found, "no container carries a data-chart-marks attribute"
    return [json.loads(raw) for raw in found]


@pytest.mark.parametrize("metric", [f"app.{DOMAIN}.cpu.percent", f"app.{DOMAIN}.mem.bytes"])
def test_the_fragment_renders_a_container_per_metric_family(
    client: TestClient, metric: str
) -> None:
    """
    Args:
        client: A signed-in client.
        metric: A per-application metric the fragment promises a container for.
    """
    assert f'data-chart="{metric}"' in charts_fragment(client), (
        f"the fragment no longer renders a container for {metric}"
    )


def test_every_container_names_a_family_the_client_recognises(client: TestClient) -> None:
    """
    The container's data-chart value is resolved by panel.js against its
    per-application shapes; a family the client does not recognise is an
    empty box forever, without a word.

    Args:
        client: A signed-in client.
    """
    charts = re.findall(r'data-chart="([^"]+)"', charts_fragment(client))

    assert charts, "the fragment renders no chart containers at all"
    shapes = [
        re.compile(pattern) for pattern in re.findall(r"pattern:\s*/(\^app\\\..+?)/", PANEL_JS)
    ]
    assert shapes, "panel.js no longer declares per-application chart shapes"
    for chart in charts:
        assert any(shape.search(chart) for shape in shapes), (
            f"the fragment renders data-chart={chart!r}, which no client shape recognises"
        )


def test_the_marks_hold_finished_attempts_only_oldest_first(
    client: TestClient, store: WASMStore
) -> None:
    """
    A mark is a moment the machine changed. A queued or running attempt has
    not changed anything yet, so it must not be painted; and the client
    appends live points oldest-first, so the marks arrive the same way.

    Args:
        client: A signed-in client.
        store: The seeded store.
    """
    landed = store.record_deployment_start(DOMAIN, "cli")
    store.finish_deployment(landed, "success")
    broke = store.record_deployment_start(DOMAIN, "panel")
    store.finish_deployment(broke, "failed", error="build exploded")
    store.record_deployment_start(DOMAIN, "webhook")  # still running: no mark

    for marks in marks_of(charts_fragment(client)):
        assert [mark["state"] for mark in marks] == ["active", "failed"]
        stamps = [mark["ts"] for mark in marks]
        assert all(isinstance(stamp, (int, float)) for stamp in stamps)
        assert stamps == sorted(stamps), "marks must arrive oldest first"


def test_a_domain_with_no_history_gets_empty_marks_not_an_error(client: TestClient) -> None:
    """
    "Nothing has been deployed" is a normal chart state. The fragment also
    renders for a domain with no application at all, like the history pages,
    because deployment history outlives the application it describes.

    Args:
        client: A signed-in client.
    """
    fragment = charts_fragment(client, "history-only.example.com")

    assert 'data-chart="app.history-only.example.com.cpu.percent"' in fragment
    for marks in marks_of(fragment):
        assert marks == []


def test_the_application_page_loads_the_fragment_lazily(client: TestClient) -> None:
    """
    The page hands htmx the fragment's address; a renamed route on either
    side is a section that never arrives.

    Args:
        client: A signed-in client.
    """
    response = client.get(f"/apps/{DOMAIN}")
    assert response.status_code == 200, response.text

    assert f'hx-get="/apps/{DOMAIN}/charts"' in response.text, (
        "the application page no longer loads the resource charts"
    )
    assert 'id="app-charts-section"' in response.text


def test_the_marks_json_survives_a_hostile_domain(client: TestClient, store: WASMStore) -> None:
    """
    The attribute is single-quoted and tojson-escaped; a domain is data and
    must not be able to break out of it. The domain lands inside the
    data-chart value too, where autoescaping owns it.

    Args:
        client: A signed-in client.
        store: The seeded store.
    """
    hostile = "quo'te.example.com"
    record = store.record_deployment_start(hostile, "cli")
    store.finish_deployment(record, "success")

    fragment = charts_fragment(client, hostile)
    for marks in marks_of(fragment):
        assert len(marks) == 1
