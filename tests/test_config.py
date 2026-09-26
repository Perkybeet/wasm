# Copyright (c) 2024-2026 Yago López Prado
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Tests for the thread safety of the :class:`~wasm.core.config.Config` singleton.

The CLI and every panel request handler read ``Config()`` from their own
thread. The check-and-set of the singleton used to hold no lock at all, and
the instance was assigned to the class attribute *before* it was loaded, so a
second caller arriving while the first was still parsing ``config.yaml`` did
not build a second instance - it got a reference to the first one, read
through it before ``_load_config`` had populated it. The fix pairs the
check-and-set with the load under one lock, so a second caller waits for the
first instead of observing a half-built configuration.
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from wasm.core.config import Config


@pytest.fixture
def config_path(sandbox: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """
    Point the global config file at the sandbox and reset the singleton.

    Args:
        sandbox: Isolated filesystem root.
        monkeypatch: Patching helper, scoped to the test.

    Returns:
        Path the config module will read from and write to.
    """
    path = sandbox / "etc" / "wasm" / "config.yaml"
    monkeypatch.setattr("wasm.core.config.DEFAULT_CONFIG_PATH", path)
    Config.reset_instance()
    try:
        yield path
    finally:
        Config.reset_instance()


def test_a_second_caller_waits_for_the_first_to_finish_loading(
    config_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    A caller that arrives while another is building the singleton must wait
    for it, not read a configuration that has not been loaded yet.
    """
    started = threading.Event()
    release = threading.Event()
    calls: list[int] = []
    original = Config._load_config

    def blocking_load(self: Config) -> None:
        calls.append(1)
        started.set()
        assert release.wait(timeout=5), "the test itself deadlocked"
        original(self)

    monkeypatch.setattr(Config, "_load_config", blocking_load)

    first = threading.Thread(target=Config)
    first.start()
    assert started.wait(timeout=5), "the first caller never started loading"

    second_result: list[Config] = []
    second = threading.Thread(target=lambda: second_result.append(Config()))
    second.start()
    second.join(timeout=0.2)

    assert second.is_alive(), (
        "a second caller returned before the first finished loading; "
        "the check-and-set and the load must share one lock"
    )

    release.set()
    first.join(timeout=5)
    second.join(timeout=5)

    assert len(calls) == 1
    assert second_result[0] is Config._instance
    assert second_result[0].get("webserver") == "nginx"


def test_a_config_file_written_by_the_old_packaged_default_still_loads(
    config_path: Path,
) -> None:
    """
    obs/wasm.default.yaml used to ship 'logging.directory' while the code's
    own default named the setting 'logging.file'. An installed config.yaml
    from that packaging is not rewritten on upgrade, so it must keep loading
    without error, and every other setting in it must still read back.
    """
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        "apps_directory: /var/www/apps\nlogging:\n  level: info\n  directory: /var/log/wasm\n",
        encoding="utf-8",
    )

    config = Config()

    assert config.get("logging.level") == "info"
    assert config.get("apps_directory") == "/var/www/apps"
