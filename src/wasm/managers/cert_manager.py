"""
SSL certificate manager for WASM using Certbot.
"""

import re
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from wasm.core.exceptions import CertificateError
from wasm.core.store import get_store
from wasm.managers.base_manager import BaseManager


class CertManager(BaseManager):
    """
    Manager for SSL certificates using Certbot.
    
    Handles obtaining, renewing, and revoking Let's Encrypt certificates.
    """
    
    LETSENCRYPT_DIR = Path("/etc/letsencrypt")
    LIVE_DIR = LETSENCRYPT_DIR / "live"
    
    def __init__(self, verbose: bool = False):
        """Initialize certificate manager."""
        super().__init__(verbose=verbose)
        self.store = get_store()
    
    def is_installed(self) -> bool:
        """Check if Certbot is installed."""
        result = self._run(["which", "certbot"])
        return result.success
    
    def get_version(self) -> Optional[str]:
        """Get Certbot version."""
        result = self._run(["certbot", "--version"])
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
    
    def get_cert_path(self, domain: str) -> Dict[str, Path]:
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
    
    def list_certificates(self) -> List[Dict]:
        """
        List all certificates.
        
        Returns:
            List of certificate information dictionaries.
        """
        certificates = []
        
        result = self._run_sudo(["certbot", "certificates"])
        if not result.success:
            return certificates
        
        current_cert = {}
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
    
    def get_cert_info(self, domain: str) -> Optional[Dict]:
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
    
    def _check_certbot_plugin(self, plugin: str) -> bool:
        """
        Check if a certbot plugin is available.
        
        Args:
            plugin: Plugin name (nginx, apache).
            
        Returns:
            True if plugin is available.
        """
        result = self._run(["certbot", "plugins"])
        if result.success:
            return f"* {plugin}" in result.stdout
        return False
    
    def obtain(
        self,
        domain: str,
        email: Optional[str] = None,
        webroot: Optional[Path] = None,
        standalone: bool = False,
        nginx: bool = False,
        apache: bool = False,
        dry_run: bool = False,
        additional_domains: Optional[List[str]] = None,
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
            
        Returns:
            True if certificate was obtained successfully.
            
        Raises:
            CertificateError: If certificate issuance fails.
        """
        if self.cert_exists(domain) and not dry_run:
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
        webroot_path = webroot or Path(f"/var/www/html")
        
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
            if self._run(["which", "nginx"]).success and self._check_certbot_plugin("nginx"):
                cmd.append("--nginx")
            elif self._run(["which", "nginx"]).success:
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
        
        # Dry run
        if dry_run:
            cmd.append("--dry-run")
        
        # Execute
        result = self._run_sudo(cmd, timeout=300)
        
        if not result.success:
            raise CertificateError(
                f"Failed to obtain certificate for {domain}",
                details=result.stderr,
            )
        
        # Update store with SSL info
        if not dry_run:
            try:
                cert_paths = self.get_cert_path(domain)
                self.store.update_site_ssl(
                    domain=domain,
                    ssl=True,
                    ssl_certificate=str(cert_paths["fullchain"]),
                    ssl_key=str(cert_paths["privkey"]),
                )
                # Also update app if exists
                app = self.store.get_app(domain)
                if app:
                    app.ssl_enabled = True
                    app.ssl_certificate = str(cert_paths["fullchain"])
                    app.ssl_key = str(cert_paths["privkey"])
                    self.store.update_app(app)
            except Exception as e:
                self.logger.debug(f"Could not update SSL in store: {e}")
        
        self.logger.debug(f"Obtained certificate for: {domain}")
        return True
    
    def renew(
        self,
        domain: Optional[str] = None,
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
        
        result = self._run_sudo(cmd, timeout=600)
        
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
            "certbot", "revoke",
            "--cert-path", str(cert_path),
            "--non-interactive",
        ]
        
        if delete:
            cmd.append("--delete-after-revoke")
        
        result = self._run_sudo(cmd)
        
        if not result.success:
            raise CertificateError(
                f"Failed to revoke certificate for {domain}",
                details=result.stderr,
            )
        
        # Update store
        try:
            self.store.update_site_ssl(domain=domain, ssl=False)
            app = self.store.get_app(domain)
            if app:
                app.ssl_enabled = False
                app.ssl_certificate = None
                app.ssl_key = None
                self.store.update_app(app)
        except Exception as e:
            self.logger.debug(f"Could not update SSL in store: {e}")
        
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
            "certbot", "delete",
            "--cert-name", domain,
            "--non-interactive",
        ]
        
        result = self._run_sudo(cmd)
        
        if not result.success:
            raise CertificateError(
                f"Failed to delete certificate for {domain}",
                details=result.stderr,
            )
        
        # Update store
        try:
            self.store.update_site_ssl(domain=domain, ssl=False)
            app = self.store.get_app(domain)
            if app:
                app.ssl_enabled = False
                app.ssl_certificate = None
                app.ssl_key = None
                self.store.update_app(app)
        except Exception as e:
            self.logger.debug(f"Could not update SSL in store: {e}")
        
        return True
    
    def setup_auto_renewal(self) -> bool:
        """
        Setup automatic certificate renewal via systemd timer.
        
        Returns:
            True if setup was successful.
        """
        # Enable certbot timer if it exists
        result = self._run_sudo(["systemctl", "enable", "certbot.timer"])
        if result.success:
            self._run_sudo(["systemctl", "start", "certbot.timer"])
            return True
        
        # Otherwise, set up cron job
        cron_cmd = "0 0,12 * * * root certbot renew -q"
        cron_file = Path("/etc/cron.d/certbot-renew")
        
        from wasm.core.utils import write_file
        return write_file(cron_file, cron_cmd + "\n", sudo=True)
    
    def test_cert(self, domain: str) -> Dict:
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
        result = self._run([
            "openssl", "x509",
            "-in", str(cert_path),
            "-noout", "-dates",
        ])
        
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
