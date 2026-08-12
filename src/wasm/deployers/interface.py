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
from pathlib import Path
from typing import Any, ClassVar

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

    def __init__(self, verbose: bool = False):
        """
        Args:
            verbose: Enable verbose logging.

        Note:
            Declared here so the registry can build any deployer the same way.
            Subclasses do their own setup and are not required to call this.
        """
        self.verbose = verbose

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
