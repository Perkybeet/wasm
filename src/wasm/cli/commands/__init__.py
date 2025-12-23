"""CLI commands package for WASM."""

from wasm.cli.commands.webapp import handle_webapp
from wasm.cli.commands.site import handle_site
from wasm.cli.commands.service import handle_service
from wasm.cli.commands.cert import handle_cert
from wasm.cli.commands.setup import handle_setup
from wasm.cli.commands.web import handle_web
from wasm.cli.commands.db import handle_db

__all__ = [
    "handle_webapp",
    "handle_site", 
    "handle_service",
    "handle_cert",
    "handle_setup",
    "handle_web",
    "handle_db",
]
