"""
Next.js deployer for WASM.
"""

from pathlib import Path
from typing import Dict, List, Optional
import json

from wasm.deployers.base import BaseDeployer
from wasm.deployers.registry import DeployerRegistry


class NextJSDeployer(BaseDeployer):
    """
    Deployer for Next.js applications.
    
    Handles deployment of Next.js applications with support for
    both standalone and standard output modes.
    """
    
    APP_TYPE = "nextjs"
    DISPLAY_NAME = "Next.js"
    
    DETECTION_FILES = [
        "next.config.js",
        "next.config.mjs",
        "next.config.ts",
    ]
    
    DEFAULT_PORT = 3000
    
    SYSTEM_DEPS = ["node", "npm"]
    
    def __init__(self, verbose: bool = False):
        """Initialize Next.js deployer."""
        super().__init__(verbose=verbose)
        self.is_standalone = False
    
    def detect(self, path: Path) -> bool:
        """Detect if path contains a Next.js project."""
        # Check for next.config files
        for f in self.DETECTION_FILES:
            if (path / f).exists():
                return True
        
        # Check package.json for next dependency
        package_json = path / "package.json"
        if package_json.exists():
            try:
                with open(package_json) as f:
                    pkg = json.load(f)
                    deps = pkg.get("dependencies", {})
                    dev_deps = pkg.get("devDependencies", {})
                    return "next" in deps or "next" in dev_deps
            except (json.JSONDecodeError, OSError) as e:
                self.logger.debug(f"Failed to read package.json for Next.js detection: {e}")

        return False
    
    def _check_standalone_mode(self) -> bool:
        """Check if the project uses standalone output mode."""
        config_files = ["next.config.js", "next.config.mjs", "next.config.ts"]
        
        for config_file in config_files:
            config_path = self.app_path / config_file
            if config_path.exists():
                try:
                    content = config_path.read_text()
                    if "output" in content and "standalone" in content:
                        return True
                except OSError as e:
                    self.logger.debug(f"Failed to read {config_file} for standalone mode check: {e}")

        return False
    
    def get_install_command(self) -> List[str]:
        """Get dependency installation command."""
        return self._get_pm_install_command()
    
    def get_build_command(self) -> List[str]:
        """Get build command."""
        return self._get_pm_run_command("build")
    
    def get_start_command(self) -> str:
        """Get start command."""
        if self.is_standalone:
            return "node .next/standalone/server.js"
        
        pm = self.package_manager
        if pm == "pnpm":
            return "pnpm run start"
        elif pm == "yarn":
            return "yarn start"
        elif pm == "bun":
            return "bun run start"
        else:
            return "npm run start"
    
    def get_health_check(self) -> str:
        """Get health check endpoint."""
        return "/"
    
    def pre_install(self) -> bool:
        """Pre-installation hook."""
        # Call parent to detect package manager and prisma
        super().pre_install()
        self.logger.debug(f"Detected package manager: {self.package_manager}")
        return True
    
    def post_build(self) -> bool:
        """Post-build hook."""
        # Check for standalone mode
        self.is_standalone = self._check_standalone_mode()
        
        if self.is_standalone:
            self.logger.debug("Standalone output mode detected")
            
            # Copy static and public files for standalone
            standalone_dir = self.app_path / ".next" / "standalone"
            if standalone_dir.exists():
                # Copy static files
                static_src = self.app_path / ".next" / "static"
                static_dest = standalone_dir / ".next" / "static"
                if static_src.exists():
                    import shutil
                    shutil.copytree(static_src, static_dest, dirs_exist_ok=True)
                
                # Copy public files
                public_src = self.app_path / "public"
                public_dest = standalone_dir / "public"
                if public_src.exists():
                    import shutil
                    shutil.copytree(public_src, public_dest, dirs_exist_ok=True)
        
        return True
    
    def get_template_context(self) -> Dict:
        """Get template context with Next.js specifics."""
        context = super().get_template_context()
        context.update({
            "is_nextjs": True,
            "is_standalone": self.is_standalone,
        })
        return context


# Register the deployer
DeployerRegistry.register(NextJSDeployer)
