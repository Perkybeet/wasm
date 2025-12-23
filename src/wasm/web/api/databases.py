# Copyright (c) 2024-2025 Yago López Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
Database API endpoints.

Provides REST API endpoints for database management.
"""

from typing import List, Optional

from fastapi import APIRouter, HTTPException, Depends, BackgroundTasks
from pydantic import BaseModel, Field

from wasm.web.api.auth import get_current_session
from wasm.managers.database import (
    DatabaseRegistry,
    get_db_manager,
    BaseDatabaseManager,
)
from wasm.core.exceptions import (
    DatabaseError,
    DatabaseNotFoundError,
    DatabaseExistsError,
    DatabaseUserError,
    DatabaseEngineError,
    DatabaseBackupError,
)

router = APIRouter()


# ==================== Pydantic Models ====================

class EngineInfo(BaseModel):
    """Database engine information."""
    name: str
    display_name: str
    installed: bool
    version: Optional[str] = None
    running: bool = False
    port: int


class EngineListResponse(BaseModel):
    """Response for listing engines."""
    engines: List[EngineInfo]


class EngineStatusResponse(BaseModel):
    """Response for engine status."""
    engine: str
    display_name: str
    installed: bool
    version: Optional[str] = None
    running: bool
    port: int
    service: str


class DatabaseInfoResponse(BaseModel):
    """Database information response."""
    name: str
    engine: str
    size: Optional[str] = None
    tables: int = 0
    owner: Optional[str] = None
    encoding: Optional[str] = None


class DatabaseListResponse(BaseModel):
    """Response for listing databases."""
    databases: List[DatabaseInfoResponse]
    total: int


class CreateDatabaseRequest(BaseModel):
    """Request to create a database."""
    name: str = Field(..., description="Database name")
    engine: str = Field(..., description="Database engine")
    owner: Optional[str] = Field(default=None, description="Database owner")
    encoding: Optional[str] = Field(default=None, description="Character encoding")


class UserInfoResponse(BaseModel):
    """Database user information response."""
    username: str
    engine: str
    host: str = "localhost"
    databases: List[str] = []
    privileges: List[str] = []


class UserListResponse(BaseModel):
    """Response for listing users."""
    users: List[UserInfoResponse]
    total: int


class CreateUserRequest(BaseModel):
    """Request to create a database user."""
    username: str = Field(..., description="Username")
    engine: str = Field(..., description="Database engine")
    password: Optional[str] = Field(default=None, description="Password (generated if not provided)")
    database: Optional[str] = Field(default=None, description="Grant access to this database")
    host: str = Field(default="localhost", description="Host restriction")


class CreateUserResponse(BaseModel):
    """Response after creating a user."""
    username: str
    password: str
    message: str


class GrantPrivilegesRequest(BaseModel):
    """Request to grant privileges."""
    username: str = Field(..., description="Username")
    database: str = Field(..., description="Database name")
    engine: str = Field(..., description="Database engine")
    privileges: Optional[List[str]] = Field(default=None, description="Privileges to grant")
    host: str = Field(default="localhost", description="Host restriction")


class BackupInfoResponse(BaseModel):
    """Backup information response."""
    path: str
    database: str
    engine: str
    size: int
    size_human: str
    created: str
    compressed: bool


class BackupListResponse(BaseModel):
    """Response for listing backups."""
    backups: List[BackupInfoResponse]
    total: int


class CreateBackupRequest(BaseModel):
    """Request to create a backup."""
    database: str = Field(..., description="Database name")
    engine: str = Field(..., description="Database engine")
    compress: bool = Field(default=True, description="Compress the backup")


class RestoreBackupRequest(BaseModel):
    """Request to restore a backup."""
    database: str = Field(..., description="Database name")
    engine: str = Field(..., description="Database engine")
    backup_path: str = Field(..., description="Path to backup file")
    drop_existing: bool = Field(default=False, description="Drop existing database first")


class QueryRequest(BaseModel):
    """Request to execute a query."""
    database: str = Field(..., description="Database name")
    engine: str = Field(..., description="Database engine")
    query: str = Field(..., description="Query to execute")


class QueryResponse(BaseModel):
    """Response for query execution."""
    success: bool
    output: str


class ConnectionStringRequest(BaseModel):
    """Request for connection string."""
    database: str
    username: str
    password: str
    engine: str
    host: str = "localhost"


class ConnectionStringResponse(BaseModel):
    """Response with connection string."""
    connection_string: str


class ActionResponse(BaseModel):
    """Generic action response."""
    success: bool
    message: str


# ==================== Helper Functions ====================

def get_manager(engine: str) -> BaseDatabaseManager:
    """Get database manager or raise HTTP exception."""
    manager = get_db_manager(engine, verbose=False)
    if not manager:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown database engine: {engine}. Available: {', '.join(DatabaseRegistry.list_engines())}"
        )
    return manager


def check_running(manager: BaseDatabaseManager) -> None:
    """Check if engine is running or raise HTTP exception."""
    if not manager.is_installed():
        raise HTTPException(
            status_code=400,
            detail=f"{manager.DISPLAY_NAME} is not installed"
        )
    if not manager.is_running():
        raise HTTPException(
            status_code=400,
            detail=f"{manager.DISPLAY_NAME} is not running"
        )


# ==================== Engine Endpoints ====================

@router.get("/engines", response_model=EngineListResponse)
async def list_engines(session: dict = Depends(get_current_session)):
    """List all available database engines."""
    engines = []
    for engine_name in DatabaseRegistry.list_engines():
        manager = get_db_manager(engine_name, verbose=False)
        if manager:
            engines.append(EngineInfo(
                name=manager.ENGINE_NAME,
                display_name=manager.DISPLAY_NAME,
                installed=manager.is_installed(),
                version=manager.get_version() if manager.is_installed() else None,
                running=manager.is_running() if manager.is_installed() else False,
                port=manager.DEFAULT_PORT,
            ))
    
    return EngineListResponse(engines=engines)


@router.get("/engines/{engine}/status", response_model=EngineStatusResponse)
async def get_engine_status(
    engine: str,
    session: dict = Depends(get_current_session)
):
    """Get status of a specific database engine."""
    manager = get_manager(engine)
    status = manager.get_status()
    
    return EngineStatusResponse(
        engine=status["engine"],
        display_name=status["display_name"],
        installed=status["installed"],
        version=status.get("version"),
        running=status.get("running", False),
        port=status["port"],
        service=status["service"],
    )


@router.post("/engines/{engine}/install", response_model=ActionResponse)
async def install_engine(
    engine: str,
    background_tasks: BackgroundTasks,
    session: dict = Depends(get_current_session)
):
    """Install a database engine."""
    manager = get_manager(engine)
    
    if manager.is_installed():
        return ActionResponse(
            success=True,
            message=f"{manager.DISPLAY_NAME} is already installed"
        )
    
    try:
        manager.install()
        return ActionResponse(
            success=True,
            message=f"{manager.DISPLAY_NAME} installed successfully"
        )
    except DatabaseEngineError as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/engines/{engine}/uninstall", response_model=ActionResponse)
async def uninstall_engine(
    engine: str,
    purge: bool = False,
    session: dict = Depends(get_current_session)
):
    """Uninstall a database engine."""
    manager = get_manager(engine)
    
    if not manager.is_installed():
        return ActionResponse(
            success=True,
            message=f"{manager.DISPLAY_NAME} is not installed"
        )
    
    try:
        manager.uninstall(purge=purge)
        return ActionResponse(
            success=True,
            message=f"{manager.DISPLAY_NAME} uninstalled"
        )
    except DatabaseEngineError as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/engines/{engine}/start", response_model=ActionResponse)
async def start_engine(
    engine: str,
    session: dict = Depends(get_current_session)
):
    """Start a database engine."""
    manager = get_manager(engine)
    
    if not manager.is_installed():
        raise HTTPException(status_code=400, detail=f"{manager.DISPLAY_NAME} is not installed")
    
    if manager.is_running():
        return ActionResponse(
            success=True,
            message=f"{manager.DISPLAY_NAME} is already running"
        )
    
    try:
        manager.start()
        return ActionResponse(
            success=True,
            message=f"{manager.DISPLAY_NAME} started"
        )
    except DatabaseEngineError as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/engines/{engine}/stop", response_model=ActionResponse)
async def stop_engine(
    engine: str,
    session: dict = Depends(get_current_session)
):
    """Stop a database engine."""
    manager = get_manager(engine)
    
    if not manager.is_running():
        return ActionResponse(
            success=True,
            message=f"{manager.DISPLAY_NAME} is not running"
        )
    
    try:
        manager.stop()
        return ActionResponse(
            success=True,
            message=f"{manager.DISPLAY_NAME} stopped"
        )
    except DatabaseEngineError as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/engines/{engine}/restart", response_model=ActionResponse)
async def restart_engine(
    engine: str,
    session: dict = Depends(get_current_session)
):
    """Restart a database engine."""
    manager = get_manager(engine)
    
    if not manager.is_installed():
        raise HTTPException(status_code=400, detail=f"{manager.DISPLAY_NAME} is not installed")
    
    try:
        manager.restart()
        return ActionResponse(
            success=True,
            message=f"{manager.DISPLAY_NAME} restarted"
        )
    except DatabaseEngineError as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/engines/{engine}/logs")
async def get_engine_logs(
    engine: str,
    lines: int = 100,
    session: dict = Depends(get_current_session)
):
    """Get database engine service logs from journalctl."""
    import subprocess
    
    manager = get_manager(engine)
    
    if not manager.is_installed():
        raise HTTPException(status_code=400, detail=f"{manager.DISPLAY_NAME} is not installed")
    
    service_name = manager.SERVICE_NAME
    
    try:
        result = subprocess.run(
            ["journalctl", "-u", service_name, "-n", str(lines), "--no-pager"],
            capture_output=True,
            text=True,
            timeout=30
        )
        logs = result.stdout or result.stderr or "No logs available"
    except subprocess.TimeoutExpired:
        logs = "Timeout retrieving logs"
    except Exception as e:
        logs = f"Error retrieving logs: {e}"
    
    return {
        "engine": engine,
        "service": service_name,
        "logs": logs,
        "lines": lines
    }


# ==================== Database Endpoints ====================

@router.get("/databases", response_model=DatabaseListResponse)
async def list_databases(
    engine: Optional[str] = None,
    session: dict = Depends(get_current_session)
):
    """List all databases."""
    all_databases = []
    
    if engine:
        managers = [get_manager(engine)]
    else:
        managers = DatabaseRegistry.get_installed(verbose=False)
    
    for manager in managers:
        if not manager.is_running():
            continue
        
        try:
            databases = manager.list_databases()
            for db in databases:
                all_databases.append(DatabaseInfoResponse(
                    name=db.name,
                    engine=db.engine,
                    size=db.size,
                    tables=db.tables,
                    owner=db.owner,
                    encoding=db.encoding,
                ))
        except Exception:
            continue
    
    return DatabaseListResponse(
        databases=all_databases,
        total=len(all_databases)
    )


@router.post("/databases", response_model=DatabaseInfoResponse)
async def create_database(
    request: CreateDatabaseRequest,
    session: dict = Depends(get_current_session)
):
    """Create a new database."""
    manager = get_manager(request.engine)
    check_running(manager)
    
    try:
        info = manager.create_database(
            name=request.name,
            owner=request.owner,
            encoding=request.encoding,
        )
        return DatabaseInfoResponse(
            name=info.name,
            engine=info.engine,
            size=info.size,
            tables=info.tables,
            owner=info.owner,
            encoding=info.encoding,
        )
    except DatabaseExistsError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except DatabaseError as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/databases/{engine}/{name}", response_model=DatabaseInfoResponse)
async def get_database_info(
    engine: str,
    name: str,
    session: dict = Depends(get_current_session)
):
    """Get information about a specific database."""
    manager = get_manager(engine)
    check_running(manager)
    
    try:
        info = manager.get_database_info(name)
        return DatabaseInfoResponse(
            name=info.name,
            engine=info.engine,
            size=info.size,
            tables=info.tables,
            owner=info.owner,
            encoding=info.encoding,
        )
    except DatabaseNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except DatabaseError as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/databases/{engine}/{name}", response_model=ActionResponse)
async def drop_database(
    engine: str,
    name: str,
    force: bool = False,
    session: dict = Depends(get_current_session)
):
    """Drop a database."""
    manager = get_manager(engine)
    check_running(manager)
    
    try:
        manager.drop_database(name, force=force)
        return ActionResponse(
            success=True,
            message=f"Database '{name}' dropped"
        )
    except DatabaseNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except DatabaseError as e:
        raise HTTPException(status_code=500, detail=str(e))


# ==================== User Endpoints ====================

@router.get("/users/{engine}", response_model=UserListResponse)
async def list_users(
    engine: str,
    session: dict = Depends(get_current_session)
):
    """List all database users."""
    manager = get_manager(engine)
    check_running(manager)
    
    try:
        users = manager.list_users()
        return UserListResponse(
            users=[
                UserInfoResponse(
                    username=u.username,
                    engine=u.engine,
                    host=u.host,
                    databases=u.databases,
                    privileges=u.privileges,
                )
                for u in users
            ],
            total=len(users)
        )
    except DatabaseError as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/users", response_model=CreateUserResponse)
async def create_user(
    request: CreateUserRequest,
    session: dict = Depends(get_current_session)
):
    """Create a new database user."""
    manager = get_manager(request.engine)
    check_running(manager)
    
    try:
        user_info, password = manager.create_user(
            username=request.username,
            password=request.password,
            host=request.host,
            database=request.database,
        )
        
        # Grant privileges if database specified
        if request.database:
            try:
                manager.grant_privileges(
                    request.username,
                    request.database,
                    host=request.host
                )
            except Exception:
                pass
        
        return CreateUserResponse(
            username=user_info.username,
            password=password,
            message=f"User '{user_info.username}' created successfully"
        )
    except DatabaseUserError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except DatabaseError as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/users/{engine}/{username}", response_model=ActionResponse)
async def delete_user(
    engine: str,
    username: str,
    host: str = "localhost",
    session: dict = Depends(get_current_session)
):
    """Delete a database user."""
    manager = get_manager(engine)
    check_running(manager)
    
    try:
        manager.drop_user(username, host=host)
        return ActionResponse(
            success=True,
            message=f"User '{username}' deleted"
        )
    except DatabaseUserError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except DatabaseError as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/users/grant", response_model=ActionResponse)
async def grant_privileges(
    request: GrantPrivilegesRequest,
    session: dict = Depends(get_current_session)
):
    """Grant privileges to a user."""
    manager = get_manager(request.engine)
    check_running(manager)
    
    try:
        manager.grant_privileges(
            request.username,
            request.database,
            privileges=request.privileges,
            host=request.host,
        )
        return ActionResponse(
            success=True,
            message=f"Privileges granted to '{request.username}' on '{request.database}'"
        )
    except DatabaseUserError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except DatabaseError as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/users/revoke", response_model=ActionResponse)
async def revoke_privileges(
    request: GrantPrivilegesRequest,
    session: dict = Depends(get_current_session)
):
    """Revoke privileges from a user."""
    manager = get_manager(request.engine)
    check_running(manager)
    
    try:
        manager.revoke_privileges(
            request.username,
            request.database,
            privileges=request.privileges,
            host=request.host,
        )
        return ActionResponse(
            success=True,
            message=f"Privileges revoked from '{request.username}' on '{request.database}'"
        )
    except DatabaseUserError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except DatabaseError as e:
        raise HTTPException(status_code=500, detail=str(e))


# ==================== Backup Endpoints ====================

@router.get("/backups", response_model=BackupListResponse)
async def list_backups(
    engine: Optional[str] = None,
    database: Optional[str] = None,
    session: dict = Depends(get_current_session)
):
    """List available backups."""
    all_backups = []
    
    if engine:
        managers = [get_manager(engine)]
    else:
        managers = DatabaseRegistry.get_installed(verbose=False)
    
    for manager in managers:
        try:
            backups = manager.list_backups(database=database)
            for backup in backups:
                backup_dict = backup.to_dict()
                all_backups.append(BackupInfoResponse(
                    path=backup_dict["path"],
                    database=backup_dict["database"],
                    engine=backup_dict["engine"],
                    size=backup_dict["size"],
                    size_human=backup_dict["size_human"],
                    created=backup_dict["created"],
                    compressed=backup_dict["compressed"],
                ))
        except Exception:
            continue
    
    return BackupListResponse(
        backups=all_backups,
        total=len(all_backups)
    )


@router.post("/backups", response_model=BackupInfoResponse)
async def create_backup(
    request: CreateBackupRequest,
    session: dict = Depends(get_current_session)
):
    """Create a database backup."""
    manager = get_manager(request.engine)
    check_running(manager)
    
    try:
        backup = manager.backup(
            database=request.database,
            compress=request.compress,
        )
        backup_dict = backup.to_dict()
        return BackupInfoResponse(
            path=backup_dict["path"],
            database=backup_dict["database"],
            engine=backup_dict["engine"],
            size=backup_dict["size"],
            size_human=backup_dict["size_human"],
            created=backup_dict["created"],
            compressed=backup_dict["compressed"],
        )
    except DatabaseNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except DatabaseBackupError as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/backups/restore", response_model=ActionResponse)
async def restore_backup(
    request: RestoreBackupRequest,
    session: dict = Depends(get_current_session)
):
    """Restore a database from backup."""
    manager = get_manager(request.engine)
    check_running(manager)
    
    from pathlib import Path
    backup_path = Path(request.backup_path)
    
    if not backup_path.exists():
        raise HTTPException(status_code=404, detail=f"Backup file not found: {request.backup_path}")
    
    try:
        manager.restore(
            database=request.database,
            backup_path=backup_path,
            drop_existing=request.drop_existing,
        )
        return ActionResponse(
            success=True,
            message=f"Database '{request.database}' restored from backup"
        )
    except DatabaseBackupError as e:
        raise HTTPException(status_code=500, detail=str(e))


# ==================== Query Endpoint ====================

@router.post("/query", response_model=QueryResponse)
async def execute_query(
    request: QueryRequest,
    session: dict = Depends(get_current_session)
):
    """Execute a database query."""
    manager = get_manager(request.engine)
    check_running(manager)
    
    try:
        success, output = manager.execute_query(
            database=request.database,
            query=request.query,
        )
        return QueryResponse(success=success, output=output)
    except DatabaseNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except DatabaseError as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/connection-string", response_model=ConnectionStringResponse)
async def get_connection_string(
    request: ConnectionStringRequest,
    session: dict = Depends(get_current_session)
):
    """Generate a connection string."""
    manager = get_manager(request.engine)
    
    conn_string = manager.get_connection_string(
        database=request.database,
        username=request.username,
        password=request.password,
        host=request.host,
    )
    
    return ConnectionStringResponse(connection_string=conn_string)
