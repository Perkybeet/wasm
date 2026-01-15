"""
Tests for security-related functions in WASM.
"""

import pytest

from wasm.core.exceptions import SecurityError
from wasm.core.utils import (
    run_trusted_installer,
    TRUSTED_INSTALLER_URLS,
)


class TestSecurityError:
    """Tests for SecurityError exception."""

    def test_security_error_inherits_from_wasm_error(self):
        """SecurityError should inherit from WASMError."""
        from wasm.core.exceptions import WASMError

        error = SecurityError("test message")
        assert isinstance(error, WASMError)

    def test_security_error_message(self):
        """SecurityError should store message."""
        error = SecurityError("test message", "details here")
        assert error.message == "test message"
        assert error.details == "details here"

    def test_security_error_str(self):
        """SecurityError string representation should include details."""
        error = SecurityError("message", "details")
        str_repr = str(error)
        assert "message" in str_repr
        assert "details" in str_repr


class TestTrustedInstallerUrls:
    """Tests for TRUSTED_INSTALLER_URLS whitelist."""

    def test_whitelist_is_frozenset(self):
        """Whitelist should be immutable."""
        assert isinstance(TRUSTED_INSTALLER_URLS, frozenset)

    def test_whitelist_contains_expected_urls(self):
        """Whitelist should contain expected trusted URLs."""
        expected_urls = [
            "https://deb.nodesource.com/setup_20.x",
            "https://bun.sh/install",
        ]
        for url in expected_urls:
            assert url in TRUSTED_INSTALLER_URLS

    def test_whitelist_urls_are_https(self):
        """All URLs in whitelist should use HTTPS."""
        for url in TRUSTED_INSTALLER_URLS:
            assert url.startswith("https://"), f"URL {url} should use HTTPS"


class TestRunTrustedInstaller:
    """Tests for run_trusted_installer function."""

    def test_rejects_untrusted_url(self):
        """Should raise SecurityError for untrusted URLs."""
        with pytest.raises(SecurityError) as exc_info:
            run_trusted_installer("https://malicious.com/install.sh")

        assert "Untrusted installer URL" in str(exc_info.value)

    def test_rejects_http_url(self):
        """Should reject HTTP URLs (not in whitelist)."""
        with pytest.raises(SecurityError):
            run_trusted_installer("http://example.com/script.sh")

    def test_rejects_arbitrary_urls(self):
        """Should reject arbitrary URLs not in whitelist."""
        untrusted_urls = [
            "https://example.com/install",
            "https://raw.githubusercontent.com/user/repo/script.sh",
            "https://pastebin.com/raw/abc123",
            "file:///etc/passwd",
        ]
        for url in untrusted_urls:
            with pytest.raises(SecurityError):
                run_trusted_installer(url)

    def test_error_message_includes_allowed_urls(self):
        """Error should list allowed URLs for user guidance."""
        with pytest.raises(SecurityError) as exc_info:
            run_trusted_installer("https://bad.com/script")

        error_details = exc_info.value.details
        assert "Only the following URLs are allowed" in error_details
