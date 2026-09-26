# Copyright (c) 2024-2026 Yago Lopez Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
Tests that every operation on an application holds its lock.

Two operations on one application used to be free to interleave: a webhook's
update while an operator rolled back, a deploy while a migration renamed the
tree. Each operation of the engine now takes the application's lock for as
long as it runs, and one that finds it taken fails at once, before touching
anything, with the name of the one running.

The releases are built by the real pipeline over a temporary tree (the
fixtures of ``tests/test_release_pipeline.py``); the concurrent holder is a
second thread, which the lock refuses exactly as it refuses a second process.
"""

# ruff: noqa: F811

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from tests.test_applock import Holder, attempt_elsewhere
from tests.test_release_pipeline import (  # noqa: F401  (pytest resolves fixtures by name)
    DOMAIN,
    GOOD_SERVER,
    active_id,
    deploy_new,
    git,
    machine,
    node_tree,
    root,
    store,
    update,
)
from wasm.core.applock import AppBusyError, is_held_here
from wasm.core.store import WASMStore
from wasm.deployers import lifecycle
from wasm.deployers.releases import ReleaseManager
from wasm.managers.service_manager import ResourceLimits


@pytest.fixture
def deployed(
    tmp_path: Path, root: Path, store: WASMStore, machine: SimpleNamespace
) -> SimpleNamespace:
    """An application on releases with one release active, and v2 published."""
    machine.git.publish(node_tree(tmp_path / "v1"))
    deploy_new(root, machine)
    machine.git.publish(node_tree(tmp_path / "v2", server=GOOD_SERVER + "// v2\n"))
    return machine


def release_ids(root: Path) -> list[str]:
    """The releases on disk, newest first."""
    return [release.id for release in ReleaseManager(root).list()]


def test_an_update_is_refused_while_another_operation_runs(
    root: Path, deployed: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = release_ids(root)

    with Holder(DOMAIN, "migration"), pytest.raises(AppBusyError) as refused:
        update(deployed, monkeypatch)

    assert refused.value.holder is not None and refused.value.holder.operation == "migration"
    assert "update of rel.example.com" in refused.value.message
    assert release_ids(root) == before, "nothing was fetched or built"


def test_an_update_holds_the_lock_until_it_has_finished(
    root: Path, deployed: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A rollback asked for while the update builds is refused, naming the update."""
    seen: list[Any] = []
    export = deployed.git.export_commit

    def export_commit(repository: Path, commit: str, destination: Path) -> None:
        seen.append(is_held_here(DOMAIN))
        seen.append(attempt_elsewhere(DOMAIN, "release activation"))
        export(repository, commit, destination)

    monkeypatch.setattr(deployed.git, "export_commit", export_commit)
    update(deployed, monkeypatch)

    assert seen[0] is True
    assert isinstance(seen[1], AppBusyError)
    assert seen[1].holder is not None and seen[1].holder.operation == "update"
    assert attempt_elsewhere(DOMAIN, "release activation") is None, "released afterwards"


def test_a_rollback_is_refused_while_an_update_runs(
    root: Path, deployed: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    update(deployed, monkeypatch)
    serving = active_id(root)
    monkeypatch.setattr(lifecycle, "ServiceManager", lambda **kwargs: deployed.services)

    with Holder(DOMAIN, "update"), pytest.raises(AppBusyError, match="update started at"):
        lifecycle.activate_release(DOMAIN)

    assert active_id(root) == serving


def test_a_deploy_is_refused_while_another_operation_runs(
    tmp_path: Path, root: Path, store: WASMStore, machine: SimpleNamespace
) -> None:
    machine.git.publish(node_tree(tmp_path / "v1"))

    with Holder(DOMAIN, "deletion"), pytest.raises(AppBusyError, match="deletion started"):
        deploy_new(root, machine)

    assert not root.exists()
    assert store.get_app(DOMAIN) is None


def test_a_change_of_limits_is_refused_while_another_operation_runs(
    root: Path, store: WASMStore, deployed: SimpleNamespace
) -> None:
    with Holder(DOMAIN, "update"), pytest.raises(AppBusyError):
        lifecycle.set_resource_limits(DOMAIN, ResourceLimits(memory_max_mb=256))

    app = store.get_app(DOMAIN)
    assert app is not None and app.memory_max_mb is None
