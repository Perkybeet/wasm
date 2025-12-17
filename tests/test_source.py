"""Tests for source validator."""

import pytest
from pathlib import Path
from unittest.mock import patch

from wasm.validators.source import (
    is_valid_source,
    validate_source,
    is_git_url,
    is_local_path,
    is_archive_url,
    parse_git_url,
    get_repo_name,
)
from wasm.core.exceptions import SourceError


class TestIsGitUrl:
    """Tests for is_git_url function."""
    
    def test_ssh_git_urls(self):
        """Test SSH Git URLs."""
        assert is_git_url("git@github.com:user/repo.git") is True
        assert is_git_url("git@gitlab.com:user/repo.git") is True
        assert is_git_url("git@bitbucket.org:user/repo.git") is True
    
    def test_https_git_urls(self):
        """Test HTTPS Git URLs."""
        assert is_git_url("https://github.com/user/repo.git") is True
        assert is_git_url("https://github.com/user/repo") is True
        assert is_git_url("https://gitlab.com/user/repo.git") is True
    
    def test_git_protocol_urls(self):
        """Test git:// protocol URLs."""
        assert is_git_url("git://github.com/user/repo.git") is True
    
    def test_invalid_git_urls(self):
        """Test invalid Git URLs."""
        assert is_git_url("not-a-url") is False
        assert is_git_url("ftp://example.com/repo") is False


class TestIsLocalPath:
    """Tests for is_local_path function."""
    
    def test_absolute_paths(self):
        """Test absolute path detection."""
        assert is_local_path("/path/to/project") is True
        assert is_local_path("/var/www/apps") is True
    
    def test_relative_paths(self):
        """Test relative path detection."""
        assert is_local_path("./project") is True
    
    def test_home_paths(self):
        """Test home directory paths."""
        assert is_local_path("~/projects/app") is True


class TestIsArchiveUrl:
    """Tests for is_archive_url function."""
    
    def test_archive_urls(self):
        """Test archive URL detection."""
        assert is_archive_url("https://example.com/app.tar.gz") is True
        assert is_archive_url("https://example.com/app.zip") is True
        assert is_archive_url("https://example.com/app.tgz") is True
    
    def test_non_archive_urls(self):
        """Test non-archive URLs."""
        assert is_archive_url("https://github.com/user/repo") is False
        assert is_archive_url("git@github.com:user/repo.git") is False


class TestParseGitUrl:
    """Tests for parse_git_url function."""
    
    def test_ssh_url_parsing(self):
        """Test parsing SSH Git URL."""
        result = parse_git_url("git@github.com:user/repo.git")
        assert result["host"] == "github.com"
        assert result["owner"] == "user"
        assert result["repo"] == "repo"
    
    def test_https_url_parsing(self):
        """Test parsing HTTPS Git URL."""
        result = parse_git_url("https://github.com/user/repo.git")
        assert result["host"] == "github.com"
        assert result["owner"] == "user"
        assert result["repo"] == "repo"
    
    def test_url_with_branch(self):
        """Test parsing URL with branch specifier."""
        result = parse_git_url("git@github.com:user/repo.git#develop")
        assert result["branch"] == "develop"
        assert result["repo"] == "repo"


class TestValidateSource:
    """Tests for validate_source function."""
    
    def test_valid_git_url(self):
        """Test validation of valid Git URLs."""
        source_type, normalized = validate_source("git@github.com:user/repo.git")
        assert source_type == "git"
        assert normalized == "git@github.com:user/repo.git"
    
    def test_valid_https_url(self):
        """Test validation of HTTPS Git URLs."""
        source_type, normalized = validate_source("https://github.com/user/repo.git")
        assert source_type == "git"
    
    def test_github_shorthand(self):
        """Test GitHub shorthand conversion."""
        source_type, normalized = validate_source("user/repo")
        assert source_type == "git"
        assert "github.com" in normalized
    
    def test_empty_source_raises_error(self):
        """Test that empty source raises SourceError."""
        with pytest.raises(SourceError):
            validate_source("")
    
    def test_invalid_source_raises_error(self):
        """Test that invalid source raises SourceError."""
        with pytest.raises(SourceError):
            validate_source("not-a-valid-source!!!")


class TestGetRepoName:
    """Tests for get_repo_name function."""
    
    def test_repo_name_from_ssh_url(self):
        """Test extracting repo name from SSH URL."""
        assert get_repo_name("git@github.com:user/my-repo.git") == "my-repo"
    
    def test_repo_name_from_https_url(self):
        """Test extracting repo name from HTTPS URL."""
        assert get_repo_name("https://github.com/user/my-app.git") == "my-app"


class TestIsValidSource:
    """Tests for is_valid_source function."""
    
    def test_valid_sources(self):
        """Test valid sources return True."""
        assert is_valid_source("git@github.com:user/repo.git") is True
        assert is_valid_source("https://github.com/user/repo") is True
    
    def test_invalid_sources(self):
        """Test invalid sources return False."""
        assert is_valid_source("") is False
        assert is_valid_source("   ") is False
