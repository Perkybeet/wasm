# Copyright (c) 2024-2026 Yago Lopez Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
The ``wasm service`` commands.

Every operation here goes through :class:`~wasm.managers.service_manager.ServiceManager`.
That is not indirection for its own sake: the manager holds the ownership guard
that refuses to touch a unit WASM did not install, and building a unit path or
calling ``systemctl`` from this module walks straight past it.

The command bodies live in the private ``_`` functions below so that the Click
commands and the argparse-era :func:`handle_service` call one implementation
while the migration is in flight.
"""

from __future__ import annotations

import sys
from argparse import Namespace

import click

from wasm.cli.app import Context, pass_context
from wasm.core.exceptions import WASMError
from wasm.core.logger import Logger
from wasm.managers.service_manager import ServiceManager

#: Alternative spellings for the subcommands of this group. They predate the
#: migration, are in scripts and in the published documentation, and dropping
#: one is a breaking change.
ALIASES: dict[str, str] = {
    "info": "status",
    "ls": "list",
    "remove": "delete",
    "rm": "delete",
}

#: Default account a new unit runs as, matching the web server's own user.
DEFAULT_USER = "www-data"

#: Journal lines shown when the caller does not ask for a different number.
DEFAULT_LINES = 50


class ServiceGroup(click.Group):
    """A group that also answers to the historical subcommand spellings."""

    def get_command(self, ctx: click.Context, name: str) -> click.Command | None:
        """
        Look a subcommand up, resolving an alias to its canonical name.

        Args:
            ctx: Click context.
            name: Name the user typed.

        Returns:
            The command, or None when nothing matches.
        """
        return super().get_command(ctx, ALIASES.get(name, name))


def _finish(code: int) -> None:
    """
    End the current command with an exit code.

    Args:
        code: Process exit code to report.
    """
    click.get_current_context().exit(code)


# Implementations ----------------------------------------------------------


def _create(
    name: str,
    command: str,
    directory: str,
    user: str,
    description: str | None,
    *,
    verbose: bool,
) -> int:
    """
    Install a systemd unit.

    Args:
        name: Service name.
        command: Command the unit runs.
        directory: Working directory for the unit.
        user: Account the unit runs as.
        description: Human readable description, or None for a generated one.
        verbose: Show the detail of each step.

    Returns:
        Exit code.

    Raises:
        ServiceError: When the unit exists or is not WASM's to create.
        ValidationError: When a name or directive value is unsafe.
    """
    logger = Logger(verbose=verbose)
    manager = ServiceManager(verbose=verbose)

    logger.info(f"Creating service: {name}")

    manager.create_service(
        name=name,
        command=command,
        working_directory=directory,
        user=user,
        description=description,
    )

    logger.success(f"Service created: {name}")
    return 0


def _list(all_services: bool, *, verbose: bool) -> int:
    """
    Print the services WASM manages.

    Args:
        all_services: Include units WASM does not manage.
        verbose: Show the detail of each step.

    Returns:
        Exit code.
    """
    logger = Logger(verbose=verbose)
    manager = ServiceManager(verbose=verbose)

    logger.header("Managed Services")

    services = manager.list_services(all_services=all_services)

    if not services:
        logger.info("No services found")
        return 0

    rows = [
        [svc["name"], "running" if svc["active"] == "active" else "stopped", svc["sub"]]
        for svc in services
    ]
    logger.table(["Name", "Status", "State"], rows)

    return 0


def _status(name: str, *, verbose: bool) -> int:
    """
    Print what systemd reports about one service.

    Args:
        name: Service name.
        verbose: Show the detail of each step.

    Returns:
        Exit code, 1 when the service does not exist.

    Raises:
        ServiceError: When the unit exists but is not WASM's.
        ValidationError: When the name is not a safe unit name.
    """
    logger = Logger(verbose=verbose)
    manager = ServiceManager(verbose=verbose)

    status = manager.get_status(name)

    logger.header(f"Service: {name}")

    if not status["exists"]:
        logger.warning("Service not found")
        return 1

    logger.key_value("Name", status["name"])
    logger.key_value("Active", "Yes" if status["active"] else "No")
    logger.key_value("Enabled", "Yes" if status["enabled"] else "No")

    # A stopped unit reports PID 0, which tells the reader nothing.
    if status.get("pid") and status["pid"] != "0":
        logger.key_value("PID", status["pid"])
    if status.get("uptime"):
        logger.key_value("Started", status["uptime"])

    return 0


def _start(name: str, *, verbose: bool) -> int:
    """
    Start a service.

    Args:
        name: Service name.
        verbose: Show the detail of each step.

    Returns:
        Exit code.

    Raises:
        ServiceError: When the unit is not WASM's or systemd refuses to start it.
        ValidationError: When the name is not a safe unit name.
    """
    logger = Logger(verbose=verbose)
    manager = ServiceManager(verbose=verbose)

    logger.info(f"Starting service: {name}")
    manager.start(name)
    logger.success(f"Service started: {name}")

    return 0


def _stop(name: str, *, verbose: bool) -> int:
    """
    Stop a service.

    Args:
        name: Service name.
        verbose: Show the detail of each step.

    Returns:
        Exit code.

    Raises:
        ServiceError: When the unit is not WASM's or does not exist.
        ValidationError: When the name is not a safe unit name.
    """
    logger = Logger(verbose=verbose)
    manager = ServiceManager(verbose=verbose)

    logger.info(f"Stopping service: {name}")
    manager.stop(name)
    logger.success(f"Service stopped: {name}")

    return 0


def _restart(name: str, *, verbose: bool) -> int:
    """
    Restart a service.

    Args:
        name: Service name.
        verbose: Show the detail of each step.

    Returns:
        Exit code.

    Raises:
        ServiceError: When the unit is not WASM's or systemd refuses to restart it.
        ValidationError: When the name is not a safe unit name.
    """
    logger = Logger(verbose=verbose)
    manager = ServiceManager(verbose=verbose)

    logger.info(f"Restarting service: {name}")
    manager.restart(name)
    logger.success(f"Service restarted: {name}")

    return 0


def _logs(name: str, follow: bool, lines: int, *, verbose: bool) -> int:
    """
    Print journal entries for a service.

    Args:
        name: Service name.
        follow: Keep printing new entries until interrupted.
        lines: Number of past entries to show.
        verbose: Show the detail of each step.

    Returns:
        Exit code.

    Raises:
        ServiceError: When the unit is not WASM's or does not exist.
        ValidationError: When the name is not a safe unit name.
    """
    manager = ServiceManager(verbose=verbose)

    if follow:
        try:
            manager.follow_logs(name, on_line=click.echo, lines=lines)
        except KeyboardInterrupt:
            # Ctrl-C is how a reader leaves a follow, not a failure.
            pass
        return 0

    click.echo(manager.logs(name, lines=lines))
    return 0


def _delete(name: str, force: bool, *, verbose: bool) -> int:
    """
    Stop, disable and remove a service.

    Args:
        name: Service name.
        force: Skip the confirmation prompt.
        verbose: Show the detail of each step.

    Returns:
        Exit code.

    Raises:
        ServiceError: When the unit is not WASM's or cannot be removed.
        ValidationError: When the name is not a safe unit name.
    """
    logger = Logger(verbose=verbose)
    manager = ServiceManager(verbose=verbose)

    if not force and not click.confirm(
        f"Stop '{name}', disable it at boot and delete its unit file?",
        default=False,
    ):
        logger.info("Aborted")
        return 0

    logger.info(f"Deleting service: {name}")
    manager.delete_service(name)
    logger.success(f"Service deleted: {name}")

    return 0


# Commands -----------------------------------------------------------------


@click.group(cls=ServiceGroup, name="service")
def cli() -> None:
    """Manage the systemd services WASM owns."""


@cli.command(name="create")
@click.option("--name", "-n", required=True, help="Name for the new service.")
@click.option("--command", "-c", required=True, help="Command the service runs.")
@click.option(
    "--directory",
    "-d",
    required=True,
    type=click.Path(file_okay=False),
    help="Directory the command runs in.",
)
@click.option("--user", "-u", default=DEFAULT_USER, show_default=True, help="Account to run as.")
@click.option("--description", default=None, help="Short description shown by systemd.")
@pass_context
def create_command(
    ctx: Context,
    name: str,
    command: str,
    directory: str,
    user: str,
    description: str | None,
) -> None:
    """Create a service and install its unit file."""
    _finish(_create(name, command, directory, user, description, verbose=ctx.verbose))


@cli.command(name="list")
@click.option("--all", "-a", "all_services", is_flag=True, help="Include units WASM does not own.")
@pass_context
def list_command(ctx: Context, all_services: bool) -> None:
    """List services and whether they are running."""
    _finish(_list(all_services, verbose=ctx.verbose))


@cli.command(name="status")
@click.argument("name")
@pass_context
def status_command(ctx: Context, name: str) -> None:
    """Show whether a service is running and enabled."""
    _finish(_status(name, verbose=ctx.verbose))


@cli.command(name="start")
@click.argument("name")
@pass_context
def start_command(ctx: Context, name: str) -> None:
    """Start a service."""
    _finish(_start(name, verbose=ctx.verbose))


@cli.command(name="stop")
@click.argument("name")
@pass_context
def stop_command(ctx: Context, name: str) -> None:
    """Stop a service."""
    _finish(_stop(name, verbose=ctx.verbose))


@cli.command(name="restart")
@click.argument("name")
@pass_context
def restart_command(ctx: Context, name: str) -> None:
    """Restart a service."""
    _finish(_restart(name, verbose=ctx.verbose))


@cli.command(name="logs")
@click.argument("name")
@click.option("--follow", "-f", is_flag=True, help="Keep printing new entries until interrupted.")
@click.option(
    "--lines",
    "-n",
    type=click.INT,
    default=DEFAULT_LINES,
    show_default=True,
    help="How many past entries to show.",
)
@pass_context
def logs_command(ctx: Context, name: str, follow: bool, lines: int) -> None:
    """Show the journal for a service."""
    _finish(_logs(name, follow, lines, verbose=ctx.verbose))


@cli.command(name="delete")
@click.argument("name")
@click.option("--force", "-f", "-y", is_flag=True, help="Delete without asking for confirmation.")
@pass_context
def delete_command(ctx: Context, name: str, force: bool) -> None:
    """Stop a service, disable it and remove its unit file."""
    _finish(_delete(name, force, verbose=ctx.verbose))


# argparse compatibility ----------------------------------------------------


def handle_service(args: Namespace) -> int:
    """
    Route an argparse invocation to the same implementations the Click tree uses.

    ``wasm.cli.parser`` still calls this. It goes away with the last argparse
    parser; until then it must not grow a second copy of the logic.

    Args:
        args: Parsed arguments.

    Returns:
        Exit code.
    """
    action = ALIASES.get(args.action, args.action)
    verbose = args.verbose

    try:
        if action == "create":
            return _create(
                args.name,
                args.exec_command,
                args.directory,
                args.user,
                args.description,
                verbose=verbose,
            )
        if action == "list":
            return _list(getattr(args, "all", False), verbose=verbose)
        if action == "status":
            return _status(args.name, verbose=verbose)
        if action == "start":
            return _start(args.name, verbose=verbose)
        if action == "stop":
            return _stop(args.name, verbose=verbose)
        if action == "restart":
            return _restart(args.name, verbose=verbose)
        if action == "logs":
            return _logs(args.name, args.follow, args.lines, verbose=verbose)
        if action == "delete":
            return _delete(args.name, args.force, verbose=verbose)
    except WASMError as exc:
        logger = Logger(verbose=verbose)
        logger.error(str(exc))
        if exc.details:
            logger.info(exc.details)
        return 1

    print(f"Unknown action: {args.action}", file=sys.stderr)
    return 1
