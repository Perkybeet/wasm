"""
Source validation for WASM.

Validates Git URLs, local paths, and other source formats.
"""

import os
import re
from pathlib import Path

from wasm.core.exceptions import SourceError

# Git URL patterns
GIT_SSH_PATTERN = re.compile(r"^git@(?P<host>[\w.-]+):(?P<path>[\w./-]+?)(?:\.git)?$")
GIT_HTTPS_PATTERN = re.compile(r"^https?://(?P<host>[\w.-]+)/(?P<path>[\w./-]+?)(?:\.git)?$")
GIT_PROTOCOL_PATTERN = re.compile(r"^git://(?P<host>[\w.-]+)/(?P<path>[\w./-]+?)(?:\.git)?$")


def is_git_url(source: str) -> bool:
    """
    Check if source is a Git URL.

    Args:
        source: Source string to check.

    Returns:
        True if source is a Git URL.
    """
    source = source.strip()

    # Check SSH format (git@...)
    if GIT_SSH_PATTERN.match(source):
        return True

    # Check HTTPS format
    if GIT_HTTPS_PATTERN.match(source):
        return True

    # Check git:// protocol
    if GIT_PROTOCOL_PATTERN.match(source):
        return True

    # Check for .git suffix with valid URL
    if source.endswith(".git"):
        return True

    return False


def is_local_path(source: str) -> bool:
    """
    Check if source is a local path.

    Args:
        source: Source string to check.

    Returns:
        True if source is a local path.
    """
    source = source.strip()

    # Expand user home
    if source.startswith("~"):
        source = os.path.expanduser(source)

    # Check if it's an absolute or relative path that exists
    path = Path(source)
    return path.exists() or source.startswith("/") or source.startswith("./")


def is_archive_url(source: str) -> bool:
    """
    Check if source is a downloadable archive URL.

    Args:
        source: Source string to check.

    Returns:
        True if source is an archive URL.
    """
    archive_extensions = [".tar.gz", ".tgz", ".tar.bz2", ".zip", ".tar.xz"]
    source_lower = source.lower()

    if not source_lower.startswith(("http://", "https://")):
        return False

    return any(source_lower.endswith(ext) for ext in archive_extensions)


def parse_git_url(url: str) -> dict:
    """
    Parse a Git URL into components.

    Args:
        url: Git URL to parse.

    Returns:
        Dictionary with host, owner, repo, and branch.
    """
    url = url.strip()
    result = {
        "host": "",
        "owner": "",
        "repo": "",
        "branch": None,
        "original": url,
    }

    # Handle branch specification (url#branch)
    if "#" in url:
        url, result["branch"] = url.rsplit("#", 1)

    # SSH format
    match = GIT_SSH_PATTERN.match(url)
    if match:
        result["host"] = match.group("host")
        path_parts = match.group("path").rstrip(".git").split("/")
        if len(path_parts) >= 2:
            result["owner"] = path_parts[0]
            result["repo"] = path_parts[1]
        elif len(path_parts) == 1:
            result["repo"] = path_parts[0]
        return result

    # HTTPS format
    match = GIT_HTTPS_PATTERN.match(url)
    if match:
        result["host"] = match.group("host")
        path_parts = match.group("path").rstrip(".git").split("/")
        if len(path_parts) >= 2:
            result["owner"] = path_parts[0]
            result["repo"] = path_parts[1]
        elif len(path_parts) == 1:
            result["repo"] = path_parts[0]
        return result

    return result


def validate_git_url(url: str) -> tuple[bool, str]:
    """
    Validate a Git URL format.

    Args:
        url: Git URL to validate.

    Returns:
        Tuple of (is_valid, error_message).
    """
    if not url:
        return False, "Git URL cannot be empty"

    url = url.strip()

    # Check for valid patterns
    if not is_git_url(url):
        return False, "Invalid Git URL format"

    # Parse and check components
    parsed = parse_git_url(url)
    if not parsed["host"]:
        return False, "Could not determine Git host"

    if not parsed["repo"]:
        return False, "Could not determine repository name"

    return True, ""


def validate_local_path(path: str) -> tuple[bool, str]:
    """
    Validate a local path.

    Args:
        path: Local path to validate.

    Returns:
        Tuple of (is_valid, error_message).
    """
    if not path:
        return False, "Path cannot be empty"

    # Expand user home
    if path.startswith("~"):
        path = os.path.expanduser(path)

    path_obj = Path(path)

    if not path_obj.exists():
        return False, f"Path does not exist: {path}"

    if not path_obj.is_dir():
        return False, f"Path is not a directory: {path}"

    # Check for common project files
    project_indicators = [
        "package.json",
        "requirements.txt",
        "pyproject.toml",
        "index.html",
        "Cargo.toml",
        "go.mod",
    ]

    has_project_file = any((path_obj / f).exists() for f in project_indicators)
    if not has_project_file:
        return False, "Directory doesn't appear to contain a valid project"

    return True, ""


def validate_source(
    source: str,
    must_exist: bool = False,
) -> tuple[str, str]:
    """
    Validate and identify a source.

    Args:
        source: Source to validate (Git URL or local path).
        must_exist: For local paths, require the path to exist.

    Returns:
        Tuple of (source_type, normalized_source).
        source_type is one of: "git", "local", "archive".

    Raises:
        SourceError: If source is invalid.
    """
    if not source:
        raise SourceError("Source is required")

    source = source.strip()

    # Check if it's a Git URL
    if is_git_url(source):
        is_valid, error = validate_git_url(source)
        if not is_valid:
            raise SourceError(f"Invalid Git URL: {error}")
        return "git", source

    # Check if it's an archive URL
    if is_archive_url(source):
        return "archive", source

    # Check if it's a local path
    if is_local_path(source):
        # Expand user home
        if source.startswith("~"):
            source = os.path.expanduser(source)

        source = str(Path(source).resolve())

        if must_exist:
            is_valid, error = validate_local_path(source)
            if not is_valid:
                raise SourceError(error)

        return "local", source

    # Could be a GitHub shorthand (user/repo)
    if re.match(r"^[\w.-]+/[\w.-]+$", source):
        # Convert to GitHub URL
        github_url = f"https://github.com/{source}.git"
        return "git", github_url

    raise SourceError(
        f"Invalid source: '{source}'",
        details="Source must be a Git URL (SSH or HTTPS), local path, or GitHub shorthand (user/repo)",
    )


def get_repo_name(source: str) -> str:
    """
    Extract repository/project name from source.

    Args:
        source: Source URL or path.

    Returns:
        Repository name.
    """
    source = source.strip()

    # Git URL
    if is_git_url(source):
        parsed = parse_git_url(source)
        return parsed["repo"] or "app"

    # Local path
    if source.startswith("~"):
        source = os.path.expanduser(source)

    return Path(source).name or "app"


def is_valid_source(source: str) -> bool:
    """
    Check if source is valid (Git URL or local path).

    Args:
        source: Source string to validate.

    Returns:
        True if source is valid, False otherwise.
    """
    if not source or not source.strip():
        return False

    source = source.strip()

    # Check if it's a Git URL
    if is_git_url(source):
        is_valid, _ = validate_git_url(source)
        return is_valid

    # Check if it's an archive URL
    if is_archive_url(source):
        return True

    # Check if it's a local path
    if is_local_path(source):
        return True

    return False
