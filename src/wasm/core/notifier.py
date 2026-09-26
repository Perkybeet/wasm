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

A fourth rule is enforced by :func:`_require_public_destination` rather than
by convention: a notification channel is *configured* by whoever can write to
``/etc/wasm/config.yaml``, but *delivering* one means this process making an
outbound request with attacker-influenced content, from the same machine that
runs systemd as root. A webhook URL of ``http://169.254.169.254/latest/...``
or ``http://127.0.0.1:8080/api/...`` would turn "send a notification" into a
way to reach the cloud metadata service or the panel's own loopback-only
surface, so every destination is resolved and checked against
:data:`_FORBIDDEN_NETWORKS` before a request is built - and every redirect
hop is checked again by :class:`_SafeRedirectHandler`, because a destination
that starts out public and then answers with a 302 would otherwise reach this
guard exactly once. An operator who genuinely wants to notify a private
address (a webhook on the same network) lists the host under
``notifications.allow_private_hosts``.

:meth:`Notifier.test_channel` additionally never returns the remote response
body, only the status: unlike :func:`_describe_error`\\ 's use in
:meth:`Notifier.notify` (whose result only ever reaches a log an operator
already trusts), the test button's answer is rendered straight into the
settings page, and reflecting an arbitrary response body there is a second
way for a reachable-but-not-quite-blocked destination to hand content back to
whoever is looking at the panel.

Tests inject ``opener`` instead of opening sockets, and monkeypatch
:func:`_resolve_host` instead of resolving a real name; the suite never talks
to the network.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import re
import socket
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

#: Address family alias, matching wasm.core.net's.
_IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address

#: Networks a notification destination must not resolve into, unless the host
#: is explicitly trusted through ``notifications.allow_private_hosts``. Not
#: the full IANA special-purpose registry - just the ranges that turn a
#: notification into a way to reach the panel's own network: this machine
#: (loopback), private networks (RFC 1918), carrier-grade NAT (RFC 6598,
#: what a cloud metadata endpoint typically sits behind), link-local (which
#: is also where 169.254.169.254, the cloud metadata address itself, lives),
#: "this network" (0.0.0.0/8), IPv6's loopback, unique-local and link-local
#: equivalents, and the unspecified address (``::``), which Linux treats as
#: the local host on connect. This list only covers addresses in their own,
#: native form: an address that reaches one of these IPv4 ranges wrapped in
#: an IPv4-mapped, NAT64 or 6to4 IPv6 form is caught separately, by
#: :func:`_embedded_ipv4`, because none of these entries matches the wrapper.
_FORBIDDEN_NETWORKS: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...] = tuple(
    ipaddress.ip_network(cidr)
    for cidr in (
        "0.0.0.0/8",
        "10.0.0.0/8",
        "100.64.0.0/10",
        "127.0.0.0/8",
        "169.254.0.0/16",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "::/128",
        "::1/128",
        "fc00::/7",
        "fe80::/10",
    )
)

#: RFC 6052's "well-known prefix": an address in this /96 carries an IPv4
#: address in its low 32 bits, so a request to it is a request to that IPv4
#: address, wrapper aside. ``ipaddress`` has no built-in accessor for this
#: one, unlike ``ipv4_mapped`` and ``sixtofour`` below.
_NAT64_PREFIX = ipaddress.ip_network("64:ff9b::/96")


def _resolve_host(host: str) -> tuple[str, ...]:
    """
    Resolve a destination host to the literal addresses it points at.

    A module-level function rather than a method, so a test can replace it
    with :func:`pytest.MonkeyPatch.setattr` instead of opening a real socket:
    DNS is nondeterministic, reaches outside the sandbox, and would make this
    guard's own tests as slow and flaky as the network they exist to avoid a
    dependency on.

    Args:
        host: Hostname or address literal from the destination URL.

    Returns:
        Every address the name resolves to. A literal address resolves to
        itself without a lookup.

    Raises:
        OSError: When the name cannot be resolved.
    """
    try:
        return (str(ipaddress.ip_address(host)),)
    except ValueError:
        pass
    infos = socket.getaddrinfo(host, None)
    # A link-local sockaddr carries a %scope suffix ipaddress refuses.
    return tuple(sorted({str(info[4][0]).split("%", 1)[0] for info in infos}))


def _embedded_ipv4(address: _IPAddress) -> ipaddress.IPv4Address | None:
    """
    Extract the IPv4 address an IPv6 address maps or tunnels, if any.

    Three IPv6 forms carry an IPv4 address that a network stack dials
    exactly as if it had been given directly: IPv4-mapped
    (``::ffff:0:0/96``), NAT64 (``64:ff9b::/96``, RFC 6052) and 6to4
    (``2002::/16``). Each is a second route to an address
    :data:`_FORBIDDEN_NETWORKS` would otherwise catch, so :func:`_is_forbidden`
    checks the embedded address too instead of trusting the IPv6 wrapper to
    be exempt.

    Args:
        address: A resolved destination address.

    Returns:
        The embedded IPv4 address, or None when ``address`` is already IPv4
        or carries no embedded address.
    """
    if not isinstance(address, ipaddress.IPv6Address):
        return None
    if address.ipv4_mapped is not None:
        return address.ipv4_mapped
    if address.sixtofour is not None:
        return address.sixtofour
    if address in _NAT64_PREFIX:
        return ipaddress.IPv4Address(int(address) & 0xFFFFFFFF)
    return None


def _is_forbidden(address: _IPAddress) -> bool:
    """
    Report whether an address falls inside :data:`_FORBIDDEN_NETWORKS`.

    Also follows IPv4-mapped, NAT64 and 6to4 IPv6 addresses to the IPv4
    address they embed (see :func:`_embedded_ipv4`): those forms would
    otherwise reach a forbidden address through a wrapper none of this
    tuple's entries matches.

    Args:
        address: A resolved destination address.

    Returns:
        True when the address, or the IPv4 address it embeds, is not one a
        notification may be sent to.
    """
    if any(
        address.version == network.version and address in network for network in _FORBIDDEN_NETWORKS
    ):
        return True
    embedded = _embedded_ipv4(address)
    return embedded is not None and _is_forbidden(embedded)


def _require_public_destination(url: str, config: Config) -> None:
    """
    Refuse a destination that resolves inside the machine's own networks.

    This is the SSRF guard: see the module docstring for the attack it
    closes. It is called once for the destination a request is built for, and
    again - by :class:`_SafeRedirectHandler` - for every redirect the
    endpoint answers with, so a public host that 302s to a private one is
    refused exactly as if it had been configured that way from the start.

    Args:
        url: The destination URL, already scheme-checked.
        config: Configuration to read ``notifications.allow_private_hosts``
            from.

    Raises:
        ValueError: When the URL carries no host, the host cannot be
            resolved, or every address it resolves to is inside a forbidden
            network and the host is not on the allowlist.
    """
    host = urlparse(url).hostname
    if not host:
        raise ValueError(f"{url!r} has no host to validate")

    allowed = {
        str(entry).lower() for entry in (config.get("notifications.allow_private_hosts") or [])
    }
    if host.lower() in allowed:
        return

    try:
        addresses = _resolve_host(host)
    except OSError as exc:
        raise ValueError(f"Could not resolve notification destination {host!r}: {exc}") from exc
    if not addresses:
        raise ValueError(f"Notification destination {host!r} did not resolve to any address")

    for raw in addresses:
        address = ipaddress.ip_address(raw)
        if _is_forbidden(address):
            raise ValueError(
                f"Refusing to notify {host!r}: resolves to {address}, inside a private or "
                "internal network. List it under notifications.allow_private_hosts to allow it."
            )


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """
    Re-applies the SSRF guard to every redirect a destination answers with.

    ``urlopen`` follows redirects transparently, so a webhook endpoint that
    starts out public and 302s to ``http://169.254.169.254/`` would meet
    :func:`_require_public_destination` exactly once, at the address that
    passed. Every hop is validated again here before urllib is allowed to
    follow it.
    """

    def __init__(self, config: Config) -> None:
        """
        Args:
            config: Configuration to read the allowlist from, forwarded to
                every redirect check.
        """
        self._config = config

    def redirect_request(
        self,
        req: Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> Request | None:
        """
        Validate a redirect target before deciding whether to follow it.

        Args:
            req: The request that received the redirect.
            fp: The response file object.
            code: The redirect's HTTP status.
            msg: The redirect's reason phrase.
            headers: The redirect's response headers.
            newurl: Where the redirect points.

        Returns:
            The next request to send, exactly as the base class builds it.

        Raises:
            ValueError: When the redirect target is not a public destination.
        """
        _require_public_destination(newurl, self._config)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


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


def _describe_error(exc: BaseException, *, include_body: bool = True) -> str:
    """
    Return a failure in the server's own words.

    A system error is never paraphrased: the response body of an HTTP
    rejection is what Slack or Telegram actually said, and it is the message
    the operator can act on - in a log they already trust. ``test_channel``
    answers the settings page instead, which renders whatever string comes
    back, so it asks for ``include_body=False``: the destination's response
    is not this endpoint's data to repeat, especially once the SSRF guard's
    allowlist has let through a host that is private but not entirely
    untrusted.

    Args:
        exc: The exception delivery raised.
        include_body: Whether to read and include the response body of an
            HTTP rejection. Only :meth:`Notifier.notify`'s log line does.

    Returns:
        Human-readable failure text.
    """
    if isinstance(exc, HTTPError):
        status = f"HTTP {exc.code} {exc.reason}"
        if not include_body:
            return status
        try:
            # Bounded read: the error page of a misbehaving endpoint must not
            # be buffered wholesale into a log line.
            body = exc.read(2048).decode("utf-8", "replace").strip()
        except (OSError, ValueError):
            body = ""
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
        # The default opener carries its own redirect handler, so a
        # destination that answers with a 302 is re-checked by
        # _require_public_destination at every hop instead of only at the
        # address that was configured.
        self._opener: Opener = (
            opener or urllib.request.build_opener(_SafeRedirectHandler(self._config)).open
        )
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
            # include_body=False: this string is rendered on the settings
            # page, and the endpoint's response body is not ours to repeat.
            return _scrub(_describe_error(exc, include_body=False), channels)
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
            ValueError: When the configured URL or token is malformed, or the
                destination resolves inside a forbidden network.
            WASMError: When the email transport reports a problem.
        """
        if name == "email":
            return self._send_email(event, channels.get("email") or {})

        request = self._request_for(name, event, channels)
        if request is None:
            return False
        # The SSRF guard: see the module docstring. Checked here, once, for
        # every HTTP channel, rather than in each _*_request builder - one
        # chokepoint a new channel cannot forget to pass through.
        _require_public_destination(request.full_url, self._config)
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
