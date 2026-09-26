# Copyright (c) 2024-2026 Yago Lopez Prado
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
The ``wasm token`` command group: named, scoped API tokens.

Before this existed, an API token could only be issued, listed or revoked from
the panel's settings screen: an operator scripting against the API from a
server with no browser had no way to mint the credential the script needs.
This is a thin front end over :class:`~wasm.web.auth.TokenManager`, the exact
manager ``POST /api/auth/tokens`` and its siblings call, built over the panel's
own on-disk state (:class:`~wasm.web.auth.SecurityConfig`'s state directory) -
a token issued here authenticates against the panel and vice versa, because
there is one store of them, not two.

A token is shown exactly once, at creation: only its salted hash is stored,
the same way the master token and a TOTP backup code are, so it cannot be
shown again afterwards.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

import click

from wasm.cli.app import Context, WasmGroup, json_option, pass_context
from wasm.cli.web_state import token_manager

#: Scopes ``POST /api/auth/tokens`` accepts, in the order shown by --help.
SCOPES: tuple[str, ...] = ("read", "deploy", "admin")


def _fmt(timestamp: float | None) -> str:
    """
    Render a UNIX timestamp for a table cell.

    Args:
        timestamp: The timestamp, or None.

    Returns:
        A human-readable local time, or a placeholder for None.
    """
    if timestamp is None:
        return "-"
    return datetime.fromtimestamp(timestamp).isoformat(sep=" ", timespec="seconds")


@click.group("token", cls=WasmGroup)
def cli() -> None:
    """Manage API tokens: named, scoped credentials for scripts and automation."""


@cli.command("list")
@json_option("Print the tokens as JSON.")
@pass_context
def list_command(ctx: Context) -> None:
    """
    List every API token ever issued, live and revoked alike.

    No output from this command ever shows a token: only its salted hash is
    stored, so it cannot be shown again after 'wasm token create'.
    """
    records = token_manager().list_api_tokens()

    if ctx.json_output:
        click.echo(json.dumps({"tokens": records}))
        return

    logger = ctx.logger
    if not records:
        logger.info("No API tokens have been issued.")
        return

    logger.table(
        ["ID", "Name", "Scope", "Created", "Expires", "Last used", "Revoked"],
        [
            [
                str(record["id"]),
                record["name"],
                record["scope"],
                _fmt(record["created_at"]),
                _fmt(record["expires_at"]) if record["expires_at"] else "never",
                _fmt(record["last_used_at"]),
                _fmt(record["revoked_at"]),
            ]
            for record in records
        ],
    )


@cli.command("create")
@click.argument("name")
@click.option(
    "--scope",
    type=click.Choice(SCOPES),
    default="read",
    show_default=True,
    help="What the token may do.",
)
@click.option(
    "--expires-hours",
    type=int,
    default=None,
    help="Lifetime in hours. Omitted: the token only dies by revocation.",
)
@pass_context
def create_command(ctx: Context, name: str, scope: str, expires_hours: int | None) -> None:
    """
    Issue a named, scoped API token. NAME must be unique across every token
    ever issued, live or revoked.

    The token is printed exactly once, here. Store it now: WASM only ever
    keeps a salted hash of it, the same as the master token, so it cannot be
    shown again.
    """
    issued: dict[str, Any] = token_manager().create_api_token(name, scope, expires_hours)

    logger = ctx.logger
    logger.success(f"API token issued: {issued['name']} (scope: {issued['scope']})")
    logger.blank()
    click.echo(f"Token: {issued['token']}")
    logger.blank()
    if ctx.dry_run:
        # A dry run opens the session database as a private in-memory copy
        # (wasm.web.auth.SessionStore._rehearsal_copy), so this token was
        # never written to the one WASM actually authenticates against. It
        # looks real and is not: printing it without saying so is how an
        # operator pastes a credential into a script that then never works.
        logger.warning("Rehearsal: this token was not saved and will not authenticate.")
    else:
        logger.warning("This is the only time the token is shown. Store it now.")


@cli.command("revoke")
@click.argument("token_id", type=int)
@click.option("-f", "--force", is_flag=True, help="Do not ask for confirmation.")
@pass_context
def revoke_command(ctx: Context, token_id: int, force: bool) -> None:
    """
    Revoke one API token by its id, as listed by 'wasm token list'.

    Requests presenting it stop authenticating immediately.
    """
    if not force and not click.confirm(
        f"Revoke API token {token_id}? Anything using it stops authenticating immediately",
        default=False,
    ):
        ctx.logger.info("Cancelled")
        return

    name = token_manager().revoke_api_token(token_id)
    if name is None:
        ctx.logger.error(f"No API token with id {token_id}")
        raise SystemExit(1)

    ctx.logger.success(f"Revoked API token: {name}")
