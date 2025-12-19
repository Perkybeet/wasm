"""
Configuration API endpoints for WASM Web Interface.

Provides endpoints for viewing and modifying WASM configuration.
"""

from pathlib import Path
from typing import Optional, Any, Dict
from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel, Field
import yaml

from wasm.web.auth import require_auth
from wasm.core.config import Config


router = APIRouter()

# Configuration file paths
CONFIG_PATHS = [
    Path("/etc/wasm/config.yaml"),
    Path.home() / ".config" / "wasm" / "config.yaml",
    Path.home() / ".wasm" / "config.yaml",
]


def get_config_path() -> Optional[Path]:
    """Get the active configuration file path."""
    for path in CONFIG_PATHS:
        if path.exists():
            return path
    return None


def load_config_file() -> tuple[dict, Path]:
    """Load configuration from file."""
    config_path = get_config_path()
    
    if not config_path:
        # Return default config if no file exists
        return {
            "apps_directory": "/var/www/apps",
            "webserver": "nginx",
            "service_user": "www-data",
            "backup": {
                "directory": "/var/backups/wasm",
                "max_per_app": 10,
            },
            "ssl": {
                "enabled": True,
                "provider": "certbot",
                "email": "",
            },
            "web": {
                "host": "127.0.0.1",
                "port": 8080,
                "session_timeout": 3600,
            },
        }, CONFIG_PATHS[0]
    
    with open(config_path) as f:
        return yaml.safe_load(f) or {}, config_path


def save_config_file(config: dict, path: Path):
    """Save configuration to file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, 'w') as f:
        yaml.dump(config, f, default_flow_style=False, sort_keys=False)


# ============ Request/Response Models ============

class ConfigResponse(BaseModel):
    """Response containing full configuration."""
    config: dict
    path: str
    writable: bool


class ConfigUpdateRequest(BaseModel):
    """Request to update configuration."""
    config: dict = Field(..., description="Full configuration object")


class ConfigPatchRequest(BaseModel):
    """Request to patch specific configuration values."""
    path: str = Field(..., description="Dot-separated path to config key (e.g., 'backup.max_per_app')")
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
async def get_config(_: dict = Depends(require_auth)):
    """Get the current configuration."""
    config, path = load_config_file()
    
    # Check if path is writable
    try:
        writable = path.parent.exists() and (not path.exists() or bool(path.stat().st_mode & 0o200))
    except OSError:
        writable = False
    
    return {
        "config": config,
        "path": str(path),
        "writable": writable,
    }


@router.put("")
async def update_config(request: ConfigUpdateRequest, _: dict = Depends(require_auth)):
    """Update the full configuration."""
    _, path = load_config_file()
    
    try:
        save_config_file(request.config, path)
        return {"message": "Configuration updated", "path": str(path)}
    except PermissionError:
        raise HTTPException(
            status_code=403,
            detail=f"Permission denied writing to {path}"
        )
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to save configuration: {e}"
        )


@router.patch("")
async def patch_config(request: ConfigPatchRequest, _: dict = Depends(require_auth)):
    """Update a specific configuration value."""
    config, path = load_config_file()
    
    # Navigate to the nested key
    keys = request.path.split('.')
    current = config
    
    for key in keys[:-1]:
        if key not in current:
            current[key] = {}
        current = current[key]
    
    # Set the value
    current[keys[-1]] = request.value
    
    try:
        save_config_file(config, path)
        return {
            "message": f"Configuration '{request.path}' updated",
            "path": str(path),
            "value": request.value,
        }
    except PermissionError:
        raise HTTPException(
            status_code=403,
            detail=f"Permission denied writing to {path}"
        )


@router.get("/apps-directory")
async def get_apps_directory(_: dict = Depends(require_auth)):
    """Get the applications directory configuration."""
    config, _ = load_config_file()
    return {
        "apps_directory": config.get("apps_directory", "/var/www/apps"),
    }


@router.put("/apps-directory")
async def update_apps_directory(request: AppsDirConfig, _: dict = Depends(require_auth)):
    """Update the applications directory."""
    config, path = load_config_file()
    config["apps_directory"] = request.apps_directory
    
    try:
        save_config_file(config, path)
        return {"message": "Applications directory updated", "apps_directory": request.apps_directory}
    except PermissionError:
        raise HTTPException(status_code=403, detail="Permission denied")


@router.get("/webserver")
async def get_webserver(_: dict = Depends(require_auth)):
    """Get the web server configuration."""
    config, _ = load_config_file()
    return {
        "webserver": config.get("webserver", "nginx"),
    }


@router.put("/webserver")
async def update_webserver(request: WebserverConfig, _: dict = Depends(require_auth)):
    """Update the web server setting."""
    if request.webserver not in ["nginx", "apache"]:
        raise HTTPException(status_code=400, detail="Webserver must be 'nginx' or 'apache'")
    
    config, path = load_config_file()
    config["webserver"] = request.webserver
    
    try:
        save_config_file(config, path)
        return {"message": "Web server updated", "webserver": request.webserver}
    except PermissionError:
        raise HTTPException(status_code=403, detail="Permission denied")


@router.get("/backup")
async def get_backup_config(_: dict = Depends(require_auth)):
    """Get backup configuration."""
    config, _ = load_config_file()
    backup = config.get("backup", {})
    return {
        "directory": backup.get("directory", "/var/backups/wasm"),
        "max_per_app": backup.get("max_per_app", 10),
    }


@router.put("/backup")
async def update_backup_config(request: BackupConfig, _: dict = Depends(require_auth)):
    """Update backup configuration."""
    config, path = load_config_file()
    config["backup"] = {
        "directory": request.directory,
        "max_per_app": request.max_per_app,
    }
    
    try:
        save_config_file(config, path)
        return {"message": "Backup configuration updated"}
    except PermissionError:
        raise HTTPException(status_code=403, detail="Permission denied")


@router.get("/ssl")
async def get_ssl_config(_: dict = Depends(require_auth)):
    """Get SSL configuration."""
    config, _ = load_config_file()
    ssl = config.get("ssl", {})
    return {
        "enabled": ssl.get("enabled", True),
        "provider": ssl.get("provider", "certbot"),
        "email": ssl.get("email", ""),
    }


@router.put("/ssl")
async def update_ssl_config(request: SSLConfig, _: dict = Depends(require_auth)):
    """Update SSL configuration."""
    config, path = load_config_file()
    config["ssl"] = {
        "enabled": request.enabled,
        "provider": request.provider,
        "email": request.email,
    }
    
    try:
        save_config_file(config, path)
        return {"message": "SSL configuration updated"}
    except PermissionError:
        raise HTTPException(status_code=403, detail="Permission denied")


@router.get("/web")
async def get_web_config(_: dict = Depends(require_auth)):
    """Get web interface configuration."""
    config, _ = load_config_file()
    web = config.get("web", {})
    return {
        "host": web.get("host", "127.0.0.1"),
        "port": web.get("port", 8080),
        "session_timeout": web.get("session_timeout", 3600),
    }


@router.put("/web")
async def update_web_config(request: WebConfig, _: dict = Depends(require_auth)):
    """Update web interface configuration."""
    config, path = load_config_file()
    config["web"] = {
        "host": request.host,
        "port": request.port,
        "session_timeout": request.session_timeout,
    }
    
    try:
        save_config_file(config, path)
        return {"message": "Web configuration updated (restart required)"}
    except PermissionError:
        raise HTTPException(status_code=403, detail="Permission denied")


@router.post("/reload")
async def reload_config(_: dict = Depends(require_auth)):
    """Reload configuration from disk."""
    try:
        config, path = load_config_file()
        return {
            "message": "Configuration reloaded",
            "path": str(path),
            "config": config,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to reload: {e}")


@router.get("/defaults")
async def get_defaults(_: dict = Depends(require_auth)):
    """Get default configuration values."""
    return {
        "apps_directory": "/var/www/apps",
        "webserver": "nginx",
        "service_user": "www-data",
        "backup": {
            "directory": "/var/backups/wasm",
            "max_per_app": 10,
        },
        "ssl": {
            "enabled": True,
            "provider": "certbot",
            "email": "",
        },
        "web": {
            "host": "127.0.0.1",
            "port": 8080,
            "session_timeout": 3600,
        },
    }
