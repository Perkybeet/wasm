"""
WASM Web Interface Module.

Provides a secure web-based dashboard for managing WASM deployments.
"""

from wasm.web.auth import SecurityConfig, TokenManager
from wasm.web.server import create_app, run_server

__all__ = [
    "SecurityConfig",
    "TokenManager",
    "create_app",
    "run_server",
]
