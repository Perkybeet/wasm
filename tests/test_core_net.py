# Copyright (c) 2024-2026 Yago Lopez Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
Tests for :mod:`wasm.core.net`.

Two things are being defended here:

- The classification of a listening address. It decides whether WASM will serve
  a root panel on it, and "" , "*", "0", "0.0.0.0" and "::" are five spellings
  of the same socket that a set of known-good strings would miss.
- The instructions given to an operator who bound the panel to loopback. A
  panel on a headless server prints an address that only that server can open,
  so the banner has to say how to reach it or the operator is stuck.
"""

from __future__ import annotations

import pytest

from wasm.core.net import (
    ALL_INTERFACES,
    is_loopback_host,
    local_address,
    loopback_access_lines,
    normalize_host,
    server_address,
    ssh_target,
)

# ---------------------------------------------------------------------------
# Classifying an address
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("host", ["127.0.0.1", "127.0.0.53", "::1", "[::1]", "localhost"])
def test_loopback_spellings_are_recognised(host: str) -> None:
    """
    Args:
        host: A spelling only this machine could reach.
    """
    assert is_loopback_host(host) is True


@pytest.mark.parametrize("host", ["", "*", "0.0.0.0", "::", "10.0.0.4", "example.com"])  # noqa: S104
def test_reachable_spellings_are_not_loopback(host: str) -> None:
    """
    The empty string is the one that mattered: a set of loopback *strings* with
    "" in it let ``--host ""`` bind a root panel to every interface.

    Args:
        host: A spelling something other than this machine could reach.
    """
    assert is_loopback_host(host) is False


def test_an_unresolvable_name_is_treated_as_exposed() -> None:
    """Guessing in the other direction publishes a root shell."""
    assert is_loopback_host("no-such-host.invalid") is False


@pytest.mark.parametrize("host", ["", "*"])
def test_wildcard_spellings_normalise_to_the_address_they_bind(host: str) -> None:
    """
    Args:
        host: A spelling of "every interface" no resolver accepts.
    """
    assert normalize_host(host) == ALL_INTERFACES


def test_a_name_is_left_for_the_resolver() -> None:
    """Binding a name is the resolver's business, not this module's."""
    assert normalize_host("panel.example.com") == "panel.example.com"


# ---------------------------------------------------------------------------
# Telling an operator how to reach a loopback panel
# ---------------------------------------------------------------------------


def test_a_reachable_address_needs_no_forwarding_instructions() -> None:
    """Nothing is explained when the browser can already open the address."""
    assert loopback_access_lines(ALL_INTERFACES, 8080) == ()


def test_a_loopback_panel_is_explained_with_an_ssh_tunnel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    The reported failure: a panel started over SSH on a headless VPS printed
    ``Server: http://127.0.0.1:8081`` and nothing that could open it.

    Args:
        monkeypatch: Patching helper, scoped to the test.
    """
    monkeypatch.setenv("SSH_CONNECTION", "203.0.113.9 51000 198.51.100.7 22")
    monkeypatch.setattr("wasm.core.net._current_user", lambda: "root")

    lines = loopback_access_lines("127.0.0.1", 8081)

    assert "ssh -L 8081:127.0.0.1:8081 root@198.51.100.7" in "\n".join(lines)
    assert "http://localhost:8081" in "\n".join(lines)


def test_the_forwarded_url_keeps_the_scheme_the_panel_serves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Args:
        monkeypatch: Patching helper, scoped to the test.
    """
    monkeypatch.setattr("wasm.core.net.server_address", lambda: "198.51.100.7")
    monkeypatch.setattr("wasm.core.net._current_user", lambda: "root")

    lines = loopback_access_lines("127.0.0.1", 8443, scheme="https")

    assert "https://localhost:8443" in "\n".join(lines)


def test_an_ipv6_loopback_is_forwarded_with_brackets(monkeypatch: pytest.MonkeyPatch) -> None:
    """``ssh -L`` needs the literal bracketed or it reads the colons as fields.

    Args:
        monkeypatch: Patching helper, scoped to the test.
    """
    monkeypatch.setattr("wasm.core.net.server_address", lambda: "198.51.100.7")
    monkeypatch.setattr("wasm.core.net._current_user", lambda: "root")

    lines = loopback_access_lines("::1", 8080)

    assert "ssh -L 8080:[::1]:8080 root@198.51.100.7" in "\n".join(lines)


# ---------------------------------------------------------------------------
# Naming the server the operator would tunnel to
# ---------------------------------------------------------------------------


def test_the_address_the_client_connected_to_is_preferred(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    SSH_CONNECTION holds the address this session actually arrived on, which
    beats guessing at an outbound interface behind NAT.

    Args:
        monkeypatch: Patching helper, scoped to the test.
    """
    monkeypatch.setenv("SSH_CONNECTION", "203.0.113.9 51000 198.51.100.7 22")

    assert server_address() == "198.51.100.7"


def test_a_malformed_ssh_connection_falls_back_to_the_local_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Args:
        monkeypatch: Patching helper, scoped to the test.
    """
    monkeypatch.setenv("SSH_CONNECTION", "nonsense")
    monkeypatch.setattr("wasm.core.net.local_address", lambda: "10.0.0.4")

    assert server_address() == "10.0.0.4"


def test_a_console_session_falls_back_to_the_local_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Args:
        monkeypatch: Patching helper, scoped to the test.
    """
    monkeypatch.delenv("SSH_CONNECTION", raising=False)
    monkeypatch.setattr("wasm.core.net.local_address", lambda: "10.0.0.4")

    assert server_address() == "10.0.0.4"


def test_the_local_address_answers_even_with_no_route(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    A banner is not worth an exception; the placeholder is still readable.

    Args:
        monkeypatch: Patching helper, scoped to the test.
    """

    def no_route(*args: object, **kwargs: object) -> None:
        raise OSError("Network is unreachable")

    monkeypatch.setattr("socket.socket", no_route)

    assert local_address() == "127.0.0.1"


def test_an_unknown_user_still_produces_a_usable_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A container with no passwd entry raises from getuser; the hint survives it.

    Args:
        monkeypatch: Patching helper, scoped to the test.
    """

    def no_passwd_entry() -> str:
        raise KeyError("getpwuid(): uid not found")

    monkeypatch.setattr("getpass.getuser", no_passwd_entry)
    monkeypatch.setattr("wasm.core.net.server_address", lambda: "198.51.100.7")

    assert ssh_target() == "user@198.51.100.7"
