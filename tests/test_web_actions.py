# Copyright (c) 2024-2026 Yago Lopez Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
Tests for the controls a resource row offers.

The panel could list a stopped service and a disabled site and do nothing about
either: the API has had start, stop, enable and disable since the beginning,
and the rows only ever called restart. The sites list was the clearest case -
it displayed "enabled" or "disabled" and gave no way to change it, which is a
screen stating the exact fact the operator came to act on.

Two rules are pinned here:

- **A row offers what would change something.** Offering every verb at all
  times means an operator clicks Start on a running unit and reads a failure
  that is really a no-op, which teaches them to distrust the screen.
- **An irreversible action names what it will do.** Revoking a certificate
  cannot be undone, and disabling a site stops answering for a domain.
"""

from __future__ import annotations

from typing import Any

import pytest

from wasm.web.views.resources import _service_actions, _site_actions


class FakeService:
    """A systemd unit, with only what the shaper reads."""

    def __init__(self, status: str = "active", enabled: bool = True) -> None:
        """
        Args:
            status: What systemd reports.
            enabled: Whether it starts on boot.
        """
        self.name = "wasm-example-com"
        self.status = status
        self.enabled = enabled


class FakeSite:
    """A web server site, with only what the shaper reads."""

    def __init__(self, enabled: bool = True) -> None:
        """
        Args:
            enabled: Whether the web server answers for it.
        """
        self.domain = "example.com"
        self.enabled = enabled


def labels(actions: list[dict[str, Any]]) -> set[str]:
    """
    Args:
        actions: Action descriptions from a shaper.

    Returns:
        The button labels.
    """
    return {action["label"] for action in actions}


# ---------------------------------------------------------------------------
# Services
# ---------------------------------------------------------------------------


def test_a_running_service_can_be_stopped_and_restarted() -> None:
    """The panel could only restart, on a screen showing it was running."""
    actions = _service_actions(FakeService(status="active"))

    assert "Stop" in labels(actions)
    assert "Restart" in labels(actions)


def test_a_running_service_is_not_offered_a_start() -> None:
    """A verb that would be a no-op teaches the operator to distrust the row."""
    assert "Start" not in labels(_service_actions(FakeService(status="active")))


def test_a_stopped_service_can_be_started() -> None:
    """This is the case the panel had no answer for at all."""
    assert "Start" in labels(_service_actions(FakeService(status="inactive")))


def test_a_stopped_service_is_not_offered_a_stop() -> None:
    """
    Args:
        None.
    """
    assert "Stop" not in labels(_service_actions(FakeService(status="inactive")))


def test_a_failed_service_can_be_started() -> None:
    """A failed unit is a stopped unit as far as the operator is concerned."""
    assert "Start" in labels(_service_actions(FakeService(status="failed")))


@pytest.mark.parametrize("status", ["active", "running", "ACTIVE"])
def test_every_spelling_of_running_is_recognised(status: str) -> None:
    """
    systemd and the store do not always agree on the word, and reading it
    wrongly offers Start on a unit that is already up.

    Args:
        status: A status meaning the unit is up.
    """
    assert "Stop" in labels(_service_actions(FakeService(status=status)))


def test_an_enabled_service_can_be_disabled() -> None:
    """Whether a unit comes back after a reboot is the operator's decision."""
    assert "Disable" in labels(_service_actions(FakeService(enabled=True)))


def test_a_disabled_service_can_be_enabled() -> None:
    """
    Args:
        None.
    """
    assert "Enable" in labels(_service_actions(FakeService(enabled=False)))


def test_enable_and_disable_are_never_offered_together() -> None:
    """They are one decision, and showing both makes the state unreadable."""
    for enabled in (True, False):
        shown = labels(_service_actions(FakeService(enabled=enabled)))
        assert not {"Enable", "Disable"} <= shown


def test_stopping_a_service_asks_first() -> None:
    """It stops answering, and the operator may be on the wrong machine."""
    stop = next(a for a in _service_actions(FakeService()) if a["label"] == "Stop")

    assert stop["confirm"]
    assert "wasm-example-com" in stop["confirm"]


def test_starting_a_service_does_not_ask() -> None:
    """
    Confirming everything is how confirmations stop being read. Starting is
    recoverable and is the thing the operator just asked for.
    """
    start = next(
        a for a in _service_actions(FakeService(status="inactive")) if a["label"] == "Start"
    )

    assert start["confirm"] is None


def test_every_service_action_reports_in_the_past_tense() -> None:
    """
    The design direction commits to an action keeping its name through the
    whole flow: the button that says Restart produces the notice Restarted.
    """
    for status in ("active", "inactive"):
        for action in _service_actions(FakeService(status=status)):
            assert action["done"], f"{action['label']} says nothing when it works"
            assert action["done"].split()[0].endswith("ed"), action["done"]


def test_every_service_action_points_at_that_service() -> None:
    """A row acting on a different unit is the worst defect this panel could have."""
    for action in _service_actions(FakeService()):
        assert "/api/services/wasm-example-com/" in action["endpoint"]


# ---------------------------------------------------------------------------
# Sites
# ---------------------------------------------------------------------------


def test_an_enabled_site_can_be_disabled() -> None:
    """The row displayed the state and offered no way to change it."""
    assert labels(_site_actions(FakeSite(enabled=True))) == {"Disable"}


def test_a_disabled_site_can_be_enabled() -> None:
    """
    Args:
        None.
    """
    assert labels(_site_actions(FakeSite(enabled=False))) == {"Enable"}


def test_disabling_a_site_says_what_stops_answering() -> None:
    """ "Are you sure?" tells an operator nothing."""
    action = _site_actions(FakeSite(enabled=True))[0]

    assert "example.com" in action["confirm"]
    assert "stops answering" in action["confirm"]


def test_every_site_action_points_at_that_domain() -> None:
    """
    Args:
        None.
    """
    for enabled in (True, False):
        for action in _site_actions(FakeSite(enabled=enabled)):
            assert action["endpoint"].startswith("/api/sites/example.com/")
