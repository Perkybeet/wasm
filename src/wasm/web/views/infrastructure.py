# Copyright (c) 2024-2026 Yago Lopez Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
Infrastructure pages: creating and editing systemd services and web server sites.

Kept as its own module so panel work on infrastructure never has to edit
``views/router.py``: the aggregate router includes this one at the bottom of
that file. Handlers here follow the same contract as the rest of the views:
synchronous, session-guarded, rendering Jinja fragments over the managers.

Every handler is an adapter over :mod:`wasm.web.api.services` or
:mod:`wasm.web.api.sites`, exactly like the databases screen is an adapter
over its API module: the name validation, the unit ownership guard, the
configtest gate and the atomic writes all live behind those functions, and a
second implementation of "write a unit file as root" is what this panel must
never grow. A handler here translates a form into a request model and a
refusal into a fragment, nothing more.

Refusals render at 200 on purpose: htmx does not swap an error status, so a
400 would leave the screen frozen and report the refusal nowhere. The refusal
is on the fragment itself, as a problem block - the fix in plain words above,
the tool's own output verbatim in mono below - which is where the operator is
looking.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import parse_qs

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from pydantic import ValidationError

from wasm.core.exceptions import WASMError
from wasm.web.views.rendering import page
from wasm.web.views.router import (
    _RESOURCE_COPY,
    WEBSERVERS,
    PageErrorRoute,
    _form_fields,
    require_page_session,
)

#: The templates the site creation form offers. Both backends ship exactly
#: these two; the API checks whatever is chosen against the selected backend's
#: own template list, so this tuple can only ever under-offer, never lie.
SITE_TEMPLATES = ("proxy", "static")

# Annotated explicitly: this module and router.py import each other, and inside
# that cycle mypy cannot infer the type of a module-level variable another
# module reads.
router: APIRouter = APIRouter(
    include_in_schema=False,
    dependencies=[Depends(require_page_session)],
    route_class=PageErrorRoute,
)

# The services and sites list pages are rendered by views/router.py from this
# copy table. Their creation flows live in this module, so the copy that
# announces them is updated here at import time: one source for the page text,
# and the aggregate router stays untouched during concurrent panel work.
# Services are the feature the classic segment runs a VPS for, so the empty
# state sells the range instead of shrugging.
_RESOURCE_COPY["services"].update(
    {
        "empty_title": "Run anything systemd can run",
        "empty_body": (
            "Run anything systemd can run: daemons, workers, schedulers. "
            "Create one from a command or a whole unit file; deploying an "
            "application creates its own."
        ),
        "action_label": "New service",
        "action_href": "/services/new",
    }
)
_RESOURCE_COPY["sites"].update(
    {
        "empty_body": (
            "A site is the web server configuration that puts a domain in "
            "front of an application or a directory of files."
        ),
        "action_label": "New site",
        "action_href": "/sites/new",
    }
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


def _split_detail(detail: str) -> dict[str, str]:
    """
    Recover the fix/output pair from an HTTPException raised by an API module.

    The services API folds a :class:`~wasm.core.exceptions.WASMError` into one
    string - ``str(exc)`` puts the actionable details after a fixed separator -
    before raising it as an HTTPException. Splitting on that separator puts the
    fix back above the block and the details back in mono, instead of both
    collapsing into one paragraph.

    Args:
        detail: The exception detail as the API composed it.

    Returns:
        ``fix`` and ``output`` for the problem block.
    """
    fix, _, output = str(detail).partition("\n  Details: ")
    return {"fix": fix, "output": output}


def _form_text(body: bytes, field: str) -> str:
    """
    Read one field out of an urlencoded form without stripping it.

    :func:`~wasm.web.views.router._form_fields` strips values, which is right
    for names and ports and wrong for file bodies: a unit file's own leading
    and trailing whitespace is content, and "the panel wrote exactly what was
    typed" is the whole promise of a raw editor.

    Args:
        body: The raw request body.
        field: Field name.

    Returns:
        The first submitted value, verbatim, or an empty string.
    """
    values = parse_qs(body.decode("utf-8", errors="replace")).get(field)
    return values[0] if values else ""


# ---------------------------------------------------------------- services


def _unit_skeleton(user: str) -> str:
    """
    Render what the raw-mode textarea starts from.

    It carries the WASM marker because the manager refuses to write a unit
    without it: prefilling a body the backend would reject is a form that
    teaches the operator to fail.

    Args:
        user: Account the skeleton runs as.

    Returns:
        A complete, valid unit file body.
    """
    from wasm.managers.service_manager import WASM_UNIT_MARKER

    return (
        f"# {WASM_UNIT_MARKER}\n"
        "[Unit]\n"
        "Description=Queue worker\n"
        "After=network.target\n"
        "\n"
        "[Service]\n"
        "Type=simple\n"
        f"User={user}\n"
        "WorkingDirectory=/var/www\n"
        "ExecStart=/usr/bin/node worker.js\n"
        "Restart=always\n"
        "RestartSec=5\n"
        "\n"
        "[Install]\n"
        "WantedBy=multi-user.target\n"
    )


def _service_form_context(
    mode: str,
    submitted: dict[str, Any] | None = None,
    problem: dict[str, str] | None = None,
) -> dict[str, Any]:
    """
    Build the context the service creation form renders from.

    Args:
        mode: ``"simple"`` for the command form, ``"raw"`` for the unit
            textarea.
        submitted: What the operator typed, so a refusal does not empty the
            form they have to correct.
        problem: A ``fix``/``output`` mapping when a submission was refused.

    Returns:
        The fragment context, under the single key the template reads.
    """
    from wasm.core.config import Config
    from wasm.managers.service_manager import WASM_UNIT_MARKER

    user = Config().service_user
    return {
        "sform": {
            "mode": "raw" if mode == "raw" else "simple",
            "service_user": user,
            "marker": WASM_UNIT_MARKER,
            "skeleton": _unit_skeleton(user),
            "submitted": submitted or {},
            "problem": problem,
        }
    }


def _service_form_fragment(request: Request, mode: str, **kwargs: Any) -> HTMLResponse:
    """
    Render the service creation form for an htmx swap.

    Args:
        request: The incoming request.
        mode: Which tab to show.
        **kwargs: Forwarded to :func:`_service_form_context`.

    Returns:
        The rendered fragment.
    """
    return page(request, "fragments/infra_service_form.html", _service_form_context(mode, **kwargs))


@router.get("/services/new", response_class=HTMLResponse)
def service_new(request: Request, mode: str = "simple") -> HTMLResponse:
    """
    Render the page that creates a systemd service.

    Args:
        request: The incoming request.
        mode: Which tab to open with.

    Returns:
        The creation page, on the simple tab unless raw was asked for.
    """
    return page(request, "pages/service_new.html", _service_form_context(mode))


@router.get("/services/new/form", response_class=HTMLResponse)
def service_new_form(request: Request, mode: str = "simple") -> HTMLResponse:
    """
    Render one tab of the creation form, for the tab buttons to swap in.

    Args:
        request: The incoming request.
        mode: Which tab to render.

    Returns:
        The form fragment.
    """
    return _service_form_fragment(request, mode)


@router.post("/services/new", response_class=HTMLResponse)
def service_create(request: Request, body: bytes = Body(default=b"")) -> Response:
    """
    Create a service from either tab of the form.

    Simple mode sends the fields; raw mode sends a whole unit file, written
    verbatim. Both go through :func:`wasm.web.api.services.create_service`,
    where the name validation and the ownership guard live. An empty user
    field means the configured service user, never root: that resolution is
    the manager's, not this form's.

    Args:
        request: The incoming request.
        body: The urlencoded form.

    Returns:
        A redirect to the new unit's editor, or the form again with the
        refusal inline and what was typed preserved.
    """
    from wasm.web.api.services import CreateServiceRequest, create_service

    fields = _form_fields(body)
    mode = "raw" if fields.get("mode") == "raw" else "simple"
    submitted: dict[str, Any] = {
        "name": fields.get("name", ""),
        "command": fields.get("command", ""),
        "directory": fields.get("directory", "/var/www"),
        "user": fields.get("user", ""),
        "port": fields.get("port", ""),
        "unit": _form_text(body, "unit"),
    }

    if mode == "raw":
        model_kwargs: dict[str, Any] = {
            "name": submitted["name"],
            "raw_content": submitted["unit"],
        }
    else:
        # The port travels as the PORT environment variable: the simple mode
        # of the API has no port field, and PORT is what application servers
        # actually read. The unit file makes the handover explicit.
        model_kwargs = {
            "name": submitted["name"],
            "command": submitted["command"],
            "working_directory": submitted["directory"] or "/var/www",
            "user": submitted["user"] or None,
            "environment": {"PORT": submitted["port"]} if submitted["port"] else None,
        }

    try:
        model = CreateServiceRequest(**model_kwargs)
    except ValidationError as exc:
        return _service_form_fragment(
            request,
            mode,
            submitted=submitted,
            problem={"fix": "Check the form.", "output": str(exc)},
        )

    try:
        outcome = create_service(model, request, _session(request))
    except HTTPException as exc:
        return _service_form_fragment(
            request, mode, submitted=submitted, problem=_split_detail(str(exc.detail))
        )
    except WASMError as exc:
        return _service_form_fragment(request, mode, submitted=submitted, problem=_refusal(exc))

    # Straight to the unit editor: the first thing an operator wants after
    # creating a unit is to see exactly what was written for it.
    return Response(status_code=200, headers={"HX-Redirect": f"/services/{outcome.service}/config"})


def _service_editor_context(
    request: Request,
    name: str,
    *,
    content: str | None = None,
    notice: str | None = None,
    problem: dict[str, str] | None = None,
) -> dict[str, Any]:
    """
    Build the context the unit editor renders from.

    The current body is read through the same API function a JSON client
    uses, so the editor and a Bearer client can never disagree about what a
    unit holds.

    Args:
        request: The incoming request.
        name: Service name, resolved by the API.
        content: Textarea content to show verbatim, when the operator's own
            submission is being redisplayed after a refusal, rather than what
            is on disk.
        notice: One line reporting what a save just did.
        problem: A ``fix``/``output`` mapping when a save was refused.

    Returns:
        The fragment context.

    Raises:
        HTTPException: 404 when the unit does not exist, 400 when the name is
            not a safe unit name.
    """
    from wasm.managers.service_manager import WASM_UNIT_MARKER
    from wasm.web.api.services import get_service_config

    data = get_service_config(name, request, _session(request))
    return {
        "editor": {
            "name": data["service"],
            "path": data["path"],
            "marker": WASM_UNIT_MARKER,
            "content": content if content is not None else data["config"],
            "notice": notice,
            "problem": problem,
        }
    }


def _service_editor_fragment(request: Request, name: str, **kwargs: Any) -> HTMLResponse:
    """
    Render the unit editor for an htmx swap.

    Args:
        request: The incoming request.
        name: Service name.
        **kwargs: Forwarded to :func:`_service_editor_context`.

    Returns:
        The rendered fragment.
    """
    return page(
        request,
        "fragments/infra_service_editor.html",
        _service_editor_context(request, name, **kwargs),
    )


@router.get("/services/{name}/config", response_class=HTMLResponse)
def service_editor(name: str, request: Request) -> HTMLResponse:
    """
    Render the editor for one unit file.

    Args:
        name: Service name.
        request: The incoming request.

    Returns:
        The editor page, or a page naming the unit that does not exist. The
        name is echoed escaped, like every other value the templates render.
    """
    try:
        context = _service_editor_context(request, name)
    except HTTPException as exc:
        return page(
            request,
            "pages/missing.html",
            {
                "section": "Services",
                "title": "No such service",
                "body": str(exc.detail),
                "command": "wasm service list",
            },
            status_code=exc.status_code,
        )
    return page(request, "pages/service_editor.html", context)


@router.post("/services/{name}/config", response_class=HTMLResponse)
def service_editor_save(
    name: str, request: Request, body: bytes = Body(default=b"")
) -> HTMLResponse:
    """
    Replace a unit file body from the editor.

    The write goes through :func:`wasm.web.api.services.update_service_config`,
    which is where the ownership guard and the marker rule live. A refused
    body answers 200 with the backend's refusal verbatim - fix above, details
    in mono below - and the file on disk untouched; the textarea keeps what
    the operator typed so they correct it in place.

    Args:
        name: Service name.
        request: The incoming request.
        body: The urlencoded form carrying the unit body.

    Returns:
        The editor in its new state, or with the refusal inline.
    """
    from wasm.web.api.services import UpdateServiceConfigRequest, update_service_config

    text = _form_text(body, "config")
    try:
        outcome = update_service_config(
            name, UpdateServiceConfigRequest(config=text), request, _session(request)
        )
    except HTTPException as exc:
        return _service_editor_fragment(
            request, name, content=text, problem=_split_detail(str(exc.detail))
        )
    return _service_editor_fragment(request, name, notice=outcome.message)


# ------------------------------------------------------------------- sites


def _site_form_context(
    submitted: dict[str, Any] | None = None, problem: dict[str, str] | None = None
) -> dict[str, Any]:
    """
    Build the context the site creation form renders from.

    Args:
        submitted: What the operator typed, so a refusal does not empty the
            form they have to correct.
        problem: A ``fix``/``output`` mapping when a submission was refused.

    Returns:
        The fragment context, under the single key the template reads.
    """
    return {
        "nsite": {
            "templates": SITE_TEMPLATES,
            "webservers": WEBSERVERS,
            "submitted": submitted or {"type": "proxy", "webserver": "nginx"},
            "problem": problem,
        }
    }


def _site_form_fragment(request: Request, **kwargs: Any) -> HTMLResponse:
    """
    Render the site creation form for an htmx swap.

    Args:
        request: The incoming request.
        **kwargs: Forwarded to :func:`_site_form_context`.

    Returns:
        The rendered fragment.
    """
    return page(request, "fragments/infra_site_form.html", _site_form_context(**kwargs))


@router.get("/sites/new", response_class=HTMLResponse)
def site_new(request: Request) -> HTMLResponse:
    """
    Render the page that creates a virtual host.

    Args:
        request: The incoming request.

    Returns:
        The creation page.
    """
    return page(request, "pages/site_new.html", _site_form_context())


@router.post("/sites/new", response_class=HTMLResponse)
def site_create(request: Request, body: bytes = Body(default=b"")) -> Response:
    """
    Create a virtual host from the form.

    The work is :func:`wasm.web.api.sites.create_site`, which names the file
    through the manager - the same name the CLI, the store and certificate
    issuance expect - and refuses templates the chosen backend does not ship.

    Args:
        request: The incoming request.
        body: The urlencoded form: domain, type, port, web server and the TLS
            checkbox.

    Returns:
        A redirect to the new site's config editor, or the form again with
        the refusal inline and what was typed preserved.
    """
    from wasm.web.api.sites import CreateSiteRequest, create_site

    fields = _form_fields(body)
    submitted: dict[str, Any] = {
        "domain": fields.get("domain", ""),
        "type": fields.get("type", "proxy"),
        "port": fields.get("port", ""),
        "webserver": fields.get("webserver", "nginx"),
        "ssl": "ssl" in fields,
    }

    try:
        model = CreateSiteRequest(
            domain=submitted["domain"],
            webserver=submitted["webserver"] or None,
            template=submitted["type"],
            port=int(submitted["port"]) if submitted["port"] else 3000,
            ssl=submitted["ssl"],
        )
    except (ValueError, ValidationError) as exc:
        return _site_form_fragment(
            request, submitted=submitted, problem={"fix": "Check the form.", "output": str(exc)}
        )

    try:
        outcome = create_site(model, _session(request))
    except HTTPException as exc:
        return _site_form_fragment(
            request, submitted=submitted, problem={"fix": str(exc.detail), "output": ""}
        )
    except WASMError as exc:
        return _site_form_fragment(request, submitted=submitted, problem=_refusal(exc))

    return Response(status_code=200, headers={"HX-Redirect": f"/sites/{outcome.site}/config"})


def _site_editor_context(
    request: Request,
    domain: str,
    *,
    content: str | None = None,
    notice: str | None = None,
    problem: dict[str, str] | None = None,
) -> dict[str, Any]:
    """
    Build the context the site config editor renders from.

    Args:
        request: The incoming request.
        domain: Domain of the site.
        content: Textarea content to show verbatim, when the operator's own
            submission is being redisplayed after a refusal, rather than what
            is on disk.
        notice: One line reporting what an action just did.
        problem: A ``fix``/``output`` mapping when an action was refused.

    Returns:
        The fragment context.

    Raises:
        HTTPException: 404 when no such site exists.
        DomainError: When the domain is not acceptable.
    """
    from wasm.web.api.sites import get_site_config

    data = get_site_config(domain, _session(request))
    return {
        "editor": {
            "domain": data.site,
            "webserver": data.webserver,
            "path": data.path,
            "content": content if content is not None else data.config,
            "notice": notice,
            "problem": problem,
        }
    }


def _site_editor_fragment(request: Request, domain: str, **kwargs: Any) -> HTMLResponse:
    """
    Render the site config editor for an htmx swap.

    Args:
        request: The incoming request.
        domain: Domain of the site.
        **kwargs: Forwarded to :func:`_site_editor_context`.

    Returns:
        The rendered fragment.
    """
    return page(
        request,
        "fragments/infra_site_editor.html",
        _site_editor_context(request, domain, **kwargs),
    )


@router.get("/sites/{domain}/config", response_class=HTMLResponse)
def site_editor(domain: str, request: Request) -> HTMLResponse:
    """
    Render the editor for one virtual host configuration.

    Args:
        domain: Domain of the site.
        request: The incoming request.

    Returns:
        The editor page, or a page naming the site that does not exist.
    """
    try:
        context = _site_editor_context(request, domain)
    except HTTPException as exc:
        return page(
            request,
            "pages/missing.html",
            {
                "section": "Sites",
                "title": "No such site",
                "body": str(exc.detail),
                "command": "wasm site list",
            },
            status_code=exc.status_code,
        )
    return page(request, "pages/site_editor.html", context)


@router.post("/sites/{domain}/config", response_class=HTMLResponse)
def site_editor_save(
    domain: str, request: Request, body: bytes = Body(default=b"")
) -> HTMLResponse:
    """
    Replace a virtual host configuration from the editor.

    The write goes through :func:`wasm.web.api.sites.update_site_config`,
    which asks the web server itself before persisting anything: an invalid
    configuration comes back here as a refusal carrying the server's own
    output, and the file on disk is left exactly as it was. That output is
    rendered verbatim in mono with the fix above it, and the textarea keeps
    what the operator typed.

    Args:
        domain: Domain of the site.
        request: The incoming request.
        body: The urlencoded form carrying the configuration.

    Returns:
        The editor in its new state, or with the server's refusal inline.
    """
    from wasm.web.api.sites import UpdateSiteConfigRequest, update_site_config

    text = _form_text(body, "config")
    try:
        outcome = update_site_config(
            domain, UpdateSiteConfigRequest(config=text), _session(request)
        )
    except HTTPException as exc:
        return _site_editor_fragment(
            request, domain, content=text, problem={"fix": str(exc.detail), "output": ""}
        )
    except WASMError as exc:
        return _site_editor_fragment(request, domain, content=text, problem=_refusal(exc))
    return _site_editor_fragment(request, domain, notice=outcome.message)


@router.post("/sites/{domain}/reload", response_class=HTMLResponse)
def site_reload(domain: str, request: Request) -> HTMLResponse:
    """
    Test and reload the web server, from the editor's own button.

    The work is :func:`wasm.web.api.sites.reload_webserver`: the manager runs
    the server's own configuration test first and keeps the running config
    when it fails, so this button cannot take the other sites on the machine
    down.

    Args:
        domain: Domain of the site whose editor is on screen.
        request: The incoming request.

    Returns:
        The editor with the outcome, or with the refusal inline.
    """
    from wasm.web.api.sites import reload_webserver

    try:
        outcome = reload_webserver(_session(request))
    except HTTPException as exc:
        return _site_editor_fragment(
            request, domain, problem={"fix": str(exc.detail), "output": ""}
        )
    return _site_editor_fragment(request, domain, notice=outcome.message)
