# Copyright (c) 2024-2026 Yago Lopez Prado
# SPDX-License-Identifier: AGPL-3.0-or-later

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

It is also where the layout turns into paths for everything that is not a
deploy: where an application's ``.env`` lives and where its running code is.
``wasm env``, the panel's environment editor and the backups all used to
assume ``<app>/.env``, which on the release layout reads nothing and writes a
stray file the application never sees; they ask :func:`env_file_for` instead.
"""

from __future__ import annotations

from pathlib import Path

from wasm.core.config import Config
from wasm.core.exceptions import DeploymentError
from wasm.core.store import App, AppLayout
from wasm.core.utils import domain_to_app_name
from wasm.deployers.releases import CURRENT_LINK, ENV_FILE, SHARED_DIR

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


def app_root(app: App) -> Path:
    """
    Return an application's directory.

    Args:
        app: The application's store row.

    Returns:
        The directory the row records, or the conventional one under the apps
        directory for a row that records none.
    """
    if app.app_path:
        return Path(app.app_path)
    return Config().apps_directory / domain_to_app_name(app.domain)


def layout_on_disk(root: Path) -> str:
    """
    Tell which layout a directory is on, from what is in it.

    For the callers that have a directory and no store row: an archive being
    restored, or an application whose store went missing. A ``current`` link
    is only ever created by the release layout; the in-place layout never has
    one.

    Args:
        root: An application directory.

    Returns:
        ``releases`` when ``current`` is a symlink, ``inplace`` otherwise.
    """
    return RELEASES if (root / CURRENT_LINK).is_symlink() else INPLACE


def env_file_in(root: Path, layout: str | None) -> Path:
    """
    Return where the ``.env`` of an application directory lives.

    Args:
        root: The application directory.
        layout: ``inplace`` or ``releases``. None, which is what a row that
            predates layouts says, means in place.

    Returns:
        ``<root>/shared/.env`` on releases, where every release links it
        from; ``<root>/.env`` in place.
    """
    if layout == RELEASES:
        return root / SHARED_DIR / ENV_FILE
    return root / ENV_FILE


def env_file_for(app: App) -> Path:
    """
    Return where an application's ``.env`` lives.

    The one lookup every reader and writer of an application's environment
    goes through: the CLI, the panel's editor, the deployers and the backups.

    Args:
        app: The application's store row.

    Returns:
        The file, which may not exist yet.
    """
    return env_file_in(app_root(app), app.layout)


def code_path_for(app: App) -> Path:
    """
    Return where an application's running code is.

    Args:
        app: The application's store row.

    Returns:
        ``<root>/current`` on releases, the application directory in place.
        This is where ``.env.example`` and the build are, not where anything
        persistent should be written.
    """
    root = app_root(app)
    return root / CURRENT_LINK if app.layout == RELEASES else root
