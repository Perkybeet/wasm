# Copyright (c) 2024-2026 Yago Lopez Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
The context every page shares.

The shell needs the same four things on every screen: which machine this is,
how many of each resource there are, the CSRF token for mutations, and which
navigation entry is current. Assembling that in one place keeps every route
from repeating it and keeps the shell from silently losing a piece of itself.
"""

from __future__ import annotations

from typing import Any

from fastapi import Request

from wasm.web.views.machine import MachineState, read_machine


def resource_counts() -> dict[str, int | None]:
    """
    Count the resources the navigation shows beside each entry.

    A count that cannot be read is reported as None rather than zero, because
    "none deployed" and "could not tell" are different facts and showing the
    second as the first is how an operator stops trusting the panel.

    Returns:
        Counts keyed by navigation entry.
    """
    counts: dict[str, int | None] = {
        "apps": None,
        "services": None,
        "sites": None,
        "certificates": None,
        "databases": None,
        "backups": None,
        "running_jobs": None,
    }

    try:
        from wasm.core.store import get_store
    except ImportError:
        return counts

    store = get_store()
    try:
        counts["apps"] = len(store.list_apps())
        counts["sites"] = len(store.list_sites())
        counts["services"] = len(store.list_services())
        counts["databases"] = len(store.list_databases())
    except Exception as exc:  # noqa: BLE001 - navigation must render regardless
        # The store being unreadable is worth knowing about, but it must not
        # stop the panel from rendering: the operator may be here precisely
        # because something is broken.
        import logging

        logging.getLogger(__name__).warning("Could not read resource counts: %s", exc)

    return counts


def shared_context(request: Request) -> dict[str, Any]:
    """
    Build the context the shell needs.

    Args:
        request: The incoming request.

    Returns:
        Values every template can rely on being present.
    """
    session = getattr(request.state, "session", None)
    return {
        "machine": machine_state(),
        "counts": resource_counts(),
        "csrf_token": getattr(session, "csrf_token", "") if session else "",
        "page": request.url.path.strip("/").split("/")[0] or "dashboard",
        "theme": None,
    }


def machine_state() -> MachineState:
    """
    Read the host snapshot for the header.

    Returns:
        The current machine state.
    """
    from wasm.core.config import Config

    try:
        apps_root = str(Config().get("apps.directory", "/var/www/apps"))
    except Exception:  # noqa: BLE001 - the header must render without config
        apps_root = "/var/www/apps"
    return read_machine(apps_root)
