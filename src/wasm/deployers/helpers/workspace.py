# Copyright (c) 2024-2025 Yago Lopez Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
Workspace helper for monorepo deployers.

Handles detection and parsing of pnpm workspaces, analyzing individual
apps within a monorepo structure.
"""

import json
from pathlib import Path
from typing import Dict, List, Optional

from wasm.core.exceptions import DeploymentError
from wasm.core.logger import Logger
from wasm.core.store import MonorepoWorkspace, AppType


class WorkspaceHelper:
    """
    Helper for workspace analysis in monorepos.

    Provides detection of workspace apps, port extraction, and
    start command analysis for Turborepo/pnpm monorepos.
    """

    # Mapping of framework indicators to app types
    FRAMEWORK_INDICATORS = {
        "nextjs": ["next", "@next/core"],
        "nodejs": ["express", "fastify", "koa", "@nestjs/core", "hono"],
        "vite": ["vite", "@vitejs/plugin-react"],
        "python": [],  # Detected by files, not package.json
    }

    # Default ports by app type
    DEFAULT_PORTS = {
        "nextjs": 3001,
        "nodejs": 3000,
        "vite": 4173,
        "static": 80,
    }

    def __init__(self, logger: Optional[Logger] = None):
        """
        Initialize workspace helper.

        Args:
            logger: Logger instance for output.
        """
        self.logger = logger or Logger()

    def parse_pnpm_workspace(self, app_path: Path) -> List[str]:
        """
        Parse pnpm-workspace.yaml to find workspace patterns.

        Args:
            app_path: Root path of the monorepo.

        Returns:
            List of workspace glob patterns.
        """
        workspace_file = app_path / "pnpm-workspace.yaml"
        if not workspace_file.exists():
            return []

        try:
            import yaml
            with open(workspace_file) as f:
                config = yaml.safe_load(f)
                return config.get("packages", [])
        except ImportError:
            # Fallback: basic parsing without yaml library
            patterns = []
            with open(workspace_file) as f:
                in_packages = False
                for line in f:
                    line = line.strip()
                    if line == "packages:":
                        in_packages = True
                    elif in_packages and line.startswith("- "):
                        pattern = line[2:].strip().strip("'\"")
                        patterns.append(pattern)
                    elif in_packages and not line.startswith("-") and line:
                        in_packages = False
            return patterns
        except Exception as e:
            self.logger.debug(f"Error parsing pnpm-workspace.yaml: {e}")
            return []

    def discover_apps(self, app_path: Path) -> List[Path]:
        """
        Discover deployable apps in the monorepo.

        Looks in apps/ directory for directories containing package.json
        or other indicators of deployable applications.

        Args:
            app_path: Root path of the monorepo.

        Returns:
            List of paths to discovered apps.
        """
        apps_dir = app_path / "apps"
        if not apps_dir.exists():
            raise DeploymentError(
                "No apps directory found",
                details=(
                    "Monorepo must have an 'apps/' directory containing "
                    "deployable applications.\n\n"
                    "Expected structure:\n"
                    "  monorepo/\n"
                    "    apps/\n"
                    "      frontend/\n"
                    "      backend/\n"
                    "    packages/\n"
                    "    turbo.json"
                )
            )

        apps = []
        for item in apps_dir.iterdir():
            if not item.is_dir():
                continue

            # Check for deployable indicators
            has_package_json = (item / "package.json").exists()
            has_requirements = (item / "requirements.txt").exists()
            has_pyproject = (item / "pyproject.toml").exists()

            if has_package_json or has_requirements or has_pyproject:
                apps.append(item)

        if not apps:
            raise DeploymentError(
                "No deployable apps found in apps/ directory",
                details=(
                    "Each app in apps/ must have a package.json, "
                    "requirements.txt, or pyproject.toml"
                )
            )

        return sorted(apps, key=lambda p: p.name)

    def detect_app_type(self, app_dir: Path) -> str:
        """
        Detect the type of application in a workspace directory.

        Args:
            app_dir: Path to the workspace app directory.

        Returns:
            Detected app type string.
        """
        package_json = app_dir / "package.json"

        # Check for Python apps first
        if (app_dir / "requirements.txt").exists() or \
           (app_dir / "pyproject.toml").exists():
            return AppType.PYTHON.value

        if not package_json.exists():
            return AppType.UNKNOWN.value

        try:
            with open(package_json) as f:
                pkg = json.load(f)

            deps = pkg.get("dependencies", {})
            dev_deps = pkg.get("devDependencies", {})
            all_deps = {**deps, **dev_deps}

            # Check for Next.js
            if "next" in all_deps:
                return AppType.NEXTJS.value

            # Check for Vite
            if "vite" in all_deps:
                return AppType.VITE.value

            # Check for Node.js frameworks (NestJS, Express, etc.)
            nodejs_indicators = ["express", "fastify", "koa", "@nestjs/core", "hono"]
            for indicator in nodejs_indicators:
                if indicator in all_deps:
                    return AppType.NODEJS.value

            # Fallback: if has main or scripts.start, consider it nodejs
            if pkg.get("main") or pkg.get("scripts", {}).get("start"):
                return AppType.NODEJS.value

            return AppType.UNKNOWN.value

        except (json.JSONDecodeError, OSError) as e:
            self.logger.debug(f"Error reading package.json: {e}")
            return AppType.UNKNOWN.value

    def extract_port(self, app_dir: Path, app_type: str) -> int:
        """
        Extract or determine the port for an app.

        Checks package.json scripts for port specifications, then
        falls back to defaults based on app type.

        Args:
            app_dir: Path to the workspace app directory.
            app_type: Detected app type.

        Returns:
            Port number for the app.
        """
        package_json = app_dir / "package.json"

        if package_json.exists():
            try:
                with open(package_json) as f:
                    pkg = json.load(f)

                scripts = pkg.get("scripts", {})

                # Check start script for port specification
                start_script = scripts.get("start:prod") or \
                               scripts.get("start:production") or \
                               scripts.get("start", "")

                # Look for --port or -p flags
                import re
                port_match = re.search(r'(?:--port|-p)\s*[=\s]?\s*(\d+)', start_script)
                if port_match:
                    return int(port_match.group(1))

                # Look for PORT= in the script
                env_port_match = re.search(r'PORT=(\d+)', start_script)
                if env_port_match:
                    return int(env_port_match.group(1))

            except (json.JSONDecodeError, OSError):
                pass

        # Return default port based on app type
        return self.DEFAULT_PORTS.get(app_type, 3000)

    def extract_start_command(self, app_dir: Path, app_type: str) -> Optional[str]:
        """
        Extract or determine the start command for an app.

        Args:
            app_dir: Path to the workspace app directory.
            app_type: Detected app type.

        Returns:
            Start command string or None if not determinable.
        """
        package_json = app_dir / "package.json"

        if app_type == AppType.PYTHON.value:
            return self._python_start_command(app_dir)

        if not package_json.exists():
            return None

        try:
            with open(package_json) as f:
                pkg = json.load(f)

            scripts = pkg.get("scripts", {})

            # Priority order for start scripts
            script_priorities = [
                "start:prod",
                "start:production",
                "start",
            ]

            for script_name in script_priorities:
                if script_name in scripts:
                    return script_name

            # For Next.js, default to "start"
            if app_type == AppType.NEXTJS.value:
                return "start"

            # For NestJS, check for dist/main.js
            if (app_dir / "dist" / "main.js").exists() or \
               (app_dir / "nest-cli.json").exists():
                return "start:prod"

            return None

        except (json.JSONDecodeError, OSError):
            return None

    def _python_start_command(self, app_dir: Path) -> Optional[str]:
        """
        Determine start command for Python apps.

        Args:
            app_dir: Path to the Python app directory.

        Returns:
            Start command or None.
        """
        # Check for common entry points
        if (app_dir / "main.py").exists():
            return "python main.py"
        if (app_dir / "app.py").exists():
            return "python app.py"
        if (app_dir / "manage.py").exists():
            return "python manage.py runserver"

        return None

    def generate_subdomain(self, app_name: str, all_apps: List[str]) -> str:
        """
        Generate a subdomain for an app based on its name.

        Args:
            app_name: Name of the workspace app.
            all_apps: List of all app names for context.

        Returns:
            Generated subdomain string.
        """
        # Common naming patterns
        subdomain_mappings = {
            "web-gateway": "app",
            "web": "app",
            "frontend": "app",
            "client": "app",
            "erp-backend": "api",
            "backend": "api",
            "api": "api",
            "server": "api",
            "admin": "admin",
            "dashboard": "dashboard",
        }

        # Check for known patterns
        normalized = app_name.lower()
        for pattern, subdomain in subdomain_mappings.items():
            if pattern in normalized:
                return subdomain

        # Fallback: use app name as subdomain
        return normalized.replace("_", "-")

    def analyze_workspace(
        self,
        app_dir: Path,
        all_apps: List[str],
        port_offset: int = 0,
    ) -> MonorepoWorkspace:
        """
        Fully analyze a workspace app directory.

        Args:
            app_dir: Path to the workspace app directory.
            all_apps: List of all app names in the monorepo.
            port_offset: Offset for port assignment (0-based index).

        Returns:
            MonorepoWorkspace dataclass with all extracted info.
        """
        app_name = app_dir.name
        app_type = self.detect_app_type(app_dir)
        base_port = self.extract_port(app_dir, app_type)

        # Adjust port if default was used and we need offset
        if base_port == self.DEFAULT_PORTS.get(app_type, 3000) and port_offset > 0:
            base_port = 3000 + port_offset

        return MonorepoWorkspace(
            name=app_name,
            path=f"apps/{app_name}",
            app_type=app_type,
            subdomain=self.generate_subdomain(app_name, all_apps),
            port=base_port,
            start_command=self.extract_start_command(app_dir, app_type),
            health_check="/",
            env_vars={},
        )

    def analyze_all_workspaces(
        self,
        app_path: Path,
        subdomain_overrides: Optional[Dict[str, str]] = None,
    ) -> List[MonorepoWorkspace]:
        """
        Analyze all workspace apps in a monorepo.

        Args:
            app_path: Root path of the monorepo.
            subdomain_overrides: Optional dict mapping app names to subdomains.

        Returns:
            List of MonorepoWorkspace configurations.
        """
        apps = self.discover_apps(app_path)
        all_app_names = [a.name for a in apps]
        subdomain_overrides = subdomain_overrides or {}

        workspaces = []
        used_ports = set()

        for idx, app_dir in enumerate(apps):
            workspace = self.analyze_workspace(app_dir, all_app_names, idx)

            # Apply subdomain override if provided
            if workspace.name in subdomain_overrides:
                workspace.subdomain = subdomain_overrides[workspace.name]

            # Ensure unique ports
            while workspace.port in used_ports:
                workspace.port += 1
            used_ports.add(workspace.port)

            workspaces.append(workspace)
            self.logger.debug(
                f"Workspace: {workspace.name} ({workspace.app_type}) "
                f"-> {workspace.subdomain} on port {workspace.port}"
            )

        return workspaces
