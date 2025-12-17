"""Tests for domain validator."""

import pytest

from wasm.validators.domain import (
    is_valid_domain,
    validate_domain,
    check_domain,
    get_domain_parts,
    is_subdomain,
)
from wasm.core.exceptions import DomainError


class TestIsValidDomain:
    """Tests for is_valid_domain function."""
    
    def test_valid_domains(self):
        """Test valid domain names."""
        valid_domains = [
            "example.com",
            "www.example.com",
            "sub.domain.example.com",
            "example-site.com",
            "123.example.com",
            "ex.co",
        ]
        for domain in valid_domains:
            is_valid, _ = is_valid_domain(domain)
            assert is_valid is True, f"Expected {domain} to be valid"
    
    def test_invalid_domains(self):
        """Test invalid domain names."""
        invalid_domains = [
            "",
            "-example.com",
            "example-.com",
        ]
        for domain in invalid_domains:
            is_valid, _ = is_valid_domain(domain)
            assert is_valid is False, f"Expected {domain} to be invalid"


class TestCheckDomain:
    """Tests for check_domain function (boolean only)."""
    
    def test_valid_domain(self):
        """Test valid domain returns True."""
        assert check_domain("example.com") is True
        assert check_domain("www.example.com") is True
    
    def test_invalid_domain(self):
        """Test invalid domain returns False."""
        assert check_domain("") is False
        assert check_domain("-invalid.com") is False


class TestValidateDomain:
    """Tests for validate_domain function."""
    
    def test_valid_domain_returns_normalized(self):
        """Test that validate_domain returns normalized domain."""
        assert validate_domain("EXAMPLE.COM") == "example.com"
        assert validate_domain("  example.com  ") == "example.com"
    
    def test_strips_protocol(self):
        """Test that protocol is stripped."""
        assert validate_domain("http://example.com") == "example.com"
        assert validate_domain("https://example.com/path") == "example.com"
    
    def test_strips_port(self):
        """Test that port is stripped."""
        assert validate_domain("example.com:8080") == "example.com"
    
    def test_invalid_domain_raises_error(self):
        """Test that validate_domain raises DomainError for invalid domains."""
        with pytest.raises(DomainError):
            validate_domain("")


class TestGetDomainParts:
    """Tests for get_domain_parts function."""
    
    def test_simple_domain(self):
        """Test extracting parts from simple domain."""
        parts = get_domain_parts("example.com")
        assert parts["subdomain"] == ""
        assert parts["domain"] == "example"
        assert parts["tld"] == "com"
    
    def test_domain_with_subdomain(self):
        """Test extracting parts from domain with subdomain."""
        parts = get_domain_parts("www.example.com")
        assert parts["subdomain"] == "www"
        assert parts["domain"] == "example"
        assert parts["tld"] == "com"
    
    def test_domain_with_multiple_subdomains(self):
        """Test extracting parts from domain with multiple subdomains."""
        parts = get_domain_parts("a.b.c.example.com")
        assert parts["subdomain"] == "a.b.c"
        assert parts["domain"] == "example"
        assert parts["tld"] == "com"
    
    def test_single_part_domain(self):
        """Test domain with single part (localhost)."""
        parts = get_domain_parts("localhost")
        assert parts["domain"] == "localhost"
        assert parts["tld"] == ""


class TestIsSubdomain:
    """Tests for is_subdomain function."""
    
    def test_is_subdomain(self):
        """Test subdomain detection."""
        assert is_subdomain("www.example.com") is True
        assert is_subdomain("api.example.com") is True
        assert is_subdomain("a.b.example.com") is True
    
    def test_is_not_subdomain(self):
        """Test non-subdomain detection."""
        assert is_subdomain("example.com") is False
        assert is_subdomain("localhost") is False
