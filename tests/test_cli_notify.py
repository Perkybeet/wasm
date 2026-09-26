# Copyright (c) 2024-2026 Yago Lopez Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
Tests for ``wasm notify test``: trying a notification channel from the CLI.

Before this existed, checking a webhook URL was right meant turning
notifications on and waiting for a real event, or clicking "Test" in the
panel - unavailable on a deployment with no panel installed. This command is
a thin front end over :class:`~wasm.core.notifier.Notifier`, the exact class
the settings page's own "Test" button uses.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from wasm.cli.app import cli as root_cli
from wasm.cli.commands import notify as notify_module
from wasm.core.notifier import Notifier


@pytest.fixture
def cli_runner() -> CliRunner:
    """Returns: A runner that captures stdout and stderr together."""
    return CliRunner()


@pytest.fixture
def fake_notifier(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """
    Stand in for :class:`Notifier`, so no real socket is ever opened.

    Args:
        monkeypatch: Patching helper, scoped to the test.

    Returns:
        A dict the test scripts: set "error" to make the channel fail.
    """
    state: dict[str, Any] = {"error": None, "calls": []}

    class FakeNotifier:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            """
            Args:
                *args: Ignored, kept for signature compatibility.
                **kwargs: Ignored, kept for signature compatibility.
            """

        def test_channel(self, name: str) -> str | None:
            """
            Args:
                name: Channel name.

            Returns:
                The scripted error, or None for success.
            """
            state["calls"].append(name)
            return state["error"]

    monkeypatch.setattr(notify_module, "Notifier", FakeNotifier)
    return state


def test_a_successful_test_reports_success(
    cli_runner: CliRunner, fake_notifier: dict[str, Any]
) -> None:
    result = cli_runner.invoke(root_cli, ["notify", "test", "webhook"])

    assert result.exit_code == 0, result.output
    assert "sent through webhook" in result.output
    assert fake_notifier["calls"] == ["webhook"]


def test_a_failed_test_exits_non_zero_with_the_channels_own_words(
    cli_runner: CliRunner, fake_notifier: dict[str, Any]
) -> None:
    fake_notifier["error"] = "connection refused"

    result = cli_runner.invoke(root_cli, ["notify", "test", "slack"])

    assert result.exit_code != 0
    assert "connection refused" in result.output


def test_an_unknown_channel_is_a_usage_error(cli_runner: CliRunner) -> None:
    result = cli_runner.invoke(root_cli, ["notify", "test", "carrier-pigeon"])

    assert result.exit_code == 2
    assert "invalid value" in result.output.lower()


def test_uses_the_real_notifier_class(cli_runner: CliRunner, tmp_path: Path) -> None:
    """Without the fake, the command reaches the real Notifier and its guard."""
    assert notify_module.Notifier is Notifier
