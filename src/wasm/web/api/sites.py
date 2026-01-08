"""
Sites API endpoints.

Provides endpoints for managing web server sites (nginx/apache).
"""

import subprocess
from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter, Request, HTTPException, Depends
from pydantic import BaseModel

from wasm.web.api.auth import get_current_session
from wasm.core.config import (
    NGINX_SITES_AVAILABLE,
    NGINX_SITES_ENABLED,
    APACHE_SITES_AVAILABLE,
    APACHE_SITES_ENABLED,
)
from wasm.core.store import get_store

router = APIRouter()


class SiteInfo(BaseModel):
    """Site information."""
    name: str
    webserver: str
    enabled: bool
    config_path: str
    has_ssl: bool = False


class SiteListResponse(BaseModel):
    """Response for listing sites."""
    sites: List[SiteInfo]
    total: int
    webserver: str


class SiteActionResponse(BaseModel):
    """Response for site actions."""
    success: bool
    message: str
    site: str


class CreateSiteRequest(BaseModel):
    """Request to create a new site."""
    domain: str
    webserver: str = "nginx"
    template: str = "proxy"  # proxy or static
    port: int = 3000
    raw_config: Optional[str] = None  # Raw config content for advanced mode


def _detect_webserver() -> str:
    """Detect which webserver is installed and active."""
    try:
        result = subprocess.run(
            ["systemctl", "is-active", "nginx"],
            capture_output=True,
            text=True
        )
        if result.returncode == 0:
            return "nginx"
    except Exception:
        pass
    
    try:
        result = subprocess.run(
            ["systemctl", "is-active", "apache2"],
            capture_output=True,
            text=True
        )
        if result.returncode == 0:
            return "apache"
    except Exception:
        pass
    
    # Check if config directories exist
    if NGINX_SITES_AVAILABLE.exists():
        return "nginx"
    if APACHE_SITES_AVAILABLE.exists():
        return "apache"
    
    return "nginx"  # Default


def _check_ssl_in_config(config_path: Path) -> bool:
    """Check if a site config has SSL enabled."""
    try:
        content = config_path.read_text()
        return "ssl_certificate" in content or "SSLCertificateFile" in content
    except Exception:
        return False


@router.get("", response_model=SiteListResponse)
async def list_sites(
    request: Request,
    session: dict = Depends(get_current_session)
):
    """
    List all configured sites.
    """
    store = get_store()
    webserver = _detect_webserver()
    
    # Get sites from store
    stored_sites = store.list_sites()
    
    sites = []
    for site in stored_sites:
        sites.append(SiteInfo(
            name=site.domain,
            webserver=site.webserver,
            enabled=site.enabled,
            config_path=site.config_path or "",
            has_ssl=site.ssl_enabled
        ))
    
    # Fallback to filesystem if store is empty (for backwards compatibility)
    if not sites:
        if webserver == "nginx":
            available_dir = NGINX_SITES_AVAILABLE
            enabled_dir = NGINX_SITES_ENABLED
        else:
            available_dir = APACHE_SITES_AVAILABLE
            enabled_dir = APACHE_SITES_ENABLED
        
        if available_dir.exists():
            for config_file in available_dir.iterdir():
                if config_file.is_file() and not config_file.name.startswith("."):
                    enabled_link = enabled_dir / config_file.name
                    sites.append(SiteInfo(
                        name=config_file.name,
                        webserver=webserver,
                        enabled=enabled_link.exists(),
                        config_path=str(config_file),
                        has_ssl=_check_ssl_in_config(config_file)
                    ))
    
    return SiteListResponse(
        sites=sites,
        total=len(sites),
        webserver=webserver
    )


@router.get("/{name}", response_model=SiteInfo)
async def get_site(
    name: str,
    request: Request,
    session: dict = Depends(get_current_session)
):
    """
    Get details for a specific site.
    """
    store = get_store()
    webserver = _detect_webserver()
    
    # Try store first
    site = store.get_site(name)
    
    if site:
        return SiteInfo(
            name=site.domain,
            webserver=site.webserver,
            enabled=site.enabled,
            config_path=site.config_path or "",
            has_ssl=site.ssl_enabled
        )
    
    # Fallback to filesystem
    if webserver == "nginx":
        available_dir = NGINX_SITES_AVAILABLE
        enabled_dir = NGINX_SITES_ENABLED
    else:
        available_dir = APACHE_SITES_AVAILABLE
        enabled_dir = APACHE_SITES_ENABLED
    
    config_path = available_dir / name
    
    if not config_path.exists():
        raise HTTPException(status_code=404, detail=f"Site not found: {name}")
    
    enabled_link = enabled_dir / name
    
    return SiteInfo(
        name=name,
        webserver=webserver,
        enabled=enabled_link.exists(),
        config_path=str(config_path),
        has_ssl=_check_ssl_in_config(config_path)
    )


@router.get("/{name}/config")
async def get_site_config(
    name: str,
    request: Request,
    session: dict = Depends(get_current_session)
):
    """
    Get the configuration content for a site.
    """
    webserver = _detect_webserver()
    
    if webserver == "nginx":
        available_dir = NGINX_SITES_AVAILABLE
    else:
        available_dir = APACHE_SITES_AVAILABLE
    
    config_path = available_dir / name
    
    if not config_path.exists():
        raise HTTPException(status_code=404, detail=f"Site not found: {name}")
    
    try:
        content = config_path.read_text()
        return {
            "site": name,
            "webserver": webserver,
            "config": content
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error reading config: {e}")


class UpdateSiteConfigRequest(BaseModel):
    """Request to update site configuration."""
    config: str


@router.put("/{name}/config", response_model=SiteActionResponse)
async def update_site_config(
    name: str,
    data: UpdateSiteConfigRequest,
    request: Request,
    session: dict = Depends(get_current_session)
):
    """
    Update the configuration content for a site.
    """
    webserver = _detect_webserver()
    
    if webserver == "nginx":
        available_dir = NGINX_SITES_AVAILABLE
    else:
        available_dir = APACHE_SITES_AVAILABLE
    
    config_path = available_dir / name
    
    if not config_path.exists():
        raise HTTPException(status_code=404, detail=f"Site not found: {name}")
    
    try:
        # Write the new config
        config_path.write_text(data.config)
        
        return SiteActionResponse(
            success=True,
            message=f"Configuration updated for {name}. Reload web server to apply changes.",
            site=name
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error updating config: {e}")


@router.post("", response_model=SiteActionResponse)
async def create_site(
    data: CreateSiteRequest,
    request: Request,
    session: dict = Depends(get_current_session)
):
    """
    Create a new site configuration.
    """
    webserver = data.webserver if data.webserver else _detect_webserver()
    
    if webserver == "nginx":
        available_dir = NGINX_SITES_AVAILABLE
        enabled_dir = NGINX_SITES_ENABLED
    else:
        available_dir = APACHE_SITES_AVAILABLE
        enabled_dir = APACHE_SITES_ENABLED
    
    site_name = data.domain.replace(".", "_")
    config_path = available_dir / site_name
    
    if config_path.exists():
        raise HTTPException(status_code=400, detail=f"Site already exists: {data.domain}")
    
    # Generate config based on template
    if webserver == "nginx":
        if data.template == "static":
            config = f"""server {{
    listen 80;
    server_name {data.domain};
    root /var/www/{data.domain};
    index index.html index.htm;

    location / {{
        try_files $uri $uri/ =404;
    }}
}}"""
        else:  # proxy
            config = f"""server {{
    listen 80;
    server_name {data.domain};

    location / {{
        proxy_pass http://127.0.0.1:{data.port};
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection 'upgrade';
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_cache_bypass $http_upgrade;
    }}
}}"""
    else:  # apache
        if data.template == "static":
            config = f"""<VirtualHost *:80>
    ServerName {data.domain}
    DocumentRoot /var/www/{data.domain}

    <Directory /var/www/{data.domain}>
        Options Indexes FollowSymLinks
        AllowOverride All
        Require all granted
    </Directory>
</VirtualHost>"""
        else:  # proxy
            config = f"""<VirtualHost *:80>
    ServerName {data.domain}

    ProxyPreserveHost On
    ProxyPass / http://127.0.0.1:{data.port}/
    ProxyPassReverse / http://127.0.0.1:{data.port}/

    <Proxy *>
        Require all granted
    </Proxy>
</VirtualHost>"""
    
    # Use raw config if provided (advanced mode)
    if data.raw_config:
        config = data.raw_config
    
    try:
        config_path.write_text(config)
        
        # Enable the site by creating symlink
        enabled_link = enabled_dir / site_name
        if not enabled_link.exists():
            enabled_link.symlink_to(config_path)
        
        return SiteActionResponse(
            success=True,
            message=f"Site created: {data.domain}",
            site=site_name
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error creating site: {e}")


@router.post("/{name}/enable", response_model=SiteActionResponse)
async def enable_site(
    name: str,
    request: Request,
    session: dict = Depends(get_current_session)
):
    """
    Enable a site.
    """
    webserver = _detect_webserver()
    
    if webserver == "nginx":
        from wasm.managers.nginx_manager import NginxManager
        manager = NginxManager(verbose=False)
    else:
        from wasm.managers.apache_manager import ApacheManager
        manager = ApacheManager(verbose=False)
    
    try:
        manager.enable_site(name)
        
        # Reload webserver
        subprocess.run(
            ["systemctl", "reload", webserver if webserver == "nginx" else "apache2"],
            check=True
        )
        
        return SiteActionResponse(
            success=True,
            message=f"Site enabled: {name}",
            site=name
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/{name}/disable", response_model=SiteActionResponse)
async def disable_site(
    name: str,
    request: Request,
    session: dict = Depends(get_current_session)
):
    """
    Disable a site.
    """
    webserver = _detect_webserver()
    
    if webserver == "nginx":
        from wasm.managers.nginx_manager import NginxManager
        manager = NginxManager(verbose=False)
    else:
        from wasm.managers.apache_manager import ApacheManager
        manager = ApacheManager(verbose=False)
    
    try:
        manager.disable_site(name)
        
        # Reload webserver
        subprocess.run(
            ["systemctl", "reload", webserver if webserver == "nginx" else "apache2"],
            check=True
        )
        
        return SiteActionResponse(
            success=True,
            message=f"Site disabled: {name}",
            site=name
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/{name}", response_model=SiteActionResponse)
async def delete_site(
    name: str,
    request: Request,
    session: dict = Depends(get_current_session)
):
    """
    Delete a site configuration.
    """
    webserver = _detect_webserver()
    
    if webserver == "nginx":
        from wasm.managers.nginx_manager import NginxManager
        manager = NginxManager(verbose=False)
    else:
        from wasm.managers.apache_manager import ApacheManager
        manager = ApacheManager(verbose=False)
    
    try:
        manager.delete_site(name)
        
        return SiteActionResponse(
            success=True,
            message=f"Site deleted: {name}",
            site=name
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to delete site: {e}")


@router.post("/reload")
async def reload_webserver(
    request: Request,
    session: dict = Depends(get_current_session)
):
    """
    Reload the web server configuration.
    """
    webserver = _detect_webserver()
    service_name = webserver if webserver == "nginx" else "apache2"
    
    try:
        # Test config first
        if webserver == "nginx":
            result = subprocess.run(
                ["nginx", "-t"],
                capture_output=True,
                text=True
            )
            if result.returncode != 0:
                raise HTTPException(
                    status_code=400,
                    detail=f"Configuration test failed: {result.stderr}"
                )
        else:
            result = subprocess.run(
                ["apache2ctl", "configtest"],
                capture_output=True,
                text=True
            )
            if result.returncode != 0:
                raise HTTPException(
                    status_code=400,
                    detail=f"Configuration test failed: {result.stderr}"
                )
        
        # Reload
        subprocess.run(
            ["systemctl", "reload", service_name],
            check=True
        )
        
        return {
            "success": True,
            "message": f"{webserver.capitalize()} reloaded successfully",
            "webserver": webserver
        }
    except subprocess.CalledProcessError as e:
        raise HTTPException(status_code=500, detail=f"Failed to reload: {e}")
