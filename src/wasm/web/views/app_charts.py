# Copyright (c) 2024-2026 Yago Lopez Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
Per-application resource charts, kept out of ``views/router.py``.

Own module so concurrent panel work never edits the aggregate router: it
includes this one at the bottom of that file. The one route here renders the
application page's "Resources" section for its htmx load, like the webhook
and deployment history sections around it, so the page handler does not have
to know the section exists.

The fragment is two ``data-chart`` containers naming the collector's
per-application metrics - panel.js resolves the spec from the name and owns
the drawing - plus the domain's deployment history as a JSON attribute, which
the client paints as vertical marks so a step in the CPU trace can be read
against the deploy that caused it.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from wasm.web.views.rendering import page
from wasm.web.views.router import PageErrorRoute, require_page_session

router: APIRouter = APIRouter(
    include_in_schema=False,
    dependencies=[Depends(require_page_session)],
    route_class=PageErrorRoute,
)

#: Deployment outcomes drawn as marks, mapped onto the state tones the client
#: strokes them with. Queued and running attempts are not marks: a mark is a
#: moment the machine changed, and an attempt still in flight has not changed
#: anything yet. A rolled back build keeps its original outcome mark; the
#: rollback itself lands as the success mark of the restoring deployment.
_MARK_STATES: dict[str, str] = {"success": "active", "failed": "failed"}

#: How many history rows are read for marks. Matches the store's own rotation
#: depth, so the marks cover exactly the history that still exists.
_MARK_LIMIT = 20


def _deploy_marks(domain: str) -> list[dict[str, Any]]:
    """
    Read one domain's deployment history as chart marks.

    Args:
        domain: The application's domain.

    Returns:
        Oldest-first ``{"ts": epoch_seconds, "state": tone}`` mappings, one
        per finished attempt. The moment is when the outcome landed -
        ``finished_at`` - because that is when the machine changed; a row
        whose timestamps cannot be read is skipped rather than guessed at.
    """
    from wasm.core.store import get_store

    marks: list[dict[str, Any]] = []
    for record in get_store().list_deployments(domain=domain, limit=_MARK_LIMIT):
        state = _MARK_STATES.get(record.status)
        stamp = record.finished_at or record.started_at
        if state is None or not stamp:
            continue
        try:
            moment = datetime.fromisoformat(stamp)
        except ValueError:
            continue
        marks.append({"ts": moment.timestamp(), "state": state})
    marks.reverse()
    return marks


@router.get("/apps/{domain}/charts", response_class=HTMLResponse)
def app_charts(domain: str, request: Request) -> HTMLResponse:
    """
    Render the application page's resource charts, for its htmx load.

    The fragment renders whether or not the domain is currently deployed,
    like the deployment history it annotates: an empty chart is a normal
    state, not a missing resource.

    Args:
        domain: The application's domain.
        request: The incoming request.

    Returns:
        The rendered fragment.
    """
    return page(
        request,
        "fragments/app_charts.html",
        {"charts_domain": domain, "chart_marks": _deploy_marks(domain)},
    )
