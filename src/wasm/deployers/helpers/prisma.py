# Copyright (c) 2024-2025 Yago López Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
Prisma ORM helper for deployers.

Handles detection and setup of Prisma in Node.js applications.
"""

import json
from pathlib import Path
from typing import Callable, List, Optional

from wasm.core.logger import Logger


class PrismaHelper:
    """
    Helper for Prisma ORM operations.

    Provides detection and setup functionality for applications
    using Prisma as their ORM.
    """

    def __init__(
        self,
        logger: Optional[Logger] = None,
        run_command: Optional[Callable] = None,
        get_exec_command: Optional[Callable] = None,
    ):
        """
        Initialize Prisma helper.

        Args:
            logger: Logger instance for output.
            run_command: Function to run commands.
            get_exec_command: Function to get package manager exec command.
        """
        self.logger = logger or Logger()
        self._run_command = run_command
        self._get_exec_command = get_exec_command

    def detect(self, app_path: Path) -> bool:
        """
        Detect if project uses Prisma ORM.

        Args:
            app_path: Path to the application directory.

        Returns:
            True if Prisma is detected.
        """
        if not app_path or not app_path.exists():
            return False

        # Check for prisma directory
        if (app_path / "prisma").exists():
            return True

        # Check package.json for prisma
        package_json = app_path / "package.json"
        if package_json.exists():
            try:
                with open(package_json) as f:
                    pkg = json.load(f)
                    deps = pkg.get("dependencies", {})
                    dev_deps = pkg.get("devDependencies", {})
                    if "@prisma/client" in deps or "prisma" in dev_deps:
                        return True
            except (json.JSONDecodeError, OSError) as e:
                self.logger.debug(f"Failed to read package.json for Prisma detection: {e}")

        return False

    def generate(self, app_path: Path) -> bool:
        """
        Generate Prisma client.

        Args:
            app_path: Path to the application directory.

        Returns:
            True if successful or not needed.
        """
        if not self._run_command or not self._get_exec_command:
            self.logger.warning("Prisma generate skipped: no command runner configured")
            return True

        self.logger.substep("Generating Prisma client")

        command = self._get_exec_command("prisma generate")
        result = self._run_command(command, cwd=app_path, timeout=120)

        if not result.success:
            self.logger.warning(f"Prisma generate failed: {result.stderr}")
            # Don't fail the whole deployment for this
            return True

        return True

    def migrate(self, app_path: Path, deploy: bool = True) -> bool:
        """
        Run Prisma migrations.

        Args:
            app_path: Path to the application directory.
            deploy: If True, run deploy (production), else run dev.

        Returns:
            True if successful.
        """
        if not self._run_command or not self._get_exec_command:
            self.logger.warning("Prisma migrate skipped: no command runner configured")
            return True

        if deploy:
            self.logger.substep("Running Prisma migrations (deploy)")
            command = self._get_exec_command("prisma migrate deploy")
        else:
            self.logger.substep("Running Prisma migrations (dev)")
            command = self._get_exec_command("prisma migrate dev")

        result = self._run_command(command, cwd=app_path, timeout=300)

        if not result.success:
            self.logger.warning(f"Prisma migrate failed: {result.stderr}")
            return False

        return True
