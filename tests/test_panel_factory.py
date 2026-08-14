# Copyright (c) 2024-2026 Yago Lopez Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
Tests for the panel's shared seed data factory.

:func:`tests.panel_factory.seed_panel_state` backs both
``scripts/panel_browser_check.py`` and any test that wants a populated panel.
These tests are its own contract: that it seeds exactly the counts it is
asked for, and that the parameters the browser check relies on to reproduce
its historical data still do.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.panel_factory import DEFAULT_DOMAINS, seed_panel_state
from wasm.core.store import WASMStore


@pytest.fixture
def store(tmp_path: Path) -> Iterator[WASMStore]:
    """
    Args:
        tmp_path: Per-test temporary directory.

    Yields:
        A store of its own.
    """
    WASMStore.reset_instance()
    instance = WASMStore(tmp_path / "wasm.db")
    try:
        yield instance
    finally:
        instance.close()
        WASMStore.reset_instance()


def test_the_defaults_seed_exactly_what_they_say(store: WASMStore) -> None:
    """The factory's own defaults are a claim; this is what checks it."""
    state = seed_panel_state(store)

    assert state.domains == list(DEFAULT_DOMAINS[:3])
    assert len(state.service_domains) == 2
    assert len(state.site_domains) == 2
    assert len(state.cert_domains) == 2
    assert len(state.backup_domains) == 1
    assert len(state.failed_domains) == 1
    assert len(state.app_ids) == 3

    apps = store.list_apps()
    assert {a.domain for a in apps} == set(state.domains)
    assert store.list_services()
    assert store.list_sites()


def test_failed_apps_are_the_last_ones_created(store: WASMStore) -> None:
    """Failure lands at the tail so the normal running/stopped pattern is untouched."""
    state = seed_panel_state(store, apps=4, services=0, sites=0, certs=0, backups=0, failed=2)

    assert state.failed_domains == list(DEFAULT_DOMAINS[2:4])
    failing_apps = {
        a.domain: a.status for a in store.list_apps() if a.domain in state.failed_domains
    }
    assert all(status == "failed" for status in failing_apps.values())


def test_certs_only_land_on_seeded_sites(store: WASMStore) -> None:
    """A certificate path is a detail of a site; it cannot outrun how many sites exist."""
    state = seed_panel_state(store, apps=4, services=0, sites=2, certs=3, backups=0, failed=0)

    # certs=3 asked for more than the 2 sites that exist, so it is capped.
    assert state.cert_domains == state.site_domains
    sites = {s.domain: s for s in store.list_sites()}
    for domain in state.cert_domains:
        assert sites[domain].ssl_certificate is not None
        assert sites[domain].ssl_key is not None


def test_seeding_the_original_eight_reproduces_the_browser_check(store: WASMStore) -> None:
    """
    Regression guard for the extraction.

    The browser check used to build these eight applications, services and
    sites inline, every one of them with a service and a site and none of
    them failed or carrying an explicit certificate path. This is that
    dataset, produced through the factory instead.
    """
    count = len(DEFAULT_DOMAINS)
    state = seed_panel_state(
        store,
        apps=count,
        services=count,
        sites=count,
        certs=0,
        backups=0,
        failed=0,
    )

    assert state.domains == list(DEFAULT_DOMAINS)
    assert state.service_domains == list(DEFAULT_DOMAINS)
    assert state.site_domains == list(DEFAULT_DOMAINS)
    assert state.failed_domains == []
    assert state.cert_domains == []
    assert state.backup_domains == []

    apps_by_domain = {a.domain: a for a in store.list_apps()}
    for index, domain in enumerate(DEFAULT_DOMAINS):
        app = apps_by_domain[domain]
        assert app.status == ("running" if index % 3 else "stopped")
        assert app.ssl_enabled
        assert app.port == 3000 + index

    sites_by_domain = {s.domain: s for s in store.list_sites()}
    for index, domain in enumerate(DEFAULT_DOMAINS):
        site = sites_by_domain[domain]
        assert site.enabled == (index % 4 != 0)
        assert site.ssl_certificate is None


def test_asking_for_more_than_the_applications_created_is_rejected(store: WASMStore) -> None:
    """Every count is a subset of the applications; a caller that gets this backwards should fail loudly."""
    with pytest.raises(ValueError, match="failed=2"):
        seed_panel_state(store, apps=1, services=0, sites=0, certs=0, backups=0, failed=2)
