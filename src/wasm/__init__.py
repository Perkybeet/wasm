# Copyright (c) 2024-2025 Yago López Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
WASM - Web App System Management

A robust CLI tool for deploying and managing web applications on Linux servers.
"""

__version__ = "0.13.5"
__author__ = "Yago López Prado"
__license__ = "WASM-NCSAL"

from wasm.core.config import Config
from wasm.core.logger import Logger
from wasm.core.exceptions import WASMError

__all__ = [
    "Config",
    "Logger",
    "WASMError",
    "__version__",
]
