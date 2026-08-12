"""
Source code manager for WASM.

Handles fetching source code from Git repositories, URLs, and local paths.
"""

import os
import shutil
import tarfile
import tempfile
import zipfile
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

from wasm.core.exceptions import SourceError
from wasm.core.utils import remove_directory
from wasm.managers.base_manager import BaseManager
from wasm.validators.source import (
    parse_git_url,
    validate_source,
)
from wasm.validators.ssh import (
    ensure_ssh_setup,
    is_ssh_url,
)


class SourceManager(BaseManager):
    """
    Manager for source code operations.

    Handles cloning Git repositories, downloading archives,
    and copying local directories.
    """

    def __init__(self, verbose: bool = False):
        """Initialize source manager."""
        super().__init__(verbose=verbose)

    def is_installed(self) -> bool:
        """Check if Git is installed."""
        result = self._run(["which", "git"])
        return result.success

    def get_version(self) -> str | None:
        """Get Git version."""
        result = self._run(["git", "--version"])
        if result.success:
            # git version 2.x.x
            parts = result.stdout.strip().split()
            if len(parts) >= 3:
                return parts[2]
        return None

    def fetch(
        self,
        source: str,
        destination: Path,
        branch: str | None = None,
        depth: int = 1,
        clean: bool = True,
        force: bool = False,
    ) -> bool:
        """
        Fetch source code from any supported source.

        Args:
            source: Source URL or path.
            destination: Destination directory.
            branch: Git branch (for Git sources).
            depth: Clone depth (for Git sources).
            clean: Remove destination if exists.
            force: Force update even if destination exists (for Git, does reset).

        Returns:
            True if fetch was successful.

        Raises:
            SourceError: If fetch fails.
        """
        # Validate source
        source_type, normalized = validate_source(source)

        # Handle force update for existing Git repos
        if force and destination.exists() and (destination / ".git").exists():
            self.logger.debug(f"Force updating Git repository: {destination}")
            return self._force_update_git(normalized, destination, branch)

        # Clean destination if requested
        if clean and destination.exists():
            self.logger.debug(f"Removing existing directory: {destination}")
            remove_directory(destination, sudo=True)

        # Ensure parent directory exists
        destination.parent.mkdir(parents=True, exist_ok=True)

        # Fetch based on source type
        if source_type == "git":
            return self.clone_git(normalized, destination, branch=branch, depth=depth)
        elif source_type == "archive":
            return self.download_archive(normalized, destination)
        elif source_type == "local":
            return self.copy_local(Path(normalized), destination)
        else:
            raise SourceError(f"Unsupported source type: {source_type}")

    def _force_update_git(
        self,
        url: str,
        destination: Path,
        branch: str | None = None,
    ) -> bool:
        """
        Force update a Git repository by fetching and resetting.

        Preserves untracked files like .env.

        Args:
            url: Git repository URL.
            destination: Repository path.
            branch: Branch to update to.

        Returns:
            True if update was successful.
        """
        # Ensure directory is marked as safe (handles dubious ownership)
        self._ensure_safe_directory(destination)

        # Update remote URL if different
        result = self._run(["git", "remote", "get-url", "origin"], cwd=destination)
        current_url = result.stdout.strip() if result.success else ""

        if current_url != url:
            self.logger.debug(f"Updating remote URL to: {url}")
            self._run(["git", "remote", "set-url", "origin", url], cwd=destination)

        # Fetch all branches
        result = self._run(["git", "fetch", "--all"], cwd=destination, timeout=300)
        if not result.success:
            raise SourceError("Git fetch failed", details=result.stderr)

        # Determine target branch
        if branch:
            target_ref = f"origin/{branch}"
        else:
            # Get default branch
            result = self._run(
                ["git", "symbolic-ref", "refs/remotes/origin/HEAD", "--short"], cwd=destination
            )
            if result.success:
                target_ref = result.stdout.strip()
            else:
                target_ref = "origin/main"  # Fallback

        # Reset to target (preserves untracked files)
        result = self._run(["git", "reset", "--hard", target_ref], cwd=destination)
        if not result.success:
            raise SourceError("Git reset failed", details=result.stderr)

        # Clean tracked files only
        self._run(["git", "clean", "-fd"], cwd=destination)

        return True

    def clone_git(
        self,
        url: str,
        destination: Path,
        branch: str | None = None,
        depth: int = 1,
        recursive: bool = True,
    ) -> bool:
        """
        Clone a Git repository.

        Args:
            url: Git repository URL.
            destination: Destination directory.
            branch: Branch to checkout.
            depth: Clone depth (0 for full history).
            recursive: Initialize submodules.

        Returns:
            True if clone was successful.

        Raises:
            SourceError: If clone fails.
            SSHError: If SSH authentication is not properly configured.
        """
        if not self.is_installed():
            raise SourceError("Git is not installed")

        # Parse URL for branch if specified with #
        parsed = parse_git_url(url)
        if parsed["branch"] and not branch:
            branch = parsed["branch"]
            url = url.split("#")[0]

        # Validate SSH setup for SSH URLs
        if is_ssh_url(url):
            self.logger.debug("Validating SSH configuration...")
            ensure_ssh_setup(url, auto_generate=True, verbose=self.verbose)

        # Build clone command
        cmd = ["git", "clone"]

        if depth > 0:
            cmd.extend(["--depth", str(depth)])

        if branch:
            cmd.extend(["--branch", branch])

        if recursive:
            cmd.append("--recursive")

        cmd.extend([url, str(destination)])

        self.logger.debug(f"Cloning: {url}")
        result = self._run(cmd, timeout=600)

        if not result.success:
            raise SourceError(
                f"Git clone failed: {url}",
                details=result.stderr,
            )

        return True

    def _ensure_safe_directory(self, path: Path) -> None:
        """
        Ensure a directory is marked as safe for Git operations.

        This handles the "dubious ownership" error that occurs when Git
        is run as root on a repository owned by another user.

        Args:
            path: Repository path to mark as safe.
        """
        # Check if already in safe.directory
        result = self._run(["git", "config", "--global", "--get-all", "safe.directory"])
        if result.success:
            safe_dirs = result.stdout.strip().split("\n")
            if str(path) in safe_dirs or "*" in safe_dirs:
                return

        # Add to safe.directory
        self.logger.debug(f"Adding to Git safe.directory: {path}")
        self._run(["git", "config", "--global", "--add", "safe.directory", str(path)])

    def pull(self, path: Path, branch: str | None = None) -> bool:
        """
        Pull latest changes in a Git repository.

        Handles common git errors:
        - Unstaged/uncommitted changes (stash and restore)
        - Dubious ownership (safe.directory)
        - Divergent branches (fetch + reset)
        - Merge conflicts (reset to remote)

        Args:
            path: Repository path.
            branch: Branch to pull.

        Returns:
            True if pull was successful.
        """
        if not (path / ".git").exists():
            raise SourceError(f"Not a Git repository: {path}")

        # Ensure directory is marked as safe (handles dubious ownership)
        self._ensure_safe_directory(path)

        # Check for local changes that would prevent pull
        has_changes = self._has_local_changes(path)
        stashed = False
        force_reset_used = False

        if has_changes:
            self.logger.debug("Local changes detected, stashing...")
            result = self._run(
                ["git", "stash", "push", "-m", "wasm-auto-stash-before-update"], cwd=path
            )
            if result.success and "No local changes" not in result.stdout:
                stashed = True
                self.logger.debug("Changes stashed successfully")

        try:
            # Checkout branch if specified
            if branch:
                result = self._run(["git", "checkout", branch], cwd=path)
                if not result.success:
                    # Branch might not exist locally, try fetching first
                    self._run(["git", "fetch", "origin", branch], cwd=path, timeout=300)
                    result = self._run(["git", "checkout", branch], cwd=path)
                    if not result.success:
                        raise SourceError(f"Failed to checkout branch: {branch}")

            # Try regular pull first
            result = self._run(["git", "pull", "--rebase"], cwd=path, timeout=300)

            if not result.success:
                # Analyze the error and try to recover
                error_msg = result.stderr.lower()

                if "unstaged changes" in error_msg or "uncommitted changes" in error_msg:
                    # This shouldn't happen if stash worked, but handle it anyway
                    self.logger.debug("Uncommitted changes blocking pull, forcing reset...")
                    force_reset_used = True
                    return self._force_pull_with_reset(path, branch)

                elif "divergent branches" in error_msg or "need to specify" in error_msg:
                    # Divergent history - fetch and reset to remote
                    self.logger.debug("Divergent branches detected, resetting to remote...")
                    force_reset_used = True
                    return self._force_pull_with_reset(path, branch)

                elif "conflict" in error_msg:
                    # Merge/rebase conflict - abort and reset
                    self.logger.debug("Conflict detected, aborting rebase and resetting...")
                    self._run(["git", "rebase", "--abort"], cwd=path)
                    force_reset_used = True
                    return self._force_pull_with_reset(path, branch)

                elif "refusing to merge unrelated histories" in error_msg:
                    # Unrelated histories - force reset
                    self.logger.debug("Unrelated histories, forcing reset...")
                    force_reset_used = True
                    return self._force_pull_with_reset(path, branch)

                else:
                    # Unknown error, try force reset as last resort
                    self.logger.debug(f"Pull failed with: {result.stderr}")
                    force_reset_used = True
                    return self._force_pull_with_reset(path, branch)

            return True

        finally:
            # Handle stashed changes
            if stashed:
                if force_reset_used:
                    # After a force reset, the stash is based on the old commit
                    # and will likely have conflicts. Drop it to avoid issues.
                    self.logger.debug("Force reset was used, dropping incompatible stash...")
                    self._run(["git", "stash", "drop"], cwd=path)
                else:
                    # Normal pull succeeded, try to restore stash
                    self.logger.debug("Restoring stashed changes...")
                    pop_result = self._run(["git", "stash", "pop"], cwd=path)
                    if not pop_result.success:
                        # Stash pop failed (likely conflicts), leave stash for manual handling
                        self.logger.debug(
                            "Could not auto-restore stashed changes (may have conflicts)"
                        )
                        self.logger.debug(
                            "Stashed changes preserved - run 'git stash pop' manually if needed"
                        )

    def _has_local_changes(self, path: Path) -> bool:
        """
        Check if repository has local changes (staged or unstaged).

        Args:
            path: Repository path.

        Returns:
            True if there are local changes.
        """
        # Check for staged and unstaged changes
        result = self._run(["git", "status", "--porcelain"], cwd=path)
        if result.success and result.stdout.strip():
            return True
        return False

    def _force_pull_with_reset(self, path: Path, branch: str | None = None) -> bool:
        """
        Force pull by fetching and resetting to remote.

        This is a more aggressive approach when normal pull fails.
        Preserves untracked files like .env.

        Args:
            path: Repository path.
            branch: Target branch.

        Returns:
            True if successful.
        """
        # Fetch all from remote
        result = self._run(["git", "fetch", "--all"], cwd=path, timeout=300)
        if not result.success:
            raise SourceError("Git fetch failed", details=result.stderr)

        # Determine target reference
        if branch:
            target_ref = f"origin/{branch}"
        else:
            # Get current branch
            result = self._run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=path)
            current_branch = result.stdout.strip() if result.success else "main"
            target_ref = f"origin/{current_branch}"

        # Reset hard to remote (preserves untracked files)
        result = self._run(["git", "reset", "--hard", target_ref], cwd=path)
        if not result.success:
            raise SourceError("Git reset failed", details=result.stderr)

        # Clean only tracked files (not untracked like .env)
        self._run(["git", "clean", "-fd"], cwd=path)

        return True

    def download_archive(self, url: str, destination: Path) -> bool:
        """
        Download and extract an archive.

        Args:
            url: Archive URL.
            destination: Destination directory.

        Returns:
            True if download and extraction was successful.

        Raises:
            SourceError: If download or extraction fails.
        """
        self.logger.debug(f"Downloading: {url}")

        # Determine archive type
        url_lower = url.lower()

        try:
            with tempfile.NamedTemporaryFile(delete=False) as tmp_file:
                # Download
                try:
                    with urlopen(url, timeout=300) as response:
                        tmp_file.write(response.read())
                except URLError as e:
                    raise SourceError(f"Download failed: {e}")

                tmp_path = tmp_file.name

            # Create destination
            destination.mkdir(parents=True, exist_ok=True)

            # Extract based on type
            if url_lower.endswith(".zip"):
                with zipfile.ZipFile(tmp_path, "r") as zf:
                    zf.extractall(destination)
            elif url_lower.endswith((".tar.gz", ".tgz")):
                with tarfile.open(tmp_path, "r:gz") as tf:
                    tf.extractall(destination)
            elif url_lower.endswith(".tar.bz2"):
                with tarfile.open(tmp_path, "r:bz2") as tf:
                    tf.extractall(destination)
            elif url_lower.endswith(".tar.xz"):
                with tarfile.open(tmp_path, "r:xz") as tf:
                    tf.extractall(destination)
            else:
                raise SourceError(f"Unsupported archive format: {url}")

            # Check if extracted to subdirectory
            contents = list(destination.iterdir())
            if len(contents) == 1 and contents[0].is_dir():
                # Move contents up one level
                subdir = contents[0]
                for item in subdir.iterdir():
                    shutil.move(str(item), str(destination))
                subdir.rmdir()

            return True

        except Exception as e:
            raise SourceError(f"Archive extraction failed: {e}")
        finally:
            # Cleanup temp file
            if "tmp_path" in locals():
                os.unlink(tmp_path)

    def copy_local(self, source: Path, destination: Path) -> bool:
        """
        Copy a local directory.

        Args:
            source: Source directory.
            destination: Destination directory.

        Returns:
            True if copy was successful.

        Raises:
            SourceError: If copy fails.
        """
        if not source.exists():
            raise SourceError(f"Source path does not exist: {source}")

        if not source.is_dir():
            raise SourceError(f"Source is not a directory: {source}")

        self.logger.debug(f"Copying: {source} -> {destination}")

        try:
            # Use shutil.copytree
            shutil.copytree(
                source,
                destination,
                dirs_exist_ok=True,
                ignore=shutil.ignore_patterns(
                    ".git",
                    "node_modules",
                    "__pycache__",
                    ".venv",
                    "venv",
                    ".env.local",
                ),
            )
            return True
        except Exception as e:
            raise SourceError(f"Copy failed: {e}")

    def get_repo_info(self, path: Path) -> dict:
        """
        Get information about a Git repository.

        Args:
            path: Repository path.

        Returns:
            Dictionary with repository information.
        """
        info = {
            "is_git": False,
            "branch": None,
            "remote": None,
            "commit": None,
            "dirty": False,
        }

        if not (path / ".git").exists():
            return info

        info["is_git"] = True

        # Get current branch
        result = self._run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=path,
        )
        if result.success:
            info["branch"] = result.stdout.strip()

        # Get remote URL
        result = self._run(
            ["git", "config", "--get", "remote.origin.url"],
            cwd=path,
        )
        if result.success:
            info["remote"] = result.stdout.strip()

        # Get current commit
        result = self._run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=path,
        )
        if result.success:
            info["commit"] = result.stdout.strip()

        # Check if dirty
        result = self._run(["git", "status", "--porcelain"], cwd=path)
        if result.success:
            info["dirty"] = bool(result.stdout.strip())

        return info
