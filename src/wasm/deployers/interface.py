# Copyright (c) 2024-2026 Yago Lopez Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
The contract every deployer implements.

The registry used to be typed ``dict[str, type[BaseDeployer]]`` while two of the
registered classes, monorepo and docker-compose, inherited from nothing. That
lie had two visible consequences: the CLI needed ``if app_type == "monorepo"``
branches to call each odd one out by hand, and ``POST /api/apps`` raised
TypeError as soon as somebody asked for one of them. This module states the
contract once so the registry tells the truth and any caller can drive any
deployer through the same three calls: ``detect``, ``configure``, ``deploy``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar, TypeAlias

from wasm.core.fs import FileSystem, get_fs

#: Called with a short description as each update step begins.
StepReporter: TypeAlias = Callable[[str], None]

#: Detection precedence. When several deployers recognise the same directory,
#: the one with the highest priority wins, and ties break on the type name so
#: the answer never depends on import order.
#:
#: The ordering encodes what a repository is *for*, from most specific to least:
#:
#: - ``monorepo`` (90): a turbo/pnpm workspace with several deployable apps is a
#:   monorepo even though every one of its apps also has a package.json.
#: - ``docker-compose`` (80): a compose file describes how the author wants the
#:   whole thing run; that beats guessing from a package.json. Note that the
#:   docker-compose deployer itself declines when the compose file is clearly a
#:   local-development database next to a framework project.
#: - ``nextjs`` (70) and ``vite`` (60): specific frameworks beat generic Node.
#: - ``python`` (50): requirements.txt / pyproject.toml.
#: - ``nodejs`` (40): the fallback for any remaining package.json.
#: - ``static`` (10): plain index.html, only when nothing else claimed it.
DEFAULT_DETECTION_PRIORITY = 50


class AppDeployer(ABC):
    """
    Something that can recognise a project and deploy it.

    Attributes:
        APP_TYPE: Stable identifier used by the CLI, the store and the registry.
        DISPLAY_NAME: Human-readable name.
        DETECTION_FILES: Files that hint at this application type. Informational
            only; :meth:`detect` is the authority.
        DEFAULT_PORT: Port used when the caller does not choose one.
        DETECTION_PRIORITY: Precedence when several deployers match one tree.
        source_already_fetched: Set by :class:`~wasm.deployers.auto.AutoDeployer`
            when it has already placed the source at ``app_path``, so the
            delegate does not re-clone (and, with ``clean=True``, delete) it.
    """

    APP_TYPE: str = "base"
    DISPLAY_NAME: str = "Application"
    DETECTION_FILES: ClassVar[list[str]] = []
    DEFAULT_PORT: int = 3000
    DETECTION_PRIORITY: int = DEFAULT_DETECTION_PRIORITY

    source_already_fetched: bool = False

    #: Class-level default so :attr:`fs` answers even for a subclass that builds
    #: its own state without calling ``__init__`` here.
    _fs: FileSystem | None = None

    def __init__(self, verbose: bool = False, fs: FileSystem | None = None):
        """
        Args:
            verbose: Enable verbose logging.
            fs: Filesystem every change goes through. Defaults to the
                process-wide one, which is what makes ``--dry-run`` real for
                what a deployment *writes*, not only for what it runs.

        Note:
            Declared here so the registry can build any deployer the same way.
            Subclasses do their own setup and are not required to call this.
        """
        self.verbose = verbose
        self._fs = fs

    @property
    def fs(self) -> FileSystem:
        """
        The filesystem this deployer changes the machine through.

        Returns:
            The injected filesystem, or the process-wide one.
        """
        return self._fs if self._fs is not None else get_fs()

    @abstractmethod
    def detect(self, path: Path) -> bool:
        """
        Report whether a directory holds this kind of application.

        Args:
            path: Directory containing the fetched source code.

        Returns:
            True when this deployer can handle the project.
        """

    @abstractmethod
    def configure(
        self,
        domain: str,
        source: str,
        *,
        port: int | None = None,
        webserver: str = "nginx",
        ssl: bool = True,
        branch: str | None = None,
        env_vars: dict[str, str] | None = None,
        app_path: Path | None = None,
        package_manager: str = "auto",
        include_www: bool = False,
        **options: Any,
    ) -> None:
        """
        Set up the deployment parameters.

        Every deployer accepts the same core parameters. Deployer-specific
        knobs arrive in ``options`` (for example ``compose_file`` for
        docker-compose, ``workspace_filter`` for monorepo) and are ignored by
        deployers that do not understand them.

        Args:
            domain: Target domain.
            source: Git URL or local path.
            port: Application port. Defaults to ``DEFAULT_PORT``.
            webserver: ``nginx`` or ``apache``.
            ssl: Request a certificate.
            branch: Git branch.
            env_vars: Environment variables for the application.
            app_path: Override the directory the application lives in.
            package_manager: Node package manager, or ``auto``.
            include_www: Also serve and certify the ``www.`` subdomain.
            **options: Deployer-specific settings.
        """

    @abstractmethod
    def deploy(self) -> bool:
        """
        Run the full deployment.

        Returns:
            True when the application ended up deployed.

        Raises:
            WASMError: When a step fails and could not be recovered from. The
                deployer rolls back whatever it created before re-raising.
        """

    def update(self, on_step: StepReporter | None = None) -> UpdateResult:
        """
        Rebuild an application that is already deployed, in place.

        This exists because the CLI used to drive the sequence itself, poking
        at ``_package_manager`` and calling ``pre_install``,
        ``install_dependencies``, ``build`` and ``get_start_command`` in order.
        That made the update flow a second, divergent copy of the deploy
        pipeline, reachable only from one command and testable from none.

        Not abstract: a monorepo rebuilds one workspace of many, and a compose
        project pulls images rather than installing dependencies, so those two
        override it with something that is genuinely different rather than
        being forced into this shape.

        Args:
            on_step: Called as each step begins, so the caller can report
                progress without knowing what the steps are.

        Returns:
            What was done, for the caller to present.

        Raises:
            NotImplementedError: When this deployer has no in-place update.
            WASMError: When a step fails. The application is left running on
                its previous build wherever that is possible.
        """
        raise NotImplementedError(
            f"{type(self).__name__} cannot update in place. Redeploy it instead."
        )


@dataclass
class UpdateResult:
    """
    Outcome of an in-place update.

    Attributes:
        package_manager: The package manager that was used.
        prisma_updated: Whether Prisma client generation and migrations ran.
        is_static: Whether the application has no service to restart.
        start_command: The command the service runs, empty for a static site.
    """

    package_manager: str
    prisma_updated: bool
    is_static: bool
    start_command: str
