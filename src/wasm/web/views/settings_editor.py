# Copyright (c) 2024-2026 Yago Lopez Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
Editable configuration sections on the settings page.

Own module so concurrent panel work never edits the aggregate router: it
includes this one at the bottom of that file. Handlers follow the same
contract as the rest of the views: synchronous, session-guarded, rendering
Jinja fragments over the managers.

Every handler is an adapter over :mod:`wasm.web.api.config`, exactly like the
databases screen is an adapter over its API module: the single config writer,
the redaction and the placeholder round-trip all live there, and a second
implementation of "write config.yaml" is what this panel must never grow. A
handler here translates a form into the API's own request models and a
refusal into a fragment, nothing more.

What is editable is deliberately bounded:

- **Stored credentials are not.** Database passwords, the SMTP account and
  API keys are changed from a root shell, because a panel session must not be
  able to raise the panel's own security configuration (the page says so).
- **The bind address and port are not.** Where the panel listens is decided
  by whoever starts it (``wasm web start``), not from inside a session that
  only exists because of the current bind.
- **Notification endpoints are the one exception**: they are secrets by the
  redaction rules, so the stored value is never shown; the operator pastes a
  whole new value to replace one, leaves the placeholder to keep it, or
  clears the field to remove it - the contract
  :func:`wasm.core.config.restore_redacted` was written for.

Every save is recorded in the audit log with the dotted key names that
changed and never their values: a webhook URL is a capability, and the audit
trail must not become the second place it is stored.

Refusals render at 200 on purpose: htmx does not swap an error status, so a
400 would leave the screen frozen and report the refusal nowhere. The refusal
is on the fragment itself, verbatim, which is where the operator is looking.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from urllib.parse import urlparse

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import ValidationError

from wasm.core.config import DEFAULT_CONFIG, REDACTED
from wasm.core.notifier import CHANNELS, EVENT_KINDS, Notifier
from wasm.web.api import config as config_api
from wasm.web.auth import get_audit_logger, get_client_ip
from wasm.web.views.rendering import page
from wasm.web.views.router import PageErrorRoute, _form_fields, require_page_session

router: APIRouter = APIRouter(
    include_in_schema=False,
    dependencies=[Depends(require_page_session)],
    route_class=PageErrorRoute,
)

#: The editable sections, in the order they render. Every name is also the
#: suffix of the form template that edits it.
SECTION_TITLES: dict[str, str] = {
    "paths": "Paths",
    "webserver": "Web server",
    "ssl": "SSL",
    "backups": "Backups",
    "web": "Panel",
    "notifications": "Notifications",
}

#: The ``web.*`` keys the panel may edit. The bind address and port are
#: start-up decisions and stay out; these three only tune how strict the
#: running panel is with its clients.
_WEB_KEYS: tuple[str, ...] = (
    "token_expiration_hours",
    "rate_limit_requests",
    "rate_limit_window",
)

#: Channel names to what the section calls them on screen.
_CHANNEL_LABELS: dict[str, str] = {
    "webhook": "Webhook",
    "slack": "Slack",
    "discord": "Discord",
    "telegram": "Telegram",
    "email": "Email",
}


def _build_notifier() -> Notifier:
    """
    Build the notifier over the configuration as it stands on disk.

    Module-level so a test can stand in a notifier whose opener never opens a
    socket, the same seam :mod:`tests.test_notifier` uses.

    Returns:
        A notifier reading the freshly reloaded configuration.
    """
    return Notifier(config_api.load_config())


def _session(request: Request) -> dict[str, Any]:
    """
    Read the session the page dependency attached.

    Args:
        request: The incoming request.

    Returns:
        The session payload, or an empty mapping outside a request cycle.
    """
    return getattr(request.state, "session", None) or {}


def _bool_text(value: Any) -> str:
    """
    Render a boolean the way the read-only configuration dump does.

    Args:
        value: The stored value.

    Returns:
        ``true`` or ``false``.
    """
    return "true" if value else "false"


def _require_known(section: str) -> None:
    """
    Refuse a section name the editor does not offer.

    Args:
        section: The name from the URL.

    Raises:
        HTTPException: 404 for anything not in :data:`SECTION_TITLES`.
    """
    if section not in SECTION_TITLES:
        raise HTTPException(status_code=404, detail=f"No editable section named {section!r}")


def _positive_int(name: str, raw: str) -> int:
    """
    Parse a form field that must be a whole number of at least one.

    Args:
        name: Field name, for the error message.
        raw: What the form carried.

    Returns:
        The parsed value.

    Raises:
        ValueError: When the field is not a positive whole number.
    """
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(f"{name} must be a whole number, got {raw!r}") from None
    if value < 1:
        raise ValueError(f"{name} must be at least 1, got {value}")
    return value


def _require_http_url(name: str, url: str) -> str:
    """
    Refuse a notification URL whose scheme is not plain HTTP(S).

    A courtesy copy of the check the notifier applies at delivery time, so a
    typo is reported at the form instead of stored and discovered in a log.
    The notifier remains the chokepoint.

    Args:
        name: Field name, for the error message.
        url: The submitted URL. Empty is allowed: it clears the channel.

    Returns:
        The URL, unchanged.

    Raises:
        ValueError: When the scheme is anything but http or https.
    """
    if url and urlparse(url).scheme.lower() not in ("http", "https"):
        raise ValueError(f"{name} must be an http:// or https:// URL")
    return url


# ------------------------------------------------------------------ context


def _paths_context(request: Request) -> dict[str, Any]:
    """
    Args:
        request: The incoming request.

    Returns:
        The paths section's own context keys.
    """
    answer = config_api.get_apps_directory(_session(request))
    return {
        "apps_directory": answer["apps_directory"],
        "entries": [("apps_directory", answer["apps_directory"])],
    }


def _webserver_context(request: Request) -> dict[str, Any]:
    """
    Args:
        request: The incoming request.

    Returns:
        The web server section's own context keys.
    """
    answer = config_api.get_webserver(_session(request))
    return {
        "webserver": answer["webserver"],
        "webservers": sorted(config_api.SUPPORTED_WEBSERVERS),
        "entries": [("webserver", answer["webserver"])],
    }


def _ssl_context(request: Request) -> dict[str, Any]:
    """
    Args:
        request: The incoming request.

    Returns:
        The SSL section's own context keys.
    """
    answer = config_api.get_ssl_config(_session(request))
    return {
        "enabled": bool(answer["enabled"]),
        "provider": answer["provider"],
        "email": answer["email"],
        "entries": [
            ("ssl.enabled", _bool_text(answer["enabled"])),
            ("ssl.provider", answer["provider"]),
            ("ssl.email", answer["email"] or "—"),
        ],
    }


def _backups_context(request: Request) -> dict[str, Any]:
    """
    Args:
        request: The incoming request.

    Returns:
        The backups section's own context keys.
    """
    answer = config_api.get_backup_config(_session(request))
    return {
        "directory": answer["directory"],
        "max_per_app": answer["max_per_app"],
        "entries": [
            ("backup.directory", answer["directory"]),
            ("backup.max_per_app", answer["max_per_app"]),
        ],
    }


def _web_values() -> dict[str, int]:
    """
    Read the editable ``web.*`` values, with the shipped defaults.

    Returns:
        Field name to current value.
    """
    config = config_api.load_config()
    defaults: dict[str, Any] = DEFAULT_CONFIG["web"]
    return {key: int(config.get(f"web.{key}", defaults[key])) for key in _WEB_KEYS}


def _web_context(request: Request) -> dict[str, Any]:
    """
    Args:
        request: The incoming request. Unused; the builders share a signature.

    Returns:
        The panel section's own context keys.
    """
    values = _web_values()
    return {
        **values,
        "entries": [(f"web.{key}", values[key]) for key in _WEB_KEYS],
    }


def _notifications_context(request: Request) -> dict[str, Any]:
    """
    Build the notifications section context, secrets excluded.

    Only "configured or not" leaves this function for the URL and token
    channels: the stored values are capabilities, and the form renders the
    redaction placeholder instead so an unchanged save keeps them.

    Args:
        request: The incoming request. Unused; the builders share a signature.

    Returns:
        The notifications section's own context keys.
    """
    config = config_api.load_config()
    settings = config.get("notifications", {}) or {}
    channels: dict[str, Any] = settings.get("channels") or {}
    events: dict[str, Any] = settings.get("events") or {}
    enabled = bool(settings.get("enabled", False))

    telegram: dict[str, Any] = channels.get("telegram") or {}
    chat_id = str(telegram.get("chat_id") or "")
    email_on = bool((channels.get("email") or {}).get("enabled", False))

    def has_url(name: str) -> bool:
        return bool((channels.get(name) or {}).get("webhook_url"))

    configured = {
        "webhook": has_url("webhook"),
        "slack": has_url("slack"),
        "discord": has_url("discord"),
        "telegram": bool(telegram.get("bot_token")) and bool(chat_id),
        "email": email_on,
    }

    rows = []
    for name in CHANNELS:
        meta: list[tuple[str, str]] = []
        if name == "telegram":
            meta = [("chat id", chat_id or "—")]
        if name == "email":
            meta = [("smtp", "monitor.smtp.* (CLI)")]
        rows.append(
            {
                "name": name,
                "label": _CHANNEL_LABELS[name],
                "configured": configured[name],
                "meta": meta,
            }
        )

    on_kinds = [kind for kind in EVENT_KINDS if bool(events.get(kind, True))]
    return {
        "enabled": enabled,
        "events": [{"kind": kind, "enabled": bool(events.get(kind, True))} for kind in EVENT_KINDS],
        "channels": rows,
        "chat_id": chat_id,
        "token_set": bool(telegram.get("bot_token")),
        "url_set": {name: configured[name] for name in ("webhook", "slack", "discord")},
        "email_enabled": email_on,
        "entries": [
            ("notifications.enabled", _bool_text(enabled)),
            ("events", ", ".join(on_kinds) if on_kinds else "all off"),
        ],
    }


_CONTEXTS: dict[str, Callable[[Request], dict[str, Any]]] = {
    "paths": _paths_context,
    "webserver": _webserver_context,
    "ssl": _ssl_context,
    "backups": _backups_context,
    "web": _web_context,
    "notifications": _notifications_context,
}


def _section_context(
    request: Request,
    section: str,
    *,
    editing: bool = False,
    notice: str | None = None,
    problem: dict[str, str] | None = None,
    submitted: dict[str, str] | None = None,
    test: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Build the context one editable section renders from.

    Args:
        request: The incoming request.
        section: Section name, one of :data:`SECTION_TITLES`.
        editing: Whether to render the form instead of the facts.
        notice: One line reporting what a save just did.
        problem: A ``fix``/``output`` mapping when a save was refused.
        submitted: What the operator typed, preserved across a refusal so the
            form does not empty what they have to correct. Never populated
            for the notifications form: echoing a pasted secret back would
            put it in one more response than it needs to be in.
        test: The outcome of a channel test: ``channel`` and ``error``, the
            latter None on success and otherwise the failure verbatim.

    Returns:
        The fragment context, under the single key the template reads.
    """
    context = {
        "name": section,
        "title": SECTION_TITLES[section],
        "editing": editing,
        "notice": notice,
        "problem": problem,
        "submitted": submitted or {},
        "test": test,
    }
    context.update(_CONTEXTS[section](request))
    return context


def _section_fragment(request: Request, section: str, **kwargs: Any) -> HTMLResponse:
    """
    Render one editable section for an htmx swap.

    Args:
        request: The incoming request.
        section: Section name.
        **kwargs: Forwarded to :func:`_section_context`.

    Returns:
        The rendered fragment.
    """
    return page(
        request,
        "fragments/settings_section.html",
        {"section": _section_context(request, section, **kwargs)},
    )


# ------------------------------------------------------------------- savers


def _save_paths(request: Request, fields: dict[str, str]) -> list[str]:
    """
    Apply the paths form through the API.

    Args:
        request: The incoming request.
        fields: The submitted form.

    Returns:
        The dotted keys that changed.

    Raises:
        ValueError: When the directory is missing or not absolute.
        HTTPException: When the configuration cannot be written.
    """
    directory = fields.get("apps_directory", "")
    if not directory.startswith("/"):
        raise ValueError(f"apps_directory must be an absolute path, got {directory!r}")
    if directory == config_api.get_apps_directory(_session(request))["apps_directory"]:
        return []
    config_api.update_apps_directory(
        config_api.AppsDirConfig(apps_directory=directory), _session(request)
    )
    return ["apps_directory"]


def _save_webserver(request: Request, fields: dict[str, str]) -> list[str]:
    """
    Apply the web server form through the API, which owns the whitelist.

    Args:
        request: The incoming request.
        fields: The submitted form.

    Returns:
        The dotted keys that changed.

    Raises:
        HTTPException: 400 from the API for an unsupported server, or a
            write failure.
    """
    webserver = fields.get("webserver", "")
    if webserver == config_api.get_webserver(_session(request))["webserver"]:
        return []
    config_api.update_webserver(config_api.WebserverConfig(webserver=webserver), _session(request))
    return ["webserver"]


def _save_ssl(request: Request, fields: dict[str, str]) -> list[str]:
    """
    Apply the SSL form through the API.

    The provider is carried over unchanged: the panel offers no alternative,
    so a form must not be able to blank it.

    Args:
        request: The incoming request.
        fields: The submitted form.

    Returns:
        The dotted keys that changed.

    Raises:
        HTTPException: When the configuration cannot be written.
    """
    current = config_api.get_ssl_config(_session(request))
    enabled = "enabled" in fields
    email = fields.get("email", "")

    changed = []
    if enabled != bool(current["enabled"]):
        changed.append("ssl.enabled")
    if email != current["email"]:
        changed.append("ssl.email")
    if not changed:
        return []

    config_api.update_ssl_config(
        config_api.SSLConfig(enabled=enabled, provider=current["provider"], email=email),
        _session(request),
    )
    return changed


def _save_backups(request: Request, fields: dict[str, str]) -> list[str]:
    """
    Apply the backups form through the API.

    Args:
        request: The incoming request.
        fields: The submitted form.

    Returns:
        The dotted keys that changed.

    Raises:
        ValueError: When the directory is not absolute or the retention is
            not a positive whole number.
        ValidationError: When the API model refuses the retention bounds.
        HTTPException: When the configuration cannot be written.
    """
    directory = fields.get("directory", "")
    if not directory.startswith("/"):
        raise ValueError(f"backup.directory must be an absolute path, got {directory!r}")
    max_per_app = _positive_int("backup.max_per_app", fields.get("max_per_app", ""))

    current = config_api.get_backup_config(_session(request))
    changed = []
    if directory != current["directory"]:
        changed.append("backup.directory")
    if max_per_app != int(current["max_per_app"]):
        changed.append("backup.max_per_app")
    if not changed:
        return []

    config_api.update_backup_config(
        config_api.BackupConfig(directory=directory, max_per_app=max_per_app), _session(request)
    )
    return changed


def _save_web(request: Request, fields: dict[str, str]) -> list[str]:
    """
    Apply the panel form as one PATCH per changed key.

    Everything is parsed before anything is written, so a refused field
    cannot leave the section half saved.

    Args:
        request: The incoming request.
        fields: The submitted form.

    Returns:
        The dotted keys that changed.

    Raises:
        ValueError: When a field is not a positive whole number.
        HTTPException: When the configuration cannot be written.
    """
    current = _web_values()
    changes = []
    for key in _WEB_KEYS:
        value = _positive_int(f"web.{key}", fields.get(key, ""))
        if value != current[key]:
            changes.append((f"web.{key}", value))

    for path, value in changes:
        config_api.patch_config(
            config_api.ConfigPatchRequest(path=path, value=value), _session(request)
        )
    return [path for path, _ in changes]


def _save_notifications(request: Request, fields: dict[str, str]) -> list[str]:
    """
    Apply the notifications form as one PATCH per changed key.

    A URL or token field carrying the redaction placeholder is the "keep it"
    position and is skipped before it can travel further; an empty one clears
    the channel; anything else replaces the stored value whole. Everything is
    validated before anything is written.

    Args:
        request: The incoming request.
        fields: The submitted form.

    Returns:
        The dotted keys that changed.

    Raises:
        ValueError: When a submitted URL is not plain HTTP(S).
        HTTPException: When the configuration cannot be written.
    """
    config = config_api.load_config()
    changes: list[tuple[str, Any]] = []

    def want(path: str, value: Any) -> None:
        if config.get(path) != value:
            changes.append((path, value))

    want("notifications.enabled", "enabled" in fields)
    for kind in EVENT_KINDS:
        want(f"notifications.events.{kind}", f"event_{kind}" in fields)

    for name in ("webhook", "slack", "discord"):
        raw = fields.get(f"{name}_url", "")
        if raw == REDACTED:
            continue
        _require_http_url(f"notifications.channels.{name}.webhook_url", raw)
        want(f"notifications.channels.{name}.webhook_url", raw)

    token = fields.get("telegram_bot_token", "")
    if token != REDACTED:
        want("notifications.channels.telegram.bot_token", token)
    want("notifications.channels.telegram.chat_id", fields.get("telegram_chat_id", ""))
    want("notifications.channels.email.enabled", "email_enabled" in fields)

    for path, value in changes:
        config_api.patch_config(
            config_api.ConfigPatchRequest(path=path, value=value), _session(request)
        )
    return [path for path, _ in changes]


_SAVERS: dict[str, Callable[[Request, dict[str, str]], list[str]]] = {
    "paths": _save_paths,
    "webserver": _save_webserver,
    "ssl": _save_ssl,
    "backups": _save_backups,
    "web": _save_web,
    "notifications": _save_notifications,
}


def _audit_save(request: Request, section: str, changed: list[str]) -> None:
    """
    Record a configuration save in the audit trail.

    Key names only, never values: several of these settings are capability
    URLs and the audit log must not become the second place they are stored.

    Args:
        request: The incoming request.
        section: The section that was saved.
        changed: The dotted keys that changed. May be empty.
    """
    audit = get_audit_logger()
    if audit is None:
        return
    audit.record(
        action="config.update",
        result="success",
        client_ip=get_client_ip(request),
        actor=str(_session(request).get("sid") or "master"),
        resource=f"/settings/config/{section}",
        detail=("changed " + ", ".join(changed)) if changed else "no changes",
    )


def _saved_notice(section: str, changed: list[str]) -> str:
    """
    Word the confirmation a save answers with.

    Args:
        section: The section that was saved.
        changed: The dotted keys that changed.

    Returns:
        The notice line.
    """
    if not changed:
        return "Nothing changed."
    if section == "web":
        return "Saved. The panel reads these at start-up, so they apply on the next restart."
    return "Saved."


# ---------------------------------------------------------------- handlers


@router.get("/settings/config", response_class=HTMLResponse)
def settings_editor(request: Request) -> HTMLResponse:
    """
    Render every editable section, for the settings page's lazy load.

    The settings page handler lives in the aggregate router; this module adds
    to the page by being fetched into it, so concurrent panel work never
    edits that file.

    Args:
        request: The incoming request.

    Returns:
        The editor fragment holding all sections.
    """
    sections = [_section_context(request, name) for name in SECTION_TITLES]
    return page(request, "fragments/settings_editor.html", {"editable_sections": sections})


@router.get("/settings/config/{section}", response_class=HTMLResponse)
def settings_section(section: str, request: Request) -> HTMLResponse:
    """
    Render one section in its read state; the edit form's Cancel lands here.

    Args:
        section: Section name.
        request: The incoming request.

    Returns:
        The section fragment.

    Raises:
        HTTPException: 404 for a section the editor does not offer.
    """
    _require_known(section)
    return _section_fragment(request, section)


@router.get("/settings/config/{section}/edit", response_class=HTMLResponse)
def settings_section_edit(section: str, request: Request) -> HTMLResponse:
    """
    Render one section's edit form.

    Args:
        section: Section name.
        request: The incoming request.

    Returns:
        The section fragment in its editing state.

    Raises:
        HTTPException: 404 for a section the editor does not offer.
    """
    _require_known(section)
    return _section_fragment(request, section, editing=True)


@router.post("/settings/config/{section}", response_class=HTMLResponse)
def settings_section_save(
    section: str, request: Request, body: bytes = Body(default=b"")
) -> HTMLResponse:
    """
    Apply one section's form through the config API and say what happened.

    Args:
        section: Section name.
        request: The incoming request.
        body: The urlencoded form.

    Returns:
        The section back in its read state with the save confirmed, or the
        form again with the refusal verbatim and nothing half written.

    Raises:
        HTTPException: 404 for a section the editor does not offer.
    """
    _require_known(section)
    fields = _form_fields(body)
    # Pasted secrets are not echoed back into a response; every other form
    # keeps what the operator typed so a refusal does not empty it.
    submitted = None if section == "notifications" else fields

    try:
        changed = _SAVERS[section](request, fields)
    except (ValueError, ValidationError) as exc:
        return _section_fragment(
            request,
            section,
            editing=True,
            submitted=submitted,
            problem={"fix": "Nothing was saved. Check the form.", "output": str(exc)},
        )
    except HTTPException as exc:
        if exc.status_code == 404:
            raise
        return _section_fragment(
            request,
            section,
            editing=True,
            submitted=submitted,
            problem={"fix": "Nothing was saved.", "output": str(exc.detail)},
        )

    _audit_save(request, section, changed)
    return _section_fragment(request, section, notice=_saved_notice(section, changed))


@router.post("/settings/notifications/test/{channel}", response_class=HTMLResponse)
def notifications_test(channel: str, request: Request) -> HTMLResponse:
    """
    Send a test message through one channel and report the outcome verbatim.

    The notifier ignores the on/off switches for a test on purpose: the
    button exists to try a channel before enabling anything. An unknown
    channel name comes back as words from the same method, not as a 404, so
    the answer always lands in the section the operator is looking at.

    Args:
        channel: Channel name, one of the notifier's channels.
        request: The incoming request.

    Returns:
        The notifications section with the result on it: a sent notice, or
        the failure in the receiving server's own words.
    """
    error = _build_notifier().test_channel(channel)
    return _section_fragment(request, "notifications", test={"channel": channel, "error": error})
