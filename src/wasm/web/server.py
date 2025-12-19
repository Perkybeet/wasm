"""
FastAPI server for WASM Web Interface.

Provides the main application server with security middleware,
API routing, and static file serving.
"""

import asyncio
import os
import signal
import socket
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Request, Response, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles

from wasm.web.auth import (
    TokenManager,
    SecurityConfig,
    RateLimiter,
    BruteForceProtection,
    get_client_ip,
)

# Get the static files directory
STATIC_DIR = Path(__file__).parent / "static"

# Global instances
_token_manager: Optional[TokenManager] = None
_rate_limiter: Optional[RateLimiter] = None
_brute_force: Optional[BruteForceProtection] = None
_security_config: Optional[SecurityConfig] = None


def get_token_manager() -> TokenManager:
    """Get the global token manager instance."""
    global _token_manager
    if _token_manager is None:
        _token_manager = TokenManager()
    return _token_manager


def get_rate_limiter() -> RateLimiter:
    """Get the global rate limiter instance."""
    global _rate_limiter
    if _rate_limiter is None:
        config = _security_config or SecurityConfig()
        _rate_limiter = RateLimiter(
            max_requests=config.rate_limit_requests,
            window=config.rate_limit_window
        )
    return _rate_limiter


def get_brute_force() -> BruteForceProtection:
    """Get the global brute force protection instance."""
    global _brute_force
    if _brute_force is None:
        config = _security_config or SecurityConfig()
        _brute_force = BruteForceProtection(
            max_attempts=config.max_failed_attempts,
            lockout_duration=config.lockout_duration
        )
    return _brute_force


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan handler."""
    # Startup
    yield
    # Shutdown
    pass


def create_app(config: Optional[SecurityConfig] = None) -> FastAPI:
    """
    Create and configure the FastAPI application.
    
    Args:
        config: Security configuration options.
        
    Returns:
        Configured FastAPI application.
    """
    global _security_config, _token_manager, _rate_limiter, _brute_force
    
    _security_config = config or SecurityConfig()
    _token_manager = TokenManager(_security_config)
    _rate_limiter = RateLimiter(
        max_requests=_security_config.rate_limit_requests,
        window=_security_config.rate_limit_window
    )
    _brute_force = BruteForceProtection(
        max_attempts=_security_config.max_failed_attempts,
        lockout_duration=_security_config.lockout_duration
    )
    
    # Set global token manager for auth dependency
    from wasm.web.auth import set_token_manager
    set_token_manager(_token_manager)
    
    app = FastAPI(
        title="WASM Web Interface",
        description="Web-based dashboard for WASM - Web App System Management",
        version="1.0.0",
        docs_url=None,  # Disable Swagger UI in production
        redoc_url=None,  # Disable ReDoc in production
        lifespan=lifespan,
    )
    
    # Add security middleware
    app.middleware("http")(_security_middleware)
    
    # Add CORS if enabled
    if _security_config.enable_cors:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=_security_config.cors_origins or ["*"],
            allow_credentials=True,
            allow_methods=["GET", "POST", "PUT", "DELETE"],
            allow_headers=["*"],
        )
    
    # Register API routes
    from wasm.web.api import router as api_router
    app.include_router(api_router, prefix="/api")
    
    # Register WebSocket routes
    from wasm.web.websockets import router as ws_router
    app.include_router(ws_router, prefix="/ws")
    
    # Serve static files
    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    
    # Root route - serve dashboard
    @app.get("/", response_class=HTMLResponse)
    async def root(request: Request):
        """Serve the main dashboard."""
        index_path = STATIC_DIR / "index.html"
        if index_path.exists():
            return FileResponse(index_path)
        return HTMLResponse(
            content="<h1>WASM Web Interface</h1><p>Static files not found.</p>",
            status_code=200
        )
    
    # Login page
    @app.get("/login", response_class=HTMLResponse)
    async def login_page(request: Request, token: Optional[str] = None):
        """Serve the login page or auto-login with token."""
        login_path = STATIC_DIR / "login.html"
        if login_path.exists():
            return FileResponse(login_path)
        return HTMLResponse(
            content=_generate_login_html(),
            status_code=200
        )
    
    # Health check endpoint (no auth required)
    @app.get("/health")
    async def health_check():
        """Health check endpoint."""
        return {"status": "healthy", "service": "wasm-web"}
    
    return app


async def _security_middleware(request: Request, call_next):
    """
    Security middleware for all requests.
    
    Handles:
    - Rate limiting
    - IP whitelisting
    - HTTPS enforcement
    - Security headers
    """
    client_ip = get_client_ip(request)
    config = _security_config or SecurityConfig()
    
    # Check IP whitelist if configured
    if config.ip_whitelist and client_ip not in config.ip_whitelist:
        # Also check if it's a local request
        if client_ip not in ["127.0.0.1", "::1", "localhost"]:
            return JSONResponse(
                status_code=403,
                content={"detail": "Access denied: IP not whitelisted"}
            )
    
    # Rate limiting
    if config.rate_limit_enabled:
        rate_limiter = get_rate_limiter()
        if not rate_limiter.is_allowed(client_ip):
            return JSONResponse(
                status_code=429,
                content={"detail": "Too many requests. Please try again later."},
                headers={
                    "Retry-After": str(config.rate_limit_window),
                    "X-RateLimit-Remaining": "0",
                }
            )
    
    # Check brute force lockout for auth endpoints
    if request.url.path in ["/api/auth/login", "/api/auth/token"]:
        brute_force = get_brute_force()
        if brute_force.is_locked(client_ip):
            remaining = brute_force.get_lockout_remaining(client_ip)
            return JSONResponse(
                status_code=429,
                content={
                    "detail": f"Too many failed attempts. Locked for {remaining} seconds."
                },
                headers={"Retry-After": str(remaining)}
            )
    
    # Process request
    response = await call_next(request)
    
    # Add security headers
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    
    # Add rate limit headers
    if config.rate_limit_enabled:
        rate_limiter = get_rate_limiter()
        response.headers["X-RateLimit-Remaining"] = str(
            rate_limiter.get_remaining(client_ip)
        )
        response.headers["X-RateLimit-Limit"] = str(config.rate_limit_requests)
    
    return response


def _generate_login_html() -> str:
    """Generate a simple login page HTML."""
    return """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>WASM - Login</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { 
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: linear-gradient(135deg, #1a1a2e 0%, #16213e 100%);
            min-height: 100vh;
            display: flex;
            align-items: center;
            justify-content: center;
        }
        .login-box {
            background: rgba(255, 255, 255, 0.05);
            backdrop-filter: blur(10px);
            border-radius: 16px;
            padding: 40px;
            width: 100%;
            max-width: 400px;
            border: 1px solid rgba(255, 255, 255, 0.1);
        }
        h1 { 
            color: #fff; 
            text-align: center; 
            margin-bottom: 30px;
            font-size: 28px;
        }
        .form-group { margin-bottom: 20px; }
        label { 
            display: block; 
            color: #a0a0a0; 
            margin-bottom: 8px;
            font-size: 14px;
        }
        input {
            width: 100%;
            padding: 14px;
            border: 1px solid rgba(255, 255, 255, 0.1);
            border-radius: 8px;
            background: rgba(0, 0, 0, 0.3);
            color: #fff;
            font-size: 16px;
        }
        input:focus {
            outline: none;
            border-color: #4f46e5;
        }
        button {
            width: 100%;
            padding: 14px;
            background: #4f46e5;
            border: none;
            border-radius: 8px;
            color: #fff;
            font-size: 16px;
            cursor: pointer;
            transition: background 0.2s;
        }
        button:hover { background: #4338ca; }
        .error {
            background: rgba(239, 68, 68, 0.2);
            border: 1px solid rgba(239, 68, 68, 0.5);
            color: #fca5a5;
            padding: 12px;
            border-radius: 8px;
            margin-bottom: 20px;
            display: none;
        }
    </style>
</head>
<body>
    <div class="login-box">
        <h1>🚀 WASM</h1>
        <div id="error" class="error"></div>
        <form id="loginForm">
            <div class="form-group">
                <label for="token">Access Token</label>
                <input type="password" id="token" name="token" 
                       placeholder="Enter your access token" required>
            </div>
            <button type="submit">Login</button>
        </form>
    </div>
    <script>
        // Check for token in URL
        const urlParams = new URLSearchParams(window.location.search);
        const urlToken = urlParams.get('token');
        if (urlToken) {
            document.getElementById('token').value = urlToken;
            document.getElementById('loginForm').dispatchEvent(new Event('submit'));
        }
        
        document.getElementById('loginForm').addEventListener('submit', async (e) => {
            e.preventDefault();
            const token = document.getElementById('token').value;
            const errorDiv = document.getElementById('error');
            
            try {
                const response = await fetch('/api/auth/login', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ token })
                });
                
                const data = await response.json();
                
                if (response.ok) {
                    localStorage.setItem('wasm_session', data.session_token);
                    window.location.href = '/';
                } else {
                    errorDiv.textContent = data.detail || 'Authentication failed';
                    errorDiv.style.display = 'block';
                }
            } catch (err) {
                errorDiv.textContent = 'Connection error. Please try again.';
                errorDiv.style.display = 'block';
            }
        });
    </script>
</body>
</html>
"""


def get_local_ip() -> str:
    """Get the local IP address of the machine."""
    try:
        # Create a socket to determine the local IP
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def run_server(
    host: str = "127.0.0.1",
    port: int = 8080,
    config: Optional[SecurityConfig] = None,
    show_token: bool = True,
) -> None:
    """
    Run the WASM web server.
    
    Args:
        host: Host to bind to.
        port: Port to bind to.
        config: Security configuration.
        show_token: Whether to display the access token.
    """
    import uvicorn
    
    # Create security config with provided host/port
    if config is None:
        config = SecurityConfig(host=host, port=port)
    else:
        config.host = host
        config.port = port
    
    # Create app
    app = create_app(config)
    
    # Generate and display token
    token_manager = get_token_manager()
    master_token = token_manager.generate_master_token()
    
    if show_token:
        local_ip = get_local_ip() if host == "0.0.0.0" else host
        print("\n" + "=" * 60)
        print("🌐 WASM Web Interface")
        print("=" * 60)
        print(f"\n🔐 Access Token: {master_token}")
        print(f"\n📡 Server: http://{local_ip}:{port}")
        print(f"🔗 Quick Login: http://{local_ip}:{port}/login?token={master_token}")
        print("\n⚠️  Keep this token secure! It grants full access to WASM.")
        print("=" * 60 + "\n")
    
    # Flush stdout to ensure token is displayed before uvicorn starts
    import sys
    sys.stdout.flush()
    
    # Run server
    uvicorn.run(
        app,
        host=host,
        port=port,
        log_level="error",
        access_log=False,
    )
