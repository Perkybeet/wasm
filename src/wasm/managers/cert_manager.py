"""
SSL certificate manager for WASM using Certbot.

Certificate information crosses two module boundaries: ``certbot certificates``
is parsed here and consumed by the CLI and by the health check. That data
travels as a :class:`CertificateInfo`, so the key names are stated once instead
of being guessed at each end. ``wasm health`` spent several releases looking for
an ``expires`` key that this module never wrote.
"""

import re
import sqlite3
from pathlib import Path
from typing import TypedDict

from wasm.core.exceptions import CertificateError, WASMError
from wasm.core.runner import DEFAULT_TIMEOUT, CommandResult, CommandRunner, get_runner
from wasm.core.store import get_store
from wasm.managers.base_manager import BaseManager

#: Issuing or renewing a certificate involves ACME round trips.
_ISSUE_TIMEOUT = 300
_RENEW_TIMEOUT = 600


class CertificateInfo(TypedDict, total=False):
    """
    One entry of ``certbot certificates``.

    A TypedDict rather than a dataclass because these values are already
    consumed as mappings across the CLI and the web API; this pins the key
    names without forcing every reader to change shape.

    Attributes:
        name: Certificate (lineage) name.
        domains: Every domain the certificate covers.
        expiry: Expiry date as ``YYYY-MM-DD``, absent when certbot's output
            could not be parsed.
        expiry_full: The raw expiry line, including certbot's validity note.
        cert_path: Path to the certificate file.
        key_path: Path to the private key.
    """

    name: str
    domains: list[str]
    expiry: str
    expiry_full: str
    cert_path: str
    key_path: str


class CertManager(BaseManager):
    """
    Manager for SSL certificates using Certbot.

    Handles obtaining, renewing, and revoking Let's Encrypt certificates.
    """

    LETSENCRYPT_DIR = Path("/etc/letsencrypt")
    LIVE_DIR = LETSENCRYPT_DIR / "live"

    def __init__(self, verbose: bool = False, runner: CommandRunner | None = None):
        """
        Initialize certificate manager.

        Args:
            verbose: Enable verbose logging.
            runner: Command runner to execute certbot with. Defaults to the
                process-wide runner.
        """
        super().__init__(verbose=verbose)
        self.store = get_store()
        self._runner = runner

    @property
    def runner(self) -> CommandRunner:
        """
        The command runner used for every certbot invocation.

        Returns:
            The injected runner, or the process-wide one.
        """
        return self._runner or get_runner()

    def _exec(
        self,
        argv: list[str],
        *,
        sudo: bool = True,
        timeout: int = DEFAULT_TIMEOUT,
    ) -> CommandResult:
        """
        Run a command through the shared runner.

        Certbot reads and writes ``/etc/letsencrypt``; it is run with
        privileges by default because every unprivileged invocation lies.

        Args:
            argv: Program and arguments.
            sudo: Prefix the command with sudo.
            timeout: Deadline in seconds.

        Returns:
            The command outcome.
        """
        return self.runner.run(["sudo", *argv] if sudo else argv, timeout=timeout)

    def is_installed(self) -> bool:
        """Check if Certbot is installed."""
        return self.runner.exists("certbot")

    def get_version(self) -> str | None:
        """Get Certbot version."""
        result = self._exec(["certbot", "--version"])
        if result.success:
            match = re.search(r"certbot (\S+)", result.stdout)
            if match:
                return match.group(1)
        return None

    def cert_exists(self, domain: str) -> bool:
        """
        Check if a certificate exists for a domain.

        Args:
            domain: Domain name.

        Returns:
            True if certificate exists.
        """
        cert_path = self.LIVE_DIR / domain / "fullchain.pem"
        return cert_path.exists()

    def get_cert_path(self, domain: str) -> dict[str, Path]:
        """
        Get certificate file paths.

        Args:
            domain: Domain name.

        Returns:
            Dictionary with certificate paths.
        """
        base = self.LIVE_DIR / domain
        return {
            "fullchain": base / "fullchain.pem",
            "privkey": base / "privkey.pem",
            "cert": base / "cert.pem",
            "chain": base / "chain.pem",
        }

    def list_certificates(self) -> list[CertificateInfo]:
        """
        List all certificates.

        Returns:
            One entry per certificate known to certbot.
        """
        certificates: list[CertificateInfo] = []

        result = self._exec(["certbot", "certificates"])
        if not result.success:
            return certificates

        current_cert: CertificateInfo = {}
        for line in result.stdout.split("\n"):
            line = line.strip()

            if line.startswith("Certificate Name:"):
                if current_cert:
                    certificates.append(current_cert)
                current_cert = {"name": line.split(":", 1)[1].strip()}
            elif line.startswith("Domains:"):
                current_cert["domains"] = line.split(":", 1)[1].strip().split()
            elif line.startswith("Expiry Date:"):
                expiry_str = line.split(":", 1)[1].strip()
                # Parse expiry date
                match = re.search(r"(\d{4}-\d{2}-\d{2})", expiry_str)
                if match:
                    current_cert["expiry"] = match.group(1)
                current_cert["expiry_full"] = expiry_str
            elif line.startswith("Certificate Path:"):
                current_cert["cert_path"] = line.split(":", 1)[1].strip()
            elif line.startswith("Private Key Path:"):
                current_cert["key_path"] = line.split(":", 1)[1].strip()

        if current_cert:
            certificates.append(current_cert)

        return certificates

    def get_cert_info(self, domain: str) -> CertificateInfo | None:
        """
        Get certificate information for a domain.

        Args:
            domain: Domain name.

        Returns:
            Certificate information or None.
        """
        certificates = self.list_certificates()

        for cert in certificates:
            if domain in cert.get("domains", []) or cert.get("name") == domain:
                return cert

        return None

    def cert_covers_domains(self, domain: str, required_domains: list[str]) -> bool:
        """
        Check if an existing certificate covers all required domains.

        Args:
            domain: Primary domain (certificate name).
            required_domains: List of domains that must be covered.

        Returns:
            True if all required domains are covered.
        """
        info = self.get_cert_info(domain)
        if not info:
            return False

        cert_domains = info.get("domains", [])
        for d in required_domains:
            if d not in cert_domains:
                return False
        return True

    def _check_certbot_plugin(self, plugin: str) -> bool:
        """
        Check if a certbot plugin is available.

        Without privileges certbot cannot read its own configuration and exits
        non-zero, so this probe used to answer False for every plugin and every
        issuance silently degraded to ``--webroot /var/www/html``.

        Args:
            plugin: Plugin name (nginx, apache).

        Returns:
            True if plugin is available.
        """
        result = self._exec(["certbot", "plugins"])
        if result.success:
            return f"* {plugin}" in result.stdout
        return False

    def create(
        self,
        domains: list[str],
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
            webserver: Web server whose certbot plugin should be used
                ("nginx" or "apache").
            webroot: Webroot path for the webroot plugin.
            dry_run: Test certificate issuance.
            expand: Expand an existing certificate.

        Returns:
            True if the certificate was obtained.

        Raises:
            CertificateError: If no domain was given or issuance fails.
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
        additional_domains: list[str] | None = None,
        expand: bool = False,
    ) -> bool:
        """
        Obtain a new certificate.

        Args:
            domain: Primary domain name.
            email: Email for registration and recovery.
            webroot: Webroot path for webroot plugin.
            standalone: Use standalone plugin.
            nginx: Use nginx plugin.
            apache: Use apache plugin.
            dry_run: Test certificate issuance.
            additional_domains: Additional domains for the certificate.
            expand: Expand existing certificate to include additional domains.

        Returns:
            True if certificate was obtained successfully.

        Raises:
            CertificateError: If certificate issuance fails.
        """
        if self.cert_exists(domain) and not dry_run:
            # If additional domains requested, check if cert already covers them
            if additional_domains:
                all_required = [domain, *additional_domains]
                if self.cert_covers_domains(domain, all_required):
                    self.logger.info(
                        f"Certificate already covers all domains: {', '.join(all_required)}"
                    )
                    return True
                # Cert exists but doesn't cover all domains - need to expand
                self.logger.info(
                    f"Expanding certificate to include: {', '.join(additional_domains)}"
                )
                expand = True
            elif not expand:
                self.logger.warning(f"Certificate already exists for {domain}")
                return True

        # Build command
        cmd = ["certbot", "certonly"]

        # Add email
        email = email or self.config.ssl_email
        if email:
            cmd.extend(["--email", email])
        else:
            cmd.append("--register-unsafely-without-email")

        # Non-interactive
        cmd.extend(["--non-interactive", "--agree-tos"])

        # Plugin selection with fallback logic
        use_webroot = False
        webroot_path = webroot or Path("/var/www/html")

        if nginx:
            # Check if nginx plugin is available
            if self._check_certbot_plugin("nginx"):
                cmd.append("--nginx")
            else:
                self.logger.warning(
                    "certbot nginx plugin not installed. Using webroot method instead. "
                    "Install with: sudo apt install python3-certbot-nginx"
                )
                use_webroot = True
        elif apache:
            # Check if apache plugin is available
            if self._check_certbot_plugin("apache"):
                cmd.append("--apache")
            else:
                self.logger.warning(
                    "certbot apache plugin not installed. Using webroot method instead. "
                    "Install with: sudo apt install python3-certbot-apache"
                )
                use_webroot = True
        elif standalone:
            cmd.append("--standalone")
        elif webroot:
            use_webroot = True
        else:
            # Auto-detect: prefer nginx plugin if available
            nginx_installed = self.runner.exists("nginx")
            if nginx_installed and self._check_certbot_plugin("nginx"):
                cmd.append("--nginx")
            elif nginx_installed:
                # Nginx installed but plugin not available, use webroot
                self.logger.warning(
                    "certbot nginx plugin not installed. Using webroot method. "
                    "Install with: sudo apt install python3-certbot-nginx"
                )
                use_webroot = True
            else:
                cmd.append("--standalone")

        # Configure webroot if needed
        if use_webroot:
            cmd.extend(["--webroot", "-w", str(webroot_path)])

        # Add domains
        cmd.extend(["-d", domain])
        if additional_domains:
            for d in additional_domains:
                cmd.extend(["-d", d])

        # Expand existing certificate
        if expand and self.cert_exists(domain):
            cmd.append("--expand")

        # Dry run
        if dry_run:
            cmd.append("--dry-run")

        # Execute
        result = self._exec(cmd, timeout=_ISSUE_TIMEOUT)

        if not result.success:
            raise CertificateError(
                f"Failed to obtain certificate for {domain}",
                details=result.stderr,
            )

        # Update store with SSL info
        if not dry_run:
            cert_paths = self.get_cert_path(domain)
            self._store_ssl_state(
                domain,
                enabled=True,
                certificate=str(cert_paths["fullchain"]),
                key=str(cert_paths["privkey"]),
            )

        self.logger.debug(f"Obtained certificate for: {domain}")
        return True

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
        except (WASMError, sqlite3.Error) as e:
            self.logger.debug(f"Could not update SSL in store: {e}")

    def renew(
        self,
        domain: str | None = None,
        force: bool = False,
        dry_run: bool = False,
    ) -> bool:
        """
        Renew certificates.

        Args:
            domain: Specific domain to renew (or all if None).
            force: Force renewal even if not due.
            dry_run: Test renewal without making changes.

        Returns:
            True if renewal was successful.
        """
        cmd = ["certbot", "renew"]

        if domain:
            cmd.extend(["--cert-name", domain])

        if force:
            cmd.append("--force-renewal")

        if dry_run:
            cmd.append("--dry-run")

        cmd.append("--non-interactive")

        result = self._exec(cmd, timeout=_RENEW_TIMEOUT)

        if not result.success:
            raise CertificateError(
                "Certificate renewal failed",
                details=result.stderr,
            )

        return True

    def revoke(self, domain: str, delete: bool = True) -> bool:
        """
        Revoke a certificate.

        Args:
            domain: Domain name.
            delete: Also delete certificate files.

        Returns:
            True if revocation was successful.
        """
        if not self.cert_exists(domain):
            raise CertificateError(f"Certificate not found: {domain}")

        cert_path = self.get_cert_path(domain)["fullchain"]

        cmd = [
            "certbot",
            "revoke",
            "--cert-path",
            str(cert_path),
            "--non-interactive",
        ]

        if delete:
            cmd.append("--delete-after-revoke")

        result = self._exec(cmd, timeout=_ISSUE_TIMEOUT)

        if not result.success:
            raise CertificateError(
                f"Failed to revoke certificate for {domain}",
                details=result.stderr,
            )

        self._store_ssl_state(domain, enabled=False)

        self.logger.debug(f"Revoked certificate for: {domain}")
        return True

    def delete(self, domain: str) -> bool:
        """
        Delete a certificate (without revoking).

        Args:
            domain: Domain name.

        Returns:
            True if deletion was successful.
        """
        cmd = [
            "certbot",
            "delete",
            "--cert-name",
            domain,
            "--non-interactive",
        ]

        result = self._exec(cmd, timeout=_ISSUE_TIMEOUT)

        if not result.success:
            raise CertificateError(
                f"Failed to delete certificate for {domain}",
                details=result.stderr,
            )

        self._store_ssl_state(domain, enabled=False)

        return True

    def setup_auto_renewal(self) -> bool:
        """
        Setup automatic certificate renewal via systemd timer.

        Returns:
            True if setup was successful.
        """
        # Enable certbot timer if it exists
        result = self._exec(["systemctl", "enable", "certbot.timer"])
        if result.success:
            self._exec(["systemctl", "start", "certbot.timer"])
            return True

        # Otherwise, set up cron job
        cron_cmd = "0 0,12 * * * root certbot renew -q"
        cron_file = Path("/etc/cron.d/certbot-renew")

        from wasm.core.utils import write_file

        return write_file(cron_file, cron_cmd + "\n", sudo=True)

    def test_cert(self, domain: str) -> dict:
        """
        Test certificate validity.

        Args:
            domain: Domain name.

        Returns:
            Dictionary with test results.
        """
        if not self.cert_exists(domain):
            return {
                "valid": False,
                "error": "Certificate not found",
            }

        cert_path = self.get_cert_path(domain)["fullchain"]

        # Use openssl to check certificate
        result = self._exec(
            [
                "openssl",
                "x509",
                "-in",
                str(cert_path),
                "-noout",
                "-dates",
            ],
            sudo=False,
        )

        if not result.success:
            return {
                "valid": False,
                "error": result.stderr,
            }

        # Parse dates
        not_before = None
        not_after = None

        for line in result.stdout.split("\n"):
            if line.startswith("notBefore="):
                not_before = line.split("=", 1)[1].strip()
            elif line.startswith("notAfter="):
                not_after = line.split("=", 1)[1].strip()

        return {
            "valid": True,
            "not_before": not_before,
            "not_after": not_after,
            "path": str(cert_path),
        }
