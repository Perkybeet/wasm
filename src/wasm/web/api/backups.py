"""
Backups API endpoints.

A thin client of :class:`~wasm.managers.backup_manager.BackupManager`. Three
things changed:

- **Creating and restoring a backup are jobs.** Both tar or untar a whole
  application tree and may dump databases; run from an ``async def`` handler
  they blocked the event loop for as long as that took. They now answer
  ``202 Accepted`` with a job id.
- **Storage usage comes from the manager.** The endpoint used to walk the
  backup directory itself and format sizes with its own thresholds, so the
  panel and ``wasm backup`` disagreed about how much disk backups used.
- **Identifiers are validated.** A backup id names a file inside the backup
  directory, so it goes through
  :func:`wasm.validators.names.validate_filename` before it reaches the
  manager.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from wasm.managers.backup_manager import BackupManager, BackupMetadata
from wasm.validators.names import validate_filename
from wasm.web.api.auth import get_current_session
from wasm.web.api.deps import JobAcceptedResponse, WASMErrorRoute, strict_domain
from wasm.web.jobs import (
    JobType,
    backup_app_job,
    get_job_manager,
    restore_backup_job,
)

router = APIRouter(route_class=WASMErrorRoute)

#: Size unit thresholds, largest first.
_SIZE_UNITS: tuple[tuple[int, str], ...] = (
    (1024**3, "GB"),
    (1024**2, "MB"),
    (1024, "KB"),
)


class BackupInfo(BaseModel):
    """One backup as the manager records it."""

    backup_id: str
    domain: str
    timestamp: str
    size: int
    size_human: str
    age: str
    description: str = ""
    app_type: str | None = None
    includes_env: bool = False
    includes_node_modules: bool = False
    includes_build: bool = False
    has_database: bool = False
    database_backups: list[dict[str, Any]] = Field(default_factory=list)
    git_commit: str | None = None
    git_branch: str | None = None
    tags: list[str] = Field(default_factory=list)


class BackupListResponse(BaseModel):
    """Response for listing backups."""

    backups: list[BackupInfo]
    total: int


class BackupStorageResponse(BaseModel):
    """Response describing how much disk the backups take."""

    path: str
    total_size: int
    total_size_human: str
    backup_count: int
    domains: list[str]


class CreateBackupRequest(BaseModel):
    """Request to create a backup."""

    domain: str = Field(..., description="Domain of the app to back up")
    description: str = Field(default="", description="Description for the backup")
    include_env: bool = Field(default=True, description="Include .env files")
    include_node_modules: bool = Field(default=False, description="Include node_modules (large)")
    include_build: bool = Field(default=False, description="Include build artefacts")
    include_database: bool = Field(default=False, description="Include database dumps")
    tags: list[str] = Field(default_factory=list, description="Tags for the backup")


class RestoreBackupRequest(BaseModel):
    """Request to restore a backup."""

    target_domain: str | None = Field(default=None, description="Domain to restore into")


class BackupActionResponse(BaseModel):
    """Response for a backup action that completed immediately."""

    success: bool
    message: str
    backup_id: str | None = None


class VerifyBackupResponse(BaseModel):
    """Response for backup verification."""

    backup_id: str
    valid: bool
    checksum_ok: bool
    files_ok: bool
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


def _human_size(size: int) -> str:
    """
    Render a byte count the way the CLI does.

    Args:
        size: Size in bytes.

    Returns:
        The size with a unit suffix.
    """
    for threshold, unit in _SIZE_UNITS:
        if size >= threshold:
            return f"{size / threshold:.2f} {unit}"
    return f"{size} B"


def _to_backup_info(backup: BackupMetadata) -> BackupInfo:
    """
    Convert backup metadata into the API model.

    Args:
        backup: Metadata as the manager records it.

    Returns:
        The API representation.
    """
    return BackupInfo(
        backup_id=backup.id,
        domain=backup.domain,
        timestamp=backup.created_at,
        size=backup.size_bytes,
        size_human=backup.size_human,
        age=backup.age,
        description=backup.description,
        app_type=backup.app_type,
        includes_env=backup.includes_env,
        includes_node_modules=backup.includes_node_modules,
        includes_build=backup.includes_build,
        has_database=backup.includes_databases,
        database_backups=backup.database_backups,
        git_commit=backup.git_commit,
        git_branch=backup.git_branch,
        tags=backup.tags,
    )


def _load_backup(backup_id: str) -> tuple[BackupManager, BackupMetadata]:
    """
    Validate a backup identifier and load its metadata.

    Args:
        backup_id: Identifier as supplied by the client.

    Returns:
        Tuple of the manager and the backup metadata.

    Raises:
        ValidationError: When the identifier is not a safe file name.
        HTTPException: 404 when no such backup exists.
    """
    validated = validate_filename(backup_id)
    manager = BackupManager(verbose=False)
    backup = manager.get_backup(validated)
    if backup is None:
        raise HTTPException(status_code=404, detail=f"Backup not found: {validated}")
    return manager, backup


@router.get("", response_model=BackupListResponse)
def list_backups(
    session: Annotated[dict, Depends(get_current_session)],
    domain: Annotated[str | None, Query(description="Filter by domain")] = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
) -> BackupListResponse:
    """
    List backups, optionally for one domain.

    Args:
        domain: Domain to filter by.
        limit: Maximum number of backups to return.
        session: The authenticated session.

    Returns:
        The backups, newest first.
    """
    validated = strict_domain(domain) if domain else None
    backups = BackupManager(verbose=False).list_backups(domain=validated, limit=limit)
    return BackupListResponse(
        backups=[_to_backup_info(backup) for backup in backups], total=len(backups)
    )


@router.get("/storage", response_model=BackupStorageResponse)
def get_storage_info(
    session: Annotated[dict, Depends(get_current_session)],
) -> BackupStorageResponse:
    """
    Report how much disk the backups take.

    Declared before ``/{backup_id}`` so the literal path wins.

    Args:
        session: The authenticated session.

    Returns:
        Totals and the applications that own them.
    """
    manager = BackupManager(verbose=False)
    usage = manager.get_storage_usage()
    total_size = int(usage["total_size_bytes"])

    return BackupStorageResponse(
        path=str(manager.backup_dir),
        total_size=total_size,
        total_size_human=_human_size(total_size),
        backup_count=int(usage["total_backups"]),
        domains=sorted(usage["by_app"]),
    )


@router.post("", response_model=JobAcceptedResponse, status_code=202)
def create_backup(
    data: CreateBackupRequest, session: Annotated[dict, Depends(get_current_session)]
) -> JobAcceptedResponse:
    """
    Queue a backup of an application.

    Args:
        data: The backup request.
        session: The authenticated session.

    Returns:
        The queued job.
    """
    domain = strict_domain(data.domain)

    job = get_job_manager().create_job(
        job_type=JobType.BACKUP,
        name=f"Backup {domain}",
        description=f"Creating a backup of {domain}",
        func=backup_app_job,
        kwargs={
            "domain": domain,
            "description": data.description,
            "include_env": data.include_env,
            "include_node_modules": data.include_node_modules,
            "include_build": data.include_build,
            "include_databases": data.include_database,
            "tags": data.tags,
        },
        metadata={"domain": domain},
    )

    return JobAcceptedResponse(
        job_id=job.id,
        status=job.status.value,
        message=f"Backup queued for {domain}",
        job=job.to_dict(),
    )


@router.get("/{backup_id}", response_model=BackupInfo)
def get_backup(
    backup_id: str, session: Annotated[dict, Depends(get_current_session)]
) -> BackupInfo:
    """
    Describe one backup.

    Args:
        backup_id: Backup identifier.
        session: The authenticated session.

    Returns:
        The backup description.
    """
    _, backup = _load_backup(backup_id)
    return _to_backup_info(backup)


@router.post("/{backup_id}/verify", response_model=VerifyBackupResponse)
def verify_backup(
    backup_id: str, session: Annotated[dict, Depends(get_current_session)]
) -> VerifyBackupResponse:
    """
    Check that a backup can still be read and matches its checksum.

    Args:
        backup_id: Backup identifier.
        session: The authenticated session.

    Returns:
        The verification result.
    """
    manager, backup = _load_backup(backup_id)
    result = manager.verify(backup.id)

    return VerifyBackupResponse(
        backup_id=backup.id,
        valid=bool(result.get("valid", False)),
        checksum_ok=bool(result.get("checksum_ok", False)),
        files_ok=bool(result.get("files_ok", False)),
        errors=list(result.get("errors", [])),
        warnings=list(result.get("warnings", [])),
    )


@router.post("/{backup_id}/restore", response_model=JobAcceptedResponse, status_code=202)
def restore_backup(
    backup_id: str,
    session: Annotated[dict, Depends(get_current_session)],
    data: RestoreBackupRequest | None = None,
) -> JobAcceptedResponse:
    """
    Queue a restore of an application from a backup.

    Args:
        backup_id: Backup identifier.
        data: Restore options.
        session: The authenticated session.

    Returns:
        The queued job.
    """
    _, backup = _load_backup(backup_id)

    requested = data.target_domain if data else None
    target_domain = strict_domain(requested) if requested else backup.domain

    job = get_job_manager().create_job(
        job_type=JobType.RESTORE,
        name=f"Restore {backup.id}",
        description=f"Restoring {backup.id} into {target_domain}",
        func=restore_backup_job,
        kwargs={"backup_id": backup.id, "target_domain": target_domain},
        metadata={"domain": target_domain, "backup_id": backup.id},
    )

    return JobAcceptedResponse(
        job_id=job.id,
        status=job.status.value,
        message=f"Restore queued for {target_domain}",
        job=job.to_dict(),
    )


@router.delete("/{backup_id}", response_model=BackupActionResponse)
def delete_backup(
    backup_id: str, session: Annotated[dict, Depends(get_current_session)]
) -> BackupActionResponse:
    """
    Delete a backup and everything that belongs to it.

    Args:
        backup_id: Backup identifier.
        session: The authenticated session.

    Returns:
        The action outcome.

    Raises:
        BackupError: When the manager cannot delete the backup.
    """
    manager, backup = _load_backup(backup_id)
    manager.delete(backup.id)

    return BackupActionResponse(
        success=True, message=f"Backup deleted: {backup.id}", backup_id=backup.id
    )
