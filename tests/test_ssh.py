"""Tests for SSH validator."""

import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock

from wasm.validators.ssh import (
    is_ssh_url,
    get_host_from_git_url,
    validate_ssh_setup_for_url,
)


class TestIsSSHUrl:
    """Tests for is_ssh_url function."""
    
    def test_ssh_urls(self):
        """Test SSH URL detection."""
        assert is_ssh_url("git@github.com:user/repo.git") is True
        assert is_ssh_url("git@gitlab.com:user/repo.git") is True
        assert is_ssh_url("ssh://git@github.com/user/repo.git") is True
    
    def test_non_ssh_urls(self):
        """Test non-SSH URL detection."""
        assert is_ssh_url("https://github.com/user/repo.git") is False
        assert is_ssh_url("http://github.com/user/repo.git") is False
        assert is_ssh_url("/local/path/repo") is False


class TestGetHostFromGitUrl:
    """Tests for get_host_from_git_url function."""
    
    def test_ssh_urls(self):
        """Test host extraction from SSH URLs."""
        assert get_host_from_git_url("git@github.com:user/repo.git") == "github.com"
        assert get_host_from_git_url("git@gitlab.com:org/repo.git") == "gitlab.com"
        assert get_host_from_git_url("git@bitbucket.org:team/repo.git") == "bitbucket.org"
    
    def test_https_urls(self):
        """Test host extraction from HTTPS URLs."""
        assert get_host_from_git_url("https://github.com/user/repo.git") == "github.com"
        assert get_host_from_git_url("https://gitlab.com/user/repo") == "gitlab.com"
    
    def test_invalid_urls(self):
        """Test invalid URL handling."""
        assert get_host_from_git_url("invalid") is None
        assert get_host_from_git_url("") is None


class TestValidateSSHSetupForUrl:
    """Tests for validate_ssh_setup_for_url function."""
    
    def test_https_url_always_valid(self):
        """Test that HTTPS URLs are always valid (no SSH needed)."""
        result = validate_ssh_setup_for_url("https://github.com/user/repo.git")
        assert result["valid"] is True
        assert result["is_ssh"] is False
    
    @patch("wasm.validators.ssh.ssh_key_exists")
    def test_ssh_url_without_key(self, mock_key_exists):
        """Test SSH URL without SSH key."""
        mock_key_exists.return_value = (False, None)
        
        result = validate_ssh_setup_for_url("git@github.com:user/repo.git")
        
        assert result["valid"] is False
        assert result["is_ssh"] is True
        assert result["has_ssh_key"] is False
        assert len(result["guidance"]) > 0
    
    @patch("wasm.validators.ssh.ssh_key_exists")
    @patch("wasm.validators.ssh.get_public_key")
    @patch("wasm.validators.ssh.test_ssh_connection")
    def test_ssh_url_with_working_key(self, mock_test, mock_pubkey, mock_key_exists):
        """Test SSH URL with working SSH key."""
        mock_key_exists.return_value = (True, Path("/home/user/.ssh/id_ed25519"))
        mock_pubkey.return_value = "ssh-ed25519 AAAA... user@host"
        mock_test.return_value = (True, "SSH connection successful")
        
        result = validate_ssh_setup_for_url("git@github.com:user/repo.git")
        
        assert result["valid"] is True
        assert result["has_ssh_key"] is True
        assert result["connection_success"] is True
