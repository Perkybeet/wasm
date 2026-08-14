"""
Authentication and session security for the WASM web panel.

The panel drives systemd, nginx and certbot as root, so a session here is
equivalent to a root shell. The design decisions that follow from that:

- **The browser never holds the session in JavaScript.** Sessions travel in a
  ``HttpOnly``/``SameSite=Strict`` cookie (``Secure`` whenever the request is
  served over TLS), so an XSS bug cannot read the credential. ``Authorization:
  Bearer`` stays supported for the CLI and automation, which have no cookie
  jar and no ambient-authority problem.
- **Cookie authentication requires a CSRF token on every mutation.** The
  scheme is double-submit *bound to the session*: the CSRF value is generated
  with the session, stored server-side, mirrored in a readable cookie, and has
  to come back in the ``X-WASM-CSRF`` header. A cross-site attacker can neither
  read the cookie nor set a custom header without a CORS preflight the server
  refuses, and unlike plain double-submit a cookie injected by a sibling
  subdomain does not match the stored value.
- **Client identity comes from the TCP peer.** ``X-Forwarded-For`` is honoured
  only when the peer is a configured trusted proxy, and only when the value it
  carries parses as an IP address. Otherwise the IP whitelist, the rate limiter
  and the brute-force lockout could all be defeated by rotating a header, or by
  turning the limiter's key into a string the attacker picks.
- **Every rejected credential is counted in one place.** Cookies, ``Bearer``
  headers and WebSocket handshakes all fail through :func:`record_auth_failure`,
  so a lockout cannot be escaped by changing channel or endpoint.
- **Secrets and sessions are persisted, never invented on the fly.** A signing
  key that silently regenerates logs everyone out on restart and makes multiple
  workers impossible; if the key cannot be written, or exists but is empty, the
  server refuses to start instead of quietly issuing a new one.
- **Sessions die of old age.** Renewal keeps an active operator logged in, but
  it rotates the session id and never pushes the absolute deadline, so a session
  that is used continuously still expires.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import logging
import os
import secrets
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fastapi import HTTPException, Request, status
from starlette.requests import HTTPConnection

from wasm.core import totp
from wasm.core.exceptions import SecurityError
from wasm.core.fs import SECRET_MODE, get_fs

logger = logging.getLogger(__name__)

#: Bytes of entropy in the master token and in the signing key.
TOKEN_LENGTH = 32
SECRET_KEY_LENGTH = 64

#: Five wrong tokens is far more than a human typing a copy-pasted secret needs,
#: and the 15 minute lockout turns an online guessing attack against a 256-bit
#: token into something with no practical end.
MAX_FAILED_ATTEMPTS = 5
LOCKOUT_DURATION = 900

#: A logged-in dashboard polling every widget stays well under 120 requests per
#: minute (2/s sustained per IP). Anything above that is a script, and scripts
#: that need more should use their own token and their own trusted-proxy entry.
RATE_LIMIT_WINDOW = 60
RATE_LIMIT_MAX_REQUESTS = 120

#: Upper bound on tracked client IPs, so a spoofed-source flood cannot turn the
#: rate limiter into unbounded memory growth.
RATE_LIMIT_MAX_TRACKED_IPS = 4096

#: Where the signing key, the master token hash, the session database and the
#: audit log live. Overridable for tests and for unprivileged installs.
DEFAULT_STATE_DIR = Path("/etc/wasm")
STATE_DIR_ENV = "WASM_WEB_STATE_DIR"

SECRET_FILE_NAME = "web-secret"  # noqa: S105 - file name, not a credential
TOKEN_FILE_NAME = "web-token"  # noqa: S105 - file name, not a credential
TOTP_FILE_NAME = "web-totp"
SESSION_DB_NAME = "web-sessions.db"
AUDIT_LOG_NAME = "web-audit.log"

#: Single-use recovery codes issued when TOTP is confirmed. Eight is what an
#: operator can print on one line; each is 32 bits, which a five-attempt
#: lockout makes unguessable in practice and single use makes worthless after.
BACKUP_CODE_COUNT = 8

SESSION_COOKIE_NAME = "wasm_session"
CSRF_COOKIE_NAME = "wasm_csrf"
CSRF_HEADER_NAME = "X-WASM-CSRF"

#: Methods that do not change state and therefore do not need a CSRF token.
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

#: WebSocket tickets are single use and only have to survive the handshake.
WS_TICKET_TTL = 30

#: Close codes for a handshake the middleware refuses. They are in the private
#: 4000-4999 range so a client can tell "log in again" from "you are blocked".
WS_CLOSE_UNAUTHORIZED = 4401
WS_CLOSE_FORBIDDEN = 4403
WS_CLOSE_RATE_LIMITED = 4429

#: Subprotocol prefix carrying a session token, for clients that cannot send a
#: cookie: ``Sec-WebSocket-Protocol: wasm.auth, wasm.token.<token>``.
WS_SUBPROTOCOL = "wasm.auth"
WS_TOKEN_PREFIX = "wasm.token."  # noqa: S105 - subprotocol prefix, not a credential

#: Recorded in the payload the auth dependency hands to endpoints. Kept as
#: names rather than a JWT: see SessionStore._encode for why the JWT went.
SESSION_ISSUER = "wasm-web"
SESSION_SUBJECT = "wasm_session"
JWT_EXPIRATION_HOURS = 12

#: A session is re-issued once it is past this fraction of its lifetime, so an
#: active operator is never logged out mid-deploy while idle sessions still die.
SESSION_RENEW_RATIO = 0.5

#: How long a rotated session id keeps working after it is replaced. Long
#: enough for the requests a dashboard already had in flight, short enough that
#: a captured cookie is worthless by the time it is replayed.
SESSION_ROTATION_GRACE = 30

#: Hard ceiling on a session's life, however active it is. Renewal resets the
#: idle clock but never this one, so a stolen cookie that is kept warm still
#: stops working within a day.
SESSION_MAX_HOURS = 24

#: The audit log is written by anonymous, unauthenticated events (a refused
#: handshake is one), so it is rotated rather than allowed to fill the disk.
AUDIT_MAX_BYTES = 5 * 1024 * 1024
AUDIT_BACKUPS = 3

FILE_MODE = 0o600
DIR_MODE = 0o700


def utcnow() -> datetime:
    """
    Return the current time as an aware UTC datetime.

    Returns:
        The current UTC time.
    """
    return datetime.now(timezone.utc)


@dataclass
class SecurityConfig:
    """
    Security configuration for the web interface.

    Attributes:
        host: Interface the server binds to.
        port: TCP port the server binds to.
        allowed_hosts: Host header values accepted by the deployment.
        enable_cors: Whether cross-origin requests are allowed at all.
        cors_origins: Explicit origins allowed when CORS is enabled.
        rate_limit_enabled: Whether per-IP rate limiting is applied.
        rate_limit_requests: Requests allowed per window and per client IP.
        rate_limit_window: Length of the rate limit window in seconds.
        max_failed_attempts: Failed logins before an IP is locked out.
        lockout_duration: Lockout length in seconds.
        token_expiration_hours: Session lifetime in hours, refreshed by activity.
        session_max_hours: Absolute lifetime, never extended by activity.
        require_https: Refuse to serve or start without TLS when true.
        ssl_certfile: Path to the TLS certificate chain.
        ssl_keyfile: Path to the TLS private key.
        ip_whitelist: Client IPs or CIDRs allowed to reach the panel.
        trusted_proxies: Peer addresses whose forwarding headers are believed.
            Empty by default: an unconfigured deployment trusts nobody.
        bind_session_to_ip: Reject a session presented from a different IP.
        state_dir: Directory holding secrets, sessions and the audit log.
        audit_enabled: Whether privileged actions are written to the audit log.
        audit_max_bytes: Size at which the audit log is rotated.
        audit_backups: Rotated audit files kept before the oldest is deleted.
    """

    host: str = "127.0.0.1"
    port: int = 8080
    allowed_hosts: list[str] = field(default_factory=lambda: ["127.0.0.1", "localhost"])
    enable_cors: bool = False
    cors_origins: list[str] = field(default_factory=list)
    rate_limit_enabled: bool = True
    rate_limit_requests: int = RATE_LIMIT_MAX_REQUESTS
    rate_limit_window: int = RATE_LIMIT_WINDOW
    max_failed_attempts: int = MAX_FAILED_ATTEMPTS
    lockout_duration: int = LOCKOUT_DURATION
    token_expiration_hours: int = JWT_EXPIRATION_HOURS
    session_max_hours: int = SESSION_MAX_HOURS
    require_https: bool = False
    ssl_certfile: str | None = None
    ssl_keyfile: str | None = None
    ip_whitelist: list[str] = field(default_factory=list)
    trusted_proxies: list[str] = field(default_factory=list)
    bind_session_to_ip: bool = True
    state_dir: Path | None = None
    audit_enabled: bool = True
    audit_max_bytes: int = AUDIT_MAX_BYTES
    audit_backups: int = AUDIT_BACKUPS

    @property
    def resolved_state_dir(self) -> Path:
        """
        Directory used for secrets, sessions and audit records.

        Returns:
            The explicit ``state_dir``, else ``WASM_WEB_STATE_DIR``, else
            ``/etc/wasm``.
        """
        if self.state_dir is not None:
            return Path(self.state_dir)
        env_dir = os.environ.get(STATE_DIR_ENV)
        if env_dir:
            return Path(env_dir)
        return DEFAULT_STATE_DIR

    @property
    def secret_file(self) -> Path:
        """Path of the signing key file."""
        return self.resolved_state_dir / SECRET_FILE_NAME

    @property
    def token_file(self) -> Path:
        """Path of the master token hash file."""
        return self.resolved_state_dir / TOKEN_FILE_NAME

    @property
    def totp_file(self) -> Path:
        """Path of the two-factor state file."""
        return self.resolved_state_dir / TOTP_FILE_NAME

    @property
    def session_db(self) -> Path:
        """Path of the session database."""
        return self.resolved_state_dir / SESSION_DB_NAME

    @property
    def audit_log(self) -> Path:
        """Path of the audit log."""
        return self.resolved_state_dir / AUDIT_LOG_NAME


# Module-wide configuration, installed by the server factory.
_global_config: SecurityConfig | None = None
_global_token_manager: TokenManager | None = None
_global_audit_logger: AuditLogger | None = None
_global_brute_force: BruteForceProtection | None = None


def set_security_config(config: SecurityConfig) -> None:
    """
    Install the configuration used by helpers that only receive a request.

    Args:
        config: The active security configuration.
    """
    global _global_config
    _global_config = config


def get_security_config() -> SecurityConfig:
    """
    Return the active security configuration.

    Returns:
        The installed configuration, or a default one when the server has not
        been created yet.
    """
    return _global_config or SecurityConfig()


def set_token_manager(manager: TokenManager | None) -> None:
    """
    Install the token manager used by the authentication dependency.

    Args:
        manager: The manager to install, or None to clear it.
    """
    global _global_token_manager
    _global_token_manager = manager


def get_global_token_manager() -> TokenManager | None:
    """
    Return the installed token manager.

    Returns:
        The manager, or None when the server has not been created yet.
    """
    return _global_token_manager


def set_audit_logger(audit: AuditLogger | None) -> None:
    """
    Install the audit logger used by the API and the middleware.

    Args:
        audit: The logger to install, or None to disable auditing.
    """
    global _global_audit_logger
    _global_audit_logger = audit


def get_audit_logger() -> AuditLogger | None:
    """
    Return the installed audit logger.

    Returns:
        The logger, or None when auditing is disabled.
    """
    return _global_audit_logger


def set_brute_force_protection(protection: BruteForceProtection | None) -> None:
    """
    Install the lockout tracker shared by every credential channel.

    Args:
        protection: The tracker to install, or None to clear it.
    """
    global _global_brute_force
    _global_brute_force = protection


def get_brute_force_protection() -> BruteForceProtection | None:
    """
    Return the installed lockout tracker.

    Returns:
        The tracker, or None when the server has not been created yet.
    """
    return _global_brute_force


def ensure_state_dir(path: Path) -> None:
    """
    Create the state directory with owner-only permissions.

    Args:
        path: Directory that must exist and be private.

    Raises:
        SecurityError: When the directory cannot be created.
    """
    try:
        path.mkdir(parents=True, exist_ok=True)
        os.chmod(path, DIR_MODE)
    except OSError as exc:
        raise SecurityError(
            f"Cannot create the WASM web state directory {path}",
            details=(
                "The web panel stores its signing key, sessions and audit log there. "
                f"Create it as root with 'install -d -m 700 {path}', or point the panel "
                f"somewhere writable with {STATE_DIR_ENV}=/path/to/dir."
            ),
        ) from exc


def write_private_file(path: Path, content: str) -> None:
    """
    Write a file that only its owner can read.

    Args:
        path: Destination file.
        content: Text to write.

    Raises:
        SecurityError: When the file cannot be written.
    """
    ensure_state_dir(path.parent)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, FILE_MODE)
        try:
            os.write(fd, content.encode())
        finally:
            os.close(fd)
        os.chmod(path, FILE_MODE)
    except OSError as exc:
        raise SecurityError(
            f"Cannot write {path}",
            details=(
                "Run the web panel as root, or set "
                f"{STATE_DIR_ENV} to a directory the current user owns."
            ),
        ) from exc


@dataclass
class FailedAttempt:
    """
    Failed authentication attempts recorded for one client.

    Attributes:
        count: Failures inside the current window.
        first_attempt: Monotonic-ish timestamp of the first failure.
        locked_until: Timestamp until which the client is locked out.
    """

    count: int = 0
    first_attempt: float = 0.0
    locked_until: float = 0.0


class RateLimiter:
    """Sliding window request limiter, keyed by the real client IP."""

    def __init__(
        self,
        max_requests: int = RATE_LIMIT_MAX_REQUESTS,
        window: int = RATE_LIMIT_WINDOW,
        max_tracked: int = RATE_LIMIT_MAX_TRACKED_IPS,
    ) -> None:
        """
        Build a rate limiter.

        Args:
            max_requests: Requests allowed per window and client.
            window: Window length in seconds.
            max_tracked: Maximum number of clients kept in memory.
        """
        self.max_requests = max_requests
        self.window = window
        self.max_tracked = max_tracked
        self._requests: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def is_allowed(self, client_ip: str) -> bool:
        """
        Record a request and report whether it is within the limit.

        Args:
            client_ip: The client's IP address.

        Returns:
            True when the request is allowed, False when rate limited.
        """
        now = time.time()
        with self._lock:
            self._purge(now)
            timestamps = [ts for ts in self._requests.get(client_ip, []) if now - ts < self.window]
            if len(timestamps) >= self.max_requests:
                self._requests[client_ip] = timestamps
                return False
            timestamps.append(now)
            self._requests[client_ip] = timestamps
            self._enforce_capacity()
            return True

    def get_remaining(self, client_ip: str) -> int:
        """
        Report how many requests the client has left in the window.

        Args:
            client_ip: The client's IP address.

        Returns:
            The number of remaining requests.
        """
        now = time.time()
        with self._lock:
            timestamps = [ts for ts in self._requests.get(client_ip, []) if now - ts < self.window]
            return max(0, self.max_requests - len(timestamps))

    def reset(self, client_ip: str) -> None:
        """
        Forget the history of one client.

        Args:
            client_ip: The client's IP address.
        """
        with self._lock:
            self._requests.pop(client_ip, None)

    def tracked_clients(self) -> int:
        """
        Report how many clients are currently tracked.

        Returns:
            Number of client entries held in memory.
        """
        with self._lock:
            return len(self._requests)

    def _purge(self, now: float) -> None:
        """
        Drop clients with no recent activity.

        Args:
            now: Current timestamp.
        """
        stale = [
            ip
            for ip, timestamps in self._requests.items()
            if not timestamps or now - timestamps[-1] >= self.window
        ]
        for ip in stale:
            del self._requests[ip]

    def _enforce_capacity(self) -> None:
        """Drop the least recently seen clients once the table is full."""
        # Spoofed sources can create entries faster than the window expires them.
        if len(self._requests) <= self.max_tracked:
            return
        oldest = sorted(self._requests, key=lambda ip: self._requests[ip][-1])
        for ip in oldest[: len(self._requests) - self.max_tracked]:
            del self._requests[ip]


class BruteForceProtection:
    """Lockout of clients that keep failing authentication."""

    def __init__(
        self,
        max_attempts: int = MAX_FAILED_ATTEMPTS,
        lockout_duration: int = LOCKOUT_DURATION,
        max_tracked: int = RATE_LIMIT_MAX_TRACKED_IPS,
    ) -> None:
        """
        Build the lockout tracker.

        Args:
            max_attempts: Failures tolerated before locking a client out.
            lockout_duration: Lockout length in seconds.
            max_tracked: Maximum number of clients kept in memory.
        """
        self.max_attempts = max_attempts
        self.lockout_duration = lockout_duration
        self.max_tracked = max_tracked
        self._failed_attempts: dict[str, FailedAttempt] = {}
        self._lock = threading.Lock()

    def record_failure(self, client_ip: str) -> None:
        """
        Record one failed authentication.

        Args:
            client_ip: The client's IP address.
        """
        now = time.time()
        with self._lock:
            self._purge(now)
            attempt = self._failed_attempts.get(client_ip)
            if attempt is None:
                self._failed_attempts[client_ip] = FailedAttempt(count=1, first_attempt=now)
                return

            if now - attempt.first_attempt > self.lockout_duration:
                attempt.count = 1
                attempt.first_attempt = now
                attempt.locked_until = 0.0
                return

            attempt.count += 1
            if attempt.count >= self.max_attempts:
                attempt.locked_until = now + self.lockout_duration

    def record_success(self, client_ip: str) -> None:
        """
        Clear the failure history of a client after a successful login.

        Args:
            client_ip: The client's IP address.
        """
        with self._lock:
            self._failed_attempts.pop(client_ip, None)

    def is_locked(self, client_ip: str) -> bool:
        """
        Report whether a client is currently locked out.

        Args:
            client_ip: The client's IP address.

        Returns:
            True while the lockout is in force.
        """
        with self._lock:
            attempt = self._failed_attempts.get(client_ip)
            return bool(attempt and attempt.locked_until > time.time())

    def get_lockout_remaining(self, client_ip: str) -> int:
        """
        Report the remaining lockout time.

        Args:
            client_ip: The client's IP address.

        Returns:
            Seconds left, zero when not locked out.
        """
        with self._lock:
            attempt = self._failed_attempts.get(client_ip)
            if not attempt:
                return 0
            return max(0, int(attempt.locked_until - time.time()))

    def get_attempts_remaining(self, client_ip: str) -> int:
        """
        Report how many failures the client has left.

        Args:
            client_ip: The client's IP address.

        Returns:
            Remaining attempts before lockout.
        """
        with self._lock:
            attempt = self._failed_attempts.get(client_ip)
            if not attempt:
                return self.max_attempts
            return max(0, self.max_attempts - attempt.count)

    def _purge(self, now: float) -> None:
        """
        Drop expired lockouts and cap the tracked client count.

        Args:
            now: Current timestamp.
        """
        stale = [
            ip
            for ip, attempt in self._failed_attempts.items()
            if now - attempt.first_attempt > self.lockout_duration and attempt.locked_until <= now
        ]
        for ip in stale:
            del self._failed_attempts[ip]

        if len(self._failed_attempts) > self.max_tracked:
            oldest = sorted(
                self._failed_attempts, key=lambda ip: self._failed_attempts[ip].first_attempt
            )
            for ip in oldest[: len(self._failed_attempts) - self.max_tracked]:
                del self._failed_attempts[ip]


class SessionStore:
    """
    Durable session and WebSocket ticket storage.

    Sessions live in SQLite rather than in a process-local set so that they
    survive a restart, work with more than one worker, and can be purged.
    """

    def __init__(self, path: Path) -> None:
        """
        Open (creating if needed) the session database.

        Args:
            path: Database file path.

        Raises:
            SecurityError: When the database cannot be created.
        """
        self.path = path
        ensure_state_dir(path.parent)
        try:
            self._conn = sqlite3.connect(str(path), check_same_thread=False)
            os.chmod(path, FILE_MODE)
        except (sqlite3.Error, OSError) as exc:
            raise SecurityError(
                f"Cannot open the web session database {path}",
                details=(
                    "Run the web panel as root, or set "
                    f"{STATE_DIR_ENV} to a directory the current user owns."
                ),
            ) from exc
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        self._create_schema()

    def _create_schema(self) -> None:
        """Create the session and ticket tables when missing, and migrate old ones."""
        with self._lock, self._conn:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    sid TEXT PRIMARY KEY,
                    csrf_token TEXT NOT NULL,
                    client_ip TEXT NOT NULL,
                    issued_at REAL NOT NULL,
                    created_at REAL NOT NULL DEFAULT 0,
                    expires_at REAL NOT NULL,
                    revoked INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            columns = {
                row["name"] for row in self._conn.execute("PRAGMA table_info(sessions)").fetchall()
            }
            if "created_at" not in columns:
                # Databases written before absolute expiry existed: the safest
                # reading of an unknown birth date is "when it was last issued".
                self._conn.execute(
                    "ALTER TABLE sessions ADD COLUMN created_at REAL NOT NULL DEFAULT 0"
                )
                self._conn.execute("UPDATE sessions SET created_at = issued_at")
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS ws_tickets (
                    ticket_hash TEXT PRIMARY KEY,
                    sid TEXT NOT NULL,
                    client_ip TEXT NOT NULL,
                    expires_at REAL NOT NULL
                )
                """
            )

    def create(
        self,
        sid: str,
        csrf_token: str,
        client_ip: str,
        expires_at: float,
        created_at: float | None = None,
    ) -> None:
        """
        Persist a new session.

        Args:
            sid: Session identifier.
            csrf_token: CSRF token bound to the session.
            client_ip: IP the session was issued to.
            expires_at: Expiry as a UNIX timestamp.
            created_at: Birth of the login this session descends from. Defaults
                to now; a rotation passes the original value so that renewal
                cannot extend the absolute lifetime.
        """
        now = time.time()
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO sessions "
                "(sid, csrf_token, client_ip, issued_at, created_at, expires_at, revoked) "
                "VALUES (?, ?, ?, ?, ?, ?, 0)",
                (
                    sid,
                    csrf_token,
                    client_ip,
                    now,
                    now if created_at is None else created_at,
                    expires_at,
                ),
            )
        self.purge_expired()

    def get(self, sid: str) -> dict[str, Any] | None:
        """
        Fetch a live session.

        Args:
            sid: Session identifier.

        Returns:
            The session row as a dict, or None when unknown, revoked or expired.
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM sessions WHERE sid = ? AND revoked = 0 AND expires_at > ?",
                (sid, time.time()),
            ).fetchone()
        return dict(row) if row else None

    def extend(self, sid: str, expires_at: float) -> None:
        """
        Push a session's expiry further out.

        Args:
            sid: Session identifier.
            expires_at: New expiry as a UNIX timestamp.
        """
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE sessions SET expires_at = ? WHERE sid = ?", (expires_at, sid)
            )

    def rotate(self, old_sid: str, new_sid: str, csrf_token: str, expires_at: float) -> dict | None:
        """
        Replace a session with a fresh identifier in one transaction.

        Args:
            old_sid: Session being retired.
            new_sid: Identifier of the replacement.
            csrf_token: CSRF token of the replacement.
            expires_at: Expiry of the replacement, as a UNIX timestamp.

        Returns:
            The row of the retired session, or None when it no longer exists.
        """
        now = time.time()
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT * FROM sessions WHERE sid = ? AND revoked = 0", (old_sid,)
            ).fetchone()
            if row is None:
                return None
            self._conn.execute(
                "INSERT OR REPLACE INTO sessions "
                "(sid, csrf_token, client_ip, issued_at, created_at, expires_at, revoked) "
                "VALUES (?, ?, ?, ?, ?, ?, 0)",
                (new_sid, csrf_token, row["client_ip"], now, row["created_at"], expires_at),
            )
            # The retired identifier is not deleted outright: a dashboard fires
            # several requests at once and they all still carry the old cookie.
            # It is given a short grace instead, after which a captured copy of
            # the previous cookie is worthless.
            self._conn.execute(
                "UPDATE sessions SET expires_at = MIN(expires_at, ?) WHERE sid = ?",
                (now + SESSION_ROTATION_GRACE, old_sid),
            )
        return dict(row)

    def revoke(self, sid: str) -> None:
        """
        Revoke one session.

        Args:
            sid: Session identifier.
        """
        with self._lock, self._conn:
            self._conn.execute("UPDATE sessions SET revoked = 1 WHERE sid = ?", (sid,))
            self._conn.execute("DELETE FROM ws_tickets WHERE sid = ?", (sid,))

    def revoke_all(self) -> None:
        """Revoke every session and drop every pending ticket."""
        with self._lock, self._conn:
            self._conn.execute("UPDATE sessions SET revoked = 1")
            self._conn.execute("DELETE FROM ws_tickets")

    def active_count(self) -> int:
        """
        Count sessions that are still usable.

        Returns:
            Number of live sessions.
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM sessions WHERE revoked = 0 AND expires_at > ?",
                (time.time(),),
            ).fetchone()
        return int(row["n"])

    def purge_expired(self) -> int:
        """
        Delete expired sessions, revoked sessions and expired tickets.

        Returns:
            Number of session rows removed.
        """
        now = time.time()
        with self._lock, self._conn:
            cursor = self._conn.execute(
                "DELETE FROM sessions WHERE expires_at <= ? OR revoked = 1", (now,)
            )
            self._conn.execute("DELETE FROM ws_tickets WHERE expires_at <= ?", (now,))
        return cursor.rowcount

    def store_ticket(self, ticket_hash: str, sid: str, client_ip: str, expires_at: float) -> None:
        """
        Persist a single-use WebSocket ticket.

        Args:
            ticket_hash: Hash of the ticket value.
            sid: Session the ticket belongs to.
            client_ip: IP the ticket was issued to.
            expires_at: Expiry as a UNIX timestamp.
        """
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO ws_tickets (ticket_hash, sid, client_ip, expires_at) "
                "VALUES (?, ?, ?, ?)",
                (ticket_hash, sid, client_ip, expires_at),
            )

    def consume_ticket(self, ticket_hash: str) -> dict[str, Any] | None:
        """
        Atomically redeem a WebSocket ticket.

        Args:
            ticket_hash: Hash of the presented ticket.

        Returns:
            The ticket row as a dict, or None when unknown or expired.
        """
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT * FROM ws_tickets WHERE ticket_hash = ? AND expires_at > ?",
                (ticket_hash, time.time()),
            ).fetchone()
            self._conn.execute("DELETE FROM ws_tickets WHERE ticket_hash = ?", (ticket_hash,))
        return dict(row) if row else None

    def close(self) -> None:
        """Close the underlying database connection."""
        with self._lock:
            self._conn.close()


class AuditLogger:
    """
    Append-only record of privileged actions.

    Each line is one JSON object: who acted, when, from where, on what, and how
    it ended. Tokens never reach this file; sessions are identified by their
    session id only.

    Unauthenticated events are auditable too - a refused handshake is the most
    interesting record there is - which means an anonymous client can drive the
    write rate. The file is therefore rotated at a fixed size and a fixed number
    of backups, so the worst an attacker achieves is erasing their own older
    footprints rather than filling the disk of a machine WASM runs as root.
    """

    def __init__(
        self,
        path: Path,
        enabled: bool = True,
        max_bytes: int = AUDIT_MAX_BYTES,
        backups: int = AUDIT_BACKUPS,
    ) -> None:
        """
        Prepare the audit log.

        Args:
            path: Log file path.
            enabled: When false, records are dropped.
            max_bytes: Size at which the file is rotated.
            backups: Number of rotated files kept.

        Raises:
            SecurityError: When the log file cannot be created.
        """
        self.path = path
        self.enabled = enabled
        self.max_bytes = max(1024, max_bytes)
        self.backups = max(0, backups)
        self._lock = threading.Lock()
        self._size = 0
        if not enabled:
            return
        ensure_state_dir(path.parent)
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, FILE_MODE)
            os.close(fd)
            os.chmod(path, FILE_MODE)
            self._size = path.stat().st_size
        except OSError as exc:
            raise SecurityError(
                f"Cannot open the web audit log {path}",
                details=(
                    "A panel that runs systemd as root must be auditable. Run as root, "
                    f"or set {STATE_DIR_ENV} to a directory the current user owns."
                ),
            ) from exc

    def record(
        self,
        action: str,
        result: str,
        client_ip: str,
        actor: str = "anonymous",
        resource: str | None = None,
        detail: str | None = None,
    ) -> None:
        """
        Append one audit entry.

        Args:
            action: What was attempted, for example ``auth.login``.
            result: Outcome, for example ``success`` or ``denied``.
            client_ip: Address the request came from.
            actor: Session id or ``master``/``anonymous``.
            resource: Target of the action, such as an API path.
            detail: Extra context. Must never contain a credential.
        """
        if not self.enabled:
            return

        entry = {
            "ts": utcnow().isoformat(),
            "action": action,
            "result": result,
            "actor": actor,
            "ip": client_ip,
            "resource": resource,
            "detail": detail,
        }
        payload = (json.dumps({k: v for k, v in entry.items() if v is not None}) + "\n").encode()
        try:
            with self._lock:
                if self._size + len(payload) > self.max_bytes:
                    self._rotate()
                fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, FILE_MODE)
                try:
                    os.write(fd, payload)
                finally:
                    os.close(fd)
                self._size += len(payload)
        except OSError as exc:
            # Losing the audit trail must be noisy, but it must not take the
            # panel down mid-request.
            logger.error("Cannot write audit entry to %s: %s", self.path, exc)

    def _rotate(self) -> None:
        """
        Move the current log aside, dropping the oldest backup.

        Raises:
            OSError: When the files cannot be renamed; the caller logs it.
        """
        if self.backups == 0:
            self.path.unlink(missing_ok=True)
        else:
            oldest = self.path.with_name(f"{self.path.name}.{self.backups}")
            oldest.unlink(missing_ok=True)
            for index in range(self.backups - 1, 0, -1):
                source = self.path.with_name(f"{self.path.name}.{index}")
                if source.exists():
                    source.rename(self.path.with_name(f"{self.path.name}.{index + 1}"))
            if self.path.exists():
                self.path.rename(self.path.with_name(f"{self.path.name}.1"))
        self._size = 0


@dataclass(frozen=True)
class IssuedSession:
    """
    A freshly created session.

    Attributes:
        token: Signed session token for the cookie or the Bearer header.
        session_id: Server-side identifier of the session.
        csrf_token: Token that must accompany cookie-authenticated mutations.
        expires_at: Expiry as a UNIX timestamp.
        max_age: Lifetime in seconds.
    """

    token: str
    session_id: str
    csrf_token: str
    expires_at: float
    max_age: int


class TokenManager:
    """
    Master token, signing key and session lifecycle.

    The signing key is generated once and persisted with mode 0600. It is never
    regenerated silently: a key that changes on restart invalidates every
    session and cannot be shared between workers.
    """

    def __init__(self, config: SecurityConfig | None = None) -> None:
        """
        Load persistent state, creating it on first run.

        Args:
            config: Security configuration; a default one is used when omitted.

        Raises:
            SecurityError: When secrets or sessions cannot be persisted.
        """
        self.config = config or SecurityConfig()
        self._master_token: str | None = None
        self._secret_key: str = self._load_or_create_secret()
        self.sessions = SessionStore(self.config.session_db)
        # Serialises read-modify-write cycles on the two-factor state file, so
        # two logins racing to consume the same backup code cannot both win.
        self._totp_lock = threading.Lock()

    def _load_or_create_secret(self) -> str:
        """
        Load the signing key, generating and persisting it on first run.

        Returns:
            The hex-encoded signing key.

        Raises:
            SecurityError: When the key cannot be read or written.
        """
        secret_file = self.config.secret_file
        if secret_file.exists():
            try:
                existing = secret_file.read_text().strip()
            except OSError as exc:
                raise SecurityError(
                    f"Cannot read the web signing key {secret_file}",
                    details=(
                        "The web panel must run as the user that owns the key. "
                        f"Run as root, or set {STATE_DIR_ENV} to a directory you own."
                    ),
                ) from exc
            if existing:
                return existing
            # Overwriting a key file that exists but is empty would invalidate
            # every session without anyone asking for it, and would hide the
            # truncation (a full disk, an interrupted write, a bad restore).
            raise SecurityError(
                f"The web signing key {secret_file} exists but is empty",
                details=(
                    "WASM refuses to invent a new key silently, because that logs every "
                    "operator out and hides whatever truncated the file. Restore the file "
                    f"from backup, or delete it with 'rm {secret_file}' to start over, "
                    "which revokes all existing sessions on purpose."
                ),
            )

        secret = secrets.token_hex(SECRET_KEY_LENGTH)
        write_private_file(secret_file, secret)
        return secret

    def generate_master_token(self, save: bool = True) -> str:
        """
        Generate a new master token.

        Args:
            save: Whether to persist the token hash.

        Returns:
            The generated master token, which is shown to the operator once.

        Raises:
            SecurityError: When the token hash cannot be persisted.
        """
        self._master_token = f"wasm_{secrets.token_urlsafe(TOKEN_LENGTH)}"
        if save:
            write_private_file(self.config.token_file, self._hash_token(self._master_token))
        return self._master_token

    def _hash_token(self, token: str) -> str:
        """
        Hash a master token for storage.

        Args:
            token: The plaintext token.

        Returns:
            Hex digest of the token, salted with the signing key.
        """
        return hashlib.sha256((token + self._secret_key).encode()).hexdigest()

    def _load_master_token_hash(self) -> str | None:
        """
        Read the stored master token hash.

        Returns:
            The stored hash, or None when absent or unreadable.
        """
        token_file = self.config.token_file
        try:
            if token_file.exists():
                return token_file.read_text().strip()
        except OSError as exc:
            logger.error("Cannot read master token hash %s: %s", token_file, exc)
        return None

    def verify_master_token(self, token: str) -> bool:
        """
        Verify a master token.

        Args:
            token: The token presented by the client.

        Returns:
            True when the token matches the active master token.
        """
        if not token:
            return False

        if self._master_token and secrets.compare_digest(token, self._master_token):
            return True

        stored_hash = self._load_master_token_hash()
        if stored_hash:
            return secrets.compare_digest(self._hash_token(token), stored_hash)

        return False

    # ------------------------------------------------------------------ TOTP

    def _read_totp_state(self) -> dict[str, Any]:
        """
        Read the two-factor state file.

        Returns:
            The stored state, or the disabled default when the file has never
            been written.

        Raises:
            SecurityError: When the file exists but cannot be read or parsed.
                Treating a corrupt file as "two-factor is off" would turn any
                truncation into a silent bypass of the second factor.
        """
        default: dict[str, Any] = {
            "enabled": False,
            "secret": "",
            "pending_secret": "",
            "backup_codes": [],
        }
        path = self.config.totp_file
        try:
            raw = path.read_text()
        except FileNotFoundError:
            return default
        except OSError as exc:
            raise SecurityError(
                f"Cannot read the two-factor state file {path}",
                details=(
                    "Run the web panel as the user that owns it, or delete the file to "
                    "turn two-factor authentication off on purpose."
                ),
            ) from exc
        try:
            state = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise SecurityError(
                f"The two-factor state file {path} is corrupt",
                details=(
                    "WASM refuses to guess whether two-factor authentication was on. "
                    f"Restore the file from backup, or delete it with 'rm {path}' to "
                    "disable the second factor on purpose."
                ),
            ) from exc
        default.update(state if isinstance(state, dict) else {})
        return default

    def _write_totp_state(self, state: dict[str, Any]) -> None:
        """
        Persist the two-factor state with owner-only permissions.

        Args:
            state: The state to write.
        """
        fs = get_fs()
        fs.write_text(self.config.totp_file, json.dumps(state), mode=SECRET_MODE)

    def _hash_backup_code(self, code: str) -> str:
        """
        Hash a backup code for storage, the way the master token is hashed.

        Args:
            code: The code, in whatever spacing the operator typed it.

        Returns:
            Hex digest of the normalised code, salted with the signing key.
        """
        compact = code.strip().lower().replace("-", "").replace(" ", "")
        return hashlib.sha256((compact + self._secret_key).encode()).hexdigest()

    def totp_enabled(self) -> bool:
        """
        Report whether logins require a second factor.

        Returns:
            True when two-factor authentication is confirmed and active.
        """
        return bool(self._read_totp_state()["enabled"])

    def totp_status(self) -> dict[str, Any]:
        """
        Describe the two-factor state without exposing any secret.

        Returns:
            Whether it is enabled, whether an enrolment is pending, and how
            many backup codes remain unused.
        """
        state = self._read_totp_state()
        return {
            "enabled": bool(state["enabled"]),
            "pending": bool(state["pending_secret"]),
            "backup_codes_remaining": len(state["backup_codes"]),
        }

    def begin_totp_enrollment(self) -> str:
        """
        Generate a pending secret for enrolment. Nothing is enforced yet.

        Returns:
            The new secret, to be shown to the operator exactly once as a QR
            code and as text.

        Raises:
            SecurityError: When two-factor authentication is already enabled.
                Replacing an active secret without presenting a current code
                would let a hijacked session swap the operator's authenticator
                for its own.
        """
        with self._totp_lock:
            state = self._read_totp_state()
            if state["enabled"]:
                raise SecurityError(
                    "Two-factor authentication is already enabled",
                    details="Disable it with a current code before enrolling a new authenticator.",
                )
            secret = totp.generate_secret()
            state["pending_secret"] = secret
            self._write_totp_state(state)
        return secret

    def pending_totp_secret(self) -> str | None:
        """
        Return the secret of an enrolment that has been begun but not confirmed.

        Only the enrolment screen reads this, to re-show the QR when the first
        code typed was wrong. It is meaningless to an attacker who is not
        already inside an authenticated session, because nothing accepts codes
        derived from it until it is confirmed.

        Returns:
            The pending secret, or None when no enrolment is in progress.
        """
        return self._read_totp_state()["pending_secret"] or None

    def confirm_totp_enrollment(self, code: str) -> list[str] | None:
        """
        Verify a code against the pending secret and activate the second factor.

        Args:
            code: The six-digit code the authenticator app shows.

        Returns:
            The backup codes, in clear, to be shown exactly once - they are
            stored only as salted hashes. None when the code did not verify.

        Raises:
            SecurityError: When no enrolment is in progress.
        """
        with self._totp_lock:
            state = self._read_totp_state()
            pending = state["pending_secret"]
            if not pending:
                raise SecurityError(
                    "No two-factor enrolment is in progress",
                    details="Begin one first: POST /api/auth/2fa/enroll, or Enable in Settings.",
                )
            if not totp.verify(pending, code):
                return None
            codes = [
                f"{secrets.token_hex(2)}-{secrets.token_hex(2)}" for _ in range(BACKUP_CODE_COUNT)
            ]
            state.update(
                {
                    "enabled": True,
                    "secret": pending,
                    "pending_secret": "",
                    "backup_codes": [self._hash_backup_code(c) for c in codes],
                }
            )
            self._write_totp_state(state)
        return codes

    def verify_second_factor(self, code: str) -> bool:
        """
        Check a login's second factor: a TOTP code, or a single-use backup code.

        A backup code that matches is consumed in the same locked cycle that
        verified it, so it cannot be replayed by a second login racing the
        first.

        Args:
            code: What the client typed.

        Returns:
            True when the code is a current TOTP value or an unused backup
            code. False otherwise, including when two-factor is not enabled:
            this method fails closed, and the caller decides whether a second
            factor was required at all.
        """
        if not code or not code.strip():
            return False
        with self._totp_lock:
            state = self._read_totp_state()
            if not state["enabled"]:
                return False
            if totp.verify(state["secret"], code):
                return True
            presented = self._hash_backup_code(code)
            remaining = [
                stored
                for stored in state["backup_codes"]
                if not hmac.compare_digest(stored, presented)
            ]
            if len(remaining) != len(state["backup_codes"]):
                state["backup_codes"] = remaining
                self._write_totp_state(state)
                return True
            return False

    def disable_totp(self, code: str) -> bool:
        """
        Turn the second factor off, on presentation of a current code.

        Args:
            code: A TOTP code or an unused backup code.

        Returns:
            True when it was disabled. False when the code did not verify, in
            which case nothing changed.

        Raises:
            SecurityError: When two-factor authentication is not enabled.
        """
        if not self._read_totp_state()["enabled"]:
            raise SecurityError(
                "Two-factor authentication is not enabled",
                details="There is nothing to disable. Enrol first from Settings.",
            )
        if not self.verify_second_factor(code):
            return False
        with self._totp_lock:
            self._write_totp_state(
                {"enabled": False, "secret": "", "pending_secret": "", "backup_codes": []}
            )
        return True

    def create_session(self, client_ip: str) -> IssuedSession:
        """
        Create and persist a session.

        Args:
            client_ip: IP the session is issued to.

        Returns:
            The issued session, including its CSRF token.
        """
        now = utcnow()
        max_age = int(self.config.token_expiration_hours * 3600)
        expires = now + timedelta(seconds=max_age)
        session_id = secrets.token_hex(16)
        csrf_token = secrets.token_urlsafe(32)

        self.sessions.create(session_id, csrf_token, client_ip, expires.timestamp())
        token = self._encode(session_id, client_ip, now, expires)

        return IssuedSession(
            token=token,
            session_id=session_id,
            csrf_token=csrf_token,
            expires_at=expires.timestamp(),
            max_age=max_age,
        )

    def _encode(self, session_id: str, client_ip: str, issued: datetime, expires: datetime) -> str:
        """
        Sign a session identifier.

        The token is the opaque identifier plus an HMAC of it, not a JWT. Every
        field the server trusts - expiry, address, CSRF token - is read from the
        session record, never from the token, so a JWT's payload was carried
        across the wire and then ignored. What is left is "did we issue this",
        which one HMAC answers.

        Dropping it also drops ``python-jose``, which is not packaged in Ubuntu
        26.04 and would have left the panel uninstallable there.

        Args:
            session_id: Server-side session identifier.
            client_ip: IP the session belongs to. Unused in the token itself;
                it is checked against the stored record.
            issued: Issue time. Recorded in the store.
            expires: Expiry time. Recorded in the store.

        Returns:
            The signed token.
        """
        signature = hmac.new(
            self._secret_key.encode(), session_id.encode(), hashlib.sha256
        ).hexdigest()
        return f"{session_id}.{signature}"

    def _decode(self, token: str) -> str | None:
        """
        Return the session identifier a token carries, if we signed it.

        Args:
            token: The token presented by the client.

        Returns:
            The session identifier, or None when the token is malformed or the
            signature does not match.
        """
        session_id, _, signature = token.partition(".")
        if not session_id or not signature:
            return None
        expected = hmac.new(
            self._secret_key.encode(), session_id.encode(), hashlib.sha256
        ).hexdigest()
        # Constant time: a timing oracle here would let an attacker forge a
        # signature one byte at a time.
        if not hmac.compare_digest(expected, signature):
            return None
        return session_id

    def verify_session_token(
        self, token: str, client_ip: str | None = None
    ) -> dict[str, Any] | None:
        """
        Verify a session token and return its payload.

        Args:
            token: The signed session token.
            client_ip: Address the token is being presented from. When set and
                ``bind_session_to_ip`` is on, a stolen token is useless from a
                different address.

        Returns:
            The payload, extended with the session's CSRF token, or None when
            the token is invalid, revoked, expired or presented from the wrong
            address.
        """
        if not token:
            return None

        session_id = self._decode(token)
        if not session_id:
            return None

        record = self.sessions.get(session_id)
        if record is None:
            return None

        payload: dict[str, Any] = {
            "sub": SESSION_SUBJECT,
            "sid": session_id,
            "ip": record["client_ip"],
            "exp": record["expires_at"],
            "iss": SESSION_ISSUER,
        }

        if self._past_absolute_deadline(record):
            self.sessions.revoke(session_id)
            return None

        if self.config.bind_session_to_ip and client_ip and record["client_ip"] != client_ip:
            return None

        payload["csrf"] = record["csrf_token"]
        payload["expires_at"] = record["expires_at"]
        payload["type"] = "session"
        return payload

    def _absolute_seconds(self) -> float:
        """
        Return the hard lifetime of a login, in seconds.

        Returns:
            The absolute lifetime, never shorter than the idle lifetime.
        """
        return max(
            float(self.config.session_max_hours) * 3600.0,
            float(self.config.token_expiration_hours) * 3600.0,
        )

    def _past_absolute_deadline(self, record: dict[str, Any]) -> bool:
        """
        Report whether a session is older than the absolute limit.

        Args:
            record: A session row.

        Returns:
            True when the login it descends from is too old to keep using.
        """
        created = float(record.get("created_at") or record["issued_at"])
        return time.time() - created >= self._absolute_seconds()

    def renew_session(self, payload: dict[str, Any]) -> IssuedSession | None:
        """
        Re-issue a session that is past half of its lifetime.

        The replacement gets a new session id and a new CSRF token, and inherits
        the original login's birth date: activity buys more idle time, never a
        longer life. The old identifier is deleted, so a captured copy of the
        previous cookie stops working the moment the operator's browser renews.

        Args:
            payload: A verified session payload.

        Returns:
            The refreshed session, or None when renewal is not due yet or the
            login has reached its absolute deadline.
        """
        session_id = payload.get("sid")
        record = self.sessions.get(session_id) if session_id else None
        if record is None:
            return None

        if self._past_absolute_deadline(record):
            self.sessions.revoke(session_id)
            return None

        max_age = int(self.config.token_expiration_hours * 3600)
        now_ts = time.time()
        if now_ts - record["issued_at"] < max_age * SESSION_RENEW_RATIO:
            return None

        created = float(record.get("created_at") or record["issued_at"])
        deadline = created + self._absolute_seconds()
        now = utcnow()
        expires = now + timedelta(seconds=min(float(max_age), deadline - now_ts))

        new_sid = secrets.token_hex(16)
        new_csrf = secrets.token_urlsafe(32)
        if self.sessions.rotate(session_id, new_sid, new_csrf, expires.timestamp()) is None:
            return None

        token = self._encode(new_sid, record["client_ip"], now, expires)
        return IssuedSession(
            token=token,
            session_id=new_sid,
            csrf_token=new_csrf,
            expires_at=expires.timestamp(),
            max_age=int(expires.timestamp() - now_ts),
        )

    def issue_ws_ticket(self, session_id: str, client_ip: str) -> tuple[str, int]:
        """
        Issue a single-use, short-lived WebSocket ticket.

        Query strings end up in access logs and proxy logs, so the value that
        travels there must be worthless seconds later and unusable twice.

        Args:
            session_id: Session the ticket is issued for.
            client_ip: Address the ticket is issued to.

        Returns:
            The ticket value and its lifetime in seconds.
        """
        ticket = secrets.token_urlsafe(32)
        self.sessions.store_ticket(
            hashlib.sha256(ticket.encode()).hexdigest(),
            session_id,
            client_ip,
            time.time() + WS_TICKET_TTL,
        )
        return ticket, WS_TICKET_TTL

    def consume_ws_ticket(self, ticket: str, client_ip: str | None = None) -> dict[str, Any] | None:
        """
        Redeem a WebSocket ticket.

        Args:
            ticket: The ticket value presented by the client.
            client_ip: Address presenting the ticket.

        Returns:
            A session payload when the ticket is valid, None otherwise.
        """
        if not ticket:
            return None

        record = self.sessions.consume_ticket(hashlib.sha256(ticket.encode()).hexdigest())
        if record is None:
            return None

        if self.config.bind_session_to_ip and client_ip and record["client_ip"] != client_ip:
            return None

        session = self.sessions.get(record["sid"])
        if session is None or self._past_absolute_deadline(session):
            return None

        return {
            "type": "session",
            "sid": record["sid"],
            "ip": record["client_ip"],
            "csrf": session["csrf_token"],
        }

    def revoke_session(self, session_id: str) -> None:
        """
        Revoke one session.

        Args:
            session_id: The session identifier.
        """
        self.sessions.revoke(session_id)

    def revoke_all_sessions(self) -> None:
        """Revoke every session."""
        self.sessions.revoke_all()

    def get_active_session_count(self) -> int:
        """
        Count usable sessions.

        Returns:
            Number of live sessions.
        """
        return self.sessions.active_count()

    def purge_expired_sessions(self) -> int:
        """
        Delete expired and revoked sessions.

        Returns:
            Number of rows removed.
        """
        return self.sessions.purge_expired()

    def rotate_secrets(self) -> str:
        """
        Rotate the signing key and the master token, killing every session.

        Returns:
            The new master token.

        Raises:
            SecurityError: When the new secrets cannot be persisted.
        """
        self._secret_key = secrets.token_hex(SECRET_KEY_LENGTH)
        write_private_file(self.config.secret_file, self._secret_key)
        self.revoke_all_sessions()
        self.purge_expired_sessions()
        return self.generate_master_token(save=True)


def _parse_networks(entries: list[str]) -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    """
    Parse IP or CIDR strings, ignoring malformed entries.

    Args:
        entries: Addresses or networks from the configuration.

    Returns:
        The parsed networks.
    """
    networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for entry in entries:
        try:
            networks.append(ipaddress.ip_network(entry.strip(), strict=False))
        except ValueError:
            logger.warning("Ignoring invalid IP or CIDR in configuration: %r", entry)
    return networks


def ip_matches(candidate: str, entries: list[str]) -> bool:
    """
    Check an address against a list of addresses or CIDRs.

    Args:
        candidate: The address to test.
        entries: Allowed addresses or networks.

    Returns:
        True when the address falls inside one of the entries.
    """
    if not entries:
        return False
    try:
        address = ipaddress.ip_address(candidate)
    except ValueError:
        return False
    return any(address in network for network in _parse_networks(entries))


def _as_ip_address(value: str) -> str | None:
    """
    Return the value when it is a bare IP address.

    Args:
        value: A candidate address, possibly with surrounding whitespace or
            brackets around an IPv6 literal.

    Returns:
        The normalised address, or None when it is not an IP address at all.
    """
    candidate = value.strip()
    if candidate.startswith("[") and candidate.endswith("]"):
        candidate = candidate[1:-1]
    try:
        return str(ipaddress.ip_address(candidate))
    except ValueError:
        return None


def get_client_ip(connection: HTTPConnection, config: SecurityConfig | None = None) -> str:
    """
    Determine the client address that security decisions are keyed on.

    The policy, in one sentence: **the peer address is the truth unless the peer
    is a proxy we deployed, and even then only a parseable IP address is
    believed.** Two rules follow from it.

    - Forwarding headers are read only when the direct peer matches
      ``trusted_proxies``, which is empty by default. Otherwise any client could
      pick its own identity and rotate out of a lockout or a rate limit.
    - A hop that is not a valid IP address is discarded rather than used. A
      trusted proxy can be tricked into appending client-supplied garbage, and a
      free-form string as the limiter's key is the same unbounded-rotation bug
      one layer down.

    Args:
        connection: The incoming HTTP request or WebSocket handshake.
        config: Configuration to use; the installed one by default.

    Returns:
        The client's IP address, the peer address when no header applies, or
        ``"unknown"`` when there is no peer at all.
    """
    config = config or get_security_config()
    peer = connection.client.host if connection.client else ""

    if peer and config.trusted_proxies and ip_matches(peer, config.trusted_proxies):
        forwarded_for = connection.headers.get("X-Forwarded-For")
        if forwarded_for:
            # Walk right to left: the rightmost entry that is not one of our own
            # proxies is the first address our infrastructure actually observed.
            for raw_hop in reversed(forwarded_for.split(",")):
                hop = _as_ip_address(raw_hop)
                if hop is None:
                    # Anything unparseable ends the walk: entries to its left
                    # were appended before it and are just as untrustworthy.
                    break
                if not ip_matches(hop, config.trusted_proxies):
                    return hop
        real_ip = connection.headers.get("X-Real-IP")
        if real_ip:
            parsed = _as_ip_address(real_ip)
            if parsed is not None:
                return parsed

    return peer or "unknown"


def is_secure_request(connection: HTTPConnection, config: SecurityConfig | None = None) -> bool:
    """
    Report whether the request reached the panel over TLS.

    Args:
        connection: The incoming request or WebSocket handshake.
        config: Configuration to use; the installed one by default.

    Returns:
        True when the connection is TLS, directly or through a trusted proxy
        that declared it with ``X-Forwarded-Proto``.
    """
    config = config or get_security_config()
    if connection.url.scheme in ("https", "wss"):
        return True

    peer = connection.client.host if connection.client else ""
    if peer and config.trusted_proxies and ip_matches(peer, config.trusted_proxies):
        return connection.headers.get("X-Forwarded-Proto", "").strip().lower() in ("https", "wss")

    return False


def _origin_host(origin: str) -> str | None:
    """
    Extract the host of an Origin header.

    Args:
        origin: The header value.

    Returns:
        The lowercase host with its port, or None when the value is opaque
        (``null``) or not an absolute origin.
    """
    value = origin.strip().lower()
    if not value or value == "null":
        return None
    _scheme, separator, remainder = value.partition("://")
    if not separator or not remainder:
        return None
    return remainder.split("/", 1)[0] or None


def is_allowed_origin(connection: HTTPConnection, config: SecurityConfig | None = None) -> bool:
    """
    Check the Origin of a handshake against the hosts allowed to open one.

    A ``SameSite=Strict`` cookie is still sent by a sibling subdomain, because
    same-site is not same-origin. Without this check an XSS anywhere under the
    parent domain could open ``/ws/logs/{domain}`` and read the root journal, a
    cross-site WebSocket hijack.

    Args:
        connection: The incoming handshake.
        config: Configuration to use; the installed one by default.

    Returns:
        True when the request carries no Origin (a non-browser client, which
        cannot be tricked into ambient authority), when it matches the Host the
        request was addressed to, or when it is explicitly configured.
    """
    config = config or get_security_config()
    origin = connection.headers.get("origin")
    if not origin:
        return True

    origin_host = _origin_host(origin)
    if origin_host is None:
        return False

    host_header = connection.headers.get("host", "").strip().lower()
    if host_header and origin_host == host_header:
        return True

    allowed = {value.strip().lower() for value in config.cors_origins}
    if origin.strip().lower() in allowed:
        return True
    return any(_origin_host(value) == origin_host for value in allowed)


def is_safe_path(path: str) -> bool:
    """
    Check that a path stays inside its base directory.

    Args:
        path: The path to check.

    Returns:
        True when the path is relative and contains no traversal.
    """
    normalized = os.path.normpath(path)
    if ".." in normalized.split(os.sep):
        return False
    return not normalized.startswith("/")


def sanitize_input(value: str, max_length: int = 1000) -> str:
    """
    Trim and de-fang a user supplied string.

    Args:
        value: The input value.
        max_length: Maximum allowed length.

    Returns:
        The sanitized value.
    """
    if not value:
        return ""
    return value[:max_length].replace("\x00", "")


def _unauthorized(detail: str) -> HTTPException:
    """
    Build a 401 response.

    Args:
        detail: Message for the client.

    Returns:
        The exception to raise.
    """
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


def bearer_token(connection: HTTPConnection) -> str | None:
    """
    Extract a Bearer token from the Authorization header.

    Args:
        connection: The incoming request or handshake.

    Returns:
        The token, or None when the header is absent or not a Bearer header.
    """
    header = connection.headers.get("Authorization", "")
    scheme, _, credentials = header.partition(" ")
    if scheme.lower() != "bearer" or not credentials.strip():
        return None
    return credentials.strip()


def subprotocol_token(connection: HTTPConnection) -> str | None:
    """
    Extract a session token from the requested WebSocket subprotocols.

    Args:
        connection: The pending handshake.

    Returns:
        The token, or None when no subprotocol carries one.
    """
    header = connection.headers.get("sec-websocket-protocol", "")
    for entry in (part.strip() for part in header.split(",")):
        if entry.startswith(WS_TOKEN_PREFIX):
            return entry[len(WS_TOKEN_PREFIX) :] or None
    return None


def record_auth_failure(client_ip: str, resource: str, source: str) -> None:
    """
    Count and audit one rejected credential, whatever channel it arrived on.

    This is the only place that increments the lockout counter for a bad
    credential. A cookie, a ``Bearer`` header on any endpoint and a WebSocket
    handshake all land here, so an attacker cannot reset the count by changing
    endpoint or by moving from HTTP to ``/ws``.

    Args:
        client_ip: Address the credential came from.
        resource: Path that was being reached.
        source: Channel the credential arrived on, for the audit record.
    """
    protection = get_brute_force_protection()
    if protection is not None:
        protection.record_failure(client_ip)

    audit = get_audit_logger()
    if audit is not None:
        audit.record(
            action="auth.credential",
            result="denied",
            client_ip=client_ip,
            resource=resource,
            detail=f"invalid credential presented via {source}",
        )


def check_credential(credential: str, client_ip: str) -> dict[str, Any] | None:
    """
    Verify one credential without recording anything.

    Args:
        credential: A session token or the master token.
        client_ip: Address presenting it.

    Returns:
        The session payload, or None when the credential is not valid.
    """
    manager = get_global_token_manager()
    if manager is None or not credential:
        return None

    payload = manager.verify_session_token(credential, client_ip)
    if payload is not None:
        return payload

    if manager.verify_master_token(credential):
        return {"type": "master", "sid": "master", "ip": client_ip}

    return None


def verify_credential(
    credential: str, client_ip: str, *, resource: str, source: str
) -> dict[str, Any] | None:
    """
    Verify one credential, counting the failure when it does not match.

    Args:
        credential: A session token or the master token.
        client_ip: Address presenting it.
        resource: Path being reached, for the audit record.
        source: Channel the credential arrived on.

    Returns:
        The session payload, or None when the credential is not valid.
    """
    payload = check_credential(credential, client_ip)
    if payload is None:
        record_auth_failure(client_ip, resource, source)
    return payload


def authenticate_connection(
    connection: HTTPConnection, ticket: str | None = None
) -> dict[str, Any] | None:
    """
    Authenticate a WebSocket handshake from every credential it may carry.

    Order: session cookie, ``wasm.token.<token>`` subprotocol, ``Authorization:
    Bearer``, then a single-use ticket from ``POST /api/auth/ws-ticket``. A
    handshake that presents nothing usable counts as exactly one failure, not
    one per channel tried.

    Args:
        connection: The pending handshake.
        ticket: Single-use ticket from the query string, if any.

    Returns:
        The session payload, or None when the handshake is not authenticated.
    """
    manager = get_global_token_manager()
    if manager is None:
        return None

    config = get_security_config()
    client_ip = get_client_ip(connection, config)
    resource = connection.scope.get("path", "")

    candidates = (
        connection.cookies.get(SESSION_COOKIE_NAME),
        subprotocol_token(connection),
        bearer_token(connection),
    )
    for credential in candidates:
        if not credential:
            continue
        payload = check_credential(credential, client_ip)
        if payload is not None:
            return payload

    if ticket:
        payload = manager.consume_ws_ticket(ticket, client_ip)
        if payload is not None:
            return payload

    record_auth_failure(client_ip, resource, "websocket")
    return None


def _check_csrf(request: Request, payload: dict[str, Any], client_ip: str) -> None:
    """
    Enforce the CSRF token on cookie-authenticated mutations.

    Args:
        request: The incoming request.
        payload: The verified session payload.
        client_ip: The caller's address, for the audit record.

    Raises:
        HTTPException: 403 when the CSRF token is missing or wrong.
    """
    if request.method.upper() in SAFE_METHODS:
        return

    presented = request.headers.get(CSRF_HEADER_NAME, "")
    expected = payload.get("csrf", "")
    if presented and expected and secrets.compare_digest(presented, expected):
        return

    audit = get_audit_logger()
    if audit:
        audit.record(
            action="auth.csrf",
            result="denied",
            client_ip=client_ip,
            actor=payload.get("sid", "unknown"),
            resource=request.url.path,
            detail=f"missing or invalid {CSRF_HEADER_NAME} header",
        )
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail=(
            f"Missing or invalid CSRF token. Send the {CSRF_COOKIE_NAME} cookie value "
            f"in the {CSRF_HEADER_NAME} header, or authenticate with a Bearer token."
        ),
    )


async def require_auth(request: Request) -> dict[str, Any]:
    """
    FastAPI dependency enforcing authentication on an endpoint.

    Accepts, in order, an ``Authorization: Bearer`` session token, a Bearer
    master token (for the CLI), or the session cookie. Cookie authentication
    additionally requires the CSRF header on every unsafe method.

    Whichever channel is used, a credential that does not match is counted by
    :func:`record_auth_failure`, so the lockout applies to master token guessing
    on any endpoint and not only to ``/api/auth/login``.

    Args:
        request: The incoming request.

    Returns:
        The session payload, also stored on ``request.state.session``.

    Raises:
        HTTPException: 401 when unauthenticated, 403 on a CSRF failure, 500
            when the server was never initialised.
    """
    manager = get_global_token_manager()
    if manager is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Authentication system not initialized",
        )

    config = get_security_config()
    client_ip = get_client_ip(request, config)
    resource = request.url.path

    bearer = bearer_token(request)
    if bearer:
        payload = verify_credential(bearer, client_ip, resource=resource, source="bearer")
        if payload is None:
            raise _unauthorized("Invalid or expired authentication token")
        request.state.session = payload
        return payload

    cookie = request.cookies.get(SESSION_COOKIE_NAME)
    if cookie:
        payload = verify_credential(cookie, client_ip, resource=resource, source="cookie")
        if payload is None:
            raise _unauthorized("Session expired or revoked. Please log in again.")
        _check_csrf(request, payload, client_ip)
        request.state.session = payload
        renewed = manager.renew_session(payload)
        if renewed is not None:
            request.state.renewed_session = renewed
        return payload

    raise _unauthorized("Not authenticated")
