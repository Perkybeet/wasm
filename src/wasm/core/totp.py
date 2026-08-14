# Copyright (c) 2024-2026 Yago Lopez Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
Time-based one-time passwords, RFC 6238, with nothing but the standard library.

The panel's second factor is ~60 lines of ``hmac`` + ``struct`` + ``base64``,
which is the whole algorithm. A dependency here would have to be declared in
four packaging files and exist on every target distribution, and ``pyotp`` is
not packaged everywhere WASM ships; the RFC is shorter than that negotiation.

SHA-1 is what the RFC specifies and what every authenticator app implements.
Its collision weakness is irrelevant to HMAC truncated to six digits, so this
is not the place to be original.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
import time
from urllib.parse import quote

#: 160 bits of secret, the length RFC 4226 recommends for HMAC-SHA1. It also
#: encodes to exactly 32 base32 characters, so the secret never needs padding.
SECRET_BYTES = 20

#: The time step and code length every authenticator app defaults to.
PERIOD = 30
DIGITS = 6


def generate_secret() -> str:
    """
    Generate a shared secret for enrolment.

    Returns:
        The secret, base32-encoded without padding, ready to be typed into an
        authenticator app or embedded in a provisioning URI.
    """
    return base64.b32encode(secrets.token_bytes(SECRET_BYTES)).decode("ascii").rstrip("=")


def _decode_secret(secret_b32: str) -> bytes:
    """
    Decode a base32 secret, tolerating missing padding and stray spaces.

    Authenticator apps display secrets in spaced groups and without padding,
    and an operator typing one back should not be failed over either.

    Args:
        secret_b32: The base32-encoded secret.

    Returns:
        The raw key bytes.

    Raises:
        binascii.Error: When the value is not base32 at all. The secret is
            server-generated, so this is a corrupt state file, not user input.
    """
    compact = secret_b32.strip().replace(" ", "")
    padded = compact + "=" * (-len(compact) % 8)
    return base64.b32decode(padded, casefold=True)


def _hotp(secret_b32: str, counter: int, digits: int = DIGITS) -> str:
    """
    Compute one HOTP value, RFC 4226 section 5.

    Args:
        secret_b32: The base32-encoded shared secret.
        counter: The moving factor.
        digits: Length of the code.

    Returns:
        The code, zero-padded to ``digits``.
    """
    key = _decode_secret(secret_b32)
    mac = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = mac[-1] & 0x0F
    code = (int.from_bytes(mac[offset : offset + 4], "big") & 0x7FFFFFFF) % 10**digits
    return str(code).zfill(digits)


def totp_now(secret_b32: str, *, t: float | None = None, digits: int = DIGITS) -> str:
    """
    Compute the TOTP value for a moment in time.

    Args:
        secret_b32: The base32-encoded shared secret.
        t: UNIX timestamp to compute for; the current time when omitted.
        digits: Length of the code.

    Returns:
        The code an authenticator app would show at that moment.
    """
    moment = time.time() if t is None else t
    return _hotp(secret_b32, int(moment // PERIOD), digits)


def verify(secret_b32: str, code: str, *, window: int = 1, t: float | None = None) -> bool:
    """
    Check a code against the secret, allowing for clock drift.

    Every candidate in the window is compared in constant time, and all of
    them are computed whether or not an earlier one already matched, so the
    comparison leaks nothing about which step a code belongs to.

    Args:
        secret_b32: The base32-encoded shared secret.
        code: The code the client typed.
        window: Steps of drift tolerated on either side. The default accepts
            the previous and the next 30-second step, which is what a phone a
            few seconds off needs and no more.
        t: UNIX timestamp to verify against; the current time when omitted.

    Returns:
        True when the code is valid for some step inside the window.
    """
    candidate = code.strip()
    if not candidate.isdigit() or len(candidate) != DIGITS or not secret_b32:
        return False

    now = int((time.time() if t is None else t) // PERIOD)
    matched = False
    for offset in range(-window, window + 1):
        matched |= hmac.compare_digest(_hotp(secret_b32, now + offset), candidate)
    return matched


def provisioning_uri(secret_b32: str, *, issuer: str = "WASM", account: str = "admin") -> str:
    """
    Build the ``otpauth://`` URI an authenticator app enrols from.

    Args:
        secret_b32: The base32-encoded shared secret.
        issuer: Name the app files the account under.
        account: Name of the account itself; the hostname reads best for a
            panel, so an operator with several servers can tell them apart.

    Returns:
        The URI, with every component percent-encoded, suitable for a QR code
        or for pasting into an app by hand.
    """
    label = quote(f"{issuer}:{account}", safe="")
    return (
        f"otpauth://totp/{label}"
        f"?secret={quote(secret_b32, safe='')}"
        f"&issuer={quote(issuer, safe='')}"
        f"&algorithm=SHA1&digits={DIGITS}&period={PERIOD}"
    )
