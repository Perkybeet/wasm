# Copyright (c) 2024-2026 Yago Lopez Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
User cron job pages, kept out of ``views/router.py``.

Own module so concurrent panel work never edits the aggregate router: it
includes this one at the bottom of that file. Handlers follow the same
contract as the rest of the views: synchronous, session-guarded, rendering
Jinja fragments over the managers.

Every handler is an adapter over :mod:`wasm.web.api.cron`, the same way the
databases screen is an adapter over its API module: the shlex parsing, the
calendar validation, the ownership guard and the audit trail all live there
and in the manager it drives. A handler here translates a form into a request
model and a refusal into a fragment, nothing more.

Deleting confirms by typed name, compared here server-side: a ``required``
attribute on an input is a courtesy, not a guard. Refusals render at 200 on
purpose: htmx does not swap an error status, so a 400 would leave the screen
frozen and report the refusal nowhere.
"""

from __future__ import annotations

import re
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import ValidationError

from wasm.core.exceptions import WASMError
from wasm.managers.backup_scheduler import SCHEDULE_ALIASES
from wasm.web.views import resources
from wasm.web.views.rendering import page
from wasm.web.views.router import PageErrorRoute, _form_fields, require_page_session

router: APIRouter = APIRouter(
    include_in_schema=False,
    dependencies=[Depends(require_page_session)],
    route_class=PageErrorRoute,
)

#: What the form's schedule select offers besides the aliases: a free systemd
#: OnCalendar expression, typed into the field beside it.
CUSTOM_SCHEDULE = "custom"

#: Characters allowed to survive into an HTML id. Job names may carry dots,
#: which would break every CSS selector aimed at the id.
_ID_UNSAFE = re.compile(r"[^A-Za-z0-9_-]")


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


def _shape_job(info: Any) -> dict[str, Any]:
    """
    Turn one API job into the fields the table renders.

    Args:
        info: A :class:`~wasm.web.api.cron.CronJobInfo`.

    Returns:
        A row context. Endpoints and state words are built here, not in the
        template: a template that concatenates a route can be wrong about it,
        which is how a button ends up aimed at nothing.
    """
    if info.last_exit_code is None:
        exit_state, exit_text = "idle", "never ran"
    elif info.last_exit_code == 0:
        exit_state, exit_text = "active", "exit 0"
    else:
        exit_state, exit_text = "failed", f"exit {info.last_exit_code}"

    return {
        "name": info.name,
        "dom_id": _ID_UNSAFE.sub("-", info.name),
        "command": info.command,
        "user": info.user,
        "working_directory": info.working_directory,
        "app_domain": info.app_domain,
        "schedule": info.schedule,
        "on_calendar": info.on_calendar or "—",
        "enabled": info.enabled,
        "next_run": info.next_run if info.enabled else "disabled",
        "last_run": info.last_run,
        "exit_state": exit_state,
        "exit_text": exit_text,
        "run_endpoint": f"/cron/jobs/{info.name}/run",
        "toggle_endpoint": (
            f"/cron/jobs/{info.name}/disable" if info.enabled else f"/cron/jobs/{info.name}/enable"
        ),
        "toggle_label": "Disable" if info.enabled else "Enable",
        "delete_endpoint": f"/cron/jobs/{info.name}/delete",
        "runs_endpoint": f"/cron/jobs/{info.name}/runs",
    }


def _section_context(
    request: Request,
    *,
    notice: str | None = None,
    problem: dict[str, str] | None = None,
    form: dict[str, str] | None = None,
) -> dict[str, Any]:
    """
    Build the context the cron jobs section renders from.

    The listing is read through the JSON API's own function, so the section
    and a Bearer client can never disagree about what is scheduled.

    Args:
        request: The incoming request, carrying the session the API call
            needs.
        notice: One line reporting what an action just did.
        problem: A ``fix``/``output`` mapping when an action was refused.
        form: Field values to keep on the form, so a refusal does not empty
            what the operator typed.

    Returns:
        The fragment context.
    """
    from wasm.web.api import cron as api

    listing = api.list_jobs(_session(request))
    apps = sorted(row["domain"] for row in resources.resource_rows("apps"))
    defaults = {
        "name": "",
        "command": "",
        "schedule": "daily",
        "on_calendar": "",
        "user": "",
        "working_directory": "",
        "app_domain": "",
    }
    defaults.update(form or {})
    return {
        "jobs": [_shape_job(info) for info in listing.jobs],
        "apps": apps,
        "presets": [*SCHEDULE_ALIASES.items()],
        "notice": notice,
        "problem": problem,
        "form": defaults,
    }


def _section_fragment(request: Request, **kwargs: Any) -> HTMLResponse:
    """
    Render the cron jobs section for an htmx swap.

    Args:
        request: The incoming request.
        **kwargs: Forwarded to :func:`_section_context`.

    Returns:
        The rendered fragment.
    """
    return page(request, "fragments/cron_section.html", _section_context(request, **kwargs))


@router.get("/cron", response_class=HTMLResponse)
def cron_page(request: Request) -> HTMLResponse:
    """
    Render the cron jobs screen.

    Args:
        request: The incoming request.

    Returns:
        The page. The section itself loads over htmx, so the first paint does
        not wait on two systemctl calls per job.
    """
    return page(request, "pages/cron.html", {})


@router.get("/cron/jobs", response_class=HTMLResponse)
def jobs_section(request: Request) -> HTMLResponse:
    """
    Render the cron jobs section the page loads over htmx.

    Args:
        request: The incoming request.

    Returns:
        The section: the jobs systemd knows about, and the form that creates
        one.
    """
    return _section_fragment(request)


@router.post("/cron/jobs", response_class=HTMLResponse)
def job_create(request: Request, body: bytes = Body(default=b"")) -> HTMLResponse:
    """
    Create a cron job from the inline form.

    Args:
        request: The incoming request.
        body: The urlencoded form: name, command line, a preset or ``custom``,
            the custom OnCalendar expression, and the optional user, directory
            and application.

    Returns:
        The section with the new job listed, or with the refusal inline and
        the operator's input preserved.
    """
    from wasm.web.api.cron import CreateCronJobRequest, create_job

    fields = _form_fields(body)
    schedule = fields.get("schedule", "daily")
    if schedule == CUSTOM_SCHEDULE:
        schedule = fields.get("on_calendar", "")
    redisplay = {
        "name": fields.get("name", ""),
        "command": fields.get("command", ""),
        "schedule": fields.get("schedule", "daily"),
        "on_calendar": fields.get("on_calendar", ""),
        "user": fields.get("user", ""),
        "working_directory": fields.get("working_directory", ""),
        "app_domain": fields.get("app_domain", ""),
    }

    try:
        model = CreateCronJobRequest(
            name=fields.get("name", ""),
            command=fields.get("command", ""),
            schedule=schedule,
            user=fields.get("user") or None,
            working_directory=fields.get("working_directory") or None,
            app_domain=fields.get("app_domain") or None,
        )
    except ValidationError as exc:
        return _section_fragment(
            request,
            problem={"fix": "Check the form.", "output": str(exc)},
            form=redisplay,
        )

    try:
        outcome = create_job(model, _session(request))
    except WASMError as exc:
        return _section_fragment(request, problem=_refusal(exc), form=redisplay)
    return _section_fragment(request, notice=outcome.message)


@router.post("/cron/jobs/{name}/run", response_class=HTMLResponse)
def job_run(name: str, request: Request) -> HTMLResponse:
    """
    Start a job now and redraw the section.

    Args:
        name: Job name.
        request: The incoming request.

    Returns:
        The section with the start reported, or with the refusal inline.
    """
    from wasm.web.api.cron import run_job

    try:
        outcome = run_job(name, _session(request))
    except WASMError as exc:
        return _section_fragment(request, problem=_refusal(exc))
    return _section_fragment(request, notice=outcome.message)


@router.post("/cron/jobs/{name}/enable", response_class=HTMLResponse)
def job_enable(name: str, request: Request) -> HTMLResponse:
    """
    Enable a job's timer and redraw the section.

    Args:
        name: Job name.
        request: The incoming request.

    Returns:
        The section in its new state, or with the refusal inline.
    """
    from wasm.web.api.cron import enable_job

    try:
        outcome = enable_job(name, _session(request))
    except WASMError as exc:
        return _section_fragment(request, problem=_refusal(exc))
    return _section_fragment(request, notice=outcome.message)


@router.post("/cron/jobs/{name}/disable", response_class=HTMLResponse)
def job_disable(name: str, request: Request) -> HTMLResponse:
    """
    Disable a job's timer and redraw the section.

    Args:
        name: Job name.
        request: The incoming request.

    Returns:
        The section in its new state, or with the refusal inline.
    """
    from wasm.web.api.cron import disable_job

    try:
        outcome = disable_job(name, _session(request))
    except WASMError as exc:
        return _section_fragment(request, problem=_refusal(exc))
    return _section_fragment(request, notice=outcome.message)


@router.post("/cron/jobs/{name}/delete", response_class=HTMLResponse)
def job_delete(name: str, request: Request, body: bytes = Body(default=b"")) -> HTMLResponse:
    """
    Delete a job, on presentation of its exact name typed back.

    The comparison happens here, server-side. A mismatch answers 200 with the
    refusal inline and deletes nothing: htmx does not swap an error status,
    and a frozen screen that reports nothing is how an operator clicks again.

    Args:
        name: Job name.
        request: The incoming request.
        body: The urlencoded form carrying the typed confirmation.

    Returns:
        The section without the job, or with the mismatch refused inline.
    """
    from wasm.web.api.cron import delete_job

    typed = _form_fields(body).get("confirm", "")
    if typed != name:
        return _section_fragment(
            request,
            problem={
                "fix": (
                    f"Nothing was deleted. Deleting '{name}' removes its timer and its "
                    "service unit, so it only proceeds when the exact name is typed; "
                    f"'{typed}' does not match."
                ),
                "output": "",
            },
        )

    try:
        outcome = delete_job(name, _session(request))
    except HTTPException as exc:
        return _section_fragment(request, problem={"fix": str(exc.detail), "output": ""})
    except WASMError as exc:
        return _section_fragment(request, problem=_refusal(exc))
    return _section_fragment(request, notice=outcome.message)


@router.get("/cron/jobs/{name}/runs", response_class=HTMLResponse)
def job_runs(name: str, request: Request) -> HTMLResponse:
    """
    Render one job's execution history for an htmx swap.

    Args:
        name: Job name.
        request: The incoming request.

    Returns:
        The runs, newest first, each with its exit code and its own output
        verbatim in mono. A refusal renders inline at 200, in the fragment's
        own place.
    """
    from wasm.web.api.cron import job_runs as api_runs

    dom_id = _ID_UNSAFE.sub("-", name)
    try:
        history = api_runs(name, _session(request))
    except WASMError as exc:
        return page(
            request,
            "fragments/cron_runs.html",
            {"name": name, "dom_id": dom_id, "runs": [], "problem": _refusal(exc)},
        )
    return page(
        request,
        "fragments/cron_runs.html",
        {"name": name, "dom_id": dom_id, "runs": history.runs, "problem": None},
    )
