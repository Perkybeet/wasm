# Copyright (c) 2024-2026 Yago Lopez Prado
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Tests for diagnosing a failed certificate order or renewal.

The owner's report: a certbot challenge failure against a domain whose AAAA
record pointed at another host printed only certbot's own, indirect words
("Fetching ...: Connection refused"). An operator without a matching mental
model of "Let's Encrypt tries IPv6 first" has no way to connect that to their
DNS. This turns it into the actual, actionable cause - reusing
:func:`wasm.deployers.domains.check_dns`, the one implementation of "does this
name resolve to this machine", rather than a second resolver - while keeping
certbot's own output verbatim in ``output``.

A rate limit or a missing plugin is a certbot failure too, and none of them
has anything to do with DNS: :func:`test_a_non_challenge_failure_is_not_given_a_dns_cause`
pins that the lookup is skipped entirely unless the failure looks like a
challenge or authorization problem.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import wasm.deployers.domains as domains_module
from wasm.core.exceptions import CertificateError
from wasm.core.runner import FakeRunner
from wasm.deployers.domains import DnsCheck
from wasm.managers import cert_manager as cert_manager_module
from wasm.managers.cert_manager import CertManager

#: A realistic certbot authorization failure, HTTP-01 over the wrong address.
_CHALLENGE_FAILURE = (
    "Certbot failed to authenticate some domains (authenticator: standalone). "
    "The Certificate Authority reported these problems:\n"
    "  Domain: arenna38.com\n"
    "  Type:   connection\n"
    "  Detail: Fetching http://arenna38.com/.well-known/acme-challenge/xyz: "
    "Connection refused connecting to 2001:db8::9999, addressUsed: 2001:db8::9999\n"
)


class _Store:
    """A store that records nothing, so issuance never touches SQLite."""

    def update_site_ssl(self, **_kwargs: Any) -> None:
        """Accept and discard a TLS state update."""

    def get_app(self, _domain: str) -> None:
        """No deployed application in these tests."""
        return None

    def list_domains(self, _domain: str) -> list[Any]:
        """No deployed application in these tests."""
        return []


@pytest.fixture
def certs(runner: FakeRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> CertManager:
    """A certificate manager whose letsencrypt tree and store are disposable."""
    monkeypatch.setattr("wasm.managers.cert_manager.get_store", lambda: _Store())
    manager = CertManager()
    manager.LETSENCRYPT_DIR = tmp_path / "letsencrypt"
    manager.LIVE_DIR = manager.LETSENCRYPT_DIR / "live"
    manager.config = SimpleNamespace(ssl_email="")  # type: ignore[assignment]
    return manager


def _fake_check_dns(check: DnsCheck) -> Any:
    """Build a ``check_dns`` replacement that always answers with ``check``."""
    return lambda domain, **_kwargs: check


def test_a_wrong_aaaa_record_is_named_as_the_cause(
    certs: CertManager, runner: FakeRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The exact wording the owner asked for, for the exact scenario reported."""
    runner.script(
        ["certbot", "certonly"],
        stderr=_CHALLENGE_FAILURE,
        exit_code=1,
    )
    monkeypatch.setattr(
        domains_module,
        "check_dns",
        _fake_check_dns(
            DnsCheck(
                domain="arenna38.com",
                expected_addresses=("203.0.113.10",),
                resolved_addresses=("2001:db8::9999",),
                points_here=False,
            )
        ),
    )

    with pytest.raises(CertificateError) as raised:
        certs.obtain("arenna38.com", standalone=True)

    error = raised.value
    assert error.message == (
        "arenna38.com has an IPv6 (AAAA) record, 2001:db8::9999, that is not this machine. "
        "Let's Encrypt connects over IPv6 first, so the challenge reached another server."
    )
    assert error.details == (
        "Remove or correct the AAAA record for arenna38.com; this machine answers on 203.0.113.10."
    )
    # certbot's own output is never lost, even once it has been diagnosed.
    assert "Connection refused" in (error.output or "")


def test_a_wrong_a_record_is_named_as_the_cause(
    certs: CertManager, runner: FakeRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same diagnosis, for IPv4-only DNS pointing at the wrong host."""
    runner.script(
        ["certbot", "certonly"],
        stderr=(
            "The Certificate Authority reported these problems:\n"
            "  Domain: shop.example.com\n"
            "  Detail: Fetching http://shop.example.com/.well-known/acme-challenge/xyz: "
            "Connection refused connecting to 198.51.100.9\n"
        ),
        exit_code=1,
    )
    monkeypatch.setattr(
        domains_module,
        "check_dns",
        _fake_check_dns(
            DnsCheck(
                domain="shop.example.com",
                expected_addresses=("203.0.113.10",),
                resolved_addresses=("198.51.100.9",),
                points_here=False,
            )
        ),
    )

    with pytest.raises(CertificateError) as raised:
        certs.obtain("shop.example.com", standalone=True)

    error = raised.value
    assert error.message == (
        "shop.example.com has an IPv4 (A) record, 198.51.100.9, that is not this machine."
    )
    assert error.details == (
        "Remove or correct the A record for shop.example.com; this machine answers on 203.0.113.10."
    )


def test_no_dns_record_at_all_is_its_own_message(
    certs: CertManager, runner: FakeRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A domain that does not resolve at all gets a message that says so, not a false A/AAAA claim."""
    runner.script(
        ["certbot", "certonly"],
        stderr="Detail: DNS problem: NXDOMAIN looking up A for missing.example.com\n",
        exit_code=1,
    )
    monkeypatch.setattr(
        domains_module,
        "check_dns",
        _fake_check_dns(
            DnsCheck(
                domain="missing.example.com",
                expected_addresses=("203.0.113.10",),
                resolved_addresses=(),
                points_here=False,
            )
        ),
    )

    with pytest.raises(CertificateError) as raised:
        certs.obtain("missing.example.com", standalone=True)

    assert raised.value.message == "missing.example.com has no DNS record pointing at this machine"


def test_a_domain_pointing_here_is_not_blamed(
    certs: CertManager, runner: FakeRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When DNS is fine, the failure keeps its generic message; something else caused it."""
    runner.script(
        ["certbot", "certonly"],
        stderr="Detail: some other ACME problem entirely\n",
        exit_code=1,
    )
    monkeypatch.setattr(
        domains_module,
        "check_dns",
        _fake_check_dns(
            DnsCheck(
                domain="fine.example.com",
                expected_addresses=("203.0.113.10",),
                resolved_addresses=("203.0.113.10",),
                points_here=True,
            )
        ),
    )

    with pytest.raises(CertificateError) as raised:
        certs.obtain("fine.example.com", standalone=True)

    assert raised.value.message == "Failed to obtain certificate for fine.example.com"
    assert "some other ACME problem entirely" in (raised.value.details or "")


def test_a_non_challenge_failure_is_not_given_a_dns_cause(
    certs: CertManager, runner: FakeRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A rate limit is a certbot failure too, and has nothing to do with DNS."""
    called = False

    def _fail_if_called(*_args: Any, **_kwargs: Any) -> DnsCheck:
        nonlocal called
        called = True
        raise AssertionError("DNS must not be checked for a non-challenge failure")

    runner.script(
        ["certbot", "certonly"],
        stderr="too many certificates already issued for this exact set of domains",
        exit_code=1,
    )
    monkeypatch.setattr(domains_module, "check_dns", _fail_if_called)

    with pytest.raises(CertificateError) as raised:
        certs.obtain("example.com", standalone=True)

    assert called is False
    assert "too many certificates" in (raised.value.details or "")


def test_renewal_of_everything_is_never_given_a_single_domains_dns_cause(
    certs: CertManager, runner: FakeRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Renewing every due certificate at once has no single domain to blame."""

    def _fail_if_called(*_args: Any, **_kwargs: Any) -> DnsCheck:
        raise AssertionError("no single domain to diagnose when renewing everything")

    runner.script(["certbot", "renew"], stderr=_CHALLENGE_FAILURE, exit_code=1)
    monkeypatch.setattr(domains_module, "check_dns", _fail_if_called)

    with pytest.raises(CertificateError) as raised:
        certs.renew()

    assert raised.value.message == "Certificate renewal failed"


def test_renewal_of_one_domain_is_diagnosed_the_same_way(
    certs: CertManager, runner: FakeRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``wasm cert renew -d <domain>`` gets the same treatment as issuance."""
    runner.script(["certbot", "renew"], stderr=_CHALLENGE_FAILURE, exit_code=1)
    monkeypatch.setattr(
        domains_module,
        "check_dns",
        _fake_check_dns(
            DnsCheck(
                domain="arenna38.com",
                expected_addresses=("203.0.113.10",),
                resolved_addresses=("2001:db8::9999",),
                points_here=False,
            )
        ),
    )

    with pytest.raises(CertificateError) as raised:
        certs.renew("arenna38.com")

    assert "IPv6 (AAAA) record" in raised.value.message


def test_the_letsencrypt_log_fills_in_a_detail_stdout_lacks(
    certs: CertManager, runner: FakeRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Read-only, and tolerant of the log rotating in mid-line: only the tail matters."""
    log_path = certs.LETSENCRYPT_DIR.parent / "letsencrypt.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(
        "2026-01-01 00:00:00,000:DEBUG:certbot._internal.log:noise\n"
        "2026-01-01 00:00:01,000:DEBUG:acme.client:Storing nonce\n"
        "2026-01-01 00:00:02,000:INFO:certbot._internal.reporter:"
        "Detail: Fetching http://arenna38.com/.well-known/acme-challenge/xyz: "
        "Connection refused connecting to 2001:db8::9999, addressUsed: 2001:db8::9999\n"
    )
    monkeypatch.setattr(cert_manager_module, "LETSENCRYPT_LOG", log_path)
    # certbot's own stdout/stderr, deliberately thin: it mentions the fetch but
    # carries neither "Detail:" nor "addressUsed" itself.
    runner.script(
        ["certbot", "certonly"],
        stderr="Fetching http://arenna38.com/.well-known/acme-challenge/xyz timed out\n",
        exit_code=1,
    )
    monkeypatch.setattr(
        domains_module,
        "check_dns",
        _fake_check_dns(
            DnsCheck(
                domain="arenna38.com",
                expected_addresses=("203.0.113.10",),
                resolved_addresses=("203.0.113.10",),
                points_here=True,
            )
        ),
    )

    with pytest.raises(CertificateError) as raised:
        certs.obtain("arenna38.com", standalone=True)

    assert "Fetching http://arenna38.com/.well-known/acme-challenge/xyz timed out" in (
        raised.value.output or ""
    )
    assert "Detail: Fetching" in (raised.value.output or "")
    assert "addressUsed: 2001:db8::9999" in (raised.value.output or "")


def test_the_letsencrypt_log_is_tolerated_when_absent(
    certs: CertManager, runner: FakeRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """No log at all - a fresh install, or a distribution that logs elsewhere - is not an error."""
    monkeypatch.setattr(cert_manager_module, "LETSENCRYPT_LOG", tmp_path / "does-not-exist.log")
    runner.script(
        ["certbot", "certonly"],
        stderr="Fetching http://example.com/.well-known/acme-challenge/xyz timed out\n",
        exit_code=1,
    )
    monkeypatch.setattr(
        domains_module,
        "check_dns",
        _fake_check_dns(
            DnsCheck(
                domain="example.com",
                expected_addresses=("203.0.113.10",),
                resolved_addresses=("203.0.113.10",),
                points_here=True,
            )
        ),
    )

    with pytest.raises(CertificateError) as raised:
        certs.obtain("example.com", standalone=True)

    assert (
        raised.value.output
        == "Fetching http://example.com/.well-known/acme-challenge/xyz timed out"
    )
