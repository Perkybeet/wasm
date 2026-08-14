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

import datetime as dt
import logging
from collections.abc import Callable, Coroutine
from typing import Any
from urllib.parse import parse_qs

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.routing import APIRoute
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool

from wasm.core.exceptions import WASMError
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
            # htmx follows a 303 itself and swaps the body it gets back, so a
            # plain redirect dropped the sign-in page's <main> inside the live
            # shell: a login form nested in a panel that was still showing the
            # machine's data. HX-Redirect makes the browser navigate instead.
            if request.headers.get("HX-Request"):
                raise HTTPException(
                    status_code=401,
                    headers={"HX-Redirect": "/login"},
                    detail="Sign in to reach the panel",
                ) from exc
            raise HTTPException(
                status_code=303,
                headers={"Location": "/login"},
                detail="Sign in to reach the panel",
            ) from exc
        raise
    request.state.session = session
    return session


class PageErrorRoute(APIRoute):
    """
    Route that renders a failure instead of dropping the panel.

    The API has had this since the beginning, as
    :class:`~wasm.web.api.deps.WASMErrorRoute`; the pages never did. A manager
    raising anywhere in a handler here reached Starlette's default and the
    operator got the words "Internal Server Error" as plain text on a white
    page: no navigation, no machine strip, and above all none of what nginx or
    systemd actually said, which is the one thing they needed.

    Attaching it to the router rather than to each handler is the point. A
    boundary enforced per caller has as many holes as there are callers, and
    there are eleven of them here.
    """

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        """
        Wrap the generated handler in the panel's error boundary.

        Returns:
            The wrapped handler.
        """
        handler = super().get_route_handler()

        async def wrapped(request: Request) -> Response:
            try:
                return await handler(request)
            except WASMError as exc:
                # Only WASMError. An AttributeError from this layer is a bug in
                # the panel, and the project's whole position on error handling
                # is that those stay loud: catching them here to render a
                # polite screen is how five calls to methods that did not exist
                # shipped for entire releases. HTTPException is not caught
                # either - a redirect to the sign-in page and a 404 for an
                # undeployed domain are answers, not failures.
                log.warning("%s failed: %s", request.url.path, exc)
                return failure(request, str(exc), getattr(exc, "details", "") or "")

        return wrapped


def failure(request: Request, fix: str, output: str) -> Response:
    """
    Render the failure screen.

    Args:
        request: The incoming request.
        fix: What to do about it, in plain words.
        output: The tool's own output, verbatim.

    Returns:
        The rendered page, at 500.
    """
    return page(
        request,
        "pages/failure.html",
        {
            "title": "This screen could not be rendered",
            "section": None,
            "fix": fix,
            "output": output,
        },
        status_code=500,
    )


router = APIRouter(
    include_in_schema=False,
    dependencies=[Depends(require_page_session)],
    route_class=PageErrorRoute,
)


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


#: Web servers a site can be fronted by. The same two the CLI offers.
WEBSERVERS = ("nginx", "apache")


def _deploy_form(request: Request, submitted: dict[str, Any], problem: Any = None) -> HTMLResponse:
    """
    Render the deployment form.

    Args:
        request: The incoming request.
        submitted: What the operator typed, so a refusal does not empty the
            form they have to correct.
        problem: A ``fix``/``output`` mapping when a submission was refused.

    Returns:
        The deployment page.
    """
    from wasm.deployers.registry import available_types

    return page(
        request,
        "pages/deploy.html",
        {
            "app_types": available_types(),
            "webservers": WEBSERVERS,
            "submitted": submitted,
            "problem": problem,
        },
        status_code=400 if problem else 200,
    )


# Declared before /apps/{domain}, or the parametrised route would answer for
# it and the panel would look for an application called "new".
@router.get("/apps/new", response_class=HTMLResponse)
def deploy_form(request: Request) -> HTMLResponse:
    """
    Render the form that deploys an application.

    Args:
        request: The incoming request.

    Returns:
        The deployment page.
    """
    return _deploy_form(request, {"ssl": True, "app_type": "auto", "webserver": "nginx"})


@router.post("/apps/new")
async def deploy_submit(request: Request) -> Response:
    """
    Queue a deployment from the form.

    The work is done by :func:`wasm.web.api.apps.create_app`, which is the same
    function the JSON API calls. This route only translates a form submission
    into the request model and a refusal into a screen: a second implementation
    of "deploy an application" is exactly what the panel must never grow.

    Async, unlike every other handler here, because reading a request body
    requires awaiting it - the same reason ``login_submit`` is. Everything that
    blocks is handed to a worker thread rather than run on the event loop,
    which is what the rule about synchronous handlers actually protects: one
    deployment must not freeze every other request, every WebSocket and the
    heartbeat that says the panel is alive.

    The body is parsed with ``parse_qs`` rather than ``request.form()`` so the
    panel does not acquire python-multipart, which would have to be declared in
    four packaging files and exist on every target distribution.

    Args:
        request: The incoming request.

    Returns:
        A redirect to the activity screen, or the form again with the reason it
        was refused.
    """
    from wasm.web.api.apps import CreateAppRequest, create_app

    fields = parse_qs((await request.body()).decode("utf-8", errors="replace"))

    def field(name: str, default: str = "") -> str:
        """
        Args:
            name: Field name.
            default: Value to use when the field was not submitted.

        Returns:
            The submitted value, stripped.
        """
        return fields.get(name, [default])[0].strip()

    submitted: dict[str, Any] = {
        "domain": field("domain"),
        "source": field("source"),
        "app_type": field("app_type", "auto"),
        "branch": field("branch"),
        "port": field("port"),
        "webserver": field("webserver", "nginx"),
        "ssl": "ssl" in fields,
    }

    try:
        body = CreateAppRequest(
            domain=submitted["domain"],
            source=submitted["source"],
            app_type=submitted["app_type"],
            port=int(submitted["port"]) if submitted["port"] else None,
            webserver=submitted["webserver"],
            branch=submitted["branch"] or None,
            ssl=submitted["ssl"],
        )
    except (ValueError, ValidationError) as exc:
        return await run_in_threadpool(
            _deploy_form, request, submitted, {"fix": "Check the form.", "output": str(exc)}
        )

    session = getattr(request.state, "session", {})

    try:
        await run_in_threadpool(create_app, body, session)
    except HTTPException as exc:
        # 409 for a domain already deployed, 503 when no port is free. Both are
        # answers to what was typed, so the form comes back with them.
        return await run_in_threadpool(
            _deploy_form, request, submitted, {"fix": str(exc.detail), "output": ""}
        )
    except WASMError as exc:
        return await run_in_threadpool(
            _deploy_form,
            request,
            submitted,
            {"fix": str(exc), "output": getattr(exc, "details", "") or ""},
        )

    # A deployment takes minutes, so the operator is sent where they can watch
    # it rather than left on a form that looks like it did nothing.
    return RedirectResponse("/activity", status_code=303)


@router.post("/apps/{domain}/backup")
def back_up_now(domain: str, request: Request) -> Response:
    """
    Queue a backup of one application.

    An adapter, like the deployment form: the work is
    :func:`wasm.web.api.backups.create_backup`, which takes its request as a
    JSON body. A button in a row cannot send one without an htmx extension the
    panel does not vendor, so the domain travels in the path here and the
    request model is built on this side. Nothing about what a backup *is* lives
    here.

    Args:
        domain: The application to back up.
        request: The incoming request.

    Returns:
        An empty response; the job is reported by the feed and the activity
        screen, like every other queued job.
    """
    from wasm.web.api.backups import CreateBackupRequest, create_backup

    session = getattr(request.state, "session", {})
    create_backup(CreateBackupRequest(domain=domain), session)
    return Response(status_code=204)


@router.post("/backups/{backup_id}/verify")
def verify_now(backup_id: str, request: Request) -> Response:
    """
    Verify one archive, and say plainly when it is not sound.

    The API answers 200 with ``valid: false`` for a corrupt archive, which is
    correct for a JSON client and wrong for a button: the panel would report
    "Verified" in the same green it uses for success, about a backup that
    cannot be restored. A backup nobody can restore is worse than no backup,
    because it is the one people are counting on.

    So a failed verification is answered as a failure here, with what the
    checker actually found.

    Args:
        backup_id: The archive to check.
        request: The incoming request.

    Returns:
        An empty response when the archive is sound.

    Raises:
        HTTPException: 422 with the checker's findings when it is not.
    """
    from wasm.web.api.backups import verify_backup

    session = getattr(request.state, "session", {})
    result = verify_backup(backup_id, session)

    if result.valid:
        return Response(status_code=204)

    findings = result.errors or result.warnings or ["The archive did not pass verification."]
    raise HTTPException(
        status_code=422,
        detail=f"{backup_id} failed verification. " + " ".join(findings),
    )


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
        The settings page, including the two-factor authentication section.
    """
    sections, problem = resources.settings_sections()
    context: dict[str, Any] = {"sections": sections, "problem": problem}
    context.update(_totp_context())
    context.update(_api_tokens_context())
    context.update(_sessions_context(request))
    return page(request, "pages/settings.html", context)


def _totp_context(
    enroll: Any = None,
    backup_codes: list[str] | None = None,
    problem: dict[str, str] | None = None,
) -> dict[str, Any]:
    """
    Build the context the two-factor fragment renders from.

    Args:
        enroll: A begun enrolment (secret and URI), when one is on screen.
        backup_codes: Freshly issued backup codes, shown exactly once.
        problem: A ``fix``/``output`` mapping when a code was refused.

    Returns:
        The fragment context. The active secret is never in it: only a pending,
        unconfirmed enrolment ever renders one.
    """
    from wasm.web.server import get_token_manager

    return {
        "totp": get_token_manager().totp_status(),
        "totp_enroll": enroll,
        "totp_backup_codes": backup_codes,
        "totp_problem": problem,
    }


def _totp_fragment(
    request: Request,
    *,
    enroll: Any = None,
    backup_codes: list[str] | None = None,
    problem: dict[str, str] | None = None,
) -> HTMLResponse:
    """
    Render the two-factor section for an htmx swap.

    Refusals render at 200 on purpose: htmx does not swap an error status, so
    a 400 here would leave the screen frozen and report the refusal nowhere.
    The refusal is on the fragment itself, as a problem block, which is where
    the operator is looking.

    Args:
        request: The incoming request.
        enroll: A begun enrolment to show.
        backup_codes: Freshly issued backup codes to show once.
        problem: A ``fix``/``output`` mapping when a code was refused.

    Returns:
        The rendered fragment.
    """
    return page(
        request,
        "fragments/totp.html",
        _totp_context(enroll=enroll, backup_codes=backup_codes, problem=problem),
    )


def _form_code(body: bytes) -> str:
    """
    Read the ``code`` field out of an urlencoded fragment form.

    Args:
        body: The raw request body.

    Returns:
        The submitted code, stripped.
    """
    return parse_qs(body.decode("utf-8", errors="replace")).get("code", [""])[0].strip()


# These three handlers are adapters over the JSON API, exactly like the deploy
# form: the work, the auditing and the lockout accounting live in
# wasm.web.api.auth, and a second implementation of "enable two-factor" is what
# the panel must never grow. They stay synchronous - the contract for page
# handlers - which is why the body arrives as a Body(bytes) parameter FastAPI
# reads off the event loop, rather than through an await of our own.


@router.post("/settings/2fa/enroll", response_class=HTMLResponse)
def totp_enroll(request: Request) -> HTMLResponse:
    """
    Begin enrolment and show the QR, the manual key and the confirm field.

    Args:
        request: The incoming request.

    Returns:
        The enrolment fragment.
    """
    from wasm.web.api.auth import two_factor_enroll

    session = getattr(request.state, "session", {})
    enrollment = two_factor_enroll(request, session)
    return _totp_fragment(request, enroll=enrollment)


@router.post("/settings/2fa/confirm", response_class=HTMLResponse)
def totp_confirm(request: Request, body: bytes = Body(default=b"")) -> HTMLResponse:
    """
    Confirm enrolment with a code and show the backup codes once.

    Args:
        request: The incoming request.
        body: The urlencoded form carrying the code.

    Returns:
        The fragment with the backup codes, or the enrolment again with the
        refusal when the code did not verify.
    """
    from wasm.web.api.auth import TwoFactorCode, two_factor_confirm

    session = getattr(request.state, "session", {})
    try:
        confirmed = two_factor_confirm(request, TwoFactorCode(code=_form_code(body)), session)
    except HTTPException as exc:
        from wasm.web.api.auth import enrollment_uri
        from wasm.web.server import get_token_manager

        pending = get_token_manager().pending_totp_secret()
        enroll = (
            {"secret": pending, "uri": enrollment_uri(pending)} if pending is not None else None
        )
        return _totp_fragment(
            request, enroll=enroll, problem={"fix": str(exc.detail), "output": ""}
        )
    return _totp_fragment(request, backup_codes=confirmed.backup_codes)


@router.post("/settings/2fa/disable", response_class=HTMLResponse)
def totp_disable(request: Request, body: bytes = Body(default=b"")) -> HTMLResponse:
    """
    Turn the second factor off, on presentation of a current code.

    Args:
        request: The incoming request.
        body: The urlencoded form carrying the code.

    Returns:
        The fragment in its new state, or with the refusal when the code did
        not verify. A wrong code here counts against the same lockout a failed
        login does; that accounting lives in the API function this adapts.
    """
    from wasm.web.api.auth import TwoFactorCode, two_factor_disable

    session = getattr(request.state, "session", {})
    try:
        two_factor_disable(request, TwoFactorCode(code=_form_code(body)), session)
    except HTTPException as exc:
        return _totp_fragment(request, problem={"fix": str(exc.detail), "output": ""})
    return _totp_fragment(request)


def _moment(value: float | None) -> dt.datetime | None:
    """
    Turn a stored UNIX timestamp into what the template filters read.

    Args:
        value: Seconds since the epoch, or None.

    Returns:
        An aware datetime, or None when there is nothing to show.
    """
    return dt.datetime.fromtimestamp(value, tz=dt.timezone.utc) if value else None


def _form_fields(body: bytes) -> dict[str, str]:
    """
    Read the fields out of an urlencoded fragment form.

    Args:
        body: The raw request body.

    Returns:
        First value per field, stripped.
    """
    parsed = parse_qs(body.decode("utf-8", errors="replace"))
    return {name: values[0].strip() for name, values in parsed.items() if values}


def _api_tokens_context(
    created: Any = None, problem: dict[str, str] | None = None
) -> dict[str, Any]:
    """
    Build the context the API tokens fragment renders from.

    Args:
        created: A freshly issued token, shown exactly once.
        problem: A ``fix``/``output`` mapping when a submission was refused.

    Returns:
        The fragment context. No stored token is ever in it: the list carries
        names and metadata, and only an issuance just performed carries the
        credential.
    """
    from wasm.web.server import get_token_manager

    tokens = [
        {
            "id": record["id"],
            "name": record["name"],
            "scope": record["scope"],
            "created": _moment(record["created_at"]),
            "expires": _moment(record["expires_at"]),
            "last_used": _moment(record["last_used_at"]),
            "revoked": record["revoked_at"] is not None,
        }
        for record in get_token_manager().list_api_tokens()
    ]
    return {"api_tokens": tokens, "api_token_created": created, "api_token_problem": problem}


def _api_tokens_fragment(
    request: Request,
    *,
    created: Any = None,
    problem: dict[str, str] | None = None,
) -> HTMLResponse:
    """
    Render the API tokens section for an htmx swap.

    Refusals render at 200 on purpose: htmx does not swap an error status, so
    a 400 here would leave the screen frozen and report the refusal nowhere.

    Args:
        request: The incoming request.
        created: A freshly issued token to show once.
        problem: A ``fix``/``output`` mapping when a submission was refused.

    Returns:
        The rendered fragment.
    """
    return page(
        request, "fragments/api_tokens.html", _api_tokens_context(created=created, problem=problem)
    )


def _sessions_context(request: Request, problem: dict[str, str] | None = None) -> dict[str, Any]:
    """
    Build the context the sessions fragment renders from.

    Args:
        request: The incoming request, carrying the caller's own session so
            its row can be marked and its Revoke disabled.
        problem: A ``fix``/``output`` mapping when a revocation was refused.

    Returns:
        The fragment context. Only truncated session id prefixes are in it.
    """
    from wasm.web.server import get_token_manager

    session = getattr(request.state, "session", None) or {}
    current_sid = session.get("sid") if session.get("type") == "session" else None
    rows = [
        {
            "sid_prefix": entry["sid_prefix"],
            "client_ip": entry["client_ip"],
            "created": _moment(entry["created_at"]),
            "last_seen": _moment(entry["last_seen"]),
            "expires": _moment(entry["expires_at"]),
            "is_current": entry["is_current"],
        }
        for entry in get_token_manager().list_sessions(current_sid)
    ]
    return {"panel_sessions": rows, "sessions_problem": problem}


def _sessions_fragment(request: Request, problem: dict[str, str] | None = None) -> HTMLResponse:
    """
    Render the sessions section for an htmx swap.

    Args:
        request: The incoming request.
        problem: A ``fix``/``output`` mapping when a revocation was refused.

    Returns:
        The rendered fragment.
    """
    return page(request, "fragments/sessions.html", _sessions_context(request, problem=problem))


# Adapters over the JSON API, exactly like the two-factor handlers above: the
# work, the auditing and the scope rules live in wasm.web.api.auth, and a
# second implementation of "issue a token" is what the panel must never grow.


@router.post("/settings/tokens", response_class=HTMLResponse)
def api_token_create(request: Request, body: bytes = Body(default=b"")) -> HTMLResponse:
    """
    Issue an API token from the form and show it exactly once.

    Args:
        request: The incoming request.
        body: The urlencoded form: name, scope and an expiry preset in hours,
            empty for a token that only dies by revocation.

    Returns:
        The fragment with the fresh token, or with the refusal inline.
    """
    from wasm.web.api.auth import ApiTokenRequest
    from wasm.web.api.auth import create_api_token as api_create_token

    fields = _form_fields(body)
    session = getattr(request.state, "session", {})

    try:
        model = ApiTokenRequest(
            name=fields.get("name", ""),
            scope=fields.get("scope", ""),
            expires_hours=int(fields["expires_hours"]) if fields.get("expires_hours") else None,
        )
    except (ValueError, ValidationError) as exc:
        return _api_tokens_fragment(request, problem={"fix": "Check the form.", "output": str(exc)})

    try:
        created = api_create_token(request, model, session)
    except WASMError as exc:
        return _api_tokens_fragment(
            request, problem={"fix": str(exc), "output": getattr(exc, "details", "") or ""}
        )
    return _api_tokens_fragment(request, created=created)


@router.post("/settings/tokens/{token_id}/revoke", response_class=HTMLResponse)
def api_token_revoke(token_id: int, request: Request) -> HTMLResponse:
    """
    Revoke one API token from its row.

    Args:
        token_id: The record to revoke.
        request: The incoming request.

    Returns:
        The fragment in its new state, or with the refusal inline.
    """
    from wasm.web.api.auth import revoke_api_token as api_revoke_token

    session = getattr(request.state, "session", {})
    try:
        api_revoke_token(token_id, request, session)
    except HTTPException as exc:
        return _api_tokens_fragment(request, problem={"fix": str(exc.detail), "output": ""})
    return _api_tokens_fragment(request)


@router.post("/settings/sessions/{sid_prefix}/revoke", response_class=HTMLResponse)
def session_revoke(sid_prefix: str, request: Request) -> HTMLResponse:
    """
    Revoke one session from its row.

    Args:
        sid_prefix: Truncated id of the session to revoke.
        request: The incoming request.

    Returns:
        The fragment in its new state, or with the refusal inline - including
        the refusal to revoke the session this very click arrived on.
    """
    from wasm.web.api.auth import revoke_one_session

    session = getattr(request.state, "session", {})
    try:
        revoke_one_session(sid_prefix, request, session)
    except HTTPException as exc:
        return _sessions_fragment(request, problem={"fix": str(exc.detail), "output": ""})
    except WASMError as exc:
        return _sessions_fragment(
            request, problem={"fix": str(exc), "output": getattr(exc, "details", "") or ""}
        )
    return _sessions_fragment(request)


@router.post("/settings/sessions/revoke-all")
def sessions_revoke_all(request: Request) -> Response:
    """
    Sign out every session, including this one, and leave for the sign-in page.

    The revocation and its audit line are
    :func:`wasm.web.api.auth.revoke_all_sessions`, called directly; this route
    only adds what a browser needs on top of the JSON answer - the cookie
    clearing it already does, plus the redirect htmx follows.

    Args:
        request: The incoming request.

    Returns:
        An empty response telling the browser to go to the sign-in page.
    """
    from wasm.web.api.auth import revoke_all_sessions

    session = getattr(request.state, "session", {})
    response = Response(status_code=200, headers={"HX-Redirect": "/login"})
    revoke_all_sessions(request, response, session)
    return response


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
        "action_label": "Deploy",
        "action_href": "/apps/new",
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
        # Only applications can be created from the panel. The others are made
        # by deploying one, and offering a button that leads nowhere is worse
        # than offering none: the empty state's own copy says so.
        "action_label": copy.get("action_label"),
        "action_href": copy.get("action_href"),
    }
