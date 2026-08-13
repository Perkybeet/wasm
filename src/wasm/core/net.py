# Copyright (c) 2024-2026 Yago Lopez Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
How a listening address is classified, and how an operator reaches it.

One question is asked of this module twice. The CLI asks whether an address is
reachable from another machine, because that decides whether it will serve a
root panel on it at all. The startup banner asks the same thing, because an
address only this machine can open has to come with instructions for opening it
from somewhere else. Two callers, one classification: a security decision that
is made in two places is a security decision that drifts, and this one is
subtle enough that the second copy would have been the wrong one - "", ``*``,
``0``, ``0.0.0.0``, ``::`` and a name resolving to any of them are six
spellings of the same socket.

The instructions matter as much as the classification. A panel started over SSH
on a server with no desktop printed ``Server: http://127.0.0.1:8080`` and
stopped there, which is an address that, from the machine the operator is
actually sitting at, does not exist.
"""

from __future__ import annotations

import getpass
import ipaddress
import os
import socket

#: The address a socket is given when it should answer on every interface. It
#: is named here to be recognised and refused, never bound by default.
ALL_INTERFACES = "0.0.0.0"  # noqa: S104

#: Spellings of "every interface" that no resolver accepts, so they cannot be
#: classified by looking them up. The empty string is the one that mattered: a
#: version of this check kept a set of loopback *strings* with "" in it, and
#: ``wasm web start --host ""`` walked past the refusal and bound the root
#: panel to every interface in cleartext.
WILDCARD_SPELLINGS = frozenset({"", "*"})

#: Shown in place of a login name when the passwd database has no entry for
#: this uid, which happens in containers. The command stays copy-editable.
UNKNOWN_USER = "user"

#: Address family alias, for the helpers below.
IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address


def strip_brackets(host: str) -> str:
    """
    Remove the brackets an IPv6 literal is often written with.

    Args:
        host: The spelling the operator typed.

    Returns:
        The spelling without a surrounding ``[]`` pair.
    """
    candidate = host.strip()
    if len(candidate) > 1 and candidate.startswith("[") and candidate.endswith("]"):
        return candidate[1:-1]
    return candidate


def host_addresses(host: str) -> tuple[IPAddress, ...]:
    """
    Return every address a host spelling would end up bound to.

    Comparing host strings is what made this decision wrong: "", ``*``, ``0``,
    ``0.0.0.0``, ``::`` and a name in ``/etc/hosts`` pointing at 0.0.0.0 are six
    spellings of the same socket, and a set of known-good strings misses five of
    them. The resolver is asked instead, and its answer is classified with
    :mod:`ipaddress`.

    Args:
        host: The spelling the operator typed.

    Returns:
        The addresses, empty when the spelling cannot be resolved. Empty means
        "unknown", and every caller treats unknown as exposed.
    """
    candidate = strip_brackets(host)
    if candidate in WILDCARD_SPELLINGS:
        return (ipaddress.ip_address(ALL_INTERFACES),)

    try:
        return (ipaddress.ip_address(candidate),)
    except ValueError:
        pass

    try:
        # A resolution covers names and also the inet_aton spellings ipaddress
        # refuses but a socket accepts, such as "0" (INADDR_ANY) and "127.1".
        infos = socket.getaddrinfo(candidate, None, type=socket.SOCK_STREAM)
    except (OSError, UnicodeError, ValueError):
        return ()

    addresses = []
    for info in infos:
        # A link-local sockaddr carries a %scope suffix that ipaddress refuses.
        text = str(info[4][0]).split("%", 1)[0]
        try:
            addresses.append(ipaddress.ip_address(text))
        except ValueError:
            return ()
    return tuple(addresses)


def is_loopback_host(host: str) -> bool:
    """
    Report whether only this machine could reach a service bound to a host.

    Args:
        host: The spelling the operator typed.

    Returns:
        True only when the spelling is known to resolve to loopback addresses
        and nothing else. A spelling that cannot be resolved is not loopback,
        because guessing in the other direction publishes a root shell.
    """
    addresses = host_addresses(host)
    return bool(addresses) and all(address.is_loopback for address in addresses)


def normalize_host(host: str) -> str:
    """
    Canonicalise a host spelling so what is checked is what is reported.

    Args:
        host: The spelling the operator typed.

    Returns:
        The canonical spelling. Every way of writing "every interface" becomes
        the address it binds, so a refusal names a real address instead of
        quoting an empty string back at the operator.
    """
    candidate = strip_brackets(host)
    addresses = host_addresses(candidate)
    if not addresses:
        return candidate
    if candidate in WILDCARD_SPELLINGS or all(address.is_unspecified for address in addresses):
        return str(addresses[0])
    try:
        return str(ipaddress.ip_address(candidate))
    except ValueError:
        # A name: binding it is the resolver's business, not this module's.
        return candidate


def local_address() -> str:
    """
    Best-effort address of this machine, for a banner.

    Returns:
        The outbound interface address, or 127.0.0.1 when it cannot be found.
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            # A UDP connect sends nothing; it only asks the routing table which
            # source address a packet to that destination would leave from.
            sock.connect(("8.8.8.8", 80))
            return str(sock.getsockname()[0])
    except OSError:
        return "127.0.0.1"


def server_address() -> str:
    """
    Name this server the way the operator's own machine would reach it.

    ``SSH_CONNECTION`` is preferred over the outbound interface because it holds
    the address this very session arrived on. Behind NAT, on a host with several
    interfaces, or on a VPS with a separate private network, that is the address
    that works and the outbound one is a guess.

    Returns:
        An address or a placeholder; never raises, because a banner is not worth
        an exception.
    """
    parts = os.environ.get("SSH_CONNECTION", "").split()
    if len(parts) >= 4:
        return parts[2]
    return local_address()


def _current_user() -> str:
    """
    Return the login name to put in an SSH command.

    Returns:
        The current user, or a placeholder when there is no passwd entry.
    """
    try:
        return getpass.getuser()
    except (KeyError, OSError):
        return UNKNOWN_USER


def ssh_target() -> str:
    """
    Build the ``user@host`` an operator would SSH to.

    Returns:
        The target, ready to paste into an ssh command line.
    """
    return f"{_current_user()}@{server_address()}"


def loopback_access_lines(host: str, port: int, scheme: str = "http") -> tuple[str, ...]:
    """
    Explain how to open a service that only answers on this machine.

    Args:
        host: The address the service is bound to.
        port: The port it listens on.
        scheme: ``http`` or ``https``, matching what it serves.

    Returns:
        Lines for a banner, empty when the address is reachable from another
        machine and needs no forwarding. Callers print them as they are.
    """
    if not is_loopback_host(host):
        return ()

    literal = normalize_host(host)
    # ssh -L splits its argument on colons, so an IPv6 literal has to be
    # bracketed or the address is read as a port and a host.
    forwarded = f"[{literal}]" if ":" in literal else literal

    return (
        "This address answers on the server itself and nowhere else.",
        "To open it from your own machine, forward the port over SSH:",
        f"  ssh -L {port}:{forwarded}:{port} {ssh_target()}",
        f"then browse to {scheme}://localhost:{port} there.",
    )


__all__ = [
    "ALL_INTERFACES",
    "UNKNOWN_USER",
    "WILDCARD_SPELLINGS",
    "IPAddress",
    "host_addresses",
    "is_loopback_host",
    "local_address",
    "loopback_access_lines",
    "normalize_host",
    "server_address",
    "ssh_target",
    "strip_brackets",
]
