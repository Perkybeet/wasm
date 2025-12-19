"""
Authentication API endpoints.
"""

from fastapi import APIRouter, Request, HTTPException, Depends
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel
from typing import Optional

from wasm.web.auth import get_client_ip
from wasm.web.server import get_token_manager, get_brute_force

router = APIRouter()
security = HTTPBearer(auto_error=False)


class LoginRequest(BaseModel):
    """Login request body."""
    token: str


class LoginResponse(BaseModel):
    """Login response body."""
    success: bool
    session_token: str
    expires_in: int


class TokenInfo(BaseModel):
    """Token information response."""
    valid: bool
    expires_at: Optional[float] = None
    session_id: Optional[str] = None


async def get_current_session(
    request: Request,
    credentials: HTTPAuthorizationCredentials = Depends(security)
) -> dict:
    """
    Dependency to get and validate the current session.
    
    Raises HTTPException if not authenticated.
    """
    token_manager = get_token_manager()
    client_ip = get_client_ip(request)
    
    # Check for session token in header
    if credentials:
        payload = token_manager.verify_session_token(credentials.credentials, client_ip)
        if payload:
            return payload
    
    # Check for session token in cookie
    session_cookie = request.cookies.get("wasm_session")
    if session_cookie:
        payload = token_manager.verify_session_token(session_cookie, client_ip)
        if payload:
            return payload
    
    raise HTTPException(
        status_code=401,
        detail="Not authenticated",
        headers={"WWW-Authenticate": "Bearer"}
    )


@router.post("/login", response_model=LoginResponse)
async def login(request: Request, body: LoginRequest):
    """
    Authenticate with a master token and get a session token.
    """
    token_manager = get_token_manager()
    brute_force = get_brute_force()
    client_ip = get_client_ip(request)
    
    # Verify master token
    if not token_manager.verify_master_token(body.token):
        brute_force.record_failure(client_ip)
        attempts_remaining = brute_force.get_attempts_remaining(client_ip)
        
        raise HTTPException(
            status_code=401,
            detail=f"Invalid token. {attempts_remaining} attempts remaining."
        )
    
    # Success - clear failed attempts and create session
    brute_force.record_success(client_ip)
    session_token = token_manager.create_session_token(client_ip)
    
    return LoginResponse(
        success=True,
        session_token=session_token,
        expires_in=token_manager.config.token_expiration_hours * 3600
    )


@router.post("/logout")
async def logout(
    request: Request,
    session: dict = Depends(get_current_session)
):
    """
    Logout and invalidate the current session.
    """
    token_manager = get_token_manager()
    session_id = session.get("sid")
    
    if session_id:
        token_manager.revoke_session(session_id)
    
    return {"success": True, "message": "Logged out successfully"}


@router.get("/verify")
async def verify_token(
    request: Request,
    session: dict = Depends(get_current_session)
):
    """
    Verify the current session token is valid.
    """
    return TokenInfo(
        valid=True,
        expires_at=session.get("exp"),
        session_id=session.get("sid")
    )


@router.get("/sessions")
async def get_sessions(
    request: Request,
    session: dict = Depends(get_current_session)
):
    """
    Get information about active sessions.
    """
    token_manager = get_token_manager()
    
    return {
        "active_sessions": token_manager.get_active_session_count(),
        "current_session": session.get("sid")
    }


@router.post("/sessions/revoke-all")
async def revoke_all_sessions(
    request: Request,
    session: dict = Depends(get_current_session)
):
    """
    Revoke all active sessions (logout everywhere).
    """
    token_manager = get_token_manager()
    token_manager.revoke_all_sessions()
    
    return {"success": True, "message": "All sessions revoked"}
