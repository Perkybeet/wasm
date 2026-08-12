"""
Authentication endpoints.

Logging in sets a ``HttpOnly`` session cookie plus a readable CSRF cookie. The
session token is only returned in the response body when the caller explicitly
asks for it (``bearer: true``), which is what the CLI and automation do; the
browser never needs it, and a token the browser cannot read is a token an XSS
bug cannot steal.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel

from wasm.web.auth import (
    CSRF_COOKIE_NAME,
    SESSION_COOKIE_NAME,
    IssuedSession,
    get_audit_logger,
    get_client_ip,
    is_secure_request,
    require_auth,
)
from wasm.web.server import get_brute_force, get_token_manager

router = APIRouter()

#: Historical name of the auth dependency. Kept as an alias so the rest of the
#: API keeps working while there is exactly one implementation.
get_current_session = require_auth


class LoginRequest(BaseModel):
    """
    Login request body.

    Attributes:
        token: The master access token.
        bearer: Whether to also return the session token in the response, for
            clients without a cookie jar.
    """

    token: str
    bearer: bool = False


class LoginResponse(BaseModel):
    """
    Login response body.

    Attributes:
        success: Always true when the request succeeded.
        expires_in: Session lifetime in seconds.
        csrf_token: Token to echo in the ``X-WASM-CSRF`` header on mutations.
        session_token: Session token, only present for ``bearer`` clients.
    """

    success: bool
    expires_in: int
    csrf_token: str
    session_token: str | None = None


class TokenInfo(BaseModel):
    """
    Session information.

    Attributes:
        valid: Whether the session is usable.
        expires_at: Expiry as a UNIX timestamp.
        session_id: Server-side session identifier.
    """

    valid: bool
    expires_at: float | None = None
    session_id: str | None = None


class WebSocketTicket(BaseModel):
    """
    Single-use credential for opening a WebSocket.

    Attributes:
        ticket: The ticket value.
        expires_in: Ticket lifetime in seconds.
    """

    ticket: str
    expires_in: int


def set_session_cookies(response: Response, session: IssuedSession, secure: bool) -> None:
    """
    Attach the session and CSRF cookies to a response.

    Args:
        response: The response being returned to the browser.
        session: The issued session.
        secure: Whether to mark the cookies ``Secure``.
    """
    response.set_cookie(
        SESSION_COOKIE_NAME,
        session.token,
        max_age=session.max_age,
        httponly=True,
        samesite="strict",
        secure=secure,
        path="/",
    )
    # Readable on purpose: the SPA has to copy it into the CSRF header. Its
    # value is useless without the HttpOnly session cookie.
    response.set_cookie(
        CSRF_COOKIE_NAME,
        session.csrf_token,
        max_age=session.max_age,
        httponly=False,
        samesite="strict",
        secure=secure,
        path="/",
    )


@router.post("/login", response_model=LoginResponse)
async def login(request: Request, response: Response, body: LoginRequest) -> LoginResponse:
    """
    Exchange the master token for a session.

    Args:
        request: The incoming request.
        response: Response used to set the session cookies.
        body: The login payload.

    Returns:
        The login result.

    Raises:
        HTTPException: 401 when the master token is wrong.
    """
    token_manager = get_token_manager()
    brute_force = get_brute_force()
    audit = get_audit_logger()
    client_ip = get_client_ip(request)

    if not token_manager.verify_master_token(body.token):
        brute_force.record_failure(client_ip)
        attempts_remaining = brute_force.get_attempts_remaining(client_ip)
        if audit:
            audit.record(
                action="auth.login",
                result="failure",
                client_ip=client_ip,
                resource="/api/auth/login",
                detail=f"invalid master token, {attempts_remaining} attempts remaining",
            )
        raise HTTPException(
            status_code=401, detail=f"Invalid token. {attempts_remaining} attempts remaining."
        )

    brute_force.record_success(client_ip)
    session = token_manager.create_session(client_ip)
    set_session_cookies(response, session, secure=is_secure_request(request))

    if audit:
        audit.record(
            action="auth.login",
            result="success",
            client_ip=client_ip,
            actor=session.session_id,
            resource="/api/auth/login",
        )

    return LoginResponse(
        success=True,
        expires_in=session.max_age,
        csrf_token=session.csrf_token,
        session_token=session.token if body.bearer else None,
    )


@router.post("/logout")
async def logout(
    request: Request, response: Response, session: dict[str, Any] = Depends(require_auth)
) -> dict[str, Any]:
    """
    Revoke the current session and clear its cookies.

    Args:
        request: The incoming request.
        response: Response used to clear the cookies.
        session: The authenticated session.

    Returns:
        A confirmation payload.
    """
    token_manager = get_token_manager()
    audit = get_audit_logger()
    session_id = session.get("sid")

    if session_id and session.get("type") == "session":
        token_manager.revoke_session(session_id)

    response.delete_cookie(SESSION_COOKIE_NAME, path="/")
    response.delete_cookie(CSRF_COOKIE_NAME, path="/")

    if audit:
        audit.record(
            action="auth.logout",
            result="success",
            client_ip=get_client_ip(request),
            actor=str(session_id),
            resource="/api/auth/logout",
        )

    return {"success": True, "message": "Logged out successfully"}


@router.get("/verify", response_model=TokenInfo)
async def verify_token(session: dict[str, Any] = Depends(require_auth)) -> TokenInfo:
    """
    Report whether the presented credential is still valid.

    Args:
        session: The authenticated session.

    Returns:
        Session information.
    """
    return TokenInfo(
        valid=True,
        expires_at=session.get("expires_at") or session.get("exp"),
        session_id=session.get("sid"),
    )


@router.post("/ws-ticket", response_model=WebSocketTicket)
async def create_ws_ticket(
    request: Request, session: dict[str, Any] = Depends(require_auth)
) -> WebSocketTicket:
    """
    Issue a single-use ticket for opening a WebSocket.

    Browsers cannot set headers on a WebSocket handshake, so clients that
    cannot rely on cookies use this instead of putting a long-lived token in a
    query string that proxies and access logs record.

    Args:
        request: The incoming request.
        session: The authenticated session.

    Returns:
        The ticket and its lifetime.
    """
    token_manager = get_token_manager()
    client_ip = get_client_ip(request)
    ticket, expires_in = token_manager.issue_ws_ticket(str(session.get("sid")), client_ip)

    audit = get_audit_logger()
    if audit:
        audit.record(
            action="auth.ws_ticket",
            result="success",
            client_ip=client_ip,
            actor=str(session.get("sid")),
            resource="/api/auth/ws-ticket",
        )

    return WebSocketTicket(ticket=ticket, expires_in=expires_in)


@router.get("/sessions")
async def get_sessions(session: dict[str, Any] = Depends(require_auth)) -> dict[str, Any]:
    """
    Report how many sessions are active.

    Args:
        session: The authenticated session.

    Returns:
        Active session count and the caller's session id.
    """
    token_manager = get_token_manager()
    return {
        "active_sessions": token_manager.get_active_session_count(),
        "current_session": session.get("sid"),
    }


@router.post("/sessions/revoke-all")
async def revoke_all_sessions(
    request: Request, response: Response, session: dict[str, Any] = Depends(require_auth)
) -> dict[str, Any]:
    """
    Revoke every session, including the caller's.

    Args:
        request: The incoming request.
        response: Response used to clear the caller's cookies.
        session: The authenticated session.

    Returns:
        A confirmation payload.
    """
    token_manager = get_token_manager()
    token_manager.revoke_all_sessions()

    response.delete_cookie(SESSION_COOKIE_NAME, path="/")
    response.delete_cookie(CSRF_COOKIE_NAME, path="/")

    audit = get_audit_logger()
    if audit:
        audit.record(
            action="auth.revoke_all",
            result="success",
            client_ip=get_client_ip(request),
            actor=str(session.get("sid")),
            resource="/api/auth/sessions/revoke-all",
        )

    return {"success": True, "message": "All sessions revoked"}
