# Copyright (c) 2024-2026 Yago López Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
Tests for Task 1.10: security hardening.

Five defects, each closed at its own chokepoint:

- **The notifier is an SSRF vector.** A webhook URL is configuration; the
  request it produces is this process reaching out with attacker-influenced
  content from the machine that runs systemd as root. Resolution is
  mocked through :func:`wasm.core.notifier._resolve_host`, never real DNS.
- **A plain-HTTP bind reachable from another machine.** ``run_server`` is
  where the socket is actually bound, so it refuses the exposure again even
  though the CLI already refused it once.
- **A hashed build asset and the SPA shell shared one Cache-Control.**
  ``/assets/*`` may be cached forever; everything else must not be cached at
  all.
- **A forged webhook signature was a free guess.** It now counts against the
  same lockout a bad master token does.
- **The SQL console's read mode ran as the engine's superuser/admin
  account.** It now runs as a dedicated, least-privilege role or user that
  cannot read server files or reach superuser-only catalogs.
"""

# The notifier's, the database managers' and the hooks fixtures are imported
# rather than replicated, so there stays one definition of each. Ruff reads a
# test parameter named after an imported fixture as a redefinition; here it
# is the mechanism.
# ruff: noqa: F811

from __future__ import annotations

import hashlib
import hmac
import io
import ipaddress
import uuid
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request

import pytest
from fastapi.testclient import TestClient

import wasm.core.notifier as notifier_module
from tests.test_database_managers import mysql, postgres  # noqa: F401  (fixtures)
from tests.test_notifier import CapturingOpener, config, make_event  # noqa: F401  (fixtures)
from wasm.core.exceptions import SecurityError
from wasm.core.notifier import Notifier
from wasm.core.runner import FakeRunner
from wasm.core.store import App, WASMStore
from wasm.web.api import hooks as hooks_module
from wasm.web.api.hooks import mint_webhook_secret
from wasm.web.auth import SecurityConfig
from wasm.web.server import ASSETS_DIR, run_server
from wasm.web.server import create_app as build_app

PSQL_PREFIX = ("runuser", "-u", "postgres", "--", "psql")

#: The address under test, never actually bound: uvicorn.run is mocked below.
ALL_INTERFACES = "0.0.0.0"  # noqa: S104

# --------------------------------------------------------------------------
# Notifier SSRF guard
# --------------------------------------------------------------------------


class TestResolveHost:
    """The default resolver, without a monkeypatch."""

    def test_a_literal_address_resolves_to_itself_without_a_lookup(self) -> None:
        """A literal never needs DNS, so this assertion never touches a socket."""
        assert notifier_module._resolve_host("127.0.0.1") == ("127.0.0.1",)


class TestForbiddenNetworks:
    """:func:`_is_forbidden` against every network the guard names."""

    @pytest.mark.parametrize(
        "address",
        [
            "127.0.0.1",  # loopback
            "10.0.0.5",  # RFC 1918
            "172.16.0.1",  # RFC 1918
            "192.168.1.1",  # RFC 1918
            "169.254.169.254",  # link-local / cloud metadata
            "100.64.0.1",  # CGNAT
            "0.0.0.5",  # "this network"
            "::1",  # IPv6 loopback
            "fc00::1",  # IPv6 unique-local
            "fe80::1",  # IPv6 link-local
        ],
    )
    def test_every_named_network_is_forbidden(self, address: str) -> None:
        assert notifier_module._is_forbidden(ipaddress.ip_address(address)) is True

    def test_an_ordinary_public_address_is_not_forbidden(self) -> None:
        assert notifier_module._is_forbidden(ipaddress.ip_address("93.184.216.34")) is False


class TestNotifierSSRFGuard:
    """The guard as :class:`Notifier` actually reaches it."""

    def test_refuses_a_loopback_destination(
        self, config: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config.set("notifications.enabled", True)
        config.set("notifications.channels.webhook.webhook_url", "http://127.0.0.1:9000/hook")
        monkeypatch.setattr(notifier_module, "_resolve_host", lambda host: ("127.0.0.1",))
        opener = CapturingOpener()

        Notifier(config, opener=opener).notify(make_event())

        assert opener.requests == []

    def test_refuses_an_rfc1918_destination(
        self, config: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config.set("notifications.enabled", True)
        config.set("notifications.channels.webhook.webhook_url", "http://internal.example/hook")
        monkeypatch.setattr(notifier_module, "_resolve_host", lambda host: ("10.0.0.5",))
        opener = CapturingOpener()

        Notifier(config, opener=opener).notify(make_event())

        assert opener.requests == []

    def test_refuses_the_cloud_metadata_address(
        self, config: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config.set("notifications.enabled", True)
        config.set("notifications.channels.webhook.webhook_url", "http://metadata.internal/latest")
        monkeypatch.setattr(notifier_module, "_resolve_host", lambda host: ("169.254.169.254",))
        opener = CapturingOpener()

        Notifier(config, opener=opener).notify(make_event())

        assert opener.requests == []

    def test_a_genuinely_public_destination_is_delivered(
        self, config: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config.set("notifications.enabled", True)
        config.set("notifications.channels.webhook.webhook_url", "http://hooks.example.test/wasm")
        monkeypatch.setattr(notifier_module, "_resolve_host", lambda host: ("93.184.216.34",))
        opener = CapturingOpener()

        Notifier(config, opener=opener).notify(make_event())

        assert len(opener.requests) == 1

    def test_allow_private_hosts_bypasses_the_guard(
        self, config: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config.set("notifications.enabled", True)
        config.set("notifications.channels.webhook.webhook_url", "http://internal.example/hook")
        config.set("notifications.allow_private_hosts", ["internal.example"])
        monkeypatch.setattr(notifier_module, "_resolve_host", lambda host: ("10.0.0.5",))
        opener = CapturingOpener()

        Notifier(config, opener=opener).notify(make_event())

        assert len(opener.requests) == 1

    def test_a_redirect_into_a_private_address_is_refused(
        self, config: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        A public host answering with a redirect to a private one is refused.

        urlopen follows redirects transparently, so the guard has to run
        again at every hop; this exercises the handler urllib calls for that,
        directly, since the suite injects an opener that bypasses urllib's
        own redirect machinery entirely.
        """
        addresses = {"internal.example": ("127.0.0.1",)}
        monkeypatch.setattr(
            notifier_module,
            "_resolve_host",
            lambda host: addresses.get(host, ("93.184.216.34",)),
        )
        handler = notifier_module._SafeRedirectHandler(config)
        request = Request("https://public.example/hook")

        with pytest.raises(ValueError, match="private or internal network"):
            handler.redirect_request(request, None, 302, "Found", {}, "http://internal.example/")

    def test_a_redirect_to_another_public_host_is_left_to_the_base_handler(
        self, config: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(notifier_module, "_resolve_host", lambda host: ("93.184.216.34",))
        handler = notifier_module._SafeRedirectHandler(config)
        request = Request("https://public.example/hook")

        built = handler.redirect_request(
            request, None, 302, "Found", {}, "https://also-public.example/hook"
        )

        assert built is not None
        assert built.full_url == "https://also-public.example/hook"

    def test_the_test_button_never_returns_the_remote_response_body(
        self, config: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        D5: the test button answers with status only, never the remote body.

        Unlike notify()'s log line, this string is rendered straight into the
        settings page; echoing the body would let a reachable destination
        hand content back to whoever is looking at the panel.
        """
        config.set("notifications.channels.webhook.webhook_url", "http://hooks.example.test/wasm")
        monkeypatch.setattr(notifier_module, "_resolve_host", lambda host: ("93.184.216.34",))
        opener = CapturingOpener()
        opener.errors["http://hooks.example.test"] = HTTPError(
            "http://hooks.example.test/wasm",
            500,
            "Internal Server Error",
            None,
            io.BytesIO(b"leaked-internal-debug-trace"),
        )

        result = Notifier(config, opener=opener).test_channel("webhook")

        assert result == "HTTP 500 Internal Server Error"
        assert result is not None
        assert "leaked-internal-debug-trace" not in result


# --------------------------------------------------------------------------
# run_server: TLS-or-loopback is enforced at the chokepoint that binds
# --------------------------------------------------------------------------


class TestRunServerRefusesUnprotectedExposure:
    def test_a_non_loopback_plain_http_bind_is_refused(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[dict[str, Any]] = []
        monkeypatch.setattr("uvicorn.run", lambda **kwargs: calls.append(kwargs))

        with pytest.raises(SecurityError, match="--insecure-http"):
            run_server(host=ALL_INTERFACES, port=8080)

        assert calls == []

    def test_insecure_http_opts_in_explicitly(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[dict[str, Any]] = []
        monkeypatch.setattr("uvicorn.run", lambda **kwargs: calls.append(kwargs))

        run_server(
            host=ALL_INTERFACES,
            port=8080,
            config=SecurityConfig(state_dir=tmp_path / "state", rate_limit_requests=5000),
            show_token=False,
            insecure_http=True,
        )

        assert len(calls) == 1

    def test_loopback_needs_neither_tls_nor_the_opt_out(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[dict[str, Any]] = []
        monkeypatch.setattr("uvicorn.run", lambda **kwargs: calls.append(kwargs))

        run_server(
            host="127.0.0.1",
            port=8080,
            config=SecurityConfig(state_dir=tmp_path / "state", rate_limit_requests=5000),
            show_token=False,
        )

        assert len(calls) == 1


# --------------------------------------------------------------------------
# Path-aware Cache-Control
# --------------------------------------------------------------------------


class TestCacheControlIsPathAware:
    def _client(self, tmp_path: Path) -> TestClient:
        app = build_app(SecurityConfig(state_dir=tmp_path / "state", rate_limit_requests=5000))
        return TestClient(app)

    def test_a_hashed_asset_is_cached_forever(self, tmp_path: Path) -> None:
        client = self._client(tmp_path)
        asset = next((ASSETS_DIR).glob("*.js")).name

        response = client.get(f"/assets/{asset}")

        assert response.status_code == 200, response.text
        assert response.headers["cache-control"] == "public, max-age=31536000, immutable"

    def test_a_missing_asset_is_never_stored(self, tmp_path: Path) -> None:
        """A 404 under ``/assets`` must not outlive the deploy that fixes it."""
        client = self._client(tmp_path)

        response = client.get("/assets/does-not-exist.js")

        assert response.status_code == 404
        assert response.headers["cache-control"] == "no-store"

    def test_everything_else_is_never_stored(self, tmp_path: Path) -> None:
        client = self._client(tmp_path)

        response = client.get("/health")

        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"


# --------------------------------------------------------------------------
# Webhook signature failures feed the lockout
# --------------------------------------------------------------------------

DOMAIN = "hooks.example.com"


@pytest.fixture(autouse=True)
def fresh_delivery_cache() -> None:
    """The replay cache is process-wide; no test may inherit another's ids."""
    hooks_module._deliveries.clear()


@pytest.fixture
def store(tmp_path: Path) -> Any:
    WASMStore.reset_instance()
    instance = WASMStore(tmp_path / "wasm.db")
    try:
        yield instance
    finally:
        instance.close()
        WASMStore.reset_instance()


@pytest.fixture
def seeded(store: WASMStore) -> App:
    return store.create_app(
        App(
            domain=DOMAIN,
            app_type="nodejs",
            source="https://github.com/you/app",
            branch="main",
            port=3000,
            app_path=f"/var/www/apps/{DOMAIN.replace('.', '-')}",
        )
    )


@pytest.fixture
def secret(store: WASMStore, seeded: App) -> str:
    return mint_webhook_secret(DOMAIN)


@pytest.fixture
def lockout_app(tmp_path: Path, store: WASMStore) -> Any:
    return build_app(
        SecurityConfig(
            state_dir=tmp_path / "state", rate_limit_requests=5000, max_failed_attempts=3
        )
    )


@pytest.fixture
def hook_client(lockout_app: Any) -> TestClient:
    return TestClient(lockout_app, client=("testclient", 50000))


class TestWebhookSignatureFailuresLockTheApplication:
    """
    Bad signatures are counted per application, never per address.

    They used to feed the address lockout, and a forge delivers every
    customer's webhooks from a few shared addresses: three bad deliveries
    from anyone on the same forge locked the forge out of the panel. See
    tests/test_web_hooks.py for the per-application lockout itself.
    """

    def test_bad_signatures_do_not_lock_the_address_out_of_signing_in(
        self, hook_client: TestClient, secret: str
    ) -> None:
        body = b'{"ref": "refs/heads/main"}'
        for _ in range(3):
            response = hook_client.post(
                f"/hooks/deploy/{DOMAIN}",
                content=body,
                headers={
                    "X-Hub-Signature-256": "sha256=" + "0" * 64,
                    "X-GitHub-Delivery": str(uuid.uuid4()),
                    "Content-Type": "application/json",
                },
            )
            assert response.status_code == 401

        login = hook_client.post("/api/auth/login", json={"token": "whatever-it-is"})

        assert login.status_code == 401, login.text

    def test_bad_logins_do_not_stop_the_forge_delivering(
        self, hook_client: TestClient, secret: str
    ) -> None:
        for _ in range(3):
            hook_client.post("/api/auth/login", json={"token": "whatever-it-is"})
        assert hook_client.post("/api/auth/login", json={"token": "x"}).status_code == 429

        body = b'{"ref": "refs/heads/other"}'
        signature = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        response = hook_client.post(
            f"/hooks/deploy/{DOMAIN}",
            content=body,
            headers={
                "X-Hub-Signature-256": signature,
                "X-GitHub-Delivery": str(uuid.uuid4()),
                "Content-Type": "application/json",
            },
        )

        # Authentic, and ignored only because it pushed another branch.
        assert response.status_code == 200, response.text


# --------------------------------------------------------------------------
# SQL console read mode: least-privilege role / account
# --------------------------------------------------------------------------


class TestPostgresReadOnlyRole:
    """See wasm.managers.database.postgres.PostgresManager._ensure_read_only_role."""

    def test_read_mode_signs_in_as_the_role(self, postgres: Any, runner: FakeRunner) -> None:
        runner.script(PSQL_PREFIX, stdout="1\n")

        postgres.execute_query(database="shop", query="SELECT 1", read_only=True)

        # Not a superuser session that switched roles, which one SELECT can
        # switch back (set_config('role', ...)): the login is the role.
        call = runner.calls[-1]
        sent = [call[i + 1] for i, arg in enumerate(call[:-1]) if arg == "-c"]
        assert sent == ["BEGIN READ ONLY", "SELECT 1", "COMMIT"]
        assert call[0] == "psql"
        assert call[call.index("-U") + 1] == "wasm_ro_shop"
        assert call[call.index("-h") + 1] == "127.0.0.1"

    def test_the_role_is_provisioned_least_privilege(
        self, postgres: Any, runner: FakeRunner
    ) -> None:
        runner.script(PSQL_PREFIX, stdout="1\n")

        postgres.execute_query(database="shop", query="SELECT 1", read_only=True)

        # Provisioning happens in its own call, ahead of the read-only query.
        provisioning = runner.inputs[-2]
        assert "CREATE ROLE" in provisioning
        assert "NOSUPERUSER" in provisioning
        assert "LOGIN PASSWORD 'SCRAM-SHA-256$" in provisioning
        assert 'GRANT CONNECT ON DATABASE "shop" TO "wasm_ro_shop";' in provisioning
        assert "GRANT USAGE ON SCHEMA" in provisioning
        assert "GRANT SELECT ON ALL TABLES IN SCHEMA" in provisioning
        # Nothing here reaches for a file-reading, superuser-only function:
        # pg_read_file/pg_ls_dir are refused because the session signs in as
        # this role, not by this statement, but the role it creates asks for
        # none of the broader grants (ALL PRIVILEGES, pg_read_server_files
        # membership) that would defeat the point.
        assert "ALL PRIVILEGES" not in provisioning
        assert len(runner.calls) == 3

    def test_a_write_never_touches_the_read_only_role(
        self, postgres: Any, runner: FakeRunner
    ) -> None:
        runner.script(PSQL_PREFIX, stdout="1\n")

        postgres.execute_query(database="shop", query="DELETE FROM t", read_only=False)

        assert not any("wasm_ro_" in (call_input or "") for call_input in runner.inputs)
        assert len(runner.calls) == 2

    def test_a_long_database_name_does_not_overflow_the_identifier_limit(
        self, postgres: Any, runner: FakeRunner
    ) -> None:
        runner.script(PSQL_PREFIX, stdout="1\n")
        long_name = "x" * 60

        postgres.execute_query(database=long_name, query="SELECT 1", read_only=True)

        call = runner.calls[-1]
        role = call[call.index("-U") + 1]
        assert len(role) <= 63


class TestMySQLReadOnlyAccount:
    """See wasm.managers.database.mysql.MySQLManager._ensure_read_only_user."""

    def test_read_mode_connects_with_a_dedicated_option_file(
        self, mysql: Any, runner: FakeRunner
    ) -> None:
        runner.script(["mysql"], stdout="shop\n")

        mysql.execute_query(database="shop", query="SELECT 1", read_only=True)

        final_call = runner.calls[-1]
        assert any(arg.startswith("--defaults-extra-file=") for arg in final_call)
        sent = runner.inputs[-1]
        assert sent.startswith("START TRANSACTION READ ONLY;")
        assert sent.rstrip().endswith("COMMIT;")

    def test_the_account_is_provisioned_least_privilege(
        self, mysql: Any, runner: FakeRunner
    ) -> None:
        runner.script(["mysql"], stdout="shop\n")

        mysql.execute_query(database="shop", query="SELECT 1", read_only=True)

        provisioning = runner.inputs[-2]
        assert "CREATE USER IF NOT EXISTS 'wasm_ro_shop'@'localhost'" in provisioning
        assert "REVOKE ALL PRIVILEGES, GRANT OPTION FROM" in provisioning
        assert "GRANT SELECT ON `shop`.* TO" in provisioning
        assert "FILE" not in provisioning
        assert "SUPER" not in provisioning
        assert len(runner.calls) == 3

    def test_the_provisioning_statement_never_reaches_argv(
        self, mysql: Any, runner: FakeRunner
    ) -> None:
        """
        ``IDENTIFIED BY`` carries the freshly generated password.

        It travels to the client on stdin, as every statement in this module
        does; CLAUDE.md's rule one is that a secret never appears in argv,
        which is visible to every local user through ``ps``.
        """
        runner.script(["mysql"], stdout="shop\n")

        mysql.execute_query(database="shop", query="SELECT 1", read_only=True)

        for call in runner.calls:
            assert not any("IDENTIFIED" in arg for arg in call)

    def test_a_write_never_provisions_the_read_only_account(
        self, mysql: Any, runner: FakeRunner
    ) -> None:
        runner.script(["mysql"], stdout="shop\n")

        mysql.execute_query(database="shop", query="DELETE FROM t", read_only=False)

        assert not any("wasm_ro_" in (call_input or "") for call_input in runner.inputs)
        assert len(runner.calls) == 2

    def test_a_long_database_name_does_not_overflow_the_identifier_limit(
        self, mysql: Any, runner: FakeRunner
    ) -> None:
        runner.script(["mysql"], stdout="x" * 40 + "\n")
        long_name = "x" * 40

        mysql.execute_query(database=long_name, query="SELECT 1", read_only=True)

        provisioning = runner.inputs[-2]
        # "CREATE USER IF NOT EXISTS '<name>'@'localhost'" - the account name
        # inside the quotes must fit MySQL's 32-character limit.
        account = provisioning.split("CREATE USER IF NOT EXISTS '")[1].split("'@")[0]
        assert len(account) <= 32
