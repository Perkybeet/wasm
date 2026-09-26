# Copyright (c) 2024-2026 Yago Lopez Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
Tests for the machine snapshot: one implementation, JSON in and out.

:func:`wasm.web.machine.read_machine` is now the only place that samples
psutil and systemd for the machine's headline numbers. The console's topbar,
the ``machine`` SSE event and ``GET /api/system/machine`` all read the exact
same snapshot, instead of the SSE side rendering its own HTML fragment from a
second implementation the way it used to.

What is defended:

- **The snapshot has the console's shape** and survives ``dataclasses.asdict``
  plus ``json.dumps`` unchanged, because that is exactly how it reaches the
  wire in :func:`wasm.web.events.machine_frame`.
- **Unit counts come from ServiceManager, not a probe per unit.** One
  ``systemctl list-units`` call, scripted on a ``FakeRunner``, is all a test -
  and the five-second SSE timer - can afford.
- **Application counts are read off that same unit list**, matched by name,
  rather than costing a second round trip per application the way
  :func:`wasm.core.app_state.resolve_states` would.
- **A systemd or store failure degrades the snapshot to zero, not a crash.**
  The strip refreshes on a timer; one bad tick must not take the stream
  carrying it down.
- **The REST endpoint answers with exactly this shape**, authenticated the
  same way every other system endpoint is.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import asdict
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from wasm.core.config import Config
from wasm.core.exceptions import ServiceError, WASMError
from wasm.core.runner import FakeRunner
from wasm.core.store import App, Service, WASMStore
from wasm.core.utils import domain_to_app_name, legacy_app_name
from wasm.managers.service_manager import ServiceManager
from wasm.web.api.auth import get_current_session
from wasm.web.api.system import router as system_router
from wasm.web.machine import (
    AppTally,
    DiskSnapshot,
    MachineState,
    MemorySnapshot,
    UnitTally,
    _resolve_apps_root,
    classify_unit,
    fetch_service_states,
    read_machine,
)

#: The one systemctl call the machine snapshot's unit tally is allowed to
#: make. Scripting only this prefix, with no patterns after it, proves the
#: refresh never launches a process per unit or per application.
LIST_UNITS = ["systemctl", "list-units", "--type=service", "--all", "--no-pager", "--plain"]


@pytest.fixture
def store(tmp_path: Path) -> Iterator[WASMStore]:
    """
    Give the snapshot a store of its own.

    Args:
        tmp_path: Per-test temporary directory.

    Yields:
        The store the applications tally reads.
    """
    WASMStore.reset_instance()
    instance = WASMStore(tmp_path / "wasm.db")
    try:
        yield instance
    finally:
        instance.close()
        WASMStore.reset_instance()


def deploy(store: WASMStore, domain: str, **overrides: Any) -> App:
    """
    Record an application the way a deploy would, for the applications tally.

    Args:
        store: The store to write to.
        domain: The application's domain.
        **overrides: Fields to override on the application record.

    Returns:
        The stored application.
    """
    fields: dict[str, Any] = {
        "domain": domain,
        "app_type": "nextjs",
        "source": "https://github.com/you/app",
        "port": 3000,
        "app_path": f"/var/www/apps/{domain}",
        "status": "running",
        "is_static": False,
    }
    fields.update(overrides)
    return store.create_app(App(**fields))


def register_service(store: WASMStore, name: str, *, status: str = "active") -> None:
    """
    Register a unit in the store the way ``ServiceManager.create_service``
    would during a real deploy.

    ``ServiceManager.is_managed`` recognises a unit with the current,
    unprefixed naming convention only when the store knows its name; the
    ``wasm-`` prefix is what lets a *legacy* unit skip that lookup. A test
    that scripts an unprefixed unit without this would see it silently
    filtered out of ``list_services()``, same as a real deploy that forgot to
    register it would.

    Args:
        store: The store to write to.
        name: The unit's name, without the ``.service`` suffix.
        status: Recorded status; the tally reads systemd, not this column.
    """
    store.create_service(Service(name=name, status=status))


# --------------------------------------------------------------------- shape


def test_the_snapshot_has_the_console_s_shape(runner: FakeRunner, store: WASMStore) -> None:
    """Every field the topbar, the SSE event and the REST endpoint rely on is present."""
    state = read_machine(apps_root="/tmp")

    assert isinstance(state, MachineState)
    assert isinstance(state.hostname, str) and state.hostname
    assert isinstance(state.uptime_s, float)
    assert len(state.load) == 3
    assert isinstance(state.load_history, list)
    assert isinstance(state.cpu_percent, float)
    assert isinstance(state.memory, MemorySnapshot)
    assert isinstance(state.disk, DiskSnapshot)
    assert isinstance(state.units, UnitTally)
    assert isinstance(state.apps, AppTally)


def test_the_snapshot_survives_asdict_and_json_dumps(runner: FakeRunner, store: WASMStore) -> None:
    """`wasm.web.events.machine_frame` hands this straight to `json.dumps`."""
    payload = asdict(read_machine(apps_root="/tmp"))

    # `dataclasses.asdict` keeps `load` a tuple; the wire format does not
    # have tuples. Round-tripping through JSON must reproduce the same
    # values, the array/tuple distinction included, since that is exactly
    # what `wasm.web.events.machine_frame` does before sending it.
    round_tripped = json.loads(json.dumps(payload))
    assert round_tripped["load"] == list(payload["load"])
    assert round_tripped["load_history"] == payload["load_history"]

    assert set(payload) == {
        "hostname",
        "uptime_s",
        "load",
        "load_history",
        "cpu_percent",
        "memory",
        "disk",
        "units",
        "apps",
    }
    assert set(payload["memory"]) == {"used", "total", "percent"}
    assert set(payload["disk"]) == {"used", "total", "percent"}
    assert set(payload["units"]) == {"running", "failed", "stopped"}
    assert set(payload["apps"]) == {"running", "failed", "stopped", "static"}


# --------------------------------------------------------------- unit tally


def test_classify_unit_sorts_the_four_systemd_signals() -> None:
    """The one place a systemd active/sub pair becomes a bucket."""
    assert classify_unit({"active": "active", "sub": "running"}) == "active"
    assert classify_unit({"active": "failed", "sub": "failed"}) == "failed"
    assert classify_unit({"active": "active", "sub": "failed"}) == "failed"
    assert classify_unit({"active": "activating", "sub": "auto-restart"}) == "busy"
    assert classify_unit({"active": "inactive", "sub": "dead"}) == "stopped"


def test_units_come_from_service_manager_not_a_probe_per_unit(
    runner: FakeRunner, store: WASMStore
) -> None:
    """
    The header used to always read "0 running / 0 failed": nothing ever
    tallied the live systemd source. It has to come from one
    ``systemctl list-units`` call, never a probe per unit - the SSE event
    refreshes every five seconds, and a per-unit round trip is exactly what
    the predecessor of this module was written to avoid.
    """
    runner.script(
        LIST_UNITS,
        stdout=(
            "UNIT LOAD ACTIVE SUB DESCRIPTION\n"
            "wasm-one.service loaded active running App\n"
            "wasm-two.service loaded active running App\n"
            "wasm-broken.service loaded failed failed App\n"
        ),
    )

    state = read_machine(apps_root="/tmp")

    assert state.units == UnitTally(running=2, failed=1, stopped=0)
    assert not any(call[:2] == ("systemctl", "is-active") for call in runner.calls), (
        "the tally must not ask about units one at a time"
    )


def test_a_crash_looping_unit_folds_into_stopped(runner: FakeRunner, store: WASMStore) -> None:
    """
    A unit systemd is restarting every few seconds reads as active in between
    restarts. The JSON schema has no "busy" bucket of its own - the console
    reads the `job` and `app` events for that, not a unit poll - so it lands
    in "stopped", not silently inside "running".
    """
    runner.script(
        LIST_UNITS,
        stdout=(
            "UNIT LOAD ACTIVE SUB DESCRIPTION\n"
            "wasm-looping.service loaded activating auto-restart App\n"
        ),
    )

    state = read_machine(apps_root="/tmp")

    assert state.units == UnitTally(running=0, failed=0, stopped=1)


def test_only_units_wasm_manages_are_counted(runner: FakeRunner, store: WASMStore) -> None:
    """A unit systemd happens to report alongside them, such as ssh, never inflates the tally."""
    runner.script(
        LIST_UNITS,
        stdout=(
            "UNIT LOAD ACTIVE SUB DESCRIPTION\n"
            "wasm-one.service loaded active running App\n"
            "ssh.service loaded active running OpenBSD Secure Shell server\n"
        ),
    )

    state = read_machine(apps_root="/tmp")

    assert state.units == UnitTally(running=1, failed=0, stopped=0)


def test_a_systemd_that_cannot_be_reached_degrades_the_snapshot_to_zero(
    runner: FakeRunner, store: WASMStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The snapshot refreshes on a timer; a systemd that cannot be reached must not raise."""

    def broken(self: ServiceManager, all_services: bool = False) -> list[dict]:
        raise ServiceError("systemd is not reachable")

    monkeypatch.setattr(ServiceManager, "list_services", broken)

    state = read_machine(apps_root="/tmp")

    assert state.units == UnitTally(running=0, failed=0, stopped=0)
    assert state.apps == AppTally(running=0, failed=0, stopped=0, static=0)


def test_fetch_service_states_is_the_one_call_both_tallies_share(
    runner: FakeRunner, store: WASMStore
) -> None:
    """Both the unit tally and the applications tally read this, not systemd directly."""
    runner.script(
        LIST_UNITS,
        stdout="UNIT LOAD ACTIVE SUB DESCRIPTION\nwasm-one.service loaded active running App\n",
    )

    services = fetch_service_states()

    assert services == [
        {"name": "wasm-one", "load": "loaded", "active": "active", "sub": "running"}
    ]


# --------------------------------------------------------------- app tally


def test_a_static_app_is_counted_without_asking_systemd_about_it(
    runner: FakeRunner, store: WASMStore
) -> None:
    """A static site has no unit; asking systemd about one would always say "not running"."""
    deploy(store, domain="static.example.com", is_static=True)

    state = read_machine(apps_root="/tmp")

    assert state.apps == AppTally(running=0, failed=0, stopped=0, static=1)


def test_a_running_application_is_matched_by_its_unit_name(
    runner: FakeRunner, store: WASMStore
) -> None:
    """The current naming convention: the unit is the sanitised domain, no prefix."""
    deploy(store, domain="one.example.com")
    register_service(store, domain_to_app_name("one.example.com"))
    runner.script(
        LIST_UNITS,
        stdout=(
            "UNIT LOAD ACTIVE SUB DESCRIPTION\n"
            f"{domain_to_app_name('one.example.com')}.service loaded active running App\n"
        ),
    )

    state = read_machine(apps_root="/tmp")

    assert state.apps == AppTally(running=1, failed=0, stopped=0, static=0)


def test_a_legacy_prefixed_unit_still_matches_its_application(
    runner: FakeRunner, store: WASMStore
) -> None:
    """An application deployed before the ``wasm-`` prefix was dropped is still found."""
    deploy(store, domain="old.example.com")
    runner.script(
        LIST_UNITS,
        stdout=(
            "UNIT LOAD ACTIVE SUB DESCRIPTION\n"
            f"{legacy_app_name('old.example.com')}.service loaded failed failed App\n"
        ),
    )

    state = read_machine(apps_root="/tmp")

    assert state.apps == AppTally(running=0, failed=1, stopped=0, static=0)


def test_an_application_with_no_matching_unit_counts_as_stopped(
    runner: FakeRunner, store: WASMStore
) -> None:
    """A deployed application whose unit is gone reads as stopped, not silently dropped."""
    deploy(store, domain="ghost.example.com")

    state = read_machine(apps_root="/tmp")

    assert state.apps == AppTally(running=0, failed=0, stopped=1, static=0)


def test_applications_and_units_are_tallied_from_one_systemctl_call(
    runner: FakeRunner, store: WASMStore
) -> None:
    """
    A mix of static, running and unmatched applications alongside a
    standalone service unit, all read from the single scripted response.
    """
    deploy(store, domain="app.example.com")
    deploy(store, domain="static.example.com", is_static=True)
    deploy(store, domain="ghost.example.com")
    register_service(store, domain_to_app_name("app.example.com"))
    runner.script(
        LIST_UNITS,
        stdout=(
            "UNIT LOAD ACTIVE SUB DESCRIPTION\n"
            f"{domain_to_app_name('app.example.com')}.service loaded active running App\n"
            "wasm-worker.service loaded active running Standalone service\n"
        ),
    )

    state = read_machine(apps_root="/tmp")

    assert state.units == UnitTally(running=2, failed=0, stopped=0)
    assert state.apps == AppTally(running=1, failed=0, stopped=1, static=1)


def test_the_store_being_unreadable_degrades_apps_to_zero_not_a_crash(
    runner: FakeRunner, store: WASMStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The applications tally must not take the snapshot down with it."""

    def broken() -> WASMStore:
        raise WASMError("store is locked")

    monkeypatch.setattr("wasm.core.store.get_store", broken)

    state = read_machine(apps_root="/tmp")

    assert state.apps == AppTally(running=0, failed=0, stopped=0, static=0)


# ------------------------------------------------------------- REST endpoint


@pytest.fixture
def client(runner: FakeRunner, store: WASMStore) -> TestClient:
    """
    Build a client for the system router with authentication stubbed.

    Args:
        runner: The fake command runner, so the unit tally reaches no process.
        store: The sandboxed store, so the applications tally reaches no
            real database.

    Returns:
        A client whose requests are already authenticated.
    """
    app = FastAPI()
    app.include_router(system_router, prefix="/api/system")
    app.dependency_overrides[get_current_session] = lambda: {"session_id": "test"}
    return TestClient(app)


def test_get_machine_answers_with_the_snapshot(client: TestClient) -> None:
    """GET /api/system/machine is read_machine's answer, typed for the OpenAPI contract."""
    response = client.get("/api/system/machine")

    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {
        "hostname",
        "uptime_s",
        "load",
        "load_history",
        "cpu_percent",
        "memory",
        "disk",
        "units",
        "apps",
    }
    assert set(body["units"]) == {"running", "failed", "stopped"}
    assert set(body["apps"]) == {"running", "failed", "stopped", "static"}


def test_get_machine_reports_the_live_unit_tally(client: TestClient, runner: FakeRunner) -> None:
    """The endpoint and the SSE event read the same live systemd source."""
    runner.script(
        LIST_UNITS,
        stdout=(
            "UNIT LOAD ACTIVE SUB DESCRIPTION\n"
            "wasm-ok.service loaded active running App\n"
            "wasm-broken.service loaded failed failed App\n"
        ),
    )

    body = client.get("/api/system/machine").json()

    assert body["units"] == {"running": 1, "failed": 1, "stopped": 0}


def test_get_machine_demands_a_session(tmp_path: Path, runner: FakeRunner) -> None:
    """The machine snapshot names every application on the host; it is not public."""
    from wasm.web.auth import SecurityConfig
    from wasm.web.server import create_app

    app = create_app(SecurityConfig(state_dir=tmp_path / "state", rate_limit_requests=5000))
    client = TestClient(app, client=("testclient", 50000), follow_redirects=False)

    response = client.get("/api/system/machine")

    assert response.status_code == 401


def test_the_disk_meter_reads_the_key_every_deployer_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    Every deployer writes the flat 'apps_directory'; the meter used to read
    the deprecated 'apps.directory' alias instead, which nothing ever wrote,
    so it always reported the hard-coded default regardless of where
    applications were actually deployed.
    """
    monkeypatch.setattr(
        "wasm.core.config.DEFAULT_CONFIG_PATH", tmp_path / "etc" / "wasm" / "config.yaml"
    )
    Config.reset_instance()
    try:
        config = Config()
        config.set("apps_directory", "/srv/apps")
        assert config.save() is True

        assert _resolve_apps_root(None) == "/srv/apps"
    finally:
        Config.reset_instance()
