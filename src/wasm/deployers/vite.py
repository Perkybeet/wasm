"""
Vite deployer for WASM.
"""

from pathlib import Path
from typing import Dict, List
import json

from wasm.deployers.base import BaseDeployer
from wasm.deployers.registry import DeployerRegistry


class ViteDeployer(BaseDeployer):
    """
    Deployer for Vite-based applications.
    
    Handles deployment of React, Vue, Svelte, and other
    applications built with Vite. Builds static assets and
    serves them through Nginx/Apache.
    """
    
    APP_TYPE = "vite"
    DISPLAY_NAME = "Vite (React/Vue/Svelte)"
    
    DETECTION_FILES = [
        "vite.config.js",
        "vite.config.ts",
        "vite.config.mjs",
    ]
    
    DEFAULT_PORT = 5173
    
    SYSTEM_DEPS = ["node", "npm"]
    
    def __init__(self, verbose: bool = False):
        """Initialize Vite deployer."""
        super().__init__(verbose=verbose)
        self.output_dir = "dist"
        self.is_ssr = False
    
    def detect(self, path: Path) -> bool:
        """Detect if path contains a Vite project."""
        # Check for vite config files
        for f in self.DETECTION_FILES:
            if (path / f).exists():
                return True
        
        # Check package.json for vite dependency
        package_json = path / "package.json"
        if package_json.exists():
            try:
                with open(package_json) as f:
                    pkg = json.load(f)
                    deps = pkg.get("dependencies", {})
                    dev_deps = pkg.get("devDependencies", {})
                    return "vite" in deps or "vite" in dev_deps
            except Exception:
                pass
        
        return False
    
    def _check_ssr_mode(self) -> bool:
        """Check if the project uses SSR."""
        config_files = ["vite.config.js", "vite.config.ts", "vite.config.mjs"]
        
        for config_file in config_files:
            config_path = self.app_path / config_file
            if config_path.exists():
                try:
                    content = config_path.read_text()
                    if "ssr" in content.lower():
                        return True
                except Exception:
                    pass
        
        return False
    
    def _detect_output_dir(self) -> str:
        """Detect the build output directory."""
        config_files = ["vite.config.js", "vite.config.ts", "vite.config.mjs"]
        
        for config_file in config_files:
            config_path = self.app_path / config_file
            if config_path.exists():
                try:
                    content = config_path.read_text()
                    # Simple regex to find outDir
                    import re
                    match = re.search(r"outDir\s*:\s*['\"]([^'\"]+)['\"]", content)
                    if match:
                        return match.group(1)
                except Exception:
                    pass
        
        return "dist"
    
    def get_install_command(self) -> List[str]:
        """Get dependency installation command."""
        return self._get_pm_install_command()
    
    def get_build_command(self) -> List[str]:
        """Get build command."""
        return self._get_pm_run_command("build")
    
    def get_start_command(self) -> str:
        """Get start command."""
        if self.is_ssr:
            pm = self.package_manager
            if pm == "pnpm":
                return "pnpm run preview"
            elif pm == "yarn":
                return "yarn preview"
            elif pm == "bun":
                return "bun run preview"
            else:
                return "npm run preview"
        
        # For static builds, no start command needed
        return ""
    
    def get_nginx_template(self) -> str:
        """Get Nginx template."""
        if self.is_ssr:
            return "proxy"
        return "static"
    
    def get_apache_template(self) -> str:
        """Get Apache template."""
        if self.is_ssr:
            return "proxy"
        return "static"
    
    def pre_install(self) -> bool:
        """Pre-installation hook."""
        # Call parent to detect package manager and prisma
        super().pre_install()
        self.output_dir = self._detect_output_dir()
        self.is_ssr = self._check_ssr_mode()
        
        self.logger.debug(f"Package manager: {self.package_manager}")
        self.logger.debug(f"Output directory: {self.output_dir}")
        self.logger.debug(f"SSR mode: {self.is_ssr}")
        
        return True
    
    def get_template_context(self) -> Dict:
        """Get template context with Vite specifics."""
        context = super().get_template_context()
        context.update({
            "is_vite": True,
            "is_static": not self.is_ssr,
            "static_dir": str(self.app_path / self.output_dir),
            "output_dir": self.output_dir,
        })
        return context
    
    def create_service(self) -> bool:
        """Create service only if SSR mode, but always register in store."""
        if self.is_ssr:
            return super().create_service()
        
        self.logger.substep("Static build - no service needed")
        # Note: App is already registered in deploy() before create_service is called
        # The is_static flag is set based on get_start_command() returning ""
        return True
    
    def start(self) -> bool:
        """Start service only if SSR mode."""
        if self.is_ssr:
            return super().start()
        return True
    
    def health_check(self, retries: int = 5, delay: float = 2.0) -> bool:
        """Check if the static site is accessible."""
        if not self.is_ssr:
            # For static sites, check if index.html exists
            index_path = self.app_path / self.output_dir / "index.html"
            if index_path.exists():
                self.logger.debug("Static build verified")
                return True
            return False
        
        return super().health_check(retries, delay)


# Register the deployer
DeployerRegistry.register(ViteDeployer)
