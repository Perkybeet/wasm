# Copyright (c) 2024-2026 Yago Lopez Prado
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Tests for ``wasm sessions``: active panel logins from the command line.

An operator locked out of the browser but still with shell access - exactly
the moment a stray session is worth revoking - used to have no way to see or
end one. These tests pin the CLI as a client of the exact
:class:`~wasm.web.auth.TokenManager` every ``/api/auth/sessions*`` endpoint
calls, over the same on-disk state.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from wasm.cli.app import cli as root_cli
from wasm.web.auth import STATE_DIR_ENV, SecurityConfig, TokenManager


@pytest.fixture
def state_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """
    Keep the session store inside the test's own directory.

    Args:
        tmp_path: Per-test temporary directory.
        monkeypatch: Patching helper, scoped to the test.

    Returns:
        The state directory the commands will use.
    """
    directory = tmp_path / "state"
    directory.mkdir()
    monkeypatch.setenv(STATE_DIR_ENV, str(directory))
    return directory


@pytest.fixture
def cli_runner() -> CliRunner:
    """Returns: A runner that captures stdout and stderr together."""
    return CliRunner()


@pytest.fixture
def two_sessions(state_dir: Path) -> TokenManager:
    """
    Create two live sessions directly through the manager.

    Args:
        state_dir: The sandboxed state directory.

    Returns:
        The manager the sessions were created through.
    """
    manager = TokenManager(SecurityConfig())
    manager.create_session("203.0.113.10")
    manager.create_session("203.0.113.20")
    return manager


def test_list_reports_no_sessions_on_a_fresh_install(
    cli_runner: CliRunner, state_dir: Path
) -> None:
    result = cli_runner.invoke(root_cli, ["sessions", "list"])

    assert result.exit_code == 0, result.output
    assert "no active sessions" in result.output.lower()


def test_list_shows_every_live_session(cli_runner: CliRunner, two_sessions: TokenManager) -> None:
    result = cli_runner.invoke(root_cli, ["sessions", "list"])

    assert result.exit_code == 0, result.output
    assert "203.0.113.10" in result.output
    assert "203.0.113.20" in result.output


def test_list_as_json_carries_no_forgeable_credential(
    cli_runner: CliRunner, two_sessions: TokenManager
) -> None:
    result = cli_runner.invoke(root_cli, ["sessions", "list", "--json"])

    assert result.exit_code == 0, result.output
    body = json.loads(result.output)
    assert body["active_sessions"] == 2
    for entry in body["sessions"]:
        assert "sid" not in entry
        assert len(entry["sid_prefix"]) < 20


def test_revoke_by_prefix_ends_one_session(
    cli_runner: CliRunner, two_sessions: TokenManager
) -> None:
    (record,) = [
        record for record in two_sessions.list_sessions() if record["client_ip"] == "203.0.113.10"
    ]

    result = cli_runner.invoke(root_cli, ["sessions", "revoke", record["sid_prefix"]])

    assert result.exit_code == 0, result.output
    remaining = {r["client_ip"] for r in two_sessions.list_sessions()}
    assert remaining == {"203.0.113.20"}


def test_revoke_of_an_unknown_prefix_fails_loudly(cli_runner: CliRunner, state_dir: Path) -> None:
    result = cli_runner.invoke(root_cli, ["sessions", "revoke", "deadbeef"])

    assert result.exit_code != 0
    assert "no active session" in result.output.lower()


def test_revoke_others_asks_for_confirmation_unless_forced(
    cli_runner: CliRunner, two_sessions: TokenManager
) -> None:
    declined = cli_runner.invoke(root_cli, ["sessions", "revoke-others"], input="n\n")

    assert declined.exit_code == 0, declined.output
    assert "Cancelled" in declined.output
    assert two_sessions.get_active_session_count() == 2


def test_revoke_others_with_force_ends_every_session(
    cli_runner: CliRunner, two_sessions: TokenManager
) -> None:
    result = cli_runner.invoke(root_cli, ["sessions", "revoke-others", "--force"])

    assert result.exit_code == 0, result.output
    assert two_sessions.get_active_session_count() == 0
