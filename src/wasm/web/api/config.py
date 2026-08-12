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
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from wasm.core.config import DEFAULT_CONFIG, Config, redact_secrets
from wasm.core.exceptions import WASMError
from wasm.web.api.auth import get_current_session

router = APIRouter()

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
        HTTPException: 403 when the path is not writable, 500 for anything else.
    """
    try:
        return config.write()
    except PermissionError as exc:
        raise HTTPException(
            status_code=403, detail=f"Permission denied writing to {config.path}"
        ) from exc
    except (OSError, WASMError) as exc:
        raise HTTPException(status_code=500, detail=f"Failed to save configuration: {exc}") from exc


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

    directory: str = Field("/var/backups/wasm", description="Backup storage directory")
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


@router.put("")
def update_config(
    request: ConfigUpdateRequest, session: dict = Depends(get_current_session)
) -> dict[str, str]:
    """
    Replace the full configuration.

    Placeholders sent back for secrets keep the stored value, and settings the
    code no longer honours are dropped.

    Args:
        request: Body carrying the new configuration.
        session: Authenticated session, injected by the dependency.

    Returns:
        Confirmation message and the path that was written.

    Raises:
        HTTPException: If the configuration cannot be written.
    """
    config = load_config()
    config.replace(request.config)
    path = persist(config)
    return {"message": "Configuration updated", "path": str(path)}


@router.patch("")
def patch_config(
    request: ConfigPatchRequest, session: dict = Depends(get_current_session)
) -> dict[str, Any]:
    """
    Update a single configuration value addressed by a dotted path.

    The stored value is echoed back redacted, so a secret does not travel twice.

    Args:
        request: Body carrying the dotted path and the new value.
        session: Authenticated session, injected by the dependency.

    Returns:
        Confirmation message, the path written and the resulting value.

    Raises:
        HTTPException: If the configuration cannot be written.
    """
    config = load_config()
    config.set(request.path, request.value)
    path = persist(config)

    leaf = request.path.rsplit(".", 1)[-1]
    return {
        "message": f"Configuration '{request.path}' updated",
        "path": str(path),
        "value": redact_secrets({leaf: request.value})[leaf],
    }


@router.get("/apps-directory")
def get_apps_directory(session: dict = Depends(get_current_session)) -> dict[str, Any]:
    """
    Get the applications directory configuration.

    Args:
        session: Authenticated session, injected by the dependency.

    Returns:
        The configured applications directory.
    """
    config = load_config()
    return {"apps_directory": config.get("apps_directory", str(DEFAULT_CONFIG["apps_directory"]))}


@router.put("/apps-directory")
def update_apps_directory(
    request: AppsDirConfig, session: dict = Depends(get_current_session)
) -> dict[str, str]:
    """
    Update the applications directory.

    Args:
        request: Body carrying the new directory.
        session: Authenticated session, injected by the dependency.

    Returns:
        Confirmation message and the stored directory.

    Raises:
        HTTPException: If the configuration cannot be written.
    """
    config = load_config()
    config.set("apps_directory", request.apps_directory)
    persist(config)
    return {
        "message": "Applications directory updated",
        "apps_directory": request.apps_directory,
    }


@router.get("/webserver")
def get_webserver(session: dict = Depends(get_current_session)) -> dict[str, Any]:
    """
    Get the web server configuration.

    Args:
        session: Authenticated session, injected by the dependency.

    Returns:
        The configured web server.
    """
    config = load_config()
    return {"webserver": config.get("webserver", DEFAULT_CONFIG["webserver"])}


@router.put("/webserver")
def update_webserver(
    request: WebserverConfig, session: dict = Depends(get_current_session)
) -> dict[str, str]:
    """
    Update the web server setting.

    Args:
        request: Body carrying the web server name.
        session: Authenticated session, injected by the dependency.

    Returns:
        Confirmation message and the stored web server.

    Raises:
        HTTPException: 400 for an unsupported web server, or a write failure.
    """
    if request.webserver not in SUPPORTED_WEBSERVERS:
        raise HTTPException(status_code=400, detail="Webserver must be 'nginx' or 'apache'")

    config = load_config()
    config.set("webserver", request.webserver)
    persist(config)
    return {"message": "Web server updated", "webserver": request.webserver}


@router.get("/backup")
def get_backup_config(session: dict = Depends(get_current_session)) -> dict[str, Any]:
    """
    Get backup configuration.

    Args:
        session: Authenticated session, injected by the dependency.

    Returns:
        The backup directory and the retention limit.
    """
    config = load_config()
    return {
        "directory": config.get("backup.directory", "/var/backups/wasm"),
        "max_per_app": config.get("backup.max_per_app", 10),
    }


@router.put("/backup")
def update_backup_config(
    request: BackupConfig, session: dict = Depends(get_current_session)
) -> dict[str, str]:
    """
    Update backup configuration.

    Args:
        request: Body carrying the backup directory and retention limit.
        session: Authenticated session, injected by the dependency.

    Returns:
        Confirmation message.

    Raises:
        HTTPException: If the configuration cannot be written.
    """
    config = load_config()
    config.set("backup.directory", request.directory)
    config.set("backup.max_per_app", request.max_per_app)
    persist(config)
    return {"message": "Backup configuration updated"}


@router.get("/ssl")
def get_ssl_config(session: dict = Depends(get_current_session)) -> dict[str, Any]:
    """
    Get SSL configuration.

    Args:
        session: Authenticated session, injected by the dependency.

    Returns:
        The SSL block, redacted.
    """
    config = load_config()
    ssl_defaults: dict[str, Any] = DEFAULT_CONFIG["ssl"]
    return {
        "enabled": config.get("ssl.enabled", ssl_defaults["enabled"]),
        "provider": config.get("ssl.provider", ssl_defaults["provider"]),
        "email": config.get("ssl.email", ssl_defaults["email"]),
    }


@router.put("/ssl")
def update_ssl_config(
    request: SSLConfig, session: dict = Depends(get_current_session)
) -> dict[str, str]:
    """
    Update SSL configuration.

    Args:
        request: Body carrying the SSL settings.
        session: Authenticated session, injected by the dependency.

    Returns:
        Confirmation message.

    Raises:
        HTTPException: If the configuration cannot be written.
    """
    config = load_config()
    config.set("ssl.enabled", request.enabled)
    config.set("ssl.provider", request.provider)
    config.set("ssl.email", request.email)
    persist(config)
    return {"message": "SSL configuration updated"}


@router.get("/web")
def get_web_config(session: dict = Depends(get_current_session)) -> dict[str, Any]:
    """
    Get web interface configuration.

    Args:
        session: Authenticated session, injected by the dependency.

    Returns:
        The bind address, port and session timeout.
    """
    config = load_config()
    web_defaults: dict[str, Any] = DEFAULT_CONFIG["web"]
    return {
        "host": config.get("web.host", web_defaults["host"]),
        "port": config.get("web.port", web_defaults["port"]),
        "session_timeout": config.get("web.session_timeout", 3600),
    }


@router.put("/web")
def update_web_config(
    request: WebConfig, session: dict = Depends(get_current_session)
) -> dict[str, str]:
    """
    Update web interface configuration.

    Only the three exposed keys are touched; the security settings that live in
    the same block are left alone.

    Args:
        request: Body carrying the web interface settings.
        session: Authenticated session, injected by the dependency.

    Returns:
        Confirmation message.

    Raises:
        HTTPException: If the configuration cannot be written.
    """
    config = load_config()
    config.set("web.host", request.host)
    config.set("web.port", request.port)
    config.set("web.session_timeout", request.session_timeout)
    persist(config)
    return {"message": "Web configuration updated (restart required)"}


@router.post("/reload")
def reload_config(session: dict = Depends(get_current_session)) -> dict[str, Any]:
    """
    Reload configuration from disk.

    Args:
        session: Authenticated session, injected by the dependency.

    Returns:
        The redacted configuration and the path it came from.
    """
    config = load_config()
    return {
        "message": "Configuration reloaded",
        "path": str(config.path),
        "config": public_config(config),
    }


@router.get("/defaults")
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
