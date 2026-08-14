"""
What every endpoint in this package needs before it may call a manager.

Two things live here because they used to be repeated, inconsistently, in every
module of the API:

- **The error boundary.** Managers raise :class:`~wasm.core.exceptions.WASMError`
  subclasses carrying an actionable message. Each handler used to wrap its
  manager call in ``try/except Exception`` and answer 500, which is how a
  rejected domain name and a dead certbot ended up as the same HTTP status. The
  translation is stated once, as a route class every router installs, so a
  handler can simply let the error propagate.
- **Strict identifier checks.** The panel runs as root, so a name arriving in a
  path segment or a JSON body is validated with :mod:`wasm.validators.names`
  and :mod:`wasm.validators.domain` before it becomes a path, a unit name or
  SQL. Validation that only normalises is not enough here: ``sub/dir`` must be
  refused, not silently turned into ``sub``.

The route class exists because a router cannot register an exception handler in
FastAPI; only an application can. :func:`install_error_handlers` registers the
same translation on the application for anything raised outside a route, and
the two are deliberately the same function.
"""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from typing import Any

from fastapi import Depends, FastAPI, Request, Response
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, Field

from wasm.core.exceptions import (
    ConfigError,
    DatabaseExistsError,
    DatabaseNotFoundError,
    DomainError,
    SecurityError,
    ValidationError,
    WASMError,
)
from wasm.core.exceptions import (
    PermissionError as WASMPermissionError,
)
from wasm.validators.domain import validate_domain
from wasm.web.auth import SCOPE_RANK, ensure_scope, require_auth

#: Status used for a WASMError with no more specific mapping. A manager that
#: raises anything else is reporting that the operation failed on the server,
#: not that the request was malformed.
DEFAULT_ERROR_STATUS = 500

#: HTTP status per error class, most specific first: the first entry the
#: exception is an instance of wins, so a subclass must be listed before its
#: base class.
_STATUS_BY_ERROR: tuple[tuple[type[WASMError], int], ...] = (
    (DatabaseNotFoundError, 404),
    (DatabaseExistsError, 409),
    (SecurityError, 400),
    (ValidationError, 400),
    (DomainError, 400),
    (ConfigError, 400),
    (WASMPermissionError, 403),
)


class ErrorResponse(BaseModel):
    """
    Body of any failed API call.

    Attributes:
        detail: What went wrong. Named ``detail`` so the shape matches
            FastAPI's own ``HTTPException`` responses and clients need one
            code path.
        hint: How to fix it, when the manager supplied one.
        error: Name of the error class, for clients that branch on it.
    """

    detail: str
    hint: str | None = None
    error: str


class JobAcceptedResponse(BaseModel):
    """
    Body of a long operation that was handed to the job manager.

    Attributes:
        job_id: Identifier to poll or subscribe to.
        status: Job status at the moment the request returned.
        message: Human-readable summary.
        job: Full job snapshot, the same shape the jobs API returns.
    """

    job_id: str
    status: str
    message: str
    job: dict[str, Any] = Field(default_factory=dict)


def status_for(exc: WASMError) -> int:
    """
    Map a WASM error onto an HTTP status.

    Args:
        exc: The raised error.

    Returns:
        The status code to answer with.
    """
    for error_type, status in _STATUS_BY_ERROR:
        if isinstance(exc, error_type):
            return status
    return DEFAULT_ERROR_STATUS


def error_response(exc: WASMError) -> JSONResponse:
    """
    Render a WASM error as the API's error body.

    Args:
        exc: The raised error.

    Returns:
        The JSON response, with the status implied by the error class.
    """
    # WASMError defaults ``details`` to an empty string; an empty hint is no
    # hint, and the client should not have to know the difference.
    return JSONResponse(
        status_code=status_for(exc),
        content=ErrorResponse(
            detail=str(exc),
            hint=getattr(exc, "details", None) or None,
            error=type(exc).__name__,
        ).model_dump(),
    )


class WASMErrorRoute(APIRoute):
    """
    Route that answers a :class:`WASMError` instead of crashing on it.

    Every router in this package is built with ``route_class=WASMErrorRoute``,
    which is the only way to attach an error boundary to a router rather than
    to the whole application.
    """

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        """
        Wrap the generated handler in the API's error boundary.

        Returns:
            The wrapped handler.
        """
        handler = super().get_route_handler()

        async def wrapped(request: Request) -> Response:
            try:
                return await handler(request)
            except WASMError as exc:
                return error_response(exc)

        return wrapped


def install_error_handlers(app: FastAPI) -> None:
    """
    Register the same translation for errors raised outside a route.

    Args:
        app: The application to register the handler on.
    """

    async def handle(request: Request, exc: Exception) -> Response:
        """
        Render a WASM error, re-raising anything else.

        Args:
            request: The request being served. Unused; Starlette's handler
                signature requires it.
            exc: The exception Starlette caught.

        Returns:
            The error response.

        Raises:
            Exception: The original exception, when it is not a WASM error.
                Starlette types handlers against ``Exception``, so this
                narrowing is the handler's own guard rather than an assertion.
        """
        if not isinstance(exc, WASMError):
            raise exc
        return error_response(exc)

    app.add_exception_handler(WASMError, handle)


def require_scope(scope: str) -> Callable[..., Coroutine[Any, Any, dict[str, Any]]]:
    """
    Build a dependency that demands a minimum credential scope.

    The blanket policy already runs where the credential is resolved -
    :func:`wasm.web.auth.required_scope` at the ``require_auth`` chokepoint -
    so most endpoints declare nothing. This is for the ones whose need is
    stricter than the method implies: listing the API tokens is a GET, and a
    ``read`` token must still not see it. It can only tighten; the chokepoint
    has already enforced the floor by the time this runs.

    Args:
        scope: The minimum scope, one of ``read``, ``deploy`` or ``admin``.

    Returns:
        A dependency that yields the session payload, exactly as
        ``require_auth`` does, so it can replace it in an endpoint signature.

    Raises:
        ValueError: When the scope is not a scope. At import time, on purpose:
            a typo here must fail the module, not silently guard nothing.
    """
    if scope not in SCOPE_RANK:
        raise ValueError(f"Unknown scope {scope!r}; use one of {sorted(SCOPE_RANK)}")

    async def dependency(
        request: Request, session: dict[str, Any] = Depends(require_auth)
    ) -> dict[str, Any]:
        """
        Args:
            request: The incoming request.
            session: The authenticated session payload.

        Returns:
            The session payload.

        Raises:
            HTTPException: 403 when the credential's scope is below ``scope``.
        """
        ensure_scope(request, session, scope)
        return session

    return dependency


def strict_domain(value: str) -> str:
    """
    Validate a domain that is about to become a file name or a certificate name.

    :func:`wasm.validators.domain.validate_domain` normalises as well as
    validates: it strips a scheme, a port and everything after the first ``/``,
    so ``evil/../..`` would come back as ``evil`` and the caller would act on a
    resource the client never named. Anything that changes here beyond case and
    surrounding whitespace is therefore refused.

    Args:
        value: Domain exactly as it arrived from the client.

    Returns:
        The validated, lowercased domain.

    Raises:
        DomainError: When the value is not a domain, or is a domain only after
            characters were removed from it.
    """
    normalised = validate_domain(value)
    if normalised != value.strip().lower():
        raise DomainError(
            f"Invalid domain: {value!r}",
            details=(
                "Send the bare domain name. Schemes, ports, paths and '..' "
                "segments are not accepted here."
            ),
        )
    return normalised
