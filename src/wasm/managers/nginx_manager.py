# Copyright (c) 2024-2026 Yago Lopez Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
Nginx virtual host manager.

The implementation lives in :mod:`wasm.managers.webserver`; what remains here is
the nginx backend binding plus the one operation nginx has and apache does not.
Keeping the class name means the two dozen call sites, the AST test that checks
them and the ``from wasm.managers import NginxManager`` imports all keep working.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from wasm.core.fs import FileSystem
from wasm.core.runner import CommandRunner
from wasm.managers.webserver import (
    NGINX_BACKEND,
    SiteInfo,
    WebServerBackend,
    WebServerManager,
    WebServerStatus,
)

if TYPE_CHECKING:
    # Imported for typing only: the builder pulls in the deployer helpers, which
    # is a heavier dependency than a site manager should take at import time.
    from wasm.deployers.helpers.nginx_config import NginxAdvancedConfig

__all__ = ["NginxManager", "SiteInfo", "WebServerStatus"]


class NginxManager(WebServerManager):
    """
    Manager for Nginx site configurations.

    Handles creating, enabling, disabling and removing Nginx virtual hosts.
    """

    #: Kept as class attributes because callers and tests read them to build
    #: paths without instantiating the manager.
    SITES_AVAILABLE = NGINX_BACKEND.sites_available
    SITES_ENABLED = NGINX_BACKEND.sites_enabled

    def __init__(
        self,
        verbose: bool = False,
        runner: CommandRunner | None = None,
        backend: WebServerBackend | None = None,
        fs: FileSystem | None = None,
    ) -> None:
        """
        Initialize the Nginx manager.

        Args:
            verbose: Enable verbose logging.
            runner: Command runner to execute with. Defaults to the process-wide
                one.
            backend: Backend override, used by tests to point the manager at a
                temporary configuration tree.
            fs: Filesystem to write through. Defaults to the process-wide one.
        """
        super().__init__(backend or NGINX_BACKEND, verbose=verbose, runner=runner, fs=fs)

    def create_advanced_site(
        self,
        domain: str,
        config: NginxAdvancedConfig,
        ssl: bool = False,
        app_path: str = "",
    ) -> bool:
        """
        Create a multi-route Nginx site configuration.

        Args:
            domain: Domain name.
            config: Parsed ``wasm.nginx.yaml`` configuration.
            ssl: Whether the site serves TLS.
            app_path: Application directory, used as the document root.

        Returns:
            True when the configuration was written.

        Raises:
            NginxError: When the site already exists or cannot be written.
            DomainError: When the domain is not a valid domain name.
            TemplateError: When the advanced template fails to render.
        """
        from wasm.deployers.helpers.nginx_config import NginxConfigBuilder

        builder = NginxConfigBuilder(verbose=self.verbose)
        context = builder.build_context(config, domain, ssl, app_path)
        if self.site_exists(domain):
            return self.update_site(domain, template="advanced", context=context)
        return self.create_site(domain, template="advanced", context=context)
