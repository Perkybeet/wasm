# Copyright (c) 2024-2026 Yago Lopez Prado
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Tests for ``wasm token``: API tokens from the command line.

Before this command group existed, an API token could only be issued from the
panel's settings screen - a server with no browser open had no way to mint
one. These tests pin the CLI as a client of the exact
:class:`~wasm.web.auth.TokenManager` the API uses, over the same on-disk
state, so a token minted here works against a running panel.
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
    Keep the token store inside the test's own directory.

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


def test_list_reports_no_tokens_on_a_fresh_install(cli_runner: CliRunner, state_dir: Path) -> None:
    result = cli_runner.invoke(root_cli, ["token", "list"])

    assert result.exit_code == 0, result.output
    assert "no api tokens" in result.output.lower()


def test_create_prints_the_token_exactly_once(cli_runner: CliRunner, state_dir: Path) -> None:
    result = cli_runner.invoke(root_cli, ["token", "create", "ci-script", "--scope", "deploy"])

    assert result.exit_code == 0, result.output
    assert "Token: wasm_" in result.output
    assert "only time" in result.output.lower()


def test_dry_run_create_says_the_token_was_not_saved(
    cli_runner: CliRunner, state_dir: Path
) -> None:
    """
    A dry run opens the session database as a private in-memory copy
    (wasm.web.auth.SessionStore._rehearsal_copy), so the printed token was
    never written to the one WASM actually authenticates against. Printing
    it as if it were real, the way 'this is the only time it is shown' does
    outside a rehearsal, would send an operator off with a credential that
    silently never works.
    """
    result = cli_runner.invoke(root_cli, ["--dry-run", "token", "create", "ci-script"])

    assert result.exit_code == 0, result.output
    assert "Token: wasm_" in result.output
    assert "not saved" in result.output.lower()
    assert "will not authenticate" in result.output.lower()

    listing = cli_runner.invoke(root_cli, ["token", "list"])
    assert "no api tokens" in listing.output.lower()


def test_the_created_token_authenticates_against_the_same_manager(
    cli_runner: CliRunner, state_dir: Path
) -> None:
    """The CLI and the API mint tokens the same manager verifies."""
    result = cli_runner.invoke(root_cli, ["token", "create", "ci-script"])
    printed = next(
        line.split("Token:", 1)[1].strip()
        for line in result.output.splitlines()
        if line.startswith("Token:")
    )

    verified = TokenManager(SecurityConfig()).verify_api_token(printed)

    assert verified is not None
    assert verified["scope"] == "read"


def test_list_as_json_carries_no_token_value(cli_runner: CliRunner, state_dir: Path) -> None:
    cli_runner.invoke(root_cli, ["token", "create", "ci-script", "--scope", "admin"])

    result = cli_runner.invoke(root_cli, ["token", "list", "--json"])

    assert result.exit_code == 0, result.output
    body = json.loads(result.output)
    (record,) = body["tokens"]
    assert record["name"] == "ci-script"
    assert record["scope"] == "admin"
    assert "token" not in record


def test_revoke_needs_confirmation_unless_forced(cli_runner: CliRunner, state_dir: Path) -> None:
    cli_runner.invoke(root_cli, ["token", "create", "ci-script"])

    declined = cli_runner.invoke(root_cli, ["token", "revoke", "1"], input="n\n")
    assert declined.exit_code == 0, declined.output
    assert "Cancelled" in declined.output

    result = cli_runner.invoke(root_cli, ["token", "list", "--json"])
    assert json.loads(result.output)["tokens"][0]["revoked_at"] is None


def test_revoke_with_force_skips_the_prompt_and_revokes(
    cli_runner: CliRunner, state_dir: Path
) -> None:
    cli_runner.invoke(root_cli, ["token", "create", "ci-script"])

    result = cli_runner.invoke(root_cli, ["token", "revoke", "1", "--force"])

    assert result.exit_code == 0, result.output
    assert "Revoked API token: ci-script" in result.output


def test_revoking_an_unknown_id_fails_loudly(cli_runner: CliRunner, state_dir: Path) -> None:
    result = cli_runner.invoke(root_cli, ["token", "revoke", "999", "--force"])

    assert result.exit_code != 0
    assert "No API token with id 999" in result.output


def test_creating_a_duplicate_name_reports_the_managers_own_words(
    cli_runner: CliRunner, state_dir: Path
) -> None:
    cli_runner.invoke(root_cli, ["token", "create", "ci-script"])

    result = cli_runner.invoke(root_cli, ["token", "create", "ci-script"])

    assert result.exit_code != 0
