"""
Jobs API endpoints.

The queue itself lives in :mod:`wasm.web.jobs`; this module only translates
HTTP into calls on it. The job functions it queues are the same ones the
resource endpoints queue, so ``POST /api/jobs/deploy`` and ``POST /api/apps``
run identical code - they used to be two different implementations of a
deployment, one of which shelled out to the ``wasm`` binary.

Literal routes are declared before ``/{job_id}``: registered the other way
round, ``/active`` and ``/cleanup`` would be matched as job identifiers.
"""

from __future__ import annotations

import re
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from wasm.core.exceptions import ValidationError
from wasm.validators.names import validate_filename
from wasm.web.api.auth import get_current_session
from wasm.web.api.deps import WASMErrorRoute, strict_domain
from wasm.web.jobs import (
    Job,
    JobStatus,
    JobType,
    backup_app_job,
    cert_create_job,
    delete_app_job,
    deploy_app_job,
    get_job_manager,
    rollback_app_job,
    update_app_job,
)

router = APIRouter(prefix="/jobs", tags=["jobs"], route_class=WASMErrorRoute)

#: Job identifiers are the first eight characters of a uuid4.
JOB_ID_PATTERN = re.compile(r"^[0-9a-f]{1,36}$")


class DeployRequest(BaseModel):
    """Request to deploy a new application."""

    domain: str = Field(..., description="Domain name for the application")
    source: str = Field(..., description="Git repository URL or local path")
    app_type: str = Field(default="auto", description="Application type")
    port: int | None = Field(default=None, description="Port, assigned when omitted")
    branch: str | None = Field(default=None, description="Git branch to deploy")
    env_vars: dict[str, str] | None = Field(default=None, description="Environment variables")
    webserver: str = Field(default="nginx", description="Web server to configure")
    ssl: bool = Field(default=True, description="Obtain a certificate")


class UpdateRequest(BaseModel):
    """Request to update an application."""

    domain: str = Field(..., description="Domain of the application to update")


class DeleteRequest(BaseModel):
    """Request to delete an application."""

    domain: str = Field(..., description="Domain of the application to delete")
    remove_files: bool = Field(default=True, description="Remove application files")
    remove_ssl: bool = Field(default=True, description="Remove SSL certificates")


class BackupRequest(BaseModel):
    """Request to back up an application."""

    domain: str = Field(..., description="Domain of the application to back up")
    description: str = Field(default="", description="Description for the backup")


class RollbackRequest(BaseModel):
    """Request to roll an application back."""

    domain: str = Field(..., description="Domain of the application")
    backup_id: str | None = Field(default=None, description="Backup to roll back to")


class CertRequest(BaseModel):
    """Request to obtain a certificate."""

    domain: str = Field(..., description="Domain for the certificate")
    email: str | None = Field(default=None, description="Registration email")
    webserver: str = Field(default="nginx", description="Certbot plugin to use")
    include_www: bool = Field(default=False, description="Also cover the www subdomain")


class JobResponse(BaseModel):
    """One job, as the queue records it."""

    id: str
    type: str
    name: str
    description: str
    status: str
    progress: int
    total_steps: int
    current_step: str
    created_at: str
    started_at: str | None = None
    completed_at: str | None = None
    result: dict[str, Any] | None = None
    error: str | None = None
    logs: list[dict[str, Any]] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class JobListResponse(BaseModel):
    """Response for listing jobs."""

    jobs: list[JobResponse]
    total: int
    active: int


class JobCreatedResponse(BaseModel):
    """Response after queueing a job."""

    message: str
    job: JobResponse


class JobActionResponse(BaseModel):
    """Response for an action on a job."""

    message: str
    job_id: str | None = None


class CleanupResponse(BaseModel):
    """Response after dropping old jobs."""

    message: str
    removed: int


def _to_response(job: Job) -> JobResponse:
    """
    Convert a job into its API representation.

    Args:
        job: The job.

    Returns:
        The API model.
    """
    return JobResponse(**job.to_dict())


def _validated_job_id(job_id: str) -> str:
    """
    Check that a job identifier looks like one.

    Args:
        job_id: Identifier as supplied by the client.

    Returns:
        The identifier.

    Raises:
        ValidationError: When it is not a hexadecimal identifier.
    """
    if not JOB_ID_PATTERN.match(job_id):
        raise ValidationError(
            f"Invalid job id: {job_id!r}",
            details="Job ids are hexadecimal, as returned when the job was queued.",
        )
    return job_id


def _queued(message: str, job: Job) -> JobCreatedResponse:
    """
    Build the response for a freshly queued job.

    Args:
        message: Human-readable summary.
        job: The queued job.

    Returns:
        The response body.
    """
    return JobCreatedResponse(message=message, job=_to_response(job))


@router.get("", response_model=JobListResponse)
def list_jobs(
    session: Annotated[dict, Depends(get_current_session)],
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    status: Annotated[str | None, Query(description="Filter by job status")] = None,
) -> JobListResponse:
    """
    List recent jobs.

    Args:
        limit: Maximum number of jobs to return.
        status: Status to filter by.
        session: The authenticated session.

    Returns:
        The jobs, newest first.

    Raises:
        ValidationError: When the status filter is not a known status.
    """
    manager = get_job_manager()
    jobs = manager.get_all_jobs(limit=limit)

    if status:
        try:
            wanted = JobStatus(status)
        except ValueError as exc:
            raise ValidationError(
                f"Unknown job status: {status!r}",
                details=f"Use one of: {', '.join(s.value for s in JobStatus)}.",
            ) from exc
        jobs = [job for job in jobs if job.status == wanted]

    return JobListResponse(
        jobs=[_to_response(job) for job in jobs],
        total=len(jobs),
        active=len(manager.get_active_jobs()),
    )


@router.get("/active", response_model=JobListResponse)
def list_active_jobs(session: Annotated[dict, Depends(get_current_session)]) -> JobListResponse:
    """
    List queued and running jobs.

    Args:
        session: The authenticated session.

    Returns:
        The active jobs.
    """
    jobs = get_job_manager().get_active_jobs()
    return JobListResponse(
        jobs=[_to_response(job) for job in jobs], total=len(jobs), active=len(jobs)
    )


@router.post("/deploy", response_model=JobCreatedResponse, status_code=202)
def create_deploy_job(
    request: DeployRequest, session: Annotated[dict, Depends(get_current_session)]
) -> JobCreatedResponse:
    """
    Queue a deployment.

    Args:
        request: The deployment request.
        session: The authenticated session.

    Returns:
        The queued job.
    """
    domain = strict_domain(request.domain)
    job = get_job_manager().create_job(
        job_type=JobType.DEPLOY,
        name=f"Deploy {domain}",
        description=f"Deploying a {request.app_type} application to {domain}",
        func=deploy_app_job,
        kwargs={
            "domain": domain,
            "source": request.source,
            "app_type": request.app_type,
            "port": request.port,
            "branch": request.branch,
            "env_vars": request.env_vars,
            "webserver": request.webserver,
            "ssl": request.ssl,
        },
        metadata={"domain": domain, "source": request.source, "app_type": request.app_type},
    )
    return _queued("Deployment job created", job)


@router.post("/update", response_model=JobCreatedResponse, status_code=202)
def create_update_job(
    request: UpdateRequest, session: Annotated[dict, Depends(get_current_session)]
) -> JobCreatedResponse:
    """
    Queue an update.

    Args:
        request: The update request.
        session: The authenticated session.

    Returns:
        The queued job.
    """
    domain = strict_domain(request.domain)
    job = get_job_manager().create_job(
        job_type=JobType.UPDATE,
        name=f"Update {domain}",
        description=f"Updating the application at {domain}",
        func=update_app_job,
        kwargs={"domain": domain},
        metadata={"domain": domain},
    )
    return _queued("Update job created", job)


@router.post("/delete", response_model=JobCreatedResponse, status_code=202)
def create_delete_job(
    request: DeleteRequest, session: Annotated[dict, Depends(get_current_session)]
) -> JobCreatedResponse:
    """
    Queue a deletion.

    Args:
        request: The deletion request.
        session: The authenticated session.

    Returns:
        The queued job.
    """
    domain = strict_domain(request.domain)
    job = get_job_manager().create_job(
        job_type=JobType.DELETE,
        name=f"Delete {domain}",
        description=f"Deleting the application at {domain}",
        func=delete_app_job,
        kwargs={
            "domain": domain,
            "remove_files": request.remove_files,
            "remove_ssl": request.remove_ssl,
        },
        metadata={"domain": domain},
    )
    return _queued("Deletion job created", job)


@router.post("/backup", response_model=JobCreatedResponse, status_code=202)
def create_backup_job(
    request: BackupRequest, session: Annotated[dict, Depends(get_current_session)]
) -> JobCreatedResponse:
    """
    Queue a backup.

    Args:
        request: The backup request.
        session: The authenticated session.

    Returns:
        The queued job.
    """
    domain = strict_domain(request.domain)
    job = get_job_manager().create_job(
        job_type=JobType.BACKUP,
        name=f"Backup {domain}",
        description=f"Creating a backup of {domain}",
        func=backup_app_job,
        kwargs={"domain": domain, "description": request.description},
        metadata={"domain": domain},
    )
    return _queued("Backup job created", job)


@router.post("/rollback", response_model=JobCreatedResponse, status_code=202)
def create_rollback_job(
    request: RollbackRequest, session: Annotated[dict, Depends(get_current_session)]
) -> JobCreatedResponse:
    """
    Queue a rollback.

    Args:
        request: The rollback request.
        session: The authenticated session.

    Returns:
        The queued job.
    """
    domain = strict_domain(request.domain)
    backup_id = validate_filename(request.backup_id) if request.backup_id else None

    job = get_job_manager().create_job(
        job_type=JobType.RESTORE,
        name=f"Rollback {domain}",
        description=(f"Rolling back {domain}" + (f" to backup {backup_id}" if backup_id else "")),
        func=rollback_app_job,
        kwargs={"domain": domain, "backup_id": backup_id},
        metadata={"domain": domain, "backup_id": backup_id},
    )
    return _queued("Rollback job created", job)


@router.post("/cert", response_model=JobCreatedResponse, status_code=202)
def create_cert_job(
    request: CertRequest, session: Annotated[dict, Depends(get_current_session)]
) -> JobCreatedResponse:
    """
    Queue a certificate issuance.

    Args:
        request: The certificate request.
        session: The authenticated session.

    Returns:
        The queued job.
    """
    domain = strict_domain(request.domain)
    job = get_job_manager().create_job(
        job_type=JobType.CERT_CREATE,
        name=f"SSL for {domain}",
        description=f"Obtaining an SSL certificate for {domain}",
        func=cert_create_job,
        kwargs={
            "domain": domain,
            "email": request.email,
            "webserver": request.webserver,
            "include_www": request.include_www,
        },
        metadata={"domain": domain},
    )
    return _queued("Certificate job created", job)


@router.delete("/cleanup", response_model=CleanupResponse)
def cleanup_jobs(
    session: Annotated[dict, Depends(get_current_session)],
    max_age_hours: Annotated[int, Query(ge=1, le=168)] = 24,
) -> CleanupResponse:
    """
    Forget finished jobs older than a cutoff.

    Args:
        max_age_hours: Age above which a finished job is dropped.
        session: The authenticated session.

    Returns:
        How many jobs were dropped.
    """
    removed = get_job_manager().cleanup_old_jobs(max_age_hours=max_age_hours)
    return CleanupResponse(
        message=f"Cleaned up jobs older than {max_age_hours} hours", removed=removed
    )


@router.get("/{job_id}", response_model=JobResponse)
def get_job(job_id: str, session: Annotated[dict, Depends(get_current_session)]) -> JobResponse:
    """
    Describe one job.

    Args:
        job_id: Job identifier.
        session: The authenticated session.

    Returns:
        The job.

    Raises:
        HTTPException: 404 when the job is unknown or has been cleaned up.
    """
    job = get_job_manager().get_job(_validated_job_id(job_id))
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job not found: {job_id}")
    return _to_response(job)


@router.post("/{job_id}/cancel", response_model=JobActionResponse)
def cancel_job(
    job_id: str, session: Annotated[dict, Depends(get_current_session)]
) -> JobActionResponse:
    """
    Cancel a job that has not started yet.

    Args:
        job_id: Job identifier.
        session: The authenticated session.

    Returns:
        The action outcome.

    Raises:
        HTTPException: 404 when the job is unknown, 409 when it has already
            started or finished.
    """
    validated = _validated_job_id(job_id)
    manager = get_job_manager()

    if manager.get_job(validated) is None:
        raise HTTPException(status_code=404, detail=f"Job not found: {validated}")

    if not manager.cancel_job(validated):
        raise HTTPException(
            status_code=409,
            detail="Cannot cancel this job: it is already running or finished",
        )

    return JobActionResponse(message="Job cancelled", job_id=validated)
