# Copyright (c) 2024-2026 Yago Lopez Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
Repository inspection for the new-app wizard.

D7 of the v2 design ("Asistente de alta") pastes a repository and shows the
operator what WASM would do with it before anything is deployed: which
application type it matches, what commands would install, build and start
it, which environment variables it declares, and which commit it is at. This
module answers all of that from a throwaway checkout, so the wizard has
something real to show instead of a guess, and does it without creating an
application, a domain or a unit.

The checkout lives in a scratch directory this module owns end to end: a
``tempfile.TemporaryDirectory`` that is removed when the inspection ends, so
a clone that half-succeeds, a detector that raises, or a caller that never
goes on to deploy the result never leaves a fetched repository behind.

That directory is the one piece of this package that is not created through
``wasm.core.fs``, on purpose: it is a preview, not a deployment, so there is
nothing for ``--dry-run`` to rehearse, and it must be cleaned up for real even
when the process-wide filesystem is a dry run. ``tests/test_deployers.py``
lists it as a named exemption of its filesystem guard, with this reason.

Only detection runs against the checkout, never a deployer's install/build
hooks: those create virtualenvs, run ``npm install`` and spawn processes,
which is the deployment itself, not a preview of it. The commands reported
here are what a deployer would run, read off its ``get_*_command`` methods
once ``configure()`` and package-manager detection have run; a framework
hook that refines them further (a build actually setting standalone mode, a
venv actually existing) only runs during the real deploy.
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass
from pathlib import Path

from wasm.core.exceptions import DeploymentError
from wasm.deployers.base import BaseDeployer
from wasm.deployers.helpers.env_manager import EnvManager
from wasm.deployers.helpers.package_manager import PackageManagerHelper
from wasm.deployers.interface import AppDeployer
from wasm.deployers.registry import DeployerRegistry, _import_deployers
from wasm.managers.source_manager import SourceManager

#: Domain handed to ``configure()`` so a deployer has something to derive an
#: app name from. Never persisted, never used to build a path outside the
#: throwaway checkout: ``app_path`` is always overridden explicitly.
_PLACEHOLDER_DOMAIN = "wasm-inspect.invalid"

#: Substrings that mark an environment variable's name as a credential.
#: Broader than :data:`EnvManager.SECRET_PATTERNS`, which is tuned to avoid
#: false positives on a deploy where a wrong guess writes a value straight
#: into a systemd unit. Here a false positive only pre-selects a password
#: field in a form the operator can still see and edit, so the wizard leans
#: toward flagging more names rather than fewer: ``KEY``, ``PASS`` and
#: ``PRIVATE`` alone, not just their compounds.
SECRET_NAME_MARKERS: tuple[str, ...] = ("SECRET", "KEY", "TOKEN", "PASSWORD", "PASS", "PRIVATE")


def _looks_secret(name: str) -> bool:
    """
    Decide whether an environment variable's name looks like a credential.

    Args:
        name: The variable's name, as written in ``.env.example``.

    Returns:
        True when the name contains any of :data:`SECRET_NAME_MARKERS`.
    """
    upper = name.upper()
    return any(marker in upper for marker in SECRET_NAME_MARKERS)


@dataclass
class EnvKey:
    """
    One environment variable discovered in a repository.

    Attributes:
        name: Variable name.
        default: Default value from ``.env.example``, or None when it has
            none.
        secret: Whether the name looks like it holds a credential.
        required: Whether the application needs a value because
            ``.env.example`` gave it none.
    """

    name: str
    default: str | None
    secret: bool
    required: bool


@dataclass
class SourceInspection:
    """
    What a repository is, read before anything is deployed from it.

    Attributes:
        app_type: The application type the wizard would deploy as; the first
            entry of ``detected_types``.
        detected_types: Every registered application type that recognised
            the repository, most specific first (registry priority order).
        package_manager: The Node package manager the repository's lock file
            implies, or None when the project has no ``package.json``.
        install_command: Argv the chosen deployer would run to install
            dependencies. Empty when the type has none (a static site, a
            Docker Compose stack).
        build_command: Argv the chosen deployer would run to build the
            project. Empty when there is nothing to build.
        start_command: Shell command the chosen deployer would run as the
            service's ``ExecStart``. Empty for a static site.
        default_port: Port the chosen deployer uses when none is requested.
        env_keys: Environment variables discovered from ``.env.example``.
        branch: The branch this inspection targeted: the one requested, or
            failing that the checkout's current branch, or empty when
            neither applies (an archive, a plain local directory).
        commit: Short commit hash of the checkout, or empty when the source
            is not a Git repository.
    """

    app_type: str
    detected_types: list[str]
    package_manager: str | None
    install_command: list[str]
    build_command: list[str]
    start_command: str
    default_port: int
    env_keys: list[EnvKey]
    branch: str
    commit: str


def _matching_types(path: Path) -> list[type[AppDeployer]]:
    """
    Every registered application type that recognises a directory.

    Mirrors :meth:`DeployerRegistry.detect`, which stops at the first match;
    the wizard wants every match, so the operator can see what else the
    repository looked like and pick a different type than the one WASM
    would have guessed.

    Args:
        path: Directory holding the fetched source.

    Returns:
        The matching deployer classes, most specific first (registry
        priority order). Empty when nothing recognises the tree.
    """
    _import_deployers()
    matches = []
    for deployer_class in DeployerRegistry.in_detection_order():
        if deployer_class.APP_TYPE == "auto":
            continue
        if deployer_class(verbose=False).detect(path):
            matches.append(deployer_class)
    return matches


def inspect_source(source: str, *, branch: str | None = None) -> SourceInspection:
    """
    Fetch a repository into a throwaway checkout and report what it is.

    Args:
        source: Git URL, archive URL or local path, exactly as
            :meth:`SourceManager.fetch` accepts it.
        branch: Branch to check out. Meaningful only for a Git source; a
            source that carries no concept of a branch reports it back
            verbatim in :attr:`SourceInspection.branch` regardless.

    Returns:
        What the wizard needs to preview the deployment: the detected
        type(s), the commands the deployer would run, the discovered
        environment variables, and where the checkout is, revision-wise.

    Raises:
        SourceError: The source is invalid, or fetching it failed (an
            unreachable host, a repository that does not exist, a clone
            that failed).
        DeploymentError: The checkout does not match any registered
            application type.
    """
    with tempfile.TemporaryDirectory(prefix="wasm-inspect-") as scratch:
        return _inspect_checkout(source, branch, Path(scratch) / "source")


def _inspect_checkout(source: str, branch: str | None, app_path: Path) -> SourceInspection:
    """
    Fetch a source into ``app_path`` and describe it.

    Args:
        source: Git URL, archive URL or local path.
        branch: Branch to fetch, for git sources.
        app_path: Where to fetch; it must not exist yet.

    Returns:
        The inspection.

    Raises:
        SourceError: The source is invalid or could not be fetched.
        DeploymentError: No application type matches the checkout.
    """
    source_manager = SourceManager()
    source_manager.fetch(source, app_path, branch=branch, depth=1)

    matched = _matching_types(app_path)
    if not matched:
        raise DeploymentError(
            f"Could not identify the application type at {source}",
            details=(
                "Nothing under the fetched source matches a registered "
                "application type. Choose a type explicitly instead of "
                "relying on auto-detection."
            ),
        )

    winner = matched[0]
    instance = winner(verbose=False)
    instance.configure(
        domain=_PLACEHOLDER_DOMAIN,
        source=source,
        branch=branch,
        app_path=app_path,
    )

    package_manager: str | None = None
    if (app_path / "package.json").exists():
        package_manager = PackageManagerHelper().detect(app_path, "auto")

    install_command: list[str] = []
    build_command: list[str] = []
    start_command = ""
    if isinstance(instance, BaseDeployer):
        if package_manager is not None:
            instance.package_manager = package_manager
        install_command = instance.get_install_command()
        build_command = instance.get_build_command()
        start_command = instance.get_start_command()

    env_keys = [
        EnvKey(
            name=variable.name,
            default=variable.default or None,
            secret=_looks_secret(variable.name),
            required=variable.required,
        )
        for variable in EnvManager().discover(app_path)
    ]

    repo_info = source_manager.get_repo_info(app_path)
    resolved_branch = branch or repo_info.get("branch") or ""
    commit = repo_info.get("commit") or ""

    return SourceInspection(
        app_type=winner.APP_TYPE,
        detected_types=[deployer_class.APP_TYPE for deployer_class in matched],
        package_manager=package_manager,
        install_command=install_command,
        build_command=build_command,
        start_command=start_command,
        default_port=winner.DEFAULT_PORT,
        env_keys=env_keys,
        branch=resolved_branch,
        commit=commit,
    )
