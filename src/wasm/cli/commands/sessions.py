# Copyright (c) 2024-2026 Yago Lopez Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
The ``wasm sessions`` command group: active panel logins.

Listing and revoking a session used to require the panel's own settings
screen; an operator locked out of the browser but still with shell access -
the exact moment a stray session is worth revoking - had no way to do it. This
is a thin front end over :class:`~wasm.web.auth.TokenManager`, the manager
every ``/api/auth/sessions*`` endpoint calls, built over the panel's own
on-disk state.

Only a session's id prefix is ever shown, the same restraint the panel's own
listing keeps: enough to name a row for revocation, useless for forging the
cookie it belongs to.
"""

from __future__ import annotations

import json
from datetime import datetime

import click

from wasm.cli.app import Context, WasmGroup, json_option, pass_context
from wasm.web.auth import SecurityConfig, TokenManager


def _manager() -> TokenManager:
    """
    Build the token manager over the same on-disk state the panel uses.

    Returns:
        The manager.
    """
    return TokenManager(SecurityConfig())


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


@click.group("sessions", cls=WasmGroup)
def cli() -> None:
    """Manage active panel sessions."""


@cli.command("list")
@json_option("Print the sessions as JSON.")
@pass_context
def list_command(ctx: Context) -> None:
    """List every active panel session."""
    manager = _manager()
    records = manager.list_sessions()

    if ctx.json_output:
        click.echo(
            json.dumps({"active_sessions": manager.get_active_session_count(), "sessions": records})
        )
        return

    logger = ctx.logger
    if not records:
        logger.info("No active sessions.")
        return

    logger.table(
        ["Session", "Client IP", "Created", "Last seen", "Expires"],
        [
            [
                record["sid_prefix"],
                record["client_ip"],
                _fmt(record["created_at"]),
                _fmt(record["last_seen"]),
                _fmt(record["expires_at"]),
            ]
            for record in records
        ],
    )


@cli.command("revoke")
@click.argument("prefix")
@pass_context
def revoke_command(ctx: Context, prefix: str) -> None:
    """
    Revoke one session, named by a unique PREFIX of its id, as listed by
    'wasm sessions list'.
    """
    revoked = _manager().revoke_session_by_prefix(prefix)
    if revoked is None:
        ctx.logger.error("No active session matches that prefix. It may have expired.")
        raise SystemExit(1)

    ctx.logger.success(f"Revoked session: {revoked}")


@cli.command("revoke-others")
@click.option("-f", "--force", is_flag=True, help="Do not ask for confirmation.")
@pass_context
def revoke_others_command(ctx: Context, force: bool) -> None:
    """
    Revoke every active session.

    The panel's own 'sign out everywhere except this tab' keeps the browser
    that asked for it signed in; the CLI has no session of its own to protect
    the same way, so this revokes every one of them - the operator running it
    already has the shell, which is the credential that matters here.
    """
    if not force and not click.confirm(
        "Revoke every active session? Every signed-in browser tab is signed out",
        default=False,
    ):
        ctx.logger.info("Cancelled")
        return

    _manager().revoke_all_sessions()
    ctx.logger.success("All sessions revoked")
