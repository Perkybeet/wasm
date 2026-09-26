# Copyright (c) 2024-2026 Yago Lopez Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
The ``wasm notify`` command group: notification channels.

Trying a channel before turning notifications on used to be a button in the
panel's settings screen and nothing else; a deployment with no panel installed
had no way to check a webhook URL was right before relying on it during a real
deploy failure. This is a thin front end over
:class:`~wasm.core.notifier.Notifier`, the exact class the settings page's
"Test" button and every real event delivery use - one implementation of
sending a message through a channel, reached from two places.
"""

from __future__ import annotations

import click

from wasm.cli.app import Context, WasmGroup, pass_context
from wasm.core.config import Config
from wasm.core.notifier import CHANNELS, Notifier


@click.group("notify", cls=WasmGroup)
def cli() -> None:
    """Send test notifications through a configured channel."""


@cli.command("test")
@click.argument("channel", type=click.Choice(CHANNELS))
@pass_context
def test_command(ctx: Context, channel: str) -> None:
    """
    Send a test message through CHANNEL.

    Ignores the master switch and the per-event filters, the same as the
    panel's own "Test" button: this exists to check a channel is configured
    correctly before notifications are turned on.
    """
    error = Notifier(Config()).test_channel(channel)
    if error is not None:
        ctx.logger.error(f"Test message through {channel} failed", details=error)
        raise SystemExit(1)

    ctx.logger.success(f"Test message sent through {channel}.")


@cli.command("telegram-chats")
@pass_context
def telegram_chats_command(ctx: Context) -> None:
    """
    List the chats the configured Telegram bot has seen.

    Finding a chat id today means opening the Bot API's getUpdates URL by hand
    and reading raw JSON. Send the bot a message, or add it to the group or
    channel, then run this to read the id back - Telegram only queues an
    update the bot has not already been asked for.
    """
    try:
        chats = Notifier(Config()).list_telegram_chats()
    except (OSError, ValueError) as exc:
        ctx.logger.error("Could not list Telegram chats", details=str(exc))
        raise SystemExit(1) from exc

    if not chats:
        ctx.logger.warning(
            "No chats yet. Send the bot a message, or add it to the group or channel, "
            "then run this again."
        )
        return

    for chat in chats:
        label = chat.title or (f"@{chat.username}" if chat.username else "(no name)")
        ctx.logger.info(f"{chat.id}\t{chat.type}\t{label}")
