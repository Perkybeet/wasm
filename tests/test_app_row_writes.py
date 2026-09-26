# Copyright (c) 2024-2026 Yago Lopez Prado
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
A change of the health check or the retention is never undone by a deploy.

A deploy or an update reads the application's row when it starts and writes
the whole row back when it finishes. The health check and the retention have
setters of their own, so a change made while the deploy ran was silently put
back to what the deploy had read. What is pinned:

- **The full-row write leaves those columns alone**: only their setters write
  them.
- **Changing the health check holds the application's lock**, like changing
  the retention, so it is refused while an operation runs rather than racing it.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.test_applock import Holder
from wasm.core.applock import AppBusyError
from wasm.core.store import App, WASMStore
from wasm.deployers import lifecycle

DOMAIN = "rows.example.com"


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[WASMStore]:
    """A store in the test directory, where the lifecycle looks."""
    WASMStore.reset_instance()
    instance = WASMStore(tmp_path / "wasm.db")
    monkeypatch.setattr(lifecycle, "get_store", lambda: instance)
    yield instance
    WASMStore.reset_instance()


def make_app(store: WASMStore, tmp_path: Path) -> App:
    """A running application with a row."""
    return store.create_app(
        App(
            domain=DOMAIN,
            app_type="nodejs",
            app_path=str(tmp_path / "rows-example-com"),
            port=3000,
            status="deploying",
        )
    )


def test_a_full_row_write_does_not_undo_a_health_check_set_meanwhile(
    store: WASMStore, tmp_path: Path
) -> None:
    """What a deploy read before the change is written back; the change stays."""
    read_by_the_deploy = make_app(store, tmp_path)
    store.set_app_health(DOMAIN, path="/healthz", expect="200-299", timeout=90)
    store.set_keep_releases(DOMAIN, 12)

    read_by_the_deploy.status = "running"
    store.update_app(read_by_the_deploy)

    now = store.get_app(DOMAIN)
    assert now is not None
    assert now.status == "running"
    assert (now.health_path, now.health_expect, now.health_timeout) == ("/healthz", "200-299", 90)
    assert now.keep_releases == 12


def test_the_setters_are_the_only_writers_of_those_columns(
    store: WASMStore, tmp_path: Path
) -> None:
    """Setting the attributes on a row and writing it changes nothing there."""
    app = make_app(store, tmp_path)
    app.health_path = "/other"
    app.keep_releases = 30

    store.update_app(app)

    now = store.get_app(DOMAIN)
    assert now is not None
    assert now.health_path is None
    assert now.keep_releases != 30


def test_changing_the_health_check_is_refused_while_an_operation_runs(
    store: WASMStore, tmp_path: Path
) -> None:
    """It waits for nobody and races nobody: the lock says who is running."""
    make_app(store, tmp_path)

    with Holder(DOMAIN, "update"), pytest.raises(AppBusyError):
        lifecycle.set_health_check(DOMAIN, path="/healthz", expect=None, timeout=None)

    now = store.get_app(DOMAIN)
    assert now is not None and now.health_path is None
