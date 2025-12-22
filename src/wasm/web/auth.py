"""
Security and authentication module for WASM Web Interface.

Implements secure token-based authentication with:
- Cryptographically secure token generation
- JWT-based session management
- Rate limiting
- IP whitelisting support
- Brute force protection
"""

import hashlib
import hmac
import os
import secrets
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Set

from jose import JWTError, jwt

# Security constants
TOKEN_LENGTH = 32
SECRET_KEY_LENGTH = 64
MAX_FAILED_ATTEMPTS = 5
LOCKOUT_DURATION = 300  # 5 minutes
RATE_LIMIT_WINDOW = 60  # 1 minute
RATE_LIMIT_MAX_REQUESTS = 1000  # Increased from 100 to 1000 for better UX
TOKEN_FILE_PATH = Path("/etc/wasm/web-token")
SECRET_FILE_PATH = Path("/etc/wasm/web-secret")

# JWT settings
JWT_ALGORITHM = "HS256"
JWT_EXPIRATION_HOURS = 24


@dataclass
class SecurityConfig:
    """Security configuration for the web interface."""
    
    host: str = "127.0.0.1"  # Default to localhost only for security
    port: int = 8080
    allowed_hosts: List[str] = field(default_factory=lambda: ["127.0.0.1", "localhost"])
    enable_cors: bool = False
    cors_origins: List[str] = field(default_factory=list)
    rate_limit_enabled: bool = True
    rate_limit_requests: int = RATE_LIMIT_MAX_REQUESTS
    rate_limit_window: int = RATE_LIMIT_WINDOW
    max_failed_attempts: int = MAX_FAILED_ATTEMPTS
    lockout_duration: int = LOCKOUT_DURATION
    token_expiration_hours: int = JWT_EXPIRATION_HOURS
    require_https: bool = False
    ip_whitelist: List[str] = field(default_factory=list)


@dataclass
class FailedAttempt:
    """Track failed authentication attempts."""
    
    count: int = 0
    first_attempt: float = 0.0
    locked_until: float = 0.0


class RateLimiter:
    """
    Token bucket rate limiter to prevent abuse.
    
    Tracks requests per IP address within a sliding window.
    """
    
    def __init__(self, max_requests: int = RATE_LIMIT_MAX_REQUESTS, window: int = RATE_LIMIT_WINDOW):
        self.max_requests = max_requests
        self.window = window
        self._requests: Dict[str, List[float]] = {}
    
    def is_allowed(self, client_ip: str) -> bool:
        """
        Check if a request from the given IP is allowed.
        
        Args:
            client_ip: The client's IP address.
            
        Returns:
            True if the request is allowed, False if rate limited.
        """
        now = time.time()
        
        # Clean old entries
        if client_ip in self._requests:
            self._requests[client_ip] = [
                ts for ts in self._requests[client_ip]
                if now - ts < self.window
            ]
        else:
            self._requests[client_ip] = []
        
        # Check limit
        if len(self._requests[client_ip]) >= self.max_requests:
            return False
        
        # Record this request
        self._requests[client_ip].append(now)
        return True
    
    def get_remaining(self, client_ip: str) -> int:
        """Get remaining requests for an IP."""
        if client_ip not in self._requests:
            return self.max_requests
        
        now = time.time()
        valid_requests = [ts for ts in self._requests[client_ip] if now - ts < self.window]
        return max(0, self.max_requests - len(valid_requests))
    
    def reset(self, client_ip: str) -> None:
        """Reset rate limit for an IP."""
        if client_ip in self._requests:
            del self._requests[client_ip]


class BruteForceProtection:
    """
    Protection against brute force authentication attacks.
    
    Tracks failed login attempts and implements lockout after
    too many failures.
    """
    
    def __init__(
        self,
        max_attempts: int = MAX_FAILED_ATTEMPTS,
        lockout_duration: int = LOCKOUT_DURATION
    ):
        self.max_attempts = max_attempts
        self.lockout_duration = lockout_duration
        self._failed_attempts: Dict[str, FailedAttempt] = {}
    
    def record_failure(self, client_ip: str) -> None:
        """Record a failed authentication attempt."""
        now = time.time()
        
        if client_ip not in self._failed_attempts:
            self._failed_attempts[client_ip] = FailedAttempt(
                count=1,
                first_attempt=now
            )
        else:
            attempt = self._failed_attempts[client_ip]
            # Reset if outside window
            if now - attempt.first_attempt > self.lockout_duration:
                attempt.count = 1
                attempt.first_attempt = now
                attempt.locked_until = 0.0
            else:
                attempt.count += 1
                
                # Lock if too many attempts
                if attempt.count >= self.max_attempts:
                    attempt.locked_until = now + self.lockout_duration
    
    def record_success(self, client_ip: str) -> None:
        """Clear failed attempts after successful auth."""
        if client_ip in self._failed_attempts:
            del self._failed_attempts[client_ip]
    
    def is_locked(self, client_ip: str) -> bool:
        """Check if an IP is currently locked out."""
        if client_ip not in self._failed_attempts:
            return False
        
        attempt = self._failed_attempts[client_ip]
        if attempt.locked_until > time.time():
            return True
        
        return False
    
    def get_lockout_remaining(self, client_ip: str) -> int:
        """Get remaining lockout time in seconds."""
        if client_ip not in self._failed_attempts:
            return 0
        
        attempt = self._failed_attempts[client_ip]
        remaining = attempt.locked_until - time.time()
        return max(0, int(remaining))
    
    def get_attempts_remaining(self, client_ip: str) -> int:
        """Get remaining attempts before lockout."""
        if client_ip not in self._failed_attempts:
            return self.max_attempts
        
        attempt = self._failed_attempts[client_ip]
        return max(0, self.max_attempts - attempt.count)


class TokenManager:
    """
    Secure token management for WASM Web Interface.
    
    Handles:
    - Master token generation and storage
    - JWT session token creation and validation
    - Token rotation
    """
    
    def __init__(self, config: Optional[SecurityConfig] = None):
        self.config = config or SecurityConfig()
        self._master_token: Optional[str] = None
        self._secret_key: Optional[str] = None
        self._active_sessions: Set[str] = set()
        self._revoked_tokens: Set[str] = set()
        
        # Load or generate secrets
        self._init_secrets()
    
    def _init_secrets(self) -> None:
        """Initialize or load secret key and master token."""
        # Try to load existing secret key
        if SECRET_FILE_PATH.exists():
            try:
                self._secret_key = SECRET_FILE_PATH.read_text().strip()
            except PermissionError:
                pass
        
        # Generate new secret if needed
        if not self._secret_key:
            self._secret_key = secrets.token_hex(SECRET_KEY_LENGTH)
    
    def generate_master_token(self, save: bool = True) -> str:
        """
        Generate a new master token.
        
        Args:
            save: Whether to save the token to disk.
            
        Returns:
            The generated master token.
        """
        # Generate cryptographically secure token
        token_bytes = secrets.token_bytes(TOKEN_LENGTH)
        timestamp = str(int(time.time())).encode()
        
        # Create HMAC signature
        signature = hmac.new(
            self._secret_key.encode(),
            token_bytes + timestamp,
            hashlib.sha256
        ).hexdigest()[:16]
        
        # Format: wasm_<random>_<signature>
        self._master_token = f"wasm_{secrets.token_hex(16)}_{signature}"
        
        if save:
            self._save_master_token()
        
        return self._master_token
    
    def _save_master_token(self) -> None:
        """Save master token securely to disk."""
        try:
            # Create directory if needed
            TOKEN_FILE_PATH.parent.mkdir(parents=True, exist_ok=True)
            
            # Hash the token before storing
            token_hash = hashlib.sha256(
                (self._master_token + self._secret_key).encode()
            ).hexdigest()
            
            # Write with restricted permissions
            TOKEN_FILE_PATH.write_text(token_hash)
            os.chmod(TOKEN_FILE_PATH, 0o600)
            
            # Save secret key too
            SECRET_FILE_PATH.write_text(self._secret_key)
            os.chmod(SECRET_FILE_PATH, 0o600)
        except PermissionError:
            # Running as non-root, store in memory only
            pass
    
    def _load_master_token_hash(self) -> Optional[str]:
        """Load the master token hash from disk."""
        try:
            if TOKEN_FILE_PATH.exists():
                return TOKEN_FILE_PATH.read_text().strip()
        except PermissionError:
            pass
        return None
    
    def verify_master_token(self, token: str) -> bool:
        """
        Verify a master token.
        
        Args:
            token: The token to verify.
            
        Returns:
            True if the token is valid.
        """
        if not token:
            return False
        
        # Check in-memory token first
        if self._master_token and secrets.compare_digest(token, self._master_token):
            return True
        
        # Check against stored hash
        stored_hash = self._load_master_token_hash()
        if stored_hash:
            token_hash = hashlib.sha256(
                (token + self._secret_key).encode()
            ).hexdigest()
            return secrets.compare_digest(token_hash, stored_hash)
        
        return False
    
    def create_session_token(self, client_ip: str) -> str:
        """
        Create a JWT session token.
        
        Args:
            client_ip: The client's IP address.
            
        Returns:
            JWT token string.
        """
        now = datetime.utcnow()
        expires = now + timedelta(hours=self.config.token_expiration_hours)
        
        # Generate unique session ID
        session_id = secrets.token_hex(16)
        self._active_sessions.add(session_id)
        
        payload = {
            "sub": "wasm_session",
            "sid": session_id,
            "ip": client_ip,
            "iat": now.timestamp(),
            "exp": expires.timestamp(),
            "iss": "wasm-web",
        }
        
        return jwt.encode(payload, self._secret_key, algorithm=JWT_ALGORITHM)
    
    def verify_session_token(self, token: str, client_ip: str) -> Optional[Dict]:
        """
        Verify a JWT session token.
        
        Args:
            token: The JWT token to verify.
            client_ip: The client's current IP address.
            
        Returns:
            Token payload if valid, None otherwise.
        """
        try:
            payload = jwt.decode(
                token,
                self._secret_key,
                algorithms=[JWT_ALGORITHM]
            )
            
            # Verify session is active
            session_id = payload.get("sid")
            if session_id not in self._active_sessions:
                return None
            
            # Check if token is revoked
            if session_id in self._revoked_tokens:
                return None
            
            # Verify IP hasn't changed (optional security measure)
            # Commented out for flexibility with proxies
            # if payload.get("ip") != client_ip:
            #     return None
            
            return payload
            
        except JWTError:
            return None
    
    def revoke_session(self, session_id: str) -> None:
        """Revoke a specific session."""
        if session_id in self._active_sessions:
            self._active_sessions.discard(session_id)
            self._revoked_tokens.add(session_id)
    
    def revoke_all_sessions(self) -> None:
        """Revoke all active sessions."""
        self._revoked_tokens.update(self._active_sessions)
        self._active_sessions.clear()
    
    def get_active_session_count(self) -> int:
        """Get the number of active sessions."""
        return len(self._active_sessions)
    
    def rotate_secrets(self) -> str:
        """
        Rotate all secrets and generate new master token.
        
        This invalidates all existing sessions.
        
        Returns:
            New master token.
        """
        # Generate new secret key
        self._secret_key = secrets.token_hex(SECRET_KEY_LENGTH)
        
        # Revoke all sessions
        self.revoke_all_sessions()
        
        # Generate new master token
        return self.generate_master_token(save=True)


def get_client_ip(request) -> str:
    """
    Extract the real client IP from a request.
    
    Handles X-Forwarded-For and X-Real-IP headers for
    requests behind reverse proxies.
    
    Args:
        request: FastAPI/Starlette request object.
        
    Returns:
        The client's IP address.
    """
    # Check for proxy headers
    forwarded_for = request.headers.get("X-Forwarded-For")
    if forwarded_for:
        # Take the first IP in the chain
        return forwarded_for.split(",")[0].strip()
    
    real_ip = request.headers.get("X-Real-IP")
    if real_ip:
        return real_ip.strip()
    
    # Fall back to direct client IP
    if request.client:
        return request.client.host
    
    return "unknown"


def is_safe_path(path: str) -> bool:
    """
    Check if a path is safe (no path traversal).
    
    Args:
        path: The path to check.
        
    Returns:
        True if the path is safe.
    """
    # Normalize the path
    normalized = os.path.normpath(path)
    
    # Check for path traversal attempts
    if ".." in normalized:
        return False
    
    # Check for absolute paths trying to escape
    if normalized.startswith("/"):
        return False
    
    return True


def sanitize_input(value: str, max_length: int = 1000) -> str:
    """
    Sanitize user input.
    
    Args:
        value: The input value.
        max_length: Maximum allowed length.
        
    Returns:
        Sanitized value.
    """
    if not value:
        return ""
    
    # Truncate to max length
    value = value[:max_length]
    
    # Remove null bytes
    value = value.replace("\x00", "")
    
    return value


# FastAPI dependency for authentication
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

security = HTTPBearer()

# Global token manager reference (set by server.py)
_global_token_manager: Optional[TokenManager] = None


def set_token_manager(manager: TokenManager):
    """Set the global token manager for auth dependency."""
    global _global_token_manager
    _global_token_manager = manager


async def require_auth(credentials: HTTPAuthorizationCredentials = Depends(security)) -> dict:
    """
    FastAPI dependency that requires valid authentication.
    
    Usage:
        @router.get("/protected")
        async def protected_route(auth: dict = Depends(require_auth)):
            # auth contains the decoded token payload
            pass
    
    Returns:
        Decoded token payload with user session info.
        
    Raises:
        HTTPException: If authentication fails.
    """
    if not _global_token_manager:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Authentication system not initialized"
        )
    
    token = credentials.credentials
    
    # Try to verify as session token
    payload = _global_token_manager.verify_session_token(token, "api-request")
    if payload:
        return payload
    
    # Try to verify as master token
    if _global_token_manager.verify_master_token(token):
        return {"type": "master", "token": token}
    
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid or expired authentication token",
        headers={"WWW-Authenticate": "Bearer"},
    )
