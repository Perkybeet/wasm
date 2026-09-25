# Copyright (c) 2024-2026 Yago Lopez Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
Reading and writing the environment of a deployed application.

``wasm env`` and the panel's environment editor are two surfaces over the same
file, so they share this module rather than each joining a directory with
``".env"``. That join is how the release layout broke both: its ``.env`` lives
in ``shared/``, and ``<app>/.env`` read nothing and wrote a stray file the
application never saw. Where the file is comes from
:func:`~wasm.deployers.helpers.layout.env_file_for`; what is done with it is
here.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from wasm.core.config import Config
from wasm.core.exceptions import DeploymentError
from wasm.core.fs import SECRET_MODE, FileSystem, get_fs
from wasm.core.logger import Logger
from wasm.core.runner import CommandRunner, get_runner
from wasm.core.store import App, get_store
from wasm.core.utils import domain_to_app_name
from wasm.deployers.helpers.env_manager import EnvManager
from wasm.deployers.helpers.layout import (
    RELEASES,
    app_root,
    env_file_for,
    layout_on_disk,
)
from wasm.deployers.helpers.permissions import hand_over_file
from wasm.deployers.releases import ReleaseManager


def find_app(domain: str) -> App | None:
    """
    Find a deployed application by domain, with or without a store row.

    Args:
        domain: The application's domain.

    Returns:
        The store row; failing that, a stand-in for a directory deployed under
        the conventional name whose store row is missing (the store moved, or
        it predates the store), on the layout its directory shows. None when
        neither exists.
    """
    app = get_store().get_app(domain)
    if app is not None:
        return app
    root = Config().apps_directory / domain_to_app_name(domain)
    if not root.is_dir():
        return None
    return App(domain=domain, app_path=str(root), layout=layout_on_disk(root))


def read_app_env(app: App, *, manager: EnvManager | None = None) -> dict[str, str]:
    """
    Read the variables in an application's ``.env``.

    Args:
        app: The application.
        manager: Parser to read with. Defaults to a new one.

    Returns:
        Variable name to value; empty when the application has no ``.env``.
    """
    return (manager or EnvManager()).read_env_file(env_file_for(app))


def write_app_env(
    app: App,
    values: Mapping[str, str],
    *,
    manager: EnvManager | None = None,
    fs: FileSystem | None = None,
    runner: CommandRunner | None = None,
    logger: Logger | None = None,
) -> Path:
    """
    Replace an application's ``.env`` with exactly these variables.

    The file is written 0600 and handed to the account the service runs as:
    written by root and left root's, it is a file the application cannot
    read, and the edit it carries never takes effect. On the release layout
    it is written to ``shared/`` and, when the active release has no link to
    it yet (it was deployed before there was a ``.env``), linked in, so a
    restart picks it up without a redeploy.

    Args:
        app: The application.
        values: The complete set of variables.
        manager: Writer to use. Defaults to a new one.
        fs: Filesystem the release link goes through.
        runner: Runner the ownership change goes through.
        logger: Where a failed hand-over or link is reported.

    Returns:
        The file written.

    Raises:
        SecurityError: If the destination is a symlink.
        OSError: If the file cannot be written.
    """
    log = logger or Logger()
    env_file = env_file_for(app)
    (manager or EnvManager(fs=fs)).write_env_file(env_file, dict(values))

    config = Config()
    hand_over_file(
        env_file,
        user=config.service_user,
        group=config.service_group,
        mode=SECRET_MODE,
        runner=runner or get_runner(),
        logger=log,
    )

    if app.layout == RELEASES:
        releases = ReleaseManager(app_root(app), fs=fs or get_fs(), logger=log)
        active = releases.current()
        if active is not None:
            try:
                releases.link_shared(active.path, [])
            except DeploymentError as exc:
                log.warning(f"The new .env was not linked into release {active.id}: {exc}")
    return env_file
