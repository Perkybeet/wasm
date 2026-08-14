# Copyright (c) 2024-2026 Yago López Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
Backup manager for WASM.

A backup is a promise that the data can come back. Three things used to break
that promise, and this module exists in its current shape because of them:

- **The archive was files only.** ``--include-databases`` recorded
  ``includes_databases: true`` in the metadata and left the dump wherever the
  engine manager happened to write it, on the same machine, tracked by nothing.
  Rotation and ``delete()`` never touched those files, and an archive copied to
  another host restored an application with an empty database. A backup is now
  **self-contained**: dumps and Docker volume archives live *inside* the
  ``.tar.gz`` under :data:`PAYLOAD_DIR`, together with a manifest describing
  them, and :meth:`BackupManager.restore_archive` can restore a bare archive
  file with no metadata and no store next to it.
- **Extraction trusted the archive.** ``restore()`` ran ``tar -xzf``, which
  writes ``../`` members outside the destination and follows symlinks. The
  member-by-member extractor written for :mod:`wasm.managers.source_manager` is
  reused here rather than re-implemented.
- **``verify()`` verified nothing** it could not see: it shelled out to
  ``sha256sum`` and ``tar -tzf``. Both checks now run in process, and the
  archive is unpacked with the *same* extractor a restore uses, so an archive
  that ``restore()`` would always refuse is reported before a restore needs it.
  ``tar -tzf`` said "valid" for an archive full of absolute symlinks.
- **A Python application's backup could not be restored at all.**
  ``PythonDeployer`` creates ``<app>/venv``, a virtualenv is full of absolute
  symlinks into ``/usr``, and the hardened extractor refuses those - correctly,
  because that is how an archive reaches a file outside the deployment. So the
  virtualenv is excluded from the archive (see :attr:`BackupManager.DEFAULT_EXCLUDES`)
  and rebuilt from ``requirements.txt`` on the next install, which is also the
  only way it could ever have worked: a venv carries the absolute paths of the
  machine that built it.

**Rehearsals.** Everything that changes the persistent filesystem goes through
:mod:`wasm.core.fs`, because ``wasm --dry-run backup delete <id> --force`` used
to announce a rehearsal and then unlink the archive. The one thing that does not
is the archive built inside a :class:`tempfile.TemporaryDirectory`: it is
staging, it is removed by its own context manager, and a rehearsal never reaches
it (see :meth:`BackupManager.create`).

**Size limits.** The whole application tree, every dump and every volume go into
one archive, so a backup is as large as the data it protects. Extraction is
bounded by :data:`MAX_BACKUP_ENTRIES` and :data:`MAX_BACKUP_BYTES` (overridable
through ``backup.max_entries`` and ``backup.max_bytes``); an archive past those
limits is refused rather than allowed to fill the disk during a restore.

WASM requires root, so file operations happen in process and external commands
(docker, chown, the hooks) go through the :class:`~wasm.core.runner.CommandRunner`
with a timeout. There is no ``sudo`` here.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import shutil
import sqlite3
import tarfile
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any, ClassVar, Protocol, runtime_checkable

from wasm.core.config import Config
from wasm.core.exceptions import (
    BackupError,
    DatabaseError,
    SecurityError,
    ServiceError,
    ValidationError,
    WASMError,
)
from wasm.core.fs import (
    SECRET_DIR_MODE,
    SECRET_MODE,
    DryRunFileSystem,
    FileSystem,
    get_fs,
)
from wasm.core.logger import Logger
from wasm.core.runner import DEFAULT_TIMEOUT, CommandResult, CommandRunner, get_runner
from wasm.core.store import DeploymentTrigger, get_store
from wasm.core.utils import domain_to_app_name
from wasm.deployers.recorder import CapturingLogger, DeploymentRecorder
from wasm.managers.service_manager import ServiceManager
from wasm.managers.source_manager import SourceError, extract_archive
from wasm.validators.names import resolve_within, validate_app_name, validate_filename

__all__ = [
    "DATABASES_DIR",
    "MANIFEST_NAME",
    "MAX_BACKUP_BYTES",
    "MAX_BACKUP_ENTRIES",
    "PAYLOAD_DIR",
    "VOLUMES_DIR",
    "BackupError",
    "BackupManager",
    "BackupMetadata",
    "RollbackManager",
]

#: Docker has to pull alpine the first time a volume is backed up.
_DOCKER_TIMEOUT = 600

#: Deadline for chown over a restored tree.
_OWNERSHIP_TIMEOUT = 300

#: Deadline for a user-supplied hook.
_HOOK_TIMEOUT = 600

#: gzip level. 9 costs several times the CPU of 6 for a few percent of size on
#: an application tree, and a backup that takes too long is a backup nobody runs.
_COMPRESS_LEVEL = 6

#: Reserved top-level directory inside the archive. Everything that is not the
#: application tree lives under it.
PAYLOAD_DIR = "wasm-backup"

#: Database dumps, inside :data:`PAYLOAD_DIR`.
DATABASES_DIR = "databases"

#: Docker volume archives, inside :data:`PAYLOAD_DIR`.
VOLUMES_DIR = "volumes"

#: Description of the archive's own contents, inside :data:`PAYLOAD_DIR`.
MANIFEST_NAME = "manifest.json"

#: Members allowed out of a backup archive during a restore.
MAX_BACKUP_ENTRIES = 2_000_000

#: Bytes allowed out of a backup archive during a restore.
MAX_BACKUP_BYTES = 256 * 1024**3


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
    includes_build: bool = False
    includes_databases: bool = False
    includes_docker_volumes: bool = False
    database_backups: list[dict[str, Any]] = field(default_factory=list)
    docker_volume_backups: list[dict[str, Any]] = field(default_factory=list)
    schema_backups: list[dict[str, Any]] = field(default_factory=list)
    git_commit: str | None = None
    git_branch: str | None = None
    checksum: str | None = None
    tags: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """
        Convert to a JSON-serialisable dictionary.

        Returns:
            The metadata as plain data.
        """
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
            "includes_build": self.includes_build,
            "includes_databases": self.includes_databases,
            "includes_docker_volumes": self.includes_docker_volumes,
            "database_backups": self.database_backups,
            "docker_volume_backups": self.docker_volume_backups,
            "schema_backups": self.schema_backups,
            "git_commit": self.git_commit,
            "git_branch": self.git_branch,
            "checksum": self.checksum,
            "tags": self.tags,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> BackupMetadata:
        """
        Create metadata from a dictionary.

        Args:
            data: Metadata as read from a ``.json`` sidecar or a manifest.

        Returns:
            The parsed metadata.
        """
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
            includes_build=data.get("includes_build", False),
            includes_databases=data.get("includes_databases", False),
            includes_docker_volumes=data.get("includes_docker_volumes", False),
            database_backups=data.get("database_backups", []),
            docker_volume_backups=data.get("docker_volume_backups", []),
            schema_backups=data.get("schema_backups", []),
            git_commit=data.get("git_commit"),
            git_branch=data.get("git_branch"),
            checksum=data.get("checksum"),
            tags=data.get("tags", []),
        )

    @property
    def size_human(self) -> str:
        """
        Render the archive size in the largest unit that keeps it under 1024.

        Returns:
            A human-readable size.
        """
        size = float(self.size_bytes)
        for unit in ["B", "KB", "MB", "GB"]:
            if size < 1024:
                return f"{size:.1f} {unit}"
            size /= 1024
        return f"{size:.1f} TB"

    @property
    def age(self) -> str:
        """
        Render how long ago the backup was taken.

        Returns:
            A human-readable age, or "unknown" for an unparsable timestamp.
        """
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
        except ValueError:
            return "unknown"


class BackupManager:
    """
    Manager for application backups.

    Creates self-contained archives, lists and verifies them, restores them and
    enforces retention.
    """

    # Default backup directory
    DEFAULT_BACKUP_DIR = Path("/var/backups/wasm")

    # Files/directories to always exclude from backups.
    #
    # The virtualenv entries are not a size optimisation. A venv is a tree of
    # absolute symlinks into the interpreter of the machine that built it, and
    # the extractor a restore uses refuses absolute link targets, so an archive
    # containing one could never be restored - the backup only looked like a
    # backup. ``install_dependencies()`` recreates it from requirements.txt.
    DEFAULT_EXCLUDES: ClassVar[list[str]] = [
        "node_modules",
        "venv",
        ".venv",
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
    OPTIONAL_INCLUDES: ClassVar[list[str]] = [
        "node_modules",
        ".next",
        "dist",
        "build",
    ]

    # Maximum backups to keep per app (default)
    DEFAULT_MAX_BACKUPS = 10

    # Archive layout version. 1.x archives kept the application tree at the top
    # level and pointed at dumps living outside the archive.
    BACKUP_VERSION = "2.0.0"

    def __init__(
        self,
        verbose: bool = False,
        runner: CommandRunner | None = None,
        fs: FileSystem | None = None,
    ) -> None:
        """
        Initialize the backup manager.

        Args:
            verbose: Enable verbose logging.
            runner: Command runner used for docker, chown and hooks. Defaults
                to the process-wide runner.
            fs: Filesystem used for every change to the persistent tree.
                Defaults to the process-wide one, which is what makes
                ``--dry-run`` true for a deletion as well as for a command.
        """
        self.verbose = verbose
        self.logger = Logger(verbose=verbose)
        self.config = Config()
        self._runner = runner
        self._fs = fs
        self.service_manager = ServiceManager(verbose=verbose, runner=runner)

        self.backup_dir = Path(self.config.get("backup.directory", str(self.DEFAULT_BACKUP_DIR)))
        self.max_backups = self.config.get("backup.max_per_app", self.DEFAULT_MAX_BACKUPS)
        self.max_entries = int(self.config.get("backup.max_entries", MAX_BACKUP_ENTRIES))
        self.max_bytes = int(self.config.get("backup.max_bytes", MAX_BACKUP_BYTES))

    # -- plumbing ---------------------------------------------------------

    @property
    def runner(self) -> CommandRunner:
        """
        Return the command runner used for every external command.

        Returns:
            The injected runner, or the process-wide one.
        """
        return self._runner or get_runner()

    @property
    def fs(self) -> FileSystem:
        """
        Return the filesystem used for every change to the persistent tree.

        Returns:
            The injected filesystem, or the process-wide one.
        """
        return self._fs or get_fs()

    @property
    def _rehearsing(self) -> bool:
        """
        Report whether changes are being rehearsed rather than applied.

        Only :meth:`create` asks. Every other operation stays oblivious and
        lets the seam refuse, which is the whole point of having a seam; but
        building a multi-gigabyte archive and dumping production databases in
        order to throw the result away is not a rehearsal, it is the operation.

        Returns:
            True when the active filesystem records changes instead of making
            them.
        """
        return isinstance(self.fs, DryRunFileSystem)

    def _exec(
        self,
        argv: Sequence[str],
        *,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
        timeout: int = DEFAULT_TIMEOUT,
    ) -> CommandResult:
        """
        Run a command through the shared runner.

        Args:
            argv: Program and arguments.
            cwd: Working directory.
            env: Extra environment variables.
            timeout: Deadline in seconds.

        Returns:
            The command outcome.
        """
        return self.runner.run([str(a) for a in argv], cwd=cwd, env=env, timeout=timeout)

    def _ensure_backup_dir(self) -> None:
        """
        Ensure the backup directory exists and is readable only by root.

        An archive carries the application's ``.env`` and its database dumps, so
        the directory holding it is a secrets directory, not a shared one.

        Raises:
            BackupError: If the directory cannot be created.
        """
        try:
            self.fs.make_dir(self.backup_dir, mode=SECRET_DIR_MODE)
            # An installation that predates this tightening already has the
            # directory, and make_dir only sets the mode on what it creates.
            self.fs.chmod(self.backup_dir, SECRET_DIR_MODE)
        except OSError as exc:
            raise BackupError(
                f"Failed to create backup directory: {self.backup_dir}",
                details=f"{exc}. WASM must run as root.",
            ) from exc

    def _get_app_backup_dir(self, app_name: str) -> Path:
        """
        Return the backup directory of one application.

        Args:
            app_name: Application name.

        Returns:
            The per-application directory under :attr:`backup_dir`.
        """
        return self.backup_dir / app_name

    def _generate_backup_id(self, domain: str) -> str:
        """
        Build a unique backup identifier.

        Args:
            domain: Domain the backup belongs to.

        Returns:
            An identifier of the form ``<domain-with-dashes>_<timestamp>``.
        """
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        return f"{domain.replace('.', '-')}_{timestamp}"

    def _calculate_checksum(self, file_path: Path) -> str:
        """
        Calculate the SHA256 checksum of a file.

        Args:
            file_path: File to hash.

        Returns:
            The hex digest.

        Raises:
            BackupError: If the file cannot be read.
        """
        sha256 = hashlib.sha256()
        try:
            with open(file_path, "rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    sha256.update(chunk)
        except OSError as exc:
            raise BackupError(f"Cannot read {file_path}", details=str(exc)) from exc
        return sha256.hexdigest()

    def _get_git_info(self, app_path: Path) -> tuple[str | None, str | None]:
        """
        Read the commit and branch of the deployed tree.

        Args:
            app_path: Application directory.

        Returns:
            The short commit and the branch, either of which may be None.
        """
        git_commit: str | None = None
        git_branch: str | None = None

        if (app_path / ".git").exists():
            result = self._exec(["git", "rev-parse", "HEAD"], cwd=app_path)
            if result.success:
                git_commit = result.stdout.strip()[:12]

            result = self._exec(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=app_path)
            if result.success:
                git_branch = result.stdout.strip()

        return git_commit, git_branch

    def _detect_app_type(self, app_path: Path) -> str:
        """
        Detect the application type from the files in the tree.

        Args:
            app_path: Application directory.

        Returns:
            The detected type, or "unknown".
        """
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
        custom_excludes: list[str] | None = None,
    ) -> list[str]:
        """
        Build the list of patterns kept out of the archive.

        Args:
            include_node_modules: Keep ``node_modules`` in the archive.
            include_build: Keep build output in the archive.
            custom_excludes: Extra patterns from the caller.

        Returns:
            The exclusion patterns.
        """
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

    @staticmethod
    def _is_excluded(relative: PurePosixPath, patterns: Sequence[str]) -> bool:
        """
        Decide whether an archive member matches an exclusion pattern.

        Args:
            relative: Member path relative to the application root.
            patterns: Patterns from :meth:`_build_exclude_list`.

        Returns:
            True when the member must be left out.
        """
        text = relative.as_posix()
        for pattern in patterns:
            if "/" in pattern:
                if (
                    text == pattern
                    or text.startswith(f"{pattern}/")
                    or fnmatch.fnmatch(text, pattern)
                    or fnmatch.fnmatch(text, f"{pattern}/*")
                ):
                    return True
            elif any(fnmatch.fnmatch(part, pattern) for part in relative.parts):
                return True
        return False

    # -- archive construction --------------------------------------------

    def _write_archive(
        self,
        destination: Path,
        app_path: Path,
        app_name: str,
        payload_dir: Path,
        excludes: Sequence[str],
    ) -> None:
        """
        Write the backup archive: application tree plus payload.

        The archive is built in process rather than by shelling out to ``tar``:
        the payload has to be placed at an exact path, unsupported file types
        (sockets, devices, FIFOs) have to be dropped rather than guessed at, and
        a backup nobody can reason about is a backup nobody can trust.

        This is the one write that does not go through :mod:`wasm.core.fs`: a
        tar stream cannot, and ``destination`` is always a file inside the
        caller's :class:`tempfile.TemporaryDirectory`, which removes itself and
        which a rehearsal never reaches.

        Args:
            destination: Archive file to create, inside the staging directory.
            app_path: Application directory to archive.
            app_name: Name the application tree takes inside the archive.
            payload_dir: Directory holding dumps, volumes and the manifest.
            excludes: Patterns to leave out of the application tree.

        Raises:
            BackupError: If the archive cannot be written.
        """

        def _filter(info: tarfile.TarInfo) -> tarfile.TarInfo | None:
            parts = PurePosixPath(info.name).parts[1:]
            if parts and self._is_excluded(PurePosixPath(*parts), excludes):
                return None
            if not (info.isfile() or info.isdir() or info.issym() or info.islnk()):
                self.logger.debug(f"Skipping unsupported file type in backup: {info.name}")
                return None
            return info

        try:
            with tarfile.open(destination, "w:gz", compresslevel=_COMPRESS_LEVEL) as archive:
                archive.add(app_path, arcname=app_name, recursive=True, filter=_filter)
                archive.add(payload_dir, arcname=PAYLOAD_DIR, recursive=True)
        except (tarfile.TarError, OSError) as exc:
            self.fs.remove(destination)
            raise BackupError(
                f"Failed to create backup archive for {app_name}",
                details=str(exc),
            ) from exc

    def create(
        self,
        domain: str,
        description: str = "",
        include_env: bool = True,
        include_node_modules: bool = False,
        include_build: bool = False,
        include_databases: bool = False,
        include_docker_volumes: bool = False,
        schemas: list[str] | None = None,
        redis_method: str = "rdb",
        retention_count: int | None = None,
        retention_days: int | None = None,
        tags: list[str] | None = None,
        pre_backup_hook: str | None = None,
    ) -> BackupMetadata:
        """
        Create a self-contained backup of an application.

        Args:
            domain: Domain name of the application.
            description: Optional description for the backup.
            include_env: Include ``.env`` files in the backup.
            include_node_modules: Include ``node_modules`` (large).
            include_build: Include build artifacts.
            include_databases: Dump the associated databases into the archive.
            include_docker_volumes: Put the named Docker volumes in the archive.
            schemas: Not supported; see Raises.
            redis_method: Redis backup method ("rdb" or "aof").
            retention_count: Max backups to keep (overrides the default).
            retention_days: Max age in days for backups.
            tags: Optional tags for the backup.
            pre_backup_hook: Optional command to run before the backup.

        Returns:
            Metadata describing the archive that was written. In a rehearsal,
            metadata describing the archive that *would* have been written, with
            no size and no checksum, because nothing was read or compressed.

        Raises:
            BackupError: If the application is missing, a dump fails, or the
                archive cannot be written. Also when ``schemas`` is given: a
                per-schema dump is written by the engine outside the archive,
                which is exactly the promise this module no longer makes.
            ValidationError: If the domain does not yield a usable app name.
        """
        if schemas:
            raise BackupError(
                "Per-schema dumps cannot be placed inside a self-contained backup",
                details="Back up the whole database, or use 'wasm db backup --schema' separately.",
            )

        app_name = validate_app_name(domain_to_app_name(domain))
        if app_name == PAYLOAD_DIR:
            raise BackupError(
                f"Application name '{app_name}' is reserved inside a backup archive",
                details="Rename the deployment; this name collides with the archive payload.",
            )

        app_path = self.config.apps_directory / app_name
        if not app_path.exists():
            raise BackupError(
                f"Application not found: {domain}",
                details=f"Nothing to back up at {app_path}.",
            )

        self._ensure_backup_dir()
        app_backup_dir = self._get_app_backup_dir(app_name)
        try:
            self.fs.make_dir(app_backup_dir, mode=SECRET_DIR_MODE)
            self.fs.chmod(app_backup_dir, SECRET_DIR_MODE)
        except OSError as exc:
            raise BackupError(
                f"Failed to create backup directory: {app_backup_dir}",
                details=str(exc),
            ) from exc

        backup_id = self._generate_backup_id(domain)
        backup_file = app_backup_dir / f"{backup_id}.tar.gz"
        metadata_file = app_backup_dir / f"{backup_id}.json"

        self.logger.debug(f"Creating backup: {backup_id}")

        if self._rehearsing:
            return self._rehearse_create(
                domain=domain,
                app_name=app_name,
                app_path=app_path,
                backup_id=backup_id,
                backup_file=backup_file,
                metadata_file=metadata_file,
                description=description,
                include_env=include_env,
                include_node_modules=include_node_modules,
                include_build=include_build,
                tags=tags,
            )

        if pre_backup_hook:
            self.logger.debug(f"Running pre-backup hook: {pre_backup_hook}")
            result = self._exec(
                ["bash", "-c", pre_backup_hook], cwd=app_path, timeout=_HOOK_TIMEOUT
            )
            if not result.success:
                self.logger.warning(f"Pre-backup hook failed: {result.stderr}")

        git_commit, git_branch = self._get_git_info(app_path)
        app_type = self._detect_app_type(app_path)
        excludes = self._build_exclude_list(
            include_node_modules=include_node_modules,
            include_build=include_build,
        )
        if not include_env:
            excludes.extend([".env", ".env.*"])

        with tempfile.TemporaryDirectory(prefix="wasm-backup-") as staging:
            staging_path = Path(staging)
            payload_dir = staging_path / PAYLOAD_DIR
            self.fs.make_dir(payload_dir, mode=SECRET_DIR_MODE)

            database_backups: list[dict[str, Any]] = []
            if include_databases:
                database_backups = self._dump_databases(
                    domain, payload_dir / DATABASES_DIR, redis_method=redis_method
                )
                if not database_backups:
                    self.logger.warning(
                        f"No database was backed up for {domain}: the archive contains files only"
                    )

            volume_backups: list[dict[str, Any]] = []
            if include_docker_volumes:
                volumes = self._discover_docker_volumes(app_path)
                if volumes:
                    volume_backups = self._dump_docker_volumes(volumes, payload_dir / VOLUMES_DIR)

            created_at = datetime.now().isoformat()
            manifest: dict[str, Any] = {
                "manifest_version": self.BACKUP_VERSION,
                "id": backup_id,
                "domain": domain,
                "app_name": app_name,
                "app_root": app_name,
                "app_type": app_type,
                "created_at": created_at,
                "databases": database_backups,
                "volumes": volume_backups,
            }
            self.fs.write_text(
                payload_dir / MANIFEST_NAME,
                json.dumps(manifest, indent=2),
                mode=SECRET_MODE,
            )

            staged_archive = staging_path / "archive.tar.gz"
            self._write_archive(staged_archive, app_path, app_name, payload_dir, excludes)

            size_bytes = staged_archive.stat().st_size
            checksum = self._calculate_checksum(staged_archive)

            try:
                self.fs.move(staged_archive, backup_file)
                # The archive carries the application's .env and its database
                # dumps: it is a secret, not a file for the adm group.
                self.fs.chmod(backup_file, SECRET_MODE)
            except OSError as exc:
                self.fs.remove(backup_file)
                raise BackupError(
                    f"Failed to store backup archive: {backup_file}",
                    details=str(exc),
                ) from exc

        metadata = BackupMetadata(
            id=backup_id,
            domain=domain,
            app_name=app_name,
            created_at=created_at,
            size_bytes=size_bytes,
            app_type=app_type,
            version=self.BACKUP_VERSION,
            description=description,
            includes_env=include_env,
            includes_node_modules=include_node_modules,
            includes_build=include_build,
            # The flags describe the archive, not the request: a backup that
            # dumped nothing must not claim to carry a database.
            includes_databases=bool(database_backups),
            includes_docker_volumes=bool(volume_backups),
            database_backups=database_backups,
            docker_volume_backups=volume_backups,
            schema_backups=[],
            git_commit=git_commit,
            git_branch=git_branch,
            checksum=checksum,
            tags=tags or [],
        )

        try:
            self.fs.write_text(
                metadata_file, json.dumps(metadata.to_dict(), indent=2), mode=SECRET_MODE
            )
        except OSError as exc:
            self.fs.remove(backup_file)
            raise BackupError(
                f"Failed to write backup metadata: {metadata_file}",
                details=str(exc),
            ) from exc

        if retention_count is not None or retention_days is not None:
            self.rotate_by_policy(
                app_name,
                max_count=retention_count or self.max_backups,
                max_age_days=retention_days,
            )
        else:
            self._rotate_backups(app_name)

        self.logger.debug(f"Backup created: {backup_file} ({metadata.size_human})")

        return metadata

    def _rehearse_create(
        self,
        *,
        domain: str,
        app_name: str,
        app_path: Path,
        backup_id: str,
        backup_file: Path,
        metadata_file: Path,
        description: str,
        include_env: bool,
        include_node_modules: bool,
        include_build: bool,
        tags: list[str] | None,
    ) -> BackupMetadata:
        """
        Report the backup a real run would have taken, without taking it.

        Compressing the tree and dumping the databases only to delete the result
        is not a rehearsal: it costs the same disk, CPU and database load as the
        operation, on a machine the operator asked not to touch. So the two
        writes that would have persisted are announced through the seam and
        nothing is read or compressed.

        Only :meth:`create` calls this, and only once it has established that
        the filesystem in effect records changes instead of making them.

        Args:
            domain: Domain being backed up.
            app_name: Application name.
            app_path: Directory that would have been archived.
            backup_id: Identifier the backup would have been given.
            backup_file: Archive that would have been written.
            metadata_file: Sidecar that would have been written.
            description: Description the caller passed.
            include_env: Whether ``.env`` files would have been included.
            include_node_modules: Whether ``node_modules`` would be included.
            include_build: Whether build output would be included.
            tags: Tags the caller passed.

        Returns:
            Metadata for the archive that was not written: no size, no checksum.
        """
        self.fs.write_text(backup_file, "", mode=SECRET_MODE)
        self.fs.write_text(metadata_file, "", mode=SECRET_MODE)
        self.logger.info(f"Would back up {app_path} to {backup_file}")

        return BackupMetadata(
            id=backup_id,
            domain=domain,
            app_name=app_name,
            created_at=datetime.now().isoformat(),
            size_bytes=0,
            app_type=self._detect_app_type(app_path),
            version=self.BACKUP_VERSION,
            description=description,
            includes_env=include_env,
            includes_node_modules=include_node_modules,
            includes_build=include_build,
            checksum=None,
            tags=tags or [],
        )

    # -- listing ----------------------------------------------------------

    def _read_metadata_file(self, path: Path) -> BackupMetadata | None:
        """
        Read one metadata sidecar.

        Args:
            path: The ``.json`` file to read.

        Returns:
            The metadata, or None when the file is unreadable or malformed.
        """
        try:
            return BackupMetadata.from_dict(json.loads(path.read_text()))
        except (OSError, ValueError, KeyError, TypeError) as exc:
            self.logger.debug(f"Error reading metadata {path}: {exc}")
            return None

    def list_backups(
        self,
        domain: str | None = None,
        app_name: str | None = None,
        tags: list[str] | None = None,
        limit: int | None = None,
    ) -> list[BackupMetadata]:
        """
        List backups for one application or for all of them.

        Args:
            domain: Filter by domain (None for all).
            app_name: Filter by app name directly (alternative to domain).
            tags: Only return backups carrying one of these tags.
            limit: Maximum number of backups to return.

        Returns:
            The backups whose archive is present, newest first.
        """
        backups: list[BackupMetadata] = []

        if not self.backup_dir.exists():
            return backups

        if app_name:
            dirs_to_scan = [self._get_app_backup_dir(app_name)]
        elif domain:
            dirs_to_scan = [self._get_app_backup_dir(domain_to_app_name(domain))]
        else:
            dirs_to_scan = [d for d in self.backup_dir.iterdir() if d.is_dir()]

        for app_dir in dirs_to_scan:
            if not app_dir.exists():
                continue

            for metadata_file in app_dir.glob("*.json"):
                metadata = self._read_metadata_file(metadata_file)
                if metadata is None:
                    continue

                if not (app_dir / f"{metadata.id}.tar.gz").is_file():
                    continue

                if tags and not any(tag in metadata.tags for tag in tags):
                    continue

                backups.append(metadata)

        backups.sort(key=lambda b: b.created_at, reverse=True)

        if limit:
            backups = backups[:limit]

        return backups

    def get_backup(self, backup_id: str) -> BackupMetadata | None:
        """
        Get one backup by identifier.

        Args:
            backup_id: Backup identifier.

        Returns:
            The metadata, or None when no such backup exists.
        """
        if not self.backup_dir.exists():
            return None

        try:
            name = validate_filename(f"{backup_id}.json")
        except ValidationError:
            self.logger.debug(f"Refusing to look up an unusable backup id: {backup_id!r}")
            return None

        for app_dir in self.backup_dir.iterdir():
            if not app_dir.is_dir():
                continue

            metadata_file = app_dir / name
            if metadata_file.is_file():
                metadata = self._read_metadata_file(metadata_file)
                if metadata is not None:
                    return metadata

        return None

    def get_latest_backup(self, domain: str) -> BackupMetadata | None:
        """
        Get the most recent backup of an application.

        Args:
            domain: Domain name.

        Returns:
            The newest backup, or None when there is none.
        """
        backups = self.list_backups(domain=domain, limit=1)
        return backups[0] if backups else None

    # -- restore ----------------------------------------------------------

    def restore(
        self,
        backup_id: str,
        target_domain: str | None = None,
        restore_env: bool = True,
        stop_service: bool = True,
        verify_checksum: bool = True,
        pre_restore_hook: str | None = None,
        post_restore_hook: str | None = None,
    ) -> bool:
        """
        Restore an application from a backup this manager knows about.

        Args:
            backup_id: Backup identifier.
            target_domain: Target domain (defaults to the original).
            restore_env: Restore the ``.env`` files from the archive.
            stop_service: Stop the service before restoring.
            verify_checksum: Check the archive against the recorded checksum.
            pre_restore_hook: Command to run before the restore.
            post_restore_hook: Command to run after the restore.

        Returns:
            True if the restore succeeded.

        Raises:
            BackupError: If the backup is missing, corrupted, unsafe to extract,
                or if a database it carries cannot be put back.
        """
        metadata = self.get_backup(backup_id)
        if not metadata:
            raise BackupError(
                f"Backup not found: {backup_id}",
                details="Run 'wasm backup list' to see the backups WASM knows about.",
            )

        source_app_name = domain_to_app_name(metadata.domain)
        backup_file = self._get_app_backup_dir(source_app_name) / f"{backup_id}.tar.gz"
        if not backup_file.is_file():
            raise BackupError(
                f"Backup file not found: {backup_file}",
                details="The metadata is there but the archive is not.",
            )

        return self.restore_archive(
            backup_file,
            target_domain=target_domain or metadata.domain,
            restore_env=restore_env,
            stop_service=stop_service,
            expected_checksum=metadata.checksum if verify_checksum else None,
            fallback=metadata,
            pre_restore_hook=pre_restore_hook,
            post_restore_hook=post_restore_hook,
        )

    def restore_archive(
        self,
        archive: Path,
        *,
        target_domain: str | None = None,
        restore_env: bool = True,
        stop_service: bool = True,
        expected_checksum: str | None = None,
        fallback: BackupMetadata | None = None,
        pre_restore_hook: str | None = None,
        post_restore_hook: str | None = None,
    ) -> bool:
        """
        Restore an application from an archive file, wherever it lives.

        This is the method that makes a backup portable: the archive carries its
        own manifest, so a file copied from another machine restores the
        application tree *and* the databases and volumes it contains, with no
        metadata sidecar and no store entry.

        Args:
            archive: Path to the ``.tar.gz`` backup.
            target_domain: Domain to restore into. Defaults to the one recorded
                in the archive manifest.
            restore_env: Restore the ``.env`` files from the archive. When
                False, the ``.env`` currently deployed is kept.
            stop_service: Stop the service before restoring.
            expected_checksum: SHA256 the archive must match, when known.
            fallback: Metadata of an older archive that carries no manifest.
            pre_restore_hook: Command to run before the restore.
            post_restore_hook: Command to run after the restore.

        Returns:
            True if the restore succeeded.

        Raises:
            BackupError: If the archive is missing, does not match its checksum,
                contains a member that is unsafe to extract, does not say which
                application it belongs to, or carries data that cannot be put
                back.
        """
        archive = Path(archive)
        if not archive.is_file():
            raise BackupError(
                f"Backup archive not found: {archive}",
                details="Pass the path of a .tar.gz written by 'wasm backup create'.",
            )

        if expected_checksum:
            self.logger.debug("Verifying backup checksum")
            if self._calculate_checksum(archive) != expected_checksum:
                raise BackupError(
                    f"Backup checksum mismatch: {archive.name}",
                    details="The archive does not match the checksum recorded when it was "
                    "created. Restoring it would restore corrupted data.",
                )

        if self._rehearsing:
            return self._rehearse_restore(archive, target_domain, fallback)

        with tempfile.TemporaryDirectory(prefix="wasm-restore-") as tmp_dir:
            tmp_path = Path(tmp_dir)
            extracted = tmp_path / "extracted"

            try:
                extract_archive(
                    archive,
                    extracted,
                    archive_format="tar.gz",
                    max_entries=self.max_entries,
                    max_total_bytes=self.max_bytes,
                )
            except SourceError as exc:
                raise BackupError(
                    f"Refusing to restore {archive.name}",
                    details=str(exc),
                ) from exc

            manifest = self._read_manifest(extracted)
            domain = target_domain or self._manifest_domain(manifest, fallback)
            try:
                app_name = validate_app_name(domain_to_app_name(domain))
            except ValidationError as exc:
                raise BackupError(
                    f"Cannot restore into {domain!r}",
                    details=f"{exc}. The target domain does not yield a usable directory name.",
                ) from exc
            app_path = self.config.apps_directory / app_name
            app_root = self._locate_app_root(extracted, manifest, fallback)

            service_was_running = self._stop_service_for_restore(app_name, stop_service)

            if pre_restore_hook:
                self.logger.debug(f"Running pre-restore hook: {pre_restore_hook}")
                result = self._exec(
                    ["bash", "-c", pre_restore_hook], cwd=app_path, timeout=_HOOK_TIMEOUT
                )
                if not result.success:
                    self.logger.warning(f"Pre-restore hook failed: {result.stderr}")

            env_backup: str | None = None
            env_file = app_path / ".env"
            if not restore_env and env_file.is_file():
                try:
                    env_backup = env_file.read_text()
                except OSError as exc:
                    self.logger.warning(f"Could not read the current .env: {exc}")

            self._swap_in_tree(app_root, app_path, tmp_path)

            if env_backup is not None:
                self.logger.debug("Restoring the previously deployed .env file")
                self.fs.write_text(env_file, env_backup, mode=SECRET_MODE)

            service_user = self.config.service_user
            self._exec(
                ["chown", "-R", f"{service_user}:{service_user}", str(app_path)],
                timeout=_OWNERSHIP_TIMEOUT,
            )

            # Putting only the files back was silent data loss for every
            # application whose state lives in a database or a volume.
            self._restore_databases(manifest, extracted, fallback)
            self._restore_docker_volumes(manifest, extracted, fallback)

            self._warn_about_missing_environment(app_path)

        if post_restore_hook:
            self.logger.debug(f"Running post-restore hook: {post_restore_hook}")
            result = self._exec(
                ["bash", "-c", post_restore_hook], cwd=app_path, timeout=_HOOK_TIMEOUT
            )
            if not result.success:
                self.logger.warning(f"Post-restore hook failed: {result.stderr}")

        if service_was_running:
            self.logger.debug(f"Starting service: {app_name}")
            self.service_manager.start(app_name)

        return True

    def _rehearse_restore(
        self,
        archive: Path,
        target_domain: str | None,
        fallback: BackupMetadata | None,
    ) -> bool:
        """
        Report what a restore would replace, without unpacking anything.

        A restore deletes the deployed tree and puts the archive's copy in its
        place, so the rehearsal announces that deletion through the seam and
        stops. It deliberately does not unpack the archive: the extraction is
        the size of the backup, and a rehearsal that fills ``/tmp`` has damaged
        the machine it promised not to touch. ``wasm backup verify`` is the
        command that unpacks an archive to prove it is restorable.

        Args:
            archive: Archive that would have been restored.
            target_domain: Domain the caller asked to restore into.
            fallback: Metadata sidecar, for archives with no manifest.

        Returns:
            True, as a real restore would.

        Raises:
            BackupError: If nothing says which application the archive belongs
                to, which is the same refusal a real restore makes.
        """
        manifest = self._read_manifest_in_place(archive)
        domain = target_domain or self._manifest_domain(manifest, fallback)
        app_name = validate_app_name(domain_to_app_name(domain))
        app_path = self.config.apps_directory / app_name

        if app_path.exists():
            self.fs.remove_tree(app_path)
        self.logger.info(f"Would restore {archive} into {app_path}")

        databases = self._payload_entries(manifest, fallback, "databases", "database_backups")
        for entry in databases:
            self.logger.info(f"Would restore {entry.get('engine')} database {entry.get('name')}")
        volumes = self._payload_entries(manifest, fallback, "volumes", "docker_volume_backups")
        for entry in volumes:
            self.logger.info(f"Would restore Docker volume {entry.get('volume')}")

        return True

    def _read_manifest_in_place(self, archive: Path) -> dict[str, Any] | None:
        """
        Read an archive's manifest without extracting the archive.

        Args:
            archive: Archive to read.

        Returns:
            The manifest, or None for a 1.x archive that carries none.

        Raises:
            BackupError: If the archive cannot be read, or its manifest is not
                a JSON object.
        """
        try:
            with tarfile.open(archive, "r:gz") as tar:
                try:
                    member = tar.getmember(f"{PAYLOAD_DIR}/{MANIFEST_NAME}")
                except KeyError:
                    return None
                source = tar.extractfile(member)
                if source is None:
                    return None
                with source:
                    data = json.loads(source.read().decode("utf-8"))
        except (tarfile.TarError, OSError, EOFError, ValueError, UnicodeDecodeError) as exc:
            raise BackupError(
                f"Cannot read {archive.name}",
                details=f"{exc}. The archive is corrupted or was not written by WASM.",
            ) from exc
        if not isinstance(data, dict):
            raise BackupError(
                "Backup manifest is not an object",
                details="The archive was not written by WASM.",
            )
        return data

    def _warn_about_missing_environment(self, app_path: Path) -> None:
        """
        Tell the operator that dependencies still have to be installed.

        A backup deliberately excludes ``node_modules`` and the virtualenv: both
        are machine-specific, and a virtualenv full of absolute symlinks is one
        the extractor would refuse outright. The restored tree is therefore
        complete but not runnable, and saying so beats a service that fails to
        start with a stack trace about a missing module.

        Args:
            app_path: Directory the application was restored into.
        """
        app_type = self._detect_app_type(app_path)
        if app_type == "python" and not (app_path / "venv").is_dir():
            self.logger.warning(
                "The restored application has no virtualenv: a venv holds absolute paths "
                "and is never archived. Run 'wasm update <domain>' to rebuild it."
            )
        elif app_type in {"nextjs", "nodejs", "vite"} and not (app_path / "node_modules").is_dir():
            self.logger.warning(
                "The restored application has no node_modules: dependencies are not archived. "
                "Run 'wasm update <domain>' to install them."
            )

    def _stop_service_for_restore(self, app_name: str, stop_service: bool) -> bool:
        """
        Stop the application's service before its files are replaced.

        Args:
            app_name: Application name.
            stop_service: Whether the caller asked for the service to be stopped.

        Returns:
            True when the service was running and must be started again.
        """
        if not stop_service:
            return False
        try:
            status = self.service_manager.get_status(app_name)
            running = bool(status.get("active", False))
            if running:
                self.logger.debug(f"Stopping service: {app_name}")
                self.service_manager.stop(app_name)
            return running
        except ServiceError as exc:
            self.logger.warning(f"Could not stop service before restore: {exc}")
            return False

    def _swap_in_tree(self, app_root: Path, app_path: Path, workspace: Path) -> None:
        """
        Replace the deployed tree with the extracted one, or put it back.

        Args:
            app_root: Extracted application tree.
            app_path: Directory the application is deployed in.
            workspace: Temporary directory used to hold the previous tree.

        Raises:
            BackupError: If the safety copy cannot be made, or if the swap
                fails. The previous tree is restored first when one existed.
        """
        safety_copy = workspace / "previous"
        had_existing = app_path.exists()

        if had_existing:
            self.logger.debug("Copying the current state aside in case the restore fails")
            try:
                self.fs.copy_tree(app_path, safety_copy)
            except OSError as exc:
                # Carrying on here used to degrade this to a warning and then
                # delete the deployed tree anyway, with nothing to roll back
                # to. A restore that cannot protect what is already running
                # must not destroy it: refusing costs the operator a retry,
                # continuing costs them the application.
                raise BackupError(
                    f"Refusing to restore over {app_path}: the current state could not be "
                    "copied aside first",
                    details=(
                        f"{exc}. Free space or fix permissions under {workspace.parent}, or "
                        f"move {app_path} out of the way yourself and restore again."
                    ),
                ) from exc

        try:
            if app_path.exists():
                self.fs.remove_tree(app_path)
            self.fs.make_dir(app_path.parent)
            self.fs.move(app_root, app_path)
        except OSError as exc:
            if had_existing and safety_copy.exists():
                self.logger.warning("Restore failed, rolling back to the previous state")
                try:
                    if app_path.exists():
                        self.fs.remove_tree(app_path)
                    self.fs.move(safety_copy, app_path)
                    self.logger.info("Rollback successful - original state restored")
                except OSError as rollback_error:
                    self.logger.error(f"Rollback failed: {rollback_error}")
                    self.logger.error(f"The previous tree is still at {safety_copy}")
            raise BackupError(
                f"Failed to restore files into {app_path}",
                details=str(exc),
            ) from exc

    def _read_manifest(self, extracted: Path) -> dict[str, Any] | None:
        """
        Read the manifest an archive carries about itself.

        Args:
            extracted: Directory the archive was extracted into.

        Returns:
            The manifest, or None for a 1.x archive that has none.

        Raises:
            BackupError: If the manifest exists but is not readable JSON.
        """
        manifest_file = extracted / PAYLOAD_DIR / MANIFEST_NAME
        if not manifest_file.is_file():
            return None
        try:
            data = json.loads(manifest_file.read_text())
        except (OSError, ValueError) as exc:
            raise BackupError(
                "Backup manifest is unreadable",
                details=f"{exc}. The archive is corrupted or was not written by WASM.",
            ) from exc
        if not isinstance(data, dict):
            raise BackupError(
                "Backup manifest is not an object",
                details="The archive was not written by WASM.",
            )
        return data

    @staticmethod
    def _manifest_domain(
        manifest: dict[str, Any] | None,
        fallback: BackupMetadata | None,
    ) -> str:
        """
        Work out which application an archive belongs to.

        Args:
            manifest: Manifest read from inside the archive.
            fallback: Metadata sidecar, for archives with no manifest.

        Returns:
            The domain to restore into.

        Raises:
            BackupError: When neither source names a domain.
        """
        domain = (manifest or {}).get("domain") or (fallback.domain if fallback else None)
        if not domain or not isinstance(domain, str):
            raise BackupError(
                "This archive does not say which application it belongs to",
                details="Pass --target-domain to restore it explicitly.",
            )
        return domain

    def _locate_app_root(
        self,
        extracted: Path,
        manifest: dict[str, Any] | None,
        fallback: BackupMetadata | None,
    ) -> Path:
        """
        Find the application tree inside an extracted archive.

        Args:
            extracted: Directory the archive was extracted into.
            manifest: Manifest read from inside the archive.
            fallback: Metadata sidecar, for archives with no manifest.

        Returns:
            The directory holding the application tree.

        Raises:
            BackupError: When the archive holds no application tree, or more
                than one and nothing says which is the right one.
        """
        candidates = [
            name
            for name in ((manifest or {}).get("app_root"), fallback.app_name if fallback else None)
            if isinstance(name, str) and name
        ]
        for name in candidates:
            try:
                root = resolve_within(extracted, name)
            except (ValidationError, SecurityError) as exc:
                raise BackupError(
                    f"Backup names an application root outside the archive: {name!r}",
                    details=str(exc),
                ) from exc
            if root.is_dir():
                return root

        directories = [
            entry for entry in extracted.iterdir() if entry.is_dir() and entry.name != PAYLOAD_DIR
        ]
        if len(directories) == 1:
            return directories[0]

        raise BackupError(
            "Backup archive does not contain a single application directory",
            details=f"Found: {sorted(entry.name for entry in directories) or 'nothing'}.",
        )

    # -- deletion and retention ------------------------------------------

    def _sidecar_paths(self, metadata: BackupMetadata, app_backup_dir: Path) -> list[Path]:
        """
        Collect the extra files a 1.x backup left next to its archive.

        Paths recorded in metadata are attacker-influenced in the sense that a
        metadata file may have been written by an older, buggier version or
        edited by hand, so only paths that resolve inside the application's own
        backup directory are ever returned.

        Args:
            metadata: Metadata of the backup being deleted.
            app_backup_dir: The application's backup directory.

        Returns:
            The sidecar files that belong to this backup.
        """
        recorded = [entry.get("path") for entry in metadata.docker_volume_backups]
        recorded += [entry.get("backup_path") for entry in metadata.database_backups]

        paths: list[Path] = []
        for raw in recorded:
            if not raw or not isinstance(raw, str):
                continue
            candidate = Path(raw)
            try:
                inside = resolve_within(app_backup_dir, candidate.name)
            except (ValidationError, SecurityError):
                self.logger.debug(f"Ignoring recorded path with an unusable name: {raw}")
                continue
            if candidate.resolve() != inside:
                # The recorded file lives somewhere else entirely: whatever it
                # is, it is not this backup's to delete.
                self.logger.debug(f"Leaving alone a recorded path outside the backup dir: {raw}")
                continue
            paths.append(inside)
        return paths

    def _prune_orphan_metadata(self, app_name: str) -> int:
        """
        Delete metadata sidecars whose archive is gone.

        A sidecar with no archive is invisible to :meth:`list_backups`, so it
        would otherwise survive every rotation forever and keep claiming a
        backup that no longer exists. Archives without a sidecar are left alone:
        they are still restorable with :meth:`restore_archive`.

        Args:
            app_name: Application name.

        Returns:
            Number of sidecars removed.
        """
        app_backup_dir = self._get_app_backup_dir(app_name)
        if not app_backup_dir.is_dir():
            return 0

        removed = 0
        for metadata_file in app_backup_dir.glob("*.json"):
            if (app_backup_dir / f"{metadata_file.stem}.tar.gz").is_file():
                continue
            try:
                self.fs.remove(metadata_file)
                removed += 1
                self.logger.debug(f"Removed orphan backup metadata: {metadata_file.name}")
            except OSError as exc:
                self.logger.warning(f"Failed to delete {metadata_file}: {exc}")
        return removed

    def delete(self, backup_id: str) -> bool:
        """
        Delete a backup and everything that belongs to it.

        Every unlink goes through :mod:`wasm.core.fs`. This method is the reason
        that seam exists: ``wasm --dry-run backup delete <id> --force`` printed
        "no changes will be made to this machine" and then removed the archive,
        because a deletion never goes near a subprocess.

        Args:
            backup_id: Backup identifier.

        Returns:
            True if the backup was deleted.

        Raises:
            BackupError: If the backup does not exist.
        """
        metadata = self.get_backup(backup_id)
        if not metadata:
            raise BackupError(
                f"Backup not found: {backup_id}",
                details="Run 'wasm backup list' to see the backups WASM knows about.",
            )

        app_backup_dir = self._get_app_backup_dir(domain_to_app_name(metadata.domain))

        targets = [
            app_backup_dir / f"{backup_id}.tar.gz",
            app_backup_dir / f"{backup_id}.json",
            *self._sidecar_paths(metadata, app_backup_dir),
        ]

        for path in targets:
            try:
                self.fs.remove(path)
            except OSError as exc:
                self.logger.warning(f"Failed to delete {path}: {exc}")

        self.logger.debug(f"Deleted backup: {backup_id}")
        return True

    def _rotate_backups(self, app_name: str) -> None:
        """
        Keep only the most recent backups of an application.

        Args:
            app_name: Application name.
        """
        self._prune_orphan_metadata(app_name)
        backups = self.list_backups(app_name=app_name)

        if len(backups) > self.max_backups:
            for backup in backups[self.max_backups :]:
                try:
                    self.delete(backup.id)
                    self.logger.debug(f"Rotated old backup: {backup.id}")
                except BackupError as exc:
                    self.logger.warning(f"Failed to rotate backup {backup.id}: {exc}")

    def rotate_by_policy(
        self,
        app_name: str,
        max_count: int | None = None,
        max_age_days: int | None = None,
    ) -> int:
        """
        Rotate backups by count and/or age.

        Args:
            app_name: Application name.
            max_count: Maximum number of backups to keep.
            max_age_days: Maximum age of a backup, in days.

        Returns:
            Number of backups deleted.
        """
        from datetime import timedelta

        self._prune_orphan_metadata(app_name)
        deleted = 0

        if max_age_days:
            cutoff = datetime.now() - timedelta(days=max_age_days)
            for backup in self.list_backups(app_name=app_name):
                try:
                    if datetime.fromisoformat(backup.created_at) < cutoff:
                        self.delete(backup.id)
                        deleted += 1
                        self.logger.debug(f"Rotated old backup (age): {backup.id}")
                except (BackupError, ValueError) as exc:
                    self.logger.warning(f"Failed to rotate backup {backup.id}: {exc}")

        if max_count:
            remaining = self.list_backups(app_name=app_name)
            for backup in remaining[max_count:]:
                try:
                    self.delete(backup.id)
                    deleted += 1
                    self.logger.debug(f"Rotated old backup (count): {backup.id}")
                except BackupError as exc:
                    self.logger.warning(f"Failed to rotate backup {backup.id}: {exc}")

        return deleted

    # -- databases --------------------------------------------------------

    def _dump_databases(
        self,
        domain: str,
        destination: Path,
        *,
        redis_method: str = "rdb",
    ) -> list[dict[str, Any]]:
        """
        Dump every database of an application into the archive payload.

        A dump that fails is fatal: an archive whose metadata claims a database
        it does not carry is the bug this whole module was rewritten for.

        Args:
            domain: Domain name of the application.
            destination: Directory inside the payload to write the dumps into.
            redis_method: Method handed to Redis-like engines.

        Returns:
            One entry per database actually dumped, each pointing at a path
            *inside* the archive. Empty when the application has no database.

        Raises:
            BackupError: If a registered database could not be dumped.
        """
        # The engine managers pull in optional client libraries; a machine
        # without them can still back up files.
        try:
            from wasm.managers.database.registry import DatabaseRegistry
        except ImportError as exc:
            raise BackupError(
                "Database backup requested but the database managers are unavailable",
                details=f"{exc}. Install the database extras or drop --include-databases.",
            ) from exc

        try:
            app = get_store().get_app(domain)
        except (WASMError, sqlite3.Error) as exc:
            raise BackupError(
                f"Could not read the application record for {domain}",
                details=str(exc),
            ) from exc

        if not app or not app.id:
            self.logger.debug(f"No app found in store for {domain}")
            return []

        databases = get_store().list_databases(app_id=app.id)
        if not databases:
            self.logger.debug(f"No databases associated with {domain}")
            return []

        self.fs.make_dir(destination, mode=SECRET_DIR_MODE)

        self.logger.info(f"Backing up {len(databases)} database(s) for {domain}")

        dumps: list[dict[str, Any]] = []
        for db in databases:
            manager = DatabaseRegistry.get(db.engine, verbose=self.verbose)
            if not manager:
                raise BackupError(
                    f"No manager available for database engine: {db.engine}",
                    details=f"Database '{db.name}' is registered for {domain} but cannot be dumped.",
                )

            if not manager.is_installed():
                raise BackupError(
                    f"{db.engine} is not installed, cannot back up '{db.name}'",
                    details=f"Install {db.engine} or run the backup without --include-databases.",
                )

            suffix = getattr(manager, "BACKUP_SUFFIX", ".dump")
            filename = validate_filename(f"{db.engine}-{db.name}{suffix}.gz")
            target = destination / filename

            kwargs: dict[str, Any] = {}
            if db.engine.lower() in {"redis", "valkey"}:
                kwargs["method"] = redis_method

            try:
                info = manager.backup(database=db.name, output_path=target, compress=True, **kwargs)
            except (DatabaseError, OSError) as exc:
                raise BackupError(
                    f"Failed to back up {db.engine} database '{db.name}'",
                    details=str(exc),
                ) from exc

            written = Path(info.path)
            if written != target:
                # An engine that ignores output_path would leave the dump
                # outside the archive, which is exactly what must not happen.
                self.fs.move(written, target)

            if not target.is_file():
                raise BackupError(
                    f"Dump of '{db.name}' did not reach the backup archive",
                    details=f"{db.engine} reported success but {target} does not exist.",
                )

            dumps.append(
                {
                    "engine": db.engine,
                    "name": db.name,
                    "archive_path": f"{PAYLOAD_DIR}/{DATABASES_DIR}/{filename}",
                    "size_bytes": target.stat().st_size,
                    "created": datetime.now().isoformat(),
                }
            )

            self.logger.info(f"  Backed up {db.engine} database: {db.name}")

        return dumps

    def _payload_entries(
        self,
        manifest: dict[str, Any] | None,
        fallback: BackupMetadata | None,
        key: str,
        legacy: str,
    ) -> list[dict[str, Any]]:
        """
        Read one list of payload entries from the manifest or the sidecar.

        Args:
            manifest: Manifest read from inside the archive.
            fallback: Metadata sidecar, for archives with no manifest.
            key: Manifest key holding the entries.
            legacy: Attribute of the metadata holding the same entries.

        Returns:
            The entries, or an empty list.
        """
        if manifest is not None:
            entries = manifest.get(key, [])
            return [entry for entry in entries if isinstance(entry, dict)]
        if fallback is not None:
            return list(getattr(fallback, legacy, []))
        return []

    def _resolve_payload_file(self, extracted: Path, entry: dict[str, Any], label: str) -> Path:
        """
        Locate the file an archive entry points at.

        Args:
            extracted: Directory the archive was extracted into.
            entry: Manifest or metadata entry.
            label: What the entry describes, for error messages.

        Returns:
            The path of the file inside the extracted archive, or the external
            path recorded by a 1.x backup when the archive carries none.

        Raises:
            BackupError: If the entry points nowhere usable.
        """
        relative = entry.get("archive_path")
        if isinstance(relative, str) and relative:
            try:
                path = resolve_within(extracted, relative)
            except (ValidationError, SecurityError) as exc:
                raise BackupError(
                    f"Backup entry for {label} points outside the archive",
                    details=str(exc),
                ) from exc
            if path.is_file():
                return path
            raise BackupError(
                f"Backup claims a {label} it does not carry: {relative}",
                details="The archive is incomplete; do not trust it as a backup.",
            )

        # A 1.x archive kept its dumps outside. Restoring one is only possible
        # on the machine that created it, which is the defect, not the design.
        external = entry.get("backup_path") or entry.get("path")
        if isinstance(external, str) and external and Path(external).is_file():
            self.logger.warning(
                f"Restoring {label} from {external}, which lives outside the archive. "
                "Re-create this backup to make it portable."
            )
            return Path(external)

        raise BackupError(
            f"Backup lists a {label} that cannot be found",
            details=f"Recorded location: {external or 'none'}.",
        )

    def _restore_databases(
        self,
        manifest: dict[str, Any] | None,
        extracted: Path,
        fallback: BackupMetadata | None,
    ) -> None:
        """
        Restore every database dump the archive carries.

        Args:
            manifest: Manifest read from inside the archive.
            extracted: Directory the archive was extracted into.
            fallback: Metadata sidecar, for archives with no manifest.

        Raises:
            BackupError: If a dump is missing or an engine refuses it.
        """
        entries = self._payload_entries(manifest, fallback, "databases", "database_backups")
        if not entries:
            return

        try:
            from wasm.managers.database.registry import DatabaseRegistry
        except ImportError as exc:
            raise BackupError(
                "This backup contains databases but the database managers are unavailable",
                details=f"{exc}. Install the database extras before restoring.",
            ) from exc

        self.logger.info(f"Restoring {len(entries)} database(s)")

        for entry in entries:
            engine = str(entry.get("engine", ""))
            name = str(entry.get("name", ""))
            dump_path = self._resolve_payload_file(extracted, entry, f"dump of '{name}'")

            manager = DatabaseRegistry.get(engine, verbose=self.verbose)
            if not manager:
                raise BackupError(
                    f"No manager available for database engine: {engine}",
                    details=f"Cannot restore database '{name}'.",
                )

            try:
                manager.restore(database=name, backup_path=dump_path)
            except (DatabaseError, OSError) as exc:
                raise BackupError(
                    f"Failed to restore {engine} database '{name}' from {dump_path.name}",
                    details=str(exc),
                ) from exc

            self.logger.info(f"  Restored {engine} database: {name}")

    # -- docker volumes ---------------------------------------------------

    def _dump_docker_volumes(
        self,
        volume_names: Sequence[str],
        destination: Path,
    ) -> list[dict[str, Any]]:
        """
        Archive Docker volumes into the backup payload.

        A throw-away Alpine container tars the volume contents, because the
        volume is only reachable from inside Docker.

        Args:
            volume_names: Named volumes to archive.
            destination: Directory inside the payload to write into.

        Returns:
            One entry per volume actually archived.
        """
        # A volume holds application state, which is as likely to hold a
        # credential as a database dump does.
        self.fs.make_dir(destination, mode=SECRET_DIR_MODE)

        volume_backups: list[dict[str, Any]] = []
        for vol_name in volume_names:
            try:
                filename = validate_filename(f"{vol_name}.tar.gz")
            except ValidationError as exc:
                self.logger.warning(f"Skipping Docker volume with an unusable name: {exc}")
                continue

            target = destination / filename
            result = self._exec(
                [
                    "docker",
                    "run",
                    "--rm",
                    "-v",
                    f"{vol_name}:/data:ro",
                    "-v",
                    f"{destination}:/backup",
                    "alpine",
                    "tar",
                    "czf",
                    f"/backup/{filename}",
                    "-C",
                    "/data",
                    ".",
                ],
                timeout=_DOCKER_TIMEOUT,
            )

            if not result.success:
                self.logger.warning(f"Failed to backup volume {vol_name}: {result.stderr}")
                continue

            if not target.is_file():
                self.logger.warning(
                    f"Docker reported success but produced no archive for volume {vol_name}"
                )
                continue

            volume_backups.append(
                {
                    "volume": vol_name,
                    "archive_path": f"{PAYLOAD_DIR}/{VOLUMES_DIR}/{filename}",
                    "size_bytes": target.stat().st_size,
                }
            )
            self.logger.debug(f"Backed up volume: {vol_name}")

        return volume_backups

    def _discover_docker_volumes(self, app_path: Path) -> list[str]:
        """
        Discover named Docker volumes from a compose file.

        Args:
            app_path: Application path containing the compose file.

        Returns:
            The named volumes declared by the compose file.
        """
        try:
            import yaml  # type: ignore[import-untyped]
        except ImportError:
            return []

        for compose_name in [
            "docker-compose.prod.yml",
            "docker-compose.yml",
            "compose.yml",
            "docker-compose.prod.yaml",
            "docker-compose.yaml",
            "compose.yaml",
        ]:
            compose_file = app_path / compose_name
            if compose_file.exists():
                try:
                    data = yaml.safe_load(compose_file.read_text())
                    volumes = data.get("volumes", {})
                    if isinstance(volumes, dict):
                        return list(volumes.keys())
                except (OSError, yaml.YAMLError, AttributeError) as exc:
                    self.logger.debug(f"Could not read volumes from {compose_file}: {exc}")
                break

        return []

    def _restore_docker_volumes(
        self,
        manifest: dict[str, Any] | None,
        extracted: Path,
        fallback: BackupMetadata | None,
    ) -> None:
        """
        Restore every Docker volume archive the backup carries.

        Args:
            manifest: Manifest read from inside the archive.
            extracted: Directory the archive was extracted into.
            fallback: Metadata sidecar, for archives with no manifest.

        Raises:
            BackupError: If a volume archive is missing or cannot be unpacked.
        """
        entries = self._payload_entries(manifest, fallback, "volumes", "docker_volume_backups")

        for entry in entries:
            volume = str(entry.get("volume", ""))
            archive = self._resolve_payload_file(extracted, entry, f"volume '{volume}'")

            result = self._exec(
                [
                    "docker",
                    "run",
                    "--rm",
                    "-v",
                    f"{volume}:/data",
                    "-v",
                    f"{archive.parent}:/backup:ro",
                    "alpine",
                    "tar",
                    "xzf",
                    f"/backup/{archive.name}",
                    "-C",
                    "/data",
                ],
                timeout=_DOCKER_TIMEOUT,
            )

            if not result.success:
                raise BackupError(
                    f"Failed to restore Docker volume: {volume}",
                    details=result.stderr,
                )

            self.logger.info(f"  Restored Docker volume: {volume}")

    # -- verification -----------------------------------------------------

    def verify(self, backup_id: str, *, deep: bool = True) -> dict[str, Any]:
        """
        Verify a backup's integrity, by reading it and by unpacking it.

        The archive is hashed and walked in process: a backup that cannot be
        opened, or whose bytes changed since it was written, is reported as
        invalid before a restore needs it. When the metadata claims databases,
        the payload is checked for the dumps it names.

        Listing the members is not enough. ``restore()`` extracts through a
        hardened extractor that refuses absolute symlinks, ``..``, hardlinks to
        files outside the archive and device nodes, so an archive that lists
        cleanly can still be one ``restore()`` will *always* refuse - which is
        how every backup of a Python application containing a ``venv`` passed
        verification and failed the restore. The check therefore runs the real
        extractor, into a temporary directory that is thrown away, rather than a
        second copy of its rules that could drift from it.

        Args:
            backup_id: Backup identifier.
            deep: Unpack the archive to prove it is extractable. This costs one
                full pass over the archive and as much temporary space as the
                backup expands to, and it is skipped rather than allowed to fill
                the disk. Turning it off makes the verification cheap and much
                weaker: it then only proves the archive is readable, not
                restorable, which is the state that let every Python
                application's backup pass.

        Returns:
            A dictionary with ``valid``, ``errors``, ``warnings`` and, when the
            archive could be read, ``checksum_ok``, ``files_ok``, ``file_count``
            and ``extractable``.
        """
        errors: list[str] = []
        warnings: list[str] = []
        results: dict[str, Any] = {"valid": True, "errors": errors, "warnings": warnings}

        metadata = self.get_backup(backup_id)
        if not metadata:
            results["valid"] = False
            errors.append("Backup metadata not found")
            return results

        app_name = domain_to_app_name(metadata.domain)
        backup_file = self._get_app_backup_dir(app_name) / f"{backup_id}.tar.gz"

        if not backup_file.is_file():
            results["valid"] = False
            errors.append("Backup file not found")
            return results

        if metadata.checksum:
            try:
                if self._calculate_checksum(backup_file) != metadata.checksum:
                    results["valid"] = False
                    errors.append("Checksum mismatch: the archive changed since it was created")
                else:
                    results["checksum_ok"] = True
            except BackupError as exc:
                results["valid"] = False
                errors.append(f"Could not read the archive: {exc.message}")
                return results
        else:
            warnings.append("No checksum stored in metadata")

        try:
            with tarfile.open(backup_file, "r:gz") as archive:
                members = archive.getmembers()
            names = [member.name for member in members]
            declared_bytes = sum(member.size for member in members if member.isfile())
        except (tarfile.TarError, OSError, EOFError) as exc:
            results["valid"] = False
            errors.append(f"Archive is corrupted: {exc}")
            return results

        results["files_ok"] = True
        results["file_count"] = len(names)

        if deep:
            self._verify_extractable(backup_file, declared_bytes, results, errors, warnings)

        member_names = set(names)
        if metadata.includes_databases:
            expected = [
                entry.get("archive_path")
                for entry in metadata.database_backups
                if entry.get("archive_path")
            ]
            if not expected:
                warnings.append(
                    "This backup predates self-contained archives: its database dumps live "
                    "outside the archive and will not travel with it"
                )
            for path in expected:
                if path not in member_names:
                    results["valid"] = False
                    errors.append(f"Archive claims a database dump it does not carry: {path}")

        if metadata.size_bytes and backup_file.stat().st_size != metadata.size_bytes:
            warnings.append("Archive size differs from the size recorded in the metadata")

        results["message"] = "Backup is valid" if results["valid"] else "; ".join(errors)
        return results

    def _verify_extractable(
        self,
        backup_file: Path,
        declared_bytes: int,
        results: dict[str, Any],
        errors: list[str],
        warnings: list[str],
    ) -> None:
        """
        Prove the archive can be extracted, with the rules a restore uses.

        The archive is unpacked into a temporary directory that is removed
        immediately. That costs the space the backup expands to, so the check is
        skipped, loudly, when the temporary filesystem cannot hold it: a
        verification that quietly fills the disk it is protecting is not an
        improvement over one that lied.

        Args:
            backup_file: Archive to unpack.
            declared_bytes: Total size the archive headers claim.
            results: Verification result being assembled, updated in place.
            errors: Errors collected so far, appended to.
            warnings: Warnings collected so far, appended to.
        """
        if self._rehearsing:
            # The extractor refuses to write anything alongside a rehearsing
            # filesystem, so running it here would prove nothing and report
            # success, which is the exact defect this check was added for.
            warnings.append("Could not check that the archive is extractable: this is a dry run")
            return

        workspace = Path(tempfile.gettempdir())
        try:
            free = shutil.disk_usage(workspace).free
        except OSError as exc:
            free = 0
            self.logger.debug(f"Could not measure free space on {workspace}: {exc}")

        # A margin, because the headers are the archive's own claim about its
        # size and the last thing a restore needs is a full /tmp.
        if declared_bytes and free < declared_bytes * 1.1:
            warnings.append(
                f"Could not check that the archive is extractable: unpacking it needs about "
                f"{declared_bytes} bytes under {workspace} and only {free} are free"
            )
            return

        try:
            with tempfile.TemporaryDirectory(prefix="wasm-verify-") as tmp_dir:
                extract_archive(
                    backup_file,
                    Path(tmp_dir) / "extracted",
                    archive_format="tar.gz",
                    max_entries=self.max_entries,
                    max_total_bytes=self.max_bytes,
                )
        except SourceError as exc:
            results["valid"] = False
            results["extractable"] = False
            errors.append(f"Archive cannot be restored: {exc}")
            return
        except OSError as exc:
            warnings.append(f"Could not check that the archive is extractable: {exc}")
            return

        results["extractable"] = True

    def get_storage_usage(self) -> dict[str, Any]:
        """
        Report how much disk the backups take.

        Returns:
            Totals overall and per application.
        """
        by_app: dict[str, dict[str, int]] = {}
        usage: dict[str, Any] = {
            "total_size_bytes": 0,
            "total_backups": 0,
            "by_app": by_app,
        }

        if not self.backup_dir.exists():
            return usage

        for app_dir in self.backup_dir.iterdir():
            if not app_dir.is_dir():
                continue

            app_backups = list(app_dir.glob("*.tar.gz"))
            app_size = 0
            for backup_file in app_backups:
                try:
                    app_size += backup_file.stat().st_size
                except OSError as exc:
                    self.logger.debug(f"Could not stat {backup_file}: {exc}")

            by_app[app_dir.name] = {"count": len(app_backups), "size_bytes": app_size}
            usage["total_size_bytes"] += app_size
            usage["total_backups"] += len(app_backups)

        return usage


@runtime_checkable
class _InPlaceRebuilder(Protocol):
    """
    The part of a deployer a rollback needs: rebuild what is already on disk.

    A rollback must not re-fetch the source; the restored tree *is* the source.
    Deployers that cannot rebuild in place (a static site has nothing to build)
    simply do not satisfy this protocol.
    """

    def install_dependencies(self) -> bool:
        """
        Install the application's dependencies.

        Returns:
            True when the install succeeded.
        """

    def build(self) -> bool:
        """
        Build the application in place.

        Returns:
            True when the build succeeded.
        """


class RollbackManager:
    """
    Manager for application rollbacks.

    Provides automatic pre-deploy backups and a one-step return to a previous
    state.
    """

    def __init__(
        self,
        verbose: bool = False,
        runner: CommandRunner | None = None,
        fs: FileSystem | None = None,
    ) -> None:
        """
        Initialize the rollback manager.

        Args:
            verbose: Enable verbose logging.
            runner: Command runner passed to the managers it drives.
            fs: Filesystem passed to the backup manager it drives.
        """
        self.verbose = verbose
        # Capturable so the rollback recording can mirror what this manager
        # reports into the captured log.
        self.logger = CapturingLogger(verbose=verbose)
        self.backup_manager = BackupManager(verbose=verbose, runner=runner, fs=fs)
        self.service_manager = ServiceManager(verbose=verbose, runner=runner)
        self.config = Config()

    def create_pre_deploy_backup(
        self,
        domain: str,
        description: str = "Pre-deploy backup",
    ) -> BackupMetadata | None:
        """
        Create a backup before a deployment.

        Args:
            domain: Domain name.
            description: Backup description.

        Returns:
            The backup metadata, or None when there is nothing deployed yet.
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
        backup_id: str | None = None,
        rebuild: bool = True,
        trigger: str = DeploymentTrigger.CLI.value,
    ) -> bool:
        """
        Roll an application back to a previous state.

        The rollback is recorded in the deployment history as **its own row**,
        with its outcome and its captured log, never by rewriting the history
        of the deploy it reverts: an operator triggered an operation and the
        history must say so. On success the domain's most recent ``success``
        row - the build this rollback discards - is additionally flipped to
        ``rolled_back`` when one exists, so the timeline shows which
        deployment stopped serving. Refusals before anything is attempted (no
        backup to roll back to) are not recorded: nothing ran.

        Args:
            domain: Domain name.
            backup_id: Specific backup (defaults to the latest manual one).
            rebuild: Rebuild the application after the restore.
            trigger: What initiated the rollback, recorded in the history:
                ``cli`` (the default), ``panel`` or ``webhook``.

        Returns:
            True if the rollback succeeded.

        Raises:
            BackupError: If there is no backup to roll back to, or the restore
                fails.
        """
        if backup_id:
            metadata = self.backup_manager.get_backup(backup_id)
            if not metadata:
                raise BackupError(f"Backup not found: {backup_id}")
        else:
            all_backups = self.backup_manager.list_backups(domain=domain)
            metadata = None
            for backup in all_backups:
                # Skip auto-generated safety backups.
                if "auto" not in backup.tags and "pre-deploy" not in backup.tags:
                    metadata = backup
                    break

            if not metadata:
                if all_backups:
                    # All of them are automatic: the oldest is the most stable.
                    metadata = all_backups[-1]
                else:
                    raise BackupError(f"No backups found for: {domain}")

        recorder = DeploymentRecorder(get_store(), domain, trigger, logger=self.logger)
        recorder.start()
        recorder.annotate(git_commit=metadata.git_commit, git_branch=metadata.git_branch)

        try:
            self.logger.info(f"Rolling back to: {metadata.id}")
            self.logger.info(f"  Created: {metadata.age}")
            if metadata.git_commit:
                self.logger.info(f"  Commit: {metadata.git_commit}")

            self.backup_manager.restore(
                backup_id=metadata.id,
                restore_env=True,
                stop_service=True,
            )

            app_name = domain_to_app_name(domain)

            if rebuild:
                app_path = self.config.apps_directory / app_name

                self.logger.info("Rebuilding application...")

                from wasm.deployers import detect_app_type, get_deployer

                app_type = detect_app_type(app_path, verbose=self.verbose)
                if app_type:
                    deployer = get_deployer(app_type, verbose=self.verbose)
                    # The rebuild happens through the deployer's own logger;
                    # capturing it puts the build output in this rollback's
                    # log. The interface does not promise a logger, so one
                    # that is missing or not capturable is simply not mirrored.
                    delegate_logger = getattr(deployer, "logger", None)
                    if isinstance(delegate_logger, Logger):
                        recorder.also_capture(delegate_logger)
                    # The source is already on disk: this is a rebuild in place,
                    # not a deployment, so the restored directory is its own
                    # source.
                    deployer.configure(domain, source=str(app_path), app_path=app_path)

                    if isinstance(deployer, _InPlaceRebuilder):
                        try:
                            deployer.install_dependencies()
                            deployer.build()
                        except WASMError as exc:
                            self.logger.warning(f"Rebuild failed: {exc}")
                            self.logger.info("Application restored but may need manual rebuild")
                    else:
                        self.logger.warning(
                            f"The {app_type} deployer cannot rebuild in place; "
                            "the files are restored but not rebuilt"
                        )

            try:
                status = self.service_manager.get_status(app_name)
                if status.get("exists"):
                    self.service_manager.start(app_name)
            except ServiceError as exc:
                self.logger.debug(f"Could not start service: {exc}")
        except Exception as exc:
            # Not handling: the failure is recorded and re-raised unchanged.
            recorder.finish_failure(exc)
            raise

        # The restore succeeded, so the newest successful deployment is the
        # build that just stopped serving.
        recorder.mark_previous_success_rolled_back()
        recorder.finish_success()
        return True

    def list_rollback_points(self, domain: str) -> list[BackupMetadata]:
        """
        List the backups an application can be rolled back to.

        Args:
            domain: Domain name.

        Returns:
            The available backups, newest first.
        """
        return self.backup_manager.list_backups(domain=domain)
