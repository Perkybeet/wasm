"""
Backup manager for WASM.

Handles creating, listing, restoring, and managing backups
for deployed web applications.
"""

import hashlib
import json
import shutil
import tarfile
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Any

from wasm.core.config import Config, DEFAULT_APPS_DIR
from wasm.core.exceptions import WASMError
from wasm.core.logger import Logger
from wasm.core.utils import (
    run_command,
    run_command_sudo,
    remove_directory,
    domain_to_app_name,
)
from wasm.managers.service_manager import ServiceManager


class BackupError(WASMError):
    """Exception raised for backup-related errors."""
    pass


@dataclass
class BackupMetadata:
    """Metadata for a backup."""
    
    id: str
    domain: str
    app_name: str
    created_at: str
    size_bytes: int
    app_type: str
    version: str
    description: str
    includes_env: bool
    includes_node_modules: bool
    git_commit: Optional[str] = None
    git_branch: Optional[str] = None
    checksum: Optional[str] = None
    tags: List[str] = field(default_factory=list)
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            "id": self.id,
            "domain": self.domain,
            "app_name": self.app_name,
            "created_at": self.created_at,
            "size_bytes": self.size_bytes,
            "app_type": self.app_type,
            "version": self.version,
            "description": self.description,
            "includes_env": self.includes_env,
            "includes_node_modules": self.includes_node_modules,
            "git_commit": self.git_commit,
            "git_branch": self.git_branch,
            "checksum": self.checksum,
            "tags": self.tags,
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "BackupMetadata":
        """Create from dictionary."""
        return cls(
            id=data["id"],
            domain=data["domain"],
            app_name=data["app_name"],
            created_at=data["created_at"],
            size_bytes=data.get("size_bytes", 0),
            app_type=data.get("app_type", "unknown"),
            version=data.get("version", "1.0.0"),
            description=data.get("description", ""),
            includes_env=data.get("includes_env", False),
            includes_node_modules=data.get("includes_node_modules", False),
            git_commit=data.get("git_commit"),
            git_branch=data.get("git_branch"),
            checksum=data.get("checksum"),
            tags=data.get("tags", []),
        )
    
    @property
    def size_human(self) -> str:
        """Get human-readable size."""
        size = self.size_bytes
        for unit in ["B", "KB", "MB", "GB"]:
            if size < 1024:
                return f"{size:.1f} {unit}"
            size /= 1024
        return f"{size:.1f} TB"
    
    @property
    def age(self) -> str:
        """Get human-readable age."""
        try:
            created = datetime.fromisoformat(self.created_at)
            delta = datetime.now() - created
            
            if delta.days > 30:
                return f"{delta.days // 30} months ago"
            elif delta.days > 0:
                return f"{delta.days} days ago"
            elif delta.seconds > 3600:
                return f"{delta.seconds // 3600} hours ago"
            elif delta.seconds > 60:
                return f"{delta.seconds // 60} minutes ago"
            else:
                return "just now"
        except Exception:
            return "unknown"


class BackupManager:
    """
    Manager for application backups.
    
    Handles creating, listing, restoring, and managing backups
    for WASM-deployed applications.
    """
    
    # Default backup directory
    DEFAULT_BACKUP_DIR = Path("/var/backups/wasm")
    
    # Files/directories to always exclude from backups
    DEFAULT_EXCLUDES = [
        "node_modules",
        ".git",
        "__pycache__",
        "*.pyc",
        ".next/cache",
        ".nuxt",
        "dist",
        "build",
        ".cache",
        "*.log",
        "*.tmp",
        ".DS_Store",
        "Thumbs.db",
    ]
    
    # Files to optionally include (excluded by default for size)
    OPTIONAL_INCLUDES = [
        "node_modules",
        ".next",
        "dist",
        "build",
    ]
    
    # Maximum backups to keep per app (default)
    DEFAULT_MAX_BACKUPS = 10
    
    # Backup format version
    BACKUP_VERSION = "1.0.0"
    
    def __init__(self, verbose: bool = False):
        """Initialize backup manager."""
        self.verbose = verbose
        self.logger = Logger(verbose=verbose)
        self.config = Config()
        self.service_manager = ServiceManager(verbose=verbose)
        
        # Get backup directory from config or use default
        self.backup_dir = Path(
            self.config.get("backup.directory", str(self.DEFAULT_BACKUP_DIR))
        )
        self.max_backups = self.config.get("backup.max_per_app", self.DEFAULT_MAX_BACKUPS)
    
    def _run(self, command: list, cwd=None, env=None, timeout=None):
        """Execute a command."""
        self.logger.debug(f"Running: {' '.join(str(c) for c in command)}")
        return run_command(command, cwd=cwd, env=env, timeout=timeout)
    
    def _ensure_backup_dir(self) -> None:
        """Ensure backup directory exists with proper permissions."""
        if not self.backup_dir.exists():
            result = run_command_sudo(["mkdir", "-p", str(self.backup_dir)])
            if not result.success:
                raise BackupError(f"Failed to create backup directory: {self.backup_dir}")
            
            # Set permissions
            run_command_sudo(["chmod", "750", str(self.backup_dir)])
    
    def _get_app_backup_dir(self, app_name: str) -> Path:
        """Get backup directory for a specific app."""
        return self.backup_dir / app_name
    
    def _generate_backup_id(self, domain: str) -> str:
        """Generate unique backup ID."""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        return f"{domain.replace('.', '-')}_{timestamp}"
    
    def _calculate_checksum(self, file_path: Path) -> str:
        """Calculate SHA256 checksum of a file."""
        sha256 = hashlib.sha256()
        with open(file_path, "rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                sha256.update(chunk)
        return sha256.hexdigest()
    
    def _get_git_info(self, app_path: Path) -> tuple:
        """Get git commit and branch info."""
        git_commit = None
        git_branch = None
        
        if (app_path / ".git").exists():
            # Get current commit
            result = self._run(["git", "rev-parse", "HEAD"], cwd=app_path)
            if result.success:
                git_commit = result.stdout.strip()[:12]
            
            # Get current branch
            result = self._run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=app_path)
            if result.success:
                git_branch = result.stdout.strip()
        
        return git_commit, git_branch
    
    def _detect_app_type(self, app_path: Path) -> str:
        """Detect application type from files."""
        if (app_path / "next.config.js").exists() or (app_path / "next.config.mjs").exists():
            return "nextjs"
        elif (app_path / "vite.config.js").exists() or (app_path / "vite.config.ts").exists():
            return "vite"
        elif (app_path / "requirements.txt").exists() or (app_path / "pyproject.toml").exists():
            return "python"
        elif (app_path / "package.json").exists():
            return "nodejs"
        elif (app_path / "index.html").exists():
            return "static"
        return "unknown"
    
    def _build_exclude_list(
        self,
        include_node_modules: bool = False,
        include_build: bool = False,
        custom_excludes: Optional[List[str]] = None,
    ) -> List[str]:
        """Build list of patterns to exclude from backup."""
        excludes = self.DEFAULT_EXCLUDES.copy()
        
        if include_node_modules and "node_modules" in excludes:
            excludes.remove("node_modules")
        
        if include_build:
            for pattern in [".next/cache", "dist", "build"]:
                if pattern in excludes:
                    excludes.remove(pattern)
        
        if custom_excludes:
            excludes.extend(custom_excludes)
        
        return excludes
    
    def create(
        self,
        domain: str,
        description: str = "",
        include_env: bool = True,
        include_node_modules: bool = False,
        include_build: bool = False,
        tags: Optional[List[str]] = None,
        pre_backup_hook: Optional[str] = None,
    ) -> BackupMetadata:
        """
        Create a backup of an application.
        
        Args:
            domain: Domain name of the application.
            description: Optional description for the backup.
            include_env: Include .env files in backup.
            include_node_modules: Include node_modules (large!).
            include_build: Include build artifacts.
            tags: Optional tags for the backup.
            pre_backup_hook: Optional command to run before backup.
            
        Returns:
            BackupMetadata for the created backup.
            
        Raises:
            BackupError: If backup fails.
        """
        app_name = domain_to_app_name(domain)
        app_path = self.config.apps_directory / app_name
        
        if not app_path.exists():
            raise BackupError(f"Application not found: {domain}")
        
        self._ensure_backup_dir()
        
        # Create app backup directory
        app_backup_dir = self._get_app_backup_dir(app_name)
        if not app_backup_dir.exists():
            run_command_sudo(["mkdir", "-p", str(app_backup_dir)])
        
        # Generate backup ID
        backup_id = self._generate_backup_id(domain)
        backup_file = app_backup_dir / f"{backup_id}.tar.gz"
        metadata_file = app_backup_dir / f"{backup_id}.json"
        
        self.logger.debug(f"Creating backup: {backup_id}")
        
        # Run pre-backup hook if specified
        if pre_backup_hook:
            self.logger.debug(f"Running pre-backup hook: {pre_backup_hook}")
            result = self._run(["bash", "-c", pre_backup_hook], cwd=app_path)
            if not result.success:
                self.logger.warning(f"Pre-backup hook failed: {result.stderr}")
        
        # Get git info
        git_commit, git_branch = self._get_git_info(app_path)
        
        # Detect app type
        app_type = self._detect_app_type(app_path)
        
        # Build exclude list
        excludes = self._build_exclude_list(
            include_node_modules=include_node_modules,
            include_build=include_build,
        )
        
        # Handle .env files
        env_files = []
        if include_env:
            for env_file in app_path.glob(".env*"):
                if env_file.is_file():
                    env_files.append(env_file.name)
        else:
            excludes.extend([".env", ".env.*", ".env.local", ".env.production"])
        
        # Create tar archive with compression
        try:
            with tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False) as tmp:
                tmp_path = Path(tmp.name)
            
            # Build tar command with excludes
            tar_cmd = ["tar", "-czf", str(tmp_path)]
            for pattern in excludes:
                tar_cmd.extend(["--exclude", pattern])
            tar_cmd.extend(["-C", str(app_path.parent), app_name])
            
            self.logger.debug(f"Running: {' '.join(tar_cmd)}")
            result = run_command(tar_cmd, timeout=600)
            
            if not result.success:
                raise BackupError(f"Tar failed: {result.stderr}")
            
            # Move to backup location
            result = run_command_sudo(["mv", str(tmp_path), str(backup_file)])
            if not result.success:
                raise BackupError(f"Failed to move backup: {result.stderr}")
            
            # Set permissions
            run_command_sudo(["chmod", "640", str(backup_file)])
            
        except Exception as e:
            # Cleanup on failure
            if tmp_path.exists():
                tmp_path.unlink()
            raise BackupError(f"Backup creation failed: {e}")
        
        # Get backup size
        result = self._run(["stat", "-c", "%s", str(backup_file)])
        size_bytes = int(result.stdout.strip()) if result.success else 0
        
        # Calculate checksum
        # Read file as root to calculate checksum
        result = run_command_sudo(["sha256sum", str(backup_file)])
        checksum = result.stdout.split()[0] if result.success else None
        
        # Create metadata
        metadata = BackupMetadata(
            id=backup_id,
            domain=domain,
            app_name=app_name,
            created_at=datetime.now().isoformat(),
            size_bytes=size_bytes,
            app_type=app_type,
            version=self.BACKUP_VERSION,
            description=description,
            includes_env=include_env,
            includes_node_modules=include_node_modules,
            git_commit=git_commit,
            git_branch=git_branch,
            checksum=checksum,
            tags=tags or [],
        )
        
        # Save metadata
        metadata_content = json.dumps(metadata.to_dict(), indent=2)
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as tmp:
            tmp.write(metadata_content)
            tmp_meta_path = Path(tmp.name)
        
        run_command_sudo(["mv", str(tmp_meta_path), str(metadata_file)])
        run_command_sudo(["chmod", "644", str(metadata_file)])
        
        # Rotate old backups
        self._rotate_backups(app_name)
        
        self.logger.debug(f"Backup created: {backup_file} ({metadata.size_human})")
        
        return metadata
    
    def list_backups(
        self,
        domain: Optional[str] = None,
        tags: Optional[List[str]] = None,
        limit: Optional[int] = None,
    ) -> List[BackupMetadata]:
        """
        List backups for an application or all applications.
        
        Args:
            domain: Filter by domain (None for all).
            tags: Filter by tags.
            limit: Maximum number of backups to return.
            
        Returns:
            List of BackupMetadata objects.
        """
        backups = []
        
        if not self.backup_dir.exists():
            return backups
        
        # Determine which directories to scan
        if domain:
            app_name = domain_to_app_name(domain)
            dirs_to_scan = [self._get_app_backup_dir(app_name)]
        else:
            dirs_to_scan = [d for d in self.backup_dir.iterdir() if d.is_dir()]
        
        for app_dir in dirs_to_scan:
            if not app_dir.exists():
                continue
            
            for metadata_file in app_dir.glob("*.json"):
                try:
                    # Read metadata (may need sudo)
                    result = run_command_sudo(["cat", str(metadata_file)])
                    if result.success:
                        data = json.loads(result.stdout)
                        metadata = BackupMetadata.from_dict(data)
                        
                        # Check if backup file exists
                        backup_file = app_dir / f"{metadata.id}.tar.gz"
                        result = run_command_sudo(["test", "-f", str(backup_file)])
                        if not result.success:
                            continue
                        
                        # Filter by tags
                        if tags:
                            if not any(tag in metadata.tags for tag in tags):
                                continue
                        
                        backups.append(metadata)
                except Exception as e:
                    self.logger.debug(f"Error reading metadata {metadata_file}: {e}")
                    continue
        
        # Sort by creation date (newest first)
        backups.sort(key=lambda b: b.created_at, reverse=True)
        
        if limit:
            backups = backups[:limit]
        
        return backups
    
    def get_backup(self, backup_id: str) -> Optional[BackupMetadata]:
        """
        Get a specific backup by ID.
        
        Args:
            backup_id: Backup ID.
            
        Returns:
            BackupMetadata or None if not found.
        """
        # Search all backup directories
        for app_dir in self.backup_dir.iterdir():
            if not app_dir.is_dir():
                continue
            
            metadata_file = app_dir / f"{backup_id}.json"
            if metadata_file.exists():
                try:
                    result = run_command_sudo(["cat", str(metadata_file)])
                    if result.success:
                        data = json.loads(result.stdout)
                        return BackupMetadata.from_dict(data)
                except Exception:
                    pass
        
        return None
    
    def get_latest_backup(self, domain: str) -> Optional[BackupMetadata]:
        """
        Get the most recent backup for an application.
        
        Args:
            domain: Domain name.
            
        Returns:
            BackupMetadata or None if no backups exist.
        """
        backups = self.list_backups(domain=domain, limit=1)
        return backups[0] if backups else None
    
    def restore(
        self,
        backup_id: str,
        target_domain: Optional[str] = None,
        restore_env: bool = True,
        stop_service: bool = True,
        verify_checksum: bool = True,
        pre_restore_hook: Optional[str] = None,
        post_restore_hook: Optional[str] = None,
    ) -> bool:
        """
        Restore an application from backup.
        
        Args:
            backup_id: Backup ID to restore.
            target_domain: Target domain (defaults to original).
            restore_env: Restore .env files.
            stop_service: Stop service before restore.
            verify_checksum: Verify backup integrity.
            pre_restore_hook: Command to run before restore.
            post_restore_hook: Command to run after restore.
            
        Returns:
            True if restore was successful.
            
        Raises:
            BackupError: If restore fails.
        """
        # Get backup metadata
        metadata = self.get_backup(backup_id)
        if not metadata:
            raise BackupError(f"Backup not found: {backup_id}")
        
        # Determine target
        domain = target_domain or metadata.domain
        app_name = domain_to_app_name(domain)
        app_path = self.config.apps_directory / app_name
        
        # Get backup file path
        source_app_name = domain_to_app_name(metadata.domain)
        backup_file = self._get_app_backup_dir(source_app_name) / f"{backup_id}.tar.gz"
        
        # Verify backup file exists
        result = run_command_sudo(["test", "-f", str(backup_file)])
        if not result.success:
            raise BackupError(f"Backup file not found: {backup_file}")
        
        # Verify checksum
        if verify_checksum and metadata.checksum:
            self.logger.debug("Verifying backup checksum...")
            result = run_command_sudo(["sha256sum", str(backup_file)])
            if result.success:
                current_checksum = result.stdout.split()[0]
                if current_checksum != metadata.checksum:
                    raise BackupError("Backup checksum mismatch - file may be corrupted")
            else:
                self.logger.warning("Could not verify checksum")
        
        # Stop service if requested
        service_was_running = False
        if stop_service:
            try:
                status = self.service_manager.get_status(app_name)
                service_was_running = status.get("active", False)
                if service_was_running:
                    self.logger.debug(f"Stopping service: {app_name}")
                    self.service_manager.stop(app_name)
            except Exception:
                pass
        
        # Run pre-restore hook
        if pre_restore_hook:
            self.logger.debug(f"Running pre-restore hook: {pre_restore_hook}")
            result = self._run(["bash", "-c", pre_restore_hook], cwd=app_path)
            if not result.success:
                self.logger.warning(f"Pre-restore hook failed: {result.stderr}")
        
        # Backup current .env if it exists and we're not restoring env
        env_backup = None
        env_file = app_path / ".env"
        if not restore_env and env_file.exists():
            result = run_command_sudo(["cat", str(env_file)])
            if result.success:
                env_backup = result.stdout
        
        # Create temporary extraction directory
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            
            # Extract backup
            self.logger.debug(f"Extracting backup to {tmp_path}")
            result = run_command_sudo([
                "tar", "-xzf", str(backup_file), "-C", str(tmp_path)
            ])
            
            if not result.success:
                raise BackupError(f"Failed to extract backup: {result.stderr}")
            
            # Find extracted app directory
            extracted_dirs = list(tmp_path.iterdir())
            if not extracted_dirs:
                raise BackupError("Backup archive is empty")
            
            extracted_path = extracted_dirs[0]
            
            # Remove current app directory
            if app_path.exists():
                self.logger.debug(f"Removing current app: {app_path}")
                remove_directory(app_path, sudo=True)
            
            # Move extracted content to app path
            self.logger.debug(f"Moving restored content to {app_path}")
            
            # Ensure parent exists
            run_command_sudo(["mkdir", "-p", str(app_path.parent)])
            
            # If restoring to different domain, rename
            if source_app_name != app_name:
                result = run_command_sudo([
                    "mv", str(extracted_path), str(app_path)
                ])
            else:
                result = run_command_sudo([
                    "mv", str(extracted_path), str(app_path)
                ])
            
            if not result.success:
                raise BackupError(f"Failed to restore files: {result.stderr}")
        
        # Restore .env backup if we had one
        if env_backup:
            self.logger.debug("Restoring original .env file")
            with tempfile.NamedTemporaryFile(mode="w", delete=False) as tmp:
                tmp.write(env_backup)
                tmp_env_path = Path(tmp.name)
            run_command_sudo(["mv", str(tmp_env_path), str(env_file)])
            run_command_sudo(["chmod", "600", str(env_file)])
        
        # Set proper ownership
        service_user = self.config.service_user
        run_command_sudo(["chown", "-R", f"{service_user}:{service_user}", str(app_path)])
        
        # Run post-restore hook
        if post_restore_hook:
            self.logger.debug(f"Running post-restore hook: {post_restore_hook}")
            result = self._run(["bash", "-c", post_restore_hook], cwd=app_path)
            if not result.success:
                self.logger.warning(f"Post-restore hook failed: {result.stderr}")
        
        # Restart service if it was running
        if service_was_running:
            self.logger.debug(f"Starting service: {app_name}")
            self.service_manager.start(app_name)
        
        return True
    
    def delete(self, backup_id: str) -> bool:
        """
        Delete a backup.
        
        Args:
            backup_id: Backup ID to delete.
            
        Returns:
            True if deleted successfully.
            
        Raises:
            BackupError: If deletion fails.
        """
        metadata = self.get_backup(backup_id)
        if not metadata:
            raise BackupError(f"Backup not found: {backup_id}")
        
        app_name = domain_to_app_name(metadata.domain)
        app_backup_dir = self._get_app_backup_dir(app_name)
        
        backup_file = app_backup_dir / f"{backup_id}.tar.gz"
        metadata_file = app_backup_dir / f"{backup_id}.json"
        
        # Delete files
        for file_path in [backup_file, metadata_file]:
            result = run_command_sudo(["rm", "-f", str(file_path)])
            if not result.success:
                self.logger.warning(f"Failed to delete: {file_path}")
        
        self.logger.debug(f"Deleted backup: {backup_id}")
        return True
    
    def _rotate_backups(self, app_name: str) -> None:
        """
        Rotate old backups to keep only the most recent ones.
        
        Args:
            app_name: Application name.
        """
        backups = self.list_backups(domain=app_name.replace("-", ".").replace("wasm-", ""))
        
        if len(backups) > self.max_backups:
            # Delete oldest backups
            for backup in backups[self.max_backups:]:
                try:
                    self.delete(backup.id)
                    self.logger.debug(f"Rotated old backup: {backup.id}")
                except Exception as e:
                    self.logger.warning(f"Failed to rotate backup {backup.id}: {e}")
    
    def verify(self, backup_id: str) -> Dict[str, Any]:
        """
        Verify a backup's integrity.
        
        Args:
            backup_id: Backup ID to verify.
            
        Returns:
            Verification results dictionary.
        """
        results = {
            "valid": True,
            "errors": [],
            "warnings": [],
        }
        
        metadata = self.get_backup(backup_id)
        if not metadata:
            results["valid"] = False
            results["errors"].append("Backup metadata not found")
            return results
        
        app_name = domain_to_app_name(metadata.domain)
        backup_file = self._get_app_backup_dir(app_name) / f"{backup_id}.tar.gz"
        
        # Check if backup file exists
        result = run_command_sudo(["test", "-f", str(backup_file)])
        if not result.success:
            results["valid"] = False
            results["errors"].append("Backup file not found")
            return results
        
        # Verify checksum
        if metadata.checksum:
            result = run_command_sudo(["sha256sum", str(backup_file)])
            if result.success:
                current_checksum = result.stdout.split()[0]
                if current_checksum != metadata.checksum:
                    results["valid"] = False
                    results["errors"].append("Checksum mismatch")
                else:
                    results["checksum_verified"] = True
            else:
                results["warnings"].append("Could not verify checksum")
        else:
            results["warnings"].append("No checksum stored in metadata")
        
        # Test archive integrity
        result = run_command_sudo(["tar", "-tzf", str(backup_file)])
        if not result.success:
            results["valid"] = False
            results["errors"].append("Archive is corrupted")
        else:
            results["archive_valid"] = True
            results["file_count"] = len(result.stdout.strip().split("\n"))
        
        return results
    
    def get_storage_usage(self) -> Dict[str, Any]:
        """
        Get backup storage usage statistics.
        
        Returns:
            Storage usage dictionary.
        """
        usage = {
            "total_size_bytes": 0,
            "total_backups": 0,
            "by_app": {},
        }
        
        if not self.backup_dir.exists():
            return usage
        
        for app_dir in self.backup_dir.iterdir():
            if not app_dir.is_dir():
                continue
            
            app_name = app_dir.name
            app_backups = list(app_dir.glob("*.tar.gz"))
            
            app_size = 0
            for backup_file in app_backups:
                result = run_command_sudo(["stat", "-c", "%s", str(backup_file)])
                if result.success:
                    app_size += int(result.stdout.strip())
            
            usage["by_app"][app_name] = {
                "count": len(app_backups),
                "size_bytes": app_size,
            }
            usage["total_size_bytes"] += app_size
            usage["total_backups"] += len(app_backups)
        
        return usage


class RollbackManager:
    """
    Manager for application rollbacks.
    
    Provides high-level rollback functionality including
    automatic pre-deploy backups and quick rollback.
    """
    
    def __init__(self, verbose: bool = False):
        """Initialize rollback manager."""
        self.verbose = verbose
        self.logger = Logger(verbose=verbose)
        self.backup_manager = BackupManager(verbose=verbose)
        self.service_manager = ServiceManager(verbose=verbose)
        self.config = Config()
    
    def create_pre_deploy_backup(
        self,
        domain: str,
        description: str = "Pre-deploy backup",
    ) -> Optional[BackupMetadata]:
        """
        Create a backup before deployment.
        
        Args:
            domain: Domain name.
            description: Backup description.
            
        Returns:
            BackupMetadata or None if app doesn't exist.
        """
        app_name = domain_to_app_name(domain)
        app_path = self.config.apps_directory / app_name
        
        if not app_path.exists():
            self.logger.debug(f"No existing app to backup: {domain}")
            return None
        
        return self.backup_manager.create(
            domain=domain,
            description=description,
            include_env=True,
            tags=["pre-deploy", "auto"],
        )
    
    def rollback(
        self,
        domain: str,
        backup_id: Optional[str] = None,
        rebuild: bool = True,
    ) -> bool:
        """
        Rollback an application to a previous state.
        
        Args:
            domain: Domain name.
            backup_id: Specific backup ID (defaults to latest non-auto backup).
            rebuild: Rebuild application after restore.
            
        Returns:
            True if rollback was successful.
        """
        # Get backup to restore
        if backup_id:
            metadata = self.backup_manager.get_backup(backup_id)
            if not metadata:
                raise BackupError(f"Backup not found: {backup_id}")
        else:
            # Get latest backup that is NOT an auto/safety backup
            all_backups = self.backup_manager.list_backups(domain=domain)
            metadata = None
            for backup in all_backups:
                # Skip auto-generated safety backups
                if "auto" not in backup.tags and "pre-deploy" not in backup.tags:
                    metadata = backup
                    break
            
            if not metadata:
                # If all backups are auto, use the oldest one (most stable)
                if all_backups:
                    metadata = all_backups[-1]
                else:
                    raise BackupError(f"No backups found for: {domain}")
        
        self.logger.info(f"Rolling back to: {metadata.id}")
        self.logger.info(f"  Created: {metadata.age}")
        if metadata.git_commit:
            self.logger.info(f"  Commit: {metadata.git_commit}")
        
        # Restore from backup
        self.backup_manager.restore(
            backup_id=metadata.id,
            restore_env=True,
            stop_service=True,
        )
        
        # Rebuild if requested
        if rebuild:
            app_name = domain_to_app_name(domain)
            app_path = self.config.apps_directory / app_name
            
            self.logger.info("Rebuilding application...")
            
            # Detect app type and run build
            from wasm.deployers import get_deployer, detect_app_type
            
            app_type = detect_app_type(app_path, verbose=self.verbose)
            if app_type:
                deployer = get_deployer(app_type, verbose=self.verbose)
                deployer.app_path = app_path
                deployer.app_name = app_name
                deployer.domain = domain
                
                try:
                    deployer.install_dependencies()
                    deployer.build()
                except Exception as e:
                    self.logger.warning(f"Rebuild failed: {e}")
                    self.logger.info("Application restored but may need manual rebuild")
        
        # Start service (if it exists)
        app_name = domain_to_app_name(domain)
        try:
            status = self.service_manager.get_status(app_name)
            if status.get("exists"):
                self.service_manager.start(app_name)
        except Exception as e:
            self.logger.debug(f"Could not start service: {e}")
        
        return True
    
    def list_rollback_points(self, domain: str) -> List[BackupMetadata]:
        """
        List available rollback points for an application.
        
        Args:
            domain: Domain name.
            
        Returns:
            List of available backups.
        """
        return self.backup_manager.list_backups(domain=domain)
