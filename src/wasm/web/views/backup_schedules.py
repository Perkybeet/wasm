"""
Panel pages for scheduled backups, kept out of views/router.py.

Own module so concurrent panel work never edits the aggregate router: it
includes this one at the bottom of that file. Handlers follow the same
contract as the rest of the views: synchronous, session-guarded, rendering
Jinja fragments over the managers.

Every handler is an adapter over :mod:`wasm.web.api.backup_schedules`, the
same way the databases screen is an adapter over its API module: the calendar
validation, the unit writing and the audit trail all live there and in the
scheduler it drives. A handler here translates a form into a request model
and a refusal into a fragment, nothing more. Deleting a schedule goes
straight to the API with the row as its target, exactly like every other
Delete button on the backups page.

Refusals render at 200 on purpose: htmx does not swap an error status, so a
400 would leave the screen frozen and report the refusal nowhere. The refusal
is on the fragment itself, as a problem block, which is where the operator is
looking.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Depends, Request
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


def _shape_schedule(info: Any) -> dict[str, Any]:
    """
    Turn one API schedule into the fields the table renders.

    Args:
        info: A :class:`~wasm.web.api.backup_schedules.BackupScheduleInfo`.

    Returns:
        A row context. The endpoint and the confirmation are built here, not
        in the template: a template that concatenates a route can be wrong
        about it, which is how a delete button ends up aimed at nothing.
    """
    retention = "—"
    if info.retention_count is not None:
        retention = f"keep {info.retention_count}, {info.retention_days or '—'} days"
    return {
        "domain": info.domain,
        "schedule": info.schedule,
        "on_calendar": info.on_calendar or "—",
        "retention": retention,
        "next_run": info.next_run,
        "last_run": info.last_run,
        "delete_endpoint": f"/api/backup-schedules/{info.domain}",
        "delete_question": (
            f"Delete the backup schedule for {info.domain}? "
            "Automatic backups stop; the archives already taken are kept."
        ),
    }


def _schedules_context(
    request: Request,
    *,
    notice: str | None = None,
    problem: dict[str, str] | None = None,
    form: dict[str, str] | None = None,
) -> dict[str, Any]:
    """
    Build the context the scheduled backups section renders from.

    The listing is read through the JSON API's own function, so the section
    and a Bearer client can never disagree about what is scheduled.

    Args:
        request: The incoming request, carrying the session the API call
            needs.
        notice: One line reporting what an action just did.
        problem: A ``fix``/``output`` mapping when an action was refused.
        form: Field values to keep on the form, so a refusal does not empty
            what the operator chose.

    Returns:
        The fragment context.
    """
    from wasm.web.api import backup_schedules as api

    listing = api.list_schedules(_session(request))
    apps = sorted(row["domain"] for row in resources.resource_rows("apps"))
    defaults = {"domain": "", "schedule": "daily", "on_calendar": ""}
    defaults.update(form or {})
    return {
        "schedules": [_shape_schedule(info) for info in listing.schedules],
        "apps": apps,
        "presets": [*SCHEDULE_ALIASES.items()],
        "notice": notice,
        "problem": problem,
        "form": defaults,
    }


def _schedules_fragment(request: Request, **kwargs: Any) -> HTMLResponse:
    """
    Render the scheduled backups section for an htmx swap.

    Args:
        request: The incoming request.
        **kwargs: Forwarded to :func:`_schedules_context`.

    Returns:
        The rendered fragment.
    """
    return page(
        request, "fragments/backup_schedule_section.html", _schedules_context(request, **kwargs)
    )


@router.get("/backups/schedules", response_class=HTMLResponse)
def schedules_section(request: Request) -> HTMLResponse:
    """
    Render the scheduled backups section the backups page loads over htmx.

    Loaded lazily so the page handler does not have to know this section
    exists, and so the first paint does not wait on two systemctl calls per
    timer.

    Args:
        request: The incoming request.

    Returns:
        The section: the timers systemd knows about, and the form that
        creates one.
    """
    return _schedules_fragment(request)


@router.post("/backups/schedules", response_class=HTMLResponse)
def schedule_create(request: Request, body: bytes = Body(default=b"")) -> HTMLResponse:
    """
    Create a backup schedule from the inline form.

    Args:
        request: The incoming request.
        body: The urlencoded form: domain, a preset or ``custom``, the custom
            OnCalendar expression, and the retention pair.

    Returns:
        The section with the new timer listed, or with the refusal inline and
        the operator's choices preserved.
    """
    from wasm.web.api.backup_schedules import CreateScheduleRequest, create_schedule

    fields = _form_fields(body)
    schedule = fields.get("schedule", "daily")
    if schedule == CUSTOM_SCHEDULE:
        schedule = fields.get("on_calendar", "")
    redisplay = {
        "domain": fields.get("domain", ""),
        "schedule": fields.get("schedule", "daily"),
        "on_calendar": fields.get("on_calendar", ""),
    }

    try:
        model = CreateScheduleRequest(
            domain=fields.get("domain", ""),
            schedule=schedule,
            retention_count=int(fields.get("retention_count") or 7),
            retention_days=int(fields.get("retention_days") or 30),
        )
    except (ValidationError, ValueError) as exc:
        return _schedules_fragment(
            request,
            problem={"fix": "Check the schedule.", "output": str(exc)},
            form=redisplay,
        )

    try:
        outcome = create_schedule(model, _session(request))
    except WASMError as exc:
        return _schedules_fragment(request, problem=_refusal(exc), form=redisplay)
    return _schedules_fragment(request, notice=outcome.message)
