# Copyright (c) 2024-2025 Yago López Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
Global configuration management for WASM.

The configuration file holds credentials (MySQL root password, SMTP account,
OpenAI API key), so this module owns four security guarantees:

* every file it writes is created with :data:`SECRET_FILE_MODE` at ``open``
  time, never with a ``chmod`` afterwards, which would leave a window where the
  secrets are world readable, and a symlink planted in the destination
  directory cannot redirect the write,
* every directory it creates on the way there gets :data:`SECRET_DIR_MODE`, not
  just the last one,
* the in-memory configuration is isolated from :data:`DEFAULT_CONFIG`; defaults
  are deep-copied on load and accessors hand out copies, so a secret set on one
  instance cannot leak into the next one,
* settings listed in :data:`REMOVED_KEYS` cannot be read, written or persisted
  through their container either, so a stale file or a stale panel form cannot
  hand a permissive answer back.

This is the only writer of ``config.yaml``. The web API and the CLI both go
through :class:`Config`; a second writer is how the hardening was lost once
already.

Every change to the filesystem goes through :mod:`wasm.core.fs`. Nothing here
calls ``mkdir``, ``chmod`` or ``open`` for writing directly, because a
``--dry-run`` that writes half of ``/etc/wasm/config.yaml`` is worse than no
rehearsal at all: the operator has already been told nothing would change.
"""

from __future__ import annotations

import copy
import logging
import os
import re
import stat
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]

from wasm.core.exceptions import SecurityError
from wasm.core.fs import SECRET_DIR_MODE, SECRET_MODE, FileSystem, get_fs

logger = logging.getLogger(__name__)

#: Files holding secrets are owner-only. The value lives in :mod:`wasm.core.fs`
#: so the seam and the callers that ask it for a mode cannot drift apart; the
#: name is kept because the rest of the codebase imports it from here.
SECRET_FILE_MODE = SECRET_MODE

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
        # These values are enforced by wasm.web.auth.SecurityConfig, whose
        # dataclass defaults must say the same numbers - core cannot import
        # the web layer to share one constant, so the agreement is pinned by
        # a test in tests/test_cli_web.py. They used to disagree, which went
        # unnoticed exactly as long as this section was ignored.
        "rate_limit_enabled": True,
        "rate_limit_requests": 120,
        "rate_limit_window": 60,
        "max_failed_attempts": 5,
        "lockout_duration": 900,
        "token_expiration_hours": 12,
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

# Settings whose name contains a marker word but which hold no credential. The
# list is explicit and short on purpose: everything not named here that looks
# like a secret is treated as one.
NON_SECRET_KEYS: frozenset[str] = frozenset({"token_expiration_hours"})

REDACTED = "***"


def _is_secret_key(key: str) -> bool:
    """
    Check whether a key name suggests it holds a secret.

    The key is split on separators and camel case boundaries, and each resulting
    word is compared against :data:`SECRET_KEY_MARKERS`. Whole words only:
    ``api_key`` and ``AuthToken`` match, ``keyboard_layout`` and ``monkey`` do
    not. Names listed in :data:`NON_SECRET_KEYS` are excluded.

    Args:
        key: Configuration key name (not a dotted path).

    Returns:
        True if the value behind this key must be redacted.
    """
    normalized = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", key).lower()
    if normalized in NON_SECRET_KEYS:
        return False

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


def restore_redacted(incoming: Any, current: Any) -> Any:
    """
    Put the stored secrets back where a caller sent :data:`REDACTED`.

    The web panel renders what :func:`redact_secrets` produced, so saving a form
    the user did not touch posts ``***`` back. Storing that literally would
    destroy the credential. Every secret key whose incoming value is the
    placeholder takes the value already stored instead, or the empty string when
    nothing was stored. Non-secret keys are copied verbatim: a literal ``***``
    elsewhere is data, not a placeholder. Neither input is modified.

    Args:
        incoming: Configuration structure received from a caller.
        current: Configuration structure currently stored.

    Returns:
        A deep copy of ``incoming`` with the placeholders resolved.
    """
    if isinstance(incoming, dict):
        stored = current if isinstance(current, dict) else {}
        resolved: dict[Any, Any] = {}
        for key, value in incoming.items():
            if _is_secret_key(str(key)) and value == REDACTED:
                previous = stored.get(key, "")
                resolved[key] = copy.deepcopy(previous) if previous != REDACTED else ""
            else:
                resolved[key] = restore_redacted(value, stored.get(key))
        return resolved
    if isinstance(incoming, (list, tuple)):
        return [restore_redacted(item, None) for item in incoming]
    return copy.deepcopy(incoming)


def _removed_keys_under(prefix: str) -> dict[str, Any]:
    """
    Select the removed settings that live inside a given container.

    Args:
        prefix: Dotted path of the container.

    Returns:
        Mapping of the path relative to ``prefix`` to the pinned safe value.
    """
    head = f"{prefix}." if prefix else ""
    return {
        dotted[len(head) :]: safe
        for dotted, safe in REMOVED_KEYS.items()
        if dotted.startswith(head) and dotted != prefix
    }


def _pin_removed_keys(prefix: str, subtree: dict[str, Any]) -> dict[str, Any]:
    """
    Force the safe value of every removed setting inside a container.

    Callers that predate the removal read ``config.get("monitor")`` and then
    ``.get("auto_terminate", True)``; without the pin they get their own
    permissive default back and the removal is void. Missing intermediate
    containers are created for the same reason: an absent container answers
    every lookup with the caller's default.

    Args:
        prefix: Dotted path of ``subtree``.
        subtree: Container to pin, modified in place.

    Returns:
        The same container.
    """
    for relative, safe in _removed_keys_under(prefix).items():
        *parents, leaf = relative.split(".")
        node = subtree
        for parent in parents:
            child = node.get(parent)
            if not isinstance(child, dict):
                child = {}
                node[parent] = child
            node = child
        node[leaf] = safe
    return subtree


def _strip_removed_under(prefix: str, value: Any) -> Any:
    """
    Drop removed settings from a value about to be stored under a container.

    ``set("monitor", {...})`` must obey the same guard as
    ``set("monitor.auto_terminate", ...)``; otherwise the guard is one dotted
    path away from being bypassed.

    Args:
        prefix: Dotted path the value is stored at.
        value: Value to filter, modified in place when it is a mapping.

    Returns:
        The filtered value.
    """
    if not isinstance(value, dict):
        return value

    for relative in _removed_keys_under(prefix):
        *parents, leaf = relative.split(".")
        node: Any = value
        for parent in parents:
            node = node.get(parent) if isinstance(node, dict) else None
            if node is None:
                break
        if isinstance(node, dict) and leaf in node:
            del node[leaf]
            logger.debug("Ignoring removed configuration key %s.%s", prefix, relative)
    return value


def secure_directory(path: Path, fs: FileSystem | None = None) -> None:
    """
    Create a directory that may hold secrets and enforce owner-only access.

    Every level that is missing is created with :data:`SECRET_DIR_MODE`;
    ``mkdir(parents=True)`` applies the mode to the last level only and leaves
    the intermediate ones at ``0777 & ~umask``, which is exactly what
    :meth:`~wasm.core.fs.FileSystem.make_dir` exists to avoid. Directories that
    already exist are left alone, except the leaf, which is tightened when it
    belongs to the current user and is not a shared directory (sticky bit):
    tightening ``/tmp`` or another shared location would break the system for
    everyone else, and the 0600 mode of the files inside already protects their
    content. A chmod that is refused is logged, not raised, because the payload
    write must still go through.

    Args:
        path: Directory to create or tighten.
        fs: Filesystem to change. Defaults to the process-wide one, which is
            what makes ``--dry-run`` leave the machine untouched.

    Raises:
        OSError: If the directory cannot be created.
    """
    filesystem = fs or get_fs()
    try:
        filesystem.make_dir(path, mode=SECRET_DIR_MODE, parents=True)
    except FileExistsError:
        # Another process won the race between the existence check and the
        # mkdir; its mode is not ours to change.
        pass

    if not path.exists():
        # A rehearsal refused to create it. There is nothing to inspect and
        # nothing to tighten, and stat() here would raise.
        return

    info = path.stat()
    is_shared = bool(info.st_mode & stat.S_ISVTX)
    if info.st_mode & 0o077 and not is_shared and info.st_uid == os.geteuid():
        try:
            filesystem.chmod(path, SECRET_DIR_MODE)
        except OSError as exc:
            logger.warning("Could not restrict permissions on %s: %s", path, exc)


def restrict_file(path: Path, fs: FileSystem | None = None) -> None:
    """
    Tighten an existing file that holds secrets to owner-only access.

    Files created by earlier versions are world readable; this repairs them on
    the next open. Missing files are ignored, and a refused chmod is logged
    rather than raised so the caller can still do its work. Symlinks are left
    untouched: ``chmod`` follows them, so a link planted in the directory would
    hand the mode change to a file of the attacker's choosing;
    :func:`secure_write` refuses that path anyway.

    Args:
        path: File to tighten.
        fs: Filesystem to change. Defaults to the process-wide one.
    """
    try:
        info = os.lstat(path)
    except OSError:
        # Missing, or a dangling symlink, for which exists() answers False.
        return

    if stat.S_ISLNK(info.st_mode):
        logger.warning("Refusing to change permissions through the symlink %s", path)
        return
    if not info.st_mode & 0o077:
        return

    filesystem = fs or get_fs()
    try:
        filesystem.chmod(path, SECRET_FILE_MODE)
    except OSError as exc:
        logger.warning("Could not restrict permissions on %s: %s", path, exc)


def _refuse_symlink(path: Path) -> None:
    """
    Stop before writing secrets to a path someone replaced with a link.

    The write itself lands on a fresh file that is renamed over ``path``, so a
    link could not redirect the content anyway; refusing loudly is still the
    right answer, because a symlink where WASM expects its own file means
    somebody is trying something and silently unlinking their link would hide
    it.

    Args:
        path: Destination about to be written.

    Raises:
        SecurityError: If ``path`` is a symlink, dangling ones included.
    """
    if path.is_symlink():
        raise SecurityError(
            f"Refusing to write secrets through the symlink {path}",
            details=(
                "Something replaced the file with a symbolic link, which would "
                "redirect the write. Inspect the directory, remove the link and "
                "retry."
            ),
        )


def secure_write(
    path: Path,
    content: str,
    secure_parent: bool = True,
    fs: FileSystem | None = None,
) -> None:
    """
    Write a file that holds secrets, owner-readable only.

    The write goes through :meth:`~wasm.core.fs.FileSystem.write_text`, which
    creates a temporary file with :data:`SECRET_FILE_MODE` already applied and
    renames it over the destination. That buys three things at once: the file is
    never briefly world readable, a reader never sees a half-written config, and
    the rename cannot follow a symlink planted at ``path``. Such a link is
    refused before anything is written, and an existing regular file with a lax
    mode is tightened as well, so a failure in between cannot leave the old
    secrets exposed.

    Args:
        path: Destination file.
        content: Text to write.
        secure_parent: Whether the parent directory must be private too. Pass
            False for files that live inside a tree served to other accounts,
            such as an application's ``.env``.
        fs: Filesystem to change. Defaults to the process-wide one, which is
            what makes ``--dry-run`` leave the machine untouched.

    Raises:
        SecurityError: If ``path`` is a symlink.
        OSError: If the file cannot be created or written.
    """
    filesystem = fs or get_fs()
    if secure_parent:
        secure_directory(path.parent, fs=filesystem)
    else:
        filesystem.make_dir(path.parent, parents=True)

    _refuse_symlink(path)
    restrict_file(path, fs=filesystem)
    filesystem.write_text(path, content, mode=SECRET_FILE_MODE)


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

    _instance: Config | None = None
    _config: dict[str, Any] = {}
    _fs: FileSystem | None = None

    def __new__(cls, fs: FileSystem | None = None) -> Config:
        """
        Return the single configuration instance, building it on first use.

        Args:
            fs: Filesystem this instance writes through. Defaults to the
                process-wide one, which is what makes ``--dry-run`` honest.
                Passing one on a later call re-points the existing instance,
                because the singleton is what every caller shares.

        Returns:
            The configuration instance.
        """
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._fs = fs
            cls._instance._load_config()
        elif fs is not None:
            cls._instance._fs = fs
        return cls._instance

    @property
    def fs(self) -> FileSystem:
        """
        The filesystem every write of this configuration goes through.

        Returns:
            The injected filesystem, or the process-wide one.
        """
        return self._fs or get_fs()

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
        env_mappings: dict[str, str | tuple[str, str]] = {
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

    def _deep_merge(self, base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
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
        value and ignore ``default``, and so do the same keys read through their
        container: ``get("monitor")["auto_terminate"]`` is the pinned value, not
        the caller's optimistic default.

        Args:
            key: Configuration key (supports dot notation for nested values).
            default: Default value if key is not found.

        Returns:
            Configuration value or default.
        """
        if key in REMOVED_KEYS:
            return REMOVED_KEYS[key]

        keys = key.split(".")
        value: Any = self._config

        for k in keys:
            if isinstance(value, dict) and k in value:
                value = value[k]
            else:
                value = default
                break

        if isinstance(value, dict):
            return _pin_removed_keys(key, copy.deepcopy(value))
        if isinstance(value, list):
            return copy.deepcopy(value)
        return value

    def set(self, key: str, value: Any) -> None:
        """
        Set a configuration value.

        Keys listed in :data:`REMOVED_KEYS` are ignored: they no longer control
        anything and must not be reintroduced into a saved config file. Writing
        the container they used to live in is filtered the same way, so the
        guard cannot be bypassed by moving one level up. A :data:`REDACTED`
        placeholder on a secret key keeps the stored secret, so a caller that
        only ever saw the redacted configuration cannot destroy a credential by
        writing back what it was shown.

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

        leaf = keys[-1]
        resolved = restore_redacted({leaf: value}, {leaf: config.get(leaf)})[leaf]
        config[leaf] = _strip_removed_under(key, resolved)

    def replace(self, config: dict[str, Any]) -> None:
        """
        Replace the whole configuration with a caller-supplied mapping.

        This is what a full update from the web panel goes through. Two things
        happen on the way in: :data:`REDACTED` placeholders take the secret that
        is currently stored, because the panel only ever saw the redacted dump,
        and removed settings are dropped, because a stale form must not be able
        to reintroduce them.

        Args:
            config: The new configuration.
        """
        resolved: dict[str, Any] = restore_redacted(config, self._config)
        self._config = _strip_removed_keys(resolved)

    @property
    def path(self) -> Path:
        """
        Path of the file this configuration is read from and written to.

        Returns:
            The single configuration file path.
        """
        return DEFAULT_CONFIG_PATH

    @property
    def apps_directory(self) -> Path:
        """Get the applications directory path."""
        return Path(str(self.get("apps_directory", str(DEFAULT_APPS_DIR))))

    @property
    def webserver(self) -> str:
        """Get the default web server."""
        return str(self.get("webserver", "nginx"))

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
        cls._fs = None

    @property
    def service_user(self) -> str:
        """Get the default service user."""
        return str(self.get("service_user", "www-data"))

    @property
    def service_group(self) -> str:
        """Get the default service group."""
        return str(self.get("service_group", "www-data"))

    @property
    def ssl_enabled(self) -> bool:
        """Check if SSL is enabled by default."""
        return bool(self.get("ssl.enabled", True))

    @property
    def ssl_email(self) -> str:
        """Get the SSL certificate email."""
        return str(self.get("ssl.email", ""))

    def write(self, path: Path | None = None) -> Path:
        """
        Write the configuration to disk, reporting failures to the caller.

        Args:
            path: Optional path to write to. Defaults to the global config path.

        Returns:
            The path that was written.

        Raises:
            SecurityError: If the destination is a symlink.
            OSError: If the file cannot be created or written.
            yaml.YAMLError: If the configuration cannot be serialised.
        """
        save_path = path or DEFAULT_CONFIG_PATH
        secure_write(save_path, yaml.dump(self._config, default_flow_style=False), fs=self.fs)
        return save_path

    def save(self, path: Path | None = None) -> bool:
        """
        Save current configuration to file.

        Args:
            path: Optional path to save to. Defaults to global config path.

        Returns:
            True if saved successfully, False otherwise. Callers that need the
            reason should use :meth:`write`.
        """
        save_path = path or DEFAULT_CONFIG_PATH

        try:
            self.write(save_path)
            return True
        except (OSError, yaml.YAMLError, SecurityError) as exc:
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
                    fs=self.fs,
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
