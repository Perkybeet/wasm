"""CLI commands package for WASM."""

from wasm.cli.commands.cert import handle_cert
from wasm.cli.commands.db import handle_db
from wasm.cli.commands.service import handle_service
from wasm.cli.commands.setup import handle_setup
from wasm.cli.commands.site import handle_site
from wasm.cli.commands.web import handle_web
from wasm.cli.commands.webapp import handle_webapp

__all__ = [
    "handle_cert",
    "handle_db",
    "handle_service",
    "handle_setup",
    "handle_site",
    "handle_web",
    "handle_webapp",
]
