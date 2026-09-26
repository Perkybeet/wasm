# Copyright (c) 2024-2026 Yago Lopez Prado
# SPDX-License-Identifier: AGPL-3.0-or-later

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
from wasm.validators.environment import EnvironmentValidationError, validate_environment

#: Variables the unit sets inline with ``Environment=`` (see
#: :meth:`~wasm.deployers.base.BaseDeployer._unit_environment`). systemd lets
#: ``EnvironmentFile=`` override ``Environment=``, so an edit here would
#: silently take over from what the unit - and nginx, which proxies to the
#: port WASM recorded - expect. A fresh deploy already keeps them out of the
#: file it generates; this is the guard for every edit made after that, where
#: silently dropping the value would look like it was accepted.
_MANAGED_ENV_VAR_HINTS: dict[str, str] = {
    "PORT": (
        "PORT is managed by WASM: the unit loads it with Environment=, which "
        "EnvironmentFile= would override if the .env file set it too, silently "
        "moving the application off the port nginx and systemd expect. Change "
        "the port by redeploying, for example 'wasm create -d <domain> -s <source> "
        "--port <port>' (or POST /api/apps with the new port)."
    ),
    "NODE_ENV": (
        "NODE_ENV is managed by WASM and fixed to 'production' in the unit; "
        "EnvironmentFile= would override that Environment= the same way, so it "
        "cannot be set from the environment file either."
    ),
}


def _reject_managed_vars(values: Mapping[str, str], current: Mapping[str, str]) -> None:
    """
    Refuse a write that would introduce or change a variable the unit already owns.

    An application deployed before this guard existed, or one whose
    ``.env.example`` declares PORT, can already have PORT or NODE_ENV sitting
    in its ``.env``. Refusing every future edit of that file over a setting
    nobody is trying to change would make the environment of every such
    application permanently uneditable - the operator adding an unrelated
    ``API_KEY`` would be blocked by a PORT they never touched. So only a save
    that adds the key or gives it a value different from what is already on
    disk is refused; the value already there, carried through unchanged,
    passes.

    Args:
        values: The complete set of variables about to be written.
        current: What the file holds right now.

    Raises:
        EnvironmentValidationError: If values adds PORT or NODE_ENV, or gives
            either one a value that differs from what is already stored.
    """
    offenders = [
        name
        for name in ("PORT", "NODE_ENV")
        if name in values and values[name] != current.get(name)
    ]
    if not offenders:
        return
    raise EnvironmentValidationError(
        f"{' and '.join(offenders)} cannot be set from the environment file",
        details=" ".join(_MANAGED_ENV_VAR_HINTS[name] for name in offenders),
    )


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

    Every name and value is validated against what can safely reach a
    systemd unit and the ``EnvironmentFile=`` it is loaded from - a POSIX
    identifier with no control character in its value, a newline included -
    before anything is written, so a caller that skips its own check (an
    older CLI path did) cannot smuggle a second directive into the file this
    unit and every other reader of it trusts.

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
        EnvironmentValidationError: If a name or value is not safe to write
            (see :func:`~wasm.validators.environment.validate_environment`),
            or if values adds PORT or NODE_ENV, or changes one already on
            disk; both are loaded into the unit inline and
            ``EnvironmentFile=`` would let this file silently override them.
        SecurityError: If the destination is a symlink.
        OSError: If the file cannot be written.
    """
    log = logger or Logger()
    env_file = env_file_for(app)
    writer = manager or EnvManager(fs=fs)
    current = writer.read_env_file(env_file)
    clean = validate_environment(values)
    _reject_managed_vars(clean, current)
    writer.write_env_file(env_file, clean)

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
