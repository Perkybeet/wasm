"""
Monitor command handlers.

``wasm monitor`` drives observability, not enforcement: it reads resource
metrics, the process table and systemd unit health, writes down what stands out
and can mail a report. It never signals a process and never deletes a file.

These handlers used to build a ``MonitorConfig`` with ``auto_terminate``,
``use_ai`` and ``dry_run``, settings that stopped existing when the monitor
stopped being an antivirus, so ``wasm monitor scan`` raised a TypeError on every
run. Nothing here reads a configuration key that the monitor does not have; the
handler map is exported so a test can exercise every action.
"""

from __future__ import annotations

from argparse import Namespace
from collections.abc import Callable

from wasm.core.config import Config
from wasm.core.exceptions import EmailError, MonitorError, WASMError
from wasm.core.logger import Logger
from wasm.core.utils import check_root
from wasm.monitor import (
    DEFAULT_CPU_SAMPLE_INTERVAL,
    DEFAULT_CPU_THRESHOLD,
    DEFAULT_MAX_OBSERVATIONS,
    DEFAULT_MEMORY_THRESHOLD,
    DEFAULT_RETENTION_DAYS,
    DEFAULT_SCAN_INTERVAL,
    MONITOR_SCOPE,
    EmailNotifier,
    ObservationStore,
    ProcessMonitor,
    ProcessObservation,
    default_db_path,
)

#: Options the parser still accepts from the antivirus era. Accepting and
#: explaining them beats an argparse error for a flag a user's script may pass.
REMOVED_SCAN_FLAGS: tuple[tuple[str, str], ...] = (
    ("force_ai", "--force-ai"),
    ("all", "--all"),
)


def _require_root(action: str) -> None:
    """
    Refuse an action that cannot work without root.

    Args:
        action: What the user asked for, used in the message.

    Raises:
        MonitorError: When the process is not running as root.
    """
    if not check_root():
        raise MonitorError(
            f"'{action}' needs root",
            details="Installing, enabling or removing a systemd unit requires root: use sudo.",
        )


def _warn_about_removed_flags(args: Namespace, logger: Logger) -> None:
    """
    Tell the user that the AI scan options no longer do anything.

    Args:
        args: Parsed arguments.
        logger: Logger to report through.
    """
    for attribute, flag in REMOVED_SCAN_FLAGS:
        if getattr(args, attribute, False):
            logger.warning(
                f"{flag} is ignored: the monitor no longer sends process data to a "
                "third-party model. It reports what it sees, locally."
            )


def _print_observation(observation: ProcessObservation, logger: Logger) -> None:
    """
    Render one observation.

    Args:
        observation: What the scan noticed.
        logger: Logger to report through.
    """
    process = observation.process
    headline = f"{process.name} (PID {process.pid}) - {observation.signal}"
    if observation.severity == "warning":
        logger.warning(headline)
    else:
        logger.info(headline)

    logger.key_value("User", process.user or "unknown", indent=4)
    logger.key_value(
        "Usage",
        f"CPU {process.cpu_percent:.1f}% | memory {process.memory_percent:.1f}%",
        indent=4,
    )
    logger.key_value("Detail", observation.detail, indent=4)
    logger.key_value("Command", process.command or "-", indent=4)


def handle_monitor(args: Namespace) -> int:
    """
    Route a ``wasm monitor`` invocation to its handler.

    Only :class:`WASMError` is caught here. A TypeError or an AttributeError is
    a defect in WASM, and the previous blanket ``except Exception`` is exactly
    what let this command ship broken: it turned a call to a constructor that no
    longer accepted its arguments into a one-line "unexpected error".

    Args:
        args: Parsed arguments.

    Returns:
        Exit code.
    """
    logger = Logger(verbose=args.verbose)

    handler = ACTIONS.get(args.action)
    if handler is None:
        logger.error(
            f"Unknown action: {args.action}",
            f"Known actions: {', '.join(sorted(ACTIONS))}",
        )
        return 1

    try:
        return handler(args)
    except WASMError as exc:
        # WASMError.__str__ already appends the details; passing them twice prints twice.
        logger.error(str(exc))
        return 1
    except KeyboardInterrupt:
        logger.info("Interrupted")
        return 130


def _handle_status(args: Namespace) -> int:
    """
    Show the state of the monitor service and what it is set to observe.

    Args:
        args: Parsed arguments.

    Returns:
        Exit code.
    """
    logger = Logger(verbose=args.verbose)
    monitor = ProcessMonitor(verbose=args.verbose)
    status = monitor.get_service_status()

    logger.header("WASM monitor")

    if status["installed"]:
        logger.key_value("Installed", "Yes")
        logger.key_value("Enabled at boot", "Yes" if status["enabled"] else "No")
        logger.key_value("Running", "Yes" if status["active"] else "No")
        if status["pid"]:
            logger.key_value("PID", str(status["pid"]))
        if status["uptime"]:
            logger.key_value("Started", str(status["uptime"]))
    else:
        logger.key_value("Installed", "No")
        logger.info("Run 'wasm monitor install' to install the service")

    config = monitor.config
    logger.section("Observing")
    logger.key_value("Scan interval", f"{config.scan_interval}s")
    logger.key_value("CPU threshold", f"{config.cpu_threshold:.0f}%")
    logger.key_value("Memory threshold", f"{config.memory_threshold:.0f}%")
    logger.key_value("Watched units", ", ".join(config.watch_units) or "none")
    logger.key_value("Email reports", "On" if config.notify else "Off")

    logger.section("Observation store")
    logger.key_value("Database", str(default_db_path()))
    logger.key_value("Retention", f"{config.retention_days} days")
    logger.key_value("Row cap", str(config.max_observations))
    _print_store_counts(logger)

    _print_scope(logger)
    return 0


def _print_store_counts(logger: Logger) -> None:
    """
    Show how much the observation store currently holds.

    Args:
        logger: Logger to report through.
    """
    try:
        stats = ObservationStore().stats()
    except (WASMError, OSError) as exc:
        # An unreadable store is worth a line, not a failed status command.
        logger.debug(f"Observation store unavailable: {exc}")
        return
    logger.key_value("Stored", f"{stats['total']} ({stats['open']} unacknowledged)")


def _print_scope(logger: Logger) -> None:
    """
    Print what the monitor does not do.

    Args:
        logger: Logger to report through.
    """
    logger.section("This tool never")
    for guarantee in MONITOR_SCOPE:
        logger.list_item(guarantee.replace("Never ", "", 1).rstrip("."))


def _handle_scan(args: Namespace) -> int:
    """
    Run a single scan and print what it noticed.

    Args:
        args: Parsed arguments.

    Returns:
        Exit code.
    """
    logger = Logger(verbose=args.verbose)
    _warn_about_removed_flags(args, logger)

    monitor = ProcessMonitor(verbose=args.verbose)
    logger.info(
        f"Sampling processes for {DEFAULT_CPU_SAMPLE_INTERVAL:.1f}s "
        "(CPU usage is a delta, a single reading is always zero)"
    )
    observations = monitor.scan_once(cpu_sample_interval=DEFAULT_CPU_SAMPLE_INTERVAL)

    if not observations:
        logger.success("Nothing stood out")
        return 0

    logger.header(f"{len(observations)} observation(s)")
    for observation in observations:
        _print_observation(observation, logger)
        logger.blank()

    logger.info("Report only: no process was signalled and no file was touched.")
    return 0


def _handle_run(args: Namespace) -> int:
    """
    Run the observation loop in the foreground.

    Args:
        args: Parsed arguments.

    Returns:
        Exit code.
    """
    logger = Logger(verbose=args.verbose)
    monitor = ProcessMonitor(verbose=args.verbose)

    logger.info(f"Starting monitor, scanning every {monitor.config.scan_interval}s")
    logger.info("Press Ctrl+C to stop")

    try:
        monitor.run()
    except KeyboardInterrupt:
        monitor.stop()
        logger.info("Stopped")

    return 0


def _handle_install(args: Namespace) -> int:
    """
    Write the systemd unit without enabling it.

    Args:
        args: Parsed arguments.

    Returns:
        Exit code.
    """
    _require_root("monitor install")
    logger = Logger(verbose=args.verbose)

    ProcessMonitor(verbose=args.verbose).install_service()
    logger.info("Enable it with: wasm monitor enable")
    return 0


def _handle_enable(args: Namespace) -> int:
    """
    Install the unit if needed, then enable and start it.

    Args:
        args: Parsed arguments.

    Returns:
        Exit code.

    Raises:
        MonitorError: When psutil is missing or systemd refuses.
    """
    _require_root("monitor enable")
    logger = Logger(verbose=args.verbose)

    logger.step(1, 3, "Checking dependencies")
    try:
        import psutil  # noqa: F401
    except ImportError as exc:
        raise MonitorError(
            "psutil is required to run the monitor",
            details=(
                "Install the distribution package (python3-psutil) or "
                "pip install 'wasm-cli[monitor]'."
            ),
        ) from exc
    logger.success("psutil available")

    monitor = ProcessMonitor(verbose=args.verbose)

    logger.step(2, 3, "Installing the service")
    if monitor.get_service_status()["installed"]:
        logger.success("Service already installed")
    else:
        monitor.install_service()

    logger.step(3, 3, "Enabling the service")
    monitor.enable_service()

    logger.info(f"Scanning every {monitor.config.scan_interval}s. Logs: journalctl -u wasm-monitor")
    _print_scope(logger)
    return 0


def _handle_disable(args: Namespace) -> int:
    """
    Stop the service and take it out of the boot sequence.

    Args:
        args: Parsed arguments.

    Returns:
        Exit code.
    """
    _require_root("monitor disable")
    logger = Logger(verbose=args.verbose)
    monitor = ProcessMonitor(verbose=args.verbose)

    if not monitor.get_service_status()["installed"]:
        logger.error("Monitor service is not installed")
        return 1

    monitor.disable_service()
    return 0


def _handle_uninstall(args: Namespace) -> int:
    """
    Stop the service and remove the unit WASM wrote.

    Args:
        args: Parsed arguments.

    Returns:
        Exit code.
    """
    _require_root("monitor uninstall")
    logger = Logger(verbose=args.verbose)
    monitor = ProcessMonitor(verbose=args.verbose)

    if not monitor.get_service_status()["installed"]:
        logger.warning("Monitor service is not installed")
        return 0

    monitor.uninstall_service()
    return 0


def _handle_test_email(args: Namespace) -> int:
    """
    Send a message to prove the notification settings work.

    Args:
        args: Parsed arguments.

    Returns:
        Exit code.
    """
    logger = Logger(verbose=args.verbose)
    notifier = EmailNotifier(verbose=args.verbose)

    if not notifier.recipients:
        logger.error(
            "No email recipients configured",
            "Set monitor.email_recipients in /etc/wasm/config.yaml",
        )
        return 1

    logger.info(f"Sending a test email to: {', '.join(notifier.recipients)}")
    try:
        notifier.send_test_email()
    except EmailError as exc:
        # WASMError.__str__ already appends the details; passing them twice prints twice.
        logger.error(str(exc))
        return 1

    logger.success("Test email sent")
    return 0


def _handle_config(args: Namespace) -> int:
    """
    Print the monitor settings that exist.

    Args:
        args: Parsed arguments.

    Returns:
        Exit code.
    """
    logger = Logger(verbose=args.verbose)
    config = Config()

    logger.header("Monitor configuration")
    logger.key_value("Enabled at boot", str(config.get("monitor.enabled", False)))
    logger.key_value(
        "Scan interval", f"{config.get('monitor.scan_interval', DEFAULT_SCAN_INTERVAL)}s"
    )
    logger.key_value(
        "CPU threshold", f"{config.get('monitor.cpu_threshold', DEFAULT_CPU_THRESHOLD)}%"
    )
    logger.key_value(
        "Memory threshold", f"{config.get('monitor.memory_threshold', DEFAULT_MEMORY_THRESHOLD)}%"
    )
    watch_units = config.get("monitor.watch_units", []) or []
    logger.key_value("Watched units", ", ".join(watch_units) or "none")

    logger.section("Observation store")
    logger.key_value("Database", str(default_db_path()))
    logger.key_value(
        "Retention", f"{config.get('monitor.retention_days', DEFAULT_RETENTION_DAYS)} days"
    )
    logger.key_value(
        "Row cap", str(config.get("monitor.max_observations", DEFAULT_MAX_OBSERVATIONS))
    )

    logger.section("Email reports")
    smtp_host = config.get("monitor.smtp.host", "")
    logger.key_value("SMTP host", smtp_host or "Not configured")
    logger.key_value("SMTP port", str(config.get("monitor.smtp.port", 465)))
    recipients = config.get("monitor.email_recipients", []) or []
    logger.key_value("Recipients", ", ".join(recipients) or "Not configured")

    _print_scope(logger)
    return 0


#: Every action ``wasm monitor`` accepts. Exported so the parser and the tests
#: agree on one list instead of two that drift.
ACTIONS: dict[str, Callable[[Namespace], int]] = {
    "status": _handle_status,
    "info": _handle_status,
    "scan": _handle_scan,
    "run": _handle_run,
    "install": _handle_install,
    "enable": _handle_enable,
    "disable": _handle_disable,
    "uninstall": _handle_uninstall,
    "test-email": _handle_test_email,
    "config": _handle_config,
}
