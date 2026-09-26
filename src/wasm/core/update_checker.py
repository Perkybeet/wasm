# Copyright (c) 2024-2025 Yago López Prado
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Update checker for WASM.

Checks for new versions on GitHub in the background without blocking the user's command.

The result is announced on stderr, and only to a person: never under
``--json`` and never when either stream is not a terminal. It was printed to
stdout after every command, which appended a banner to the JSON document of
``wasm app list --json | jq`` and broke the parse.
"""

import http.client
import json
import logging
import sys
import threading
import time
from collections.abc import Sequence
from pathlib import Path

from wasm import __version__
from wasm.core.exceptions import WASMError

logger = logging.getLogger(__name__)

#: The dotted key that turns the GitHub check off. An operator on an
#: airgapped or tightly firewalled server has no use for a request that can
#: only time out, repeated on every command.
CONFIG_KEY = "updates.check"


class UpdateChecker:
    """Check for WASM updates on GitHub."""

    CACHE_FILE = Path.home() / ".cache" / "wasm" / "version_check.json"
    CHECK_INTERVAL = 300  # 5 minutes
    TIMEOUT = 3  # Timeout for HTTP request
    GITHUB_API = "https://api.github.com/repos/Perkybeet/wasm/releases/latest"

    # Background check state
    _check_thread: threading.Thread | None = None
    _update_version: str | None = None

    @classmethod
    def enabled(cls) -> bool:
        """
        Report whether the operator has left the update check on.

        Reads the configuration rather than caching the answer: ``wasm
        config set updates.check false`` must take effect on the very next
        command, not the next restart of a long-lived process.

        Returns:
            True unless ``updates.check`` is set to false. A configuration
            that cannot be read must not turn a cosmetic check into a
            command that fails to start, so this defaults to enabled.
        """
        try:
            from wasm.core.config import Config

            return bool(Config().get(CONFIG_KEY, True))
        except OSError as exc:
            # Config._load_config already contains its own OSError/YAMLError
            # handling for a bad file; this is the belt for the one thing
            # left outside it - Path.exists() propagating a permission or
            # I/O error from an unreachable config directory (an NFS mount
            # gone away, for instance) instead of returning False.
            logger.debug(
                "Could not read %s: %s; treating the update check as enabled", CONFIG_KEY, exc
            )
            return True

    @staticmethod
    def should_announce(argv: Sequence[str]) -> bool:
        """
        Report whether this invocation may carry the update banner at all.

        Args:
            argv: The command line, without the program name.

        Returns:
            False under ``--json``, whose output is a document and nothing
            else, and when stdout or stderr is not a terminal: output being
            piped or captured is read by a program, and an ANSI-coloured
            banner is noise in a log file. True otherwise.
        """
        if "--json" in argv:
            return False
        return all(
            bool(getattr(stream, "isatty", lambda: False)()) for stream in (sys.stdout, sys.stderr)
        )

    @classmethod
    def start_background_check(cls):
        """
        Start update check in background thread.

        Call this at the beginning of command execution.
        The check runs in parallel while the command executes.
        """
        if not cls.enabled():
            cls._update_version = None
            return

        try:
            # First check cache synchronously (fast)
            if cls._is_cache_valid():
                cached_data = cls._read_cache()
                if cached_data and cached_data.get("has_update"):
                    cached_version = cached_data["latest_version"]
                    # Re-verify against current version (user may have updated)
                    if cls._is_newer_version(cached_version, __version__):
                        cls._update_version = cached_version
                return

            # Start background thread for network request
            cls._update_version = None
            cls._check_thread = threading.Thread(target=cls._background_check, daemon=True)
            cls._check_thread.start()
        except RuntimeError as exc:
            # "can't start new thread": the check is cosmetic, the command is not.
            logger.debug("Could not start the update check: %s", exc)

    @classmethod
    def _background_check(cls):
        """Perform the actual update check (runs in background thread)."""
        try:
            latest_version = cls._fetch_latest_version()

            if not latest_version:
                cls._write_cache(
                    {"latest_version": __version__, "has_update": False, "checked_at": time.time()}
                )
                return

            has_update = cls._is_newer_version(latest_version, __version__)

            cls._write_cache(
                {
                    "latest_version": latest_version,
                    "has_update": has_update,
                    "checked_at": time.time(),
                }
            )

            if has_update:
                cls._update_version = latest_version

        except Exception as exc:
            # The top of a daemon thread: nothing above it would catch this,
            # and an uncaught exception there is printed over the command's
            # own output. It is the one error boundary here, and it logs.
            logger.debug("Background update check failed: %s", exc, exc_info=True)

    @classmethod
    def show_update_if_available(cls, timeout: float = 0.1):
        """
        Show update message if check completed and update is available.

        Call this at the end of command execution.

        Args:
            timeout: Max seconds to wait for background check to complete.
        """
        # Wait briefly for background thread if still running
        if cls._check_thread and cls._check_thread.is_alive():
            cls._check_thread.join(timeout=timeout)

        if not cls._update_version:
            return

        latest, cls._update_version = cls._update_version, None
        try:
            cls._show_update_message(latest)
        except (OSError, UnicodeError, WASMError) as exc:
            # A reader that went away (`wasm ... | head`), a terminal that
            # cannot encode the banner, or a package manager probe that was
            # cancelled: none of them may change the command's exit.
            logger.debug("Could not announce update %s: %s", latest, exc)

    @classmethod
    def check_for_updates(cls):
        """
        Legacy method: Check for updates synchronously.

        Deprecated: Use start_background_check() and show_update_if_available() instead.
        """
        cls.start_background_check()
        # Wait for completion
        if cls._check_thread:
            cls._check_thread.join(timeout=cls.TIMEOUT + 1)
        cls.show_update_if_available(timeout=0)

    @classmethod
    def _fetch_latest_version(cls) -> str | None:
        """
        Fetch the latest version from GitHub API.

        Returns:
            Latest version string or None if failed.
        """
        import urllib.request

        req = urllib.request.Request(
            cls.GITHUB_API, headers={"Accept": "application/vnd.github.v3+json"}
        )

        try:
            # GITHUB_API is a module constant, not caller input.
            with urllib.request.urlopen(req, timeout=cls.TIMEOUT) as response:
                if response.status != 200:
                    return None
                data = json.loads(response.read().decode())
        except (OSError, http.client.HTTPException, ValueError) as exc:
            # OSError covers URLError, HTTPError (a 403 rate limit, a 404) and
            # timeouts; ValueError a body that is not UTF-8 JSON.
            logger.debug("Could not fetch the latest release: %s", exc)
            return None

        tag_name = data.get("tag_name") if isinstance(data, dict) else None
        if not isinstance(tag_name, str):
            logger.debug("The latest release carries no tag name: %r", data)
            return None
        # Remove 'v' prefix if present (e.g., "v0.13.11" -> "0.13.11")
        return tag_name.lstrip("v")

    @classmethod
    def _is_cache_valid(cls) -> bool:
        """
        Check if the cache is still valid (less than CHECK_INTERVAL old).

        Returns:
            True if cache is valid and should be used.
        """
        data = cls._read_cache()
        if not data:
            return False

        checked_at = data.get("checked_at")
        if not isinstance(checked_at, (int, float)):
            return False
        return time.time() - checked_at < cls.CHECK_INTERVAL

    @classmethod
    def _read_cache(cls) -> dict | None:
        """
        Read cached version check data.

        Returns:
            Cached data dict or None if failed.
        """
        try:
            if not cls.CACHE_FILE.exists():
                return None
            data = json.loads(cls.CACHE_FILE.read_text())
        except (OSError, ValueError) as exc:
            logger.debug("Could not read %s: %s", cls.CACHE_FILE, exc)
            return None
        return data if isinstance(data, dict) else None

    @classmethod
    def _write_cache(cls, data: dict):
        """
        Write version check data to cache.

        Args:
            data: Dictionary to cache.
        """
        try:
            # Create cache directory if it doesn't exist
            cls.CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)

            # Write cache file
            cls.CACHE_FILE.write_text(json.dumps(data, indent=2))

        except OSError as exc:
            # Permission errors, disk full, etc.
            logger.debug("Could not write %s: %s", cls.CACHE_FILE, exc)

    @classmethod
    def _is_newer_version(cls, remote: str, local: str) -> bool:
        """
        Compare semantic versions.

        Args:
            remote: Remote version string (e.g., "0.13.11").
            local: Local version string (e.g., "0.13.11").

        Returns:
            True if remote is newer than local.
        """
        try:
            # Parse versions into tuples of integers
            remote_parts = tuple(int(x) for x in remote.split("."))
            local_parts = tuple(int(x) for x in local.split("."))

            # Compare tuples (Python compares element by element)
            return remote_parts > local_parts

        except (ValueError, AttributeError):
            # Invalid version format
            return False

    @classmethod
    def _detect_installation_method(cls) -> str:
        """
        Detect how WASM was installed.

        Returns:
            Installation method: 'pip', 'pipx', 'apt', 'dnf', 'yum', 'zypper', or 'unknown'.
        """

        from wasm.core.runner import get_runner

        runner = get_runner()

        # Every probe is short: a package manager that does not answer in two
        # seconds is not going to, and this runs on the way to the command the
        # user actually asked for.
        probe = 2

        pipx_path = Path.home() / ".local" / "pipx" / "venvs" / "wasm-cli"
        if pipx_path.exists():
            return "pipx"

        # System package managers before pip. A distribution package installs
        # Python files that pip can also see, so asking pip first reports the
        # wrong answer and then offers the wrong upgrade command.
        if Path("/var/lib/dpkg/status").exists() and runner.run(
            ["dpkg", "-s", "wasm"], timeout=probe
        ):
            return "apt"

        for package_manager in ("dnf", "yum"):
            if runner.exists(package_manager) and runner.run(
                [package_manager, "list", "installed", "wasm-cli"], timeout=probe
            ):
                return package_manager

        if runner.exists("zypper"):
            result = runner.run(["zypper", "se", "-i", "wasm-cli"], timeout=probe)
            if result.success and "wasm-cli" in result.stdout:
                return "zypper"

        result = runner.run([sys.executable, "-m", "pip", "show", "wasm-cli"], timeout=probe)
        if result.success:
            if "Editable project location:" in result.stdout or "-e " in result.stdout:
                return "source"
            return "pip"

        return "unknown"

    @classmethod
    def _get_update_command(cls, method: str) -> str:
        """
        Get the appropriate update command for the installation method.

        Args:
            method: Installation method from _detect_installation_method.

        Returns:
            Update command string.
        """
        commands = {
            "pip": "pip install --upgrade wasm-cli",
            "pipx": "pipx upgrade wasm-cli",
            "apt": "sudo apt update && sudo apt install --only-upgrade wasm",
            "dnf": "sudo dnf upgrade wasm-cli",
            "yum": "sudo yum update wasm-cli",
            "zypper": "sudo zypper update wasm-cli",
            "source": "cd <wasm-repo> && git pull && pip install -e .",
            "unknown": "pip install --upgrade wasm-cli  # or use your system package manager",
        }
        return commands.get(method, commands["unknown"])

    @classmethod
    def _show_update_message(cls, latest_version: str):
        """
        Display update notification to user, on stderr.

        Args:
            latest_version: The latest available version.

        Raises:
            OSError: When stderr cannot be written.
            UnicodeError: When the terminal cannot encode the banner.
        """
        method = cls._detect_installation_method()
        update_command = cls._get_update_command(method)

        release_notes = f"https://github.com/Perkybeet/wasm/releases/tag/v{latest_version}"
        sys.stderr.write(
            f"\n\033[33m⚠  New version available: {latest_version} (current: {__version__})\033[0m\n"
            f"\033[33m   Update with: {update_command}\033[0m\n"
            f"\033[33m   Release notes: {release_notes}\033[0m\n\n"
        )
        sys.stderr.flush()
