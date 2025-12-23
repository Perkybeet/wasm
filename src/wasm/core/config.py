# Copyright (c) 2024-2025 Yago López Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
Global configuration management for WASM.
"""

import os
from pathlib import Path
from typing import Any, Dict, Optional

import yaml


# Default paths
DEFAULT_CONFIG_PATH = Path("/etc/wasm/config.yaml")
DEFAULT_APPS_DIR = Path("/var/www/apps")
DEFAULT_LOG_DIR = Path("/var/log/wasm")

# Nginx paths
NGINX_SITES_AVAILABLE = Path("/etc/nginx/sites-available")
NGINX_SITES_ENABLED = Path("/etc/nginx/sites-enabled")

# Apache paths
APACHE_SITES_AVAILABLE = Path("/etc/apache2/sites-available")
APACHE_SITES_ENABLED = Path("/etc/apache2/sites-enabled")

# Systemd path
SYSTEMD_DIR = Path("/etc/systemd/system")

# Default configuration values
DEFAULT_CONFIG: Dict[str, Any] = {
    "apps_directory": str(DEFAULT_APPS_DIR),
    "webserver": "nginx",
    "service_user": "www-data",
    "service_group": "www-data",
    "ssl": {
        "enabled": True,
        "provider": "certbot",
        "email": "",
    },
    "logging": {
        "level": "info",
        "file": str(DEFAULT_LOG_DIR / "wasm.log"),
    },
    "nodejs": {
        "default_version": "20",
        "use_nvm": False,
        "package_managers": ["npm"],  # Available: npm, pnpm, yarn, bun
    },
    "python": {
        "default_version": "3.11",
        "use_venv": True,
    },
    "monitor": {
        "enabled": False,
        "scan_interval": 3600,  # 1 hour in seconds
        "cpu_threshold": 80.0,
        "memory_threshold": 80.0,
        "auto_terminate": True,
        "terminate_malicious_only": True,
        "use_ai": True,
        "dry_run": False,
        "log_file": str(DEFAULT_LOG_DIR / "monitor.log"),
        "openai": {
            "api_key": "",
            "model": "gpt-4o-mini",
        },
        "smtp": {
            "host": "",
            "port": 465,
            "username": "",
            "password": "",
            "use_ssl": True,
            "use_tls": False,
            "from_address": "",
        },
        "email_recipients": [],
    },
    "web": {
        "enabled": False,
        "host": "127.0.0.1",
        "port": 8080,
        "rate_limit_enabled": True,
        "rate_limit_requests": 100,
        "rate_limit_window": 60,
        "max_failed_attempts": 5,
        "lockout_duration": 300,
        "token_expiration_hours": 24,
        "ip_whitelist": [],
    },
    "databases": {
        "backup_dir": "/var/backups/wasm/databases",
        "default_encoding": {
            "mysql": "utf8mb4",
            "postgresql": "UTF8",
        },
        "credentials": {
            "mysql": {
                "user": "root",
                "password": "",
            },
            "postgresql": {
                "user": "postgres",
                "password": "",
            },
            "redis": {
                "password": "",
            },
            "mongodb": {
                "user": "",
                "password": "",
            },
        },
        "auto_start": True,  # Start engine on install
        "auto_enable": True,  # Enable on boot on install
    },
}


class Config:
    """
    Configuration manager for WASM.
    
    Handles loading, saving, and accessing configuration values from
    the global config file and environment variables.
    """
    
    _instance: Optional["Config"] = None
    _config: Dict[str, Any] = {}
    
    def __new__(cls) -> "Config":
        """Singleton pattern to ensure single config instance."""
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._load_config()
        return cls._instance
    
    def _load_config(self) -> None:
        """Load configuration from file and merge with defaults."""
        self._config = DEFAULT_CONFIG.copy()
        
        if DEFAULT_CONFIG_PATH.exists():
            try:
                with open(DEFAULT_CONFIG_PATH, "r") as f:
                    file_config = yaml.safe_load(f) or {}
                self._config = self._deep_merge(self._config, file_config)
            except Exception:
                pass  # Use defaults if config file is invalid
        
        # Override with environment variables
        self._load_env_overrides()
    
    def _load_env_overrides(self) -> None:
        """Load configuration overrides from environment variables."""
        env_mappings = {
            "WASM_APPS_DIR": "apps_directory",
            "WASM_WEBSERVER": "webserver",
            "WASM_SERVICE_USER": "service_user",
            "WASM_SSL_EMAIL": ("ssl", "email"),
        }
        
        for env_var, config_key in env_mappings.items():
            value = os.environ.get(env_var)
            if value:
                if isinstance(config_key, tuple):
                    self._config[config_key[0]][config_key[1]] = value
                else:
                    self._config[config_key] = value
    
    def _deep_merge(self, base: Dict, override: Dict) -> Dict:
        """Deep merge two dictionaries."""
        result = base.copy()
        for key, value in override.items():
            if key in result and isinstance(result[key], dict) and isinstance(value, dict):
                result[key] = self._deep_merge(result[key], value)
            else:
                result[key] = value
        return result
    
    def get(self, key: str, default: Any = None) -> Any:
        """
        Get a configuration value.
        
        Args:
            key: Configuration key (supports dot notation for nested values).
            default: Default value if key is not found.
            
        Returns:
            Configuration value or default.
        """
        keys = key.split(".")
        value = self._config
        
        for k in keys:
            if isinstance(value, dict) and k in value:
                value = value[k]
            else:
                return default
        
        return value
    
    def set(self, key: str, value: Any) -> None:
        """
        Set a configuration value.
        
        Args:
            key: Configuration key (supports dot notation).
            value: Value to set.
        """
        keys = key.split(".")
        config = self._config
        
        for k in keys[:-1]:
            if k not in config:
                config[k] = {}
            config = config[k]
        
        config[keys[-1]] = value
    
    @property
    def apps_directory(self) -> Path:
        """Get the applications directory path."""
        return Path(self.get("apps_directory", str(DEFAULT_APPS_DIR)))
    
    @property
    def webserver(self) -> str:
        """Get the default web server."""
        return self.get("webserver", "nginx")
    
    def reload(self) -> None:
        """
        Reload configuration from disk.
        
        Use this after configuration changes to ensure
        the latest values are loaded.
        """
        self._load_config()
    
    @classmethod
    def reset_instance(cls) -> None:
        """
        Reset the singleton instance.
        
        Forces a fresh config load on next access.
        """
        cls._instance = None
        cls._config = {}
    
    @property
    def service_user(self) -> str:
        """Get the default service user."""
        return self.get("service_user", "www-data")
    
    @property
    def service_group(self) -> str:
        """Get the default service group."""
        return self.get("service_group", "www-data")
    
    @property
    def ssl_enabled(self) -> bool:
        """Check if SSL is enabled by default."""
        return self.get("ssl.enabled", True)
    
    @property
    def ssl_email(self) -> str:
        """Get the SSL certificate email."""
        return self.get("ssl.email", "")
    
    def save(self, path: Optional[Path] = None) -> bool:
        """
        Save current configuration to file.
        
        Args:
            path: Optional path to save to. Defaults to global config path.
            
        Returns:
            True if saved successfully, False otherwise.
        """
        save_path = path or DEFAULT_CONFIG_PATH
        
        try:
            save_path.parent.mkdir(parents=True, exist_ok=True)
            with open(save_path, "w") as f:
                yaml.dump(self._config, f, default_flow_style=False)
            return True
        except Exception:
            return False
    
    def reload(self) -> None:
        """Reload configuration from file."""
        self._load_config()
    
    def to_dict(self) -> Dict[str, Any]:
        """Return configuration as dictionary."""
        return self._config.copy()
