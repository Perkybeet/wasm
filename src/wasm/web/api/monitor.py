"""
Monitor API endpoints.

Provides endpoints for the AI-powered process monitor.
"""

from typing import List, Optional

from fastapi import APIRouter, Request, HTTPException, Depends
from pydantic import BaseModel

from wasm.web.api.auth import get_current_session

router = APIRouter()


class MonitorStatus(BaseModel):
    """Monitor service status."""
    installed: bool
    enabled: bool
    active: bool
    pid: Optional[int] = None
    uptime: Optional[str] = None


class MonitorConfig(BaseModel):
    """Monitor configuration."""
    scan_interval: int
    cpu_threshold: float
    memory_threshold: float
    auto_terminate: bool
    terminate_malicious_only: bool
    use_ai: bool
    dry_run: bool


class ScanResult(BaseModel):
    """Process scan result."""
    pid: int
    name: str
    cpu_percent: float
    memory_percent: float
    is_suspicious: bool
    risk_level: Optional[str] = None
    reason: Optional[str] = None


class ScanResponse(BaseModel):
    """Response from a scan."""
    scanned: int
    suspicious: int
    terminated: int
    results: List[ScanResult]


@router.get("/status", response_model=MonitorStatus)
async def get_monitor_status(
    request: Request,
    session: dict = Depends(get_current_session)
):
    """
    Get the status of the process monitor service.
    """
    try:
        from wasm.monitor.process_monitor import ProcessMonitor
        
        monitor = ProcessMonitor(verbose=False)
        status = monitor.get_service_status()
        
        return MonitorStatus(
            installed=status.get("installed", False),
            enabled=status.get("enabled", False),
            active=status.get("active", False),
            pid=status.get("pid"),
            uptime=status.get("uptime")
        )
    except ImportError:
        return MonitorStatus(
            installed=False,
            enabled=False,
            active=False
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/config", response_model=MonitorConfig)
async def get_monitor_config(
    request: Request,
    session: dict = Depends(get_current_session)
):
    """
    Get the current monitor configuration.
    """
    from wasm.core.config import Config
    
    config = Config()
    monitor_config = config.get("monitor", {})
    
    return MonitorConfig(
        scan_interval=monitor_config.get("scan_interval", 3600),
        cpu_threshold=monitor_config.get("cpu_threshold", 80.0),
        memory_threshold=monitor_config.get("memory_threshold", 80.0),
        auto_terminate=monitor_config.get("auto_terminate", True),
        terminate_malicious_only=monitor_config.get("terminate_malicious_only", True),
        use_ai=monitor_config.get("use_ai", True),
        dry_run=monitor_config.get("dry_run", False)
    )


class ProcessInfo(BaseModel):
    """Basic process information."""
    pid: int
    name: str
    user: str
    cpu_percent: float
    memory_percent: float
    command: str = ""
    status: str = "running"


class ProcessListResponse(BaseModel):
    """Response with list of all processes."""
    total: int
    processes: List[ProcessInfo]


@router.get("/processes", response_model=ProcessListResponse)
async def get_all_processes(
    request: Request,
    limit: int = 100,
    sort_by: str = "cpu",  # cpu, memory, name, pid
    session: dict = Depends(get_current_session)
):
    """
    Get list of all running processes.
    
    This shows ALL system processes, not just WASM-related ones.
    """
    try:
        import psutil
    except ImportError:
        raise HTTPException(
            status_code=500,
            detail="psutil not available. Install with: pip install psutil"
        )
    
    processes = []
    
    for proc in psutil.process_iter(['pid', 'name', 'username', 'cpu_percent', 'memory_percent', 'cmdline', 'status']):
        try:
            info = proc.info
            cmdline = info.get('cmdline') or []
            command = ' '.join(cmdline) if cmdline else info.get('name', '')
            
            processes.append(ProcessInfo(
                pid=info.get('pid', 0),
                name=info.get('name', 'unknown'),
                user=info.get('username', 'unknown'),
                cpu_percent=info.get('cpu_percent', 0.0) or 0.0,
                memory_percent=info.get('memory_percent', 0.0) or 0.0,
                command=command[:200],  # Limit command length
                status=info.get('status', 'running'),
            ))
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
    
    # Sort
    if sort_by == "cpu":
        processes.sort(key=lambda p: p.cpu_percent, reverse=True)
    elif sort_by == "memory":
        processes.sort(key=lambda p: p.memory_percent, reverse=True)
    elif sort_by == "name":
        processes.sort(key=lambda p: p.name.lower())
    elif sort_by == "pid":
        processes.sort(key=lambda p: p.pid)
    
    return ProcessListResponse(
        total=len(processes),
        processes=processes[:limit]
    )


@router.post("/scan", response_model=ScanResponse)
async def run_scan(
    request: Request,
    dry_run: bool = True,
    force_ai: bool = False,
    analyze_all: bool = False,
    session: dict = Depends(get_current_session)
):
    """
    Run a threat scan on processes.
    
    This analyzes processes for suspicious activity using AI.
    By default runs in dry_run mode for safety.
    
    Args:
        dry_run: If True, don't terminate processes, just report.
        force_ai: Force AI analysis even if no suspicious processes are found.
        analyze_all: Analyze ALL processes with AI (expensive, use sparingly).
    """
    try:
        from wasm.monitor.process_monitor import ProcessMonitor, MonitorConfig
    except ImportError:
        raise HTTPException(
            status_code=500,
            detail="Monitor module not available. Install with: pip install wasm-cli[monitor]"
        )
    
    try:
        # Create config with dry_run setting
        config = MonitorConfig(dry_run=dry_run)
        monitor = ProcessMonitor(config=config, verbose=False)
        
        # Run threat scan with force_ai and analyze_all options
        threat_reports = monitor.scan_once(force_ai=force_ai, analyze_all=analyze_all)
        
        scan_results = []
        suspicious_count = 0
        terminated_count = 0
        
        for report in threat_reports:
            is_suspicious = report.threat_level in ("suspicious", "malicious")
            if is_suspicious:
                suspicious_count += 1
            
            if report.action_taken and "TERMINATED" in report.action_taken:
                terminated_count += 1
            
            scan_results.append(ScanResult(
                pid=report.pid,
                name=report.process_name,
                cpu_percent=report.cpu_percent,
                memory_percent=report.memory_percent,
                is_suspicious=is_suspicious,
                risk_level=report.threat_level,
                reason=report.reason
            ))
        
        # Get total process count for context
        try:
            import psutil
            total_processes = len(list(psutil.process_iter()))
        except:
            total_processes = len(scan_results)
        
        return ScanResponse(
            scanned=total_processes,
            suspicious=suspicious_count,
            terminated=terminated_count,
            results=scan_results[:50]  # Limit results
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/enable")
async def enable_monitor(
    request: Request,
    session: dict = Depends(get_current_session)
):
    """
    Enable the monitor service.
    """
    try:
        from wasm.monitor.process_monitor import ProcessMonitor
        
        monitor = ProcessMonitor(verbose=False)
        monitor.enable_service()
        
        return {"success": True, "message": "Monitor service enabled"}
    except ImportError:
        raise HTTPException(
            status_code=500,
            detail="Monitor module not available"
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/disable")
async def disable_monitor(
    request: Request,
    session: dict = Depends(get_current_session)
):
    """
    Disable the monitor service.
    """
    try:
        from wasm.monitor.process_monitor import ProcessMonitor
        
        monitor = ProcessMonitor(verbose=False)
        monitor.disable_service()
        
        return {"success": True, "message": "Monitor service disabled"}
    except ImportError:
        raise HTTPException(
            status_code=500,
            detail="Monitor module not available"
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/start")
async def start_monitor(
    request: Request,
    session: dict = Depends(get_current_session)
):
    """
    Start the monitor service.
    """
    try:
        from wasm.monitor.process_monitor import ProcessMonitor
        
        monitor = ProcessMonitor(verbose=False)
        monitor.start_service()
        
        return {"success": True, "message": "Monitor service started"}
    except ImportError:
        raise HTTPException(
            status_code=500,
            detail="Monitor module not available"
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/stop")
async def stop_monitor(
    request: Request,
    session: dict = Depends(get_current_session)
):
    """
    Stop the monitor service.
    """
    try:
        from wasm.monitor.process_monitor import ProcessMonitor
        
        monitor = ProcessMonitor(verbose=False)
        monitor.stop_service()
        
        return {"success": True, "message": "Monitor service stopped"}
    except ImportError:
        raise HTTPException(
            status_code=500,
            detail="Monitor module not available"
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/install")
async def install_monitor(
    request: Request,
    session: dict = Depends(get_current_session)
):
    """
    Install the monitor service.
    """
    try:
        from wasm.monitor.process_monitor import ProcessMonitor
        
        monitor = ProcessMonitor(verbose=False)
        monitor.install_service()
        
        return {"success": True, "message": "Monitor service installed"}
    except ImportError:
        raise HTTPException(
            status_code=500,
            detail="Monitor module not available"
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/uninstall")
async def uninstall_monitor(
    request: Request,
    session: dict = Depends(get_current_session)
):
    """
    Uninstall the monitor service.
    """
    try:
        from wasm.monitor.process_monitor import ProcessMonitor
        
        monitor = ProcessMonitor(verbose=False)
        monitor.uninstall_service()
        
        return {"success": True, "message": "Monitor service uninstalled"}
    except ImportError:
        raise HTTPException(
            status_code=500,
            detail="Monitor module not available"
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/test-email")
async def test_email(
    request: Request,
    session: dict = Depends(get_current_session)
):
    """
    Send a test email notification.
    """
    try:
        from wasm.monitor.email_notifier import EmailNotifier
        from wasm.core.config import Config
        
        # Reload config to get latest values from disk
        config = Config()
        config.reload()
        
        # Check if SMTP is configured
        smtp_host = config.get("monitor.smtp.host", "")
        smtp_username = config.get("monitor.smtp.username", "")
        recipients = config.get("monitor.email_recipients", [])
        
        if not smtp_host:
            raise HTTPException(
                status_code=400,
                detail="SMTP not configured. Please configure SMTP settings in Settings → Email first."
            )
        
        if not smtp_username:
            raise HTTPException(
                status_code=400,
                detail="SMTP username not configured. Please configure SMTP settings in Settings → Email."
            )
        
        if not recipients:
            raise HTTPException(
                status_code=400,
                detail="No email recipients configured. Please add recipients in Settings → Email."
            )
        
        # Create fresh notifier that will read the reloaded config
        notifier = EmailNotifier(verbose=True)
        success = notifier.send_test_email()
        
        if success:
            return {"success": True, "message": "Test email sent successfully"}
        else:
            raise HTTPException(status_code=500, detail="Failed to send test email - check SMTP configuration")
    except ImportError as e:
        raise HTTPException(
            status_code=500,
            detail=f"Email notifier module not available: {e}"
        )
    except HTTPException:
        raise
    except Exception as e:
        error_msg = str(e)
        # Clean up nested error messages
        if "Details:" in error_msg:
            error_msg = error_msg.split("Details:")[0].strip()
        raise HTTPException(status_code=500, detail=f"Failed to send test email: {error_msg}")
