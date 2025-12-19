"""
System API endpoints.

Provides endpoints for system monitoring and information.
"""

import os
import subprocess
from typing import List, Optional
from datetime import datetime

from fastapi import APIRouter, Request, HTTPException, Depends
from pydantic import BaseModel

from wasm.web.api.auth import get_current_session

router = APIRouter()


class DiskInfo(BaseModel):
    """Disk usage information."""
    device: str
    mount_point: str
    total_gb: float
    used_gb: float
    free_gb: float
    percent_used: float


class MemoryInfo(BaseModel):
    """Memory usage information."""
    total_gb: float
    used_gb: float
    free_gb: float
    available_gb: float
    percent_used: float
    swap_total_gb: float
    swap_used_gb: float
    swap_percent: float


class CpuInfo(BaseModel):
    """CPU information."""
    cores: int
    percent: float
    load_1min: float
    load_5min: float
    load_15min: float


class SystemInfo(BaseModel):
    """Full system information."""
    hostname: str
    os: str
    kernel: str
    uptime: str
    cpu: CpuInfo
    memory: MemoryInfo
    disks: List[DiskInfo]


class ProcessInfo(BaseModel):
    """Process information."""
    pid: int
    name: str
    cpu_percent: float
    memory_percent: float
    memory_mb: float
    status: str
    user: str
    command: Optional[str] = None


class ProcessListResponse(BaseModel):
    """Response for process list."""
    processes: List[ProcessInfo]
    total: int


def _get_uptime() -> str:
    """Get system uptime as a human-readable string."""
    try:
        with open("/proc/uptime", "r") as f:
            uptime_seconds = float(f.read().split()[0])
        
        days = int(uptime_seconds // 86400)
        hours = int((uptime_seconds % 86400) // 3600)
        minutes = int((uptime_seconds % 3600) // 60)
        
        parts = []
        if days > 0:
            parts.append(f"{days}d")
        if hours > 0:
            parts.append(f"{hours}h")
        parts.append(f"{minutes}m")
        
        return " ".join(parts)
    except Exception:
        return "unknown"


def _get_system_info() -> SystemInfo:
    """Get comprehensive system information."""
    try:
        import psutil
    except ImportError:
        raise HTTPException(
            status_code=500,
            detail="psutil not installed. Install with: pip install wasm-cli[web]"
        )
    
    # Hostname
    hostname = os.uname().nodename
    
    # OS info
    try:
        with open("/etc/os-release", "r") as f:
            os_info = {}
            for line in f:
                if "=" in line:
                    key, value = line.strip().split("=", 1)
                    os_info[key] = value.strip('"')
        os_name = os_info.get("PRETTY_NAME", "Linux")
    except Exception:
        os_name = "Linux"
    
    # Kernel
    kernel = os.uname().release
    
    # CPU
    cpu_percent = psutil.cpu_percent(interval=0.1)
    load_avg = os.getloadavg()
    cpu_info = CpuInfo(
        cores=psutil.cpu_count(),
        percent=cpu_percent,
        load_1min=load_avg[0],
        load_5min=load_avg[1],
        load_15min=load_avg[2]
    )
    
    # Memory
    mem = psutil.virtual_memory()
    swap = psutil.swap_memory()
    memory_info = MemoryInfo(
        total_gb=round(mem.total / (1024**3), 2),
        used_gb=round(mem.used / (1024**3), 2),
        free_gb=round(mem.free / (1024**3), 2),
        available_gb=round(mem.available / (1024**3), 2),
        percent_used=mem.percent,
        swap_total_gb=round(swap.total / (1024**3), 2),
        swap_used_gb=round(swap.used / (1024**3), 2),
        swap_percent=swap.percent
    )
    
    # Disks
    disks = []
    for partition in psutil.disk_partitions():
        try:
            usage = psutil.disk_usage(partition.mountpoint)
            disks.append(DiskInfo(
                device=partition.device,
                mount_point=partition.mountpoint,
                total_gb=round(usage.total / (1024**3), 2),
                used_gb=round(usage.used / (1024**3), 2),
                free_gb=round(usage.free / (1024**3), 2),
                percent_used=usage.percent
            ))
        except (PermissionError, OSError):
            continue
    
    return SystemInfo(
        hostname=hostname,
        os=os_name,
        kernel=kernel,
        uptime=_get_uptime(),
        cpu=cpu_info,
        memory=memory_info,
        disks=disks
    )


@router.get("", response_model=SystemInfo)
async def get_system_info(
    request: Request,
    session: dict = Depends(get_current_session)
):
    """
    Get comprehensive system information.
    """
    return _get_system_info()


@router.get("/cpu", response_model=CpuInfo)
async def get_cpu_info(
    request: Request,
    session: dict = Depends(get_current_session)
):
    """
    Get CPU information and usage.
    """
    try:
        import psutil
    except ImportError:
        raise HTTPException(
            status_code=500,
            detail="psutil not installed"
        )
    
    cpu_percent = psutil.cpu_percent(interval=0.5)
    load_avg = os.getloadavg()
    
    return CpuInfo(
        cores=psutil.cpu_count(),
        percent=cpu_percent,
        load_1min=load_avg[0],
        load_5min=load_avg[1],
        load_15min=load_avg[2]
    )


@router.get("/memory", response_model=MemoryInfo)
async def get_memory_info(
    request: Request,
    session: dict = Depends(get_current_session)
):
    """
    Get memory usage information.
    """
    try:
        import psutil
    except ImportError:
        raise HTTPException(
            status_code=500,
            detail="psutil not installed"
        )
    
    mem = psutil.virtual_memory()
    swap = psutil.swap_memory()
    
    return MemoryInfo(
        total_gb=round(mem.total / (1024**3), 2),
        used_gb=round(mem.used / (1024**3), 2),
        free_gb=round(mem.free / (1024**3), 2),
        available_gb=round(mem.available / (1024**3), 2),
        percent_used=mem.percent,
        swap_total_gb=round(swap.total / (1024**3), 2),
        swap_used_gb=round(swap.used / (1024**3), 2),
        swap_percent=swap.percent
    )


@router.get("/disks", response_model=List[DiskInfo])
async def get_disk_info(
    request: Request,
    session: dict = Depends(get_current_session)
):
    """
    Get disk usage information.
    """
    try:
        import psutil
    except ImportError:
        raise HTTPException(
            status_code=500,
            detail="psutil not installed"
        )
    
    disks = []
    for partition in psutil.disk_partitions():
        try:
            usage = psutil.disk_usage(partition.mountpoint)
            disks.append(DiskInfo(
                device=partition.device,
                mount_point=partition.mountpoint,
                total_gb=round(usage.total / (1024**3), 2),
                used_gb=round(usage.used / (1024**3), 2),
                free_gb=round(usage.free / (1024**3), 2),
                percent_used=usage.percent
            ))
        except (PermissionError, OSError):
            continue
    
    return disks


@router.get("/processes", response_model=ProcessListResponse)
async def get_processes(
    request: Request,
    limit: int = 50,
    sort_by: str = "cpu",
    session: dict = Depends(get_current_session)
):
    """
    Get list of running processes.
    """
    try:
        import psutil
    except ImportError:
        raise HTTPException(
            status_code=500,
            detail="psutil not installed"
        )
    
    processes = []
    
    for proc in psutil.process_iter(['pid', 'name', 'cpu_percent', 'memory_percent', 
                                      'memory_info', 'status', 'username', 'cmdline']):
        try:
            info = proc.info
            processes.append(ProcessInfo(
                pid=info['pid'],
                name=info['name'],
                cpu_percent=info['cpu_percent'] or 0,
                memory_percent=info['memory_percent'] or 0,
                memory_mb=round((info['memory_info'].rss if info['memory_info'] else 0) / (1024**2), 2),
                status=info['status'],
                user=info['username'] or 'unknown',
                command=' '.join(info['cmdline'][:5]) if info['cmdline'] else None
            ))
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
    
    # Sort
    if sort_by == "cpu":
        processes.sort(key=lambda x: x.cpu_percent, reverse=True)
    elif sort_by == "memory":
        processes.sort(key=lambda x: x.memory_percent, reverse=True)
    elif sort_by == "pid":
        processes.sort(key=lambda x: x.pid)
    
    return ProcessListResponse(
        processes=processes[:limit],
        total=len(processes)
    )


@router.post("/processes/{pid}/kill")
async def kill_process(
    pid: int,
    request: Request,
    signal: int = 15,  # SIGTERM by default
    session: dict = Depends(get_current_session)
):
    """
    Kill a process by PID.
    
    signal: 15 = SIGTERM (graceful), 9 = SIGKILL (force)
    """
    try:
        import psutil
    except ImportError:
        raise HTTPException(
            status_code=500,
            detail="psutil not installed"
        )
    
    if signal not in [9, 15]:
        raise HTTPException(status_code=400, detail="Only signals 9 (KILL) and 15 (TERM) are allowed")
    
    try:
        proc = psutil.Process(pid)
        proc_name = proc.name()
        
        if signal == 15:
            proc.terminate()
        else:
            proc.kill()
        
        return {
            "success": True,
            "message": f"Signal {signal} sent to process {pid} ({proc_name})",
            "pid": pid
        }
    except psutil.NoSuchProcess:
        raise HTTPException(status_code=404, detail=f"Process {pid} not found")
    except psutil.AccessDenied:
        raise HTTPException(status_code=403, detail=f"Permission denied to kill process {pid}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/network")
async def get_network_info(
    request: Request,
    session: dict = Depends(get_current_session)
):
    """
    Get network interface information.
    """
    try:
        import psutil
    except ImportError:
        raise HTTPException(
            status_code=500,
            detail="psutil not installed"
        )
    
    interfaces = []
    
    addrs = psutil.net_if_addrs()
    stats = psutil.net_if_stats()
    io_counters = psutil.net_io_counters(pernic=True)
    
    for iface_name, iface_addrs in addrs.items():
        iface_info = {
            "name": iface_name,
            "addresses": [],
            "is_up": stats.get(iface_name, {}).isup if iface_name in stats else False,
            "speed_mbps": stats.get(iface_name, {}).speed if iface_name in stats else 0,
        }
        
        for addr in iface_addrs:
            if addr.family.name == "AF_INET":
                iface_info["addresses"].append({
                    "type": "IPv4",
                    "address": addr.address,
                    "netmask": addr.netmask
                })
            elif addr.family.name == "AF_INET6":
                iface_info["addresses"].append({
                    "type": "IPv6",
                    "address": addr.address
                })
        
        if iface_name in io_counters:
            io = io_counters[iface_name]
            iface_info["bytes_sent"] = io.bytes_sent
            iface_info["bytes_recv"] = io.bytes_recv
            iface_info["packets_sent"] = io.packets_sent
            iface_info["packets_recv"] = io.packets_recv
        
        interfaces.append(iface_info)
    
    return {"interfaces": interfaces}
