# Copyright (c) 2024-2025 Yago López Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
Backup scheduler for WASM.

Creates and manages systemd timers for automated application backups.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

from jinja2 import Environment, PackageLoader

from wasm.core.config import SYSTEMD_DIR as _SYSTEMD_DIR
from wasm.core.exceptions import BackupError
from wasm.core.logger import Logger
from wasm.core.utils import (
    run_command,
    run_command_sudo,
    write_file,
    remove_file,
    domain_to_app_name,
)


# Schedule aliases
SCHEDULE_ALIASES = {
    "hourly": "*-*-* *:00:00",
    "daily": "*-*-* 02:00:00",
    "weekly": "Mon *-*-* 02:00:00",
    "monthly": "*-*-01 02:00:00",
}


@dataclass
class BackupSchedule:
    """Configuration for a scheduled backup."""
    domain: str
    app_name: str
    schedule: str
    include_databases: bool = True
    retention_count: int = 7
    retention_days: int = 30
    tags: List[str] = None

    def __post_init__(self):
        if self.tags is None:
            self.tags = ["scheduled", "auto"]

    @property
    def on_calendar(self) -> str:
        """Convert schedule alias to systemd OnCalendar format."""
        return SCHEDULE_ALIASES.get(self.schedule, self.schedule)

    @property
    def timer_name(self) -> str:
        """Systemd timer unit name."""
        return f"wasm-backup-{self.app_name}"

    @property
    def service_name(self) -> str:
        """Systemd service unit name."""
        return f"wasm-backup-{self.app_name}"


class BackupScheduler:
    """
    Manager for scheduled backups using systemd timers.

    Creates timer/service unit pairs that trigger `wasm backup create`
    on a configurable schedule.
    """

    SYSTEMD_DIR = _SYSTEMD_DIR

    def __init__(self, verbose: bool = False):
        self.verbose = verbose
        self.logger = Logger(verbose=verbose)

        try:
            self.jinja_env = Environment(
                loader=PackageLoader("wasm", "templates/systemd"),
                trim_blocks=True,
                lstrip_blocks=True,
            )
        except Exception:
            self.jinja_env = None

    def create_schedule(self, schedule: BackupSchedule) -> bool:
        """
        Create a backup schedule using systemd timer + service.

        Args:
            schedule: Backup schedule configuration.

        Returns:
            True if schedule was created successfully.

        Raises:
            BackupError: If creation fails.
        """
        timer_path = self.SYSTEMD_DIR / f"{schedule.timer_name}.timer"
        service_path = self.SYSTEMD_DIR / f"{schedule.service_name}.service"

        # Render timer template
        timer_content = self._render_timer(schedule)
        if not write_file(timer_path, timer_content, sudo=True):
            raise BackupError(f"Failed to write timer: {timer_path}")

        # Render service template
        service_content = self._render_service(schedule)
        if not write_file(service_path, service_content, sudo=True):
            raise BackupError(f"Failed to write service: {service_path}")

        # Reload systemd
        run_command_sudo(["systemctl", "daemon-reload"])

        # Enable and start timer
        result = run_command_sudo(["systemctl", "enable", "--now", f"{schedule.timer_name}.timer"])
        if not result.success:
            raise BackupError(f"Failed to enable timer: {result.stderr}")

        self.logger.info(f"Created backup schedule: {schedule.timer_name}")
        self.logger.info(f"  Schedule: {schedule.on_calendar}")
        return True

    def remove_schedule(self, domain: str) -> bool:
        """
        Remove a backup schedule.

        Args:
            domain: Application domain.

        Returns:
            True if schedule was removed.
        """
        app_name = domain_to_app_name(domain)
        timer_name = f"wasm-backup-{app_name}"

        # Stop and disable timer
        run_command_sudo(["systemctl", "stop", f"{timer_name}.timer"])
        run_command_sudo(["systemctl", "disable", f"{timer_name}.timer"])

        # Remove unit files
        timer_path = self.SYSTEMD_DIR / f"{timer_name}.timer"
        service_path = self.SYSTEMD_DIR / f"{timer_name}.service"

        remove_file(timer_path, sudo=True)
        remove_file(service_path, sudo=True)

        # Reload systemd
        run_command_sudo(["systemctl", "daemon-reload"])

        self.logger.info(f"Removed backup schedule: {timer_name}")
        return True

    def list_schedules(self) -> List[Dict]:
        """
        List all WASM backup schedules.

        Returns:
            List of schedule information dictionaries.
        """
        result = run_command([
            "systemctl", "list-timers",
            "--no-legend", "--no-pager",
            "wasm-backup-*",
        ])

        schedules = []
        if not result.success:
            return schedules

        for line in result.stdout.strip().splitlines():
            if not line.strip():
                continue
            parts = line.split()
            if len(parts) >= 5:
                timer_name = parts[-1].replace(".timer", "")
                # Extract domain from timer name
                app_name = timer_name.replace("wasm-backup-", "")

                # Get timer details
                detail_result = run_command([
                    "systemctl", "show", f"{timer_name}.timer",
                    "--property=TimersCalendar,LastTriggerUSec,NextElapseUSecRealtime",
                ])

                schedule_info = {
                    "timer": timer_name,
                    "app_name": app_name,
                    "next_run": parts[0] if parts[0] != "n/a" else "pending",
                    "last_run": parts[2] if len(parts) > 2 and parts[2] != "n/a" else "never",
                }

                if detail_result.success:
                    for prop_line in detail_result.stdout.splitlines():
                        if prop_line.startswith("TimersCalendar="):
                            schedule_info["schedule"] = prop_line.split("=", 1)[1]

                schedules.append(schedule_info)

        return schedules

    def get_schedule(self, domain: str) -> Optional[BackupSchedule]:
        """
        Get schedule for a specific domain.

        Args:
            domain: Application domain.

        Returns:
            BackupSchedule or None if not found.
        """
        app_name = domain_to_app_name(domain)
        timer_name = f"wasm-backup-{app_name}"

        result = run_command(["systemctl", "is-enabled", f"{timer_name}.timer"])
        if not result.success:
            return None

        return BackupSchedule(
            domain=domain,
            app_name=app_name,
            schedule="unknown",
        )

    def _render_timer(self, schedule: BackupSchedule) -> str:
        """Render the systemd timer unit file."""
        if self.jinja_env:
            template = self.jinja_env.get_template("backup-timer.j2")
            return template.render(
                domain=schedule.domain,
                schedule=schedule.on_calendar,
            )

        # Fallback without Jinja2
        return (
            f"[Unit]\n"
            f"Description=WASM backup timer for {schedule.domain}\n\n"
            f"[Timer]\n"
            f"OnCalendar={schedule.on_calendar}\n"
            f"Persistent=true\n"
            f"RandomizedDelaySec=300\n\n"
            f"[Install]\n"
            f"WantedBy=timers.target\n"
        )

    def _render_service(self, schedule: BackupSchedule) -> str:
        """Render the systemd service unit file."""
        if self.jinja_env:
            template = self.jinja_env.get_template("backup-service.j2")
            return template.render(domain=schedule.domain)

        # Fallback without Jinja2
        return (
            f"[Unit]\n"
            f"Description=WASM scheduled backup for {schedule.domain}\n\n"
            f"[Service]\n"
            f"Type=oneshot\n"
            f"ExecStart=/usr/bin/wasm backup create {schedule.domain}"
            f" --include-databases --tags scheduled,auto\n"
        )
