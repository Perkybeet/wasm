"""
Backups API endpoints.

Provides endpoints for managing application backups.
"""

from typing import Any, Dict, List, Optional
from pathlib import Path

from fastapi import APIRouter, Request, HTTPException, Depends, Query
from pydantic import BaseModel, Field

from wasm.web.api.auth import get_current_session
from wasm.managers.backup_manager import BackupManager

router = APIRouter()


class BackupInfo(BaseModel):
    """Backup information."""
    backup_id: str
    domain: str
    timestamp: str
    size: int
    size_human: str
    age: str
    description: str = ""
    app_type: Optional[str] = None
    includes_env: bool = False
    includes_node_modules: bool = False
    includes_build: bool = False
    has_database: bool = False
    database_backups: List[Dict[str, Any]] = Field(default_factory=list)
    git_commit: Optional[str] = None
    git_branch: Optional[str] = None
    tags: List[str] = Field(default_factory=list)


class BackupListResponse(BaseModel):
    """Response for listing backups."""
    backups: List[BackupInfo]
    total: int


class BackupStorageResponse(BaseModel):
    """Response for backup storage info."""
    path: str
    total_size: int
    total_size_human: str
    backup_count: int
    domains: List[str]


class CreateBackupRequest(BaseModel):
    """Request to create a new backup."""
    domain: str = Field(..., description="Domain of the app to backup")
    description: str = Field(default="", description="Description for the backup")
    include_env: bool = Field(default=True, description="Include .env files")
    include_node_modules: bool = Field(default=False, description="Include node_modules (large!)")
    include_build: bool = Field(default=False, description="Include build artifacts")
    include_database: bool = Field(default=False, description="Include database dumps")
    tags: List[str] = Field(default_factory=list, description="Tags for the backup")


class RestoreBackupRequest(BaseModel):
    """Request to restore a backup."""
    target_domain: Optional[str] = Field(default=None, description="Target domain to restore to")


class BackupActionResponse(BaseModel):
    """Response for backup actions."""
    success: bool
    message: str
    backup_id: Optional[str] = None


class VerifyBackupResponse(BaseModel):
    """Response for backup verification."""
    backup_id: str
    valid: bool
    checksum_ok: bool
    files_ok: bool
    message: str


@router.get("", response_model=BackupListResponse)
async def list_backups(
    request: Request,
    domain: Optional[str] = Query(None, description="Filter by domain"),
    limit: int = Query(100, ge=1, le=1000),
    session: dict = Depends(get_current_session)
):
    """
    List all backups, optionally filtered by domain.
    """
    try:
        manager = BackupManager(verbose=False)
        backups_list = manager.list_backups(domain=domain, limit=limit)
        
        backups = []
        for backup in backups_list:
            backups.append(BackupInfo(
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
                tags=backup.tags
            ))
        
        return BackupListResponse(
            backups=backups,
            total=len(backups)
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to list backups: {e}")


@router.get("/storage", response_model=BackupStorageResponse)
async def get_storage_info(
    request: Request,
    session: dict = Depends(get_current_session)
):
    """
    Get backup storage information.
    """
    try:
        manager = BackupManager(verbose=False)
        backup_dir = manager.backup_dir
        
        total_size = 0
        backup_count = 0
        domains = set()
        
        if backup_dir.exists():
            for app_dir in backup_dir.iterdir():
                if app_dir.is_dir():
                    domains.add(app_dir.name)
                    for backup_file in app_dir.glob("*.tar.gz"):
                        total_size += backup_file.stat().st_size
                        backup_count += 1
        
        # Convert size to human readable
        if total_size >= 1073741824:  # 1 GB
            size_human = f"{total_size / 1073741824:.2f} GB"
        elif total_size >= 1048576:  # 1 MB
            size_human = f"{total_size / 1048576:.2f} MB"
        elif total_size >= 1024:  # 1 KB
            size_human = f"{total_size / 1024:.2f} KB"
        else:
            size_human = f"{total_size} B"
        
        return BackupStorageResponse(
            path=str(backup_dir),
            total_size=total_size,
            total_size_human=size_human,
            backup_count=backup_count,
            domains=sorted(list(domains))
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get storage info: {e}")


@router.get("/{backup_id}", response_model=BackupInfo)
async def get_backup(
    backup_id: str,
    request: Request,
    session: dict = Depends(get_current_session)
):
    """
    Get details for a specific backup.
    """
    try:
        manager = BackupManager(verbose=False)
        backup = manager.get_backup(backup_id)
        
        if not backup:
            raise HTTPException(status_code=404, detail=f"Backup not found: {backup_id}")
        
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
            tags=backup.tags
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get backup: {e}")


@router.post("", response_model=BackupActionResponse)
async def create_backup(
    data: CreateBackupRequest,
    request: Request,
    session: dict = Depends(get_current_session)
):
    """
    Create a new backup for an application.
    """
    try:
        manager = BackupManager(verbose=False)
        
        # Check if app exists
        from wasm.core.config import Config
        from wasm.core.utils import domain_to_app_name
        
        config = Config()
        app_name = domain_to_app_name(data.domain)
        app_path = config.apps_directory / app_name
        
        if not app_path.exists():
            raise HTTPException(status_code=404, detail=f"Application not found: {data.domain}")
        
        backup_meta = manager.create(
            domain=data.domain,
            description=data.description,
            include_env=data.include_env,
            include_node_modules=data.include_node_modules,
            include_build=data.include_build,
            include_databases=data.include_database,
            tags=data.tags,
        )
        
        return BackupActionResponse(
            success=True,
            message=f"Backup created: {backup_meta.id}",
            backup_id=backup_meta.id
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to create backup: {e}")


@router.post("/{backup_id}/verify", response_model=VerifyBackupResponse)
async def verify_backup(
    backup_id: str,
    request: Request,
    session: dict = Depends(get_current_session)
):
    """
    Verify a backup's integrity.
    """
    try:
        manager = BackupManager(verbose=False)
        result = manager.verify(backup_id)
        
        return VerifyBackupResponse(
            backup_id=backup_id,
            valid=result.get("valid", False),
            checksum_ok=result.get("checksum_ok", False),
            files_ok=result.get("files_ok", False),
            message=result.get("message", "")
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to verify backup: {e}")


@router.post("/{backup_id}/restore", response_model=BackupActionResponse)
async def restore_backup(
    backup_id: str,
    data: RestoreBackupRequest,
    request: Request,
    session: dict = Depends(get_current_session)
):
    """
    Restore an application from a backup.
    """
    try:
        manager = BackupManager(verbose=False)
        
        # Get backup info
        backup = manager.get_backup(backup_id)
        if not backup:
            raise HTTPException(status_code=404, detail=f"Backup not found: {backup_id}")
        
        # Determine target domain
        target_domain = data.target_domain or backup.domain

        # Perform restore
        success = manager.restore(
            backup_id=backup_id,
            target_domain=target_domain
        )
        
        if success:
            return BackupActionResponse(
                success=True,
                message=f"Backup restored to {target_domain}",
                backup_id=backup_id
            )
        else:
            raise HTTPException(status_code=500, detail="Restore operation failed")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to restore backup: {e}")


@router.delete("/{backup_id}", response_model=BackupActionResponse)
async def delete_backup(
    backup_id: str,
    request: Request,
    session: dict = Depends(get_current_session)
):
    """
    Delete a backup.
    """
    try:
        manager = BackupManager(verbose=False)
        
        # Check if backup exists
        backup = manager.get_backup(backup_id)
        if not backup:
            raise HTTPException(status_code=404, detail=f"Backup not found: {backup_id}")
        
        success = manager.delete(backup_id)
        
        if success:
            return BackupActionResponse(
                success=True,
                message=f"Backup deleted: {backup_id}",
                backup_id=backup_id
            )
        else:
            raise HTTPException(status_code=500, detail="Delete operation failed")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to delete backup: {e}")
