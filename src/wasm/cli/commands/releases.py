# Copyright (c) 2024-2026 Yago Lopez Prado
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
``wasm releases``: what an application on the release layout has built, and
going back to one of them.

A presentation layer over :func:`wasm.deployers.lifecycle.list_releases`,
:func:`wasm.deployers.lifecycle.activate_release` and
:func:`wasm.deployers.lifecycle.set_release_retention`, the functions the
panel's ``/api/apps/{domain}/releases`` endpoints call too. Going back is instant
because nothing is rebuilt: ``current`` is re-pointed, the unit restarted,
and the release kept only if it passes the same health gate as a deploy.
"""

from __future__ import annotations

import dataclasses
import json

import click

from wasm.cli.app import Context, WasmGroup, json_option, pass_context
from wasm.core.exceptions import WASMError
from wasm.core.logger import Logger
from wasm.core.store import DeploymentTrigger, get_store
from wasm.deployers.lifecycle import (
    ReleaseInfo,
    activate_release,
    list_releases,
    set_release_retention,
)
from wasm.deployers.recorder import CapturingLogger


def print_releases(logger: Logger, domain: str, releases: list[ReleaseInfo]) -> None:
    """
    Render an application's releases for a human.

    Args:
        logger: Logger the command writes through.
        domain: The application's domain.
        releases: Its releases, newest first.
    """
    if not releases:
        logger.info(f"{domain} has no releases yet")
        return
    logger.table(
        ["", "Release", "Commit", "Status", "Activated"],
        [
            [
                "*" if release.active else "",
                release.id,
                release.commit or "-",
                release.status if release.on_disk else f"{release.status} (removed)",
                release.activated_at or "-",
            ]
            for release in releases
        ],
    )
    logger.blank()
    logger.info(f"Go back to one with: wasm releases rollback {domain} <release>")


@click.group("releases", cls=WasmGroup)
def cli() -> None:
    """List an application's releases and go back to one instantly."""


@cli.command("list")
@click.argument("domain")
@json_option("Print the releases as JSON.")
@pass_context
def list_command(ctx: Context, domain: str) -> None:
    """
    List the releases of an application, newest first.

    The active one is marked with an asterisk. A release that failed is
    listed for a while after its directory was removed, and cannot be
    activated.
    """
    releases = list_releases(domain)
    if ctx.json_output:
        click.echo(
            json.dumps(
                {"domain": domain, "items": [dataclasses.asdict(r) for r in releases]},
                default=str,
            )
        )
        return
    print_releases(ctx.logger, domain, releases)


@cli.command("rollback")
@click.argument("domain")
@click.argument("release", required=False)
@pass_context
def rollback(ctx: Context, domain: str, release: str | None) -> None:
    """
    Make an earlier release the one that serves, without rebuilding anything.

    Without RELEASE, the release created just before the active one. The unit
    is restarted and the release kept only if it answers; if it does not, the
    release that was serving is put back and the command fails with the
    probe's and the journal's own output.
    """
    logger = CapturingLogger(verbose=ctx.verbose)
    outcome = activate_release(domain, release, trigger=DeploymentTrigger.CLI.value, logger=logger)
    if not outcome.changed:
        logger.info(f"Release {outcome.release.id} is already the active one")
        return
    verb = "Rolled back to" if outcome.went_back else "Activated"
    logger.success(f"{verb} release {outcome.release.id} of {outcome.domain}")
    if outcome.previous is not None:
        logger.info(
            f"Release {outcome.previous.id} stays on disk: "
            f"wasm releases rollback {outcome.domain} {outcome.previous.id}"
        )


@cli.command("keep")
@click.argument("domain")
@click.argument("count", required=False, type=int, metavar="N")
@json_option("Print the retention and what was pruned as JSON.")
@pass_context
def keep_command(ctx: Context, domain: str, count: int | None) -> None:
    """
    Show or set how many releases an application keeps on disk.

    N is from 1 to 50. Releases beyond it
    are removed now, oldest first; the active release and the one a rollback
    goes to are always kept, whatever N is.
    """
    if count is None:
        app = get_store().get_app(domain)
        if app is None:
            raise WASMError(
                f"Application not found: {domain}",
                details="Run 'wasm list' to see what is deployed.",
            )
        if ctx.json_output:
            click.echo(json.dumps({"domain": app.domain, "keep_releases": app.keep_releases}))
        else:
            ctx.logger.key_value("Keeps", f"{app.keep_releases} releases")
        return

    logger = CapturingLogger(verbose=ctx.verbose)
    change = set_release_retention(domain, count, logger=logger)
    if ctx.json_output:
        click.echo(json.dumps(dataclasses.asdict(change) | {"pruned": list(change.pruned)}))
        return
    logger.success(f"{change.domain} keeps {change.keep_releases} releases")
    if change.pruned:
        logger.info(f"Removed {len(change.pruned)}: {', '.join(change.pruned)}")
    else:
        logger.info("Nothing to remove")
