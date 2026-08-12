# Copyright (c) 2024-2025 Yago López Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
Package manager helper for deployers.

Handles detection, verification, and command generation for
Node.js package managers (npm, pnpm, yarn, bun).

Availability is asked of the injected :class:`~wasm.core.runner.CommandRunner`,
not of the process PATH. Reading the PATH directly made the answer depend on
whichever machine happened to run the code, which is untestable and, worse,
meant a deploy could silently substitute one package manager for another.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from wasm.core.exceptions import DeploymentError
from wasm.core.logger import Logger
from wasm.core.runner import CommandRunner, get_runner

PackageManager = Literal["npm", "pnpm", "bun", "yarn", "auto"]


class PackageManagerHelper:
    """
    Helper for package manager operations.

    Provides detection, verification, and command generation for
    Node.js package managers.
    """

    #: Lock files, in the order that decides which manager a project uses.
    LOCK_FILES: tuple[tuple[str, str], ...] = (
        ("pnpm-lock.yaml", "pnpm"),
        ("pnpm-workspace.yaml", "pnpm"),
        ("bun.lockb", "bun"),
        ("yarn.lock", "yarn"),
    )

    def __init__(self, logger: Logger | None = None, runner: CommandRunner | None = None):
        """
        Initialize package manager helper.

        Args:
            logger: Logger instance for output.
            runner: Runner used to ask whether a program is installed. Defaults
                to the process-wide one.
        """
        self.logger = logger or Logger()
        self._runner = runner

    @property
    def runner(self) -> CommandRunner:
        """The runner availability is asked of."""
        return self._runner if self._runner is not None else get_runner()

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

    def verify(self, package_manager: str, *, negotiable: bool = True) -> str:
        """
        Check the package manager is installed, or explain what to do.

        Args:
            package_manager: The manager the project needs.
            negotiable: Whether another manager would do. False for a workspace
                that only one manager can install, where substituting produces
                a broken tree rather than a different one.

        Returns:
            The manager to use. Differs from the request only when the request
            was negotiable and unavailable.

        Raises:
            DeploymentError: When the manager is missing and no substitute is
                acceptable, or when none is installed at all.
        """
        if self.runner.exists(package_manager):
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
                ),
            )

        # Falling back is only safe when nothing in the project asked for a
        # particular manager. A repository carrying pnpm-lock.yaml installed
        # with npm resolves a different dependency tree than the one that was
        # tested, and the failure shows up at runtime as something unrelated.
        fallback = available[0]
        if not negotiable:
            raise DeploymentError(
                f"This project needs {package_manager}, which is not installed",
                details=(
                    f"Its workspace layout is {package_manager}'s and no other package "
                    f"manager can install it: the dependency links it declares only "
                    f"resolve under {package_manager}.\n\n"
                    f"Install it:\n  npm install -g {package_manager}"
                ),
            )

        if package_manager != "npm":
            raise DeploymentError(
                f"This project needs {package_manager}, which is not installed",
                details=(
                    f"Its lock file is {package_manager}'s, and installing it with "
                    f"{fallback} resolves a different dependency tree than the one the "
                    f"project was tested with.\n\n"
                    f"Install it:\n  npm install -g {package_manager}\n\n"
                    f"Or pass --pm {fallback} to accept the substitution."
                ),
            )

        self.logger.warning(
            f"Package manager '{package_manager}' not installed. Using '{fallback}' instead."
        )
        self.logger.info(f"Available package managers: {', '.join(available)}")
        return fallback

    def get_available(self) -> list[str]:
        """
        Get list of available package managers.

        Returns:
            List of installed package manager names.
        """
        available = []
        for pm in ["npm", "pnpm", "yarn", "bun"]:
            if self.runner.exists(pm):
                available.append(pm)
        return available

    def get_install_command(self, package_manager: str) -> list[str]:
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

    def get_run_command(self, package_manager: str, script: str) -> list[str]:
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

    def get_exec_command(self, package_manager: str, command: str) -> list[str]:
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
            return ["pnpm", "exec", *cmd_parts]
        elif package_manager == "bun":
            return ["bunx", *cmd_parts]
        elif package_manager == "yarn":
            return ["yarn", *cmd_parts]
        else:
            return ["npx", *cmd_parts]
