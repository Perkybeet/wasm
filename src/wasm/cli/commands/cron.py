# Copyright (c) 2024-2026 Yago Lopez Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
The ``wasm cron`` command group.

User cron jobs as systemd timers, over
:class:`~wasm.managers.cron_manager.CronManager`, which owns the timer/service
unit pair, the systemctl calls, the ownership guard and every rule about what
may be written into a root-owned unit file. :mod:`wasm.web.api.cron` is a thin
client of the same manager, so a schedule or a command the panel refuses is
refused here too, in the same words - there is exactly one implementation of
what a cron job is allowed to be.

Every command is a thin shell around a private function that takes explicit
arguments and reports through a :class:`~wasm.core.logger.Logger`, mirroring
how :mod:`wasm.cli.commands.db` is laid out.
"""

from __future__ import annotations

from typing import Any

import click

from wasm.cli.app import Context, pass_context
from wasm.core.exceptions import ServiceError
from wasm.core.logger import Logger, state, styled
from wasm.managers.backup_scheduler import SCHEDULE_ALIASES
from wasm.managers.cron_manager import CronJob, CronManager

#: The alias each expansion came from, so a listing can say "daily" instead of
#: making an operator parse ``*-*-* 02:00:00``. Mirrors
#: :mod:`wasm.web.api.cron`'s own reverse lookup, built from the same source.
_ALIAS_BY_CALENDAR = {calendar: alias for alias, calendar in SCHEDULE_ALIASES.items()}

#: How many runs ``wasm cron runs`` shows by default.
DEFAULT_RUN_LIMIT = 10


def _exit(code: int) -> None:
    """
    End the current command with an exit status.

    Args:
        code: Process exit status.

    Raises:
        click.exceptions.Exit: Always; this is how Click unwinds a command.
    """
    click.get_current_context().exit(code)


def _schedule_label(on_calendar: str) -> str:
    """
    Show a calendar expression as the alias it came from, when there is one.

    Args:
        on_calendar: The systemd ``OnCalendar`` expression, verbatim.

    Returns:
        The alias (``daily``, ``weekly``, ...), or the expression itself when
        it does not match one.
    """
    return _ALIAS_BY_CALENDAR.get(on_calendar, on_calendar or "unknown")


def _list(*, logger: Logger) -> int:
    """
    List every WASM cron job with its schedule, next run and last result.

    Args:
        logger: Logger for the table and the empty-state message.

    Returns:
        Exit code.
    """
    jobs = CronManager(verbose=logger.verbose).list_jobs()

    if not jobs:
        logger.info("No cron jobs")
        logger.blank()
        logger.info("Create one with:")
        logger.info("  wasm cron create <name> '<command>' --schedule daily")
        return 0

    headers = ["Name", "Schedule", "Enabled", "Last run", "Last result", "App", "Directory"]
    rows: list[list[Any]] = []
    for job in jobs:
        rows.append(
            [
                styled(job["name"], "bold"),
                _schedule_label(job.get("on_calendar", "")),
                state("enabled" if job.get("enabled") else "disabled"),
                job.get("last_run", "never"),
                job.get("last_result", "never ran"),
                job.get("app_domain") or styled("-", "dim"),
                # Read back from the unit file's own WorkingDirectory=, so
                # this is the directory the job actually runs in - current/
                # for a releases application, not the app root a stale
                # listing would imply.
                job.get("working_directory") or styled("-", "dim"),
            ]
        )
    logger.table(headers, rows)
    return 0


def _create(
    name: str,
    command: str,
    *,
    schedule: str,
    user: str | None,
    working_directory: str | None,
    app_domain: str | None,
    logger: Logger,
) -> int:
    """
    Create a cron job as a systemd timer, or rewrite one WASM already owns.

    Args:
        name: Job name, becomes part of two unit names.
        command: One command line, split and run without a shell.
        schedule: Alias (hourly, daily, weekly, monthly) or a systemd
            ``OnCalendar`` expression.
        user: Unix user to run as. The configured ``service_user`` by default.
        working_directory: Absolute directory to run in.
        app_domain: Associate the job with a deployed application.
        logger: Logger for progress and errors.

    Returns:
        Exit code.
    """
    try:
        created = CronManager(verbose=logger.verbose).create_job(
            CronJob(
                name=name,
                command=command,
                schedule=schedule,
                user=user,
                working_directory=working_directory,
                app_domain=app_domain,
            )
        )
    except ServiceError as e:
        logger.error(str(e))
        return 1

    logger.success(f"Created cron job: {created.name}")
    logger.key_value("Schedule", _schedule_label(created.on_calendar))
    logger.key_value("Command", command)
    if created.app_domain:
        logger.key_value("Application", created.app_domain)
    return 0


def _delete(name: str, *, force: bool, logger: Logger) -> int:
    """
    Remove a cron job's timer and service units.

    Args:
        name: Job name.
        force: Do not ask for confirmation.
        logger: Logger for progress and errors.

    Returns:
        Exit code.
    """
    if not force:
        question = f"Delete cron job '{name}'? This cannot be undone"
        try:
            confirmed = click.confirm(question, default=False)
        except click.Abort:
            confirmed = False
        if not confirmed:
            logger.info("Cancelled")
            return 0

    try:
        CronManager(verbose=logger.verbose).delete_job(name)
    except ServiceError as e:
        logger.error(str(e))
        return 1

    logger.success(f"Deleted cron job: {name}")
    return 0


def _run_now(name: str, *, logger: Logger) -> int:
    """
    Start a job's service immediately, outside its schedule.

    Args:
        name: Job name.
        logger: Logger for progress and errors.

    Returns:
        Exit code.
    """
    try:
        unit = CronManager(verbose=logger.verbose).run_now(name)
    except ServiceError as e:
        logger.error(str(e))
        return 1

    logger.success(f"Started {unit}")
    logger.info(f"See its result with: wasm cron runs {name}")
    return 0


def _enable(name: str, *, logger: Logger) -> int:
    """
    Enable a job's timer and start it now.

    Args:
        name: Job name.
        logger: Logger for progress and errors.

    Returns:
        Exit code.
    """
    try:
        unit = CronManager(verbose=logger.verbose).enable_job(name)
    except ServiceError as e:
        logger.error(str(e))
        return 1

    logger.success(f"Enabled {unit}")
    return 0


def _disable(name: str, *, logger: Logger) -> int:
    """
    Disable a job's timer, keeping the unit files.

    Args:
        name: Job name.
        logger: Logger for progress and errors.

    Returns:
        Exit code.
    """
    try:
        unit = CronManager(verbose=logger.verbose).disable_job(name)
    except ServiceError as e:
        logger.error(str(e))
        return 1

    logger.success(f"Disabled {unit}")
    return 0


def _runs(name: str, *, limit: int, logger: Logger) -> int:
    """
    Show a job's recent executions, reconstructed from the journal.

    Args:
        name: Job name.
        limit: Most runs to show.
        logger: Logger for the report and errors.

    Returns:
        Exit code.
    """
    try:
        runs = CronManager(verbose=logger.verbose).runs(name, limit=limit)
    except ServiceError as e:
        logger.error(str(e))
        return 1

    if not runs:
        logger.info(f"No recorded runs for {name}")
        return 0

    for run in runs:
        if run["success"] is True:
            outcome, summary = "ok", f"exit {run['exit_code']}"
        elif run["success"] is False:
            outcome, summary = "error", f"exit {run['exit_code']}"
        else:
            outcome, summary = "info", "still running or unknown"
        logger.check(run["started"] or "unknown time", summary, outcome)
        for line in run["lines"]:
            logger._write(f"        {line}")

    return 0


@click.group("cron")
def cli() -> None:
    """Run commands on a schedule, as systemd timers."""


@cli.command("list")
@pass_context
def list_jobs(ctx: Context) -> None:
    """List every WASM cron job with its schedule and last result."""
    _exit(_list(logger=ctx.logger))


@cli.command("create")
@click.argument("name")
@click.argument("command")
@click.option(
    "--schedule",
    default="daily",
    show_default=True,
    help=f"{', '.join(SCHEDULE_ALIASES)}, or a systemd OnCalendar expression.",
)
@click.option("--user", help="Unix user to run as. The configured service_user by default.")
@click.option(
    "--working-directory",
    "working_directory",
    metavar="PATH",
    help="Absolute directory to run the command in.",
)
@click.option(
    "--app",
    "app_domain",
    metavar="DOMAIN",
    help=(
        "Associate the job with a deployed application; its runtime directory "
        "(current/ for a releases application) becomes the default."
    ),
)
@pass_context
def create(
    ctx: Context,
    name: str,
    command: str,
    schedule: str,
    user: str | None,
    working_directory: str | None,
    app_domain: str | None,
) -> None:
    """
    Create a cron job as a systemd timer, or rewrite one WASM already owns.

    COMMAND is one command line, split like a POSIX shell would split it and
    run without a shell: pipes, '&&' and globs are inert text handed to the
    program as arguments. For shell features, write '/bin/sh -c "..."'.
    """
    _exit(
        _create(
            name,
            command,
            schedule=schedule,
            user=user,
            working_directory=working_directory,
            app_domain=app_domain,
            logger=ctx.logger,
        )
    )


@cli.command("delete")
@click.argument("name")
@click.option("-f", "-y", "--force", is_flag=True, help="Do not ask for confirmation.")
@pass_context
def delete(ctx: Context, name: str, force: bool) -> None:
    """Remove a cron job's timer and service units."""
    _exit(_delete(name, force=force, logger=ctx.logger))


@cli.command("run")
@click.argument("name")
@pass_context
def run(ctx: Context, name: str) -> None:
    """Start a job's service immediately, outside its schedule."""
    _exit(_run_now(name, logger=ctx.logger))


@cli.command("enable")
@click.argument("name")
@pass_context
def enable(ctx: Context, name: str) -> None:
    """Enable a job's timer and start it now."""
    _exit(_enable(name, logger=ctx.logger))


@cli.command("disable")
@click.argument("name")
@pass_context
def disable(ctx: Context, name: str) -> None:
    """Disable a job's timer, keeping the unit files."""
    _exit(_disable(name, logger=ctx.logger))


@cli.command("runs")
@click.argument("name")
@click.option(
    "--limit",
    type=click.IntRange(1, 50),
    default=DEFAULT_RUN_LIMIT,
    show_default=True,
    help="Most runs to show.",
)
@pass_context
def runs(ctx: Context, name: str, limit: int) -> None:
    """Show a job's recent executions, reconstructed from the journal."""
    _exit(_runs(name, limit=limit, logger=ctx.logger))
