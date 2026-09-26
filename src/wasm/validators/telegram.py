# Copyright (c) 2024-2026 Yago Lopez Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
The shape of a Telegram chat id, checked wherever one is saved or used.

One rule for the configuration chokepoint (``wasm config set``, the API's
settings endpoints) and the notifier that sends with it.
"""

from __future__ import annotations

import re
from typing import Any

#: A user, group or private chat id: a signed integer. Channels and
#: supergroups are negative and, since the Bot API's migration to 64-bit ids,
#: shaped like ``-100`` followed by the chat's original id.
_TELEGRAM_CHAT_ID_RE = re.compile(r"^-?\d+$")

#: A channel's public ``@username``, the other shape ``chat_id`` accepts.
_TELEGRAM_CHANNEL_NAME_RE = re.compile(r"^@[A-Za-z][A-Za-z0-9_]{4,31}$")

#: What a supergroup or channel id looks like with its leading minus dropped.
#: Every one of them is negative and begins with "100" once that sign is
#: stripped, so a *positive* id of this shape is not a chat that could ever
#: exist - it is someone who copied the digits from a bot API response and
#: left the sign off, which fails with "chat not found" and nothing in that
#: message points back at the missing character.
_LIKELY_MISSING_MINUS = re.compile(r"^100\d{10,}$")


def validate_telegram_chat_id(value: Any) -> str:
    """
    Check that a value has the shape Telegram's Bot API accepts for a chat id.

    Two shapes are valid: a signed integer id, or a channel's public
    ``@username``. A positive id of 13 or more digits starting with ``100`` is
    called out by name rather than left to fail as "chat not found": it is
    exactly what a supergroup or channel id looks like once its leading minus
    has been dropped, which is the single most common way to end up with a
    chat id that looks plausible and can never resolve to anything.

    Args:
        value: The configured ``chat_id``, before it is used or saved.

    Returns:
        The value, as a stripped string.

    Raises:
        ValueError: When it is neither an integer id nor an ``@channelname``.
    """
    text = str(value).strip()
    if _TELEGRAM_CHANNEL_NAME_RE.match(text):
        return text
    if _TELEGRAM_CHAT_ID_RE.match(text):
        if _LIKELY_MISSING_MINUS.match(text):
            raise ValueError(
                f"{text!r} does not look like a Telegram chat id - Telegram's own "
                f"supergroup and channel ids are negative; did you mean -{text}?"
            )
        return text
    raise ValueError(
        f"{value!r} is not a Telegram chat id: expected an integer id (for example "
        "123456789 or -1001234567890) or a channel username starting with @."
    )
