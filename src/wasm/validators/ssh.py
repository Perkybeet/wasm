"""
SSH configuration validation and setup helpers for WASM.

Validates SSH keys, connectivity, and provides setup guidance.
"""

import os
import re
from pathlib import Path
from typing import Optional, Tuple, List, Dict

from wasm.core.utils import run_command
from wasm.core.exceptions import SSHError


# Default SSH key paths
DEFAULT_SSH_DIR = Path.home() / ".ssh"
DEFAULT_KEY_TYPES = ["id_ed25519", "id_rsa", "id_ecdsa"]


def get_ssh_directory() -> Path:
    """
    Get the SSH directory path.
    
    Returns:
        Path to ~/.ssh directory.
    """
    return DEFAULT_SSH_DIR


def ssh_key_exists() -> Tuple[bool, Optional[Path]]:
    """
    Check if any SSH key exists.
    
    Returns:
        Tuple of (exists, key_path).
    """
    ssh_dir = get_ssh_directory()
    
    if not ssh_dir.exists():
        return False, None
    
    for key_type in DEFAULT_KEY_TYPES:
        key_path = ssh_dir / key_type
        if key_path.exists():
            return True, key_path
    
    return False, None


def get_all_ssh_keys() -> List[Path]:
    """
    Get all existing SSH private keys.
    
    Returns:
        List of paths to SSH private keys.
    """
    ssh_dir = get_ssh_directory()
    keys = []
    
    if not ssh_dir.exists():
        return keys
    
    for key_type in DEFAULT_KEY_TYPES:
        key_path = ssh_dir / key_type
        if key_path.exists():
            keys.append(key_path)
    
    return keys


def get_public_key(private_key_path: Optional[Path] = None) -> Optional[str]:
    """
    Get the public key content.
    
    Args:
        private_key_path: Path to private key (optional, auto-detects if not provided).
        
    Returns:
        Public key content or None.
    """
    if private_key_path is None:
        exists, key_path = ssh_key_exists()
        if not exists or key_path is None:
            return None
        private_key_path = key_path
    
    public_key_path = Path(str(private_key_path) + ".pub")
    
    if not public_key_path.exists():
        return None
    
    try:
        return public_key_path.read_text().strip()
    except Exception:
        return None


def generate_ssh_key(
    key_type: str = "ed25519",
    comment: Optional[str] = None,
    passphrase: str = "",
) -> Tuple[bool, Optional[Path], str]:
    """
    Generate a new SSH key pair.
    
    Args:
        key_type: Type of key (ed25519, rsa, ecdsa).
        comment: Comment for the key.
        passphrase: Passphrase for the key (empty for no passphrase).
        
    Returns:
        Tuple of (success, key_path, message).
    """
    ssh_dir = get_ssh_directory()
    
    # Ensure .ssh directory exists with proper permissions
    if not ssh_dir.exists():
        try:
            ssh_dir.mkdir(mode=0o700, parents=True)
        except Exception as e:
            return False, None, f"Failed to create SSH directory: {e}"
    
    # Determine key path
    key_name = f"id_{key_type}"
    key_path = ssh_dir / key_name
    
    # Check if key already exists
    if key_path.exists():
        return True, key_path, "SSH key already exists"
    
    # Build command
    cmd = ["ssh-keygen", "-t", key_type, "-f", str(key_path), "-N", passphrase]
    
    if comment:
        cmd.extend(["-C", comment])
    
    # Generate key
    result = run_command(cmd)
    
    if result.success:
        # Set proper permissions
        try:
            key_path.chmod(0o600)
            pub_key_path = Path(str(key_path) + ".pub")
            if pub_key_path.exists():
                pub_key_path.chmod(0o644)
        except Exception:
            pass
        
        return True, key_path, "SSH key generated successfully"
    else:
        return False, None, f"Failed to generate SSH key: {result.stderr}"


def test_ssh_connection(host: str = "github.com", timeout: int = 10) -> Tuple[bool, str]:
    """
    Test SSH connection to a host.
    
    Args:
        host: Host to test connection to.
        timeout: Connection timeout in seconds.
        
    Returns:
        Tuple of (success, message).
    """
    # Use -T for no pseudo-terminal allocation
    # Use -o StrictHostKeyChecking=accept-new to auto-accept new hosts
    cmd = [
        "ssh",
        "-T",
        "-o", f"ConnectTimeout={timeout}",
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", "BatchMode=yes",
        f"git@{host}",
    ]
    
    result = run_command(cmd, timeout=timeout + 5)
    
    # GitHub returns exit code 1 but says "successfully authenticated"
    # GitLab returns "Welcome to GitLab"
    output = result.stdout + result.stderr
    
    if "successfully authenticated" in output.lower():
        return True, "SSH connection successful"
    if "welcome to gitlab" in output.lower():
        return True, "SSH connection successful"
    if "permission denied" in output.lower():
        return False, "Permission denied - SSH key not authorized"
    if "host key verification failed" in output.lower():
        return False, "Host key verification failed"
    if "connection refused" in output.lower():
        return False, "Connection refused"
    if "connection timed out" in output.lower():
        return False, "Connection timed out"
    if "could not resolve hostname" in output.lower():
        return False, f"Could not resolve hostname: {host}"
    
    # If we got here with exit code 1, it might still be success for GitHub
    if result.exit_code == 1 and "git@" in output:
        return True, "SSH connection successful"
    
    return False, f"SSH connection failed: {output}"


def get_host_from_git_url(url: str) -> Optional[str]:
    """
    Extract the host from a Git SSH URL.
    
    Args:
        url: Git URL (e.g., git@github.com:user/repo.git)
        
    Returns:
        Host name or None.
    """
    # SSH format: git@host:path
    ssh_match = re.match(r"^git@([\w.-]+):", url)
    if ssh_match:
        return ssh_match.group(1)
    
    # HTTPS format: https://host/path
    https_match = re.match(r"^https?://([\w.-]+)/", url)
    if https_match:
        return https_match.group(1)
    
    return None


def is_ssh_url(url: str) -> bool:
    """
    Check if a Git URL uses SSH protocol.
    
    Args:
        url: Git URL to check.
        
    Returns:
        True if URL uses SSH.
    """
    return url.startswith("git@") or url.startswith("ssh://")


def validate_ssh_setup_for_url(url: str) -> Dict:
    """
    Validate SSH setup for a Git URL and return detailed status.
    
    Args:
        url: Git URL to validate access for.
        
    Returns:
        Dictionary with validation results and guidance.
    """
    result = {
        "valid": False,
        "is_ssh": is_ssh_url(url),
        "host": get_host_from_git_url(url),
        "has_ssh_key": False,
        "key_path": None,
        "public_key": None,
        "connection_tested": False,
        "connection_success": False,
        "message": "",
        "guidance": [],
    }
    
    # If not SSH URL, no SSH setup needed
    if not result["is_ssh"]:
        result["valid"] = True
        result["message"] = "URL uses HTTPS, no SSH setup required"
        return result
    
    # Check if SSH key exists
    key_exists, key_path = ssh_key_exists()
    result["has_ssh_key"] = key_exists
    result["key_path"] = str(key_path) if key_path else None
    
    if not key_exists:
        result["message"] = "No SSH key found"
        result["guidance"] = [
            "You need to set up SSH authentication to use this repository.",
            "",
            "Option 1: Generate a new SSH key",
            "  Run: wasm setup ssh",
            "  Or manually: ssh-keygen -t ed25519",
            "",
            "Option 2: Use HTTPS URL instead",
            f"  Convert your URL to: https://{result['host']}/...",
        ]
        return result
    
    # Get public key for display
    result["public_key"] = get_public_key(key_path)
    
    # Test connection
    if result["host"]:
        result["connection_tested"] = True
        conn_success, conn_msg = test_ssh_connection(result["host"])
        result["connection_success"] = conn_success
        
        if conn_success:
            result["valid"] = True
            result["message"] = f"SSH authentication to {result['host']} is working"
        else:
            result["message"] = conn_msg
            result["guidance"] = [
                f"SSH key exists but authentication to {result['host']} failed.",
                "",
                "You need to add your public key to your Git provider:",
                "",
                f"Your public key ({key_path}.pub):",
                "─" * 60,
                result["public_key"] or "(could not read public key)",
                "─" * 60,
                "",
                _get_provider_instructions(result["host"]),
                "",
                "After adding the key, run your command again.",
            ]
    else:
        result["message"] = "Could not determine host from URL"
        result["guidance"] = ["Invalid Git URL format"]
    
    return result


def _get_provider_instructions(host: str) -> str:
    """
    Get provider-specific instructions for adding SSH keys.
    
    Args:
        host: Git host (github.com, gitlab.com, etc.)
        
    Returns:
        Instructions string.
    """
    instructions = {
        "github.com": (
            "To add your key to GitHub:\n"
            "  1. Go to https://github.com/settings/keys\n"
            "  2. Click 'New SSH key'\n"
            "  3. Paste your public key and save"
        ),
        "gitlab.com": (
            "To add your key to GitLab:\n"
            "  1. Go to https://gitlab.com/-/user_settings/ssh_keys\n"
            "  2. Paste your public key\n"
            "  3. Click 'Add key'"
        ),
        "bitbucket.org": (
            "To add your key to Bitbucket:\n"
            "  1. Go to https://bitbucket.org/account/settings/ssh-keys/\n"
            "  2. Click 'Add key'\n"
            "  3. Paste your public key and save"
        ),
    }
    
    if host in instructions:
        return instructions[host]
    
    return (
        f"To add your key to {host}:\n"
        "  1. Go to your Git provider's SSH key settings\n"
        "  2. Add a new SSH key\n"
        "  3. Paste your public key and save"
    )


def ensure_ssh_setup(
    url: str,
    auto_generate: bool = False,
    verbose: bool = False,
) -> Tuple[bool, str, Optional[str]]:
    """
    Ensure SSH is properly configured for a Git URL.
    
    Args:
        url: Git URL to validate.
        auto_generate: Automatically generate SSH key if missing.
        verbose: Show verbose output.
        
    Returns:
        Tuple of (ready, message, public_key).
        
    Raises:
        SSHError: If SSH setup is incomplete and cannot be resolved.
    """
    from wasm.core.logger import Logger
    
    logger = Logger(verbose=verbose)
    
    # Validate current setup
    validation = validate_ssh_setup_for_url(url)
    
    if validation["valid"]:
        return True, validation["message"], validation["public_key"]
    
    # If no SSH key and auto_generate is True, create one
    if not validation["has_ssh_key"] and auto_generate:
        logger.info("No SSH key found. Generating new key...")
        
        hostname = os.uname().nodename
        success, key_path, msg = generate_ssh_key(
            key_type="ed25519",
            comment=f"wasm@{hostname}",
        )
        
        if success and key_path:
            validation = validate_ssh_setup_for_url(url)
            validation["guidance"].insert(0, f"✓ New SSH key generated: {key_path}")
    
    # Build error with guidance
    guidance_text = "\n".join(validation["guidance"])
    
    raise SSHError(
        validation["message"],
        details=guidance_text,
    )
