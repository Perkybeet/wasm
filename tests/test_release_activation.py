# Copyright (c) 2024-2026 Yago Lopez Prado
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Tests for instant rollback: activating a release that is already on disk.

The releases are built by the real pipeline over a temporary tree (the
fixtures of ``tests/test_release_pipeline.py``), so what is activated is what
a deploy left behind. What is pinned:

- Going back re-points ``current``, restarts the unit and passes the same
  health gate a deploy does; nothing is rebuilt.
- A release that does not pass is recorded as failed and the release that was
  serving is active and restarted again.
- Every activation is its own history row with its trigger and log; going
  back marks the build that stopped serving as rolled back.
- An in-place application is refused with the way forward, never converted.
"""

# The pipeline fixtures are imported rather than replicated, so there stays
# one definition of the fake machine.
# ruff: noqa: F811

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from tests.test_release_pipeline import (  # noqa: F401  (pytest resolves fixtures by name)
    BROKEN_SERVER,
    DOMAIN,
    GOOD_SERVER,
    PORT,
    active_id,
    deploy_new,
    git,
    machine,
    node_tree,
    root,
    store,
    update,
)
from wasm.core.exceptions import DeploymentError, WASMError
from wasm.core.store import App, WASMStore
from wasm.deployers import lifecycle


@pytest.fixture
def two_releases(
    tmp_path: Path,
    root: Path,
    store: WASMStore,
    machine: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[str, str]:
    """
    Deploy v1, update to v2, and point the activation at the fake machine.

    Returns:
        The ids of the first and the second release; the second is active.
    """
    machine.git.publish(node_tree(tmp_path / "v1"))
    deploy_new(root, machine)
    first = active_id(root)
    machine.git.publish(node_tree(tmp_path / "v2", server=GOOD_SERVER + "// v2\n"))
    update(machine, monkeypatch)
    second = active_id(root)

    def probe(url: str, **kwargs: Any) -> bool:
        machine.probes.append(url)
        server = root / "current" / "server.js"
        if "throw" in server.read_text():
            kwargs["on_attempt"]("Health check attempt 1 failed: [Errno 111] Connection refused")
            return False
        return True

    monkeypatch.setattr(lifecycle, "ServiceManager", lambda **kwargs: machine.services)
    monkeypatch.setattr(lifecycle, "SourceManager", lambda **kwargs: machine.git)
    monkeypatch.setattr(lifecycle, "wait_until_healthy", probe)
    machine.services.restarts.clear()
    machine.probes.clear()
    return first, second


def statuses(store: WASMStore) -> dict[str, str]:
    """Release id to status, as the store records them."""
    app = store.get_app(DOMAIN)
    assert app is not None and app.id is not None
    return {row.id: row.status for row in store.list_releases(app.id)}


def test_rolling_back_activates_the_previous_release_and_restarts(
    root: Path, store: WASMStore, machine: SimpleNamespace, two_releases: tuple[str, str]
) -> None:
    """No build, no fetch: current moves, the unit restarts, the gate passes."""
    first, second = two_releases
    runs_before = list(machine.runner.calls)

    outcome = lifecycle.activate_release(DOMAIN, trigger="panel")

    assert active_id(root) == first
    assert outcome.release.id == first
    assert outcome.previous is not None and outcome.previous.id == second
    assert outcome.changed and outcome.went_back
    assert machine.services.restarts == [f"releases/{first}"]
    assert machine.probes == [f"http://127.0.0.1:{PORT}/"]
    assert machine.runner.calls == runs_before, "an activation runs no install or build"
    assert statuses(store) == {first: "active", second: "rolled_back"}

    history = store.list_deployments(DOMAIN)
    assert history[0].id == outcome.deployment_id
    assert (history[0].status, history[0].triggered_by) == ("success", "panel")
    assert history[0].git_commit == first.split("-")[2]
    assert history[0].git_branch == "main"
    assert f"Activated release {first}" in Path(history[0].log_path).read_text()
    # The update that built the second release is the build that stopped serving.
    assert history[1].status == "rolled_back"


def test_activating_a_newer_release_again_is_not_a_rollback(
    root: Path, store: WASMStore, two_releases: tuple[str, str]
) -> None:
    """Forward after a rollback supersedes; nothing more is marked rolled back."""
    first, second = two_releases
    lifecycle.activate_release(DOMAIN)

    outcome = lifecycle.activate_release(DOMAIN, second)

    assert active_id(root) == second
    assert not outcome.went_back
    assert statuses(store) == {first: "superseded", second: "active"}
    assert store.list_deployments(DOMAIN)[0].status == "success"


def test_a_release_that_fails_the_gate_puts_the_serving_one_back(
    root: Path, store: WASMStore, machine: SimpleNamespace, two_releases: tuple[str, str]
) -> None:
    """The operator ends where they started, told why in the process's own words."""
    first, second = two_releases
    (root / "releases" / first / "server.js").write_text(BROKEN_SERVER)

    with pytest.raises(DeploymentError, match=f"{second} is active again") as failure:
        lifecycle.activate_release(DOMAIN, first)

    assert active_id(root) == second
    assert machine.services.restarts == [f"releases/{first}", f"releases/{second}"]
    assert "Connection refused" in failure.value.details
    assert "Error: boom" in failure.value.details
    assert statuses(store) == {first: "failed", second: "active"}
    row = store.list_deployments(DOMAIN)[0]
    assert row.status == "failed"
    assert "Error: boom" in row.error
    # Still on disk: a release that failed to come back is not deleted.
    assert (root / "releases" / first).is_dir()


def test_the_active_release_is_not_activated_twice(
    store: WASMStore, machine: SimpleNamespace, two_releases: tuple[str, str]
) -> None:
    """Nothing to do is nothing done: no restart, no history row."""
    _, second = two_releases
    rows_before = len(store.list_deployments(DOMAIN))

    outcome = lifecycle.activate_release(DOMAIN, second)

    assert not outcome.changed
    assert machine.services.restarts == []
    assert len(store.list_deployments(DOMAIN)) == rows_before


def test_an_unknown_release_is_refused_with_what_there_is(
    store: WASMStore, two_releases: tuple[str, str]
) -> None:
    """The error lists the releases on disk."""
    first, second = two_releases

    with pytest.raises(DeploymentError) as failure:
        lifecycle.activate_release(DOMAIN, "20200101-000000-0000000")

    assert first in failure.value.details and second in failure.value.details


def test_the_oldest_release_has_nothing_to_roll_back_to(
    root: Path, store: WASMStore, two_releases: tuple[str, str]
) -> None:
    """Going back from the oldest release says so, with the alternatives."""
    lifecycle.activate_release(DOMAIN)

    with pytest.raises(DeploymentError, match="oldest"):
        lifecycle.activate_release(DOMAIN)


def test_an_in_place_app_has_no_releases_and_is_left_alone(
    tmp_path: Path, store: WASMStore
) -> None:
    """Review Focus: an in-place app is never converted by a side door."""
    root = tmp_path / "inplace"
    root.mkdir()
    store.create_app(App(domain=DOMAIN, app_type="nodejs", app_path=str(root)))

    with pytest.raises(DeploymentError, match="in place") as failure:
        lifecycle.activate_release(DOMAIN)

    assert "wasm app migrate" in failure.value.details
    assert os.listdir(root) == []


def test_an_unknown_application_is_an_error(store: WASMStore) -> None:
    """Nothing deployed at the domain."""
    with pytest.raises(WASMError, match="not found"):
        lifecycle.list_releases(DOMAIN)


def test_the_listing_joins_the_disk_and_the_history(
    root: Path, store: WASMStore, machine: SimpleNamespace, two_releases: tuple[str, str]
) -> None:
    """A failed release removed from disk is still listed, and marked as gone."""
    first, second = two_releases
    app = store.get_app(DOMAIN)
    assert app is not None and app.id is not None
    gone = "20260101-000000-ccccccc"
    from wasm.core.store import ReleaseRecord

    store.record_release(
        ReleaseRecord(
            id=gone,
            app_id=app.id,
            git_commit="ccccccc",
            created_at="2026-01-01T00:00:00+00:00",
            status="failed",
            path=str(root / "releases" / gone),
        )
    )

    listed = lifecycle.list_releases(DOMAIN)

    assert [r.id for r in listed] == [second, first, gone]
    assert [(r.status, r.active, r.on_disk) for r in listed] == [
        ("active", True, True),
        ("superseded", False, True),
        ("failed", False, False),
    ]
