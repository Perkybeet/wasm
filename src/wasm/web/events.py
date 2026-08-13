# Copyright (c) 2024-2026 Yago Lopez Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
The live feed the panel listens to.

Every mutating control in the panel is ``hx-swap="none"``: the API answers in
JSON, and swapping a payload into the page is how a delete once left a blob of
braces where a row had been. The consequence is that a successful action
changes nothing on screen by itself, and something else has to say what
happened. This is that something.

**Why server-sent events and not a WebSocket.** The design direction asks for
the machine strip to update over SSE, and the traffic is one-directional: the
server tells the browser that a state changed. SSE is a plain GET, so it goes
through the same middleware as every other request - the session cookie, the
IP whitelist, the HTTPS requirement - instead of the separate handshake-ticket
path a WebSocket needs. It also reconnects by itself, which matters for a
panel people leave open.

The shell used to open ``new EventSource("/events")`` against a route that did
not exist in any form: the only ``/events`` in the tree was a WebSocket on a
router mounted at ``/ws``. The stream 404'd, the client reported a dropped
connection, and live state, the row pulse and every server-pushed notice had
never worked at all.

What is published is what the panel can actually observe: the job manager
notifies its subscribers on every job transition, so a deploy, a restart, a
renewal and a restore all pass through here. Nothing is invented - a stream
that emits events nothing produces would be worse than no stream, because it
would look like the feature works.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse

from wasm.web.views.router import require_page_session

log = logging.getLogger(__name__)

router = APIRouter(include_in_schema=False)

#: Seconds between keepalive comments. Proxies and load balancers close an idle
#: response, and a silent stream is indistinguishable from a broken one.
HEARTBEAT_SECONDS = 25

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
        payload: JSON-serialisable body.

    Returns:
        The wire format, terminated by the blank line that ends an event.
    """
    # separators without spaces, and no newline can survive json.dumps, which
    # matters because a newline in the data field would end the event early.
    return f"event: {name}\ndata: {json.dumps(payload, separators=(',', ':'))}\n\n"


def job_events(job: Any) -> list[tuple[str, dict[str, Any]]]:
    """
    Translate a job transition into what the panel shows.

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

    events: list[tuple[str, dict[str, Any]]] = [("state", {"id": job.id, "state": state})]

    domain = job.metadata.get("domain")
    if domain:
        events.append(("state", {"id": str(domain), "state": state}))

    if status in NOTICEABLE:
        # The tool's own words when there are any. This is the one place a
        # failure is summarised rather than shown verbatim, and it is a toast
        # pointing at the activity screen, not a replacement for the output.
        text = job.error if status == "failed" and job.error else job.name
        events.append(("notice", {"text": text, "state": state}))

    return events


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

    def publish(job: Any) -> None:
        """
        Hand a job transition to the stream.

        The job manager runs its worker on a thread of its own, so this is
        called from outside the event loop and must hop back onto it.

        Args:
            job: The job that changed.
        """
        try:
            frames = [format_event(name, payload) for name, payload in job_events(job)]
        except AttributeError:
            log.exception("a job transition could not be rendered as an event")
            return

        def enqueue() -> None:
            for frame in frames:
                if queue.full():
                    # Drop the oldest rather than the newest: the newest frame
                    # is the current state of the machine.
                    queue.get_nowait()
                queue.put_nowait(frame)

        loop.call_soon_threadsafe(enqueue)

    manager = get_job_manager()
    manager.subscribe_all(publish)

    try:
        # An immediate frame settles the connection before any proxy decides
        # the response has stalled, and tells the client the feed is live.
        yield ": connected\n\n"

        while True:
            if await request.is_disconnected():
                return
            try:
                yield await asyncio.wait_for(queue.get(), timeout=HEARTBEAT_SECONDS)
            except asyncio.TimeoutError:
                yield ": keepalive\n\n"
    finally:
        manager.unsubscribe_all(publish)


@router.get("/events")
async def events(
    request: Request, _: Annotated[dict[str, Any], Depends(require_page_session)]
) -> StreamingResponse:
    """
    Stream state changes to the panel.

    Args:
        request: The incoming request.
        _: The session, required the same way every page requires one.

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
