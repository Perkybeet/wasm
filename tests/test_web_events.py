# Copyright (c) 2024-2026 Yago Lopez Prado
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Tests for the console's live event feed.

Every mutation in the console answers with JSON and changes nothing on screen
by itself, so this stream is what tells every open tab that something
happened. It replaces a client that once opened ``EventSource("/events")``
against a route that existed in no form at all, which is why live state had
never worked before it.

What is defended here:

- **The stream demands a session.** It reports which applications exist and
  what is happening to them, on a panel that holds root. It accepts the
  console's cookie session exactly as the API does and answers 401 without.
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
  use, in the wire format an EventSource parses, and a failure to read the
  machine snapshot costs a frame, not the feed.
- **Every event on the stream is JSON**, ``machine`` included: there is one
  implementation of the machine snapshot, :mod:`wasm.web.machine`, and both
  the REST endpoint and this stream hand out exactly what it returns.
- **The ``job`` event is the job endpoint's shape** and the ``app`` event is
  the app endpoint's, so the console writes both straight into its cache.
- **An application's state change reaches every tab**, whether a job moved
  it or a synchronous start, stop or restart did, read once per change.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from datetime import datetime
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
    job_events,
    machine_frame,
    metrics_frame,
)
from wasm.web.jobs import Job, JobLogEntry, JobManager, JobStatus, JobType
from wasm.web.machine import AppTally, DiskSnapshot, MachineState, MemorySnapshot, UnitTally
from wasm.web.server import create_app, get_token_manager


def log_line(message: str, level: str = "info") -> JobLogEntry:
    """
    Args:
        message: The log message.
        level: Severity the line was reported at.

    Returns:
        A job log entry.
    """
    return JobLogEntry(timestamp=datetime(2026, 9, 25, 12, 0, 0), level=level, message=message)


def make_job(
    job_id: str = "job-1",
    status: str = "running",
    name: str = "Deploy example.com",
    domain: str | None = "example.com",
    error: str | None = None,
    job_type: str = "deploy",
    progress: int = 0,
    logs: list[JobLogEntry] | None = None,
) -> Job:
    """
    Build a real job, detached from any job manager.

    Args:
        job_id: Identifier.
        status: Job status value.
        name: Human-readable job name.
        domain: Resource the job acts on, if any.
        error: The tool's own failure message.
        job_type: Job type value.
        progress: Progress value.
        logs: Log entries recorded so far, newest last.

    Returns:
        The job.
    """
    return Job(
        id=job_id,
        type=JobType(job_type),
        name=name,
        description=f"{name} (test)",
        status=JobStatus(status),
        progress=progress,
        error=error,
        logs=list(logs or []),
        metadata={"domain": domain} if domain else {},
    )


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

    # The API's answer, not a redirect: the console's EventSource cannot
    # follow one to anything useful, and there is no sign-in page on the
    # server to send it to any more.
    assert response.status_code == 401
    assert "location" not in response.headers


def test_the_feed_accepts_the_console_s_cookie_session(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    The console authenticates the stream with its session cookie, like the API.

    The stream is endless, so the route's generator is swapped for one that
    ends: what is tested is that the request got past authentication.

    Args:
        client: A signed-in client.
        monkeypatch: Replaces the endless stream.
    """

    async def one_frame(request: object, session: object = None) -> Any:
        yield ": connected\n\n"

    monkeypatch.setattr(events_module, "_stream", one_frame)

    response = client.get("/events")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.text == ": connected\n\n"


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
        publish(make_job(job_id="job-9", status="completed", domain="example.com"))

        frames = [await stream.__anext__() for _ in range(4)]
        await stream.aclose()
        return frames

    frames = asyncio.run(exercise())

    assert any(frame.startswith("event: job") for frame in frames)
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
    events = job_events(make_job(job_id="job-7", status="running", domain="example.com"))

    assert ("state", {"id": "job-7", "state": "busy"}) in events
    assert ("state", {"id": "example.com", "state": "busy"}) in events


def test_a_job_with_no_resource_reports_only_itself_and_the_job_event() -> None:
    """A row id of None would pulse nothing and cost a frame."""
    events = job_events(make_job(job_id="job-7", status="running", domain=None))

    names = [name for name, _ in events]
    assert names == ["job", "state"]
    assert ("state", {"id": "job-7", "state": "busy"}) in events


def test_a_finished_job_is_announced() -> None:
    """The action reports that it worked, which is the whole point of the feed."""
    events = job_events(make_job(status="completed", name="Deploy example.com"))

    assert ("notice", {"text": "Deploy example.com", "state": "active"}) in events


def test_a_failed_job_is_announced_in_the_tool_s_own_words() -> None:
    """A system error is never paraphrased."""
    error = "nginx: [emerg] duplicate listen options for [::]:443"
    events = job_events(make_job(status="failed", error=error))

    assert ("notice", {"text": error, "state": "failed"}) in events


def test_a_transition_between_running_states_is_not_announced() -> None:
    """
    A job going from queued to running is a rail colour, not an interruption.

    Toasting every step of a deploy is how an operator learns to ignore them.
    """
    names = [name for name, _ in job_events(make_job(status="running"))]

    assert "notice" not in names


# ---------------------------------------------------------------------------
# The `job` event: progress and log lines, for the console's activity feed
# ---------------------------------------------------------------------------


def test_the_job_event_carries_the_fields_the_console_needs() -> None:
    """One event, one shape, whether it is a progress step or a log line."""
    job = make_job(
        job_id="job-7",
        status="running",
        domain="example.com",
        job_type="deploy",
        progress=40,
        logs=[log_line("Installing dependencies", "info")],
    )

    name, payload = job_events(job)[0]

    assert name == "job"
    assert {
        "id": "job-7",
        "type": "deploy",
        "status": "running",
        "progress": 40,
        "domain": "example.com",
        "message": "Installing dependencies",
        "level": "info",
        "finished": False,
    }.items() <= payload.items()


def test_the_job_event_is_the_shape_the_job_endpoint_answers() -> None:
    """
    The console writes the event into the cache entry ``GET /api/jobs/{id}`` fills.

    A narrower payload would replace a full job with a fragment, and every
    field the event left out would read as missing on the job's page.
    """
    from wasm.web.api.jobs import JobResponse

    job = make_job(logs=[log_line("first"), log_line("second")])

    _, payload = job_events(job)[0]

    # Extra keys are ignored by both pydantic majors; missing ones fail.
    model = JobResponse(**payload)
    assert model.id == job.id
    assert model.name == job.name
    assert payload["metadata"] == {"domain": "example.com"}
    # Only the newest line: the manager notifies once per line.
    assert [entry["message"] for entry in payload["logs"]] == ["second"]


def test_a_result_json_cannot_encode_is_sent_as_text() -> None:
    """A job result holding a path must not cost the frame that reports it."""
    job = make_job(status="completed")
    job.result = {"path": Path("/var/www/apps/example.com")}

    frame = format_event(*job_events(job)[0])

    assert '"path":"/var/www/apps/example.com"' in frame


def test_the_job_event_reports_finished_for_a_terminal_status() -> None:
    """The console stops polling and closes the log stream on this flag."""
    _, payload = job_events(make_job(status="failed"))[0]

    assert payload["finished"] is True


def test_the_job_event_without_a_log_line_yet_still_has_a_message_field() -> None:
    """The event queueing a job fires before any log line exists."""
    _, payload = job_events(make_job(status="pending", logs=[]))[0]

    assert payload["message"] == ""
    assert payload["level"] == "info"


def test_the_job_event_reflects_only_the_newest_log_line() -> None:
    """A chatty step must not repeat every earlier line on every frame."""
    job = make_job(logs=[log_line("first"), log_line("second", "warning")])

    _, payload = job_events(job)[0]

    assert payload["message"] == "second"
    assert payload["level"] == "warning"


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


#: A machine snapshot with recognisable, distinct values in every field, so a
#: test can tell the JSON frame carried the real thing and not some other
#: field's default.
FAKE_MACHINE_STATE = MachineState(
    hostname="box.example",
    uptime_s=12345.0,
    load=(0.1, 0.2, 0.3),
    load_history=[0.1, 0.15, 0.2],
    cpu_percent=42.5,
    memory=MemorySnapshot(used=1024, total=4096, percent=25.0),
    disk=DiskSnapshot(used=2048, total=8192, percent=25.0),
    units=UnitTally(running=3, failed=1, stopped=2),
    apps=AppTally(running=2, failed=1, stopped=0, static=1),
)


def test_the_stream_emits_the_machine_snapshot_as_a_named_json_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    The `machine` event rides the shared connection as JSON, for the console
    to parse without an HTML fragment in the middle.
    """
    monkeypatch.setattr(events_module, "MACHINE_INTERVAL_SECONDS", 0.0)
    monkeypatch.setattr(events_module, "read_machine", lambda: FAKE_MACHINE_STATE)

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

    assert frame.startswith("event: machine\ndata: ")
    assert frame.endswith("\n\n")
    payload = json.loads(frame.removeprefix("event: machine\ndata: ").strip())
    assert payload == {
        "hostname": "box.example",
        "uptime_s": 12345.0,
        "load": [0.1, 0.2, 0.3],
        "load_history": [0.1, 0.15, 0.2],
        "cpu_percent": 42.5,
        "memory": {"used": 1024, "total": 4096, "percent": 25.0},
        "disk": {"used": 2048, "total": 8192, "percent": 25.0},
        "units": {"running": 3, "failed": 1, "stopped": 2},
        "apps": {"running": 2, "failed": 1, "stopped": 0, "static": 1},
    }


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
        publish(make_job(job_id="job-9", status="completed", domain="example.com"))

        frames = [await stream.__anext__() for _ in range(4)]
        await stream.aclose()
        return frames

    frames = asyncio.run(exercise())

    assert any(frame.startswith("event: metrics\n") for frame in frames)
    assert 'event: state\ndata: {"id":"job-9","state":"active"}\n\n' in frames
    assert 'event: state\ndata: {"id":"example.com","state":"active"}\n\n' in frames


def test_a_snapshot_that_cannot_be_read_costs_a_frame_not_the_feed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    The read samples systemd and psutil; a transient failure there must not
    take down the connection carrying the job events.
    """
    from wasm.web.jobs import get_job_manager

    def refuse() -> MachineState:
        raise WASMError("systemd is restarting")

    monkeypatch.setattr(events_module, "MACHINE_INTERVAL_SECONDS", 0.01)
    monkeypatch.setattr(events_module, "read_machine", refuse)

    assert machine_frame() is None, "a failed read must yield nothing, not raise"

    async def exercise() -> list[str]:
        """
        Returns:
            The frames the stream produced for one finished job.
        """
        stream = _stream(FakeRequest())
        await stream.__anext__()

        manager = get_job_manager()
        publish = manager._global_subscribers[-1]
        publish(make_job(job_id="job-3", status="completed", domain="example.com"))

        frames = [await stream.__anext__() for _ in range(3)]
        await stream.aclose()
        return frames

    frames = asyncio.run(exercise())

    assert 'event: state\ndata: {"id":"job-3","state":"active"}\n\n' in frames
    assert not any(frame.startswith("event: machine") for frame in frames)


def test_the_pushed_snapshot_is_the_one_true_implementation(runner: Any) -> None:
    """
    One implementation: the ``machine`` event is exactly
    :func:`wasm.web.machine.read_machine`'s answer, JSON-encoded, so the
    pushed snapshot and ``GET /api/system/machine`` can never disagree.

    Args:
        runner: The fake command runner, so the unit tally reaches no process.
    """
    frame = machine_frame()

    assert frame is not None
    assert frame.startswith("event: machine\ndata: ")
    payload = json.loads(frame.removeprefix("event: machine\ndata: ").strip())
    assert set(payload) == {
        "hostname",
        "uptime_s",
        "load",
        "load_history",
        "cpu_percent",
        "memory",
        "disk",
        "units",
        "apps",
    }


# ---------------------------------------------------------------------------
# The `app` event: an application's state changed
# ---------------------------------------------------------------------------


class Captured:
    """Records what a publisher would have sent."""

    def __init__(self) -> None:
        """Start empty."""
        self.events: list[tuple[str, dict[str, Any]]] = []
        self.refreshed: list[str] = []

    def publish(self, name: str, payload: dict[str, Any]) -> None:
        """
        Args:
            name: Event name.
            payload: Event body.
        """
        self.events.append((name, payload))

    def refresh(self, domain: str) -> None:
        """
        Args:
            domain: Domain whose state would be read and published.
        """
        self.refreshed.append(domain)


@pytest.fixture
def captured() -> Captured:
    """
    Returns:
        An empty capture.
    """
    return Captured()


@pytest.fixture
def publisher(captured: Captured) -> events_module.AppStatePublisher:
    """
    Args:
        captured: Where the publisher's output goes.

    Returns:
        A publisher that spawns no thread and queries no systemd.
    """
    return events_module.AppStatePublisher(publish=captured.publish, refresh=captured.refresh)


@pytest.mark.parametrize("job_type", ["deploy", "update", "restore"])
def test_a_running_deploy_shows_the_application_as_deploying(
    publisher: events_module.AppStatePublisher, captured: Captured, job_type: str
) -> None:
    """
    Args:
        publisher: The publisher under test.
        captured: What it sent.
        job_type: A job that rebuilds or replaces the application.
    """
    publisher(make_job(job_type=job_type, status="running", domain="shop.example.com"))

    assert captured.events == [("app", {"domain": "shop.example.com", "status": "deploying"})]


@pytest.mark.parametrize("status", ["completed", "failed", "cancelled"])
@pytest.mark.parametrize("job_type", ["deploy", "update", "restore", "delete", "service_action"])
def test_a_finished_application_job_publishes_the_application_s_real_state(
    publisher: events_module.AppStatePublisher, captured: Captured, job_type: str, status: str
) -> None:
    """
    Whatever the outcome, the truth is what the store and systemd say now.

    A failed deploy may have rolled back to a release that is running; a
    delete leaves nothing. Guessing from the job's status would be wrong in
    both cases.

    Args:
        publisher: The publisher under test.
        captured: What it sent.
        job_type: A job that changes the application.
        status: A terminal status.
    """
    publisher(make_job(job_type=job_type, status=status, domain="shop.example.com"))

    assert captured.refreshed == ["shop.example.com"]
    assert captured.events == []


def test_a_log_line_does_not_republish_the_application(
    publisher: events_module.AppStatePublisher, captured: Captured
) -> None:
    """
    The job manager notifies once per log line; only a change of status counts.

    Args:
        publisher: The publisher under test.
        captured: What it sent.
    """
    job = make_job(status="running")
    for line in ("npm ci", "npm run build", "next build"):
        job.logs.append(log_line(line))
        publisher(job)

    assert len(captured.events) == 1


@pytest.mark.parametrize("job_type", ["backup", "cert_create", "cert_renew", "custom"])
def test_jobs_that_do_not_change_the_application_are_not_its_events(
    publisher: events_module.AppStatePublisher, captured: Captured, job_type: str
) -> None:
    """
    A certificate renewal carries a domain but leaves the application as it was.

    Args:
        publisher: The publisher under test.
        captured: What it sent.
        job_type: A job type that acts beside the application.
    """
    publisher(make_job(job_type=job_type, status="completed"))
    publisher(make_job(job_type=job_type, status="running", job_id="job-2"))

    assert captured.events == [] and captured.refreshed == []


def test_a_job_without_a_domain_is_not_an_application_event(
    publisher: events_module.AppStatePublisher, captured: Captured
) -> None:
    """
    Args:
        publisher: The publisher under test.
        captured: What it sent.
    """
    publisher(make_job(domain=None, status="completed"))

    assert captured.events == [] and captured.refreshed == []


@pytest.mark.parametrize(
    ("method", "path", "expected"),
    [
        ("POST", "/api/apps/shop.example.com/start", "shop.example.com"),
        ("POST", "/api/apps/shop.example.com/stop", "shop.example.com"),
        ("POST", "/api/apps/shop.example.com/restart", "shop.example.com"),
        ("PUT", "/api/apps/shop.example.com/env", "shop.example.com"),
        ("GET", "/api/apps/shop.example.com", None),
        ("POST", "/api/apps", None),
        ("POST", "/api/apps/inspect", None),
        ("POST", "/api/services/wasm-shop/restart", None),
        ("POST", "/apps/shop.example.com/start", None),
    ],
)
def test_the_application_a_mutation_acted_on_is_named_by_its_path(
    method: str, path: str, expected: str | None
) -> None:
    """
    Args:
        method: The request method.
        path: The request path.
        expected: The domain the mutation acted on, or None.
    """
    assert events_module.app_domain_of(method, path) == expected


class ListeningHub:
    """Attaches a listener to the real hub for the length of a test."""

    def __init__(self) -> None:
        """Start with no frame received."""
        self.frames: list[str] = []

    def __call__(self, frame: str) -> None:
        """
        Args:
            frame: A frame the hub delivered.
        """
        self.frames.append(frame)


@pytest.fixture
def listening() -> Iterator[ListeningHub]:
    """
    Yields:
        A listener attached to the process-wide hub, detached afterwards.
    """
    listener = ListeningHub()
    events_module.hub.attach(listener)
    try:
        yield listener
    finally:
        events_module.hub.detach(listener)


def test_a_successful_start_publishes_the_application(
    listening: ListeningHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    Start, stop and restart are synchronous calls, not jobs.

    Without this, a restart from one tab leaves every other tab showing the
    unit as stopped until something else happens to refetch it.

    Args:
        listening: A listener on the hub, so publishing is not skipped.
        monkeypatch: Runs the publication inline and fakes the snapshot.
    """
    monkeypatch.setattr(events_module, "_in_thread", events_module.publish_app_state)
    monkeypatch.setattr(
        events_module,
        "app_snapshot",
        lambda domain: {"domain": domain, "status": "running", "active": True},
    )

    events_module.announce_app_mutation("POST", "/api/apps/shop.example.com/restart", 200)

    assert listening.frames == [
        'event: app\ndata: {"domain":"shop.example.com","status":"running","active":true}\n\n'
    ]


def test_a_service_restart_of_a_units_app_publishes_it(
    store: Any, listening: ListeningHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    Starting, stopping or restarting a unit from the services page is the
    same systemctl call the applications page makes; only the page differs.
    Without this, a restart issued from /api/services left every open
    console showing the application as whatever it was before.

    Args:
        store: The sandboxed store, holding the app and its unit.
        listening: A listener on the hub, so publishing is not skipped.
        monkeypatch: Runs the publication inline and fakes the snapshot.
    """
    from wasm.core.store import App, Service

    app = store.create_app(
        App(domain="shop.example.com", app_type="nodejs", app_path="/var/www/apps/shop")
    )
    store.create_service(
        Service(
            app_id=app.id,
            name="shop-example-com",
            unit_file="/etc/systemd/system/shop-example-com.service",
        )
    )
    monkeypatch.setattr(events_module, "_in_thread", events_module.publish_app_state)
    monkeypatch.setattr(
        events_module, "app_snapshot", lambda domain: {"domain": domain, "status": "running"}
    )

    events_module.announce_app_mutation("POST", "/api/services/shop-example-com/restart", 200)

    assert listening.frames == [
        'event: app\ndata: {"domain":"shop.example.com","status":"running"}\n\n'
    ]


def test_a_service_action_for_a_unit_owned_by_no_application_publishes_nothing(
    store: Any, listening: ListeningHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    A unit made with ``wasm service create`` was never tied to a deployment.

    Args:
        store: The sandboxed store, with no service registered.
        listening: A listener on the hub.
        monkeypatch: Records any attempt to publish.
    """
    attempts: list[str] = []
    monkeypatch.setattr(events_module, "_in_thread", attempts.append)

    events_module.announce_app_mutation("POST", "/api/services/standalone-cron/restart", 200)

    assert attempts == []


def test_a_service_action_outside_start_stop_restart_publishes_nothing(
    store: Any, listening: ListeningHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    Enabling, disabling, reconfiguring or deleting a unit is not application
    state changing hands; only the three actions the apps page also exposes
    are announced.

    Args:
        store: The sandboxed store, holding the app and its unit.
        listening: A listener on the hub.
        monkeypatch: Records any attempt to publish.
    """
    from wasm.core.store import App, Service

    app = store.create_app(
        App(domain="shop.example.com", app_type="nodejs", app_path="/var/www/apps/shop")
    )
    store.create_service(
        Service(
            app_id=app.id,
            name="shop-example-com",
            unit_file="/etc/systemd/system/shop-example-com.service",
        )
    )
    attempts: list[str] = []
    monkeypatch.setattr(events_module, "_in_thread", attempts.append)

    events_module.announce_app_mutation("POST", "/api/services/shop-example-com/enable", 200)

    assert attempts == []


@pytest.mark.parametrize("status_code", [202, 400, 403, 404, 500])
def test_a_refused_or_queued_mutation_publishes_nothing(
    listening: ListeningHub, monkeypatch: pytest.MonkeyPatch, status_code: int
) -> None:
    """
    A refusal changed nothing; a 202 queued a job that reports for itself.

    Args:
        listening: A listener on the hub.
        monkeypatch: Records any attempt to publish.
        status_code: The status the endpoint answered with.
    """
    attempts: list[str] = []
    monkeypatch.setattr(events_module, "_in_thread", attempts.append)

    events_module.announce_app_mutation("POST", "/api/apps/shop.example.com/stop", status_code)

    assert attempts == []


def test_nobody_listening_costs_no_systemd_call(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Reading an application's state runs ``systemctl``; with no console open,
    there is nobody to tell.
    """
    attempts: list[str] = []
    monkeypatch.setattr(events_module, "app_snapshot", attempts.append)

    assert not events_module.hub.listening
    events_module.announce_app_mutation("POST", "/api/apps/shop.example.com/start", 200)
    events_module.publish_app_state("shop.example.com")

    assert attempts == []


def test_a_deleted_application_is_announced_as_gone(
    listening: ListeningHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    Args:
        listening: A listener on the hub.
        monkeypatch: Makes the application unknown.
    """
    monkeypatch.setattr(events_module, "app_snapshot", lambda domain: None)

    events_module.publish_app_state("shop.example.com")

    assert listening.frames == [
        'event: app\ndata: {"domain":"shop.example.com","deleted":true}\n\n'
    ]


def test_a_state_that_cannot_be_read_costs_the_event_not_the_caller(
    listening: ListeningHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    Args:
        listening: A listener on the hub.
        monkeypatch: Makes systemd unreachable.
    """

    def refuse(domain: str) -> dict[str, Any]:
        raise WASMError("Failed to connect to bus")

    monkeypatch.setattr(events_module, "app_snapshot", refuse)

    events_module.publish_app_state("shop.example.com")

    assert listening.frames == []


@pytest.fixture
def store(tmp_path: Path) -> Iterator[Any]:
    """
    Give the API a store of its own.

    Args:
        tmp_path: Per-test temporary directory.

    Yields:
        The store the endpoints read.
    """
    from wasm.core.store import WASMStore

    WASMStore.reset_instance()
    instance = WASMStore(tmp_path / "wasm.db")
    try:
        yield instance
    finally:
        instance.close()
        WASMStore.reset_instance()


def test_the_app_event_is_what_the_app_endpoint_answers(
    runner: Any, store: Any, listening: ListeningHub
) -> None:
    """
    One implementation: the event carries ``GET /api/apps/{domain}``'s answer.

    Args:
        runner: The fake command runner systemd is queried through.
        store: The sandboxed store.
        listening: A listener on the hub.
    """
    from wasm.core.store import App
    from wasm.web.api.apps import get_app
    from wasm.web.pydantic_compat import dump_model

    store.create_app(
        App(
            domain="shop.example.com",
            app_type="static",
            source="/srv/shop",
            port=None,
            app_path="/var/www/apps/shop.example.com",
            status="running",
        )
    )

    events_module.publish_app_state("shop.example.com")

    assert len(listening.frames) == 1
    payload = json.loads(listening.frames[0].removeprefix("event: app\ndata: ").strip())
    assert payload == dump_model(get_app("shop.example.com", {}))


def test_an_open_stream_relays_what_the_hub_publishes() -> None:
    """The hub renders once; every open stream receives the frame."""

    async def exercise() -> tuple[str, bool]:
        """
        Returns:
            The frame the stream yielded, and whether it detached on close.
        """
        stream = _stream(FakeRequest())
        await stream.__anext__()

        events_module.hub.publish("app", {"domain": "shop.example.com", "status": "stopped"})

        frame = await stream.__anext__()
        await stream.aclose()
        return frame, events_module.hub.listening

    frame, still_listening = asyncio.run(exercise())

    assert frame == 'event: app\ndata: {"domain":"shop.example.com","status":"stopped"}\n\n'
    assert not still_listening, "the stream did not detach from the hub"
