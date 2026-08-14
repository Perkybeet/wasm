# Copyright (c) 2024-2026 Yago Lopez Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
Tests for the editable settings sections and their htmx adapters.

The editor writes root-owned configuration over the config API's own
functions, so these are written as audits rather than as happy paths:

- **A save lands in the file and in the audit trail**, with the dotted keys
  it changed and never their values.
- **A refusal is verbatim and writes nothing.** The API's own words come
  back at 200 on the form, because htmx does not swap an error status.
- **Channel secrets never render.** A stored webhook URL or bot token is a
  capability; the form carries the redaction placeholder, an unchanged save
  keeps the stored value, and the audit log never learns either.
- **The test button reports the channel's own answer**, success or failure,
  in the receiving server's words.
"""

# The web fixtures are imported from test_web_views rather than replicated, so
# there stays one definition of "a signed-in panel client". Ruff reads a test
# parameter named after an imported fixture as a redefinition; here it is the
# mechanism.
# ruff: noqa: F811

from __future__ import annotations

from pathlib import Path
from urllib.error import URLError

import pytest
from fastapi.testclient import TestClient

from tests.test_notifier import CapturingOpener
from tests.test_web_views import (  # noqa: F401  (pytest resolves fixtures by name)
    MISSING_MARKER,
    anonymous,
    app,
    body_of,
    client,
    config_file,
    read_audit,
    store,
)
from wasm.core.config import Config
from wasm.core.notifier import Notifier
from wasm.web.views.settings_editor import SECTION_TITLES

#: Secrets planted where the editor could leak them. Each must stay on the
#: server: out of every page, every form and every audit line.
SLACK_URL = "https://hooks.slack.test/services/T0/B0/slack-secret-77"
BOT_TOKEN = "110201543:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw"


def store_config(**values: object) -> None:
    """
    Write settings to the sandboxed config file the way the CLI would.

    Args:
        **values: Dotted keys (dots spelled as ``__``) to values.
    """
    config = Config()
    for key, value in values.items():
        config.set(key.replace("__", "."), value)
    assert config.save() is True
    Config.reset_instance()


# ------------------------------------------------------------- reachability


def test_the_settings_page_loads_the_editor(client: TestClient) -> None:
    """The page pulls the editable sections from the editor's own route."""
    page = body_of(client, "/settings")
    assert 'hx-get="/settings/config"' in page


def test_the_editor_demands_a_session(anonymous: TestClient) -> None:
    """Configuration write surfaces are not public pages."""
    for method, path in (
        ("get", "/settings/config"),
        ("get", "/settings/config/ssl/edit"),
        ("post", "/settings/config/paths"),
        ("post", "/settings/notifications/test/slack"),
    ):
        response = getattr(anonymous, method)(path)
        assert response.status_code == 303, f"{method} {path}: {response.status_code}"
        assert response.headers["location"] == "/login"


def test_every_section_and_every_form_renders(client: TestClient) -> None:
    """Each section renders in both states with no unresolved variable."""
    editor = body_of(client, "/settings/config")
    assert MISSING_MARKER not in editor
    for name in SECTION_TITLES:
        assert f'id="settings-{name}"' in editor
        form = body_of(client, f"/settings/config/{name}/edit")
        assert MISSING_MARKER not in form, name
        assert f'hx-post="/settings/config/{name}"' in form


def test_an_unknown_section_is_refused(client: TestClient) -> None:
    """A section the editor does not offer is a 404, not a stack trace."""
    assert client.get("/settings/config/monitor/edit").status_code == 404
    assert client.post("/settings/config/monitor", data={}).status_code == 404


# ------------------------------------------------------------------- saving


def test_saving_paths_persists_via_the_api_and_audits(
    client: TestClient, config_file: Path, sandbox: Path
) -> None:
    """A save reaches the file through the single writer and leaves a trace."""
    response = client.post("/settings/config/paths", data={"apps_directory": "/srv/apps"})
    assert response.status_code == 200
    assert "Saved" in response.text

    assert config_file.exists()
    assert Config().get("apps_directory") == "/srv/apps"

    saves = [e for e in read_audit(sandbox) if e["action"] == "config.update"]
    assert saves, "a configuration save must be audited"
    assert saves[-1]["resource"] == "/settings/config/paths"
    assert "apps_directory" in saves[-1]["detail"]


def test_a_relative_apps_directory_is_refused(client: TestClient, config_file: Path) -> None:
    """A relative path in a systemd world is a unit that fails to start."""
    response = client.post("/settings/config/paths", data={"apps_directory": "apps"})
    assert response.status_code == 200
    assert "absolute path" in response.text
    assert not config_file.exists(), "a refused save must write nothing"


def test_an_unsupported_webserver_is_refused_verbatim(
    client: TestClient, config_file: Path
) -> None:
    """The API's own refusal comes back on the form, and nothing is stored."""
    response = client.post("/settings/config/webserver", data={"webserver": "caddy"})
    assert response.status_code == 200
    assert "Webserver must be" in response.text
    assert not config_file.exists()


def test_ssl_email_and_toggle_persist(client: TestClient, config_file: Path) -> None:
    """The SSL section round-trips through the API's own model."""
    response = client.post(
        "/settings/config/ssl",
        data={"email": "ops@example.com"},  # checkbox absent: disabled
    )
    assert "Saved" in response.text

    config = Config()
    assert config.get("ssl.email") == "ops@example.com"
    assert config.get("ssl.enabled") is False
    assert config.get("ssl.provider") == "certbot", "the provider must survive the form"


def test_backup_settings_persist_and_bad_retention_is_refused(
    client: TestClient, config_file: Path
) -> None:
    """Retention is an integer with bounds; the refusal names the field."""
    response = client.post(
        "/settings/config/backups", data={"directory": "/srv/backups", "max_per_app": "5"}
    )
    assert "Saved" in response.text
    assert Config().get("backup.directory") == "/srv/backups"
    assert Config().get("backup.max_per_app") == 5

    refused = client.post(
        "/settings/config/backups", data={"directory": "/srv/backups", "max_per_app": "many"}
    )
    assert "max_per_app" in refused.text
    assert Config().get("backup.max_per_app") == 5, "a refused save must change nothing"


def test_web_limits_persist_as_integers(client: TestClient, config_file: Path) -> None:
    """The three exposed web keys save as numbers, with the restart warning."""
    response = client.post(
        "/settings/config/web",
        data={
            "token_expiration_hours": "24",
            "rate_limit_requests": "60",
            "rate_limit_window": "30",
        },
    )
    assert "Saved" in response.text
    assert "restart" in response.text

    config = Config()
    assert config.get("web.token_expiration_hours") == 24
    assert config.get("web.rate_limit_requests") == 60
    assert config.get("web.rate_limit_window") == 30


def test_a_non_numeric_web_value_is_refused_and_nothing_is_stored(
    client: TestClient, config_file: Path
) -> None:
    """Everything is parsed before anything is written."""
    response = client.post(
        "/settings/config/web",
        data={
            "token_expiration_hours": "24",
            "rate_limit_requests": "lots",
            "rate_limit_window": "30",
        },
    )
    assert response.status_code == 200
    assert "rate_limit_requests" in response.text
    assert not config_file.exists(), "a refused save must leave the section unwritten"


def test_the_bind_address_is_not_on_the_web_form(client: TestClient) -> None:
    """Where the panel listens is a start-up decision, not a session's."""
    form = body_of(client, "/settings/config/web/edit")
    assert 'name="host"' not in form
    assert 'name="port"' not in form


# ------------------------------------------------------------ notifications


def test_notification_toggles_persist(client: TestClient, config_file: Path) -> None:
    """The master switch and the per-kind switches land in the file."""
    response = client.post(
        "/settings/config/notifications",
        data={"enabled": "yes", "event_deploy_failed": "yes", "telegram_chat_id": ""},
    )
    assert "Saved" in response.text

    config = Config()
    assert config.get("notifications.enabled") is True
    assert config.get("notifications.events.deploy_failed") is True
    assert config.get("notifications.events.deploy_success") is False, (
        "an unticked kind must be switched off, not left at its default"
    )


def test_channel_secrets_never_render(client: TestClient, config_file: Path) -> None:
    """A webhook URL is a capability; the form shows only the placeholder."""
    store_config(
        notifications__channels__slack__webhook_url=SLACK_URL,
        notifications__channels__telegram__bot_token=BOT_TOKEN,
    )

    for path in ("/settings/config", "/settings/config/notifications/edit"):
        page = body_of(client, path)
        assert SLACK_URL not in page, path
        assert BOT_TOKEN not in page, path

    form = body_of(client, "/settings/config/notifications/edit")
    assert 'value="***"' in form, "a configured channel must read as set, not as empty"


def test_the_placeholder_keeps_the_stored_secret(client: TestClient, config_file: Path) -> None:
    """Posting back what the form showed must not destroy the credential."""
    store_config(notifications__channels__slack__webhook_url=SLACK_URL)

    response = client.post("/settings/config/notifications", data={"slack_url": "***"})
    assert response.status_code == 200
    assert Config().get("notifications.channels.slack.webhook_url") == SLACK_URL


def test_clearing_the_field_removes_the_channel(client: TestClient, config_file: Path) -> None:
    """An emptied field is the explicit off position for a channel."""
    store_config(notifications__channels__slack__webhook_url=SLACK_URL)

    client.post("/settings/config/notifications", data={"slack_url": ""})
    assert Config().get("notifications.channels.slack.webhook_url") == ""


def test_a_new_url_replaces_the_stored_one_and_stays_out_of_the_audit(
    client: TestClient, config_file: Path, sandbox: Path
) -> None:
    """The audit line names the key, never the capability behind it."""
    replacement = "https://hooks.slack.test/services/T1/B1/new-secret-88"
    response = client.post("/settings/config/notifications", data={"slack_url": replacement})
    assert "Saved" in response.text
    assert replacement not in response.text, "a pasted secret must not be echoed back"
    assert Config().get("notifications.channels.slack.webhook_url") == replacement

    audit_text = (sandbox / "state" / "web-audit.log").read_text()
    assert replacement not in audit_text
    assert "notifications.channels.slack.webhook_url" in audit_text


def test_a_non_http_url_is_refused(client: TestClient, config_file: Path) -> None:
    """urlopen would follow ftp:// and file://; the form refuses them first."""
    response = client.post(
        "/settings/config/notifications", data={"slack_url": "ftp://files.example.test/hook"}
    )
    assert response.status_code == 200
    assert "must be an http:// or https:// URL" in response.text
    assert not config_file.exists()


# --------------------------------------------------------------- test button


def notifier_with(opener: CapturingOpener) -> Notifier:
    """
    Args:
        opener: The stand-in for urlopen.

    Returns:
        A notifier over the current sandboxed configuration.
    """
    config = Config()
    config.reload()
    return Notifier(config, opener=opener)


def test_send_test_reports_success(
    client: TestClient, config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A delivered test message is confirmed in the section, by channel name."""
    store_config(notifications__channels__slack__webhook_url=SLACK_URL)
    opener = CapturingOpener()
    monkeypatch.setattr(
        "wasm.web.views.settings_editor._build_notifier", lambda: notifier_with(opener)
    )

    response = client.post("/settings/notifications/test/slack")
    assert response.status_code == 200
    assert "Test message sent through slack" in response.text
    assert [request.full_url for request in opener.requests] == [SLACK_URL]


def test_send_test_reports_the_failure_verbatim(
    client: TestClient, config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A system error is never paraphrased; these are the endpoint's words."""
    store_config(notifications__channels__slack__webhook_url=SLACK_URL)
    opener = CapturingOpener()
    opener.errors["https://hooks.slack.test"] = URLError("connection refused by the endpoint")
    monkeypatch.setattr(
        "wasm.web.views.settings_editor._build_notifier", lambda: notifier_with(opener)
    )

    response = client.post("/settings/notifications/test/slack")
    assert response.status_code == 200
    assert "connection refused by the endpoint" in response.text
    assert SLACK_URL not in response.text


def test_send_test_names_the_missing_setting(client: TestClient, config_file: Path) -> None:
    """An unconfigured channel answers with the setting to fill in."""
    response = client.post("/settings/notifications/test/webhook")
    assert response.status_code == 200
    assert "notifications.channels.webhook.webhook_url" in response.text
