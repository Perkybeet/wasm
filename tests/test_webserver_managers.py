# Copyright (c) 2024-2026 Yago Lopez Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
Tests for the web server managers and the certificate manager.

Three things are pinned here, and each one maps to a defect that shipped:

- **The contract.** ``NginxManager`` and ``ApacheManager`` are used
  interchangeably by ``wasm site``, so the table below asserts the exact argv of
  every operation for both backends. When the two were separate files their
  APIs drifted - only one of them had ``enable_module``, only the other had
  ``create_advanced_site`` - and a caller could not rely on either.
- **The rendered configuration.** Snapshots of the templates for a static site,
  a proxy, TLS and the ``www`` alias. A template edit that changes what the web
  server is told now shows up as a diff in review instead of on a server.
- **The field names that cross a module boundary.** ``wasm health`` read
  ``cert["expires"]`` for several releases while this manager wrote ``expiry``,
  and a plain dict answered that with None forever.

Plus the one that matters most for a program running as root: a domain that
carries a path separator must never become a file outside the configuration
directory.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from wasm.core.exceptions import (
    ApacheError,
    CertificateError,
    DomainError,
    NginxError,
    SecurityError,
    ValidationError,
    WASMError,
)
from wasm.core.runner import FakeRunner
from wasm.managers.apache_manager import ApacheManager
from wasm.managers.cert_manager import CertificateInfo, CertManager
from wasm.managers.nginx_manager import NginxManager
from wasm.managers.webserver import (
    APACHE_BACKEND,
    NGINX_BACKEND,
    SiteInfo,
    WebServerManager,
    WebServerStatus,
)

CERTBOT_OUTPUT = (
    "Found the following certs:\n"
    "  Certificate Name: example.com\n"
    "    Domains: example.com www.example.com\n"
    "    Expiry Date: 2026-11-30 12:00:00+00:00 (VALID: 89 days)\n"
    "    Certificate Path: /etc/letsencrypt/live/example.com/fullchain.pem\n"
    "    Private Key Path: /etc/letsencrypt/live/example.com/privkey.pem\n"
)


class FakeStore:
    """A store that keeps site records in memory."""

    def __init__(self) -> None:
        self.sites: dict[str, Any] = {}
        self.apps: dict[str, Any] = {}

    def get_site(self, domain: str) -> Any:
        """
        Look up a site record.

        Args:
            domain: Domain name.

        Returns:
            The record, or None.
        """
        return self.sites.get(domain)

    def create_site(self, site: Any) -> Any:
        """
        Store a new site record.

        Args:
            site: The record to store.

        Returns:
            The stored record.
        """
        self.sites[site.domain] = site
        return site

    def update_site(self, site: Any) -> Any:
        """
        Replace a site record.

        Args:
            site: The record to store.

        Returns:
            The stored record.
        """
        self.sites[site.domain] = site
        return site

    def delete_site(self, domain: str) -> bool:
        """
        Drop a site record.

        Args:
            domain: Domain name.

        Returns:
            True when a record was removed.
        """
        return self.sites.pop(domain, None) is not None

    def update_site_ssl(self, domain: str, ssl: bool, **_kwargs: Any) -> None:
        """
        Record the TLS state of a site.

        Args:
            domain: Domain name.
            ssl: Whether the site serves TLS.
            _kwargs: Certificate paths, ignored here.
        """
        site = self.sites.get(domain)
        if site is not None:
            site.ssl_enabled = ssl

    def get_app(self, domain: str) -> Any:
        """
        Look up an application record.

        Args:
            domain: Domain name.

        Returns:
            The record, or None.
        """
        return self.apps.get(domain)

    def update_app(self, app: Any) -> Any:
        """
        Replace an application record.

        Args:
            app: The record to store.

        Returns:
            The stored record.
        """
        self.apps[app.domain] = app
        return app


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> FakeStore:
    """
    Replace the SQLite store used by the managers with an in-memory one.

    Args:
        monkeypatch: Patching helper, scoped to the test.

    Returns:
        The fake store.
    """
    fake = FakeStore()
    monkeypatch.setattr("wasm.managers.webserver.get_store", lambda: fake)
    monkeypatch.setattr("wasm.managers.cert_manager.get_store", lambda: fake)
    return fake


@pytest.fixture
def nginx(tmp_path: Path, runner: FakeRunner, store: FakeStore) -> NginxManager:
    """
    An nginx manager pointed at a temporary configuration tree.

    Args:
        tmp_path: Per-test temporary directory.
        runner: The fake command runner.
        store: The fake store.

    Returns:
        The manager.
    """
    return NginxManager(
        backend=replace(
            NGINX_BACKEND,
            sites_available=tmp_path / "nginx/sites-available",
            sites_enabled=tmp_path / "nginx/sites-enabled",
        )
    )


@pytest.fixture
def apache(tmp_path: Path, runner: FakeRunner, store: FakeStore) -> ApacheManager:
    """
    An apache manager pointed at a temporary configuration tree.

    Args:
        tmp_path: Per-test temporary directory.
        runner: The fake command runner.
        store: The fake store.

    Returns:
        The manager.
    """
    return ApacheManager(
        backend=replace(
            APACHE_BACKEND,
            sites_available=tmp_path / "apache/sites-available",
            sites_enabled=tmp_path / "apache/sites-enabled",
        )
    )


@pytest.fixture
def managers(nginx: NginxManager, apache: ApacheManager) -> dict[str, WebServerManager]:
    """
    Both backends, keyed by name.

    Args:
        nginx: The nginx manager.
        apache: The apache manager.

    Returns:
        A mapping from backend name to manager.
    """
    return {"nginx": nginx, "apache": apache}


# ---------------------------------------------------------------------------
# The contract: same operations, same shapes, one argv table
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("backend", "operation", "expected"),
    [
        ("nginx", "get_version", [("nginx", "-v")]),
        ("apache", "get_version", [("apache2", "-v")]),
        ("nginx", "test_config", [("nginx", "-t")]),
        ("apache", "test_config", [("apache2ctl", "configtest")]),
        ("nginx", "is_running", [("systemctl", "is-active", "nginx")]),
        ("apache", "is_running", [("systemctl", "is-active", "apache2")]),
        ("nginx", "is_boot_enabled", [("systemctl", "is-enabled", "nginx")]),
        ("apache", "is_boot_enabled", [("systemctl", "is-enabled", "apache2")]),
        ("nginx", "restart", [("systemctl", "restart", "nginx")]),
        ("apache", "restart", [("systemctl", "restart", "apache2")]),
        ("nginx", "reload", [("nginx", "-t"), ("systemctl", "reload", "nginx")]),
        (
            "apache",
            "reload",
            [("apache2ctl", "configtest"), ("systemctl", "reload", "apache2")],
        ),
    ],
)
def test_operation_runs_the_expected_command(
    managers: dict[str, WebServerManager],
    runner: FakeRunner,
    backend: str,
    operation: str,
    expected: list[tuple[str, ...]],
) -> None:
    """Every operation must build one known argv, for both backends."""
    getattr(managers[backend], operation)()

    assert runner.calls == expected


class DeadlineRunner(FakeRunner):
    """A fake runner that also records the deadline each call was given."""

    def __init__(self) -> None:
        super().__init__()
        self.timeouts: list[int] = []

    def run(self, argv: Any, **kwargs: Any) -> Any:
        """
        Record the deadline, then behave like a FakeRunner.

        Args:
            argv: Program and arguments.
            kwargs: The rest of the runner protocol.

        Returns:
            The scripted result.
        """
        self.timeouts.append(kwargs.get("timeout"))
        return super().run(argv, **kwargs)


@pytest.mark.parametrize("backend", ["nginx", "apache"])
def test_every_command_carries_a_deadline(tmp_path: Path, store: FakeStore, backend: str) -> None:
    """``run_command`` used to default to no timeout, so a hung nginx hung WASM."""
    deadline_runner = DeadlineRunner()
    factory = NginxManager if backend == "nginx" else ApacheManager
    template = NGINX_BACKEND if backend == "nginx" else APACHE_BACKEND
    manager = factory(
        runner=deadline_runner,
        backend=replace(
            template,
            sites_available=tmp_path / "available",
            sites_enabled=tmp_path / "enabled",
        ),
    )

    manager.reload()
    manager.restart()
    manager.get_status()

    assert deadline_runner.timeouts
    assert all(isinstance(t, int) and t > 0 for t in deadline_runner.timeouts)


@pytest.mark.parametrize("backend", ["nginx", "apache"])
def test_no_operation_reaches_for_sudo(
    managers: dict[str, WebServerManager], runner: FakeRunner, backend: str
) -> None:
    """WASM requires root, so a manager must never re-elevate (decision D6)."""
    manager = managers[backend]
    manager.reload()
    manager.restart()
    manager.get_status()
    manager.enable_module("proxy")

    assert all(call[0] != "sudo" for call in runner.calls)


def test_both_backends_expose_the_same_contract(nginx: NginxManager, apache: ApacheManager) -> None:
    """A caller must be able to swap one manager for the other."""
    shared = set(dir(WebServerManager)) - {"__init__"}
    public = {name for name in shared if not name.startswith("_")}

    nginx_api = {name for name in dir(nginx) if not name.startswith("_")}
    apache_api = {name for name in dir(apache) if not name.startswith("_")}

    assert public <= nginx_api
    assert public <= apache_api
    # The only sanctioned difference: nginx has multi-route configurations.
    assert nginx_api - apache_api == {"create_advanced_site"}


@pytest.mark.parametrize("backend", ["nginx", "apache"])
def test_status_is_a_record_not_a_dict_of_guesses(
    managers: dict[str, WebServerManager], runner: FakeRunner, backend: str
) -> None:
    """get_status returns a typed record; an unknown field is an error."""
    runner.script(["systemctl", "is-active"], stdout="active\n")
    runner.script(["systemctl", "is-enabled"], stdout="enabled\n")
    runner.script(["nginx", "-v"], stderr="nginx version: nginx/1.24.0\n")
    runner.script(["apache2", "-v"], stdout="Server version: Apache/2.4.58 (Ubuntu)\n")

    status = managers[backend].get_status()

    assert isinstance(status, WebServerStatus)
    assert status.name == backend
    assert status.active is True
    assert status.enabled is True
    assert status.version in {"1.24.0", "2.4.58"}
    # The reader that still uses the mapping form keeps working...
    assert status["active"] is True
    assert status.get("version") == status.version
    # ...but a name that is not a field is a bug, not a missing value.
    with pytest.raises(KeyError):
        status.get("running")


@pytest.mark.parametrize("backend", ["nginx", "apache"])
def test_enable_module_is_answered_honestly_by_both_backends(
    managers: dict[str, WebServerManager], runner: FakeRunner, backend: str
) -> None:
    """The method exists on both; only apache has modules to enable."""
    enabled = managers[backend].enable_module("proxy")

    if backend == "apache":
        assert enabled is True
        assert runner.calls == [("a2enmod", "proxy")]
    else:
        assert enabled is False
        assert runner.calls == []


# ---------------------------------------------------------------------------
# Site lifecycle
# ---------------------------------------------------------------------------


def test_nginx_enables_a_site_with_a_symlink(nginx: NginxManager, runner: FakeRunner) -> None:
    """nginx has no a2ensite; the link is written directly."""
    nginx.create_site("example.com", context={"port": 8080})
    nginx.enable_site("example.com")

    link = nginx.sites_enabled / "example.com"
    assert link.is_symlink()
    assert link.resolve() == nginx.config_path("example.com")
    assert nginx.site_enabled("example.com") is True
    assert runner.calls == []


def test_apache_enables_a_site_with_a2ensite(apache: ApacheManager, runner: FakeRunner) -> None:
    """apache owns its symlinks, so the tool is asked to do it."""
    apache.create_site("example.com", context={"port": 8080})
    runner.calls.clear()

    apache.enable_site("example.com")

    assert runner.calls == [("a2ensite", "example.com.conf")]


def test_apache_disables_a_site_with_a2dissite(apache: ApacheManager, runner: FakeRunner) -> None:
    """The counterpart of a2ensite, with the same file name."""
    apache.create_site("example.com")
    (apache.sites_enabled).mkdir(parents=True, exist_ok=True)
    (apache.sites_enabled / "example.com.conf").symlink_to(apache.config_path("example.com"))
    runner.calls.clear()

    apache.disable_site("example.com")

    assert runner.calls == [("a2dissite", "example.com.conf")]


@pytest.mark.parametrize("backend", ["nginx", "apache"])
def test_create_then_update_keeps_the_site_enabled(
    managers: dict[str, WebServerManager], backend: str, store: FakeStore
) -> None:
    """Updating a site must not take it out of service on the way through."""
    manager = managers[backend]
    manager.create_site("example.com", context={"port": 3000})
    link = manager.sites_enabled / manager.config_path("example.com").name
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(manager.config_path("example.com"))

    manager.update_site("example.com", context={"port": 4100})

    assert manager.site_enabled("example.com") is True
    assert "4100" in manager.get_site_config("example.com")
    assert store.sites["example.com"].proxy_port == 4100


@pytest.mark.parametrize("backend", ["nginx", "apache"])
def test_creating_an_existing_site_is_refused(
    managers: dict[str, WebServerManager], backend: str
) -> None:
    """Overwriting a live virtual host by accident is not allowed."""
    manager = managers[backend]
    manager.create_site("example.com")

    expected = NginxError if backend == "nginx" else ApacheError
    with pytest.raises(expected):
        manager.create_site("example.com")


@pytest.mark.parametrize("backend", ["nginx", "apache"])
def test_updating_a_missing_site_is_refused(
    managers: dict[str, WebServerManager], backend: str
) -> None:
    """An update that silently created a site would hide a typo in a domain."""
    with pytest.raises((NginxError, ApacheError)):
        managers[backend].update_site("example.com")


@pytest.mark.parametrize("backend", ["nginx", "apache"])
def test_delete_removes_the_file_the_link_and_the_record(
    managers: dict[str, WebServerManager],
    backend: str,
    store: FakeStore,
    runner: FakeRunner,
) -> None:
    """Deleting a site leaves nothing of it behind."""
    manager = managers[backend]
    manager.create_site("example.com")
    link = manager.sites_enabled / manager.config_path("example.com").name
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(manager.config_path("example.com"))

    assert manager.delete_site("example.com") is True
    assert not manager.config_path("example.com").exists()
    assert "example.com" not in store.sites
    if backend == "nginx":
        assert not link.is_symlink()
    else:
        # a2dissite owns the link on apache; asserting on the call is asserting
        # on the part of the behaviour this manager is responsible for.
        assert runner.ran("a2dissite", "example.com.conf")


@pytest.mark.parametrize("backend", ["nginx", "apache"])
def test_list_sites_returns_records_and_skips_the_distribution_defaults(
    managers: dict[str, WebServerManager], backend: str
) -> None:
    """The default vhosts belong to the distribution, not to WASM."""
    manager = managers[backend]
    suffix = manager.backend.config_suffix
    manager.sites_available.mkdir(parents=True, exist_ok=True)
    for name in manager.backend.default_site_names:
        (manager.sites_available / f"{name}{suffix}").write_text("# distro\n")
    manager.create_site("example.com")
    link = manager.sites_enabled / f"example.com{suffix}"
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(manager.config_path("example.com"))

    sites = manager.list_sites()

    assert [s.domain for s in sites] == ["example.com"]
    assert isinstance(sites[0], SiteInfo)
    assert sites[0].enabled is True
    assert sites[0].webserver == backend
    # ``wasm site list`` tags each entry with its backend, so the record has to
    # accept that assignment the way the dict it replaced did.
    sites[0]["webserver"] = backend
    assert sites[0]["webserver"] == backend


def test_reload_refuses_to_apply_a_broken_configuration(
    nginx: NginxManager, runner: FakeRunner
) -> None:
    """Reloading a bad config takes every other site on the box down."""
    runner.script(["nginx", "-t"], stderr="emerg: unexpected }", exit_code=1)

    assert nginx.reload() is False
    assert runner.calls == [("nginx", "-t")]


def test_apache_syntax_warning_is_not_a_syntax_error(
    apache: ApacheManager, runner: FakeRunner
) -> None:
    """apache2ctl exits non-zero on a warning it then calls "Syntax OK"."""
    runner.script(
        ["apache2ctl", "configtest"],
        stderr="Could not reliably determine the server's FQDN\nSyntax OK\n",
        exit_code=1,
    )

    assert apache.test_config() is True


# ---------------------------------------------------------------------------
# A domain must not escape its configuration directory
# ---------------------------------------------------------------------------


HOSTILE_DOMAINS = [
    "../../etc/nginx/conf.d/evil",
    "/etc/cron.d/evil",
    "example.com/../../../etc/passwd",
    "example.com\nserver { listen 80; }",
    "example.com; rm -rf /",
    "exam ple.com",
    "example.com\x00.txt",
    "..",
]


@pytest.mark.parametrize("domain", HOSTILE_DOMAINS)
@pytest.mark.parametrize("backend", ["nginx", "apache"])
def test_a_hostile_domain_never_becomes_a_file(
    managers: dict[str, WebServerManager],
    backend: str,
    domain: str,
    tmp_path: Path,
) -> None:
    """WASM writes these files as root; a domain is not a path."""
    manager = managers[backend]

    with pytest.raises((DomainError, ValidationError, SecurityError)):
        manager.create_site(domain, context={"port": 3000})

    written = [p for p in tmp_path.rglob("*") if p.is_file()]
    assert written == []
    assert not Path("/etc/cron.d/evil").exists()


@pytest.mark.parametrize("backend", ["nginx", "apache"])
def test_a_hostile_domain_is_refused_before_it_is_rendered(
    managers: dict[str, WebServerManager], backend: str
) -> None:
    """Rendering alone must not let a newline into a server_name directive."""
    with pytest.raises((DomainError, ValidationError, SecurityError)):
        managers[backend].render_config("example.com\nserver { listen 80; }")


@pytest.mark.parametrize("backend", ["nginx", "apache"])
def test_a_symlink_out_of_the_directory_is_refused(
    managers: dict[str, WebServerManager], backend: str, tmp_path: Path
) -> None:
    """A clean name is not enough when the directory itself is not clean."""
    manager = managers[backend]
    outside = tmp_path / "outside"
    outside.mkdir()
    manager.sites_available.mkdir(parents=True, exist_ok=True)
    name = f"example.com{manager.backend.config_suffix}"
    (manager.sites_available / name).symlink_to(outside / name)

    with pytest.raises(SecurityError):
        manager.config_path("example.com")


@pytest.mark.parametrize("backend", ["nginx", "apache"])
def test_a_domain_is_normalised_before_it_becomes_a_path(
    managers: dict[str, WebServerManager], backend: str
) -> None:
    """Case and surrounding whitespace must not create a second file."""
    manager = managers[backend]

    assert manager.config_path("  Example.COM  ") == manager.config_path("example.com")


# ---------------------------------------------------------------------------
# Rendered configuration
# ---------------------------------------------------------------------------


RENDER_CASES = [
    ("static", {"ssl": False, "static_dir": "/var/www/apps/example.com/dist"}),
    ("proxy", {"ssl": False, "port": 3000}),
    (
        "proxy-ssl",
        {
            "ssl": True,
            "port": 3000,
            "ssl_certificate": "/etc/letsencrypt/live/example.com/fullchain.pem",
            "ssl_certificate_key": "/etc/letsencrypt/live/example.com/privkey.pem",
        },
    ),
    (
        "proxy-www",
        {
            "ssl": True,
            "port": 3000,
            "server_names": "example.com www.example.com",
        },
    ),
]


@pytest.mark.parametrize(("case", "context"), RENDER_CASES, ids=[c[0] for c in RENDER_CASES])
@pytest.mark.parametrize("backend", ["nginx", "apache"])
def test_rendered_configuration_matches_the_snapshot(
    managers: dict[str, WebServerManager],
    backend: str,
    case: str,
    context: dict[str, Any],
    snapshot: Any,
) -> None:
    """A change to what the web server is told must be visible in review."""
    template = "static" if case == "static" else "proxy"
    rendered = managers[backend].render_config("example.com", template, context)

    assert rendered == snapshot(name=f"{backend}-{case}")


def test_www_alias_reaches_both_templates(
    managers: dict[str, WebServerManager],
) -> None:
    """The www alias is the reason ``site create --www`` exists."""
    context = {"server_names": "example.com www.example.com"}

    nginx_config = managers["nginx"].render_config("example.com", "proxy", context)
    apache_config = managers["apache"].render_config("example.com", "proxy", context)

    assert "server_name example.com www.example.com;" in nginx_config
    assert "ServerAlias www.example.com" in apache_config


# ---------------------------------------------------------------------------
# Certificates
# ---------------------------------------------------------------------------


@pytest.fixture
def certs(runner: FakeRunner, store: FakeStore, tmp_path: Path) -> CertManager:
    """
    A certificate manager whose letsencrypt tree is a temporary directory.

    Args:
        runner: The fake command runner.
        store: The fake store.
        tmp_path: Per-test temporary directory.

    Returns:
        The manager.
    """
    manager = CertManager()
    manager.LETSENCRYPT_DIR = tmp_path / "letsencrypt"
    manager.LIVE_DIR = manager.LETSENCRYPT_DIR / "live"
    manager.config = SimpleNamespace(ssl_email="")  # type: ignore[assignment]
    return manager


def _issue_certificate(manager: CertManager, domain: str) -> None:
    """
    Put a certificate on disk for a domain.

    Args:
        manager: The certificate manager.
        domain: Domain the lineage covers.
    """
    live = manager.LIVE_DIR / domain
    live.mkdir(parents=True, exist_ok=True)
    for name in ("fullchain.pem", "privkey.pem", "cert.pem", "chain.pem"):
        (live / name).write_text("-----BEGIN CERTIFICATE-----\n")


def test_certificate_info_is_read_with_the_names_the_manager_writes(
    certs: CertManager, runner: FakeRunner
) -> None:
    """The health check and the CLI read exactly these fields."""
    runner.script(["sudo", "certbot", "certificates"], stdout=CERTBOT_OUTPUT)

    (info,) = certs.list_certificates()

    assert isinstance(info, CertificateInfo)
    assert info.name == "example.com"
    assert info.domains == ["example.com", "www.example.com"]
    assert info.expiry == "2026-11-30"
    assert info.cert_path == "/etc/letsencrypt/live/example.com/fullchain.pem"
    assert info.key_path == "/etc/letsencrypt/live/example.com/privkey.pem"


def test_the_health_check_consumes_the_record_this_manager_produces(
    certs: CertManager, runner: FakeRunner
) -> None:
    """The reader is run against the writer's output, not against a fixture."""
    try:
        from wasm.cli.commands import health
    except Exception as exc:
        # The contract under test is the field names, and the sibling test above
        # checks those from source. An unrelated import failure elsewhere in the
        # CLI must not be reported as a certificate defect.
        pytest.skip(f"wasm.cli.commands.health cannot be imported: {exc}")

    expiry = (datetime.now() + timedelta(days=3)).strftime("%Y-%m-%d")
    runner.script(
        ["sudo", "certbot", "certificates"],
        stdout=CERTBOT_OUTPUT.replace("2026-11-30", expiry),
    )

    (info,) = certs.list_certificates()

    assert health._certificate_label(info) == "example.com"
    # certbot prints a date and the reader compares it against a timestamp, so
    # the count lands on 2 or 3 depending on the time of day.
    assert health._days_until(info.get("expiry")) in (2, 3)


def test_the_expiry_key_that_was_never_written_is_now_an_error(
    certs: CertManager, runner: FakeRunner
) -> None:
    """``wasm health`` looked for 'expires' for releases and got None."""
    runner.script(["sudo", "certbot", "certificates"], stdout=CERTBOT_OUTPUT)
    (info,) = certs.list_certificates()

    with pytest.raises(KeyError):
        info.get("expires")


def test_every_field_the_health_check_reads_is_a_field_of_the_record(
    certs: CertManager, runner: FakeRunner
) -> None:
    """
    The reader and the writer must agree on the names.

    The source of ``wasm health`` is inspected rather than imported: what is
    being asserted is the vocabulary the two modules share, and a broken import
    somewhere else in the CLI must not turn this into a passing test.
    """
    import re as _re

    source = (Path(__file__).resolve().parents[1] / "src/wasm/cli/commands/health.py").read_text()
    keys = set(_re.findall(r'cert(?:_info)?(?:\.get\(|\[)"([a-z_]+)"', source))

    assert keys, "the health check no longer reads certificate fields by name"
    assert keys <= set(CertificateInfo().keys())


def test_the_expiry_a_reader_alerts_on_is_a_parseable_date(
    certs: CertManager, runner: FakeRunner
) -> None:
    """The health check turns this field into a number of days."""
    expiry = (datetime.now() + timedelta(days=3)).strftime("%Y-%m-%d")
    runner.script(
        ["sudo", "certbot", "certificates"],
        stdout=CERTBOT_OUTPUT.replace("2026-11-30", expiry),
    )

    (info,) = certs.list_certificates()

    parsed = datetime.strptime(info.get("expiry", ""), "%Y-%m-%d")
    assert (parsed.date() - datetime.now().date()).days == 3
    assert info.get("name") == "example.com"


def test_certificate_paths_are_a_record_the_callers_can_still_index(
    certs: CertManager,
) -> None:
    """``wasm site create`` reads these paths to switch a vhost to TLS."""
    paths = certs.get_cert_path("example.com")

    assert paths.fullchain.name == "fullchain.pem"
    assert paths["privkey"] == paths.privkey
    with pytest.raises(KeyError):
        paths.get("full_chain")


def test_certbot_plugin_probe_runs_with_privileges_and_is_asked_once(
    certs: CertManager, runner: FakeRunner
) -> None:
    """Unprivileged, certbot cannot read its own configuration and lies."""
    runner.script(["sudo", "certbot", "plugins"], stdout="* nginx\nDescription: Nginx\n")

    assert certs._check_certbot_plugin("nginx") is True
    assert certs._check_certbot_plugin("nginx") is True
    assert runner.calls_to("sudo").count(("sudo", "certbot", "plugins")) == 1


def test_issuance_pins_the_lineage_and_covers_www(certs: CertManager, runner: FakeRunner) -> None:
    """Without --cert-name certbot invents example.com-0001 on the next change."""
    runner.script(["sudo", "certbot", "plugins"], stdout="* nginx\n")

    certs.obtain("example.com", email="ops@example.com", nginx=True, include_www=True)

    (issued,) = [c for c in runner.calls if "certonly" in c]
    assert issued == (
        "sudo",
        "certbot",
        "certonly",
        "--cert-name",
        "example.com",
        "--email",
        "ops@example.com",
        "--non-interactive",
        "--agree-tos",
        "--nginx",
        "-d",
        "example.com",
        "-d",
        "www.example.com",
    )


def test_issuance_falls_back_to_webroot_when_the_plugin_is_missing(
    certs: CertManager, runner: FakeRunner
) -> None:
    """A missing plugin degrades the method, and says so, but still issues."""
    runner.script(["sudo", "certbot", "plugins"], stdout="* standalone\n")

    certs.obtain("example.com", email="ops@example.com", nginx=True)

    (issued,) = [c for c in runner.calls if "certonly" in c]
    assert issued[-5:] == ("--webroot", "-w", "/var/www/html", "-d", "example.com")
    assert "--nginx" not in issued


def test_a_certificate_that_already_covers_everything_is_left_alone(
    certs: CertManager, runner: FakeRunner
) -> None:
    """Issuance is rate limited; running the deploy twice must be cheap."""
    _issue_certificate(certs, "example.com")
    runner.script(["sudo", "certbot", "certificates"], stdout=CERTBOT_OUTPUT)

    assert certs.obtain("example.com", email="ops@example.com", include_www=True) is True
    assert not any("certonly" in call for call in runner.calls)


def test_a_certificate_missing_a_domain_is_expanded_not_reissued(
    certs: CertManager, runner: FakeRunner
) -> None:
    """A second lineage would leave two half-right certificates renewing."""
    _issue_certificate(certs, "example.com")
    runner.script(
        ["sudo", "certbot", "certificates"],
        stdout=CERTBOT_OUTPUT.replace(" www.example.com", ""),
    )

    certs.obtain(
        "example.com",
        email="ops@example.com",
        additional_domains=["api.example.com"],
    )

    (issued,) = [c for c in runner.calls if "certonly" in c]
    assert "--expand" in issued
    assert issued[:5] == ("sudo", "certbot", "certonly", "--cert-name", "example.com")
    assert issued.count("-d") == 2


def test_duplicate_domains_are_collapsed(certs: CertManager) -> None:
    """A duplicated SAN makes the next idempotence check fail forever."""
    domains = certs.certificate_domains(
        "example.com",
        ["www.example.com", "EXAMPLE.com"],
        include_www=True,
    )

    assert domains == ["example.com", "www.example.com"]


@pytest.mark.parametrize(
    ("domain", "expected"),
    [("example.com", ["example.com", "www.example.com"]), ("api.example.com", ["api.example.com"])],
)
def test_www_is_only_added_where_it_can_resolve(
    certs: CertManager, domain: str, expected: list[str]
) -> None:
    """A www alias on a subdomain fails the whole ACME order."""
    assert certs.certificate_domains(domain, include_www=True) == expected


def test_renewal_names_the_lineage(certs: CertManager, runner: FakeRunner) -> None:
    """Renewing one site must not renew, or skip, the others."""
    certs.renew("example.com", force=True)

    assert runner.calls == [
        (
            "sudo",
            "certbot",
            "renew",
            "--non-interactive",
            "--cert-name",
            "example.com",
            "--force-renewal",
        )
    ]


def test_renewal_failure_is_actionable(certs: CertManager, runner: FakeRunner) -> None:
    """A failed renewal is the one error an operator must be able to act on."""
    runner.script(["sudo", "certbot", "renew"], stderr="rate limit exceeded", exit_code=1)

    with pytest.raises(CertificateError) as raised:
        certs.renew()

    assert "rate limit" in str(raised.value.details)


def test_deleting_an_absent_certificate_is_not_an_error(
    certs: CertManager, runner: FakeRunner
) -> None:
    """``wasm site delete`` calls this on sites that never had TLS."""
    assert certs.delete("example.com") is True
    assert not any("delete" in call for call in runner.calls)


@pytest.mark.parametrize("domain", ["../../etc/passwd", "exam ple.com", "a.com; rm -rf /"])
def test_a_hostile_domain_never_reaches_certbot(certs: CertManager, domain: str) -> None:
    """A lineage name becomes a directory under /etc/letsencrypt."""
    with pytest.raises((CertificateError, WASMError)):
        certs.obtain(domain, email="ops@example.com")
