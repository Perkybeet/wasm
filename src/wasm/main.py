# Copyright (c) 2024-2026 Yago Lopez Prado
# SPDX-License-Identifier: AGPL-3.0-or-later

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
