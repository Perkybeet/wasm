# Copyright (c) 2024-2026 Yago Lopez Prado
# SPDX-License-Identifier: AGPL-3.0-or-later

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

import dataclasses
import json

import click

from wasm.cli.app import Context, WasmGroup, global_flags, json_option, pass_context
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
@global_flags
@json_option("Print the chats as a JSON array.")
@pass_context
def telegram_chats_command(ctx: Context) -> None:
    """
    List the chats the Telegram bot can send to, with their ids.

    Send the bot a message, or add it to the group or channel, then run this.
    The first column is the chat id: set it with 'wasm config set
    notifications.channels.telegram.chat_id ID'. Group and channel ids start
    with a minus sign, and it is part of the id. A chat shows up only after
    it has sent the bot something recently, so if the one you want is
    missing, send the bot another message and run this again.

    Needs notifications.channels.telegram.bot_token to be set first.
    """
    try:
        chats = Notifier(Config()).list_telegram_chats()
    except (OSError, ValueError) as exc:
        ctx.logger.error("Could not list Telegram chats", details=str(exc))
        raise SystemExit(1) from exc

    if ctx.json_output:
        click.echo(json.dumps([dataclasses.asdict(chat) for chat in chats]))
        return

    if not chats:
        ctx.logger.warning(
            "No chats yet. Send the bot a message, or add it to the group or channel, "
            "then run this again."
        )
        return

    for chat in chats:
        label = chat.title or (f"@{chat.username}" if chat.username else "(no name)")
        ctx.logger.info(f"{chat.id}\t{chat.type}\t{label}")
