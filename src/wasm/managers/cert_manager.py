# Copyright (c) 2024-2026 Yago Lopez Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
SSL certificates, driven through certbot.

Certificate data crosses two module boundaries: ``certbot certificates`` is
parsed here and read by the CLI and by ``wasm health``. It travels as a
:class:`CertificateInfo`, so the field names are stated once instead of being
guessed at each end - ``wasm health`` spent several releases looking for an
``expires`` key that this module never wrote, and a plain dict answered that
with ``None`` forever. Asking one of these records for a field it does not have
is now a :class:`KeyError`, not a silent miss.

The other thing this module has to get right is idempotence. Issuance is a rate
limited network operation against Let's Encrypt, so "run it again" must be
cheap and safe:

- A certificate that already covers every requested domain is left alone.
- A failure to *read* the current state is an error, never a licence to issue.
  ``certbot certificates`` returning non-zero used to be parsed as "there is no
  certificate", so every run of a broken-but-installed certbot placed a fresh
  order for a domain that was already served.
- One that covers some of them is expanded, never issued a second time under a
  new lineage name. That is why every command passes ``--cert-name``: without
  it certbot invents ``example.com-0001`` as soon as the domain set changes, and
  the renewal timer then keeps two half-right certificates alive.
- ``www`` is handled here rather than at each call site, because the rule for
  when a bare domain deserves a ``www`` alias is a property of the domain.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, overload

from wasm.core.exceptions import CertificateError, WASMError
from wasm.core.fs import FileSystem
from wasm.core.runner import DEFAULT_TIMEOUT, CommandResult, CommandRunner
from wasm.core.store import WASMStore, get_store
from wasm.managers.base_manager import BaseManager, MappingRecord
from wasm.validators.domain import is_valid_domain, should_include_www

#: Issuing or renewing a certificate involves ACME round trips.
_ISSUE_TIMEOUT = 300
_RENEW_TIMEOUT = 600

#: Where certbot keeps the ACME challenge files when no plugin is available.
DEFAULT_WEBROOT = Path("/var/www/html")

#: Cron fallback for systems whose certbot package ships no systemd timer.
_CRON_FILE = Path("/etc/cron.d/certbot-renew")
_CRON_LINE = "0 0,12 * * * root certbot renew -q\n"

#: cron refuses to run a file in ``/etc/cron.d`` that is group or world
#: writable, and the line holds no secret, so it is the usual 0644.
_CRON_MODE = 0o644

#: Fields of :class:`CertificateInfo` that hold text, used to type the mapping
#: accessor the CLI and the health check still read certificates through.
_TextField = Literal["name", "expiry", "expiry_full", "cert_path", "key_path"]


@dataclass
class CertificateInfo(MappingRecord):
    """
    One entry of ``certbot certificates``.

    Attributes:
        name: Certificate (lineage) name.
        domains: Every domain the certificate covers.
        expiry: Expiry date as ``YYYY-MM-DD``, or None when certbot's output
            could not be parsed.
        expiry_full: The raw expiry line, including certbot's validity note.
        cert_path: Path to the certificate file.
        key_path: Path to the private key.
    """

    name: str = ""
    domains: list[str] = field(default_factory=list)
    expiry: str | None = None
    expiry_full: str = ""
    cert_path: str = ""
    key_path: str = ""

    # The overloads exist so that a reader still using the mapping form keeps a
    # real static type instead of Any. Reading ``cert.expiry`` is the intended
    # way; these keep the transition from costing type safety at the call sites
    # that have not moved yet.
    @overload
    def get(self, key: Literal["domains"], default: list[str] | None = ...) -> list[str]: ...

    @overload
    def get(self, key: _TextField, default: str) -> str: ...

    @overload
    def get(self, key: _TextField, default: None = ...) -> str | None: ...

    @overload
    def get(self, key: str, default: Any = ...) -> Any: ...

    def get(self, key: str, default: Any = None) -> Any:
        """
        Read a field, falling back to a default when it is unset.

        Args:
            key: Field name.
            default: Returned when the field is unset.

        Returns:
            The field value, or the default.

        Raises:
            KeyError: When the key is not a field of this record.
        """
        return super().get(key, default)


@dataclass
class CertificatePaths(MappingRecord):
    """
    The four files a Let's Encrypt lineage publishes.

    Attributes:
        fullchain: Certificate plus intermediates, what a web server serves.
        privkey: Private key.
        cert: Leaf certificate on its own.
        chain: Intermediates on their own.
    """

    fullchain: Path
    privkey: Path
    cert: Path
    chain: Path


@dataclass
class CertificateTest(MappingRecord):
    """
    Result of inspecting a certificate file with openssl.

    Attributes:
        valid: Whether the file could be read and parsed.
        error: Why it could not, when it could not.
        not_before: Start of the validity window, as openssl prints it.
        not_after: End of the validity window, as openssl prints it.
        path: File that was inspected.
    """

    valid: bool
    error: str | None = None
    not_before: str | None = None
    not_after: str | None = None
    path: str | None = None


class CertManager(BaseManager):
    """
    Manager for SSL certificates using Certbot.

    Handles obtaining, renewing and revoking Let's Encrypt certificates.
    """

    LETSENCRYPT_DIR = Path("/etc/letsencrypt")
    LIVE_DIR = LETSENCRYPT_DIR / "live"

    def __init__(
        self,
        verbose: bool = False,
        runner: CommandRunner | None = None,
        fs: FileSystem | None = None,
    ) -> None:
        """
        Initialize the certificate manager.

        Args:
            verbose: Enable verbose logging.
            runner: Command runner to execute certbot with. Defaults to the
                process-wide runner.
            fs: Filesystem to write the renewal cron entry through. Defaults to
                the process-wide one.
        """
        super().__init__(verbose=verbose, runner=runner, fs=fs)
        self._plugin_available: dict[str, bool] = {}

    @property
    def store(self) -> WASMStore:
        """
        The persistence layer.

        Returns:
            The store singleton.
        """
        return get_store()

    def _exec(
        self,
        argv: Sequence[str],
        *,
        sudo: bool = True,
        timeout: int = DEFAULT_TIMEOUT,
    ) -> CommandResult:
        """
        Run a certbot-adjacent command through the shared runner.

        Certbot reads and writes ``/etc/letsencrypt``. Every unprivileged
        invocation lies: it cannot see its own configuration, exits non-zero,
        and the caller concludes the machine has no certificates and no plugins.
        That is how ``certbot plugins`` came to answer False for everything and
        every issuance quietly degraded to ``--webroot /var/www/html``.

        Args:
            argv: Program and arguments.
            sudo: Run the command with privileges.
            timeout: Deadline in seconds.

        Returns:
            The command outcome.
        """
        # WASM requires root, so the prefix is redundant on a correct install
        # and harmless on one where the operator used sudo to reach us. It stays
        # until the CLI entry point enforces root by itself.
        command = ["sudo", *argv] if sudo else list(argv)
        return self._run(command, timeout=timeout)

    def is_installed(self) -> bool:
        """
        Check whether certbot is installed.

        Returns:
            True when certbot is on PATH.
        """
        return self.runner.exists("certbot")

    def get_version(self) -> str | None:
        """
        Get the certbot version.

        Returns:
            The version string, or None when it cannot be determined.
        """
        result = self._exec(["certbot", "--version"])
        match = re.search(r"certbot (\S+)", f"{result.stdout}\n{result.stderr}")
        return match.group(1) if match else None

    # -- Reading existing certificates -------------------------------------

    def cert_exists(self, domain: str) -> bool:
        """
        Check whether a certificate exists for a domain.

        Args:
            domain: Domain name.

        Returns:
            True when the lineage has a full chain on disk.
        """
        return (self.LIVE_DIR / self._validated(domain) / "fullchain.pem").exists()

    def get_cert_path(self, domain: str) -> CertificatePaths:
        """
        Get the certificate file paths for a domain.

        Args:
            domain: Domain name.

        Returns:
            The four files of the lineage.

        Raises:
            CertificateError: When the domain is not a valid domain name.
        """
        base = self.LIVE_DIR / self._validated(domain)
        return CertificatePaths(
            fullchain=base / "fullchain.pem",
            privkey=base / "privkey.pem",
            cert=base / "cert.pem",
            chain=base / "chain.pem",
        )

    def list_certificates(self) -> list[CertificateInfo]:
        """
        List every certificate certbot knows about.

        Returns:
            One record per certificate, empty when certbot has none or cannot
            be queried. Callers that decide whether to issue must use
            :meth:`_query_certificates` instead, which keeps those two answers
            apart.
        """
        return self._query_certificates() or []

    def _query_certificates(self) -> list[CertificateInfo] | None:
        """
        Ask certbot what it manages, keeping "nothing" apart from "no answer".

        Returns:
            One record per certificate, or None when certbot could not be
            queried at all.
        """
        result = self._exec(["certbot", "certificates"])
        if not result.success:
            self.logger.debug(
                f"Could not read certbot's certificate list: "
                f"{(result.stderr or result.stdout).strip()}"
            )
            return None
        return self._parse_certificates(result.stdout)

    @staticmethod
    def _parse_certificates(output: str) -> list[CertificateInfo]:
        """
        Parse the report ``certbot certificates`` prints.

        Args:
            output: Certbot's standard output.

        Returns:
            One record per certificate, in the order certbot listed them.
        """
        certificates: list[CertificateInfo] = []

        current: CertificateInfo | None = None
        for raw in output.split("\n"):
            line = raw.strip()

            if line.startswith("Certificate Name:"):
                if current is not None:
                    certificates.append(current)
                current = CertificateInfo(name=line.split(":", 1)[1].strip())
            elif current is None:
                continue
            elif line.startswith("Domains:"):
                current.domains = line.split(":", 1)[1].strip().split()
            elif line.startswith("Expiry Date:"):
                expiry_str = line.split(":", 1)[1].strip()
                match = re.search(r"(\d{4}-\d{2}-\d{2})", expiry_str)
                if match:
                    current.expiry = match.group(1)
                current.expiry_full = expiry_str
            elif line.startswith("Certificate Path:"):
                current.cert_path = line.split(":", 1)[1].strip()
            elif line.startswith("Private Key Path:"):
                current.key_path = line.split(":", 1)[1].strip()

        if current is not None:
            certificates.append(current)

        return certificates

    def get_cert_info(self, domain: str) -> CertificateInfo | None:
        """
        Get the certificate covering a domain.

        Args:
            domain: Domain name.

        Returns:
            The certificate record, or None when no lineage covers the domain.
        """
        for cert in self.list_certificates():
            if cert.name == domain or domain in cert.domains:
                return cert
        return None

    def cert_covers_domains(self, domain: str, required_domains: Sequence[str]) -> bool:
        """
        Check whether an existing certificate covers every required domain.

        Args:
            domain: Primary domain, which is also the lineage name.
            required_domains: Domains that must be covered.

        Returns:
            True when all of them are covered.
        """
        info = self.get_cert_info(domain)
        if info is None:
            return False
        return all(d in info.domains for d in required_domains)

    def _existing_coverage(self, primary: str) -> list[str]:
        """
        Read the domains the existing lineage already covers.

        Args:
            primary: Lineage name, which is the primary domain.

        Returns:
            The covered domains, empty when certbot manages no lineage for the
            domain.

        Raises:
            CertificateError: When certbot's certificate list cannot be read.
                A failure to read state is not evidence that there is no
                certificate, and treating it as such re-orders from a rate
                limited API against a domain that is already served.
        """
        certificates = self._query_certificates()
        if certificates is None:
            raise CertificateError(
                f"Cannot read the certificates certbot manages, and {primary} already has one",
                details=(
                    "Issuing again without knowing what the existing certificate covers "
                    "would spend a Let's Encrypt rate limit for nothing. Run "
                    "'certbot certificates' as root to see what it reports, fix that, "
                    "and run this command again."
                ),
            )

        for cert in certificates:
            if cert.name == primary or primary in cert.domains:
                return list(cert.domains)
        return []

    def _check_certbot_plugin(self, plugin: str) -> bool:
        """
        Check whether a certbot plugin is installed.

        The answer is cached for the life of the manager: issuing one
        certificate asks the same question up to twice, and ``certbot plugins``
        is not cheap.

        Args:
            plugin: Plugin name, ``nginx`` or ``apache``.

        Returns:
            True when the plugin is available.
        """
        if plugin in self._plugin_available:
            return self._plugin_available[plugin]

        result = self._exec(["certbot", "plugins"])
        available = result.success and f"* {plugin}" in result.stdout
        self._plugin_available[plugin] = available
        return available

    # -- Issuance ----------------------------------------------------------

    @staticmethod
    def _validated(domain: str) -> str:
        """
        Normalise a domain and refuse anything that is not one.

        A lineage name becomes a directory under ``/etc/letsencrypt/live`` and a
        ``--cert-name`` argument, so it is checked before it is used even though
        the runner never involves a shell.

        Args:
            domain: Candidate domain name.

        Returns:
            The domain, lowercased and stripped.

        Raises:
            CertificateError: When the domain is not a valid domain name.
        """
        candidate = domain.strip().lower()
        valid, reason = is_valid_domain(candidate)
        if not valid:
            raise CertificateError(
                f"Invalid domain: {domain!r}",
                details=f"{reason}. Pass a bare host name such as example.com.",
            )
        return candidate

    def certificate_domains(
        self,
        domain: str,
        additional_domains: Sequence[str] | None = None,
        include_www: bool = False,
    ) -> list[str]:
        """
        Build the domain list a certificate should cover.

        Args:
            domain: Primary domain, first in the result and the lineage name.
            additional_domains: Further domains to cover.
            include_www: Add ``www.<domain>`` when the domain is a bare
                registrable name. A subdomain or a domain that already starts
                with ``www`` is left alone, because the alias would not resolve
                and certbot would fail the whole order.

        Returns:
            The validated domains, deduplicated, primary domain first.

        Raises:
            CertificateError: When any domain is not a valid domain name.
        """
        primary = self._validated(domain)
        ordered = [primary]

        if include_www and should_include_www(primary):
            ordered.append(f"www.{primary}")

        for extra in additional_domains or []:
            ordered.append(self._validated(extra))

        # Duplicates make certbot issue a certificate whose SAN list does not
        # match what was asked for, which then never compares equal on the next
        # idempotence check.
        seen: set[str] = set()
        unique = []
        for name in ordered:
            if name not in seen:
                seen.add(name)
                unique.append(name)
        return unique

    def create(
        self,
        domains: Sequence[str],
        email: str | None = None,
        webserver: str | None = None,
        webroot: Path | None = None,
        dry_run: bool = False,
        expand: bool = False,
    ) -> bool:
        """
        Obtain a certificate covering several domains.

        Args:
            domains: Domains to cover. The first one names the certificate.
            email: Email for registration and recovery.
            webserver: Web server whose certbot plugin should be used,
                ``nginx`` or ``apache``.
            webroot: Webroot path for the webroot plugin.
            dry_run: Ask certbot for a test certificate.
            expand: Expand an existing certificate.

        Returns:
            True when the certificate is in place.

        Raises:
            CertificateError: When no domain was given or issuance fails.
        """
        if not domains:
            raise CertificateError(
                "No domains given for certificate issuance",
                details="Pass at least the primary domain.",
            )

        return self.obtain(
            domains[0],
            email=email,
            webroot=webroot,
            nginx=webserver == "nginx",
            apache=webserver == "apache",
            dry_run=dry_run,
            additional_domains=list(domains[1:]) or None,
            expand=expand,
        )

    def obtain(
        self,
        domain: str,
        email: str | None = None,
        webroot: Path | None = None,
        standalone: bool = False,
        nginx: bool = False,
        apache: bool = False,
        dry_run: bool = False,
        additional_domains: Sequence[str] | None = None,
        expand: bool = False,
        include_www: bool = False,
    ) -> bool:
        """
        Obtain a certificate, or confirm that a suitable one already exists.

        Args:
            domain: Primary domain, which becomes the lineage name.
            email: Email for registration and recovery.
            webroot: Webroot path for the webroot plugin.
            standalone: Use the standalone plugin.
            nginx: Use the nginx plugin.
            apache: Use the apache plugin.
            dry_run: Ask certbot for a test certificate.
            additional_domains: Further domains to cover.
            expand: Expand the existing certificate even when it already covers
                the requested domains.
            include_www: Also cover ``www.<domain>`` when that makes sense.

        Returns:
            True when the certificate covering every requested domain exists.

        Raises:
            CertificateError: When a domain is invalid, when the state of the
                existing certificate cannot be read, or when issuance fails.
        """
        requested = self.certificate_domains(domain, additional_domains, include_www)
        primary = requested[0]

        if self.cert_exists(primary) and not dry_run:
            covered = self._existing_coverage(primary)
            if all(name in covered for name in requested) and not expand:
                self.logger.info(f"Certificate already covers all domains: {', '.join(requested)}")
                return True
            # certbot rewrites the SAN list to exactly what -d says, even with
            # --expand, so expanding with the requested names alone would drop
            # whatever else the certificate carries - usually the www alias.
            requested = requested + [name for name in covered if name not in requested]
            self.logger.info(f"Expanding certificate to cover: {', '.join(requested)}")
            expand = True

        cmd = self._build_issue_command(
            requested,
            email=email,
            webroot=webroot,
            standalone=standalone,
            nginx=nginx,
            apache=apache,
            expand=expand,
            dry_run=dry_run,
        )

        result = self._exec(cmd, timeout=_ISSUE_TIMEOUT)
        if not result.success:
            raise CertificateError(
                f"Failed to obtain certificate for {primary}",
                details=(result.stderr or result.stdout).strip()
                or "Check that the domain resolves to this host and port 80 is reachable.",
            )

        paths = self.get_cert_path(primary)
        # Record TLS on the site only once the files are actually there. A
        # rehearsal reports success without issuing anything - --dry-run is
        # enforced at the command runner, which the manager cannot see - and a
        # store claiming a site serves TLS when it has no key makes every reader
        # of that record lie, including the vhost writer.
        if not dry_run and paths.fullchain.exists():
            self._store_ssl_state(
                primary,
                enabled=True,
                certificate=str(paths.fullchain),
                key=str(paths.privkey),
            )
        elif not dry_run:
            self.logger.debug(f"Certbot reported success but {paths.fullchain} is not there")

        self.logger.debug(f"Obtained certificate for: {primary}")
        return True

    def _build_issue_command(
        self,
        domains: Sequence[str],
        *,
        email: str | None,
        webroot: Path | None,
        standalone: bool,
        nginx: bool,
        apache: bool,
        expand: bool,
        dry_run: bool,
    ) -> list[str]:
        """
        Assemble the ``certbot certonly`` argument vector.

        Args:
            domains: Validated domains, primary first.
            email: Email for registration, or None to register without one.
            webroot: Webroot path, which also selects the webroot plugin.
            standalone: Use the standalone plugin.
            nginx: Prefer the nginx plugin.
            apache: Prefer the apache plugin.
            expand: Add ``--expand``.
            dry_run: Add ``--dry-run``.

        Returns:
            The full argument vector.
        """
        primary = domains[0]
        # --cert-name pins the lineage: the same domain always renews and
        # expands in place instead of spawning example.com-0001.
        cmd = ["certbot", "certonly", "--cert-name", primary]

        email = email or self.config.ssl_email
        if email:
            cmd.extend(["--email", email])
        else:
            cmd.append("--register-unsafely-without-email")

        cmd.extend(["--non-interactive", "--agree-tos"])
        cmd.extend(self._authenticator_args(webroot, standalone, nginx, apache))

        for name in domains:
            cmd.extend(["-d", name])

        if expand:
            cmd.append("--expand")
        if dry_run:
            cmd.append("--dry-run")

        return cmd

    def _authenticator_args(
        self,
        webroot: Path | None,
        standalone: bool,
        nginx: bool,
        apache: bool,
    ) -> list[str]:
        """
        Choose how certbot should prove control of the domain.

        Args:
            webroot: Explicit webroot path, which selects the webroot plugin.
            standalone: Use the standalone plugin.
            nginx: Prefer the nginx plugin.
            apache: Prefer the apache plugin.

        Returns:
            The authenticator arguments.
        """
        if nginx or apache:
            wanted = "nginx" if nginx else "apache"
            if self._check_certbot_plugin(wanted):
                return [f"--{wanted}"]
            self.logger.warning(
                f"certbot {wanted} plugin not installed. Using the webroot method instead. "
                f"Install it with: apt install python3-certbot-{wanted}"
            )
            return ["--webroot", "-w", str(webroot or DEFAULT_WEBROOT)]

        if standalone:
            return ["--standalone"]

        if webroot is not None:
            return ["--webroot", "-w", str(webroot)]

        if self.runner.exists("nginx"):
            if self._check_certbot_plugin("nginx"):
                return ["--nginx"]
            self.logger.warning(
                "certbot nginx plugin not installed. Using the webroot method. "
                "Install it with: apt install python3-certbot-nginx"
            )
            return ["--webroot", "-w", str(DEFAULT_WEBROOT)]

        return ["--standalone"]

    def _store_ssl_state(
        self,
        domain: str,
        *,
        enabled: bool,
        certificate: str | None = None,
        key: str | None = None,
    ) -> None:
        """
        Record the SSL state of a domain in the store.

        Args:
            domain: Domain name.
            enabled: Whether the site now serves TLS.
            certificate: Path to the full chain, when enabling.
            key: Path to the private key, when enabling.
        """
        try:
            if enabled:
                self.store.update_site_ssl(
                    domain=domain,
                    ssl=True,
                    ssl_certificate=certificate,
                    ssl_key=key,
                )
            else:
                self.store.update_site_ssl(domain=domain, ssl=False)

            app = self.store.get_app(domain)
            if app:
                app.ssl_enabled = enabled
                app.ssl_certificate = certificate
                app.ssl_key = key
                self.store.update_app(app)
        except (WASMError, sqlite3.Error) as exc:
            self.logger.debug(f"Could not update SSL in store: {exc}")

    # -- Renewal and removal -----------------------------------------------

    def renew(
        self,
        domain: str | None = None,
        force: bool = False,
        dry_run: bool = False,
    ) -> bool:
        """
        Renew certificates.

        Args:
            domain: Lineage to renew, or None for every certificate that is due.
            force: Renew even when the certificate is not near expiry.
            dry_run: Rehearse the renewal against the staging environment.

        Returns:
            True when certbot reported success.

        Raises:
            CertificateError: When the domain is invalid or renewal fails.
        """
        cmd = ["certbot", "renew", "--non-interactive"]

        if domain:
            cmd.extend(["--cert-name", self._validated(domain)])
        if force:
            cmd.append("--force-renewal")
        if dry_run:
            cmd.append("--dry-run")

        result = self._exec(cmd, timeout=_RENEW_TIMEOUT)
        if not result.success:
            raise CertificateError(
                "Certificate renewal failed",
                details=(result.stderr or result.stdout).strip()
                or "Run 'certbot renew --dry-run' to see what the ACME server reports.",
            )

        return True

    def revoke(self, domain: str, delete: bool = True) -> bool:
        """
        Revoke a certificate.

        Args:
            domain: Domain name.
            delete: Also delete the certificate files.

        Returns:
            True when the certificate was revoked.

        Raises:
            CertificateError: When no such certificate exists or revocation
                fails.
        """
        primary = self._validated(domain)
        if not self.cert_exists(primary):
            raise CertificateError(
                f"Certificate not found: {primary}",
                details=f"Nothing to revoke under {self.LIVE_DIR / primary}.",
            )

        cmd = [
            "certbot",
            "revoke",
            "--cert-path",
            str(self.get_cert_path(primary).fullchain),
            "--non-interactive",
        ]
        if delete:
            cmd.append("--delete-after-revoke")

        result = self._exec(cmd, timeout=_ISSUE_TIMEOUT)
        if not result.success:
            raise CertificateError(
                f"Failed to revoke certificate for {primary}",
                details=(result.stderr or result.stdout).strip(),
            )

        self._store_ssl_state(primary, enabled=False)
        self.logger.debug(f"Revoked certificate for: {primary}")
        return True

    def delete(self, domain: str) -> bool:
        """
        Delete a certificate without revoking it.

        Deleting a lineage that is already gone is not an error: the caller
        wanted it absent and it is absent.

        Args:
            domain: Domain name.

        Returns:
            True when no certificate remains for the domain.

        Raises:
            CertificateError: When the domain is invalid or deletion fails.
        """
        primary = self._validated(domain)
        if not self.cert_exists(primary):
            self.logger.debug(f"No certificate to delete for: {primary}")
            self._store_ssl_state(primary, enabled=False)
            return True

        result = self._exec(
            ["certbot", "delete", "--cert-name", primary, "--non-interactive"],
            timeout=_ISSUE_TIMEOUT,
        )
        if not result.success:
            raise CertificateError(
                f"Failed to delete certificate for {primary}",
                details=(result.stderr or result.stdout).strip(),
            )

        self._store_ssl_state(primary, enabled=False)
        return True

    def setup_auto_renewal(self) -> bool:
        """
        Make certificates renew themselves.

        Prefers the systemd timer the certbot packages ship; falls back to a
        cron entry for systems that have neither the timer nor systemd.

        Returns:
            True when automatic renewal is in place.
        """
        if self._exec(["systemctl", "enable", "certbot.timer"]).success:
            self._exec(["systemctl", "start", "certbot.timer"])
            return True

        try:
            self.fs.make_dir(_CRON_FILE.parent)
            self.fs.write_text(_CRON_FILE, _CRON_LINE, mode=_CRON_MODE)
        except OSError as exc:
            self.logger.error(f"Could not install the renewal cron entry: {exc}")
            return False

        return True

    def test_cert(self, domain: str) -> CertificateTest:
        """
        Inspect a certificate file with openssl.

        Args:
            domain: Domain name.

        Returns:
            The validity window, or the reason it could not be read.

        Raises:
            CertificateError: When the domain is not a valid domain name.
        """
        primary = self._validated(domain)
        if not self.cert_exists(primary):
            return CertificateTest(valid=False, error="Certificate not found")

        cert_path = self.get_cert_path(primary).fullchain
        result = self._exec(
            ["openssl", "x509", "-in", str(cert_path), "-noout", "-dates"],
            sudo=False,
        )

        if not result.success:
            return CertificateTest(valid=False, error=result.stderr.strip() or "openssl failed")

        not_before: str | None = None
        not_after: str | None = None
        for line in result.stdout.split("\n"):
            if line.startswith("notBefore="):
                not_before = line.split("=", 1)[1].strip()
            elif line.startswith("notAfter="):
                not_after = line.split("=", 1)[1].strip()

        return CertificateTest(
            valid=True,
            not_before=not_before,
            not_after=not_after,
            path=str(cert_path),
        )
