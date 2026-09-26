"""
``--dry-run`` and the console's own security state.

The signing key, the master token hash and the session database are written
by :mod:`wasm.web.auth`, which used to write them with ``os`` and ``sqlite3``
directly - past the filesystem seam ``--dry-run`` swaps out. So
``wasm --dry-run token create ci`` printed "nothing on this machine will be
changed" and then created ``/etc/wasm``, a signing key, a session database and
a live API token. A rehearsal reads the real state when there is one and
writes none of it.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from pathlib import Path

import pytest
from click.testing import CliRunner

from wasm.cli.app import cli as root_cli
from wasm.core.fs import DryRunFileSystem, set_fs
from wasm.web.auth import STATE_DIR_ENV, SecurityConfig, TokenManager


@pytest.fixture
def state_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """
    Point the console's state at a directory that does not exist yet.

    Returns:
        The state directory the commands would use.
    """
    directory = tmp_path / "etc-wasm"
    monkeypatch.setenv(STATE_DIR_ENV, str(directory))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    return directory


@pytest.fixture
def cli_runner() -> CliRunner:
    """Returns: A runner that captures stdout and stderr together."""
    return CliRunner()


def snapshot(directory: Path) -> dict[str, str]:
    """
    Fingerprint every file under a directory.

    Args:
        directory: The directory to fingerprint.

    Returns:
        Relative path to a digest of the content, for every file.
    """
    return {
        str(path.relative_to(directory)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(directory.rglob("*"))
        if path.is_file()
    }


@pytest.fixture
def rehearsal() -> Iterator[DryRunFileSystem]:
    """
    Install the rehearsing filesystem the way ``--dry-run`` does.

    Yields:
        The filesystem, whose ``skipped`` lists what a real run would do.
    """
    fs = DryRunFileSystem()
    set_fs(fs)
    try:
        yield fs
    finally:
        set_fs(None)


@pytest.mark.parametrize(
    "argv",
    [
        ["--dry-run", "token", "create", "ci"],
        ["--dry-run", "token", "list"],
        ["--dry-run", "sessions", "list"],
    ],
)
def test_a_rehearsal_on_a_fresh_machine_creates_nothing(
    cli_runner: CliRunner, state_dir: Path, argv: list[str]
) -> None:
    """Not the directory, not the key, not the database."""
    result = cli_runner.invoke(root_cli, argv)

    assert result.exit_code == 0, result.output
    assert not state_dir.exists(), sorted(p.name for p in state_dir.rglob("*"))


def test_a_rehearsal_over_real_state_changes_none_of_it(
    cli_runner: CliRunner, state_dir: Path
) -> None:
    """It reads what is there, and leaves every byte of it as it was."""
    created = cli_runner.invoke(root_cli, ["token", "create", "ci"])
    assert created.exit_code == 0, created.output
    before = snapshot(state_dir)

    for argv in (
        ["--dry-run", "token", "create", "other"],
        ["--dry-run", "token", "revoke", "1"],
        ["--dry-run", "sessions", "revoke-others", "--force"],
        ["--dry-run", "sessions", "list"],
    ):
        cli_runner.invoke(root_cli, argv)

    assert snapshot(state_dir) == before
    listing = cli_runner.invoke(root_cli, ["token", "list", "--json"])
    assert listing.exit_code == 0, listing.output
    assert '"name": "ci"' in listing.output
    assert '"name": "other"' not in listing.output
    assert '"revoked_at": null' in listing.output


def test_a_rehearsal_still_sees_the_real_tokens(cli_runner: CliRunner, state_dir: Path) -> None:
    """Reading is not a change: a rehearsed listing shows what is really there."""
    assert cli_runner.invoke(root_cli, ["token", "create", "ci"]).exit_code == 0

    listing = cli_runner.invoke(root_cli, ["--dry-run", "token", "list", "--json"])

    assert listing.exit_code == 0, listing.output
    assert '"name": "ci"' in listing.output


def test_rotating_secrets_in_a_rehearsal_rotates_nothing(
    tmp_path: Path, rehearsal: DryRunFileSystem
) -> None:
    """``wasm --dry-run web token --new`` must not retire the token in use."""
    config = SecurityConfig(state_dir=tmp_path / "state")
    set_fs(None)
    real = TokenManager(config)
    token = real.generate_master_token()
    real.sessions.close()
    before = snapshot(tmp_path / "state")

    set_fs(rehearsal)
    rehearsed = TokenManager(config)
    rehearsed.generate_master_token()
    rehearsed.rotate_secrets()
    rehearsed.create_session("10.0.0.1")
    rehearsed.sessions.close()

    assert snapshot(tmp_path / "state") == before
    assert rehearsal.skipped, "a rehearsal says what it would have written"
    set_fs(None)
    after = TokenManager(config)
    assert after.verify_master_token(token)
    after.sessions.close()
