"""
Configuration API endpoints for WASM Web Interface.

Two rules govern this module:

- **Nothing here writes configuration.** :class:`~wasm.core.config.Config` is
  the single writer of ``config.yaml``; it creates the file 0600 inside a 0700
  directory, refuses to follow a symlink, and drops settings the code no longer
  honours. This module used to keep a second writer (``save_config_file``) built
  on ``open(path, "w")`` and a mode-less ``mkdir``, which quietly undid all of
  that on the very path the panel uses.
- **No response carries a secret.** Every payload built from configuration goes
  through :func:`~wasm.core.config.redact_secrets` first. The panel is
  authenticated, but a session is not a reason to hand out the MySQL root
  password, the OpenAI API key and the SMTP account in a JSON body that ends up
  in browser caches, screenshots and bug reports. Writes accept the
  :data:`~wasm.core.config.REDACTED` placeholder back and keep the stored value.

Handlers are synchronous: they read and write a file, and declared ``async def``
that I/O would run on the event loop and stall every other request.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from wasm.core.config import (
    DEFAULT_BACKUP_DIR,
    DEFAULT_CONFIG,
    NO_DEFAULT,
    REDACTED,
    Config,
    coerce_config_value,
    redact_secrets,
)
from wasm.web.api.auth import get_current_session
from wasm.web.api.deps import WASMErrorRoute, require_elevated
from wasm.web.auth import actor_label, get_audit_logger, get_client_ip
from wasm.web.pydantic_compat import field_validator

if TYPE_CHECKING:
    from wasm.core.notifier import Notifier

# The error boundary: Config.replace()/set() raise ConfigError (a WASMError)
# for a rejected key, which used to crash with a bare 500 because this router
# had no way to catch it - WASMErrorRoute is the one place that translation
# is stated.
router = APIRouter(route_class=WASMErrorRoute)

#: Web servers WASM can actually configure.
SUPPORTED_WEBSERVERS = frozenset({"nginx", "apache"})


def load_config() -> Config:
    """
    Return the global configuration, refreshed from disk.

    Returns:
        The configuration singleton, holding what the file currently says.
    """
    config = Config()
    config.reload()
    return config


def persist(config: Config) -> Path:
    """
    Write the configuration through the single writer and map failures to HTTP.

    Args:
        config: The configuration to persist.

    Returns:
        The path that was written.

    Raises:
        HTTPException: 403 when the path is not writable, 500 when the write
            itself fails for a reason that is not a WASM error (disk full,
            unserialisable value). A ``SecurityError`` from a symlinked
            destination is a WASMError and is left to propagate: WASMErrorRoute
            maps it to 400, which a bare 500 here used to hide.
    """
    try:
        return config.write()
    except PermissionError as exc:
        raise HTTPException(
            status_code=403, detail=f"Permission denied writing to {config.path}"
        ) from exc
    except (OSError, yaml.YAMLError) as exc:
        raise HTTPException(status_code=500, detail=f"Failed to save configuration: {exc}") from exc


def _audit_write(request: Request, session: dict[str, Any], *, changed: Sequence[str]) -> None:
    """
    Record a configuration write in the audit trail.

    Only the names of the top-level keys that changed are recorded, never a
    value: this file is where the MySQL root password and the SMTP account
    live, and the audit log is append-only and kept far longer than a single
    settings screen.

    Args:
        request: The incoming request, for the client address and the path.
        session: The authenticated session, for the actor.
        changed: Top-level configuration keys the write touched.
    """
    audit = get_audit_logger()
    if not audit:
        return
    audit.record(
        action="config.update",
        result="success",
        client_ip=get_client_ip(request),
        actor=actor_label(session),
        resource=request.url.path,
        detail=f"changed keys: {', '.join(changed)}" if changed else "no keys changed",
    )


def public_config(config: Config) -> dict[str, Any]:
    """
    Build the configuration dump that may leave the server.

    Args:
        config: The configuration to serialise.

    Returns:
        A deep copy with every secret replaced by the placeholder.
    """
    redacted: dict[str, Any] = redact_secrets(config.to_dict())
    return redacted


def _is_writable(path: Path) -> bool:
    """
    Report whether the configuration file can be written.

    A missing file is writable when the closest existing ancestor is: that is
    the case on a fresh install, where the panel must still offer to save.

    Args:
        path: Configuration file path.

    Returns:
        True if a write is expected to succeed.
    """
    try:
        if path.exists():
            return os.access(path, os.W_OK)

        ancestor = path.parent
        while not ancestor.exists() and ancestor.parent != ancestor:
            ancestor = ancestor.parent
        return os.access(ancestor, os.W_OK)
    except OSError:
        return False


# ============ Request/Response Models ============


class ConfigResponse(BaseModel):
    """Response containing full configuration, with secrets redacted."""

    config: dict
    path: str
    writable: bool


class ConfigUpdateRequest(BaseModel):
    """Request to update configuration."""

    config: dict = Field(..., description="Full configuration object")


class ConfigPatchRequest(BaseModel):
    """Request to patch specific configuration values."""

    path: str = Field(
        ..., description="Dot-separated path to config key (e.g., 'backup.max_per_app')"
    )
    value: Any = Field(..., description="New value for the configuration key")


class AppsDirConfig(BaseModel):
    """Applications directory configuration."""

    apps_directory: str = Field(..., description="Directory for deployed applications")


class WebserverConfig(BaseModel):
    """Web server configuration."""

    webserver: str = Field(..., description="Web server to use (nginx or apache)")


class BackupConfig(BaseModel):
    """Backup configuration."""

    directory: str = Field(
        str(DEFAULT_BACKUP_DIR),
        description="Backup storage directory: an absolute path, or empty for the default",
    )
    max_per_app: int = Field(10, ge=1, le=100, description="Maximum backups per application")


class SSLConfig(BaseModel):
    """SSL/TLS configuration."""

    enabled: bool = Field(True, description="Enable SSL certificates")
    provider: str = Field("certbot", description="SSL provider (certbot)")
    email: str = Field("", description="Email for certificate notifications")


class WebConfig(BaseModel):
    """Web interface configuration."""

    host: str = Field("127.0.0.1", description="Host to bind web interface")
    port: int = Field(8080, ge=1, le=65535, description="Port for web interface")
    session_timeout: int = Field(3600, ge=300, le=86400, description="Session timeout in seconds")


class SMTPConfig(BaseModel):
    """
    SMTP settings for the monitor's email notifications and test message.

    ``password`` is write-only: it is never sent back by ``GET /config/smtp``
    (see :class:`SMTPSettingsResponse`), so unlike a secret round-tripped
    through the generic ``PUT``/``PATCH /api/config`` there is no
    :data:`~wasm.core.config.REDACTED` placeholder for the console to echo
    back untouched. Instead an empty ``password`` keeps whatever is already
    stored - but only while it would still go where it went before: the same
    host, port, username and transport. Changing any of those with a blank
    password is refused (see :func:`_refuse_smtp_password_move`), so a
    credential that may write this section but never saw the password cannot
    point it at a server of its own and send itself a test email. There is no
    way to explicitly blank the password through this endpoint; ``wasm config
    set monitor.smtp.password ''`` still does that directly.
    """

    host: str = Field("", description="SMTP server hostname; empty means not configured")
    port: int = Field(465, ge=1, le=65535, description="SMTP server port")
    use_ssl: bool = Field(True, description="Connect with implicit TLS (SMTPS), usually port 465")
    use_tls: bool = Field(
        False,
        description="Connect in the clear and upgrade with STARTTLS, usually port 587",
    )
    username: str = Field(
        "", description="Account to authenticate with; empty for an anonymous relay"
    )
    password: str = Field(
        "", description="Password for that account; leave blank to keep the one already stored"
    )
    from_address: str = Field(
        "", description="Envelope sender address; falls back to the username when empty"
    )
    recipients: list[str] = Field(
        default_factory=list,
        description="Addresses the monitor's reports and test message are sent to",
    )


# ============ Response-only models ============
#
# Every model below answers a GET or confirms a write. None of them is also
# a request body, even where the fields exactly match one above (compare
# AppsDirConfig and AppsDirectoryResponse): a model FastAPI sees used as both
# a request and a response gets separate input and output schemas, and a
# field with a default becomes optional to send but required to receive -
# right for a PUT body, wrong for what a GET always returns. Two small
# classes are cheaper than that inconsistency showing up in the exported
# contract.


class MessageResponse(BaseModel):
    """A bare confirmation, for a write with nothing else to report back."""

    message: str


class ConfigUpdateResponse(BaseModel):
    """Confirmation for a full configuration replacement."""

    message: str
    path: str


class ConfigPatchResponse(BaseModel):
    """Confirmation for a single-key update, echoing the value that was stored."""

    message: str
    path: str
    value: Any


class ConfigReloadResponse(BaseModel):
    """Configuration re-read from disk."""

    message: str
    path: str
    config: dict


class AppsDirectoryResponse(BaseModel):
    """The configured applications directory."""

    apps_directory: str


class AppsDirectoryUpdateResponse(BaseModel):
    """Confirmation for an applications directory update."""

    message: str
    apps_directory: str


class WebserverResponse(BaseModel):
    """The configured web server."""

    webserver: str


class WebserverUpdateResponse(BaseModel):
    """Confirmation for a web server update."""

    message: str
    webserver: str


class BackupSettingsResponse(BaseModel):
    """The configured backup directory and retention."""

    directory: str
    max_per_app: int


class SSLSettingsResponse(BaseModel):
    """The configured SSL/TLS settings."""

    enabled: bool
    provider: str
    email: str


class WebSettingsResponse(BaseModel):
    """The configured web interface settings."""

    host: str
    port: int
    session_timeout: int


class SMTPSettingsResponse(BaseModel):
    """
    The configured SMTP settings.

    ``password`` is not a field here at all, redacted or otherwise: unlike
    ``GET /api/config``'s untyped dump, this endpoint never sends the password
    out, so there is nothing to redact and no ``***`` for a form to treat as
    "leave alone". ``password_set`` is what a console form uses instead, to
    show "a password is configured" without ever holding the value.
    """

    host: str
    port: int
    use_ssl: bool
    use_tls: bool
    username: str
    from_address: str
    recipients: list[str]
    password_set: bool


# ============ Endpoints ============


@router.get("", response_model=ConfigResponse)
def get_config(session: dict = Depends(get_current_session)) -> ConfigResponse:
    """
    Get the current configuration, with every secret redacted.

    Args:
        session: Authenticated session, injected by the dependency.

    Returns:
        The redacted configuration, its path and whether it can be written.
    """
    config = load_config()
    return ConfigResponse(
        config=public_config(config),
        path=str(config.path),
        writable=_is_writable(config.path),
    )


@router.put("", response_model=ConfigUpdateResponse)
def update_config(
    body: ConfigUpdateRequest,
    request: Request,
    session: dict = Depends(require_elevated),
) -> ConfigUpdateResponse:
    """
    Replace the full configuration.

    Placeholders sent back for secrets keep the stored value, and settings the
    code no longer honours are dropped. ``Config.replace`` refuses a value that
    ``wasm config set`` or the typed endpoints below would also refuse - an
    unsupported web server, a relative apps directory - so a whole-config body
    is not a back door around either.

    Args:
        body: Body carrying the new configuration.
        request: The incoming request, for the audit record.
        session: Authenticated session, injected by the dependency.

    Returns:
        Confirmation message and the path that was written.

    Raises:
        HTTPException: If the configuration cannot be written.
    """
    config = load_config()
    before = config.to_dict()
    smtp_before = _smtp_destination(config)
    config.replace(body.config)
    _refuse_smtp_password_move(smtp_before, config)
    path = persist(config)

    after = config.to_dict()
    changed = sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k))
    _audit_write(request, session, changed=changed)

    return ConfigUpdateResponse(message="Configuration updated", path=str(path))


@router.patch("", response_model=ConfigPatchResponse)
def patch_config(
    body: ConfigPatchRequest,
    request: Request,
    session: dict = Depends(require_elevated),
) -> ConfigPatchResponse:
    """
    Update a single configuration value addressed by a dotted path.

    The stored value is echoed back redacted, so a secret does not travel twice.

    A string value is coerced against the key's schema exactly as ``wasm
    config set`` coerces argv: a key with a default is parsed as that
    default's type, and a key with none is parsed as a JSON scalar or list,
    falling back to a plain string. Without it, a caller that posts
    form-shaped data - everything a string, such as a boolean field posted as
    "false" - would store the literal string rather than the value it looks
    like. A value that already arrived as JSON's own boolean, number or array
    is left exactly as it was decoded.

    Args:
        body: Body carrying the dotted path and the new value.
        request: The incoming request, for the audit record.
        session: Authenticated session, injected by the dependency.

    Returns:
        Confirmation message, the path written and the resulting value.

    Raises:
        HTTPException: If the configuration cannot be written.
    """
    config = load_config()
    smtp_before = _smtp_destination(config)
    value = body.value
    if isinstance(value, str):
        value = coerce_config_value(config.get(body.path, NO_DEFAULT), value)
    config.set(body.path, value)
    _refuse_smtp_password_move(smtp_before, config)
    path = persist(config)
    _audit_write(request, session, changed=[body.path.split(".", 1)[0]])

    leaf = body.path.rsplit(".", 1)[-1]
    return ConfigPatchResponse(
        message=f"Configuration '{body.path}' updated",
        path=str(path),
        value=redact_secrets({leaf: value})[leaf],
    )


@router.get("/apps-directory", response_model=AppsDirectoryResponse)
def get_apps_directory(session: dict = Depends(get_current_session)) -> AppsDirectoryResponse:
    """
    Get the applications directory configuration.

    Args:
        session: Authenticated session, injected by the dependency.

    Returns:
        The configured applications directory.
    """
    config = load_config()
    return AppsDirectoryResponse(
        apps_directory=config.get("apps_directory", str(DEFAULT_CONFIG["apps_directory"]))
    )


@router.put("/apps-directory", response_model=AppsDirectoryUpdateResponse)
def update_apps_directory(
    body: AppsDirConfig,
    request: Request,
    session: dict = Depends(require_elevated),
) -> AppsDirectoryUpdateResponse:
    """
    Update the applications directory.

    Args:
        body: Body carrying the new directory.
        request: The incoming request, for the audit record.
        session: Authenticated session, injected by the dependency.

    Returns:
        Confirmation message and the stored directory.

    Raises:
        HTTPException: If the configuration cannot be written.
        ConfigError: 400, through the error boundary, when the directory is
            not an absolute path.
    """
    config = load_config()
    config.set("apps_directory", body.apps_directory)
    persist(config)
    _audit_write(request, session, changed=["apps_directory"])
    return AppsDirectoryUpdateResponse(
        message="Applications directory updated",
        apps_directory=body.apps_directory,
    )


@router.get("/webserver", response_model=WebserverResponse)
def get_webserver(session: dict = Depends(get_current_session)) -> WebserverResponse:
    """
    Get the web server configuration.

    Args:
        session: Authenticated session, injected by the dependency.

    Returns:
        The configured web server.
    """
    config = load_config()
    return WebserverResponse(webserver=config.get("webserver", DEFAULT_CONFIG["webserver"]))


@router.put("/webserver", response_model=WebserverUpdateResponse)
def update_webserver(
    body: WebserverConfig,
    request: Request,
    session: dict = Depends(require_elevated),
) -> WebserverUpdateResponse:
    """
    Update the web server setting.

    Args:
        body: Body carrying the web server name.
        request: The incoming request, for the audit record.
        session: Authenticated session, injected by the dependency.

    Returns:
        Confirmation message and the stored web server.

    Raises:
        HTTPException: 400 for an unsupported web server, or a write failure.
    """
    if body.webserver not in SUPPORTED_WEBSERVERS:
        raise HTTPException(status_code=400, detail="Webserver must be 'nginx' or 'apache'")

    config = load_config()
    config.set("webserver", body.webserver)
    persist(config)
    _audit_write(request, session, changed=["webserver"])
    return WebserverUpdateResponse(message="Web server updated", webserver=body.webserver)


@router.get("/backup", response_model=BackupSettingsResponse)
def get_backup_config(session: dict = Depends(get_current_session)) -> BackupSettingsResponse:
    """
    Get backup configuration.

    Args:
        session: Authenticated session, injected by the dependency.

    Returns:
        The backup directory, as every backup reader resolves it, and the
        retention limit.

    Raises:
        ConfigError: When the stored directory is a relative path.
    """
    config = load_config()
    return BackupSettingsResponse(
        directory=str(config.backup_directory),
        max_per_app=config.get("backup.max_per_app", 10),
    )


@router.put("/backup", response_model=MessageResponse)
def update_backup_config(
    body: BackupConfig,
    request: Request,
    session: dict = Depends(require_elevated),
) -> MessageResponse:
    """
    Update backup configuration.

    Args:
        body: Body carrying the backup directory and retention limit.
        request: The incoming request, for the audit record.
        session: Authenticated session, injected by the dependency.

    Returns:
        Confirmation message.

    Raises:
        ConfigError: When the directory is a relative path; an empty one is
            stored as the default. The rule is Config.set's, the same one
            'wasm config set backup.directory' meets.
        HTTPException: If the configuration cannot be written.
    """
    config = load_config()
    config.set("backup.directory", body.directory)
    config.set("backup.max_per_app", body.max_per_app)
    persist(config)
    _audit_write(request, session, changed=["backup"])
    return MessageResponse(message="Backup configuration updated")


@router.get("/ssl", response_model=SSLSettingsResponse)
def get_ssl_config(session: dict = Depends(get_current_session)) -> SSLSettingsResponse:
    """
    Get SSL configuration.

    Args:
        session: Authenticated session, injected by the dependency.

    Returns:
        The SSL block, redacted.
    """
    config = load_config()
    ssl_defaults: dict[str, Any] = DEFAULT_CONFIG["ssl"]
    return SSLSettingsResponse(
        enabled=config.get("ssl.enabled", ssl_defaults["enabled"]),
        provider=config.get("ssl.provider", ssl_defaults["provider"]),
        email=config.get("ssl.email", ssl_defaults["email"]),
    )


@router.put("/ssl", response_model=MessageResponse)
def update_ssl_config(
    body: SSLConfig,
    request: Request,
    session: dict = Depends(require_elevated),
) -> MessageResponse:
    """
    Update SSL configuration.

    Args:
        body: Body carrying the SSL settings.
        request: The incoming request, for the audit record.
        session: Authenticated session, injected by the dependency.

    Returns:
        Confirmation message.

    Raises:
        HTTPException: If the configuration cannot be written.
    """
    config = load_config()
    config.set("ssl.enabled", body.enabled)
    config.set("ssl.provider", body.provider)
    config.set("ssl.email", body.email)
    persist(config)
    _audit_write(request, session, changed=["ssl"])
    return MessageResponse(message="SSL configuration updated")


@router.get("/web", response_model=WebSettingsResponse)
def get_web_config(session: dict = Depends(get_current_session)) -> WebSettingsResponse:
    """
    Get web interface configuration.

    Args:
        session: Authenticated session, injected by the dependency.

    Returns:
        The bind address, port and session timeout.
    """
    config = load_config()
    web_defaults: dict[str, Any] = DEFAULT_CONFIG["web"]
    return WebSettingsResponse(
        host=config.get("web.host", web_defaults["host"]),
        port=config.get("web.port", web_defaults["port"]),
        session_timeout=config.get("web.session_timeout", 3600),
    )


@router.put("/web", response_model=MessageResponse)
def update_web_config(
    body: WebConfig,
    request: Request,
    session: dict = Depends(require_elevated),
) -> MessageResponse:
    """
    Update web interface configuration.

    Only the three exposed keys are touched; the security settings that live in
    the same block are left alone.

    Args:
        body: Body carrying the web interface settings.
        request: The incoming request, for the audit record.
        session: Authenticated session, injected by the dependency.

    Returns:
        Confirmation message.

    Raises:
        HTTPException: If the configuration cannot be written.
    """
    config = load_config()
    config.set("web.host", body.host)
    config.set("web.port", body.port)
    config.set("web.session_timeout", body.session_timeout)
    persist(config)
    _audit_write(request, session, changed=["web"])
    return MessageResponse(message="Web configuration updated (restart required)")


@router.get("/smtp", response_model=SMTPSettingsResponse)
def get_smtp_config(session: dict = Depends(get_current_session)) -> SMTPSettingsResponse:
    """
    Get the monitor's SMTP settings.

    Args:
        session: Authenticated session, injected by the dependency.

    Returns:
        The configured server, transport, account and recipients, and whether
        a password is stored - never the password itself.
    """
    config = load_config()
    smtp_defaults: dict[str, Any] = DEFAULT_CONFIG["monitor"]["smtp"]
    return SMTPSettingsResponse(
        host=config.get("monitor.smtp.host", smtp_defaults["host"]),
        port=config.get("monitor.smtp.port", smtp_defaults["port"]),
        use_ssl=config.get("monitor.smtp.use_ssl", smtp_defaults["use_ssl"]),
        use_tls=config.get("monitor.smtp.use_tls", smtp_defaults["use_tls"]),
        username=config.get("monitor.smtp.username", smtp_defaults["username"]),
        from_address=config.get("monitor.smtp.from_address", smtp_defaults["from_address"]),
        recipients=config.get("monitor.email_recipients", []),
        password_set=bool(config.get("monitor.smtp.password", "")),
    )


#: What decides where the SMTP password goes: a new host or port is a new
#: recipient of it, a new username a new account, and dropping or switching
#: TLS changes who on the path can read it.
_SMTP_DESTINATION_KEYS = ("host", "port", "username", "use_ssl", "use_tls")


def _smtp_destination(config: Config) -> tuple[tuple[Any, ...], str]:
    """
    Read where the stored SMTP password would be sent, and the password.

    Args:
        config: The configuration, as stored or as about to be written.

    Returns:
        The destination, normalised (host names compare case-insensitively,
        as DNS does), and the password.
    """
    defaults: dict[str, Any] = DEFAULT_CONFIG["monitor"]["smtp"]
    value = {
        key: config.get(f"monitor.smtp.{key}", defaults[key]) for key in _SMTP_DESTINATION_KEYS
    }
    port = value["port"]
    try:
        port = int(port)
    except (TypeError, ValueError):
        pass  # A hand-edited value is compared as it is written.
    destination = (
        str(value["host"] or "").strip().lower(),
        port,
        str(value["username"] or ""),
        bool(value["use_ssl"]),
        bool(value["use_tls"]),
    )
    return destination, str(config.get("monitor.smtp.password", "") or "")


def _refuse_smtp_password_move(before: tuple[tuple[Any, ...], str], config: Config) -> None:
    """
    Refuse a write that would send the stored SMTP password somewhere new.

    A secret the caller was never shown is kept on a write that does not name
    it - a blank field on ``PUT /api/config/smtp``, ``***`` echoed back to
    ``PUT /api/config``, a ``PATCH`` of another key - because the console
    never receives it. That only means "leave it alone" while it keeps
    authenticating the same account on the same server over the same
    transport. Otherwise a credential allowed to write the settings could
    point the password at a server of its own and send itself a test email.
    Every API write of the configuration passes here, after the change is
    applied in memory and before it is persisted.

    Args:
        before: :func:`_smtp_destination` of the configuration as stored.
        config: The configuration with the change applied, not yet written.

    Raises:
        HTTPException: 422 on ``password`` when a password is stored, the
            write keeps it, and the destination changed.
    """
    destination, password = _smtp_destination(config)
    old_destination, old_password = before
    if not old_password or password != old_password or destination == old_destination:
        return
    raise HTTPException(
        status_code=422,
        detail={
            "error": "validation_error",
            "detail": "Validation failed",
            "hint": None,
            "fields": {
                "password": (
                    "Enter the password again: the server, port, username or "
                    "transport changed, and the stored password is only kept "
                    "for the server it was entered for."
                )
            },
        },
    )


@router.put("/smtp", response_model=MessageResponse)
def update_smtp_config(
    body: SMTPConfig,
    request: Request,
    session: dict = Depends(require_elevated),
) -> MessageResponse:
    """
    Update the monitor's SMTP settings.

    Goes through :meth:`~wasm.core.config.Config.set`, so the same rule 'wasm
    config set monitor.smtp.*' enforces - a hostname for ``host``, a port in
    range, ``use_ssl`` and ``use_tls`` not both on, a valid address for
    ``from_address`` and every recipient - rejects a value here too, in the
    same words. An empty ``password`` is translated to the
    :data:`~wasm.core.config.REDACTED` placeholder before the write, which is
    what actually keeps the stored password, and is refused when the
    password would go somewhere else: see :class:`SMTPConfig`.

    Args:
        body: Body carrying the SMTP settings.
        request: The incoming request, for the audit record.
        session: Authenticated session, injected by the dependency.

    Returns:
        Confirmation message.

    Raises:
        ConfigError: 400, through the error boundary, for a host that is not
            a hostname, a port out of range, both ``use_ssl`` and ``use_tls``
            enabled, or an invalid ``from_address`` or recipient.
        HTTPException: 422 on ``password`` when it is blank, a password is
            stored, and the host, port, username or transport changed; or if
            the configuration cannot be written.
    """
    config = load_config()
    before = _smtp_destination(config)
    config.set(
        "monitor.smtp",
        {
            "host": body.host,
            "port": body.port,
            "use_ssl": body.use_ssl,
            "use_tls": body.use_tls,
            "username": body.username,
            "password": body.password if body.password else REDACTED,
            "from_address": body.from_address,
        },
    )
    config.set("monitor.email_recipients", body.recipients)
    _refuse_smtp_password_move(before, config)
    persist(config)
    _audit_write(request, session, changed=["monitor"])
    return MessageResponse(message="SMTP configuration updated")


@router.post("/reload", response_model=ConfigReloadResponse)
def reload_config(session: dict = Depends(get_current_session)) -> ConfigReloadResponse:
    """
    Reload configuration from disk.

    Args:
        session: Authenticated session, injected by the dependency.

    Returns:
        The redacted configuration and the path it came from.
    """
    config = load_config()
    return ConfigReloadResponse(
        message="Configuration reloaded",
        path=str(config.path),
        config=public_config(config),
    )


@router.get("/defaults", response_model=dict[str, Any])
def get_defaults(session: dict = Depends(get_current_session)) -> dict[str, Any]:
    """
    Get default configuration values.

    Args:
        session: Authenticated session, injected by the dependency.

    Returns:
        The shipped defaults, redacted like any other configuration payload.
    """
    defaults: dict[str, Any] = redact_secrets(DEFAULT_CONFIG)
    return defaults


class TelegramConfig(BaseModel):
    """
    Telegram bot settings for notifications.

    ``bot_token`` is write-only, the same rule :class:`SMTPConfig` follows for
    a password: it is never sent back by ``GET .../telegram`` (see
    :class:`TelegramSettingsResponse`), and an empty value on a write keeps
    whatever is already stored rather than clearing it.
    """

    bot_token: str = Field(
        "", description="Bot API token; leave blank to keep the one already stored"
    )
    chat_id: str = Field("", description="Destination chat: an integer id, or an @channel username")

    @field_validator("bot_token")
    @classmethod
    def _bot_token_has_the_bot_api_shape(cls, value: str) -> str:
        """
        Refuse a token that is not ``<digits>:<secret>``, from end to end.

        The token becomes part of the Bot API's request path, so the notifier
        refuses any other shape before sending; checking it here too turns
        that into an error at save time, next to the field, instead of a
        warning in the log at the next deploy. Empty is left alone: it means
        "keep the stored token".

        Args:
            value: The token as it arrived.

        Returns:
            The value unchanged.

        Raises:
            ValueError: When it has any other shape. The message never quotes
                the value. FastAPI answers this as a 422 on ``bot_token``.
        """
        if not value:
            return value
        from wasm.core.notifier import validate_telegram_bot_token

        return validate_telegram_bot_token(value)

    @field_validator("chat_id")
    @classmethod
    def _chat_id_telegram_would_accept(cls, value: str) -> str:
        """
        Refuse here what Telegram would refuse, with the likely mistake named.

        Empty is left alone: it means "not set yet", which is how a bot token
        can be saved before its chat id is known - :meth:`Notifier.list_telegram_chats`
        exists for finding that id, and needs only the token.

        Args:
            value: The chat id as it arrived.

        Returns:
            The value unchanged.

        Raises:
            ValueError: When it is neither an integer id nor an
                ``@channelname``. A positive id of 13 or more digits starting
                with ``100`` is named as what it almost certainly is: a
                supergroup or channel id missing its leading minus. FastAPI
                answers this as a 422 with the message as detail.
        """
        if not value:
            return value
        from wasm.validators.telegram import validate_telegram_chat_id

        return validate_telegram_chat_id(value)


class TelegramSettingsResponse(BaseModel):
    """The configured Telegram channel. ``bot_token`` is never sent back."""

    chat_id: str
    bot_token_set: bool


@router.get("/notifications/telegram", response_model=TelegramSettingsResponse)
def get_telegram_config(session: dict = Depends(get_current_session)) -> TelegramSettingsResponse:
    """
    Get the configured Telegram channel, without its bot token.

    Args:
        session: Authenticated session, injected by the dependency.

    Returns:
        The configured chat id and whether a bot token is stored.
    """
    config = load_config()
    return TelegramSettingsResponse(
        chat_id=config.get("notifications.channels.telegram.chat_id", ""),
        bot_token_set=bool(config.get("notifications.channels.telegram.bot_token", "")),
    )


@router.put("/notifications/telegram", response_model=MessageResponse)
def update_telegram_config(
    body: TelegramConfig,
    request: Request,
    session: dict = Depends(require_elevated),
) -> MessageResponse:
    """
    Update the Telegram channel's bot token and chat id.

    The chat id is validated against the shape Telegram's Bot API accepts
    before it is ever written - see :class:`TelegramConfig` - which is what
    turns a missing minus sign into an error naming the fix at save time
    instead of a "chat not found" the next time a deploy tries to notify.

    Args:
        body: Body carrying the bot token and chat id.
        request: The incoming request, for the audit record.
        session: Authenticated session, injected by the dependency.

    Returns:
        Confirmation message.

    Raises:
        HTTPException: If the configuration cannot be written.
    """
    config = load_config()
    bot_token = body.bot_token or config.get("notifications.channels.telegram.bot_token", "")
    config.set(
        "notifications.channels.telegram",
        {"bot_token": bot_token, "chat_id": body.chat_id},
    )
    persist(config)
    _audit_write(request, session, changed=["notifications"])
    return MessageResponse(message="Telegram configuration updated")


class NotificationTestResult(BaseModel):
    """Outcome of sending a test message through one notification channel."""

    ok: bool
    detail: str


def _build_notifier() -> Notifier:
    """
    Build the notifier over the configuration as it stands on disk.

    The same one-line construction :mod:`wasm.web.views.settings_editor` uses
    for its own notifier - ``Notifier(config_api.load_config())``, reading
    this module's :func:`load_config` - because the meaningful logic (the SSRF
    guard, the redirect re-checking, never echoing a remote body) lives once
    in :class:`~wasm.core.notifier.Notifier`, and both callers are thin wiring
    over it. Module level so a test can stand in a notifier whose opener never
    opens a socket.

    Returns:
        A notifier reading the freshly reloaded configuration.
    """
    from wasm.core.notifier import Notifier

    return Notifier(load_config())


@router.post("/notifications/{channel}/test", response_model=NotificationTestResult)
def test_notification_channel(
    channel: str, session: dict = Depends(get_current_session)
) -> NotificationTestResult:
    """
    Send a test message through one notification channel.

    Ignores the master switch and the per-event filters on purpose - the
    button exists to try a channel before notifications are turned on - and
    never echoes the remote server's response body back to the client:
    :meth:`~wasm.core.notifier.Notifier.test_channel` already refuses a
    private destination and scrubs configured secrets out of any failure it
    reports.

    Args:
        channel: Channel name, one of the notifier's channels.
        session: Authenticated session, injected by the dependency.

    Returns:
        ``ok=True`` with a confirmation, or ``ok=False`` with the failure in
        the receiving server's own words.
    """
    error = _build_notifier().test_channel(channel)
    if error is None:
        return NotificationTestResult(ok=True, detail=f"Test message sent through {channel}.")
    return NotificationTestResult(ok=False, detail=error)


class TelegramChatOut(BaseModel):
    """One chat the configured Telegram bot has seen, as the panel shows it."""

    id: int
    type: str
    title: str | None = None
    username: str | None = None


class TelegramChatsResult(BaseModel):
    """Every chat :meth:`~wasm.core.notifier.Notifier.list_telegram_chats` found."""

    chats: list[TelegramChatOut]


@router.post("/notifications/telegram/chats", response_model=TelegramChatsResult)
def list_telegram_chats(session: dict = Depends(get_current_session)) -> TelegramChatsResult:
    """
    List the chats the configured Telegram bot has seen.

    Finding a chat id today means opening the Bot API's ``getUpdates`` URL by
    hand and reading raw JSON, or asking an assistant to do it. This is that
    lookup, using the bot token already saved, over the notifier's own HTTP
    path - the same SSRF guard and timeout as every other channel.

    Plain ``get_current_session``, not :func:`require_elevated`: this only
    reads what Telegram has queued for the bot, the same reasoning
    :func:`test_notification_channel` already applies to sending a message -
    neither one changes anything WASM manages.

    Args:
        session: Authenticated session, injected by the dependency.

    Returns:
        Every chat found.

    Raises:
        HTTPException: 400 when no bot token is configured, the token does
            not have the Bot API's shape, or the Bot API could not be reached
            or answered with an error - in its own words.
    """
    try:
        chats = _build_notifier().list_telegram_chats()
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return TelegramChatsResult(
        chats=[
            TelegramChatOut(id=chat.id, type=chat.type, title=chat.title, username=chat.username)
            for chat in chats
        ]
    )
