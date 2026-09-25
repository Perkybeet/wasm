# Copyright (c) 2024-2025 Yago López Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
Health check command for WASM.

Provides system-wide health diagnostics.

This is the command an operator runs when something looks wrong, so it is the
last place that should quietly report nothing. Two defects made it do exactly
that: it asked ServiceManager for a ``status`` method that does not exist, so
every application was counted as failed, and it looked for an ``expires`` key
in certificate data that carries ``expiry``, so no certificate ever appeared to
be close to renewal.

The check itself lives in :mod:`wasm.managers.health`, as
:func:`~wasm.managers.health.collect_health_report`, so ``GET
/api/system/health`` reports exactly what this command does rather than a
second opinion. This module is the presentation layer over it: the Click
command and the argparse-shaped :func:`handle_health` both go through
:func:`run_health_check`, so the two cannot drift. ``wasm.cli.parser`` is gone
and nothing calls :func:`handle_health` in production anymore; it is kept, and
tested directly, for the same reason.
"""

from __future__ import annotations

from argparse import Namespace

import click

from wasm.cli.app import Context, pass_context
from wasm.core.logger import Logger
from wasm.managers.health import HealthCheck, HealthReport, collect_health_report


def _print_status(logger: Logger, key: str, value: str, status: str) -> None:
    """
    Print a key-value pair with status indicator.

    Args:
        logger: Logger of the current command. Writing through it is what makes
            ``wasm --no-color health`` colourless; the escape codes used to be
            written to stdout directly, so the flag did nothing here.
        key: Name of the checked item.
        value: Human readable result.
        status: One of "ok", "warning", "error" or "info".
    """
    logger.check(key, value, status)


def _print_check(logger: Logger, check: HealthCheck | None) -> None:
    """
    Print one check, when it ran.

    Args:
        logger: Logger of the current command.
        check: The check to print, or ``None`` when the section could not run
            at all (only the disk space check can skip outright).
    """
    if check is not None:
        _print_status(logger, check.name, check.value, check.status)


def _print_report(logger: Logger, report: HealthReport) -> None:
    """
    Render a health report the way an operator reads it: section by section,
    in the same order :func:`~wasm.managers.health.collect_health_report`
    runs its checks.

    Args:
        logger: Logger of the current command.
        report: The report to print.
    """
    logger.info("Checking disk space...")
    _print_check(logger, report.disk)

    logger.blank()
    logger.info("Checking web servers...")
    _print_check(logger, report.nginx)
    _print_check(logger, report.apache)

    logger.blank()
    logger.info("Checking deployed applications...")
    _print_check(logger, report.applications)

    logger.blank()
    logger.info("Checking SSL certificates...")
    _print_check(logger, report.certificates)

    logger.blank()
    logger.info("Checking system resources...")
    _print_check(logger, report.memory)

    logger.blank()
    logger.blank()

    if report.issues:
        logger.error(f"Health check found {len(report.issues)} issue(s):")
        for issue in report.issues:
            logger.error(f"  - {issue}")
        logger.blank()

    if report.warnings:
        logger.warning(f"Health check found {len(report.warnings)} warning(s):")
        for warning in report.warnings:
            logger.warning(f"  - {warning}")
        logger.blank()

    if report.verdict == "healthy":
        logger.success("All systems healthy!")
    elif report.verdict == "error":
        logger.error("System has issues that need attention.")
    else:
        logger.warning("System is healthy with minor warnings.")


def run_health_check(verbose: bool = False) -> int:
    """
    Inspect the server and report what is wrong with it.

    Checks disk space, the web servers, every deployed application, the
    certificates close to expiry and memory pressure.

    Args:
        verbose: Print the detail of each step.

    Returns:
        1 when the check found issues, 0 otherwise.
    """
    logger = Logger(verbose=verbose)

    logger.header("System Health Check")
    logger.blank()

    report = collect_health_report(verbose=verbose)
    _print_report(logger, report)

    return 1 if report.verdict == "error" else 0


@click.command("health")
@pass_context
def cli(ctx: Context) -> int:
    """
    Check the server and report anything that needs attention.

    Looks at free disk space, the web server, every deployed application,
    certificates close to expiry and memory pressure. It only reads.
    """
    return run_health_check(verbose=ctx.verbose)


def handle_health(args: Namespace) -> int:
    """
    Handle the health check command.

    ``wasm.cli.parser`` is gone and nothing calls this in production; it is
    kept, and tested directly, sharing :func:`run_health_check` with the Click
    command rather than repeating it.

    Args:
        args: Parsed arguments; only ``verbose`` is read.

    Returns:
        1 when the check found issues, 0 otherwise.
    """
    return run_health_check(verbose=getattr(args, "verbose", False))
