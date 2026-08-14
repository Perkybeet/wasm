# Copyright (c) 2024-2026 Yago Lopez Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
Tests for the panel's live event feed.

Every mutating control in the panel is ``hx-swap="none"``, so a successful
action changes nothing on screen by itself and this stream is what says it
happened. It replaces a client that opened ``EventSource("/events")`` against
a route that existed in no form at all - the only ``/events`` was a WebSocket
on a router mounted at ``/ws`` - which is why live state, the row pulse and
every server-pushed notice had never worked.

What is defended here:

- **The stream demands a session.** It reports which applications exist and
  what is happening to them, on a panel that holds root.
- **It is a real event stream.** The media type and the frame format are the
  contract an EventSource parses; a JSON body on this route is a dead feature
  that looks alive.
- **A job transition reaches both rows it belongs to.** Restarting from the
  applications list has to pulse the application, not only a job row on a
  screen the operator is not looking at.
- **Withdrawing works.** The stream subscribes once per open tab, across
  reconnections, for days.
- **The stream is multiplexed without breaking the jobs on it.** The named
  ``metrics`` and ``machine`` events ride the same connection the job events
  use, in the wire format an EventSource and the htmx SSE extension parse,
  and a failure to render the strip costs a frame, not the feed.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from wasm.core.exceptions import WASMError
from wasm.web import events as events_module
from wasm.web import metrics_collector
from wasm.web.auth import SecurityConfig
from wasm.web.events import (
    HEARTBEAT_SECONDS,
    JOB_STATES,
    _stream,
    events,
    format_event,
    format_html_event,
    job_events,
    machine_frame,
    metrics_frame,
    render_machine_strip,
)
from wasm.web.jobs import JobManager
from wasm.web.server import create_app, get_token_manager


class FakeStatus:
    """Stands in for a JobStatus enum member."""

    def __init__(self, value: str) -> None:
        """
        Args:
            value: The status name.
        """
        self.value = value


class FakeJob:
    """A job transition, with only what the feed reads off it."""

    def __init__(
        self,
        job_id: str = "job-1",
        status: str = "running",
        name: str = "Deploy example.com",
        domain: str | None = "example.com",
        error: str | None = None,
    ) -> None:
        """
        Args:
            job_id: Identifier.
            status: Job status value.
            name: Human-readable job name.
            domain: Resource the job acts on, if any.
            error: The tool's own failure message.
        """
        self.id = job_id
        self.status = FakeStatus(status)
        self.name = name
        self.error = error
        self.metadata: dict[str, Any] = {"domain": domain} if domain else {}


@pytest.fixture
def app(tmp_path: Path, runner: object) -> FastAPI:
    """
    Build the panel with its state in a temporary directory.

    Args:
        tmp_path: Per-test temporary directory.
        runner: The fake command runner, so no manager reaches a real process.

    Returns:
        The application.
    """
    return create_app(SecurityConfig(state_dir=tmp_path / "state", rate_limit_requests=5000))


@pytest.fixture
def anonymous(app: FastAPI) -> TestClient:
    """
    Args:
        app: The application.

    Returns:
        A client with no session.
    """
    return TestClient(app, client=("testclient", 50000), follow_redirects=False)


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    """
    Args:
        app: The application.

    Returns:
        A signed-in client.
    """
    signed_in = TestClient(app, client=("testclient", 50000), follow_redirects=False)
    token = get_token_manager().generate_master_token()
    response = signed_in.post("/api/auth/login", json={"token": token})
    assert response.status_code == 200, response.text
    return signed_in


# ---------------------------------------------------------------------------
# The route
# ---------------------------------------------------------------------------


def test_the_feed_is_refused_without_a_session(anonymous: TestClient) -> None:
    """
    The feed names every application on the machine and what is happening to it.

    Args:
        anonymous: A client with no session.
    """
    response = anonymous.get("/events")

    assert response.status_code == 303
    assert response.headers["location"] == "/login"


class FakeRequest:
    """
    A request that reports the client as still connected.

    The stream is endless by design, so it is exercised through its generator
    rather than through TestClient: a test client cannot close a response whose
    body never ends, and the suite would hang instead of failing.
    """

    def __init__(self, connected: bool = True) -> None:
        """
        Args:
            connected: Whether the client is still there.
        """
        self.connected = connected

    async def is_disconnected(self) -> bool:
        """
        Returns:
            True once the client has gone away.
        """
        return not self.connected


def test_the_feed_is_declared_as_a_server_sent_event_stream() -> None:
    """
    An EventSource parses the media type before anything else.

    A JSON body here would leave the client reconnecting forever against a
    route that answers 200, which is the failure this whole file exists for.
    """
    response = asyncio.run(events(FakeRequest(), {}))

    assert response.media_type == "text/event-stream"
    # Buffering a live feed holds every event until the buffer fills.
    assert response.headers["cache-control"] == "no-cache"
    assert response.headers["x-accel-buffering"] == "no"


def test_the_feed_opens_with_a_comment_and_withdraws_when_it_is_closed() -> None:
    """
    One subscription per tab, across reconnections, for days.

    Without withdrawal every open-and-close leaves a callback holding a queue
    nothing will read again.
    """
    from wasm.web.jobs import get_job_manager

    manager = get_job_manager()
    before = len(manager._global_subscribers)

    async def exercise() -> tuple[str, int]:
        """
        Returns:
            The opening frame, and how many subscribers there were while open.
        """
        stream = _stream(FakeRequest())
        opening = await stream.__anext__()
        during = len(manager._global_subscribers)
        await stream.aclose()
        return opening, during

    opening, during = asyncio.run(exercise())

    assert opening.startswith(":"), "an SSE comment settles the connection"
    assert during == before + 1, "the feed did not subscribe"
    assert len(manager._global_subscribers) == before, "the feed did not withdraw"


def test_a_job_transition_reaches_an_open_stream() -> None:
    """
    The end-to-end path: the job manager notifies, the browser is told.

    Every piece of this is tested on its own above; this is the one that fails
    if they are wired to each other incorrectly. It is also the only test that
    would have failed on the shipped panel, where the route did not exist.
    """
    from wasm.web.jobs import get_job_manager

    async def exercise() -> list[str]:
        """
        Returns:
            The frames the stream produced for one finished job.
        """
        stream = _stream(FakeRequest())
        await stream.__anext__()

        manager = get_job_manager()
        publish = manager._global_subscribers[-1]
        publish(FakeJob(job_id="job-9", status="completed", domain="example.com"))

        frames = [await stream.__anext__() for _ in range(3)]
        await stream.aclose()
        return frames

    frames = asyncio.run(exercise())

    assert 'event: state\ndata: {"id":"job-9","state":"active"}\n\n' in frames
    assert 'event: state\ndata: {"id":"example.com","state":"active"}\n\n' in frames
    assert any(frame.startswith("event: notice") for frame in frames)


# ---------------------------------------------------------------------------
# The wire format
# ---------------------------------------------------------------------------


def test_an_event_carries_its_name_and_a_json_body() -> None:
    """The blank line is what ends an event; without it nothing is delivered."""
    frame = format_event("state", {"id": "example.com", "state": "busy"})

    assert frame == 'event: state\ndata: {"id":"example.com","state":"busy"}\n\n'


def test_a_newline_in_the_payload_cannot_end_the_event_early() -> None:
    """
    A job name is server data and can hold anything.

    A raw newline in a data field terminates the field, so the rest of the
    payload would arrive as a separate, unparseable event.
    """
    frame = format_event("notice", {"text": "line one\nline two", "state": "failed"})

    assert frame.count("\n\n") == 1
    assert frame.endswith("\n\n")
    assert "line one\nline two" not in frame


def test_the_heartbeat_is_short_enough_to_survive_a_proxy() -> None:
    """A silent stream is indistinguishable from a broken one."""
    assert 0 < HEARTBEAT_SECONDS < 60


# ---------------------------------------------------------------------------
# Translating a job transition
# ---------------------------------------------------------------------------


def test_a_running_job_pulses_both_the_job_row_and_the_resource_row() -> None:
    """
    Restarting from the applications list must pulse the application.

    The job also has a row of its own on the activity screen, and the operator
    is usually not looking at it.
    """
    events = job_events(FakeJob(job_id="job-7", status="running", domain="example.com"))

    assert ("state", {"id": "job-7", "state": "busy"}) in events
    assert ("state", {"id": "example.com", "state": "busy"}) in events


def test_a_job_with_no_resource_reports_only_itself() -> None:
    """A row id of None would pulse nothing and cost a frame."""
    events = job_events(FakeJob(job_id="job-7", status="running", domain=None))

    assert events == [("state", {"id": "job-7", "state": "busy"})]


def test_a_finished_job_is_announced() -> None:
    """The action reports that it worked, which is the whole point of the feed."""
    events = job_events(FakeJob(status="completed", name="Deploy example.com"))

    assert ("notice", {"text": "Deploy example.com", "state": "active"}) in events


def test_a_failed_job_is_announced_in_the_tool_s_own_words() -> None:
    """A system error is never paraphrased."""
    error = "nginx: [emerg] duplicate listen options for [::]:443"
    events = job_events(FakeJob(status="failed", error=error))

    assert ("notice", {"text": error, "state": "failed"}) in events


def test_a_transition_between_running_states_is_not_announced() -> None:
    """
    A job going from queued to running is a rail colour, not an interruption.

    Toasting every step of a deploy is how an operator learns to ignore them.
    """
    names = [name for name, _ in job_events(FakeJob(status="running"))]

    assert "notice" not in names


@pytest.mark.parametrize("status", sorted(JOB_STATES))
def test_every_job_status_maps_to_the_shared_state_vocabulary(status: str) -> None:
    """
    Args:
        status: A job status the manager can report.
    """
    assert JOB_STATES[status] in {"active", "failed", "busy", "idle"}


def test_the_status_map_covers_what_the_job_manager_reports() -> None:
    """A status with no mapping renders as idle, which reads as "nothing happened"."""
    from wasm.web.jobs import JobStatus

    assert {status.value for status in JobStatus} <= set(JOB_STATES)


# ---------------------------------------------------------------------------
# Withdrawing a subscription
# ---------------------------------------------------------------------------


@pytest.fixture
def manager() -> Iterator[JobManager]:
    """
    Yields:
        A job manager with no subscribers of its own.
    """
    instance = JobManager()
    previous = list(instance._global_subscribers)
    instance._global_subscribers.clear()
    try:
        yield instance
    finally:
        instance._global_subscribers[:] = previous


def test_a_global_subscriber_can_withdraw(manager: JobManager) -> None:
    """
    Args:
        manager: A job manager with no subscribers of its own.
    """

    def callback(job: Any) -> None:
        """
        Args:
            job: The job that changed.
        """

    manager.subscribe_all(callback)
    assert callback in manager._global_subscribers

    manager.unsubscribe_all(callback)
    assert callback not in manager._global_subscribers


def test_withdrawing_twice_is_not_an_error(manager: JobManager) -> None:
    """
    A stream unsubscribes on the way out without tracking how far it got.

    Args:
        manager: A job manager with no subscribers of its own.
    """

    def callback(job: Any) -> None:
        """
        Args:
            job: The job that changed.
        """

    manager.subscribe_all(callback)
    manager.unsubscribe_all(callback)
    manager.unsubscribe_all(callback)


# ---------------------------------------------------------------------------
# The multiplexed events: metrics and the machine strip
# ---------------------------------------------------------------------------


class FakeCollector:
    """Stands in for the metrics collector, with only what the feed reads."""

    def __init__(self, snapshot: dict[str, float]) -> None:
        """
        Args:
            snapshot: What latest() should hand out.
        """
        self._snapshot = snapshot

    def latest(self) -> dict[str, float]:
        """
        Returns:
            The snapshot.
        """
        return dict(self._snapshot)


def test_markup_crosses_the_wire_as_one_event_with_a_data_line_per_line() -> None:
    """
    A raw newline ends an SSE data field early, and rendered templates are
    full of them. The format's own answer is one ``data:`` field per line; the
    browser joins consecutive fields with a newline, reproducing the markup.
    """
    frame = format_html_event("machine", "<header>\n  ok\n</header>")

    assert frame == "event: machine\ndata: <header>\ndata:   ok\ndata: </header>\n\n"
    assert frame.count("\n\n") == 1, "exactly one blank line, the one that ends the event"


def test_the_stream_opens_with_the_collector_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    The first ``metrics`` event goes out immediately, so a freshly opened page
    has numbers before the collector's next tick.
    """
    monkeypatch.setattr(metrics_collector, "_collector", FakeCollector({"cpu.percent": 12.5}))

    async def exercise() -> str:
        """
        Returns:
            The first frame after the opening comment.
        """
        stream = _stream(FakeRequest())
        await stream.__anext__()
        frame = await stream.__anext__()
        await stream.aclose()
        return frame

    frame = asyncio.run(exercise())

    assert frame == 'event: metrics\ndata: {"cpu.percent":12.5}\n\n'


def test_without_a_collector_no_metrics_event_is_invented() -> None:
    """A metrics event with made-up data would look like the feature works."""
    assert metrics_frame() is None


def test_the_stream_emits_the_machine_strip_as_a_named_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The strip rides the shared connection for the sse-swap to consume."""
    monkeypatch.setattr(events_module, "MACHINE_INTERVAL_SECONDS", 0.0)
    monkeypatch.setattr(
        events_module, "render_machine_strip", lambda: '<header id="machine-strip">ok</header>'
    )

    async def exercise() -> str:
        """
        Returns:
            The first frame after the opening comment.
        """
        stream = _stream(FakeRequest())
        await stream.__anext__()
        frame = await stream.__anext__()
        await stream.aclose()
        return frame

    frame = asyncio.run(exercise())

    assert frame == 'event: machine\ndata: <header id="machine-strip">ok</header>\n\n'


def test_job_events_still_flow_between_the_periodic_ones(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Multiplexing must not cost the stream its original job: the state events
    the rows pulse on arrive alongside the metrics.
    """
    from wasm.web.jobs import get_job_manager

    monkeypatch.setattr(metrics_collector, "_collector", FakeCollector({"cpu.percent": 1.0}))

    async def exercise() -> list[str]:
        """
        Returns:
            The frames the stream produced around one finished job.
        """
        stream = _stream(FakeRequest())
        await stream.__anext__()

        manager = get_job_manager()
        publish = manager._global_subscribers[-1]
        publish(FakeJob(job_id="job-9", status="completed", domain="example.com"))

        frames = [await stream.__anext__() for _ in range(4)]
        await stream.aclose()
        return frames

    frames = asyncio.run(exercise())

    assert any(frame.startswith("event: metrics\n") for frame in frames)
    assert 'event: state\ndata: {"id":"job-9","state":"active"}\n\n' in frames
    assert 'event: state\ndata: {"id":"example.com","state":"active"}\n\n' in frames


def test_a_strip_that_cannot_render_costs_a_frame_not_the_feed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    The render reads systemd and psutil; a transient failure there must not
    take down the connection carrying the job events.
    """
    from wasm.web.jobs import get_job_manager

    def refuse() -> str:
        raise WASMError("systemd is restarting")

    monkeypatch.setattr(events_module, "MACHINE_INTERVAL_SECONDS", 0.01)
    monkeypatch.setattr(events_module, "render_machine_strip", refuse)

    assert machine_frame() is None, "a failed render must yield nothing, not raise"

    async def exercise() -> list[str]:
        """
        Returns:
            The frames the stream produced for one finished job.
        """
        stream = _stream(FakeRequest())
        await stream.__anext__()

        manager = get_job_manager()
        publish = manager._global_subscribers[-1]
        publish(FakeJob(job_id="job-3", status="completed", domain="example.com"))

        frames = [await stream.__anext__() for _ in range(3)]
        await stream.aclose()
        return frames

    frames = asyncio.run(exercise())

    assert 'event: state\ndata: {"id":"job-3","state":"active"}\n\n' in frames
    assert not any(frame.startswith("event: machine") for frame in frames)


def test_the_pushed_strip_is_the_same_fragment_the_shell_includes(runner: Any) -> None:
    """
    One implementation: the ``machine`` event carries the very fragment the
    shell renders and swaps, sse-swap attribute included, so the pushed strip
    and the served one can never disagree.

    Args:
        runner: The fake command runner, so the unit tally reaches no process.
    """
    html = render_machine_strip()

    assert 'id="machine-strip"' in html
    assert 'sse-swap="machine"' in html
    assert 'hx-get="/fragments/machine"' not in html, "the strip must not also poll"
