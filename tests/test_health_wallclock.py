# Copyright (c) 2024-2026 Yago Lopez Prado
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
The health check's timeout is wall-clock time.

The gate turned a timeout into ``ceil(timeout / 2)`` probes, each allowed five
seconds plus a two-second pause: an application that accepted connections and
never answered held the application's lock for about three and a half times
the timeout the operator set. The probe now loops until a monotonic deadline,
and no probe may wait longer than what is left of it. A fake clock drives it:
nothing here waits for real.
"""

from __future__ import annotations

from typing import Any
from urllib.error import URLError

import pytest

from wasm.deployers.helpers import health as health_module
from wasm.deployers.helpers.health import PROBE_TIMEOUT, wait_until_healthy
from wasm.deployers.helpers.health_gate import HealthCheck, HealthGate

URL = "http://127.0.0.1:3000/"


class Clock:
    """Monotonic time that only moves when something takes time."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


class HangingOpener:
    """An application that accepts the connection and never answers."""

    def __init__(self, clock: Clock) -> None:
        self.clock = clock
        self.timeouts: list[float] = []

    def open(self, url: str, timeout: float) -> Any:
        self.timeouts.append(timeout)
        self.clock.now += timeout
        raise URLError("timed out")


class RefusingOpener(HangingOpener):
    """Nothing listens: every probe fails at once."""

    def open(self, url: str, timeout: float) -> Any:
        self.timeouts.append(timeout)
        raise URLError("[Errno 111] Connection refused")


@pytest.fixture
def clock() -> Clock:
    """The fake clock."""
    return Clock()


def install(monkeypatch: pytest.MonkeyPatch, opener: HangingOpener) -> None:
    """Make every probe go to ``opener``."""
    monkeypatch.setattr(health_module.urllib.request, "build_opener", lambda *handlers: opener)


@pytest.mark.parametrize("timeout", [5, 12, 30, 61])
def test_an_application_that_never_answers_is_given_up_on_at_the_deadline(
    monkeypatch: pytest.MonkeyPatch, clock: Clock, timeout: int
) -> None:
    """Five-second probes and two-second pauses fit inside the timeout, not 3.5 times it."""
    opener = HangingOpener(clock)
    install(monkeypatch, opener)
    start = clock.now

    healthy = wait_until_healthy(
        URL, retries=1000, delay=2.0, within=timeout, clock=clock, sleep=clock.sleep
    )

    assert healthy is False
    assert clock.now - start <= timeout
    assert all(0 < t <= PROBE_TIMEOUT for t in opener.timeouts)


def test_no_probe_waits_longer_than_what_is_left(
    monkeypatch: pytest.MonkeyPatch, clock: Clock
) -> None:
    """With three seconds left, the probe gets three seconds, not five."""
    opener = HangingOpener(clock)
    install(monkeypatch, opener)

    wait_until_healthy(URL, retries=1000, delay=2.0, within=10, clock=clock, sleep=clock.sleep)

    # 5 s probe, 2 s pause, then 3 s left.
    assert opener.timeouts == [5, 3]


def test_a_refused_connection_is_asked_again_until_the_deadline(
    monkeypatch: pytest.MonkeyPatch, clock: Clock
) -> None:
    """A process still starting gets the whole timeout, not a fixed number of tries."""
    opener = RefusingOpener(clock)
    install(monkeypatch, opener)
    start = clock.now

    wait_until_healthy(URL, retries=1000, delay=2.0, within=30, clock=clock, sleep=clock.sleep)

    assert clock.now - start == pytest.approx(30)
    assert len(opener.timeouts) == 15  # at 0, 2, ..., 28, as 2.0's fifteen attempts


def test_without_a_deadline_the_attempts_are_what_bounds_it(
    monkeypatch: pytest.MonkeyPatch, clock: Clock
) -> None:
    """The post-deploy report still counts attempts, as it always did."""
    opener = RefusingOpener(clock)
    install(monkeypatch, opener)

    wait_until_healthy(URL, retries=3, delay=2.0, clock=clock, sleep=clock.sleep)

    assert len(opener.timeouts) == 3
    assert clock.slept == [2.0, 2.0]


def test_the_gate_gives_the_probe_the_timeout_as_a_deadline() -> None:
    """The gate passes the operator's seconds, not only a derived count."""
    asked: list[dict[str, Any]] = []

    def probe(url: str, **kwargs: Any) -> bool:
        asked.append(kwargs)
        return True

    class Units:
        def restart(self, name: str) -> None:
            pass

        def logs(self, name: str, lines: int = 50) -> str:
            return ""

    for check, seconds in ((HealthCheck(timeout=45), 45), (HealthCheck(), 30)):
        gate = HealthGate(
            unit="app", url=URL, services=Units(), logger=_Quiet(), probe=probe, check=check
        )
        assert gate.restart_and_probe() == (True, "")
        assert asked[-1]["within"] == seconds


class _Quiet:
    """A logger that says nothing."""

    def substep(self, message: str) -> None:
        pass

    def debug(self, message: str) -> None:
        pass
