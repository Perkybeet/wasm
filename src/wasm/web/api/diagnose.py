# Copyright (c) 2024-2026 Yago Lopez Prado
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Why an application is down, over the API.

A thin translation of :func:`wasm.managers.diagnose.diagnose` to HTTP - every
correlation rule lives there, the same place ``wasm diagnose`` reads it from,
so the console and the CLI can never disagree about why an application is
down. Kept in its own router rather than folded into ``apps.py``: another
agent owns that file while this task is in flight, and this endpoint needs no
part of it beyond the "/apps" prefix, which :mod:`wasm.web.api.router` mounts
both routers under.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from wasm.managers.diagnose import diagnose
from wasm.web.api.auth import get_current_session
from wasm.web.api.deps import WASMErrorRoute, strict_domain

router = APIRouter(route_class=WASMErrorRoute)


class DiagnoseCheck(BaseModel):
    """
    One diagnostic probe's result.

    Mirrors :class:`wasm.managers.diagnose.Check` field for field: this module
    only translates it to HTTP, it does not reinterpret it.

    Attributes:
        name: Stable identifier for the probe, such as ``"unit"`` or ``"port"``.
        status: ``"ok"``, ``"warn"``, ``"fail"`` or ``"skip"``.
        summary: One line, in WASM's own words.
        evidence: Raw output the probe collected, verbatim.
    """

    name: str
    status: str
    summary: str
    evidence: str = ""


class DiagnoseResponse(BaseModel):
    """
    The full answer to "why is this app down".

    Mirrors :class:`wasm.managers.diagnose.Diagnosis`, which is also what
    ``wasm diagnose --json`` prints - the console and the CLI read the same
    shape.

    Attributes:
        domain: The domain that was diagnosed.
        verdict: ``"healthy"``, ``"degraded"`` or ``"down"``.
        probable_cause: One sentence naming the most likely explanation, or
            ``None`` when the checks disagree with each other or are all clean.
        checks: Every probe that ran, in the order it ran in.
    """

    domain: str
    verdict: str
    probable_cause: str | None
    checks: list[DiagnoseCheck]


@router.get("/{domain}/diagnose", response_model=DiagnoseResponse)
def diagnose_app(
    domain: str, session: dict[str, Any] = Depends(get_current_session)
) -> DiagnoseResponse:
    """
    Correlate everything WASM can read about a domain into one diagnosis.

    Runs the same probes as ``wasm diagnose <domain>``: the systemd unit, the
    port, an HTTP probe direct to the app and through the web server, its last
    journal lines, the web server's own error log, its certificate, its last
    deployment, OOM kills and disk space - and reports the most likely cause
    first. Every probe only reads; nothing here changes the machine. An
    unknown domain is not a 404: the probes report it as the most likely
    cause instead, exactly as the CLI does.

    Args:
        domain: Domain to diagnose.
        session: Authenticated session, injected.

    Returns:
        Every probe's result and the verdict derived from them.
    """
    validated = strict_domain(domain)
    diagnosis = diagnose(validated)
    return DiagnoseResponse(
        domain=diagnosis.domain,
        verdict=diagnosis.verdict,
        probable_cause=diagnosis.probable_cause,
        checks=[
            DiagnoseCheck(
                name=check.name,
                status=check.status,
                summary=check.summary,
                evidence=check.evidence,
            )
            for check in diagnosis.checks
        ],
    )
