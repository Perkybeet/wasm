"""
Static site deployer for WASM.
"""

from pathlib import Path
from typing import Dict, List

from wasm.deployers.base import BaseDeployer
from wasm.deployers.registry import DeployerRegistry


class StaticDeployer(BaseDeployer):
    """
    Deployer for static HTML/CSS/JS sites.
    
    Serves static files directly through Nginx or Apache
    without any build process or application server.
    """
    
    APP_TYPE = "static"
    DISPLAY_NAME = "Static Site"
    
    DETECTION_FILES = ["index.html"]
    
    DEFAULT_PORT = 80  # Not used for static sites
    
    SYSTEM_DEPS = []
    
    def __init__(self, verbose: bool = False):
        """Initialize static deployer."""
        super().__init__(verbose=verbose)
        self.static_dir = None
    
    def detect(self, path: Path) -> bool:
        """Detect if path contains a static site."""
        # Check for index.html
        if (path / "index.html").exists():
            # Make sure it's not a framework project
            framework_files = [
                "package.json",
                "requirements.txt",
                "pyproject.toml",
                "Cargo.toml",
                "go.mod",
            ]
            for f in framework_files:
                if (path / f).exists():
                    return False
            return True
        
        return False
    
    def get_install_command(self) -> List[str]:
        """No installation needed for static sites."""
        return []
    
    def get_build_command(self) -> List[str]:
        """No build needed for static sites."""
        return []
    
    def get_start_command(self) -> str:
        """No start command for static sites."""
        return ""
    
    def get_nginx_template(self) -> str:
        """Get Nginx template for static sites."""
        return "static"
    
    def get_apache_template(self) -> str:
        """Get Apache template for static sites."""
        return "static"
    
    def pre_install(self) -> bool:
        """Determine static directory."""
        # Check for common static directories
        static_dirs = ["public", "dist", "build", "www", "html", "."]
        
        for dir_name in static_dirs:
            dir_path = self.app_path / dir_name
            if dir_name == ".":
                dir_path = self.app_path
            
            if (dir_path / "index.html").exists():
                self.static_dir = dir_path
                break
        
        if not self.static_dir:
            self.static_dir = self.app_path
        
        self.logger.debug(f"Static directory: {self.static_dir}")
        return True
    
    def get_template_context(self) -> Dict:
        """Get template context for static site."""
        context = super().get_template_context()
        context.update({
            "is_static": True,
            "static_dir": str(self.static_dir or self.app_path),
        })
        return context
    
    def create_service(self) -> bool:
        """No service needed for static sites."""
        self.logger.substep("Static site - no service needed")
        return True
    
    def start(self) -> bool:
        """No start needed for static sites."""
        return True
    
    def stop(self) -> bool:
        """No stop needed for static sites."""
        return True
    
    def restart(self) -> bool:
        """No restart needed for static sites."""
        return True
    
    def health_check(self, retries: int = 5, delay: float = 2.0) -> bool:
        """Check if static site files exist."""
        index_path = (self.static_dir or self.app_path) / "index.html"
        if index_path.exists():
            self.logger.debug("Static site verified")
            return True
        return False
    
    def deploy(self, total_steps: int = 5) -> bool:
        """
        Deploy static site with fewer steps.
        
        Args:
            total_steps: Total number of deployment steps.
            
        Returns:
            True if deployment was successful.
        """
        from wasm.core.logger import Icons
        from wasm.core.exceptions import CertificateError
        from wasm.core.store import AppStatus
        from datetime import datetime
        
        # Track if SSL was successfully obtained
        ssl_obtained = False
        
        # Register app in store at the start
        app = self._register_app_in_store(AppStatus.DEPLOYING.value)
        
        try:
            # Step 1: Fetch source
            self.logger.step(1, total_steps, "Fetching source code", Icons.DOWNLOAD)
            self.fetch_source()
            
            # Step 2: Prepare
            self.logger.step(2, total_steps, "Preparing static files", Icons.FOLDER)
            self.pre_install()
            
            # Step 3: Create site (initially WITHOUT SSL if SSL is requested)
            self.logger.step(3, total_steps, "Creating site configuration", Icons.GLOBE)
            self.create_site(with_ssl=False)
            
            # Step 4: SSL certificate (best effort - continue if it fails)
            if self.ssl:
                self.logger.step(4, total_steps, "Obtaining SSL certificate", Icons.LOCK)
                try:
                    self.obtain_certificate()
                    ssl_obtained = True
                    # Update site config with SSL
                    self.logger.substep("Updating site configuration with SSL")
                    self.create_site(with_ssl=True)
                except CertificateError as e:
                    self.logger.warning(f"SSL certificate failed: {e.message}")
                    self.logger.warning("Continuing deployment without SSL...")
                    self.logger.substep("Site will be available via HTTP only")
                except Exception as e:
                    self.logger.warning(f"SSL certificate failed: {e}")
                    self.logger.warning("Continuing deployment without SSL...")
                    self.logger.substep("Site will be available via HTTP only")
            else:
                self.logger.step(4, total_steps, "Skipping SSL certificate", Icons.LOCK)
            
            # Step 5: Verify
            self.logger.step(5, total_steps, "Verifying deployment", Icons.CHECK)
            
            # Update app status to running
            app.status = AppStatus.RUNNING.value
            app.ssl_enabled = ssl_obtained
            app.deployed_at = datetime.now().isoformat()
            self.store.update_app(app)
            
            if self.health_check():
                self.logger.success("Static site deployed successfully!")
                self.logger.blank()
                protocol = "https" if ssl_obtained else "http"
                self.logger.key_value("URL", f"{protocol}://{self.domain}")
                self.logger.key_value("Root", str(self.static_dir or self.app_path))
                if self.ssl and not ssl_obtained:
                    self.logger.blank()
                    self.logger.warning("SSL was requested but could not be obtained.")
                    self.logger.info("To add SSL later, run: wasm cert create -d " + self.domain)
                return True
            else:
                self.logger.warning("Deployment completed but verification failed")
                return True
                
        except Exception as e:
            # Update app status to failed
            app.status = AppStatus.FAILED.value
            self.store.update_app(app)
            self.logger.error(f"Deployment failed: {e}")
            raise


# Register the deployer
DeployerRegistry.register(StaticDeployer)
