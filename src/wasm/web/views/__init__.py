# Copyright (c) 2024-2026 Yago Lopez Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
Server-rendered pages for the control panel.

The panel is hypermedia: the server sends HTML, htmx swaps fragments of it, and
the only JavaScript that exists covers the three things hypermedia cannot do
(a terminal, notices that outlive a swap, and the theme toggle).

This package holds the presentation layer and nothing else. It reads through
the managers exactly like the CLI does, so there is one implementation of the
product rather than two.
"""

from wasm.web.views.rendering import fragment, page, templates
from wasm.web.views.router import router

__all__ = ["fragment", "page", "router", "templates"]
