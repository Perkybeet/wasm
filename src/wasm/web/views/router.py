# Copyright (c) 2024-2026 Yago Lopez Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
Page routes for the control panel.

These handlers are synchronous on purpose. They read the store and call the
managers, both of which do blocking work, and FastAPI runs a synchronous
handler in a threadpool. Declaring them ``async`` is what let a single deploy
freeze the event loop and with it every other request, every WebSocket and the
heartbeat that tells the operator the panel is alive.

The routes read through the same managers the CLI uses. There is one
implementation of the product; this is a view of it.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from wasm.web.auth import require_auth
from wasm.web.views.rendering import page

log = logging.getLogger(__name__)


async def require_page_session(request: Request) -> dict[str, Any]:
    """
    Require a session, sending a browser to the sign-in page when there is none.

    Pages get a redirect where the API gets a 401: a person who typed a URL
    should land on a form, not on a JSON error. The check itself is the same
    one the API uses, so there is one place where a credential is verified and
    one counter recording failures.

    Args:
        request: The incoming request.

    Returns:
        The authenticated session payload.

    Raises:
        HTTPException: A 303 redirect to the sign-in page when unauthenticated.
    """
    try:
        session = await require_auth(request)
    except HTTPException as exc:
        if exc.status_code in (401, 403):
            raise HTTPException(
                status_code=303,
                headers={"Location": "/login"},
                detail="Sign in to reach the panel",
            ) from exc
        raise
    request.state.session = session
    return session


router = APIRouter(include_in_schema=False, dependencies=[Depends(require_page_session)])


def _rows_from_store(kind: str) -> list[dict[str, Any]]:
    """
    Read a resource list from the store, shaped for the row component.

    Args:
        kind: Which resource to read: apps, sites, services or databases.

    Returns:
        Rows ready for the template. Empty when the store cannot be read.
    """
    from wasm.core.store import get_store

    store = get_store()
    readers = {
        "apps": store.list_apps,
        "sites": store.list_sites,
        "services": store.list_services,
        "databases": store.list_databases,
    }
    try:
        records = readers[kind]()
    except Exception as exc:  # noqa: BLE001 - a page must render to report this
        log.warning("Could not read %s from the store: %s", kind, exc)
        return []

    return [_shape(kind, record) for record in records]


def _shape(kind: str, record: Any) -> dict[str, Any]:
    """
    Turn a store record into the fields the row component needs.

    Args:
        kind: Which resource this is.
        record: The store dataclass.

    Returns:
        A row context.
    """
    if kind == "apps":
        return {
            "id": record.domain,
            "domain": record.domain,
            "state": record.status,
            "meta": [
                ("type", record.app_type),
                ("port", record.port or "—"),
                ("ssl", "yes" if record.ssl_enabled else "no"),
            ],
        }
    if kind == "sites":
        return {
            "id": record.domain,
            "domain": record.domain,
            "state": "active" if record.enabled else "idle",
            "meta": [
                ("server", record.webserver),
                ("ssl", "yes" if record.ssl_enabled else "no"),
            ],
        }
    if kind == "services":
        return {
            "id": record.name,
            "domain": record.name,
            "state": record.status,
            "meta": [("port", record.port or "—"), ("user", record.user)],
        }
    return {
        "id": getattr(record, "name", "?"),
        "domain": getattr(record, "name", "?"),
        "state": "idle",
        "meta": [("engine", getattr(record, "engine", "?"))],
    }


def _needs_attention(apps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Pick out what an operator should look at first.

    Args:
        apps: Shaped application rows.

    Returns:
        The subset in a failed state, with a link to open each.
    """
    return [
        {**app, "name": app["domain"], "href": f"/apps/{app['domain']}"}
        for app in apps
        if app["state"] in {"failed", "error"}
    ]


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request) -> HTMLResponse:
    """
    Render the overview.

    Args:
        request: The incoming request.

    Returns:
        The overview page.
    """
    apps = _rows_from_store("apps")
    return page(
        request,
        "pages/dashboard.html",
        {
            "apps": apps,
            "attention": _needs_attention(apps),
            "activity": [],
        },
    )


@router.get("/fragments/machine", response_class=HTMLResponse)
def machine_fragment(request: Request) -> HTMLResponse:
    """
    Render just the machine strip, for the header's periodic refresh.

    Args:
        request: The incoming request.

    Returns:
        The machine strip fragment.
    """
    return page(request, "fragments/machine.html", {})


@router.get("/apps", response_class=HTMLResponse)
def apps(request: Request) -> HTMLResponse:
    """
    Render the applications list.

    Args:
        request: The incoming request.

    Returns:
        The applications page.
    """
    return page(request, "pages/resources.html", _resource_page("apps"))


@router.get("/services", response_class=HTMLResponse)
def services(request: Request) -> HTMLResponse:
    """
    Render the services list.

    Args:
        request: The incoming request.

    Returns:
        The services page.
    """
    return page(request, "pages/resources.html", _resource_page("services"))


@router.get("/sites", response_class=HTMLResponse)
def sites(request: Request) -> HTMLResponse:
    """
    Render the sites list.

    Args:
        request: The incoming request.

    Returns:
        The sites page.
    """
    return page(request, "pages/resources.html", _resource_page("sites"))


@router.get("/databases", response_class=HTMLResponse)
def databases(request: Request) -> HTMLResponse:
    """
    Render the databases list.

    Args:
        request: The incoming request.

    Returns:
        The databases page.
    """
    return page(request, "pages/resources.html", _resource_page("databases"))


#: Copy for each resource list. Empty states name the one thing to do next and
#: give the equivalent command, because this audience often prefers the
#: terminal and should not have to guess the incantation.
_RESOURCE_COPY: dict[str, dict[str, str]] = {
    "apps": {
        "title": "Applications",
        "empty_title": "No applications yet",
        "empty_body": "Deploy one from a Git repository, an archive or a local directory.",
        "command": "wasm create -d example.com -s https://github.com/you/app",
    },
    "services": {
        "title": "Services",
        "empty_title": "No services yet",
        "empty_body": "Deploying an application creates the systemd service that runs it.",
        "command": "wasm service list",
    },
    "sites": {
        "title": "Sites",
        "empty_title": "No sites yet",
        "empty_body": "A site is the web server configuration that puts a domain in front of an application.",
        "command": "wasm site create -d example.com",
    },
    "databases": {
        "title": "Databases",
        "empty_title": "No databases yet",
        "empty_body": "Install an engine, then create the database your application needs.",
        "command": "wasm db create -e postgresql -n myapp",
    },
}


def _resource_page(kind: str) -> dict[str, Any]:
    """
    Build the context for a resource list page.

    Args:
        kind: Which resource to list.

    Returns:
        The page context.
    """
    rows = _rows_from_store(kind)
    copy = _RESOURCE_COPY[kind]
    return {
        "kind": kind,
        "rows": rows,
        "title": copy["title"],
        "subtitle": f"{len(rows)} on this machine" if rows else None,
        "empty_title": copy["empty_title"],
        "empty_body": copy["empty_body"],
        "command": copy["command"],
    }
