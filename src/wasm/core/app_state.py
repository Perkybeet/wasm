# Copyright (c) 2024-2026 Yago Lopez Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
One answer to the question "is this application actually working".

``wasm list`` printed the status column from the database and ``wasm health``
asked systemd, so on the same machine at the same moment list called fifteen
applications Running while health reported seven of them stopped. Nothing ever
wrote to that column after a deploy, so list was reading a value that had been
true once.

Asking systemd instead is necessary but not sufficient. A unit is active while
the process exists, which includes a service that accepts no connections and
one systemd is restarting every few seconds because it crashes on startup. Both
report ``active`` if you catch them between restarts. So the state here is
decided from four signals:

- what systemd says the unit is doing (ActiveState and SubState),
- how many times it has been restarted (NRestarts),
- whether anything answers on the port the application is supposed to serve,
- and whether the application is static, in which case there is no unit at all
  and the web server serves the files directly.

The last one is why health warned about five sites that were fine: a static
site has no systemd unit, so querying one always says "not running".
"""

from __future__ import annotations

import socket
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from wasm.core.exceptions import ValidationError, WASMError
from wasm.core.utils import domain_to_app_name

if TYPE_CHECKING:  # pragma: no cover - imported for types only
    from wasm.core.store import App
    from wasm.managers.service_manager import ServiceManager

#: How long to wait for a port to accept a connection. The probe runs against
#: loopback, where anything listening answers immediately; a longer wait would
#: only make `wasm list` slower on the applications that are already broken.
PROBE_TIMEOUT = 0.4

#: Labels. These are what the Status column shows, and core.logger.STATE_STYLES
#: gives each of them its colour.
RUNNING = "Running"
RESTARTING = "Restarting"
NOT_RESPONDING = "No answer"
STOPPED = "Stopped"
FAILED = "Failed"
STATIC = "Static"
UNKNOWN = "Unknown"


@dataclass(frozen=True)
class AppState:
    """
    What is true about one application right now.

    Attributes:
        label: One word for the Status column.
        healthy: Whether this needs an operator's attention. Static sites are
            healthy: there is nothing to run.
        detail: Why, in a sentence, when the answer is not simply "it works".
            Empty when there is nothing to explain.
    """

    label: str
    healthy: bool
    detail: str = ""


def port_answers(port: int, host: str = "127.0.0.1", timeout: float = PROBE_TIMEOUT) -> bool:
    """
    Ask whether anything accepts a connection on a port.

    Args:
        port: The port the application should be serving.
        host: Where to look, loopback by default.
        timeout: Seconds to wait.

    Returns:
        True when the connection is accepted.
    """
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except (OSError, ValueError):
        return False


def _restart_count(status: dict[str, Any]) -> int:
    """
    Read NRestarts out of a status dictionary.

    Args:
        status: What ServiceManager.get_status returned.

    Returns:
        The restart count, and 0 when systemd did not report one.
    """
    try:
        return int(str(status.get("restarts", "0")).strip() or 0)
    except ValueError:
        return 0


def resolve_state(
    app: App,
    service_manager: ServiceManager,
    *,
    probe: bool = True,
) -> AppState:
    """
    Work out what is true about one application.

    Args:
        app: The application record.
        service_manager: Used to ask systemd about the unit.
        probe: Whether to check that the port answers. Turn it off where the
            extra connection is not wanted; the systemd signals still apply.

    Returns:
        The state, ready to be shown or counted.
    """
    if app.is_static:
        return AppState(STATIC, healthy=True, detail="served directly by the web server")

    try:
        status = service_manager.get_status(domain_to_app_name(app.domain))
    except (WASMError, ValidationError) as error:
        return AppState(UNKNOWN, healthy=False, detail=str(error))

    active_state = str(status.get("active_state", "")).strip()
    sub_state = str(status.get("sub_state", "")).strip()
    restarts = _restart_count(status)

    if not status.get("exists", True) and not status.get("active"):
        return AppState(STOPPED, healthy=False, detail="there is no systemd unit for it")

    if active_state == "failed" or sub_state == "failed":
        reason = str(status.get("result", "")).strip()
        detail = f"systemd gave up on it ({reason})" if reason else "systemd gave up on it"
        return AppState(FAILED, healthy=False, detail=detail)

    # A crash loop: systemd keeps starting it and it keeps dying. Between two
    # restarts the unit reads as active, which is how this got reported as
    # Running for as long as it did.
    if sub_state == "auto-restart" or active_state == "activating":
        detail = f"restarted {restarts} times; check the logs with wasm service logs {app.domain}"
        return AppState(RESTARTING, healthy=False, detail=detail)

    if not status.get("active"):
        return AppState(STOPPED, healthy=False, detail="the unit is not running")

    # systemd is satisfied. That only means a process exists, so ask the
    # application itself.
    if probe and app.port and not port_answers(app.port):
        return AppState(
            NOT_RESPONDING,
            healthy=False,
            detail=f"the unit is up but nothing accepts connections on port {app.port}",
        )

    if restarts:
        return AppState(RUNNING, healthy=True, detail=f"answering, after {restarts} restarts")

    return AppState(RUNNING, healthy=True)


def resolve_states(
    apps: list[App],
    service_manager: ServiceManager,
    *,
    probe: bool = True,
) -> dict[str, AppState]:
    """
    Resolve several applications at once.

    Each one costs three systemctl calls and possibly a connection attempt, so
    fifteen applications in sequence is a visible pause before `wasm list`
    prints anything. They are independent, so they run together.

    Args:
        apps: The applications to resolve.
        service_manager: Used to ask systemd about the units.
        probe: Whether to check that the ports answer.

    Returns:
        The state of each application, keyed by domain.
    """
    if not apps:
        return {}

    def one(app: App) -> tuple[str, AppState]:
        return app.domain, resolve_state(app, service_manager, probe=probe)

    workers = min(8, len(apps))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return dict(pool.map(one, apps))
