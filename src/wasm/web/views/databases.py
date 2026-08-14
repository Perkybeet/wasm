# Copyright (c) 2024-2026 Yago Lopez Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
Database management pages.

Kept as its own module so panel work on databases never has to edit
``views/router.py``: the aggregate router includes this one at the bottom of
that file. Handlers here follow the same contract as the rest of the views:
synchronous, session-guarded, rendering Jinja fragments over the managers.

Every handler is an adapter over :mod:`wasm.web.api.databases`, exactly like
the environment editor and the two-factor handlers are adapters over their API
modules: the validation, the privilege whitelists, the read-only enforcement
and the audit trail all live there, and a second implementation of "drop a
database" is what this panel must never grow. A handler here translates a form
into a request model and a refusal into a fragment, nothing more.

Refusals render at 200 on purpose: htmx does not swap an error status, so a
400 would leave the screen frozen and report the refusal nowhere. The refusal
is on the fragment itself, as a problem block, which is where the operator is
looking.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import ValidationError

from wasm.core.exceptions import DatabaseError, WASMError
from wasm.web.views import resources
from wasm.web.views.rendering import page
from wasm.web.views.router import (
    _RESOURCE_COPY,
    PageErrorRoute,
    _form_fields,
    require_page_session,
)

#: What stands in for the password inside a rendered connection string. WASM
#: stores only what the engine stores, which is a hash, so there is no clear
#: password on this machine to reveal: the mask is not hiding a value, it is
#: the honest statement that the server does not hold one.
PASSWORD_MASK = "***"  # noqa: S105 - the mask shown instead of a credential

# Annotated explicitly: this module and router.py import each other, and inside
# that cycle mypy cannot infer the type of a module-level variable another
# module reads.
router: APIRouter = APIRouter(
    include_in_schema=False,
    dependencies=[Depends(require_page_session)],
    route_class=PageErrorRoute,
)


def _session(request: Request) -> dict[str, Any]:
    """
    Read the session the page dependency attached.

    Args:
        request: The incoming request.

    Returns:
        The session payload, or an empty mapping outside a request cycle.
    """
    return getattr(request.state, "session", None) or {}


def _refusal(exc: WASMError) -> dict[str, str]:
    """
    Turn a manager's refusal into the fix/output pair the problem block shows.

    Args:
        exc: The refusal.

    Returns:
        ``fix`` in plain words, ``output`` verbatim from the tool when the
        error carries any.
    """
    return {
        "fix": str(getattr(exc, "message", "") or exc),
        "output": getattr(exc, "details", "") or "",
    }


def _engine_context(
    request: Request,
    engine: str,
    *,
    notice: str | None = None,
    problem: dict[str, str] | None = None,
    created_user: dict[str, str] | None = None,
    conn: dict[str, str] | None = None,
) -> dict[str, Any]:
    """
    Build the context one engine section renders from.

    Everything is read through the JSON API's own functions, so the section and
    a Bearer client can never disagree about what an engine holds.

    Args:
        request: The incoming request, carrying the session the API calls need.
        engine: Engine name or alias.
        notice: One line reporting what an action just did.
        problem: A ``fix``/``output`` mapping when an action was refused.
        created_user: A freshly created user and its password, shown exactly
            once. No later render can repeat it: the engine stores a hash.
        conn: A just-built connection string, password masked, and the database
            it belongs to.

    Returns:
        The fragment context, under the single key the template reads.
    """
    from wasm.web.api import databases as db_api

    session = _session(request)
    status = db_api.get_engine_status(engine, session)

    databases: list[Any] = []
    users: list[Any] = []
    if status.running:
        try:
            databases = db_api.list_databases(session, engine=status.engine).databases
            users = db_api.list_users(status.engine, session).users
        except DatabaseError as exc:
            # An engine that answers systemd but not its own client still gets
            # its section drawn, with the refusal on it verbatim. Failing the
            # whole page would hide the three healthy engines beside it.
            problem = problem or _refusal(exc)

    backups: list[dict[str, Any]] = []
    if status.installed:
        backups = [
            {
                "file": entry.path.rsplit("/", 1)[-1],
                "database": entry.database,
                "size_human": entry.size_human,
                "created": entry.created,
            }
            for entry in db_api.list_backups(session, engine=status.engine).backups
        ]

    if status.installed:
        state, state_text = ("active", "running") if status.running else ("idle", "stopped")
    else:
        state, state_text = "idle", "not installed"

    return {
        "name": status.engine,
        "display_name": status.display_name,
        "installed": status.installed,
        "running": status.running,
        "version": status.version,
        "port": status.port,
        "service": status.service,
        "state": state,
        "state_text": state_text,
        "databases": databases,
        "users": users,
        "backups": backups,
        "privileges": sorted(db_api.get_manager(status.engine).VALID_PRIVILEGES),
        "notice": notice,
        "problem": problem,
        "created_user": created_user,
        "conn": conn,
    }


def _engine_fragment(request: Request, engine: str, **kwargs: Any) -> HTMLResponse:
    """
    Render one engine section for an htmx swap.

    Args:
        request: The incoming request.
        engine: Engine name or alias.
        **kwargs: Forwarded to :func:`_engine_context`.

    Returns:
        The rendered fragment.
    """
    return page(
        request, "fragments/db_engine.html", {"eng": _engine_context(request, engine, **kwargs)}
    )


def _console_context(
    request: Request,
    *,
    query: str = "",
    selected: str = "",
    allow_writes: bool = False,
    result: Any = None,
    problem: dict[str, str] | None = None,
) -> dict[str, Any]:
    """
    Build the context the SQL console renders from.

    Args:
        request: The incoming request.
        query: The statement to show in the textarea, preserved across a run
            so a refusal does not empty what the operator typed.
        selected: The ``engine:database`` value to keep selected.
        allow_writes: Whether the write opt-in stays checked.
        result: The API's query response, when a statement just ran.
        problem: A ``fix``/``output`` mapping when a statement was refused.

    Returns:
        The fragment context, under the single key the template reads.
    """
    from wasm.web.api import databases as db_api

    session = _session(request)
    targets = [
        {"engine": entry.engine, "database": entry.name, "value": f"{entry.engine}:{entry.name}"}
        for entry in db_api.list_databases(session).databases
    ]
    return {
        "targets": targets,
        "query": query,
        "selected": selected,
        "allow_writes": allow_writes,
        "result": result,
        "problem": problem,
    }


def _console_fragment(request: Request, **kwargs: Any) -> HTMLResponse:
    """
    Render the SQL console for an htmx swap.

    Args:
        request: The incoming request.
        **kwargs: Forwarded to :func:`_console_context`.

    Returns:
        The rendered fragment.
    """
    return page(
        request, "fragments/db_console.html", {"console": _console_context(request, **kwargs)}
    )


@router.get("/databases", response_class=HTMLResponse)
def databases(request: Request) -> HTMLResponse:
    """
    Render the databases screen: engines, their contents, and the console.

    Args:
        request: The incoming request.

    Returns:
        The databases page. The store's own records render alongside the live
        engine listings because they are different facts: the store remembers
        what deploys provisioned, and an engine reports what it holds right
        now, whether or not WASM created it.
    """
    from wasm.web.api import databases as db_api

    engines = [_engine_context(request, name) for name in db_api.DatabaseRegistry.list_engines()]
    installed = sum(1 for engine in engines if engine["installed"])
    return page(
        request,
        "pages/databases.html",
        {
            "engines": engines,
            "subtitle": f"{installed} of {len(engines)} engines installed",
            "console": _console_context(request),
            "rows": resources.resource_rows("databases"),
            "record_copy": _RESOURCE_COPY["databases"],
        },
    )


@router.post("/databases/console", response_class=HTMLResponse)
def console_run(request: Request, body: bytes = Body(default=b"")) -> HTMLResponse:
    """
    Run one statement from the console.

    Read-only unless the operator ticked "Allow writes": the checkbox is the
    explicit ``mode="write"`` the API demands, so nothing writes by accident.
    The enforcement, the single-statement rule and the audit line all live in
    :func:`wasm.web.api.databases.execute_query`.

    Args:
        request: The incoming request.
        body: The urlencoded form: target as ``engine:database``, the
            statement, and the write opt-in.

    Returns:
        The console with the engine's output verbatim, or with the refusal
        inline and the statement preserved.
    """
    from wasm.web.api.databases import QueryRequest, execute_query

    fields = _form_fields(body)
    target = fields.get("target", "")
    engine, _, database = target.partition(":")
    allow_writes = "allow_writes" in fields
    redisplay: dict[str, Any] = {
        "query": fields.get("query", ""),
        "selected": target,
        "allow_writes": allow_writes,
    }

    try:
        model = QueryRequest(
            database=database,
            engine=engine,
            query=fields.get("query", ""),
            mode="write" if allow_writes else "read",
        )
    except ValidationError as exc:
        return _console_fragment(
            request, problem={"fix": "Check the form.", "output": str(exc)}, **redisplay
        )

    try:
        result = execute_query(model, _session(request))
    except WASMError as exc:
        return _console_fragment(request, problem=_refusal(exc), **redisplay)
    return _console_fragment(request, result=result, **redisplay)


# ---------------------------------------------------------------- engines


@router.post("/databases/engines/{engine}/start", response_class=HTMLResponse)
def engine_start(engine: str, request: Request) -> HTMLResponse:
    """
    Start an engine's service and redraw its section.

    Args:
        engine: Engine name.
        request: The incoming request.

    Returns:
        The section in its new state, or with the refusal inline.
    """
    return _engine_action(request, engine, "start")


@router.post("/databases/engines/{engine}/stop", response_class=HTMLResponse)
def engine_stop(engine: str, request: Request) -> HTMLResponse:
    """
    Stop an engine's service and redraw its section.

    Args:
        engine: Engine name.
        request: The incoming request.

    Returns:
        The section in its new state, or with the refusal inline.
    """
    return _engine_action(request, engine, "stop")


@router.post("/databases/engines/{engine}/restart", response_class=HTMLResponse)
def engine_restart(engine: str, request: Request) -> HTMLResponse:
    """
    Restart an engine's service and redraw its section.

    Args:
        engine: Engine name.
        request: The incoming request.

    Returns:
        The section in its new state, or with the refusal inline.
    """
    return _engine_action(request, engine, "restart")


def _engine_action(request: Request, engine: str, action: str) -> HTMLResponse:
    """
    Apply one synchronous service verb through the API.

    Args:
        request: The incoming request.
        engine: Engine name.
        action: ``start``, ``stop`` or ``restart``.

    Returns:
        The engine section in its new state, or with systemd's own words when
        the unit refused.
    """
    from wasm.web.api import databases as db_api

    verbs = {
        "start": db_api.start_engine,
        "stop": db_api.stop_engine,
        "restart": db_api.restart_engine,
    }
    try:
        outcome = verbs[action](engine, _session(request))
    except WASMError as exc:
        return _engine_fragment(request, engine, problem=_refusal(exc))
    return _engine_fragment(request, engine, notice=outcome.message)


@router.post("/databases/engines/{engine}/install", response_class=HTMLResponse)
def engine_install(engine: str, request: Request) -> HTMLResponse:
    """
    Queue an engine installation and say it runs in the background.

    Installation drives the distribution package manager, so the API answers
    202 with a job; the feed and the activity screen report it from there.

    Args:
        engine: Engine name.
        request: The incoming request.

    Returns:
        The section with the queued notice, or with the refusal inline.
    """
    from wasm.web.api.databases import install_engine

    try:
        accepted = install_engine(engine, _session(request))
    except HTTPException as exc:
        return _engine_fragment(request, engine, problem={"fix": str(exc.detail), "output": ""})
    return _engine_fragment(
        request,
        engine,
        notice=(
            f"{accepted.message}. It is running in the background; "
            "progress appears in the activity feed."
        ),
    )


@router.post("/databases/engines/{engine}/uninstall", response_class=HTMLResponse)
def engine_uninstall(engine: str, request: Request) -> HTMLResponse:
    """
    Queue an engine removal and say it runs in the background.

    The packages go; the data directories stay. Purging data is deliberately
    not offered here: destroying every database an engine holds deserves the
    ceremony of the CLI's own double confirmation, not a button.

    Args:
        engine: Engine name.
        request: The incoming request.

    Returns:
        The section with the queued notice.
    """
    from wasm.web.api.databases import uninstall_engine

    accepted = uninstall_engine(engine, _session(request))
    return _engine_fragment(
        request,
        engine,
        notice=(
            f"{accepted.message}. It is running in the background; "
            "progress appears in the activity feed."
        ),
    )


# --------------------------------------------------------------- databases


@router.post("/databases/{engine}/create", response_class=HTMLResponse)
def database_create(engine: str, request: Request, body: bytes = Body(default=b"")) -> HTMLResponse:
    """
    Create a database from the inline form.

    Args:
        engine: Engine name.
        request: The incoming request.
        body: The urlencoded form: name, and an optional owner.

    Returns:
        The section with the new database listed, or with the refusal inline.
    """
    from wasm.web.api.databases import CreateDatabaseRequest, create_database

    fields = _form_fields(body)
    try:
        created = create_database(
            CreateDatabaseRequest(
                name=fields.get("name", ""),
                engine=engine,
                owner=fields.get("owner") or None,
            ),
            _session(request),
        )
    except WASMError as exc:
        return _engine_fragment(request, engine, problem=_refusal(exc))
    return _engine_fragment(request, engine, notice=f"Database '{created.name}' created")


@router.post("/databases/{engine}/restore", response_class=HTMLResponse)
def database_restore(
    engine: str, request: Request, body: bytes = Body(default=b"")
) -> HTMLResponse:
    """
    Restore a database from one of the engine's own dumps.

    The form demands the database's exact name typed back before anything
    happens, because a restore replaces what the database holds now. The
    comparison is made here, server-side: a required attribute on an input is
    a courtesy, not a guard.

    Args:
        engine: Engine name.
        request: The incoming request.
        body: The urlencoded form: the dump's file name, the database it
            belongs to, the typed confirmation, and the drop opt-in.

    Returns:
        The section with the outcome, or with the mismatch refused inline and
        nothing restored.
    """
    from wasm.web.api.databases import RestoreBackupRequest, restore_backup

    fields = _form_fields(body)
    database = fields.get("database", "")
    typed = fields.get("confirm", "")
    if not database or typed != database:
        return _engine_fragment(
            request,
            engine,
            problem={
                "fix": (
                    f"Nothing was restored. Restoring replaces what '{database}' holds now, "
                    "so it only proceeds when the database's exact name is typed; "
                    f"'{typed}' does not match."
                ),
                "output": "",
            },
        )

    try:
        restore_backup(
            RestoreBackupRequest(
                database=database,
                engine=engine,
                backup_name=fields.get("backup_name", ""),
                drop_existing="drop_existing" in fields,
            ),
            _session(request),
        )
    except HTTPException as exc:
        return _engine_fragment(request, engine, problem={"fix": str(exc.detail), "output": ""})
    except WASMError as exc:
        return _engine_fragment(request, engine, problem=_refusal(exc))
    return _engine_fragment(
        request,
        engine,
        notice=f"Database '{database}' restored from {fields.get('backup_name', '')}",
    )


# ------------------------------------------------------------------- users


@router.post("/databases/{engine}/users/create", response_class=HTMLResponse)
def user_create(engine: str, request: Request, body: bytes = Body(default=b"")) -> HTMLResponse:
    """
    Create a database user and show its password exactly once.

    The engine stores a hash, so no later screen can repeat the password; the
    fragment says so beside the value. An empty password field asks the
    manager to generate a strong one.

    Args:
        engine: Engine name.
        request: The incoming request.
        body: The urlencoded form: username, optional password, optional
            database to grant on.

    Returns:
        The section with the one-time password, or with the refusal inline.
    """
    from wasm.web.api.databases import CreateUserRequest, create_user

    fields = _form_fields(body)
    try:
        created = create_user(
            CreateUserRequest(
                username=fields.get("username", ""),
                engine=engine,
                password=fields.get("password") or None,
                database=fields.get("database") or None,
            ),
            _session(request),
        )
    except WASMError as exc:
        return _engine_fragment(request, engine, problem=_refusal(exc))
    return _engine_fragment(
        request,
        engine,
        created_user={"username": created.username, "password": created.password},
    )


@router.post("/databases/{engine}/users/{username}/delete", response_class=HTMLResponse)
def user_delete(engine: str, username: str, request: Request) -> HTMLResponse:
    """
    Delete a database user.

    Args:
        engine: Engine name.
        username: User to delete.
        request: The incoming request.

    Returns:
        The section without the user, or with the refusal inline.
    """
    from wasm.web.api.databases import delete_user

    try:
        outcome = delete_user(engine, username, _session(request))
    except WASMError as exc:
        return _engine_fragment(request, engine, problem=_refusal(exc))
    return _engine_fragment(request, engine, notice=outcome.message)


@router.post("/databases/{engine}/users/{username}/grant", response_class=HTMLResponse)
def user_grant(
    engine: str, username: str, request: Request, body: bytes = Body(default=b"")
) -> HTMLResponse:
    """
    Grant a privilege on a database to a user.

    The privilege names come from the engine manager's own whitelist, which is
    also what the API enforces: the select and the guard cannot drift apart
    because they read the same frozen set.

    Args:
        engine: Engine name.
        username: User to grant to.
        request: The incoming request.
        body: The urlencoded form: database, and a privilege or empty for the
            engine's default set.

    Returns:
        The section in its new state, or with the refusal inline.
    """
    return _privilege_action(request, engine, username, body, revoke=False)


@router.post("/databases/{engine}/users/{username}/revoke", response_class=HTMLResponse)
def user_revoke(
    engine: str, username: str, request: Request, body: bytes = Body(default=b"")
) -> HTMLResponse:
    """
    Revoke a privilege on a database from a user.

    Args:
        engine: Engine name.
        username: User to revoke from.
        request: The incoming request.
        body: The urlencoded form: database, and a privilege or empty for the
            engine's default set.

    Returns:
        The section in its new state, or with the refusal inline.
    """
    return _privilege_action(request, engine, username, body, revoke=True)


def _privilege_action(
    request: Request, engine: str, username: str, body: bytes, *, revoke: bool
) -> HTMLResponse:
    """
    Apply a grant or a revoke through the API.

    Args:
        request: The incoming request.
        engine: Engine name.
        username: User the privileges belong to.
        body: The urlencoded form carrying database and privilege.
        revoke: Whether to revoke rather than grant.

    Returns:
        The engine section in its new state, or with the refusal inline.
    """
    from wasm.web.api.databases import GrantPrivilegesRequest, grant_privileges, revoke_privileges

    fields = _form_fields(body)
    privilege = fields.get("privilege", "")
    model = GrantPrivilegesRequest(
        username=username,
        database=fields.get("database", ""),
        engine=engine,
        privileges=[privilege] if privilege else None,
    )
    act = revoke_privileges if revoke else grant_privileges
    try:
        outcome = act(model, _session(request))
    except WASMError as exc:
        return _engine_fragment(request, engine, problem=_refusal(exc))
    return _engine_fragment(request, engine, notice=outcome.message)


# ------------------------------------------------- per-database row actions


@router.post("/databases/{engine}/{name}/drop", response_class=HTMLResponse)
def database_drop(
    engine: str, name: str, request: Request, body: bytes = Body(default=b"")
) -> HTMLResponse:
    """
    Drop a database, on presentation of its exact name typed back.

    The comparison happens here, server-side. A mismatch answers 200 with the
    refusal inline and drops nothing: htmx does not swap an error status, and
    a frozen screen that reports nothing is how an operator clicks again.

    Args:
        engine: Engine name.
        name: Database to drop.
        request: The incoming request.
        body: The urlencoded form carrying the typed confirmation.

    Returns:
        The section without the database, or with the mismatch refused inline.
    """
    from wasm.web.api.databases import drop_database

    typed = _form_fields(body).get("confirm", "")
    if typed != name:
        return _engine_fragment(
            request,
            engine,
            problem={
                "fix": (
                    f"Nothing was dropped. Dropping '{name}' removes every table in it, "
                    "so it only proceeds when the exact name is typed; "
                    f"'{typed}' does not match."
                ),
                "output": "",
            },
        )

    try:
        outcome = drop_database(engine, name, _session(request))
    except WASMError as exc:
        return _engine_fragment(request, engine, problem=_refusal(exc))
    return _engine_fragment(request, engine, notice=outcome.message)


@router.post("/databases/{engine}/{name}/backup", response_class=HTMLResponse)
def database_backup(engine: str, name: str, request: Request) -> HTMLResponse:
    """
    Dump one database to the engine's backup directory.

    Args:
        engine: Engine name.
        name: Database to dump.
        request: The incoming request.

    Returns:
        The section with the dump listed and named, or with the refusal
        inline.
    """
    from wasm.web.api.databases import CreateBackupRequest, create_backup

    try:
        written = create_backup(
            CreateBackupRequest(database=name, engine=engine), _session(request)
        )
    except WASMError as exc:
        return _engine_fragment(request, engine, problem=_refusal(exc))
    return _engine_fragment(
        request, engine, notice=f"Backup written to {written.path} ({written.size_human})"
    )


@router.get("/databases/{engine}/{name}/connection-string", response_class=HTMLResponse)
def database_connection_string(
    engine: str, name: str, request: Request, username: str = ""
) -> HTMLResponse:
    """
    Show a connection string for a database, with the password masked.

    The mask is not coyness: WASM stores only the hash the engine stores, so
    there is no clear password on this machine to print. The operator
    substitutes the one issued when the user was created, which is the only
    time it ever existed in clear.

    Args:
        engine: Engine name.
        name: Database the string points at.
        request: The incoming request.
        username: User the application connects as.

    Returns:
        The section with the masked string, or with the refusal inline.
    """
    from wasm.web.api.databases import ConnectionStringRequest, get_connection_string

    user = username.strip()
    if not user:
        return _engine_fragment(
            request,
            engine,
            problem={
                "fix": "Name the user the application connects as; the string embeds it.",
                "output": "",
            },
        )

    try:
        answer = get_connection_string(
            ConnectionStringRequest(
                database=name, username=user, password=PASSWORD_MASK, engine=engine
            ),
            _session(request),
        )
    except WASMError as exc:
        return _engine_fragment(request, engine, problem=_refusal(exc))
    return _engine_fragment(
        request, engine, conn={"database": name, "value": answer.connection_string}
    )
