# Copyright (c) 2024-2026 Yago Lopez Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
Tests that the panel's journal streams do not leave processes behind.

Every log stream in the panel spawns a ``journalctl -f``, which by definition
never ends on its own, so whether it is cleaned up decides whether a server
that has been up for a month is still healthy. A browser reconnects an event
source or a socket by itself, and an operator leaves the panel open for days:
one orphan per connection is not a slow leak, it is a machine that eventually
cannot fork.

The system events stream had exactly that. Its ``terminate()`` sat on the happy
path, after the task loop, while closing a browser tab raises
``WebSocketDisconnect`` well before it - so the normal way of leaving the page
was the way that leaked. The ``finally`` cleaned up the WebSocket and nothing
else.

These tests exercise the cleanup helper directly rather than through a socket:
the leak is about what happens on the abnormal paths, and those are far easier
to provoke on the function than through a handshake.
"""

from __future__ import annotations

import asyncio

import pytest

from wasm.web.websockets.router import TERMINATE_GRACE_SECONDS, _terminate


class FakeProcess:
    """
    Stands in for an asyncio subprocess.

    Attributes:
        returncode: None while running, an integer once it has exited.
        terminated: Whether SIGTERM was sent.
        killed: Whether SIGKILL was sent.
    """

    def __init__(self, returncode: int | None = None, ignores_sigterm: bool = False) -> None:
        """
        Args:
            returncode: Exit status, or None for a running process.
            ignores_sigterm: Whether it keeps running after being asked nicely.
        """
        self.returncode = returncode
        self.ignores_sigterm = ignores_sigterm
        self.terminated = False
        self.killed = False

    def terminate(self) -> None:
        """Record a SIGTERM, and exit unless this process ignores it."""
        self.terminated = True
        if not self.ignores_sigterm:
            self.returncode = -15

    def kill(self) -> None:
        """Record a SIGKILL."""
        self.killed = True
        self.returncode = -9

    async def wait(self) -> int:
        """
        Returns:
            The exit status, once there is one.
        """
        while self.returncode is None:
            await asyncio.sleep(0.01)
        return self.returncode


def test_a_running_stream_is_stopped() -> None:
    """The ordinary case: the operator closed the tab."""
    process = FakeProcess()

    asyncio.run(_terminate(process))

    assert process.terminated
    assert process.returncode is not None


def test_a_stream_that_ignores_sigterm_is_killed() -> None:
    """
    A follow that will not exit politely must not be left running.

    Waiting forever for it would also hold the connection handler open, so the
    grace period is bounded and the kill is unconditional after it.
    """
    process = FakeProcess(ignores_sigterm=True)

    asyncio.run(_terminate(process))

    assert process.terminated
    assert process.killed


def test_an_already_finished_stream_is_not_signalled() -> None:
    """Signalling a reaped pid can reach whatever inherited the number."""
    process = FakeProcess(returncode=0)

    asyncio.run(_terminate(process))

    assert not process.terminated
    assert not process.killed


def test_a_stream_that_never_started_is_not_an_error() -> None:
    """
    The regression, in its smallest form.

    The process is bound before the try so that the cleanup can always see it;
    when the spawn itself is what failed, it is still None there.
    """
    asyncio.run(_terminate(None))


def test_a_process_that_disappears_between_the_check_and_the_signal_is_survived() -> None:
    """
    The pid can be reaped in the window between reading returncode and
    signalling, and a raise here would skip the rest of the cleanup.
    """

    class Vanishing(FakeProcess):
        """A process that is gone by the time it is signalled."""

        def terminate(self) -> None:
            """
            Raises:
                ProcessLookupError: Always, as the kernel would.
            """
            self.terminated = True
            raise ProcessLookupError(3, "No such process")

        def kill(self) -> None:
            """
            Raises:
                ProcessLookupError: Always, as the kernel would.
            """
            self.killed = True
            raise ProcessLookupError(3, "No such process")

    process = Vanishing()

    asyncio.run(_terminate(process))

    assert process.terminated


def test_the_grace_period_is_short_enough_to_close_a_connection_promptly() -> None:
    """
    The cleanup runs while the client is already gone, and every connection
    handler waits on it.
    """
    assert 0 < TERMINATE_GRACE_SECONDS <= 5


@pytest.mark.parametrize("handler", ["websocket_logs", "websocket_events"])
def test_every_journal_stream_cleans_up_in_a_finally(handler: str) -> None:
    """
    The shape of the bug, pinned so it cannot come back.

    ``terminate()`` on the happy path is not cleanup: the ordinary way out of
    these handlers is an exception raised by the client going away.

    Args:
        handler: Name of the WebSocket handler to inspect.
    """
    import ast
    import importlib
    import inspect
    import textwrap

    # The package exports the APIRouter object under the name "router", so the
    # module has to be imported by path.
    module = importlib.import_module("wasm.web.websockets.router")

    source = textwrap.dedent(inspect.getsource(getattr(module, handler)))
    tree = ast.parse(source)

    cleaned = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Try)
        for statement in node.finalbody
        for call in ast.walk(statement)
        if isinstance(call, ast.Call) and getattr(call.func, "id", None) == "_terminate"
    ]

    assert cleaned, f"{handler} does not stop its journal process in a finally block"
