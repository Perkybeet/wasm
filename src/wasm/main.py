# Copyright (c) 2024-2026 Yago Lopez Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
Entry point.

The command tree lives in :mod:`wasm.cli.app`. This module stays because
``wasm.main:cli`` is the console script recorded in every already-installed
copy, and an upgrade must not leave those users with a package whose entry
point has moved.
"""

from __future__ import annotations

from wasm.cli.app import entrypoint as cli
from wasm.cli.app import main

__all__ = ["cli", "main"]


if __name__ == "__main__":
    cli()
