# Copyright (c) 2024-2026 Yago Lopez Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
Apache virtual host manager.

The implementation lives in :mod:`wasm.managers.webserver`. This module is the
apache backend binding and nothing else: every method this class used to define
was the nginx one with ``apache2`` written in it, and the differences that were
real - the ``.conf`` suffix, ``a2ensite``, the module list - are now data on
:data:`~wasm.managers.webserver.APACHE_BACKEND`.
"""

from __future__ import annotations

from wasm.core.fs import FileSystem
from wasm.core.runner import CommandRunner
from wasm.managers.webserver import (
    APACHE_BACKEND,
    SiteInfo,
    WebServerBackend,
    WebServerManager,
    WebServerStatus,
)

__all__ = ["ApacheManager", "SiteInfo", "WebServerStatus"]


class ApacheManager(WebServerManager):
    """
    Manager for Apache site configurations.

    Handles creating, enabling, disabling and removing Apache virtual hosts.
    """

    #: Kept as class attributes because callers and tests read them to build
    #: paths without instantiating the manager.
    SITES_AVAILABLE = APACHE_BACKEND.sites_available
    SITES_ENABLED = APACHE_BACKEND.sites_enabled

    def __init__(
        self,
        verbose: bool = False,
        runner: CommandRunner | None = None,
        backend: WebServerBackend | None = None,
        fs: FileSystem | None = None,
    ) -> None:
        """
        Initialize the Apache manager.

        Args:
            verbose: Enable verbose logging.
            runner: Command runner to execute with. Defaults to the process-wide
                one.
            backend: Backend override, used by tests to point the manager at a
                temporary configuration tree.
            fs: Filesystem to write through. Defaults to the process-wide one.
        """
        super().__init__(backend or APACHE_BACKEND, verbose=verbose, runner=runner, fs=fs)
