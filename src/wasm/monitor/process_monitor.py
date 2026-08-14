"""
The monitor daemon: observability, not enforcement.

What this module used to do, running as root from a systemd unit: match a
regular expression against a command line, call the result malicious,
terminate the process tree, and hand the process working directory to
``shutil.rmtree`` (so any process with ``cwd=/tmp`` cost the machine ``/tmp``).

What it does now: read metrics, read the process table, read unit state, write
down what stands out, and tell somebody. It never signals a process and never
deletes a file. The one filesystem write it makes is its own systemd unit, at a
fixed path, and only when an operator runs ``wasm monitor install``.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from wasm.core.config import SYSTEMD_DIR, Config
from wasm.core.exceptions import MonitorError, WASMError
from wasm.core.logger import Logger
from wasm.core.notifier import NotificationEvent, Notifier
from wasm.core.runner import CommandRunner, get_runner
from wasm.core.utils import remove_file, write_file
from wasm.monitor.email_notifier import EmailNotifier
from wasm.monitor.metrics import (
    SYSTEMCTL_TIMEOUT,
    collect_resource_metrics,
    collect_service_health,
    list_processes,
)
from wasm.monitor.models import (
    SEVERITY_WARNING,
    ProcessObservation,
    ResourceMetrics,
    ServiceHealth,
)
from wasm.monitor.observation_store import DEFAULT_MAX_OBSERVATIONS, ObservationStore
from wasm.monitor.signals import observe_processes

#: Seconds between scans. A minute is enough for capacity planning and cheap
#: enough to leave running on a small box.
DEFAULT_SCAN_INTERVAL = 60

#: Floor for the scan interval. Walking /proc for every process is the most
#: expensive thing this daemon does; doing it several times a second costs more
#: CPU than whatever it would observe.
MIN_SCAN_INTERVAL = 10

#: Percentages above which a process is written down as a heavy consumer.
DEFAULT_CPU_THRESHOLD = 80.0
DEFAULT_MEMORY_THRESHOLD = 80.0

#: How long observations are kept before they are purged.
DEFAULT_RETENTION_DAYS = 30

#: Retention is a housekeeping job, not a per-scan one. Running the delete once
#: an hour instead of once a minute keeps the scan loop to reads.
PURGE_INTERVAL_SECONDS = 3_600

#: Filesystem usage from which a disk is called out in the log rather than left
#: at debug level. Past this, a full disk is a matter of days.
DISK_ALERT_PERCENT = 90.0

#: What this package will not do, in the words used to check it. The CLI and the
#: web panel print these, and tests assert them against the package source.
MONITOR_SCOPE: tuple[str, ...] = (
    "Never signals, terminates or restarts a process.",
    "Never deletes or modifies a file, except the systemd unit it installs.",
    "Never sends data about the machine anywhere except the configured SMTP relay.",
    "Never inspects a process command line to decide anything.",
)


@dataclass
class MonitorConfig:
    """
    Monitor settings.

    There is deliberately no auto-terminate and no dry-run: with nothing
    destructive left to switch off, a dry-run flag would only imply that the
    other mode does something to the machine.

    Attributes:
        enabled: Whether the daemon should run at boot.
        scan_interval: Seconds between scans.
        cpu_threshold: CPU percentage above which a process is noted.
        memory_threshold: Memory percentage above which a process is noted.
        notify: Send observations by email.
        watch_units: systemd units whose health is checked each scan.
        retention_days: How long observations are kept.
        max_observations: Hard ceiling on rows kept in the observation store.
        log_file: Where the daemon writes its log.
    """

    enabled: bool = False
    scan_interval: int = DEFAULT_SCAN_INTERVAL
    cpu_threshold: float = DEFAULT_CPU_THRESHOLD
    memory_threshold: float = DEFAULT_MEMORY_THRESHOLD
    notify: bool = False
    watch_units: tuple[str, ...] = field(default_factory=tuple)
    retention_days: int = DEFAULT_RETENTION_DAYS
    max_observations: int = DEFAULT_MAX_OBSERVATIONS
    log_file: Path | None = None

    def __post_init__(self) -> None:
        """Clamp the scan interval so a config typo cannot pin a CPU."""
        self.scan_interval = max(MIN_SCAN_INTERVAL, int(self.scan_interval))


class ProcessMonitor:
    """
    Periodic observer of processes, resources and services.

    Every public method here either reads the machine or writes to the
    observation store. None of them changes the state of a process.
    """

    SERVICE_NAME = "wasm-monitor"

    def __init__(
        self,
        config: MonitorConfig | None = None,
        verbose: bool = False,
        store: Any | None = None,
        notifier: Any | None = None,
        runner: CommandRunner | None = None,
        event_notifier: Any | None = None,
    ) -> None:
        """
        Args:
            config: Monitor settings. Loaded from the global config if None.
            verbose: Enable verbose logging.
            store: Observation store. Created on first use if None.
            notifier: Email notifier. Created on first use if None.
            runner: Command runner. Defaults to the process-wide one.
            event_notifier: Multi-channel notifier for unit and disk events.
                Created on first use if None; tests inject a capture.
        """
        self.verbose = verbose
        self.logger = Logger(verbose=verbose)
        self.global_config = Config()
        self.config = config or self._load_config()
        self.runner = runner or get_runner()

        self._store = store
        self._notifier = notifier
        self._event_notifier = event_notifier
        self._running = False
        # Far enough in the past that the first loop iteration purges once.
        self._last_purge = float("-inf")
        # Transition memory for the notifier: a disk over the threshold or a
        # unit down is one event when it crosses, not one per scan, or a
        # sixty second interval becomes a message a minute to every channel.
        self._alerted_disks: set[str] = set()
        self._failed_units: set[str] = set()

    def _load_config(self) -> MonitorConfig:
        """
        Read monitor settings from the global configuration.

        Returns:
            The settings, with defaults for anything unset.
        """
        get = self.global_config.get
        return MonitorConfig(
            enabled=get("monitor.enabled", False),
            scan_interval=int(get("monitor.scan_interval", DEFAULT_SCAN_INTERVAL)),
            cpu_threshold=float(get("monitor.cpu_threshold", DEFAULT_CPU_THRESHOLD)),
            memory_threshold=float(get("monitor.memory_threshold", DEFAULT_MEMORY_THRESHOLD)),
            notify=bool(get("monitor.notify", False)),
            watch_units=tuple(get("monitor.watch_units", []) or ()),
            retention_days=int(get("monitor.retention_days", DEFAULT_RETENTION_DAYS)),
            max_observations=int(get("monitor.max_observations", DEFAULT_MAX_OBSERVATIONS)),
            log_file=Path(get("monitor.log_file", "/var/log/wasm/monitor.log")),
        )

    @property
    def store(self) -> Any:
        """The observation store, opened on first use."""
        if self._store is None:
            self._store = ObservationStore(
                verbose=self.verbose,
                max_observations=self.config.max_observations,
            )
        return self._store

    @property
    def notifier(self) -> Any:
        """The email notifier, built on first use."""
        if self._notifier is None:
            self._notifier = EmailNotifier(verbose=self.verbose)
        return self._notifier

    @property
    def event_notifier(self) -> Any:
        """The multi-channel notifier, built on first use."""
        if self._event_notifier is None:
            self._event_notifier = Notifier(self.global_config)
        return self._event_notifier

    def _publish_event(self, kind: str, title: str, body: str) -> None:
        """
        Hand one observation to the multi-channel notifier.

        The daemon runs for months, so the configuration is re-read from disk
        first: an operator enabling notifications in the panel reaches the
        next scan without a restart. Delivery problems are the notifier's to
        log and skip; a scan must never fail because an endpoint is down.

        Args:
            kind: One of the notifier's event kinds.
            title: One-line summary.
            body: The detail the operator acts on.
        """
        try:
            self.global_config.reload()
        except (WASMError, OSError) as exc:
            self.logger.debug(f"Configuration reload before notification failed: {exc}")
        self.event_notifier.notify(NotificationEvent(kind=kind, title=title, body=body))

    def collect_metrics(self) -> ResourceMetrics:
        """
        Read the machine's resource counters.

        Returns:
            A point-in-time reading.

        Raises:
            MonitorError: When psutil is not installed.
        """
        return collect_resource_metrics()

    def check_services(self, units: list[str] | None = None) -> list[ServiceHealth]:
        """
        Ask systemd about the units being watched.

        Args:
            units: Units to check. Defaults to the configured ones.

        Returns:
            One health record per unit.
        """
        watched = list(units if units is not None else self.config.watch_units)
        if not watched:
            return []
        return collect_service_health(watched, runner=self.runner)

    def scan_once(self, cpu_sample_interval: float = 0.0) -> list[ProcessObservation]:
        """
        Take one look at the process table and record what stands out.

        Nothing is terminated and nothing is deleted: the return value and the
        observation store are the entire effect of a scan.

        Args:
            cpu_sample_interval: Seconds to spend sampling CPU usage. Zero in
                the daemon loop, where the previous scan is the reference
                point; a fraction of a second for a one-shot scan, which would
                otherwise see 0% CPU for every process.

        Returns:
            The observations made, warnings first.

        Raises:
            MonitorError: When the process table cannot be read.
        """
        processes = list_processes(cpu_sample_interval=cpu_sample_interval)
        self.logger.debug(f"Scanned {len(processes)} processes")

        observations = observe_processes(
            processes,
            cpu_threshold=self.config.cpu_threshold,
            memory_threshold=self.config.memory_threshold,
        )

        if not observations:
            self.logger.debug("Nothing worth reporting")
            return []

        warnings = sum(1 for o in observations if o.severity == SEVERITY_WARNING)
        self.logger.info(
            f"Noted {len(observations)} process(es) ({warnings} warning(s)). "
            "Report only: no process was signalled."
        )
        for observation in observations:
            self.logger.debug(
                f"  {observation.severity}: {observation.process.name} "
                f"(PID {observation.process.pid}) - {observation.detail}"
            )

        stored: list[int] = []
        try:
            stored = self.store.save_many(observations) or []
        except WASMError as exc:
            self.logger.error(f"Failed to persist observations: {exc}")
        except OSError as exc:
            self.logger.error(f"Failed to persist observations: {exc}")

        # Only new rows are worth an email. The store collapses a process that
        # keeps doing the same thing into one row per window, and the report
        # follows it: otherwise one busy process is a mail every scan interval.
        if self.config.notify and stored:
            try:
                self.notifier.send_observation_alert(observations)
            except WASMError as exc:
                self.logger.error(f"Failed to send observation report: {exc}")

        return observations

    def _log_metrics(self) -> None:
        """
        Record a one-line resource summary, best effort.

        Readings are not persisted: a time series would grow without bound for
        a panel that only ever shows the current value. The line goes to debug
        unless something is close enough to hurting to be worth a journal entry.
        """
        try:
            metrics = self.collect_metrics()
        except MonitorError as exc:
            self.logger.debug(f"Resource metrics unavailable: {exc}")
            return

        disks = ", ".join(f"{d.mountpoint} {d.percent:.0f}%" for d in metrics.disks)
        summary = (
            f"CPU {metrics.cpu_percent:.1f}% | RAM {metrics.memory_percent:.1f}% | "
            f"procs {metrics.process_count} | disks: {disks or 'n/a'}"
        )

        full_disks = [d for d in metrics.disks if d.percent >= DISK_ALERT_PERCENT]
        if full_disks or metrics.memory_percent >= self.config.memory_threshold:
            self.logger.warning(summary)
        else:
            self.logger.debug(summary)

        self._notify_full_disks(full_disks)

    def _notify_full_disks(self, full_disks: list[Any]) -> None:
        """
        Publish ``disk_threshold`` for disks newly over the line.

        Transition-based: a filesystem already announced stays silent until
        it drops under the threshold and crosses it again, so a full disk is
        one message, not one per scan interval.

        Args:
            full_disks: The disks at or over :data:`DISK_ALERT_PERCENT`.
        """
        for disk in full_disks:
            if disk.mountpoint in self._alerted_disks:
                continue
            self._publish_event(
                "disk_threshold",
                f"Disk usage at {disk.percent:.0f}% on {disk.mountpoint}",
                (
                    f"{disk.mountpoint} is {disk.percent:.1f}% full, past the "
                    f"{DISK_ALERT_PERCENT:.0f}% alert threshold. A full disk stops "
                    "deployments, logs and databases on this machine."
                ),
            )
        self._alerted_disks = {disk.mountpoint for disk in full_disks}

    def _report_services(self) -> None:
        """
        Log any watched unit that is not active, and announce the transition.

        The ``unit_failed`` event goes out once per outage, when a unit goes
        from active to anything else; a unit that stays down is not repeated
        every scan, and one that recovers re-arms.
        """
        down: set[str] = set()
        for health in self.check_services():
            if health.active:
                continue
            state = health.active_state or "unknown"
            down.add(health.unit)
            self.logger.warning(f"Service {health.unit} is {state}")
            if health.unit not in self._failed_units:
                self._publish_event(
                    "unit_failed",
                    f"Unit {health.unit} is {state}",
                    (
                        f"The watched systemd unit {health.unit} reports {state}. "
                        f"Inspect it with: systemctl status {health.unit}"
                    ),
                )
        self._failed_units = down

    def run(self) -> None:
        """
        Observe the machine until :meth:`stop` is called.

        Scans, resource metrics and service checks all happen once per
        interval; old observations are purged hourly as they age out.
        """
        self._running = True
        interval = max(MIN_SCAN_INTERVAL, self.config.scan_interval)
        self.logger.info(f"Starting process monitor (scan every {interval}s, report only)")

        while self._running:
            try:
                self._log_metrics()
                self._report_services()
                self.scan_once()
            except WASMError as exc:
                self.logger.error(f"Scan failed: {exc}")
            except OSError as exc:
                self.logger.error(f"Scan failed to read the system: {exc}")

            self._purge_old_observations()

            for _ in range(interval):
                if not self._running:
                    break
                time.sleep(1)

        self.logger.info("Process monitor stopped")

    def stop(self) -> None:
        """Ask the monitor loop to finish the current interval and exit."""
        self._running = False

    def _purge_old_observations(self) -> None:
        """
        Drop observations past the retention window, best effort.

        Throttled: with a 60 second interval this would otherwise be 1.440
        DELETE statements a day to remove rows that age out once.
        """
        now = time.monotonic()
        if now - self._last_purge < PURGE_INTERVAL_SECONDS:
            return
        self._last_purge = now

        try:
            self.store.purge_older_than(self.config.retention_days)
            self.store.enforce_limit()
        except WASMError as exc:
            self.logger.debug(f"Retention purge skipped: {exc}")
        except OSError as exc:
            self.logger.debug(f"Retention purge skipped: {exc}")

    @property
    def unit_path(self) -> Path:
        """Path of the systemd unit this monitor installs."""
        return SYSTEMD_DIR / f"{self.SERVICE_NAME}.service"

    def _wasm_executable(self) -> str:
        """
        Locate the wasm entry point for the unit's ExecStart.

        systemd has no PATH of its own, so a relative command in a unit file is
        a service that fails to start.

        Returns:
            An absolute path to the wasm executable.

        Raises:
            MonitorError: When wasm cannot be found on PATH.
        """
        for directory in os.environ.get("PATH", "").split(os.pathsep):
            if not directory:
                continue
            candidate = Path(directory) / "wasm"
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return str(candidate.resolve())

        raise MonitorError(
            "Could not find the wasm executable to reference from the systemd unit",
            details="Install WASM system-wide (pip install wasm-cli) before installing the service.",
        )

    def _unit_content(self) -> str:
        """
        Render the systemd unit.

        Returns:
            The unit file body.

        Raises:
            MonitorError: When the wasm executable cannot be located.
        """
        wasm_path = self._wasm_executable()

        return f"""# WASM process monitor
# Generated by WASM. Do not edit; reinstall with: wasm monitor install

[Unit]
Description=WASM process and resource monitor
Documentation=https://github.com/Perkybeet/wasm
After=network.target

[Service]
Type=simple
User=root
Group=root
ExecStart={wasm_path} monitor run
Restart=always
RestartSec=30

# The monitor only reads the system; deny it the ability to do anything else.
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=read-only
# StateDirectory and LogsDirectory rather than ReadWritePaths: systemd creates
# these before the unit starts and adds them to the writable set itself.
# ReadWritePaths does neither, so on a machine where /var/lib/wasm did not
# exist yet - which is every machine that installed the monitor before it had
# ever written a database - mounting the namespace failed outright. The unit
# exited 226/NAMESPACE and systemd restarted it every 30 seconds forever: one
# production server was found at 2379 consecutive failures, with the monitor
# never having run once.
StateDirectory=wasm
LogsDirectory=wasm
PrivateDevices=true
RestrictSUIDSGID=true

StandardOutput=journal
StandardError=journal
SyslogIdentifier={self.SERVICE_NAME}

[Install]
WantedBy=multi-user.target
"""

    def install_service(self) -> bool:
        """
        Write the systemd unit and reload systemd.

        Returns:
            True when the unit was installed.

        Raises:
            MonitorError: When the unit cannot be written or systemd refuses.
        """
        if not write_file(self.unit_path, self._unit_content(), mode=0o644):
            raise MonitorError(
                f"Failed to write {self.unit_path}",
                details="Installing a systemd unit requires root: run with sudo.",
            )

        result = self.runner.run(["systemctl", "daemon-reload"], timeout=SYSTEMCTL_TIMEOUT)
        if not result.success:
            raise MonitorError(
                "systemd rejected the reload after installing the monitor unit",
                details=result.stderr or result.stdout,
            )

        self.logger.success(f"Monitor service installed: {self.unit_path}")
        return True

    def _systemctl(self, *args: str, action: str) -> bool:
        """
        Run a systemctl subcommand against the monitor unit.

        Args:
            args: Arguments after ``systemctl``.
            action: Human-readable action, used in the error message.

        Returns:
            True when systemctl succeeded.

        Raises:
            MonitorError: When systemctl failed.
        """
        result = self.runner.run(["systemctl", *args], timeout=SYSTEMCTL_TIMEOUT)
        if not result.success:
            raise MonitorError(
                f"Failed to {action} the monitor service",
                details=result.stderr or result.stdout or f"systemctl exited {result.exit_code}",
            )
        return True

    def enable_service(self) -> bool:
        """
        Enable the monitor unit and start it now.

        Returns:
            True when systemd accepted the change.

        Raises:
            MonitorError: When systemctl failed.
        """
        self._systemctl("enable", "--now", self.SERVICE_NAME, action="enable")
        self.logger.success("Monitor service enabled and started")
        return True

    def disable_service(self) -> bool:
        """
        Disable the monitor unit and stop it now.

        Returns:
            True when systemd accepted the change.

        Raises:
            MonitorError: When systemctl failed.
        """
        self._systemctl("disable", "--now", self.SERVICE_NAME, action="disable")
        self.logger.success("Monitor service disabled and stopped")
        return True

    def start_service(self) -> bool:
        """
        Start the monitor unit without enabling it at boot.

        Returns:
            True when systemd accepted the change.

        Raises:
            MonitorError: When systemctl failed.
        """
        self._systemctl("start", self.SERVICE_NAME, action="start")
        self.logger.success("Monitor service started")
        return True

    def stop_service(self) -> bool:
        """
        Stop the monitor unit without disabling it at boot.

        Returns:
            True when systemd accepted the change.

        Raises:
            MonitorError: When systemctl failed.
        """
        self._systemctl("stop", self.SERVICE_NAME, action="stop")
        self.logger.success("Monitor service stopped")
        return True

    def uninstall_service(self) -> bool:
        """
        Stop the monitor unit and delete the unit file WASM wrote.

        Returns:
            True when the unit is gone.

        Raises:
            MonitorError: When systemd refuses the reload.
        """
        # Disabling a unit that is already gone is not an error worth failing on.
        self.runner.run(
            ["systemctl", "disable", "--now", self.SERVICE_NAME],
            timeout=SYSTEMCTL_TIMEOUT,
        )

        # The only file this package ever deletes: a constant path, written by
        # install_service, removed on explicit operator request. Nothing here is
        # derived from observed process data.
        if self.unit_path.exists() and not remove_file(self.unit_path):
            raise MonitorError(
                f"Failed to delete {self.unit_path}",
                details="Removing a systemd unit requires root: run with sudo.",
            )

        result = self.runner.run(["systemctl", "daemon-reload"], timeout=SYSTEMCTL_TIMEOUT)
        if not result.success:
            raise MonitorError(
                "systemd rejected the reload after removing the monitor unit",
                details=result.stderr or result.stdout,
            )

        self.logger.success("Monitor service uninstalled")
        return True

    def get_service_status(self) -> dict[str, Any]:
        """
        Report what systemd knows about the monitor unit.

        Returns:
            Keys: installed, enabled, active, pid, uptime.
        """
        status: dict[str, Any] = {
            "installed": self.unit_path.exists(),
            "enabled": False,
            "active": False,
            "pid": None,
            "uptime": None,
        }
        if not status["installed"]:
            return status

        enabled = self.runner.run(
            ["systemctl", "is-enabled", self.SERVICE_NAME],
            timeout=SYSTEMCTL_TIMEOUT,
        )
        status["enabled"] = enabled.output in ("enabled", "enabled-runtime")

        active = self.runner.run(
            ["systemctl", "is-active", self.SERVICE_NAME],
            timeout=SYSTEMCTL_TIMEOUT,
        )
        status["active"] = active.output in ("active", "activating")

        shown = self.runner.run(
            [
                "systemctl",
                "show",
                self.SERVICE_NAME,
                "--property=MainPID,ActiveEnterTimestamp,ActiveState",
            ],
            timeout=SYSTEMCTL_TIMEOUT,
        )
        for line in shown.output.splitlines():
            key, _, value = line.partition("=")
            value = value.strip()
            if key == "MainPID" and value.isdigit() and value != "0":
                status["pid"] = int(value)
            elif key == "ActiveEnterTimestamp" and value:
                status["uptime"] = value
            elif key == "ActiveState" and value.lower() in ("active", "activating"):
                status["active"] = True

        return status
