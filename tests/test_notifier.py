# Copyright (c) 2024-2026 Yago López Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
Tests for :mod:`wasm.core.notifier`.

The suite never opens a socket: every test injects an opener with urlopen's
calling convention and asserts on the :class:`urllib.request.Request` objects
the notifier built. What is being defended:

- the exact payload each channel receives, because the receiving side is not
  ours to fix,
- delivery isolation: one dead channel must not cost the other channels the
  event, nor the caller its deploy,
- secrecy: the Telegram bot token is part of the request URL and must never
  reach a log,
- the test-button contract: the failure comes back in the server's own words,
- the agreement between ``DEFAULT_CONFIG["notifications"]`` and the constants
  here, which no import can enforce because config.py cannot import the
  notifier.
"""

from __future__ import annotations

import io
import json
import logging
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request

import pytest

from wasm.core.config import DEFAULT_CONFIG, Config
from wasm.core.notifier import (
    CHANNELS,
    EVENT_KINDS,
    NOTIFY_TIMEOUT,
    USER_AGENT,
    NotificationEvent,
    Notifier,
)

#: The documented shape of a Bot API token: ``<bot id>:<secret>``.
BOT_TOKEN = "110201543:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw"

WEBHOOK_URL = "https://hooks.example.test/wasm"
SLACK_URL = "https://hooks.slack.test/services/T0/B0/slack-secret"
DISCORD_URL = "https://discord.test/api/webhooks/1/discord-secret"


@pytest.fixture
def config(sandbox: Path, monkeypatch: pytest.MonkeyPatch) -> Config:
    """
    Point the global config file at the sandbox and reset the singleton.

    Args:
        sandbox: Isolated filesystem root.
        monkeypatch: Patching helper, scoped to the test.

    Returns:
        A fresh configuration carrying only the defaults.
    """
    monkeypatch.setattr(
        "wasm.core.config.DEFAULT_CONFIG_PATH", sandbox / "etc" / "wasm" / "config.yaml"
    )
    Config.reset_instance()
    try:
        yield Config()
    finally:
        Config.reset_instance()


class CapturingOpener:
    """
    Stands in for ``urllib.request.urlopen``.

    Attributes:
        requests: Every request that would have been sent, in order.
        timeouts: The timeout passed with each call, failed calls included.
        errors: URL prefix mapped to the exception raised instead of sending.
    """

    def __init__(self) -> None:
        self.requests: list[Request] = []
        self.timeouts: list[float | None] = []
        self.errors: dict[str, Exception] = {}

    def __call__(self, request: Request, timeout: float | None = None) -> io.BytesIO:
        """
        Record a request, or fail the way urlopen would.

        Args:
            request: The request the notifier built.
            timeout: The deadline the notifier asked for.

        Returns:
            A closeable stand-in for the HTTP response.
        """
        self.timeouts.append(timeout)
        for prefix, error in self.errors.items():
            if request.full_url.startswith(prefix):
                raise error
        self.requests.append(request)
        return io.BytesIO(b"ok")


def make_event(**overrides: object) -> NotificationEvent:
    """
    Build a valid event, overriding only what a test cares about.

    Args:
        overrides: Field values that replace the defaults.

    Returns:
        The event.
    """
    fields: dict = {
        "kind": "deploy_success",
        "title": "Deployed example.com",
        "body": "wasm-example.com is running",
        "domain": "example.com",
    }
    fields.update(overrides)
    return NotificationEvent(**fields)


class TestWebhookChannel:
    """The generic webhook carries the documented JSON payload."""

    def test_sends_the_documented_payload(self, config: Config) -> None:
        """URL, method, headers, timeout and payload are all part of the API."""
        config.set("notifications.enabled", True)
        config.set("notifications.channels.webhook.webhook_url", WEBHOOK_URL)
        opener = CapturingOpener()

        Notifier(config, opener=opener).notify(make_event())

        assert len(opener.requests) == 1
        request = opener.requests[0]
        assert request.full_url == WEBHOOK_URL
        assert request.get_method() == "POST"
        assert request.get_header("Content-type") == "application/json"
        assert request.get_header("User-agent") == USER_AGENT
        assert opener.timeouts == [NOTIFY_TIMEOUT]

        payload = json.loads(request.data)
        assert payload == {
            "event": "deploy_success",
            "title": "Deployed example.com",
            "body": "wasm-example.com is running",
            "domain": "example.com",
            "ts": payload["ts"],
        }
        assert payload["ts"]  # ISO 8601, present even when nobody set it

    def test_the_master_switch_gates_everything(self, config: Config) -> None:
        """A configured channel must stay silent while notifications are off."""
        config.set("notifications.channels.webhook.webhook_url", WEBHOOK_URL)
        opener = CapturingOpener()

        Notifier(config, opener=opener).notify(make_event())

        assert opener.requests == []

    def test_an_unconfigured_channel_sends_nothing(self, config: Config) -> None:
        """Empty URLs are the off position; no channel may guess a default."""
        config.set("notifications.enabled", True)
        opener = CapturingOpener()

        Notifier(config, opener=opener).notify(make_event())

        assert opener.requests == []

    def test_a_non_http_url_is_refused_and_logged(
        self, config: Config, caplog: pytest.LogCaptureFixture
    ) -> None:
        """urlopen would happily fetch file:// and ftp://; the notifier must not."""
        config.set("notifications.enabled", True)
        config.set("notifications.channels.webhook.webhook_url", "ftp://files.example.test/hook")
        opener = CapturingOpener()

        with caplog.at_level(logging.WARNING, logger="wasm.core.notifier"):
            Notifier(config, opener=opener).notify(make_event())

        assert opener.requests == []
        assert "http" in caplog.text


class TestEventFilter:
    """``notifications.events.*`` switches one kind off, not the feature."""

    def test_a_disabled_kind_is_filtered(self, config: Config) -> None:
        """The kind the operator switched off must not be delivered."""
        config.set("notifications.enabled", True)
        config.set("notifications.channels.webhook.webhook_url", WEBHOOK_URL)
        config.set("notifications.events.deploy_success", False)
        opener = CapturingOpener()

        Notifier(config, opener=opener).notify(make_event(kind="deploy_success"))

        assert opener.requests == []

    def test_other_kinds_still_deliver(self, config: Config) -> None:
        """Filtering one kind must leave the others alone."""
        config.set("notifications.enabled", True)
        config.set("notifications.channels.webhook.webhook_url", WEBHOOK_URL)
        config.set("notifications.events.deploy_success", False)
        opener = CapturingOpener()

        Notifier(config, opener=opener).notify(
            make_event(kind="deploy_failed", title="Deploy failed: example.com")
        )

        assert [json.loads(r.data)["event"] for r in opener.requests] == ["deploy_failed"]


class TestDeliveryIsolation:
    """One dead channel must not cost the others the event."""

    def test_a_failing_channel_does_not_block_the_rest(
        self, config: Config, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The failure is logged; Slack and Discord still get the event."""
        config.set("notifications.enabled", True)
        config.set("notifications.channels.webhook.webhook_url", WEBHOOK_URL)
        config.set("notifications.channels.slack.webhook_url", SLACK_URL)
        config.set("notifications.channels.discord.webhook_url", DISCORD_URL)
        opener = CapturingOpener()
        opener.errors["https://hooks.example.test"] = URLError("connection refused")

        with caplog.at_level(logging.WARNING, logger="wasm.core.notifier"):
            Notifier(config, opener=opener).notify(make_event())

        assert [r.full_url for r in opener.requests] == [SLACK_URL, DISCORD_URL]
        assert "webhook" in caplog.text
        assert "connection refused" in caplog.text

    def test_notify_never_raises_for_a_delivery_problem(self, config: Config) -> None:
        """The caller is a deploy; its work matters more than the announcement."""
        config.set("notifications.enabled", True)
        config.set("notifications.channels.webhook.webhook_url", WEBHOOK_URL)
        opener = CapturingOpener()
        opener.errors["https://"] = URLError("total outage")

        Notifier(config, opener=opener).notify(make_event())


class TestChatChannels:
    """Slack and Discord each get their own one-key payload."""

    def test_slack_payload_is_text(self, config: Config) -> None:
        """Slack incoming webhooks require ``{"text": ...}``."""
        config.set("notifications.enabled", True)
        config.set("notifications.channels.slack.webhook_url", SLACK_URL)
        opener = CapturingOpener()

        Notifier(config, opener=opener).notify(make_event())

        assert [r.full_url for r in opener.requests] == [SLACK_URL]
        assert json.loads(opener.requests[0].data) == {
            "text": "Deployed example.com\nwasm-example.com is running"
        }

    def test_discord_payload_is_content(self, config: Config) -> None:
        """Discord webhooks require ``{"content": ...}``."""
        config.set("notifications.enabled", True)
        config.set("notifications.channels.discord.webhook_url", DISCORD_URL)
        opener = CapturingOpener()

        Notifier(config, opener=opener).notify(make_event())

        assert [r.full_url for r in opener.requests] == [DISCORD_URL]
        assert json.loads(opener.requests[0].data) == {
            "content": "Deployed example.com\nwasm-example.com is running"
        }


class TestTelegramChannel:
    """The bot token rides in the URL, which is why it must never be logged."""

    def _configure(self, config: Config) -> None:
        config.set("notifications.enabled", True)
        config.set("notifications.channels.telegram.bot_token", BOT_TOKEN)
        config.set("notifications.channels.telegram.chat_id", "-1002003004005")

    def test_builds_the_bot_api_url(self, config: Config) -> None:
        """sendMessage with chat_id and text, addressed with the token."""
        self._configure(config)
        opener = CapturingOpener()

        Notifier(config, opener=opener).notify(make_event())

        assert len(opener.requests) == 1
        request = opener.requests[0]
        assert request.full_url == f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        assert json.loads(request.data) == {
            "chat_id": "-1002003004005",
            "text": "Deployed example.com\nwasm-example.com is running",
        }

    def test_half_a_configuration_sends_nothing(self, config: Config) -> None:
        """A token without a chat_id has nowhere to deliver to."""
        config.set("notifications.enabled", True)
        config.set("notifications.channels.telegram.bot_token", BOT_TOKEN)
        opener = CapturingOpener()

        Notifier(config, opener=opener).notify(make_event())

        assert opener.requests == []

    def test_a_failure_never_logs_the_token(
        self, config: Config, caplog: pytest.LogCaptureFixture
    ) -> None:
        """urllib quotes the full URL in some errors; the log must not."""
        self._configure(config)
        opener = CapturingOpener()
        opener.errors["https://api.telegram.org"] = ValueError(
            f"unknown url type: 'https://api.telegram.org/bot{BOT_TOKEN}/sendMessage'"
        )

        with caplog.at_level(logging.WARNING, logger="wasm.core.notifier"):
            Notifier(config, opener=opener).notify(make_event())

        assert "telegram" in caplog.text
        assert BOT_TOKEN not in caplog.text
        assert "***" in caplog.text

    def test_a_malformed_token_is_refused_before_it_becomes_a_url(
        self, config: Config, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The token is part of the request path; junk must not reshape the URL."""
        config.set("notifications.enabled", True)
        config.set("notifications.channels.telegram.bot_token", "junk/../../evil")
        config.set("notifications.channels.telegram.chat_id", "42")
        opener = CapturingOpener()

        with caplog.at_level(logging.WARNING, logger="wasm.core.notifier"):
            Notifier(config, opener=opener).notify(make_event())

        assert opener.requests == []
        assert "telegram" in caplog.text


class TestTestChannel:
    """The settings-page button: try one channel, get the truth back."""

    def test_returns_none_on_success(self, config: Config) -> None:
        """A delivered test message is a None, so the UI can say 'sent'."""
        config.set("notifications.channels.webhook.webhook_url", WEBHOOK_URL)
        opener = CapturingOpener()

        result = Notifier(config, opener=opener).test_channel("webhook")

        assert result is None
        assert [r.full_url for r in opener.requests] == [WEBHOOK_URL]

    def test_works_while_notifications_are_disabled(self, config: Config) -> None:
        """The button exists to try a channel before switching the feature on."""
        assert config.get("notifications.enabled") is False
        config.set("notifications.channels.slack.webhook_url", SLACK_URL)
        opener = CapturingOpener()

        assert Notifier(config, opener=opener).test_channel("slack") is None
        assert len(opener.requests) == 1

    def test_returns_the_error_verbatim(self, config: Config) -> None:
        """A system error is never paraphrased; the UI shows these words."""
        config.set("notifications.channels.webhook.webhook_url", WEBHOOK_URL)
        opener = CapturingOpener()
        opener.errors["https://hooks.example.test"] = URLError("connection refused by the endpoint")

        result = Notifier(config, opener=opener).test_channel("webhook")

        assert result == "connection refused by the endpoint"

    def test_reports_an_unconfigured_channel(self, config: Config) -> None:
        """The message names the setting the operator has to fill in."""
        result = Notifier(config, opener=CapturingOpener()).test_channel("slack")

        assert result is not None
        assert "notifications.channels.slack.webhook_url" in result

    def test_rejects_an_unknown_channel_name(self, config: Config) -> None:
        """A typo in the caller must come back as words, not a crash."""
        result = Notifier(config, opener=CapturingOpener()).test_channel("pigeon")

        assert result is not None
        assert "pigeon" in result


class FakeEmailNotifier:
    """
    Records what would have been emailed, instead of opening SMTP.

    Attributes:
        sent: Every message handed to the transport.
        is_configured: What the notifier answers when asked.
    """

    def __init__(self, configured: bool = True) -> None:
        self.sent: list = []
        self.is_configured = configured

    def _send(self, content: object) -> bool:
        self.sent.append(content)
        return True


class TestEmailChannel:
    """Email reuses the monitor's SMTP implementation, never a second one."""

    def test_delegates_to_the_email_notifier(self, config: Config) -> None:
        """The event becomes one message through the injected transport."""
        config.set("notifications.enabled", True)
        config.set("notifications.channels.email.enabled", True)
        opener = CapturingOpener()
        email = FakeEmailNotifier()

        Notifier(config, opener=opener, email_notifier=email).notify(make_event())

        assert len(email.sent) == 1
        assert email.sent[0].subject == "[WASM] Deployed example.com"
        assert "wasm-example.com is running" in email.sent[0].text
        assert opener.requests == []

    def test_a_disabled_email_channel_is_skipped(self, config: Config) -> None:
        """channels.email.enabled is the switch, not the SMTP settings."""
        config.set("notifications.enabled", True)
        email = FakeEmailNotifier()

        Notifier(config, opener=CapturingOpener(), email_notifier=email).notify(make_event())

        assert email.sent == []

    def test_unconfigured_smtp_is_skipped_not_raised(self, config: Config) -> None:
        """A host without SMTP settings must not turn every deploy into noise."""
        config.set("notifications.enabled", True)
        config.set("notifications.channels.email.enabled", True)
        email = FakeEmailNotifier(configured=False)

        Notifier(config, opener=CapturingOpener(), email_notifier=email).notify(make_event())

        assert email.sent == []


class TestAgreementWithDefaults:
    """
    config.py spells out the same names the notifier owns.

    It cannot import them - the notifier reads its settings from config.py -
    so the agreement is pinned here, the same way the web security defaults
    are pinned in tests/test_cli_web.py.
    """

    def test_default_event_switches_match_event_kinds(self) -> None:
        """Every kind is switchable, and no orphan switch survives a rename."""
        assert set(DEFAULT_CONFIG["notifications"]["events"]) == set(EVENT_KINDS)

    def test_default_channel_blocks_match_channel_names(self) -> None:
        """Every channel is configurable, and no orphan block survives."""
        assert set(DEFAULT_CONFIG["notifications"]["channels"]) == set(CHANNELS)

    def test_notifications_ship_disabled(self) -> None:
        """Nothing may phone anywhere until the operator says so."""
        assert DEFAULT_CONFIG["notifications"]["enabled"] is False

    def test_an_unknown_kind_is_rejected_at_the_publisher(self) -> None:
        """A typo'd kind would silently bypass the operator's filters."""
        with pytest.raises(ValueError, match="deploy_sucess"):
            NotificationEvent(kind="deploy_sucess", title="t", body="b")
