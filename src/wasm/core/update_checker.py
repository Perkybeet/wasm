# Copyright (c) 2024-2025 Yago López Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
Update checker for WASM.

Checks for new versions on GitHub without blocking the user's command.
"""

import json
import logging
import time
from pathlib import Path
from typing import Optional

from wasm import __version__

logger = logging.getLogger(__name__)


class UpdateChecker:
    """Check for WASM updates on GitHub."""

    CACHE_FILE = Path.home() / ".wasm" / "version_check.json"
    CHECK_INTERVAL = 0  # Instant
    TIMEOUT = 3  # Timeout for HTTP request
    GITHUB_API = "https://api.github.com/repos/Perkybeet/wasm/releases/latest"

    @classmethod
    def check_for_updates(cls):
        """
        Check for updates and notify user if available.

        This method is non-blocking and fails silently on errors.
        Uses a cache to avoid checking too frequently.
        """
        try:
            # Check if we need to verify (cache expired)
            if cls._is_cache_valid():
                cached_data = cls._read_cache()
                if cached_data and cached_data.get("has_update"):
                    cls._show_update_message(cached_data["latest_version"])
                return

            # Fetch latest version from GitHub
            latest_version = cls._fetch_latest_version()

            if not latest_version:
                # Cache negative result to avoid repeated failures
                cls._write_cache({
                    "latest_version": __version__,
                    "has_update": False,
                    "checked_at": time.time()
                })
                return

            # Compare versions
            has_update = cls._is_newer_version(latest_version, __version__)

            # Save to cache
            cls._write_cache({
                "latest_version": latest_version,
                "has_update": has_update,
                "checked_at": time.time()
            })

            # Show message if update is available
            if has_update:
                cls._show_update_message(latest_version)

        except Exception as e:
            # Log but don't interrupt the user
            logger.debug(f"Failed to check for updates: {e}")

    @classmethod
    def _fetch_latest_version(cls) -> Optional[str]:
        """
        Fetch the latest version from GitHub API.

        Returns:
            Latest version string or None if failed.
        """
        try:
            import urllib.request
            import urllib.error

            req = urllib.request.Request(
                cls.GITHUB_API,
                headers={"Accept": "application/vnd.github.v3+json"}
            )

            with urllib.request.urlopen(req, timeout=cls.TIMEOUT) as response:
                if response.status == 200:
                    data = json.loads(response.read().decode())
                    tag_name = data.get("tag_name", "")
                    # Remove 'v' prefix if present (e.g., "v0.13.11" -> "0.13.11")
                    return tag_name.lstrip("v")

        except urllib.error.HTTPError:
            # Handle HTTP errors (404, 403 rate limit, etc.)
            pass
        except urllib.error.URLError:
            # Handle network errors (no internet, timeout, etc.)
            pass
        except json.JSONDecodeError:
            # Handle invalid JSON response
            pass
        except Exception:
            # Catch all other errors
            pass

        return None

    @classmethod
    def _is_cache_valid(cls) -> bool:
        """
        Check if the cache is still valid (less than CHECK_INTERVAL old).

        Returns:
            True if cache is valid and should be used.
        """
        try:
            if not cls.CACHE_FILE.exists():
                return False

            data = cls._read_cache()
            if not data or "checked_at" not in data:
                return False

            checked_at = data["checked_at"]
            elapsed = time.time() - checked_at

            return elapsed < cls.CHECK_INTERVAL

        except Exception:
            return False

    @classmethod
    def _read_cache(cls) -> Optional[dict]:
        """
        Read cached version check data.

        Returns:
            Cached data dict or None if failed.
        """
        try:
            if cls.CACHE_FILE.exists():
                content = cls.CACHE_FILE.read_text()
                return json.loads(content)
        except (json.JSONDecodeError, OSError, IOError):
            pass
        except Exception:
            pass

        return None

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

        except (OSError, IOError):
            # Permission errors, disk full, etc.
            pass
        except Exception:
            # Any other error
            pass

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
        except Exception:
            # Any other parsing error
            return False

    @classmethod
    def _detect_installation_method(cls) -> str:
        """
        Detect how WASM was installed.

        Returns:
            Installation method: 'pip', 'pipx', 'apt', 'dnf', 'yum', 'zypper', or 'unknown'.
        """
        import subprocess
        import sys

        try:
            # Check pipx first (most specific)
            pipx_path = Path.home() / ".local" / "pipx" / "venvs" / "wasm-cli"
            if pipx_path.exists():
                return "pipx"

            # Check if installed with pip in current Python environment
            result = subprocess.run(
                [sys.executable, "-m", "pip", "show", "wasm-cli"],
                capture_output=True,
                text=True,
                timeout=2
            )
            if result.returncode == 0:
                # Check if installed in editable mode (from source)
                if "Editable project location:" in result.stdout or "-e " in result.stdout:
                    return "source"
                return "pip"

            # Check system package managers
            # APT (Ubuntu, Debian) - package is called "wasm" not "wasm-cli"
            if Path("/var/lib/dpkg/status").exists():
                result = subprocess.run(
                    ["dpkg", "-l", "wasm"],
                    capture_output=True,
                    timeout=2
                )
                if result.returncode == 0 and b"wasm" in result.stdout:
                    return "apt"

            # DNF/YUM (Fedora, RHEL, CentOS)
            for pkg_manager in ["dnf", "yum"]:
                try:
                    result = subprocess.run(
                        [pkg_manager, "list", "installed", "wasm-cli"],
                        capture_output=True,
                        timeout=2
                    )
                    if result.returncode == 0:
                        return pkg_manager
                except FileNotFoundError:
                    continue

            # Zypper (openSUSE)
            try:
                result = subprocess.run(
                    ["zypper", "se", "-i", "wasm-cli"],
                    capture_output=True,
                    timeout=2
                )
                if result.returncode == 0 and b"wasm-cli" in result.stdout:
                    return "zypper"
            except FileNotFoundError:
                pass

        except Exception as e:
            logger.debug(f"Failed to detect installation method: {e}")

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
            "unknown": "pip install --upgrade wasm-cli  # or use your system package manager"
        }
        return commands.get(method, commands["unknown"])

    @classmethod
    def _show_update_message(cls, latest_version: str):
        """
        Display update notification to user.

        Args:
            latest_version: The latest available version.
        """
        try:
            method = cls._detect_installation_method()
            update_command = cls._get_update_command(method)

            print(f"\n\033[33m⚠  New version available: {latest_version} (current: {__version__})\033[0m")
            print(f"\033[33m   Update with: {update_command}\033[0m")
            print(f"\033[33m   Release notes: https://github.com/Perkybeet/wasm/releases/tag/v{latest_version}\033[0m\n")
        except Exception:
            # Even printing can fail in some edge cases
            pass
