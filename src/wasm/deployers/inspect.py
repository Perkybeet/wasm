# Copyright (c) 2024-2026 Yago Lopez Prado
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Repository inspection for the new-app wizard.

D7 of the v2 design ("Asistente de alta") pastes a repository and shows the
operator what WASM would do with it before anything is deployed: which
application type it matches, what commands would install, build and start
it, which environment variables it declares, which commit it is at, and
whether this server can deploy it at all. This module answers all of that
without creating an application, a domain or a unit.

**A git source is never cloned for this.** ``git ls-remote`` goes first: it
answers in about a second whether the repository is reachable, whether the
credentials are accepted and which branch is the default, so a private
repository fails with the actionable credentials error before anything is
downloaded. Then a blobless, shallow clone without a checkout fetches one
commit's trees, and a non-cone sparse checkout materialises only the files
the detectors read (:func:`sparse_patterns`, read off the deployers
themselves). A git too old for that falls back to the shallow clone this
used to be, without ``--recursive``: submodules never decide a type.

A local directory is read where it is; detection only reads.

**Cancel is real.** :func:`inspect_source` runs every command inside a
:func:`~wasm.core.runner.cancellable` scope: when the caller sets the event
(the console's request whose browser went away), the running git is killed
with every process it started, and the scratch directory is removed before
this returns. What a killed process cannot clean up, a killed console cannot
either, so :func:`remove_stale_checkouts` runs when the console starts.

The scratch directory is the one piece of this package that is not created
through ``wasm.core.fs``, on purpose: it is a preview, not a deployment, so
there is nothing for ``--dry-run`` to rehearse, and it must be cleaned up for
real even when the process-wide filesystem is a dry run.
``tests/test_deployers.py`` lists it as a named exemption of its filesystem
guard, with this reason; the directories inspection adds inside its own
checkout and the stale-checkout sweep are exempted the same way.

Only detection runs against the checkout, never a deployer's install/build
hooks: those create virtualenvs, run ``npm install`` and spawn processes,
which is the deployment itself, not a preview of it. The commands reported
here are what a deployer would run, read off its ``get_*_command`` methods
once ``configure()`` and package-manager detection have run; a framework
hook that refines them further (a build actually setting standalone mode, a
venv actually existing) only runs during the real deploy.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import stat
import tempfile
import threading
import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from wasm.core.exceptions import DeploymentError
from wasm.core.runner import CommandCancelled, cancellable, get_runner
from wasm.deployers.base import BaseDeployer
from wasm.deployers.docker_compose import COMPOSE_FILE_PRIORITY
from wasm.deployers.helpers.env_manager import EnvManager
from wasm.deployers.helpers.package_manager import PackageManagerHelper
from wasm.deployers.interface import AppDeployer
from wasm.deployers.registry import DeployerRegistry, _import_deployers
from wasm.managers.source_manager import SourceManager
from wasm.validators.source import validate_source

_logger = logging.getLogger(__name__)

#: Prefix of every scratch directory, and how the stale sweep recognises one.
SCRATCH_PREFIX = "wasm-inspect-"

#: A scratch directory older than this belongs to an inspection that is no
#: longer running: every git step has a deadline far shorter.
STALE_CHECKOUT_AGE = 3600

#: Where :meth:`EnvManager.discover` looks for example environment files,
#: besides the root, and the names it looks for.
ENV_EXAMPLE_PARENTS = ("apps", "packages", "services")
ENV_EXAMPLE_FILES = (".env.example", ".env.template", ".env.sample")

#: Root files read by detection beyond the deployers' ``DETECTION_FILES``,
#: each with its reader. :func:`sparse_patterns` adds these to what it reads
#: off the deployers; ``tests/test_inspect.py`` inspects every detector's
#: trees both ways and fails when a reader is missing here.
_ALSO_READ_AT_ROOT: tuple[str, ...] = (
    # NextJS/Vite/NodeJS/Monorepo detect() read its dependencies.
    "package.json",
    # PackageManagerHelper.detect and get_install_command.
    "pnpm-lock.yaml",
    "bun.lockb",
    "yarn.lock",
    "package-lock.json",
    "npm-shrinkwrap.json",
    # StaticDeployer.detect: an index.html next to these is not a static site.
    "requirements.txt",
    "pyproject.toml",
    "Cargo.toml",
    "go.mod",
    *ENV_EXAMPLE_FILES,
)

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

#: Root files of stacks WASM has no deployer for, and what they are called.
_UNSUPPORTED_STACKS: tuple[tuple[str, str], ...] = (
    ("go.mod", "Go"),
    ("Cargo.toml", "Rust"),
    ("composer.json", "PHP"),
    ("Gemfile", "Ruby"),
    ("pom.xml", "Java (Maven)"),
    ("build.gradle", "Java (Gradle)"),
    ("build.gradle.kts", "Kotlin (Gradle)"),
    ("mix.exs", "Elixir"),
    ("deno.json", "Deno"),
)

#: Framework dependencies NodeJSDeployer.detect turns away and nothing else
#: deploys.
_UNSUPPORTED_NODE_FRAMEWORKS: tuple[tuple[str, str], ...] = (
    ("nuxt", "Nuxt"),
    ("@angular/core", "Angular"),
)

_DOCKERFILES = ("Dockerfile", "Containerfile")

#: Directories never worth naming as "a project lives here".
_NOT_PROJECTS = frozenset({"node_modules", ".git", "vendor", ".venv", "venv", "__pycache__"})

#: The smallest compose file that deploys a Dockerfile.
_COMPOSE_EXAMPLE = "\n".join(
    ["services:", "  app:", "    build: .", "    ports:", '      - "3000:3000"']
)


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
        compatible: Whether this server can deploy it as ``app_type`` as it
            is (see :class:`Verdict`).
        verdict: What WASM found, in a sentence.
        suggestion: What to do before deploying, or None.
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
    compatible: bool = True
    verdict: str = ""
    suggestion: str | None = None


@dataclass(frozen=True)
class Verdict:
    """
    Whether WASM can deploy what an inspection found, and if not, what to do.

    Attributes:
        compatible: True when a deploy of the detected type can succeed on
            this server as it is.
        summary: What was found, in one or two sentences.
        suggestion: What to do about it, or None when there is nothing to do.
    """

    compatible: bool
    summary: str
    suggestion: str | None = None


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


def sparse_patterns() -> list[str]:
    """
    The files inspection needs from a repository, as sparse-checkout patterns.

    Read off the deployers themselves: every registered type's
    ``DETECTION_FILES`` and ``FRAMEWORK_CONFIG_FILES``, the compose file
    names, plus the files detection reads beyond those
    (:data:`_ALSO_READ_AT_ROOT`), each workspace app's ``package.json``
    (the monorepo detector counts them) and the example environment files
    :meth:`EnvManager.discover` reads under ``apps/``, ``packages/`` and
    ``services/``.

    Returns:
        Sorted, anchored non-cone patterns (``/package.json``,
        ``/apps/*/package.json``); ``*`` never spans a directory.
    """
    _import_deployers()
    root: set[str] = set(COMPOSE_FILE_PRIORITY) | set(_ALSO_READ_AT_ROOT)
    for deployer_class in DeployerRegistry.in_detection_order():
        root.update(deployer_class.DETECTION_FILES)
        root.update(getattr(deployer_class, "FRAMEWORK_CONFIG_FILES", ()))
    nested = {"apps/*/package.json"} | {
        f"{parent}/*/{name}" for parent in ENV_EXAMPLE_PARENTS for name in ENV_EXAMPLE_FILES
    }
    return sorted(f"/{name}" for name in root | nested)


def inspect_source(
    source: str, *, branch: str | None = None, cancel: threading.Event | None = None
) -> SourceInspection:
    """
    Report what a repository is, fetching as little of it as possible.

    Args:
        source: Git URL, archive URL or local path, exactly as
            :meth:`SourceManager.fetch` accepts it.
        branch: Branch to inspect. Meaningful only for a Git source; a
            source that carries no concept of a branch reports it back
            verbatim in :attr:`SourceInspection.branch` regardless.
        cancel: Set it, from any thread, to stop the inspection: the git
            command running is killed and the scratch directory removed
            before this returns.

    Returns:
        What the wizard needs to preview the deployment: the detected
        type(s), the commands the deployer would run, the discovered
        environment variables, where the checkout is, revision-wise, and
        whether this server can deploy it.

    Raises:
        SourceError: The source is invalid, or fetching it failed (an
            unreachable host, refused credentials, a repository or branch
            that does not exist).
        DeploymentError: Nothing in the source matches a registered
            application type. The message says what was found instead and
            ``details`` what to do (a Compose file for a lone Dockerfile,
            where the projects are when they are not at the root, ...).
        CommandCancelled: ``cancel`` was set.
    """
    event = cancel if cancel is not None else threading.Event()
    with cancellable(event):
        _stop_if_cancelled(event)
        source_type, normalized = validate_source(source)
        if source_type == "local":
            return _inspect_directory(Path(normalized), branch)
        if source_type == "git":
            return _inspect_git(normalized, branch, event)
        return _inspect_archive(normalized, branch, event)


def _stop_if_cancelled(event: threading.Event) -> None:
    """
    Stop between steps that run no command, once cancelled.

    Args:
        event: The inspection's cancel event.

    Raises:
        CommandCancelled: When the event is set.
    """
    if event.is_set():
        raise CommandCancelled("Inspection cancelled")


def _inspect_directory(path: Path, branch: str | None) -> SourceInspection:
    """
    Describe a local directory in place.

    Args:
        path: The directory.
        branch: Echoed back; a directory has none.

    Returns:
        The inspection.

    Raises:
        SourceError: The path is not an existing directory.
        DeploymentError: Nothing matches.
    """
    if not path.is_dir():
        # The same refusal SourceManager.copy_local gives, which is what
        # answered here while inspection still copied the directory.
        SourceManager().copy_local(path, path)
    return _describe(path, branch=branch, listing=_walk(path), repository=None)


def _inspect_git(source: str, branch: str | None, event: threading.Event) -> SourceInspection:
    """
    Probe a git remote, check out the files detection reads, describe them.

    Args:
        source: Validated git URL, possibly with ``#branch``.
        branch: Branch requested, or None for the URL's or the default.
        event: The inspection's cancel event.

    Returns:
        The inspection.

    Raises:
        SourceError: The remote cannot be read, refuses the credentials, or
            has no such branch.
        DeploymentError: Nothing matches.
    """
    manager = SourceManager()
    # No separate ls-remote first: git runs non-interactively, so a private
    # repository or a typo fails the clone itself in one round trip, and an
    # extra round trip made every small repository slower than a plain clone.
    # The clone also takes a tag where a branch is named, as deploys do.
    with tempfile.TemporaryDirectory(prefix=SCRATCH_PREFIX) as scratch:
        checkout = Path(scratch) / "source"
        if manager.sparse_clone(source, checkout, branch=branch, patterns=sparse_patterns()):
            listing = manager.list_files(checkout)
            _add_directory_skeleton(checkout, listing)
        else:
            # This git cannot check out sparsely; a fresh directory rather
            # than removing the failed attempt, which the scratch directory
            # takes with it anyway.
            checkout = Path(scratch) / "clone"
            manager.clone_git(source, checkout, branch=branch, depth=1, recursive=False)
            listing = manager.list_files(checkout)
        _stop_if_cancelled(event)
        return _describe(checkout, branch=branch, listing=listing, repository=manager)


def _inspect_archive(source: str, branch: str | None, event: threading.Event) -> SourceInspection:
    """
    Download and extract an archive into a scratch directory and describe it.

    Args:
        source: Validated archive URL.
        branch: Echoed back; an archive has none.
        event: The inspection's cancel event.

    Returns:
        The inspection.

    Raises:
        SourceError: The download or the extraction failed.
        DeploymentError: Nothing matches.
    """
    with tempfile.TemporaryDirectory(prefix=SCRATCH_PREFIX) as scratch:
        checkout = Path(scratch) / "source"
        SourceManager().fetch(source, checkout, depth=1)
        _stop_if_cancelled(event)
        return _describe(checkout, branch=branch, listing=_walk(checkout), repository=None)


def _add_directory_skeleton(checkout: Path, listing: Iterable[str]) -> None:
    """
    Create the workspace directories the sparse checkout left out.

    Detection counts the directories under ``apps/`` (the Compose detector
    counts every one, with or without a ``package.json``) and
    :meth:`EnvManager.discover` walks those of ``apps/``, ``packages/`` and
    ``services/``. A sparse checkout only creates the ones holding a file it
    kept, so the rest are created empty from the tree listing: the detectors
    then see the same directories a full checkout has.

    Args:
        checkout: The sparse checkout, inside the scratch directory.
        listing: Every file of the commit, from :meth:`SourceManager.list_files`.
    """
    for name in listing:
        parts = PurePosixPath(name).parts
        if len(parts) < 3 or parts[0] not in ENV_EXAMPLE_PARENTS:
            continue
        if parts[1] in {".", "..", ".git"} or (checkout / parts[0]).is_symlink():
            continue
        os.makedirs(checkout / parts[0] / parts[1], exist_ok=True)


def _walk(root: Path, max_depth: int = 3) -> list[str]:
    """
    Name the files of a directory tree, a few levels deep.

    Args:
        root: The tree.
        max_depth: Deepest path, in components, worth listing.

    Returns:
        Paths relative to ``root``, POSIX separators. Links are not followed.
    """
    listing: list[str] = []
    for directory, subdirectories, files in os.walk(root):
        relative = PurePosixPath(Path(directory).relative_to(root).as_posix())
        depth = 0 if str(relative) == "." else len(relative.parts)
        subdirectories[:] = [
            d for d in subdirectories if d not in _NOT_PROJECTS and depth + 1 < max_depth
        ]
        listing.extend(str(relative / name) if depth else name for name in files)
    return sorted(listing)


def _describe(
    checkout: Path,
    *,
    branch: str | None,
    listing: list[str],
    repository: SourceManager | None,
) -> SourceInspection:
    """
    Run detection over a checkout and describe the result.

    Args:
        checkout: The files to inspect.
        branch: Branch requested, or None.
        listing: Every file of the source (or the first levels of it), which
            a sparse checkout does not have on disk.
        repository: The manager to read branch and commit through, for a git
            checkout; None for anything else.

    Returns:
        The inspection.

    Raises:
        DeploymentError: Nothing matches; the error carries the verdict.
    """
    matched = _matching_types(checkout)
    if not matched:
        verdict = _unmatched_verdict(checkout, listing)
        raise DeploymentError(verdict.summary, details=verdict.suggestion or "")

    winner = matched[0]
    instance = winner(verbose=False)
    instance.configure(
        domain=_PLACEHOLDER_DOMAIN,
        source=str(checkout),
        branch=branch,
        app_path=checkout,
    )

    package_manager: str | None = None
    if (checkout / "package.json").exists():
        package_manager = PackageManagerHelper().detect(checkout, "auto")

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
        for variable in EnvManager().discover(checkout)
    ]

    resolved_branch = branch or ""
    commit = ""
    if repository is not None:
        repo_info = repository.get_repo_info(checkout)
        resolved_branch = branch or repo_info.get("branch") or ""
        commit = repo_info.get("commit") or ""

    verdict = _matched_verdict(matched)
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
        compatible=verdict.compatible,
        verdict=verdict.summary,
        suggestion=verdict.suggestion,
    )


# Verdicts ------------------------------------------------------------------


def _matched_verdict(matched: list[type[AppDeployer]]) -> Verdict:
    """
    Judge a source that matched: can this server deploy it as it is?

    Args:
        matched: The matching types, most specific first.

    Returns:
        Compatible unless a program the chosen type declares it needs
        (``SYSTEM_DEPS``) is missing here.
    """
    winner = matched[0]
    runner = get_runner()
    missing = [
        program for program in getattr(winner, "SYSTEM_DEPS", ()) if not runner.exists(program)
    ]
    if missing:
        return Verdict(
            compatible=False,
            summary=(
                f"This is a {winner.DISPLAY_NAME} project, but this server does not have "
                f"{', '.join(missing)}."
            ),
            suggestion="Install what it needs with `wasm setup init`, then deploy it.",
        )
    summary = f"WASM can deploy this as {winner.DISPLAY_NAME}."
    others = [deployer_class.DISPLAY_NAME for deployer_class in matched[1:]]
    if others:
        summary += (
            f" It also looks like {', '.join(others)}; choose that type instead to deploy "
            "it that way."
        )
    return Verdict(compatible=True, summary=summary)


def _unmatched_verdict(checkout: Path, listing: list[str]) -> Verdict:
    """
    Say why nothing matched, and what would make it deployable.

    Works from names in the listing (a sparse checkout has little on disk)
    and from the root ``package.json``, which the sparse checkout keeps.

    Args:
        checkout: The files inspected.
        listing: Every file of the source.

    Returns:
        An incompatible verdict with a suggestion.
    """
    root_files = {name for name in listing if "/" not in name}
    container_route = (
        "Build it into a container instead: add a Dockerfile, and a compose.yaml next to "
        f"it that builds it:\n\n{_COMPOSE_EXAMPLE}\n\nwith the port it listens on, then "
        "deploy it as Docker Compose."
    )

    if "turbo.json" in root_files:
        return _near_miss_monorepo(checkout, listing)

    dockerfile = next((name for name in _DOCKERFILES if name in root_files), None)
    if dockerfile:
        return Verdict(
            compatible=False,
            summary=f"The repository has a {dockerfile} but no Compose file.",
            suggestion=(
                "WASM runs containers through Docker Compose. Commit a compose.yaml next "
                f"to the {dockerfile} that builds it:\n\n{_COMPOSE_EXAMPLE}\n\nwith the "
                "port the image listens on, then deploy it as Docker Compose."
            ),
        )

    for marker, stack in _UNSUPPORTED_STACKS:
        if marker in root_files:
            return Verdict(
                compatible=False,
                summary=f"This is a {stack} project ({marker}); WASM has no {stack} deployer.",
                suggestion=container_route,
            )

    if "package.json" in root_files:
        return _unrecognised_package_json(checkout / "package.json", container_route)

    projects = _subprojects(listing)
    if projects:
        found = ", ".join(f"{directory}/ ({marker})" for directory, marker in projects)
        return Verdict(
            compatible=False,
            summary=f"Nothing WASM deploys at the root of the repository; found {found}.",
            suggestion=(
                "WASM deploys a repository from its root. Deploy each project from a "
                "repository of its own, or add a compose.yaml at the root that builds "
                "them and deploy it as Docker Compose. A JavaScript workspace with "
                "turbo.json, a workspace config and its apps under apps/ deploys as a "
                "monorepo."
            ),
        )

    _import_deployers()
    examples = sorted(
        {
            name
            for deployer_class in DeployerRegistry.in_detection_order()
            for name in deployer_class.DETECTION_FILES
        }
    )
    return Verdict(
        compatible=False,
        summary="Nothing in the repository matches an application type WASM deploys.",
        suggestion=(
            "WASM recognises a project by the files at the root of the repository ("
            f"{', '.join(examples)}, or a package.json with a start script). Check the "
            "branch, or choose a type explicitly instead of relying on auto-detection."
        ),
    )


def _near_miss_monorepo(checkout: Path, listing: list[str]) -> Verdict:
    """
    Explain what a Turborepo lacks for the monorepo type.

    Args:
        checkout: The files inspected.
        listing: Every file of the source.

    Returns:
        An incompatible verdict naming what is missing.
    """
    apps = sorted(
        {
            PurePosixPath(name).parts[1]
            for name in listing
            if len(PurePosixPath(name).parts) == 3
            and name.startswith("apps/")
            and name.endswith("/package.json")
        }
    )
    workspace = "pnpm-workspace.yaml" in listing or "workspaces" in _read_package_json(
        checkout / "package.json"
    )
    problems = []
    if not workspace:
        problems.append('no workspace (pnpm-workspace.yaml, or "workspaces" in package.json)')
    if not apps:
        problems.append("no app under apps/ has a package.json")
    elif len(apps) < 2:
        problems.append(f"only apps/{apps[0]}/ has a package.json under apps/")
    summary = "The repository uses Turborepo (turbo.json) but is not a monorepo WASM deploys"
    summary += f": {'; '.join(problems)}." if problems else "."
    return Verdict(
        compatible=False,
        summary=summary,
        suggestion=(
            "The monorepo type needs turbo.json, a workspace config and at least two apps "
            "under apps/, each with its own package.json. With a single app, move it to the "
            "root of the repository; or add a compose.yaml at the root that builds the apps "
            "and deploy it as Docker Compose."
        ),
    )


def _unrecognised_package_json(package_json: Path, container_route: str) -> Verdict:
    """
    Explain why a root ``package.json`` matched no Node type.

    Args:
        package_json: The file.
        container_route: The generic "build it into a container" suggestion.

    Returns:
        An incompatible verdict.
    """
    try:
        package = json.loads(package_json.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return Verdict(
            compatible=False,
            summary="package.json cannot be read as JSON.",
            suggestion=f"Fix package.json and inspect again ({exc}).",
        )
    if not isinstance(package, dict):
        package = {}
    dependencies = {
        **(package.get("dependencies") or {}),
        **(package.get("devDependencies") or {}),
    }
    for dependency, framework in _UNSUPPORTED_NODE_FRAMEWORKS:
        if dependency in dependencies:
            return Verdict(
                compatible=False,
                summary=f"This is a {framework} project; WASM has no {framework} deployer.",
                suggestion=container_route,
            )
    return Verdict(
        compatible=False,
        summary=(
            "package.json has no start script, no main entry and no framework WASM "
            "recognises (Next.js, Vite)."
        ),
        suggestion=(
            'Add a start script to package.json ("scripts": {"start": "node server.js"}) '
            'or a "main" entry, so WASM knows how to run it; or choose the type explicitly.'
        ),
    )


def _read_package_json(path: Path) -> dict:
    """
    Read a package.json, or nothing.

    Args:
        path: The file.

    Returns:
        Its object, or an empty dict when it is missing or not a JSON object.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _subprojects(listing: list[str], limit: int = 5) -> list[tuple[str, str]]:
    """
    Find directories one or two levels down that hold a recognisable project.

    Args:
        listing: Every file of the source.
        limit: How many to name.

    Returns:
        ``(directory, marker file)`` pairs, shallowest first.
    """
    _import_deployers()
    markers = {"package.json", *COMPOSE_FILE_PRIORITY, *_DOCKERFILES}
    for deployer_class in DeployerRegistry.in_detection_order():
        markers.update(deployer_class.DETECTION_FILES)
    found: dict[str, str] = {}
    for name in sorted(listing, key=lambda n: (n.count("/"), n)):
        path = PurePosixPath(name)
        if not 2 <= len(path.parts) <= 3 or path.name not in markers:
            continue
        if any(part in _NOT_PROJECTS for part in path.parts):
            continue
        directory = str(path.parent)
        if directory not in found and not any(directory.startswith(f"{d}/") for d in found):
            found[directory] = path.name
    return list(found.items())[:limit]


# Stale checkouts ------------------------------------------------------------


def _owner(path: Path) -> int:
    """
    Name the account that owns a path, without following a link.

    Args:
        path: The path.

    Returns:
        Its owner's uid.
    """
    return path.lstat().st_uid


def remove_stale_checkouts(
    directory: Path | None = None, *, older_than: float = STALE_CHECKOUT_AGE
) -> list[Path]:
    """
    Remove scratch directories an inspection that died left behind.

    An inspection removes its own directory on every way out, cancel
    included; only a process that was killed outright (the console stopped
    mid-clone) cannot. Run when the console starts.

    The temporary directory is shared with every account on the machine, so
    only real directories owned by this process's user are touched: a
    ``wasm-inspect-*`` link is never followed, whoever made it.

    Args:
        directory: Where scratch directories are created. Defaults to the
            system's temporary directory.
        older_than: Seconds since the last change after which a directory
            is stale.

    Returns:
        The directories removed.
    """
    root = directory if directory is not None else Path(tempfile.gettempdir())
    try:
        entries = [entry for entry in root.iterdir() if entry.name.startswith(SCRATCH_PREFIX)]
    except OSError as exc:
        _logger.warning("Cannot look for stale inspection checkouts in %s: %s", root, exc)
        return []
    now = time.time()
    removed: list[Path] = []
    for path in entries:
        try:
            info = path.lstat()
            if not stat.S_ISDIR(info.st_mode) or _owner(path) != os.geteuid():
                continue
            if now - info.st_mtime < older_than:
                continue
            shutil.rmtree(path)
        except OSError as exc:
            _logger.warning("Cannot remove the stale inspection checkout %s: %s", path, exc)
            continue
        removed.append(path)
    return removed
