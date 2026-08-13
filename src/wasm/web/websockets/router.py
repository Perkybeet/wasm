"""
WebSocket router for real-time features.

Handshakes are authenticated and rate limited by
:class:`~wasm.web.server.SecurityMiddleware` before a handler is ever reached,
so a route added here cannot forget to check credentials. The middleware leaves
the session payload in ``scope["state"]["session"]``; the handlers below read it
back through :func:`authenticate_websocket`, which re-verifies from scratch if
the payload is absent so that the handlers are still safe on their own.

Credentials travel in the session cookie, in the ``Sec-WebSocket-Protocol``
header, or as a single-use ticket from ``POST /api/auth/ws-ticket``. A
long-lived token is never accepted in the query string, because query strings
are recorded by browsers, proxies and access logs.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect

from wasm.core.exceptions import ValidationError
from wasm.core.utils import domain_to_app_name
from wasm.validators.names import validate_service_name
from wasm.web.auth import (
    WS_CLOSE_UNAUTHORIZED,
    WS_SUBPROTOCOL,
    WS_TOKEN_PREFIX,
    authenticate_connection,
    get_audit_logger,
    get_client_ip,
)

logger = logging.getLogger(__name__)

router = APIRouter()

__all__ = [
    "WS_CLOSE_UNAUTHORIZED",
    "WS_SUBPROTOCOL",
    "WS_TOKEN_PREFIX",
    "authenticate_websocket",
    "router",
]

#: Upper bound on how long one journal stream may run before the client has to
#: reconnect. A follow with no deadline is a process that outlives its session.
LOG_STREAM_MAX_SECONDS = 12 * 3600

# Active WebSocket connections
_log_connections: dict[str, set[WebSocket]] = {}
_system_connections: set[WebSocket] = set()

#: How long a journal follow is given to exit after being asked politely.
TERMINATE_GRACE_SECONDS = 2.0


async def _terminate(process: asyncio.subprocess.Process | None) -> None:
    """
    Stop a journal follow, and make sure it is really gone.

    Every stream in this module spawns a ``journalctl -f``, which by definition
    never ends on its own. Whether it is cleaned up therefore decides whether a
    long-lived panel accumulates one orphaned process per connection, and a
    browser reconnects on its own: this is the difference between a server that
    is stable for months and one that runs out of processes.

    Args:
        process: The process to stop, if one was started at all.
    """
    if process is None or process.returncode is not None:
        return

    try:
        process.terminate()
        await asyncio.wait_for(process.wait(), timeout=TERMINATE_GRACE_SECONDS)
    except (ProcessLookupError, asyncio.TimeoutError):
        # It ignored SIGTERM, or it was reaped between the check and the
        # signal. Either way, do not leave it behind.
        try:
            process.kill()
        except ProcessLookupError:
            pass


async def authenticate_websocket(
    websocket: WebSocket, ticket: str | None = None
) -> dict[str, Any] | None:
    """
    Return the session the middleware authenticated for this handshake.

    Args:
        websocket: The pending connection.
        ticket: Single-use ticket from ``POST /api/auth/ws-ticket``, if any.

    Returns:
        The session payload when the handshake is authenticated, None otherwise.
    """
    session = websocket.scope.get("state", {}).get("session")
    if isinstance(session, dict):
        return session
    # Defence in depth: a handler must not serve the journal just because the
    # middleware was left out of an embedding application.
    return authenticate_connection(websocket, ticket)


async def _reject(websocket: WebSocket, path: str) -> None:
    """
    Close an unauthenticated handshake and record it.

    Args:
        websocket: The pending connection.
        path: Resource the client tried to reach, for the audit trail.
    """
    audit = get_audit_logger()
    if audit:
        audit.record(
            action="ws.connect",
            result="denied",
            client_ip=get_client_ip(websocket),
            resource=path,
            detail="no valid cookie, subprotocol token or ticket",
        )
    await websocket.close(code=WS_CLOSE_UNAUTHORIZED, reason="Authentication required")


async def _accept(websocket: WebSocket, session: dict[str, Any], path: str) -> None:
    """
    Accept an authenticated handshake and record it.

    Args:
        websocket: The pending connection.
        session: The authenticated session payload.
        path: Resource being streamed, for the audit trail.
    """
    subprotocol = (
        WS_SUBPROTOCOL
        if WS_SUBPROTOCOL in websocket.headers.get("sec-websocket-protocol", "")
        else None
    )
    await websocket.accept(subprotocol=subprotocol)

    audit = get_audit_logger()
    if audit:
        audit.record(
            action="ws.connect",
            result="success",
            client_ip=get_client_ip(websocket),
            actor=str(session.get("sid")),
            resource=path,
        )


@router.websocket("/logs/{domain}")
async def websocket_logs(
    websocket: WebSocket,
    domain: str,
    ticket: str | None = Query(default=None),
    lines: int = Query(default=50, ge=1, le=500),
):
    """
    Stream application logs in real-time.

    Connect with the session cookie, with ``Sec-WebSocket-Protocol:
    wasm.auth, wasm.token.<token>``, or with ``?ticket=<single-use ticket>``.

    Args:
        websocket: The client connection.
        domain: Domain whose service logs are streamed.
        ticket: Optional single-use handshake ticket.
        lines: Backlog of log lines to send first.
    """
    session = await authenticate_websocket(websocket, ticket)
    if session is None:
        await _reject(websocket, f"/ws/logs/{domain}")
        return

    await _accept(websocket, session, f"/ws/logs/{domain}")

    from wasm.managers.service_manager import ServiceManager

    try:
        # The domain is client supplied and ends up as a journalctl unit
        # selector, where '*' and '/' are not inert. Validate before spawning.
        app_name = domain_to_app_name(domain)
        service_manager = ServiceManager(verbose=False)
        service_name = validate_service_name(service_manager._resolve_service_name(app_name))
    except ValidationError as exc:
        await websocket.send_json({"type": "error", "message": f"Invalid domain: {exc}"})
        await websocket.close()
        return

    # Add to connections
    if domain not in _log_connections:
        _log_connections[domain] = set()
    _log_connections[domain].add(websocket)

    process = None

    try:
        # Check if journalctl exists
        import shutil

        if not shutil.which("journalctl"):
            await websocket.send_json(
                {
                    "type": "error",
                    "message": "journalctl not found. Log streaming requires systemd.",
                }
            )
            return

        # Start journalctl follow process
        process = await asyncio.create_subprocess_exec(
            "journalctl",
            "-u",
            service_name,
            "-f",
            "-n",
            str(lines),
            "--no-pager",
            "-o",
            "short-iso",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        # Send initial message
        await websocket.send_json({"type": "connected", "domain": domain, "service": service_name})

        if process.stdout is None or process.stderr is None:
            await websocket.send_json({"type": "error", "message": "journalctl produced no output"})
            return

        stdout = process.stdout
        stderr = process.stderr

        # Check for immediate stderr (e.g., service not found)
        async def check_stderr() -> None:
            try:
                stderr_data = await asyncio.wait_for(stderr.read(1024), timeout=0.5)
                if stderr_data:
                    error_msg = stderr_data.decode("utf-8", errors="replace").strip()
                    if error_msg:
                        await websocket.send_json(
                            {"type": "warning", "data": f"journalctl: {error_msg}"}
                        )
            except asyncio.TimeoutError:
                pass  # No stderr available yet, this is normal
            except Exception:
                pass  # WebSocket may be closed, ignore

        await check_stderr()

        # Stream logs
        async def read_logs() -> None:
            while True:
                try:
                    line = await stdout.readline()
                    if not line:
                        # Check if process exited
                        if process.returncode is not None:
                            break
                        continue

                    log_line = line.decode("utf-8", errors="replace").strip()
                    if log_line:
                        await websocket.send_json({"type": "log", "data": log_line})
                except Exception:
                    break  # Connection closed or process terminated

        # Handle incoming messages (for ping/pong or commands)
        async def handle_messages() -> None:
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

        _done, pending = await asyncio.wait(
            [log_task, msg_task],
            timeout=LOG_STREAM_MAX_SECONDS,
            return_when=asyncio.FIRST_COMPLETED,
        )

        # Cancel pending tasks
        for task in pending:
            task.cancel()

    except WebSocketDisconnect:
        pass
    except Exception as e:
        try:
            await websocket.send_json({"type": "error", "message": str(e)})
        except Exception:
            pass
    finally:
        await _terminate(process)

        # Remove from connections
        if domain in _log_connections:
            _log_connections[domain].discard(websocket)

        try:
            await websocket.close()
        except Exception:
            pass  # WebSocket already closed


@router.websocket("/system")
async def websocket_system(
    websocket: WebSocket,
    ticket: str | None = Query(default=None),
    interval: float = Query(default=2.0, ge=0.5, le=30.0),
):
    """
    Stream system metrics in real-time.

    Args:
        websocket: The client connection.
        ticket: Optional single-use handshake ticket.
        interval: Seconds between metric samples.
    """
    session = await authenticate_websocket(websocket, ticket)
    if session is None:
        await _reject(websocket, "/ws/system")
        return

    await _accept(websocket, session, "/ws/system")
    _system_connections.add(websocket)

    try:
        import psutil
    except ImportError:
        await websocket.send_json({"type": "error", "message": "psutil not installed"})
        await websocket.close()
        return

    try:
        await websocket.send_json({"type": "connected", "interval": interval})

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
                "cpu": {"percent": cpu_percent, "cores": psutil.cpu_count()},
                "memory": {
                    "percent": mem.percent,
                    "used_gb": round(mem.used / (1024**3), 2),
                    "total_gb": round(mem.total / (1024**3), 2),
                },
                "disk": {"percent": disk_percent},
                "load": {"1min": load[0], "5min": load[1], "15min": load[2]},
                "network": {"bytes_sent": net_io.bytes_sent, "bytes_recv": net_io.bytes_recv},
            }

            await websocket.send_json(metrics)

            # Wait for next interval or message
            try:
                msg = await asyncio.wait_for(websocket.receive_text(), timeout=interval)
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
            await websocket.send_json({"type": "error", "message": str(e)})
        except Exception:
            pass
    finally:
        _system_connections.discard(websocket)
        try:
            await websocket.close()
        except Exception:
            pass


@router.websocket("/events")
async def websocket_events(websocket: WebSocket, ticket: str | None = Query(default=None)):
    """
    Stream system events (service changes, deployments, etc).

    Args:
        websocket: The client connection.
        ticket: Optional single-use handshake ticket.
    """
    session = await authenticate_websocket(websocket, ticket)
    if session is None:
        await _reject(websocket, "/ws/events")
        return

    await _accept(websocket, session, "/ws/events")

    # Bound before the try, so the cleanup below can always see it. It used to
    # be assigned inside, and the terminate() was on the happy path: closing
    # the browser tab raises WebSocketDisconnect long before that line, so
    # every connection left a journalctl -f running forever. A panel that
    # reconnects on its own accumulates one per reconnection.
    process: asyncio.subprocess.Process | None = None

    try:
        await websocket.send_json({"type": "connected", "message": "Listening for system events"})

        # Watch systemd events using journalctl
        process = await asyncio.create_subprocess_exec(
            "journalctl",
            "-f",
            "-n",
            "0",
            "--no-pager",
            "-o",
            "json",
            "-u",
            "wasm-*",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        if process.stdout is None:
            await websocket.send_json({"type": "error", "message": "journalctl produced no output"})
            return

        stdout = process.stdout

        async def read_events() -> None:
            while True:
                try:
                    line = await stdout.readline()
                    if not line:
                        break

                    try:
                        event = json.loads(line.decode("utf-8"))
                        await websocket.send_json(
                            {
                                "type": "event",
                                "unit": event.get("_SYSTEMD_UNIT", ""),
                                "message": event.get("MESSAGE", ""),
                                "priority": event.get("PRIORITY", 6),
                                "timestamp": event.get("__REALTIME_TIMESTAMP", ""),
                            }
                        )
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

        _done, pending = await asyncio.wait(
            [event_task, msg_task], return_when=asyncio.FIRST_COMPLETED
        )

        for task in pending:
            task.cancel()

    except WebSocketDisconnect:
        pass
    except Exception as e:
        try:
            await websocket.send_json({"type": "error", "message": str(e)})
        except Exception:
            pass
    finally:
        await _terminate(process)

        _system_connections.discard(websocket)

        try:
            await websocket.close()
        except Exception:
            pass


# Job connections: job_id -> set of websockets
_job_connections: dict[str, set[WebSocket]] = {}
_all_jobs_connections: set[WebSocket] = set()


@router.websocket("/jobs/{job_id}")
async def websocket_job(
    websocket: WebSocket,
    job_id: str,
    ticket: str | None = Query(default=None),
):
    """
    Stream updates for a specific job in real-time.

    Args:
        websocket: The client connection.
        job_id: Identifier of the job to follow.
        ticket: Optional single-use handshake ticket.
    """
    from wasm.web.jobs import get_job_manager

    session = await authenticate_websocket(websocket, ticket)
    if session is None:
        await _reject(websocket, f"/ws/jobs/{job_id}")
        return

    await _accept(websocket, session, f"/ws/jobs/{job_id}")

    manager = get_job_manager()
    job = manager.get_job(job_id)

    if not job:
        await websocket.send_json({"type": "error", "message": f"Job {job_id} not found"})
        await websocket.close()
        return

    # Add to connections
    if job_id not in _job_connections:
        _job_connections[job_id] = set()
    _job_connections[job_id].add(websocket)

    # Queue for job updates
    update_queue: asyncio.Queue[dict[str, object]] = asyncio.Queue()

    # Capture the current event loop for thread-safe callback
    loop = asyncio.get_running_loop()

    def on_job_update(updated_job):
        """Callback when job is updated (called from worker thread)."""
        if updated_job.id == job_id:
            try:
                loop.call_soon_threadsafe(update_queue.put_nowait, updated_job.to_dict())
            except Exception:
                pass

    # Subscribe to job updates
    manager.subscribe(job_id, on_job_update)

    try:
        # Send initial state
        await websocket.send_json({"type": "connected", "job": job.to_dict()})

        async def send_updates():
            while True:
                try:
                    job_data = await asyncio.wait_for(update_queue.get(), timeout=30.0)
                    await websocket.send_json({"type": "update", "job": job_data})

                    # Check if job is complete
                    if job_data.get("status") in ["completed", "failed", "cancelled"]:
                        await websocket.send_json({"type": "finished", "job": job_data})
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
                            await websocket.send_json({"type": "cancelled", "job_id": job_id})
                except WebSocketDisconnect:
                    break
                except Exception:
                    break

        update_task = asyncio.create_task(send_updates())
        msg_task = asyncio.create_task(handle_messages())

        _done, pending = await asyncio.wait(
            [update_task, msg_task], return_when=asyncio.FIRST_COMPLETED
        )

        for task in pending:
            task.cancel()

    except WebSocketDisconnect:
        pass
    except Exception as e:
        try:
            await websocket.send_json({"type": "error", "message": str(e)})
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
    ticket: str | None = Query(default=None),
):
    """
    Stream updates for all jobs in real-time.

    Args:
        websocket: The client connection.
        ticket: Optional single-use handshake ticket.
    """
    from wasm.web.jobs import get_job_manager

    session = await authenticate_websocket(websocket, ticket)
    if session is None:
        await _reject(websocket, "/ws/jobs")
        return

    await _accept(websocket, session, "/ws/jobs")
    _all_jobs_connections.add(websocket)

    manager = get_job_manager()

    # Queue for job updates
    update_queue: asyncio.Queue[dict[str, object]] = asyncio.Queue()

    # Capture the current event loop for thread-safe callback
    loop = asyncio.get_running_loop()

    def on_any_job_update(job):
        """Callback when any job is updated (called from worker thread)."""
        try:
            loop.call_soon_threadsafe(update_queue.put_nowait, job.to_dict())
        except Exception:
            pass

    # Subscribe to all job updates
    manager.subscribe_all(on_any_job_update)

    try:
        # Send current jobs
        jobs = manager.get_all_jobs(limit=20)
        await websocket.send_json(
            {
                "type": "connected",
                "jobs": [j.to_dict() for j in jobs],
                "active": len(manager.get_active_jobs()),
            }
        )

        async def send_updates():
            while True:
                try:
                    job_data = await asyncio.wait_for(update_queue.get(), timeout=30.0)
                    await websocket.send_json({"type": "job_update", "job": job_data})
                except asyncio.TimeoutError:
                    # Send heartbeat with active count
                    await websocket.send_json(
                        {"type": "heartbeat", "active": len(manager.get_active_jobs())}
                    )
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
                        await websocket.send_json(
                            {"type": "jobs_list", "jobs": [j.to_dict() for j in jobs]}
                        )
                except WebSocketDisconnect:
                    break
                except Exception:
                    break

        update_task = asyncio.create_task(send_updates())
        msg_task = asyncio.create_task(handle_messages())

        _done, pending = await asyncio.wait(
            [update_task, msg_task], return_when=asyncio.FIRST_COMPLETED
        )

        for task in pending:
            task.cancel()

    except WebSocketDisconnect:
        pass
    except Exception as e:
        try:
            await websocket.send_json({"type": "error", "message": str(e)})
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
