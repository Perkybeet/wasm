"""
Node.js deployer for WASM.
"""

import json
from pathlib import Path

from wasm.deployers.base import BaseDeployer
from wasm.deployers.registry import DeployerRegistry


class NodeJSDeployer(BaseDeployer):
    """
    Deployer for generic Node.js applications.

    Handles deployment of Express, Fastify, Koa, and other
    Node.js applications.
    """

    APP_TYPE = "nodejs"
    DISPLAY_NAME = "Node.js"

    DETECTION_FILES = ["package.json"]

    DEFAULT_PORT = 3000

    SYSTEM_DEPS = ["node", "npm"]

    def __init__(self, verbose: bool = False):
        """Initialize Node.js deployer."""
        super().__init__(verbose=verbose)
        self.package_manager = "npm"
        self.start_script = "start"
        self.has_build = False

    def detect(self, path: Path) -> bool:
        """Detect if path contains a Node.js project."""
        package_json = path / "package.json"
        if not package_json.exists():
            return False

        try:
            with open(package_json) as f:
                pkg = json.load(f)

                # Check it's not a Next.js, Vite, etc.
                deps = pkg.get("dependencies", {})
                dev_deps = pkg.get("devDependencies", {})
                all_deps = {**deps, **dev_deps}

                # Exclude framework-specific projects
                frameworks = ["next", "vite", "@angular/core", "nuxt"]
                for framework in frameworks:
                    if framework in all_deps:
                        return False

                # Check for Node.js indicators
                if "express" in deps or "fastify" in deps or "koa" in deps:
                    return True

                # Check for main field or start script
                if "main" in pkg or "start" in pkg.get("scripts", {}):
                    return True
        except Exception:
            pass

        return False

    def _analyze_package_json(self) -> None:
        """Analyze package.json for build and start scripts."""
        package_json = self.app_path / "package.json"
        if package_json.exists():
            try:
                with open(package_json) as f:
                    pkg = json.load(f)
                    scripts = pkg.get("scripts", {})

                    # Check for build script
                    self.has_build = "build" in scripts

                    # Determine start script
                    if "start:prod" in scripts:
                        self.start_script = "start:prod"
                    elif "start:production" in scripts:
                        self.start_script = "start:production"
                    else:
                        self.start_script = "start"
            except Exception:
                pass

    def get_install_command(self) -> list[str]:
        """Get dependency installation command."""
        return self._get_pm_install_command()

    def get_build_command(self) -> list[str]:
        """Get build command."""
        if not self.has_build:
            return []
        return self._get_pm_run_command("build")

    def get_start_command(self) -> str:
        """Get start command."""
        pm = self.package_manager
        script = self.start_script

        if pm == "pnpm":
            return f"pnpm run {script}"
        elif pm == "yarn":
            return f"yarn {script}"
        elif pm == "bun":
            return f"bun run {script}"
        else:
            return f"npm run {script}"

    def pre_install(self) -> bool:
        """Pre-installation hook."""
        # Call parent to detect package manager and prisma
        super().pre_install()
        self._analyze_package_json()
        self.logger.debug(f"Package manager: {self.package_manager}")
        self.logger.debug(f"Start script: {self.start_script}")
        self.logger.debug(f"Has build: {self.has_build}")
        return True


# Register the deployer
DeployerRegistry.register(NodeJSDeployer)
