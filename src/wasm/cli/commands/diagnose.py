# Copyright (c) 2024-2026 Yago Lopez Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
``wasm diagnose``: why is this application down.

The command is a thin presentation layer over :func:`wasm.managers.diagnose.diagnose`;
every correlation rule lives there, not here. What this module owns is turning
a :class:`~wasm.managers.diagnose.Diagnosis` into what an operator reads: the
verdict first, the probable cause in bold, then each check with its status and
its evidence printed verbatim underneath - CLAUDE.md's rule that a system
error is never paraphrased applies to the CLI exactly as it does to the panel.
"""

from __future__ import annotations

import dataclasses
import json

import click

from wasm.cli.app import Context, json_option, pass_context
from wasm.core.logger import Colors, Logger
from wasm.managers.diagnose import Diagnosis, diagnose

#: A check's status in Logger.check's vocabulary.
_CHECK_OUTCOME: dict[str, str] = {"ok": "ok", "warn": "warning", "fail": "error", "skip": "info"}

#: A verdict in the same vocabulary, for the headline.
_VERDICT_OUTCOME: dict[str, str] = {"healthy": "ok", "degraded": "warning", "down": "error"}


def _print_evidence(logger: Logger, evidence: str) -> None:
    """
    Print a check's evidence verbatim, indented under its summary.

    Args:
        logger: Logger the command writes through.
        evidence: Raw output the check collected. Nothing is printed when it
            is empty.
    """
    for line in evidence.splitlines():
        logger._write(f"        {line}")


def print_diagnosis(logger: Logger, diagnosis: Diagnosis) -> None:
    """
    Render a diagnosis for a human to read.

    Args:
        logger: Logger the command writes through.
        diagnosis: The result of :func:`wasm.managers.diagnose.diagnose`.
    """
    logger.check("Verdict", diagnosis.verdict, _VERDICT_OUTCOME[diagnosis.verdict])

    if diagnosis.probable_cause:
        logger.blank()
        logger._write(logger._colorize(diagnosis.probable_cause, Colors.BOLD))

    logger.blank()
    for check in diagnosis.checks:
        logger.check(check.name, check.summary, _CHECK_OUTCOME[check.status])
        if check.evidence:
            _print_evidence(logger, check.evidence)

    logger.blank()


@click.command("diagnose")
@click.argument("domain")
@json_option("Print the diagnosis as JSON.")
@pass_context
def cli(ctx: Context, domain: str) -> None:
    """
    Explain why an application is down.

    Correlates the state of the app's systemd unit, the port it is meant to
    serve, an HTTP probe direct to the app and through nginx, its last
    journal lines, nginx's own error log, its certificate, its last
    deployment, OOM kills and disk space, and reports the most likely cause
    first. Every probe only reads; nothing here changes the machine.
    """
    result = diagnose(domain)

    if ctx.json_output:
        click.echo(json.dumps(dataclasses.asdict(result), default=str))
    else:
        print_diagnosis(ctx.logger, result)

    # A plain `return 1` is not enough: Click only turns a callback's return
    # value into a process exit code when the command calls ctx.exit itself,
    # see wasm.cli.commands.backup._finish for the same pattern.
    if result.verdict == "down":
        click.get_current_context().exit(1)
