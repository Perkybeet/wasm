# Copyright (c) 2024-2025 Yago López Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
Package manager helper for deployers.

Handles detection, verification, and command generation for
Node.js package managers (npm, pnpm, yarn, bun).
"""

from pathlib import Path
from typing import List, Literal, Optional

from wasm.core.exceptions import DeploymentError
from wasm.core.logger import Logger
from wasm.core.utils import command_exists


PackageManager = Literal["npm", "pnpm", "bun", "yarn", "auto"]


class PackageManagerHelper:
    """
    Helper for package manager operations.

    Provides detection, verification, and command generation for
    Node.js package managers.
    """

    def __init__(self, logger: Optional[Logger] = None):
        """
        Initialize package manager helper.

        Args:
            logger: Logger instance for output.
        """
        self.logger = logger or Logger()

    def detect(self, app_path: Path, requested: PackageManager = "auto") -> str:
        """
        Detect the package manager used in the project.

        Args:
            app_path: Path to the application directory.
            requested: Requested package manager (auto for detection).

        Returns:
            Detected package manager name.
        """
        if requested != "auto":
            return requested

        if not app_path or not app_path.exists():
            return "npm"

        # Check for lock files
        if (app_path / "pnpm-lock.yaml").exists():
            return "pnpm"
        elif (app_path / "bun.lockb").exists():
            return "bun"
        elif (app_path / "yarn.lock").exists():
            return "yarn"

        return "npm"

    def verify(self, package_manager: str) -> str:
        """
        Verify the package manager is installed and available.

        Falls back to an available package manager if the requested
        one is not installed.

        Args:
            package_manager: Requested package manager.

        Returns:
            Available package manager (may differ from requested).

        Raises:
            DeploymentError: If no package manager is available.
        """
        if command_exists(package_manager):
            return package_manager

        # Requested PM not available, check what is available
        available = self.get_available()

        if not available:
            raise DeploymentError(
                "No package manager available",
                details=(
                    "No Node.js package manager (npm, pnpm, yarn, bun) is installed.\n\n"
                    "To fix this, run the setup wizard:\n"
                    "  sudo wasm setup init\n\n"
                    "Or install Node.js manually which includes npm:\n"
                    "  curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -\n"
                    "  sudo apt install -y nodejs"
                )
            )

        # Fall back to first available package manager
        fallback = available[0]
        self.logger.warning(
            f"Package manager '{package_manager}' not installed. "
            f"Using '{fallback}' instead."
        )
        self.logger.info(f"Available package managers: {', '.join(available)}")

        return fallback

    def get_available(self) -> List[str]:
        """
        Get list of available package managers.

        Returns:
            List of installed package manager names.
        """
        available = []
        for pm in ["npm", "pnpm", "yarn", "bun"]:
            if command_exists(pm):
                available.append(pm)
        return available

    def get_install_command(self, package_manager: str) -> List[str]:
        """
        Get the install command for the package manager.

        Args:
            package_manager: Package manager name.

        Returns:
            Install command as list.
        """
        commands = {
            "pnpm": ["pnpm", "install", "--frozen-lockfile"],
            "bun": ["bun", "install", "--frozen-lockfile"],
            "yarn": ["yarn", "install", "--frozen-lockfile"],
            "npm": ["npm", "ci"],
        }
        return commands.get(package_manager, ["npm", "ci"])

    def get_run_command(self, package_manager: str, script: str) -> List[str]:
        """
        Get the run command for a script.

        Args:
            package_manager: Package manager name.
            script: Script name to run.

        Returns:
            Run command as list.
        """
        if package_manager == "pnpm":
            return ["pnpm", "run", script]
        elif package_manager == "bun":
            return ["bun", "run", script]
        elif package_manager == "yarn":
            return ["yarn", script]
        else:
            return ["npm", "run", script]

    def get_exec_command(self, package_manager: str, command: str) -> List[str]:
        """
        Get the exec/dlx command for running a binary.

        Args:
            package_manager: Package manager name.
            command: Command to execute.

        Returns:
            Exec command as list.
        """
        cmd_parts = command.split()

        if package_manager == "pnpm":
            return ["pnpm", "exec"] + cmd_parts
        elif package_manager == "bun":
            return ["bunx"] + cmd_parts
        elif package_manager == "yarn":
            return ["yarn"] + cmd_parts
        else:
            return ["npx"] + cmd_parts
