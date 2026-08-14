"""
Database management pages.

Kept as its own module so panel work on databases never has to edit
``views/router.py``: the aggregate router includes this one at the bottom of
that file. Handlers here follow the same contract as the rest of the views:
synchronous, session-guarded, rendering Jinja fragments over the managers.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from wasm.web.views.router import PageErrorRoute, require_page_session

router = APIRouter(
    include_in_schema=False,
    dependencies=[Depends(require_page_session)],
    route_class=PageErrorRoute,
)
