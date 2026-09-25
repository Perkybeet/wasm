# Copyright (c) 2024-2026 Yago Lopez Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
The live feed the console listens to.

Every mutation in the console answers with JSON and changes nothing on screen
by itself; this stream is what tells every open tab that something changed,
so a deploy started from the CLI, a webhook or another browser shows up
without anyone reloading.

**Why server-sent events and not a WebSocket.** The traffic is
one-directional: the server tells the browser that a state changed. SSE is a
plain GET, so it goes through the same middleware as every other request -
the session cookie, the IP whitelist, the HTTPS requirement - instead of the
separate handshake-ticket path a WebSocket needs. It also reconnects by
itself, which matters for a panel people leave open.

The stream is multiplexed - one connection per tab, several named events -
because a browser caps concurrent connections per origin low enough that a
stream per concern would starve the console of request slots. Every event is
JSON:

- ``job``: a job's snapshot, the shape ``GET /api/jobs/{id}`` answers, on
  every transition and every log line (with only the newest line in ``logs``).
- ``state`` and ``notice``: the transition vocabulary and the toast for a
  finished job.
- ``app``: an application's state changed - a deploy, update, rollback or
  delete job moved, or a start, stop or restart succeeded. It carries the
  domain and the fields ``GET /api/apps/{domain}`` answers, read once per
  change and fanned out to every open stream through :data:`hub`.
- ``metrics`` and ``machine``: the collector's newest snapshot and the
  machine snapshot from :mod:`wasm.web.machine`, on a timer.

What is published is what the panel can actually observe. Nothing is
invented - a stream that emits events nothing produces would be worse than no
stream, because it would look like the feature works.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from collections import OrderedDict
from collections.abc import AsyncIterator, Callable
from dataclasses import asdict
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from starlette.concurrency import run_in_threadpool

from wasm.core.exceptions import WASMError
from wasm.web import metrics_collector
from wasm.web.auth import SAFE_METHODS, require_auth
from wasm.web.jobs import Job
from wasm.web.machine import read_machine
from wasm.web.pydantic_compat import dump_model

log = logging.getLogger(__name__)

router = APIRouter(include_in_schema=False)

#: Seconds between keepalive comments. Proxies and load balancers close an idle
#: response, and a silent stream is indistinguishable from a broken one.
HEARTBEAT_SECONDS = 25

#: Seconds between ``metrics`` events, matching the collector's own tick.
METRICS_INTERVAL_SECONDS = 2.0

#: Seconds between ``machine`` events, matching the cadence the strip used to
#: poll at.
MACHINE_INTERVAL_SECONDS = 5.0

#: Most events held for a slow client before the oldest are dropped. A browser
#: that cannot keep up gets a gap, which it recovers from on the next change;
#: an unbounded queue would be a memory leak driven by a remote peer.
QUEUE_SIZE = 256

#: Job status to the four-word state vocabulary the rails, badges and notices
#: all share.
JOB_STATES = {
    "pending": "busy",
    "running": "busy",
    "completed": "active",
    "failed": "failed",
    "cancelled": "idle",
}

#: The statuses worth a notice. A job passing from pending to running is a rail
#: colour, not an interruption.
NOTICEABLE = {"completed", "failed", "cancelled"}


def format_event(name: str, payload: dict[str, Any]) -> str:
    """
    Render one server-sent event.

    Args:
        name: Event name the client listens for.
        payload: JSON body. A value JSON has no type for - a ``Path`` or a
            ``datetime`` in a job's result - is sent as its string form
            rather than costing the whole frame.

    Returns:
        The wire format, terminated by the blank line that ends an event.
    """
    # separators without spaces, and no newline can survive json.dumps, which
    # matters because a newline in the data field would end the event early.
    body = json.dumps(payload, separators=(",", ":"), default=str)
    return f"event: {name}\ndata: {body}\n\n"


#: What reading the snapshot can fail with in operation: the managers' own
#: errors and the OS reads underneath psutil. Named rather than Exception so a
#: bug in the snapshot stays loud instead of becoming a strip that silently
#: stops updating.
RENDER_ERRORS: tuple[type[Exception], ...] = (WASMError, OSError)


def machine_frame() -> str | None:
    """
    Build the periodic ``machine`` event, or nothing.

    This is an error boundary: the read samples systemd and psutil, and a
    transient failure there must cost one frame, not the whole stream with the
    job events on it.

    Returns:
        The wire frame, or None when the snapshot could not be read.
    """
    try:
        state = read_machine()
    except RENDER_ERRORS:
        log.exception("the machine snapshot could not be read for the stream")
        return None
    return format_event("machine", asdict(state))


def metrics_frame() -> str | None:
    """
    Build the periodic ``metrics`` event, or nothing.

    Returns:
        The wire frame with the collector's newest snapshot, or None when no
        collector is running in this process.
    """
    collector = metrics_collector.get_metrics_collector()
    if collector is None:
        return None
    return format_event("metrics", collector.latest())


def job_payload(job: Job) -> dict[str, Any]:
    """
    Render a job as the ``job`` event carries it.

    The shape is what ``GET /api/jobs/{id}`` answers, so the console writes
    the event straight into the same query cache entry, plus four flat fields
    for the activity rail. ``logs`` holds only the newest line: the job
    manager notifies once per log line, and repeating the whole tail on every
    frame would make a deploy's traffic grow with the square of its
    length. The full log is ``GET /api/jobs/{id}/log`` and ``/ws/jobs/{id}``.

    Args:
        job: The job that changed.

    Returns:
        The event body.
    """
    body = job.to_dict()
    latest = job.logs[-1] if job.logs else None
    body["logs"] = [latest.to_dict()] if latest else []
    body["domain"] = job.metadata.get("domain")
    body["message"] = latest.message if latest else ""
    body["level"] = latest.level if latest else "info"
    body["finished"] = job.status.value in NOTICEABLE
    return body


def job_events(job: Job) -> list[tuple[str, dict[str, Any]]]:
    """
    Translate a job transition into what the console shows.

    A job is rendered as its own row on the activity screen, and it usually
    also acts on a resource that has a row of its own somewhere else. Both are
    told, so restarting an application from the applications list pulses the
    application's rail, not only a job row on another page.

    Args:
        job: The job that changed.

    Returns:
        Pairs of event name and payload.

    Raises:
        AttributeError: Never caught here; a job without a status is a bug in
            the job manager and must not be turned into a silent gap.
    """
    status = job.status.value
    state = JOB_STATES.get(status, "idle")
    domain = job.metadata.get("domain")

    events: list[tuple[str, dict[str, Any]]] = [
        ("job", job_payload(job)),
        ("state", {"id": job.id, "state": state}),
    ]

    if domain:
        events.append(("state", {"id": str(domain), "state": state}))

    if status in NOTICEABLE:
        # The tool's own words when there are any. This is the one place a
        # failure is summarised rather than shown verbatim, and it is a toast
        # pointing at the activity screen, not a replacement for the output.
        text = job.error if status == "failed" and job.error else job.name
        events.append(("notice", {"text": text, "state": state}))

    return events


# ---------------------------------------------------------------------------
# Application state: computed once per change, fanned out to every stream
# ---------------------------------------------------------------------------


class EventHub:
    """
    Fans one event out to every open stream.

    Job events are cheap to render, so each stream subscribes to the job
    manager and renders its own. An application's state is not: it is read
    from the store and from systemd. Reading it once per open tab would
    multiply the ``systemctl`` calls by the number of tabs, so it is read
    once, here, and every stream receives the same frame.
    """

    def __init__(self) -> None:
        """Start with no stream listening."""
        self._listeners: list[Callable[[str], None]] = []
        self._lock = threading.Lock()

    @property
    def listening(self) -> bool:
        """
        Returns:
            Whether at least one stream is open, so a producer can skip the
            work of reading a state nobody would receive.
        """
        with self._lock:
            return bool(self._listeners)

    def attach(self, listener: Callable[[str], None]) -> None:
        """
        Start delivering frames to a stream.

        Args:
            listener: Receives each rendered frame. Called from whatever
                thread published, so it must be thread-safe.
        """
        with self._lock:
            self._listeners.append(listener)

    def detach(self, listener: Callable[[str], None]) -> None:
        """
        Stop delivering frames to a stream.

        Args:
            listener: A listener given to :meth:`attach`. Detaching one that
                is not attached is not an error.
        """
        with self._lock:
            if listener in self._listeners:
                self._listeners.remove(listener)

    def publish(self, name: str, payload: dict[str, Any]) -> None:
        """
        Render one event and hand it to every open stream.

        Args:
            name: Event name.
            payload: JSON body.
        """
        frame = format_event(name, payload)
        with self._lock:
            listeners = list(self._listeners)
        for listener in listeners:
            listener(frame)


#: The process-wide hub every stream attaches to.
hub = EventHub()

#: Job types whose progress and outcome change what an application looks like.
#: ``restore`` is the rollback job; certificate jobs carry a domain too but
#: change a certificate, not the application's state.
APP_JOB_TYPES = frozenset({"deploy", "update", "restore", "delete", "service_action"})

#: Job types whose run is shown on the application as in progress.
_IN_PROGRESS_TYPES = frozenset({"deploy", "update", "restore"})

#: Path segments directly under ``/api/apps/`` that are not a domain.
_NON_DOMAIN_SEGMENTS = frozenset({"inspect"})

#: Service actions that are also exposed under ``/api/apps/{domain}/...`` and
#: change what the applications page shows. Enabling, disabling, rewriting a
#: unit's config or deleting it are unit administration, not the running
#: state a deploy, an update or this trio also change.
_SERVICE_APP_ACTIONS = frozenset({"start", "stop", "restart"})

#: Most job ids whose last seen status is remembered by the job publisher.
_SEEN_JOBS_LIMIT = 512


def app_snapshot(domain: str) -> dict[str, Any] | None:
    """
    Read an application the way ``GET /api/apps/{domain}`` describes it.

    This calls the endpoint's own implementation rather than a copy of it, so
    the event and the endpoint cannot drift apart: the console writes the
    event into the same cache entry the endpoint fills.

    Args:
        domain: Domain of the application.

    Returns:
        The application's fields, or None when there is no such application
        (it was just deleted, or the domain is not one).
    """
    from wasm.web.api.apps import get_app

    try:
        return dump_model(get_app(domain, {}))
    except HTTPException as exc:
        if exc.status_code in (400, 404):
            return None
        raise


def publish_app_state(domain: str) -> None:
    """
    Publish an ``app`` event with an application's current state.

    This is an error boundary: it runs off the request path, and a systemd
    that cannot be queried must cost the event, not the thread that called.

    Args:
        domain: Domain of the application that changed.
    """
    if not hub.listening:
        return
    try:
        snapshot = app_snapshot(domain)
    except RENDER_ERRORS:
        log.exception("the state of %s could not be read for the stream", domain)
        return
    if snapshot is None:
        # Gone. The console drops it from the list on this event and leaves a
        # page that still shows it to say so.
        hub.publish("app", {"domain": domain, "deleted": True})
        return
    hub.publish("app", {**snapshot, "domain": domain})


def _in_thread(domain: str) -> None:
    """
    Publish an application's state from a daemon thread.

    Reading it runs ``systemctl``; the callers are the job worker, which must
    not stall the next job behind it, and a response being sent.

    Args:
        domain: Domain of the application that changed.
    """
    threading.Thread(
        target=publish_app_state, args=(domain,), name="wasm-app-event", daemon=True
    ).start()


class AppStatePublisher:
    """
    Turns application jobs into ``app`` events.

    Registered with the job manager's ``subscribe_all`` for the life of the
    server, next to the notification subscriber in
    :func:`wasm.web.server.lifespan`. The job manager notifies on every log
    line; only a change of status changes the application, so everything
    else is ignored.
    """

    def __init__(
        self,
        publish: Callable[[str, dict[str, Any]], None] | None = None,
        refresh: Callable[[str], None] | None = None,
    ) -> None:
        """
        Args:
            publish: Replacement for :meth:`EventHub.publish` on :data:`hub`.
            refresh: Replacement for :func:`_in_thread`. Tests inject
                captures so nothing spawns a thread or queries systemd.
        """
        self._publish = publish or hub.publish
        self._refresh = refresh or _in_thread
        self._seen: OrderedDict[str, str] = OrderedDict()
        self._lock = threading.Lock()

    def __call__(self, job: Job) -> None:
        """
        Receive one job transition from the job manager.

        Args:
            job: The job that changed.
        """
        job_type = job.type.value
        domain = job.metadata.get("domain")
        if job_type not in APP_JOB_TYPES or not domain:
            return

        status = job.status.value
        with self._lock:
            if self._seen.get(job.id) == status:
                return
            self._seen[job.id] = status
            while len(self._seen) > _SEEN_JOBS_LIMIT:
                self._seen.popitem(last=False)

        if status in NOTICEABLE:
            # Whatever the outcome, the truth is what the store and systemd
            # say now: a failed deploy may have rolled back to a running
            # release.
            self._refresh(str(domain))
        elif job_type in _IN_PROGRESS_TYPES:
            self._publish("app", {"domain": str(domain), "status": "deploying"})


def app_domain_of(method: str, path: str) -> str | None:
    """
    Name the application a successful API mutation acted on, if any.

    Starting, stopping and restarting an application are synchronous calls,
    not jobs, so no job event reports them. The security middleware calls
    this for every mutation it has already audited - the one place every API
    call passes through - so an endpoint under ``/api/apps/{domain}`` cannot
    forget to announce itself.

    Args:
        method: The request method.
        path: The request path.

    Returns:
        The domain segment of ``/api/apps/{domain}[/...]`` for an unsafe
        method, or None. Existence is not checked here:
        :func:`publish_app_state` reads the store and skips what is not an
        application.
    """
    if method.upper() in SAFE_METHODS:
        return None
    parts = path.split("/")
    # ["", "api", "apps", "<domain>", ...]
    if len(parts) < 4 or parts[1] != "api" or parts[2] != "apps":
        return None
    segment = parts[3]
    if not segment or segment in _NON_DOMAIN_SEGMENTS:
        return None
    return segment


def _domain_owning_unit(unit_name: str) -> str | None:
    """
    Look up the application a systemd unit belongs to.

    This is an error boundary: it runs inside the response path of every
    service mutation while a console is open, and a store that cannot be
    read must cost this one lookup, not the request.

    Args:
        unit_name: The unit's name, without the ``.service`` suffix.

    Returns:
        Its application's domain, or None when the unit is not registered,
        or belongs to no application - a unit ``wasm service create`` made
        by hand, never tied to a deployment.
    """
    from wasm.core.store import get_store

    try:
        store = get_store()
        service = store.get_service(unit_name)
        if service is None or service.app_id is None:
            return None
        app = store.get_app_by_id(service.app_id)
    except WASMError:
        log.exception("could not resolve which application owns unit %s", unit_name)
        return None
    return app.domain if app is not None else None


def service_domain_of(method: str, path: str) -> str | None:
    """
    Name the application a successful ``/api/services/`` mutation acted on.

    Starting, stopping and restarting a unit from the services page issues
    exactly the systemctl call ``POST /api/apps/{domain}/start`` and its
    siblings do; the only difference is which page the operator used. Without
    this, a restart issued from the services page left every other open
    console showing the application as whatever it was before, until
    something else refetched it.

    Args:
        method: The request method.
        path: The request path.

    Returns:
        The domain of the application the unit belongs to, for an unsafe
        method against one of :data:`_SERVICE_APP_ACTIONS`; None otherwise,
        or when the unit belongs to no application.
    """
    if method.upper() in SAFE_METHODS:
        return None
    parts = path.split("/")
    # ["", "api", "services", "<name>", "<action>"]
    if len(parts) != 5 or parts[1] != "api" or parts[2] != "services":
        return None
    if not parts[3] or parts[4] not in _SERVICE_APP_ACTIONS:
        return None
    return _domain_owning_unit(parts[3])


def announce_app_mutation(method: str, path: str, status_code: int) -> None:
    """
    Publish the state of an application a successful API call changed.

    A 202 is skipped: it queued a job, and :class:`AppStatePublisher` reports
    the job's progress and outcome. Covers both ``/api/apps/{domain}/...``
    and, through :func:`service_domain_of`, the same start, stop and restart
    actions issued against the unit directly under ``/api/services/{name}``.

    Args:
        method: The request method.
        path: The request path.
        status_code: The status the endpoint answered with.
    """
    if not 200 <= status_code < 300 or status_code == 202 or not hub.listening:
        return
    domain = app_domain_of(method, path) or service_domain_of(method, path)
    if domain is not None:
        _in_thread(domain)


async def _stream(request: Request) -> AsyncIterator[str]:
    """
    Yield events until the client goes away.

    Args:
        request: The incoming request, watched for disconnection.

    Yields:
        Server-sent event frames.
    """
    from wasm.web.jobs import get_job_manager

    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[str] = asyncio.Queue(maxsize=QUEUE_SIZE)

    def deliver(frames: list[str]) -> None:
        """
        Queue frames for this stream from any thread.

        The job manager and the hub publish from threads of their own, so
        this is called from outside the event loop and must hop back onto it.

        Args:
            frames: Rendered frames, in order.
        """

        def enqueue() -> None:
            for frame in frames:
                if queue.full():
                    # Drop the oldest rather than the newest: the newest frame
                    # is the current state of the machine.
                    queue.get_nowait()
                queue.put_nowait(frame)

        loop.call_soon_threadsafe(enqueue)

    def publish(job: Job) -> None:
        """
        Hand a job transition to the stream.

        Args:
            job: The job that changed.
        """
        try:
            frames = [format_event(name, payload) for name, payload in job_events(job)]
        except AttributeError:
            log.exception("a job transition could not be rendered as an event")
            return
        deliver(frames)

    def relay(frame: str) -> None:
        """
        Hand a frame the hub rendered once for every stream to this one.

        Args:
            frame: The rendered frame.
        """
        deliver([frame])

    manager = get_job_manager()
    manager.subscribe_all(publish)
    hub.attach(relay)

    try:
        # An immediate frame settles the connection before any proxy decides
        # the response has stalled, and tells the client the feed is live.
        yield ": connected\n\n"

        now = loop.time()
        # The first metrics event goes out at once, so a freshly opened page
        # has numbers before the collector's next tick. The strip waits a full
        # interval: the page just rendered its own copy.
        metrics_at = now
        machine_at = now + MACHINE_INTERVAL_SECONDS
        heartbeat_at = now + HEARTBEAT_SECONDS

        while True:
            if await request.is_disconnected():
                return

            now = loop.time()
            if now >= metrics_at:
                frame = metrics_frame()
                if frame is not None:
                    yield frame
                    heartbeat_at = now + HEARTBEAT_SECONDS
                metrics_at = now + METRICS_INTERVAL_SECONDS
            if now >= machine_at:
                # In a worker thread: the render tallies systemd units, and a
                # blocking call here stalls every stream on the event loop.
                frame = await run_in_threadpool(machine_frame)
                if frame is not None:
                    yield frame
                    heartbeat_at = loop.time() + HEARTBEAT_SECONDS
                machine_at = now + MACHINE_INTERVAL_SECONDS

            timeout = min(metrics_at, machine_at, heartbeat_at) - loop.time()
            try:
                yield await asyncio.wait_for(queue.get(), timeout=max(0.0, timeout))
                heartbeat_at = loop.time() + HEARTBEAT_SECONDS
            except asyncio.TimeoutError:
                if loop.time() >= heartbeat_at:
                    yield ": keepalive\n\n"
                    heartbeat_at = loop.time() + HEARTBEAT_SECONDS
    finally:
        manager.unsubscribe_all(publish)
        hub.detach(relay)


@router.get("/events")
async def events(
    request: Request, _: Annotated[dict[str, Any], Depends(require_auth)]
) -> StreamingResponse:
    """
    Stream state changes to the console.

    Args:
        request: The incoming request.
        _: The session, required exactly as the API requires it: an
            EventSource sends the session cookie, and a missing or expired
            one is a 401, which the console cannot read from an EventSource:
            it notices on its next session check after the drop and answers
            with the sign-in page.

    Returns:
        An event stream.
    """
    return StreamingResponse(
        _stream(request),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            # nginx buffers a proxied response by default, which holds every
            # event until the buffer fills. A live feed that arrives in
            # ten-minute batches is not a live feed.
            "X-Accel-Buffering": "no",
        },
    )
