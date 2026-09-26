# Copyright (c) 2024-2026 Yago Lopez Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
The ``wasm 2fa`` command group: two-factor authentication for panel logins.

Enrolling, confirming and disabling the second factor used to be reachable
only from the panel's settings screen. This is a thin front end over
:class:`~wasm.web.auth.TokenManager`, the exact manager every
``/api/auth/2fa/*`` endpoint calls, built over the panel's own on-disk state -
turning the second factor on from here is turning it on for the panel, not a
separate copy of the feature.

The QR code the panel draws is not reproduced here: ``enroll`` prints the
secret and the ``otpauth://`` URI verbatim, which any authenticator app
accepts typed in by hand or turned into a QR code by another tool.
"""

from __future__ import annotations

import click

from wasm.cli.app import Context, pass_context
from wasm.web.auth import SecurityConfig, TokenManager


def _manager() -> TokenManager:
    """
    Build the token manager over the same on-disk state the panel uses.

    Returns:
        The manager.
    """
    return TokenManager(SecurityConfig())


@click.group("2fa")
def cli() -> None:
    """Enrol, confirm, disable or recover two-factor authentication for logins."""


@cli.command("status")
@pass_context
def status_command(ctx: Context) -> None:
    """Report the two-factor state, without exposing any secret."""
    status = _manager().totp_status()
    logger = ctx.logger
    logger.key_value("Enabled", "yes" if status["enabled"] else "no")
    logger.key_value("Enrolment pending", "yes" if status["pending"] else "no")
    logger.key_value("Backup codes remaining", str(status["backup_codes_remaining"]))


@cli.command("enroll")
@pass_context
def enroll_command(ctx: Context) -> None:
    """
    Begin enrolment: generate a pending secret. Nothing is enforced until
    'wasm 2fa confirm CODE' verifies it.
    """
    from wasm.web.api.auth import enrollment_uri

    secret = _manager().begin_totp_enrollment()

    logger = ctx.logger
    logger.success("Two-factor enrolment started")
    logger.blank()
    logger.key_value("Secret", secret)
    logger.key_value("URI", enrollment_uri(secret))
    logger.blank()
    logger.info(
        "Add the secret to an authenticator app, then confirm with the code it shows: "
        "wasm 2fa confirm <code>"
    )


@cli.command("confirm")
@click.argument("code")
@pass_context
def confirm_command(ctx: Context, code: str) -> None:
    """
    Verify CODE against the pending secret and activate the second factor.

    Prints the backup codes exactly once: only their salted hashes are
    stored, so they cannot be shown again.
    """
    codes = _manager().confirm_totp_enrollment(code)
    if codes is None:
        ctx.logger.error("That code was not accepted. Scan the QR again and enter a fresh code.")
        raise SystemExit(1)

    logger = ctx.logger
    logger.success("Two-factor authentication is now enabled")
    logger.blank()
    logger.info("Backup codes (store them somewhere safe; each works once):")
    for backup_code in codes:
        logger.list_item(backup_code)


@cli.command("disable")
@click.argument("code")
@pass_context
def disable_command(ctx: Context, code: str) -> None:
    """Turn the second factor off, on presentation of a current CODE or an unused backup code."""
    if not _manager().disable_totp(code):
        ctx.logger.error("That code was not accepted. Two-factor authentication stays on.")
        raise SystemExit(1)

    ctx.logger.success("Two-factor authentication disabled")


@cli.command("backup-codes")
@click.option("-f", "--force", is_flag=True, help="Do not ask for confirmation.")
@pass_context
def backup_codes_command(ctx: Context, force: bool) -> None:
    """
    Regenerate the backup codes, invalidating every one issued before.

    For recovering when the set on hand has been lost or shown somewhere it
    should not have been - the same action 'POST /api/auth/2fa/backup-codes'
    performs for a sudo-mode panel session.
    """
    if not force and not click.confirm(
        "Regenerate backup codes? Every backup code issued before this stops working",
        default=False,
    ):
        ctx.logger.info("Cancelled")
        return

    codes = _manager().regenerate_backup_codes()

    logger = ctx.logger
    logger.success("New backup codes issued")
    logger.blank()
    logger.info("Store them somewhere safe; each works once:")
    for backup_code in codes:
        logger.list_item(backup_code)
