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
    Config,
    coerce_config_value,
    redact_secrets,
)
from wasm.web.api.auth import get_current_session
from wasm.web.api.deps import WASMErrorRoute, require_elevated
from wasm.web.auth import actor_label, get_audit_logger, get_client_ip

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
    config.replace(body.config)
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
    value = body.value
    if isinstance(value, str):
        value = coerce_config_value(config.get(body.path, NO_DEFAULT), value)
    config.set(body.path, value)
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
