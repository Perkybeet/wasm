# Copyright (c) 2024-2026 Yago Lopez Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
Deep links from the CLI into the panel.

Fly.io's ``fly dashboard`` opens the web page that shows the same thing the
command just printed. ``--open`` is the same idea: a handful of read commands
(``status``, ``list``, ``logs``, ``monitor status``, ``backup list``,
``cert list``, ``db list``) can print the panel URL for what they just showed,
and hand it to a browser when one is plausibly available.

The panel is optional - ``wasm web start`` needs fastapi and uvicorn, and
:mod:`wasm.cli.commands.web` only imports them lazily inside the functions
that actually start it - so this module must not force that dependency onto
every other command. It only reads ``web.*`` off the layered configuration
and, best-effort, the self-signed certificate path that command mints; both
live in modules that import cleanly without the panel's own dependencies
installed.
"""

from __future__ import annotations

import os
import socket

from wasm.core.config import Config
from wasm.core.logger import Logger
from wasm.core.net import ALL_INTERFACES
from wasm.core.runner import get_runner

#: Deadline for ``xdg-open``. It only has to hand the URL to a browser and
#: return; anything slower than this is not going to open a window either.
_OPEN_TIMEOUT = 5


def _panel_serves_tls() -> bool:
    """
    Report whether the panel's self-signed certificate pair is on disk.

    Best-effort, and deliberately narrow: a certificate the operator brought
    with ``--tls-cert``/``--tls-key`` to ``wasm web start`` leaves no trace in
    configuration, so this can only see the pair WASM mints itself. A panel
    started that way is reported as plain HTTP here; the link still opens the
    right host and port, just with the wrong scheme in front of them.

    Returns:
        True when both halves of the self-signed pair exist on disk.
    """
    # Imported lazily, and from a CLI command module rather than duplicated,
    # so this stays the one place that knows where that pair lives. The
    # import is cheap: wasm.cli.commands.web does not touch fastapi or
    # uvicorn until a function that actually starts the server runs.
    from wasm.cli.commands.web import PANEL_TLS_CERT, PANEL_TLS_KEY

    return PANEL_TLS_CERT.exists() and PANEL_TLS_KEY.exists()


def panel_url(path: str) -> str | None:
    """
    Build the panel URL for a path, if the panel is configured.

    The host and port come from the ``web.*`` section of ``config.yaml``,
    which is what the panel's own settings page writes to, not from the flags
    a ``wasm web start`` invocation happened to use: those are not persisted
    anywhere this command could read them back from.

    Args:
        path: Path on the panel, such as ``/apps/example.com``. Must start
            with ``/``.

    Returns:
        The absolute URL, or None when the panel is not configured
        (``web.enabled`` is false, which is also the shipped default).
    """
    config = Config()
    if not config.get("web.enabled", False):
        return None

    host = str(config.get("web.host", "127.0.0.1") or "127.0.0.1")
    if host == ALL_INTERFACES:
        # Every interface is not a place a browser can point at; the
        # operator's own machine name is the closest thing to a real answer.
        host = socket.gethostname() or "localhost"

    port = config.get("web.port", 8080) or 8080
    scheme = "https" if _panel_serves_tls() else "http"

    return f"{scheme}://{host}:{port}{path}"


def open_in_panel(path: str, *, logger: Logger) -> None:
    """
    Print the panel URL for a path, and open it when a display is available.

    The print always happens, whether or not a browser gets launched: the URL
    is the useful part over SSH, where there is nothing to open it with.
    Launching a browser is a bonus for a local session, gated on
    ``DISPLAY``/``WAYLAND_DISPLAY`` and never allowed to fail the command that
    asked for it.

    Args:
        path: Path on the panel, such as ``/apps/example.com``.
        logger: Logger of the current command, so the URL lands next to the
            rest of what the command printed.
    """
    url = panel_url(path)
    if url is None:
        logger.warning("The panel is not configured on this server (web.enabled is off).")
        logger.info("Configure and start it with: wasm web start")
        return

    logger.key_value("Panel", url)

    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        return

    result = get_runner().run(["xdg-open", url], timeout=_OPEN_TIMEOUT)
    if not result.success:
        logger.warning(
            f"Could not open the panel automatically: {result.stderr or result.stdout}".strip()
        )
