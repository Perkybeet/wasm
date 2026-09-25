# Copyright (c) 2024-2026 Yago Lopez Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
Which layout a deployment uses: in place, or releases.

This is decided in one place because it is decided from three: ``wasm
create``, ``POST /api/apps`` and every redeploy or update of an application
that already exists. The rules:

- An application that exists keeps the layout it has. An application deployed
  by 1.x is in place, and only an explicit migration may move it onto
  releases; a deploy or an update that did it implicitly would point the unit
  at a ``current`` that does not exist yet.
- A new application gets what it asked for, or what this server is
  configured to give new applications (``deploy.layout``).
- Deployers that cannot build releases yet (monorepo, docker-compose) stay in
  place unless releases were explicitly requested, which is an error rather
  than a silent downgrade.
"""

from __future__ import annotations

from wasm.core.config import Config
from wasm.core.exceptions import DeploymentError
from wasm.core.store import App, AppLayout

#: The 1.x layout: the service runs the tree every update rebuilds.
INPLACE = AppLayout.INPLACE.value

#: Every deploy builds its own directory under ``releases/``.
RELEASES = AppLayout.RELEASES.value

#: The concrete layouts an application can be on.
LAYOUTS: tuple[str, ...] = (INPLACE, RELEASES)

#: Asks for whatever this server gives new applications. What ``wasm create``
#: and the API send when the operator did not choose, so an existing
#: application is never told to change layout by a default.
CONFIGURED = "default"

#: The configuration key that holds the layout for new applications.
CONFIG_KEY = "deploy.layout"


def validate_layout(value: str) -> str:
    """
    Check that a layout name is one WASM knows.

    Args:
        value: Candidate layout.

    Returns:
        The layout.

    Raises:
        DeploymentError: If it is neither ``inplace`` nor ``releases``.
    """
    if value not in LAYOUTS:
        raise DeploymentError(
            f"Unknown layout {value!r}",
            details=f"Use one of: {', '.join(LAYOUTS)}.",
        )
    return value


def configured_layout(config: Config | None = None) -> str:
    """
    Read the layout this server gives new applications.

    Args:
        config: Configuration to read. Defaults to the process-wide one.

    Returns:
        ``inplace`` or ``releases``; ``releases`` when nothing is configured.

    Raises:
        DeploymentError: If ``deploy.layout`` holds something else.
    """
    value = (config or Config()).get(CONFIG_KEY, RELEASES)
    if value not in LAYOUTS:
        raise DeploymentError(
            f"The configured {CONFIG_KEY} is {value!r}",
            details=f"Set {CONFIG_KEY} to one of: {', '.join(LAYOUTS)}.",
        )
    return str(value)


def choose_layout(
    existing: App | None,
    requested: str | None,
    *,
    app_type: str,
    supports_releases: bool,
    config: Config | None = None,
) -> str:
    """
    Decide the layout of one deployment.

    Args:
        existing: The application's row when it is already deployed.
        requested: ``inplace`` or ``releases`` when chosen explicitly,
            :data:`CONFIGURED` for the server's default, or None for callers
            that predate layouts, which get the in-place layout they always had.
        app_type: Type of the deployer, for the error messages.
        supports_releases: Whether that deployer can build releases.
        config: Configuration :data:`CONFIGURED` is read from.

    Returns:
        ``inplace`` or ``releases``.

    Raises:
        DeploymentError: When an existing application is asked to change
            layout, or a deployer that cannot build releases is explicitly
            asked to.
    """
    if requested is not None and requested != CONFIGURED:
        validate_layout(requested)

    if existing is not None:
        stored = existing.layout or INPLACE
        if requested in LAYOUTS and requested != stored:
            raise DeploymentError(
                f"{existing.domain} is on the {stored} layout; a deploy does not change it",
                details="Deploy it with the layout it has. Changing layout is an explicit "
                "migration (wasm app migrate), never a side effect of a deploy.",
            )
        if stored == RELEASES and not supports_releases:
            raise DeploymentError(
                f"{existing.domain} is on the releases layout, which {app_type} "
                "applications do not support",
                details="Deploy it with the type it was created with.",
            )
        return stored

    if requested is None:
        return INPLACE
    layout = configured_layout(config) if requested == CONFIGURED else requested
    if layout == RELEASES and not supports_releases:
        if requested == RELEASES:
            raise DeploymentError(
                f"{app_type} applications cannot use the releases layout yet",
                details="Deploy it with --layout inplace.",
            )
        # The server default is a preference, not a demand: a type that has
        # no release pipeline yet keeps working exactly as it did.
        return INPLACE
    return layout
