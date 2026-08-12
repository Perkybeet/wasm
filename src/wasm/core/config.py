# Copyright (c) 2024-2025 Yago López Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
Global configuration management for WASM.

The configuration file holds credentials (MySQL root password, SMTP account,
OpenAI API key), so this module owns two security guarantees:

* every file it writes is created with :data:`SECRET_FILE_MODE` via ``os.open``,
  never with a ``chmod`` afterwards, which would leave a window where the
  secrets are world readable,
* the in-memory configuration is isolated from :data:`DEFAULT_CONFIG`; defaults
  are deep-copied on load and accessors hand out copies, so a secret set on one
  instance cannot leak into the next one.
"""

import copy
import logging
import os
import re
import stat
from pathlib import Path
from typing import Any, Optional

import yaml

logger = logging.getLogger(__name__)

# Files holding secrets are owner-only; the directories that contain them too.
SECRET_FILE_MODE = 0o600
SECRET_DIR_MODE = 0o700

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
DEFAULT_CONFIG: dict[str, Any] = {
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
        "scan_interval": 30,  # Local pattern scan every 30 seconds
        "ai_interval": 3600,  # AI analysis every 1 hour
        "cpu_threshold": 80.0,
        "memory_threshold": 80.0,
        "use_ai": True,
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

# Keys that older versions wrote and that no longer exist. The monitor reports,
# it does not kill processes, so these settings are dropped on load and pinned
# to their safe value on read: a stale config file must never be able to hand a
# permissive answer back to a caller that has not been updated yet.
REMOVED_KEYS: dict[str, Any] = {
    "monitor.auto_terminate": False,
    "monitor.terminate_malicious_only": False,
    "monitor.dry_run": True,
}

# Words that mark a configuration key as holding a secret. The key name is split
# on separators and camel case boundaries and each word is compared case
# insensitively, so "api_key", "API_KEY", "AuthToken" and "smtp.password" are
# all covered while "keyboard_layout" and "monkey" are left alone.
SECRET_KEY_MARKERS: frozenset[str] = frozenset(
    {
        "password",
        "passwd",
        "secret",
        "token",
        "key",
        "apikey",
        "credential",
        "credentials",
        "auth",
    }
)

REDACTED = "***"


def _is_secret_key(key: str) -> bool:
    """
    Check whether a key name suggests it holds a secret.

    The key is split on separators and camel case boundaries, and each resulting
    word is compared against :data:`SECRET_KEY_MARKERS`. Whole words only:
    ``api_key`` and ``AuthToken`` match, ``keyboard_layout`` and ``monkey`` do
    not.

    Args:
        key: Configuration key name (not a dotted path).

    Returns:
        True if the value behind this key must be redacted.
    """
    normalized = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", key).lower()
    words = re.split(r"[^a-z0-9]+", normalized)
    return any(word in SECRET_KEY_MARKERS for word in words)


def redact_secrets(config: Any) -> Any:
    """
    Return a copy of a configuration structure with secrets replaced.

    Walks dictionaries and lists recursively and replaces the value of every key
    whose name matches :data:`SECRET_KEY_MARKERS` with :data:`REDACTED`, empty
    values included, so the result never reveals whether a secret is set. A
    container behind such a key is walked instead of being replaced wholesale,
    so a ``credentials`` block keeps its user names and loses only its
    passwords. The input is not modified.

    Args:
        config: Configuration mapping, sequence or scalar to redact.

    Returns:
        A redacted deep copy of the input.
    """
    if isinstance(config, dict):
        return {
            key: REDACTED
            if _is_secret_key(str(key)) and not isinstance(value, (dict, list, tuple))
            else redact_secrets(value)
            for key, value in config.items()
        }
    if isinstance(config, (list, tuple)):
        return [redact_secrets(item) for item in config]
    return config


def secure_directory(path: Path) -> None:
    """
    Create a directory that may hold secrets and enforce owner-only access.

    A missing directory is created with :data:`SECRET_DIR_MODE`. An existing one
    is only tightened when it belongs to the current user and is not a shared
    directory (sticky bit): tightening ``/tmp`` or another shared location would
    break the system for everyone else, and the 0600 mode of the files inside
    already protects their content. A chmod that is refused is logged, not
    raised, because the payload write must still go through.

    Args:
        path: Directory to create or tighten.

    Raises:
        OSError: If the directory cannot be created.
    """
    path.mkdir(parents=True, exist_ok=True, mode=SECRET_DIR_MODE)

    info = path.stat()
    is_shared = bool(info.st_mode & stat.S_ISVTX)
    if info.st_mode & 0o077 and not is_shared and info.st_uid == os.geteuid():
        try:
            path.chmod(SECRET_DIR_MODE)
        except OSError as exc:
            logger.warning("Could not restrict permissions on %s: %s", path, exc)


def restrict_file(path: Path) -> None:
    """
    Tighten an existing file that holds secrets to owner-only access.

    Files created by earlier versions are world readable; this repairs them on
    the next open. Missing files are ignored, and a refused chmod is logged
    rather than raised so the caller can still do its work.

    Args:
        path: File to tighten.
    """
    if not path.exists() or not path.stat().st_mode & 0o077:
        return
    try:
        path.chmod(SECRET_FILE_MODE)
    except OSError as exc:
        logger.warning("Could not restrict permissions on %s: %s", path, exc)


def secure_write(path: Path, content: str) -> None:
    """
    Write a file that holds secrets, owner-readable only.

    The file is created through ``os.open`` with the restrictive mode already
    applied, so it is never briefly world readable. An existing file with a lax
    mode is tightened before its new content is written.

    Args:
        path: Destination file.
        content: Text to write.

    Raises:
        OSError: If the file cannot be created or written.
    """
    secure_directory(path.parent)
    restrict_file(path)

    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, SECRET_FILE_MODE)
    with os.fdopen(fd, "w") as handle:
        handle.write(content)


def _strip_removed_keys(config: dict[str, Any]) -> dict[str, Any]:
    """
    Drop settings that no longer exist from a configuration mapping.

    Args:
        config: Configuration loaded from disk, possibly written by an older
            version.

    Returns:
        The same mapping, without the keys listed in :data:`REMOVED_KEYS`.
    """
    for dotted_key in REMOVED_KEYS:
        *parents, leaf = dotted_key.split(".")
        node: Any = config
        for parent in parents:
            node = node.get(parent) if isinstance(node, dict) else None
            if node is None:
                break
        if isinstance(node, dict) and leaf in node:
            del node[leaf]
            logger.debug("Ignoring removed configuration key %s", dotted_key)
    return config


class Config:
    """
    Configuration manager for WASM.

    Handles loading, saving, and accessing configuration values from
    the global config file and environment variables.
    """

    _instance: Optional["Config"] = None
    _config: dict[str, Any] = {}

    def __new__(cls) -> "Config":
        """Singleton pattern to ensure single config instance."""
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._load_config()
        return cls._instance

    def _load_config(self) -> None:
        """
        Load configuration from file and merge with defaults.

        The defaults are deep-copied: a shallow copy would share the nested
        dictionaries with :data:`DEFAULT_CONFIG`, so any :meth:`set` on a nested
        key would rewrite the module-level defaults and leak, secrets included,
        into every later instance.
        """
        self._config = copy.deepcopy(DEFAULT_CONFIG)

        if DEFAULT_CONFIG_PATH.exists():
            try:
                with open(DEFAULT_CONFIG_PATH) as f:
                    file_config = yaml.safe_load(f) or {}
            except (OSError, yaml.YAMLError) as exc:
                logger.warning("Ignoring invalid config file %s: %s", DEFAULT_CONFIG_PATH, exc)
            else:
                if isinstance(file_config, dict):
                    self._config = self._deep_merge(self._config, _strip_removed_keys(file_config))

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

    def _deep_merge(self, base: dict, override: dict) -> dict:
        """
        Deep merge two dictionaries.

        Args:
            base: Base mapping; it is not modified.
            override: Values that win over the base.

        Returns:
            A new mapping with the override values applied, deep-copied so the
            result shares no nested container with either input.
        """
        result = copy.deepcopy(base)
        for key, value in override.items():
            if key in result and isinstance(result[key], dict) and isinstance(value, dict):
                result[key] = self._deep_merge(result[key], value)
            else:
                result[key] = copy.deepcopy(value)
        return result

    def get(self, key: str, default: Any = None) -> Any:
        """
        Get a configuration value.

        Nested containers are returned as deep copies, so callers cannot mutate
        the configuration, and through it the module defaults, by accident.
        Keys listed in :data:`REMOVED_KEYS` always return their pinned safe
        value and ignore ``default``.

        Args:
            key: Configuration key (supports dot notation for nested values).
            default: Default value if key is not found.

        Returns:
            Configuration value or default.
        """
        if key in REMOVED_KEYS:
            return REMOVED_KEYS[key]

        keys = key.split(".")
        value = self._config

        for k in keys:
            if isinstance(value, dict) and k in value:
                value = value[k]
            else:
                return default

        if isinstance(value, (dict, list)):
            return copy.deepcopy(value)
        return value

    def set(self, key: str, value: Any) -> None:
        """
        Set a configuration value.

        Keys listed in :data:`REMOVED_KEYS` are ignored: they no longer control
        anything and must not be reintroduced into a saved config file.

        Args:
            key: Configuration key (supports dot notation).
            value: Value to set.
        """
        if key in REMOVED_KEYS:
            logger.debug("Ignoring write to removed configuration key %s", key)
            return

        keys = key.split(".")
        config = self._config

        for k in keys[:-1]:
            if k not in config or not isinstance(config[k], dict):
                config[k] = {}
            config = config[k]

        config[keys[-1]] = copy.deepcopy(value)

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

    def save(self, path: Path | None = None) -> bool:
        """
        Save current configuration to file.

        Args:
            path: Optional path to save to. Defaults to global config path.

        Returns:
            True if saved successfully, False otherwise.
        """
        save_path = path or DEFAULT_CONFIG_PATH

        try:
            secure_write(
                save_path,
                yaml.dump(self._config, default_flow_style=False),
            )
            return True
        except (OSError, yaml.YAMLError) as exc:
            logger.error("Could not save configuration to %s: %s", save_path, exc)
            return False

    def to_dict(self) -> dict[str, Any]:
        """
        Return configuration as dictionary.

        Returns:
            A deep copy of the configuration, safe to mutate. Secrets are
            included; use :func:`redact_secrets` before exposing it.
        """
        return copy.deepcopy(self._config)

    def upgrade(self, path: Path | None = None) -> dict[str, Any]:
        """
        Upgrade configuration file with new defaults.

        Merges DEFAULT_CONFIG with user's existing config, preserving
        user values while adding new keys from defaults.

        Args:
            path: Optional path to config file. Defaults to global config path.

        Returns:
            Dictionary with upgrade results:
            - added_keys: List of new keys added
            - removed_keys: List of keys no longer in defaults (kept in file)
            - upgraded: Boolean indicating if file was modified
        """
        config_path = path or DEFAULT_CONFIG_PATH

        # Load user's current config (raw, without merging defaults)
        user_config: dict[str, Any] = {}
        if config_path.exists():
            try:
                with open(config_path) as f:
                    loaded = yaml.safe_load(f) or {}
            except (OSError, yaml.YAMLError) as exc:
                logger.warning("Ignoring invalid config file %s: %s", config_path, exc)
            else:
                if isinstance(loaded, dict):
                    user_config = _strip_removed_keys(loaded)

        # Find keys that need to be added
        added_keys = self._find_missing_keys(DEFAULT_CONFIG, user_config)

        # Merge: defaults first, then user config (user wins)
        merged_config = self._deep_merge(DEFAULT_CONFIG, user_config)

        # Only save if there are new keys
        if added_keys:
            try:
                secure_write(
                    config_path,
                    yaml.dump(merged_config, default_flow_style=False, sort_keys=False),
                )

                # Reload to use new config
                self._config = merged_config
            except (OSError, yaml.YAMLError) as e:
                logger.error("Could not upgrade configuration at %s: %s", config_path, e)
                return {
                    "added_keys": [],
                    "upgraded": False,
                    "error": str(e),
                }

        return {
            "added_keys": added_keys,
            "upgraded": len(added_keys) > 0,
        }

    def _find_missing_keys(self, defaults: dict, user: dict, prefix: str = "") -> list:
        """
        Find keys in defaults that are missing from user config.

        Args:
            defaults: Default configuration dictionary.
            user: User's configuration dictionary.
            prefix: Current key prefix for nested keys.

        Returns:
            List of missing key paths (dot notation).
        """
        missing = []

        for key, value in defaults.items():
            full_key = f"{prefix}.{key}" if prefix else key

            if key not in user:
                missing.append(full_key)
            elif isinstance(value, dict) and isinstance(user.get(key), dict):
                # Recurse into nested dicts
                missing.extend(self._find_missing_keys(value, user[key], full_key))

        return missing
