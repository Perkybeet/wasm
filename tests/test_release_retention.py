# Copyright (c) 2024-2026 Yago Lopez Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
Tests for changing how many releases an application keeps.

:func:`wasm.deployers.lifecycle.set_release_retention` is what ``wasm
releases keep`` and ``PATCH /api/apps/{d}/releases/retention`` both call.
Pinned here: the value is validated where it is stored, the releases beyond
it are pruned at once rather than at the next deploy, and pruning never takes
the release that serves or the one a rollback would go to.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from wasm.core.exceptions import DeploymentError, ValidationError
from wasm.core.store import App, ReleaseRecord, WASMStore
from wasm.deployers import lifecycle

DOMAIN = "rel.example.com"
#: Oldest first.
IDS = [f"20260925-12000{n}-aaaaaaa" for n in range(1, 6)]
#: A release that failed and was removed; only its row is left.
FAILED = "20260925-110000-bbbbbbb"


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """A store of the test's own, where the lifecycle looks for it."""
    WASMStore.reset_instance()
    instance = WASMStore(tmp_path / "wasm.db")
    monkeypatch.setattr(lifecycle, "get_store", lambda: instance)
    yield instance
    WASMStore.reset_instance()


def releases_app(store: WASMStore, root: Path, *, active: str = IDS[-1]) -> App:
    """
    Five releases on disk and in the store, one failed row, ``current`` at ``active``.

    Args:
        store: Where the rows go.
        root: The application directory.
        active: The release ``current`` points at.

    Returns:
        The stored application.
    """
    for release_id in IDS:
        (root / "releases" / release_id).mkdir(parents=True)
    (root / "current").symlink_to(Path("releases") / active)
    app = store.create_app(
        App(domain=DOMAIN, app_type="nodejs", port=3100, app_path=str(root), layout="releases")
    )
    for release_id in [FAILED, *IDS]:
        store.record_release(
            ReleaseRecord(
                id=release_id,
                app_id=app.id,
                git_commit="aaaaaaa",
                created_at=f"2026-09-25T{release_id[9:11]}:{release_id[11:13]}:{release_id[13:15]}Z",
                status="failed" if release_id == FAILED else "superseded",
                path=str(root / "releases" / release_id),
            )
        )
    return app


def on_disk(root: Path) -> set[str]:
    return {path.name for path in (root / "releases").iterdir()}


def test_lowering_the_retention_prunes_at_once(store: WASMStore, tmp_path: Path) -> None:
    """The newest two stay; the rest and their rows go, including the stale failure."""
    root = tmp_path / "rel"
    app = releases_app(store, root)

    change = lifecycle.set_release_retention(DOMAIN, 2)

    assert change.keep_releases == 2
    assert sorted(change.pruned) == sorted(IDS[:3])
    assert on_disk(root) == set(IDS[3:])
    assert store.get_app(DOMAIN).keep_releases == 2
    assert {row.id for row in store.list_releases(app.id)} == set(IDS[3:])


def test_pruning_keeps_the_active_release_and_its_rollback_target(
    store: WASMStore, tmp_path: Path
) -> None:
    """After a rollback the active release is old; it and the one before it survive keep=1."""
    root = tmp_path / "rel"
    releases_app(store, root, active=IDS[1])

    change = lifecycle.set_release_retention(DOMAIN, 1)

    assert on_disk(root) == {IDS[4], IDS[1], IDS[0]}
    assert sorted(change.pruned) == sorted([IDS[2], IDS[3]])


def test_raising_the_retention_removes_nothing(store: WASMStore, tmp_path: Path) -> None:
    root = tmp_path / "rel"
    app = releases_app(store, root)

    change = lifecycle.set_release_retention(DOMAIN, 10)

    assert change.pruned == ()
    assert on_disk(root) == set(IDS)
    assert len(store.list_releases(app.id)) == len(IDS) + 1


@pytest.mark.parametrize("keep", [0, -3, 51, 1000])
def test_a_retention_outside_1_to_50_is_refused_and_nothing_is_pruned(
    store: WASMStore, tmp_path: Path, keep: int
) -> None:
    root = tmp_path / "rel"
    releases_app(store, root)

    with pytest.raises(ValidationError, match="50"):
        lifecycle.set_release_retention(DOMAIN, keep)

    assert on_disk(root) == set(IDS)
    assert store.get_app(DOMAIN).keep_releases == 5


def test_the_store_refuses_a_retention_outside_1_to_50(store: WASMStore, tmp_path: Path) -> None:
    """The chokepoint: the store itself will not hold a value pruning cannot honour."""
    store.create_app(App(domain=DOMAIN, app_type="nodejs", app_path=str(tmp_path)))

    with pytest.raises(ValidationError):
        store.set_keep_releases(DOMAIN, 0)
    assert store.set_keep_releases(DOMAIN, 50)
    assert store.get_app(DOMAIN).keep_releases == 50


def test_an_in_place_application_has_no_releases_to_keep(store: WASMStore, tmp_path: Path) -> None:
    store.create_app(App(domain=DOMAIN, app_type="nodejs", app_path=str(tmp_path)))

    with pytest.raises(DeploymentError, match="in place"):
        lifecycle.set_release_retention(DOMAIN, 3)
    assert store.get_app(DOMAIN).keep_releases == 5
