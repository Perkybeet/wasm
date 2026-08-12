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
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from jinja2 import Environment, PackageLoader, TemplateError

from wasm.core.config import SYSTEMD_DIR as _SYSTEMD_DIR
from wasm.core.exceptions import BackupError, WASMError
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

    def __init__(self, verbose: bool = False, runner: CommandRunner | None = None) -> None:
        """
        Initialize the scheduler.

        Args:
            verbose: Enable verbose logging.
            runner: Command runner used for systemctl. Defaults to the
                process-wide runner.
        """
        self.verbose = verbose
        self.logger = Logger(verbose=verbose)
        self._runner = runner
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

        calendar = schedule.on_calendar
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

        return domain, app_name, calendar

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
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
            path.chmod(_UNIT_MODE)
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
                path.unlink(missing_ok=True)
            except OSError as exc:
                self.logger.warning(f"Could not remove {path}: {exc}")

        self._systemctl("daemon-reload")

        self.logger.info(f"Removed backup schedule: {timer_name}")
        return True

    def list_schedules(self) -> list[dict[str, str]]:
        """
        List all WASM backup schedules.

        Returns:
            One dictionary per timer, with its name, application and run times.
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
            if not line.strip():
                continue
            parts = line.split()
            if len(parts) < 5:
                continue

            timer_name = parts[-1].replace(".timer", "")
            schedule_info: dict[str, str] = {
                "timer": timer_name,
                "app_name": timer_name.replace("wasm-backup-", ""),
                "next_run": parts[0] if parts[0] != "n/a" else "pending",
                "last_run": parts[2] if len(parts) > 2 and parts[2] != "n/a" else "never",
            }

            detail = self._systemctl(
                "show",
                f"{timer_name}.timer",
                "--property=TimersCalendar,LastTriggerUSec,NextElapseUSecRealtime",
            )
            if detail.success:
                for prop_line in detail.stdout.splitlines():
                    if prop_line.startswith("TimersCalendar="):
                        schedule_info["schedule"] = prop_line.split("=", 1)[1]

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
