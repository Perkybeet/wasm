# Copyright (c) 2024-2026 Yago Lopez Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
Tests for the infrastructure screens: service and site creation and editing.

The panel talks to the real managers here, pointed at throwaway directories,
and is exercised exactly as a browser would exercise it: form posts,
form-encoded bodies, fragments back. What is asserted is the contract that
matters:

- **Simple mode never defaults to root.** A creation with the user field left
  empty runs as the configured service user, resolved by the manager, which is
  the same resolution the CLI uses.
- **Raw mode writes exactly what was typed.** A unit editor that reformats is
  an editor nobody can trust.
- **A rejected edit changes nothing and answers 200.** The refusal is inline,
  the backend's own words verbatim, and the file on disk stays what it was;
  htmx does not swap an error status, so a 4xx would freeze the screen.
- **A site edit is gated by the web server's own configtest.** nginx's output
  reaches the screen unparaphrased, and the reload button drives the same
  test-then-reload path the CLI uses.
- **The lists advertise the flows.** Rows carry their editors, the pages
  carry their creation links, and the services page sells what the feature
  actually covers: anything systemd can run.
"""

# The web fixtures are imported from test_web_views rather than replicated, so
# there stays one definition of "a signed-in panel client". Ruff reads a test
# parameter named after an imported fixture as a redefinition; here it is the
# mechanism.
# ruff: noqa: F811

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any

import pytest

from tests.test_web_views import (  # noqa: F401  (pytest resolves fixtures by name)
    MISSING_MARKER,
    anonymous,
    app,
    body_of,
    client,
    config_file,
    deploy,
    store,
)
from wasm.managers.service_manager import WASM_UNIT_MARKER, ServiceManager
from wasm.web.api import services as services_api
from wasm.web.api import sites as sites_api

#: A unit body an operator could paste into raw mode, marker included. It must
#: land on disk byte for byte.
RAW_UNIT = (
    f"# {WASM_UNIT_MARKER}\n"
    "[Unit]\n"
    "Description=Raw queue worker\n"
    "\n"
    "[Service]\n"
    "ExecStart=/usr/bin/true\n"
)

#: What nginx says about a broken snippet. It must reach the screen verbatim.
NGINX_REFUSAL = (
    "nginx: [emerg] unknown directive server_nam in /tmp/wasm-validate/panel.example.com:3"
)


@pytest.fixture
def unit_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """
    Point both the services API and the manager at a throwaway unit directory.

    Args:
        tmp_path: Per-test temporary directory.
        monkeypatch: Patching helper, scoped to the test.

    Returns:
        The directory unit files are written into.
    """
    directory = tmp_path / "etc" / "systemd" / "system"
    directory.mkdir(parents=True)
    monkeypatch.setattr(services_api, "SYSTEMD_UNIT_DIR", directory, raising=False)
    monkeypatch.setattr(ServiceManager, "SYSTEMD_DIR", directory, raising=False)
    return directory


@pytest.fixture
def site_dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """
    Point the sites API at nginx managers bound to a throwaway tree.

    The managers are real - rendering, validation and the store record all run
    - only their configuration directories move into the sandbox. The store is
    not faked: a site created here must appear in the same store the sites
    page reads, because that is the assertion that matters.

    Args:
        tmp_path: Per-test temporary directory.
        monkeypatch: Patching helper, scoped to the test.

    Returns:
        The sites-available and sites-enabled directories.
    """
    from wasm.managers.nginx_manager import NginxManager
    from wasm.managers.webserver import NGINX_BACKEND

    available = tmp_path / "etc" / "nginx" / "sites-available"
    enabled = tmp_path / "etc" / "nginx" / "sites-enabled"
    available.mkdir(parents=True)
    enabled.mkdir(parents=True)
    backend = dataclasses.replace(NGINX_BACKEND, sites_available=available, sites_enabled=enabled)

    def make(verbose: bool = False, **kwargs: Any) -> NginxManager:
        """
        Build a manager pinned to the sandbox tree.

        Args:
            verbose: Ignored, kept for signature compatibility.
            **kwargs: Ignored, kept for signature compatibility.

        Returns:
            The manager.
        """
        return NginxManager(verbose=verbose, backend=backend)

    monkeypatch.setattr(sites_api, "MANAGERS", {"nginx": make})
    monkeypatch.setattr(sites_api, "detect_webserver", lambda: "nginx")
    return available, enabled


def make_site(client: Any, domain: str = "panel.example.com") -> None:
    """
    Create a site through the panel, as the tests' common starting point.

    Args:
        client: A signed-in client.
        domain: Domain to create.
    """
    response = client.post(
        "/sites/new",
        data={"domain": domain, "type": "proxy", "port": "3100", "webserver": "nginx"},
    )
    assert response.status_code == 200, response.text
    assert response.headers["HX-Redirect"] == f"/sites/{domain}/config"


def seeded_unit(unit_dir: Path, name: str = "infra-editor") -> Path:
    """
    Put a WASM-owned unit on disk for the editor tests.

    Args:
        unit_dir: The sandboxed unit directory.
        name: Unit name without the suffix.

    Returns:
        The unit file path.
    """
    path = unit_dir / f"{name}.service"
    path.write_text(RAW_UNIT)
    return path


# ------------------------------------------------------------------ the lists


def test_the_services_page_sells_the_range_and_offers_creation(client) -> None:
    """The empty state names what the feature covers, with the link to start."""
    page = body_of(client, "/services")

    assert "Run anything systemd can run" in page
    assert "Run anything systemd can run: daemons, workers, schedulers" in page
    assert 'href="/services/new"' in page
    assert MISSING_MARKER not in page


def test_the_sites_page_offers_creation(client) -> None:
    """The sites list carries its creation link, empty or not."""
    page = body_of(client, "/sites")

    assert 'href="/sites/new"' in page
    assert MISSING_MARKER not in page


def test_rows_link_to_their_editors(client, store) -> None:
    """Every service row reaches its unit, every site row its config."""
    deploy(store)

    services_page = body_of(client, "/services")
    assert "Edit unit" in services_page
    assert 'href="/services/wasm-example-com/config"' in services_page
    # With rows on the page, the creation link moves to the toolbar and stays.
    assert 'href="/services/new"' in services_page

    sites_page = body_of(client, "/sites")
    assert "Edit config" in sites_page
    assert 'href="/sites/example.com/config"' in sites_page
    assert 'href="/sites/new"' in sites_page


def test_the_creation_forms_render_both_modes(client) -> None:
    """The tabs are real routes, and raw mode prefills a body that would pass."""
    page = body_of(client, "/services/new")
    assert 'name="command"' in page, "the simple form is not the default"
    assert 'hx-get="/services/new/form?mode=raw"' in page
    assert MISSING_MARKER not in page

    raw = client.get("/services/new/form", params={"mode": "raw"})
    assert raw.status_code == 200
    assert 'name="unit"' in raw.text
    assert WASM_UNIT_MARKER in raw.text, "the skeleton would be refused without the marker"
    assert MISSING_MARKER not in raw.text

    site_page = body_of(client, "/sites/new")
    assert 'name="domain"' in site_page
    assert MISSING_MARKER not in site_page


def test_the_adapters_demand_a_session(anonymous) -> None:
    """Creation and editing are not holes in the fence."""
    assert anonymous.get("/services/new").status_code == 303

    response = anonymous.post("/sites/new", data={"domain": "x.example.com"})
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


# ------------------------------------------------------------ creating services


def test_creating_a_simple_service_defaults_to_the_configured_user(client, unit_dir) -> None:
    """An empty user field means the service user the CLI would use, never root."""
    response = client.post(
        "/services/new",
        data={
            "mode": "simple",
            "name": "infra-worker",
            "command": "/usr/bin/python3 -m worker",
            "directory": "/var/www",
            "user": "",
            "port": "9000",
        },
    )

    assert response.status_code == 200, response.text
    assert response.headers["HX-Redirect"] == "/services/infra-worker/config"

    unit = (unit_dir / "infra-worker.service").read_text()
    assert "User=www-data" in unit, "the unit does not run as the configured service user"
    assert "User=root" not in unit
    assert "ExecStart=/usr/bin/python3 -m worker" in unit
    assert 'Environment="PORT=9000"' in unit, "the port did not travel as PORT"
    assert WASM_UNIT_MARKER in unit

    page = body_of(client, "/services")
    assert "infra-worker" in page, "the new service is missing from the list"


def test_creating_a_raw_service_writes_the_text_verbatim(client, unit_dir) -> None:
    """Raw mode is a promise: the file on disk is exactly what was typed."""
    response = client.post(
        "/services/new", data={"mode": "raw", "name": "infra-raw", "unit": RAW_UNIT}
    )

    assert response.status_code == 200, response.text
    assert response.headers["HX-Redirect"] == "/services/infra-raw/config"
    assert (unit_dir / "infra-raw.service").read_text() == RAW_UNIT

    editor = body_of(client, "/services/infra-raw/config")
    assert "ExecStart=/usr/bin/true" in editor, "the editor does not show what was written"


def test_a_raw_unit_without_the_marker_is_refused_inline(client, unit_dir) -> None:
    """The manager's rule surfaces on the form, and nothing lands on disk."""
    body = "[Service]\nExecStart=/usr/bin/true\n"
    response = client.post(
        "/services/new", data={"mode": "raw", "name": "infra-naked", "unit": body}
    )

    assert response.status_code == 200, response.text
    assert "Refusing to write a unit without the WASM marker" in response.text
    assert WASM_UNIT_MARKER in response.text, "the fix does not name the marker to keep"
    assert not (unit_dir / "infra-naked.service").exists()
    # The refusal keeps what was typed, so the operator corrects it in place.
    assert "ExecStart=/usr/bin/true" in response.text


# ------------------------------------------------------------- the unit editor


def test_the_unit_editor_shows_the_file(client, unit_dir) -> None:
    """The textarea holds the unit body and the page names the real path."""
    path = seeded_unit(unit_dir)

    page = body_of(client, "/services/infra-editor/config")

    assert "<textarea" in page
    assert "ExecStart=/usr/bin/true" in page
    assert str(path) in page
    assert MISSING_MARKER not in page


def test_a_rejected_unit_body_answers_200_verbatim_and_writes_nothing(client, unit_dir) -> None:
    """The refusal is inline and exact, and the file is byte-for-byte intact."""
    path = seeded_unit(unit_dir)
    original = path.read_text()
    broken = "[Service]\nExecStart=/usr/bin/false\n"

    response = client.post("/services/infra-editor/config", data={"config": broken})

    assert response.status_code == 200, response.text
    assert "Refusing to write a unit without the WASM marker" in response.text
    assert WASM_UNIT_MARKER in response.text
    assert path.read_text() == original, "a refused edit must not touch the file"
    assert "ExecStart=/usr/bin/false" in response.text, "the typed body was thrown away"


def test_saving_a_unit_writes_it_and_says_restart(client, unit_dir) -> None:
    """An accepted body replaces the file and the notice says what comes next."""
    path = seeded_unit(unit_dir)
    updated = RAW_UNIT.replace("/usr/bin/true", "/usr/bin/env worker")

    response = client.post("/services/infra-editor/config", data={"config": updated})

    assert response.status_code == 200, response.text
    assert path.read_text() == updated
    assert "Restart the service to apply changes" in response.text
    assert 'hx-post="/api/services/infra-editor/restart"' in response.text


def test_an_unknown_service_names_itself_at_404(client, unit_dir) -> None:
    """A missing unit is reported with its name and the command that lists them."""
    response = client.get("/services/infra-ghost/config")

    assert response.status_code == 404
    assert "infra-ghost" in response.text
    assert "wasm service list" in response.text


# --------------------------------------------------------------- creating sites


def test_creating_a_site_appears_in_the_list(client, store, site_dirs) -> None:
    """The store, the disk and the page all agree a created site exists."""
    available, enabled = site_dirs
    make_site(client)

    config = available / "panel.example.com"
    assert config.is_file(), "the manager did not write the configuration"
    content = config.read_text()
    assert "server_name panel.example.com" in content
    assert "proxy_pass http://127.0.0.1:3100" in content
    assert (enabled / "panel.example.com").is_symlink(), "the site was not enabled"

    page = body_of(client, "/sites")
    assert "panel.example.com" in page, "the created site is missing from the list"
    assert 'href="/sites/panel.example.com/config"' in page


def test_creating_a_duplicate_site_is_refused_inline(client, store, site_dirs) -> None:
    """The API's 409 comes back as a problem block with the form preserved."""
    make_site(client)

    response = client.post(
        "/sites/new",
        data={"domain": "panel.example.com", "type": "proxy", "port": "3100"},
    )

    assert response.status_code == 200, response.text
    assert "already exists" in response.text
    assert 'value="panel.example.com"' in response.text, "the typed domain was thrown away"


# ------------------------------------------------------------- the site editor


def test_the_site_editor_shows_the_config_and_the_reload_button(client, store, site_dirs) -> None:
    """The editor is the config verbatim, with the reload one click away."""
    make_site(client)

    page = body_of(client, "/sites/panel.example.com/config")

    assert "<textarea" in page
    assert "server_name panel.example.com" in page
    assert 'hx-post="/sites/panel.example.com/reload"' in page
    assert MISSING_MARKER not in page


def test_a_config_nginx_rejects_is_refused_verbatim_and_not_written(
    client, store, site_dirs, runner
) -> None:
    """nginx's own words reach the screen in mono and the file stays intact."""
    available, _ = site_dirs
    make_site(client)
    original = (available / "panel.example.com").read_text()
    runner.script(["nginx", "-t", "-c"], stderr=NGINX_REFUSAL, exit_code=1)

    response = client.post("/sites/panel.example.com/config", data={"config": "server { broken }"})

    assert response.status_code == 200, response.text
    assert "unknown directive server_nam" in response.text, "nginx's output was paraphrased away"
    assert 'class="problem__output"' in response.text, "the output is not in the verbatim block"
    assert "rejected the configuration" in response.text
    assert (available / "panel.example.com").read_text() == original, (
        "a rejected configuration must never land on disk"
    )
    assert "server { broken }" in response.text, "the typed configuration was thrown away"


def test_a_config_nginx_accepts_is_saved_and_invites_the_reload(client, store, site_dirs) -> None:
    """An accepted save replaces the file and points at the reload button."""
    available, _ = site_dirs
    make_site(client)
    updated = "# rewritten from the panel\nserver {\n    server_name panel.example.com;\n}"

    response = client.post("/sites/panel.example.com/config", data={"config": updated})

    assert response.status_code == 200, response.text
    assert "Configuration updated" in response.text
    assert "Reload nginx" in response.text
    assert 'hx-post="/sites/panel.example.com/reload"' in response.text
    assert (available / "panel.example.com").read_text() == updated


def test_the_reload_button_tests_then_reloads(client, store, site_dirs, runner) -> None:
    """The reload runs the config test first, then reloads the unit."""
    make_site(client)

    response = client.post("/sites/panel.example.com/reload")

    assert response.status_code == 200, response.text
    assert ("nginx", "-t") in runner.calls, "the reload skipped the configuration test"
    assert ("systemctl", "reload", "nginx") in runner.calls
    assert "nginx reloaded" in response.text


def test_a_failed_config_test_blocks_the_reload_inline(client, store, site_dirs, runner) -> None:
    """A broken live configuration refuses the reload and keeps what runs."""
    make_site(client)
    runner.script(["nginx", "-t"], stderr="nginx: configuration file test failed", exit_code=1)

    response = client.post("/sites/panel.example.com/reload")

    assert response.status_code == 200, response.text
    assert "the running config was kept" in response.text
    assert ("systemctl", "reload", "nginx") not in runner.calls, (
        "a failed test must never be followed by a reload"
    )


def test_an_unknown_site_names_itself_at_404(client, store, site_dirs) -> None:
    """A missing site is reported with its domain and the command that lists them."""
    response = client.get("/sites/ghost.example.com/config")

    assert response.status_code == 404
    assert "ghost.example.com" in response.text
    assert "wasm site list" in response.text
