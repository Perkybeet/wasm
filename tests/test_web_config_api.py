# Copyright (c) 2024-2025 Yago López Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
Tests for the configuration web API.

Three defect classes are pinned here:

- **Secrets served in clear.** ``GET /api/config`` and ``POST /api/config/reload``
  returned the MySQL root password, the OpenAI API key and the SMTP password to
  any panel session. Every response that carries configuration must go through
  :func:`~wasm.core.config.redact_secrets`.
- **A second configuration writer.** The module used to write ``config.yaml``
  with ``open(path, 'w')`` and ``mkdir()`` without a mode, which silently undid
  the 0600/0700 hardening on the very path the panel uses. There is exactly one
  writer, :class:`~wasm.core.config.Config`.
- **Settings that no longer exist.** A body may not reintroduce a key the code
  stopped honouring, such as the monitor's process termination switches.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient

from wasm.core.config import DEFAULT_CONFIG, REDACTED, Config
from wasm.web.api import config as config_api
from wasm.web.api.auth import get_current_session

#: Secrets planted in the stored configuration; none may reach a response.
PLANTED_SECRETS = {
    "databases.credentials.mysql.password": "mysql-root-hunter2",
    "databases.credentials.postgresql.password": "postgres-hunter2",
    "databases.credentials.redis.password": "redis-hunter2",
    "databases.credentials.mongodb.password": "mongo-hunter2",
    "monitor.smtp.password": "smtp-hunter2",
    "monitor.openai.api_key": "sk-live-openai",
}


@pytest.fixture
def config_path(sandbox: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """
    Point the global config file at the sandbox and reset the singleton.

    Args:
        sandbox: Isolated filesystem root.
        monkeypatch: Patching helper, scoped to the test.

    Yields:
        Path the API reads from and writes to.
    """
    path = sandbox / "etc" / "wasm" / "config.yaml"
    monkeypatch.setattr("wasm.core.config.DEFAULT_CONFIG_PATH", path)
    Config.reset_instance()
    try:
        yield path
    finally:
        Config.reset_instance()


@pytest.fixture
def client(config_path: Path) -> TestClient:
    """
    Build a test client for the config router with authentication stubbed out.

    Args:
        config_path: Fixture redirecting configuration writes into the sandbox.

    Returns:
        A client whose requests are already authenticated.
    """
    app = FastAPI()
    app.include_router(config_api.router, prefix="/api/config")
    app.dependency_overrides[get_current_session] = lambda: {"session_id": "test"}
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def stored_secrets(config_path: Path) -> dict[str, str]:
    """
    Write a configuration file that carries every known credential.

    Args:
        config_path: Path the API reads from.

    Returns:
        The dotted key to secret value mapping that was persisted.
    """
    config = Config()
    for key, value in PLANTED_SECRETS.items():
        config.set(key, value)
    assert config.save() is True
    Config.reset_instance()
    return dict(PLANTED_SECRETS)


def stored_value(config_path: Path, dotted_key: str) -> Any:
    """
    Read a value straight from the configuration file on disk.

    Args:
        config_path: Path of the configuration file.
        dotted_key: Dotted path of the setting.

    Returns:
        The stored value, or None when absent.
    """
    node: Any = yaml.safe_load(config_path.read_text())
    for part in dotted_key.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


class TestSecretsNeverLeave:
    """The panel is authenticated, but the response body is still not a vault."""

    def test_get_config_redacts_every_planted_secret(
        self, client: TestClient, stored_secrets: dict[str, str]
    ) -> None:
        """No credential may appear anywhere in the payload."""
        response = client.get("/api/config")

        assert response.status_code == 200
        body = response.text
        for key, secret in stored_secrets.items():
            assert secret not in body, f"{key} was served in clear"

    def test_get_config_shows_the_placeholder(
        self, client: TestClient, stored_secrets: dict[str, str]
    ) -> None:
        """Secrets are replaced, not removed, so the form still renders."""
        config = client.get("/api/config").json()["config"]

        assert config["monitor"]["openai"]["api_key"] == REDACTED
        assert config["databases"]["credentials"]["mysql"]["password"] == REDACTED
        assert config["databases"]["credentials"]["mysql"]["user"] == "root"
        assert config["webserver"] == "nginx"

    def test_reload_redacts_too(self, client: TestClient, stored_secrets: dict[str, str]) -> None:
        """The reload endpoint returns the same dump and must redact it."""
        response = client.post("/api/config/reload")

        assert response.status_code == 200
        for secret in stored_secrets.values():
            assert secret not in response.text

    def test_no_secret_key_in_the_whole_tree_is_served_in_clear(
        self, client: TestClient, config_path: Path
    ) -> None:
        """Walk the real configuration tree, not a hand-picked list of keys."""
        config = Config()
        markers = {}
        for index, key in enumerate(_secret_keys(DEFAULT_CONFIG)):
            marker = f"leaked-{index}-{key.replace('.', '-')}"
            markers[key] = marker
            config.set(key, marker)
        assert config.save() is True
        Config.reset_instance()

        body = client.get("/api/config").text

        for key, marker in markers.items():
            assert marker not in body, f"{key} was served in clear"

    @pytest.mark.parametrize(
        "endpoint",
        ["", "/apps-directory", "/webserver", "/backup", "/ssl", "/web", "/defaults"],
    )
    def test_read_endpoints_carry_no_secret(
        self, client: TestClient, stored_secrets: dict[str, str], endpoint: str
    ) -> None:
        """Every reader is a potential leak, not just the full dump."""
        response = client.get(f"/api/config{endpoint}")

        assert response.status_code == 200
        for secret in stored_secrets.values():
            assert secret not in response.text


def _secret_keys(node: Any, prefix: str = "") -> list[str]:
    """
    List the dotted paths of the scalar settings whose key names a secret.

    Args:
        node: Configuration subtree to walk.
        prefix: Dotted path of ``node`` itself.

    Returns:
        Dotted paths of values that must never be served in clear.
    """
    from wasm.core.config import _is_secret_key

    found: list[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            if isinstance(value, (dict, list)):
                found.extend(_secret_keys(value, path))
            elif _is_secret_key(str(key)):
                found.append(path)
    return found


class TestSingleWriter:
    """Config is the only thing allowed to create /etc/wasm/config.yaml."""

    def test_the_second_writer_is_gone(self) -> None:
        """save_config_file() bypassed secure_write(); it must not come back."""
        assert not hasattr(config_api, "save_config_file")

    @pytest.mark.parametrize(
        ("method", "endpoint", "payload"),
        [
            ("put", "", {"config": {"webserver": "apache"}}),
            ("patch", "", {"path": "web.port", "value": 9090}),
            ("put", "/apps-directory", {"apps_directory": "/srv/apps"}),
            ("put", "/webserver", {"webserver": "apache"}),
            ("put", "/backup", {"directory": "/var/backups/wasm", "max_per_app": 5}),
            ("put", "/ssl", {"enabled": True, "provider": "certbot", "email": "a@b.c"}),
            ("put", "/web", {"host": "127.0.0.1", "port": 8081, "session_timeout": 600}),
        ],
    )
    def test_every_write_endpoint_produces_a_private_file(
        self,
        client: TestClient,
        config_path: Path,
        method: str,
        endpoint: str,
        payload: dict[str, Any],
    ) -> None:
        """The 0600 file and 0700 directory must hold on every write path."""
        response = getattr(client, method)(f"/api/config{endpoint}", json=payload)

        assert response.status_code == 200, response.text
        assert config_path.exists()
        assert config_path.stat().st_mode & 0o077 == 0
        assert config_path.parent.stat().st_mode & 0o077 == 0

    def test_a_preexisting_lax_file_is_tightened_by_a_write(
        self, client: TestClient, config_path: Path
    ) -> None:
        """A file left world readable by an older version is repaired, not kept."""
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text("webserver: nginx\n")
        config_path.chmod(0o644)

        response = client.put("/api/config/webserver", json={"webserver": "apache"})

        assert response.status_code == 200, response.text
        assert config_path.stat().st_mode & 0o077 == 0


class TestWritesPreserveSecrets:
    """The panel sends back the placeholder it was shown."""

    def test_full_update_with_placeholders_keeps_the_stored_secrets(
        self, client: TestClient, config_path: Path, stored_secrets: dict[str, str]
    ) -> None:
        """A round trip through the panel must not wipe the credentials."""
        shown = client.get("/api/config").json()["config"]
        shown["webserver"] = "apache"

        response = client.put("/api/config", json={"config": shown})

        assert response.status_code == 200, response.text
        assert stored_value(config_path, "webserver") == "apache"
        assert (
            stored_value(config_path, "monitor.openai.api_key")
            == stored_secrets["monitor.openai.api_key"]
        )
        assert (
            stored_value(config_path, "databases.credentials.mysql.password")
            == stored_secrets["databases.credentials.mysql.password"]
        )

    def test_a_rotated_secret_is_stored(
        self, client: TestClient, config_path: Path, stored_secrets: dict[str, str]
    ) -> None:
        """Placeholders are ignored, real values are not."""
        response = client.patch(
            "/api/config",
            json={"path": "monitor.openai.api_key", "value": "sk-rotated"},
        )

        assert response.status_code == 200, response.text
        assert stored_value(config_path, "monitor.openai.api_key") == "sk-rotated"

    def test_patching_a_secret_with_the_placeholder_is_a_no_op(
        self, client: TestClient, config_path: Path, stored_secrets: dict[str, str]
    ) -> None:
        """Saving a form the user did not touch must not blank the password."""
        response = client.patch(
            "/api/config",
            json={"path": "monitor.smtp.password", "value": REDACTED},
        )

        assert response.status_code == 200, response.text
        assert (
            stored_value(config_path, "monitor.smtp.password")
            == stored_secrets["monitor.smtp.password"]
        )

    def test_a_response_never_echoes_the_secret_it_just_stored(
        self, client: TestClient, config_path: Path
    ) -> None:
        """The PATCH acknowledgement must not repeat the value back."""
        response = client.patch(
            "/api/config",
            json={"path": "monitor.openai.api_key", "value": "sk-rotated"},
        )

        assert response.status_code == 200
        assert "sk-rotated" not in response.text


class TestRemovedKeysCannotComeBack:
    """The monitor reports; it does not kill processes."""

    def test_full_update_drops_the_termination_switches(
        self, client: TestClient, config_path: Path
    ) -> None:
        """A stale panel posting the old form must not re-enable them."""
        response = client.put(
            "/api/config",
            json={
                "config": {
                    "webserver": "nginx",
                    "monitor": {
                        "enabled": True,
                        "auto_terminate": True,
                        "terminate_malicious_only": False,
                        "dry_run": False,
                    },
                }
            },
        )

        assert response.status_code == 200, response.text
        assert stored_value(config_path, "monitor.auto_terminate") is None
        assert stored_value(config_path, "monitor.dry_run") is None
        assert stored_value(config_path, "monitor.enabled") is True

    def test_patch_cannot_reintroduce_a_removed_key(
        self, client: TestClient, config_path: Path
    ) -> None:
        """Neither can a targeted patch."""
        response = client.patch(
            "/api/config", json={"path": "monitor.auto_terminate", "value": True}
        )

        assert response.status_code in (200, 400), response.text
        assert stored_value(config_path, "monitor.auto_terminate") is None


class TestUpdateSemantics:
    """The panel expects its edits to survive a reload."""

    def test_patch_persists_a_nested_value(self, client: TestClient, config_path: Path) -> None:
        """A dotted path must create the intermediate containers."""
        response = client.patch("/api/config", json={"path": "backup.max_per_app", "value": 7})

        assert response.status_code == 200, response.text
        assert stored_value(config_path, "backup.max_per_app") == 7

    def test_apps_directory_round_trip(self, client: TestClient) -> None:
        """What was written must be what is read back."""
        assert (
            client.put(
                "/api/config/apps-directory", json={"apps_directory": "/srv/apps"}
            ).status_code
            == 200
        )

        assert client.get("/api/config/apps-directory").json()["apps_directory"] == "/srv/apps"

    def test_ssl_round_trip(self, client: TestClient) -> None:
        """The SSL block must survive the write/read cycle."""
        payload = {"enabled": False, "provider": "certbot", "email": "ops@example.com"}

        assert client.put("/api/config/ssl", json=payload).status_code == 200

        assert client.get("/api/config/ssl").json() == payload

    def test_webserver_rejects_an_unknown_value(
        self, client: TestClient, config_path: Path
    ) -> None:
        """Only nginx and apache are implemented."""
        response = client.put("/api/config/webserver", json={"webserver": "iis"})

        assert response.status_code == 400
        assert not config_path.exists()

    def test_a_fresh_install_is_reported_as_writable(self, client: TestClient) -> None:
        """No config file yet must not look like a read-only deployment."""
        response = client.get("/api/config")

        assert response.json()["writable"] is True

    def test_get_reports_the_single_configuration_path(self, client: TestClient) -> None:
        """The panel shows the path; it must be the one Config actually uses."""
        response = client.get("/api/config")

        assert response.json()["path"] == str(Config().path)


class TestWriteFailures:
    """A failed write must be reported, never silently swallowed."""

    def test_symlinked_config_file_is_refused(self, client: TestClient, config_path: Path) -> None:
        """Whoever can write in the directory must not redirect the write."""
        config_path.parent.mkdir(parents=True, exist_ok=True)
        victim = config_path.parent / "victim.txt"
        victim.write_text("original\n")
        config_path.symlink_to(victim)

        response = client.put("/api/config/webserver", json={"webserver": "apache"})

        assert response.status_code >= 400
        assert victim.read_text() == "original\n"

    def test_permission_denied_is_reported_as_403(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A read-only /etc must produce a 403, not a 500 or a false success."""

        def deny(*args: Any, **kwargs: Any) -> None:
            raise PermissionError(13, "Permission denied")

        monkeypatch.setattr(config_api.Config, "write", deny)

        response = client.put("/api/config/webserver", json={"webserver": "apache"})

        assert response.status_code == 403
