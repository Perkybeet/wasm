# Copyright (c) 2024-2026 Yago Lopez Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
Tests for where backups are written, and for bringing back the ones that were not.

A production server had ``backup.directory: ''`` in ``/etc/wasm/config.yaml``,
written by the 1.x panel's settings form, which rendered the field empty (the
section had no default) and saved the whole form. ``Path('')`` is the current
working directory, so from then on every backup went to ``/root/<app>/`` when
the operator ran wasm from ``/root``, and to ``/<app>/`` when a timer did. The
console's storage page then listed ``/root/.ssh``, ``.docker`` and ``.claude``
as applications, and the backups in ``/var/backups/wasm`` were no longer seen.

Pinned here:

- the working directory never decides where a backup goes: empty means the
  default, and a relative path is refused rather than resolved;
- ``wasm config set`` refuses a relative directory (the API side is in
  ``tests/test_web_config_api.py``);
- ``wasm backup import`` moves exactly the WASM backups out of a directory,
  never overwrites, is idempotent and changes nothing under ``--dry-run``;
- storage usage counts only directories holding WASM backups, and says where
  misplaced ones are.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
import yaml
from click.testing import CliRunner

from wasm.cli import app as app_module
from wasm.cli.app import cli as root_cli
from wasm.cli.commands import config as config_cmd
from wasm.core.config import DEFAULT_BACKUP_DIR, Config, resolve_backup_directory
from wasm.core.exceptions import BackupError, ConfigError
from wasm.core.fs import DryRunFileSystem
from wasm.core.logger import Logger
from wasm.core.runner import FakeRunner
from wasm.managers.backup_manager import BackupManager, BackupMetadata


class _TestLogger(Logger):
    """A logger that writes to stdout as it is when built, so Click can read it."""

    def __init__(
        self,
        verbose: bool = False,
        no_color: bool = False,
        log_file: Path | None = None,
        stream: Any = None,
    ) -> None:
        """
        Args:
            verbose: Show debug messages.
            no_color: Disable colour.
            log_file: Optional file to mirror output into.
            stream: Where to write; defaults to stdout as it is right now.
        """
        super().__init__(
            verbose=verbose,
            no_color=True,
            log_file=log_file,
            stream=stream if stream is not None else sys.stdout,
        )


@pytest.fixture(autouse=True)
def _readable_output(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Make everything a command prints readable by the test.

    Args:
        monkeypatch: Patching helper, scoped to the test.
    """
    monkeypatch.setattr(app_module, "Logger", _TestLogger)
    monkeypatch.setattr(config_cmd, "Logger", _TestLogger)


@pytest.fixture
def config_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """
    Point the configuration singleton at a sandboxed file that does not exist yet.

    Args:
        tmp_path: Per-test temporary directory.
        monkeypatch: Patching helper, scoped to the test.

    Yields:
        The configuration file path.
    """
    path = tmp_path / "etc" / "wasm" / "config.yaml"
    monkeypatch.setattr("wasm.core.config.DEFAULT_CONFIG_PATH", path)
    monkeypatch.setattr(config_cmd, "DEFAULT_CONFIG_PATH", path)
    Config.reset_instance()
    try:
        yield path
    finally:
        Config.reset_instance()


def write_config(path: Path, tree: dict[str, Any]) -> None:
    """
    Write a configuration file the way an operator's server has it.

    Args:
        path: Where to write.
        tree: The YAML content.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(tree))
    Config.reset_instance()


def run_wasm(*args: str) -> tuple[int, str]:
    """
    Run the real command tree.

    Args:
        *args: Arguments after the program name.

    Returns:
        The exit code and everything printed.
    """
    result = CliRunner().invoke(root_cli, list(args))
    return result.exit_code, result.output


def plant_backup(root: Path, domain: str, stamp: str = "20260801_101500") -> tuple[Path, Path]:
    """
    Write a backup pair the way ``BackupManager.create`` lays it out.

    Args:
        root: The directory that plays the backup root.
        domain: Domain the backup belongs to.
        stamp: ``YYYYMMDD_HHMMSS`` part of the identifier.

    Returns:
        The archive and its metadata file.
    """
    app_name = domain.replace(".", "-")
    backup_id = f"{app_name}_{stamp}"
    directory = root / app_name
    directory.mkdir(parents=True, exist_ok=True)
    archive = directory / f"{backup_id}.tar.gz"
    archive.write_bytes(b"archive of " + backup_id.encode())
    metadata = BackupMetadata(
        id=backup_id,
        domain=domain,
        app_name=app_name,
        created_at=datetime(2026, 8, 1, 10, 15).isoformat(),
        size_bytes=archive.stat().st_size,
        app_type="nextjs",
        version=BackupManager.BACKUP_VERSION,
        description="",
        includes_env=True,
        includes_node_modules=False,
    )
    sidecar = directory / f"{backup_id}.json"
    sidecar.write_text(json.dumps(metadata.to_dict()))
    return archive, sidecar


def plant_home_clutter(root: Path) -> list[Path]:
    """
    Write what a real ``/root`` holds next to misplaced backups.

    Args:
        root: The directory that plays ``/root``.

    Returns:
        Every file planted, so a test can assert none of them moved.
    """
    files = {
        ".ssh/id_ed25519": "private key",
        ".docker/config.json": '{"auths": {}}',
        ".claude/settings.json": "{}",
        # A tarball that is not a WASM backup, in a directory named like one.
        "shop-example-com/export.tar.gz": "not a backup",
        # Named like a backup but with no metadata to prove it.
        "orphan-example-com/orphan-example-com_20260801_101500.tar.gz": "no sidecar",
    }
    planted = []
    for relative, content in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        planted.append(path)
    return planted


def manager_at(backup_dir: Path, **kwargs: Any) -> BackupManager:
    """
    Build a manager writing into a sandboxed backup directory.

    Args:
        backup_dir: The configured backup directory.
        **kwargs: Passed to :class:`BackupManager`.

    Returns:
        The manager.
    """
    manager = BackupManager(verbose=False, runner=FakeRunner(), **kwargs)
    manager.backup_dir = backup_dir
    return manager


# -- one resolution ----------------------------------------------------------


class TestResolution:
    """Empty means the default; anything else must be absolute."""

    @pytest.mark.parametrize("value", [None, "", "   ", "\t\n"])
    def test_empty_means_the_default(self, value: Any) -> None:
        assert resolve_backup_directory(value) == DEFAULT_BACKUP_DIR

    def test_an_absolute_path_is_kept(self) -> None:
        assert resolve_backup_directory("/srv/backups") == Path("/srv/backups")

    @pytest.mark.parametrize("value", ["backups", "./backups", "../var/backups", "~/backups"])
    def test_a_relative_path_is_refused(self, value: str) -> None:
        with pytest.raises(ConfigError) as caught:
            resolve_backup_directory(value)

        assert "absolute path" in str(caught.value)
        assert "wasm config set backup.directory" in (caught.value.details or "")


class TestWorkingDirectoryNeverMatters:
    """What happened on the server: '' became whatever directory wasm ran from."""

    def test_an_empty_setting_uses_the_default_wherever_wasm_runs(
        self, config_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        write_config(config_path, {"backup": {"directory": "", "max_per_app": 10}})
        elsewhere = tmp_path / "root"
        elsewhere.mkdir()
        monkeypatch.chdir(elsewhere)

        manager = BackupManager(verbose=False, runner=FakeRunner())

        assert manager.backup_dir == BackupManager.DEFAULT_BACKUP_DIR
        assert manager.backup_dir.is_absolute()
        assert Config().backup_directory == DEFAULT_BACKUP_DIR

    def test_a_missing_setting_uses_the_default(
        self, config_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)

        assert BackupManager(verbose=False, runner=FakeRunner()).backup_dir == DEFAULT_BACKUP_DIR

    def test_loading_interprets_the_empty_value_without_rewriting_the_file(
        self, config_path: Path
    ) -> None:
        write_config(config_path, {"backup": {"directory": ""}})
        before = config_path.read_bytes()

        config = Config()

        assert config.get("backup.directory") is None
        assert config_path.read_bytes() == before

    def test_the_next_write_persists_the_interpretation(self, config_path: Path) -> None:
        write_config(config_path, {"backup": {"directory": "", "max_per_app": 7}})

        config = Config()
        config.set("ssl.email", "ops@example.com")
        config.write()

        stored = yaml.safe_load(config_path.read_text())
        assert "directory" not in stored["backup"]
        assert stored["backup"]["max_per_app"] == 7

    def test_a_relative_setting_is_refused_rather_than_resolved(
        self, config_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A file edited by hand can still say 'backups'; it must not mean ./backups."""
        write_config(config_path, {"backup": {"directory": "backups"}})
        monkeypatch.chdir(tmp_path)

        with pytest.raises(ConfigError) as caught:
            BackupManager(verbose=False, runner=FakeRunner())

        assert "absolute path" in str(caught.value)
        assert not (tmp_path / "backups").exists()


class TestConfigSetRefusesRelative:
    """The chokepoint: Config.set, which 'wasm config set' and every API write use."""

    def test_the_cli_refuses_a_relative_directory(self, config_path: Path) -> None:
        code, output = run_wasm("config", "set", "backup.directory", "backups")

        assert code == 1
        assert "backup.directory must be an absolute path" in output
        assert not config_path.exists()

    def test_the_cli_accepts_an_absolute_directory(self, config_path: Path) -> None:
        code, output = run_wasm("config", "set", "backup.directory", "/srv/backups")

        assert code == 0, output
        assert yaml.safe_load(config_path.read_text())["backup"]["directory"] == "/srv/backups"

    def test_the_cli_treats_empty_as_the_default(self, config_path: Path) -> None:
        code, output = run_wasm("config", "set", "backup.directory", "")

        assert code == 0, output
        stored = yaml.safe_load(config_path.read_text())["backup"]["directory"]
        assert stored == str(DEFAULT_BACKUP_DIR)

    def test_a_full_replace_normalises_an_empty_directory(self, config_path: Path) -> None:
        """The 1.x panel saved the whole form; that is the path the '' came through."""
        config = Config()
        config.replace({**config.to_dict(), "backup": {"directory": "", "max_per_app": 10}})

        assert config.get("backup.directory") == str(DEFAULT_BACKUP_DIR)

    def test_a_full_replace_refuses_a_relative_directory(self, config_path: Path) -> None:
        config = Config()

        with pytest.raises(ConfigError):
            config.replace({**config.to_dict(), "backup": {"directory": "var/backups"}})


# -- import --------------------------------------------------------------------


class TestImport:
    """``wasm backup import`` moves WASM backups, and only them."""

    def test_moves_only_backups(self, tmp_path: Path) -> None:
        home = tmp_path / "root"
        archive, sidecar = plant_backup(home, "shop.example.com")
        clutter = plant_home_clutter(home)
        dest = tmp_path / "backups"
        manager = manager_at(dest)

        report = manager.import_backups(home)

        moved_archive = dest / "shop-example-com" / archive.name
        assert moved_archive.read_bytes() == b"archive of " + archive.name[:-7].encode()
        assert (dest / "shop-example-com" / sidecar.name).is_file()
        assert not archive.exists() and not sidecar.exists()
        assert [(src, dst) for src, dst in report.moved] == [(archive, moved_archive)]
        for path in clutter:
            assert path.exists(), f"{path} is not a backup and must not move"
        assert [b.id for b in manager.list_backups()] == [archive.name[:-7]]

    def test_reports_what_looks_like_a_backup_but_is_not_imported(self, tmp_path: Path) -> None:
        home = tmp_path / "root"
        plant_home_clutter(home)

        report = manager_at(tmp_path / "backups").import_backups(home)

        assert report.moved == []
        left = {path.name: reason for path, reason in report.left}
        assert "orphan-example-com_20260801_101500.tar.gz" in left
        assert "metadata" in left["orphan-example-com_20260801_101500.tar.gz"]
        # Things that do not look like backups at all are not even mentioned.
        assert "export.tar.gz" not in left

    def test_is_idempotent(self, tmp_path: Path) -> None:
        home = tmp_path / "root"
        plant_backup(home, "shop.example.com")
        plant_backup(home, "blog.example.com", stamp="20260802_030000")
        manager = manager_at(tmp_path / "backups")

        first = manager.import_backups(home)
        second = manager.import_backups(home)

        assert len(first.moved) == 2
        assert second.moved == [] and second.left == []
        assert len(manager.list_backups()) == 2

    def test_never_overwrites(self, tmp_path: Path) -> None:
        home = tmp_path / "root"
        archive, sidecar = plant_backup(home, "shop.example.com")
        dest = tmp_path / "backups"
        existing = dest / "shop-example-com" / archive.name
        existing.parent.mkdir(parents=True)
        existing.write_bytes(b"the copy already in place")

        report = manager_at(dest).import_backups(home)

        assert existing.read_bytes() == b"the copy already in place"
        assert not (dest / "shop-example-com" / sidecar.name).exists()
        assert archive.exists() and sidecar.exists()
        assert report.moved == []
        assert [(path, "already exists" in reason) for path, reason in report.left] == [
            (archive, True)
        ]

    def test_dry_run_changes_nothing(self, tmp_path: Path) -> None:
        home = tmp_path / "root"
        archive, sidecar = plant_backup(home, "shop.example.com")
        dest = tmp_path / "backups"

        report = manager_at(dest, fs=DryRunFileSystem()).import_backups(home)

        assert report.rehearsal is True
        assert [src for src, _ in report.moved] == [archive]
        assert archive.exists() and sidecar.exists()
        assert not dest.exists()

    def test_a_symlinked_archive_is_left_alone(self, tmp_path: Path) -> None:
        """A link planted in the source must not smuggle another file into the backups."""
        home = tmp_path / "root"
        archive, _ = plant_backup(home, "shop.example.com")
        target = tmp_path / "secret"
        target.write_text("not yours")
        archive.unlink()
        archive.symlink_to(target)

        report = manager_at(tmp_path / "backups").import_backups(home)

        assert report.moved == []
        assert archive.is_symlink()

    def test_refuses_the_backup_directory_itself(self, tmp_path: Path) -> None:
        dest = tmp_path / "backups"
        plant_backup(dest, "shop.example.com")

        with pytest.raises(BackupError):
            manager_at(dest).import_backups(dest)

    def test_refuses_a_missing_directory(self, tmp_path: Path) -> None:
        with pytest.raises(BackupError):
            manager_at(tmp_path / "backups").import_backups(tmp_path / "nowhere")


class TestImportCommand:
    """The CLI surface over :meth:`BackupManager.import_backups`."""

    @pytest.fixture
    def dest(self, tmp_path: Path, config_path: Path) -> Path:
        """
        Configure a sandboxed backup directory.

        Args:
            tmp_path: Per-test temporary directory.
            config_path: The sandboxed configuration file.

        Returns:
            The configured backup directory.
        """
        dest = tmp_path / "backups"
        write_config(config_path, {"backup": {"directory": str(dest)}})
        return dest

    def test_imports_and_reports(self, tmp_path: Path, dest: Path, runner: FakeRunner) -> None:
        home = tmp_path / "root"
        archive, _ = plant_backup(home, "shop.example.com")

        code, output = run_wasm("backup", "import", str(home))

        assert code == 0, output
        assert (dest / "shop-example-com" / archive.name).is_file()
        assert "Moved 1 backup" in output

    def test_dry_run_moves_nothing(self, tmp_path: Path, dest: Path, runner: FakeRunner) -> None:
        home = tmp_path / "root"
        archive, _ = plant_backup(home, "shop.example.com")

        code, output = run_wasm("--dry-run", "backup", "import", str(home))

        assert code == 0, output
        assert archive.exists()
        assert not (dest / "shop-example-com").exists()
        assert "Would move 1 backup" in output

    def test_a_collision_fails_the_command(
        self, tmp_path: Path, dest: Path, runner: FakeRunner
    ) -> None:
        home = tmp_path / "root"
        archive, _ = plant_backup(home, "shop.example.com")
        (dest / "shop-example-com").mkdir(parents=True)
        (dest / "shop-example-com" / archive.name).write_bytes(b"kept")

        code, output = run_wasm("backup", "import", str(home))

        assert code == 1
        assert "already exists" in output
        assert archive.exists()


# -- storage -------------------------------------------------------------------


class TestStorageUsage:
    """Only directories holding WASM backups are applications."""

    def test_unrelated_directories_are_not_listed(self, tmp_path: Path) -> None:
        """What the console showed: /root/.ssh, .docker and .claude as applications."""
        root = tmp_path / "root"
        archive, _ = plant_backup(root, "shop.example.com")
        plant_home_clutter(root)

        usage = manager_at(root).get_storage_usage()

        assert sorted(usage["by_app"]) == ["orphan-example-com", "shop-example-com"]
        assert usage["by_app"]["shop-example-com"] == {
            "count": 1,
            "size_bytes": archive.stat().st_size,
        }
        assert usage["total_backups"] == 2


class TestMisplacedBackupsHint:
    """When backups are somewhere else, the listing says where."""

    def test_the_old_default_is_offered_when_the_configured_directory_is_empty(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        old_default = tmp_path / "var-backups-wasm"
        plant_backup(old_default, "shop.example.com")
        monkeypatch.setattr(BackupManager, "DEFAULT_BACKUP_DIR", old_default)
        monkeypatch.setattr(BackupManager, "MISPLACED_BACKUP_ROOTS", ())

        found = manager_at(tmp_path / "new-backups").find_misplaced_backups()

        assert [(m.directory, m.count) for m in found] == [(old_default, 1)]

    def test_the_old_default_is_not_offered_when_the_configured_directory_has_backups(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        old_default = tmp_path / "var-backups-wasm"
        plant_backup(old_default, "shop.example.com")
        configured = tmp_path / "new-backups"
        plant_backup(configured, "blog.example.com")
        monkeypatch.setattr(BackupManager, "DEFAULT_BACKUP_DIR", old_default)
        monkeypatch.setattr(BackupManager, "MISPLACED_BACKUP_ROOTS", ())

        assert manager_at(configured).find_misplaced_backups() == []

    def test_backups_written_to_a_working_directory_are_found(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The server's case: /var/backups/wasm has backups, newer ones went to /root."""
        configured = tmp_path / "var-backups-wasm"
        plant_backup(configured, "shop.example.com", stamp="20260701_000000")
        home = tmp_path / "root"
        plant_backup(home, "shop.example.com")
        plant_backup(home, "shop.example.com", stamp="20260802_000000")
        plant_home_clutter(home)
        monkeypatch.setattr(BackupManager, "DEFAULT_BACKUP_DIR", configured)
        monkeypatch.setattr(BackupManager, "MISPLACED_BACKUP_ROOTS", (home,))

        found = manager_at(configured).find_misplaced_backups()

        assert [(m.directory, m.count) for m in found] == [(home, 2)]

    def test_an_unreadable_place_is_skipped_not_fatal(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A directory that cannot be read (another user's home) must not fail the report."""
        configured = tmp_path / "var-backups-wasm"
        plant_backup(configured, "shop.example.com", stamp="20260701_000000")
        home = tmp_path / "root"
        plant_backup(home, "shop.example.com")
        monkeypatch.setattr(BackupManager, "DEFAULT_BACKUP_DIR", configured)
        monkeypatch.setattr(BackupManager, "MISPLACED_BACKUP_ROOTS", (home,))
        real_is_dir = Path.is_dir

        def refused(self: Path, *args: Any, **kwargs: Any) -> bool:
            if self.parent == home:
                raise PermissionError(13, "Permission denied", str(self))
            return real_is_dir(self, *args, **kwargs)

        monkeypatch.setattr(Path, "is_dir", refused)

        assert manager_at(configured).find_misplaced_backups() == []

    def test_the_storage_command_says_how_to_import_them(
        self, tmp_path: Path, config_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        configured = tmp_path / "backups"
        apps = tmp_path / "apps"
        (apps / "shop-example-com").mkdir(parents=True)
        write_config(
            config_path, {"apps_directory": str(apps), "backup": {"directory": str(configured)}}
        )
        home = tmp_path / "root"
        plant_backup(home, "shop.example.com")
        monkeypatch.setattr(BackupManager, "MISPLACED_BACKUP_ROOTS", (home,))

        code, output = run_wasm("backup", "storage")

        assert code == 0, output
        assert f"wasm backup import {home}" in output

    def test_the_storage_json_carries_them(
        self, tmp_path: Path, config_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        configured = tmp_path / "backups"
        apps = tmp_path / "apps"
        (apps / "shop-example-com").mkdir(parents=True)
        write_config(
            config_path, {"apps_directory": str(apps), "backup": {"directory": str(configured)}}
        )
        home = tmp_path / "root"
        plant_backup(home, "shop.example.com")
        monkeypatch.setattr(BackupManager, "MISPLACED_BACKUP_ROOTS", (home,))

        code, output = run_wasm("--json", "backup", "storage")

        assert code == 0, output
        assert json.loads(output)["misplaced"] == [{"directory": str(home), "count": 1}]
