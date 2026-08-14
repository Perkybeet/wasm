"""
Authentication endpoints.

Logging in sets a ``HttpOnly`` session cookie plus a readable CSRF cookie. The
session token is only returned in the response body when the caller explicitly
asks for it (``bearer: true``), which is what the CLI and automation do; the
browser never needs it, and a token the browser cannot read is a token an XSS
bug cannot steal.
"""

from __future__ import annotations

import socket
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from wasm.core.totp import provisioning_uri
from wasm.web.api.deps import WASMErrorRoute, require_scope
from wasm.web.auth import (
    CSRF_COOKIE_NAME,
    SESSION_COOKIE_NAME,
    IssuedSession,
    get_audit_logger,
    get_client_ip,
    is_secure_request,
    record_auth_failure,
    require_auth,
)
from wasm.web.server import get_brute_force, get_token_manager

# The error boundary every other API router already has: without it a
# SecurityError from the two-factor manager would crash the route instead of
# answering 400 with the actionable half attached.
router = APIRouter(route_class=WASMErrorRoute)

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
        totp_code: Second factor - a six-digit authenticator code or a backup
            code. Required when two-factor authentication is enabled.
    """

    token: str
    bearer: bool = False
    totp_code: str | None = None


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
        HTTPException: 401 when the master token is wrong, when a required
            second factor is missing, or when the second factor is wrong.
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

    if token_manager.totp_enabled():
        code = (body.totp_code or "").strip()
        if not code:
            # Not counted by the lockout: an absent code is a client that does
            # not know the second factor exists, not a guess at it.
            if audit:
                audit.record(
                    action="auth.login",
                    result="failure",
                    client_ip=client_ip,
                    resource="/api/auth/login",
                    detail="second factor required but not presented",
                )
            raise HTTPException(
                status_code=401,
                detail="Two-factor authentication is enabled. Include totp_code.",
            )
        if not token_manager.verify_second_factor(code):
            # The same chokepoint that counts a bad master token: a wrong
            # second factor is a credential guess, and it must not have its
            # own, softer counter.
            record_auth_failure(client_ip, "/api/auth/login", "totp")
            attempts_remaining = brute_force.get_attempts_remaining(client_ip)
            raise HTTPException(
                status_code=401,
                detail=f"Invalid two-factor code. {attempts_remaining} attempts remaining.",
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
    Report the active sessions.

    Only a truncated prefix of each session id is included: enough to name a
    row for ``DELETE /api/auth/sessions/{sid_prefix}``, useless for forging
    the cookie it belongs to.

    Args:
        session: The authenticated session.

    Returns:
        Active session count, the caller's session id, and one entry per live
        session with its address, birth, last activity and expiry.
    """
    token_manager = get_token_manager()
    current = session.get("sid") if session.get("type") == "session" else None
    return {
        "active_sessions": token_manager.get_active_session_count(),
        "current_session": session.get("sid"),
        "sessions": token_manager.list_sessions(current),
    }


# Synchronous so the settings screen's "Sign out everywhere" adapter can call
# it directly; FastAPI runs it in a threadpool either way. Declared before the
# parametrised sibling, the way every router here orders its routes.
@router.post("/sessions/revoke-all")
def revoke_all_sessions(
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


@router.delete("/sessions/{sid_prefix}")
def revoke_one_session(
    sid_prefix: str, request: Request, session: dict[str, Any] = Depends(require_auth)
) -> dict[str, Any]:
    """
    Revoke exactly one session, named by a unique prefix of its id.

    Synchronous on purpose, like the two-factor handlers: the settings screen
    calls this function directly, so there is one implementation of "revoke a
    session" with one audit trail.

    Args:
        sid_prefix: Leading characters of the session id, as listed by
            ``GET /api/auth/sessions``.
        request: The incoming request.
        session: The authenticated session.

    Returns:
        A confirmation payload naming the revoked prefix.

    Raises:
        HTTPException: 404 when nothing matches the prefix.
        SecurityError: When the prefix is malformed or ambiguous, or names the
            caller's own session - ending the session you are inside is
            sign-out, which also clears the browser's cookies.
    """
    token_manager = get_token_manager()
    protect = session.get("sid") if session.get("type") == "session" else None
    revoked = token_manager.revoke_session_by_prefix(sid_prefix, protect_sid=protect)

    if revoked is None:
        raise HTTPException(
            status_code=404, detail="No active session matches that prefix. It may have expired."
        )

    audit = get_audit_logger()
    if audit:
        audit.record(
            action="auth.session.revoke",
            result="success",
            client_ip=get_client_ip(request),
            actor=str(session.get("sid")),
            resource=f"/api/auth/sessions/{revoked}",
            detail=f"revoked session {revoked}",
        )

    return {"success": True, "revoked": revoked}


class TwoFactorStatus(BaseModel):
    """
    Two-factor state, with no secret in it.

    Attributes:
        enabled: Whether logins require a second factor.
        pending: Whether an enrolment has been begun but not confirmed.
        backup_codes_remaining: Unused single-use backup codes left.
    """

    enabled: bool
    pending: bool
    backup_codes_remaining: int


class TwoFactorEnrollment(BaseModel):
    """
    A begun enrolment. This is the only response that ever carries the secret.

    Attributes:
        secret: The base32 secret, to type into an authenticator app by hand.
        uri: The ``otpauth://`` URI the QR code encodes.
    """

    secret: str
    uri: str


class TwoFactorCode(BaseModel):
    """
    A second-factor code presented to confirm or disable.

    Attributes:
        code: A six-digit authenticator code, or a backup code for disable.
    """

    code: str


class TwoFactorConfirmed(BaseModel):
    """
    The result of activating the second factor.

    Attributes:
        success: Always true when the request succeeded.
        backup_codes: Single-use recovery codes, shown exactly once. Only
            salted hashes are stored, so they cannot be shown again.
    """

    success: bool
    backup_codes: list[str]


def enrollment_uri(secret: str) -> str:
    """
    Build the provisioning URI this panel enrols with.

    One implementation, used by the JSON response and by the settings
    fragment: the issuer and the account must agree wherever the QR is drawn.

    Args:
        secret: The base32 secret being enrolled.

    Returns:
        The ``otpauth://`` URI, naming this machine so an operator with
        several panels can tell them apart in the app.
    """
    return provisioning_uri(secret, issuer="WASM", account=socket.gethostname())


# The 2FA handlers are synchronous on purpose, and it buys two things: FastAPI
# runs them in a threadpool, and the panel's settings screen can call them
# directly as functions - same auditing, same lockout accounting - instead of
# growing a second implementation of each mutation.


@router.get("/2fa", response_model=TwoFactorStatus)
def two_factor_status(session: dict[str, Any] = Depends(require_auth)) -> TwoFactorStatus:
    """
    Report the two-factor state.

    Args:
        session: The authenticated session.

    Returns:
        The state, never including any secret.
    """
    return TwoFactorStatus(**get_token_manager().totp_status())


@router.post("/2fa/enroll", response_model=TwoFactorEnrollment)
def two_factor_enroll(
    request: Request, session: dict[str, Any] = Depends(require_auth)
) -> TwoFactorEnrollment:
    """
    Begin enrolment: generate a pending secret. Nothing is enforced yet.

    Args:
        request: The incoming request.
        session: The authenticated session.

    Returns:
        The secret and its provisioning URI, shown to the operator once.
    """
    token_manager = get_token_manager()
    secret = token_manager.begin_totp_enrollment()

    audit = get_audit_logger()
    if audit:
        audit.record(
            action="auth.2fa.enroll",
            result="success",
            client_ip=get_client_ip(request),
            actor=str(session.get("sid")),
            resource="/api/auth/2fa/enroll",
        )

    return TwoFactorEnrollment(secret=secret, uri=enrollment_uri(secret))


@router.post("/2fa/confirm", response_model=TwoFactorConfirmed)
def two_factor_confirm(
    request: Request, body: TwoFactorCode, session: dict[str, Any] = Depends(require_auth)
) -> TwoFactorConfirmed:
    """
    Verify a code from the authenticator and activate the second factor.

    Args:
        request: The incoming request.
        body: The code the app shows for the pending secret.
        session: The authenticated session.

    Returns:
        The backup codes, in clear, exactly once.

    Raises:
        HTTPException: 400 when the code does not verify. Not counted by the
            lockout: the pending secret is on the operator's own screen, so a
            wrong code here proves a typo, not a guess at a credential.
    """
    token_manager = get_token_manager()
    codes = token_manager.confirm_totp_enrollment(body.code)
    audit = get_audit_logger()
    client_ip = get_client_ip(request)

    if codes is None:
        if audit:
            audit.record(
                action="auth.2fa.confirm",
                result="failure",
                client_ip=client_ip,
                actor=str(session.get("sid")),
                resource="/api/auth/2fa/confirm",
                detail="code did not match the pending secret",
            )
        raise HTTPException(
            status_code=400,
            detail="That code was not accepted. Scan the QR again and enter a fresh code.",
        )

    if audit:
        audit.record(
            action="auth.2fa.confirm",
            result="success",
            client_ip=client_ip,
            actor=str(session.get("sid")),
            resource="/api/auth/2fa/confirm",
        )

    return TwoFactorConfirmed(success=True, backup_codes=codes)


@router.post("/2fa/disable")
def two_factor_disable(
    request: Request, body: TwoFactorCode, session: dict[str, Any] = Depends(require_auth)
) -> dict[str, Any]:
    """
    Turn the second factor off, on presentation of a current code.

    Args:
        request: The incoming request.
        body: A TOTP code or an unused backup code.
        session: The authenticated session.

    Returns:
        A confirmation payload.

    Raises:
        HTTPException: 400 when the code does not verify. Counted by the same
            lockout as a failed login: this endpoint guards the switch that
            turns the second factor off, so a wrong code here is a credential
            guess by whoever holds the session.
    """
    token_manager = get_token_manager()
    audit = get_audit_logger()
    client_ip = get_client_ip(request)

    if not token_manager.disable_totp(body.code):
        record_auth_failure(client_ip, "/api/auth/2fa/disable", "totp")
        raise HTTPException(
            status_code=400,
            detail="That code was not accepted. Two-factor authentication stays on.",
        )

    if audit:
        audit.record(
            action="auth.2fa.disable",
            result="success",
            client_ip=client_ip,
            actor=str(session.get("sid")),
            resource="/api/auth/2fa/disable",
        )

    return {"success": True, "message": "Two-factor authentication disabled"}


class ApiTokenRequest(BaseModel):
    """
    Request to issue an API token.

    Attributes:
        name: Human-chosen name, unique across all tokens ever issued.
        scope: ``read``, ``deploy`` or ``admin``.
        expires_hours: Lifetime in hours; omit for a token that only dies by
            revocation.
    """

    name: str = Field(min_length=1, max_length=64)
    scope: str
    expires_hours: int | None = Field(default=None, ge=1, le=24 * 3650)


class ApiTokenCreated(BaseModel):
    """
    A freshly issued API token. The only response that ever carries the token.

    Attributes:
        id: Record id, used to revoke it.
        name: The token's name.
        scope: The token's scope.
        token: The credential, shown exactly once - only its salted hash is
            stored, so it cannot be shown again.
        created_at: Creation time as a UNIX timestamp.
        expires_at: Expiry as a UNIX timestamp, or None for no expiry.
    """

    id: int
    name: str
    scope: str
    token: str
    created_at: float
    expires_at: float | None = None


class ApiTokenInfo(BaseModel):
    """
    One API token record, with no credential in it.

    Attributes:
        id: Record id.
        name: The token's name.
        scope: The token's scope.
        created_at: Creation time as a UNIX timestamp.
        expires_at: Expiry as a UNIX timestamp, or None for no expiry.
        last_used_at: When it last authenticated a request, or None.
        revoked_at: When it was revoked, or None while it is live.
    """

    id: int
    name: str
    scope: str
    created_at: float
    expires_at: float | None = None
    last_used_at: float | None = None
    revoked_at: float | None = None


class ApiTokenListResponse(BaseModel):
    """
    Every API token record.

    Attributes:
        tokens: The records, newest first.
    """

    tokens: list[ApiTokenInfo]


# Token management is admin-only in both directions: a read token must not
# even list its siblings, so the GETs here carry an explicit require_scope
# where the chokepoint's method-based floor would have allowed "read".
# Synchronous like the two-factor handlers, and for the same reason: the
# settings screen calls these functions directly.


@router.get("/tokens", response_model=ApiTokenListResponse)
def list_api_tokens(
    session: dict[str, Any] = Depends(require_scope("admin")),
) -> ApiTokenListResponse:
    """
    List every API token ever issued, live and revoked alike.

    Args:
        session: The authenticated session, admin scope required.

    Returns:
        The records. No response from this endpoint carries a token.
    """
    records = get_token_manager().list_api_tokens()
    return ApiTokenListResponse(tokens=[ApiTokenInfo(**record) for record in records])


@router.post("/tokens", response_model=ApiTokenCreated, status_code=201)
def create_api_token(
    request: Request,
    body: ApiTokenRequest,
    session: dict[str, Any] = Depends(require_scope("admin")),
) -> ApiTokenCreated:
    """
    Issue a named, scoped API token, returned in clear exactly once.

    Args:
        request: The incoming request.
        body: Name, scope and optional expiry.
        session: The authenticated session, admin scope required.

    Returns:
        The record, including the one and only clear copy of the token.

    Raises:
        SecurityError: When the name is taken or the scope is not a scope. The
            audit record names the token; the token itself never reaches the
            audit log.
    """
    issued = get_token_manager().create_api_token(body.name, body.scope, body.expires_hours)

    audit = get_audit_logger()
    if audit:
        audit.record(
            action="auth.token.create",
            result="success",
            client_ip=get_client_ip(request),
            actor=str(session.get("sid")),
            resource="/api/auth/tokens",
            detail=f"issued token '{issued['name']}' with scope '{issued['scope']}'",
        )

    return ApiTokenCreated(**issued)


@router.delete("/tokens/{token_id}")
def revoke_api_token(
    token_id: int, request: Request, session: dict[str, Any] = Depends(require_scope("admin"))
) -> dict[str, Any]:
    """
    Revoke one API token. Requests presenting it stop authenticating at once.

    Args:
        token_id: The record's id, as listed by ``GET /api/auth/tokens``.
        request: The incoming request.
        session: The authenticated session, admin scope required.

    Returns:
        A confirmation payload naming the revoked token.

    Raises:
        HTTPException: 404 when no record has that id.
    """
    name = get_token_manager().revoke_api_token(token_id)
    if name is None:
        raise HTTPException(status_code=404, detail=f"No API token with id {token_id}")

    audit = get_audit_logger()
    if audit:
        audit.record(
            action="auth.token.revoke",
            result="success",
            client_ip=get_client_ip(request),
            actor=str(session.get("sid")),
            resource=f"/api/auth/tokens/{token_id}",
            detail=f"revoked token '{name}'",
        )

    return {"success": True, "revoked": name}
