# Copyright (c) 2024-2026 Yago Lopez Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
Tests for the ``wasm backup`` and ``wasm rollback`` commands.

Nothing here reaches a real server: :class:`BackupManager`,
:class:`RollbackManager` and the scheduler are replaced by recorders. What is
tested is the command surface (every name the frozen contract froze still
answers, a bad value is refused before anything happens) and the call each
command makes.

The commands report through :class:`~wasm.core.logger.Logger`, which binds its
stream when it is built, so the tests hand the command a logger writing into a
buffer through the Click context rather than trying to capture stdout after
the fact.
"""

from __future__ import annotations

import io
import json
from argparse import Namespace
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

import click
import pytest
from click.testing import CliRunner, Result

from wasm.cli.app import Context
from wasm.cli.commands import backup as backup_cmd
from wasm.cli.commands.backup import cli, handle_backup, handle_rollback
from wasm.core.exceptions import BackupError
from wasm.core.logger import Logger
from wasm.managers.backup_manager import BackupMetadata

CONTRACT = json.loads(
    (Path(__file__).parent / "contracts" / "cli_surface.json").read_text(encoding="utf-8")
)

#: Command paths this module owns, as the frozen argparse surface spells them.
OWNED_PATHS = sorted(key for key in CONTRACT if key and key.split(" ")[0] in ("backup", "rollback"))

#: Flags that belong to the root command. A subcommand that declares one of
#: these shadows the value the user set before the subcommand name, which is
#: the defect this migration exists to remove.
GLOBAL_FLAGS = frozenset({"-v", "--verbose", "--dry-run", "--json", "--no-color"})


def _metadata(**overrides: Any) -> BackupMetadata:
    """
    Build backup metadata for a fake manager to return.

    Args:
        **overrides: Fields to override.

    Returns:
        Metadata for a plausible backup.
    """
    fields: dict[str, Any] = {
        "id": "example-com-20260101-000000",
        "domain": "example.com",
        "app_name": "example-com",
        "created_at": datetime.now().isoformat(),
        "size_bytes": 4096,
        "app_type": "nextjs",
        "version": "2.0.0",
        "description": "nightly",
        "includes_env": True,
        "includes_node_modules": False,
    }
    fields.update(overrides)
    return BackupMetadata(**fields)


class FakeBackupManager:
    """A BackupManager that records calls instead of touching the disk."""

    #: Calls made through every instance, as (method, kwargs) pairs.
    calls: list[tuple[str, dict[str, Any]]] = []

    #: Metadata returned by ``get_backup``; None means "no such backup".
    stored: BackupMetadata | None = None

    #: Result returned by ``verify``.
    verify_result: dict[str, Any] = {"valid": True, "errors": [], "warnings": []}

    #: Exception raised by every recorded method, when set.
    failure: Exception | None = None

    def __init__(self, verbose: bool = False) -> None:
        """
        Args:
            verbose: Recorded so a test can assert the flag reached the manager.
        """
        self.verbose = verbose
        type(self).calls.append(("__init__", {"verbose": verbose}))

    def _record(self, method: str, kwargs: dict[str, Any]) -> None:
        """
        Record a call and raise the scripted failure, if any.

        Args:
            method: Method name.
            kwargs: Arguments the caller passed.

        Raises:
            Exception: Whatever ``failure`` is set to.
        """
        type(self).calls.append((method, kwargs))
        failure = type(self).failure
        if failure is not None:
            raise failure

    def create(self, **kwargs: Any) -> BackupMetadata:
        """
        Pretend to create a backup.

        Args:
            **kwargs: Recorded verbatim.

        Returns:
            Metadata for the backup that would have been written.
        """
        self._record("create", kwargs)
        return _metadata()

    def list_backups(self, **kwargs: Any) -> list[BackupMetadata]:
        """
        Pretend to list backups.

        Args:
            **kwargs: Recorded verbatim.

        Returns:
            The stored metadata, if there is any.
        """
        self._record("list_backups", kwargs)
        stored = type(self).stored
        return [stored] if stored else []

    def get_backup(self, backup_id: str) -> BackupMetadata | None:
        """
        Look a backup up.

        Args:
            backup_id: Identifier asked for.

        Returns:
            The stored metadata, or None.
        """
        self._record("get_backup", {"backup_id": backup_id})
        return type(self).stored

    def verify(self, backup_id: str) -> dict[str, Any]:
        """
        Pretend to verify an archive.

        Args:
            backup_id: Identifier asked for.

        Returns:
            The scripted verification result.
        """
        self._record("verify", {"backup_id": backup_id})
        return type(self).verify_result

    def restore(self, **kwargs: Any) -> bool:
        """
        Pretend to restore an application.

        Args:
            **kwargs: Recorded verbatim.

        Returns:
            Always True.
        """
        self._record("restore", kwargs)
        return True

    def delete(self, backup_id: str) -> bool:
        """
        Pretend to delete an archive.

        Args:
            backup_id: Identifier asked for.

        Returns:
            Always True.
        """
        self._record("delete", {"backup_id": backup_id})
        return True

    def get_storage_usage(self) -> dict[str, Any]:
        """
        Report storage usage.

        Returns:
            A usage summary shaped like the real manager's.
        """
        self._record("get_storage_usage", {})
        return {
            "total_size_bytes": 2048,
            "total_backups": 1,
            "by_app": {"example-com": {"size_bytes": 2048, "count": 1}},
        }


class FakeRollbackManager:
    """A RollbackManager that records calls instead of restoring anything."""

    #: Calls made through every instance, as (method, kwargs) pairs.
    calls: list[tuple[str, dict[str, Any]]] = []

    #: Backups ``list_rollback_points`` reports.
    points: list[BackupMetadata] = []

    def __init__(self, verbose: bool = False) -> None:
        """
        Args:
            verbose: Recorded so a test can assert the flag reached the manager.
        """
        self.verbose = verbose

    def list_rollback_points(self, domain: str) -> list[BackupMetadata]:
        """
        List the backups this domain can return to.

        Args:
            domain: Domain asked about.

        Returns:
            The scripted rollback points.
        """
        type(self).calls.append(("list_rollback_points", {"domain": domain}))
        return type(self).points

    def create_pre_deploy_backup(self, **kwargs: Any) -> BackupMetadata:
        """
        Pretend to take the safety backup.

        Args:
            **kwargs: Recorded verbatim.

        Returns:
            Metadata for the safety backup.
        """
        type(self).calls.append(("create_pre_deploy_backup", kwargs))
        return _metadata()

    def rollback(self, **kwargs: Any) -> bool:
        """
        Pretend to roll the application back.

        Args:
            **kwargs: Recorded verbatim.

        Returns:
            Always True.
        """
        type(self).calls.append(("rollback", kwargs))
        return True


class Invoker:
    """
    Runs a command with a logger the test can read back.

    Attributes:
        state: The global state the command sees on the Click context.
    """

    def __init__(self) -> None:
        """Build a runner whose logger writes into a buffer."""
        self._messages = io.StringIO()
        self._runner = CliRunner()
        self._last: Result | None = None
        self.state = Context(_logger=Logger(stream=self._messages))

    def invoke(
        self, argv: list[str], command: click.Command | None = None, **kwargs: Any
    ) -> Result:
        """
        Run a command line.

        Args:
            argv: Arguments, without the program name.
            command: Command to run, defaulting to this module's group.
            **kwargs: Passed to the Click test runner, for example ``input``.

        Returns:
            The Click result.
        """
        self._last = self._runner.invoke(command or cli, argv, obj=self.state, **kwargs)
        return self._last

    @property
    def output(self) -> str:
        """Everything the last invocation printed, prompts included."""
        printed = self._last.output if self._last is not None else ""
        return self._messages.getvalue() + printed


@pytest.fixture
def wasm() -> Invoker:
    """
    Provide a command runner wired to a readable logger.

    Returns:
        The invoker.
    """
    return Invoker()


@pytest.fixture
def manager(monkeypatch: pytest.MonkeyPatch) -> Iterator[type[FakeBackupManager]]:
    """
    Replace BackupManager with a recorder for the duration of a test.

    Args:
        monkeypatch: Patching helper, scoped to the test.

    Yields:
        The fake manager class, for scripting and for asserting on calls.
    """
    FakeBackupManager.calls = []
    FakeBackupManager.stored = _metadata()
    FakeBackupManager.verify_result = {"valid": True, "errors": [], "warnings": []}
    FakeBackupManager.failure = None
    monkeypatch.setattr(backup_cmd, "BackupManager", FakeBackupManager)
    yield FakeBackupManager
    FakeBackupManager.calls = []
    FakeBackupManager.failure = None


@pytest.fixture
def rollbacks(monkeypatch: pytest.MonkeyPatch) -> Iterator[type[FakeRollbackManager]]:
    """
    Replace RollbackManager with a recorder for the duration of a test.

    Args:
        monkeypatch: Patching helper, scoped to the test.

    Yields:
        The fake rollback manager class.
    """
    FakeRollbackManager.calls = []
    FakeRollbackManager.points = [_metadata()]
    monkeypatch.setattr(backup_cmd, "RollbackManager", FakeRollbackManager)
    yield FakeRollbackManager
    FakeRollbackManager.calls = []


def _call(calls: list[tuple[str, dict[str, Any]]], method: str) -> dict[str, Any]:
    """
    Return the arguments of the first call to a method.

    Args:
        calls: Recorded calls.
        method: Method name to look for.

    Returns:
        The keyword arguments of that call.

    Raises:
        AssertionError: If the method was never called.
    """
    for name, kwargs in calls:
        if name == method:
            return kwargs
    raise AssertionError(f"{method} was never called; recorded: {[name for name, _ in calls]}")


def _called(calls: list[tuple[str, dict[str, Any]]], method: str) -> bool:
    """
    Report whether a method was called.

    Args:
        calls: Recorded calls.
        method: Method name to look for.

    Returns:
        True if the method appears in the recorded calls.
    """
    return any(name == method for name, _ in calls)


def _walk(
    command: click.Command, path: tuple[str, ...] = ()
) -> Iterator[tuple[str, click.Command]]:
    """
    Walk a command tree.

    Args:
        command: Root command.
        path: Names leading to it.

    Yields:
        The space-joined path and the command at it.
    """
    yield " ".join(path), command
    if isinstance(command, click.Group):
        ctx = click.Context(command)
        for name in command.list_commands(ctx):
            sub = command.get_command(ctx, name)
            if sub is not None:
                yield from _walk(sub, (*path, name))


# ---------------------------------------------------------------------------
# Surface: every command and alias of the frozen contract still answers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", OWNED_PATHS)
def test_contract_command_exists_and_documents_itself(wasm: Invoker, path: str) -> None:
    """
    Every command of the frozen surface exists and prints help.

    Args:
        wasm: Command runner.
        path: Command path from the contract.
    """
    result = wasm.invoke([*path.split(" "), "--help"])

    assert result.exit_code == 0, wasm.output
    assert f"Usage: cli {path}" in result.output


@pytest.mark.parametrize(
    ("path", "alias"),
    [(path, alias) for path in OWNED_PATHS for alias in CONTRACT[path]["aliases"]],
)
def test_contract_alias_still_resolves(wasm: Invoker, path: str, alias: str) -> None:
    """
    Every alias of the frozen surface reaches the command it always did.

    Args:
        wasm: Command runner.
        path: Command path from the contract.
        alias: Alternative spelling of its last segment.
    """
    parts = path.split(" ")
    if len(parts) == 1:
        pytest.skip("a top-level alias is resolved by the root command, not by this group")

    result = wasm.invoke([*parts[:-1], alias, "--help"])

    assert result.exit_code == 0, wasm.output
    assert f"Usage: cli {path}" in result.output


def test_help_lists_canonical_names_only(wasm: Invoker) -> None:
    """
    The help page shows each action once, not once per spelling.

    Args:
        wasm: Command runner.
    """
    result = wasm.invoke(["backup", "--help"])

    assert result.exit_code == 0
    assert "verify" in result.output
    assert "check" not in result.output


def test_no_command_redeclares_a_global_flag() -> None:
    """No command in this subtree shadows a flag that belongs to the root."""
    offenders: dict[str, list[str]] = {}
    for path, command in _walk(cli):
        declared = {
            spelling
            for param in command.params
            if isinstance(param, click.Option)
            for spelling in param.opts + param.secondary_opts
        }
        shadowed = sorted(declared & GLOBAL_FLAGS)
        if shadowed:
            offenders[path] = shadowed

    assert offenders == {}


# ---------------------------------------------------------------------------
# Validation: bad input is refused before anything is touched
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "argv",
    [
        ["backup", "create"],
        ["backup", "restore"],
        ["backup", "delete"],
        ["backup", "verify"],
        ["backup", "info"],
        ["backup", "schedule", "create"],
        ["backup", "schedule", "delete"],
        ["rollback"],
    ],
)
def test_missing_required_argument_is_a_usage_error(
    wasm: Invoker, manager: type[FakeBackupManager], argv: list[str]
) -> None:
    """
    A missing argument produces a usage error, not a traceback.

    Args:
        wasm: Command runner.
        manager: Fake backup manager, to prove nothing ran.
        argv: Command line missing its required argument.
    """
    result = wasm.invoke(argv)

    assert result.exit_code == 2, wasm.output
    assert "Missing argument" in result.output
    assert manager.calls == []


@pytest.mark.parametrize(
    "argv",
    [
        ["backup", "list", "--limit", "soon"],
        ["backup", "create", "example.com", "--retention-count", "many"],
        ["backup", "create", "example.com", "--retention-days", "forever"],
        ["backup", "create", "example.com", "--redis-method", "sqlite"],
        ["backup", "schedule", "create", "example.com", "--retention-count", "many"],
    ],
)
def test_invalid_value_is_rejected_before_the_manager_is_built(
    wasm: Invoker, manager: type[FakeBackupManager], argv: list[str]
) -> None:
    """
    A value of the wrong type or outside the choices never reaches the manager.

    Args:
        wasm: Command runner.
        manager: Fake backup manager, to prove nothing ran.
        argv: Command line carrying the bad value.
    """
    result = wasm.invoke(argv)

    assert result.exit_code == 2, wasm.output
    assert manager.calls == []


def test_unknown_action_is_a_usage_error(wasm: Invoker) -> None:
    """
    An action that does not exist is refused.

    Args:
        wasm: Command runner.
    """
    result = wasm.invoke(["backup", "compress"])

    assert result.exit_code == 2
    assert "No such command" in result.output


# ---------------------------------------------------------------------------
# Behaviour: each command makes the call it promises
# ---------------------------------------------------------------------------


def test_create_passes_every_option_through(
    wasm: Invoker, manager: type[FakeBackupManager]
) -> None:
    """
    Options the argparse tree declared and then dropped now reach the manager.

    Args:
        wasm: Command runner.
        manager: Fake backup manager.
    """
    result = wasm.invoke(
        [
            "backup",
            "create",
            "example.com",
            "-m",
            "before upgrade",
            "--no-env",
            "--include-node-modules",
            "--include-build",
            "--include-db",
            "--include-docker-volumes",
            "--redis-method",
            "aof",
            "--retention-count",
            "3",
            "--retention-days",
            "14",
            "-t",
            "release, manual",
        ]
    )

    assert result.exit_code == 0, wasm.output
    assert _call(manager.calls, "create") == {
        "domain": "example.com",
        "description": "before upgrade",
        "include_env": False,
        "include_node_modules": True,
        "include_build": True,
        "include_databases": True,
        "include_docker_volumes": True,
        "schemas": None,
        "redis_method": "aof",
        "retention_count": 3,
        "retention_days": 14,
        "tags": ["release", "manual"],
    }


def test_create_defaults_keep_the_archive_small(
    wasm: Invoker, manager: type[FakeBackupManager]
) -> None:
    """
    By default a backup carries the application and its .env, nothing heavier.

    Args:
        wasm: Command runner.
        manager: Fake backup manager.
    """
    result = wasm.invoke(["backup", "new", "example.com"])

    assert result.exit_code == 0, wasm.output
    assert _call(manager.calls, "create") == {
        "domain": "example.com",
        "description": "",
        "include_env": True,
        "include_node_modules": False,
        "include_build": False,
        "include_databases": False,
        "include_docker_volumes": False,
        "schemas": None,
        "redis_method": "rdb",
        "retention_count": None,
        "retention_days": None,
        "tags": [],
    }


def test_create_forwards_repeated_schemas(wasm: Invoker, manager: type[FakeBackupManager]) -> None:
    """
    --schemas is forwarded, so the manager can refuse a partial dump.

    Args:
        wasm: Command runner.
        manager: Fake backup manager.
    """
    result = wasm.invoke(
        ["backup", "create", "example.com", "--schemas", "public", "--schemas", "audit"]
    )

    assert result.exit_code == 0, wasm.output
    assert _call(manager.calls, "create")["schemas"] == ["public", "audit"]


def test_bare_backup_lists(wasm: Invoker, manager: type[FakeBackupManager]) -> None:
    """
    'wasm backup' with no action still lists the backups.

    Args:
        wasm: Command runner.
        manager: Fake backup manager.
    """
    result = wasm.invoke(["backup"])

    assert result.exit_code == 0, wasm.output
    assert _call(manager.calls, "list_backups") == {"domain": None, "tags": None, "limit": None}


def test_list_filters(wasm: Invoker, manager: type[FakeBackupManager]) -> None:
    """
    The domain, tags and limit filters reach the manager.

    Args:
        wasm: Command runner.
        manager: Fake backup manager.
    """
    result = wasm.invoke(["backup", "ls", "example.com", "-t", "nightly", "-n", "5"])

    assert result.exit_code == 0, wasm.output
    assert _call(manager.calls, "list_backups") == {
        "domain": "example.com",
        "tags": ["nightly"],
        "limit": 5,
    }


def test_verify_reports_an_invalid_archive_with_exit_code_one(
    wasm: Invoker, manager: type[FakeBackupManager]
) -> None:
    """
    A corrupt archive fails the command.

    Args:
        wasm: Command runner.
        manager: Fake backup manager.
    """
    manager.verify_result = {"valid": False, "errors": ["checksum mismatch"], "warnings": []}

    result = wasm.invoke(["backup", "check", "example-com-20260101-000000"])

    assert result.exit_code == 1
    assert "checksum mismatch" in wasm.output


def test_info_reports_an_unknown_backup(wasm: Invoker, manager: type[FakeBackupManager]) -> None:
    """
    Asking about a backup that is not there fails cleanly.

    Args:
        wasm: Command runner.
        manager: Fake backup manager.
    """
    manager.stored = None

    result = wasm.invoke(["backup", "show", "nope"])

    assert result.exit_code == 1
    assert "Backup not found: nope" in wasm.output


def test_info_states_what_the_archive_holds(
    wasm: Invoker, manager: type[FakeBackupManager]
) -> None:
    """
    Info reports the archive contents, databases and volumes included.

    Args:
        wasm: Command runner.
        manager: Fake backup manager.
    """
    manager.stored = _metadata(
        includes_databases=True,
        database_backups=[{"engine": "postgres", "name": "app", "size_bytes": 10}],
    )

    result = wasm.invoke(["backup", "info", "example-com-20260101-000000"])

    assert result.exit_code == 0, wasm.output
    assert "Archive contains:" in wasm.output
    assert "postgres/app" in wasm.output


def test_storage_totals_are_human_readable(wasm: Invoker, manager: type[FakeBackupManager]) -> None:
    """
    Storage usage is reported in units an operator reads.

    Args:
        wasm: Command runner.
        manager: Fake backup manager.
    """
    result = wasm.invoke(["backup", "storage"])

    assert result.exit_code == 0, wasm.output
    assert "2.0 KB" in wasm.output


# ---------------------------------------------------------------------------
# Destructive commands name what they are about to destroy
# ---------------------------------------------------------------------------


def test_delete_names_the_backup_and_the_consequence(
    wasm: Invoker, manager: type[FakeBackupManager]
) -> None:
    """
    Declining the prompt leaves the archive alone.

    Args:
        wasm: Command runner.
        manager: Fake backup manager.
    """
    result = wasm.invoke(["backup", "delete", "example-com-20260101-000000"], input="n\n")

    assert result.exit_code == 0, wasm.output
    assert "Permanently delete backup example-com-20260101-000000 of example.com" in result.output
    assert "cannot be recovered" in result.output
    assert "Cancelled" in wasm.output
    assert not _called(manager.calls, "delete")


def test_delete_proceeds_when_confirmed(wasm: Invoker, manager: type[FakeBackupManager]) -> None:
    """
    Accepting the prompt deletes the named backup.

    Args:
        wasm: Command runner.
        manager: Fake backup manager.
    """
    result = wasm.invoke(["backup", "rm", "example-com-20260101-000000"], input="y\n")

    assert result.exit_code == 0, wasm.output
    assert _call(manager.calls, "delete") == {"backup_id": "example-com-20260101-000000"}


def test_delete_force_skips_the_prompt(wasm: Invoker, manager: type[FakeBackupManager]) -> None:
    """
    -y deletes without asking, as scripts expect.

    Args:
        wasm: Command runner.
        manager: Fake backup manager.
    """
    result = wasm.invoke(["backup", "delete", "example-com-20260101-000000", "-y"])

    assert result.exit_code == 0, wasm.output
    assert "Permanently delete" not in wasm.output
    assert _call(manager.calls, "delete") == {"backup_id": "example-com-20260101-000000"}


def test_restore_names_the_target_domain(wasm: Invoker, manager: type[FakeBackupManager]) -> None:
    """
    The restore prompt names the domain that gets overwritten.

    Args:
        wasm: Command runner.
        manager: Fake backup manager.
    """
    result = wasm.invoke(
        ["backup", "restore", "example-com-20260101-000000", "--target-domain", "staging.test"],
        input="n\n",
    )

    assert result.exit_code == 0, wasm.output
    assert "staging.test" in result.output
    assert "overwritten" in result.output
    assert not _called(manager.calls, "restore")


def test_restore_forwards_its_switches(wasm: Invoker, manager: type[FakeBackupManager]) -> None:
    """
    --no-env, --no-verify and --force reach the manager.

    Args:
        wasm: Command runner.
        manager: Fake backup manager.
    """
    result = wasm.invoke(
        ["backup", "restore", "example-com-20260101-000000", "--no-env", "--no-verify", "-f"]
    )

    assert result.exit_code == 0, wasm.output
    assert _call(manager.calls, "restore") == {
        "backup_id": "example-com-20260101-000000",
        "target_domain": None,
        "restore_env": False,
        "verify_checksum": False,
    }
    assert not _called(manager.calls, "verify")


def test_restore_verifies_before_overwriting(
    wasm: Invoker, manager: type[FakeBackupManager]
) -> None:
    """
    A backup that fails verification is not restored.

    Args:
        wasm: Command runner.
        manager: Fake backup manager.
    """
    manager.verify_result = {"valid": False, "errors": ["truncated archive"], "warnings": []}

    result = wasm.invoke(["backup", "restore", "example-com-20260101-000000", "--force"])

    assert result.exit_code == 1
    assert "truncated archive" in wasm.output
    assert not _called(manager.calls, "restore")


# ---------------------------------------------------------------------------
# Schedules
# ---------------------------------------------------------------------------


def test_schedule_create_forwards_retention(wasm: Invoker, monkeypatch: pytest.MonkeyPatch) -> None:
    """
    The schedule and its retention policy reach the scheduler.

    Args:
        wasm: Command runner.
        monkeypatch: Patching helper, scoped to the test.
    """
    from wasm.managers import backup_scheduler

    created: list[Any] = []

    class Recorder:
        """Scheduler that records the schedule it was handed."""

        def __init__(self, verbose: bool = False) -> None:
            """
            Args:
                verbose: Ignored.
            """

        def create_schedule(self, schedule: Any) -> None:
            """
            Record the schedule.

            Args:
                schedule: The schedule that would have been installed.
            """
            created.append(schedule)

    monkeypatch.setattr(backup_scheduler, "BackupScheduler", Recorder)

    result = wasm.invoke(
        [
            "backup",
            "schedule",
            "create",
            "example.com",
            "--schedule",
            "weekly",
            "--retention-count",
            "2",
            "--retention-days",
            "9",
        ]
    )

    assert result.exit_code == 0, wasm.output
    assert created[0].domain == "example.com"
    assert created[0].app_name == "example-com"
    assert created[0].schedule == "weekly"
    assert created[0].retention_count == 2
    assert created[0].retention_days == 9


def test_schedule_list_reports_nothing_scheduled(
    wasm: Invoker, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    An empty schedule list is stated, not left blank.

    Args:
        wasm: Command runner.
        monkeypatch: Patching helper, scoped to the test.
    """
    from wasm.managers import backup_scheduler

    class Empty:
        """Scheduler with nothing installed."""

        def __init__(self, verbose: bool = False) -> None:
            """
            Args:
                verbose: Ignored.
            """

        def list_schedules(self) -> list[dict[str, Any]]:
            """
            Report no schedules.

            Returns:
                An empty list.
            """
            return []

    monkeypatch.setattr(backup_scheduler, "BackupScheduler", Empty)

    result = wasm.invoke(["backup", "schedule", "ls"])

    assert result.exit_code == 0, wasm.output
    assert "No backup schedules found" in wasm.output


def test_schedule_delete_names_the_domain(wasm: Invoker, monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Removing a schedule reaches the scheduler with the domain given.

    Args:
        wasm: Command runner.
        monkeypatch: Patching helper, scoped to the test.
    """
    from wasm.managers import backup_scheduler

    removed: list[str] = []

    class Recorder:
        """Scheduler that records the domain it was asked to forget."""

        def __init__(self, verbose: bool = False) -> None:
            """
            Args:
                verbose: Ignored.
            """

        def remove_schedule(self, domain: str) -> None:
            """
            Record the removal.

            Args:
                domain: Domain whose timer would have been removed.
            """
            removed.append(domain)

    monkeypatch.setattr(backup_scheduler, "BackupScheduler", Recorder)

    result = wasm.invoke(["backup", "schedule", "rm", "example.com"])

    assert result.exit_code == 0, wasm.output
    assert removed == ["example.com"]


# ---------------------------------------------------------------------------
# Rollback
# ---------------------------------------------------------------------------


def test_rollback_uses_the_latest_backup_and_rebuilds(
    wasm: Invoker, rollbacks: type[FakeRollbackManager]
) -> None:
    """
    Without a backup id, rollback takes the most recent one and rebuilds.

    Args:
        wasm: Command runner.
        rollbacks: Fake rollback manager.
    """
    result = wasm.invoke(["rollback", "example.com"])

    assert result.exit_code == 0, wasm.output
    assert _call(rollbacks.calls, "rollback") == {
        "domain": "example.com",
        "backup_id": None,
        "rebuild": True,
    }
    assert _call(rollbacks.calls, "create_pre_deploy_backup")["domain"] == "example.com"


def test_rollback_no_rebuild_and_explicit_backup(
    wasm: Invoker, rollbacks: type[FakeRollbackManager]
) -> None:
    """
    A named backup is used as given, and --no-rebuild is honoured.

    Args:
        wasm: Command runner.
        rollbacks: Fake rollback manager.
    """
    result = wasm.invoke(["rollback", "example.com", "example-com-20260101-000000", "--no-rebuild"])

    assert result.exit_code == 0, wasm.output
    assert _call(rollbacks.calls, "rollback") == {
        "domain": "example.com",
        "backup_id": "example-com-20260101-000000",
        "rebuild": False,
    }


def test_rollback_without_a_backup_to_return_to_fails(
    wasm: Invoker, rollbacks: type[FakeRollbackManager]
) -> None:
    """
    Rolling back an application with no backups fails and says so.

    Args:
        wasm: Command runner.
        rollbacks: Fake rollback manager.
    """
    rollbacks.points = []

    result = wasm.invoke(["rollback", "example.com"])

    assert result.exit_code == 1
    assert "No backups found for example.com" in wasm.output
    assert not _called(rollbacks.calls, "rollback")


# ---------------------------------------------------------------------------
# Global state comes from the root; the argparse path shares the helpers
# ---------------------------------------------------------------------------


def test_json_comes_from_the_root_command(wasm: Invoker, manager: type[FakeBackupManager]) -> None:
    """
    'wasm --json backup list' emits JSON: the flag lives on the root context.

    Args:
        wasm: Command runner.
        manager: Fake backup manager.
    """
    from wasm.cli.app import cli as root

    result = wasm.invoke(["--json", "backup", "list"], command=root)

    assert result.exit_code == 0, wasm.output
    assert json.loads(result.output)[0]["domain"] == "example.com"


def test_verbose_from_the_root_reaches_the_manager(manager: type[FakeBackupManager]) -> None:
    """
    'wasm -v backup storage' builds a verbose manager.

    The state is left without a logger on purpose: the command has to build one
    from the verbosity the root recorded, which is the plumbing under test.

    Args:
        manager: Fake backup manager.
    """
    from wasm.cli.app import cli as root

    state = Context()
    result = CliRunner().invoke(root, ["-v", "backup", "storage"], obj=state)

    assert result.exit_code == 0, result.output
    assert state.verbose is True
    assert _call(manager.calls, "__init__") == {"verbose": True}


def test_root_alias_reaches_the_group(wasm: Invoker, manager: type[FakeBackupManager]) -> None:
    """
    'wasm bak ls' resolves through the root alias and then the local one.

    Args:
        wasm: Command runner.
        manager: Fake backup manager.
    """
    from wasm.cli.app import cli as root

    result = wasm.invoke(["bak", "ls", "example.com"], command=root)

    assert result.exit_code == 0, wasm.output
    assert _call(manager.calls, "list_backups")["domain"] == "example.com"


def test_argparse_handler_shares_the_helpers(manager: type[FakeBackupManager]) -> None:
    """
    The handler wasm.cli.parser still calls runs the same code as the command.

    Args:
        manager: Fake backup manager.
    """
    exit_code = handle_backup(Namespace(action="new", domain="example.com", verbose=False))

    assert exit_code == 0
    assert _call(manager.calls, "create")["domain"] == "example.com"


def test_argparse_handler_resolves_action_aliases(manager: type[FakeBackupManager]) -> None:
    """
    'wasm backup rm' used to reach the handler as an unknown action.

    Args:
        manager: Fake backup manager.
    """
    exit_code = handle_backup(
        Namespace(action="rm", backup_id="example-com-20260101-000000", force=True, verbose=False)
    )

    assert exit_code == 0
    assert _call(manager.calls, "delete") == {"backup_id": "example-com-20260101-000000"}


def test_argparse_rollback_requires_a_domain() -> None:
    """The legacy rollback handler still refuses to run without a domain."""
    assert handle_rollback(Namespace(domain=None, verbose=False)) == 1


def test_failed_backup_exits_one(wasm: Invoker, manager: type[FakeBackupManager]) -> None:
    """
    A manager error becomes an exit code and a message, not a traceback.

    Args:
        wasm: Command runner.
        manager: Fake backup manager.
    """
    manager.failure = BackupError("Application not found: example.com", details="Nothing there.")

    result = wasm.invoke(["backup", "create", "example.com"])

    assert result.exit_code == 1
    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert "Application not found: example.com" in wasm.output
    assert "Nothing there." in wasm.output
