"""
Nginx site manager for WASM.
"""

import re
from pathlib import Path
from typing import Dict, List, Optional

from jinja2 import Environment, PackageLoader, TemplateNotFound

from wasm.core.config import NGINX_SITES_AVAILABLE, NGINX_SITES_ENABLED
from wasm.core.exceptions import NginxError, TemplateError
from wasm.core.store import get_store, Site, WebServer
from wasm.core.utils import (
    create_symlink,
    read_file,
    remove_file,
    write_file,
)
from wasm.managers.base_manager import BaseManager


class NginxManager(BaseManager):
    """
    Manager for Nginx site configurations.
    
    Handles creating, enabling, disabling, and removing Nginx virtual hosts.
    """
    
    SITES_AVAILABLE = NGINX_SITES_AVAILABLE
    SITES_ENABLED = NGINX_SITES_ENABLED
    
    def __init__(self, verbose: bool = False):
        """Initialize Nginx manager."""
        super().__init__(verbose=verbose)
        self.store = get_store()
        
        try:
            self.jinja_env = Environment(
                loader=PackageLoader("wasm", "templates/nginx"),
                trim_blocks=True,
                lstrip_blocks=True,
            )
        except Exception:
            self.jinja_env = None
    
    def is_installed(self) -> bool:
        """Check if Nginx is installed."""
        result = self._run(["which", "nginx"])
        return result.success
    
    def get_version(self) -> Optional[str]:
        """Get Nginx version."""
        result = self._run(["nginx", "-v"])
        if result.success or result.stderr:
            # nginx -v outputs to stderr
            match = re.search(r"nginx/(\S+)", result.stderr)
            if match:
                return match.group(1)
        return None
    
    def test_config(self) -> bool:
        """
        Test Nginx configuration syntax.
        
        Returns:
            True if configuration is valid.
        """
        result = self._run_sudo(["nginx", "-t"])
        return result.success
    
    def reload(self) -> bool:
        """
        Reload Nginx configuration.
        
        Returns:
            True if reload was successful.
        """
        if not self.test_config():
            self.logger.error("Nginx configuration test failed")
            return False
        
        result = self._run_sudo(["systemctl", "reload", "nginx"])
        return result.success
    
    def restart(self) -> bool:
        """
        Restart Nginx service.
        
        Returns:
            True if restart was successful.
        """
        result = self._run_sudo(["systemctl", "restart", "nginx"])
        return result.success
    
    def get_status(self) -> Dict:
        """
        Get Nginx service status.
        
        Returns:
            Dictionary with status information.
        """
        result = self._run(["systemctl", "is-active", "nginx"])
        is_active = result.stdout.strip() == "active"
        
        result = self._run(["systemctl", "is-enabled", "nginx"])
        is_enabled = result.stdout.strip() == "enabled"
        
        return {
            "installed": self.is_installed(),
            "version": self.get_version(),
            "active": is_active,
            "enabled": is_enabled,
        }
    
    def site_exists(self, domain: str) -> bool:
        """
        Check if a site configuration exists.
        
        Args:
            domain: Domain name.
            
        Returns:
            True if site exists.
        """
        config_path = self.SITES_AVAILABLE / domain
        return config_path.exists()
    
    def site_enabled(self, domain: str) -> bool:
        """
        Check if a site is enabled.
        
        Args:
            domain: Domain name.
            
        Returns:
            True if site is enabled.
        """
        link_path = self.SITES_ENABLED / domain
        return link_path.exists()
    
    def list_sites(self) -> List[Dict]:
        """
        List all Nginx sites.
        
        Returns:
            List of site information dictionaries.
        """
        sites = []
        
        if not self.SITES_AVAILABLE.exists():
            return sites
        
        for config_file in self.SITES_AVAILABLE.iterdir():
            if config_file.is_file() and config_file.name != "default":
                enabled = (self.SITES_ENABLED / config_file.name).exists()
                sites.append({
                    "domain": config_file.name,
                    "enabled": enabled,
                    "config_path": str(config_file),
                })
        
        return sites
    
    def create_site(
        self,
        domain: str,
        template: str = "proxy",
        context: Optional[Dict] = None,
    ) -> bool:
        """
        Create a new Nginx site configuration.
        
        Args:
            domain: Domain name.
            template: Template name (without .conf.j2).
            context: Template context variables.
            
        Returns:
            True if site was created successfully.
            
        Raises:
            NginxError: If creation fails.
        """
        if self.site_exists(domain):
            raise NginxError(f"Site already exists: {domain}")
        
        if not self.jinja_env:
            raise NginxError("Template environment not initialized")
        
        # Prepare context
        ctx = {
            "domain": domain,
            "port": 3000,
            "app_path": f"/var/www/apps/{domain}",
            "ssl": False,
            "ssl_certificate": f"/etc/letsencrypt/live/{domain}/fullchain.pem",
            "ssl_certificate_key": f"/etc/letsencrypt/live/{domain}/privkey.pem",
        }
        if context:
            ctx.update(context)
        
        # Render template
        try:
            template_obj = self.jinja_env.get_template(f"{template}.conf.j2")
            config_content = template_obj.render(**ctx)
        except TemplateNotFound:
            raise TemplateError(f"Template not found: {template}.conf.j2")
        except Exception as e:
            raise TemplateError(f"Template rendering failed: {e}")
        
        # Write configuration
        config_path = self.SITES_AVAILABLE / domain
        if not write_file(config_path, config_content, sudo=True):
            raise NginxError(f"Failed to write configuration: {config_path}")
        
        # Register in store
        try:
            site = Site(
                domain=domain,
                webserver=WebServer.NGINX.value,
                config_path=str(config_path),
                proxy_port=ctx.get("port"),
                ssl_enabled=ctx.get("ssl", False),
                ssl_certificate=ctx.get("ssl_certificate") if ctx.get("ssl") else None,
                ssl_key=ctx.get("ssl_certificate_key") if ctx.get("ssl") else None,
                enabled=False,
            )
            self.store.create_site(site)
        except Exception as e:
            self.logger.debug(f"Could not register site in store: {e}")
        
        self.logger.debug(f"Created site configuration: {config_path}")
        return True
    
    def enable_site(self, domain: str) -> bool:
        """
        Enable a site by creating symlink.
        
        Args:
            domain: Domain name.
            
        Returns:
            True if site was enabled.
            
        Raises:
            NginxError: If enabling fails.
        """
        if not self.site_exists(domain):
            raise NginxError(f"Site does not exist: {domain}")
        
        if self.site_enabled(domain):
            self.logger.debug(f"Site already enabled: {domain}")
            return True
        
        source = self.SITES_AVAILABLE / domain
        link = self.SITES_ENABLED / domain
        
        if not create_symlink(source, link, sudo=True):
            raise NginxError(f"Failed to enable site: {domain}")
        
        # Update store
        try:
            site = self.store.get_site(domain)
            if site:
                site.enabled = True
                self.store.update_site(site)
        except Exception as e:
            self.logger.debug(f"Could not update site in store: {e}")
        
        self.logger.debug(f"Enabled site: {domain}")
        return True
    
    def disable_site(self, domain: str) -> bool:
        """
        Disable a site by removing symlink.
        
        Args:
            domain: Domain name.
            
        Returns:
            True if site was disabled.
        """
        if not self.site_enabled(domain):
            self.logger.debug(f"Site already disabled: {domain}")
            return True
        
        link = self.SITES_ENABLED / domain
        
        if not remove_file(link, sudo=True):
            raise NginxError(f"Failed to disable site: {domain}")
        
        # Update store
        try:
            site = self.store.get_site(domain)
            if site:
                site.enabled = False
                self.store.update_site(site)
        except Exception as e:
            self.logger.debug(f"Could not update site in store: {e}")
        
        self.logger.debug(f"Disabled site: {domain}")
        return True
    
    def delete_site(self, domain: str) -> bool:
        """
        Delete a site configuration.
        
        Args:
            domain: Domain name.
            
        Returns:
            True if site was deleted.
        """
        # Disable first if enabled
        if self.site_enabled(domain):
            self.disable_site(domain)
        
        # Remove configuration file
        config_path = self.SITES_AVAILABLE / domain
        if config_path.exists():
            if not remove_file(config_path, sudo=True):
                raise NginxError(f"Failed to delete site: {domain}")
        
        # Remove from store
        try:
            self.store.delete_site(domain)
        except Exception as e:
            self.logger.debug(f"Could not remove site from store: {e}")
        
        self.logger.debug(f"Deleted site: {domain}")
        return True
    
    def get_site_config(self, domain: str) -> Optional[str]:
        """
        Get site configuration content.
        
        Args:
            domain: Domain name.
            
        Returns:
            Configuration content or None.
        """
        config_path = self.SITES_AVAILABLE / domain
        return read_file(config_path, sudo=True)
    
    def update_site(
        self,
        domain: str,
        template: str = "proxy",
        context: Optional[Dict] = None,
    ) -> bool:
        """
        Update an existing site configuration.
        
        Args:
            domain: Domain name.
            template: Template name.
            context: Template context.
            
        Returns:
            True if updated successfully.
        """
        if not self.site_exists(domain):
            raise NginxError(f"Site does not exist: {domain}")
        
        # Delete and recreate
        was_enabled = self.site_enabled(domain)
        
        self.delete_site(domain)
        self.create_site(domain, template, context)
        
        if was_enabled:
            self.enable_site(domain)
        
        return True
