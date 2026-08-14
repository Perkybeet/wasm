"""
User cron job pages, kept out of ``views/router.py``.

Own module so concurrent panel work never edits the aggregate router: it
includes this one at the bottom of that file. Handlers follow the same
contract as the rest of the views: synchronous, session-guarded, rendering
Jinja fragments over the managers.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from wasm.web.views.router import PageErrorRoute, require_page_session

router: APIRouter = APIRouter(
    include_in_schema=False,
    dependencies=[Depends(require_page_session)],
    route_class=PageErrorRoute,
)
