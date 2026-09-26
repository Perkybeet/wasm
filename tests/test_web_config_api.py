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

import json
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
from wasm.web.auth import CSRF_HEADER_NAME, AuditLogger, SecurityConfig, set_audit_logger
from wasm.web.server import create_app, get_token_manager

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
def audit_log(tmp_path: Path) -> Iterator[Path]:
    """
    Install a real audit logger for the duration of a test.

    Args:
        tmp_path: Per-test temporary directory.

    Yields:
        Path the audit entries are appended to.
    """
    path = tmp_path / "web-audit.log"
    set_audit_logger(AuditLogger(path))
    try:
        yield path
    finally:
        set_audit_logger(None)


def read_audit(path: Path) -> list[dict[str, Any]]:
    """
    Read the audit log written during a test.

    Args:
        path: Path of the audit log file.

    Returns:
        One dict per audit line, oldest first. Empty when nothing was written.
    """
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


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

    def test_an_unset_secret_reads_back_empty_not_redacted(self, client: TestClient) -> None:
        """
        Nothing planted a secret here, so the console must see "not
        configured", not the same ``***`` a real password would show -
        Settings > Notifications needs this to grey out a channel's "test"
        button honestly.
        """
        config = client.get("/api/config").json()["config"]

        assert config["monitor"]["smtp"]["password"] == ""

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

    def test_patch_coerces_a_string_boolean_for_a_key_with_no_default(
        self, client: TestClient, config_path: Path
    ) -> None:
        """
        A caller posting form-shaped data (everything a string) hits the same
        coercion gap the CLI has: monitor.notify has no default, so
        Config.set stored the literal string "false" until PATCH used the
        same schema-aware coercion 'wasm config set' does.
        """
        response = client.patch("/api/config", json={"path": "monitor.notify", "value": "false"})

        assert response.status_code == 200, response.text
        assert stored_value(config_path, "monitor.notify") is False

    def test_patch_leaves_an_already_typed_value_alone(
        self, client: TestClient, config_path: Path
    ) -> None:
        """A JSON boolean sent as JSON must not be coerced a second time."""
        response = client.patch("/api/config", json={"path": "monitor.notify", "value": True})

        assert response.status_code == 200, response.text
        assert stored_value(config_path, "monitor.notify") is True

    def test_patch_normalises_the_deprecated_apps_directory_alias(
        self, client: TestClient, config_path: Path
    ) -> None:
        """
        'apps.directory' used to be a second, independent setting from the
        flat 'apps_directory' every deployer reads. A PATCH addressed at the
        alias must land on the canonical key, not create a separate 'apps'
        container nothing reads.
        """
        response = client.patch(
            "/api/config", json={"path": "apps.directory", "value": "/srv/apps"}
        )

        assert response.status_code == 200, response.text
        assert stored_value(config_path, "apps_directory") == "/srv/apps"
        assert stored_value(config_path, "apps") is None

    def test_full_replace_folds_the_deprecated_apps_directory_alias(
        self, client: TestClient, config_path: Path
    ) -> None:
        """A whole-config PUT written with the old nested shape is folded too."""
        response = client.put("/api/config", json={"config": {"apps": {"directory": "/srv/apps"}}})

        assert response.status_code == 200, response.text
        assert stored_value(config_path, "apps_directory") == "/srv/apps"
        assert stored_value(config_path, "apps") is None

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


class TestSavesAreAudited:
    """
    Every way of saving settings leaves a record of what changed, not what to.

    The server-rendered settings page this API replaced did both; losing that
    on the way to the panel would mean an operator has no way to answer "who
    changed the web server" or "who pointed the apps directory somewhere new".
    """

    def test_full_update_is_audited_with_the_changed_top_level_keys(
        self, client: TestClient, audit_log: Path
    ) -> None:
        response = client.put("/api/config", json={"config": {"webserver": "apache"}})

        assert response.status_code == 200, response.text
        updates = [e for e in read_audit(audit_log) if e["action"] == "config.update"]
        assert updates, "a full configuration write must be audited"
        assert updates[0]["result"] == "success"
        assert updates[0]["resource"] == "/api/config"
        assert "webserver" in updates[0]["detail"]

    def test_patch_is_audited_with_the_top_level_key_of_the_dotted_path(
        self, client: TestClient, audit_log: Path
    ) -> None:
        response = client.patch("/api/config", json={"path": "web.port", "value": 9090})

        assert response.status_code == 200, response.text
        updates = [e for e in read_audit(audit_log) if e["action"] == "config.update"]
        assert updates, "a patch must be audited"
        assert updates[0]["detail"] == "changed keys: web"

    @pytest.mark.parametrize(
        ("endpoint", "payload", "expected_key"),
        [
            ("/apps-directory", {"apps_directory": "/srv/apps"}, "apps_directory"),
            ("/webserver", {"webserver": "apache"}, "webserver"),
            ("/backup", {"directory": "/var/backups/wasm", "max_per_app": 5}, "backup"),
            ("/ssl", {"enabled": True, "provider": "certbot", "email": "a@b.c"}, "ssl"),
            ("/web", {"host": "127.0.0.1", "port": 8081, "session_timeout": 600}, "web"),
        ],
    )
    def test_every_typed_section_put_is_audited(
        self,
        client: TestClient,
        audit_log: Path,
        endpoint: str,
        payload: dict[str, Any],
        expected_key: str,
    ) -> None:
        response = client.put(f"/api/config{endpoint}", json=payload)

        assert response.status_code == 200, response.text
        updates = [e for e in read_audit(audit_log) if e["action"] == "config.update"]
        assert updates, f"PUT {endpoint} must be audited"
        assert expected_key in updates[0]["detail"]

    def test_audit_entry_never_carries_a_secret_value(
        self, client: TestClient, audit_log: Path, stored_secrets: dict[str, str]
    ) -> None:
        """The changed key name is recorded; what it changed to is not."""
        response = client.patch(
            "/api/config", json={"path": "monitor.openai.api_key", "value": "sk-rotated"}
        )

        assert response.status_code == 200, response.text
        raw = audit_log.read_text()
        assert "sk-rotated" not in raw
        for secret in stored_secrets.values():
            assert secret not in raw

    def test_a_refused_write_is_not_reported_as_a_change(
        self, client: TestClient, audit_log: Path
    ) -> None:
        """A value that never reached disk must not appear as a successful update."""
        response = client.put("/api/config/webserver", json={"webserver": "iis"})

        assert response.status_code == 400
        assert [e for e in read_audit(audit_log) if e["action"] == "config.update"] == []


class TestRelativeAppsDirectoryIsRefused:
    """A relative apps directory resolves against whatever CWD a caller has."""

    def test_the_typed_endpoint_refuses_a_relative_path(
        self, client: TestClient, config_path: Path
    ) -> None:
        response = client.put("/api/config/apps-directory", json={"apps_directory": "var/www/apps"})

        assert response.status_code == 400
        assert "absolute path" in response.text
        assert not config_path.exists()

    def test_patch_refuses_a_relative_path(self, client: TestClient, config_path: Path) -> None:
        response = client.patch(
            "/api/config", json={"path": "apps_directory", "value": "relative/apps"}
        )

        assert response.status_code == 400
        assert "absolute path" in response.text

    def test_full_replace_refuses_a_relative_path(
        self, client: TestClient, config_path: Path
    ) -> None:
        response = client.put("/api/config", json={"config": {"apps_directory": "relative/apps"}})

        assert response.status_code == 400
        assert "absolute path" in response.text
        assert not config_path.exists()

    def test_an_absolute_path_is_accepted(self, client: TestClient, config_path: Path) -> None:
        """The guard rejects relative paths, not the directory setting itself."""
        response = client.put("/api/config/apps-directory", json={"apps_directory": "/srv/apps"})

        assert response.status_code == 200, response.text
        assert client.get("/api/config/apps-directory").json()["apps_directory"] == "/srv/apps"


class TestTypedSectionsNeedElevation:
    """
    The five typed section saves need sudo mode exactly like ``PUT``/``PATCH
    /api/config`` already do: a config write is as destructive as anything
    else on D5's list, and ``config.set("apps_directory", ...)`` is no less
    dangerous for arriving through the typed endpoint than through the raw
    one.

    Unlike the rest of this file, these use a real application and a real
    cookie session - the fake session the ``client`` fixture installs has no
    ``source``, which trivially satisfies :func:`ensure_elevated` and would
    prove nothing about the guard under test.
    """

    @pytest.fixture
    def real_app(self, config_path: Path) -> FastAPI:
        """
        Args:
            config_path: Fixture redirecting configuration writes into the
                sandbox; also provides the sandboxed state dir this needs.

        Returns:
            The full application, not just the config router.
        """
        state_dir = config_path.parent.parent / "web-state"
        return create_app(SecurityConfig(state_dir=state_dir, rate_limit_requests=5000))

    @pytest.fixture
    def master_token(self, real_app: FastAPI) -> str:
        """
        Returns:
            A freshly generated master token for ``real_app``.
        """
        return get_token_manager().generate_master_token()

    @pytest.fixture
    def cookie_client(self, real_app: FastAPI, master_token: str) -> TestClient:
        """
        Args:
            real_app: The application.
            master_token: The credential to log in with.

        Returns:
            A client signed in with the master token, not yet elevated.
        """
        signed_in = TestClient(real_app, client=("testclient", 50000), follow_redirects=False)
        response = signed_in.post("/api/auth/login", json={"token": master_token})
        assert response.status_code == 200, response.text
        signed_in.headers[CSRF_HEADER_NAME] = response.json()["csrf_token"]
        return signed_in

    @pytest.mark.parametrize(
        ("path", "body"),
        [
            ("/api/config/apps-directory", {"apps_directory": "/srv/apps"}),
            ("/api/config/webserver", {"webserver": "nginx"}),
            ("/api/config/backup", {"directory": "/var/backups/wasm", "max_per_app": 5}),
            ("/api/config/ssl", {"enabled": True, "provider": "certbot", "email": "ops@x.com"}),
            ("/api/config/web", {"host": "127.0.0.1", "port": 8080, "session_timeout": 3600}),
        ],
    )
    def test_a_fresh_cookie_session_is_refused(
        self, cookie_client: TestClient, path: str, body: dict
    ) -> None:
        response = cookie_client.put(path, json=body)

        assert response.status_code == 403, response.text
        assert response.json()["error"] == "elevation_required"

    @pytest.mark.parametrize(
        ("path", "body"),
        [
            ("/api/config/apps-directory", {"apps_directory": "/srv/apps"}),
            ("/api/config/webserver", {"webserver": "nginx"}),
            ("/api/config/backup", {"directory": "/var/backups/wasm", "max_per_app": 5}),
            ("/api/config/ssl", {"enabled": True, "provider": "certbot", "email": "ops@x.com"}),
            ("/api/config/web", {"host": "127.0.0.1", "port": 8080, "session_timeout": 3600}),
        ],
    )
    def test_an_elevated_cookie_session_may_write(
        self, cookie_client: TestClient, master_token: str, path: str, body: dict
    ) -> None:
        elevate_response = cookie_client.post("/api/auth/elevate", json={"token": master_token})
        assert elevate_response.status_code == 200, elevate_response.text

        response = cookie_client.put(path, json=body)

        assert response.status_code == 200, response.text
