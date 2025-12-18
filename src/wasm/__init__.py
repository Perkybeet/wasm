"""
WASM - Web App System Management

A robust CLI tool for deploying and managing web applications on Linux servers.
"""

__version__ = "0.10.1"
__author__ = "WASM Team"
__license__ = "MIT"

from wasm.core.config import Config
from wasm.core.logger import Logger
from wasm.core.exceptions import WASMError

__all__ = [
    "Config",
    "Logger",
    "WASMError",
    "__version__",
]
