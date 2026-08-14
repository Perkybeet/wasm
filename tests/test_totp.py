# Copyright (c) 2024-2026 Yago Lopez Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
Tests for the stdlib TOTP implementation.

The ground truth is RFC 6238 Appendix B: the SHA-1 test vectors are computed
from the ASCII secret ``12345678901234567890``, so a passing suite here means
the codes agree with every authenticator app on earth, not with ourselves.
"""

from __future__ import annotations

import base64
from urllib.parse import parse_qs, unquote, urlsplit

import pytest

from wasm.core import totp

#: The RFC 6238 test secret, ASCII "12345678901234567890", as base32.
RFC_SECRET = base64.b32encode(b"12345678901234567890").decode("ascii")

#: Appendix B of RFC 6238, the SHA-1 rows: time, 8-digit TOTP.
RFC_VECTORS = (
    (59, "94287082"),
    (1111111109, "07081804"),
    (1111111111, "14050471"),
    (1234567890, "89005924"),
    (2000000000, "69279037"),
    (20000000000, "65353130"),
)


@pytest.mark.parametrize(("moment", "expected"), RFC_VECTORS)
def test_rfc_6238_appendix_b_vectors(moment: int, expected: str) -> None:
    """
    The eight-digit codes must match the RFC's own table exactly.

    Args:
        moment: UNIX time of the vector.
        expected: The code the RFC says an implementation must produce.
    """
    assert totp.totp_now(RFC_SECRET, t=moment, digits=8) == expected


def test_the_six_digit_code_is_the_truncation_the_rfc_describes() -> None:
    """Six digits are the last six of the eight-digit dynamic truncation."""
    assert totp.totp_now(RFC_SECRET, t=59) == "287082"
    assert totp.verify(RFC_SECRET, "287082", t=59)


def test_a_secret_without_padding_and_with_spaces_still_verifies() -> None:
    """Apps display secrets in spaced groups and drop the padding."""
    spaced = " ".join([RFC_SECRET[i : i + 4] for i in range(0, len(RFC_SECRET), 4)])
    assert totp.totp_now(spaced.rstrip("=").lower(), t=59) == "287082"


def test_generated_secrets_are_base32_160_bits_and_unpadded() -> None:
    """20 bytes encode to exactly 32 base32 characters, never padded."""
    secret = totp.generate_secret()

    assert len(secret) == 32
    assert "=" not in secret
    assert len(base64.b32decode(secret)) == 20
    assert secret != totp.generate_secret(), "two secrets in a row came out identical"


def test_the_window_tolerates_one_step_of_drift_and_no_more() -> None:
    """A phone thirty seconds off still works; a code a minute old does not."""
    now = 1_700_000_000
    previous_step = totp.totp_now(RFC_SECRET, t=now - 30)
    two_steps_back = totp.totp_now(RFC_SECRET, t=now - 60)

    assert totp.verify(RFC_SECRET, previous_step, t=now)
    assert not totp.verify(RFC_SECRET, two_steps_back, t=now, window=1)
    assert totp.verify(RFC_SECRET, two_steps_back, t=now, window=2)


@pytest.mark.parametrize(
    "wrong",
    [
        "000000",
        "12345",
        "1234567",
        "28708a",
        "",
        "      ",
        "287082 or 1=1",
    ],
)
def test_codes_that_are_wrong_or_malformed_fail(wrong: str) -> None:
    """
    Anything that is not the six digits of the moment is refused.

    Args:
        wrong: A code that must not verify at t=59.
    """
    if wrong.strip() == totp.totp_now(RFC_SECRET, t=59):
        pytest.skip("collided with the real code")
    assert not totp.verify(RFC_SECRET, wrong, t=59)


def test_an_empty_secret_never_verifies_anything() -> None:
    """A missing secret must fail closed, not open."""
    assert not totp.verify("", "287082", t=59)


def test_the_code_may_arrive_with_surrounding_whitespace() -> None:
    """A copy-pasted code often carries a stray space."""
    assert totp.verify(RFC_SECRET, " 287082 ", t=59)


def test_provisioning_uri_is_well_formed_and_fully_encoded() -> None:
    """The URI is what the QR carries; a bad one fails silently in the app."""
    secret = totp.generate_secret()
    uri = totp.provisioning_uri(secret, issuer="WASM Panel", account="root@web-01")

    split = urlsplit(uri)
    assert split.scheme == "otpauth"
    assert split.netloc == "totp"
    assert unquote(split.path.lstrip("/")) == "WASM Panel:root@web-01"
    # The raw label must not carry characters that end the URI component.
    assert " " not in uri
    assert "%20" in uri

    params = parse_qs(split.query)
    assert params["secret"] == [secret]
    assert params["issuer"] == ["WASM Panel"]
    assert params["algorithm"] == ["SHA1"]
    assert params["digits"] == ["6"]
    assert params["period"] == ["30"]


def test_provisioning_uri_defaults_name_the_product() -> None:
    """The defaults must produce a scannable URI without any arguments."""
    uri = totp.provisioning_uri(RFC_SECRET)

    assert uri.startswith("otpauth://totp/WASM%3Aadmin?secret=")
    assert "issuer=WASM" in uri
