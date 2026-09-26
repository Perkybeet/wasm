# Copyright (c) 2024-2025 Yago López Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
Global configuration management for WASM.

The configuration file holds credentials (MySQL root password, SMTP account,
and, in a file an older version wrote, an OpenAI API key that is no longer a
default but is still redacted on sight if present), so this module owns four
security guarantees:

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
import json
import logging
import os
import re
import stat
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]

from wasm.core.exceptions import ConfigError, SecurityError
from wasm.core.fs import SECRET_DIR_MODE, SECRET_MODE, FileSystem, get_fs
from wasm.validators.domain import is_valid_domain

logger = logging.getLogger(__name__)

#: Files holding secrets are owner-only. The value lives in :mod:`wasm.core.fs`
#: so the seam and the callers that ask it for a mode cannot drift apart; the
#: name is kept because the rest of the codebase imports it from here.
SECRET_FILE_MODE = SECRET_MODE

# Default paths
DEFAULT_CONFIG_PATH = Path("/etc/wasm/config.yaml")
DEFAULT_APPS_DIR = Path("/var/www/apps")
DEFAULT_LOG_DIR = Path("/var/log/wasm")
#: Where backups go when ``backup.directory`` is unset or empty. It lives here,
#: not in the backup manager, so the config chokepoint and every reader resolve
#: an empty value to the same place.
DEFAULT_BACKUP_DIR = Path("/var/backups/wasm")

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
    "deploy": {
        # Layout of applications created from now on: "releases" builds each
        # deploy in its own directory behind a health gate; "inplace" is the
        # 1.x layout. Applications that already exist keep the layout they
        # have until they are migrated explicitly.
        "layout": "releases",
    },
    "updates": {
        # Whether wasm asks GitHub for the latest release: the CLI's
        # background check on every command and the panel's GET
        # /api/system/version. On by default; an operator on an airgapped or
        # firewalled server turns it off here rather than have every command
        # start a request that can only time out. Off never blocks anything -
        # the check already runs off the request/command path, in a
        # background thread with a short timeout, behind a cache - it just
        # means the request stops being made at all.
        "check": True,
    },
    "monitor": {
        "enabled": False,
        # Seconds between scans, and so the longest a failed unit can go
        # unannounced. Matches process_monitor.DEFAULT_SCAN_INTERVAL.
        "scan_interval": 60,
        "cpu_threshold": 80.0,
        "memory_threshold": 80.0,
        "log_file": str(DEFAULT_LOG_DIR / "monitor.log"),
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
    "notifications": {
        # Multi-channel event notifications, delivered by wasm.core.notifier.
        # Off until the operator turns them on; every event kind defaults to
        # on so enabling the feature is one switch, not seven. The kind names
        # are pinned against notifier.EVENT_KINDS by a test in
        # tests/test_notifier.py, because this module cannot import the
        # notifier (the notifier reads its settings from here).
        "enabled": False,
        "events": {
            "deploy_success": True,
            "deploy_failed": True,
            "cert_expiring": True,
            "unit_failed": True,
            "disk_threshold": True,
            "backup_failed": True,
        },
        "channels": {
            # A webhook URL is a capability: Slack and Discord embed the
            # secret in the path. Every one of them is named "webhook_url",
            # the generic endpoint included, so the "webhook" redaction
            # marker covers them all by name.
            "webhook": {"webhook_url": ""},
            "slack": {"webhook_url": ""},
            "discord": {"webhook_url": ""},
            "telegram": {"bot_token": "", "chat_id": ""},
            # Email reuses the monitor's SMTP account (monitor.smtp.*).
            "email": {"enabled": False},
        },
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
        "rate_limit_authenticated_requests": 1200,
        "rate_limit_window": 60,
        "max_failed_attempts": 5,
        "lockout_duration": 900,
        "token_expiration_hours": 12,
        "ip_whitelist": [],
    },
    "databases": {
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

# Web servers WASM can actually front a site with. wasm.web.api.config keeps
# its own copy for its dedicated /config/webserver endpoint; this is the one
# both wasm.cli.commands.config's `set` and the generic PATCH /api/config
# enforce, because that generic path is the one that had no rule of its own -
# it wrote whatever value it was given straight into the unit and site
# templates.
SUPPORTED_WEBSERVERS = frozenset({"nginx", "apache"})


def _validate_webserver(value: Any) -> Any:
    """
    Refuse a web server WASM has no manager for.

    Args:
        value: The candidate value, from either front end.

    Returns:
        The value unchanged, once it is known to be usable.

    Raises:
        ConfigError: When the value is not a web server WASM can manage.
    """
    if value not in SUPPORTED_WEBSERVERS:
        raise ConfigError(
            f"Unsupported webserver: {value!r}",
            details=f"Choose one of: {', '.join(sorted(SUPPORTED_WEBSERVERS))}.",
        )
    return value


def _validate_absolute_path(label: str) -> Callable[[Any], str]:
    """
    Build a validator that refuses a relative filesystem path.

    A relative apps directory resolves against whatever the current working
    directory happens to be at the time - the CLI's, the panel's, or a
    ``systemd`` unit started with its own - which differs per caller and can
    change under a long-running process. Every consumer downstream already
    requires an absolute path (systemd units, nginx and Apache document
    roots, the deployers): this is what stops a relative value from ever
    reaching them broken.

    Args:
        label: Name used in the error message.

    Returns:
        A function raising :class:`ConfigError` for anything that is not an
        absolute path.
    """

    def validator(value: Any) -> str:
        text = str(value)
        if not text or not Path(text).is_absolute():
            raise ConfigError(
                f"{label} must be an absolute path",
                details=f"Got {text!r}. Use a path starting with '/', such as /var/www/apps.",
            )
        return text

    return validator


def resolve_backup_directory(value: Any, default: Path = DEFAULT_BACKUP_DIR) -> Path:
    """
    Turn a stored ``backup.directory`` into the directory backups really go to.

    This is the only interpretation of the setting. ``Path('')`` is the current
    working directory, and ``backup.directory: ''`` - written by the 1.x
    panel's settings form, whose field rendered empty because the section had
    no default - sent every backup to ``/root/<app>/`` when an operator ran
    wasm from ``/root`` and to ``/<app>/`` when a timer did. So empty means
    the default, and a relative path is refused, never resolved against
    whatever directory the caller happens to be in.

    Args:
        value: The stored value; None, empty or whitespace mean "not set".
        default: Directory used when the value is not set. The backup manager
            passes its own class attribute so a sandbox can redirect it.

    Returns:
        An absolute directory.

    Raises:
        ConfigError: When the value is set to a relative path.
    """
    text = "" if value is None else str(value).strip()
    if not text:
        return default
    path = Path(text)
    if not path.is_absolute():
        raise ConfigError(
            "backup.directory must be an absolute path",
            details=(
                f"Got {text!r}, which would depend on the directory wasm is started from. "
                f"Run 'wasm config set backup.directory {DEFAULT_BACKUP_DIR}' (or any path "
                "starting with '/'), or set it to '' to use the default."
            ),
        )
    return path


def _validate_backup_directory(value: Any) -> str:
    """
    Refuse a relative backup directory and store an empty one as the default.

    Args:
        value: The candidate value, from either front end.

    Returns:
        The absolute directory, as a string.

    Raises:
        ConfigError: When the value is a relative path.
    """
    return str(resolve_backup_directory(value))


def _forget_blank_backup_directory(tree: dict[str, Any]) -> None:
    """
    Treat a stored ``backup.directory: ''`` as not set, in memory only.

    The file is deliberately not rewritten here. The configuration is loaded
    by every command, read-only ones and ``--dry-run`` included, and by the
    console while the CLI may be writing the same file: a write on load would
    change ``/etc/wasm/config.yaml`` without anyone asking, would behave
    differently under a rehearsal, and could race the real writer. Dropping
    the key gives exactly the behaviour a rewrite would, and the next write
    through :meth:`Config.set` or :meth:`Config.replace` persists it.

    Args:
        tree: The configuration just merged from the file, changed in place.
    """
    section = tree.get("backup")
    if not isinstance(section, dict) or "directory" not in section:
        return
    stored = section["directory"]
    if stored is None or (isinstance(stored, str) and not stored.strip()):
        del section["directory"]
        logger.debug("Treating an empty backup.directory as the default %s", DEFAULT_BACKUP_DIR)


def _int_range_validator(label: str, low: int, high: int) -> Callable[[Any], int]:
    """
    Build a validator that requires a whole number in a closed range.

    Accepting the raw value rather than only an ``int`` is what lets
    ``wasm config set`` reuse this directly: argv never hands it anything but
    a string, and ``int("8080")`` is exactly the conversion a human typing a
    port number expects.

    Args:
        label: Name used in the error message.
        low: Smallest value accepted, inclusive.
        high: Largest value accepted, inclusive.

    Returns:
        A function raising :class:`ConfigError` outside the range.
    """

    def validator(value: Any) -> int:
        try:
            number = int(value)
        except (TypeError, ValueError) as exc:
            raise ConfigError(f"{label} must be a whole number", details=f"Got {value!r}.") from exc
        if not low <= number <= high:
            raise ConfigError(f"{label} must be between {low} and {high}", details=f"Got {number}.")
        return number

    return validator


#: A loose but real email address: something@something.tld. This is a syntax
#: check, not a mailbox check - the only way to know an address actually
#: receives mail is to send it one, which 'wasm config set' and a PUT of the
#: monitor's SMTP settings have no business doing.
_EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _is_valid_email(value: str) -> bool:
    """
    Check whether a string looks like an email address.

    Args:
        value: Candidate address.

    Returns:
        True if it has the shape of an address.
    """
    return bool(_EMAIL_PATTERN.match(value))


def _validate_optional_email(label: str) -> Callable[[Any], str]:
    """
    Build a validator that accepts an email address, or leaves it unset.

    An SMTP sender address is meaningfully absent (it falls back to the
    account username), unlike a recipient list, so empty is accepted here and
    refused by :func:`_validate_email_list`.

    Args:
        label: Name used in the error message.

    Returns:
        A function raising :class:`ConfigError` for a non-empty value that is
        not an email address.
    """

    def validator(value: Any) -> str:
        text = "" if value is None else str(value).strip()
        if not text:
            return ""
        if not _is_valid_email(text):
            raise ConfigError(
                f"{label} is not a valid email address",
                details=f"Got {text!r}. Use an address such as ops@example.com.",
            )
        return text

    return validator


def _validate_email_list(label: str) -> Callable[[Any], list[str]]:
    """
    Build a validator that requires a list of email addresses.

    Accepts a comma-separated string too, the same shape 'wasm config set'
    would otherwise need ``--list`` for, so a caller that bypasses the CLI's
    own coercion (a direct :meth:`Config.set` call, or a future front end that
    posts a string) is still refused rather than silently storing a single
    malformed entry.

    Args:
        label: Name used in the error message.

    Returns:
        A function raising :class:`ConfigError` when the value is not a list
        (or comma-separated string) of valid addresses.
    """

    def validator(value: Any) -> list[str]:
        if isinstance(value, str):
            items = [item.strip() for item in value.split(",") if item.strip()]
        elif isinstance(value, list):
            items = [str(item).strip() for item in value if str(item).strip()]
        else:
            raise ConfigError(
                f"{label} must be a list of email addresses",
                details=f"Got {value!r}.",
            )

        invalid = [item for item in items if not _is_valid_email(item)]
        if invalid:
            raise ConfigError(
                f"{label} contains an invalid email address: {', '.join(invalid)}",
                details="Every recipient must be an address such as ops@example.com.",
            )
        return items

    return validator


def _validate_smtp_host(value: Any) -> str:
    """
    Accept an SMTP server hostname, or leave it unset.

    Reuses :func:`~wasm.validators.domain.is_valid_domain`, the same check a
    site's domain goes through - loose enough to accept ``localhost`` and a
    bare IP octet by shape, strict enough to catch a URL or a value with a
    stray path or space pasted in by mistake.

    Args:
        value: The candidate value, from either front end.

    Returns:
        The value unchanged, once it is known to be usable.

    Raises:
        ConfigError: When the value is set and is not a valid hostname.
    """
    text = "" if value is None else str(value).strip()
    if not text:
        return ""
    valid, error = is_valid_domain(text)
    if not valid:
        raise ConfigError(
            f"monitor.smtp.host is not a valid hostname: {error}",
            details=f"Got {text!r}. Use a hostname such as smtp.example.com.",
        )
    return text


def _validate_smtp_section(smtp: dict[str, Any]) -> None:
    """
    Refuse an SMTP configuration that cannot describe a real connection.

    Applied to the whole ``monitor.smtp`` section as it would end up after any
    write that touches it - a single ``wasm config set monitor.smtp.use_tls
    true`` and a full ``PUT /api/config/smtp`` both funnel through
    :meth:`Config.set`, so a contradiction cannot land in the file through one
    and not the other.

    Args:
        smtp: The section as it would be stored, after the write being
            validated is applied.

    Raises:
        ConfigError: When both ``use_ssl`` and ``use_tls`` are enabled at
            once - implicit TLS and STARTTLS are two different ways to reach
            the same server, and a connection can only be made one way.
    """
    if smtp.get("use_ssl") and smtp.get("use_tls"):
        raise ConfigError(
            "monitor.smtp.use_ssl and monitor.smtp.use_tls cannot both be enabled",
            details=(
                "use_ssl connects with implicit TLS, usually on port 465; use_tls "
                "connects in the clear and upgrades with STARTTLS, usually on port "
                "587. They are two ways to reach the same server - turn one off."
            ),
        )


# Validation wasm.web.api.config applies through its own typed endpoints
# (WebserverConfig, BackupConfig, WebConfig, SMTPConfig), reproduced here
# against the dotted key each endpoint actually writes so a value the panel
# would reject cannot be waved through by using 'wasm config set' or the
# generic PATCH /api/config instead.
def _validate_telegram_chat_id(value: Any) -> str:
    """
    Accept a Telegram chat id or leave it unset.

    Args:
        value: The configured ``chat_id``.

    Returns:
        The id as a stripped string, or ``""`` when unset.

    Raises:
        ConfigError: When it is neither an integer id nor an ``@channelname``,
            including the id of a group written without its minus sign.
    """
    from wasm.validators.telegram import validate_telegram_chat_id

    text = "" if value is None else str(value).strip()
    if not text:
        return ""
    try:
        return validate_telegram_chat_id(text)
    except ValueError as exc:
        raise ConfigError(
            "notifications.channels.telegram.chat_id is not a Telegram chat id",
            details=str(exc),
        ) from exc


_KEY_VALIDATORS: dict[str, Callable[[Any], Any]] = {
    "webserver": _validate_webserver,
    "backup.max_per_app": _int_range_validator("backup.max_per_app", 1, 100),
    "web.port": _int_range_validator("web.port", 1, 65535),
    "web.session_timeout": _int_range_validator("web.session_timeout", 300, 86400),
    # The one key every deployer, wasm.core.config.Config.apps_directory and
    # the panel's disk usage meter read. "apps.directory" is a deprecated
    # dotted alias, resolved to this key by _canonical_key() before a
    # validator is even looked up - see KEY_ALIASES.
    "apps_directory": _validate_absolute_path("apps_directory"),
    # Empty is stored as the default; relative is refused. See
    # resolve_backup_directory for the server this came from.
    "backup.directory": _validate_backup_directory,
    "monitor.smtp.host": _validate_smtp_host,
    "monitor.smtp.port": _int_range_validator("monitor.smtp.port", 1, 65535),
    "monitor.smtp.from_address": _validate_optional_email("monitor.smtp.from_address"),
    "monitor.email_recipients": _validate_email_list("monitor.email_recipients"),
    "notifications.channels.telegram.chat_id": _validate_telegram_chat_id,
}

# Rules spanning more than one key of the same container - monitor.smtp's
# use_ssl and use_tls being mutually exclusive cannot be expressed as a rule
# over a single dotted key the way _KEY_VALIDATORS entries are. Keyed by the
# container's own dotted path.
_SECTION_VALIDATORS: dict[str, Callable[[dict[str, Any]], None]] = {
    "monitor.smtp": _validate_smtp_section,
}


def _validate_known_value(key: str, value: Any) -> Any:
    """
    Apply the shared rule for a key, if one exists.

    Args:
        key: Fully resolved dotted key.
        value: Value about to be written.

    Returns:
        The value, normalised by the validator when there is one.

    Raises:
        ConfigError: When the key has a rule and the value fails it.
    """
    validator = _KEY_VALIDATORS.get(key)
    if validator is None:
        return value
    return validator(value)


def _validate_known_values_in(tree: dict[str, Any]) -> None:
    """
    Apply every rule in :data:`_KEY_VALIDATORS` to the keys present in a tree.

    This is what lets :meth:`Config.replace` enforce the same guards
    :meth:`Config.set` does, without :meth:`Config.set`'s dotted-key API: a
    full configuration replacement carries the whole tree already, and a key
    the caller did not include is left alone rather than manufactured, so
    omitting a setting from a ``PUT`` still means "leave it out", not
    "reject the request".

    Args:
        tree: Resolved configuration about to be stored. Values that pass are
            normalised in place, the same way :meth:`Config.set` normalises
            them.

    Raises:
        ConfigError: When a present key's value fails its rule.
    """
    for key, validator in _KEY_VALIDATORS.items():
        parts = key.split(".")
        node: Any = tree
        for part in parts[:-1]:
            if not isinstance(node, dict) or part not in node:
                node = None
                break
            node = node[part]
        if not isinstance(node, dict):
            continue
        leaf = parts[-1]
        if leaf not in node:
            continue
        node[leaf] = validator(node[leaf])


def _validate_known_sections_in(tree: dict[str, Any]) -> None:
    """
    Apply every whole-section rule in :data:`_SECTION_VALIDATORS` present in a tree.

    The counterpart of :func:`_validate_known_values_in` for a rule that spans
    more than one key of the same container, so :meth:`Config.replace` enforces
    it too: a full ``PUT /api/config`` carrying a contradictory
    ``monitor.smtp`` section must be refused exactly as a typed endpoint or
    ``wasm config set`` would refuse it.

    Args:
        tree: Resolved configuration about to be stored.

    Raises:
        ConfigError: When a present section's value fails its rule.
    """
    for key, validator in _SECTION_VALIDATORS.items():
        parts = key.split(".")
        node: Any = tree
        for part in parts:
            if not isinstance(node, dict) or part not in node:
                node = None
                break
            node = node[part]
        if isinstance(node, dict):
            validator(node)


# Deprecated dotted spellings, mapped to the flat key every deployer and the
# rest of the codebase actually reads. "apps.directory" used to be treated as
# a second, independent setting: wasm.web.machine's disk meter and the
# command the panel told an operator to run both addressed it, while every
# deployer read "apps_directory" - so the disk meter always reported the
# hard-coded default, no matter what the apps directory was really set to.
# Both Config.get and Config.set resolve an alias to its canonical key before
# doing anything else, so there is exactly one setting on disk and every
# reader agrees on it, whichever spelling wrote it.
#
# "logging.directory" is the same defect in miniature: obs/wasm.default.yaml
# shipped it while DEFAULT_CONFIG named the setting "logging.file", so a
# packaged install and the code disagreed about which key held the log
# location. A config.yaml written by that packaging keeps loading - and
# keeps meaning what it said - because it resolves here too.
KEY_ALIASES: dict[str, str] = {
    "apps.directory": "apps_directory",
    "logging.directory": "logging.file",
}


def _canonical_key(key: str) -> str:
    """
    Resolve a deprecated dotted alias to the key every reader actually uses.

    Args:
        key: The key exactly as a caller addressed it.

    Returns:
        The canonical key, or ``key`` unchanged when it is not an alias.
    """
    return KEY_ALIASES.get(key, key)


def _fold_aliases(tree: dict[str, Any]) -> dict[str, Any]:
    """
    Move a deprecated alias's value to its canonical key in a whole tree.

    :meth:`Config.get` and :meth:`Config.set` resolve a dotted alias before
    they ever look at ``self._config``, but :meth:`Config.replace` receives a
    full tree - such as ``{"apps": {"directory": "/srv/apps"}}`` - with no
    single dotted key to resolve. Without this, a full ``PUT`` written with
    the deprecated shape would store it as a container the canonical reader
    never looks at, silently reintroducing the split this alias exists to
    close.

    A canonical key already present in the tree wins over the alias, the same
    precedence a caller setting both in one request should expect from the
    more specific, current spelling. The canonical key itself may be nested
    (``"logging.directory"`` folds into ``"logging.file"``, not just into a
    flat top-level key), the same shape :meth:`Config.get` and
    :meth:`Config.set` already navigate.

    Args:
        tree: Configuration about to be stored. Not modified.

    Returns:
        A copy of ``tree`` with every alias folded into its canonical key and
        removed.
    """
    resolved = copy.deepcopy(tree)
    for alias, canonical in KEY_ALIASES.items():
        *parents, leaf = alias.split(".")
        containers: list[dict[str, Any]] = [resolved]
        node: Any = resolved
        for parent in parents:
            if not isinstance(node, dict) or parent not in node:
                node = None
                break
            node = node[parent]
            containers.append(node)
        if not isinstance(node, dict) or leaf not in node:
            continue

        value = node.pop(leaf)

        *canonical_parents, canonical_leaf = canonical.split(".")
        target = resolved
        for part in canonical_parents:
            if not isinstance(target.get(part), dict):
                target[part] = {}
            target = target[part]
        if canonical_leaf not in target:
            target[canonical_leaf] = value

        # An alias container left empty by the pop is not a setting either;
        # leaving it behind would still be a second, if empty, place the
        # value used to live. Pruned deepest first, in case that empties its
        # own parent in turn.
        for depth in range(len(parents) - 1, -1, -1):
            if containers[depth + 1]:
                break
            del containers[depth][parents[depth]]
    return resolved


#: Marks "this key has no default value to coerce against", for
#: :func:`coerce_config_value`. A key's own value cannot serve as that marker,
#: because a key can genuinely default to ``None``.
NO_DEFAULT = object()

#: Command line or PATCH words a boolean setting accepts, compared
#: case-insensitively.
TRUE_WORDS = frozenset({"1", "true", "yes", "on"})
FALSE_WORDS = frozenset({"0", "false", "no", "off"})


def coerce_config_value(existing: Any, raw: str) -> Any:
    """
    Parse a string value the way ``wasm config set`` and ``PATCH /api/config``
    both have to: against the type of the key's default, or, when there is no
    default, as a JSON scalar or list.

    Argv only ever hands a command a string, and a caller sending
    form-shaped data (everything a string, no JSON types) has the same
    problem: without this, ``wasm config set ssl.enabled false`` or a PATCH
    body ``{"path": "ssl.enabled", "value": "false"}`` stores the literal
    string ``"false"``, which is truthy. A key whose current or default value
    is a list also accepts a JSON array (``["a@example.com"]``) or a plain
    comma-separated value (``a@example.com,b@example.com``) without any extra
    flag, since a command line or a string field has no other way to shape
    one and typing brackets and quotes for every list-valued key would defeat
    the point of a default telling the coercion what shape to expect.

    A key with no default at all - ``existing`` is :data:`NO_DEFAULT` - has no
    type to match, so the value is parsed as a JSON scalar (``true``,
    ``false``, ``null``, a number) or a JSON array, and left as the plain
    string it looks like when it is not valid JSON or parses to something
    else, such as an object.

    Args:
        existing: Current or default value for the key, or :data:`NO_DEFAULT`
            when the key has none.
        raw: The value exactly as typed on the command line or received as a
            string over the API.

    Returns:
        The value cast to match ``existing``, a JSON scalar or list recovered
        from ``raw`` when there is no default to match, or ``raw`` unchanged
        when nothing else applies.

    Raises:
        ConfigError: When ``existing`` is a boolean or a number and ``raw``
            cannot be parsed as one.
    """
    if isinstance(existing, list):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, list):
            return parsed
        # Not JSON, or valid JSON that parsed to something other than a list
        # (a bare number, for instance): fall back to the same comma-separated
        # shape '--list' produces for a key with no default, so 'wasm config
        # set monitor.email_recipients a@x.com,b@y.com' stores a list instead
        # of the literal string - argv has no native list type, and a value
        # already shaped like one is the whole reason this key has a default
        # to coerce against in the first place.
        return [item.strip() for item in raw.split(",") if item.strip()]

    if isinstance(existing, bool):
        lowered = raw.strip().lower()
        if lowered in TRUE_WORDS:
            return True
        if lowered in FALSE_WORDS:
            return False
        raise ConfigError(
            f"Expected a boolean, got {raw!r}",
            details=f"Use one of: {', '.join(sorted(TRUE_WORDS | FALSE_WORDS))}.",
        )

    if isinstance(existing, int):
        try:
            return int(raw)
        except ValueError as exc:
            raise ConfigError(f"Expected a whole number, got {raw!r}") from exc

    if isinstance(existing, float):
        try:
            return float(raw)
        except ValueError as exc:
            raise ConfigError(f"Expected a number, got {raw!r}") from exc

    if existing is NO_DEFAULT:
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return raw
        if parsed is None or isinstance(parsed, (bool, int, float, list)):
            return parsed
        return raw

    return raw


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
        # A webhook URL is a capability URL: whoever holds it can post as this
        # server, and Slack and Discord embed the secret in the path. "url" on
        # its own is deliberately not a marker, because values like a
        # DATABASE_URL are shown with only their embedded password redacted
        # (see cli/commands/env.py) and would disappear wholesale otherwise.
        "webhook",
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


def is_secret_key(key: str) -> bool:
    """
    Report whether a configuration key's leaf name holds a secret.

    The public entry point to the same classification :func:`redact_secrets`
    uses, for a caller outside this module - ``wasm config set`` warning that
    a secret typed in argv lands in shell history and ``ps``, for one - that
    needs the answer without depending on a private name.

    Args:
        key: Configuration key name (not a dotted path; pass the leaf, such
            as ``"password"`` out of ``"monitor.smtp.password"``).

    Returns:
        True if the value behind this key must be redacted.
    """
    return _is_secret_key(key)


def redact_secrets(config: Any) -> Any:
    """
    Return a copy of a configuration structure with secrets replaced.

    Walks dictionaries and lists recursively and replaces the value of every
    key whose name matches :data:`SECRET_KEY_MARKERS` with :data:`REDACTED` -
    unless the value is already empty (``""`` or ``None``), which is left as
    it is. A client reading ``***`` for every unset secret alongside every
    configured one could not tell "the SMTP password is set" from "it never
    was", which is exactly the distinction a settings screen needs to show
    whether a notification channel is actually usable. An empty value is not
    a credential either way, so leaving it empty costs nothing: a save that
    does not touch the field round-trips the same empty string it was shown,
    the same as it always has.

    A container behind a secret key is walked instead of being replaced
    wholesale, so a ``credentials`` block keeps its user names and loses only
    its passwords. The input is not modified.

    Args:
        config: Configuration mapping, sequence or scalar to redact.

    Returns:
        A redacted deep copy of the input.
    """
    if isinstance(config, dict):
        return {
            key: (REDACTED if value not in ("", None) else value)
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
    #: Guards the whole check-and-set below, the load included. Without it,
    #: two threads racing to be first each pass the None check, each build
    #: their own instance and each parse config.yaml, and whichever
    #: assignment to ``_instance`` happens last is the one every later caller
    #: gets - the other instance, and its redundant read of the file, are
    #: simply discarded. Worse, the instance is published to ``_instance``
    #: before ``_load_config`` runs, so without the lock a second caller can
    #: receive a reference to it and read ``_config`` before it has been set.
    _lock = threading.Lock()

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
        with cls._lock:
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
                    _forget_blank_backup_directory(self._config)

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
        the caller's optimistic default. A key listed in :data:`KEY_ALIASES`,
        such as ``apps.directory``, reads its canonical key instead, so an
        alias always answers with what was actually stored.

        Args:
            key: Configuration key (supports dot notation for nested values).
            default: Default value if key is not found.

        Returns:
            Configuration value or default.
        """
        key = _canonical_key(key)

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

        A handful of keys carry the same rule
        :mod:`wasm.web.api.config` enforces through its own typed endpoints
        (``webserver``, ``backup.max_per_app``, ``web.port``,
        ``web.session_timeout``, the ``monitor.smtp`` fields and
        ``monitor.email_recipients``); a value one of them rejects is rejected
        here too, whichever front end called.

        A key listed in :data:`KEY_ALIASES`, such as ``apps.directory``, is
        written to its canonical key instead, so a deprecated spelling cannot
        create a second, independent setting that the rest of the codebase
        never reads.

        Args:
            key: Configuration key (supports dot notation).
            value: Value to set.

        Raises:
            ConfigError: When ``key`` has a shared rule and ``value`` fails it.
        """
        key = _canonical_key(key)

        if key in REMOVED_KEYS:
            logger.debug("Ignoring write to removed configuration key %s", key)
            return

        value = _validate_known_value(key, value)
        if isinstance(value, dict):
            # A whole section written at once ("backup" with a dict) must meet
            # the same per-key rules as writing its keys one by one, and any
            # rule spanning more than one key of that section (monitor.smtp's
            # use_ssl/use_tls).
            value = copy.deepcopy(value)
            wrapper: dict[str, Any] = value
            for part in reversed(key.split(".")):
                wrapper = {part: wrapper}
            _validate_known_values_in(wrapper)
            _validate_known_sections_in(wrapper)

        keys = key.split(".")
        config = self._config

        for k in keys[:-1]:
            if k not in config or not isinstance(config[k], dict):
                config[k] = {}
            config = config[k]

        leaf = keys[-1]
        resolved = restore_redacted({leaf: value}, {leaf: config.get(leaf)})[leaf]
        resolved = _strip_removed_under(key, resolved)

        # A rule spanning more than one key of the same container (again,
        # monitor.smtp's use_ssl/use_tls) must also catch a single leaf write
        # such as "monitor.smtp.use_tls" - the dict branch above only sees a
        # whole section written at once, and "wasm config set
        # monitor.smtp.use_tls true" never presents one. The section is
        # checked as it will read after this write, siblings included.
        container_key = ".".join(keys[:-1])
        section_validator = _SECTION_VALIDATORS.get(container_key)
        if section_validator is not None:
            prospective = dict(config)
            prospective[leaf] = resolved
            section_validator(prospective)

        config[leaf] = resolved

    def replace(self, config: dict[str, Any]) -> None:
        """
        Replace the whole configuration with a caller-supplied mapping.

        This is what a full update from the web panel goes through. Four
        things happen on the way in: a deprecated alias such as
        ``{"apps": {"directory": ...}}`` is folded into its canonical key,
        because a whole-tree ``PUT`` has no single dotted key for
        :meth:`get`/:meth:`set` to resolve; :data:`REDACTED` placeholders take
        the secret that is currently stored, because the panel only ever saw
        the redacted dump; removed settings are dropped, because a stale form
        must not be able to reintroduce them; and every key in
        :data:`_KEY_VALIDATORS` that is present is checked against the same
        rule :meth:`set` enforces. Without that last step a value ``wasm
        config set`` or ``PATCH /api/config`` would refuse - an unsupported
        web server, a relative apps directory - sailed through a full
        ``PUT /api/config`` untouched, because ``replace`` wrote the mapping
        straight in.

        Args:
            config: The new configuration.

        Raises:
            ConfigError: When a validated key is present with a value that
                fails its rule.
        """
        folded = _fold_aliases(config)
        resolved: dict[str, Any] = restore_redacted(folded, self._config)
        stripped = _strip_removed_keys(resolved)
        _validate_known_values_in(stripped)
        _validate_known_sections_in(stripped)
        self._config = stripped

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
    def backup_directory(self) -> Path:
        """
        The directory backups are written to.

        Returns:
            An absolute directory; the default when the setting is unset or
            empty.

        Raises:
            ConfigError: When the stored value is a relative path.
        """
        return resolve_backup_directory(self.get("backup.directory"))

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
