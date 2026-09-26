# Copyright (c) 2024-2026 Yago Lopez Prado
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Tests for the cron manager.

The manager writes root-owned systemd units and runs systemctl and journalctl,
so what is asserted here is the exact argv it builds and the exact unit files
it writes - through the FakeRunner and a systemd directory inside tmp_path,
never a real process.

The two decisions that define the manager are pinned hardest:

- **No shell.** The command line is split with shlex and written token by
  token, so ``&&`` and a quoted shell script are inert arguments, and an
  operator who wants a shell writes ``/bin/sh -c '...'`` and gets exactly
  that argv.
- **The ownership guard.** A ``wasm-cron-*`` unit somebody wrote by hand
  carries no WASM marker, and nothing here will overwrite, start, delete or
  even list it.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from wasm.core.config import Config
from wasm.core.exceptions import ServiceError, WASMError
from wasm.core.runner import FakeRunner
from wasm.core.store import App, WASMStore
from wasm.managers.cron_manager import (
    CronJob,
    CronManager,
    command_comment,
    read_command_comment,
)

#: What ``systemctl list-unit-files`` prints for one enabled WASM cron timer.
LIST_UNIT_FILES_LINE = "wasm-cron-cleanup.timer enabled enabled\n"

#: What ``systemctl show`` answers about that timer.
SHOW_TIMER_OUTPUT = (
    "TimersCalendar={ OnCalendar=*-*-* 02:00:00 ; next_elapse=Sat 2026-08-15 02:00:00 UTC }\n"
    "LastTriggerUSec=Fri 2026-08-14 02:00:00 UTC\n"
    "NextElapseUSecRealtime=Sat 2026-08-15 02:00:00 UTC\n"
)

#: What ``systemctl show`` answers about the service after a failing run.
SHOW_SERVICE_FAILED = (
    "ExecMainStatus=2\nExecMainExitTimestamp=Fri 2026-08-14 02:00:05 UTC\nResult=exit-code\n"
)

#: A schedule that would append a directive to a root-owned unit file.
INJECTED_CALENDAR = "daily\nOnBootSec=1s"

#: Journal JSON for two runs: an older success and a newer failure. The exit
#: comes from the fields systemd attaches, never from message text.
JOURNAL_OUTPUT = "\n".join(
    [
        '{"__REALTIME_TIMESTAMP": "1700000000000000", "MESSAGE": "cleaning caches",'
        ' "_SYSTEMD_INVOCATION_ID": "aaa"}',
        '{"__REALTIME_TIMESTAMP": "1700000001000000", "MESSAGE": "removed 12 files",'
        ' "_SYSTEMD_INVOCATION_ID": "aaa"}',
        '{"__REALTIME_TIMESTAMP": "1700000002000000", "UNIT": "wasm-cron-cleanup.service",'
        ' "INVOCATION_ID": "aaa", "JOB_RESULT": "done", "JOB_TYPE": "start"}',
        '{"__REALTIME_TIMESTAMP": "1700086400000000", "MESSAGE": "ERROR: disk full",'
        ' "_SYSTEMD_INVOCATION_ID": "bbb"}',
        '{"__REALTIME_TIMESTAMP": "1700086401000000", "UNIT": "wasm-cron-cleanup.service",'
        ' "INVOCATION_ID": "bbb", "EXIT_CODE": "exited", "EXIT_STATUS": "2"}',
    ]
)


@pytest.fixture
def systemd_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """
    Point the manager's unit directory into the sandbox.

    Args:
        tmp_path: Per-test temporary directory.
        monkeypatch: Patching helper, scoped to the test.

    Returns:
        The directory unit files land in.
    """
    path = tmp_path / "systemd"
    path.mkdir()
    monkeypatch.setattr(CronManager, "SYSTEMD_DIR", path)
    return path


@pytest.fixture
def config_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """
    Point the configuration singleton at the sandbox.

    Args:
        tmp_path: Per-test temporary directory.
        monkeypatch: Patching helper, scoped to the test.

    Yields:
        The path configuration is read from. It does not exist, so every key
        answers its default - including ``service_user``.
    """
    path = tmp_path / "etc" / "wasm" / "config.yaml"
    monkeypatch.setattr("wasm.core.config.DEFAULT_CONFIG_PATH", path)
    Config.reset_instance()
    try:
        yield path
    finally:
        Config.reset_instance()


@pytest.fixture
def store(tmp_path: Path) -> Iterator[WASMStore]:
    """
    Give the manager a store of its own, for the app-association tests.

    Args:
        tmp_path: Per-test temporary directory.

    Yields:
        The store the manager reads.
    """
    WASMStore.reset_instance()
    instance = WASMStore(tmp_path / "wasm.db")
    try:
        yield instance
    finally:
        instance.close()
        WASMStore.reset_instance()


def job(**overrides: object) -> CronJob:
    """
    Build a valid job, with fields overridden per test.

    Args:
        **overrides: Fields to replace.

    Returns:
        The job.
    """
    fields: dict = {
        "name": "cleanup",
        "command": "/usr/bin/find /tmp/caches -delete",
        "schedule": "daily",
        "user": "www-data",
        "working_directory": "/srv/caches",
    }
    fields.update(overrides)
    return CronJob(**fields)


def written_units(systemd_dir: Path) -> tuple[Path, Path]:
    """
    Args:
        systemd_dir: The patched unit directory.

    Returns:
        The paths the timer and service units land at.
    """
    return (
        systemd_dir / "wasm-cron-cleanup.timer",
        systemd_dir / "wasm-cron-cleanup.service",
    )


def write_owned_pair(systemd_dir: Path) -> tuple[Path, Path]:
    """
    Write a unit pair the way the manager would, marker included.

    Args:
        systemd_dir: The patched unit directory.

    Returns:
        The timer and service paths.
    """
    timer, service = written_units(systemd_dir)
    timer.write_text("# Generated by WASM\n[Timer]\nOnCalendar=*-*-* 02:00:00\n")
    service.write_text(
        "# Generated by WASM\n# Application: example.com\n[Service]\nUser=www-data\n"
        "WorkingDirectory=/srv/caches\nExecStart=/usr/bin/find /tmp/caches -delete\n"
    )
    return timer, service


# ------------------------------------------------------------------ create


def test_create_writes_both_units_and_enables_the_timer(
    runner: FakeRunner, systemd_dir: Path
) -> None:
    """Create renders the pair through the escaped templates and enables it."""
    CronManager().create_job(job())

    timer, service = written_units(systemd_dir)
    timer_text = timer.read_text()
    service_text = service.read_text()

    assert "Generated by WASM" in timer_text
    assert "OnCalendar=*-*-* 02:00:00" in timer_text
    assert "Persistent=true" in timer_text

    assert "Generated by WASM" in service_text
    assert "Type=oneshot" in service_text
    assert "User=www-data" in service_text
    assert "WorkingDirectory=/srv/caches" in service_text
    assert "ExecStart=/usr/bin/find /tmp/caches -delete" in service_text
    assert "SyslogIdentifier=wasm-cron-cleanup" in service_text

    assert ("systemctl", "daemon-reload") in runner.calls
    assert ("systemctl", "enable", "--now", "wasm-cron-cleanup.timer") in runner.calls


def test_the_command_runs_without_a_shell(runner: FakeRunner, systemd_dir: Path) -> None:
    """Shell metacharacters are inert arguments, not syntax."""
    CronManager().create_job(job(command="/usr/bin/echo done && /bin/rm -rf /"))

    _, service = written_units(systemd_dir)
    # && is one quoted argument handed to echo; nothing will ever run rm.
    assert 'ExecStart=/usr/bin/echo done "&&" /bin/rm -rf /' in service.read_text()


def test_an_explicit_shell_travels_as_one_quoted_argument(
    runner: FakeRunner, systemd_dir: Path
) -> None:
    """The documented escape hatch: /bin/sh -c '...' keeps its script whole."""
    CronManager().create_job(job(command="/bin/sh -c 'echo hello && date'"))

    _, service = written_units(systemd_dir)
    assert 'ExecStart=/bin/sh -c "echo hello && date"' in service.read_text()


def test_percent_is_doubled_so_systemd_does_not_expand_it(
    runner: FakeRunner, systemd_dir: Path
) -> None:
    """A literal % must not become a systemd specifier inside the unit."""
    CronManager().create_job(job(command="/usr/bin/date +%Y-%m-%d"))

    _, service = written_units(systemd_dir)
    assert 'ExecStart=/usr/bin/date "+%%Y-%%m-%%d"' in service.read_text()


def test_dollar_is_doubled_so_the_script_reaches_the_shell_as_typed(
    runner: FakeRunner, systemd_dir: Path
) -> None:
    """systemd substituted $HOME itself, so the shell never saw the variable."""
    CronManager().create_job(job(command="/bin/sh -c 'echo $HOME ${USER}'"))

    _, service = written_units(systemd_dir)
    assert 'ExecStart=/bin/sh -c "echo $$HOME $${USER}"' in service.read_text()


def test_an_unparseable_command_is_refused_and_nothing_is_written(
    runner: FakeRunner, systemd_dir: Path
) -> None:
    """A dangling quote answers with the parser's refusal, not a broken unit."""
    with pytest.raises(ServiceError, match="Cannot parse"):
        CronManager().create_job(job(command="/usr/bin/echo 'unterminated"))

    assert list(systemd_dir.iterdir()) == []
    assert not any(call[:2] == ("systemctl", "enable") for call in runner.calls)


def test_an_empty_command_is_refused(runner: FakeRunner, systemd_dir: Path) -> None:
    """A job with nothing to run is a refusal, not an empty ExecStart."""
    with pytest.raises(ServiceError, match="no command"):
        CronManager().create_job(job(command="   "))
    assert list(systemd_dir.iterdir()) == []


def test_a_bare_program_name_is_resolved_to_an_absolute_path(
    runner: FakeRunner, systemd_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Units carry absolute paths; PATH is resolved once, at write time."""
    monkeypatch.setattr(
        "wasm.managers.cron_manager.shutil.which",
        lambda program: "/usr/bin/certbot" if program == "certbot" else None,
    )

    CronManager().create_job(job(command="certbot renew"))

    _, service = written_units(systemd_dir)
    assert "ExecStart=/usr/bin/certbot renew" in service.read_text()


def test_a_program_that_is_not_installed_is_refused(
    runner: FakeRunner, systemd_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A unit for a missing program would fail silently at 2am instead."""
    monkeypatch.setattr("wasm.managers.cron_manager.shutil.which", lambda program: None)

    with pytest.raises(ServiceError, match="Command not found"):
        CronManager().create_job(job(command="no-such-tool run"))
    assert list(systemd_dir.iterdir()) == []


def test_a_relative_path_is_refused(runner: FakeRunner, systemd_dir: Path) -> None:
    """./run.sh depends on a working directory systemd does not promise."""
    with pytest.raises(ServiceError, match="not absolute"):
        CronManager().create_job(job(command="./run.sh"))
    assert list(systemd_dir.iterdir()) == []


def test_the_calendar_validation_is_the_schedulers_own(
    runner: FakeRunner, systemd_dir: Path
) -> None:
    """An expression that would inject a directive is refused, nothing written."""
    with pytest.raises(ServiceError, match="Invalid cron schedule"):
        CronManager().create_job(job(schedule=INJECTED_CALENDAR))

    assert list(systemd_dir.iterdir()) == []


def test_aliases_expand_through_the_shared_table(runner: FakeRunner, systemd_dir: Path) -> None:
    """weekly means what the backup scheduler means by weekly."""
    CronManager().create_job(job(schedule="weekly"))

    timer, _ = written_units(systemd_dir)
    assert "OnCalendar=Mon *-*-* 02:00:00" in timer.read_text()


def test_the_user_defaults_to_the_configured_service_user(
    runner: FakeRunner, systemd_dir: Path, config_file: Path
) -> None:
    """No user on the form means the service user, never root."""
    CronManager().create_job(job(user=None))

    _, service = written_units(systemd_dir)
    assert "User=www-data" in service.read_text()


def test_an_associated_app_supplies_the_working_directory(
    runner: FakeRunner, systemd_dir: Path, store: WASMStore
) -> None:
    """The app's own directory is the default place its jobs run in."""
    store.create_app(
        App(
            domain="example.com",
            app_type="nextjs",
            source="https://github.com/you/app",
            branch="main",
            port=3000,
            app_path="/var/www/apps/example.com",
            status="running",
        )
    )

    CronManager().create_job(job(working_directory=None, app_domain="example.com"))

    _, service = written_units(systemd_dir)
    text = service.read_text()
    assert "WorkingDirectory=/var/www/apps/example.com" in text
    assert "# Application: example.com" in text


def test_a_releases_app_runs_its_job_in_current_not_the_app_root(
    runner: FakeRunner, systemd_dir: Path, store: WASMStore
) -> None:
    """
    A releases application's root holds releases/ and current/, not the
    running code itself, so a job that defaults to it - rather than
    current/ - sees none of the relative paths its command expects.
    """
    store.create_app(
        App(
            domain="example.com",
            app_type="nextjs",
            source="https://github.com/you/app",
            branch="main",
            port=3000,
            app_path="/var/www/apps/example.com",
            status="running",
            layout="releases",
        )
    )

    CronManager().create_job(job(working_directory=None, app_domain="example.com"))

    _, service = written_units(systemd_dir)
    text = service.read_text()
    assert "WorkingDirectory=/var/www/apps/example.com/current" in text
    assert "# Application: example.com" in text


def test_an_unknown_app_is_refused(runner: FakeRunner, systemd_dir: Path, store: WASMStore) -> None:
    """Associating with a domain nothing is deployed at is an error."""
    with pytest.raises(ServiceError, match="No application"):
        CronManager().create_job(job(app_domain="ghost.example.com"))
    assert list(systemd_dir.iterdir()) == []


# --------------------------------------------------------------- ownership


def test_create_refuses_to_overwrite_a_foreign_unit(runner: FakeRunner, systemd_dir: Path) -> None:
    """A hand-written wasm-cron-* unit is never clobbered."""
    foreign = systemd_dir / "wasm-cron-cleanup.timer"
    foreign.write_text("[Timer]\nOnCalendar=hourly\n")

    with pytest.raises(ServiceError, match="does not manage"):
        CronManager().create_job(job())

    assert foreign.read_text() == "[Timer]\nOnCalendar=hourly\n"


def test_delete_refuses_a_unit_without_the_marker(runner: FakeRunner, systemd_dir: Path) -> None:
    """The guard runs before systemctl is ever asked to stop anything."""
    foreign = systemd_dir / "wasm-cron-cleanup.timer"
    foreign.write_text("[Timer]\nOnCalendar=hourly\n")

    with pytest.raises(ServiceError, match="does not manage"):
        CronManager().delete_job("cleanup")

    assert foreign.exists()
    assert ("systemctl", "stop", "wasm-cron-cleanup.timer") not in runner.calls


def test_run_now_refuses_a_unit_without_the_marker(runner: FakeRunner, systemd_dir: Path) -> None:
    """Run now must not start something WASM did not write."""
    (systemd_dir / "wasm-cron-cleanup.service").write_text("[Service]\nExecStart=/bin/true\n")

    with pytest.raises(ServiceError, match="does not manage"):
        CronManager().run_now("cleanup")

    assert not any(call[:2] == ("systemctl", "start") for call in runner.calls)


def test_actions_on_a_job_that_does_not_exist_are_refused(
    runner: FakeRunner, systemd_dir: Path
) -> None:
    """No unit pair, no action: the error names the missing units."""
    manager = CronManager()
    for action in (manager.delete_job, manager.run_now, manager.enable_job, manager.disable_job):
        with pytest.raises(ServiceError, match="Unknown cron job"):
            action("cleanup")


def test_a_traversal_name_is_refused(runner: FakeRunner, systemd_dir: Path) -> None:
    """A name is an identifier, never a path."""
    with pytest.raises(ServiceError, match="Invalid cron job name"):
        CronManager().delete_job("../../etc/systemd/system/sshd")


# ----------------------------------------------------------------- actions


def test_delete_stops_the_timer_and_removes_both_units(
    runner: FakeRunner, systemd_dir: Path
) -> None:
    """Delete tears down exactly what create built."""
    timer, service = write_owned_pair(systemd_dir)

    CronManager().delete_job("cleanup")

    assert not timer.exists() and not service.exists()
    assert ("systemctl", "stop", "wasm-cron-cleanup.timer") in runner.calls
    assert ("systemctl", "disable", "wasm-cron-cleanup.timer") in runner.calls
    assert ("systemctl", "daemon-reload") in runner.calls


def test_run_now_starts_the_service_unit(runner: FakeRunner, systemd_dir: Path) -> None:
    """Run now is one systemctl start of the service, not the timer."""
    write_owned_pair(systemd_dir)

    unit = CronManager().run_now("cleanup")

    assert unit == "wasm-cron-cleanup.service"
    assert ("systemctl", "start", "--no-block", "wasm-cron-cleanup.service") in runner.calls


def test_enable_and_disable_drive_the_timer(runner: FakeRunner, systemd_dir: Path) -> None:
    """Enable and disable act on the timer and keep the unit files."""
    timer, service = write_owned_pair(systemd_dir)
    manager = CronManager()

    manager.enable_job("cleanup")
    manager.disable_job("cleanup")

    assert ("systemctl", "enable", "--now", "wasm-cron-cleanup.timer") in runner.calls
    assert ("systemctl", "disable", "--now", "wasm-cron-cleanup.timer") in runner.calls
    assert timer.exists() and service.exists()


def test_a_refused_systemctl_start_surfaces_systemds_words(
    runner: FakeRunner, systemd_dir: Path
) -> None:
    """systemd's own refusal travels verbatim in the error details."""
    write_owned_pair(systemd_dir)
    runner.script(
        ("systemctl", "start"),
        exit_code=1,
        stderr="Failed to start wasm-cron-cleanup.service: Unit not found.",
    )

    with pytest.raises(ServiceError) as caught:
        CronManager().run_now("cleanup")

    assert "Unit not found" in (caught.value.details or "")


# ----------------------------------------------------------------- listing


def test_list_reads_unit_files_shows_and_the_units_own_writing(
    runner: FakeRunner, systemd_dir: Path
) -> None:
    """One entry per timer: schedule, run times, last exit and the command."""
    write_owned_pair(systemd_dir)
    runner.script(("systemctl", "list-unit-files"), stdout=LIST_UNIT_FILES_LINE)
    runner.script(("systemctl", "show", "wasm-cron-cleanup.timer"), stdout=SHOW_TIMER_OUTPUT)
    runner.script(("systemctl", "show", "wasm-cron-cleanup.service"), stdout=SHOW_SERVICE_FAILED)

    jobs = CronManager().list_jobs()

    assert len(jobs) == 1
    entry = jobs[0]
    assert entry["name"] == "cleanup"
    assert entry["enabled"] is True
    assert entry["on_calendar"] == "*-*-* 02:00:00"
    assert entry["next_run"] == "Sat 2026-08-15 02:00:00 UTC"
    assert entry["last_run"] == "Fri 2026-08-14 02:00:00 UTC"
    assert entry["last_exit_code"] == 2
    assert entry["last_result"] == "exit-code"
    assert entry["command"] == "/usr/bin/find /tmp/caches -delete"
    assert entry["user"] == "www-data"
    assert entry["working_directory"] == "/srv/caches"
    assert entry["app_domain"] == "example.com"


def test_a_job_that_never_ran_reports_no_exit_code(runner: FakeRunner, systemd_dir: Path) -> None:
    """ExecMainStatus is 0 before any run; the exit code must not lie."""
    write_owned_pair(systemd_dir)
    runner.script(("systemctl", "list-unit-files"), stdout=LIST_UNIT_FILES_LINE)
    runner.script(
        ("systemctl", "show", "wasm-cron-cleanup.service"),
        stdout="ExecMainStatus=0\nExecMainExitTimestamp=\nResult=success\n",
    )

    entry = CronManager().list_jobs()[0]

    assert entry["last_exit_code"] is None
    assert entry["last_result"] == "never ran"


def test_list_skips_a_foreign_wasm_cron_unit(runner: FakeRunner, systemd_dir: Path) -> None:
    """A hand-written wasm-cron-* timer is not presented as manageable."""
    (systemd_dir / "wasm-cron-handmade.timer").write_text("[Timer]\nOnCalendar=hourly\n")
    runner.script(
        ("systemctl", "list-unit-files"),
        stdout="wasm-cron-handmade.timer enabled enabled\n",
    )

    assert CronManager().list_jobs() == []


def test_get_job_answers_none_for_the_unknown(runner: FakeRunner, systemd_dir: Path) -> None:
    """No owned unit pair means None, not an invented entry."""
    assert CronManager().get_job("cleanup") is None


# ----------------------------------------------------------------- history


def test_runs_groups_the_journal_by_invocation(runner: FakeRunner, systemd_dir: Path) -> None:
    """Two invocations become two runs, newest first, exits from the fields."""
    write_owned_pair(systemd_dir)
    runner.script(("journalctl",), stdout=JOURNAL_OUTPUT)

    runs = CronManager().runs("cleanup")

    assert [
        ("journalctl", "-u", "wasm-cron-cleanup.service", "-o", "json", "-n", "500", "--no-pager")
    ] == [call for call in runner.calls if call[0] == "journalctl"]

    assert len(runs) == 2
    newest, oldest = runs
    assert newest["exit_code"] == 2
    assert newest["success"] is False
    assert newest["lines"] == ["ERROR: disk full"]
    assert oldest["exit_code"] == 0
    assert oldest["success"] is True
    assert oldest["lines"] == ["cleaning caches", "removed 12 files"]
    assert oldest["started"] == "2023-11-14 22:13:20 UTC"


def test_runs_refuses_a_foreign_unit(runner: FakeRunner, systemd_dir: Path) -> None:
    """History is guarded too: reading a foreign unit's journal is refused."""
    (systemd_dir / "wasm-cron-cleanup.timer").write_text("[Timer]\n")

    with pytest.raises(ServiceError, match="does not manage"):
        CronManager().runs("cleanup")


def test_runs_tolerates_journal_noise(runner: FakeRunner, systemd_dir: Path) -> None:
    """A non-JSON line or an entry without invocation data is skipped."""
    write_owned_pair(systemd_dir)
    runner.script(
        ("journalctl",),
        stdout='not json\n{"MESSAGE": "orphan line"}\n' + JOURNAL_OUTPUT,
    )

    assert len(CronManager().runs("cleanup")) == 2


# ------------------------------------------------------ editing round trip

#: Commands whose ExecStart form differs from what the operator typed: every
#: character build_exec_start escapes, quoting of both kinds, and spacing
#: that only survives inside quotes.
ROUND_TRIP_COMMANDS = [
    "/bin/sh -c 'echo $HOME ${USER} $$'",
    "/usr/bin/date +%Y-%m-%d",
    "/bin/sh -c 'echo 100%% done'",
    "/usr/bin/printf '%s\\n' \"it's quoted\" 'say \"hi\"'",
    "/usr/bin/echo 'two  spaces'   \"and  more\"",
    "/usr/bin/echo back\\\\slash 'tab\\there'",
    "/usr/bin/echo héllo wörld",
]


@pytest.mark.parametrize("command", ROUND_TRIP_COMMANDS)
def test_editing_a_job_leaves_its_command_as_typed(
    runner: FakeRunner, systemd_dir: Path, command: str
) -> None:
    """
    Create, read back, save again: the unit must not change by one byte.

    The console's edit dialog sends back the command the listing gave it.
    When that was the escaped ExecStart, every save doubled each ``$`` and
    ``%`` again, so ``$HOME`` became ``$$HOME`` and then ``$$$$HOME`` - the
    shell's PID - and the job silently ran something else.
    """
    manager = CronManager()
    manager.create_job(job(command=command))
    _, service = written_units(systemd_dir)
    first = service.read_bytes()

    entry = manager.get_job("cleanup")
    assert entry is not None
    assert entry["command"] == command

    manager.create_job(job(command=entry["command"]))
    assert service.read_bytes() == first

    again = manager.get_job("cleanup")
    assert again is not None
    assert again["command"] == command


def test_the_listing_reads_the_command_the_operator_typed(
    runner: FakeRunner, systemd_dir: Path
) -> None:
    """list_jobs answers the raw command, not the escaped ExecStart."""
    CronManager().create_job(job(command="/bin/sh -c 'echo $HOME 50%'"))
    runner.script(("systemctl", "list-unit-files"), stdout=LIST_UNIT_FILES_LINE)

    entry = CronManager().list_jobs()[0]

    assert entry["command"] == "/bin/sh -c 'echo $HOME 50%'"
    _, service = written_units(systemd_dir)
    assert 'ExecStart=/bin/sh -c "echo $$HOME 50%%"' in service.read_text()


def test_the_raw_command_is_recorded_as_one_quoted_comment_line(
    runner: FakeRunner, systemd_dir: Path
) -> None:
    """The record is a comment systemd ignores, JSON-quoted so it always ends in a quote."""
    CronManager().create_job(job(command="/bin/sh -c 'echo \"$HOME\"'"))

    _, service = written_units(systemd_dir)
    lines = service.read_text().split("\n")
    recorded = [line for line in lines if line.startswith("# Command: ")]
    assert recorded == ['# Command: "/bin/sh -c \'echo \\"$HOME\\"\'"']


def test_a_command_is_stored_without_surrounding_whitespace(
    runner: FakeRunner, systemd_dir: Path
) -> None:
    """The dialog trims before saving; the record must agree with it so a save is a no-op."""
    manager = CronManager()
    manager.create_job(job(command="  /usr/bin/date +%s  "))
    _, service = written_units(systemd_dir)
    first = service.read_bytes()

    manager.create_job(job(command="/usr/bin/date +%s"))

    assert service.read_bytes() == first
    entry = manager.get_job("cleanup")
    assert entry is not None
    assert entry["command"] == "/usr/bin/date +%s"


@pytest.mark.parametrize(
    "command",
    [
        "/usr/bin/echo hi\nExecStartPre=/bin/sh -c evil",
        "/usr/bin/echo hi\rExecStartPre=/bin/sh -c evil",
        "/usr/bin/echo hi\x00",
        "/usr/bin/echo hi\x0bthere",
        "/usr/bin/echo hi\x7f",
    ],
)
def test_a_command_with_a_control_character_is_refused_before_anything_is_written(
    runner: FakeRunner, systemd_dir: Path, command: str
) -> None:
    """The comment line can only be one line: a line break never reaches it."""
    with pytest.raises(WASMError, match=r"contains a"):
        CronManager().create_job(job(command=command))

    assert list(systemd_dir.iterdir()) == []


@pytest.mark.parametrize(
    "raw",
    [
        "/usr/bin/echo hi\nExecStartPre=/bin/evil",
        "/usr/bin/echo hi\r[Service]",
        "/usr/bin/echo trailing\\",
        "/usr/bin/echo \u2028 separators \u2029 \x85",
    ],
)
def test_the_command_comment_cannot_become_a_directive_even_unvalidated(raw: str) -> None:
    """
    Escaping is the last line of defence, independent of validation.

    Whatever the value, the record is one line, starts with ``#`` and does
    not end in a lone backslash - which systemd would read as a continuation
    swallowing the next line into the comment - and it decodes back exactly.
    """
    line = command_comment(raw)

    assert "\n" not in line
    assert "\r" not in line
    assert line.startswith("# Command: ")
    assert line.endswith('"')
    assert read_command_comment(line) == raw


# ------------------------------------------------ units written before 2.1


@pytest.mark.parametrize(
    ("exec_start", "expected"),
    [
        ('/bin/sh -c "echo $$HOME $${USER}"', "/bin/sh -c 'echo $HOME ${USER}'"),
        ('/usr/bin/date "+%%Y-%%m-%%d"', "/usr/bin/date +%Y-%m-%d"),
        ('/bin/sh -c "echo \\"hi\\" && date"', "/bin/sh -c 'echo \"hi\" && date'"),
        (
            '/usr/bin/echo "two  spaces" "back\\\\slash"',
            "/usr/bin/echo 'two  spaces' 'back\\slash'",
        ),
        ("/usr/bin/find /tmp/caches -delete", "/usr/bin/find /tmp/caches -delete"),
    ],
)
def test_a_unit_without_the_record_reads_back_unescaped(
    runner: FakeRunner, systemd_dir: Path, exec_start: str, expected: str
) -> None:
    """
    Before the record existed only ExecStart was written. Its systemd escaping
    is undone - ``$$`` and ``%%`` halved, the quoting stripped - so the first
    edit of an old job saves what it ran, not a doubled copy of it.
    """
    timer, service = written_units(systemd_dir)
    timer.write_text("# Generated by WASM\n[Timer]\nOnCalendar=*-*-* 02:00:00\n")
    service.write_text(f"# Generated by WASM\n[Service]\nUser=www-data\nExecStart={exec_start}\n")

    manager = CronManager()
    entry = manager.get_job("cleanup")
    assert entry is not None
    assert entry["command"] == expected

    manager.create_job(job(command=entry["command"]))
    assert f"ExecStart={exec_start}\n" in service.read_text()


def test_a_hand_edited_exec_start_wins_over_a_stale_record(
    runner: FakeRunner, systemd_dir: Path
) -> None:
    """The console shows what runs: a record that no longer matches ExecStart is ignored."""
    CronManager().create_job(job(command="/usr/bin/date +%s"))
    _, service = written_units(systemd_dir)
    service.write_text(
        service.read_text().replace(
            'ExecStart=/usr/bin/date "+%%s"', 'ExecStart=/usr/bin/date "+%%F"'
        )
    )

    entry = CronManager().get_job("cleanup")

    assert entry is not None
    assert entry["command"] == "/usr/bin/date +%F"


def test_a_record_naming_a_program_resolved_on_path_still_matches(
    runner: FakeRunner, systemd_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bare program name becomes an absolute path in ExecStart; the record keeps the name."""
    monkeypatch.setattr(
        "wasm.managers.cron_manager.shutil.which",
        lambda program: f"/usr/bin/{program}",
    )
    CronManager().create_job(job(command="php artisan schedule:run"))

    entry = CronManager().get_job("cleanup")

    assert entry is not None
    assert entry["command"] == "php artisan schedule:run"
