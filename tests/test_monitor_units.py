"""
Which units the monitor watches, and what it calls a failure.

From the owner's production: ``unit_failed`` never fired. The monitor only
checked ``monitor.watch_units``, which defaults to empty, so none of WASM's own
application units was watched; and once one was, any state but ``active``
counted as down, so a unit an operator stopped alerted exactly like a crash.
These tests pin the replacement: every unit WASM manages is watched, one
``systemctl show`` reads them all, and only a failure or a crash loop alerts.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from wasm.core.exceptions import ServiceError
from wasm.core.runner import FakeRunner
from wasm.managers.service_manager import ServiceManager
from wasm.monitor.metrics import collect_service_health
from wasm.monitor.models import ServiceHealth
from wasm.monitor.process_monitor import (
    DEFAULT_SCAN_INTERVAL,
    SCAN_INTERVAL_WARNING_SECONDS,
    MonitorConfig,
    ProcessMonitor,
    scan_interval_warning,
    unit_failure,
)


def show_block(
    unit: str,
    *,
    active: str = "active",
    sub: str = "running",
    result: str = "success",
    status: int = 0,
    restarts: int = 0,
    load: str = "loaded",
    names: str | None = None,
) -> str:
    """
    Render what ``systemctl show --property=...`` prints for one unit.

    Args:
        unit: The unit's Id, with its suffix.
        active: ActiveState.
        sub: SubState.
        result: Result of the last run.
        status: ExecMainStatus.
        restarts: NRestarts.
        load: LoadState.
        names: Names, when it differs from the Id (an alias).

    Returns:
        The block, without the blank line that separates blocks.
    """
    return (
        f"Id={unit}\n"
        f"Names={names or unit}\n"
        f"LoadState={load}\n"
        f"ActiveState={active}\n"
        f"SubState={sub}\n"
        "UnitFileState=enabled\n"
        f"Result={result}\n"
        f"ExecMainStatus={status}\n"
        f"NRestarts={restarts}\n"
    )


class FakeServiceManager:
    """
    The real ServiceManager on a fake runner, with the managed set given.

    Which units WASM manages is the service manager's to decide and its own
    tests pin it; here it is an input. Reading their state is the real
    ``describe_units``, so the ``systemctl show`` asserted on is the real one.
    """

    def __init__(
        self, runner: FakeRunner, names: list[str], error: Exception | None = None
    ) -> None:
        self.names = names
        self.error = error
        self.describe_units = ServiceManager(runner=runner).describe_units

    def managed_units(self) -> list[SimpleNamespace]:
        if self.error is not None:
            raise self.error
        return [SimpleNamespace(name=name) for name in self.names]


class FakeEventNotifier:
    """Records every event instead of delivering it."""

    def __init__(self) -> None:
        self.events: list[Any] = []

    def notify(self, event: Any) -> None:
        self.events.append(event)


def make_monitor(
    runner: FakeRunner,
    managed: list[str],
    *,
    extras: tuple[str, ...] = (),
    services: Any = None,
) -> tuple[ProcessMonitor, FakeEventNotifier]:
    """
    Build a monitor over fakes.

    Args:
        runner: Answers systemctl.
        managed: The units the service manager says WASM manages.
        extras: ``monitor.watch_units``.
        services: A service manager to use instead of one listing ``managed``.

    Returns:
        The monitor and the notifier that records what it announced.
    """
    notifier = FakeEventNotifier()
    monitor = ProcessMonitor(
        config=MonitorConfig(watch_units=extras),
        runner=runner,
        event_notifier=notifier,
        service_manager=services or FakeServiceManager(runner, managed),
    )
    # The notifier path reloads the configuration from disk first; nothing
    # here depends on it.
    monitor.global_config.reload = lambda: None  # type: ignore[method-assign]
    return monitor, notifier


def scan(runner: FakeRunner, monitor: ProcessMonitor, *blocks: str) -> None:
    """
    Run one service check with systemd answering ``blocks``.

    Args:
        runner: The fake runner; later scripts win, so each scan re-scripts.
        monitor: The monitor.
        blocks: One ``show_block`` per unit.
    """
    runner.script(["systemctl", "show"], stdout="\n".join(blocks))
    monitor._report_services()


def show_calls(runner: FakeRunner) -> list[tuple[str, ...]]:
    """The ``systemctl show`` calls the runner saw."""
    return [call for call in runner.calls if call[:2] == ("systemctl", "show")]


# ---------------------------------------------------------------------------
# What is watched
# ---------------------------------------------------------------------------


def test_every_unit_wasm_manages_is_watched_without_configuration() -> None:
    """watch_units defaults to empty, and that used to mean nothing was watched."""
    runner = FakeRunner()
    monitor, _ = make_monitor(runner, ["shop-example-com", "wasm-legacy-com"])

    assert MonitorConfig().watch_units == ()
    assert monitor.watched_units() == ["shop-example-com", "wasm-legacy-com"]


def test_watch_units_are_extras_on_top_of_wasms_own() -> None:
    """An operator's list adds units; it does not replace WASM's, and nothing is doubled."""
    runner = FakeRunner()
    monitor, _ = make_monitor(
        runner, ["shop-example-com"], extras=("postgresql", "shop-example-com.service")
    )

    assert monitor.watched_units() == ["shop-example-com", "postgresql"]


def test_a_failed_listing_still_watches_the_extras() -> None:
    """The service manager failing costs the managed units one scan, not the extras."""
    runner = FakeRunner()
    monitor, _ = make_monitor(
        runner,
        [],
        extras=("postgresql",),
        services=FakeServiceManager(runner, [], error=ServiceError("systemctl unreachable")),
    )

    assert monitor.watched_units() == ["postgresql"]


def test_every_unit_is_read_in_one_systemctl_call() -> None:
    """Result, ExecMainStatus and NRestarts for all units, one process per scan."""
    runner = FakeRunner()
    monitor, _ = make_monitor(runner, ["a-com", "b-com", "c-com"])

    scan(runner, monitor, show_block("a-com.service"), show_block("b-com.service"))

    calls = show_calls(runner)
    assert len(calls) == 1
    call = calls[0]
    assert call[-3:] == ("a-com.service", "b-com.service", "c-com.service")
    properties = call[call.index("-p") + 1]
    for name in ("ActiveState", "SubState", "Result", "ExecMainStatus", "NRestarts"):
        assert name in properties
    assert not any(c[:2] == ("systemctl", "is-active") for c in runner.calls)


def test_each_unit_gets_its_own_block_and_a_silent_one_is_unknown() -> None:
    """Blocks are matched by Id; a unit systemd said nothing about is neither up nor failed."""
    runner = FakeRunner()
    runner.script(
        ["systemctl", "show"],
        stdout="\n".join(
            [
                show_block("postgresql.service"),
                show_block(
                    "shop-example-com.service",
                    active="failed",
                    sub="failed",
                    result="exit-code",
                    status=1,
                    restarts=4,
                ),
            ]
        ),
    )

    health = collect_service_health(
        ["shop-example-com", "postgresql.service", "missing"], runner=runner
    )

    shop, pg, missing = health
    assert shop.unit == "shop-example-com"
    assert shop.active_state == "failed"
    assert shop.result == "exit-code"
    assert shop.exec_main_status == 1
    assert shop.restarts == 4
    assert pg.unit == "postgresql.service"
    assert pg.active is True
    assert missing.active_state == ""
    assert missing.restarts is None
    assert unit_failure(missing, previous_restarts=None) is None


def test_no_units_asks_systemd_nothing() -> None:
    """An empty watch list is not a call with no arguments, which lists the manager."""
    runner = FakeRunner()

    assert collect_service_health([], runner=runner) == []
    assert runner.calls == []


# ---------------------------------------------------------------------------
# What is a failure
# ---------------------------------------------------------------------------


def test_a_failed_unit_alerts_once_and_rearms_after_it_recovers() -> None:
    """One event per outage: not one per scan, and a second outage is a second event."""
    runner = FakeRunner()
    monitor, notifier = make_monitor(runner, ["shop-example-com"])
    failed = show_block(
        "shop-example-com.service", active="failed", sub="failed", result="exit-code", status=1
    )

    scan(runner, monitor, failed)
    scan(runner, monitor, failed)

    assert len(notifier.events) == 1
    event = notifier.events[0]
    assert event.kind == "unit_failed"
    assert event.title == "Unit shop-example-com failed"
    assert "result exit-code" in event.body
    assert "exit status 1" in event.body
    assert "journalctl -u shop-example-com" in event.body

    scan(runner, monitor, show_block("shop-example-com.service"))
    scan(runner, monitor, failed)

    assert len(notifier.events) == 2


def test_a_unit_stopped_on_purpose_does_not_alert() -> None:
    """inactive after a clean stop is what `wasm stop` leaves; nobody needs a message for it."""
    runner = FakeRunner()
    monitor, notifier = make_monitor(runner, ["shop-example-com"])

    scan(runner, monitor, show_block("shop-example-com.service"))
    scan(runner, monitor, show_block("shop-example-com.service", active="inactive", sub="dead"))

    assert notifier.events == []


def test_an_inactive_unit_whose_last_run_failed_alerts() -> None:
    """Inactive is only a clean stop when the last run ended in success."""
    runner = FakeRunner()
    monitor, notifier = make_monitor(runner, ["shop-example-com"])

    scan(
        runner,
        monitor,
        show_block(
            "shop-example-com.service", active="inactive", sub="dead", result="signal", status=9
        ),
    )

    assert [e.title for e in notifier.events] == ["Unit shop-example-com stopped on a failure"]
    assert "result signal" in notifier.events[0].body


def test_a_crash_loop_alerts_when_the_restart_count_grows() -> None:
    """
    Review Focus: 7750 restarts of "Missing script: start", never announced.

    The first scan has no baseline; the second sees NRestarts grow while the
    unit waits to be restarted. A loop caught between two crashes is still the
    same outage, and only a scan with no new restarts re-arms.
    """
    runner = FakeRunner()
    monitor, notifier = make_monitor(runner, ["arennalabs-com"])
    unit = "arennalabs-com.service"

    scan(
        runner,
        monitor,
        show_block(
            unit, active="activating", sub="auto-restart", result="exit-code", status=1, restarts=5
        ),
    )
    assert notifier.events == []

    scan(
        runner,
        monitor,
        show_block(
            unit, active="activating", sub="auto-restart", result="exit-code", status=1, restarts=11
        ),
    )
    assert [e.title for e in notifier.events] == ["Unit arennalabs-com is crash-looping"]
    assert "6 time(s) since the previous check" in notifier.events[0].body
    assert "11 automatic restarts in total" in notifier.events[0].body

    # Caught up between two crashes: still restarting, still the same outage.
    scan(runner, monitor, show_block(unit, restarts=12))
    assert len(notifier.events) == 1

    # Up, with no restart since the previous scan: recovered, and re-armed.
    scan(runner, monitor, show_block(unit, restarts=12))
    scan(
        runner,
        monitor,
        show_block(
            unit, active="activating", sub="auto-restart", result="exit-code", status=1, restarts=13
        ),
    )
    assert len(notifier.events) == 2


def test_many_restarts_in_one_interval_is_a_loop_even_when_caught_up() -> None:
    """A unit that dies every few seconds is usually caught active; the count gives it away."""
    runner = FakeRunner()
    monitor, notifier = make_monitor(runner, ["shop-example-com"])
    unit = "shop-example-com.service"

    scan(runner, monitor, show_block(unit, restarts=0))
    scan(runner, monitor, show_block(unit, restarts=4))

    assert [e.title for e in notifier.events] == ["Unit shop-example-com is crash-looping"]


def test_a_single_restart_that_recovered_is_not_a_loop() -> None:
    """One crash that systemd restarted into a running unit is Restart= doing its job."""
    health = ServiceHealth(
        unit="shop-example-com",
        active=True,
        enabled=True,
        active_state="active",
        sub_state="running",
        result="success",
        restarts=3,
    )

    assert unit_failure(health, previous_restarts=2) is None


def test_the_start_limit_is_explained() -> None:
    """start-limit-hit is what the new unit template ends a hopeless loop with."""
    health = ServiceHealth(
        unit="shop-example-com",
        active=False,
        enabled=True,
        active_state="failed",
        sub_state="failed",
        result="start-limit-hit",
        exec_main_status=1,
        restarts=5,
    )

    failure = unit_failure(health, previous_restarts=None)

    assert failure is not None
    assert failure.kind == "failed"
    assert "stopped restarting it after repeated failures" in failure.detail


def test_a_missing_extra_unit_is_logged_not_alerted() -> None:
    """A typo in watch_units is the operator's to fix, not an outage."""
    runner = FakeRunner()
    monitor, notifier = make_monitor(runner, [], extras=("postgressql",))
    warnings: list[str] = []
    monitor.logger.warning = warnings.append  # type: ignore[method-assign]

    scan(
        runner,
        monitor,
        show_block(
            "postgressql.service", active="inactive", sub="dead", load="not-found", result="success"
        ),
    )

    assert notifier.events == []
    assert warnings == ["Watched unit postgressql does not exist"]


# ---------------------------------------------------------------------------
# How often
# ---------------------------------------------------------------------------


def test_the_default_scan_interval_is_a_minute() -> None:
    """Both defaults agree: the config file's and the monitor's own."""
    from wasm.core.config import DEFAULT_CONFIG

    assert DEFAULT_SCAN_INTERVAL == 60
    assert DEFAULT_CONFIG["monitor"]["scan_interval"] == DEFAULT_SCAN_INTERVAL
    assert MonitorConfig().scan_interval == DEFAULT_SCAN_INTERVAL


@pytest.mark.parametrize("interval", [10, 60, SCAN_INTERVAL_WARNING_SECONDS])
def test_a_prompt_interval_has_no_warning(interval: int) -> None:
    """Up to five minutes, the operator's choice needs no comment."""
    assert scan_interval_warning(interval) is None


def test_a_long_interval_is_respected_but_warned_about() -> None:
    """The owner's 3600 s: kept, and the status says what it costs."""
    assert MonitorConfig(scan_interval=3600).scan_interval == 3600

    warning = scan_interval_warning(3600)

    assert warning is not None
    assert "unnoticed for up to 60 minutes" in warning
    assert "monitor.scan_interval" in warning
