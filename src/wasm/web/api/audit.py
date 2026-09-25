# Copyright (c) 2024-2026 Yago Lopez Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
Read side of the append-only audit log.

:class:`wasm.web.auth.AuditLogger` is where every privileged action already
writes; this module only exposes it. Audit is sensitive - it names sessions,
IPs and what they did - so it is gated behind ``admin``, not the ``read``
scope a GET would otherwise imply.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from wasm.web.api.deps import WASMErrorRoute, require_scope
from wasm.web.auth import get_audit_logger
from wasm.web.pydantic_compat import iso_offset_validator

router = APIRouter(route_class=WASMErrorRoute)

#: Most entries a single page may carry.
MAX_LIMIT = 200


class AuditEntry(BaseModel):
    """
    One audit log line.

    Attributes:
        timestamp: When the action was attempted, with its UTC offset.
        action: What was attempted, for example ``apps.delete``.
        result: Outcome, for example ``success`` or ``denied``.
        actor: Session id, API token name, ``master`` or ``anonymous``.
        client_ip: Address the request came from.
        resource: Target of the action, such as an API path.
        detail: Extra context. Never a credential.
    """

    timestamp: str
    action: str
    result: str
    actor: str
    client_ip: str | None = None
    resource: str | None = None
    detail: str | None = None

    _iso_timestamps = iso_offset_validator("timestamp")


class AuditListResponse(BaseModel):
    """
    Response for ``GET /api/audit``.

    Attributes:
        items: The matching entries, newest first.
        next_before: Pass as ``before`` to fetch the next page, or None when
            this page reached the end of the log.
    """

    items: list[AuditEntry]
    next_before: str | None = None


def _to_entry(raw: dict) -> AuditEntry:
    """
    Convert one of the logger's dicts into the API model.

    Args:
        raw: An entry as :meth:`~wasm.web.auth.AuditLogger.record` wrote it.

    Returns:
        The API representation.
    """
    return AuditEntry(
        timestamp=str(raw.get("ts", "")),
        action=str(raw.get("action", "")),
        result=str(raw.get("result", "")),
        actor=str(raw.get("actor", "")),
        client_ip=raw.get("ip"),
        resource=raw.get("resource"),
        detail=raw.get("detail"),
    )


@router.get("", response_model=AuditListResponse)
def list_audit_entries(
    session: Annotated[dict, Depends(require_scope("admin"))],
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = 50,
    before: Annotated[str | None, Query(description="Only entries older than this cursor")] = None,
    action: Annotated[str | None, Query(description="Filter by exact action")] = None,
    result: Annotated[str | None, Query(description="Filter by exact result")] = None,
    actor: Annotated[str | None, Query(description="Filter by exact actor")] = None,
) -> AuditListResponse:
    """
    List audit entries, newest first, with keyset pagination.

    Args:
        limit: Maximum entries to return.
        before: Cursor from a previous page's ``next_before``.
        action: Only entries with this exact action.
        result: Only entries with this exact result.
        actor: Only entries with this exact actor.
        session: The authenticated session; admin scope is required.

    Returns:
        The matching entries and the cursor for the next page.
    """
    audit = get_audit_logger()
    if audit is None:
        return AuditListResponse(items=[], next_before=None)

    raw_entries = audit.read(limit=limit, before=before, action=action, result=result, actor=actor)
    items = [_to_entry(entry) for entry in raw_entries]
    next_before = str(raw_entries[-1].get("ts", "")) if len(items) == limit else None

    return AuditListResponse(items=items, next_before=next_before)
