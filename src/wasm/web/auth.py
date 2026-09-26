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

import errno
import hashlib
import hmac
import ipaddress
import json
import logging
import math
import os
import re
import secrets
import sqlite3
import stat
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import HTTPException, Request, status
from starlette.requests import HTTPConnection

from wasm.core import totp
from wasm.core.exceptions import SecurityError
from wasm.core.fs import SECRET_MODE, get_fs, is_rehearsal

logger = logging.getLogger(__name__)

#: Bytes of entropy in the master token and in the signing key.
TOKEN_LENGTH = 32
SECRET_KEY_LENGTH = 64

#: Five wrong tokens is far more than a human typing a copy-pasted secret needs,
#: and the 15 minute lockout turns an online guessing attack against a 256-bit
#: token into something with no practical end.
MAX_FAILED_ATTEMPTS = 5
LOCKOUT_DURATION = 900

#: The budget of a request that carries no valid credential, per client IP:
#: the sign-in page, the forge webhooks, a scanner. Nothing anonymous needs 2/s
#: sustained, and it is what an attacker without a credential gets.
RATE_LIMIT_WINDOW = 60
RATE_LIMIT_MAX_REQUESTS = 120

#: The budget of a request that carries a valid credential, per credential
#: (one sign-in, one API token, the master token) rather than per address. The
#: console alone exceeds 120 a minute following a deploy and navigating, and
#: over an SSH tunnel every request comes from 127.0.0.1, so a per-address
#: budget made every tab and script share one. Holding a valid credential is
#: what guessing one would win, so this protects the machine from a runaway
#: client, not from an attacker; credential guessing is the lockout's job.
RATE_LIMIT_AUTHENTICATED_MAX_REQUESTS = 1200

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

#: Wrong webhook signatures one application's hook tolerates inside
#: :data:`WEBHOOK_LOCKOUT_DURATION` before it refuses deliveries for that long.
#: Counted per application, never per address: a forge delivers every
#: customer's webhooks from a few shared addresses, so an address lockout fed
#: by signatures let anyone with an account on the forge cut off everyone.
#: Ten is more wrong deliveries than a misconfigured secret produces between
#: the operator noticing and fixing it; the secret itself is 256 bits.
WEBHOOK_MAX_FAILURES = 10
WEBHOOK_LOCKOUT_DURATION = 900

#: Largest request body the panel reads, checked before authentication. The
#: API's biggest legitimate bodies - a unit file, a site configuration, an
#: ``.env`` - are a few kilobytes; a mebibyte is generous and still bounded.
MAX_BODY_BYTES = 1024 * 1024

#: The forge webhook's own cap. GitHub sends push payloads of up to 25 MB, but
#: those are pushes of hundreds of commits; the hook reads nothing but the
#: branch, and an ordinary push is a few kilobytes. A delivery over this is
#: refused with 413, which the forge shows in its delivery log.
MAX_HOOK_BODY_BYTES = 5 * 1024 * 1024

#: WebSockets one credential may hold open at once. Every log socket is a
#: ``journalctl -f`` running as root, so without a cap one token could hold
#: the process table hostage. A console tab uses one or two (the log drawer,
#: a job it follows); eight leaves room for several tabs and a script.
WS_MAX_PER_CREDENTIAL = 8

#: Subprotocol prefix carrying a session token, for clients that cannot send a
#: cookie: ``Sec-WebSocket-Protocol: wasm.auth, wasm.token.<token>``.
WS_SUBPROTOCOL = "wasm.auth"
WS_TOKEN_PREFIX = "wasm.token."  # noqa: S105 - subprotocol prefix, not a credential

#: Prefix that routes a Bearer credential to the API token table instead of
#: the session store or the master token. The master token's own prefix is
#: ``wasm_``, so the two are distinguishable at a glance in a config file.
API_TOKEN_PREFIX = "wasm_tok_"  # noqa: S105 - a prefix, not a credential

#: The ``sid`` of the master token's payload, and of a WebSocket ticket it was
#: issued to. API tokens use ``token:<name>``; cookie sessions a random hex id.
MASTER_SID = "master"
API_TOKEN_SID_PREFIX = "token:"  # noqa: S105 - a prefix, not a credential

#: API token scopes, weakest first. The order is the hierarchy: a scope
#: satisfies every requirement at or below its own rank.
API_TOKEN_SCOPES = ("read", "deploy", "admin")
SCOPE_RANK = {scope: rank for rank, scope in enumerate(API_TOKEN_SCOPES)}

#: How often a token's last_used_at is written, at most. Recording every
#: request would turn a polling dashboard into a constant stream of writes to
#: a database whose value here is "when was this credential last alive".
API_TOKEN_LAST_USED_THROTTLE = 60

#: The mutations a ``deploy`` scope is for: moving an application that already
#: exists - an update, a rollback. Every other mutation - creating an
#: application, inspecting a source, deleting, editing configuration, managing
#: credentials - stays ``admin``. Creating and inspecting are not deploy
#: operations in this sense: both fetch a source the caller names and build or
#: read it as root, so a token handed to CI would otherwise be able to point
#: the machine at any repository, or any directory on it. This is the whole
#: scope policy, stated once and enforced at the same chokepoint that resolves
#: the credential; see :func:`required_scope`.
DEPLOY_SCOPE_PATHS = frozenset(
    {
        "/api/jobs/update",
        "/api/jobs/rollback",
    }
)

#: The same policy for mutations whose path carries a parameter, which no
#: exact path can name. Each pattern is anchored at both ends and a segment
#: never matches a ``/``, so a pattern cannot be satisfied by a longer path
#: that merely contains it.
DEPLOY_SCOPE_PATTERNS: tuple[re.Pattern[str], ...] = (
    # Activating a release is an instant rollback: the same act as queueing one.
    re.compile(r"^/api/apps/[^/]+/releases/[^/]+/activate$"),
    # Rebuilding a deployment's commit is an update; going back to what a
    # deployment produced is a rollback.
    re.compile(r"^/api/apps/[^/]+/deployments/[0-9]+/(rebuild|rollback)$"),
)

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

#: How long POST /api/auth/elevate confirms a cookie session for, per D5:
#: "marca la sesion como elevada 10 minutos". Destructive actions - deleting
#: an app, a database, a service or a site, revealing a .env, writing raw
#: config - all require it.
ELEVATION_SECONDS = 600

#: Indirection over time.time(), so a test can advance a fake clock to expire
#: an elevation window without perturbing session expiry, which is computed
#: from the real wall clock elsewhere in this module.
_now: Callable[[], float] = time.time


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
        rate_limit_enabled: Whether rate limiting is applied.
        rate_limit_requests: Requests without a valid credential allowed per
            window and per client IP.
        rate_limit_authenticated_requests: Requests with a valid credential
            allowed per window and per credential.
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
        ws_max_per_credential: WebSockets one credential may hold open at once.
        max_body_bytes: Largest request body read, before authentication.
        max_hook_body_bytes: The same for forge deliveries under ``/hooks/``.
        webhook_max_failures: Wrong signatures one application's hook takes
            before it refuses deliveries for ``webhook_lockout_duration``.
        webhook_lockout_duration: Seconds that window and that refusal last.
    """

    host: str = "127.0.0.1"
    port: int = 8080
    allowed_hosts: list[str] = field(default_factory=lambda: ["127.0.0.1", "localhost"])
    enable_cors: bool = False
    cors_origins: list[str] = field(default_factory=list)
    rate_limit_enabled: bool = True
    rate_limit_requests: int = RATE_LIMIT_MAX_REQUESTS
    rate_limit_authenticated_requests: int = RATE_LIMIT_AUTHENTICATED_MAX_REQUESTS
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
    ws_max_per_credential: int = WS_MAX_PER_CREDENTIAL
    max_body_bytes: int = MAX_BODY_BYTES
    max_hook_body_bytes: int = MAX_HOOK_BODY_BYTES
    webhook_max_failures: int = WEBHOOK_MAX_FAILURES
    webhook_lockout_duration: int = WEBHOOK_LOCKOUT_DURATION

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

    Through the filesystem seam, like every other change WASM makes, so a
    ``--dry-run`` reports the directory it would create and creates nothing.

    Args:
        path: Directory that must exist and be private.

    Raises:
        SecurityError: When the directory cannot be created.
    """
    fs = get_fs()
    try:
        if not path.is_dir():
            fs.make_dir(path, mode=DIR_MODE, parents=True)
        if not path.is_dir():
            if is_rehearsal():
                # The seam declined to create it; there is nothing to tighten.
                return
            # make_dir leaves an existing entry alone, so a file squatting on
            # the name is only noticed here.
            raise NotADirectoryError(errno.ENOTDIR, os.strerror(errno.ENOTDIR), str(path))
        if stat.S_IMODE(path.stat().st_mode) != DIR_MODE:
            fs.chmod(path, DIR_MODE)
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

    Through the filesystem seam, which writes beside the destination and
    renames over it with the mode set at creation: a running console
    re-reads the signing key and the token hash whenever they change (see
    :class:`TokenManager`), and a truncate-then-write would let it read the
    empty file in between. Under ``--dry-run`` nothing is written; the
    caller keeps what it meant to write in memory for the rest of the run.

    Args:
        path: Destination file.
        content: Text to write.

    Raises:
        SecurityError: When the file cannot be written.
    """
    ensure_state_dir(path.parent)
    try:
        get_fs().write_text(path, content, mode=FILE_MODE)
    except OSError as exc:
        raise SecurityError(
            f"Cannot write {path}",
            details=(
                "Run the web panel as root, or set "
                f"{STATE_DIR_ENV} to a directory the current user owns."
            ),
        ) from exc


def _generation(secret_material: str) -> str:
    """
    Digest a secret into a label that says which issue of it is in force.

    Args:
        secret_material: A stored hash or a signing key.

    Returns:
        The first 16 hex characters of its SHA-256: enough to tell two issues
        apart, and nothing a caller could turn back into the material.
    """
    return hashlib.sha256(secret_material.encode()).hexdigest()[:16]


def _file_stamp(path: Path) -> tuple[int, int, int, int] | None:
    """
    Identify one version of a state file without reading it.

    Args:
        path: The file to identify.

    Returns:
        Inode, size and modification and change times, which together change
        on every :func:`write_private_file` (it renames a new file into
        place), or None when the file cannot be stat'ed.
    """
    try:
        info = path.stat()
    except OSError:
        return None
    return (info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


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
    """
    Sliding window request limiter.

    Keyed by whatever the caller counts by: the server keeps one keyed by
    client IP for anonymous requests and one keyed by credential for
    authenticated ones (see :func:`rate_limit_identity`).
    """

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

    def retry_after(self, client_ip: str) -> int:
        """
        Report how long until the client may make its next request.

        Args:
            client_ip: The client's key.

        Returns:
            Whole seconds until the oldest request in the window expires,
            at least 1; 0 when the client is within its budget.
        """
        now = time.time()
        with self._lock:
            timestamps = [ts for ts in self._requests.get(client_ip, []) if now - ts < self.window]
        if len(timestamps) < self.max_requests:
            return 0
        if not timestamps:
            # A budget of zero: nothing ever frees a slot.
            return self.window
        # The request whose expiry brings the count back under the budget.
        freeing = timestamps[len(timestamps) - self.max_requests]
        return max(1, math.ceil(freeing + self.window - now))

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


class ConnectionLimiter:
    """
    Counts the long-lived connections each credential holds open.

    Keyed by :func:`credential_key`, not by address: the cost being bounded -
    a process and a socket per stream - is spent on behalf of a credential,
    and one script at its limit must not stop the operator's console from
    opening its own.
    """

    def __init__(self, limit: int = WS_MAX_PER_CREDENTIAL) -> None:
        """
        Args:
            limit: Connections allowed per credential at once.
        """
        self.limit = limit
        self._open: dict[str, int] = {}
        self._lock = threading.Lock()

    def acquire(self, key: str) -> bool:
        """
        Take a place for one more connection, if there is one.

        Args:
            key: The credential's key.

        Returns:
            True when the connection may open; the caller must
            :meth:`release` it when it closes. False when the credential is
            already at its limit.
        """
        with self._lock:
            held = self._open.get(key, 0)
            if held >= self.limit:
                return False
            self._open[key] = held + 1
            return True

    def release(self, key: str) -> None:
        """
        Give back the place a closed connection held.

        Args:
            key: The credential's key, as passed to :meth:`acquire`.
        """
        with self._lock:
            held = self._open.get(key, 0) - 1
            if held > 0:
                self._open[key] = held
            else:
                self._open.pop(key, None)

    def held(self, key: str) -> int:
        """
        Args:
            key: The credential's key.

        Returns:
            How many connections it holds open now.
        """
        with self._lock:
            return self._open.get(key, 0)


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
        try:
            if is_rehearsal():
                self._conn = self._rehearsal_copy(path)
            else:
                ensure_state_dir(path.parent)
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

    @staticmethod
    def _rehearsal_copy(path: Path) -> sqlite3.Connection:
        """
        Open a private, in-memory copy of the database for a ``--dry-run``.

        SQLite writes past the filesystem seam, so a rehearsal cannot open the
        real file: ``wasm --dry-run token create`` would issue a live token,
        and on a fresh machine simply connecting creates the file. The copy
        answers reads with the real state and forgets every write when the
        process ends, which is what a rehearsal promises.

        Args:
            path: The database file, which may not exist.

        Returns:
            An in-memory connection holding whatever the file held.

        Raises:
            sqlite3.Error: When the existing file cannot be read.
        """
        memory = sqlite3.connect(":memory:", check_same_thread=False)
        if path.is_file():
            source = sqlite3.connect(f"file:{quote(str(path.resolve()))}?mode=ro", uri=True)
            try:
                source.backup(memory)
            finally:
                source.close()
        return memory

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
            if "elevated_until" not in columns:
                # NULL by default: a session predating sudo mode, like a
                # freshly created one, has not confirmed anything yet.
                self._conn.execute("ALTER TABLE sessions ADD COLUMN elevated_until REAL")
            if "family" not in columns:
                # The login a session descends from. Renewal replaces the sid,
                # so the sid alone cannot say "this is still the same sign-in"
                # - which is what a stream opened before the renewal, and a
                # logout that must also end the predecessor, need to ask.
                self._conn.execute("ALTER TABLE sessions ADD COLUMN family TEXT")
                self._conn.execute("UPDATE sessions SET family = sid")
            if "rotated_to" not in columns:
                # The one successor a renewal minted. Set once, so a retired
                # sid in its grace period re-issues that successor instead of
                # minting another on every request still carrying it.
                self._conn.execute("ALTER TABLE sessions ADD COLUMN rotated_to TEXT")
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
            # The token itself is never stored: only its salted hash, exactly
            # like the master token. Names stay unique across revocations so
            # an audit line naming a token always names one thing.
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS api_tokens (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE,
                    token_hash TEXT NOT NULL UNIQUE,
                    scope TEXT NOT NULL CHECK (scope IN ('read', 'deploy', 'admin')),
                    created_at REAL NOT NULL,
                    expires_at REAL,
                    last_used_at REAL,
                    revoked_at REAL
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
                "(sid, csrf_token, client_ip, issued_at, created_at, expires_at, revoked, family) "
                "VALUES (?, ?, ?, ?, ?, ?, 0, ?)",
                (
                    sid,
                    csrf_token,
                    client_ip,
                    now,
                    now if created_at is None else created_at,
                    expires_at,
                    sid,
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

    def set_elevated(self, sid: str, elevated_until: float | None) -> None:
        """
        Record or clear a session's sudo-mode confirmation.

        Args:
            sid: Session identifier.
            elevated_until: Timestamp the elevation expires at, or None to
                drop it, for example on logout.
        """
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE sessions SET elevated_until = ? WHERE sid = ?", (elevated_until, sid)
            )

    def rotate(self, old_sid: str, new_sid: str, csrf_token: str, expires_at: float) -> dict | None:
        """
        Replace a session with a fresh identifier in one transaction.

        A session rotates once. The check and the write share the lock and
        the transaction, so two requests racing with the same retired cookie
        cannot both mint a successor.

        Args:
            old_sid: Session being retired.
            new_sid: Identifier of the replacement.
            csrf_token: CSRF token of the replacement.
            expires_at: Expiry of the replacement, as a UNIX timestamp.

        Returns:
            The row of the retired session, or None when it no longer exists
            or was already rotated - ask :meth:`get` for its ``rotated_to``.
        """
        now = time.time()
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT * FROM sessions WHERE sid = ? AND revoked = 0 AND rotated_to IS NULL",
                (old_sid,),
            ).fetchone()
            if row is None:
                return None
            self._conn.execute(
                "INSERT OR REPLACE INTO sessions "
                "(sid, csrf_token, client_ip, issued_at, created_at, expires_at, revoked, "
                "elevated_until, family) VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?)",
                (
                    new_sid,
                    csrf_token,
                    row["client_ip"],
                    now,
                    row["created_at"],
                    expires_at,
                    # Renewal must not reset the 10 minute window, only carry
                    # whatever is left of it to the new session id.
                    row["elevated_until"],
                    row["family"] or old_sid,
                ),
            )
            # The retired identifier is not deleted outright: a dashboard fires
            # several requests at once and they all still carry the old cookie.
            # It is given a short grace instead, after which a captured copy of
            # the previous cookie is worthless.
            self._conn.execute(
                "UPDATE sessions SET expires_at = MIN(expires_at, ?), rotated_to = ? WHERE sid = ?",
                (now + SESSION_ROTATION_GRACE, new_sid, old_sid),
            )
        return dict(row)

    def family_alive(self, family: str) -> dict[str, Any] | None:
        """
        Find the live session of a sign-in, whatever its sid is by now.

        Args:
            family: The ``family`` of a session: the sid its login started
                with, inherited by every renewal.

        Returns:
            The most recently issued live row of that sign-in, or None when
            every session of it is revoked or expired.
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM sessions WHERE family = ? AND revoked = 0 AND expires_at > ? "
                "ORDER BY issued_at DESC LIMIT 1",
                (family, time.time()),
            ).fetchone()
        return dict(row) if row else None

    def revoke(self, sid: str) -> None:
        """
        Revoke one sign-in: the session and every sid it renewed from or into.

        A renewal leaves its predecessor alive for a short grace; revoking
        only the current sid would leave that predecessor - and a stream
        opened with it - working after a logout.

        Args:
            sid: Session identifier, current or retired.
        """
        with self._lock, self._conn:
            row = self._conn.execute("SELECT family FROM sessions WHERE sid = ?", (sid,)).fetchone()
            family = row["family"] if row is not None and row["family"] else sid
            members = [
                str(member["sid"])
                for member in self._conn.execute(
                    "SELECT sid FROM sessions WHERE family = ? OR sid = ?", (family, sid)
                ).fetchall()
            ]
            for member in members or [sid]:
                self._conn.execute("UPDATE sessions SET revoked = 1 WHERE sid = ?", (member,))
                self._conn.execute("DELETE FROM ws_tickets WHERE sid = ?", (member,))

    def revoke_all(self) -> None:
        """Revoke every session and drop every pending ticket."""
        with self._lock, self._conn:
            self._conn.execute("UPDATE sessions SET revoked = 1")
            self._conn.execute("DELETE FROM ws_tickets")

    def revoke_all_except(self, keep_sid: str) -> int:
        """
        Revoke every session but one, and drop every pending ticket but its own.

        Args:
            keep_sid: Session identifier to leave untouched.

        Returns:
            Number of sessions revoked.
        """
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT family FROM sessions WHERE sid = ?", (keep_sid,)
            ).fetchone()
            family = row["family"] if row is not None and row["family"] else keep_sid
            # The kept sign-in keeps its whole lineage: the predecessor still
            # in its grace is the same operator's requests in flight.
            cursor = self._conn.execute(
                "UPDATE sessions SET revoked = 1 "
                "WHERE sid != ? AND COALESCE(family, sid) != ? AND revoked = 0",
                (keep_sid, family),
            )
            self._conn.execute(
                "DELETE FROM ws_tickets WHERE sid != ? AND sid NOT IN "
                "(SELECT sid FROM sessions WHERE family = ?)",
                (keep_sid, family),
            )
        return cursor.rowcount

    def active_count(self) -> int:
        """
        Count sessions that are still usable.

        Returns:
            Number of live sessions.
        """
        with self._lock:
            # A renewed session's predecessor, alive for its grace period, is
            # the same sign-in, not another one.
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM sessions "
                "WHERE revoked = 0 AND expires_at > ? AND rotated_to IS NULL",
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

    def list_active(self) -> list[dict[str, Any]]:
        """
        List every session that is still usable, newest activity first.

        Returns:
            The live session rows as dicts.
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT sid, client_ip, issued_at, created_at, expires_at FROM sessions "
                "WHERE revoked = 0 AND expires_at > ? AND rotated_to IS NULL "
                "ORDER BY issued_at DESC",
                (time.time(),),
            ).fetchall()
        return [dict(row) for row in rows]

    def sids_with_prefix(self, prefix: str) -> list[str]:
        """
        Find the live sessions whose identifier starts with a prefix.

        Args:
            prefix: Leading characters of a session id. The caller has already
                validated it as hexadecimal, so it cannot carry LIKE wildcards.

        Returns:
            The matching session identifiers.
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT sid FROM sessions WHERE revoked = 0 AND expires_at > ? "
                "AND rotated_to IS NULL AND sid LIKE ? ORDER BY sid",
                (time.time(), prefix + "%"),
            ).fetchall()
        return [str(row["sid"]) for row in rows]

    def create_api_token(
        self,
        name: str,
        token_hash: str,
        scope: str,
        created_at: float,
        expires_at: float | None,
    ) -> int:
        """
        Persist a new API token record.

        Args:
            name: Unique human-chosen name.
            token_hash: Salted hash of the token; the token itself never lands.
            scope: One of :data:`API_TOKEN_SCOPES`.
            created_at: Creation time as a UNIX timestamp.
            expires_at: Expiry as a UNIX timestamp, or None for no expiry.

        Returns:
            The new record's id.

        Raises:
            sqlite3.IntegrityError: When the name is already taken.
        """
        with self._lock, self._conn:
            cursor = self._conn.execute(
                "INSERT INTO api_tokens (name, token_hash, scope, created_at, expires_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (name, token_hash, scope, created_at, expires_at),
            )
        return int(cursor.lastrowid or 0)

    def get_api_token(self, token_hash: str) -> dict[str, Any] | None:
        """
        Fetch an unrevoked API token by its hash.

        Expiry is deliberately left to the caller: "expired" and "unknown" are
        the same refusal on the wire, but the caller decides both from one row.

        Args:
            token_hash: Salted hash of the presented token.

        Returns:
            The token row as a dict, or None when unknown or revoked.
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM api_tokens WHERE token_hash = ? AND revoked_at IS NULL",
                (token_hash,),
            ).fetchone()
        return dict(row) if row else None

    def get_api_token_by_name(self, name: str) -> dict[str, Any] | None:
        """
        Fetch an unrevoked API token by its name.

        Names are unique across every token ever issued, so a name names one
        credential for good; a WebSocket ticket is bound to one this way.

        Args:
            name: The token's name.

        Returns:
            The token row as a dict, or None when unknown or revoked.
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM api_tokens WHERE name = ? AND revoked_at IS NULL", (name,)
            ).fetchone()
        return dict(row) if row else None

    def touch_api_token(self, token_id: int, used_at: float) -> None:
        """
        Record when a token last authenticated a request.

        Args:
            token_id: The token record's id.
            used_at: The moment of use, as a UNIX timestamp.
        """
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE api_tokens SET last_used_at = ? WHERE id = ?", (used_at, token_id)
            )

    def list_api_tokens(self) -> list[dict[str, Any]]:
        """
        List every API token record, newest first, without the hashes.

        Returns:
            The token rows as dicts.
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, name, scope, created_at, expires_at, last_used_at, revoked_at "
                "FROM api_tokens ORDER BY created_at DESC, id DESC"
            ).fetchall()
        return [dict(row) for row in rows]

    def revoke_api_token(self, token_id: int) -> str | None:
        """
        Revoke one API token.

        Args:
            token_id: The token record's id.

        Returns:
            The token's name, for the audit record, or None when no such
            record exists. Revoking an already revoked token is a no-op that
            still reports the name: the outcome the caller asked for holds.
        """
        now = time.time()
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT name FROM api_tokens WHERE id = ?", (token_id,)
            ).fetchone()
            if row is None:
                return None
            self._conn.execute(
                "UPDATE api_tokens SET revoked_at = COALESCE(revoked_at, ?) WHERE id = ?",
                (now, token_id),
            )
        return str(row["name"])

    def store_ticket(self, ticket_hash: str, sid: str, client_ip: str, expires_at: float) -> None:
        """
        Persist a single-use WebSocket ticket.

        Args:
            ticket_hash: Hash of the ticket value.
            sid: The ``sid`` of the credential the ticket was issued to: a
                session id, :data:`MASTER_SID` or ``token:<name>``.
            client_ip: IP the ticket was issued to.
            expires_at: Expiry as a UNIX timestamp.
        """
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO ws_tickets (ticket_hash, sid, client_ip, expires_at) "
                "VALUES (?, ?, ?, ?)",
                (ticket_hash, sid, client_ip, expires_at),
            )

    def revoke_tickets(self, sid: str) -> None:
        """
        Spend every outstanding WebSocket ticket issued to one credential.

        Args:
            sid: The ``sid`` the tickets were issued to.
        """
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM ws_tickets WHERE sid = ?", (sid,))

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
        # A rehearsal writes no file, the audit log included; what would have
        # been recorded goes to the process log instead, so it is not lost.
        self._rehearsal = is_rehearsal()
        if not enabled or self._rehearsal:
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
            actor: Who acted, as :func:`actor_label` names them, or
                ``anonymous`` before any credential was verified.
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
        if self._rehearsal:
            logger.info("Audit entry not written during a rehearsal: %s", payload.decode().strip())
            return
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

    def _files_newest_first(self) -> list[Path]:
        """
        List every log file this logger has written, most recent first.

        Returns:
            The current file, then each rotated backup in age order, for
            whichever of them actually exist.
        """
        files = [self.path]
        for index in range(1, self.backups + 1):
            candidate = self.path.with_name(f"{self.path.name}.{index}")
            if candidate.exists():
                files.append(candidate)
        return files

    def _iter_entries(self) -> Iterator[dict[str, Any]]:
        """
        Yield every recorded entry, newest first, across rotated files.

        A file's own lines are already chronological, so reading each file
        backwards and reading the files themselves in rotation order (current,
        then ``.1``, then ``.2``, ...) gives a single newest-first stream
        without loading the whole log into memory to sort it.

        Yields:
            Each entry as the dict :meth:`record` wrote, malformed lines
            skipped rather than failing the whole read.
        """
        for path in self._files_newest_first():
            try:
                lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue
            for line in reversed(lines):
                if not line.strip():
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(entry, dict):
                    yield entry

    def read(
        self,
        *,
        limit: int = 50,
        before: str | None = None,
        action: str | None = None,
        result: str | None = None,
        actor: str | None = None,
    ) -> list[dict[str, Any]]:
        """
        Read audit entries, newest first, with keyset pagination.

        Args:
            limit: Maximum number of entries to return.
            before: Only entries strictly older than this ``ts`` value - the
                timestamp of the last entry from a previous call, so the next
                page picks up exactly where it left off even as new entries
                keep being appended between calls.
            action: Only entries with this exact action, when given.
            result: Only entries with this exact result, when given.
            actor: Only entries with this exact actor, when given.

        Returns:
            Up to ``limit`` matching entries, newest first. Empty when
            auditing is disabled.
        """
        if not self.enabled:
            return []

        matched: list[dict[str, Any]] = []
        for entry in self._iter_entries():
            timestamp = entry.get("ts", "")
            if before is not None and not (str(timestamp) < before):
                continue
            if action is not None and entry.get("action") != action:
                continue
            if result is not None and entry.get("result") != result:
                continue
            if actor is not None and entry.get("actor") != actor:
                continue
            matched.append(entry)
            if len(matched) >= limit:
                break
        return matched


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

    The files in the state directory are the only record of the master token
    and the key, and a running console answers from them, not from a copy it
    took at start. ``wasm web token --new`` and ``--regenerate`` run in a
    process of their own; if the console kept the token it issued in memory,
    the token those commands retire would keep opening it until a restart -
    and the restart would issue yet another token. So the token hash is read
    on every verification and the key is re-read whenever its file changes.
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
        # Stamped before reading, so a rotation landing between the two makes
        # the next _signing_key() call read again instead of being missed.
        self._secret_stamp = _file_stamp(self.config.secret_file)
        self._secret_key: str = self._load_or_create_secret()
        if self._secret_stamp is None:
            self._secret_stamp = _file_stamp(self.config.secret_file)
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

    def generate_master_token(self) -> str:
        """
        Issue a new master token, retiring the previous one everywhere.

        Only the hash is persisted, and every console verifies against it, so
        the previous token stops working in a running console on its next
        request, not on its next restart.

        Returns:
            The generated master token, which is shown to the operator once.

        Raises:
            SecurityError: When the token hash cannot be persisted.
        """
        token = f"wasm_{secrets.token_urlsafe(TOKEN_LENGTH)}"
        write_private_file(self.config.token_file, self._hash_token(token))
        # A ticket issued to the retired token would otherwise outlive it by
        # up to WS_TICKET_TTL seconds. The table is shared with a running
        # console, so this holds across processes too.
        self.sessions.revoke_tickets(MASTER_SID)
        return token

    def _signing_key(self) -> str:
        """
        The signing key in force, re-read when another process rotated it.

        ``wasm web token --regenerate`` rewrites the key file from its own
        process. A console holding on to the key it loaded would refuse the
        token issued under the new key and keep signing sessions that the next
        start rejects. A stat per call is the price of noticing.

        Returns:
            The hex-encoded signing key.
        """
        secret_file = self.config.secret_file
        stamp = _file_stamp(secret_file)
        if stamp is None or stamp == self._secret_stamp:
            # A key file that vanished is not a rotation: the key in memory
            # stays in force until a new file appears, since inventing one
            # here is exactly the silent regeneration _load_or_create_secret
            # refuses.
            return self._secret_key
        # Recorded before reading so an unreadable or empty file is reported
        # once, not on every request; any repair changes the stamp again.
        self._secret_stamp = stamp
        try:
            key = secret_file.read_text().strip()
        except OSError as exc:
            logger.error("Cannot re-read the web signing key %s: %s", secret_file, exc)
            return self._secret_key
        if not key:
            logger.error(
                "The web signing key %s is empty; keeping the key already loaded", secret_file
            )
            return self._secret_key
        self._secret_key = key
        return key

    def _hash_token(self, token: str) -> str:
        """
        Hash a master token for storage.

        Args:
            token: The plaintext token.

        Returns:
            Hex digest of the token, salted with the signing key.
        """
        return hashlib.sha256((token + self._signing_key()).encode()).hexdigest()

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
        Verify a master token against the hash on record right now.

        Args:
            token: The token presented by the client.

        Returns:
            True when the token matches the stored hash. The file is read on
            every call, so a token issued by another process is accepted and
            the one it replaced is refused from the next request on.
        """
        return self.master_token_generation(token) is not None

    def master_token_generation(self, token: str) -> str | None:
        """
        Verify a master token and name which issue of it matched.

        A stream opened with the master token stays open long after the
        handshake; it is re-checked by comparing this value with
        :meth:`current_master_generation`, so ``wasm web token --new`` ends
        it without the stream having to keep the token itself.

        Args:
            token: The token presented by the client.

        Returns:
            A short digest of the stored hash the token matched - not a
            credential, and useless for recovering one - or None when the
            token does not match.
        """
        if not token:
            return None

        stored_hash = self._load_master_token_hash()
        if not stored_hash:
            return None
        if not secrets.compare_digest(self._hash_token(token), stored_hash):
            return None
        return _generation(stored_hash)

    def current_master_generation(self) -> str | None:
        """
        Returns:
            The digest :meth:`master_token_generation` reports for the master
            token in force, or None when none has been issued.
        """
        stored_hash = self._load_master_token_hash()
        return _generation(stored_hash) if stored_hash else None

    def _key_generation(self) -> str:
        """
        Returns:
            A short digest of the signing key in force. API tokens are hashed
            with it, so ``--regenerate`` retires every one of them; a stream an
            API token opened is re-checked against this.
        """
        return _generation(self._signing_key())

    def credential_is_current(self, payload: Mapping[str, Any]) -> bool:
        """
        Re-check a credential that was verified earlier, at a handshake.

        Every stream calls this on its heartbeat, so revoking a token,
        rotating the master token, signing out or letting a session expire
        ends the streams it had already opened, not only the ones it opens
        next.

        Args:
            payload: The payload the credential authenticated, as
                :func:`check_credential` or a WebSocket ticket built it.

        Returns:
            True while the same credential would still be accepted. A
            session is judged by its sign-in (``family``), not its sid, so a
            renewal - which replaces the sid - does not end it. A payload of
            no known type is not current.
        """
        kind = payload.get("type")
        if kind == "master":
            generation = payload.get("generation")
            return generation is not None and generation == self.current_master_generation()

        if kind == "api_token":
            record = self.sessions.get_api_token_by_name(str(payload.get("token_name") or ""))
            if record is None or int(record["id"]) != payload.get("token_id"):
                return False
            expires_at = record["expires_at"]
            if expires_at is not None and float(expires_at) <= time.time():
                return False
            return payload.get("generation") == self._key_generation()

        if kind == "session":
            family = payload.get("family") or payload.get("sid")
            if not family:
                return False
            row = self.sessions.family_alive(str(family))
            return row is not None and not self._past_absolute_deadline(row)

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
        return hashlib.sha256((compact + self._signing_key()).encode()).hexdigest()

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
            if not totp.verify(pending, code, t=_now()):
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
                    # Steps spent under a previous secret mean nothing for this one.
                    "last_steps": {},
                }
            )
            self._write_totp_state(state)
        return codes

    def verify_second_factor(self, code: str, purpose: str = "login") -> bool:
        """
        Check a second factor: a TOTP code, or a single-use backup code.

        A backup code that matches is consumed in the same locked cycle that
        verified it, so it cannot be replayed by a second login racing the
        first. A TOTP code is one-use too (RFC 6238, 5.2): the step it
        matched is remembered per purpose, and that step or any earlier one
        is refused for the same purpose afterwards. Without that, a code
        read over a shoulder stayed good for the rest of its window, up to
        ninety seconds with the drift allowance.

        Args:
            code: What the client typed.
            purpose: What the code is being spent on - ``login``, ``elevate``
                or ``disable``. Remembered separately so that signing in and
                then confirming a destructive action with the same code, each
                once, does not make the operator wait for the next one.

        Returns:
            True when the code is a current, unused TOTP value or an unused
            backup code. False otherwise, including when two-factor is not
            enabled: this method fails closed, and the caller decides whether
            a second factor was required at all.
        """
        if not code or not code.strip():
            return False
        with self._totp_lock:
            state = self._read_totp_state()
            if not state["enabled"]:
                return False
            step = totp.matched_step(state["secret"], code, t=_now())
            if step is not None:
                spent = state.get("last_steps")
                spent = dict(spent) if isinstance(spent, dict) else {}
                last = spent.get(purpose)
                if isinstance(last, int) and step <= last:
                    return False
                spent[purpose] = step
                state["last_steps"] = spent
                self._write_totp_state(state)
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

    def regenerate_backup_codes(self) -> list[str]:
        """
        Replace the backup codes with a fresh set, invalidating the old ones.

        Unlike :meth:`disable_totp`, this does not ask for a current code: the
        caller is a cookie session that has just called ``POST
        /api/auth/elevate`` (D5), the same standing proof of identity that
        guards issuing an API token, and asking for a second, TOTP-specific
        proof on top of it would only make losing the authenticator - the
        situation this command exists for - harder to recover from.

        Returns:
            The new codes, in clear, to be shown exactly once - only their
            salted hashes are stored, so they cannot be shown again.

        Raises:
            SecurityError: When two-factor authentication is not enabled.
                There is nothing to regenerate for a login that needs no
                second factor.
        """
        with self._totp_lock:
            state = self._read_totp_state()
            if not state["enabled"]:
                raise SecurityError(
                    "Two-factor authentication is not enabled",
                    details="There is nothing to regenerate. Enrol first from Settings.",
                )
            codes = [
                f"{secrets.token_hex(2)}-{secrets.token_hex(2)}" for _ in range(BACKUP_CODE_COUNT)
            ]
            state["backup_codes"] = [self._hash_backup_code(c) for c in codes]
            self._write_totp_state(state)
        return codes

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
        if not self.verify_second_factor(code, purpose="disable"):
            return False
        with self._totp_lock:
            self._write_totp_state(
                {"enabled": False, "secret": "", "pending_secret": "", "backup_codes": []}
            )
        return True

    # ------------------------------------------------------------- API tokens

    def create_api_token(
        self, name: str, scope: str, expires_hours: float | None = None
    ) -> dict[str, Any]:
        """
        Issue a named, scoped API token.

        The token is returned in clear exactly once; only its salted hash is
        stored, the same way the master token is stored.

        Args:
            name: Human-chosen name, unique across all tokens ever issued.
            scope: One of :data:`API_TOKEN_SCOPES`.
            expires_hours: Lifetime in hours, or None for a token that only
                dies by revocation.

        Returns:
            The record, including the one and only clear copy of the token
            under ``"token"``.

        Raises:
            SecurityError: When the name is empty or taken, the scope is not
                a scope, or the expiry is not positive.
        """
        cleaned = name.strip()
        if not cleaned or len(cleaned) > 64:
            raise SecurityError(
                "API token names are 1 to 64 characters",
                details="Name the token after what will hold it, such as 'ci-deploy'.",
            )
        if scope not in API_TOKEN_SCOPES:
            raise SecurityError(
                f"Unknown API token scope: {scope!r}",
                details=f"Use one of: {', '.join(API_TOKEN_SCOPES)}.",
            )
        if expires_hours is not None and expires_hours <= 0:
            raise SecurityError(
                "The token expiry must be a positive number of hours",
                details="Omit it entirely for a token that only dies by revocation.",
            )

        token = f"{API_TOKEN_PREFIX}{secrets.token_urlsafe(TOKEN_LENGTH)}"
        now = time.time()
        expires_at = now + float(expires_hours) * 3600 if expires_hours is not None else None
        try:
            token_id = self.sessions.create_api_token(
                cleaned, self._hash_token(token), scope, now, expires_at
            )
        except sqlite3.IntegrityError as exc:
            raise SecurityError(
                f"An API token named {cleaned!r} already exists",
                details=(
                    "Names are unique, including revoked tokens, so an audit line "
                    "naming one always names one thing. Pick a new name."
                ),
            ) from exc

        return {
            "id": token_id,
            "name": cleaned,
            "scope": scope,
            "token": token,
            "created_at": now,
            "expires_at": expires_at,
        }

    def list_api_tokens(self) -> list[dict[str, Any]]:
        """
        List every API token record. No hash and no token is in the result.

        Returns:
            The records, newest first.
        """
        return self.sessions.list_api_tokens()

    def revoke_api_token(self, token_id: int) -> str | None:
        """
        Revoke one API token by id.

        Args:
            token_id: The record's id, as listed.

        Returns:
            The token's name, or None when no such record exists.
        """
        return self.sessions.revoke_api_token(token_id)

    def verify_api_token(self, token: str, client_ip: str | None = None) -> dict[str, Any] | None:
        """
        Verify an API token and return the payload it authenticates.

        The lookup is by the salted hash of the presented value, so timing
        reveals nothing about any stored token, and the fetched row's hash is
        still compared in constant time. Success records ``last_used_at``, at
        most once per :data:`API_TOKEN_LAST_USED_THROTTLE` seconds.

        Args:
            token: The presented credential.
            client_ip: Address presenting it, recorded in the payload.

        Returns:
            The payload, carrying the token's scope, or None when the token is
            unknown, revoked or expired.
        """
        if not token.startswith(API_TOKEN_PREFIX):
            return None

        presented = self._hash_token(token)
        record = self.sessions.get_api_token(presented)
        if record is None:
            return None
        if not hmac.compare_digest(str(record["token_hash"]), presented):
            return None

        return self._api_token_payload(record, client_ip)

    def _api_token_payload(
        self, record: Mapping[str, Any], client_ip: str | None
    ) -> dict[str, Any] | None:
        """
        Turn an unrevoked API token row into the payload it authenticates.

        Shared by a Bearer token and a WebSocket ticket issued to one, so the
        two cannot disagree about expiry or about the scope a token carries.

        Args:
            record: The token row, already known to be unrevoked.
            client_ip: Address presenting the credential.

        Returns:
            The payload, or None when the token has expired.
        """
        now = time.time()
        expires_at = record["expires_at"]
        if expires_at is not None and float(expires_at) <= now:
            return None

        last_used = record["last_used_at"]
        if last_used is None or now - float(last_used) >= API_TOKEN_LAST_USED_THROTTLE:
            self.sessions.touch_api_token(int(record["id"]), now)

        return {
            "type": "api_token",
            "sid": f"{API_TOKEN_SID_PREFIX}{record['name']}",
            "scope": str(record["scope"]),
            "ip": client_ip,
            "token_id": int(record["id"]),
            "token_name": str(record["name"]),
            "generation": self._key_generation(),
        }

    # ---------------------------------------------------------------- sessions

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
            self._signing_key().encode(), session_id.encode(), hashlib.sha256
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
            self._signing_key().encode(), session_id.encode(), hashlib.sha256
        ).hexdigest()
        # Constant time: a timing oracle here would let an attacker forge a
        # signature one byte at a time.
        if not hmac.compare_digest(expected, signature):
            return None
        return session_id

    def signed_session_token(self, token: str) -> bool:
        """
        Report whether a value is a session token this server signed.

        Says nothing about whether the session is still alive; see
        :meth:`verify_session_token` for that.

        Args:
            token: The presented value.

        Returns:
            True when its signature verifies under the signing key in force.
        """
        return bool(token) and self._decode(token) is not None

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
        payload["family"] = record.get("family") or session_id
        payload["type"] = "session"
        # A session is an operator in a browser; scopes exist to narrow
        # automation, not to narrow the person holding the panel.
        payload["scope"] = "admin"
        payload["elevated_until"] = record.get("elevated_until")
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
        longer life. The old identifier keeps working for
        :data:`SESSION_ROTATION_GRACE` seconds, for the requests already in
        flight - and each of those is answered with the *same* successor,
        re-issued, rather than a new one: a session renews once.

        Args:
            payload: A verified session payload.

        Returns:
            The refreshed session, or None when renewal is not due yet or the
            login has reached its absolute deadline.
        """
        session_id = str(payload.get("sid") or "")
        record = self.sessions.get(session_id) if session_id else None
        if record is None:
            return None

        if self._past_absolute_deadline(record):
            self.sessions.revoke(session_id)
            return None

        if record.get("rotated_to"):
            return self._reissue(str(record["rotated_to"]))

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
            # Another request carrying the same cookie won the race to rotate
            # it; hand this one the successor that request created.
            raced = self.sessions.get(session_id)
            if raced is not None and raced.get("rotated_to"):
                return self._reissue(str(raced["rotated_to"]))
            return None

        token = self._encode(new_sid, record["client_ip"], now, expires)
        return IssuedSession(
            token=token,
            session_id=new_sid,
            csrf_token=new_csrf,
            expires_at=expires.timestamp(),
            max_age=int(expires.timestamp() - now_ts),
        )

    def _reissue(self, successor_sid: str) -> IssuedSession | None:
        """
        Hand out a successor a renewal already minted, again.

        The token is an HMAC of the sid and the CSRF token is stored, so the
        same cookie pair can be re-derived exactly; nothing new is created.

        Args:
            successor_sid: The ``rotated_to`` of a retired session.

        Returns:
            The successor, or None when it is no longer live.
        """
        successor = self.sessions.get(successor_sid)
        if successor is None:
            return None
        now = utcnow()
        expires_at = float(successor["expires_at"])
        return IssuedSession(
            token=self._encode(
                successor_sid,
                str(successor["client_ip"]),
                now,
                datetime.fromtimestamp(expires_at, tz=timezone.utc),
            ),
            session_id=successor_sid,
            csrf_token=str(successor["csrf_token"]),
            expires_at=expires_at,
            max_age=max(0, int(expires_at - time.time())),
        )

    def elevate(self, sid: str, seconds: int = ELEVATION_SECONDS) -> float:
        """
        Confirm a session for sudo mode: the destructive actions D5 names.

        Args:
            sid: Session identifier being elevated.
            seconds: How long the confirmation lasts. Defaults to the ten
                minutes the design calls for.

        Returns:
            The elevation deadline, as a UNIX timestamp.
        """
        elevated_until = _now() + seconds
        self.sessions.set_elevated(sid, elevated_until)
        return elevated_until

    def issue_ws_ticket(self, session_id: str, client_ip: str) -> tuple[str, int]:
        """
        Issue a single-use, short-lived WebSocket ticket.

        Query strings end up in access logs and proxy logs, so the value that
        travels there must be worthless seconds later and unusable twice.

        Args:
            session_id: The ``sid`` of the payload asking for the ticket: a
                cookie or Bearer session's id, :data:`MASTER_SID`, or
                ``token:<name>`` for an API token. The ticket redeems as that
                credential and nothing more; see :meth:`consume_ws_ticket`.
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
        Redeem a WebSocket ticket as the credential it was issued to.

        The credential is checked again here, not only when the ticket was
        issued: a session revoked, an API token revoked or expired, or a master
        token rotated inside the ticket's lifetime leaves nothing to redeem.
        An API token's ticket carries that token's scope, so a ticket never
        opens more than the token itself would.

        Args:
            ticket: The ticket value presented by the client.
            client_ip: Address presenting the ticket.

        Returns:
            The payload of the credential the ticket was issued to when the
            ticket and that credential are both still valid, None otherwise.
        """
        if not ticket:
            return None

        record = self.sessions.consume_ticket(hashlib.sha256(ticket.encode()).hexdigest())
        if record is None:
            return None

        if self.config.bind_session_to_ip and client_ip and record["client_ip"] != client_ip:
            return None

        sid = str(record["sid"])
        if sid == MASTER_SID:
            # generate_master_token spends these tickets, so one that is still
            # here was issued to the token in force.
            return master_payload(record["client_ip"], self.current_master_generation())
        if sid.startswith(API_TOKEN_SID_PREFIX):
            token = self.sessions.get_api_token_by_name(sid[len(API_TOKEN_SID_PREFIX) :])
            return None if token is None else self._api_token_payload(token, record["client_ip"])

        session = self.sessions.get(sid)
        if session is None or self._past_absolute_deadline(session):
            return None

        return {
            "type": "session",
            "sid": record["sid"],
            "ip": record["client_ip"],
            "csrf": session["csrf_token"],
            "family": session.get("family") or record["sid"],
            "scope": "admin",
        }

    def revoke_session(self, session_id: str) -> None:
        """
        Revoke one session.

        Args:
            session_id: The session identifier.
        """
        self.sessions.revoke(session_id)

    def list_sessions(self, current_sid: str | None = None) -> list[dict[str, Any]]:
        """
        Describe every live session without exposing a usable identifier.

        Only a truncated prefix of each session id leaves the server: enough
        to name a row for revocation, useless for forging the cookie it
        belongs to.

        Args:
            current_sid: The caller's own session id, so its row is marked.

        Returns:
            One dict per live session, newest activity first.
        """
        return [
            {
                "sid_prefix": str(row["sid"])[:8],
                "client_ip": str(row["client_ip"]),
                "created_at": float(row["created_at"] or row["issued_at"]),
                "last_seen": float(row["issued_at"]),
                "expires_at": float(row["expires_at"]),
                "is_current": bool(current_sid and row["sid"] == current_sid),
            }
            for row in self.sessions.list_active()
        ]

    def revoke_session_by_prefix(self, prefix: str, protect_sid: str | None = None) -> str | None:
        """
        Revoke exactly one session, named by a unique prefix of its id.

        Args:
            prefix: Leading hexadecimal characters of the session id, as
                listed by :meth:`list_sessions`. At least six of them.
            protect_sid: The caller's own session id. Revoking it here is
                refused: ending the session you are inside is sign-out, and it
                has its own button that also clears the browser's cookies.

        Returns:
            The revoked session's prefix, or None when nothing matches.

        Raises:
            SecurityError: When the prefix is not hexadecimal, matches more
                than one session, or names the protected session.
        """
        candidate = prefix.strip().lower()
        if not re.fullmatch(r"[0-9a-f]{6,32}", candidate):
            raise SecurityError(
                f"Not a session id prefix: {prefix!r}",
                details=(
                    "Prefixes are the leading characters of a session id as listed by "
                    "GET /api/auth/sessions: hexadecimal, at least six characters."
                ),
            )

        matches = self.sessions.sids_with_prefix(candidate)
        if not matches:
            return None
        if len(matches) > 1:
            raise SecurityError(
                f"The prefix {candidate!r} matches {len(matches)} sessions",
                details="Send more characters of the session id so exactly one matches.",
            )
        if protect_sid and matches[0] == protect_sid:
            raise SecurityError(
                "That is the session you are using",
                details=(
                    "Revoking the current session from here would leave the browser "
                    "holding dead cookies. Use Sign out, or POST /api/auth/logout."
                ),
            )

        self.sessions.revoke(matches[0])
        return matches[0][:8]

    def revoke_all_sessions(self) -> None:
        """Revoke every session."""
        self.sessions.revoke_all()

    def revoke_other_sessions(self, keep_sid: str) -> int:
        """
        Revoke every session except the one named, leaving it signed in.

        The "sign out everywhere else" button: an operator who spots a
        session they do not recognise in the list wants every *other*
        session gone without also being signed out of the tab that told
        them, which :meth:`revoke_all_sessions` cannot do.

        Args:
            keep_sid: The session id to leave untouched.

        Returns:
            Number of sessions revoked.
        """
        return self.sessions.revoke_all_except(keep_sid)

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
        self._secret_stamp = _file_stamp(self.config.secret_file)
        self.revoke_all_sessions()
        self.purge_expired_sessions()
        return self.generate_master_token()


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


def is_elevated(payload: dict[str, Any]) -> bool:
    """
    Report whether a session payload is currently inside its sudo-mode window.

    Args:
        payload: A verified session payload, as :func:`require_auth` builds
            it. Only a cookie session ever carries a meaningful
            ``elevated_until``; see :func:`wasm.web.api.deps.ensure_elevated`
            for who is asked at all.

    Returns:
        True while ``POST /api/auth/elevate`` was called within the last
        :data:`ELEVATION_SECONDS`.
    """
    elevated_until = payload.get("elevated_until")
    if elevated_until is None:
        return False
    return float(elevated_until) > _now()


def scope_satisfies(granted: str, required: str) -> bool:
    """
    Report whether a granted scope covers a required one.

    Args:
        granted: Scope carried by the credential.
        required: Scope the operation demands.

    Returns:
        True when the grant is at or above the requirement. An unknown scope
        on either side fails closed.
    """
    wanted = SCOPE_RANK.get(required)
    if wanted is None:
        return False
    return SCOPE_RANK.get(granted, -1) >= wanted


def sees_command_lines(payload: Mapping[str, Any]) -> bool:
    """
    Decide whether a credential may read other processes' command lines.

    A process's argv routinely carries a secret it was started with - a
    database password or an API token passed as a flag - and every process
    listing the panel serves would otherwise hand it to a ``read`` token given
    to a dashboard. The policy, stated once for every listing: ``admin`` sees
    the command line, anything less sees the process name.

    Args:
        payload: The authenticated payload.

    Returns:
        True for a credential of ``admin`` scope.
    """
    return scope_satisfies(str(payload.get("scope") or "read"), "admin")


def actor_label(session: Mapping[str, Any]) -> str:
    """
    A short, non-secret label naming who is behind a session payload.

    The one way an action records who did it: every audit record, every
    audit log line and every job's actor go through here, so the Activity
    page can filter the audit log by the same actor a job shows. Reading the
    payload by hand is how three routers came to log ``session=unknown`` for
    every call - they read a ``session_id`` key that payloads never had.

    An API token's payload already carries a human name (``token:<name>``,
    set by :meth:`TokenManager.verify_api_token`); the master token's is the
    literal ``master``; a cookie session's id is an opaque, non-secret
    identifier - the credential is the signed cookie value, not the id alone -
    shortened here to keep a jobs list readable. Twelve characters still start
    with the prefix ``GET /api/auth/sessions`` lists, so a line can be traced
    to the session that wrote it.

    Args:
        session: The authenticated session payload, as :func:`require_auth`
            builds it.

    Returns:
        ``"master"``, ``"token:<name>"``, or the first 12 characters of a
        cookie session's id.
    """
    sid = str(session.get("sid") or "unknown")
    if sid in (MASTER_SID, "unknown") or sid.startswith(API_TOKEN_SID_PREFIX):
        return sid
    return sid[:12]


def required_scope(method: str, path: str) -> str:
    """
    The scope policy, stated once: what a request needs to be allowed to run.

    Reads need ``read``. The mutations that move an existing application - an
    update, a rollback, activating a release - need ``deploy``. Every other
    mutation, creating an application included, needs ``admin``. Endpoints
    with a stricter need than this table gives them - listing the API tokens
    is a GET that must not be readable by a ``read`` token - declare it with
    :func:`wasm.web.api.deps.require_scope`; nothing may declare a looser one.

    Args:
        method: The HTTP method.
        path: The request path.

    Returns:
        The minimum scope.
    """
    verb = method.upper()
    if verb in SAFE_METHODS:
        return "read"
    if verb == "POST" and (
        path in DEPLOY_SCOPE_PATHS or any(p.match(path) for p in DEPLOY_SCOPE_PATTERNS)
    ):
        return "deploy"
    return "admin"


def ensure_scope(request: Request, payload: dict[str, Any], required: str) -> None:
    """
    Refuse a request whose credential does not carry the scope it needs.

    This runs at the same chokepoint that resolves the credential, so a new
    endpoint is covered the moment it exists rather than when somebody
    remembers to guard it.

    Args:
        request: The incoming request.
        payload: The verified session payload; its ``scope`` was set where the
            credential was resolved. A payload without one fails closed as
            ``read``.
        required: The scope the operation demands.

    Raises:
        HTTPException: 403 when the scope is insufficient. The refusal is
            audited with the token's name, never the token.
    """
    granted = str(payload.get("scope") or "read")
    if scope_satisfies(granted, required):
        return

    audit = get_audit_logger()
    if audit:
        audit.record(
            action="auth.scope",
            result="denied",
            client_ip=get_client_ip(request),
            actor=actor_label(payload),
            resource=request.url.path,
            detail=f"scope '{granted}' below required '{required}'",
        )
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail=(
            f"This credential's scope is '{granted}', and {request.method} "
            f"{request.url.path} requires '{required}'. Issue a token with the "
            "right scope from Settings, or POST /api/auth/tokens."
        ),
    )


def master_payload(client_ip: str | None, generation: str | None = None) -> dict[str, Any]:
    """
    The payload the master token authenticates, wherever it is presented.

    Args:
        client_ip: Address presenting it.
        generation: Which issue of the master token it was, from
            :meth:`TokenManager.master_token_generation`, so a stream can
            tell later whether it has been rotated since.

    Returns:
        An ``admin`` payload named :data:`MASTER_SID`.
    """
    return {
        "type": "master",
        "sid": MASTER_SID,
        "ip": client_ip,
        "scope": "admin",
        "generation": generation,
    }


def credential_is_current(payload: Mapping[str, Any]) -> bool:
    """
    Re-check a credential verified earlier; see :meth:`TokenManager.credential_is_current`.

    Args:
        payload: The payload a stream was opened with.

    Returns:
        True while it would still be accepted. False when the server was
        never initialised: a stream nobody can vouch for is not current.
    """
    manager = get_global_token_manager()
    if manager is None:
        return False
    return manager.credential_is_current(payload)


def credential_key(payload: Mapping[str, Any]) -> str:
    """
    Name the credential behind a payload for per-credential accounting.

    Args:
        payload: A verified payload.

    Returns:
        ``master``, ``token:<name>``, or ``session:<family>`` - the sign-in,
        not the sid, so the connections a session opened before and after a
        renewal count against one budget.
    """
    if payload.get("type") == "session":
        return f"session:{payload.get('family') or payload.get('sid')}"
    return str(payload.get("sid") or "unknown")


#: Paths whose endpoints verify a credential the request carries. Anywhere
#: else - ``/health``, the forge webhooks - nothing ever checks one, so the
#: rate limiter does not either: a credential it looked at there would land a
#: valid guess in the roomy budget, say so in ``X-RateLimit-Limit``, and never
#: count a wrong one.
CREDENTIAL_PATH_PREFIXES = ("/api", "/events", "/ws")


def carries_credentials(path: str) -> bool:
    """
    Report whether a request path is one whose endpoints verify credentials.

    Args:
        path: The request path.

    Returns:
        True for a path equal to or below one of
        :data:`CREDENTIAL_PATH_PREFIXES`, matched by whole segment.
    """
    return any(
        path == prefix or path.startswith(prefix + "/") for prefix in CREDENTIAL_PATH_PREFIXES
    )


@dataclass
class CredentialLedger:
    """
    The wrong credentials one request presented, and whether each was counted.

    The rate limiter checks a credential before any endpoint does, and an
    endpoint may never check it (a public route, a 404). Without a ledger the
    limiter had two bad choices: count the failure itself, so every endpoint
    that also counts it counted it twice, or count nothing, and be an oracle
    wherever no endpoint looked. So the limiter notes each wrong credential
    here, :func:`verify_credential` and :func:`authenticate_connection` mark
    the ones they count, and :func:`settle_credential_failures` counts the
    rest once the request is over.

    Attributes:
        client_ip: The address the request came from.
        resource: The path it reached, for the audit record.
        pending: Fingerprint of each wrong credential not yet counted, mapped
            to the channel it arrived on.
    """

    client_ip: str
    resource: str
    pending: dict[str, str] = field(default_factory=dict)


_credential_ledger: ContextVar[CredentialLedger | None] = ContextVar(
    "wasm_credential_ledger", default=None
)


def _fingerprint(credential: str) -> str:
    """
    Name a credential without keeping it.

    Args:
        credential: The credential.

    Returns:
        Its SHA-256, hexadecimal.
    """
    return hashlib.sha256(credential.encode("utf-8", "surrogateescape")).hexdigest()


def open_credential_ledger(client_ip: str, resource: str) -> Token[CredentialLedger | None]:
    """
    Start accounting for the wrong credentials of one request.

    Args:
        client_ip: The resolved client address.
        resource: The request path.

    Returns:
        The token :func:`settle_credential_failures` closes the ledger with.
    """
    return _credential_ledger.set(CredentialLedger(client_ip, resource))


def settle_credential_failures(token: Token[CredentialLedger | None]) -> None:
    """
    Count every wrong credential the request presented that nothing counted.

    Args:
        token: What :func:`open_credential_ledger` returned for this request.
    """
    ledger = _credential_ledger.get()
    _credential_ledger.reset(token)
    if ledger is None:
        return
    for source in ledger.pending.values():
        record_auth_failure(ledger.client_ip, ledger.resource, source)


def _note_wrong_credential(credential: str, source: str) -> None:
    """
    Note a wrong credential the rate limiter found, for counting later.

    Args:
        credential: The credential.
        source: Channel it arrived on.
    """
    ledger = _credential_ledger.get()
    if ledger is not None:
        ledger.pending.setdefault(_fingerprint(credential), source)


def _mark_counted(*credentials: str | None) -> None:
    """
    Take credentials off the ledger because their failure has been counted.

    Args:
        *credentials: The credentials whose failure was just recorded.
    """
    ledger = _credential_ledger.get()
    if ledger is None:
        return
    for credential in credentials:
        if credential:
            ledger.pending.pop(_fingerprint(credential), None)


def _is_guess(credential: str) -> bool:
    """
    Report whether a credential that did not verify counts as a guess.

    Args:
        credential: A credential :func:`check_credential` refused.

    Returns:
        False for a session token this server signed: it can be expired,
        revoked or presented from the wrong address, but producing one takes
        the signing key, and counting it would lock out the operator whose
        browser simply outlived its session.
    """
    manager = get_global_token_manager()
    return manager is None or not manager.signed_session_token(credential)


def rate_limit_identity(connection: HTTPConnection, client_ip: str) -> str | None:
    """
    Name the valid credential a request carries, for the rate limiter.

    Checks the channels a request can carry a credential on - the session
    cookie, the ``wasm.token.`` subprotocol of a WebSocket handshake, the
    ``Authorization`` header - with :func:`check_credential`. A wrong one is
    noted on the request's :class:`CredentialLedger`, and counted against the
    lockout once: by the endpoint that refuses it, or when the request ends if
    no endpoint looked at it. A single-use WebSocket ticket is not looked at,
    because checking it would spend it.

    Args:
        connection: The incoming request or handshake.
        client_ip: The resolved client address, which a session is bound to.

    Returns:
        :func:`credential_key` of the first valid credential, or None when the
        request carries none, in which case it is counted by address.
    """
    candidates = [("cookie", connection.cookies.get(SESSION_COOKIE_NAME))]
    if connection.scope["type"] == "websocket":
        candidates.append(("websocket", subprotocol_token(connection)))
    candidates.append(("bearer", bearer_token(connection)))
    for source, credential in candidates:
        if not credential:
            continue
        payload = check_credential(credential, client_ip)
        if payload is not None:
            return credential_key(payload)
        if _is_guess(credential):
            _note_wrong_credential(credential, source)
    return None


def check_credential(credential: str, client_ip: str) -> dict[str, Any] | None:
    """
    Verify one credential without recording anything.

    Args:
        credential: A session token, an API token or the master token.
        client_ip: Address presenting it.

    Returns:
        The session payload, or None when the credential is not valid.
    """
    manager = get_global_token_manager()
    if manager is None or not credential:
        return None

    # The prefix routes to the token table and nowhere else, so an expired or
    # revoked API token cannot fall through to a slower master-token check.
    if credential.startswith(API_TOKEN_PREFIX):
        return manager.verify_api_token(credential, client_ip)

    payload = manager.verify_session_token(credential, client_ip)
    if payload is not None:
        return payload

    generation = manager.master_token_generation(credential)
    if generation is not None:
        return master_payload(client_ip, generation)

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
        source: Channel the credential arrived on: ``"cookie"`` or ``"bearer"``.

    Returns:
        The session payload, tagged with the channel it arrived on under
        ``"source"`` for the record, or None when the credential is not valid.
        No policy reads the channel: CSRF and sudo mode follow the
        credential's ``"type"``, so a session cannot shed either by moving
        from the cookie to the header.
    """
    payload = check_credential(credential, client_ip)
    if payload is None:
        if _is_guess(credential):
            record_auth_failure(client_ip, resource, source)
            # The rate limiter may have noted the same guess; it is paid for.
            _mark_counted(credential)
        return None
    payload["source"] = source
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
    # One failure per handshake, whichever channels the rate limiter noted.
    _mark_counted(*candidates)
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
            actor=actor_label(payload),
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

    Accepts, in order, an ``Authorization: Bearer`` session token, API token
    or master token (for the CLI), or the session cookie. A session - in the
    cookie or in the header - additionally requires its CSRF header on every
    unsafe method; the master token and API tokens are not sessions and do
    not have one.

    Whichever channel is used, a credential that does not match is counted by
    :func:`record_auth_failure`, so the lockout applies to master token guessing
    on any endpoint and not only to ``/api/auth/login``. The credential's scope
    is then held against :func:`required_scope` here, at the same chokepoint,
    so a scoped API token is narrowed on every endpoint including the ones
    written after it was issued.

    Args:
        request: The incoming request.

    Returns:
        The session payload, also stored on ``request.state.session``.

    Raises:
        HTTPException: 401 when unauthenticated, 403 on a CSRF failure or an
            insufficient scope, 500 when the server was never initialised.
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
        if payload.get("type") == "session":
            # A session is held to the session's rules whichever header
            # carries it: the CSRF token here, sudo mode in ensure_elevated.
            # Only the master token and API tokens, which are no session, go
            # without. A client that logged in with ``bearer: true`` received
            # the CSRF token in the same response as the session token.
            _check_csrf(request, payload, client_ip)
        request.state.session = payload
        ensure_scope(request, payload, required_scope(request.method, resource))
        return payload

    cookie = request.cookies.get(SESSION_COOKIE_NAME)
    if cookie:
        payload = verify_credential(cookie, client_ip, resource=resource, source="cookie")
        if payload is None:
            raise _unauthorized("Session expired or revoked. Please log in again.")
        _check_csrf(request, payload, client_ip)
        request.state.session = payload
        ensure_scope(request, payload, required_scope(request.method, resource))
        renewed = manager.renew_session(payload)
        if renewed is not None:
            request.state.renewed_session = renewed
        return payload

    raise _unauthorized("Not authenticated")
