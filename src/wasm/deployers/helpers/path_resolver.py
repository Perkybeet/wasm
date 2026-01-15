# Copyright (c) 2024-2025 Yago López Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
Path resolver helper for deployers.

Handles resolution of absolute paths for executables, with special
handling for systemd services that require global system paths.
"""

import os
import shlex
import shutil
from typing import Optional

from wasm.core.logger import Logger


class PathResolver:
    """
    Helper for resolving executable paths.

    Handles resolution of absolute paths for executables, preferring
    global system paths over user-specific installations (like nvm)
    to ensure compatibility with systemd services.
    """

    # System paths to search (in order of preference)
    SYSTEM_PATHS = [
        "/usr/bin",
        "/usr/local/bin",
        "/bin",
        "/usr/sbin",
        "/usr/local/sbin",
        "/sbin",
        "/snap/bin",
    ]

    # Patterns indicating private/user-specific paths
    PRIVATE_PATTERNS = [
        "/root/",
        "/.nvm/",
        "/.local/",
        "/.npm/",
        "/.yarn/",
        "/.bun/",
    ]

    def __init__(self, logger: Optional[Logger] = None):
        """
        Initialize path resolver.

        Args:
            logger: Logger instance for output.
        """
        self.logger = logger or Logger()

    def is_private_path(self, path: str) -> bool:
        """
        Check if path is in a private/user-specific directory.

        Private paths (like /root/.nvm or /home/user/.nvm) are not
        accessible by systemd services running as non-root users.

        Args:
            path: Absolute path to check.

        Returns:
            True if path is in a private directory.
        """
        # Check for home directories (e.g., /home/user/.nvm/)
        if path.startswith("/home/"):
            parts = path.split("/")
            if len(parts) > 3:
                # Check if it's a hidden directory in user's home
                for part in parts[3:]:
                    if part.startswith("."):
                        return True

        # Check for known private patterns
        for pattern in self.PRIVATE_PATTERNS:
            if pattern in path:
                return True

        return False

    def find_global_executable(self, executable: str) -> Optional[str]:
        """
        Find executable in global/system paths only.

        Searches common system paths for the executable, avoiding
        user-specific installations like nvm.

        Args:
            executable: Name of executable to find.

        Returns:
            Absolute path if found in system paths, None otherwise.
        """
        for sys_path in self.SYSTEM_PATHS:
            candidate = os.path.join(sys_path, executable)
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                return candidate

        return None

    def resolve_command(self, command: str) -> str:
        """
        Resolve command to use absolute paths for executables.

        This is required for systemd services as ExecStart
        must use absolute paths. Prefers global system paths
        over user-specific installations (like nvm) to avoid
        permission issues with systemd services.

        Args:
            command: Command string (e.g., "npm run start")

        Returns:
            Command with absolute path (e.g., "/usr/bin/npm run start")
        """
        try:
            parts = shlex.split(command)
        except ValueError as e:
            self.logger.warning(f"Could not parse command '{command}': {e}")
            parts = command.split()

        if not parts:
            return command

        executable = parts[0]

        # Already absolute - check if it's a private path
        if executable.startswith("/"):
            if self.is_private_path(executable):
                # Try to find a global alternative
                base_name = executable.split("/")[-1]
                global_path = self.find_global_executable(base_name)
                if global_path:
                    self.logger.warning(
                        f"Executable '{executable}' is in a private directory. "
                        f"Using system path: {global_path}"
                    )
                    parts[0] = global_path
                    return " ".join(parts)
                else:
                    self.logger.warning(
                        f"Executable '{executable}' is in a private directory "
                        f"and no system alternative found. "
                        f"The service may fail with 'Permission denied'."
                    )
            return command

        # First, try to find in global system paths
        global_path = self.find_global_executable(executable)
        if global_path:
            parts[0] = global_path
            return " ".join(parts)

        # Fallback to shutil.which (includes PATH from current environment)
        abs_path = shutil.which(executable)
        if abs_path:
            # Check if the found path is in a private directory
            if self.is_private_path(abs_path):
                self.logger.warning(
                    f"Package manager '{executable}' found at '{abs_path}' "
                    f"which is in a private directory (e.g., nvm installation)."
                )
                self.logger.warning(
                    f"The systemd service runs as a non-root user and won't be able "
                    f"to access this path."
                )
                self.logger.info(
                    f"To fix this, install Node.js/npm globally:\n"
                    f"  curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -\n"
                    f"  sudo apt install -y nodejs\n"
                    f"Or for pnpm: sudo npm install -g pnpm"
                )
            parts[0] = abs_path
            return " ".join(parts)

        # Fallback: return original (systemd will fail, but error will be clearer)
        self.logger.warning(
            f"Could not find absolute path for '{executable}'. "
            f"Service may fail to start."
        )
        return command
