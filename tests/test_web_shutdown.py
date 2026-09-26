# Copyright (c) 2024-2026 Yago Lopez Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
Ctrl+C on ``wasm web start`` with a console tab open.

The event stream never ends by itself, and uvicorn waits for open connections
before it stops: one Ctrl+C waited for the browser forever, and the second
cancelled the stream and the lifespan mid-await and printed their
``CancelledError`` tracebacks. What is defended here:

- The server's signal handler tells the streams, and a stream told ends its
  response like any finished request.
- A cancellation that still happens at shutdown (the graceful timeout, a
  second Ctrl+C) ends quietly; one at any other time is re-raised.
- uvicorn is given a bound on the graceful shutdown where it supports one.
"""

from __future__ import annotations

import asyncio
import signal
import threading
from typing import Any

import pytest
from fastapi import FastAPI

from wasm.web import events as events_module
from wasm.web import server as server_module
from wasm.web.events import _stream, begin_shutdown, shutting_down


@pytest.fixture(autouse=True)
def fresh_shutdown_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Give every test a server that has not been asked to stop.

    Args:
        monkeypatch: Patching helper, scoped to the test.
    """
    monkeypatch.setattr(events_module, "_closing", threading.Event())


class ConnectedRequest:
    """A request whose client never goes away by itself."""

    async def is_disconnected(self) -> bool:
        """
        Returns:
            False, always.
        """
        return False


def test_an_open_stream_ends_when_the_server_is_asked_to_stop() -> None:
    """The stream returns on its own, so uvicorn has nothing to wait for or cancel."""

    async def exercise() -> list[str]:
        """
        Returns:
            Every frame the stream produced after the opening one.
        """
        stream = _stream(ConnectedRequest())
        await stream.__anext__()
        waiting = asyncio.ensure_future(stream.__anext__())
        await asyncio.sleep(0.05)
        assert not waiting.done(), "the stream should be waiting for its next event"

        begin_shutdown()

        rest: list[str] = []
        try:
            rest.append(await asyncio.wait_for(waiting, timeout=2))
            async for frame in stream:
                rest.append(frame)
        except StopAsyncIteration:
            pass
        return rest

    frames = asyncio.run(exercise())

    # Ended without writing the wake-up, or anything else, to the wire.
    assert frames == []


def test_a_stream_opened_while_stopping_ends_at_once() -> None:
    """A browser reconnecting in the shutdown window is not given a new endless feed."""
    begin_shutdown()

    async def exercise() -> list[str]:
        """
        Returns:
            Every frame the stream produced.
        """
        return [frame async for frame in _stream(ConnectedRequest())]

    frames = asyncio.run(asyncio.wait_for(exercise(), timeout=2))

    assert frames == [": connected\n\n"]


def test_the_signal_handler_schedules_the_streams_to_end(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    uvicorn owns SIGINT, so its handler is the one place to learn of a stop.

    The handler only schedules the work on the loop: it interrupts whatever
    the main thread was doing, which may be holding a lock the work takes.
    """
    import uvicorn

    servers: list[Any] = []
    monkeypatch.setattr(uvicorn.Server, "run", lambda self, sockets=None: servers.append(self))

    server_module._serve({"app": FastAPI()})
    (server,) = servers

    async def exercise() -> tuple[bool, bool]:
        """
        Returns:
            Whether the streams were told before and after the loop ran.
        """
        server.loop = asyncio.get_running_loop()
        server.handle_exit(signal.SIGINT, None)
        before = shutting_down()
        await asyncio.sleep(0)
        return before, shutting_down()

    before, after = asyncio.run(exercise())

    assert before is False, "the handler itself must not do the work"
    assert after is True
    assert server.should_exit is True


def test_uvicorn_is_given_a_bound_on_the_graceful_shutdown() -> None:
    """An open stream can never hold shutdown longer than this."""
    kwargs = server_module._uvicorn_kwargs(FastAPI(), "127.0.0.1", 8080, None, None)

    assert kwargs["timeout_graceful_shutdown"] == server_module.GRACEFUL_SHUTDOWN_SECONDS
    assert 0 < server_module.GRACEFUL_SHUTDOWN_SECONDS <= 10


def test_an_older_uvicorn_is_not_given_an_argument_it_refuses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Debian 12 packages uvicorn 0.17, which has no graceful shutdown timeout."""
    import uvicorn

    class OldConfig:
        def __init__(self, app: Any, host: str = "127.0.0.1", port: int = 8000) -> None:
            self.app = app

    monkeypatch.setattr(uvicorn, "Config", OldConfig)

    assert server_module._graceful_shutdown_kwargs() == {}


def _cancelled_request() -> Any:
    """
    Returns:
        A coroutine that runs a request which is cancelled while it waits.
    """

    async def request() -> None:
        await asyncio.sleep(3600)

    async def exercise() -> None:
        task = asyncio.ensure_future(server_module._quiet_at_shutdown(request()))
        await asyncio.sleep(0)
        task.cancel()
        await task

    return exercise()


def test_a_request_cancelled_at_shutdown_ends_quietly() -> None:
    """uvicorn would log it as "Exception in ASGI application" with a traceback."""
    begin_shutdown()

    asyncio.run(_cancelled_request())


def test_a_request_cancelled_otherwise_is_still_cancelled() -> None:
    """Swallowing a cancellation nobody asked the server for would break its canceller."""
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(_cancelled_request())


def _quiet_lifespan(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Keep the lifespan's side effects out of a test that only drives its edges.

    Args:
        monkeypatch: Patching helper, scoped to the test.
    """
    from wasm.web import metrics_collector

    monkeypatch.setattr(metrics_collector, "start_metrics_collector", lambda: None)
    monkeypatch.setattr(metrics_collector, "stop_metrics_collector", lambda: None)

    class Tokens:
        def purge_expired_sessions(self) -> None:
            return None

    monkeypatch.setattr(server_module, "get_token_manager", Tokens)


def test_the_lifespan_cancelled_by_a_second_ctrl_c_ends_quietly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A forced stop skips uvicorn's lifespan shutdown and cancels the lifespan instead."""
    _quiet_lifespan(monkeypatch)
    begin_shutdown()

    async def exercise() -> None:
        async with server_module.lifespan(FastAPI()):
            raise asyncio.CancelledError

    asyncio.run(exercise())


def test_the_lifespan_cancelled_otherwise_is_still_cancelled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Outside a shutdown a cancellation is not the lifespan's to swallow."""
    _quiet_lifespan(monkeypatch)

    async def exercise() -> None:
        async with server_module.lifespan(FastAPI()):
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(exercise())
