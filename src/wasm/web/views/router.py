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

Every entry in the navigation resolves to a route declared here. A dead link
in a panel that holds root over the machine costs more trust than a screen
that is missing, because the operator stops believing the rest of it.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, Response

from wasm.web.auth import require_auth
from wasm.web.views import resources
from wasm.web.views.rendering import page

log = logging.getLogger(__name__)


async def require_page_session(request: Request) -> dict[str, Any]:
    """
    Require a session, sending a browser to the sign-in page when there is none.

    Pages get a redirect where the API gets a 401: a person who typed a URL
    should land on a form, not on a JSON error. The check itself is the same
    one the API uses, so there is one place where a credential is verified and
    one counter recording failures.

    Only a 401 becomes a redirect. A 403 from here means the session is valid
    and its CSRF token was not presented, and answering that with "sign in
    again" is both a lie and a loop: signing in produces the same session and
    the next click fails the same way. It is passed through so the operator
    reads what actually went wrong.

    Args:
        request: The incoming request.

    Returns:
        The authenticated session payload.

    Raises:
        HTTPException: A 303 redirect to the sign-in page when unauthenticated,
            or the original refusal when the session is real but the request
            was not accepted.
    """
    try:
        session = await require_auth(request)
    except HTTPException as exc:
        if exc.status_code == 401:
            raise HTTPException(
                status_code=303,
                headers={"Location": "/login"},
                detail="Sign in to reach the panel",
            ) from exc
        raise
    request.state.session = session
    return session


router = APIRouter(include_in_schema=False, dependencies=[Depends(require_page_session)])


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request) -> HTMLResponse:
    """
    Render the overview.

    Args:
        request: The incoming request.

    Returns:
        The overview page.
    """
    apps = resources.resource_rows("apps")
    running, recent = resources.job_rows()
    return page(
        request,
        "pages/dashboard.html",
        {
            "apps": apps,
            "attention": resources.needs_attention(apps),
            "running": running,
            "activity": recent,
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


@router.get("/apps/{domain}", response_class=HTMLResponse)
def app_detail(request: Request, domain: str) -> HTMLResponse:
    """
    Render one application: what it is, what runs it and what it was told.

    Args:
        request: The incoming request.
        domain: The application's domain.

    Returns:
        The application page, or a 404 page naming the domain that is not
        deployed. The domain is echoed escaped, like every other value the
        templates render.
    """
    detail = resources.application_detail(domain)
    if detail is None:
        return page(
            request,
            "pages/missing.html",
            {
                "section": "Applications",
                "title": "No such application",
                "body": f"Nothing is deployed at {domain} on this machine.",
                "command": "wasm list",
            },
            status_code=404,
        )
    return page(request, "pages/app.html", detail)


@router.get("/certificates", response_class=HTMLResponse)
def certificates(request: Request) -> HTMLResponse:
    """
    Render the certificates: what covers what, and for how much longer.

    Args:
        request: The incoming request.

    Returns:
        The certificates page. When certbot cannot be asked, the uncovered
        list is None rather than empty: "no domain is missing a certificate"
        and "this machine cannot tell" are different facts, and showing the
        second as the first told the operator every domain was covered at
        exactly the moment nothing could be issued at all.
    """
    rows, problem = resources.certificate_rows()
    uncovered = resources.domains_without_certificate(rows) if problem is None else None
    expiring = [row for row in rows if row["state"] in ("busy", "failed")]
    return page(
        request,
        "pages/certificates.html",
        {
            "rows": rows,
            "uncovered": uncovered,
            "expiring": len(expiring),
            "problem": problem,
        },
    )


@router.get("/backups", response_class=HTMLResponse)
def backups(request: Request) -> HTMLResponse:
    """
    Render the backups: when each was taken, how big it is and what it holds.

    Args:
        request: The incoming request.

    Returns:
        The backups page.
    """
    rows, storage = resources.backup_rows()
    return page(request, "pages/backups.html", {"rows": rows, "storage": storage})


@router.get("/activity", response_class=HTMLResponse)
def activity(request: Request) -> HTMLResponse:
    """
    Render what this machine is doing and what it just did.

    Args:
        request: The incoming request.

    Returns:
        The activity page.
    """
    running, recent = resources.job_rows()
    return page(request, "pages/activity.html", {"running": running, "recent": recent})


@router.get("/settings", response_class=HTMLResponse)
def settings(request: Request) -> HTMLResponse:
    """
    Render the stored configuration, with every secret redacted.

    Args:
        request: The incoming request.

    Returns:
        The settings page.
    """
    sections, problem = resources.settings_sections()
    return page(
        request,
        "pages/settings.html",
        {"sections": sections, "problem": problem},
    )


@router.post("/logout")
def sign_out(request: Request) -> Response:
    """
    End the session and send the browser back to the sign-in page.

    The JSON endpoint at ``/api/auth/logout`` cannot finish this job: it
    answers with a payload, and a person who clicked "Sign out" needs to end up
    somewhere else. This does the same revocation and hands htmx the redirect
    header, so the button in the shell leads somewhere instead of nowhere.

    The CSRF header is required here exactly as it is on every other mutation,
    because it goes through the same session dependency as the pages.

    Args:
        request: The incoming request.

    Returns:
        An empty response telling the browser to go to the sign-in page.
    """
    from wasm.web.auth import (
        CSRF_COOKIE_NAME,
        SESSION_COOKIE_NAME,
        get_global_token_manager,
    )

    session = getattr(request.state, "session", None) or {}
    manager = get_global_token_manager()
    session_id = session.get("sid")
    if manager is not None and session_id and session.get("type") == "session":
        manager.revoke_session(session_id)

    response = Response(status_code=200, headers={"HX-Redirect": "/login"})
    response.delete_cookie(SESSION_COOKIE_NAME, path="/")
    response.delete_cookie(CSRF_COOKIE_NAME, path="/")
    return response


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
    rows = resources.resource_rows(kind)
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
