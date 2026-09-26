# Copyright (c) 2024-2026 Yago Lopez Prado
# SPDX-License-Identifier: AGPL-3.0-or-later

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

Two actions on one deployment live here too, mounted under ``/api/apps`` by
:data:`app_router`: rebuilding its exact commit (the update job, with the
commit) and going back to what it produced
(:func:`~wasm.deployers.lifecycle.rollback_to_deployment`). Both need the
``deploy`` scope, like an update.
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
from wasm.deployers.lifecycle import rollback_availability
from wasm.deployers.logs import read_deployment_log
from wasm.web.api.auth import get_current_session
from wasm.web.api.deps import JobAcceptedResponse, WASMErrorRoute, strict_domain
from wasm.web.auth import actor_label
from wasm.web.jobs import JobType, get_job_manager, rollback_deployment_job, update_app_job
from wasm.web.pydantic_compat import iso_offset_validator

router = APIRouter(route_class=WASMErrorRoute)

#: Actions on one deployment, mounted at ``/api/apps`` beside the apps
#: router: ``/{domain}/deployments/{id}/...`` is a path apps.py never defines.
app_router = APIRouter(route_class=WASMErrorRoute)

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
    """
    One deployment attempt, as the console lists and inspects it.

    Attributes:
        job_id: The background job that started this deployment, when the
            panel queued it. None for a CLI or webhook deploy.
        release_id: The release this deployment built, on the releases
            layout. None for an in-place deployment.
        commit_message: Subject line of the deployed commit, for a git
            source. None for a source that is not git.
        snapshot_backup: In place, the backup that holds exactly what this
            deployment produced, taken by the update that followed it.
        rollback_available: Whether going back to this deployment is
            possible now: its release is on disk and not live, or its
            snapshot backup still exists.
        rollback_unavailable_reason: Why not, when it is not.
    """

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
    job_id: str | None = None
    release_id: str | None = None
    commit_message: str | None = None
    snapshot_backup: str | None = None
    rollback_available: bool = False
    rollback_unavailable_reason: str | None = None

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


def _to_out(record: DeploymentRecord, availability: dict[int, str | None]) -> DeploymentOut:
    """
    Args:
        record: A row read from the store.
        availability: :func:`~wasm.deployers.lifecycle.rollback_availability`
            of the rows being answered.

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
        job_id=record.job_id,
        release_id=record.release_id,
        commit_message=record.commit_message,
        snapshot_backup=record.snapshot_backup,
        rollback_available=record.id in availability and availability[record.id] is None,
        rollback_unavailable_reason=availability.get(record.id),
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

    availability = rollback_availability(page)
    return DeploymentListResponse(
        items=[_to_out(record, availability) for record in page],
        total=total,
        next_before_id=next_before_id,
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
    return _to_out(record, rollback_availability([record]))


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


def _deployment_of(domain: str, deployment_id: int) -> DeploymentRecord:
    """
    Read a deployment that must belong to an application.

    Args:
        domain: The application's domain, validated.
        deployment_id: The deployment.

    Returns:
        The row.

    Raises:
        HTTPException: 404 when there is no such deployment of that application.
    """
    record = get_store().get_deployment(deployment_id)
    if record is None or record.domain != domain:
        raise HTTPException(
            status_code=404, detail=f"Deployment {deployment_id} of {domain} not found"
        )
    return record


@app_router.post(
    "/{domain}/deployments/{deployment_id}/rebuild",
    response_model=JobAcceptedResponse,
    status_code=202,
)
def rebuild_deployment(
    domain: str, deployment_id: int, session: Annotated[dict, Depends(get_current_session)]
) -> JobAcceptedResponse:
    """
    Queue a deploy of the exact commit a deployment was built from.

    The update job, given the commit: on releases a release of that commit
    still on disk is activated (instant), otherwise the commit is built as a
    new release; in place the checkout is put on the commit and rebuilt.
    There is no ``nothing_new`` check: asking for a commit is explicit.

    Args:
        domain: Domain of the application.
        deployment_id: The deployment whose commit to rebuild.
        session: The authenticated session.

    Returns:
        The queued job.

    Raises:
        HTTPException: 404 for an unknown application or deployment, 409 when
            the deployment recorded no commit (its source is not git).
    """
    domain = strict_domain(domain)
    record = _deployment_of(domain, deployment_id)
    if get_store().get_app(domain) is None:
        raise HTTPException(status_code=404, detail=f"Application not found: {domain}")
    commit = record.git_commit
    if not commit:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "no_commit",
                "detail": f"Deployment {deployment_id} recorded no commit to rebuild",
                "hint": "Its source is not a git repository. Update the application to "
                "build its source again.",
            },
        )
    job = get_job_manager().create_job(
        job_type=JobType.UPDATE,
        name=f"Rebuild {domain} at {commit}",
        description=f"Deploying commit {commit} of {domain} again",
        func=update_app_job,
        kwargs={"domain": domain, "commit": commit},
        metadata={"domain": domain, "commit": commit, "deployment_id": deployment_id},
        actor=actor_label(session),
    )
    return JobAcceptedResponse(
        job_id=job.id,
        status=job.status.value,
        message=f"Rebuild of {domain} at {commit} queued",
        job=job.to_dict(),
    )


@app_router.post(
    "/{domain}/deployments/{deployment_id}/rollback",
    response_model=JobAcceptedResponse,
    status_code=202,
)
def rollback_deployment(
    domain: str, deployment_id: int, session: Annotated[dict, Depends(get_current_session)]
) -> JobAcceptedResponse:
    """
    Queue going back to what a deployment produced.

    On releases its release is activated behind the health gate. In place in
    a git checkout its commit is rebuilt where the application runs, behind
    the same gate; in place without history its snapshot backup (taken by
    the update that followed it) is restored after a safety backup, rebuilt
    and gated, keeping the deployed ``.env``. See
    :func:`~wasm.deployers.lifecycle.rollback_to_deployment`.

    Args:
        domain: Domain of the application.
        deployment_id: The deployment to go back to.
        session: The authenticated session.

    Returns:
        The queued job.

    Raises:
        HTTPException: 404 for an unknown deployment of the application, 409
            ``rollback_unavailable`` with the reason when it cannot be gone
            back to.
    """
    domain = strict_domain(domain)
    record = _deployment_of(domain, deployment_id)
    reason = rollback_availability([record]).get(deployment_id)
    if reason is not None:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "rollback_unavailable",
                "detail": reason,
                "hint": "Rebuild its commit instead"
                if record.git_commit
                else "Restore a backup from the Backups tab instead",
            },
        )
    job = get_job_manager().create_job(
        job_type=JobType.RESTORE,
        name=f"Rollback {domain}",
        description=f"Going back to deployment {deployment_id} of {domain}",
        func=rollback_deployment_job,
        kwargs={"domain": domain, "deployment_id": deployment_id},
        metadata={"domain": domain, "deployment_id": deployment_id},
        actor=actor_label(session),
    )
    return JobAcceptedResponse(
        job_id=job.id,
        status=job.status.value,
        message=f"Rollback of {domain} to deployment {deployment_id} queued",
        job=job.to_dict(),
    )
