# Copyright (c) 2024-2026 Yago Lopez Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
Tests for ``wasm 2fa``: two-factor authentication from the command line.

Enrolling, confirming, disabling and recovering used to be reachable only
from the panel's settings screen. These tests pin the CLI as a client of the
exact :class:`~wasm.web.auth.TokenManager` every ``/api/auth/2fa/*`` endpoint
calls, over the same on-disk state.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from wasm.cli.app import cli as root_cli
from wasm.core import totp
from wasm.web.auth import STATE_DIR_ENV, SecurityConfig, TokenManager


@pytest.fixture
def state_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """
    Keep the two-factor state inside the test's own directory.

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


def _enroll(cli_runner: CliRunner) -> str:
    """
    Args:
        cli_runner: The Click test runner.

    Returns:
        The secret printed by 'wasm 2fa enroll'.
    """
    result = cli_runner.invoke(root_cli, ["2fa", "enroll"])
    assert result.exit_code == 0, result.output
    return next(
        line.split("Secret:", 1)[1].strip()
        for line in result.output.splitlines()
        if "Secret:" in line
    )


def test_status_reports_disabled_on_a_fresh_install(cli_runner: CliRunner, state_dir: Path) -> None:
    result = cli_runner.invoke(root_cli, ["2fa", "status"])

    assert result.exit_code == 0, result.output
    assert "Enabled: no" in result.output


def test_status_json_reports_the_same_state(cli_runner: CliRunner, state_dir: Path) -> None:
    """The payload carries the same facts the key/value report shows."""
    result = cli_runner.invoke(root_cli, ["2fa", "status", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload == {"enabled": False, "pending": False, "backup_codes_remaining": 0}


def test_status_without_json_still_prints_for_a_human(
    cli_runner: CliRunner, state_dir: Path
) -> None:
    """The default stays human-readable; --json is opt-in."""
    result = cli_runner.invoke(root_cli, ["2fa", "status"])

    assert result.exit_code == 0, result.output
    with pytest.raises(json.JSONDecodeError):
        json.loads(result.output)


def test_enroll_prints_a_secret_and_an_otpauth_uri(cli_runner: CliRunner, state_dir: Path) -> None:
    result = cli_runner.invoke(root_cli, ["2fa", "enroll"])

    assert result.exit_code == 0, result.output
    assert "Secret:" in result.output
    assert "otpauth://totp/" in result.output


def test_confirm_with_a_valid_code_activates_and_prints_backup_codes(
    cli_runner: CliRunner, state_dir: Path
) -> None:
    secret = _enroll(cli_runner)

    result = cli_runner.invoke(root_cli, ["2fa", "confirm", totp.totp_now(secret)])

    assert result.exit_code == 0, result.output
    assert "now enabled" in result.output.lower()
    status = cli_runner.invoke(root_cli, ["2fa", "status"])
    assert "Enabled: yes" in status.output
    assert "Backup codes remaining: 8" in status.output


def test_confirm_with_a_wrong_code_fails_and_leaves_it_disabled(
    cli_runner: CliRunner, state_dir: Path
) -> None:
    _enroll(cli_runner)

    result = cli_runner.invoke(root_cli, ["2fa", "confirm", "000000"])

    assert result.exit_code != 0
    status = cli_runner.invoke(root_cli, ["2fa", "status"])
    assert "Enabled: no" in status.output


def test_disable_with_the_current_code_turns_it_off(cli_runner: CliRunner, state_dir: Path) -> None:
    secret = _enroll(cli_runner)
    cli_runner.invoke(root_cli, ["2fa", "confirm", totp.totp_now(secret)])

    result = cli_runner.invoke(root_cli, ["2fa", "disable", totp.totp_now(secret)])

    assert result.exit_code == 0, result.output
    status = cli_runner.invoke(root_cli, ["2fa", "status"])
    assert "Enabled: no" in status.output


def test_disable_with_a_wrong_code_leaves_it_on(cli_runner: CliRunner, state_dir: Path) -> None:
    secret = _enroll(cli_runner)
    cli_runner.invoke(root_cli, ["2fa", "confirm", totp.totp_now(secret)])

    result = cli_runner.invoke(root_cli, ["2fa", "disable", "000000"])

    assert result.exit_code != 0
    status = cli_runner.invoke(root_cli, ["2fa", "status"])
    assert "Enabled: yes" in status.output


def test_backup_codes_regenerates_and_invalidates_the_old_set(
    cli_runner: CliRunner, state_dir: Path
) -> None:
    secret = _enroll(cli_runner)
    confirmed = cli_runner.invoke(root_cli, ["2fa", "confirm", totp.totp_now(secret)])
    old_codes = [
        line.strip().removeprefix("•").strip()
        for line in confirmed.output.splitlines()
        if line.strip().startswith("•")
    ]

    result = cli_runner.invoke(root_cli, ["2fa", "backup-codes", "--force"])

    assert result.exit_code == 0, result.output
    new_codes = [
        line.strip().removeprefix("•").strip()
        for line in result.output.splitlines()
        if line.strip().startswith("•")
    ]
    assert set(new_codes).isdisjoint(old_codes)

    manager = TokenManager(SecurityConfig())
    assert manager.verify_second_factor(old_codes[0]) is False
    assert manager.verify_second_factor(new_codes[0]) is True


def test_backup_codes_asks_for_confirmation_unless_forced(
    cli_runner: CliRunner, state_dir: Path
) -> None:
    secret = _enroll(cli_runner)
    cli_runner.invoke(root_cli, ["2fa", "confirm", totp.totp_now(secret)])
    before = TokenManager(SecurityConfig()).totp_status()["backup_codes_remaining"]

    declined = cli_runner.invoke(root_cli, ["2fa", "backup-codes"], input="n\n")

    assert declined.exit_code == 0, declined.output
    assert "Cancelled" in declined.output
    assert TokenManager(SecurityConfig()).totp_status()["backup_codes_remaining"] == before


def test_backup_codes_needs_two_factor_enabled(cli_runner: CliRunner, state_dir: Path) -> None:
    result = cli_runner.invoke(root_cli, ["2fa", "backup-codes", "--force"])

    assert result.exit_code != 0
