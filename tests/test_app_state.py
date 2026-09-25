"""
Tests for the one place that decides whether an application is working.

The bug that produced this module: ``wasm list`` printed the status column and
``wasm health`` asked systemd, so on the same machine list reported fifteen
applications running while health reported seven stopped. Then the second
question, which is the harder one: systemd calls a unit active while the
process exists, so a service crash-looping every four seconds and a service
refusing every connection both read as running.
"""

from __future__ import annotations

from typing import Any

import pytest

from wasm.core.app_state import (
    FAILED,
    NOT_RESPONDING,
    RESTARTING,
    RUNNING,
    STATIC,
    STOPPED,
    UNKNOWN,
    resolve_state,
    resolve_state_with_status,
    resolve_states,
    resolve_states_with_status,
)
from wasm.core.exceptions import ServiceError
from wasm.core.store import App


class _Services:
    """A systemd that answers whatever the test scripted."""

    def __init__(self, statuses: dict[str, dict[str, Any]], failing: tuple[str, ...] = ()) -> None:
        """
        Args:
            statuses: Unit name to the status mapping to report.
            failing: Units whose query raises, as an unmanaged one would.
        """
        self.statuses = statuses
        self.failing = failing
        self.asked: list[str] = []

    def get_status(self, name: str) -> dict[str, Any]:
        """
        Args:
            name: Service name.

        Returns:
            The scripted status.

        Raises:
            ServiceError: When the test declared this unit unqueryable.
        """
        self.asked.append(name)
        if name in self.failing:
            raise ServiceError(f"{name} is not managed by WASM")
        return self.statuses.get(name, {"exists": True, "active": False})


def _app(domain: str = "example.com", *, is_static: bool = False, port: int | None = 3000) -> App:
    """
    Build an application record.

    Args:
        domain: The domain it is served on.
        is_static: Whether it is served from a directory.
        port: The port it should be answering on.

    Returns:
        The record the store would return.
    """
    return App(id=1, domain=domain, port=port, is_static=is_static)


def _active(**extra: Any) -> dict[str, Any]:
    """
    Build the status of a unit systemd is happy with.

    Args:
        **extra: Fields to override.

    Returns:
        A status mapping.

    """
    status = {
        "exists": True,
        "active": True,
        "enabled": True,
        "active_state": "active",
        "sub_state": "running",
        "restarts": "0",
    }
    status.update(extra)
    return status


def test_an_active_unit_that_answers_is_running() -> None:
    """The ordinary case."""
    services = _Services({"example-com": _active()})

    result = resolve_state(_app(), services)

    assert result.label == RUNNING
    assert result.healthy


def test_an_inactive_unit_is_stopped() -> None:
    """The case list used to miss entirely."""
    services = _Services({"example-com": {"exists": True, "active": False}})

    result = resolve_state(_app(), services)

    assert result.label == STOPPED
    assert not result.healthy


def test_a_static_site_is_never_asked_about() -> None:
    """There is no unit, so querying one could only ever say stopped."""
    services = _Services({})

    result = resolve_state(_app(is_static=True), services)

    assert result.label == STATIC
    assert result.healthy
    assert services.asked == []


def test_a_crash_loop_is_not_running() -> None:
    """
    The case the operator raised: systemd active, application broken.

    Between two restarts the unit reads as active, so "active" alone reported a
    service dying every four seconds as healthy.
    """
    services = _Services(
        {"example-com": _active(active_state="activating", sub_state="auto-restart", restarts="37")}
    )

    result = resolve_state(_app(), services)

    assert result.label == RESTARTING
    assert not result.healthy
    assert "37" in result.detail
    assert "logs" in result.detail, "the detail has to say where to look"


def test_a_failed_unit_reports_why() -> None:
    """systemd giving up carries a reason worth repeating."""
    services = _Services(
        {"example-com": _active(active_state="failed", sub_state="failed", result="exit-code")}
    )

    result = resolve_state(_app(), services)

    assert result.label == FAILED
    assert not result.healthy
    assert "exit-code" in result.detail


def test_an_active_unit_whose_port_answers_nothing(ports: Any) -> None:
    """
    A process existing is not the same as an application working.

    Args:
        ports: Port probe, told to refuse the application's port.
    """
    ports.closed.add(3000)
    services = _Services({"example-com": _active()})

    result = resolve_state(_app(), services)

    assert result.label == NOT_RESPONDING
    assert not result.healthy
    assert "3000" in result.detail


def test_the_probe_can_be_turned_off(ports: Any) -> None:
    """
    Callers that only want the systemd signals do not pay for a connection.

    Args:
        ports: Port probe, which must not be consulted.
    """
    ports.closed.add(3000)
    services = _Services({"example-com": _active()})

    result = resolve_state(_app(), services, probe=False)

    assert result.label == RUNNING
    assert ports.asked == []


def test_an_app_with_no_port_is_not_probed(ports: Any) -> None:
    """
    Args:
        ports: Port probe, which has nothing to ask about.
    """
    services = _Services({"example-com": _active()})

    result = resolve_state(_app(port=None), services)

    assert result.label == RUNNING
    assert ports.asked == []


def test_a_unit_that_cannot_be_queried_is_unknown_not_running() -> None:
    """An error is reported as an error, not smoothed into either answer."""
    services = _Services({}, failing=("example-com",))

    result = resolve_state(_app(), services)

    assert result.label == UNKNOWN
    assert not result.healthy
    assert "not managed by WASM" in result.detail


def test_a_restart_count_that_is_not_a_number_does_not_crash() -> None:
    """systemd not reporting NRestarts must not take the whole command down."""
    services = _Services({"example-com": _active(restarts="")})

    result = resolve_state(_app(), services)

    assert result.label == RUNNING


@pytest.mark.parametrize("count", [1, 5, 20])
def test_several_applications_are_all_resolved(count: int) -> None:
    """
    resolve_states runs them together; every one still gets an answer.

    Args:
        count: How many applications to resolve.
    """
    apps = [_app(f"app{index}.example.com") for index in range(count)]
    services = _Services({f"app{index}-example-com": _active() for index in range(count)})

    states = resolve_states(apps, services)

    assert len(states) == count
    assert all(s.label == RUNNING for s in states.values())


def test_no_applications_asks_nothing() -> None:
    """An empty deployment costs no systemctl calls."""
    services = _Services({})

    assert resolve_states([], services) == {}
    assert services.asked == []


# ---------------------------------------------------------------------------
# resolve_state_with_status / resolve_states_with_status
#
# A caller that also wants the fields systemd reported - the PID, whether the
# unit is enabled - would otherwise have to query systemd a second time,
# doubling the subprocess calls the docstring on resolve_states promises not
# to cost. These are the one place both come out of a single query.
# ---------------------------------------------------------------------------


def test_resolve_state_with_status_returns_the_same_label_as_resolve_state() -> None:
    services = _Services({"example-com": _active()})

    state, status = resolve_state_with_status(_app(), services)

    assert state.label == RUNNING
    assert state.healthy


def test_resolve_state_with_status_hands_back_the_raw_status_mapping() -> None:
    """The status dict is exactly what ServiceManager.get_status returned."""
    services = _Services({"example-com": _active(pid="4242")})

    _, status = resolve_state_with_status(_app(), services)

    assert status["pid"] == "4242"
    assert status["active"] is True


def test_resolve_state_with_status_queries_systemd_exactly_once() -> None:
    services = _Services({"example-com": _active()})

    resolve_state_with_status(_app(), services)

    assert services.asked == ["example-com"]


def test_resolve_state_with_status_of_a_static_app_asks_nothing() -> None:
    services = _Services({})

    state, status = resolve_state_with_status(_app(is_static=True), services)

    assert state.label == STATIC
    assert status == {}
    assert services.asked == []


def test_resolve_state_with_status_of_an_unqueryable_unit_is_unknown() -> None:
    services = _Services({}, failing=("example-com",))

    state, status = resolve_state_with_status(_app(), services)

    assert state.label == UNKNOWN
    assert status == {}


def test_resolve_state_is_a_thin_wrapper_over_resolve_state_with_status() -> None:
    """The public, single-value function must not duplicate the decision."""
    services = _Services({"example-com": _active(active_state="failed", sub_state="failed")})

    assert resolve_state(_app(), services).label == FAILED


def test_resolve_states_with_status_resolves_several_applications() -> None:
    apps = [_app("one.example.com"), _app("two.example.com", is_static=True)]
    services = _Services({"one-example-com": _active()})

    results = resolve_states_with_status(apps, services)

    assert results["one.example.com"][0].label == RUNNING
    assert results["one.example.com"][1]["active"] is True
    assert results["two.example.com"][0].label == STATIC
    assert results["two.example.com"][1] == {}


def test_resolve_states_with_status_of_no_applications_asks_nothing() -> None:
    services = _Services({})

    assert resolve_states_with_status([], services) == {}
    assert services.asked == []
