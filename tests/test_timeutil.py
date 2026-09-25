# Copyright (c) 2024-2026 Yago Lopez Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
Tests for the one place a stored timestamp gets a UTC offset attached.

The store writes ``datetime.now().isoformat()`` - naive, in whatever zone the
machine happens to be set to. A CLI process on that same machine never
noticed: it read the string back on the same machine, in the same zone. A
browser reading the same string over the API has no such luck, so
``to_iso_offset`` is the one function every timestamp-carrying response field
funnels through.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from wasm.core.timeutil import to_iso_offset


def test_none_stays_none() -> None:
    """A timestamp that was never set must not become a string."""
    assert to_iso_offset(None) is None


def test_a_naive_string_gets_the_machines_own_offset() -> None:
    """
    This is the exact shape ``datetime.now().isoformat()`` produces.

    The result must parse back to the same wall-clock reading, with an
    offset attached rather than guessed at as UTC.
    """
    naive = datetime(2026, 6, 15, 10, 30, 0)

    result = to_iso_offset(naive.isoformat())

    assert result is not None
    parsed = datetime.fromisoformat(result)
    assert parsed.tzinfo is not None
    # The wall clock must not move: only the offset was attached.
    assert parsed.replace(tzinfo=None) == naive
    assert parsed.utcoffset() == naive.astimezone().utcoffset()


def test_a_naive_datetime_object_gets_the_machines_own_offset() -> None:
    """The function accepts a live ``datetime``, not only its string form."""
    naive = datetime(2026, 1, 1, 0, 0, 0)

    result = to_iso_offset(naive)

    assert result is not None
    parsed = datetime.fromisoformat(result)
    assert parsed.tzinfo is not None
    assert parsed.replace(tzinfo=None) == naive


def test_an_aware_string_is_left_exactly_as_it_was() -> None:
    """
    A value that already carries an offset is not the store's naive kind.

    Re-stamping it with the machine's own offset would be wrong whenever the
    machine's zone differs from the one the timestamp was already in.
    """
    aware = datetime(2026, 3, 1, 12, 0, 0, tzinfo=timezone(timedelta(hours=-5)))

    result = to_iso_offset(aware.isoformat())

    assert result == aware.isoformat()


def test_an_aware_datetime_object_is_left_exactly_as_it_was() -> None:
    """Same guarantee, for a live ``datetime`` instead of its string form."""
    aware = datetime(2026, 3, 1, 12, 0, 0, tzinfo=timezone.utc)

    result = to_iso_offset(aware)

    assert result == aware.isoformat()


def test_a_string_that_is_not_a_timestamp_is_returned_unchanged() -> None:
    """
    systemd's own printed text ("Wed 2024-01-01 UTC") must not crash a
    response just because it landed on a field this function also touches;
    the operator needs to see it verbatim, not lose it to a 500.
    """
    assert to_iso_offset("never") == "never"


def test_an_empty_string_is_returned_unchanged() -> None:
    """A field that is a plain str, not optional, must stay a str."""
    assert to_iso_offset("") == ""


def test_the_offset_reflects_daylight_saving_at_that_date_not_today() -> None:
    """
    Python's own naive-to-aware conversion already accounts for this; this
    test pins that the function does not flatten it to "now's" offset.
    """
    winter = datetime(2026, 1, 15, 12, 0, 0)
    summer = datetime(2026, 7, 15, 12, 0, 0)

    winter_result = to_iso_offset(winter.isoformat())
    summer_result = to_iso_offset(summer.isoformat())

    assert winter_result is not None and summer_result is not None
    assert datetime.fromisoformat(winter_result).utcoffset() == winter.astimezone().utcoffset()
    assert datetime.fromisoformat(summer_result).utcoffset() == summer.astimezone().utcoffset()
