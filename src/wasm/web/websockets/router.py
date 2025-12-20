"""
WebSocket router for real-time features.
"""

import asyncio
import json
import subprocess
from typing import Dict, Set

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Query

from wasm.web.server import get_token_manager
from wasm.web.auth import get_client_ip
from wasm.core.utils import domain_to_app_name

router = APIRouter()

# Active WebSocket connections
_log_connections: Dict[str, Set[WebSocket]] = {}
_system_connections: Set[WebSocket] = set()


async def _verify_websocket_token(websocket: WebSocket, token: str) -> bool:
    """Verify the WebSocket connection token."""
    token_manager = get_token_manager()
    
    # Get client IP (WebSocket doesn't have the same request object)
    client_ip = websocket.client.host if websocket.client else "unknown"
    
    # Try session token first
    payload = token_manager.verify_session_token(token, client_ip)
    if payload:
        return True
    
    # Try master token
    if token_manager.verify_master_token(token):
        return True
    
    return False


@router.websocket("/logs/{domain}")
async def websocket_logs(
    websocket: WebSocket,
    domain: str,
    token: str = Query(...),
    lines: int = Query(default=50, ge=1, le=500)
):
    """
    Stream application logs in real-time.
    
    Connect with: ws://host:port/ws/logs/{domain}?token=xxx
    """
    # Verify token
    if not await _verify_websocket_token(websocket, token):
        await websocket.close(code=4001, reason="Invalid token")
        return
    
    await websocket.accept()
    
    app_name = domain_to_app_name(domain)
    service_name = f"wasm-{app_name}"
    
    # Add to connections
    if domain not in _log_connections:
        _log_connections[domain] = set()
    _log_connections[domain].add(websocket)
    
    process = None
    
    try:
        # Check if journalctl exists
        import shutil
        if not shutil.which("journalctl"):
            await websocket.send_json({
                "type": "error",
                "message": "journalctl not found. Log streaming requires systemd."
            })
            return
        
        # Start journalctl follow process
        process = await asyncio.create_subprocess_exec(
            "journalctl",
            "-u", service_name,
            "-f",
            "-n", str(lines),
            "--no-pager",
            "-o", "short-iso",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        
        # Send initial message
        await websocket.send_json({
            "type": "connected",
            "domain": domain,
            "service": service_name
        })
        
        # Check for immediate stderr (e.g., service not found)
        async def check_stderr():
            try:
                stderr_data = await asyncio.wait_for(
                    process.stderr.read(1024),
                    timeout=0.5
                )
                if stderr_data:
                    error_msg = stderr_data.decode("utf-8", errors="replace").strip()
                    if error_msg:
                        await websocket.send_json({
                            "type": "warning",
                            "data": f"journalctl: {error_msg}"
                        })
            except asyncio.TimeoutError:
                pass
            except Exception:
                pass
        
        await check_stderr()
        
        # Stream logs
        async def read_logs():
            while True:
                try:
                    line = await process.stdout.readline()
                    if not line:
                        # Check if process exited
                        if process.returncode is not None:
                            break
                        continue
                    
                    log_line = line.decode("utf-8", errors="replace").strip()
                    if log_line:
                        await websocket.send_json({
                            "type": "log",
                            "data": log_line
                        })
                except Exception:
                    break
        
        # Handle incoming messages (for ping/pong or commands)
        async def handle_messages():
            while True:
                try:
                    data = await websocket.receive_text()
                    msg = json.loads(data)
                    
                    if msg.get("type") == "ping":
                        await websocket.send_json({"type": "pong"})
                except WebSocketDisconnect:
                    break
                except Exception:
                    break
        
        # Run both tasks
        log_task = asyncio.create_task(read_logs())
        msg_task = asyncio.create_task(handle_messages())
        
        done, pending = await asyncio.wait(
            [log_task, msg_task],
            return_when=asyncio.FIRST_COMPLETED
        )
        
        # Cancel pending tasks
        for task in pending:
            task.cancel()
        
    except WebSocketDisconnect:
        pass
    except Exception as e:
        try:
            await websocket.send_json({
                "type": "error",
                "message": str(e)
            })
        except Exception:
            pass
    finally:
        # Kill process if running
        if process and process.returncode is None:
            try:
                process.terminate()
                await asyncio.wait_for(process.wait(), timeout=2.0)
            except Exception:
                try:
                    process.kill()
                except Exception:
                    pass
        
        # Remove from connections
        if domain in _log_connections:
            _log_connections[domain].discard(websocket)
        
        try:
            await websocket.close()
        except Exception:
            pass


@router.websocket("/system")
async def websocket_system(
    websocket: WebSocket,
    token: str = Query(...),
    interval: float = Query(default=2.0, ge=0.5, le=30.0)
):
    """
    Stream system metrics in real-time.
    
    Connect with: ws://host:port/ws/system?token=xxx&interval=2
    """
    # Verify token
    if not await _verify_websocket_token(websocket, token):
        await websocket.close(code=4001, reason="Invalid token")
        return
    
    await websocket.accept()
    _system_connections.add(websocket)
    
    try:
        import psutil
    except ImportError:
        await websocket.send_json({
            "type": "error",
            "message": "psutil not installed"
        })
        await websocket.close()
        return
    
    try:
        await websocket.send_json({
            "type": "connected",
            "interval": interval
        })
        
        while True:
            # Gather system metrics
            cpu_percent = psutil.cpu_percent(interval=0.1)
            mem = psutil.virtual_memory()
            
            # Get disk for root
            try:
                disk = psutil.disk_usage("/")
                disk_percent = disk.percent
            except Exception:
                disk_percent = 0
            
            # Get load average
            load = list(psutil.getloadavg())
            
            # Network I/O
            net_io = psutil.net_io_counters()
            
            metrics = {
                "type": "metrics",
                "timestamp": asyncio.get_event_loop().time(),
                "cpu": {
                    "percent": cpu_percent,
                    "cores": psutil.cpu_count()
                },
                "memory": {
                    "percent": mem.percent,
                    "used_gb": round(mem.used / (1024**3), 2),
                    "total_gb": round(mem.total / (1024**3), 2)
                },
                "disk": {
                    "percent": disk_percent
                },
                "load": {
                    "1min": load[0],
                    "5min": load[1],
                    "15min": load[2]
                },
                "network": {
                    "bytes_sent": net_io.bytes_sent,
                    "bytes_recv": net_io.bytes_recv
                }
            }
            
            await websocket.send_json(metrics)
            
            # Wait for next interval or message
            try:
                msg = await asyncio.wait_for(
                    websocket.receive_text(),
                    timeout=interval
                )
                data = json.loads(msg)
                
                if data.get("type") == "ping":
                    await websocket.send_json({"type": "pong"})
                elif data.get("type") == "close":
                    break
                    
            except asyncio.TimeoutError:
                # Normal - continue to next iteration
                pass
    
    except WebSocketDisconnect:
        pass
    except Exception as e:
        try:
            await websocket.send_json({
                "type": "error",
                "message": str(e)
            })
        except Exception:
            pass
    finally:
        _system_connections.discard(websocket)
        try:
            await websocket.close()
        except Exception:
            pass


@router.websocket("/events")
async def websocket_events(
    websocket: WebSocket,
    token: str = Query(...)
):
    """
    Stream system events (service changes, deployments, etc).
    
    Connect with: ws://host:port/ws/events?token=xxx
    """
    # Verify token
    if not await _verify_websocket_token(websocket, token):
        await websocket.close(code=4001, reason="Invalid token")
        return
    
    await websocket.accept()
    
    try:
        await websocket.send_json({
            "type": "connected",
            "message": "Listening for system events"
        })
        
        # Watch systemd events using journalctl
        process = await asyncio.create_subprocess_exec(
            "journalctl",
            "-f",
            "-n", "0",
            "--no-pager",
            "-o", "json",
            "-u", "wasm-*",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        
        async def read_events():
            while True:
                try:
                    line = await process.stdout.readline()
                    if not line:
                        break
                    
                    try:
                        event = json.loads(line.decode("utf-8"))
                        await websocket.send_json({
                            "type": "event",
                            "unit": event.get("_SYSTEMD_UNIT", ""),
                            "message": event.get("MESSAGE", ""),
                            "priority": event.get("PRIORITY", 6),
                            "timestamp": event.get("__REALTIME_TIMESTAMP", "")
                        })
                    except json.JSONDecodeError:
                        pass
                except Exception:
                    break
        
        async def handle_messages():
            while True:
                try:
                    data = await websocket.receive_text()
                    msg = json.loads(data)
                    
                    if msg.get("type") == "ping":
                        await websocket.send_json({"type": "pong"})
                except WebSocketDisconnect:
                    break
                except Exception:
                    break
        
        event_task = asyncio.create_task(read_events())
        msg_task = asyncio.create_task(handle_messages())
        
        done, pending = await asyncio.wait(
            [event_task, msg_task],
            return_when=asyncio.FIRST_COMPLETED
        )
        
        for task in pending:
            task.cancel()
        
        process.terminate()
        
    except WebSocketDisconnect:
        pass
    except Exception as e:
        try:
            await websocket.send_json({
                "type": "error",
                "message": str(e)
            })
        except Exception:
            pass
    finally:
        try:
            await websocket.close()
        except Exception:
            pass


# Job connections: job_id -> set of websockets
_job_connections: Dict[str, Set[WebSocket]] = {}
_all_jobs_connections: Set[WebSocket] = set()


@router.websocket("/jobs/{job_id}")
async def websocket_job(
    websocket: WebSocket,
    job_id: str,
    token: str = Query(...),
):
    """
    Stream updates for a specific job in real-time.
    
    Connect with: ws://host:port/ws/jobs/{job_id}?token=xxx
    """
    from wasm.web.jobs import get_job_manager
    
    # Verify token
    if not await _verify_websocket_token(websocket, token):
        await websocket.close(code=4001, reason="Invalid token")
        return
    
    await websocket.accept()
    
    manager = get_job_manager()
    job = manager.get_job(job_id)
    
    if not job:
        await websocket.send_json({
            "type": "error",
            "message": f"Job {job_id} not found"
        })
        await websocket.close()
        return
    
    # Add to connections
    if job_id not in _job_connections:
        _job_connections[job_id] = set()
    _job_connections[job_id].add(websocket)
    
    # Queue for job updates
    update_queue = asyncio.Queue()
    
    # Capture the current event loop for thread-safe callback
    loop = asyncio.get_running_loop()
    
    def on_job_update(updated_job):
        """Callback when job is updated (called from worker thread)."""
        if updated_job.id == job_id:
            try:
                loop.call_soon_threadsafe(
                    update_queue.put_nowait,
                    updated_job.to_dict()
                )
            except Exception as e:
                print(f"[WS] Error queueing job update: {e}")
    
    # Subscribe to job updates
    manager.subscribe(job_id, on_job_update)
    
    try:
        # Send initial state
        await websocket.send_json({
            "type": "connected",
            "job": job.to_dict()
        })
        
        async def send_updates():
            while True:
                try:
                    job_data = await asyncio.wait_for(update_queue.get(), timeout=30.0)
                    await websocket.send_json({
                        "type": "update",
                        "job": job_data
                    })
                    
                    # Check if job is complete
                    if job_data.get("status") in ["completed", "failed", "cancelled"]:
                        await websocket.send_json({
                            "type": "finished",
                            "job": job_data
                        })
                        break
                except asyncio.TimeoutError:
                    # Send heartbeat
                    await websocket.send_json({"type": "heartbeat"})
                except Exception:
                    break
        
        async def handle_messages():
            while True:
                try:
                    data = await websocket.receive_text()
                    msg = json.loads(data)
                    
                    if msg.get("type") == "ping":
                        await websocket.send_json({"type": "pong"})
                    elif msg.get("type") == "cancel":
                        if manager.cancel_job(job_id):
                            await websocket.send_json({
                                "type": "cancelled",
                                "job_id": job_id
                            })
                except WebSocketDisconnect:
                    break
                except Exception:
                    break
        
        update_task = asyncio.create_task(send_updates())
        msg_task = asyncio.create_task(handle_messages())
        
        done, pending = await asyncio.wait(
            [update_task, msg_task],
            return_when=asyncio.FIRST_COMPLETED
        )
        
        for task in pending:
            task.cancel()
        
    except WebSocketDisconnect:
        pass
    except Exception as e:
        try:
            await websocket.send_json({
                "type": "error",
                "message": str(e)
            })
        except Exception:
            pass
    finally:
        # Unsubscribe and cleanup
        manager.unsubscribe(job_id, on_job_update)
        if job_id in _job_connections:
            _job_connections[job_id].discard(websocket)
        try:
            await websocket.close()
        except Exception:
            pass


@router.websocket("/jobs")
async def websocket_all_jobs(
    websocket: WebSocket,
    token: str = Query(...),
):
    """
    Stream updates for all jobs in real-time.
    
    Connect with: ws://host:port/ws/jobs?token=xxx
    """
    from wasm.web.jobs import get_job_manager
    
    # Verify token
    if not await _verify_websocket_token(websocket, token):
        await websocket.close(code=4001, reason="Invalid token")
        return
    
    await websocket.accept()
    _all_jobs_connections.add(websocket)
    
    manager = get_job_manager()
    
    # Queue for job updates
    update_queue = asyncio.Queue()
    
    # Capture the current event loop for thread-safe callback
    loop = asyncio.get_running_loop()
    
    def on_any_job_update(job):
        """Callback when any job is updated (called from worker thread)."""
        try:
            loop.call_soon_threadsafe(
                update_queue.put_nowait,
                job.to_dict()
            )
        except Exception as e:
            print(f"[WS] Error queueing all-jobs update: {e}")
    
    # Subscribe to all job updates
    manager.subscribe_all(on_any_job_update)
    
    try:
        # Send current jobs
        jobs = manager.get_all_jobs(limit=20)
        await websocket.send_json({
            "type": "connected",
            "jobs": [j.to_dict() for j in jobs],
            "active": len(manager.get_active_jobs())
        })
        
        async def send_updates():
            while True:
                try:
                    job_data = await asyncio.wait_for(update_queue.get(), timeout=30.0)
                    await websocket.send_json({
                        "type": "job_update",
                        "job": job_data
                    })
                except asyncio.TimeoutError:
                    # Send heartbeat with active count
                    await websocket.send_json({
                        "type": "heartbeat",
                        "active": len(manager.get_active_jobs())
                    })
                except Exception:
                    break
        
        async def handle_messages():
            while True:
                try:
                    data = await websocket.receive_text()
                    msg = json.loads(data)
                    
                    if msg.get("type") == "ping":
                        await websocket.send_json({"type": "pong"})
                    elif msg.get("type") == "list":
                        jobs = manager.get_all_jobs(limit=50)
                        await websocket.send_json({
                            "type": "jobs_list",
                            "jobs": [j.to_dict() for j in jobs]
                        })
                except WebSocketDisconnect:
                    break
                except Exception:
                    break
        
        update_task = asyncio.create_task(send_updates())
        msg_task = asyncio.create_task(handle_messages())
        
        done, pending = await asyncio.wait(
            [update_task, msg_task],
            return_when=asyncio.FIRST_COMPLETED
        )
        
        for task in pending:
            task.cancel()
        
    except WebSocketDisconnect:
        pass
    except Exception as e:
        try:
            await websocket.send_json({
                "type": "error",
                "message": str(e)
            })
        except Exception:
            pass
    finally:
        # Remove global subscriber
        try:
            manager._global_subscribers.remove(on_any_job_update)
        except ValueError:
            pass
        _all_jobs_connections.discard(websocket)
        try:
            await websocket.close()
        except Exception:
            pass
