# Copyright (c) 2024-2026 Yago Lopez Prado
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Tests for the one deletion of an application.

``wasm delete`` and the console's delete job used to be two implementations.
The job never took a Docker Compose stack down: it stopped the unit, and a
unit that would not stop left its unit file behind, while the stack's
containers kept running from a directory the job then deleted. Both now go
through :func:`wasm.deployers.lifecycle.delete_app`, which takes the stack
down (keeping its volumes unless told otherwise), attempts every step
whatever the one before it did, and holds the application's lock.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from tests.test_applock import Holder
from wasm.core.applock import AppBusyError
from wasm.core.exceptions import ServiceError, WASMError
from wasm.core.runner import FakeRunner
from wasm.core.store import App, Service, Site, WASMStore
from wasm.deployers import lifecycle
from wasm.managers.webserver import SiteDeletion
from wasm.web.jobs import Job, JobContext, JobType, delete_app_job

DOMAIN = "shop.example.com"


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[WASMStore]:
    """A store in the test directory, where every module looks."""
    WASMStore.reset_instance()
    instance = WASMStore(tmp_path / "wasm.db")
    monkeypatch.setattr(lifecycle, "get_store", lambda: instance)
    yield instance
    WASMStore.reset_instance()


class FakeUnits:
    """Stands in for ServiceManager, recording what was removed."""

    def __init__(self) -> None:
        self.deleted: list[str] = []
        self.refuse: set[str] = set()

    def delete_service(self, name: str) -> None:
        if name in self.refuse:
            raise ServiceError(f"Failed to stop {name}", details="Job canceled")
        self.deleted.append(name)


@pytest.fixture
def machine(monkeypatch: pytest.MonkeyPatch, runner: FakeRunner) -> Any:
    """The units, the sites and docker, faked."""
    units = FakeUnits()
    sites: list[tuple[str, bool]] = []

    def delete_site(domain: str, **kwargs: Any) -> SiteDeletion:
        sites.append((domain, kwargs["delete_certificate"]))
        return SiteDeletion(domain=domain, nginx_removed=True, certificate_removed=True)

    monkeypatch.setattr(lifecycle, "ServiceManager", lambda **kwargs: units)
    monkeypatch.setattr(lifecycle, "delete_site_completely", delete_site)
    return type("Machine", (), {"units": units, "sites": sites, "runner": runner})


def compose_app(store: WASMStore, root: Path) -> App:
    """A Docker Compose application with a bind-mounted data directory."""
    root.mkdir(parents=True)
    (root / "docker-compose.prod.yml").write_text("services:\n  db:\n    image: postgres\n")
    (root / "data").mkdir()
    (root / "data" / "PG_VERSION").write_text("16")
    app = store.create_app(
        App(domain=DOMAIN, app_type="docker-compose", port=8080, app_path=str(root))
    )
    store.create_service(
        Service(app_id=app.id, name=root.name, command="docker compose up", working_directory="")
    )
    store.create_site(Site(app_id=app.id, domain=DOMAIN, proxy_port=8080))
    return app


def downs(runner: FakeRunner) -> list[tuple[str, ...]]:
    """Every ``docker compose ... down`` that ran."""
    return [call for call in runner.calls if call[:2] == ("docker", "compose") and "down" in call]


def job_context() -> JobContext:
    """A context for running a job function outside the job manager."""
    return JobContext(Job(id="job-1", type=JobType.DELETE, name="delete", description=""), print)


def test_a_compose_stack_is_taken_down_and_its_volumes_kept(
    tmp_path: Path, store: WASMStore, machine: Any
) -> None:
    root = tmp_path / "apps" / "shop-example-com"
    compose_app(store, root)

    outcome = lifecycle.delete_app(DOMAIN)

    (down,) = downs(machine.runner)
    assert str(root / "docker-compose.prod.yml") in down
    assert "--volumes" not in down
    assert outcome.containers_stopped and not outcome.volumes_removed
    assert machine.units.deleted == ["shop-example-com"]
    assert machine.sites == [(DOMAIN, True)]
    assert not root.exists() and outcome.files_removed
    assert store.get_app(DOMAIN) is None
    assert store.get_service("shop-example-com") is None


def test_volumes_go_only_when_asked_for(tmp_path: Path, store: WASMStore, machine: Any) -> None:
    compose_app(store, tmp_path / "apps" / "shop-example-com")

    outcome = lifecycle.delete_app(DOMAIN, remove_volumes=True)

    (down,) = downs(machine.runner)
    assert "--volumes" in down
    assert outcome.volumes_removed


def test_the_console_job_takes_the_same_path(
    tmp_path: Path, store: WASMStore, machine: Any
) -> None:
    """The job never ran compose down: its containers kept running."""
    root = tmp_path / "apps" / "shop-example-com"
    compose_app(store, root)

    result = delete_app_job(DOMAIN, remove_files=False, job_context=job_context())

    (down,) = downs(machine.runner)
    assert "--volumes" not in down
    assert machine.units.deleted == ["shop-example-com"]
    assert (root / "data" / "PG_VERSION").is_file(), "files kept, as asked"
    assert store.get_app(DOMAIN) is None
    assert result["status"] == "deleted"
    assert result["volumes_removed"] is False


def test_a_unit_that_will_not_stop_does_not_stop_the_rest(
    tmp_path: Path, store: WASMStore, machine: Any
) -> None:
    root = tmp_path / "apps" / "shop-example-com"
    compose_app(store, root)
    machine.units.refuse.add("shop-example-com")

    outcome = lifecycle.delete_app(DOMAIN)

    assert any("shop-example-com was not removed" in w for w in outcome.warnings)
    assert machine.sites == [(DOMAIN, True)]
    assert not root.exists()
    assert store.get_app(DOMAIN) is None


def test_every_unit_of_the_application_is_removed(
    tmp_path: Path, store: WASMStore, machine: Any
) -> None:
    """A monorepo runs one unit per workspace; deleting only the first left the others."""
    root = tmp_path / "apps" / "shop-example-com"
    root.mkdir(parents=True)
    app = store.create_app(App(domain=DOMAIN, app_type="monorepo", app_path=str(root)))
    for name in ("shop-example-com-web", "shop-example-com-api"):
        store.create_service(Service(app_id=app.id, name=name, command="x", working_directory=""))

    lifecycle.delete_app(DOMAIN, remove_certificate=False)

    assert sorted(machine.units.deleted) == ["shop-example-com-api", "shop-example-com-web"]
    assert machine.sites == [(DOMAIN, False)]
    assert downs(machine.runner) == []


def test_a_deletion_is_refused_while_another_operation_runs(
    tmp_path: Path, store: WASMStore, machine: Any
) -> None:
    root = tmp_path / "apps" / "shop-example-com"
    compose_app(store, root)

    with Holder(DOMAIN, "update"), pytest.raises(AppBusyError, match="update started at"):
        lifecycle.delete_app(DOMAIN)

    assert downs(machine.runner) == []
    assert (root / "data" / "PG_VERSION").is_file()
    assert store.get_app(DOMAIN) is not None


def test_nothing_deployed_is_an_error(tmp_path: Path, store: WASMStore, machine: Any) -> None:
    with pytest.raises(WASMError, match="Application not found"):
        lifecycle.delete_app("other.example.com")
