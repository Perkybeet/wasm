"""
FastAPI server for the WASM web panel.

Everything security relevant that is not per-endpoint lives here: the
connection middleware (client identification, IP whitelist, HTTPS enforcement,
rate limiting, lockout, WebSocket authentication and Origin checking, response
hardening headers and the audit trail for mutations) and the startup checks
that refuse to run an unsafe configuration.

**Why a raw ASGI middleware and not ``BaseHTTPMiddleware``.** Starlette's
HTTP middleware is only invoked for ``scope["type"] == "http"``. The panel also
serves ``/ws/*``, which streams the root journal and the machine's metrics, so
with an HTTP-only middleware the entire WebSocket surface was outside the IP
whitelist, outside the HTTPS requirement, outside the rate limiter and outside
the lockout. :class:`SecurityMiddleware` speaks ASGI directly, so ``http`` and
``websocket`` connections go through exactly the same checks, and the
WebSocket handshake is authenticated centrally instead of once per handler.
"""

from __future__ import annotations

import logging
import socket
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.datastructures import MutableHeaders
from starlette.requests import HTTPConnection
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from wasm.core.exceptions import SecurityError
from wasm.core.net import host_addresses, local_address, loopback_access_lines
from wasm.web.auth import (
    SAFE_METHODS,
    WS_CLOSE_FORBIDDEN,
    WS_CLOSE_RATE_LIMITED,
    WS_CLOSE_UNAUTHORIZED,
    AuditLogger,
    BruteForceProtection,
    RateLimiter,
    SecurityConfig,
    TokenManager,
    authenticate_connection,
    bearer_token,
    get_audit_logger,
    get_client_ip,
    get_security_config,
    ip_matches,
    is_allowed_origin,
    is_secure_request,
    record_auth_failure,
    set_audit_logger,
    set_brute_force_protection,
    set_security_config,
    set_token_manager,
)

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"

#: No inline scripts, no third party origins, no framing. The panel executes
#: systemd as root, so an injected script is a root shell.
#:
#: ``style-src`` allows inline styles and ``script-src`` does not, and the
#: asymmetry is deliberate. The strict form of both was tried and shipped, and
#: what it actually did was switch features off in silence: xterm builds the
#: whole log terminal out of inline styles and htmx sets them for its request
#: indicators, so the drawer rendered blank and every pending state was
#: invisible. Nothing reported it, because a Content Security Policy is only
#: enforced in a browser and the suite has none.
#:
#: The exposure the two directives control is not comparable. An injected
#: script here runs systemd as root; an injected style can deface the page and,
#: with attribute selectors and a background image, leak what is already on
#: screen to a third party - which ``default-src 'self'`` also has to allow
#: before it works at all. Against that, the templates are autoescaped and
#: tested against injection on every screen.
#:
#: Server-rendered markup still carries no style attributes: that rule is
#: enforced in tests/test_web_style_contract.py and is about the stylesheet
#: being the one place styling lives, not about this header.
CONTENT_SECURITY_POLICY = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; "
    "font-src 'self'; "
    "connect-src 'self' ws: wss:; "
    "form-action 'self'; "
    "frame-ancestors 'none'; "
    "base-uri 'none'; "
    "object-src 'none'"
)

HSTS_VALUE = "max-age=31536000; includeSubDomains"

#: Endpoints that exist to be given a credential by an anonymous client, and
#: are therefore the ones a lockout has to guard even before authentication.
#: ``/login`` is the browser's form; ``/api/auth/login`` is the same exchange
#: for a script. Both count towards the same lockout.
AUTH_PATHS = frozenset({"/login", "/api/auth/login", "/api/auth/token"})

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
        # The counter has to be the same object the credential checks in
        # wasm.web.auth reach for, or failures would be split across channels.
        set_brute_force_protection(_brute_force)
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

    The metrics collector is started and stopped here too: it is a daemon
    thread owned by the web process, so its lifetime is the server's and
    nothing else's. Note that the test suite builds the application without
    running this, which is what keeps the sampling thread out of every test
    that is not about it.

    Args:
        app: The application being started.
    """
    from wasm.web.metrics_collector import start_metrics_collector, stop_metrics_collector

    manager = get_token_manager()
    manager.purge_expired_sessions()
    start_metrics_collector()
    yield
    stop_metrics_collector()
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

    _audit_logger = AuditLogger(
        security_config.audit_log,
        enabled=security_config.audit_enabled,
        max_bytes=security_config.audit_max_bytes,
        backups=security_config.audit_backups,
    )
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
    set_brute_force_protection(_brute_force)

    app = FastAPI(
        title="WASM Web Interface",
        description="Web-based dashboard for WASM - Web App System Management",
        version="1.0.0",
        docs_url=None,
        redoc_url=None,
        # The schema of an API that runs systemd as root is a map for an
        # attacker and is of no use to an anonymous client.
        openapi_url=None,
        lifespan=lifespan,
    )

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

    # Added last, so it is the outermost layer: CORS preflights are answered
    # from inside the whitelist and the rate limiter, not in front of them.
    app.add_middleware(SecurityMiddleware, config=security_config)

    from wasm.web.api import router as api_router

    app.include_router(api_router, prefix="/api")

    # Git forges call the webhook surface server-to-server, so it is mounted
    # at the root rather than under /api: there is no session and no ambient
    # cookie, hence no CSRF, and the per-app HMAC secret inside the router is
    # the authentication. It is mounted here and not in wasm.web.api.router so
    # it cannot inherit the /api prefix. SecurityMiddleware still stands in
    # front of it - added last, outermost - so the IP whitelist, the HTTPS
    # requirement and the rate limiter hold for a forge exactly as they do for
    # a browser. The companion router manages the secrets and is an ordinary
    # authenticated admin surface under /api/apps.
    from wasm.web.api.hooks import admin_router as webhook_admin_router
    from wasm.web.api.hooks import router as hooks_router

    app.include_router(hooks_router, prefix="/hooks", tags=["Webhooks"])
    app.include_router(webhook_admin_router, prefix="/api/apps", tags=["Webhooks"])

    from wasm.web.websockets import router as ws_router

    app.include_router(ws_router, prefix="/ws")

    # The live feed the shell listens to, at the root rather than under /api,
    # because that is the address the client opens and an EventSource is not
    # an API call. Mounted before the pages so the routing table reads in the
    # order a browser meets it: data, then documents.
    from wasm.web.events import router as events_router

    app.include_router(events_router)

    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    # Pages are rendered on the server and updated by htmx. The single-page
    # application this replaces built its markup by interpolating server data
    # into innerHTML in eighty-six places and loaded Tailwind and Font Awesome
    # from public CDNs, so a control panel with root over the machine could not
    # render without internet access.
    from wasm.web.views import router as views_router

    app.include_router(views_router)

    def render_login(request: Request, *, error: str | None = None, status: int = 200) -> Response:
        """
        Render the sign-in page.

        Args:
            request: The incoming request.
            error: Message to show above the form.
            status: HTTP status to answer with.

        Returns:
            The sign-in page. Tokens are never accepted in the URL: query
            strings are recorded by proxies, browsers and access logs.
        """
        from wasm.web.views.rendering import templates

        client_ip = get_client_ip(request)
        locked_for = get_brute_force().get_lockout_remaining(client_ip) or None

        return HTMLResponse(
            templates.get_template("login.html").render(
                hostname=socket.gethostname(),
                csrf_token="",
                error=error,
                locked_for=locked_for,
                theme=None,
                # The server knows whether a second factor is required, so the
                # form only shows the code field when it will actually be read.
                totp_required=get_token_manager().totp_enabled(),
            ),
            status_code=status,
        )

    @app.get("/login", response_class=HTMLResponse)
    def login_page(request: Request) -> Response:
        """
        Serve the sign-in page.

        Args:
            request: The incoming request.

        Returns:
            The sign-in page.
        """
        return render_login(request)

    # The browser posts here. It used to post to a route that did not exist:
    # the page offered a form and the only login endpoint was the JSON one
    # under /api/auth/login, so nobody could sign in with a browser at all.
    # Async, unlike the rest: reading a request body requires awaiting it, and
    # this handler does no blocking work of its own. The body is parsed here
    # rather than with fastapi.Form, which would pull in python-multipart for
    # a plain urlencoded field and add a dependency to package on four
    # distributions for no gain.
    @app.post("/login")
    async def login_submit(request: Request) -> Response:
        """
        Exchange a token typed into the form for a session cookie.

        Args:
            request: The incoming request.

        Returns:
            A redirect to the panel, or the form again with the reason.
        """
        body = await request.body()
        fields = parse_qs(body.decode("utf-8", errors="replace"))
        token = fields.get("token", [""])[0]
        totp_code = fields.get("totp_code", [""])[0].strip()
        manager = get_token_manager()
        brute_force = get_brute_force()
        audit = get_audit()
        client_ip = get_client_ip(request)

        if brute_force.is_locked(client_ip):
            return render_login(
                request,
                error=f"Too many attempts. Try again in "
                f"{brute_force.get_lockout_remaining(client_ip)} seconds.",
                status=429,
            )

        if not manager.verify_master_token(token):
            brute_force.record_failure(client_ip)
            if audit:
                audit.record(
                    action="auth.login",
                    result="failure",
                    client_ip=client_ip,
                    resource="/login",
                    detail=f"{brute_force.get_attempts_remaining(client_ip)} attempts remaining",
                )
            return render_login(
                request,
                error=(
                    "That token was not accepted. "
                    f"{brute_force.get_attempts_remaining(client_ip)} attempts remaining."
                ),
                status=401,
            )

        if manager.totp_enabled():
            if not totp_code:
                # Not counted: an absent code is a form submitted before the
                # operator saw the field, not a guess at the second factor.
                return render_login(
                    request,
                    error="Enter the code from your authenticator app.",
                    status=401,
                )
            if not manager.verify_second_factor(totp_code):
                # The same chokepoint that counts a bad token, so the lockout
                # cannot be escaped by bringing a stolen token to this form
                # and guessing only the second factor.
                record_auth_failure(client_ip, "/login", "totp")
                return render_login(
                    request,
                    error=(
                        "That code was not accepted. "
                        f"{brute_force.get_attempts_remaining(client_ip)} attempts remaining."
                    ),
                    status=401,
                )

        brute_force.record_success(client_ip)
        session = manager.create_session(client_ip)

        # 303 so the browser follows with GET; a 302 after a POST is allowed to
        # repeat the POST, which would replay the credential.
        response = RedirectResponse("/", status_code=303)
        from wasm.web.api.auth import set_session_cookies

        set_session_cookies(response, session, secure=is_secure_request(request))

        if audit:
            audit.record(
                action="auth.login",
                result="success",
                client_ip=client_ip,
                actor=session.session_id,
                resource="/login",
            )
        return response

    # Sign-out lives in wasm.web.views.router, which is included above and
    # therefore answers POST /logout. A second one used to be declared here and
    # was unreachable for that reason alone - which was fortunate, because it
    # read session_id off request.state.session, and that is a dict: the
    # attribute was always None, so it cleared the browser's cookies and left
    # the root session live on the server for the rest of its 24 hours. One
    # implementation of each thing, and this was not it.

    @app.exception_handler(404)
    async def not_found(request: Request, exc: Exception) -> Response:
        """
        Answer a mistyped panel address with the panel's own missing screen.

        The template has existed all along and exactly one route rendered it,
        for a domain that is not deployed. Every other unknown address - a
        stale bookmark, a truncated paste, a link from an older version -
        produced Starlette's plain-text 404, which reads like the server is
        broken rather than like the page is gone.

        Args:
            request: The request that matched no route.
            exc: The 404 Starlette raised. Unused; the signature requires it.

        Returns:
            The missing screen for a browser with a session, and the plain
            answer for everything else. A 404 is not a place to start rendering
            a root panel's navigation to someone who has not signed in.
        """
        path = request.url.path
        machine_paths = ("/api", "/ws", "/static", "/events", "/health", "/hooks")
        wants_html = "text/html" in request.headers.get("accept", "")

        if path.startswith(machine_paths) or not wants_html:
            return JSONResponse({"detail": "Not found"}, status_code=404)

        from wasm.web.auth import require_auth

        try:
            await require_auth(request)
        except HTTPException:
            return RedirectResponse("/login", status_code=303)

        from wasm.web.views.rendering import page as render_page

        return render_page(
            request,
            "pages/missing.html",
            {
                "section": "Not found",
                "title": "No such screen",
                "body": f"The panel has nothing at {path}.",
                "command": "wasm --help",
            },
            status_code=404,
        )

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


class SecurityMiddleware:
    """
    Connection-level security for every scope the panel serves.

    The checks run in the order that keeps the cheapest and most absolute first:
    an address that is not allowed to talk to the panel never reaches the rate
    limiter, and an address that is being rate limited never reaches credential
    verification. The WebSocket handshake is authenticated here rather than in
    each handler, so a new ``@router.websocket`` route cannot forget to do it.
    """

    def __init__(self, app: ASGIApp, config: SecurityConfig) -> None:
        """
        Wrap an ASGI application.

        Args:
            app: The application to protect.
            config: The security configuration of this deployment.
        """
        self.app = app
        self.config = config

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """
        Apply the security policy, then delegate to the wrapped application.

        Args:
            scope: The ASGI connection scope.
            receive: ASGI receive channel.
            send: ASGI send channel.
        """
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        config = self.config
        connection = HTTPConnection(scope)
        client_ip = get_client_ip(connection, config)
        path = str(scope.get("path", ""))
        audit = get_audit_logger()

        if config.ip_whitelist and not ip_matches(client_ip, config.ip_whitelist):
            if audit:
                audit.record(
                    action=f"{scope['type']}.request",
                    result="denied",
                    client_ip=client_ip,
                    resource=path,
                    detail="IP not whitelisted",
                )
            await self._deny(
                scope,
                receive,
                send,
                connection,
                status_code=403,
                ws_code=WS_CLOSE_FORBIDDEN,
                detail="Access denied: IP not whitelisted",
            )
            return

        if config.require_https and not is_secure_request(connection, config):
            await self._deny(
                scope,
                receive,
                send,
                connection,
                status_code=403,
                ws_code=WS_CLOSE_FORBIDDEN,
                detail="HTTPS is required to reach this panel.",
            )
            return

        if config.rate_limit_enabled and not get_rate_limiter().is_allowed(client_ip):
            await self._deny(
                scope,
                receive,
                send,
                connection,
                status_code=429,
                ws_code=WS_CLOSE_RATE_LIMITED,
                detail="Too many requests. Please try again later.",
                headers={
                    "Retry-After": str(config.rate_limit_window),
                    "X-RateLimit-Remaining": "0",
                },
            )
            return

        if scope["type"] == "websocket" and not is_allowed_origin(connection, config):
            if audit:
                audit.record(
                    action="ws.connect",
                    result="denied",
                    client_ip=client_ip,
                    resource=path,
                    detail="origin not allowed",
                )
            await self._deny(
                scope,
                receive,
                send,
                connection,
                status_code=403,
                ws_code=WS_CLOSE_FORBIDDEN,
                detail="Origin not allowed for this panel.",
            )
            return

        if self._guards_credentials(scope, connection, path):
            brute_force = get_brute_force()
            if brute_force.is_locked(client_ip):
                remaining = brute_force.get_lockout_remaining(client_ip)
                if audit:
                    audit.record(
                        action="auth.lockout",
                        result="locked",
                        client_ip=client_ip,
                        resource=path,
                        detail=f"locked for {remaining} seconds",
                    )
                await self._deny(
                    scope,
                    receive,
                    send,
                    connection,
                    status_code=429,
                    ws_code=WS_CLOSE_RATE_LIMITED,
                    detail=f"Too many failed attempts. Locked for {remaining} seconds.",
                    headers={"Retry-After": str(remaining)},
                )
                return

        if scope["type"] == "websocket":
            session = authenticate_connection(connection, _query_ticket(scope))
            if session is None:
                await self._deny(
                    scope,
                    receive,
                    send,
                    connection,
                    status_code=401,
                    ws_code=WS_CLOSE_UNAUTHORIZED,
                    detail="Authentication required",
                )
                return
            scope.setdefault("state", {})["session"] = session
            await self.app(scope, receive, send)
            return

        await self.app(scope, receive, self._wrap_send(scope, connection, client_ip, send))

    @staticmethod
    def _guards_credentials(scope: Scope, connection: HTTPConnection, path: str) -> bool:
        """
        Report whether the lockout applies to this connection.

        The lockout exists to stop credential guessing, so it covers everything
        that can carry a guess: the login endpoints, any request presenting a
        Bearer token, and every WebSocket handshake. A browser that already
        holds a valid session cookie is deliberately not blocked, so one
        attacker cannot lock the operator out of their own panel.

        Args:
            scope: The ASGI connection scope.
            connection: A view over the scope.
            path: The request path.

        Returns:
            True when a locked-out client must be refused.
        """
        if scope["type"] == "websocket":
            return True
        if path in AUTH_PATHS:
            return True
        return bearer_token(connection) is not None

    def _wrap_send(
        self, scope: Scope, connection: HTTPConnection, client_ip: str, send: Send
    ) -> Send:
        """
        Decorate the response as it leaves: cookies, audit and hardening.

        Args:
            scope: The ASGI connection scope.
            connection: A view over the scope.
            client_ip: The resolved client address.
            send: The original ASGI send channel.

        Returns:
            A send channel that rewrites ``http.response.start``.
        """

        async def wrapped(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                self._harden_headers(headers, connection)
                self._apply_renewed_session(scope, headers, connection)
                if self.config.rate_limit_enabled:
                    headers["X-RateLimit-Remaining"] = str(
                        get_rate_limiter().get_remaining(client_ip)
                    )
                    headers["X-RateLimit-Limit"] = str(self.config.rate_limit_requests)
                self._audit_mutation(scope, client_ip, int(message["status"]))
            await send(message)

        return wrapped

    def _apply_renewed_session(
        self, scope: Scope, headers: MutableHeaders, connection: HTTPConnection
    ) -> None:
        """
        Attach the cookies of a session that was re-issued during the request.

        Args:
            scope: The ASGI connection scope, carrying the endpoint's state.
            headers: Headers of the outgoing response.
            connection: A view over the scope.
        """
        renewed = scope.get("state", {}).get("renewed_session")
        if renewed is None:
            return

        from wasm.web.api.auth import set_session_cookies

        carrier = Response()
        set_session_cookies(carrier, renewed, secure=is_secure_request(connection, self.config))
        for key, value in carrier.raw_headers:
            if key.decode("latin-1").lower() == "set-cookie":
                headers.append("set-cookie", value.decode("latin-1"))

    def _audit_mutation(self, scope: Scope, client_ip: str, status_code: int) -> None:
        """
        Record a state-changing API call in the audit log.

        Args:
            scope: The ASGI connection scope.
            client_ip: The resolved client address.
            status_code: Status the endpoint answered with.
        """
        audit = get_audit_logger()
        method = str(scope.get("method", "GET")).upper()
        path = str(scope.get("path", ""))
        if audit is None or not path.startswith("/api") or method in SAFE_METHODS:
            return

        session = scope.get("state", {}).get("session")
        audit.record(
            action=f"api.{method.lower()}",
            result="ok" if status_code < 400 else f"error:{status_code}",
            client_ip=client_ip,
            actor=str(session.get("sid")) if session else "anonymous",
            resource=path,
        )

    def _harden_headers(self, headers: MutableHeaders, connection: HTTPConnection) -> None:
        """
        Set the response hardening headers.

        Args:
            headers: Headers of the outgoing response.
            connection: The connection being answered.
        """
        headers["Content-Security-Policy"] = CONTENT_SECURITY_POLICY
        headers["X-Content-Type-Options"] = "nosniff"
        headers["X-Frame-Options"] = "DENY"
        headers["Referrer-Policy"] = "no-referrer"
        headers["Cross-Origin-Opener-Policy"] = "same-origin"
        headers["Cross-Origin-Resource-Policy"] = "same-origin"
        headers["Cache-Control"] = "no-store"
        headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=()"

        if self.config.require_https or is_secure_request(connection, self.config):
            headers["Strict-Transport-Security"] = HSTS_VALUE

    async def _deny(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
        connection: HTTPConnection,
        *,
        status_code: int,
        ws_code: int,
        detail: str,
        headers: dict[str, str] | None = None,
    ) -> None:
        """
        Refuse a connection in the shape its protocol understands.

        Args:
            scope: The ASGI connection scope.
            receive: ASGI receive channel.
            send: ASGI send channel.
            connection: A view over the scope.
            status_code: HTTP status for an ``http`` scope.
            ws_code: Close code for a ``websocket`` scope.
            detail: Message for the client.
            headers: Extra response headers.
        """
        if scope["type"] == "websocket":
            # Closing before accepting is how ASGI refuses a handshake; the
            # server turns it into an HTTP error for the client. The connect
            # event is consumed first because that is the order the protocol
            # servers expect, and a client that already gave up sends
            # websocket.disconnect instead.
            message = await receive()
            if message["type"] != "websocket.connect":
                return
            await send({"type": "websocket.close", "code": ws_code, "reason": detail[:120]})
            return

        response = JSONResponse(
            status_code=status_code, content={"detail": detail}, headers=headers
        )
        self._harden_headers(MutableHeaders(raw=response.raw_headers), connection)
        await response(scope, receive, send)


def _query_ticket(scope: Scope) -> str | None:
    """
    Read the single-use WebSocket ticket from the query string.

    Args:
        scope: The ASGI connection scope.

    Returns:
        The ticket, or None when the handshake carries none.
    """
    raw = scope.get("query_string", b"")
    if not raw:
        return None
    values = parse_qs(raw.decode("latin-1")).get("ticket")
    return values[0] if values else None


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


def banner_address(host: str) -> str:
    """
    Turn a bound address into one an operator can type.

    Args:
        host: The address the panel was bound to.

    Returns:
        The same address, unless it is "every interface", which no browser can
        open: that becomes an address this machine actually answers on.
    """
    addresses = host_addresses(host)
    if addresses and all(address.is_unspecified for address in addresses):
        return local_address()
    return host


def startup_banner(token: str, host: str, port: int, scheme: str) -> tuple[str, ...]:
    """
    Build what the panel prints when it comes up.

    This is the entire handover between the CLI and the operator: it carries the
    only readable copy of the access token, and the address it names is all
    there is to go on. On a server with no desktop that address is loopback, so
    a banner that prints it and stops hands somebody a root credential for a
    page they have no way to open. See :func:`wasm.core.net.loopback_access_lines`.

    Args:
        token: The freshly issued access token.
        host: Address the panel is bound to.
        port: Port it listens on.
        scheme: ``http`` or ``https``, matching what it serves.

    Returns:
        The lines to print, in order.
    """
    rule = "=" * 60
    return (
        "",
        rule,
        "WASM Web Interface",
        rule,
        f"Access Token: {token}",
        f"Server: {scheme}://{host}:{port}",
        *loopback_access_lines(host, port, scheme=scheme),
        "Paste the token into the login form. It is never accepted in a URL.",
        "Keep this token secure! It grants full root access to this machine.",
        rule,
    )


def _uvicorn_kwargs(
    app: FastAPI,
    host: str,
    port: int,
    ssl_certfile: str | None,
    ssl_keyfile: str | None,
) -> dict[str, Any]:
    """
    Build the keyword arguments ``run_server`` hands to ``uvicorn.run``.

    Pulled out as its own pure function so the composition is testable
    without binding a real socket: ``uvicorn.run`` blocks for the life of the
    process and tests/conftest.py makes real network access fail, so nothing
    that actually starts the server can run in the suite.

    Args:
        app: The application to serve.
        host: Address to bind to.
        port: Port to bind to.
        ssl_certfile: Path to a TLS certificate, or None for plain HTTP.
        ssl_keyfile: Path to the certificate's private key, or None for plain
            HTTP.

    Returns:
        Keyword arguments for ``uvicorn.run``.
    """
    return {
        "app": app,
        "host": host,
        "port": port,
        "log_level": "warning",
        # The panel runs privileged operations; requests must leave a trace.
        "access_log": True,
        "ssl_certfile": ssl_certfile,
        "ssl_keyfile": ssl_keyfile,
        # uvicorn's default "Server: uvicorn" response header announces the
        # exact software running behind the panel to anyone, unauthenticated.
        # That is a fingerprint an attacker probes for before picking an
        # exploit, and the panel's own hardening headers do not touch it: it
        # is uvicorn, not the application, that writes it.
        "server_header": False,
    }


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
        print("\n".join(startup_banner(master_token, banner_address(host), port, scheme)))
        print(flush=True)

    uvicorn.run(**_uvicorn_kwargs(app, host, port, ssl_certfile, ssl_keyfile))
