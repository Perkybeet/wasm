# Copyright (c) 2024-2026 Yago Lopez Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
Tests for the dashboard's chart band.

The band is a contract with three parties and no framework holding them
together: the template renders ``data-chart`` containers, panel.js maps each
container to a spec and to the metrics the collector records, and the window
buttons name windows the history API honours. A container the script has no
spec for is an empty box forever; a window the API refuses is a button that
does nothing; and uPlot creeping back into the shell as a global load is the
regression Task C removed. Each of those is pinned here.

Page-level sweeps (addresses resolve, no unresolved variables, escaping) live
in ``tests/test_web_views.py`` and already cover the dashboard; this file only
adds what the chart band introduced.
"""

from __future__ import annotations

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
from wasm.web.api.metrics import WINDOWS
from wasm.web.auth import SecurityConfig
from wasm.web.server import create_app, get_token_manager

WEB = Path(__file__).resolve().parents[1] / "src" / "wasm" / "web"
BASE = (WEB / "templates" / "base.html").read_text(encoding="utf-8")
PANEL_JS = (WEB / "static" / "panel.js").read_text(encoding="utf-8")

#: Every chart the band promises, by the spec key its container names. The
#: first two are collector metrics drawn as they are; the last two are
#: composites panel.js derives, which is exactly why the names are pinned
#: rather than discovered.
EXPECTED_CHARTS = ("cpu.percent", "mem.used_bytes", "net.bytes_s", "disk.percent")


@pytest.fixture
def store(sandbox: Path) -> Iterator[WASMStore]:
    """
    Give the panel a store of its own.

    Args:
        sandbox: Isolated filesystem root.

    Yields:
        The store the dashboard reads.
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
        store: The store fixture, seeded so the dashboard has rows around the
            chart band rather than an empty screen.
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


def dashboard(client: TestClient) -> str:
    """
    Fetch the dashboard.

    Args:
        client: A signed-in client.

    Returns:
        The rendered page.
    """
    response = client.get("/")
    assert response.status_code == 200, f"the dashboard answered {response.status_code}"
    return response.text


@pytest.mark.parametrize("chart", EXPECTED_CHARTS)
def test_the_dashboard_renders_a_container_for_every_chart(client: TestClient, chart: str) -> None:
    """
    Args:
        client: A signed-in client.
        chart: A spec key the band promises a container for.
    """
    assert f'data-chart="{chart}"' in dashboard(client), (
        f"the dashboard no longer renders a container for the {chart} chart"
    )


def test_every_chart_container_names_a_spec_the_client_defines(client: TestClient) -> None:
    """
    The container's data-chart value is the key panel.js looks up in its spec
    table. A container with no spec is skipped without a word, so a renamed
    key on either side is a chart that silently never draws.

    Args:
        client: A signed-in client.
    """
    charts = re.findall(r'data-chart="([^"]+)"', dashboard(client))

    assert charts, "the dashboard renders no chart containers at all"
    for chart in charts:
        assert f'"{chart}": {{' in PANEL_JS, (
            f"the dashboard renders data-chart={chart!r}, which panel.js has no spec for"
        )


def test_the_window_selector_offers_windows_the_history_api_honours(
    client: TestClient,
) -> None:
    """
    The buttons carry the window straight into ``/api/metrics/...?window=``,
    whose vocabulary is fixed; a button outside it fetches a 422 and the band
    quietly never changes.

    Args:
        client: A signed-in client.
    """
    page = dashboard(client)
    offered = re.findall(r'data-chart-window="([^"]+)"', page)

    assert offered, "the dashboard renders no window selector"
    for window in offered:
        assert window in WINDOWS, f"the selector offers {window!r}, which the API refuses"


def test_exactly_one_window_is_pressed_when_the_page_arrives(client: TestClient) -> None:
    """
    The pressed state is the client's record of which window is loaded, and
    the script trusts the markup for its starting value: none pressed reads
    as no selection, two pressed is a lie about one of them.

    Args:
        client: A signed-in client.
    """
    page = dashboard(client)
    buttons = re.findall(r'data-chart-window="[^"]+"\s+aria-pressed="([^"]+)"', page)

    assert buttons, "the window buttons no longer carry aria-pressed"
    assert buttons.count("true") == 1, f"expected exactly one pressed window, got {buttons}"


def test_the_shell_no_longer_loads_uplot_globally() -> None:
    """
    uPlot is 50 KB of script and style that only the pages with charts need;
    panel.js loads both lazily when a ``[data-chart]`` container is on
    screen. A reference from base.html would put it back on every page,
    including sign-in.
    """
    # Jinja comments explain why uPlot is absent and name it doing so; only
    # real markup counts, the same way the CSP sweep reads templates.
    markup = re.sub(r"\{#.*?#\}", "", BASE, flags=re.DOTALL)
    assert "uplot" not in markup.lower(), "base.html references uPlot again; it must load lazily"
    assert "/static/vendor/uplot.min.js" in PANEL_JS, "panel.js no longer loads uPlot at all"
    assert "/static/vendor/uplot.min.css" in PANEL_JS, "panel.js no longer injects uPlot's styles"
