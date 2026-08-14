# Copyright (c) 2024-2025 Yago López Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
WASM - Web App System Management

A robust CLI tool for deploying and managing web applications on Linux servers.
"""

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _installed_version

#: Fallback used when running from a source tree that was never installed, such
#: as an OBS build directory. The single source of truth is the ``version``
#: field of pyproject.toml; ``scripts/release.py`` keeps this literal and the
#: distribution packaging files in step with it.
_FALLBACK_VERSION = "1.4.0"

try:
    __version__ = _installed_version("wasm-cli")
except PackageNotFoundError:  # pragma: no cover - only hit in uninstalled trees
    __version__ = _FALLBACK_VERSION

__author__ = "Yago López Prado"
__license__ = "WASM-NCSAL"

from wasm.core.config import Config
from wasm.core.exceptions import WASMError
from wasm.core.logger import Logger

__all__ = [
    "Config",
    "Logger",
    "WASMError",
    "__version__",
]
