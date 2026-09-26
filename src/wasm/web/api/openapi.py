# Copyright (c) 2024-2026 Yago Lopez Prado
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
The API's own schema, as JSON.

``FastAPI(openapi_url=None)`` in :mod:`wasm.web.server` keeps the schema off
the unauthenticated ``/openapi.json`` FastAPI would otherwise serve by
default - a map of every endpoint on an API that runs systemd as root is not
something an anonymous caller gets for free. This is the same document,
served instead at ``GET /api/openapi.json`` behind the same session or token
every other endpoint requires.

``scripts/export_openapi.py`` calls ``app.openapi()`` directly rather than
this route, so the committed ``panel/openapi.json`` - the one input of the
console's generated types - does not depend on a server listening anywhere.
This route exists for tooling that only has network access, and for an
operator who wants to see what the panel they are running actually exposes.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, FastAPI, Request

from wasm.web.api.deps import WASMErrorRoute
from wasm.web.auth import require_auth

router = APIRouter(route_class=WASMErrorRoute)


@router.get("/openapi.json", response_model=dict[str, Any])
def get_openapi_schema(
    request: Request, session: dict[str, Any] = Depends(require_auth)
) -> dict[str, Any]:
    """
    Serve the application's own OpenAPI document.

    Args:
        request: The incoming request, used to reach the application that
            owns the route table ``app.openapi()`` walks.
        session: Authenticated session, injected. Any credential that clears
            ``require_auth`` may read this; the schema names endpoints and
            shapes, not secrets.

    Returns:
        The OpenAPI document, the same one ``scripts/export_openapi.py``
        writes to ``panel/openapi.json``.
    """
    # Request.app is typed Any by Starlette - it has no way to know the
    # ASGI app in scope["app"] is a FastAPI and not a bare Starlette. The
    # explicit annotation, not a cast, is what tells mypy .openapi() exists
    # and returns dict[str, Any] rather than Any.
    app: FastAPI = request.app
    return app.openapi()
