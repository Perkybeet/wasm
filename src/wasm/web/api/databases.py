# Copyright (c) 2024-2026 Yago Lopez Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
Database API endpoints.

A client of :mod:`wasm.managers.database`: every statement this module causes
to run is built by an engine manager, which is where quoting, privilege
whitelists and the command runner live.

The one endpoint that needs a decision of its own is ``POST /query``. A
database console in a server panel is a legitimate feature - it is why an
operator opens the panel instead of ssh - but the previous version accepted any
string and handed it to the engine as the superuser, with no record of who ran
what. It was neither a console nor a safety net, only an unlogged root shell
into every database on the host. It is kept, and made explicit:

- **Read-only by default.** ``mode`` defaults to ``read`` and only statements
  that begin with a read keyword are accepted. Writing requires
  ``mode="write"`` in the body, so no client writes by accident.
- **One statement at a time.** An embedded ``;`` is refused, which is what
  turns "one SELECT" into "one SELECT and one DROP".
- **Audited.** Every attempt is logged to ``wasm.audit`` with the session, the
  engine, the database and the statement, accepted or not.
- **Bounded output.** The response is truncated to ``max_rows`` lines and says
  so, so a ``SELECT *`` over a large table cannot pull the panel over.

Read mode is only offered for engines whose read grammar WASM actually knows
(PostgreSQL and MySQL). For the others a query is a write by definition, and
the client has to say so.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from wasm.core.exceptions import (
    DatabaseEngineError,
    DatabaseError,
    DatabaseQueryError,
)
from wasm.managers.database import (
    BaseDatabaseManager,
    DatabaseRegistry,
    get_db_manager,
)
from wasm.managers.service_manager import ServiceManager
from wasm.validators.names import resolve_within, validate_filename
from wasm.web.api.auth import get_current_session
from wasm.web.api.deps import JobAcceptedResponse, WASMErrorRoute
from wasm.web.jobs import JobType, database_engine_job, get_job_manager

router = APIRouter(route_class=WASMErrorRoute)

#: Append-only record of privileged actions. Kept separate from the module
#: logger so an operator can route it somewhere durable.
audit_log = logging.getLogger("wasm.audit")

#: Engines whose read-only grammar WASM knows well enough to enforce it.
READ_MODE_ENGINES = frozenset({"postgres", "postgresql", "mysql", "mariadb"})

#: Statements accepted in read mode. Anything else needs mode="write".
READ_STATEMENT_KEYWORDS = frozenset(
    {"select", "show", "explain", "describe", "desc", "with", "table", "values"}
)

#: Longest statement accepted, so the console cannot be used as a file upload.
MAX_QUERY_LENGTH = 20_000


class EngineInfo(BaseModel):
    """A database engine and whether it is usable on this host."""

    name: str
    display_name: str
    installed: bool
    version: str | None = None
    running: bool = False
    port: int


class EngineListResponse(BaseModel):
    """Response for listing engines."""

    engines: list[EngineInfo]


class EngineStatusResponse(BaseModel):
    """Response for the status of one engine."""

    engine: str
    display_name: str
    installed: bool
    version: str | None = None
    running: bool
    port: int
    service: str


class EngineLogsResponse(BaseModel):
    """Response carrying journal output for an engine's service."""

    engine: str
    service: str
    logs: str
    lines: int


class DatabaseInfoResponse(BaseModel):
    """One database."""

    name: str
    engine: str
    size: str | None = None
    tables: int = 0
    owner: str | None = None
    encoding: str | None = None


class DatabaseListResponse(BaseModel):
    """Response for listing databases."""

    databases: list[DatabaseInfoResponse]
    total: int


class CreateDatabaseRequest(BaseModel):
    """Request to create a database."""

    name: str = Field(..., description="Database name")
    engine: str = Field(..., description="Database engine")
    owner: str | None = Field(default=None, description="Database owner")
    encoding: str | None = Field(default=None, description="Character encoding")


class UserInfoResponse(BaseModel):
    """One database user."""

    username: str
    engine: str
    host: str = "localhost"
    databases: list[str] = Field(default_factory=list)
    privileges: list[str] = Field(default_factory=list)


class UserListResponse(BaseModel):
    """Response for listing users."""

    users: list[UserInfoResponse]
    total: int


class CreateUserRequest(BaseModel):
    """Request to create a database user."""

    username: str = Field(..., description="Username")
    engine: str = Field(..., description="Database engine")
    password: str | None = Field(default=None, description="Password, generated when omitted")
    database: str | None = Field(default=None, description="Grant access to this database")
    host: str = Field(default="localhost", description="Host restriction")


class CreateUserResponse(BaseModel):
    """
    Response after creating a user.

    The password is returned exactly once, at creation: WASM stores only what
    the engine stores, which is a hash, so there is nowhere to read it from
    later. It is deliberately absent from every other response.
    """

    username: str
    password: str
    message: str


class GrantPrivilegesRequest(BaseModel):
    """Request to grant or revoke privileges."""

    username: str = Field(..., description="Username")
    database: str = Field(..., description="Database name")
    engine: str = Field(..., description="Database engine")
    privileges: list[str] | None = Field(default=None, description="Privileges to act on")
    host: str = Field(default="localhost", description="Host restriction")


class BackupInfoResponse(BaseModel):
    """One database backup."""

    path: str
    database: str
    engine: str
    size: int
    size_human: str
    created: str
    compressed: bool


class BackupListResponse(BaseModel):
    """Response for listing database backups."""

    backups: list[BackupInfoResponse]
    total: int


class CreateBackupRequest(BaseModel):
    """Request to dump a database."""

    database: str = Field(..., description="Database name")
    engine: str = Field(..., description="Database engine")
    compress: bool = Field(default=True, description="Compress the dump")


class RestoreBackupRequest(BaseModel):
    """
    Request to restore a database.

    Attributes:
        database: Database to restore into.
        engine: Engine that owns it.
        backup_name: File name of the dump, which must be one of the engine's
            own backups. A full path is not accepted: it would let the panel
            read any file on the host as the database superuser.
        drop_existing: Drop the database before restoring.
    """

    database: str = Field(..., description="Database name")
    engine: str = Field(..., description="Database engine")
    backup_name: str = Field(..., description="File name of the dump to restore")
    drop_existing: bool = Field(default=False, description="Drop the database first")


class QueryRequest(BaseModel):
    """
    Request to run a statement.

    Attributes:
        database: Database to run against.
        engine: Engine that owns it.
        query: The statement. One statement only.
        mode: ``read`` refuses anything that is not a read statement; ``write``
            is the explicit opt-in for statements that change data.
        max_rows: Most output lines to return.
    """

    database: str = Field(..., description="Database name")
    engine: str = Field(..., description="Database engine")
    query: str = Field(..., description="Statement to run")
    mode: Literal["read", "write"] = Field(default="read", description="Read-only unless 'write'")
    max_rows: int = Field(default=200, ge=1, le=10_000, description="Output lines to return")


class QueryResponse(BaseModel):
    """
    Result of a statement.

    Attributes:
        success: Whether the engine accepted the statement.
        output: The engine's output, truncated to ``max_rows`` lines.
        mode: The mode the statement ran in.
        truncated: Whether output was cut.
        returned_rows: How many lines the response carries.
    """

    success: bool
    output: str
    mode: str
    truncated: bool = False
    returned_rows: int = 0


class ConnectionStringRequest(BaseModel):
    """Request for a connection string."""

    database: str
    username: str
    password: str
    engine: str
    host: str = "localhost"


class ConnectionStringResponse(BaseModel):
    """
    Response with a connection string.

    The string embeds the password the caller supplied, which is the point of
    the endpoint; nothing here is read from the server.
    """

    connection_string: str


class ActionResponse(BaseModel):
    """Generic action outcome."""

    success: bool
    message: str


def get_manager(engine: str) -> BaseDatabaseManager:
    """
    Resolve an engine name to its manager.

    Args:
        engine: Engine name as supplied by the client.

    Returns:
        The engine manager.

    Raises:
        DatabaseEngineError: When the engine is not one WASM supports. The name
            is checked against the registry, which is the allowlist.
    """
    manager = get_db_manager(engine, verbose=False)
    if manager is None:
        raise DatabaseEngineError(
            f"Unknown database engine: {engine}",
            details=f"Available engines: {', '.join(DatabaseRegistry.list_engines())}.",
        )
    return manager


def check_running(manager: BaseDatabaseManager) -> None:
    """
    Refuse an operation when the engine cannot serve it.

    Args:
        manager: The engine manager.

    Raises:
        DatabaseEngineError: When the engine is not installed or not running.
    """
    if not manager.is_installed():
        raise DatabaseEngineError(
            f"{manager.DISPLAY_NAME} is not installed",
            details=f"Install it with POST /api/databases/engines/{manager.ENGINE_NAME}/install.",
        )
    if not manager.is_running():
        raise DatabaseEngineError(
            f"{manager.DISPLAY_NAME} is not running",
            details=f"Start it with POST /api/databases/engines/{manager.ENGINE_NAME}/start.",
        )


def _database_name(manager: BaseDatabaseManager, name: str) -> str:
    """
    Validate a database name with the engine's own rule.

    The engine's validator is used rather than
    :func:`wasm.validators.names.validate_database_name` because the alphabets
    genuinely differ - a Redis database is a number - and the engine manager is
    the one that has to quote it.

    Args:
        manager: The engine manager.
        name: Candidate database name.

    Returns:
        The validated name.

    Raises:
        DatabaseError: When the engine will not accept the name.
    """
    return manager.validate_database_name(name)


def _user_name(manager: BaseDatabaseManager, username: str) -> str:
    """
    Validate a user name with the engine's own rule.

    Args:
        manager: The engine manager.
        username: Candidate user name.

    Returns:
        The validated name.

    Raises:
        DatabaseError: When the engine will not accept the name.
    """
    return manager.validate_user_name(username)


def _reject_multiple_statements(query: str) -> str:
    """
    Reduce a statement to one statement.

    Args:
        query: The statement as supplied.

    Returns:
        The statement, stripped, without its optional trailing semicolon.

    Raises:
        DatabaseQueryError: When the text is empty, too long, or holds more
            than one statement.
    """
    statement = query.strip()
    if not statement:
        raise DatabaseQueryError(
            "Empty statement",
            details="Send the statement to run in the 'query' field.",
        )
    if len(statement) > MAX_QUERY_LENGTH:
        raise DatabaseQueryError(
            f"Statement is too long: {len(statement)} characters",
            details=f"The console accepts at most {MAX_QUERY_LENGTH} characters.",
        )

    stripped = statement.removesuffix(";").rstrip()
    if ";" in stripped:
        raise DatabaseQueryError(
            "Only one statement may be sent at a time",
            details="Remove the embedded ';' and send the statements one by one.",
        )
    return stripped


def _check_read_only(engine: str, statement: str) -> None:
    """
    Refuse a statement that is not a read in read mode.

    Args:
        engine: Engine name.
        statement: The single statement to run.

    Raises:
        DatabaseQueryError: When the engine has no read grammar WASM enforces,
            or the statement does not begin with a read keyword.
    """
    if engine.lower() not in READ_MODE_ENGINES:
        raise DatabaseQueryError(
            f"Read-only mode is not available for {engine}",
            details=(
                "WASM only enforces a read-only grammar for PostgreSQL and MySQL. "
                "Send mode='write' to run this statement, knowing it may change data."
            ),
        )

    keyword = statement.split(None, 1)[0].lower() if statement.split() else ""
    if keyword not in READ_STATEMENT_KEYWORDS:
        raise DatabaseQueryError(
            f"Statement is not read-only: {keyword or statement[:20]!r}",
            details=(
                "Read mode accepts: "
                f"{', '.join(sorted(READ_STATEMENT_KEYWORDS))}. "
                "Send mode='write' to run a statement that changes data."
            ),
        )


def _truncate(output: str, max_rows: int) -> tuple[str, bool, int]:
    """
    Cut the engine's output to a bounded number of lines.

    Args:
        output: Raw output.
        max_rows: Maximum number of lines to keep.

    Returns:
        Tuple of the kept text, whether anything was dropped, and how many
        lines were kept.
    """
    lines = output.splitlines()
    if len(lines) <= max_rows:
        return output, False, len(lines)
    return "\n".join(lines[:max_rows]), True, max_rows


# ==================== Engines ====================


@router.get("/engines", response_model=EngineListResponse)
def list_engines(session: Annotated[dict, Depends(get_current_session)]) -> EngineListResponse:
    """
    List every engine WASM can manage and its state on this host.

    Args:
        session: The authenticated session.

    Returns:
        The engines.
    """
    engines: list[EngineInfo] = []
    for name in DatabaseRegistry.list_engines():
        manager = get_db_manager(name, verbose=False)
        if manager is None:
            continue
        installed = manager.is_installed()
        engines.append(
            EngineInfo(
                name=manager.ENGINE_NAME,
                display_name=manager.DISPLAY_NAME,
                installed=installed,
                version=manager.get_version() if installed else None,
                running=manager.is_running() if installed else False,
                port=manager.DEFAULT_PORT,
            )
        )
    return EngineListResponse(engines=engines)


@router.get("/engines/{engine}/status", response_model=EngineStatusResponse)
def get_engine_status(
    engine: str, session: Annotated[dict, Depends(get_current_session)]
) -> EngineStatusResponse:
    """
    Report the state of one engine.

    Args:
        engine: Engine name.
        session: The authenticated session.

    Returns:
        The engine status.
    """
    status = get_manager(engine).get_status()
    return EngineStatusResponse(
        engine=status["engine"],
        display_name=status["display_name"],
        installed=status["installed"],
        version=status.get("version"),
        running=status.get("running", False),
        port=status["port"],
        service=status["service"],
    )


@router.get("/engines/{engine}/logs", response_model=EngineLogsResponse)
def get_engine_logs(
    engine: str,
    session: Annotated[dict, Depends(get_current_session)],
    lines: Annotated[int, Query(ge=1, le=1000)] = 100,
) -> EngineLogsResponse:
    """
    Read journal output for an engine's service.

    Args:
        engine: Engine name.
        lines: How many lines to return.
        session: The authenticated session.

    Returns:
        The log output.
    """
    manager = get_manager(engine)
    service = manager.SERVICE_NAME
    logs = ServiceManager(verbose=False).logs(service, lines=lines) or "No logs available"
    return EngineLogsResponse(engine=manager.ENGINE_NAME, service=service, logs=logs, lines=lines)


@router.post("/engines/{engine}/install", response_model=JobAcceptedResponse, status_code=202)
def install_engine(
    engine: str, session: Annotated[dict, Depends(get_current_session)]
) -> JobAcceptedResponse:
    """
    Queue the installation of an engine.

    Installation drives the distribution package manager, so it runs as a job.

    Args:
        engine: Engine name.
        session: The authenticated session.

    Returns:
        The queued job.

    Raises:
        HTTPException: 409 when the engine is already installed.
    """
    manager = get_manager(engine)
    if manager.is_installed():
        raise HTTPException(status_code=409, detail=f"{manager.DISPLAY_NAME} is already installed")

    job = get_job_manager().create_job(
        job_type=JobType.CUSTOM,
        name=f"Install {manager.DISPLAY_NAME}",
        description=f"Installing {manager.DISPLAY_NAME}",
        func=database_engine_job,
        kwargs={"engine": manager.ENGINE_NAME, "action": "install"},
        metadata={"engine": manager.ENGINE_NAME},
    )
    return JobAcceptedResponse(
        job_id=job.id,
        status=job.status.value,
        message=f"Installation queued for {manager.DISPLAY_NAME}",
        job=job.to_dict(),
    )


@router.post("/engines/{engine}/uninstall", response_model=JobAcceptedResponse, status_code=202)
def uninstall_engine(
    engine: str,
    session: Annotated[dict, Depends(get_current_session)],
    purge: Annotated[bool, Query(description="Also remove configuration and data")] = False,
) -> JobAcceptedResponse:
    """
    Queue the removal of an engine.

    Args:
        engine: Engine name.
        purge: Also remove configuration and data.
        session: The authenticated session.

    Returns:
        The queued job.
    """
    manager = get_manager(engine)

    job = get_job_manager().create_job(
        job_type=JobType.CUSTOM,
        name=f"Uninstall {manager.DISPLAY_NAME}",
        description=f"Uninstalling {manager.DISPLAY_NAME}",
        func=database_engine_job,
        kwargs={"engine": manager.ENGINE_NAME, "action": "uninstall", "purge": purge},
        metadata={"engine": manager.ENGINE_NAME, "purge": purge},
    )
    return JobAcceptedResponse(
        job_id=job.id,
        status=job.status.value,
        message=f"Removal queued for {manager.DISPLAY_NAME}",
        job=job.to_dict(),
    )


@router.post("/engines/{engine}/start", response_model=ActionResponse)
def start_engine(
    engine: str, session: Annotated[dict, Depends(get_current_session)]
) -> ActionResponse:
    """
    Start an engine's service.

    Args:
        engine: Engine name.
        session: The authenticated session.

    Returns:
        The action outcome.
    """
    manager = get_manager(engine)
    if not manager.is_installed():
        raise DatabaseEngineError(
            f"{manager.DISPLAY_NAME} is not installed",
            details="Install it before starting it.",
        )
    if manager.is_running():
        return ActionResponse(success=True, message=f"{manager.DISPLAY_NAME} is already running")

    manager.start()
    return ActionResponse(success=True, message=f"{manager.DISPLAY_NAME} started")


@router.post("/engines/{engine}/stop", response_model=ActionResponse)
def stop_engine(
    engine: str, session: Annotated[dict, Depends(get_current_session)]
) -> ActionResponse:
    """
    Stop an engine's service.

    Args:
        engine: Engine name.
        session: The authenticated session.

    Returns:
        The action outcome.
    """
    manager = get_manager(engine)
    if not manager.is_running():
        return ActionResponse(success=True, message=f"{manager.DISPLAY_NAME} is not running")

    manager.stop()
    return ActionResponse(success=True, message=f"{manager.DISPLAY_NAME} stopped")


@router.post("/engines/{engine}/restart", response_model=ActionResponse)
def restart_engine(
    engine: str, session: Annotated[dict, Depends(get_current_session)]
) -> ActionResponse:
    """
    Restart an engine's service.

    Args:
        engine: Engine name.
        session: The authenticated session.

    Returns:
        The action outcome.
    """
    manager = get_manager(engine)
    if not manager.is_installed():
        raise DatabaseEngineError(
            f"{manager.DISPLAY_NAME} is not installed",
            details="Install it before restarting it.",
        )

    manager.restart()
    return ActionResponse(success=True, message=f"{manager.DISPLAY_NAME} restarted")


# ==================== Databases ====================


@router.get("/databases", response_model=DatabaseListResponse)
def list_databases(
    session: Annotated[dict, Depends(get_current_session)],
    engine: Annotated[str | None, Query(description="Restrict to one engine")] = None,
) -> DatabaseListResponse:
    """
    List databases across the running engines.

    Args:
        engine: Engine to restrict the listing to.
        session: The authenticated session.

    Returns:
        Every database the running engines report.
    """
    managers = [get_manager(engine)] if engine else DatabaseRegistry.get_installed(verbose=False)

    databases: list[DatabaseInfoResponse] = []
    for manager in managers:
        if not manager.is_running():
            continue
        try:
            entries = manager.list_databases()
        except DatabaseError:
            # One unhealthy engine must not empty the whole listing.
            logging.getLogger(__name__).warning(
                "Could not list databases for %s", manager.ENGINE_NAME, exc_info=True
            )
            continue
        databases.extend(
            DatabaseInfoResponse(
                name=db.name,
                engine=db.engine,
                size=db.size,
                tables=db.tables,
                owner=db.owner,
                encoding=db.encoding,
            )
            for db in entries
        )

    return DatabaseListResponse(databases=databases, total=len(databases))


@router.post("/databases", response_model=DatabaseInfoResponse)
def create_database(
    request: CreateDatabaseRequest, session: Annotated[dict, Depends(get_current_session)]
) -> DatabaseInfoResponse:
    """
    Create a database.

    Args:
        request: The create request.
        session: The authenticated session.

    Returns:
        The new database.

    Raises:
        DatabaseExistsError: When the database already exists.
    """
    manager = get_manager(request.engine)
    check_running(manager)

    name = _database_name(manager, request.name)
    owner = _user_name(manager, request.owner) if request.owner else None

    info = manager.create_database(name=name, owner=owner, encoding=request.encoding)
    return DatabaseInfoResponse(
        name=info.name,
        engine=info.engine,
        size=info.size,
        tables=info.tables,
        owner=info.owner,
        encoding=info.encoding,
    )


@router.get("/databases/{engine}/{name}", response_model=DatabaseInfoResponse)
def get_database_info(
    engine: str, name: str, session: Annotated[dict, Depends(get_current_session)]
) -> DatabaseInfoResponse:
    """
    Describe one database.

    Args:
        engine: Engine name.
        name: Database name.
        session: The authenticated session.

    Returns:
        The database description.

    Raises:
        DatabaseNotFoundError: When no such database exists.
    """
    manager = get_manager(engine)
    check_running(manager)

    info = manager.get_database_info(_database_name(manager, name))
    return DatabaseInfoResponse(
        name=info.name,
        engine=info.engine,
        size=info.size,
        tables=info.tables,
        owner=info.owner,
        encoding=info.encoding,
    )


@router.delete("/databases/{engine}/{name}", response_model=ActionResponse)
def drop_database(
    engine: str,
    name: str,
    session: Annotated[dict, Depends(get_current_session)],
    force: Annotated[bool, Query(description="Disconnect clients first")] = False,
) -> ActionResponse:
    """
    Drop a database.

    Args:
        engine: Engine name.
        name: Database name.
        force: Disconnect open sessions before dropping.
        session: The authenticated session.

    Returns:
        The action outcome.

    Raises:
        DatabaseNotFoundError: When no such database exists.
    """
    manager = get_manager(engine)
    check_running(manager)

    validated = _database_name(manager, name)
    audit_log.info(
        "drop_database engine=%s database=%s session=%s",
        manager.ENGINE_NAME,
        validated,
        session.get("session_id", "unknown"),
    )
    manager.drop_database(validated, force=force)

    return ActionResponse(success=True, message=f"Database '{validated}' dropped")


# ==================== Users ====================


@router.post("/users", response_model=CreateUserResponse)
def create_user(
    request: CreateUserRequest, session: Annotated[dict, Depends(get_current_session)]
) -> CreateUserResponse:
    """
    Create a database user.

    Args:
        request: The create request.
        session: The authenticated session.

    Returns:
        The user and its password, which is shown exactly once.

    Raises:
        DatabaseUserError: When the engine refuses the user.
    """
    manager = get_manager(request.engine)
    check_running(manager)

    username = _user_name(manager, request.username)
    database = _database_name(manager, request.database) if request.database else None

    user_info, password = manager.create_user(
        username=username,
        password=request.password,
        host=request.host,
        database=database,
    )

    if database:
        manager.grant_privileges(username, database, host=request.host)

    audit_log.info(
        "create_user engine=%s user=%s database=%s session=%s",
        manager.ENGINE_NAME,
        username,
        database or "-",
        session.get("session_id", "unknown"),
    )

    return CreateUserResponse(
        username=user_info.username,
        password=password,
        message=f"User '{user_info.username}' created",
    )


@router.post("/users/grant", response_model=ActionResponse)
def grant_privileges(
    request: GrantPrivilegesRequest, session: Annotated[dict, Depends(get_current_session)]
) -> ActionResponse:
    """
    Grant privileges on a database.

    Declared before ``/users/{engine}`` so the literal path wins.

    Args:
        request: The grant request.
        session: The authenticated session.

    Returns:
        The action outcome.

    Raises:
        DatabaseUserError: When a privilege is not on the engine's whitelist.
    """
    manager = get_manager(request.engine)
    check_running(manager)

    username = _user_name(manager, request.username)
    database = _database_name(manager, request.database)

    manager.grant_privileges(username, database, privileges=request.privileges, host=request.host)
    audit_log.info(
        "grant engine=%s user=%s database=%s privileges=%s session=%s",
        manager.ENGINE_NAME,
        username,
        database,
        request.privileges or "default",
        session.get("session_id", "unknown"),
    )

    return ActionResponse(
        success=True, message=f"Privileges granted to '{username}' on '{database}'"
    )


@router.post("/users/revoke", response_model=ActionResponse)
def revoke_privileges(
    request: GrantPrivilegesRequest, session: Annotated[dict, Depends(get_current_session)]
) -> ActionResponse:
    """
    Revoke privileges on a database.

    Args:
        request: The revoke request.
        session: The authenticated session.

    Returns:
        The action outcome.

    Raises:
        DatabaseUserError: When a privilege is not on the engine's whitelist.
    """
    manager = get_manager(request.engine)
    check_running(manager)

    username = _user_name(manager, request.username)
    database = _database_name(manager, request.database)

    manager.revoke_privileges(username, database, privileges=request.privileges, host=request.host)
    audit_log.info(
        "revoke engine=%s user=%s database=%s privileges=%s session=%s",
        manager.ENGINE_NAME,
        username,
        database,
        request.privileges or "default",
        session.get("session_id", "unknown"),
    )

    return ActionResponse(
        success=True, message=f"Privileges revoked from '{username}' on '{database}'"
    )


@router.get("/users/{engine}", response_model=UserListResponse)
def list_users(
    engine: str, session: Annotated[dict, Depends(get_current_session)]
) -> UserListResponse:
    """
    List the users of an engine.

    Args:
        engine: Engine name.
        session: The authenticated session.

    Returns:
        The users. Passwords are never part of this response.
    """
    manager = get_manager(engine)
    check_running(manager)

    users = manager.list_users()
    return UserListResponse(
        users=[
            UserInfoResponse(
                username=user.username,
                engine=user.engine,
                host=user.host,
                databases=user.databases,
                privileges=user.privileges,
            )
            for user in users
        ],
        total=len(users),
    )


@router.delete("/users/{engine}/{username}", response_model=ActionResponse)
def delete_user(
    engine: str,
    username: str,
    session: Annotated[dict, Depends(get_current_session)],
    host: Annotated[str, Query()] = "localhost",
) -> ActionResponse:
    """
    Delete a database user.

    Args:
        engine: Engine name.
        username: User to delete.
        host: Host restriction the user was created with.
        session: The authenticated session.

    Returns:
        The action outcome.

    Raises:
        DatabaseUserError: When the engine refuses the deletion.
    """
    manager = get_manager(engine)
    check_running(manager)

    validated = _user_name(manager, username)
    audit_log.info(
        "drop_user engine=%s user=%s session=%s",
        manager.ENGINE_NAME,
        validated,
        session.get("session_id", "unknown"),
    )
    manager.drop_user(validated, host=host)

    return ActionResponse(success=True, message=f"User '{validated}' deleted")


# ==================== Backups ====================


@router.get("/backups", response_model=BackupListResponse)
def list_backups(
    session: Annotated[dict, Depends(get_current_session)],
    engine: Annotated[str | None, Query(description="Restrict to one engine")] = None,
    database: Annotated[str | None, Query(description="Restrict to one database")] = None,
) -> BackupListResponse:
    """
    List database dumps.

    Args:
        engine: Engine to restrict the listing to.
        database: Database to restrict the listing to.
        session: The authenticated session.

    Returns:
        The dumps found on disk.
    """
    managers = [get_manager(engine)] if engine else DatabaseRegistry.get_installed(verbose=False)

    backups: list[BackupInfoResponse] = []
    for manager in managers:
        name = _database_name(manager, database) if database else None
        try:
            entries = manager.list_backups(database=name)
        except DatabaseError:
            logging.getLogger(__name__).warning(
                "Could not list backups for %s", manager.ENGINE_NAME, exc_info=True
            )
            continue
        for backup in entries:
            data = backup.to_dict()
            backups.append(
                BackupInfoResponse(
                    path=data["path"],
                    database=data["database"],
                    engine=data["engine"],
                    size=data["size"],
                    size_human=data["size_human"],
                    created=data["created"],
                    compressed=data["compressed"],
                )
            )

    return BackupListResponse(backups=backups, total=len(backups))


@router.post("/backups", response_model=BackupInfoResponse)
def create_backup(
    request: CreateBackupRequest, session: Annotated[dict, Depends(get_current_session)]
) -> BackupInfoResponse:
    """
    Dump a database.

    Args:
        request: The backup request.
        session: The authenticated session.

    Returns:
        The new dump.

    Raises:
        DatabaseBackupError: When the dump fails.
    """
    manager = get_manager(request.engine)
    check_running(manager)

    database = _database_name(manager, request.database)
    data = manager.backup(database=database, compress=request.compress).to_dict()

    return BackupInfoResponse(
        path=data["path"],
        database=data["database"],
        engine=data["engine"],
        size=data["size"],
        size_human=data["size_human"],
        created=data["created"],
        compressed=data["compressed"],
    )


@router.post("/backups/restore", response_model=ActionResponse)
def restore_backup(
    request: RestoreBackupRequest, session: Annotated[dict, Depends(get_current_session)]
) -> ActionResponse:
    """
    Restore a database from one of the engine's own dumps.

    The dump is named, not pathed: the file is resolved inside the engine's
    backup directory, so the endpoint cannot be talked into reading
    ``/etc/shadow`` as the database superuser.

    Args:
        request: The restore request.
        session: The authenticated session.

    Returns:
        The action outcome.

    Raises:
        HTTPException: 404 when the named dump does not exist.
        DatabaseBackupError: When the restore fails.
    """
    manager = get_manager(request.engine)
    check_running(manager)

    database = _database_name(manager, request.database)
    backup_path: Path = resolve_within(manager.BACKUP_DIR, validate_filename(request.backup_name))

    if not backup_path.is_file():
        raise HTTPException(status_code=404, detail=f"Backup not found: {request.backup_name}")

    audit_log.info(
        "restore engine=%s database=%s backup=%s session=%s",
        manager.ENGINE_NAME,
        database,
        backup_path.name,
        session.get("session_id", "unknown"),
    )
    manager.restore(database=database, backup_path=backup_path, drop_existing=request.drop_existing)

    return ActionResponse(success=True, message=f"Database '{database}' restored")


# ==================== Console ====================


@router.post("/query", response_model=QueryResponse)
def execute_query(
    request: QueryRequest, session: Annotated[dict, Depends(get_current_session)]
) -> QueryResponse:
    """
    Run one statement against a database.

    Read-only unless the body says ``mode="write"``. See the module docstring
    for why the endpoint exists at all and what it refuses.

    Args:
        request: The query request.
        session: The authenticated session.

    Returns:
        The engine's output, truncated to ``max_rows`` lines.

    Raises:
        DatabaseQueryError: When the statement is empty, is more than one
            statement, or is not a read in read mode.
    """
    manager = get_manager(request.engine)
    check_running(manager)

    database = _database_name(manager, request.database)
    statement = _reject_multiple_statements(request.query)

    audit_log.info(
        "query engine=%s database=%s mode=%s session=%s statement=%r",
        manager.ENGINE_NAME,
        database,
        request.mode,
        session.get("session_id", "unknown"),
        statement,
    )

    read_only = request.mode == "read"
    if read_only:
        _check_read_only(manager.ENGINE_NAME, statement)

    # The keyword check above is a courtesy that gives a clear error; it is not
    # the guarantee. The guarantee is the server's own read-only transaction,
    # because a leading keyword does not tell you what a statement does:
    # WITH x AS (DELETE FROM t RETURNING *) SELECT * FROM x begins with WITH.
    success, output = manager.execute_query(database=database, query=statement, read_only=read_only)
    text, truncated, rows = _truncate(output, request.max_rows)

    return QueryResponse(
        success=success,
        output=text,
        mode=request.mode,
        truncated=truncated,
        returned_rows=rows,
    )


@router.post("/connection-string", response_model=ConnectionStringResponse)
def get_connection_string(
    request: ConnectionStringRequest, session: Annotated[dict, Depends(get_current_session)]
) -> ConnectionStringResponse:
    """
    Build a connection string from credentials the caller already has.

    Args:
        request: The connection details.
        session: The authenticated session.

    Returns:
        The connection string.
    """
    manager = get_manager(request.engine)

    return ConnectionStringResponse(
        connection_string=manager.get_connection_string(
            database=_database_name(manager, request.database),
            username=_user_name(manager, request.username),
            password=request.password,
            host=request.host,
        )
    )
