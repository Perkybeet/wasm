# Copyright (c) 2024-2026 Yago Lopez Prado
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
``wasm app``: settings of one deployed application that are not a deploy.

``migrate`` moves an in-place application onto the release layout; it is
:func:`wasm.deployers.migrate.plan_migration` and
:func:`~wasm.deployers.migrate.migrate`, which ``POST /api/apps/{d}/migrate``
calls too. ``limits`` sets the memory, CPU and task limits of its unit through
:func:`wasm.deployers.lifecycle.set_resource_limits`, like ``PATCH
/api/apps/{d}/limits``. ``health`` sets what the health gate asks of it
through :func:`wasm.deployers.lifecycle.set_health_check`, like ``PATCH
/api/apps/{d}/health``. This module only parses, presents and asks.
"""

from __future__ import annotations

import dataclasses
import json
import re

import click

from wasm.cli.app import Context, WasmGroup, global_flags, json_option, pass_context
from wasm.core.exceptions import WASMError
from wasm.core.logger import Logger
from wasm.core.store import App, DeploymentTrigger, get_store
from wasm.deployers.helpers.health_gate import HealthCheck
from wasm.deployers.lifecycle import set_health_check, set_resource_limits
from wasm.deployers.migrate import MigrationPlan, migrate, plan_migration
from wasm.deployers.recorder import CapturingLogger
from wasm.managers.service_manager import ResourceLimits

#: What removes a limit instead of setting one.
NO_LIMIT = frozenset({"none", "unlimited", "off"})


def print_plan(logger: Logger, plan: MigrationPlan) -> None:
    """
    Render a migration plan for a human.

    Args:
        logger: Logger the command writes through.
        plan: The plan.
    """
    logger.key_value("Application", f"{plan.domain} ({plan.app_path})")
    logger.key_value("First release", f"{plan.release_id} (the live tree, moved, not copied)")
    how = {
        "git": "what git does not track",
        "explicit": "as named",
        "common": "the usual upload directories that exist",
    }[plan.persistent_source]
    logger.key_value(
        "Moves to shared/", ", ".join([*plan.env_files, *plan.persistent]) or "nothing"
    )
    logger.key_value("Persistent paths", f"{', '.join(plan.persistent) or 'none'} ({how})")
    logger.key_value("Unit", "rewritten to run from current" if plan.unit_rewrite else "unchanged")
    logger.key_value("Site", "rewritten to serve current" if plan.site_rewrite else "unchanged")
    logger.key_value(
        "Files", f"{plan.count.files} ({plan.count.bytes} bytes), all kept, none copied"
    )
    for warning in plan.warnings:
        logger.warning(warning)


@click.group("app", cls=WasmGroup)
def cli() -> None:
    """Change how a deployed application is laid out and what it may use."""


@cli.command("migrate")
@click.argument("domain")
@click.option(
    "--persist",
    "persist",
    multiple=True,
    metavar="PATH",
    help="Keep this path in shared/ across releases. Repeat for each; replaces detection.",
)
@click.option("--yes", "-y", is_flag=True, default=False, help="Do not ask for confirmation.")
@json_option("Print the migration plan (or, with --yes, its result) as JSON.")
@pass_context
def migrate_command(ctx: Context, domain: str, persist: tuple[str, ...], yes: bool) -> None:
    """
    Move an in-place application onto the release layout.

    The live tree becomes the first release; the .env and every path the
    application writes for itself move to shared/; the unit and the site are
    rewritten to run from current. Nothing is deleted and nothing is copied.
    If the application does not answer afterwards, everything is put back as
    it was.
    """
    plan = plan_migration(domain, list(persist) if persist else None)
    if not ctx.json_output:
        print_plan(ctx.logger, plan)
    elif not (yes or ctx.dry_run):
        # A script asked for JSON without --yes: there is nobody to confirm,
        # so it gets the plan and nothing changes.
        click.echo(json.dumps({"plan": dataclasses.asdict(plan)}, default=str))
        return

    if not yes and not ctx.dry_run:
        click.confirm(f"Migrate {plan.domain} to releases?", abort=True)

    logger = CapturingLogger(verbose=ctx.verbose)
    result = migrate(domain, plan, trigger=DeploymentTrigger.CLI.value, logger=logger)
    if ctx.json_output:
        click.echo(
            json.dumps(
                {"plan": dataclasses.asdict(plan), "result": dataclasses.asdict(result)},
                default=str,
            )
        )
        return
    if result.rehearsed:
        logger.info("Rehearsal: nothing was changed")
        return
    logger.success(f"{result.domain} runs from release {result.release_id}")
    logger.info(
        f"Kept {result.after.files} files ({result.after.bytes} bytes); "
        f"see its releases with: wasm releases list {result.domain}"
    )


def parse_memory(value: str) -> int | None:
    """
    Read a memory limit as an operator writes it.

    Args:
        value: ``512M``, ``512``, ``2G`` or ``none``.

    Returns:
        Megabytes, or None to remove the limit.

    Raises:
        click.BadParameter: It is none of those.
    """
    if value.strip().lower() in NO_LIMIT:
        return None
    match = re.fullmatch(r"\s*(\d+)\s*([mMgG])?[bB]?\s*", value)
    if match is None:
        raise click.BadParameter(f"{value!r} is not a size; use 512M, 2G or none")
    amount = int(match.group(1))
    return amount * 1024 if (match.group(2) or "M").upper() == "G" else amount


def parse_cpu(value: str) -> int | None:
    """
    Read a CPU quota as an operator writes it.

    Args:
        value: ``50%``, ``50``, ``200%`` (two CPUs) or ``none``.

    Returns:
        Percent of one CPU, or None to remove the limit.

    Raises:
        click.BadParameter: It is none of those.
    """
    if value.strip().lower() in NO_LIMIT:
        return None
    match = re.fullmatch(r"\s*(\d+)\s*%?\s*", value)
    if match is None:
        raise click.BadParameter(f"{value!r} is not a percentage; use 50%, 200% or none")
    return int(match.group(1))


def parse_tasks(value: str) -> int | None:
    """
    Read a task limit as an operator writes it.

    Args:
        value: A number, or ``none``.

    Returns:
        The limit, or None to remove it.

    Raises:
        click.BadParameter: It is neither.
    """
    if value.strip().lower() in NO_LIMIT:
        return None
    if not value.strip().isdigit():
        raise click.BadParameter(f"{value!r} is not a number of tasks; use 256 or none")
    return int(value)


def _describe(limits: ResourceLimits) -> str:
    """
    Say what limits there are, in unit terms.

    Args:
        limits: The limits.

    Returns:
        The directives, or "no limits".
    """
    return ", ".join(limits.directives()) or "no limits"


@cli.command("limits")
@click.argument("domain")
@click.option("--memory", metavar="SIZE", help="Memory limit: 512M, 2G, or none to remove it.")
@click.option("--cpu", metavar="PERCENT", help="CPU quota: 50% of one CPU, 200% for two, or none.")
@click.option("--tasks", metavar="N", help="Processes and threads it may run, or none.")
@click.option("--restart", is_flag=True, default=False, help="Restart now so the new limits apply.")
@json_option("Print the limits as JSON.")
@pass_context
def limits_command(
    ctx: Context,
    domain: str,
    memory: str | None,
    cpu: str | None,
    tasks: str | None,
    restart: bool,
) -> None:
    """
    Show or set the memory, CPU and task limits of an application.

    A limit not named keeps its value; name it as none to remove it. The
    unit is rewritten and systemd reloaded; the running process keeps its
    old limits until it restarts, which --restart does now.
    """
    app = get_store().get_app(domain)
    if app is None:
        raise WASMError(
            f"Application not found: {domain}", details="Run 'wasm list' to see what is deployed."
        )
    current = ResourceLimits.of(app)
    if memory is None and cpu is None and tasks is None and not restart:
        if ctx.json_output:
            click.echo(json.dumps({"domain": app.domain, **dataclasses.asdict(current)}))
        else:
            ctx.logger.key_value("Limits", _describe(current))
        return

    wanted = ResourceLimits(
        memory_max_mb=current.memory_max_mb if memory is None else parse_memory(memory),
        cpu_quota_percent=current.cpu_quota_percent if cpu is None else parse_cpu(cpu),
        tasks_max=current.tasks_max if tasks is None else parse_tasks(tasks),
    )
    change = set_resource_limits(app.domain, wanted, restart=restart)
    if ctx.json_output:
        click.echo(
            json.dumps(
                {
                    "domain": change.domain,
                    **dataclasses.asdict(change.limits),
                    "units": list(change.units),
                    "restarted": change.restarted,
                }
            )
        )
        return
    ctx.logger.success(f"{change.domain}: {_describe(change.limits)}")
    if change.restarted:
        ctx.logger.info(f"Restarted {', '.join(change.units)} under the new limits")
    else:
        ctx.logger.info(
            f"The running process keeps its old limits until it restarts: wasm restart {domain}"
        )


def health_settings(app: App) -> dict[str, object]:
    """
    Describe an application's health check: what it set, and what the gate uses.

    Args:
        app: The application.

    Returns:
        The stored values (None where it keeps the default) and, under
        ``effective``, what the gate actually asks.
    """
    check = HealthCheck.for_app(app)
    return {
        "domain": app.domain,
        "path": app.health_path,
        "expect": app.health_expect,
        "timeout": app.health_timeout,
        "effective": {
            "path": check.path,
            "expect": check.describe_expect(),
            "timeout": check.seconds,
        },
    }


@cli.command("health")
@click.argument("domain")
@click.option("--path", metavar="PATH", help="Path the health check requests, such as /healthz.")
@click.option(
    "--expect",
    metavar="STATUSES",
    help="Statuses that mean up: 200-399, or 200,204. Default: any status below 500.",
)
@click.option("--timeout", type=int, metavar="SECONDS", help="Seconds it gets to answer, 5 to 600.")
@click.option("--reset", is_flag=True, default=False, help="Go back to the defaults for all three.")
@global_flags
@json_option("Print the health check settings as JSON.")
@pass_context
def health_command(
    ctx: Context,
    domain: str,
    path: str | None,
    expect: str | None,
    timeout: int | None,
    reset: bool,
) -> None:
    """
    Show or set what the health gate asks of an application.

    Every deploy, update, rollback and migration keeps the new release only
    if it answers the health check; the diagnosis asks the same. An option
    not named keeps its value. Nothing restarts: the next activation uses
    the new settings.
    """
    app = get_store().get_app(domain)
    if app is None:
        raise WASMError(
            f"Application not found: {domain}", details="Run 'wasm list' to see what is deployed."
        )
    named = path is not None or expect is not None or timeout is not None
    if reset and named:
        raise click.UsageError("--reset goes back to every default; name no value with it.")
    if reset or named:
        app = set_health_check(
            app.domain,
            path=None if reset else (path if path is not None else app.health_path),
            expect=None if reset else (expect if expect is not None else app.health_expect),
            timeout=None if reset else (timeout if timeout is not None else app.health_timeout),
        )
    settings = health_settings(app)
    if ctx.json_output:
        click.echo(json.dumps(settings))
        return
    if reset or named:
        ctx.logger.success(f"Health check of {app.domain} updated")
    check = HealthCheck.for_app(app)
    ctx.logger.key_value("Path", check.path + ("" if app.health_path else " (default)"))
    ctx.logger.key_value(
        "Healthy", check.describe_expect() + ("" if app.health_expect else " (default)")
    )
    ctx.logger.key_value(
        "Timeout", f"{check.seconds} s" + ("" if app.health_timeout is not None else " (default)")
    )
