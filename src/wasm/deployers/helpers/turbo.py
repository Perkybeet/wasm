# Copyright (c) 2024-2025 Yago Lopez Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
Turborepo helper for monorepo deployers.

Handles parsing of turbo.json configuration and provides build
pipeline information for monorepo deployments.
"""

import json
from pathlib import Path
from typing import Dict, List, Optional, Set

from wasm.core.logger import Logger


class TurboHelper:
    """
    Helper for Turborepo configuration analysis.

    Provides parsing of turbo.json, build pipeline detection,
    and dependency order calculation.
    """

    def __init__(self, logger: Optional[Logger] = None):
        """
        Initialize Turborepo helper.

        Args:
            logger: Logger instance for output.
        """
        self.logger = logger or Logger()
        self._config: Optional[Dict] = None
        self._config_path: Optional[Path] = None

    def detect(self, app_path: Path) -> bool:
        """
        Detect if the project uses Turborepo.

        Args:
            app_path: Root path of the project.

        Returns:
            True if Turborepo is detected.
        """
        turbo_json = app_path / "turbo.json"
        if turbo_json.exists():
            return True

        # Also check package.json for turbo field
        package_json = app_path / "package.json"
        if package_json.exists():
            try:
                with open(package_json) as f:
                    pkg = json.load(f)
                    return "turbo" in pkg
            except (json.JSONDecodeError, OSError):
                pass

        return False

    def load_config(self, app_path: Path) -> Dict:
        """
        Load and parse turbo.json configuration.

        Args:
            app_path: Root path of the project.

        Returns:
            Parsed configuration dictionary.
        """
        if self._config and self._config_path == app_path:
            return self._config

        turbo_json = app_path / "turbo.json"

        if not turbo_json.exists():
            self.logger.debug("No turbo.json found, using defaults")
            self._config = self._default_config()
            self._config_path = app_path
            return self._config

        try:
            with open(turbo_json) as f:
                self._config = json.load(f)
                self._config_path = app_path
                return self._config
        except (json.JSONDecodeError, OSError) as e:
            self.logger.warning(f"Error reading turbo.json: {e}")
            self._config = self._default_config()
            self._config_path = app_path
            return self._config

    def _default_config(self) -> Dict:
        """
        Return default Turborepo configuration.

        Returns:
            Default config dictionary.
        """
        return {
            "tasks": {
                "build": {
                    "dependsOn": ["^build"],
                    "outputs": [".next/**", "dist/**"],
                },
                "dev": {
                    "cache": False,
                    "persistent": True,
                },
                "start": {
                    "dependsOn": ["build"],
                    "cache": False,
                },
            }
        }

    def get_build_command(self, app_path: Path) -> List[str]:
        """
        Get the build command for the monorepo.

        Args:
            app_path: Root path of the project.

        Returns:
            Build command as list of strings.
        """
        config = self.load_config(app_path)

        # Check if build task exists
        tasks = config.get("tasks", config.get("pipeline", {}))
        has_build = "build" in tasks

        if has_build:
            return ["pnpm", "build"]

        # Fallback to turbo run build
        return ["pnpm", "exec", "turbo", "run", "build"]

    def get_build_outputs(self, app_path: Path) -> List[str]:
        """
        Get expected build output directories.

        Args:
            app_path: Root path of the project.

        Returns:
            List of output directory patterns.
        """
        config = self.load_config(app_path)
        tasks = config.get("tasks", config.get("pipeline", {}))

        build_config = tasks.get("build", {})
        outputs = build_config.get("outputs", [])

        # Default outputs if not specified
        if not outputs:
            outputs = [".next/**", "dist/**", "build/**"]

        return outputs

    def get_task_dependencies(self, task_name: str, app_path: Path) -> List[str]:
        """
        Get dependencies for a specific task.

        Args:
            task_name: Name of the task (e.g., "build", "start").
            app_path: Root path of the project.

        Returns:
            List of dependent task names.
        """
        config = self.load_config(app_path)
        tasks = config.get("tasks", config.get("pipeline", {}))

        task_config = tasks.get(task_name, {})
        depends_on = task_config.get("dependsOn", [])

        # Filter out topological dependencies (^)
        direct_deps = [d.lstrip("^") for d in depends_on if not d.startswith("$")]

        return direct_deps

    def get_global_dependencies(self, app_path: Path) -> List[str]:
        """
        Get global dependencies that affect all tasks.

        These are files/directories that, when changed, should
        invalidate all caches.

        Args:
            app_path: Root path of the project.

        Returns:
            List of global dependency patterns.
        """
        config = self.load_config(app_path)

        # Turbo 2.x format
        global_deps = config.get("globalDependencies", [])

        # Common global dependencies
        if not global_deps:
            global_deps = [
                "pnpm-lock.yaml",
                "package.json",
                "tsconfig.json",
            ]

        return global_deps

    def get_env_vars(self, app_path: Path) -> Set[str]:
        """
        Get environment variables that should be considered for builds.

        Args:
            app_path: Root path of the project.

        Returns:
            Set of environment variable names.
        """
        config = self.load_config(app_path)

        env_vars = set()

        # Global env (Turbo 2.x)
        global_env = config.get("globalEnv", [])
        env_vars.update(global_env)

        # Task-specific env
        tasks = config.get("tasks", config.get("pipeline", {}))
        for task_config in tasks.values():
            if isinstance(task_config, dict):
                task_env = task_config.get("env", [])
                env_vars.update(task_env)

        return env_vars

    def is_cacheable(self, task_name: str, app_path: Path) -> bool:
        """
        Check if a task should be cached.

        Args:
            task_name: Name of the task.
            app_path: Root path of the project.

        Returns:
            True if the task is cacheable.
        """
        config = self.load_config(app_path)
        tasks = config.get("tasks", config.get("pipeline", {}))

        task_config = tasks.get(task_name, {})

        # Cache is enabled by default unless explicitly disabled
        return task_config.get("cache", True) is not False

    def get_filter_args(self, workspaces: List[str]) -> List[str]:
        """
        Generate filter arguments for turbo to only build specific workspaces.

        Args:
            workspaces: List of workspace names to include.

        Returns:
            List of filter arguments for turbo command.
        """
        if not workspaces:
            return []

        args = []
        for workspace in workspaces:
            args.extend(["--filter", workspace])

        return args

    def estimate_build_timeout(self, app_path: Path, workspace_count: int) -> int:
        """
        Estimate build timeout based on project size.

        Args:
            app_path: Root path of the project.
            workspace_count: Number of workspaces to build.

        Returns:
            Recommended timeout in seconds.
        """
        # Base timeout: 5 minutes
        base_timeout = 300

        # Add 3 minutes per workspace
        workspace_timeout = workspace_count * 180

        # Cap at 30 minutes
        return min(base_timeout + workspace_timeout, 1800)

    def get_parallel_count(self) -> Optional[int]:
        """
        Get recommended parallelization count for builds.

        Returns:
            Number of parallel builds, or None for default.
        """
        # Let Turborepo decide based on available CPUs
        return None

    def validate_config(self, app_path: Path) -> List[str]:
        """
        Validate turbo.json configuration.

        Args:
            app_path: Root path of the project.

        Returns:
            List of validation warnings (empty if valid).
        """
        warnings = []
        config = self.load_config(app_path)

        # Check for deprecated pipeline key
        if "pipeline" in config and "tasks" not in config:
            warnings.append(
                "turbo.json uses deprecated 'pipeline' key. "
                "Consider upgrading to 'tasks' (Turbo 2.x)."
            )

        # Check for build task
        tasks = config.get("tasks", config.get("pipeline", {}))
        if "build" not in tasks:
            warnings.append(
                "No 'build' task defined in turbo.json. "
                "Build command may not work as expected."
            )

        return warnings
