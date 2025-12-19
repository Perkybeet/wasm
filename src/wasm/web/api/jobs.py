"""
Jobs API endpoints for WASM Web Interface.

Provides endpoints for managing background jobs.
"""

from typing import Optional, List
from fastapi import APIRouter, HTTPException, Depends, Query
from pydantic import BaseModel, Field

from wasm.web.auth import require_auth
from wasm.web.jobs import (
    get_job_manager,
    JobType,
    JobStatus,
    Job,
    deploy_app_job,
    update_app_job,
    delete_app_job,
    backup_app_job,
    rollback_app_job,
    cert_create_job,
)


router = APIRouter(prefix="/jobs", tags=["jobs"])


# ============ Request/Response Models ============

class DeployRequest(BaseModel):
    """Request to deploy a new application."""
    domain: str = Field(..., description="Domain name for the application")
    source: str = Field(..., description="Git repository URL or local path")
    app_type: str = Field(..., description="Application type (nextjs, vite, python, etc.)")
    port: Optional[int] = Field(None, description="Port number (auto-assigned if not provided)")
    branch: str = Field("main", description="Git branch to deploy")
    env_vars: Optional[dict] = Field(None, description="Environment variables")


class UpdateRequest(BaseModel):
    """Request to update an application."""
    domain: str = Field(..., description="Domain of the application to update")


class DeleteRequest(BaseModel):
    """Request to delete an application."""
    domain: str = Field(..., description="Domain of the application to delete")
    remove_files: bool = Field(True, description="Remove application files")
    remove_ssl: bool = Field(True, description="Remove SSL certificates")


class BackupRequest(BaseModel):
    """Request to create a backup."""
    domain: str = Field(..., description="Domain of the application to backup")


class RollbackRequest(BaseModel):
    """Request to rollback an application."""
    domain: str = Field(..., description="Domain of the application to rollback")
    backup_id: Optional[str] = Field(None, description="Specific backup ID to restore")


class CertRequest(BaseModel):
    """Request to create SSL certificate."""
    domain: str = Field(..., description="Domain for the certificate")
    email: Optional[str] = Field(None, description="Email for certificate notifications")


class JobResponse(BaseModel):
    """Response containing job information."""
    id: str
    type: str
    name: str
    description: str
    status: str
    progress: int
    total_steps: int
    current_step: str
    created_at: str
    started_at: Optional[str]
    completed_at: Optional[str]
    result: Optional[dict]
    error: Optional[str]
    logs: List[dict]
    metadata: dict


class JobListResponse(BaseModel):
    """Response containing list of jobs."""
    jobs: List[JobResponse]
    total: int
    active: int


# ============ Endpoints ============

@router.get("", response_model=JobListResponse)
async def list_jobs(
    limit: int = Query(50, ge=1, le=100),
    status: Optional[str] = Query(None),
    _: dict = Depends(require_auth),
):
    """List all jobs."""
    manager = get_job_manager()
    jobs = manager.get_all_jobs(limit=limit)
    
    # Filter by status if provided
    if status:
        try:
            status_enum = JobStatus(status)
            jobs = [j for j in jobs if j.status == status_enum]
        except ValueError:
            pass
    
    active_jobs = manager.get_active_jobs()
    
    return {
        "jobs": [j.to_dict() for j in jobs],
        "total": len(jobs),
        "active": len(active_jobs),
    }


@router.get("/active", response_model=JobListResponse)
async def list_active_jobs(_: dict = Depends(require_auth)):
    """List only active (pending/running) jobs."""
    manager = get_job_manager()
    jobs = manager.get_active_jobs()
    
    return {
        "jobs": [j.to_dict() for j in jobs],
        "total": len(jobs),
        "active": len(jobs),
    }


@router.get("/{job_id}", response_model=JobResponse)
async def get_job(job_id: str, _: dict = Depends(require_auth)):
    """Get details of a specific job."""
    manager = get_job_manager()
    job = manager.get_job(job_id)
    
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    
    return job.to_dict()


@router.post("/{job_id}/cancel")
async def cancel_job(job_id: str, _: dict = Depends(require_auth)):
    """Cancel a pending job."""
    manager = get_job_manager()
    
    if manager.cancel_job(job_id):
        return {"message": "Job cancelled", "job_id": job_id}
    
    raise HTTPException(
        status_code=400,
        detail="Cannot cancel job (may already be running or completed)"
    )


@router.post("/deploy")
async def create_deploy_job(request: DeployRequest, _: dict = Depends(require_auth)):
    """Create a new deployment job."""
    manager = get_job_manager()
    
    job = manager.create_job(
        job_type=JobType.DEPLOY,
        name=f"Deploy {request.domain}",
        description=f"Deploying {request.app_type} application to {request.domain}",
        func=deploy_app_job,
        kwargs={
            "domain": request.domain,
            "source": request.source,
            "app_type": request.app_type,
            "port": request.port,
            "branch": request.branch,
            "env_vars": request.env_vars,
        },
        metadata={
            "domain": request.domain,
            "source": request.source,
            "app_type": request.app_type,
        },
        total_steps=100,
    )
    
    return {
        "message": "Deployment job created",
        "job": job.to_dict(),
    }


@router.post("/update")
async def create_update_job(request: UpdateRequest, _: dict = Depends(require_auth)):
    """Create an update job."""
    manager = get_job_manager()
    
    job = manager.create_job(
        job_type=JobType.UPDATE,
        name=f"Update {request.domain}",
        description=f"Updating application at {request.domain}",
        func=update_app_job,
        kwargs={"domain": request.domain},
        metadata={"domain": request.domain},
        total_steps=100,
    )
    
    return {
        "message": "Update job created",
        "job": job.to_dict(),
    }


@router.post("/delete")
async def create_delete_job(request: DeleteRequest, _: dict = Depends(require_auth)):
    """Create a deletion job."""
    manager = get_job_manager()
    
    job = manager.create_job(
        job_type=JobType.DELETE,
        name=f"Delete {request.domain}",
        description=f"Deleting application at {request.domain}",
        func=delete_app_job,
        kwargs={
            "domain": request.domain,
            "remove_files": request.remove_files,
            "remove_ssl": request.remove_ssl,
        },
        metadata={"domain": request.domain},
        total_steps=100,
    )
    
    return {
        "message": "Deletion job created",
        "job": job.to_dict(),
    }


@router.post("/backup")
async def create_backup_job(request: BackupRequest, _: dict = Depends(require_auth)):
    """Create a backup job."""
    manager = get_job_manager()
    
    job = manager.create_job(
        job_type=JobType.BACKUP,
        name=f"Backup {request.domain}",
        description=f"Creating backup for {request.domain}",
        func=backup_app_job,
        kwargs={"domain": request.domain},
        metadata={"domain": request.domain},
        total_steps=100,
    )
    
    return {
        "message": "Backup job created",
        "job": job.to_dict(),
    }


@router.post("/rollback")
async def create_rollback_job(request: RollbackRequest, _: dict = Depends(require_auth)):
    """Create a rollback job."""
    manager = get_job_manager()
    
    job = manager.create_job(
        job_type=JobType.RESTORE,
        name=f"Rollback {request.domain}",
        description=f"Rolling back {request.domain}" + (f" to backup {request.backup_id}" if request.backup_id else ""),
        func=rollback_app_job,
        kwargs={
            "domain": request.domain,
            "backup_id": request.backup_id,
        },
        metadata={"domain": request.domain, "backup_id": request.backup_id},
        total_steps=100,
    )
    
    return {
        "message": "Rollback job created",
        "job": job.to_dict(),
    }


@router.post("/cert")
async def create_cert_job(request: CertRequest, _: dict = Depends(require_auth)):
    """Create a certificate creation job."""
    manager = get_job_manager()
    
    job = manager.create_job(
        job_type=JobType.CERT_CREATE,
        name=f"SSL for {request.domain}",
        description=f"Creating SSL certificate for {request.domain}",
        func=cert_create_job,
        kwargs={
            "domain": request.domain,
            "email": request.email,
        },
        metadata={"domain": request.domain},
        total_steps=100,
    )
    
    return {
        "message": "Certificate job created",
        "job": job.to_dict(),
    }


@router.delete("/cleanup")
async def cleanup_jobs(
    max_age_hours: int = Query(24, ge=1, le=168),
    _: dict = Depends(require_auth),
):
    """Clean up old completed jobs."""
    manager = get_job_manager()
    manager.cleanup_old_jobs(max_age_hours=max_age_hours)
    
    return {"message": f"Cleaned up jobs older than {max_age_hours} hours"}
