# Copyright (c) 2024-2026 Yago Lopez Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
Source code manager for WASM.

This module is the door through which third-party code enters a machine where
WASM runs as root, so it is written defensively:

- **Archives are extracted member by member, never with ``extractall``.** A tar
  or zip entry can name ``../../etc/systemd/system/evil.service``, carry a
  symlink to ``/etc``, or be a device node; ``extractall`` writes all of it.
  Every member goes through :func:`extract_archive`, which resolves the target
  inside the destination, refuses links that leave it, drops setuid bits and
  caps both the member count and the number of bytes written.
- **Downloads only speak http(s).** ``file://``, ``ftp://`` and redirects that
  change scheme are refused, and TLS certificates are verified.
- **Git URLs are validated before they reach argv.** ``ext::`` and friends turn
  a URL into a command, and a URL starting with ``-`` becomes an option, so
  both are rejected and every clone passes ``--`` before the URL.
- **Processes go through the CommandRunner**, never through ``subprocess``.
"""

from __future__ import annotations

import os
import re
import shutil
import ssl
import stat
import tarfile
import tempfile
import zipfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import IO, Any
from urllib.error import URLError
from urllib.parse import urlparse
from urllib.request import (
    HTTPDefaultErrorHandler,
    HTTPErrorProcessor,
    HTTPHandler,
    HTTPRedirectHandler,
    HTTPSHandler,
    OpenerDirector,
    ProxyHandler,
)

from wasm.core.exceptions import SourceError
from wasm.core.runner import CommandResult, CommandRunner, get_runner
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

#: Members allowed in a single archive. A source tree is thousands of files;
#: millions of them is an inode exhaustion attack.
MAX_ARCHIVE_ENTRIES = 50_000

#: Bytes allowed out of a single archive, declared or written.
MAX_ARCHIVE_BYTES = 2 * 1024**3

#: Bytes allowed off the network for one archive.
MAX_DOWNLOAD_BYTES = 1024**3

#: A symlink target longer than this is not a path, it is a payload.
MAX_LINK_TARGET_LENGTH = 4096

DOWNLOAD_TIMEOUT = 300
GIT_TIMEOUT = 60
GIT_NETWORK_TIMEOUT = 300
GIT_CLONE_TIMEOUT = 600

_COPY_CHUNK = 1 << 16

#: The only schemes that mean "fetch these bytes over the network".
ALLOWED_ARCHIVE_SCHEMES = frozenset({"http", "https"})

#: Transports git may use. ``file`` is absent on purpose: a local repository is
#: handled by :meth:`SourceManager.copy_local`, and ``file://`` in a submodule
#: is a known escalation path (CVE-2022-39253).
ALLOWED_GIT_SCHEMES = frozenset({"http", "https", "ssh", "git"})

#: git's remote-helper syntax (``ext::``, ``fd::``, ...). ``ext::sh -c ...``
#: is remote code execution spelled as a URL.
_REMOTE_HELPER_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*::")

#: scp-like syntax, the one git URL form that has no scheme.
_SCP_LIKE_RE = re.compile(r"^[\w.+-]+@[\w.-]+:(?!/)[\w./~+-]+$")

_GIT_REF_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._/+-]*$")

_WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:")

#: Applied to every git invocation. A hostile repository can smuggle an
#: ``ext::`` or ``file::`` URL through .gitmodules and have it executed during a
#: recursive clone; these two settings close that path.
_GIT_SAFE_CONFIG = ("-c", "protocol.ext.allow=never", "-c", "protocol.file.allow=never")

#: CPython's own hardened tar filter (PEP 706), present from 3.12. Used as an
#: extra gate where available; the checks below do not depend on it, because
#: the project supports 3.10 and 3.11 where it does not exist.
_DATA_FILTER = getattr(tarfile, "data_filter", None)
_FILTER_ERRORS: tuple[type[BaseException], ...] = (
    (tarfile.FilterError,) if hasattr(tarfile, "FilterError") else ()
)

_ARCHIVE_SUFFIXES: tuple[tuple[str, str], ...] = (
    (".tar.gz", "tar.gz"),
    (".tgz", "tar.gz"),
    (".tar.bz2", "tar.bz2"),
    (".tbz2", "tar.bz2"),
    (".tar.xz", "tar.xz"),
    (".txz", "tar.xz"),
    (".tar", "tar"),
    (".zip", "zip"),
)

_TAR_MODES: dict[str, str] = {
    "tar.gz": "r:gz",
    "tar.bz2": "r:bz2",
    "tar.xz": "r:xz",
    "tar": "r:",
}


# Source URL validation ----------------------------------------------------


def validate_archive_url(url: str) -> str:
    """
    Check that a URL is safe to download an archive from.

    Args:
        url: Candidate archive URL.

    Returns:
        The URL, stripped of surrounding whitespace.

    Raises:
        SourceError: If the URL is empty, uses a scheme other than http(s), has
            no host, or contains characters that could split a header or a
            command line.
    """
    candidate = (url or "").strip()
    if not candidate:
        raise SourceError(
            "Archive URL is empty",
            details="Provide an http:// or https:// URL pointing at the archive",
        )
    if any(char in candidate for char in "\x00\r\n"):
        raise SourceError(
            "Archive URL contains control characters",
            details="Remove line breaks and NUL bytes from the source URL",
        )

    parsed = urlparse(candidate)
    scheme = parsed.scheme.lower()
    if scheme not in ALLOWED_ARCHIVE_SCHEMES:
        raise SourceError(
            f"Unsupported URL scheme for an archive: '{parsed.scheme or 'none'}'",
            details="Only http:// and https:// are downloaded; file://, ftp:// and git "
            "remote helpers such as ext:: are refused",
        )
    if not parsed.hostname:
        raise SourceError(
            f"Archive URL has no host: '{candidate}'",
            details="Use a full URL such as https://example.com/app.tar.gz",
        )
    return candidate


def validate_git_remote_url(url: str) -> str:
    """
    Check that a URL is safe to hand to ``git clone`` or ``git remote set-url``.

    Args:
        url: Candidate repository URL.

    Returns:
        The URL, stripped of surrounding whitespace.

    Raises:
        SourceError: If the URL is empty, starts with a dash, uses git's
            remote-helper syntax, or uses a transport other than
            https/http/ssh/git.
    """
    candidate = (url or "").strip()
    if not candidate:
        raise SourceError(
            "Git URL is empty",
            details="Provide a repository URL such as https://github.com/user/repo.git",
        )
    if any(char in candidate for char in "\x00\r\n"):
        raise SourceError(
            "Git URL contains control characters",
            details="Remove line breaks and NUL bytes from the repository URL",
        )
    if candidate.startswith("-"):
        raise SourceError(
            f"Git URL must not start with '-': '{candidate}'",
            details="git would parse it as an option (for example --upload-pack), "
            "not as a repository",
        )
    if _REMOTE_HELPER_RE.match(candidate):
        raise SourceError(
            f"Git remote helpers are not allowed: '{candidate}'",
            details="URLs such as ext::<command> make git execute a command; "
            "use an https:// or ssh:// repository URL",
        )

    if "://" in candidate:
        scheme = urlparse(candidate).scheme.lower()
        if scheme not in ALLOWED_GIT_SCHEMES:
            raise SourceError(
                f"Unsupported git transport: '{scheme}'",
                details="Allowed transports are https, http, ssh and git. "
                "Local repositories are deployed by path, not by file:// URL",
            )
        if not urlparse(candidate).hostname:
            raise SourceError(
                f"Git URL has no host: '{candidate}'",
                details="Use a full URL such as https://github.com/user/repo.git",
            )
        return candidate

    if _SCP_LIKE_RE.match(candidate):
        return candidate

    raise SourceError(
        f"Unrecognised git URL: '{candidate}'",
        details="Use https://host/owner/repo.git, ssh://git@host/owner/repo.git "
        "or git@host:owner/repo.git",
    )


def validate_git_ref(ref: str) -> str:
    """
    Check that a branch or tag name is safe to place in a command line.

    Args:
        ref: Candidate branch or tag name.

    Returns:
        The reference, stripped of surrounding whitespace.

    Raises:
        SourceError: If the reference is empty, starts with a dash, contains
            ``..``, or contains characters git does not accept in a ref.
    """
    candidate = (ref or "").strip()
    if not candidate:
        raise SourceError(
            "Branch name is empty",
            details="Omit the branch to use the repository default",
        )
    if candidate.startswith("-"):
        raise SourceError(
            f"Branch name must not start with '-': '{candidate}'",
            details="git would parse it as an option instead of a branch",
        )
    if ".." in candidate or not _GIT_REF_RE.match(candidate):
        raise SourceError(
            f"Invalid branch name: '{candidate}'",
            details="Use letters, digits and any of . _ / + -",
        )
    return candidate


# Download -----------------------------------------------------------------


class _SafeRedirectHandler(HTTPRedirectHandler):
    """Follows redirects only while they stay on http(s)."""

    def redirect_request(
        self,
        req: Any,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> Any:
        """
        Build the request for a redirect, refusing a change of scheme.

        Args:
            req: The original request.
            fp: The response body of the redirect.
            code: HTTP status code.
            msg: HTTP status message.
            headers: Response headers.
            newurl: The location being redirected to.

        Returns:
            The follow-up request, or None when urllib decides not to redirect.

        Raises:
            SourceError: If the redirect leaves http(s).
        """
        scheme = urlparse(newurl).scheme.lower()
        if scheme not in ALLOWED_ARCHIVE_SCHEMES:
            raise SourceError(
                f"Refusing redirect to a '{scheme or 'schemeless'}' URL",
                details="A download may only be redirected to http:// or https://",
            )
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _build_opener() -> OpenerDirector:
    """
    Build a URL opener that can only speak http and https.

    The default opener knows about ``file://`` and ``ftp://``; this one is
    assembled by hand so those handlers are simply not present.

    Returns:
        The configured opener.
    """
    opener = OpenerDirector()
    opener.add_handler(ProxyHandler())
    opener.add_handler(HTTPHandler())
    opener.add_handler(HTTPSHandler(context=ssl.create_default_context()))
    opener.add_handler(_SafeRedirectHandler())
    opener.add_handler(HTTPErrorProcessor())
    opener.add_handler(HTTPDefaultErrorHandler())
    return opener


def _open_url(url: str, timeout: int = DOWNLOAD_TIMEOUT) -> IO[bytes]:
    """
    Open a validated http(s) URL for reading.

    Args:
        url: URL already accepted by :func:`validate_archive_url`.
        timeout: Socket timeout in seconds.

    Returns:
        The response, ready to be read in chunks.

    Raises:
        SourceError: If the opener cannot handle the URL.
    """
    response = _build_opener().open(url, timeout=timeout)
    if response is None:
        raise SourceError(
            f"No handler could open the URL: {url}",
            details="Only http:// and https:// downloads are supported",
        )
    return response


def _download_to_file(
    url: str,
    destination: Path,
    *,
    timeout: int = DOWNLOAD_TIMEOUT,
    max_bytes: int = MAX_DOWNLOAD_BYTES,
) -> None:
    """
    Download a URL to a local file, refusing oversized responses.

    Args:
        url: Archive URL.
        destination: File to write. Created with owner-only permissions.
        timeout: Socket timeout in seconds.
        max_bytes: Hard cap on the number of bytes written.

    Raises:
        SourceError: If the URL is not downloadable, the transfer fails, or the
            response exceeds the cap.
    """
    validate_archive_url(url)

    try:
        response = _open_url(url, timeout)
    except URLError as exc:
        raise SourceError(f"Download failed: {url}", details=str(exc.reason)) from exc
    except OSError as exc:
        raise SourceError(f"Download failed: {url}", details=str(exc)) from exc

    written = 0
    completed = False
    fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with response, os.fdopen(fd, "wb") as sink:
            while True:
                try:
                    chunk = response.read(_COPY_CHUNK)
                except (URLError, OSError) as exc:
                    raise SourceError(f"Download failed: {url}", details=str(exc)) from exc
                if not chunk:
                    break
                written += len(chunk)
                if written > max_bytes:
                    raise SourceError(
                        f"Download exceeds {max_bytes} bytes: {url}",
                        details="Refusing to fill the disk from a remote archive",
                    )
                sink.write(chunk)
        completed = True
    finally:
        # A partial download must not be mistaken for a usable archive.
        if not completed:
            destination.unlink(missing_ok=True)


# Archive extraction -------------------------------------------------------


@dataclass
class _ExtractionBudget:
    """
    Running totals that keep an archive from exhausting the machine.

    Attributes:
        max_entries: Members allowed in the archive.
        max_total_bytes: Bytes allowed out of the archive.
        entries: Members seen so far.
        declared_bytes: Bytes the archive headers claim so far.
        written_bytes: Bytes actually written to disk so far.
    """

    max_entries: int
    max_total_bytes: int
    entries: int = 0
    declared_bytes: int = 0
    written_bytes: int = 0

    def count_entry(self) -> None:
        """
        Account for one more member.

        Raises:
            SourceError: When the member budget is exhausted.
        """
        self.entries += 1
        if self.entries > self.max_entries:
            raise SourceError(
                f"Archive contains more than {self.max_entries} entries",
                details="Refusing to extract what looks like a decompression bomb",
            )

    def count_declared(self, size: int, name: str) -> None:
        """
        Account for the size a member header claims, before reading it.

        Args:
            size: Declared uncompressed size.
            name: Member name, for the error message.

        Raises:
            SourceError: When the declared total exceeds the budget.
        """
        if size < 0:
            raise SourceError(
                f"Archive entry '{name}' declares a negative size",
                details="The archive is malformed or crafted",
            )
        self.declared_bytes += size
        self._check(self.declared_bytes, name)

    def count_written(self, size: int, name: str) -> None:
        """
        Account for bytes actually written, in case a header lied.

        Args:
            size: Bytes just written.
            name: Member name, for the error message.

        Raises:
            SourceError: When the written total exceeds the budget.
        """
        self.written_bytes += size
        self._check(self.written_bytes, name)

    def _check(self, total: int, name: str) -> None:
        """
        Fail when a running total leaves the budget.

        Args:
            total: The total to compare.
            name: Member name, for the error message.

        Raises:
            SourceError: When the total exceeds the budget.
        """
        if total > self.max_total_bytes:
            raise SourceError(
                f"Archive expands past {self.max_total_bytes} bytes at entry '{name}'",
                details="Refusing to extract what looks like a decompression bomb",
            )


def detect_archive_format(name: str) -> str:
    """
    Work out an archive format from a file name or URL path.

    Args:
        name: File name or URL path.

    Returns:
        One of ``zip``, ``tar``, ``tar.gz``, ``tar.bz2``, ``tar.xz``.

    Raises:
        SourceError: If the extension is not a supported archive format.
    """
    lowered = name.lower()
    for suffix, archive_format in _ARCHIVE_SUFFIXES:
        if lowered.endswith(suffix):
            return archive_format
    raise SourceError(
        f"Unsupported archive format: {name}",
        details="Supported formats are .zip, .tar, .tar.gz, .tgz, .tar.bz2 and .tar.xz",
    )


def _prepare_destination(destination: Path) -> Path:
    """
    Create the destination directory and return its canonical path.

    Args:
        destination: Directory to extract into.

    Returns:
        The destination with every symlink resolved, which is the root every
        member is checked against.

    Raises:
        SourceError: If the directory cannot be created.
    """
    try:
        destination.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise SourceError(
            f"Cannot create destination directory: {destination}",
            details=str(exc),
        ) from exc
    return Path(os.path.realpath(destination))


def _safe_target(name: str, root: Path) -> Path:
    """
    Resolve an archive member name to a path inside the destination.

    Args:
        name: Member name exactly as stored in the archive.
        root: Canonical destination root.

    Returns:
        The path the member may be written to. Equal to ``root`` for the
        ``.`` entry that ``tar czf archive .`` produces.

    Raises:
        SourceError: If the name is empty, holds a NUL byte, is absolute, walks
            up with ``..``, or would be written through an existing symlink.
    """
    if not name:
        raise SourceError(
            "Archive contains an entry with an empty name",
            details="The archive is malformed or crafted",
        )
    if "\x00" in name:
        raise SourceError(
            "Archive contains an entry whose name holds a NUL byte",
            details="A NUL byte truncates the path at the system-call boundary, "
            "which is how a crafted name reaches an unintended file",
        )
    if "\\" in name:
        raise SourceError(
            f"Archive entry '{name}' contains a backslash",
            details="Archive members must use '/' as separator; a backslash is "
            "ambiguous and is refused",
        )
    if name.startswith("/") or PurePosixPath(name).is_absolute() or _WINDOWS_DRIVE_RE.match(name):
        raise SourceError(
            f"Archive entry '{name}' uses an absolute path",
            details="Members must be relative to the archive root",
        )

    parts = [part for part in PurePosixPath(name).parts if part != "."]
    if any(part == ".." for part in parts):
        raise SourceError(
            f"Archive entry '{name}' escapes the destination with '..'",
            details="This is a path traversal attempt (Zip Slip / CVE-2007-4559)",
        )

    target = root
    for part in parts:
        target = target / part
        if target.is_symlink():
            raise SourceError(
                f"Archive entry '{name}' would be written through an existing symlink",
                details="Extraction never follows a link inside the destination",
            )

    resolved = Path(os.path.realpath(target))
    if resolved != root and root not in resolved.parents:
        raise SourceError(
            f"Archive entry '{name}' resolves outside the destination",
            details=f"Resolved to {resolved}, which is not under {root}",
        )
    return target


def _sanitize_mode(mode: int, default: int, *, directory: bool = False) -> int:
    """
    Reduce an archive-supplied permission set to something safe.

    Drops setuid, setgid, the sticky bit and every write bit outside the owner,
    which is what makes a mode from an untrusted archive dangerous on a host
    where WASM runs as root.

    Args:
        mode: Mode stored in the archive.
        default: Mode to use when the archive stored none.
        directory: True when the entry is a directory, which needs owner search
            permission.

    Returns:
        The sanitised permission bits.
    """
    if not mode:
        return default
    sanitized = mode & 0o755
    return sanitized | (0o700 if directory else 0o600)


def _make_directory(path: Path, mode: int) -> None:
    """
    Create a directory for an archive member.

    Args:
        path: Directory to create.
        mode: Already sanitised permission bits.

    Raises:
        SourceError: If the path exists as something other than a directory, or
            cannot be created.
    """
    if path.is_symlink() or (path.exists() and not path.is_dir()):
        raise SourceError(
            f"Archive entry '{path.name}' collides with an existing non-directory",
            details=str(path),
        )
    try:
        path.mkdir(parents=True, exist_ok=True)
        os.chmod(path, mode)
    except OSError as exc:
        raise SourceError(f"Cannot create directory: {path}", details=str(exc)) from exc


def _write_regular_file(
    source: IO[bytes],
    target: Path,
    root: Path,
    mode: int,
    budget: _ExtractionBudget,
    name: str,
) -> None:
    """
    Write one regular file from an archive stream.

    Args:
        source: Stream positioned at the member contents.
        target: Path to write, already validated.
        root: Canonical destination root.
        mode: Already sanitised permission bits.
        budget: Running extraction totals.
        name: Member name, for error messages.

    Raises:
        SourceError: If the entry names the destination itself, the target
            already exists, is a symlink, or the write fails.
    """
    if target == root:
        raise SourceError(
            f"Archive entry '{name}' is a file named after the destination itself",
            details="The archive is malformed or crafted",
        )
    _make_directory(target.parent, 0o755)

    try:
        # O_EXCL rejects duplicate members, O_NOFOLLOW refuses to write through
        # a symlink that a previous member may have created.
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    except FileExistsError as exc:
        raise SourceError(
            f"Archive contains a duplicate entry: '{name}'",
            details="Refusing to overwrite a file already written from this archive",
        ) from exc
    except OSError as exc:
        raise SourceError(f"Cannot write archive entry '{name}'", details=str(exc)) from exc

    completed = False
    try:
        with os.fdopen(fd, "wb") as sink:
            while True:
                chunk = source.read(_COPY_CHUNK)
                if not chunk:
                    break
                budget.count_written(len(chunk), name)
                sink.write(chunk)
        completed = True
    finally:
        # A file cut short by the size budget must not stay behind.
        if not completed:
            target.unlink(missing_ok=True)
    os.chmod(target, mode)


def _create_symlink(linkname: str, target: Path, root: Path) -> None:
    """
    Create a symlink from an archive member, if it stays inside the destination.

    Args:
        linkname: Link destination as stored in the archive.
        target: Path of the link itself, already validated.
        root: Canonical destination root.

    Raises:
        SourceError: If the link is absolute, too long, holds a NUL byte, or
            points outside the destination.
    """
    if not linkname or "\x00" in linkname:
        raise SourceError(
            f"Archive symlink '{target.name}' has an unusable target",
            details="Empty targets and NUL bytes are refused",
        )
    if len(linkname) > MAX_LINK_TARGET_LENGTH:
        raise SourceError(
            f"Archive symlink '{target.name}' has an oversized target",
            details=f"Link targets are limited to {MAX_LINK_TARGET_LENGTH} characters",
        )
    if linkname.startswith("/") or PurePosixPath(linkname).is_absolute():
        raise SourceError(
            f"Archive symlink '{target.name}' points to the absolute path '{linkname}'",
            details="An absolute link would expose a file outside the deployment",
        )

    # The parent has already been checked to contain no symlink, so resolving
    # the link lexically is enough and cannot be fooled by a link chain.
    resolved = Path(os.path.normpath(target.parent / linkname))
    if resolved != root and root not in resolved.parents:
        raise SourceError(
            f"Archive symlink '{target.name}' points outside the destination",
            details=f"Target resolves to {resolved}, which is not under {root}",
        )

    _make_directory(target.parent, 0o755)
    if target.is_symlink() or target.exists():
        raise SourceError(
            f"Archive contains a duplicate entry: '{target.name}'",
            details="Refusing to replace a path already written from this archive",
        )
    try:
        os.symlink(linkname, target)
    except OSError as exc:
        raise SourceError(f"Cannot create symlink: {target}", details=str(exc)) from exc


def _create_hardlink(linkname: str, target: Path, root: Path) -> None:
    """
    Create a hardlink from an archive member, if its source is inside the tree.

    Args:
        linkname: Name of the member being linked to.
        target: Path of the link itself, already validated.
        root: Canonical destination root.

    Raises:
        SourceError: If the link source is outside the destination or was not
            extracted from this archive.
    """
    source = _safe_target(linkname, root)
    if not source.is_file() or source.is_symlink():
        raise SourceError(
            f"Archive hardlink '{target.name}' points to '{linkname}', which was not extracted",
            details="A hardlink may only reference a regular file from the same archive",
        )
    _make_directory(target.parent, 0o755)
    try:
        os.link(source, target)
    except OSError as exc:
        raise SourceError(f"Cannot create hardlink: {target}", details=str(exc)) from exc


def _apply_native_filter(member: tarfile.TarInfo, root: Path) -> None:
    """
    Run CPython's own tar data filter when the interpreter provides one.

    Args:
        member: The member about to be extracted.
        root: Canonical destination root.

    Raises:
        SourceError: If the interpreter's filter rejects the member.
    """
    if _DATA_FILTER is None:
        return
    try:
        _DATA_FILTER(member, str(root))
    except _FILTER_ERRORS as exc:
        raise SourceError(
            f"Archive entry '{member.name}' rejected by the tar data filter",
            details=str(exc),
        ) from exc


def _extract_tar_members(
    archive: tarfile.TarFile,
    root: Path,
    budget: _ExtractionBudget,
) -> None:
    """
    Extract a tar archive one validated member at a time.

    Args:
        archive: Open tar archive.
        root: Canonical destination root.
        budget: Running extraction totals.

    Raises:
        SourceError: On any member that is unsafe to extract.
    """
    for member in archive:
        budget.count_entry()
        _apply_native_filter(member, root)
        target = _safe_target(member.name, root)

        if member.isdir():
            _make_directory(target, _sanitize_mode(member.mode, 0o755, directory=True))
        elif member.issym():
            _create_symlink(member.linkname, target, root)
        elif member.islnk():
            _create_hardlink(member.linkname, target, root)
        elif member.isfile():
            budget.count_declared(member.size, member.name)
            source = archive.extractfile(member)
            if source is None:
                raise SourceError(
                    f"Archive entry '{member.name}' has no readable content",
                    details="The archive is malformed or crafted",
                )
            with source:
                _write_regular_file(
                    source,
                    target,
                    root,
                    _sanitize_mode(member.mode, 0o644),
                    budget,
                    member.name,
                )
        else:
            raise SourceError(
                f"Archive entry '{member.name}' is a device, socket or FIFO",
                details="Only regular files, directories and links inside the "
                "destination are extracted",
            )


def _extract_zip_members(
    archive: zipfile.ZipFile,
    root: Path,
    budget: _ExtractionBudget,
) -> None:
    """
    Extract a zip archive one validated member at a time.

    ``ZipFile.extractall`` sanitises ``..`` silently and writes unix symlinks
    out as plain files; neither is acceptable here, so members are handled
    directly and a traversal attempt fails loudly.

    Args:
        archive: Open zip archive.
        root: Canonical destination root.
        budget: Running extraction totals.

    Raises:
        SourceError: On any member that is unsafe to extract.
    """
    for info in archive.infolist():
        budget.count_entry()
        if info.flag_bits & 0x1:
            raise SourceError(
                f"Archive entry '{info.filename}' is encrypted",
                details="Encrypted archives are not supported as a deployment source",
            )
        target = _safe_target(info.filename, root)
        # The unix mode lives in the top 16 bits of the external attributes,
        # and is zero for archives created on systems without one.
        raw_mode = (info.external_attr >> 16) & 0xFFFF

        # Most tools mark a directory with a trailing slash, some only with the
        # mode bits, so both have to be recognised.
        if info.is_dir() or (stat.S_ISDIR(raw_mode) and info.file_size == 0):
            _make_directory(target, _sanitize_mode(raw_mode & 0o777, 0o755, directory=True))
            continue

        if info.create_system == 3 and stat.S_ISLNK(raw_mode):
            if info.file_size > MAX_LINK_TARGET_LENGTH:
                raise SourceError(
                    f"Archive symlink '{info.filename}' has an oversized target",
                    details=f"Link targets are limited to {MAX_LINK_TARGET_LENGTH} characters",
                )
            linkname = archive.read(info).decode("utf-8", errors="replace")
            _create_symlink(linkname, target, root)
            continue

        if raw_mode and stat.S_IFMT(raw_mode) not in (0, stat.S_IFREG):
            raise SourceError(
                f"Archive entry '{info.filename}' is not a regular file",
                details="Only regular files, directories and links inside the "
                "destination are extracted",
            )

        budget.count_declared(info.file_size, info.filename)
        with archive.open(info) as source:
            _write_regular_file(
                source,
                target,
                root,
                _sanitize_mode(raw_mode & 0o777, 0o644),
                budget,
                info.filename,
            )


def extract_archive(
    archive: Path,
    destination: Path,
    *,
    archive_format: str | None = None,
    max_entries: int = MAX_ARCHIVE_ENTRIES,
    max_total_bytes: int = MAX_ARCHIVE_BYTES,
) -> None:
    """
    Extract an archive into a directory without letting it escape.

    Every member is validated before anything is written: no absolute paths, no
    ``..``, no writing through symlinks, no links pointing outside, no device
    nodes or FIFOs, no setuid bits, and hard caps on member count and size.

    Args:
        archive: Path to the archive on disk.
        destination: Directory to extract into. Created if missing.
        archive_format: Format override. Detected from the file name when None.
        max_entries: Maximum number of members.
        max_total_bytes: Maximum number of bytes to write.

    Raises:
        SourceError: If the archive is malformed, of an unsupported format, or
            contains any member that is unsafe to extract.
    """
    resolved_format = archive_format or detect_archive_format(archive.name)
    root = _prepare_destination(destination)
    budget = _ExtractionBudget(max_entries=max_entries, max_total_bytes=max_total_bytes)

    if resolved_format == "zip":
        try:
            with zipfile.ZipFile(archive) as zip_archive:
                _extract_zip_members(zip_archive, root, budget)
        except (zipfile.BadZipFile, OSError) as exc:
            raise SourceError(f"Cannot read zip archive: {archive.name}", details=str(exc)) from exc
        return

    mode = _TAR_MODES.get(resolved_format)
    if mode is None:
        raise SourceError(
            f"Unsupported archive format: {resolved_format}",
            details="Supported formats are .zip, .tar, .tar.gz, .tgz, .tar.bz2 and .tar.xz",
        )
    try:
        with tarfile.open(archive, mode) as tar_archive:
            _extract_tar_members(tar_archive, root, budget)
    except (tarfile.TarError, OSError) as exc:
        raise SourceError(f"Cannot read tar archive: {archive.name}", details=str(exc)) from exc


class SourceManager(BaseManager):
    """
    Manager for source code operations.

    Handles cloning Git repositories, downloading archives,
    and copying local directories.
    """

    def __init__(self, verbose: bool = False, runner: CommandRunner | None = None):
        """
        Initialize source manager.

        Args:
            verbose: Enable verbose logging.
            runner: Command runner to execute git with. Defaults to the
                process-wide runner.
        """
        super().__init__(verbose=verbose)
        self.runner = runner or get_runner()

    def _git(
        self,
        args: Sequence[str],
        *,
        cwd: Path | None = None,
        timeout: int = GIT_TIMEOUT,
    ) -> CommandResult:
        """
        Run a git command through the command runner.

        Args:
            args: Arguments after ``git``.
            cwd: Working directory.
            timeout: Deadline in seconds.

        Returns:
            The command outcome.
        """
        argv = ["git", *_GIT_SAFE_CONFIG, *args]
        self.logger.debug(f"Running: {' '.join(argv)}")
        result = self.runner.run(argv, cwd=cwd, timeout=timeout)
        self.logger.command_output(result.stdout, result.stderr)
        return result

    def is_installed(self) -> bool:
        """Check if Git is installed."""
        return self.runner.exists("git")

    def get_version(self) -> str | None:
        """Get Git version."""
        result = self._git(["--version"])
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

        Raises:
            SourceError: If the URL is unsafe or the update fails.
        """
        safe_url = validate_git_remote_url(url)
        safe_branch = validate_git_ref(branch) if branch else None

        # Ensure directory is marked as safe (handles dubious ownership)
        self._ensure_safe_directory(destination)

        # Update remote URL if different
        result = self._git(["remote", "get-url", "origin"], cwd=destination)
        current_url = result.stdout.strip() if result.success else ""

        if current_url != safe_url:
            self.logger.debug(f"Updating remote URL to: {safe_url}")
            self._git(["remote", "set-url", "origin", "--", safe_url], cwd=destination)

        # Fetch all branches
        result = self._git(["fetch", "--all"], cwd=destination, timeout=GIT_NETWORK_TIMEOUT)
        if not result.success:
            raise SourceError("Git fetch failed", details=result.stderr)

        # Determine target branch
        if safe_branch:
            target_ref = f"origin/{safe_branch}"
        else:
            # Get default branch
            result = self._git(
                ["symbolic-ref", "refs/remotes/origin/HEAD", "--short"], cwd=destination
            )
            if result.success:
                target_ref = result.stdout.strip()
            else:
                target_ref = "origin/main"  # Fallback

        # Reset to target (preserves untracked files)
        result = self._git(["reset", "--hard", target_ref], cwd=destination)
        if not result.success:
            raise SourceError("Git reset failed", details=result.stderr)

        # Clean tracked files only
        self._git(["clean", "-fd"], cwd=destination)

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
            SourceError: If the URL or branch is unsafe, or the clone fails.
            SSHError: If SSH authentication is not properly configured.
        """
        if not self.is_installed():
            raise SourceError(
                "Git is not installed",
                details="Install it with 'apt install git' or 'dnf install git'",
            )

        # Parse URL for branch if specified with #
        parsed = parse_git_url(url)
        if parsed["branch"] and not branch:
            branch = parsed["branch"]
            url = url.split("#")[0]

        safe_url = validate_git_remote_url(url)
        safe_branch = validate_git_ref(branch) if branch else None

        # Validate SSH setup for SSH URLs
        if is_ssh_url(safe_url):
            self.logger.debug("Validating SSH configuration...")
            ensure_ssh_setup(safe_url, auto_generate=True, verbose=self.verbose)

        # Build clone command
        cmd = ["clone"]

        if depth > 0:
            cmd.extend(["--depth", str(int(depth))])

        if safe_branch:
            cmd.extend(["--branch", safe_branch])

        if recursive:
            cmd.append("--recursive")

        # "--" keeps git from reading the URL or the destination as options.
        cmd.extend(["--", safe_url, str(destination)])

        self.logger.debug(f"Cloning: {safe_url}")
        result = self._git(cmd, timeout=GIT_CLONE_TIMEOUT)

        if not result.success:
            raise SourceError(
                f"Git clone failed: {safe_url}",
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
        result = self._git(["config", "--global", "--get-all", "safe.directory"])
        if result.success:
            safe_dirs = result.stdout.strip().split("\n")
            if str(path) in safe_dirs or "*" in safe_dirs:
                return

        # Add to safe.directory
        self.logger.debug(f"Adding to Git safe.directory: {path}")
        self._git(["config", "--global", "--add", "safe.directory", str(path)])

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

        Raises:
            SourceError: If the path is not a repository, the branch name is
                unsafe, or the pull fails.
        """
        if not (path / ".git").exists():
            raise SourceError(f"Not a Git repository: {path}")

        safe_branch = validate_git_ref(branch) if branch else None

        # Ensure directory is marked as safe (handles dubious ownership)
        self._ensure_safe_directory(path)

        # Check for local changes that would prevent pull
        has_changes = self._has_local_changes(path)
        stashed = False
        force_reset_used = False

        if has_changes:
            self.logger.debug("Local changes detected, stashing...")
            result = self._git(["stash", "push", "-m", "wasm-auto-stash-before-update"], cwd=path)
            if result.success and "No local changes" not in result.stdout:
                stashed = True
                self.logger.debug("Changes stashed successfully")

        try:
            # Checkout branch if specified
            if safe_branch:
                result = self._git(["checkout", safe_branch], cwd=path)
                if not result.success:
                    # Branch might not exist locally, try fetching first
                    self._git(
                        ["fetch", "origin", safe_branch], cwd=path, timeout=GIT_NETWORK_TIMEOUT
                    )
                    result = self._git(["checkout", safe_branch], cwd=path)
                    if not result.success:
                        raise SourceError(f"Failed to checkout branch: {safe_branch}")

            # Try regular pull first
            result = self._git(["pull", "--rebase"], cwd=path, timeout=GIT_NETWORK_TIMEOUT)

            if not result.success:
                # Analyze the error and try to recover
                error_msg = result.stderr.lower()

                if "unstaged changes" in error_msg or "uncommitted changes" in error_msg:
                    # This shouldn't happen if stash worked, but handle it anyway
                    self.logger.debug("Uncommitted changes blocking pull, forcing reset...")
                    force_reset_used = True
                    return self._force_pull_with_reset(path, safe_branch)

                elif "divergent branches" in error_msg or "need to specify" in error_msg:
                    # Divergent history - fetch and reset to remote
                    self.logger.debug("Divergent branches detected, resetting to remote...")
                    force_reset_used = True
                    return self._force_pull_with_reset(path, safe_branch)

                elif "conflict" in error_msg:
                    # Merge/rebase conflict - abort and reset
                    self.logger.debug("Conflict detected, aborting rebase and resetting...")
                    self._git(["rebase", "--abort"], cwd=path)
                    force_reset_used = True
                    return self._force_pull_with_reset(path, safe_branch)

                elif "refusing to merge unrelated histories" in error_msg:
                    # Unrelated histories - force reset
                    self.logger.debug("Unrelated histories, forcing reset...")
                    force_reset_used = True
                    return self._force_pull_with_reset(path, safe_branch)

                else:
                    # Unknown error, try force reset as last resort
                    self.logger.debug(f"Pull failed with: {result.stderr}")
                    force_reset_used = True
                    return self._force_pull_with_reset(path, safe_branch)

            return True

        finally:
            # Handle stashed changes
            if stashed:
                if force_reset_used:
                    # After a force reset, the stash is based on the old commit
                    # and will likely have conflicts. Drop it to avoid issues.
                    self.logger.debug("Force reset was used, dropping incompatible stash...")
                    self._git(["stash", "drop"], cwd=path)
                else:
                    # Normal pull succeeded, try to restore stash
                    self.logger.debug("Restoring stashed changes...")
                    pop_result = self._git(["stash", "pop"], cwd=path)
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
        result = self._git(["status", "--porcelain"], cwd=path)
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
            branch: Target branch, already validated.

        Returns:
            True if successful.

        Raises:
            SourceError: If the fetch or the reset fails.
        """
        # Fetch all from remote
        result = self._git(["fetch", "--all"], cwd=path, timeout=GIT_NETWORK_TIMEOUT)
        if not result.success:
            raise SourceError("Git fetch failed", details=result.stderr)

        # Determine target reference
        if branch:
            target_ref = f"origin/{branch}"
        else:
            # Get current branch
            result = self._git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=path)
            current_branch = result.stdout.strip() if result.success else "main"
            target_ref = f"origin/{current_branch}"

        # Reset hard to remote (preserves untracked files)
        result = self._git(["reset", "--hard", target_ref], cwd=path)
        if not result.success:
            raise SourceError("Git reset failed", details=result.stderr)

        # Clean only tracked files (not untracked like .env)
        self._git(["clean", "-fd"], cwd=path)

        return True

    def download_archive(self, url: str, destination: Path) -> bool:
        """
        Download and extract an archive.

        The archive is never trusted: see :func:`extract_archive` for the checks
        applied to every member.

        Args:
            url: Archive URL. Must be http(s).
            destination: Destination directory.

        Returns:
            True if download and extraction was successful.

        Raises:
            SourceError: If the URL is not downloadable, the transfer fails, or
                the archive contains an unsafe member.
        """
        safe_url = validate_archive_url(url)
        archive_format = detect_archive_format(urlparse(safe_url).path)

        self.logger.debug(f"Downloading: {safe_url}")

        with tempfile.TemporaryDirectory(prefix="wasm-source-") as workdir:
            archive = Path(workdir) / "archive"
            _download_to_file(safe_url, archive)
            extract_archive(archive, destination, archive_format=archive_format)

        self._flatten_single_directory(destination)
        return True

    def _flatten_single_directory(self, destination: Path) -> None:
        """
        Move contents up when an archive wrapped everything in one directory.

        Release tarballs from GitHub and friends contain a single ``name-1.2.3``
        directory; the deployment expects the project at the top level.

        Args:
            destination: Directory that was just extracted into.

        Raises:
            SourceError: If the contents cannot be moved.
        """
        contents = list(destination.iterdir())
        if len(contents) != 1:
            return

        subdir = contents[0]
        if subdir.is_symlink() or not subdir.is_dir():
            return

        try:
            for item in subdir.iterdir():
                shutil.move(str(item), str(destination))
            subdir.rmdir()
        except (OSError, shutil.Error) as exc:
            raise SourceError(
                f"Cannot flatten extracted directory: {subdir.name}",
                details=str(exc),
            ) from exc

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
        except (OSError, shutil.Error) as exc:
            raise SourceError(f"Copy failed: {source} -> {destination}", details=str(exc)) from exc

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
        result = self._git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=path)
        if result.success:
            info["branch"] = result.stdout.strip()

        # Get remote URL
        result = self._git(["config", "--get", "remote.origin.url"], cwd=path)
        if result.success:
            info["remote"] = result.stdout.strip()

        # Get current commit
        result = self._git(["rev-parse", "--short", "HEAD"], cwd=path)
        if result.success:
            info["commit"] = result.stdout.strip()

        # Check if dirty
        result = self._git(["status", "--porcelain"], cwd=path)
        if result.success:
            info["dirty"] = bool(result.stdout.strip())

        return info
