"""
Deployment history pages.

Kept as its own module so panel work on deployment history never has to edit
``views/router.py``: the aggregate router includes this one at the bottom of
that file. Handlers here follow the same contract as the rest of the views:
synchronous, session-guarded, rendering Jinja fragments over the managers.

Three surfaces live here:

- The history pages: every attempt on the machine at ``/deployments``, one
  domain's at ``/apps/{domain}/deployments``, and the detail at
  ``/deployments/{id}`` with the captured build log verbatim.
- The application page's own history section, loaded over htmx so the page
  handler in ``views/router.py`` does not have to know this section exists.
- The rollback section: the points an application can return to, and the
  confirmation that queues the existing rollback job. The confirmation asks
  the operator to type the domain, checked server-side: a wrong name renders
  the refusal inline at 200, because htmx does not swap an error status.

The history deliberately outlives the application it describes: the record of
a deleted app matters most at exactly the moment the app is gone, so a domain
with rows but no app still gets its page rather than a 404.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse

from wasm.core.exceptions import SecurityError, ValidationError, WASMError
from wasm.validators.names import resolve_within

if TYPE_CHECKING:
    from wasm.core.store import DeploymentRecord
from wasm.web.views.rendering import duration, filesize, page, since
from wasm.web.views.router import PageErrorRoute, _form_fields, require_page_session

log = logging.getLogger(__name__)

# Annotated explicitly: this module and router.py import each other, and inside
# that cycle mypy cannot infer the type of a module-level variable another
# module reads.
router: APIRouter = APIRouter(
    include_in_schema=False,
    dependencies=[Depends(require_page_session)],
    route_class=PageErrorRoute,
)

#: Deployment status mapped onto the panel's four-word vocabulary. Queued and
#: running are both amber: from the operator's side each means "this machine is
#: mid-change". A rolled back build is grey, not red - it is not failing, it
#: has simply stopped serving.
_DEPLOYMENT_STATES: dict[str, str] = {
    "queued": "busy",
    "running": "busy",
    "success": "active",
    "failed": "failed",
    "rolled_back": "idle",
}

#: How many deployments an application's own page lists. The full history is
#: one click away on the domain's history page.
APP_DEPLOYMENTS = 5

#: How much of a large captured log the detail page shows. The full file stays
#: on disk; the tail is where a build failure speaks.
LOG_TAIL_BYTES = 512 * 1024


def _history(domain: str | None, limit: int = 50) -> list[DeploymentRecord]:
    """
    Read deployment history from the store, newest first.

    Args:
        domain: Only this domain's history; the whole machine's when None.
        limit: Most rows to return.

    Returns:
        The records.
    """
    from wasm.core.store import get_store

    return get_store().list_deployments(domain=domain, limit=limit)


def _shape_deployment(record: DeploymentRecord) -> dict[str, Any]:
    """
    Turn a deployment record into a row context.

    Args:
        record: The stored history row.

    Returns:
        A row context. For a failed attempt the note carries the first line of
        the error verbatim, so the list already says what broke; the detail
        page holds the whole message and the captured log.
    """
    note = None
    if record.error and record.error.strip():
        note = record.error.strip().splitlines()[0][:160]
    return {
        "id": record.id,
        "domain": record.domain,
        "state": _DEPLOYMENT_STATES.get(record.status, "idle"),
        "status": record.status.replace("_", " "),
        "trigger": record.triggered_by,
        "commit": record.git_commit or "—",
        "duration": duration(record.duration_s),
        "started": since(record.started_at),
        "note": note,
        "href": f"/deployments/{record.id}",
    }


@router.get("/deployments", response_class=HTMLResponse)
def deployments(request: Request) -> HTMLResponse:
    """
    Render the machine's deployment history, every domain together.

    Args:
        request: The incoming request.

    Returns:
        The history page.
    """
    rows = [_shape_deployment(record) for record in _history(None)]
    return page(
        request,
        "pages/deployments.html",
        {
            "title": "Deployments",
            "subtitle": f"{len(rows)} recorded on this machine" if rows else None,
            "deploy_rows": rows,
            "history_domain": None,
        },
    )


@router.get("/apps/{domain}/deployments", response_class=HTMLResponse)
def app_deployments(domain: str, request: Request) -> HTMLResponse:
    """
    Render one domain's deployment history.

    The page renders whether or not the domain is currently deployed: the
    history outlives the application on purpose, and answering 404 about rows
    that exist would be telling the operator their record is gone when it is
    not.

    Args:
        domain: The domain whose history is asked for.
        request: The incoming request.

    Returns:
        The history page, filtered to the domain.
    """
    rows = [_shape_deployment(record) for record in _history(domain)]
    return page(
        request,
        "pages/deployments.html",
        {
            "title": "Deployments",
            "subtitle": f"{len(rows)} recorded for {domain}" if rows else domain,
            "deploy_rows": rows,
            "history_domain": domain,
        },
    )


@router.get("/apps/{domain}/deployments/recent", response_class=HTMLResponse)
def recent_deployments(domain: str, request: Request) -> HTMLResponse:
    """
    Render the application page's history section, for its htmx load.

    Args:
        domain: The application's domain.
        request: The incoming request.

    Returns:
        The rendered fragment, holding the most recent attempts.
    """
    rows = [_shape_deployment(record) for record in _history(domain, limit=APP_DEPLOYMENTS)]
    return page(
        request,
        "fragments/deploy_recent.html",
        {"deploy_rows": rows, "history_domain": domain},
    )


def _read_log(record: DeploymentRecord) -> tuple[str | None, str | None, bool]:
    """
    Read a deployment's captured build log from disk.

    The stored path is data, not an instruction: it is only followed when it
    resolves inside the deployment log directory next to the store's own
    database, which is where the recorder writes. A row pointing anywhere else
    is refused and reported, never read.

    Args:
        record: The history row whose log is being read.

    Returns:
        ``(text, absence, truncated)``. Exactly one of ``text`` and
        ``absence`` is set: the log verbatim, or the honest reason there is
        nothing to show. ``truncated`` says the text is the tail of a larger
        file.
    """
    if not record.log_path:
        return None, "No build log was captured for this deployment.", False

    from wasm.core.store import get_store

    root = get_store().db_path.parent / "deploy-logs"
    try:
        path = resolve_within(root, record.log_path)
    except (SecurityError, ValidationError):
        log.warning(
            "Deployment %s records a log path outside %s; refusing to read it",
            record.id,
            root,
        )
        return (
            None,
            "The recorded log path is not under the deployment log directory, "
            "so the panel will not read it.",
            False,
        )

    if not path.is_file():
        return None, "The captured log is no longer on disk.", False

    try:
        # read_bytes rather than an open() of our own: the presentation layer
        # is audited against touching the filesystem, and a whole-name check
        # cannot tell a read-only handle from a writable one. Logs are rotated
        # at twenty per application, so the whole file fits in memory.
        data = path.read_bytes()
    except OSError as exc:
        log.warning("Could not read the captured log for deployment %s: %s", record.id, exc)
        return None, f"The captured log could not be read: {exc}", False

    if len(data) > LOG_TAIL_BYTES:
        text = data[-LOG_TAIL_BYTES:].decode("utf-8", errors="replace")
        # Drop the partial line the cut landed inside.
        return text.split("\n", 1)[-1], None, True
    return data.decode("utf-8", errors="replace"), None, False


@router.get("/deployments/{deployment_id}", response_class=HTMLResponse)
def deployment_detail(deployment_id: int, request: Request) -> HTMLResponse:
    """
    Render one deployment: the facts, the failure and the captured log.

    Args:
        deployment_id: The history row asked for.
        request: The incoming request.

    Returns:
        The detail page, or a 404 page naming the id that is not recorded.
    """
    from wasm.core.store import get_store

    record = get_store().get_deployment(deployment_id)
    if record is None:
        return page(
            request,
            "pages/missing.html",
            {
                "section": "Deployments",
                "title": "No such deployment",
                "body": f"No deployment {deployment_id} is recorded on this machine.",
                "command": "wasm list",
            },
            status_code=404,
        )

    log_text, log_absence, log_truncated = _read_log(record)
    detail = _shape_deployment(record)
    detail["branch"] = record.git_branch or "—"
    detail["log_path"] = record.log_path

    facts = [
        ("domain", record.domain),
        ("status", detail["status"]),
        ("trigger", record.triggered_by),
        ("commit", record.git_commit or "—"),
        ("branch", record.git_branch or "—"),
        ("started", since(record.started_at)),
        ("finished", since(record.finished_at)),
        ("duration", duration(record.duration_s)),
        ("log", record.log_path or "—"),
    ]

    return page(
        request,
        "pages/deployment_detail.html",
        {
            "deployment": detail,
            "facts": facts,
            "error": record.error,
            "log_text": log_text,
            "log_absence": log_absence,
            "log_truncated": log_truncated,
            "history_href": f"/apps/{record.domain}/deployments",
        },
    )


# Rollback ------------------------------------------------------------------


def _shape_rollback_point(record: Any) -> dict[str, Any]:
    """
    Turn backup metadata into a rollback point context.

    Args:
        record: A :class:`~wasm.managers.backup_manager.BackupMetadata`.

    Returns:
        The row context.
    """
    return {
        "id": record.id,
        "taken": since(record.created_at),
        "size": filesize(record.size_bytes),
        "tags": ", ".join(record.tags) if record.tags else "—",
        "note": record.description or record.id,
    }


def _rollback_context(
    domain: str,
    *,
    mode: str = "list",
    confirm_id: str | None = None,
    problem: dict[str, str] | None = None,
    queued: str | None = None,
) -> dict[str, Any]:
    """
    Build the context the rollback fragment renders from.

    The points come from :meth:`RollbackManager.list_rollback_points`, the
    same call the CLI's ``wasm backup rollback --list`` makes: there is one
    implementation of "what can this application return to" and this is a view
    of it.

    Args:
        domain: The application's domain.
        mode: ``"list"`` for the points, ``"confirm"`` for the type-the-domain
            form over one of them.
        confirm_id: The point being confirmed, when ``mode`` is ``"confirm"``.
        problem: A ``fix``/``output`` mapping when a submission was refused.
        queued: The queued job's name, shown once after a rollback is accepted.

    Returns:
        The fragment context. A confirmation for a point that no longer
        exists falls back to the list with the reason inline, rather than
        offering a rollback to an archive that is gone.
    """
    from wasm.managers.backup_manager import RollbackManager

    points = [
        _shape_rollback_point(record)
        for record in RollbackManager(verbose=False).list_rollback_points(domain)
    ]
    confirm = next((point for point in points if point["id"] == confirm_id), None)
    if mode == "confirm" and confirm is None:
        mode = "list"
        problem = problem or {
            "fix": "That backup is no longer available. The list below is current.",
            "output": "",
        }
    return {
        "rollback_domain": domain,
        "rollback_mode": mode,
        "rollback_points": points,
        "rollback_confirm": confirm,
        "rollback_problem": problem,
        "rollback_queued": queued,
    }


def _rollback_fragment(request: Request, domain: str, **kwargs: Any) -> HTMLResponse:
    """
    Render the rollback section for an htmx swap.

    Refusals render at 200 on purpose: htmx does not swap an error status, so
    a 400 here would leave the screen frozen and report the refusal nowhere.

    Args:
        request: The incoming request.
        domain: The application's domain.
        **kwargs: Forwarded to :func:`_rollback_context`.

    Returns:
        The rendered fragment.
    """
    return page(request, "fragments/deploy_rollback.html", _rollback_context(domain, **kwargs))


@router.get("/apps/{domain}/rollback/section", response_class=HTMLResponse)
def rollback_section(domain: str, request: Request) -> HTMLResponse:
    """
    Render the application page's rollback section, for its htmx load.

    Args:
        domain: The application's domain.
        request: The incoming request.

    Returns:
        The fragment, listing the points the application can return to.
    """
    return _rollback_fragment(request, domain)


@router.get("/apps/{domain}/rollback/confirm/{backup_id}", response_class=HTMLResponse)
def rollback_confirm(domain: str, backup_id: str, request: Request) -> HTMLResponse:
    """
    Render the confirmation for one rollback point.

    Args:
        domain: The application's domain.
        backup_id: The point the operator picked.
        request: The incoming request.

    Returns:
        The fragment, showing the type-the-domain form.
    """
    return _rollback_fragment(request, domain, mode="confirm", confirm_id=backup_id)


@router.post("/apps/{domain}/rollback", response_class=HTMLResponse)
def rollback_submit(domain: str, request: Request, body: bytes = Body(default=b"")) -> HTMLResponse:
    """
    Queue a rollback once the operator has typed the domain to confirm.

    An adapter over the JSON API, like the deploy form and the environment
    editor: the validation, the queueing and the job itself live in
    :func:`wasm.web.api.jobs.create_rollback_job`, and a second implementation
    of "roll an application back" is what the panel must never grow. Progress
    is reported by the feed and the activity screen, like every queued job.

    The name check is done here, server-side, because it is this form's own
    contract: the confirmation is not a browser dialog that scripts and habit
    click through, it is the operator writing down which application they are
    about to overwrite.

    Args:
        domain: The application to roll back.
        request: The incoming request.
        body: The urlencoded form: the backup to restore and the typed name.

    Returns:
        The fragment with the queued notice, or the confirmation again with
        the refusal inline and nothing changed.
    """
    from wasm.web.api.jobs import RollbackRequest, create_rollback_job

    fields = _form_fields(body)
    backup_id = fields.get("backup_id", "")
    typed = fields.get("confirm", "")
    session = getattr(request.state, "session", {})

    if typed != domain:
        return _rollback_fragment(
            request,
            domain,
            mode="confirm",
            confirm_id=backup_id,
            problem={
                "fix": (f"Type {domain} exactly to confirm the rollback. Nothing was changed."),
                "output": "",
            },
        )

    try:
        created = create_rollback_job(
            RollbackRequest(domain=domain, backup_id=backup_id or None), session
        )
    except HTTPException as exc:
        return _rollback_fragment(
            request,
            domain,
            mode="confirm",
            confirm_id=backup_id,
            problem={"fix": str(exc.detail), "output": ""},
        )
    except WASMError as exc:
        return _rollback_fragment(
            request,
            domain,
            mode="confirm",
            confirm_id=backup_id,
            problem={"fix": str(exc), "output": getattr(exc, "details", "") or ""},
        )

    return _rollback_fragment(request, domain, queued=created.job.name)
