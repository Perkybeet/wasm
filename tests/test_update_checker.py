# Copyright (c) 2024-2026 Yago Lopez Prado
# SPDX-License-Identifier: AGPL-3.0-or-later

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


# The banner. It used to print to stdout after every command, so it landed
# after the JSON document of `wasm ... --json | jq` and broke the parse.


class _Stream:
    """A text stream that reports whether it is a terminal."""

    def __init__(self, tty: bool) -> None:
        self.tty = tty
        self.written: list[str] = []

    def isatty(self) -> bool:
        return self.tty

    def write(self, text: str) -> int:
        self.written.append(text)
        return len(text)

    def flush(self) -> None:
        return None


@pytest.fixture
def pending_update(monkeypatch: pytest.MonkeyPatch) -> None:
    """A finished check that found a newer version, detected without a process."""
    UpdateChecker._update_version = "99.0.0"
    monkeypatch.setattr(
        UpdateChecker, "_detect_installation_method", classmethod(lambda cls: "pip")
    )


def test_the_banner_is_written_to_stderr_never_stdout(
    pending_update: None, capsys: pytest.CaptureFixture[str]
) -> None:
    UpdateChecker.show_update_if_available(timeout=0)

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "New version available: 99.0.0" in captured.err
    assert "pip install --upgrade wasm-cli" in captured.err


@pytest.mark.parametrize(
    ("argv", "stdout_tty", "stderr_tty", "expected"),
    [
        (["app", "list"], True, True, True),
        (["app", "list", "--json"], True, True, False),
        (["--json", "app", "list"], True, True, False),
        (["app", "list"], False, True, False),  # piped: `wasm app list | grep`
        (["app", "list"], True, False, False),  # stderr to a log file
    ],
)
def test_the_banner_is_only_announced_to_a_person(
    monkeypatch: pytest.MonkeyPatch,
    argv: list[str],
    stdout_tty: bool,
    stderr_tty: bool,
    expected: bool,
) -> None:
    monkeypatch.setattr("sys.stdout", _Stream(stdout_tty))
    monkeypatch.setattr("sys.stderr", _Stream(stderr_tty))

    assert UpdateChecker.should_announce(argv) is expected


def test_entrypoint_neither_checks_nor_announces_under_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """--json output must be exactly the document: no request, no banner."""
    from wasm.cli import app

    calls: list[str] = []
    monkeypatch.setattr("sys.argv", ["wasm", "app", "list", "--json"])
    monkeypatch.setattr("sys.stdout", _Stream(True))
    monkeypatch.setattr("sys.stderr", _Stream(True))
    monkeypatch.setattr(app, "main", lambda argv=None: 0)
    monkeypatch.setattr(
        UpdateChecker, "start_background_check", classmethod(lambda cls: calls.append("start"))
    )
    monkeypatch.setattr(
        UpdateChecker,
        "show_update_if_available",
        classmethod(lambda cls, timeout=0.1: calls.append("show")),
    )

    with pytest.raises(SystemExit) as exited:
        app.entrypoint()

    assert exited.value.code == 0
    assert calls == []


def test_entrypoint_checks_and_announces_on_a_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    from wasm.cli import app

    calls: list[str] = []
    monkeypatch.setattr("sys.argv", ["wasm", "app", "list"])
    monkeypatch.setattr("sys.stdout", _Stream(True))
    monkeypatch.setattr("sys.stderr", _Stream(True))
    monkeypatch.setattr(app, "main", lambda argv=None: 0)
    monkeypatch.setattr(
        UpdateChecker, "start_background_check", classmethod(lambda cls: calls.append("start"))
    )
    monkeypatch.setattr(
        UpdateChecker,
        "show_update_if_available",
        classmethod(lambda cls, timeout=0.1: calls.append("show")),
    )

    with pytest.raises(SystemExit):
        app.entrypoint()

    assert calls == ["start", "show"]


def test_a_banner_that_cannot_be_written_is_logged_not_raised(
    pending_update: None, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The reader of `wasm ... | head` went away: the command's exit must not change."""

    class Closed(_Stream):
        def write(self, text: str) -> int:
            raise BrokenPipeError("reader went away")

    monkeypatch.setattr("sys.stderr", Closed(True))

    with caplog.at_level("DEBUG", logger="wasm.core.update_checker"):
        UpdateChecker.show_update_if_available(timeout=0)

    assert "reader went away" in caplog.text


def test_a_programming_error_in_the_banner_is_not_swallowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bare except turned bugs like this one into silence for whole releases (rule 2)."""
    UpdateChecker._update_version = "99.0.0"

    def broken(cls: type[UpdateChecker]) -> str:
        raise AttributeError("no such method")

    monkeypatch.setattr(UpdateChecker, "_detect_installation_method", classmethod(broken))

    with pytest.raises(AttributeError):
        UpdateChecker.show_update_if_available(timeout=0)


def test_an_unreachable_github_is_logged_at_debug(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    import urllib.error
    import urllib.request

    def unreachable(*args: object, **kwargs: object) -> None:
        raise urllib.error.URLError("no route to host")

    monkeypatch.setattr(urllib.request, "urlopen", unreachable)

    with caplog.at_level("DEBUG", logger="wasm.core.update_checker"):
        assert UpdateChecker._fetch_latest_version() is None

    assert "no route to host" in caplog.text


def test_a_cache_that_is_not_an_object_reads_as_no_cache() -> None:
    UpdateChecker.CACHE_FILE.write_text("[1, 2, 3]")

    assert UpdateChecker._read_cache() is None
    assert UpdateChecker._is_cache_valid() is False


def test_a_cache_with_a_non_numeric_timestamp_is_not_valid() -> None:
    UpdateChecker.CACHE_FILE.write_text('{"checked_at": "yesterday"}')

    assert UpdateChecker._is_cache_valid() is False
