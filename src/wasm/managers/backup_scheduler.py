# Copyright (c) 2024-2025 Yago López Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
Backup scheduler for WASM.

Creates and manages systemd timers for automated application backups.

There used to be two renderers here: the Jinja one, which goes through the
escaped templates, and an f-string fallback twenty lines below it that
interpolated the domain and the calendar expression verbatim. A domain
containing a newline turned the second one into arbitrary directives in a
root-owned unit file, which is the exact injection the templates were fixed for.
Only the Jinja renderer survives, and the values it receives are validated
before they reach it.

Unit files are written and removed through :mod:`wasm.core.fs`, for the same
reason systemctl goes through the runner: ``wasm --dry-run backup schedule
delete`` used to unlink two root-owned unit files while announcing that it would
change nothing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from jinja2 import Environment, PackageLoader, TemplateError

from wasm.core.config import SYSTEMD_DIR as _SYSTEMD_DIR
from wasm.core.exceptions import BackupError, WASMError
from wasm.core.fs import FileSystem, get_fs
from wasm.core.logger import Logger
from wasm.core.runner import CommandRunner, get_runner
from wasm.core.utils import domain_to_app_name
from wasm.validators.domain import validate_domain
from wasm.validators.names import resolve_within, validate_app_name, validate_service_name

#: Deadline for a systemctl verb. Nothing here talks to the network.
_SYSTEMCTL_TIMEOUT = 60

#: Unit files are world readable and root writable, like every other unit.
_UNIT_MODE = 0o644

#: Longest calendar expression WASM accepts. systemd's own parser is stricter
#: still; this only bounds what reaches it.
_MAX_CALENDAR_LENGTH = 128

#: Characters a systemd calendar expression is built from. Anything else -
#: newlines above all - would let a schedule append a directive to the unit.
_CALENDAR_ALPHABET = set("0123456789*-/,:. ")

# Schedule aliases
SCHEDULE_ALIASES = {
    "hourly": "*-*-* *:00:00",
    "daily": "*-*-* 02:00:00",
    "weekly": "Mon *-*-* 02:00:00",
    "monthly": "*-*-01 02:00:00",
}

#: How a timer unit's description names the application it backs up. The
#: description is rendered by this module's own template, so reading the
#: domain back out of it is reading this module's own writing.
_DESCRIPTION_PREFIX = "WASM backup timer for "

#: The ``OnCalendar`` expression inside systemd's ``TimersCalendar`` property,
#: which prints as ``{ OnCalendar=*-*-* 02:00:00 ; next_elapse=... }``.
_ON_CALENDAR = re.compile(r"OnCalendar=([^;}]+)")


def validate_calendar(schedule: str) -> str:
    """
    Expand a schedule alias and refuse anything a unit file must not contain.

    This is the single definition of what WASM will write after ``OnCalendar=``
    in a root-owned unit: :meth:`BackupScheduler._validate` and the web API's
    request model both call it, so the panel cannot accept an expression the
    scheduler would refuse.

    Args:
        schedule: Alias from :data:`SCHEDULE_ALIASES` or a systemd calendar
            expression.

    Returns:
        The expansion of the alias, or the expression itself.

    Raises:
        BackupError: If the expression is empty, too long, or contains a
            character that could become a systemd directive.
    """
    calendar = SCHEDULE_ALIASES.get(schedule, schedule)
    if not calendar or len(calendar) > _MAX_CALENDAR_LENGTH:
        raise BackupError(
            "Backup schedule is empty or too long",
            details=f"Use an alias ({', '.join(SCHEDULE_ALIASES)}) or a calendar expression.",
        )
    # Weekday prefixes such as "Mon" are the only letters a calendar
    # expression needs, so letters are allowed but nothing else is.
    if any(not (char.isalpha() or char in _CALENDAR_ALPHABET) for char in calendar):
        raise BackupError(
            f"Invalid backup schedule: {calendar!r}",
            details="A calendar expression may only contain letters, digits and '*-/,:. '.",
        )
    return calendar


@dataclass
class BackupSchedule:
    """
    Configuration for a scheduled backup.

    Attributes:
        domain: Domain of the application to back up.
        app_name: Application name, used to build the unit names.
        schedule: Alias from :data:`SCHEDULE_ALIASES` or a systemd calendar
            expression.
        include_databases: Ask the scheduled run for database dumps.
        retention_count: Backups to keep.
        retention_days: Maximum age of a backup, in days.
        tags: Tags attached to the backups this schedule creates.
    """

    domain: str
    app_name: str
    schedule: str
    include_databases: bool = True
    retention_count: int = 7
    retention_days: int = 30
    tags: list[str] = field(default_factory=lambda: ["scheduled", "auto"])

    @property
    def on_calendar(self) -> str:
        """
        Return the schedule as a systemd ``OnCalendar`` expression.

        Returns:
            The expansion of the alias, or the expression itself.
        """
        return SCHEDULE_ALIASES.get(self.schedule, self.schedule)

    @property
    def timer_name(self) -> str:
        """
        Return the systemd timer unit name.

        Returns:
            The unit name without its ``.timer`` suffix.
        """
        return f"wasm-backup-{self.app_name}"

    @property
    def service_name(self) -> str:
        """
        Return the systemd service unit name.

        Returns:
            The unit name without its ``.service`` suffix.
        """
        return f"wasm-backup-{self.app_name}"


class BackupScheduler:
    """
    Manager for scheduled backups using systemd timers.

    Creates timer/service unit pairs that trigger ``wasm backup create`` on a
    configurable schedule. WASM requires root, so unit files are written
    directly and systemctl is invoked without sudo.
    """

    SYSTEMD_DIR = _SYSTEMD_DIR

    def __init__(
        self,
        verbose: bool = False,
        runner: CommandRunner | None = None,
        fs: FileSystem | None = None,
    ) -> None:
        """
        Initialize the scheduler.

        Args:
            verbose: Enable verbose logging.
            runner: Command runner used for systemctl. Defaults to the
                process-wide runner.
            fs: Filesystem used to write and remove unit files. Defaults to the
                process-wide one, so ``--dry-run`` reaches the unit files too.
        """
        self.verbose = verbose
        self.logger = Logger(verbose=verbose)
        self._runner = runner
        self._fs = fs
        self.jinja_env = Environment(
            loader=PackageLoader("wasm", "templates/systemd"),
            trim_blocks=True,
            lstrip_blocks=True,
            # Systemd units are not markup: HTML escaping would corrupt them.
            # The templates escape what matters through the _escape.j2 macros.
            autoescape=False,  # noqa: S701 - systemd unit files, not markup
        )

    @property
    def runner(self) -> CommandRunner:
        """
        Return the command runner used for every external command.

        Returns:
            The injected runner, or the process-wide one.
        """
        return self._runner or get_runner()

    @property
    def fs(self) -> FileSystem:
        """
        Return the filesystem used for every unit file this scheduler owns.

        Returns:
            The injected filesystem, or the process-wide one.
        """
        return self._fs or get_fs()

    def _systemctl(self, *args: str) -> Any:
        """
        Run a systemctl verb through the shared runner.

        Args:
            args: Arguments after the ``systemctl`` program name.

        Returns:
            The command outcome.
        """
        return self.runner.run(["systemctl", *args], timeout=_SYSTEMCTL_TIMEOUT)

    def _validate(self, schedule: BackupSchedule) -> tuple[str, str, str]:
        """
        Check every value that is about to be written into a unit file.

        Args:
            schedule: The schedule to validate.

        Returns:
            The normalised domain, application name and calendar expression.

        Raises:
            BackupError: If the domain, the application name or the calendar
                expression is not something WASM is willing to write into a
                root-owned unit file.
        """
        try:
            domain = validate_domain(schedule.domain)
            app_name = validate_app_name(schedule.app_name or domain_to_app_name(domain))
            validate_service_name(f"wasm-backup-{app_name}")
        except WASMError as exc:
            raise BackupError(
                "Cannot schedule a backup for this application",
                details=f"{exc}. A unit file is generated from these values, so they are "
                "restricted to characters that cannot become a systemd directive.",
            ) from exc

        return domain, app_name, validate_calendar(schedule.schedule)

    def _unit_path(self, unit_name: str) -> Path:
        """
        Resolve a unit file name inside the systemd directory.

        Args:
            unit_name: File name including its suffix.

        Returns:
            The path to write, guaranteed to stay under
            :attr:`SYSTEMD_DIR`.

        Raises:
            SecurityError: If the name escapes the systemd directory.
        """
        return resolve_within(self.SYSTEMD_DIR, unit_name)

    def _write_unit(self, path: Path, content: str) -> None:
        """
        Write a unit file as root.

        Args:
            path: Destination unit file.
            content: Rendered unit content.

        Raises:
            BackupError: If the file cannot be written.
        """
        try:
            self.fs.write_text(path, content, mode=_UNIT_MODE)
        except OSError as exc:
            raise BackupError(
                f"Failed to write unit file: {path}",
                details=f"{exc}. WASM must run as root to manage systemd units.",
            ) from exc

    def create_schedule(self, schedule: BackupSchedule) -> bool:
        """
        Create a backup schedule using a systemd timer and service pair.

        Args:
            schedule: Backup schedule configuration.

        Returns:
            True if the schedule was created.

        Raises:
            BackupError: If a value is unusable, a unit cannot be written or
                the timer cannot be enabled.
        """
        domain, app_name, calendar = self._validate(schedule)
        checked = BackupSchedule(
            domain=domain,
            app_name=app_name,
            schedule=calendar,
            include_databases=schedule.include_databases,
            retention_count=schedule.retention_count,
            retention_days=schedule.retention_days,
            tags=list(schedule.tags),
        )

        timer_path = self._unit_path(f"{checked.timer_name}.timer")
        service_path = self._unit_path(f"{checked.service_name}.service")

        self._write_unit(timer_path, self.render_timer(checked))
        self._write_unit(service_path, self.render_service(checked))

        self._systemctl("daemon-reload")

        result = self._systemctl("enable", "--now", f"{checked.timer_name}.timer")
        if not result.success:
            raise BackupError(
                f"Failed to enable timer: {checked.timer_name}.timer",
                details=result.stderr.strip() or "Check 'systemctl status' for details.",
            )

        self.logger.info(f"Created backup schedule: {checked.timer_name}")
        self.logger.info(f"  Schedule: {checked.on_calendar}")
        return True

    def remove_schedule(self, domain: str) -> bool:
        """
        Remove a backup schedule.

        Args:
            domain: Application domain.

        Returns:
            True if the schedule was removed.

        Raises:
            BackupError: If the domain does not yield a usable unit name, so a
                crafted domain cannot make this delete an unrelated unit.
        """
        _, app_name, _ = self._validate(
            BackupSchedule(domain=domain, app_name=domain_to_app_name(domain), schedule="daily")
        )
        timer_name = f"wasm-backup-{app_name}"

        self._systemctl("stop", f"{timer_name}.timer")
        self._systemctl("disable", f"{timer_name}.timer")

        for suffix in (".timer", ".service"):
            path = self._unit_path(f"{timer_name}{suffix}")
            try:
                self.fs.remove(path)
            except OSError as exc:
                self.logger.warning(f"Could not remove {path}: {exc}")

        self._systemctl("daemon-reload")

        self.logger.info(f"Removed backup schedule: {timer_name}")
        return True

    def list_schedules(self) -> list[dict[str, str]]:
        """
        List all WASM backup schedules.

        ``list-timers`` only finds the units; every value comes from
        ``systemctl show``, whose ``Property=value`` lines are a stable
        contract. The human columns of ``list-timers`` are not: they shift
        with locale and systemd version, and splitting them on whitespace is
        how the next run used to be reported as the word "Sat".

        Returns:
            One dictionary per timer: ``timer`` and ``app_name`` from the unit
            name, ``domain`` read back from the unit's own description,
            ``next_run`` and ``last_run`` as systemd prints them (``pending``
            and ``never`` when it prints nothing), plus ``schedule`` (the raw
            ``TimersCalendar`` property) and ``on_calendar`` (the expression
            inside it) when the unit can be inspected.
        """
        result = self._systemctl(
            "list-timers",
            "--no-legend",
            "--no-pager",
            "wasm-backup-*",
        )

        schedules: list[dict[str, str]] = []
        if not result.success:
            return schedules

        for line in result.stdout.strip().splitlines():
            # The columns vary; the one fact safely in the line is the unit
            # name, so it is found by its suffix rather than by position.
            unit = next((part for part in line.split() if part.endswith(".timer")), None)
            if unit is None:
                continue

            timer_name = unit[: -len(".timer")]
            app_name = timer_name.replace("wasm-backup-", "")
            schedule_info: dict[str, str] = {
                "timer": timer_name,
                "app_name": app_name,
                "domain": app_name,
                "next_run": "pending",
                "last_run": "never",
            }

            detail = self._systemctl(
                "show",
                unit,
                "--property=Description,TimersCalendar,LastTriggerUSec,NextElapseUSecRealtime",
            )
            if detail.success:
                properties: dict[str, str] = {}
                for prop_line in detail.stdout.splitlines():
                    key, separator, value = prop_line.partition("=")
                    if separator:
                        properties[key] = value.strip()

                calendar = properties.get("TimersCalendar", "")
                if calendar:
                    schedule_info["schedule"] = calendar
                    match = _ON_CALENDAR.search(calendar)
                    if match:
                        schedule_info["on_calendar"] = match.group(1).strip()

                description = properties.get("Description", "")
                if description.startswith(_DESCRIPTION_PREFIX):
                    schedule_info["domain"] = description[len(_DESCRIPTION_PREFIX) :]

                next_run = properties.get("NextElapseUSecRealtime", "")
                if next_run and next_run != "n/a":
                    schedule_info["next_run"] = next_run
                last_run = properties.get("LastTriggerUSec", "")
                if last_run and last_run != "n/a":
                    schedule_info["last_run"] = last_run

            schedules.append(schedule_info)

        return schedules

    def get_schedule(self, domain: str) -> BackupSchedule | None:
        """
        Get the schedule for a specific domain.

        Args:
            domain: Application domain.

        Returns:
            The schedule, or None when no timer is enabled for the domain.
        """
        app_name = domain_to_app_name(domain)
        timer_name = f"wasm-backup-{app_name}"

        result = self._systemctl("is-enabled", f"{timer_name}.timer")
        if not result.success:
            return None

        return BackupSchedule(
            domain=domain,
            app_name=app_name,
            schedule="unknown",
        )

    def _render(self, template_name: str, **values: str) -> str:
        """
        Render one of the escaped systemd templates.

        Args:
            template_name: File name of the template.
            values: Values passed to the template.

        Returns:
            The rendered unit file.

        Raises:
            BackupError: If the template is missing or fails to render.
        """
        try:
            template = self.jinja_env.get_template(template_name)
            return template.render(**values)
        except TemplateError as exc:
            raise BackupError(
                f"Failed to render systemd template: {template_name}",
                details=f"{exc}. The WASM installation may be incomplete.",
            ) from exc

    def render_timer(self, schedule: BackupSchedule) -> str:
        """
        Render the systemd timer unit file.

        Args:
            schedule: Schedule to render.

        Returns:
            The unit file content, with every interpolated value escaped.

        Raises:
            BackupError: If the template cannot be rendered.
        """
        return self._render(
            "backup-timer.j2",
            domain=schedule.domain,
            schedule=schedule.on_calendar,
        )

    def render_service(self, schedule: BackupSchedule) -> str:
        """
        Render the systemd service unit file.

        Args:
            schedule: Schedule to render.

        Returns:
            The unit file content, with every interpolated value escaped.

        Raises:
            BackupError: If the template cannot be rendered.
        """
        return self._render("backup-service.j2", domain=schedule.domain)
