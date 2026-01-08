"""
Applications API endpoints.

Provides endpoints for managing deployed web applications.
"""

import os
import subprocess
from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter, Request, HTTPException, Depends, Query
from pydantic import BaseModel, Field

from wasm.web.api.auth import get_current_session
from wasm.core.config import Config
from wasm.core.store import get_store
from wasm.core.utils import domain_to_app_name
from wasm.validators.domain import validate_domain
from wasm.validators.port import validate_port, find_available_port

router = APIRouter()


class AppInfo(BaseModel):
    """Application information."""
    name: str
    domain: str
    status: str
    active: bool
    enabled: bool
    pid: Optional[int] = None
    uptime: Optional[str] = None
    port: Optional[int] = None
    app_type: Optional[str] = None
    path: Optional[str] = None


class AppListResponse(BaseModel):
    """Response for listing applications."""
    apps: List[AppInfo]
    total: int


class CreateAppRequest(BaseModel):
    """Request to create a new application."""
    domain: str = Field(..., description="Target domain name")
    source: str = Field(..., description="Git URL or local path")
    app_type: str = Field(default="auto", description="Application type")
    port: Optional[int] = Field(default=None, description="Application port")
    webserver: str = Field(default="nginx", description="Web server to use")
    branch: Optional[str] = Field(default=None, description="Git branch to deploy")
    ssl: bool = Field(default=True, description="Enable SSL")
    package_manager: str = Field(default="auto", description="Package manager")
    env_vars: dict = Field(default_factory=dict, description="Environment variables")


class AppActionRequest(BaseModel):
    """Request for app actions."""
    domain: str


class AppLogsRequest(BaseModel):
    """Request for app logs."""
    lines: int = Field(default=100, ge=1, le=1000)
    follow: bool = Field(default=False)


class AppLogsResponse(BaseModel):
    """Response with app logs."""
    domain: str
    logs: str
    lines: int


@router.get("", response_model=AppListResponse)
async def list_apps(
    request: Request,
    session: dict = Depends(get_current_session)
):
    """
    List all deployed applications.
    """
    from wasm.managers.service_manager import ServiceManager
    
    store = get_store()
    config = Config()
    service_manager = ServiceManager(verbose=False)
    
    # Get apps from store
    stored_apps = store.list_apps()

    apps = []
    for app in stored_apps:
        # Get live status from systemd if service exists
        active = False
        enabled = False
        pid = None
        uptime = None

        # Get service from store by app_id
        service = store.get_service_by_app_id(app.id) if app.id else None
        if service:
            status = service_manager.get_status(service.name.replace("wasm-", ""))
            active = status.get("active", False)
            enabled = status.get("enabled", False)
            pid = status.get("pid")
            uptime = status.get("uptime")

        apps.append(AppInfo(
            name=app.domain,
            domain=app.domain,
            status="running" if active else ("stopped" if service else "static"),
            active=active,
            enabled=enabled,
            pid=int(pid) if pid and pid != "0" else None,
            uptime=uptime,
            port=app.port,
            app_type=app.app_type,
            path=app.app_path
        ))
    
    return AppListResponse(apps=apps, total=len(apps))


@router.get("/{domain}", response_model=AppInfo)
async def get_app(
    domain: str,
    request: Request,
    session: dict = Depends(get_current_session)
):
    """
    Get details for a specific application.
    """
    from wasm.managers.service_manager import ServiceManager
    
    try:
        validated_domain = validate_domain(domain)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    
    store = get_store()
    app = store.get_app(validated_domain)
    
    if not app:
        raise HTTPException(status_code=404, detail=f"Application not found: {domain}")
    
    # Get live status from systemd if service exists
    active = False
    enabled = False
    pid = None
    uptime = None

    # Get service from store by app_id
    service = store.get_service_by_app_id(app.id) if app.id else None
    if service:
        service_manager = ServiceManager(verbose=False)
        status = service_manager.get_status(service.name.replace("wasm-", ""))
        active = status.get("active", False)
        enabled = status.get("enabled", False)
        pid = status.get("pid")
        uptime = status.get("uptime")

    return AppInfo(
        name=app.domain,
        domain=app.domain,
        status="running" if active else ("stopped" if service else "static"),
        active=active,
        enabled=enabled,
        pid=int(pid) if pid and pid != "0" else None,
        uptime=uptime,
        port=app.port,
        app_type=app.app_type,
        path=app.app_path
    )


@router.post("", status_code=201)
async def create_app(
    body: CreateAppRequest,
    request: Request,
    session: dict = Depends(get_current_session)
):
    """
    Create and deploy a new application.
    """
    from wasm.deployers import get_deployer
    
    try:
        domain = validate_domain(body.domain)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    
    # Validate/find port
    port = body.port
    if port:
        try:
            port = validate_port(port)
        except Exception as e:
            raise HTTPException(status_code=400, detail=str(e))
    else:
        port = find_available_port(preferred=3000)
        if not port:
            raise HTTPException(status_code=500, detail="No available port found")
    
    try:
        # Get deployer
        deployer = get_deployer(body.app_type, verbose=False)
        
        # Configure deployer
        deployer.configure(
            domain=domain,
            source=body.source,
            port=port,
            webserver=body.webserver,
            ssl=body.ssl,
            branch=body.branch,
            env_vars=body.env_vars,
            package_manager=body.package_manager,
        )
        
        # Run deployment
        deployer.deploy()
        
        return {
            "success": True,
            "domain": domain,
            "port": port,
            "message": f"Application deployed successfully at {domain}"
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/{domain}/restart")
async def restart_app(
    domain: str,
    request: Request,
    session: dict = Depends(get_current_session)
):
    """
    Restart an application.
    """
    from wasm.managers.service_manager import ServiceManager
    
    try:
        validated_domain = validate_domain(domain)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    
    app_name = domain_to_app_name(validated_domain)
    service_manager = ServiceManager(verbose=False)
    
    status = service_manager.get_status(app_name)
    if not status["exists"]:
        raise HTTPException(status_code=404, detail=f"Application not found: {domain}")
    
    try:
        service_manager.restart(app_name)
        return {"success": True, "message": f"Application restarted: {domain}"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/{domain}/stop")
async def stop_app(
    domain: str,
    request: Request,
    session: dict = Depends(get_current_session)
):
    """
    Stop an application.
    """
    from wasm.managers.service_manager import ServiceManager
    
    try:
        validated_domain = validate_domain(domain)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    
    app_name = domain_to_app_name(validated_domain)
    service_manager = ServiceManager(verbose=False)
    
    status = service_manager.get_status(app_name)
    if not status["exists"]:
        raise HTTPException(status_code=404, detail=f"Application not found: {domain}")
    
    try:
        service_manager.stop(app_name)
        return {"success": True, "message": f"Application stopped: {domain}"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/{domain}/start")
async def start_app(
    domain: str,
    request: Request,
    session: dict = Depends(get_current_session)
):
    """
    Start an application.
    """
    from wasm.managers.service_manager import ServiceManager
    
    try:
        validated_domain = validate_domain(domain)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    
    app_name = domain_to_app_name(validated_domain)
    service_manager = ServiceManager(verbose=False)
    
    status = service_manager.get_status(app_name)
    if not status["exists"]:
        raise HTTPException(status_code=404, detail=f"Application not found: {domain}")
    
    try:
        service_manager.start(app_name)
        return {"success": True, "message": f"Application started: {domain}"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{domain}/logs", response_model=AppLogsResponse)
async def get_app_logs(
    domain: str,
    request: Request,
    lines: int = Query(default=100, ge=1, le=1000),
    session: dict = Depends(get_current_session)
):
    """
    Get application logs.
    """
    try:
        validated_domain = validate_domain(domain)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    
    app_name = domain_to_app_name(validated_domain)
    service_name = f"wasm-{app_name}"
    
    try:
        result = subprocess.run(
            ["journalctl", "-u", service_name, "-n", str(lines), "--no-pager"],
            capture_output=True,
            text=True,
            timeout=30
        )
        logs = result.stdout or result.stderr or "No logs available"
    except subprocess.TimeoutExpired:
        logs = "Timeout retrieving logs"
    except Exception as e:
        logs = f"Error retrieving logs: {e}"
    
    return AppLogsResponse(
        domain=validated_domain,
        logs=logs,
        lines=lines
    )


@router.delete("/{domain}")
async def delete_app(
    domain: str,
    request: Request,
    remove_files: bool = Query(default=False),
    remove_ssl: bool = Query(default=False),
    session: dict = Depends(get_current_session)
):
    """
    Delete an application.
    """
    from wasm.managers.service_manager import ServiceManager
    from wasm.managers.nginx_manager import NginxManager
    from wasm.core.config import Config
    import shutil
    
    try:
        validated_domain = validate_domain(domain)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    
    store = get_store()
    app = store.get_app(validated_domain)
    
    if not app:
        raise HTTPException(status_code=404, detail=f"Application not found: {domain}")
    
    app_name = domain_to_app_name(validated_domain)
    service_manager = ServiceManager(verbose=False)
    nginx_manager = NginxManager(verbose=False)
    config = Config()
    
    try:
        # Stop and delete service if exists
        service = store.get_service_by_app_id(app.id) if app.id else None
        if service:
            try:
                service_manager.stop(app_name)
                service_manager.disable(app_name)
                service_manager.delete_service(app_name)
            except Exception:
                pass

        # Remove nginx config
        try:
            nginx_manager.delete_site(validated_domain)
        except Exception:
            pass

        # Remove files if requested
        if remove_files:
            app_path = Path(app.app_path) if app.app_path else config.apps_directory / app_name
            if app_path.exists():
                shutil.rmtree(app_path)
        
        # Remove from store
        store.delete_app(validated_domain)
        
        return {
            "success": True,
            "message": f"Application deleted: {domain}",
            "files_removed": remove_files
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
