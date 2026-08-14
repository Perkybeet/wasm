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
        # Certificates and backups stay unknown here on purpose. Counting them
        # means running "certbot certificates" and walking the backup
        # directory, and this function runs on every page and on every refresh
        # of the machine strip, which is every five seconds. A number in the
        # navigation is not worth a privileged subprocess on a timer.
        "certificates": None,
        "databases": None,
        "backups": None,
        "running_jobs": None,
    }

    from wasm.web.views.resources import running_job_count

    counts["running_jobs"] = running_job_count()

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
    except Exception as exc:
        # The store being unreadable is worth knowing about, but it must not
        # stop the panel from rendering: the operator may be here precisely
        # because something is broken.
        import logging

        logging.getLogger(__name__).warning("Could not read resource counts: %s", exc)

    return counts


#: The panel's fixed destinations, in the order the command palette offers
#: them. Every entry resolves to a route declared in the views; a dead entry
#: here is a dead link with a keyboard shortcut on it.
_PALETTE_ROUTES: tuple[tuple[str, str], ...] = (
    ("Overview", "/"),
    ("Applications", "/apps"),
    ("Deploy an application", "/apps/new"),
    ("Services", "/services"),
    ("Sites", "/sites"),
    ("Certificates", "/certificates"),
    ("Databases", "/databases"),
    ("Backups", "/backups"),
    ("Deployments", "/deployments"),
    ("Activity", "/activity"),
    ("Settings", "/settings"),
)


def palette_entries() -> list[dict[str, str]]:
    """
    Build the command palette's catalogue: the fixed screens, then every
    deployed application.

    Server-rendered rather than fetched, so the palette works the instant
    Ctrl+K lands and holds exactly what this response could see; the client
    only filters it.

    Returns:
        ``label``/``href``/``hint`` mappings, fixed routes first.
    """
    entries = [{"label": label, "href": href, "hint": "screen"} for label, href in _PALETTE_ROUTES]

    try:
        from wasm.core.store import get_store
    except ImportError:
        return entries

    import sqlite3

    from wasm.core.exceptions import WASMError

    try:
        domains = sorted(app.domain for app in get_store().list_apps() if app.domain)
    except (WASMError, sqlite3.Error, OSError) as exc:
        # The store being unreadable must not take the shell down with it -
        # the operator may be here precisely because something is broken -
        # but it is worth a line.
        import logging

        logging.getLogger(__name__).warning("Could not read applications for the palette: %s", exc)
        return entries

    entries.extend(
        {"label": domain, "href": f"/apps/{domain}", "hint": "application"} for domain in domains
    )
    return entries


def shared_context(request: Request) -> dict[str, Any]:
    """
    Build the context the shell needs.

    Args:
        request: The incoming request.

    Returns:
        Values every template can rely on being present.
    """
    from wasm.web.auth import CSRF_COOKIE_NAME, CSRF_HEADER_NAME

    session = getattr(request.state, "session", None)
    return {
        "machine": machine_state(),
        "counts": resource_counts(),
        "csrf_token": csrf_token(session),
        # The name travels with the value. The shell used to hard-code
        # "X-CSRF-Token" while the server reads CSRF_HEADER_NAME, so every
        # mutation a browser sent was refused; a constant cannot drift from
        # itself.
        "csrf_header": CSRF_HEADER_NAME,
        # And the cookie the current value can be read back from. The token
        # baked into the markup is only correct until the session renews
        # itself, which it does silently at half its lifetime and which
        # rotates the CSRF token with it: from that moment every button in a
        # tab left open answered 403, with no way back except a reload nobody
        # knew to perform. The client re-reads this cookie per request.
        "csrf_cookie": CSRF_COOKIE_NAME,
        "page": request.url.path.strip("/").split("/")[0] or "dashboard",
        "theme": None,
        "palette": palette_entries(),
    }


def csrf_token(session: Any) -> str:
    """
    Read the CSRF token out of a verified session payload.

    The payload is a mapping, and the shell reads the token out of it with an
    attribute lookup, which always missed and always produced an empty string.
    Every mutation the panel offered was therefore rejected with a 403 the
    moment it left the browser: every restart button, every delete, sign-out
    included. It is read as a key here, once, for the whole shell.

    Args:
        session: The session payload attached to the request, or None.

    Returns:
        The token, or an empty string when there is no session.
    """
    if not isinstance(session, dict):
        return ""
    return str(session.get("csrf", ""))


def machine_state() -> MachineState:
    """
    Read the host snapshot for the header.

    Returns:
        The current machine state.
    """
    from wasm.core.config import Config

    try:
        apps_root = str(Config().get("apps.directory", "/var/www/apps"))
    except Exception:
        apps_root = "/var/www/apps"
    return read_machine(apps_root)
