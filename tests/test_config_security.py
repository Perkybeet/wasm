# Copyright (c) 2024-2025 Yago López Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
Security tests for the global configuration and the SQLite store.

Covers three classes of problem that were found in production code:

* global state leaking between :class:`~wasm.core.config.Config` instances,
  because the defaults were shallow-copied and therefore shared,
* files holding secrets (``config.yaml``, ``wasm.db``) created with
  world-readable permissions,
* absence of a reusable redaction helper for the web API.
"""

from __future__ import annotations

import copy
from pathlib import Path

import pytest
import yaml

from wasm.core.config import DEFAULT_CONFIG, Config, redact_secrets, secure_write
from wasm.core.store import WASMStore


@pytest.fixture
def config_path(sandbox: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """
    Point the global config file at the sandbox and reset the singleton.

    Args:
        sandbox: Isolated filesystem root.
        monkeypatch: Patching helper, scoped to the test.

    Returns:
        Path the config module will read from and write to.
    """
    path = sandbox / "etc" / "wasm" / "config.yaml"
    monkeypatch.setattr("wasm.core.config.DEFAULT_CONFIG_PATH", path)
    Config.reset_instance()
    try:
        yield path
    finally:
        Config.reset_instance()


class TestDefaultsAreNotMutated:
    """The module-level defaults must never be reachable through an instance."""

    def test_set_does_not_survive_reset_instance(self, config_path: Path) -> None:
        """A secret written with set() must not leak into a fresh instance."""
        Config().set("databases.credentials.mysql.password", "hunter2")

        Config.reset_instance()

        assert Config().get("databases.credentials.mysql.password") == ""

    def test_set_does_not_mutate_module_defaults(self, config_path: Path) -> None:
        """set() must leave DEFAULT_CONFIG byte-for-byte identical."""
        before = copy.deepcopy(DEFAULT_CONFIG)

        Config().set("databases.credentials.mysql.password", "hunter2")
        Config().set("monitor.smtp.password", "smtp-secret")

        assert DEFAULT_CONFIG == before

    def test_get_returns_a_copy_of_nested_containers(self, config_path: Path) -> None:
        """Mutating the value returned by get() must not touch global state."""
        config = Config()

        credentials = config.get("databases.credentials")
        credentials["mysql"]["password"] = "leaked"

        assert config.get("databases.credentials.mysql.password") == ""
        assert DEFAULT_CONFIG["databases"]["credentials"]["mysql"]["password"] == ""

    def test_to_dict_returns_a_deep_copy(self, config_path: Path) -> None:
        """to_dict() must not hand out references into the live config."""
        config = Config()

        dumped = config.to_dict()
        dumped["monitor"]["smtp"]["password"] = "leaked"

        assert config.get("monitor.smtp.password") == ""


class TestConfigFilePermissions:
    """config.yaml holds MySQL, SMTP and OpenAI credentials."""

    def test_save_creates_file_without_group_or_other_access(self, config_path: Path) -> None:
        """The saved config must be readable by its owner only."""
        config = Config()
        config.set("databases.credentials.mysql.password", "hunter2")

        assert config.save() is True

        assert config_path.stat().st_mode & 0o077 == 0

    def test_save_tightens_a_preexisting_world_readable_file(self, config_path: Path) -> None:
        """An already lax config file must be repaired on save, not preserved."""
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text("webserver: nginx\n")
        config_path.chmod(0o644)

        assert Config().save() is True

        assert config_path.stat().st_mode & 0o077 == 0

    def test_shared_directory_is_left_alone(self, sandbox: Path) -> None:
        """A sticky-bit directory must not be tightened, only the file inside."""
        shared = sandbox / "shared"
        shared.mkdir(mode=0o777)
        shared.chmod(0o1777)
        target = shared / "config.yaml"

        secure_write(target, "webserver: nginx\n")

        assert target.stat().st_mode & 0o077 == 0
        assert shared.stat().st_mode & 0o777 == 0o777

    def test_upgrade_writes_a_private_file(self, config_path: Path) -> None:
        """upgrade() rewrites the config and must apply the same mode."""
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text("webserver: nginx\n")

        Config().upgrade()

        assert config_path.stat().st_mode & 0o077 == 0


class TestStorePermissions:
    """wasm.db stores env_vars, which contain DATABASE_URL and friends."""

    def test_database_and_directory_are_private(self, sandbox: Path) -> None:
        """The SQLite file must be 0600 and its directory 0700."""
        db_path = sandbox / "var" / "lib" / "wasm" / "wasm.db"
        WASMStore.reset_instance()
        try:
            store = WASMStore(db_path)
            assert store.db_path == db_path

            assert db_path.stat().st_mode & 0o077 == 0
            assert db_path.parent.stat().st_mode & 0o077 == 0
        finally:
            WASMStore.reset_instance()

    def test_existing_lax_database_is_tightened(self, sandbox: Path) -> None:
        """A database left world-readable by an older version must be repaired."""
        db_dir = sandbox / "var" / "lib" / "wasm"
        db_dir.mkdir(parents=True)
        db_path = db_dir / "wasm.db"
        db_path.touch()
        db_path.chmod(0o644)
        db_dir.chmod(0o755)

        WASMStore.reset_instance()
        try:
            WASMStore(db_path)

            assert db_path.stat().st_mode & 0o077 == 0
            assert db_dir.stat().st_mode & 0o077 == 0
        finally:
            WASMStore.reset_instance()


class TestRedactSecrets:
    """redact_secrets() is the helper the web API uses before serialising."""

    def test_redacts_nested_secret_keys(self) -> None:
        """Secrets must be replaced at any depth."""
        data = {
            "databases": {"credentials": {"mysql": {"user": "root", "password": "hunter2"}}},
            "monitor": {"openai": {"api_key": "sk-live", "model": "gpt-4o-mini"}},
        }

        result = redact_secrets(data)

        assert result["databases"]["credentials"]["mysql"]["password"] == "***"
        assert result["databases"]["credentials"]["mysql"]["user"] == "root"
        assert result["monitor"]["openai"]["api_key"] == "***"
        assert result["monitor"]["openai"]["model"] == "gpt-4o-mini"

    def test_container_behind_a_secret_key_is_walked_not_replaced(self) -> None:
        """A credentials block keeps its user names and loses its passwords."""
        data = {"credentials": {"mysql": {"user": "root", "password": "hunter2"}}}

        result = redact_secrets(data)

        assert result["credentials"]["mysql"] == {"user": "root", "password": "***"}

    def test_redacts_a_full_config_dump(self, config_path: Path) -> None:
        """The helper must cover every secret the real config carries."""
        config = Config()
        config.set("databases.credentials.mysql.password", "hunter2")
        config.set("monitor.smtp.password", "smtp-secret")
        config.set("monitor.openai.api_key", "sk-live")

        result = redact_secrets(config.to_dict())

        assert "hunter2" not in yaml.safe_dump(result)
        assert "smtp-secret" not in yaml.safe_dump(result)
        assert "sk-live" not in yaml.safe_dump(result)
        assert result["webserver"] == "nginx"

    def test_does_not_mutate_the_input(self) -> None:
        """The caller's dictionary must be left untouched."""
        data = {"smtp": {"password": "hunter2"}}

        redact_secrets(data)

        assert data["smtp"]["password"] == "hunter2"

    def test_redacts_inside_lists(self) -> None:
        """Lists of dictionaries must be walked too."""
        data = {"accounts": [{"user": "a", "token": "t1"}, {"user": "b", "token": "t2"}]}

        result = redact_secrets(data)

        assert [entry["token"] for entry in result["accounts"]] == ["***", "***"]
        assert [entry["user"] for entry in result["accounts"]] == ["a", "b"]

    def test_key_matching_is_case_insensitive(self) -> None:
        """Upper and mixed case keys must be caught as well."""
        data = {"API_KEY": "sk-live", "Secret": "s", "AuthToken": "t"}

        result = redact_secrets(data)

        assert result == {"API_KEY": "***", "Secret": "***", "AuthToken": "***"}

    def test_leaves_non_secret_keys_alone(self) -> None:
        """Keys that merely look similar must keep their values."""
        data = {
            "keyboard_layout": "es",
            "monkey": "george",
            "port": 8080,
            "host": "127.0.0.1",
            "enabled": True,
        }

        result = redact_secrets(data)

        assert result == data

    def test_empty_secret_values_are_still_redacted(self) -> None:
        """An empty password must not reveal that no password is configured."""
        result = redact_secrets({"password": ""})

        assert result["password"] == "***"


class TestMonitorDefaults:
    """The monitor is observability, not an antivirus."""

    def test_process_termination_is_not_configured_by_default(self) -> None:
        """No default may authorise the monitor to kill processes."""
        monitor_defaults = DEFAULT_CONFIG["monitor"]

        assert "auto_terminate" not in monitor_defaults
        assert "terminate_malicious_only" not in monitor_defaults

    def test_removed_keys_read_back_as_safe_values(self, config_path: Path) -> None:
        """Legacy call sites must not get a permissive value from get()."""
        config = Config()

        assert config.get("monitor.auto_terminate", True) is False
        assert config.get("monitor.terminate_malicious_only", True) is False

    def test_old_config_with_removed_keys_loads(self, config_path: Path) -> None:
        """A config file written by an older version must still load."""
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(
            yaml.safe_dump(
                {
                    "webserver": "apache",
                    "monitor": {
                        "enabled": True,
                        "auto_terminate": True,
                        "terminate_malicious_only": False,
                        "dry_run": False,
                    },
                }
            )
        )

        config = Config()

        assert config.get("webserver") == "apache"
        assert config.get("monitor.enabled") is True
        assert config.get("monitor.auto_terminate", True) is False

    def test_removed_keys_are_dropped_when_saving(self, config_path: Path) -> None:
        """Re-saving an old config must not carry the removed keys forward."""
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(yaml.safe_dump({"monitor": {"auto_terminate": True}}))

        Config().save()

        saved = yaml.safe_load(config_path.read_text())
        assert "auto_terminate" not in saved["monitor"]
