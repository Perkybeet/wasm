# Copyright (c) 2024-2026 Yago López Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
Multi-channel notifications for operational events.

One implementation, stdlib only. Deploys, the monitor and the backup jobs all
publish a :class:`NotificationEvent` here instead of growing their own
delivery code, and the panel's settings page configures it; the per-channel
"send a test" button is :meth:`Notifier.test_channel`.

Channels and the payload each one receives:

- **webhook** - the operator's own endpoint. POST JSON with the keys
  ``event`` (the kind), ``title``, ``body``, ``domain`` (null when the event
  is not about one) and ``ts`` (ISO 8601, UTC).
- **slack** - Slack incoming webhook, ``{"text": "..."}``.
- **discord** - Discord webhook, ``{"content": "..."}``.
- **telegram** - Bot API ``sendMessage``, ``{"chat_id": ..., "text": ...}``.
- **email** - delegates to :class:`wasm.monitor.email_notifier.EmailNotifier`,
  so there is exactly one SMTP implementation.

Three rules hold everywhere:

- Every request has a deadline (:data:`NOTIFY_TIMEOUT`). An endpoint that
  stopped answering must not stall the deploy that fired the event.
- A failing channel is logged and skipped. The other channels still get the
  event and the caller never sees the failure.
- Secrets never reach a log. The Telegram bot token is part of the request
  URL and urllib quotes the URL in some of its errors, so error text is
  scrubbed before it is logged or returned.

Tests inject ``opener`` instead of opening sockets; the suite never talks to
the network.
"""

from __future__ import annotations

import json
import logging
import re
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from html import escape
from http.client import HTTPException
from typing import TYPE_CHECKING, Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request

from wasm import __version__
from wasm.core.config import Config
from wasm.core.exceptions import WASMError

if TYPE_CHECKING:
    from wasm.monitor.email_notifier import EmailNotifier

logger = logging.getLogger(__name__)

#: Deadline for every notification POST, in seconds. These endpoints answer in
#: well under a second when healthy; anything slower is an outage on their
#: side, and the caller's work must not wait on it.
NOTIFY_TIMEOUT = 10

#: Identifies WASM to the receiving endpoint.
USER_AGENT = f"wasm-notifier/{__version__}"

#: Event kinds an operator can switch off under ``notifications.events``.
#: ``DEFAULT_CONFIG["notifications"]["events"]`` spells out the same names;
#: config.py cannot import this module (this module reads its settings from
#: config.py), so the agreement is pinned by a test in tests/test_notifier.py,
#: the same pattern that keeps the web security defaults honest.
EVENT_KINDS: tuple[str, ...] = (
    "deploy_success",
    "deploy_failed",
    "cert_expiring",
    "unit_failed",
    "disk_threshold",
    "backup_failed",
)

#: The kind :meth:`Notifier.test_channel` sends. Always accepted and never
#: filtered, so the settings-page button works before anything is enabled.
TEST_KIND = "test"

#: Delivery order. Every name is a key under ``notifications.channels``.
CHANNELS: tuple[str, ...] = ("webhook", "slack", "discord", "telegram", "email")

#: ``<bot id>:<secret>``, the only shape the Bot API issues. The token becomes
#: part of the request path, so anything else is refused before it can reshape
#: the URL.
_TELEGRAM_TOKEN_RE = re.compile(r"^[0-9]+:[A-Za-z0-9_-]+$")

_TELEGRAM_API = "https://api.telegram.org"

_REDACTED = "***"

#: What delivery can raise; the per-channel guard catches exactly this and
#: nothing broader. OSError covers URLError, HTTPError and timeouts;
#: ValueError is urllib refusing a malformed URL (quoting it in the message);
#: HTTPException is the server breaking the protocol mid-response; WASMError
#: is the email transport reporting a delivery problem.
_DELIVERY_ERRORS = (OSError, ValueError, HTTPException, WASMError)

#: The setting the test-button error names when a channel is not configured.
_SETTING_HINTS = {
    "webhook": "notifications.channels.webhook.webhook_url",
    "slack": "notifications.channels.slack.webhook_url",
    "discord": "notifications.channels.discord.webhook_url",
    "telegram": "notifications.channels.telegram.bot_token and chat_id",
    "email": "notifications.channels.email.enabled and monitor.smtp.*",
}

#: Anything with urlopen's calling convention: ``opener(request, timeout=...)``
#: returning a closeable response. Tests inject one; the suite never opens a
#: real socket.
Opener = Callable[..., Any]


@dataclass
class NotificationEvent:
    """
    One operational fact worth telling the operator about.

    Attributes:
        kind: One of :data:`EVENT_KINDS`, or :data:`TEST_KIND`.
        title: One-line summary, e.g. ``"Deploy failed: example.com"``.
        body: The detail the operator acts on. May be empty.
        domain: Domain the event is about, when it is about one.
        ts: When it happened. Timezone-aware UTC by default.
    """

    kind: str
    title: str
    body: str
    domain: str | None = None
    ts: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self) -> None:
        """
        Refuse a kind nothing can filter.

        A typo'd kind would default to "send" and silently bypass the
        operator's ``notifications.events`` switches.

        Raises:
            ValueError: When ``kind`` is not a known event kind.
        """
        if self.kind not in EVENT_KINDS and self.kind != TEST_KIND:
            known = ", ".join(EVENT_KINDS)
            raise ValueError(f"Unknown notification kind {self.kind!r}; expected one of: {known}")


def _message_text(event: NotificationEvent) -> str:
    """
    Render the plain text the chat channels carry.

    Args:
        event: The event to render.

    Returns:
        Title and body separated by a newline, or just the title when the
        body is empty.
    """
    return f"{event.title}\n{event.body}" if event.body else event.title


def _require_http_url(url: str, setting: str) -> str:
    """
    Refuse a URL whose scheme is not plain HTTP(S).

    ``urlopen`` also follows ``file://`` and ``ftp://``; a configuration value
    must not be able to turn a notification into a local file read. Bandit's
    S310 is ignored project-wide on the promise that every call site validates
    the scheme, and this is this module's validation.

    Args:
        url: The configured URL.
        setting: Dotted configuration path, for the error message.

    Returns:
        The URL, unchanged.

    Raises:
        ValueError: When the scheme is anything but http or https.
    """
    if urlparse(url).scheme.lower() not in ("http", "https"):
        raise ValueError(f"{setting} must be an http:// or https:// URL")
    return url


def _json_request(url: str, payload: dict[str, Any]) -> Request:
    """
    Build a JSON POST with this module's identity.

    Args:
        url: Destination URL, already validated.
        payload: JSON-serialisable body.

    Returns:
        The request, ready for the opener.
    """
    return Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
        method="POST",
    )


def _webhook_request(url: str, event: NotificationEvent) -> Request:
    """
    Build the generic webhook POST.

    The payload is this module's own documented contract: ``event`` (the
    kind), ``title``, ``body``, ``domain`` (null when the event is not about
    one) and ``ts`` (ISO 8601).

    Args:
        url: The operator's endpoint.
        event: The event to deliver.

    Returns:
        The request.

    Raises:
        ValueError: When the URL is not HTTP(S).
    """
    _require_http_url(url, "notifications.channels.webhook.webhook_url")
    payload = {
        "event": event.kind,
        "title": event.title,
        "body": event.body,
        "domain": event.domain,
        "ts": event.ts.isoformat(),
    }
    return _json_request(url, payload)


def _slack_request(url: str, event: NotificationEvent) -> Request:
    """
    Build the Slack incoming-webhook POST, ``{"text": ...}``.

    Args:
        url: The Slack webhook URL.
        event: The event to deliver.

    Returns:
        The request.

    Raises:
        ValueError: When the URL is not HTTP(S).
    """
    _require_http_url(url, "notifications.channels.slack.webhook_url")
    return _json_request(url, {"text": _message_text(event)})


def _discord_request(url: str, event: NotificationEvent) -> Request:
    """
    Build the Discord webhook POST, ``{"content": ...}``.

    Args:
        url: The Discord webhook URL.
        event: The event to deliver.

    Returns:
        The request.

    Raises:
        ValueError: When the URL is not HTTP(S).
    """
    _require_http_url(url, "notifications.channels.discord.webhook_url")
    return _json_request(url, {"content": _message_text(event)})


def _telegram_request(bot_token: str, chat_id: str, event: NotificationEvent) -> Request:
    """
    Build the Bot API ``sendMessage`` POST.

    Args:
        bot_token: The bot's token; it becomes part of the request path.
        chat_id: Destination chat, travels in the JSON body.
        event: The event to deliver.

    Returns:
        The request.

    Raises:
        ValueError: When the token does not have the Bot API shape. The token
            is never included in the message.
    """
    if not _TELEGRAM_TOKEN_RE.match(bot_token):
        raise ValueError(
            "notifications.channels.telegram.bot_token does not look like a "
            "Telegram bot token (expected <digits>:<secret>)"
        )
    url = f"{_TELEGRAM_API}/bot{bot_token}/sendMessage"
    return _json_request(url, {"chat_id": chat_id, "text": _message_text(event)})


def _describe_error(exc: BaseException) -> str:
    """
    Return a failure in the server's own words.

    A system error is never paraphrased: the response body of an HTTP
    rejection is what Slack or Telegram actually said, and it is the message
    the operator can act on.

    Args:
        exc: The exception delivery raised.

    Returns:
        Human-readable failure text.
    """
    if isinstance(exc, HTTPError):
        try:
            # Bounded read: the error page of a misbehaving endpoint must not
            # be buffered wholesale into a log line.
            body = exc.read(2048).decode("utf-8", "replace").strip()
        except (OSError, ValueError):
            body = ""
        status = f"HTTP {exc.code} {exc.reason}"
        return f"{status}: {body}" if body else status
    if isinstance(exc, URLError):
        return str(exc.reason)
    return str(exc)


class Notifier:
    """
    Publishes events to every configured notification channel.

    Reads ``notifications.*`` from the configuration on every call, so a
    settings change in the panel applies to the next event without a restart.
    """

    def __init__(
        self,
        config: Config | None = None,
        *,
        opener: Opener | None = None,
        email_notifier: EmailNotifier | None = None,
    ) -> None:
        """
        Args:
            config: Configuration to read ``notifications.*`` from. Defaults
                to the global one.
            opener: Replacement for :func:`urllib.request.urlopen`. Tests
                inject one so the suite never opens a socket.
            email_notifier: Replacement email transport. Defaults to the
                monitor's :class:`EmailNotifier`, created on first use.
        """
        self._config = config or Config()
        self._opener: Opener = opener or urllib.request.urlopen
        self._email_notifier = email_notifier

    def notify(self, event: NotificationEvent) -> None:
        """
        Publish an event to every channel enabled for its kind.

        Never raises for a delivery problem: a dead channel is logged and the
        remaining channels still get the event, because the caller is a
        deploy or the monitor loop and its work matters more than the
        announcement of it.

        Args:
            event: What happened.
        """
        settings = self._settings()
        if not settings.get("enabled", False):
            return
        events = settings.get("events") or {}
        if event.kind != TEST_KIND and not events.get(event.kind, True):
            return

        channels: dict[str, Any] = settings.get("channels") or {}
        for name in CHANNELS:
            try:
                self._dispatch(name, event, channels)
            except _DELIVERY_ERRORS as exc:
                # The request URL never reaches the log: urllib quotes it in
                # some errors, and the Telegram one embeds the bot token.
                logger.warning(
                    "Notification channel %s failed for event %s: %s",
                    name,
                    event.kind,
                    _scrub(_describe_error(exc), channels),
                )

    def test_channel(self, name: str) -> str | None:
        """
        Send a test event through one channel, ignoring the on/off switches.

        The settings-page button exists to try a channel before the operator
        enables notifications, so neither ``notifications.enabled`` nor the
        per-kind filter applies here.

        Args:
            name: Channel name, one of :data:`CHANNELS`.

        Returns:
            None when the channel accepted the message, otherwise the failure
            in the server's own words, with configured secrets scrubbed.
        """
        if name not in CHANNELS:
            known = ", ".join(CHANNELS)
            return f"Unknown notification channel {name!r}; expected one of: {known}"

        channels: dict[str, Any] = self._settings().get("channels") or {}
        event = NotificationEvent(
            kind=TEST_KIND,
            title="WASM test notification",
            body=f"Receiving this means the {name} channel is configured correctly.",
        )
        try:
            sent = self._dispatch(name, event, channels)
        except _DELIVERY_ERRORS as exc:
            return _scrub(_describe_error(exc), channels)
        if not sent:
            return f"Channel {name} is not configured; set {_SETTING_HINTS[name]} first."
        return None

    def _settings(self) -> dict[str, Any]:
        """
        Read the ``notifications`` block, tolerating a sparse config file.

        Returns:
            The block, possibly empty.
        """
        settings = self._config.get("notifications", {})
        return settings if isinstance(settings, dict) else {}

    def _dispatch(self, name: str, event: NotificationEvent, channels: dict[str, Any]) -> bool:
        """
        Deliver one event through one channel.

        Args:
            name: Channel name, one of :data:`CHANNELS`.
            event: The event to deliver.
            channels: The ``notifications.channels`` block.

        Returns:
            True when a message went out, False when the channel is not
            configured.

        Raises:
            OSError: When the endpoint is unreachable or rejects the message.
            ValueError: When the configured URL or token is malformed.
            WASMError: When the email transport reports a problem.
        """
        if name == "email":
            return self._send_email(event, channels.get("email") or {})

        request = self._request_for(name, event, channels)
        if request is None:
            return False
        # urlopen raises HTTPError for any non-2xx answer, so reaching close()
        # means the endpoint accepted the message; the body is not our data.
        self._opener(request, timeout=NOTIFY_TIMEOUT).close()
        return True

    def _request_for(
        self, name: str, event: NotificationEvent, channels: dict[str, Any]
    ) -> Request | None:
        """
        Build the request one HTTP channel would send, if it is configured.

        Args:
            name: Channel name, every one of :data:`CHANNELS` except email.
            event: The event to deliver.
            channels: The ``notifications.channels`` block.

        Returns:
            The request, or None when the channel is not configured.

        Raises:
            ValueError: When the configured URL or token is malformed.
        """
        channel: dict[str, Any] = channels.get(name) or {}
        if name == "telegram":
            bot_token = str(channel.get("bot_token") or "")
            chat_id = str(channel.get("chat_id") or "")
            if not bot_token or not chat_id:
                return None
            return _telegram_request(bot_token, chat_id, event)

        url = str(channel.get("webhook_url") or "")
        if not url:
            return None
        builders: dict[str, Callable[[str, NotificationEvent], Request]] = {
            "webhook": _webhook_request,
            "slack": _slack_request,
            "discord": _discord_request,
        }
        return builders[name](url, event)

    def _send_email(self, event: NotificationEvent, channel: dict[str, Any]) -> bool:
        """
        Deliver through the monitor's SMTP implementation.

        Args:
            event: The event to deliver.
            channel: The ``notifications.channels.email`` block.

        Returns:
            True when a message was handed to the server, False when the
            channel is off or SMTP is not configured.

        Raises:
            WASMError: When the transport refuses the settings or delivery
                fails.
        """
        if not channel.get("enabled", False):
            return False

        # Deferred import: the monitor publishes events to this module, so a
        # module-level import in both directions would be a cycle.
        from wasm.monitor.email_notifier import EmailContent, EmailNotifier

        notifier = self._email_notifier
        if notifier is None:
            notifier = self._email_notifier = EmailNotifier()
        if not notifier.is_configured:
            return False

        html = (
            '<!DOCTYPE html><html><body style="font-family: system-ui, sans-serif;'
            ' color: #222;">'
            f"<h2>{escape(event.title)}</h2><p>{escape(event.body)}</p>"
            "</body></html>"
        )
        # _send is the transport's one generic entry point; its public methods
        # are all shaped around monitor observations. Reusing it beats writing
        # a second SMTP implementation, which is the defect class rule three
        # exists to prevent.
        notifier._send(
            EmailContent(
                subject=f"[WASM] {event.title}",
                text=_message_text(event),
                html=html,
            )
        )
        return True


def _scrub(text: str, channels: dict[str, Any]) -> str:
    """
    Replace configured secrets in error text before it is logged or shown.

    Args:
        text: Failure text that may quote a request URL.
        channels: The ``notifications.channels`` block the secrets live in.

    Returns:
        The text with every configured secret replaced by ``***``.
    """
    for secret in _channel_secrets(channels):
        text = text.replace(secret, _REDACTED)
    return text


def _channel_secrets(channels: dict[str, Any]) -> tuple[str, ...]:
    """
    Collect the values that must never appear in a log or an error message.

    Args:
        channels: The ``notifications.channels`` block.

    Returns:
        The non-empty secrets: the Telegram bot token and every webhook URL,
        Slack and Discord embed theirs in the path.
    """
    telegram: dict[str, Any] = channels.get("telegram") or {}
    candidates = [str(telegram.get("bot_token") or "")]
    for name in ("webhook", "slack", "discord"):
        channel: dict[str, Any] = channels.get(name) or {}
        candidates.append(str(channel.get("webhook_url") or ""))
    return tuple(value for value in candidates if value)
