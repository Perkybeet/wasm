# Copyright (c) 2024-2026 Yago Lopez Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
Tests for the one pydantic v1/v2 bridge, and its timestamp validator.

``iso_offset_validator`` is what every response model applies to a timestamp
field, once, instead of writing a bespoke ``@field_validator`` per endpoint.
It has to behave the same, bound the same way, on both pydantic majors -
that is the entire reason the bridge module exists.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel

from wasm.web.pydantic_compat import dump_model, iso_offset_validator


class _Deployment(BaseModel):
    """A model with two timestamp fields and one that is not a timestamp."""

    id: int
    started_at: str | None = None
    finished_at: str | None = None
    status: str

    _iso_timestamps = iso_offset_validator("started_at", "finished_at")


def test_a_naive_timestamp_field_gets_an_offset() -> None:
    naive = datetime(2026, 6, 15, 9, 0, 0)

    model = _Deployment(id=1, started_at=naive.isoformat(), finished_at=None, status="running")

    assert model.started_at is not None
    parsed = datetime.fromisoformat(model.started_at)
    assert parsed.tzinfo is not None
    assert parsed.replace(tzinfo=None) == naive


def test_none_stays_none_through_the_model() -> None:
    model = _Deployment(id=1, started_at=None, finished_at=None, status="queued")

    assert model.started_at is None
    assert model.finished_at is None


def test_an_unrelated_field_is_not_touched() -> None:
    model = _Deployment(id=1, started_at=None, finished_at=None, status="queued")

    assert model.status == "queued"


def test_dump_model_reflects_the_converted_value() -> None:
    naive = datetime(2026, 6, 15, 9, 0, 0)

    model = _Deployment(id=1, started_at=naive.isoformat(), finished_at=None, status="running")
    dumped = dump_model(model)

    assert dumped["started_at"] == model.started_at
