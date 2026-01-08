"""
Services API endpoints.

Provides endpoints for managing systemd services.
"""

import subprocess
from typing import List, Optional

from fastapi import APIRouter, Request, HTTPException, Depends, Query
from pydantic import BaseModel

from wasm.web.api.auth import get_current_session
from wasm.core.store import get_store

router = APIRouter()


class ServiceInfo(BaseModel):
    """Service information."""
    name: str
    description: Optional[str] = None
    active: bool
    enabled: bool
    status: str
    pid: Optional[int] = None
    uptime: Optional[str] = None
    memory: Optional[str] = None


class ServiceListResponse(BaseModel):
    """Response for listing services."""
    services: List[ServiceInfo]
    total: int


class ServiceActionResponse(BaseModel):
    """Response for service actions."""
    success: bool
    message: str
    service: str


class CreateServiceRequest(BaseModel):
    """Request to create a new service."""
    name: str
    command: Optional[str] = None
    user: str = "root"
    working_directory: str = "/var/www"
    restart: str = "always"
    environment: Optional[dict] = None
    raw_content: Optional[str] = None  # Raw systemd unit content for advanced mode


@router.get("", response_model=ServiceListResponse)
async def list_services(
    request: Request,
    wasm_only: bool = Query(default=True, description="Only show WASM services"),
    session: dict = Depends(get_current_session)
):
    """
    List all services (or only WASM services).
    """
    from wasm.managers.service_manager import ServiceManager
    
    store = get_store()
    service_manager = ServiceManager(verbose=False)
    
    # Get services from store
    stored_services = store.list_services()
    
    result = []
    for svc in stored_services:
        if wasm_only and not svc.name.startswith("wasm-"):
            continue
        
        # Get live status from systemd
        live_status = service_manager.get_status(svc.name.replace("wasm-", ""))
        
        result.append(ServiceInfo(
            name=svc.name,
            description=svc.command,
            active=live_status.get("active", False),
            enabled=live_status.get("enabled", False),
            status="running" if live_status.get("active") else "stopped",
            pid=live_status.get("pid"),
            uptime=live_status.get("uptime"),
            memory=live_status.get("memory")
        ))
    
    return ServiceListResponse(services=result, total=len(result))


@router.get("/{name}", response_model=ServiceInfo)
async def get_service(
    name: str,
    request: Request,
    session: dict = Depends(get_current_session)
):
    """
    Get details for a specific service.
    """
    from wasm.managers.service_manager import ServiceManager
    
    store = get_store()
    service_manager = ServiceManager(verbose=False)
    
    # Handle both wasm-prefixed and non-prefixed names
    svc = store.get_service(name)
    if not svc:
        svc = store.get_service(f"wasm-{name}")
    
    if not svc:
        raise HTTPException(status_code=404, detail=f"Service not found: {name}")
    
    # Get live status from systemd
    live_status = service_manager.get_status(svc.name.replace("wasm-", ""))
    
    return ServiceInfo(
        name=svc.name,
        description=svc.command,
        active=live_status.get("active", False),
        enabled=live_status.get("enabled", False),
        status="running" if live_status.get("active") else "stopped",
        pid=live_status.get("pid"),
        uptime=live_status.get("uptime"),
        memory=live_status.get("memory")
    )


@router.post("/{name}/start", response_model=ServiceActionResponse)
async def start_service(
    name: str,
    request: Request,
    session: dict = Depends(get_current_session)
):
    """
    Start a service.
    """
    from wasm.managers.service_manager import ServiceManager
    
    service_manager = ServiceManager(verbose=False)
    app_name = name.replace("wasm-", "")
    
    status = service_manager.get_status(app_name)
    if not status["exists"]:
        raise HTTPException(status_code=404, detail=f"Service not found: {name}")
    
    try:
        service_manager.start(app_name)
        return ServiceActionResponse(
            success=True,
            message=f"Service started: {name}",
            service=name
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/{name}/stop", response_model=ServiceActionResponse)
async def stop_service(
    name: str,
    request: Request,
    session: dict = Depends(get_current_session)
):
    """
    Stop a service.
    """
    from wasm.managers.service_manager import ServiceManager
    
    service_manager = ServiceManager(verbose=False)
    app_name = name.replace("wasm-", "")
    
    status = service_manager.get_status(app_name)
    if not status["exists"]:
        raise HTTPException(status_code=404, detail=f"Service not found: {name}")
    
    try:
        service_manager.stop(app_name)
        return ServiceActionResponse(
            success=True,
            message=f"Service stopped: {name}",
            service=name
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/{name}/restart", response_model=ServiceActionResponse)
async def restart_service(
    name: str,
    request: Request,
    session: dict = Depends(get_current_session)
):
    """
    Restart a service.
    """
    from wasm.managers.service_manager import ServiceManager
    
    service_manager = ServiceManager(verbose=False)
    app_name = name.replace("wasm-", "")
    
    status = service_manager.get_status(app_name)
    if not status["exists"]:
        raise HTTPException(status_code=404, detail=f"Service not found: {name}")
    
    try:
        service_manager.restart(app_name)
        return ServiceActionResponse(
            success=True,
            message=f"Service restarted: {name}",
            service=name
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/{name}/enable", response_model=ServiceActionResponse)
async def enable_service(
    name: str,
    request: Request,
    session: dict = Depends(get_current_session)
):
    """
    Enable a service to start on boot.
    """
    from wasm.managers.service_manager import ServiceManager
    
    service_manager = ServiceManager(verbose=False)
    app_name = name.replace("wasm-", "")
    
    status = service_manager.get_status(app_name)
    if not status["exists"]:
        raise HTTPException(status_code=404, detail=f"Service not found: {name}")
    
    try:
        service_manager.enable(app_name)
        return ServiceActionResponse(
            success=True,
            message=f"Service enabled: {name}",
            service=name
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/{name}/disable", response_model=ServiceActionResponse)
async def disable_service(
    name: str,
    request: Request,
    session: dict = Depends(get_current_session)
):
    """
    Disable a service from starting on boot.
    """
    from wasm.managers.service_manager import ServiceManager
    
    service_manager = ServiceManager(verbose=False)
    app_name = name.replace("wasm-", "")
    
    status = service_manager.get_status(app_name)
    if not status["exists"]:
        raise HTTPException(status_code=404, detail=f"Service not found: {name}")
    
    try:
        service_manager.disable(app_name)
        return ServiceActionResponse(
            success=True,
            message=f"Service disabled: {name}",
            service=name
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{name}/logs")
async def get_service_logs(
    name: str,
    request: Request,
    lines: int = Query(default=100, ge=1, le=1000),
    session: dict = Depends(get_current_session)
):
    """
    Get service logs from journalctl.
    """
    service_name = name if name.startswith("wasm-") else f"wasm-{name}"
    
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
    
    return {
        "service": name,
        "logs": logs,
        "lines": lines
    }


@router.get("/{name}/config")
async def get_service_config(
    name: str,
    request: Request,
    session: dict = Depends(get_current_session)
):
    """
    Get the systemd unit file content for a service.
    """
    from pathlib import Path
    
    service_name = name if name.startswith("wasm-") else f"wasm-{name}"
    service_path = Path(f"/etc/systemd/system/{service_name}.service")
    
    if not service_path.exists():
        raise HTTPException(status_code=404, detail=f"Service not found: {service_name}")
    
    try:
        content = service_path.read_text()
        return {
            "service": service_name,
            "config": content,
            "path": str(service_path)
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error reading config: {e}")


class UpdateServiceConfigRequest(BaseModel):
    """Request to update service configuration."""
    config: str


@router.put("/{name}/config", response_model=ServiceActionResponse)
async def update_service_config(
    name: str,
    data: UpdateServiceConfigRequest,
    request: Request,
    session: dict = Depends(get_current_session)
):
    """
    Update the systemd unit file content for a service.
    """
    from pathlib import Path
    
    service_name = name if name.startswith("wasm-") else f"wasm-{name}"
    service_path = Path(f"/etc/systemd/system/{service_name}.service")
    
    if not service_path.exists():
        raise HTTPException(status_code=404, detail=f"Service not found: {service_name}")
    
    try:
        # Write the new config
        service_path.write_text(data.config)
        
        # Reload systemd daemon
        subprocess.run(["systemctl", "daemon-reload"], capture_output=True, timeout=30)
        
        return ServiceActionResponse(
            success=True,
            message=f"Configuration updated for {service_name}. Restart the service to apply changes.",
            service=service_name
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error updating config: {e}")


@router.post("", response_model=ServiceActionResponse)
async def create_service(
    data: CreateServiceRequest,
    request: Request,
    session: dict = Depends(get_current_session)
):
    """
    Create a new systemd service.
    """
    from pathlib import Path
    
    service_name = data.name if data.name.startswith("wasm-") else f"wasm-{data.name}"
    service_path = Path(f"/etc/systemd/system/{service_name}.service")
    
    if service_path.exists():
        raise HTTPException(status_code=400, detail=f"Service already exists: {service_name}")
    
    # Use raw content if provided (advanced mode)
    if data.raw_content:
        service_content = data.raw_content
    else:
        # Validate command is provided in simple mode
        if not data.command:
            raise HTTPException(status_code=400, detail="Command is required in simple mode")
        
        # Build environment section
        env_section = ""
        if data.environment:
            env_lines = [f"Environment=\"{k}={v}\"" for k, v in data.environment.items()]
            env_section = "\n".join(env_lines) + "\n"
        
        # Create service file
        service_content = f"""[Unit]
Description=WASM Service: {data.name}
After=network.target

[Service]
Type=simple
User={data.user}
WorkingDirectory={data.working_directory}
ExecStart={data.command}
Restart={data.restart}
RestartSec=5
{env_section}
[Install]
WantedBy=multi-user.target
"""
    
    try:
        service_path.write_text(service_content)
        
        # Reload systemd
        subprocess.run(["systemctl", "daemon-reload"], capture_output=True, timeout=30)
        
        # Enable the service
        subprocess.run(
            ["systemctl", "enable", service_name],
            capture_output=True,
            timeout=30
        )
        
        return ServiceActionResponse(
            success=True,
            message=f"Service created: {service_name}",
            service=service_name
        )
    except Exception as e:
        # Clean up on failure
        if service_path.exists():
            service_path.unlink()
        raise HTTPException(status_code=500, detail=f"Failed to create service: {e}")


@router.delete("/{name}", response_model=ServiceActionResponse)
async def delete_service(
    name: str,
    request: Request,
    session: dict = Depends(get_current_session)
):
    """
    Delete a systemd service.
    """
    from pathlib import Path
    
    service_name = name if name.startswith("wasm-") else f"wasm-{name}"
    service_path = Path(f"/etc/systemd/system/{service_name}.service")
    
    if not service_path.exists():
        raise HTTPException(status_code=404, detail=f"Service not found: {service_name}")
    
    try:
        # Stop the service
        subprocess.run(
            ["systemctl", "stop", service_name],
            capture_output=True,
            timeout=30
        )
        
        # Disable the service
        subprocess.run(
            ["systemctl", "disable", service_name],
            capture_output=True,
            timeout=30
        )
        
        # Remove the service file
        service_path.unlink()
        
        # Reload systemd
        subprocess.run(["systemctl", "daemon-reload"], capture_output=True, timeout=30)
        
        return ServiceActionResponse(
            success=True,
            message=f"Service deleted: {service_name}",
            service=service_name
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to delete service: {e}")
