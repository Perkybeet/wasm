"""
FastAPI server for the WASM web panel.

Everything security relevant that is not per-endpoint lives here: the request
middleware (client identification, IP whitelist, HTTPS enforcement, rate
limiting, lockout, response hardening headers and the audit trail for
mutations) and the startup checks that refuse to run an unsafe configuration.
"""

from __future__ import annotations

import logging
import socket
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from wasm.core.exceptions import SecurityError
from wasm.web.auth import (
    SAFE_METHODS,
    AuditLogger,
    BruteForceProtection,
    RateLimiter,
    SecurityConfig,
    TokenManager,
    get_client_ip,
    get_security_config,
    ip_matches,
    is_secure_request,
    set_audit_logger,
    set_security_config,
    set_token_manager,
)

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"

#: No inline scripts, no third party origins, no framing. The panel executes
#: systemd as root, so an injected script is a root shell.
CONTENT_SECURITY_POLICY = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self'; "
    "img-src 'self' data:; "
    "font-src 'self'; "
    "connect-src 'self' ws: wss:; "
    "form-action 'self'; "
    "frame-ancestors 'none'; "
    "base-uri 'none'; "
    "object-src 'none'"
)

HSTS_VALUE = "max-age=31536000; includeSubDomains"

_token_manager: TokenManager | None = None
_rate_limiter: RateLimiter | None = None
_brute_force: BruteForceProtection | None = None
_audit_logger: AuditLogger | None = None


def get_token_manager() -> TokenManager:
    """
    Return the token manager, creating it on first use.

    Returns:
        The process-wide token manager.

    Raises:
        SecurityError: When persistent state cannot be initialised.
    """
    global _token_manager
    if _token_manager is None:
        _token_manager = TokenManager(get_security_config())
        set_token_manager(_token_manager)
    return _token_manager


def get_rate_limiter() -> RateLimiter:
    """
    Return the rate limiter, creating it on first use.

    Returns:
        The process-wide rate limiter.
    """
    global _rate_limiter
    if _rate_limiter is None:
        config = get_security_config()
        _rate_limiter = RateLimiter(
            max_requests=config.rate_limit_requests, window=config.rate_limit_window
        )
    return _rate_limiter


def get_brute_force() -> BruteForceProtection:
    """
    Return the lockout tracker, creating it on first use.

    Returns:
        The process-wide brute force protection.
    """
    global _brute_force
    if _brute_force is None:
        config = get_security_config()
        _brute_force = BruteForceProtection(
            max_attempts=config.max_failed_attempts, lockout_duration=config.lockout_duration
        )
    return _brute_force


def get_audit() -> AuditLogger | None:
    """
    Return the audit logger.

    Returns:
        The audit logger, or None when auditing is disabled.
    """
    return _audit_logger


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Purge stale persistent state around the server's lifetime.

    Args:
        app: The application being started.
    """
    manager = get_token_manager()
    manager.purge_expired_sessions()
    yield
    manager.purge_expired_sessions()


def create_app(config: SecurityConfig | None = None) -> FastAPI:
    """
    Create and configure the FastAPI application.

    Args:
        config: Security configuration options.

    Returns:
        The configured application.

    Raises:
        SecurityError: When secrets, sessions or the audit log cannot be
            persisted, or when TLS is required but not configured.
    """
    global _token_manager, _rate_limiter, _brute_force, _audit_logger

    security_config = config or SecurityConfig()
    set_security_config(security_config)

    if security_config.require_https:
        verify_tls_material(security_config)

    _audit_logger = AuditLogger(security_config.audit_log, enabled=security_config.audit_enabled)
    set_audit_logger(_audit_logger)

    _token_manager = TokenManager(security_config)
    set_token_manager(_token_manager)

    _rate_limiter = RateLimiter(
        max_requests=security_config.rate_limit_requests,
        window=security_config.rate_limit_window,
    )
    _brute_force = BruteForceProtection(
        max_attempts=security_config.max_failed_attempts,
        lockout_duration=security_config.lockout_duration,
    )

    app = FastAPI(
        title="WASM Web Interface",
        description="Web-based dashboard for WASM - Web App System Management",
        version="1.0.0",
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )

    app.middleware("http")(_security_middleware)

    if security_config.enable_cors:
        # Credentials plus a wildcard origin would hand any site a root session,
        # so an explicit origin list is mandatory once CORS is on.
        if not security_config.cors_origins:
            raise SecurityError(
                "CORS is enabled but no origins are configured",
                details=(
                    "Set cors_origins to the exact origins allowed to reach the panel, "
                    "for example ['https://panel.example.com'], or disable CORS."
                ),
            )
        app.add_middleware(
            CORSMiddleware,
            allow_origins=security_config.cors_origins,
            allow_credentials=True,
            allow_methods=["GET", "POST", "PUT", "DELETE"],
            allow_headers=["Authorization", "Content-Type", "X-WASM-CSRF"],
        )

    from wasm.web.api import router as api_router

    app.include_router(api_router, prefix="/api")

    from wasm.web.websockets import router as ws_router

    app.include_router(ws_router, prefix="/ws")

    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.get("/", response_class=HTMLResponse)
    async def root() -> Response:
        """
        Serve the dashboard shell.

        Returns:
            The dashboard page.
        """
        index_path = STATIC_DIR / "index.html"
        if index_path.exists():
            return FileResponse(index_path)
        return HTMLResponse(
            content="<h1>WASM Web Interface</h1><p>Static files not found.</p>", status_code=200
        )

    @app.get("/login", response_class=HTMLResponse)
    async def login_page() -> Response:
        """
        Serve the login page.

        Returns:
            The login page. Tokens are never accepted in the URL: query strings
            are recorded by proxies, browsers and access logs.
        """
        login_path = STATIC_DIR / "login.html"
        if login_path.exists():
            return FileResponse(login_path)
        return HTMLResponse(content=_login_fallback_html(), status_code=200)

    @app.get("/health")
    async def health_check() -> dict[str, str]:
        """
        Report that the process is alive.

        Returns:
            A static health payload with no environment details.
        """
        return {"status": "healthy", "service": "wasm-web"}

    return app


def verify_tls_material(config: SecurityConfig) -> tuple[str, str]:
    """
    Check that the configured TLS certificate and key exist.

    Args:
        config: The security configuration.

    Returns:
        The certificate and key paths.

    Raises:
        SecurityError: When TLS is required but the material is missing.
    """
    certfile = config.ssl_certfile
    keyfile = config.ssl_keyfile
    hint = (
        "Point ssl_certfile and ssl_keyfile at a certificate pair. For a public domain: "
        "'certbot certonly --standalone -d panel.example.com' and use "
        "/etc/letsencrypt/live/panel.example.com/{fullchain.pem,privkey.pem}. "
        "For a private network: 'openssl req -x509 -newkey rsa:4096 -days 365 -nodes "
        "-keyout /etc/wasm/web.key -out /etc/wasm/web.crt'. "
        "Set require_https=False only when the panel is bound to 127.0.0.1."
    )

    if not certfile or not keyfile:
        raise SecurityError("HTTPS is required but no TLS certificate is configured", details=hint)

    for label, path in (("certificate", certfile), ("private key", keyfile)):
        if not Path(path).is_file():
            raise SecurityError(
                f"HTTPS is required but the TLS {label} {path} does not exist", details=hint
            )

    return certfile, keyfile


async def _security_middleware(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    """
    Apply connection-level security to every request.

    Args:
        request: The incoming request.
        call_next: The next handler in the chain.

    Returns:
        The response, with hardening headers applied.
    """
    config = get_security_config()
    client_ip = get_client_ip(request, config)
    audit = get_audit()

    if config.ip_whitelist and not ip_matches(client_ip, config.ip_whitelist):
        if audit:
            audit.record(
                action="http.request",
                result="denied",
                client_ip=client_ip,
                resource=request.url.path,
                detail="IP not whitelisted",
            )
        return _harden(
            JSONResponse(status_code=403, content={"detail": "Access denied: IP not whitelisted"}),
            request,
            config,
        )

    if config.require_https and not is_secure_request(request, config):
        return _harden(
            JSONResponse(
                status_code=403,
                content={"detail": "HTTPS is required to reach this panel."},
            ),
            request,
            config,
        )

    if config.rate_limit_enabled and not get_rate_limiter().is_allowed(client_ip):
        return _harden(
            JSONResponse(
                status_code=429,
                content={"detail": "Too many requests. Please try again later."},
                headers={
                    "Retry-After": str(config.rate_limit_window),
                    "X-RateLimit-Remaining": "0",
                },
            ),
            request,
            config,
        )

    if request.url.path in ("/api/auth/login", "/api/auth/token"):
        brute_force = get_brute_force()
        if brute_force.is_locked(client_ip):
            remaining = brute_force.get_lockout_remaining(client_ip)
            if audit:
                audit.record(
                    action="auth.login",
                    result="locked",
                    client_ip=client_ip,
                    resource=request.url.path,
                    detail=f"locked for {remaining} seconds",
                )
            return _harden(
                JSONResponse(
                    status_code=429,
                    content={
                        "detail": f"Too many failed attempts. Locked for {remaining} seconds."
                    },
                    headers={"Retry-After": str(remaining)},
                ),
                request,
                config,
            )

    response = await call_next(request)

    renewed = getattr(request.state, "renewed_session", None)
    if renewed is not None:
        from wasm.web.api.auth import set_session_cookies

        set_session_cookies(response, renewed, secure=is_secure_request(request, config))

    if audit and request.url.path.startswith("/api") and request.method.upper() not in SAFE_METHODS:
        session = getattr(request.state, "session", None)
        audit.record(
            action=f"api.{request.method.lower()}",
            result="ok" if response.status_code < 400 else f"error:{response.status_code}",
            client_ip=client_ip,
            actor=str(session.get("sid")) if session else "anonymous",
            resource=request.url.path,
        )

    if config.rate_limit_enabled:
        remaining = get_rate_limiter().get_remaining(client_ip)
        response.headers["X-RateLimit-Remaining"] = str(remaining)
        response.headers["X-RateLimit-Limit"] = str(config.rate_limit_requests)

    return _harden(response, request, config)


def _harden(response: Response, request: Request, config: SecurityConfig) -> Response:
    """
    Add the response hardening headers.

    Args:
        response: The response to decorate.
        request: The request it answers.
        config: The active configuration.

    Returns:
        The same response, with security headers set.
    """
    response.headers["Content-Security-Policy"] = CONTENT_SECURITY_POLICY
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
    response.headers["Cache-Control"] = "no-store"
    response.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=()"

    if config.require_https or is_secure_request(request, config):
        response.headers["Strict-Transport-Security"] = HSTS_VALUE

    return response


def _login_fallback_html() -> str:
    """
    Render the login page used when the static assets are missing.

    Returns:
        A script-free page, so it works under the panel's CSP.
    """
    return """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>WASM - Login</title>
</head>
<body>
    <h1>WASM Web Interface</h1>
    <p>The dashboard assets are not installed on this server.</p>
    <p>Authenticate by posting your access token to <code>/api/auth/login</code>:</p>
    <pre>curl -X POST http://HOST:PORT/api/auth/login \\
     -H 'Content-Type: application/json' \\
     -d '{"token": "YOUR_TOKEN", "bearer": true}'</pre>
    <p>The token is never accepted in a URL, because query strings are stored in
    browser history, proxy logs and access logs.</p>
</body>
</html>
"""


def get_local_ip() -> str:
    """
    Best-effort local address of this machine, for the startup banner.

    Returns:
        The outbound interface address, or 127.0.0.1 when it cannot be found.
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            return str(sock.getsockname()[0])
    except OSError:
        return "127.0.0.1"


def run_server(
    host: str = "127.0.0.1",
    port: int = 8080,
    config: SecurityConfig | None = None,
    show_token: bool = True,
) -> None:
    """
    Run the WASM web server.

    Args:
        host: Host to bind to.
        port: Port to bind to.
        config: Security configuration.
        show_token: Whether to print a freshly generated access token.

    Raises:
        SecurityError: When HTTPS is required but no usable certificate is
            configured, or when persistent state cannot be written.
    """
    import uvicorn

    if config is None:
        config = SecurityConfig(host=host, port=port)
    else:
        config.host = host
        config.port = port

    ssl_certfile: str | None = None
    ssl_keyfile: str | None = None
    if config.require_https:
        ssl_certfile, ssl_keyfile = verify_tls_material(config)

    app = create_app(config)

    token_manager = get_token_manager()
    master_token = token_manager.generate_master_token()

    if show_token:
        scheme = "https" if ssl_certfile else "http"
        local_ip = get_local_ip() if host == "0.0.0.0" else host
        print()
        print("=" * 60)
        print("WASM Web Interface")
        print("=" * 60)
        print(f"Access Token: {master_token}")
        print(f"Server: {scheme}://{local_ip}:{port}")
        print("Paste the token into the login form. It is never accepted in a URL.")
        print("Keep this token secure! It grants full root access to this machine.")
        print("=" * 60)
        print(flush=True)

    uvicorn.run(
        app,
        host=host,
        port=port,
        log_level="warning",
        # The panel runs privileged operations; requests must leave a trace.
        access_log=True,
        ssl_certfile=ssl_certfile,
        ssl_keyfile=ssl_keyfile,
    )
