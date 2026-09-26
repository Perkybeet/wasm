# Copyright (c) 2024-2026 Yago Lopez Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
Tests for the ``updates.check`` switch.

The GitHub check used to be unconditional: every ``wasm`` command started a
background thread that hit the network, and there was no way to turn it off
for a server with no route to GitHub, or one where an operator simply does
not want the request made. The switch must be read fresh every time - a
long-lived process such as the panel must not need a restart for
``wasm config set updates.check false`` to take effect - and turning it off
must never make the check itself block anything: it already runs off the
command's own path, in a background thread with a short timeout.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from wasm.core.config import Config
from wasm.core.update_checker import UpdateChecker


@pytest.fixture
def config_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """
    Point the configuration singleton at a sandbox file.

    Args:
        tmp_path: Per-test temporary directory.
        monkeypatch: Patching helper, scoped to the test.

    Yields:
        The path the singleton reads from and writes to.
    """
    path = tmp_path / "etc" / "wasm" / "config.yaml"
    monkeypatch.setattr("wasm.core.config.DEFAULT_CONFIG_PATH", path)
    Config.reset_instance()
    try:
        yield path
    finally:
        Config.reset_instance()


@pytest.fixture(autouse=True)
def _reset_checker_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """
    Isolate the checker's class-level state and cache file between tests.

    Args:
        tmp_path: Per-test temporary directory.
        monkeypatch: Patching helper, scoped to the test.
    """
    monkeypatch.setattr(UpdateChecker, "CACHE_FILE", tmp_path / "version_check.json")
    UpdateChecker._check_thread = None
    UpdateChecker._update_version = None
    try:
        yield
    finally:
        UpdateChecker._check_thread = None
        UpdateChecker._update_version = None


def test_enabled_by_default(config_path: Path) -> None:
    """No configuration file at all means the check is on."""
    assert UpdateChecker.enabled() is True


def test_disabled_when_configured_off(config_path: Path) -> None:
    """'wasm config set updates.check false' must be honoured."""
    Config().set("updates.check", False)

    assert UpdateChecker.enabled() is False


def test_enabled_reads_the_configuration_fresh_each_time(config_path: Path) -> None:
    """A long-lived process must not need a restart for the switch to take effect."""
    assert UpdateChecker.enabled() is True

    Config().set("updates.check", False)
    assert UpdateChecker.enabled() is False

    Config().set("updates.check", True)
    assert UpdateChecker.enabled() is True


def test_a_configuration_that_cannot_be_read_defaults_to_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cosmetic check failing to read its own switch must not look like a crash."""

    def _broken(self: object, key: str, default: object = None) -> object:
        raise OSError("no such file or directory")

    monkeypatch.setattr("wasm.core.config.Config.get", _broken)

    assert UpdateChecker.enabled() is True


def test_start_background_check_makes_no_request_when_disabled(
    config_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Disabled must mean no thread, no cache read and no cached message left over."""
    Config().set("updates.check", False)
    calls: list[str] = []
    monkeypatch.setattr(
        UpdateChecker, "_background_check", classmethod(lambda cls: calls.append("ran"))
    )
    monkeypatch.setattr(
        UpdateChecker, "_is_cache_valid", classmethod(lambda cls: calls.append("cache") or False)
    )
    UpdateChecker._update_version = "9.9.9"  # a stale result from a previous, enabled run

    UpdateChecker.start_background_check()

    assert calls == []
    assert UpdateChecker._update_version is None
    assert UpdateChecker._check_thread is None


def test_start_background_check_still_runs_when_enabled(
    config_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ordinary case - left on - is unaffected by the switch's presence."""
    started = []
    monkeypatch.setattr(
        UpdateChecker,
        "_is_cache_valid",
        classmethod(lambda cls: started.append("checked") or False),
    )
    monkeypatch.setattr(UpdateChecker, "_background_check", classmethod(lambda cls: None))

    UpdateChecker.start_background_check()
    if UpdateChecker._check_thread is not None:
        UpdateChecker._check_thread.join(timeout=2)

    assert started == ["checked"]


def test_start_background_check_returns_immediately_when_disabled(
    config_path: Path,
) -> None:
    """Disabling the check must never be the thing that makes a command slow to start."""
    import time

    Config().set("updates.check", False)

    started = time.monotonic()
    UpdateChecker.start_background_check()
    elapsed = time.monotonic() - started

    assert elapsed < 0.5
