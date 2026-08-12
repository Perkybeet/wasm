# Copyright (c) 2024-2026 Yago Lopez Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
Printing the end-of-deployment report.

Formatting output is not deployment logic, and keeping it in the deployer was
sixty lines of ``logger.info`` in the middle of the orchestration.
"""

from __future__ import annotations

import socket
from pathlib import Path

from wasm.core.logger import Logger


def local_server_ip() -> str | None:
    """
    Best-effort guess at the address other machines reach this host on.

    Returns:
        The local address of the default route, or None when there is no route.
    """
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # No packet is sent; connect() on UDP only selects the outbound route.
        probe.connect(("8.8.8.8", 80))
        address: str = probe.getsockname()[0]
        return address
    except OSError:
        return None
    finally:
        probe.close()


def print_deployment_summary(
    logger: Logger,
    *,
    domain: str,
    app_name: str,
    port: int,
    app_path: Path | None,
    ssl_requested: bool,
    ssl_obtained: bool,
) -> None:
    """
    Report where the application ended up and what to run next.

    Args:
        logger: Logger to write to.
        domain: Deployed domain.
        app_name: Systemd service and directory name.
        port: Port the application listens on.
        app_path: Directory the application was deployed to.
        ssl_requested: Whether a certificate was asked for.
        ssl_obtained: Whether a certificate was actually issued.
    """
    logger.success("Application deployed successfully!")
    logger.blank()

    protocol = "https" if ssl_obtained else "http"
    logger.key_value("URL", f"{protocol}://{domain}")
    logger.key_value("Service", app_name)
    logger.key_value("Port", str(port))
    logger.key_value("App Path", str(app_path))

    server_ip = local_server_ip()
    if server_ip:
        logger.key_value("Server IP", server_ip)

    ssl_missing = ssl_requested and not ssl_obtained
    if ssl_missing:
        logger.blank()
        logger.warning("SSL was requested but could not be obtained.")
        logger.info(f"To add SSL later, run: wasm cert create -d {domain}")

    logger.blank()
    logger.info("Useful commands:")
    logger.info(f"  wasm status {domain}      # Check application status")
    logger.info(f"  wasm logs {domain}        # View application logs")
    logger.info(f"  wasm restart {domain}     # Restart the application")
    logger.info(f"  wasm update {domain}      # Update from source")

    if ssl_missing:
        logger.blank()
        logger.info("DNS Configuration (for SSL):")
        logger.info(f"  Add an A record pointing {domain} to your server IP")
        logger.info(f"  Then run: wasm cert create -d {domain}")
