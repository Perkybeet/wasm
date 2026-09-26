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
from collections.abc import Coroutine
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect
from starlette.concurrency import run_in_threadpool

from wasm.core.exceptions import ValidationError
from wasm.web.auth import (
    WS_CLOSE_FORBIDDEN,
    WS_CLOSE_UNAUTHORIZED,
    WS_SUBPROTOCOL,
    WS_TOKEN_PREFIX,
    actor_label,
    authenticate_connection,
    credential_is_current,
    get_audit_logger,
    get_client_ip,
    scope_satisfies,
)

if TYPE_CHECKING:  # pragma: no cover - imported for types only
    from wasm.managers.service_manager import UnitOwnership

logger = logging.getLogger(__name__)

router = APIRouter()

__all__ = [
    "WS_CLOSE_UNAUTHORIZED",
    "WS_SUBPROTOCOL",
    "WS_TOKEN_PREFIX",
    "authenticate_websocket",
    "router",
]

#: Upper bound on how long any one socket may stay open before the client has
#: to reconnect - and so authenticate again. A follow with no deadline is a
#: process that outlives its session; a token that never expires does not
#: get a socket that never closes either.
WS_MAX_LIFETIME_SECONDS: float = 12 * 3600

#: How often an open socket asks whether the credential it was opened with is
#: still accepted. Matches the job streams' heartbeat, so a revoked token, a
#: rotated master token or a signed-out session loses its streams within one.
WS_RECHECK_SECONDS: float = 30.0

#: Close code when a socket reaches :data:`WS_MAX_LIFETIME_SECONDS`. Not
#: :data:`WS_CLOSE_UNAUTHORIZED`: the console reads 4401 as "signed out" and
#: stops, while any other code makes it reconnect with a fresh ticket.
WS_CLOSE_LIFETIME = 4408

# Active WebSocket connections
_log_connections: dict[str, set[WebSocket]] = {}

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


async def _watch(session: dict[str, Any]) -> int:
    """
    Wait until a socket has to close, and say why.

    Args:
        session: The payload the socket was opened with.

    Returns:
        :data:`WS_CLOSE_UNAUTHORIZED` once the credential is no longer
        accepted, :data:`WS_CLOSE_LIFETIME` once the socket has been open for
        :data:`WS_MAX_LIFETIME_SECONDS`.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + WS_MAX_LIFETIME_SECONDS
    while True:
        remaining = deadline - loop.time()
        if remaining <= 0:
            return WS_CLOSE_LIFETIME
        await asyncio.sleep(min(WS_RECHECK_SECONDS, remaining))
        # A SQLite read, off the event loop like every other blocking call here.
        if not await run_in_threadpool(credential_is_current, session):
            return WS_CLOSE_UNAUTHORIZED


async def _serve(
    websocket: WebSocket, session: dict[str, Any], *workers: Coroutine[Any, Any, None]
) -> int:
    """
    Run a socket's workers until one of them ends or the socket must close.

    Every route runs its reader and writer through here, next to
    :func:`_watch`, so none of them can forget the re-check or the deadline.

    Args:
        websocket: The accepted connection.
        session: The payload it was opened with.
        *workers: The route's own loops.

    Returns:
        The close code the route should close with: 1000 when a worker ended
        on its own (the client left, the job finished), otherwise what
        :func:`_watch` decided.
    """
    tasks = [asyncio.create_task(worker) for worker in workers]
    guard = asyncio.create_task(_watch(session))
    done, pending = await asyncio.wait([*tasks, guard], return_when=asyncio.FIRST_COMPLETED)
    for task in pending:
        task.cancel()
    await asyncio.gather(*pending, return_exceptions=True)

    if guard not in done:
        return 1000
    code = guard.result()
    if code == WS_CLOSE_UNAUTHORIZED:
        message = "The credential this stream was opened with is no longer valid"
    else:
        message = "This stream reached its maximum lifetime; reconnect to continue"
    try:
        await websocket.send_json({"type": "error", "message": message})
    except (RuntimeError, WebSocketDisconnect):
        pass  # The client left in the same instant; there is nobody to tell.
    return code


async def _close(websocket: WebSocket, code: int = 1000) -> None:
    """
    Close a socket that may already be closed.

    Args:
        websocket: The connection.
        code: The close code to send.
    """
    try:
        await websocket.close(code=code)
    except (RuntimeError, WebSocketDisconnect):
        pass  # WebSocket already closed


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
            actor=actor_label(session),
            resource=path,
        )


def _resolve_log_units(target: str) -> tuple[list[UnitOwnership], str | None]:
    """
    Work out which units ``/ws/logs/{target}`` streams, and whether it may.

    Args:
        target: An application's domain, or a unit name.

    Returns:
        The units, primary first, and None; or no units and the message that
        refuses the stream.

    Raises:
        ValidationError: When the target cannot name a unit.
    """
    from wasm.core.store import get_store
    from wasm.core.utils import domain_to_app_name
    from wasm.managers.service_manager import ServiceManager

    manager = ServiceManager(verbose=False)
    app = get_store().get_app(target)
    if app is not None:
        names = manager.app_units(app)
        if not names:
            return [], (
                f"{target} is a static site: the web server serves its files directly, "
                "so there is no process and no journal to follow"
            )
    else:
        # A unit name as the Services page passes it, or the domain of an
        # application the store does not know, whose unit is named after it.
        name = target.removesuffix(".service")
        names = [name] if manager.inspect_unit(name).exists else [domain_to_app_name(target)]

    units = [manager.inspect_unit(name) for name in names]
    for unit in units:
        if not unit.exists or not unit.managed:
            reason = unit.reason or f"there is no unit named {unit.unit}"
            return [], f"Refusing to stream a unit WASM does not manage: {unit.unit} ({reason})"
    return units, None


async def _report_journal_exit(
    websocket: WebSocket,
    process: asyncio.subprocess.Process,
    stderr: asyncio.StreamReader,
    already_read: str = "",
) -> None:
    """
    Tell the client that journalctl ended, in journalctl's own words.

    A follow only ends on its own when journalctl failed (a journal it cannot
    read, an option this systemd does not know), and the console used to show
    a bare "The journal stream failed" with the reason thrown away.

    Args:
        websocket: The client connection.
        process: The journalctl that stopped producing output.
        stderr: Its error stream.
        already_read: What was read from that stream before, so the message
            carries all of it.
    """
    try:
        code = await asyncio.wait_for(process.wait(), timeout=TERMINATE_GRACE_SECONDS)
    except asyncio.TimeoutError:
        return  # Still running: the output ended for another reason.
    rest = (await stderr.read()).decode("utf-8", errors="replace")
    output = "\n".join(part for part in (already_read.strip(), rest.strip()) if part)
    if code == 0 and not output:
        return
    message = f"journalctl exited with status {code}"
    if output:
        message = f"{message}: {output}"
    logger.warning("Log stream ended: %s", message)
    try:
        await websocket.send_json({"type": "error", "message": message})
    except (RuntimeError, WebSocketDisconnect):
        pass  # The client left in the same instant; there is nobody to tell.


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

    Only a unit WASM manages is streamed. What the path names is resolved by
    :func:`_resolve_log_units`: an application's domain streams the unit(s)
    that application runs as, from the one mapping in
    :meth:`~wasm.managers.service_manager.ServiceManager.app_units` (so a
    legacy ``wasm-`` unit, a Compose unit and a monorepo's workspaces are all
    found); anything else is taken as a unit name. Every unit is then judged by
    :meth:`~wasm.managers.service_manager.ServiceManager.inspect_unit`, the
    ownership rule ``GET /api/services/{name}/logs`` and every other service
    operation already go through: this route used to follow whatever unit
    the path named - ``/ws/logs/ssh`` was sshd's journal, as root, for any
    valid credential.

    A monorepo streams all of its workspaces' units together, in one
    ``journalctl`` with a ``-u`` per unit: the journal interleaves them by
    time and each line names its process, which is what an operator reading
    "the application's logs" expects. ``service`` in the ``connected`` frame
    is the first of them; ``services`` lists them all.

    Args:
        websocket: The client connection.
        domain: Domain whose service logs are streamed, or the name of a unit
            WASM manages.
        ticket: Optional single-use handshake ticket.
        lines: Backlog of log lines to send first.
    """
    session = await authenticate_websocket(websocket, ticket)
    if session is None:
        await _reject(websocket, f"/ws/logs/{domain}")
        return

    await _accept(websocket, session, f"/ws/logs/{domain}")

    try:
        # The domain is client supplied and ends up as a journalctl unit
        # selector, where '*' and '/' are not inert. inspect_unit validates
        # every name before anything is spawned.
        units, refusal = await run_in_threadpool(_resolve_log_units, domain)
    except ValidationError as exc:
        await websocket.send_json({"type": "error", "message": f"Invalid domain: {exc}"})
        await _close(websocket)
        return

    if refusal is not None:
        await websocket.send_json({"type": "error", "message": refusal})
        await _close(websocket, WS_CLOSE_FORBIDDEN)
        return
    service_name = units[0].unit

    # Add to connections
    if domain not in _log_connections:
        _log_connections[domain] = set()
    _log_connections[domain].add(websocket)

    process = None
    close_code = 1000

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
        selectors = [arg for unit in units for arg in ("-u", unit.unit_file)]
        process = await asyncio.create_subprocess_exec(
            "journalctl",
            *selectors,
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
        await websocket.send_json(
            {
                "type": "connected",
                "domain": domain,
                "service": service_name,
                "services": [unit.unit for unit in units],
            }
        )

        if process.stdout is None or process.stderr is None:
            await websocket.send_json({"type": "error", "message": "journalctl produced no output"})
            return

        stdout = process.stdout
        stderr = process.stderr
        follow = process

        # What journalctl said before its first line, kept so that the error
        # sent if it then exits carries all of it.
        early_stderr: list[str] = []

        # Check for immediate stderr (e.g., service not found)
        async def check_stderr() -> None:
            try:
                stderr_data = await asyncio.wait_for(stderr.read(1024), timeout=0.5)
                if stderr_data:
                    error_msg = stderr_data.decode("utf-8", errors="replace").strip()
                    early_stderr.append(error_msg)
                    if error_msg:
                        await websocket.send_json(
                            {"type": "warning", "data": f"journalctl: {error_msg}"}
                        )
            except asyncio.TimeoutError:
                pass  # No stderr available yet, this is normal
            except (RuntimeError, WebSocketDisconnect):
                pass  # The socket closed before the warning could be sent.

        await check_stderr()

        # Stream logs
        async def read_logs() -> None:
            while True:
                try:
                    line = await stdout.readline()
                    if not line:
                        # Check if process exited
                        if follow.returncode is not None:
                            break
                        if stdout.at_eof():
                            # Nothing more will ever arrive; waiting here
                            # would spin instead of ending the stream.
                            break
                        continue

                    log_line = line.decode("utf-8", errors="replace").strip()
                    if log_line:
                        await websocket.send_json({"type": "log", "data": log_line})
                except (RuntimeError, WebSocketDisconnect):
                    return  # Connection closed or process terminated
            await _report_journal_exit(websocket, follow, stderr, "\n".join(early_stderr))

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
                except (RuntimeError, json.JSONDecodeError):
                    break

        close_code = await _serve(websocket, session, read_logs(), handle_messages())

    except WebSocketDisconnect:
        pass
    except Exception as exc:
        # The genuine error boundary for this handler: spawning journalctl,
        # orchestrating its two reader tasks, or anything neither of them
        # already turned into a clean break. Logged per rule 2 - a bare
        # "notify the client and move on" is how this failure used to leave
        # no trace of what actually broke.
        logger.warning("Log stream for %s failed: %s", domain, exc)
        try:
            await websocket.send_json({"type": "error", "message": str(exc)})
        except (RuntimeError, WebSocketDisconnect):
            pass
    finally:
        await _terminate(process)

        # Remove from connections
        if domain in _log_connections:
            _log_connections[domain].discard(websocket)

        await _close(websocket, close_code)


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
    close_code = 1000

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
                except (RuntimeError, WebSocketDisconnect):
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
                except (RuntimeError, json.JSONDecodeError):
                    break

        close_code = await _serve(websocket, session, read_events(), handle_messages())

    except WebSocketDisconnect:
        pass
    except Exception as exc:
        # journalctl is spawned here with no shutil.which() guard, unlike the
        # logs stream, so a missing binary raises FileNotFoundError (an
        # OSError) straight out of create_subprocess_exec and used to vanish
        # with nothing but a client-side error frame to show for it.
        logger.warning("Event stream failed: %s", exc)
        try:
            await websocket.send_json({"type": "error", "message": str(exc)})
        except (RuntimeError, WebSocketDisconnect):
            pass
    finally:
        await _terminate(process)
        await _close(websocket, close_code)


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
            except RuntimeError as exc:
                # The event loop closed - the connection is already gone -
                # between the check above and this call; there is nobody
                # left to deliver the update to.
                logger.debug("Could not queue a job update for %s: %s", job_id, exc)

    # Subscribe to job updates
    manager.subscribe(job_id, on_job_update)
    close_code = 1000

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
                except (RuntimeError, WebSocketDisconnect):
                    break

        async def handle_messages():
            while True:
                try:
                    data = await websocket.receive_text()
                    msg = json.loads(data)

                    if msg.get("type") == "ping":
                        await websocket.send_json({"type": "pong"})
                    elif msg.get("type") == "cancel":
                        # The same operation as POST /api/jobs/{id}/cancel, so
                        # it needs the same scope; a read token only watches.
                        if not scope_satisfies(str(session.get("scope") or "read"), "admin"):
                            await websocket.send_json(
                                {"type": "error", "message": "Cancelling a job needs admin scope"}
                            )
                        elif manager.cancel_job(job_id):
                            await websocket.send_json({"type": "cancelled", "job_id": job_id})
                except WebSocketDisconnect:
                    break
                except (RuntimeError, json.JSONDecodeError):
                    break

        close_code = await _serve(websocket, session, send_updates(), handle_messages())

    except WebSocketDisconnect:
        pass
    except Exception as exc:
        logger.warning("Job stream for %s failed: %s", job_id, exc)
        try:
            await websocket.send_json({"type": "error", "message": str(exc)})
        except (RuntimeError, WebSocketDisconnect):
            pass
    finally:
        # Unsubscribe and cleanup
        manager.unsubscribe(job_id, on_job_update)
        if job_id in _job_connections:
            _job_connections[job_id].discard(websocket)
        await _close(websocket, close_code)


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
        except RuntimeError as exc:
            # The event loop closed between the check and this call; nobody
            # is left to deliver the update to.
            logger.debug("Could not queue a job update for /ws/jobs: %s", exc)

    # Subscribe to all job updates
    manager.subscribe_all(on_any_job_update)
    close_code = 1000

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
                except (RuntimeError, WebSocketDisconnect):
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
                except (RuntimeError, json.JSONDecodeError):
                    break

        close_code = await _serve(websocket, session, send_updates(), handle_messages())

    except WebSocketDisconnect:
        pass
    except Exception as exc:
        logger.warning("All-jobs stream failed: %s", exc)
        try:
            await websocket.send_json({"type": "error", "message": str(exc)})
        except (RuntimeError, WebSocketDisconnect):
            pass
    finally:
        # Remove global subscriber
        try:
            manager._global_subscribers.remove(on_any_job_update)
        except ValueError:
            pass
        _all_jobs_connections.discard(websocket)
        await _close(websocket, close_code)
