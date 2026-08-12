# Copyright (c) 2024-2026 Yago Lopez Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
``--type auto``, which used to be the default and never detected anything.

The parser defaults ``--type`` to ``auto``; the CLI then replaced ``auto`` with
``"nodejs"`` and deployed. Every Next.js, Vite, Django and static repository
deployed without an explicit ``-t`` therefore ran through the generic Node
deployer, and the whole registry plus every ``detect()`` implementation was dead
code on the default path.

Detection needs the source, and the source arrives over the network, so this
deployer fetches first and decides second, then hands the work to the real
deployer without re-cloning.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from wasm.core.config import Config
from wasm.core.exceptions import DeploymentError
from wasm.core.logger import Logger
from wasm.core.utils import domain_to_app_name
from wasm.deployers.interface import AppDeployer
from wasm.deployers.registry import DeployerRegistry
from wasm.managers.source_manager import SourceManager


class AutoDeployer(AppDeployer):
    """
    Fetches the source, identifies what it is, and delegates.

    Attributes:
        resolved_type: The application type detection settled on, available
            after :meth:`deploy` for callers that want to report it.
        delegate: The deployer that actually performed the deployment.
    """

    APP_TYPE = "auto"
    DISPLAY_NAME = "Auto-detect"
    DEFAULT_PORT = 3000

    #: Never wins detection; it is the thing that runs detection.
    DETECTION_PRIORITY = -1

    #: Chosen when a directory holds nothing any deployer recognises. A repo
    #: with a package.json but no framework is a Node app; a repo with nothing
    #: at all is a mistake worth reporting, so this is only the fallback for
    #: trees that at least look like source.
    FALLBACK_TYPE = "nodejs"

    def __init__(self, verbose: bool = False):
        """
        Args:
            verbose: Enable verbose logging.
        """
        self.verbose = verbose
        self.logger = Logger(verbose=verbose)
        self.config = Config()
        self._source_manager: SourceManager | None = None

        self.domain: str = ""
        self.source: str = ""
        self.app_path: Path = Path()
        self.app_name: str = ""
        self.branch: str | None = None
        self._options: dict[str, Any] = {}

        self.resolved_type: str | None = None
        self.delegate: AppDeployer | None = None

    @property
    def source_manager(self) -> SourceManager:
        """The manager that fetches source code, built on first use."""
        if self._source_manager is None:
            self._source_manager = SourceManager(verbose=self.verbose)
        return self._source_manager

    @source_manager.setter
    def source_manager(self, manager: SourceManager) -> None:
        """
        Replace the source manager.

        Args:
            manager: The manager to use instead of the default.
        """
        self._source_manager = manager

    def detect(self, path: Path) -> bool:
        """
        Never claims a directory: this deployer asks the others.

        Args:
            path: Directory containing the fetched source code.

        Returns:
            Always False.
        """
        return False

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
        Record the settings, to be replayed on the deployer that is chosen.

        Args:
            domain: Target domain.
            source: Git URL or local path.
            port: Application port.
            webserver: ``nginx`` or ``apache``.
            ssl: Request a certificate.
            branch: Git branch.
            env_vars: Environment variables for the application.
            app_path: Override the directory the application lives in.
            package_manager: Node package manager, or ``auto``.
            include_www: Also serve and certify the ``www.`` subdomain.
            **options: Passed through to the chosen deployer.
        """
        self.domain = domain
        self.source = source
        self.branch = branch
        self.app_name = domain_to_app_name(domain)
        self.app_path = app_path or (self.config.apps_directory / self.app_name)
        self._options = {
            "port": port,
            "webserver": webserver,
            "ssl": ssl,
            "branch": branch,
            "env_vars": env_vars,
            "app_path": self.app_path,
            "package_manager": package_manager,
            "include_www": include_www,
            **options,
        }

    def resolve(self) -> AppDeployer:
        """
        Fetch the source and build the deployer that matches it.

        Returns:
            A configured deployer, with fetching already done.

        Raises:
            DeploymentError: If the source cannot be fetched, or if the tree
                matches nothing and does not even look like source code.
        """
        if not self.domain:
            raise DeploymentError(
                "Deployer was not configured",
                details="Call configure(domain=..., source=...) before deploy().",
            )

        self.logger.substep(f"Source: {self.source}")
        self.source_manager.fetch(self.source, self.app_path, branch=self.branch)

        app_type = DeployerRegistry.detect(self.app_path, verbose=self.verbose)
        if app_type is None:
            if not any(self.app_path.iterdir()):
                raise DeploymentError(
                    f"Nothing to deploy at {self.source}",
                    details="The fetched source directory is empty.",
                )
            app_type = self.FALLBACK_TYPE
            self.logger.warning(
                f"Could not identify the project; falling back to {app_type}. "
                "Pass --type explicitly to choose."
            )
        else:
            self.logger.substep(f"Detected application type: {app_type}")

        self.resolved_type = app_type
        deployer_class = DeployerRegistry.get(app_type)
        if deployer_class is None:  # pragma: no cover - registry is populated above
            raise DeploymentError(f"Detected unknown application type: {app_type}")

        delegate = deployer_class(verbose=self.verbose)
        delegate.configure(self.domain, self.source, **self._options)
        # The code is already on disk; re-fetching would clean the directory and
        # clone it a second time.
        delegate.source_already_fetched = True
        self.delegate = delegate
        return delegate

    def deploy(self) -> bool:
        """
        Detect the application type, then run its deployment.

        Returns:
            True if the application ended up deployed.

        Raises:
            WASMError: Whatever the chosen deployer raised.
        """
        return self.resolve().deploy()


DeployerRegistry.register(AutoDeployer)
