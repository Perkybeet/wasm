# Copyright (c) 2024-2025 Yago López Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
Security tests for the global configuration and the SQLite store.

Covers five classes of problem that were found in production code:

* global state leaking between :class:`~wasm.core.config.Config` instances,
  because the defaults were shallow-copied and therefore shared,
* files holding secrets (``config.yaml``, ``wasm.db``, deployed ``.env``)
  created with world-readable permissions,
* absence of a reusable redaction helper for the web API,
* settings that no longer exist coming back permissive when a caller reads or
  writes the container they used to live in,
* writes to a secret file following a symlink planted by whoever can write in
  the destination directory.
"""

from __future__ import annotations

import ast
import copy
import json
import os
import stat
from pathlib import Path

import pytest
import yaml

from wasm.core.config import (
    DEFAULT_CONFIG,
    REDACTED,
    Config,
    _is_secret_key,
    redact_secrets,
    restore_redacted,
    restrict_file,
    secure_directory,
    secure_write,
)
from wasm.core.exceptions import SecurityError
from wasm.core.fs import (
    SECRET_DIR_MODE,
    SECRET_MODE,
    DryRunFileSystem,
    RecordingFileSystem,
    set_fs,
)
from wasm.core.store import WASMStore
from wasm.deployers.helpers.env_manager import EnvConfig, EnvManager, EnvVariable


@pytest.fixture(autouse=True)
def _real_filesystem() -> None:
    """
    Give every test the real filesystem back, whatever the last one installed.

    The seam is process-wide, exactly like the command runner. A test that
    installs :class:`~wasm.core.fs.DryRunFileSystem` and forgets to undo it
    makes every later test in the session silently assert on files nobody
    wrote.
    """
    set_fs(None)
    try:
        yield
    finally:
        set_fs(None)


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


def secret_paths(node: object, prefix: str = "") -> list[str]:
    """
    List the dotted paths of every scalar setting whose key names a secret.

    Args:
        node: Configuration subtree to walk.
        prefix: Dotted path of ``node`` itself.

    Returns:
        Dotted paths of the scalar values that must never be served in clear.
    """
    found: list[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            if isinstance(value, (dict, list)):
                found.extend(secret_paths(value, path))
            elif _is_secret_key(str(key)):
                found.append(path)
    return found


def _flat_paths(node: object, prefix: str = "") -> list[str]:
    """
    List the dotted path of every scalar leaf in a configuration tree.

    Args:
        node: Configuration subtree to walk.
        prefix: Dotted path of ``node`` itself.

    Returns:
        Dotted paths of the scalar leaves.
    """
    if not isinstance(node, dict):
        return [prefix]
    found: list[str] = []
    for key, value in node.items():
        path = f"{prefix}.{key}" if prefix else str(key)
        found.extend(_flat_paths(value, path))
    return found


def _lookup(tree: object, dotted_key: str) -> object:
    """
    Read a dotted path out of a configuration tree.

    Args:
        tree: Configuration mapping.
        dotted_key: Dotted path of the setting.

    Returns:
        The value found, or None when the path does not exist.
    """
    node: object = tree
    for part in dotted_key.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


class TestSecretInventory:
    """The redaction markers must cover the secrets this project really ships."""

    def test_every_known_secret_is_recognised(self) -> None:
        """The real credential keys must be classified as secrets."""
        paths = set(secret_paths(DEFAULT_CONFIG))

        assert paths >= {
            "monitor.openai.api_key",
            "monitor.smtp.password",
            "databases.credentials.mysql.password",
            "databases.credentials.postgresql.password",
            "databases.credentials.redis.password",
            "databases.credentials.mongodb.password",
        }

    def test_redaction_touches_the_credentials_and_nothing_else(self) -> None:
        """A false positive turns a numeric setting into "***" in the panel."""
        redacted = redact_secrets(DEFAULT_CONFIG)

        changed = {
            path
            for path in _flat_paths(DEFAULT_CONFIG)
            if _lookup(redacted, path) != _lookup(DEFAULT_CONFIG, path)
        }
        assert changed == {
            "monitor.openai.api_key",
            "monitor.smtp.password",
            "databases.credentials.mysql.password",
            "databases.credentials.postgresql.password",
            "databases.credentials.redis.password",
            "databases.credentials.mongodb.password",
        }

    def test_no_secret_survives_a_full_tree_redaction(self, config_path: Path) -> None:
        """Filling every secret in the tree and redacting must leak nothing."""
        config = Config()
        markers = {}
        for index, path in enumerate(secret_paths(DEFAULT_CONFIG)):
            marker = f"leaked-secret-{index}"
            markers[path] = marker
            config.set(path, marker)

        dumped = yaml.safe_dump(redact_secrets(config.to_dict()))

        for path, marker in markers.items():
            assert marker not in dumped, f"{path} survived redaction"


class TestRestoreRedacted:
    """The panel sends back what it was shown, and it was shown ``***``."""

    def test_placeholder_keeps_the_stored_secret(self) -> None:
        """A redacted value must not overwrite the real one."""
        current = {"monitor": {"smtp": {"password": "hunter2", "host": "old"}}}
        incoming = {"monitor": {"smtp": {"password": REDACTED, "host": "new"}}}

        result = restore_redacted(incoming, current)

        assert result["monitor"]["smtp"]["password"] == "hunter2"
        assert result["monitor"]["smtp"]["host"] == "new"

    def test_a_real_new_secret_still_wins(self) -> None:
        """Only the placeholder is ignored; a rotated secret must be stored."""
        result = restore_redacted({"password": "rotated"}, {"password": "hunter2"})

        assert result["password"] == "rotated"

    def test_placeholder_without_a_stored_value_is_dropped(self) -> None:
        """``***`` must never be persisted as if it were a password."""
        result = restore_redacted({"password": REDACTED}, {})

        assert result["password"] == ""

    def test_placeholder_on_a_non_secret_key_is_kept(self) -> None:
        """A literal ``***`` elsewhere is data, not a placeholder."""
        result = restore_redacted({"comment": REDACTED}, {"comment": "old"})

        assert result["comment"] == REDACTED

    def test_inputs_are_not_mutated(self) -> None:
        """Neither the request body nor the live config may be modified."""
        current = {"password": "hunter2"}
        incoming = {"password": REDACTED}

        restore_redacted(incoming, current)

        assert incoming == {"password": REDACTED}
        assert current == {"password": "hunter2"}


class TestRemovedKeysThroughTheParent:
    """Reading the container must not resurrect a permissive default."""

    def test_parent_lookup_pins_the_safe_value(self, config_path: Path) -> None:
        """``get('monitor').get('auto_terminate', True)`` must be False."""
        monitor = Config().get("monitor")

        assert monitor.get("auto_terminate", True) is False
        assert monitor.get("terminate_malicious_only", True) is False
        assert monitor.get("dry_run", False) is True

    def test_parent_lookup_of_a_missing_container_pins_too(self, config_path: Path) -> None:
        """A default returned for a missing container must be pinned as well."""
        config = Config()
        config._config.pop("monitor", None)

        monitor = config.get("monitor", {})

        assert monitor.get("auto_terminate", True) is False

    def test_writing_the_parent_cannot_reintroduce_the_key(self, config_path: Path) -> None:
        """``set('monitor', ...)`` must obey the same guard as ``set`` on the leaf."""
        config = Config()

        config.set("monitor", {"enabled": True, "auto_terminate": True, "dry_run": False})

        assert config.get("monitor.auto_terminate", True) is False
        assert config.get("monitor").get("auto_terminate", True) is False
        assert "auto_terminate" not in config.to_dict()["monitor"]
        assert config.get("monitor.enabled") is True

    def test_writing_the_parent_does_not_persist_the_key(self, config_path: Path) -> None:
        """The saved file must not carry a setting the code no longer honours."""
        config = Config()
        config.set("monitor", {"enabled": True, "auto_terminate": True})

        assert config.save() is True

        saved = yaml.safe_load(config_path.read_text())
        assert "auto_terminate" not in saved["monitor"]

    def test_replace_cannot_reintroduce_the_key(self, config_path: Path) -> None:
        """A full-configuration write must be filtered too."""
        config = Config()

        config.replace({"webserver": "apache", "monitor": {"auto_terminate": True}})

        assert config.get("monitor.auto_terminate", True) is False
        assert config.get("webserver") == "apache"


class TestReplacePreservesSecrets:
    """A full write coming from the panel carries placeholders, not secrets."""

    def test_placeholders_do_not_erase_stored_secrets(self, config_path: Path) -> None:
        """Saving the panel form back must keep the password that was hidden."""
        config = Config()
        config.set("monitor.smtp.password", "hunter2")

        config.replace(
            {
                "monitor": {"smtp": {"password": REDACTED, "host": "smtp.example.com"}},
                "webserver": "apache",
            }
        )

        assert config.get("monitor.smtp.password") == "hunter2"
        assert config.get("monitor.smtp.host") == "smtp.example.com"

    def test_replace_does_not_alias_the_caller_dictionary(self, config_path: Path) -> None:
        """The stored configuration must not share containers with the request."""
        payload = {"monitor": {"enabled": True}}
        config = Config()

        config.replace(payload)
        payload["monitor"]["enabled"] = False

        assert config.get("monitor.enabled") is True


class TestDeepMergeIsolation:
    """``_deep_merge`` is the only thing keeping DEFAULT_CONFIG out of reach."""

    def test_merge_shares_no_nested_container_with_its_inputs(self) -> None:
        """A shallow copy here is how secrets leaked into the module defaults."""
        base = {"outer": {"inner": {"value": 1}}}
        override = {"other": {"value": 2}}

        merged = Config._deep_merge(Config(), base, override)

        assert merged["outer"] is not base["outer"]
        assert merged["outer"]["inner"] is not base["outer"]["inner"]
        assert merged["other"] is not override["other"]

    def test_upgrade_does_not_leave_the_defaults_reachable(self, config_path: Path) -> None:
        """After upgrade(), a secret written on the instance must stay local."""
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(yaml.safe_dump({"webserver": "apache"}))

        config = Config()
        result = config.upgrade()
        assert result["upgraded"] is True

        config.set("monitor.smtp.password", "leaked")
        config.set("databases.credentials.mysql.password", "leaked")

        assert DEFAULT_CONFIG["monitor"]["smtp"]["password"] == ""
        assert DEFAULT_CONFIG["databases"]["credentials"]["mysql"]["password"] == ""


class TestSecureDirectory:
    """Every level created on the way to a secret must be private."""

    def test_intermediate_directories_are_not_world_readable(self, sandbox: Path) -> None:
        """pathlib creates parents with 0777 & umask unless the mode is passed."""
        previous = os.umask(0o022)
        try:
            target = sandbox / "outer" / "middle" / "inner"

            secure_directory(target)

            for level in (sandbox / "outer", sandbox / "outer" / "middle", target):
                assert level.stat().st_mode & 0o077 == 0, f"{level} is not private"
        finally:
            os.umask(previous)

    def test_existing_directories_are_not_touched(self, sandbox: Path) -> None:
        """A pre-existing shared parent must keep its mode; only new levels are ours."""
        parent = sandbox / "shared"
        parent.mkdir(mode=0o755)
        parent.chmod(0o755)

        secure_directory(parent / "wasm")

        assert parent.stat().st_mode & 0o777 == 0o755
        assert (parent / "wasm").stat().st_mode & 0o077 == 0


class TestSymlinkAttacks:
    """Whoever can write in /etc/wasm must not be able to redirect the write."""

    def test_secure_write_refuses_to_follow_a_symlink(self, sandbox: Path) -> None:
        """A symlinked config file must not become a write into the target."""
        victim = sandbox / "victim.txt"
        victim.write_text("original\n")
        link = sandbox / "config.yaml"
        link.symlink_to(victim)

        with pytest.raises(SecurityError):
            secure_write(link, "webserver: nginx\n")

        assert victim.read_text() == "original\n"

    def test_secure_write_refuses_a_dangling_symlink(self, sandbox: Path) -> None:
        """A link to a not-yet-existing file must not create the target."""
        target = sandbox / "not-there.txt"
        link = sandbox / "config.yaml"
        link.symlink_to(target)

        with pytest.raises(SecurityError):
            secure_write(link, "webserver: nginx\n")

        assert not target.exists()

    def test_restrict_file_does_not_chmod_through_a_symlink(self, sandbox: Path) -> None:
        """restrict_file must inspect the link itself, not what it points at."""
        victim = sandbox / "victim.txt"
        victim.write_text("original\n")
        victim.chmod(0o644)
        link = sandbox / "config.yaml"
        link.symlink_to(victim)

        restrict_file(link)

        assert victim.stat().st_mode & 0o777 == 0o644

    def test_restrict_file_ignores_a_dangling_symlink(self, sandbox: Path) -> None:
        """A broken link must not raise: exists() lies, lstat does not."""
        link = sandbox / "config.yaml"
        link.symlink_to(sandbox / "not-there.txt")

        restrict_file(link)

    def test_config_save_reports_failure_on_a_symlinked_path(self, config_path: Path) -> None:
        """save() must fail closed rather than write through a planted link."""
        config_path.parent.mkdir(parents=True, exist_ok=True)
        victim = config_path.parent / "victim.txt"
        victim.write_text("original\n")
        config_path.symlink_to(victim)

        assert Config().save() is False
        assert victim.read_text() == "original\n"


class TestEnvFilePermissions:
    """Deployed .env files carry DATABASE_URL, API keys and generated secrets."""

    def test_env_file_is_owner_only(self, sandbox: Path) -> None:
        """A .env written next to the app must not be world readable."""
        app_path = sandbox / "app"
        app_path.mkdir()

        written = EnvManager().write_env_files(app_path, {"DATABASE_URL": "postgres://s3cret"})

        assert written == [app_path / ".env"]
        assert (app_path / ".env").stat().st_mode & 0o077 == 0

    def test_existing_world_readable_env_is_tightened(self, sandbox: Path) -> None:
        """A .env left lax by an older version must be repaired on rewrite."""
        app_path = sandbox / "app"
        app_path.mkdir()
        env_file = app_path / ".env"
        env_file.write_text("OLD=1\n")
        env_file.chmod(0o644)

        EnvManager().write_env_files(app_path, {"API_KEY": "sk-live"})

        assert env_file.stat().st_mode & 0o077 == 0

    def test_mapped_env_files_are_owner_only(self, sandbox: Path) -> None:
        """The monorepo path writes several files; all of them are secret.""" ""
        app_path = sandbox / "app"
        app_path.mkdir()

        written = EnvManager().write_env_files(
            app_path,
            {"API_KEY": "sk-live", "PORT": "3000"},
            file_mapping={"apps/web/.env": ["API_KEY"], ".env": ["PORT"]},
        )

        for path in written:
            assert path.stat().st_mode & 0o077 == 0, f"{path} is not private"

    def test_env_file_is_never_briefly_world_readable(
        self, sandbox: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        The mode must come from the open(), not from a chmod afterwards.

        The write is atomic, so the descriptor that carries the secrets belongs
        to a temporary file next to the destination. Every file created under
        the application directory is inspected rather than only ``.env``, or
        the assertion would pass by looking at nothing.
        """
        app_path = sandbox / "app"
        app_path.mkdir()
        modes: list[int] = []
        real_open = os.open

        def recording_open(path, flags, mode=0o777, **kwargs):
            fd = real_open(path, flags, mode, **kwargs)
            if flags & os.O_CREAT and Path(path).parent == app_path:
                modes.append(stat.S_IMODE(os.fstat(fd).st_mode))
            return fd

        monkeypatch.setattr(os, "open", recording_open)
        EnvManager().write_env_files(app_path, {"API_KEY": "sk-live"})

        assert modes == [0o600]
        assert stat.S_IMODE((app_path / ".env").stat().st_mode) == 0o600

    def test_app_directory_is_not_tightened(self, sandbox: Path) -> None:
        """The app tree is served by the web server; only the file is private."""
        app_path = sandbox / "app"
        app_path.mkdir(mode=0o755)
        app_path.chmod(0o755)

        EnvManager().write_env_files(app_path, {"API_KEY": "sk-live"})

        assert app_path.stat().st_mode & 0o777 == 0o755

    def test_env_config_json_is_owner_only(self, sandbox: Path) -> None:
        """.wasm/env-config.json records the variable inventory and defaults."""
        app_path = sandbox / "app"
        app_path.mkdir()
        config = EnvConfig(variables=[EnvVariable(name="API_KEY", secret=True, value="sk-live")])

        EnvManager().save_config(app_path, config)

        saved = app_path / ".wasm" / "env-config.json"
        assert saved.stat().st_mode & 0o077 == 0
        assert saved.parent.stat().st_mode & 0o077 == 0
        assert json.loads(saved.read_text())["variables"][0]["name"] == "API_KEY"

    def test_env_manager_refuses_to_write_through_a_symlink(self, sandbox: Path) -> None:
        """A symlinked .env in a shared app directory must not leak elsewhere."""
        app_path = sandbox / "app"
        app_path.mkdir()
        victim = sandbox / "victim.txt"
        victim.write_text("original\n")
        (app_path / ".env").symlink_to(victim)

        with pytest.raises(SecurityError):
            EnvManager().write_env_files(app_path, {"API_KEY": "sk-live"})

        assert victim.read_text() == "original\n"


class TestSetupCreatesAPrivateConfigDirectory:
    """/etc/wasm holds config.yaml, and WASM runs as root: 0700 is the model."""

    def test_config_directory_is_owner_only(
        self, sandbox: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """setup must not widen the directory that holds the credentials."""
        from wasm.cli.commands import setup as setup_cli

        config_path = sandbox / "etc" / "wasm" / "config.yaml"
        monkeypatch.setattr(setup_cli, "DEFAULT_CONFIG_PATH", config_path)
        previous = os.umask(0o022)
        try:
            assert setup_cli._create_config_directory(setup_cli.Logger()) is True
        finally:
            os.umask(previous)

        assert config_path.parent.stat().st_mode & 0o077 == 0


class TestConfigWritesGoThroughTheSeam:
    """
    ``--dry-run`` has to be true for what WASM writes, not only what it runs.

    Deleting or overwriting ``/etc/wasm/config.yaml`` is a ``Path`` call and
    never reaches a subprocess, so swapping the command runner alone left the
    rehearsal free to destroy the file it had just promised not to touch.
    """

    def test_save_writes_nothing_during_a_rehearsal(self, config_path: Path) -> None:
        """A rehearsed save must not create the configuration file."""
        set_fs(DryRunFileSystem())

        assert Config().save() is True

        assert not config_path.exists()
        assert not config_path.parent.exists()

    def test_save_does_not_destroy_the_existing_file_during_a_rehearsal(
        self, config_path: Path
    ) -> None:
        """The old credentials must survive a rehearsal untouched."""
        config_path.parent.mkdir(parents=True)
        config_path.write_text("webserver: apache\n", encoding="utf-8")
        config = Config()
        config.set("webserver", "nginx")
        set_fs(DryRunFileSystem())

        assert config.save() is True

        assert config_path.read_text(encoding="utf-8") == "webserver: apache\n"

    def test_upgrade_writes_nothing_during_a_rehearsal(self, config_path: Path) -> None:
        """upgrade() rewrites the whole file; a rehearsal must not."""
        config_path.parent.mkdir(parents=True)
        config_path.write_text("webserver: nginx\n", encoding="utf-8")
        set_fs(DryRunFileSystem())

        Config().upgrade()

        assert config_path.read_text(encoding="utf-8") == "webserver: nginx\n"

    def test_secure_directory_creates_nothing_during_a_rehearsal(self, sandbox: Path) -> None:
        """No level of the path may appear, and the missing leaf must not raise."""
        set_fs(DryRunFileSystem())

        secure_directory(sandbox / "outer" / "inner")

        assert not (sandbox / "outer").exists()

    def test_secure_write_leaves_no_temporary_file_behind(self, sandbox: Path) -> None:
        """A rehearsal that leaves a staging file behind is still a change."""
        target = sandbox / "etc" / "wasm" / "config.yaml"
        set_fs(DryRunFileSystem())

        secure_write(target, "webserver: nginx\n")

        assert list(sandbox.iterdir()) == []

    def test_a_rehearsal_says_what_it_would_have_written(self, config_path: Path) -> None:
        """Announcing the skipped change is the half that makes it a rehearsal."""
        rehearsal = DryRunFileSystem()
        set_fs(rehearsal)

        Config().save()

        assert any(str(config_path) in line for line in rehearsal.skipped)

    def test_the_saved_file_and_its_directory_carry_the_secret_modes(
        self, config_path: Path
    ) -> None:
        """config.yaml is 0600 inside a 0700 directory, asked for explicitly."""
        recorder = RecordingFileSystem()
        set_fs(recorder)

        assert Config().save() is True

        assert ("write", config_path) in recorder.changes
        assert ("mkdir", config_path.parent) in recorder.changes
        assert stat.S_IMODE(config_path.stat().st_mode) == SECRET_MODE
        assert stat.S_IMODE(config_path.parent.stat().st_mode) == SECRET_DIR_MODE

    def test_an_injected_filesystem_wins_over_the_process_wide_one(self, config_path: Path) -> None:
        """Constructor injection is what lets a caller rehearse one instance."""
        rehearsal = DryRunFileSystem()
        Config.reset_instance()

        assert Config(fs=rehearsal).save() is True

        assert not config_path.exists()
        assert rehearsal.skipped != []


class TestNoMutationEscapesTheSeam:
    """
    The AST guard. Migrating once is easy; staying migrated is what this is for.

    Every write, delete, chmod and mkdir in these modules has to go through
    :mod:`wasm.core.fs`, or ``--dry-run`` starts lying again the next time
    somebody reaches for ``Path.write_text`` because it is one line shorter.
    """

    #: Methods that change the filesystem, whatever they are called on.
    MUTATING_METHODS = frozenset(
        {
            "chmod",
            "copy2",
            "copyfile",
            "copytree",
            "hardlink_to",
            "lchmod",
            "makedirs",
            "mkdir",
            "mkdtemp",
            "mkstemp",
            "rmdir",
            "rmtree",
            "symlink_to",
            "touch",
            "unlink",
            "write_bytes",
            "write_text",
        }
    )

    #: Module-qualified calls that change the filesystem. Spelled out per
    #: module because ``replace``, ``rename``, ``move`` and ``copy`` are also
    #: perfectly innocent method names on strings, dicts and paths' neighbours.
    MUTATING_FUNCTIONS = frozenset(
        {
            ("os", "chmod"),
            ("os", "chown"),
            ("os", "link"),
            ("os", "makedirs"),
            ("os", "mkdir"),
            ("os", "remove"),
            ("os", "removedirs"),
            ("os", "rename"),
            ("os", "renames"),
            ("os", "replace"),
            ("os", "rmdir"),
            ("os", "symlink"),
            ("os", "truncate"),
            ("os", "unlink"),
            ("shutil", "copy"),
            ("shutil", "copy2"),
            ("shutil", "copyfile"),
            ("shutil", "copytree"),
            ("shutil", "move"),
            ("shutil", "rmtree"),
            ("tempfile", "NamedTemporaryFile"),
            ("tempfile", "TemporaryDirectory"),
            ("tempfile", "mkdtemp"),
            ("tempfile", "mkstemp"),
        }
    )

    #: The modules this guard covers.
    MODULES = (
        "src/wasm/core/config.py",
        "src/wasm/cli/commands/setup.py",
        "src/wasm/cli/commands/env.py",
    )

    @staticmethod
    def _is_seam(node: ast.expr) -> bool:
        """
        Decide whether an expression evaluates to the filesystem seam.

        Args:
            node: The receiver of an attribute call.

        Returns:
            True for ``fs``, ``self.fs``, ``filesystem``, ``get_fs()`` and the
            like, which are the only receivers allowed to mutate.
        """
        if isinstance(node, ast.Call):
            return isinstance(node.func, ast.Name) and node.func.id == "get_fs"
        name = node.id if isinstance(node, ast.Name) else getattr(node, "attr", "")
        return name in {"fs", "filesystem"} or name.endswith(("_fs", "_filesystem"))

    @classmethod
    def _violations(cls, source: str, label: str) -> list[str]:
        """
        Find every direct filesystem mutation in a module.

        Args:
            source: Python source text.
            label: Name to report offences under.

        Returns:
            One ``label:line: call`` string per offence.
        """
        found: list[str] = []
        for node in ast.walk(ast.parse(source)):
            if not isinstance(node, ast.Call):
                continue

            func = node.func
            if isinstance(func, ast.Name):
                if func.id in {"open", "mkdtemp", "mkstemp"} and _opens_for_writing(node):
                    found.append(f"{label}:{func.lineno}: {func.id}()")
                continue

            if not isinstance(func, ast.Attribute):
                continue

            owner = func.value.id if isinstance(func.value, ast.Name) else None
            if (owner, func.attr) in cls.MUTATING_FUNCTIONS:
                found.append(f"{label}:{func.lineno}: {owner}.{func.attr}()")
                continue
            if func.attr == "open" and _opens_for_writing(node):
                found.append(f"{label}:{func.lineno}: .open() for writing")
                continue
            if func.attr in cls.MUTATING_METHODS and not cls._is_seam(func.value):
                found.append(f"{label}:{func.lineno}: .{func.attr}()")

        return found

    @pytest.mark.parametrize("module", MODULES)
    def test_the_module_mutates_nothing_directly(self, module: str) -> None:
        """No write, delete, chmod or mkdir outside wasm.core.fs."""
        path = Path(__file__).resolve().parents[1] / module

        offences = self._violations(path.read_text(encoding="utf-8"), module)

        assert offences == [], "These change the filesystem without the seam:\n" + "\n".join(
            offences
        )

    def test_the_guard_catches_what_it_is_there_to_catch(self) -> None:
        """
        A guard nobody has seen fail is a guard nobody knows works.

        Each line below is a real way the migration was undone in review.
        """
        mutations = [
            "path.write_text('secret')",
            "path.unlink()",
            "path.chmod(0o600)",
            "path.parent.mkdir(parents=True)",
            "path.symlink_to(other)",
            "path.touch()",
            "shutil.rmtree(path)",
            "shutil.move(path, path)",
            "shutil.copytree(path, path)",
            "os.replace(path, path)",
            "os.remove(path)",
            "os.makedirs(path)",
            "tempfile.mkdtemp()",
            "open(path, 'w').close()",
            "open(path, mode=chosen).close()",
            "path.open('a').close()",
        ]
        allowed = [
            "fs.write_text(path, 'fine')",
            "self.fs.chmod(path, 0o600)",
            "get_fs().remove(path)",
            "filesystem.mkdir(path)",
            "self._fs.write_text(path, 'fine')",
        ]
        header = ["import os, shutil, tempfile", "def leak(path, other, chosen, fs, self):"]
        source = "\n".join(header + [f"    {line}" for line in mutations + allowed])

        offences = self._violations(source, "sample")

        # One offence per mutation, none for the seam calls, and each is
        # reported at the line the reviewer has to look at.
        expected = {len(header) + index + 1 for index in range(len(mutations))}
        assert {int(offence.split(":")[1]) for offence in offences} == expected

    def test_reading_is_not_flagged(self) -> None:
        """Reads change nothing; routing them through the seam is only noise."""
        reading = "\n".join(
            [
                "from pathlib import Path",
                "def look(path):",
                "    if path.exists():",
                "        return path.read_text(), list(path.iterdir()), path.stat()",
                "    with path.open() as handle:",
                "        return handle.read()",
            ]
        )

        assert self._violations(reading, "sample") == []


def _opens_for_writing(node: ast.Call) -> bool:
    """
    Decide whether an ``open`` call creates or truncates a file.

    ``open(path)`` and ``open(path, "r")`` are reads. Anything with ``w``,
    ``a``, ``x`` or ``+`` in its mode changes the filesystem, and a mode that is
    not a literal is treated as a change: a guard that assumes the best is not
    a guard.

    Args:
        node: The ``open`` call.

    Returns:
        True when the call may change the filesystem.
    """
    mode: ast.expr | None = None
    for index, argument in enumerate(node.args):
        # open(file, mode) on the builtin, path.open(mode) on a Path.
        if index in (0, 1):
            mode = argument if isinstance(argument, ast.Constant) else mode
    for keyword in node.keywords:
        if keyword.arg == "mode":
            mode = keyword.value

    if mode is None:
        return False
    if not isinstance(mode, ast.Constant) or not isinstance(mode.value, str):
        return True
    return any(flag in mode.value for flag in "wax+")
