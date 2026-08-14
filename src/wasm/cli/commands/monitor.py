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

Each action is a plain function taking the values it needs. The Click group and
the argparse handler that :mod:`wasm.cli.parser` still calls both dispatch
through :data:`ACTIONS`, so neither entry point can drift from the other while
the migration finishes.
"""

from __future__ import annotations

from argparse import Namespace
from collections.abc import Callable
from typing import Any

import click

from wasm.cli.app import Context, pass_context
from wasm.cli.panel_links import open_in_panel
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
#: The key is the argparse destination the flag was parsed into.
REMOVED_SCAN_FLAGS: tuple[tuple[str, str], ...] = (
    ("force_ai", "--force-ai"),
    ("all", "--all"),
)

#: Alternative spellings for the actions of this group. Local, because the root
#: alias table only rewrites the first word of the command line.
LOCAL_ALIASES: dict[str, str] = {"info": "status"}


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


def _ignored_flags(force_ai: bool = False, all_processes: bool = False) -> tuple[str, ...]:
    """
    Name the retired scan flags the caller passed.

    Args:
        force_ai: Whether ``--force-ai`` was given.
        all_processes: Whether ``--all`` was given.

    Returns:
        The flags, spelled as the user typed them.
    """
    passed = {"force_ai": force_ai, "all": all_processes}
    return tuple(flag for destination, flag in REMOVED_SCAN_FLAGS if passed[destination])


def _warn_about_removed_flags(flags: tuple[str, ...], logger: Logger) -> None:
    """
    Tell the user that the AI scan options no longer do anything.

    Args:
        flags: Retired flags the caller passed.
        logger: Logger to report through.
    """
    for flag in flags:
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


def _show_status(verbose: bool = False) -> int:
    """
    Show the state of the monitor service and what it is set to observe.

    Args:
        verbose: Print the detail of each step.

    Returns:
        Exit code.
    """
    logger = Logger(verbose=verbose)
    monitor = ProcessMonitor(verbose=verbose)
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


def _run_scan(verbose: bool = False, ignored_flags: tuple[str, ...] = ()) -> int:
    """
    Run a single scan and print what it noticed.

    Args:
        verbose: Print the detail of each step.
        ignored_flags: Retired flags the caller passed, reported back so a
            script that still sends one learns that it does nothing.

    Returns:
        Exit code.
    """
    logger = Logger(verbose=verbose)
    _warn_about_removed_flags(ignored_flags, logger)

    monitor = ProcessMonitor(verbose=verbose)
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


def _run_foreground(verbose: bool = False) -> int:
    """
    Run the observation loop in the foreground.

    Args:
        verbose: Print the detail of each step.

    Returns:
        Exit code.
    """
    logger = Logger(verbose=verbose)
    monitor = ProcessMonitor(verbose=verbose)

    logger.info(f"Starting monitor, scanning every {monitor.config.scan_interval}s")
    logger.info("Press Ctrl+C to stop")

    try:
        monitor.run()
    except KeyboardInterrupt:
        monitor.stop()
        logger.info("Stopped")

    return 0


def _install(verbose: bool = False) -> int:
    """
    Write the systemd unit without enabling it.

    Args:
        verbose: Print the detail of each step.

    Returns:
        Exit code.
    """
    _require_root("monitor install")
    logger = Logger(verbose=verbose)

    ProcessMonitor(verbose=verbose).install_service()
    logger.info("Enable it with: wasm monitor enable")
    return 0


def _enable(verbose: bool = False) -> int:
    """
    Install the unit if needed, then enable and start it.

    Args:
        verbose: Print the detail of each step.

    Returns:
        Exit code.

    Raises:
        MonitorError: When psutil is missing or systemd refuses.
    """
    _require_root("monitor enable")
    logger = Logger(verbose=verbose)

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

    monitor = ProcessMonitor(verbose=verbose)

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


def _disable(verbose: bool = False) -> int:
    """
    Stop the service and take it out of the boot sequence.

    Args:
        verbose: Print the detail of each step.

    Returns:
        Exit code.
    """
    _require_root("monitor disable")
    logger = Logger(verbose=verbose)
    monitor = ProcessMonitor(verbose=verbose)

    if not monitor.get_service_status()["installed"]:
        logger.error("Monitor service is not installed")
        return 1

    monitor.disable_service()
    return 0


def _uninstall(verbose: bool = False) -> int:
    """
    Stop the service and remove the unit WASM wrote.

    Args:
        verbose: Print the detail of each step.

    Returns:
        Exit code.
    """
    _require_root("monitor uninstall")
    logger = Logger(verbose=verbose)
    monitor = ProcessMonitor(verbose=verbose)

    if not monitor.get_service_status()["installed"]:
        logger.warning("Monitor service is not installed")
        return 0

    monitor.uninstall_service()
    return 0


def _test_email(verbose: bool = False) -> int:
    """
    Send a message to prove the notification settings work.

    Args:
        verbose: Print the detail of each step.

    Returns:
        Exit code.
    """
    logger = Logger(verbose=verbose)
    notifier = EmailNotifier(verbose=verbose)

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


def _show_config(verbose: bool = False) -> int:
    """
    Print the monitor settings that exist.

    Args:
        verbose: Print the detail of each step.

    Returns:
        Exit code.
    """
    logger = Logger(verbose=verbose)
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


#: Every action ``wasm monitor`` accepts. Exported so the parser, the Click
#: group and the tests agree on one list instead of three that drift. Every
#: entry takes ``verbose``; ``scan`` also takes ``ignored_flags``.
ACTIONS: dict[str, Callable[..., int]] = {
    "status": _show_status,
    "info": _show_status,
    "scan": _run_scan,
    "run": _run_foreground,
    "install": _install,
    "enable": _enable,
    "disable": _disable,
    "uninstall": _uninstall,
    "test-email": _test_email,
    "config": _show_config,
}


class MonitorGroup(click.Group):
    """
    A group that answers to the alternative spellings of its own actions.

    The root group rewrites only the first word of the command line, so
    ``wasm monitor info`` has to be resolved here or it stops working.
    """

    def get_command(self, ctx: click.Context, name: str) -> click.Command | None:
        """
        Look a subcommand up, resolving a local alias first.

        Args:
            ctx: Click context.
            name: Name the user typed.

        Returns:
            The command, or None when there is no such action.
        """
        return super().get_command(ctx, LOCAL_ALIASES.get(name, name))

    def resolve_command(
        self, ctx: click.Context, args: list[str]
    ) -> tuple[str | None, click.Command | None, list[str]]:
        """
        Resolve a command, reporting the name the user actually typed.

        Args:
            ctx: Click context.
            args: Remaining command line arguments.

        Returns:
            The typed name, the command and the arguments left over.
        """
        _, command, remaining = super().resolve_command(ctx, args)
        return (command.name if command else None), command, remaining


@click.group("monitor", cls=MonitorGroup)
def cli() -> None:
    """
    Watch this server and write down what stands out.

    The monitor reads resource usage, the process table and the health of the
    units you list, keeps what it noticed in a local database and can mail you
    a report. It never stops a process and never deletes a file.
    """


@cli.command("status")
@click.option(
    "--open",
    "open_panel",
    is_flag=True,
    help="Print the panel URL for the dashboard, opening it if a display is available.",
)
@pass_context
def status(ctx: Context, open_panel: bool) -> int:
    """Show whether the monitor is running and what it is watching."""
    code = _show_status(verbose=ctx.verbose)
    if open_panel and code == 0:
        # A fresh Logger, like every other action in this module, rather than
        # ctx.logger: this file never reads the shared context's logger, and
        # _show_status already reports through one built the same way.
        open_in_panel("/", logger=Logger(verbose=ctx.verbose))
    return code


@cli.command("scan")
@click.option(
    "--force-ai",
    is_flag=True,
    help="Retired. The monitor analyses nothing off this machine; it only reports.",
)
@click.option(
    "--all",
    "all_processes",
    is_flag=True,
    help="Retired. Every scan already walks the whole process table.",
)
@pass_context
def scan(ctx: Context, force_ai: bool, all_processes: bool) -> int:
    """
    Look at the machine once and print what stands out.

    Samples CPU over a short window, so it takes a moment to answer.
    """
    return _run_scan(
        verbose=ctx.verbose,
        ignored_flags=_ignored_flags(force_ai=force_ai, all_processes=all_processes),
    )


@cli.command("run")
@pass_context
def run(ctx: Context) -> int:
    """Scan on a loop in this terminal, until you press Ctrl+C."""
    return _run_foreground(verbose=ctx.verbose)


@cli.command("install")
@pass_context
def install(ctx: Context) -> int:
    """Write the monitor's systemd unit, without starting it."""
    return _install(verbose=ctx.verbose)


@cli.command("enable")
@pass_context
def enable(ctx: Context) -> int:
    """Start the monitor now and on every boot, installing it if needed."""
    return _enable(verbose=ctx.verbose)


@cli.command("disable")
@pass_context
def disable(ctx: Context) -> int:
    """Stop the monitor and keep it from starting at boot."""
    return _disable(verbose=ctx.verbose)


@cli.command("uninstall")
@click.option("-y", "--yes", is_flag=True, help="Do not ask for confirmation.")
@pass_context
def uninstall(ctx: Context, yes: bool) -> int:
    """
    Remove the monitor's systemd unit from this server.

    Observations already recorded stay in the database.
    """
    if not yes and not click.confirm(
        "Remove the wasm-monitor systemd unit? Nothing will watch this server "
        "until you run 'wasm monitor enable' again"
    ):
        click.echo("Cancelled")
        return 0
    return _uninstall(verbose=ctx.verbose)


@cli.command("test-email")
@pass_context
def test_email(ctx: Context) -> int:
    """Send one email to the configured recipients to prove the settings work."""
    return _test_email(verbose=ctx.verbose)


@cli.command("config")
@pass_context
def config(ctx: Context) -> int:
    """Show the monitor's current settings and where its database lives."""
    return _show_config(verbose=ctx.verbose)


def _namespace_kwargs(args: Namespace) -> dict[str, Any]:
    """
    Pull the arguments an action needs off an argparse namespace.

    Args:
        args: Parsed arguments.

    Returns:
        Keyword arguments for the action.
    """
    kwargs: dict[str, Any] = {"verbose": getattr(args, "verbose", False)}
    if args.action == "scan":
        kwargs["ignored_flags"] = _ignored_flags(
            force_ai=getattr(args, "force_ai", False),
            all_processes=getattr(args, "all", False),
        )
    return kwargs


def handle_monitor(args: Namespace) -> int:
    """
    Route a ``wasm monitor`` invocation to its handler.

    Kept while :mod:`wasm.cli.parser` still routes through argparse; it shares
    :data:`ACTIONS` with the Click group rather than repeating it.

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
        return handler(**_namespace_kwargs(args))
    except WASMError as exc:
        # WASMError.__str__ already appends the details; passing them twice prints twice.
        logger.error(str(exc))
        return 1
    except KeyboardInterrupt:
        logger.info("Interrupted")
        return 130
