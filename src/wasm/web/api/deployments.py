# Copyright (c) 2024-2026 Yago Lopez Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
Deployment history API endpoints.

A thin client of :class:`~wasm.core.store.WASMStore`, which owns the
``deployments`` table, and of :mod:`wasm.deployers.logs`, which owns reading a
captured build log back off disk. Nothing here writes a deployment row: that
happens inside :class:`~wasm.deployers.recorder.DeploymentRecorder`, driven by
the CLI, the panel's job manager and the webhook, so the history the console
reads is exactly what those three produced.

The store's own :meth:`~wasm.core.store.WASMStore.list_deployments` only
filters by domain and takes a flat ``limit``: it has no SQL-level filter for
status or trigger, and no keyset cursor. Extending it is out of this module's
reach (``core/store.py`` is owned elsewhere in this task's file split), and the
table is small on any real machine - :func:`~wasm.core.store.WASMStore.prune_deployments`
keeps at most twenty rows per domain - so :func:`_filtered_page` fetches a
generous batch and does the filtering, ordering and pagination here instead.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from wasm.core.store import (
    DeploymentRecord,
    DeploymentStatus,
    DeploymentTrigger,
    StoreError,
    get_store,
)
from wasm.deployers.logs import read_deployment_log
from wasm.web.api.auth import get_current_session
from wasm.web.api.deps import WASMErrorRoute, strict_domain
from wasm.web.pydantic_compat import iso_offset_validator

router = APIRouter(route_class=WASMErrorRoute)

#: Rows fetched from the store before this layer's own filtering and keyset
#: pagination are applied. Generous on purpose: a page short of matches because
#: the batch was too small would be a silent bug, and the deployments table
#: stays small (see the module docstring), so there is no real cost to asking
#: for enough of it.
_FETCH_LIMIT = 20_000

#: Default and maximum rows a listing page returns.
DEFAULT_PAGE_LIMIT = 50
MAX_PAGE_LIMIT = 200


class DeploymentOut(BaseModel):
    """One deployment attempt, as the console lists and inspects it."""

    id: int
    domain: str
    status: str
    triggered_by: str
    git_commit: str | None = None
    git_branch: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    duration_s: float | None = None
    error: str | None = None
    has_log: bool

    _iso_timestamps = iso_offset_validator("started_at", "finished_at")


class DeploymentListResponse(BaseModel):
    """
    A page of deployment history.

    Attributes:
        items: The page, newest id first.
        total: Rows matching the filters, ignoring the pagination cursor -
            what the client would see across every page.
        next_before_id: Pass as ``before_id`` to fetch the next page; ``None``
            when this page reached the end of the matching rows.
    """

    items: list[DeploymentOut]
    total: int
    next_before_id: int | None = None


class DeploymentLogOut(BaseModel):
    """A deployment's captured build log, or the reason there is none."""

    content: str
    truncated: bool
    missing_reason: str | None = None


def _to_out(record: DeploymentRecord) -> DeploymentOut:
    """
    Args:
        record: A row read from the store.

    Returns:
        The API shape of the row.
    """
    # Every row this module hands to _to_out came back from the store, whose
    # schema makes id the primary key: it is never None in practice, only in
    # a DeploymentRecord() a caller has not saved yet.
    if record.id is None:
        raise StoreError(
            "Deployment record has no id",
            details="This should not happen for a row read back from the store.",
        )
    return DeploymentOut(
        id=record.id,
        domain=record.domain,
        status=record.status,
        triggered_by=record.triggered_by,
        git_commit=record.git_commit,
        git_branch=record.git_branch,
        started_at=record.started_at,
        finished_at=record.finished_at,
        duration_s=record.duration_s,
        error=record.error,
        has_log=record.log_path is not None,
    )


def _filtered_page(
    *,
    domain: str | None,
    status: DeploymentStatus | None,
    trigger: DeploymentTrigger | None,
    before_id: int | None,
    limit: int,
) -> tuple[list[DeploymentRecord], int, bool]:
    """
    Apply this layer's filtering, ordering and keyset pagination.

    Args:
        domain: Only this domain's history; every domain when None.
        status: Only rows in this status, when given.
        trigger: Only rows started by this trigger, when given.
        before_id: Only rows with an id strictly below this one - the keyset
            cursor from a previous page's ``next_before_id``.
        limit: Rows to return, at most.

    Returns:
        ``(page, total, has_more)``: the page of records, id descending; how
        many rows in total match ``domain``/``status``/``trigger`` (before the
        cursor is applied); and whether rows beyond ``before_id`` exist beyond
        this page.
    """
    records = get_store().list_deployments(domain=domain, limit=_FETCH_LIMIT)
    matching = [
        record
        for record in records
        if (status is None or record.status == status.value)
        and (trigger is None or record.triggered_by == trigger.value)
    ]
    # list_deployments orders by (started_at, id) descending; started_at has
    # second resolution, so two rows begun in the same second would not sort
    # deterministically by id alone without this explicit re-sort.
    matching.sort(key=lambda record: record.id or 0, reverse=True)

    total = len(matching)
    if before_id is not None:
        matching = [record for record in matching if (record.id or 0) < before_id]

    return matching[:limit], total, len(matching) > limit


@router.get("", response_model=DeploymentListResponse)
def list_deployments(
    session: Annotated[dict, Depends(get_current_session)],
    domain: Annotated[str | None, Query(description="Only this domain's history")] = None,
    status: Annotated[DeploymentStatus | None, Query(description="Only this status")] = None,
    trigger: Annotated[DeploymentTrigger | None, Query(description="Only this trigger")] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_LIMIT)] = DEFAULT_PAGE_LIMIT,
    before_id: Annotated[
        int | None, Query(description="Keyset cursor: only rows with an id below this one")
    ] = None,
) -> DeploymentListResponse:
    """
    List deployment history across the machine, or for one domain.

    Args:
        session: The authenticated session.
        domain: Only this domain's history, when given.
        status: Only rows in this status, when given.
        trigger: Only rows started by this trigger, when given.
        limit: Rows per page.
        before_id: Keyset cursor from a previous page's ``next_before_id``.

    Returns:
        The page, the total matching the filters, and the cursor for the next
        page.
    """
    validated_domain = strict_domain(domain) if domain else None
    page, total, has_more = _filtered_page(
        domain=validated_domain, status=status, trigger=trigger, before_id=before_id, limit=limit
    )
    next_before_id = page[-1].id if page and has_more else None

    return DeploymentListResponse(
        items=[_to_out(record) for record in page], total=total, next_before_id=next_before_id
    )


@router.get("/{deployment_id}", response_model=DeploymentOut)
def get_deployment(
    deployment_id: int, session: Annotated[dict, Depends(get_current_session)]
) -> DeploymentOut:
    """
    Get one deployment history row.

    Args:
        deployment_id: The row's id.
        session: The authenticated session.

    Returns:
        The row.

    Raises:
        HTTPException: 404 when no deployment has this id.
    """
    record = get_store().get_deployment(deployment_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"Deployment not found: {deployment_id}")
    return _to_out(record)


@router.get("/{deployment_id}/log", response_model=DeploymentLogOut)
def get_deployment_log(
    deployment_id: int,
    session: Annotated[dict, Depends(get_current_session)],
    tail: Annotated[
        int | None, Query(ge=1, description="Bytes of the captured log to return, from the end")
    ] = None,
) -> DeploymentLogOut:
    """
    Read a deployment's captured build log.

    Args:
        deployment_id: The row's id.
        session: The authenticated session.
        tail: Bytes to return, counted from the end of the file. Defaults to
            :data:`wasm.deployers.logs.DEFAULT_TAIL_BYTES`.

    Returns:
        The log, or the reason there is nothing to show.

    Raises:
        HTTPException: 404 when no deployment has this id.
    """
    record = get_store().get_deployment(deployment_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"Deployment not found: {deployment_id}")

    result = read_deployment_log(record, tail=tail)
    return DeploymentLogOut(
        content=result.content, truncated=result.truncated, missing_reason=result.missing_reason
    )
