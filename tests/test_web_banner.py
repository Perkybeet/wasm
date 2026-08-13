# Copyright (c) 2024-2026 Yago Lopez Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
Tests for the panel's startup banner.

The banner is the whole handover between the CLI and the operator: it carries
the only copy of the access token, and the address it prints is the only thing
the operator has to go on. On a headless server that address is loopback, so a
banner that names it without saying how to forward it hands somebody a
credential for a page they cannot open.
"""

from __future__ import annotations

import pytest

from wasm.web.server import startup_banner


def test_a_loopback_banner_carries_the_ssh_tunnel(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    The reported failure, on a VPS with no desktop.

    Args:
        monkeypatch: Patching helper, scoped to the test.
    """
    monkeypatch.setattr("wasm.core.net.server_address", lambda: "198.51.100.7")
    monkeypatch.setattr("wasm.core.net._current_user", lambda: "root")

    banner = "\n".join(startup_banner("wasm_tok", host="127.0.0.1", port=8081, scheme="http"))

    assert "ssh -L 8081:127.0.0.1:8081 root@198.51.100.7" in banner
    assert "http://localhost:8081" in banner


def test_the_banner_still_carries_the_token_and_the_address() -> None:
    """The forwarding instructions are an addition, not a replacement."""
    banner = "\n".join(startup_banner("wasm_tok", host="127.0.0.1", port=8081, scheme="http"))

    assert "wasm_tok" in banner
    assert "http://127.0.0.1:8081" in banner
    assert "full root access" in banner


def test_a_reachable_banner_has_no_tunnel_instructions() -> None:
    """Nothing is explained when the browser can already open the address."""
    banner = "\n".join(startup_banner("wasm_tok", host="198.51.100.7", port=8080, scheme="https"))

    assert "ssh -L" not in banner
    assert "https://198.51.100.7:8080" in banner
