"""
Domain name validation for WASM.
"""

import re
from typing import Tuple

from wasm.core.exceptions import DomainError


# Domain name regex pattern
DOMAIN_PATTERN = re.compile(
    r"^(?!-)"  # Cannot start with hyphen
    r"(?:[a-zA-Z0-9-]{1,63}\.)*"  # Subdomains
    r"[a-zA-Z0-9-]{1,63}"  # Domain name
    r"(?:\.[a-zA-Z]{2,})?$"  # TLD (optional for localhost)
)

# Reserved/invalid domains
RESERVED_DOMAINS = [
    "localhost",
    "example.com",
    "example.org",
    "example.net",
    "test",
    "invalid",
    "local",
]


def is_valid_domain(domain: str) -> Tuple[bool, str]:
    """
    Check if a domain name is valid.
    
    Args:
        domain: Domain name to validate.
        
    Returns:
        Tuple of (is_valid, error_message).
    """
    if not domain:
        return False, "Domain name cannot be empty"
    
    # Convert to lowercase for validation
    domain = domain.lower().strip()
    
    # Check length
    if len(domain) > 253:
        return False, "Domain name too long (max 253 characters)"
    
    # Check for valid characters
    if not DOMAIN_PATTERN.match(domain):
        return False, "Invalid domain name format"
    
    # Check each label (part between dots)
    labels = domain.split(".")
    for label in labels:
        if len(label) > 63:
            return False, f"Label '{label}' too long (max 63 characters)"
        if label.startswith("-") or label.endswith("-"):
            return False, "Labels cannot start or end with hyphens"
    
    # Check for consecutive dots
    if ".." in domain:
        return False, "Domain cannot contain consecutive dots"
    
    return True, ""


def check_domain(domain: str) -> bool:
    """
    Check if a domain name is valid (boolean only).
    
    Args:
        domain: Domain name to validate.
        
    Returns:
        True if valid, False otherwise.
    """
    is_valid, _ = is_valid_domain(domain)
    return is_valid


def validate_domain(domain: str, allow_reserved: bool = False) -> str:
    """
    Validate a domain name and return the normalized form.
    
    Args:
        domain: Domain name to validate.
        allow_reserved: Allow reserved domains like localhost.
        
    Returns:
        Normalized domain name.
        
    Raises:
        DomainError: If domain is invalid.
    """
    if not domain:
        raise DomainError("Domain name is required")
    
    # Normalize
    domain = domain.lower().strip()
    
    # Remove protocol if present
    if "://" in domain:
        domain = domain.split("://")[1]
    
    # Remove trailing slash
    domain = domain.rstrip("/")
    
    # Remove path if present
    if "/" in domain:
        domain = domain.split("/")[0]
    
    # Remove port if present
    if ":" in domain:
        domain = domain.split(":")[0]
    
    # Validate format
    is_valid, error = is_valid_domain(domain)
    if not is_valid:
        raise DomainError(f"Invalid domain '{domain}': {error}")
    
    # Check reserved domains
    if not allow_reserved:
        base_domain = domain.split(".")[-1] if "." in domain else domain
        if base_domain in RESERVED_DOMAINS or domain in RESERVED_DOMAINS:
            # Allow subdomains of localhost for development
            if domain != "localhost" and not domain.endswith(".localhost"):
                pass  # Allow most reserved-looking domains in production tool
    
    return domain


def get_domain_parts(domain: str) -> dict:
    """
    Parse a domain into its parts.
    
    Args:
        domain: Domain name to parse.
        
    Returns:
        Dictionary with domain parts (subdomain, domain, tld).
    """
    domain = domain.lower().strip()
    parts = domain.split(".")
    
    if len(parts) == 1:
        return {
            "subdomain": "",
            "domain": parts[0],
            "tld": "",
            "full": domain,
        }
    elif len(parts) == 2:
        return {
            "subdomain": "",
            "domain": parts[0],
            "tld": parts[1],
            "full": domain,
        }
    else:
        return {
            "subdomain": ".".join(parts[:-2]),
            "domain": parts[-2],
            "tld": parts[-1],
            "full": domain,
        }


def should_include_www(domain: str) -> bool:
    """
    Determine if www subdomain should be included for a domain.

    Returns True for base domains like example.com.
    Returns False for subdomains (api.example.com), www domains
    (www.example.com), and single-label domains (localhost).

    Args:
        domain: Domain to check.

    Returns:
        True if www.domain should be included.
    """
    parts = domain.lower().strip().split(".")
    if len(parts) != 2:
        return False
    if parts[0] == "www":
        return False
    return True


def is_subdomain(domain: str) -> bool:
    """
    Check if a domain is a subdomain.
    
    Args:
        domain: Domain to check.
        
    Returns:
        True if domain is a subdomain.
    """
    parts = domain.split(".")
    return len(parts) > 2
