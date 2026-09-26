# Copyright (c) 2024-2026 Yago Lopez Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
Tests for :mod:`wasm.deployers.domains`: aliases and redirects per application.

Every test here runs the real store, the real nginx manager writing into a
temporary tree, the real certificate manager and the real deployers; only the
processes are faked. What is pinned:

- A change reaches the site the way a deploy renders it, in place and on
  releases, and the certificate covers every name, redirects included.
- A change the web server refuses leaves nothing behind: not the row, not the
  file, and no reload.
- Removing a name never revokes anything.
- A 1.x application that served ``www`` without a row keeps serving it.
- ``wasm create --www`` records ``www`` as a redirect.
- The DNS check compares what a name resolves to with this machine.
"""

from __future__ import annotations

import re
import socket
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from wasm.core.exceptions import (
    DependencyError,
    DeploymentError,
    DomainConflictError,
    DomainError,
    ValidationError,
    WASMError,
)
from wasm.core.runner import FakeRunner
from wasm.core.store import App, WASMStore
from wasm.deployers import domains
from wasm.deployers.base import BaseDeployer
from wasm.deployers.registry import get_deployer
from wasm.deployers.static import StaticDeployer
from wasm.managers.cert_manager import CertManager
from wasm.managers.nginx_manager import NginxManager
from wasm.managers.webserver import NGINX_BACKEND

RELEASE_ID = "20260925-120000-abcdef0"

NGINX_REJECTS = "nginx: [emerg] duplicate listen options for 0.0.0.0:443 in /etc/nginx/x:12\n"


@pytest.fixture
def store(tmp_path: Path) -> Any:
    """A store in the test's directory, installed as the process-wide singleton."""
    WASMStore.reset_instance()
    instance = WASMStore(tmp_path / "wasm.db")
    yield instance
    WASMStore.reset_instance()


@pytest.fixture
def web(tmp_path: Path, runner: FakeRunner, store: WASMStore) -> NginxManager:
    """An nginx manager writing into a temporary configuration tree."""
    return NginxManager(
        backend=replace(
            NGINX_BACKEND,
            sites_available=tmp_path / "nginx/sites-available",
            sites_enabled=tmp_path / "nginx/sites-enabled",
        )
    )


@pytest.fixture
def certs(tmp_path: Path, runner: FakeRunner, store: WASMStore) -> CertManager:
    """A certificate manager whose letsencrypt tree is temporary."""
    manager = CertManager()
    manager.LETSENCRYPT_DIR = tmp_path / "letsencrypt"
    manager.LIVE_DIR = manager.LETSENCRYPT_DIR / "live"
    manager.config = SimpleNamespace(ssl_email="")  # type: ignore[assignment]
    return manager


def put_certificate(certs: CertManager, domain: str) -> None:
    """Create the files of a live lineage."""
    live = certs.LIVE_DIR / domain
    live.mkdir(parents=True, exist_ok=True)
    for name in ("fullchain.pem", "privkey.pem", "cert.pem", "chain.pem"):
        (live / name).write_text("-----BEGIN CERTIFICATE-----\n")


class Machine:
    """Deploys applications into the temporary tree, and wires their deployers."""

    def __init__(
        self,
        tmp_path: Path,
        store: WASMStore,
        web: NginxManager,
        certs: CertManager,
        runner: FakeRunner,
    ) -> None:
        self.tmp_path = tmp_path
        self.store = store
        self.web = web
        self.certs = certs
        self.runner = runner

    def wire(self, deployer: BaseDeployer) -> BaseDeployer:
        """Point a deployer at this machine's web server and certificates."""
        deployer._webserver_manager = lambda: self.web  # type: ignore[method-assign]
        deployer.cert_manager = self.certs
        return deployer

    def deployer(self, app_type: str, verbose: bool = False) -> Any:
        """Stand-in for the registry's get_deployer, wiring what it returns."""
        deployer = get_deployer(app_type, verbose=verbose)
        if isinstance(deployer, BaseDeployer):
            self.wire(deployer)
        return deployer

    def deploy(
        self,
        domain: str = "example.com",
        *,
        app_type: str = "static",
        layout: str = "inplace",
        tls: bool = False,
    ) -> App:
        """
        Put an application on disk and in the store, with the site a deploy writes.

        Returns:
            Its row.
        """
        root = self.tmp_path / "apps" / domain
        tree = root / "releases" / RELEASE_ID if layout == "releases" else root
        if app_type == "static":
            (tree / "public").mkdir(parents=True)
            (tree / "public/index.html").write_text("<h1>hi</h1>")
        else:
            tree.mkdir(parents=True)
            (tree / "package.json").write_text('{"name": "app"}')
        if layout == "releases":
            (root / "current").symlink_to(Path("releases") / RELEASE_ID)
        app = self.store.create_app(
            App(
                domain=domain,
                app_type=app_type,
                source=str(root),
                app_path=str(root),
                port=None if app_type == "static" else 3000,
                layout=layout,
                ssl_enabled=tls,
            )
        )
        if tls:
            put_certificate(self.certs, domain)
        deployer = self.deployer(app_type)
        deployer.configure(domain, str(root), port=app.port, app_path=root)
        deployer.refresh_site(with_ssl=tls)
        self.runner.calls.clear()
        return app

    def config(self, domain: str = "example.com") -> str:
        """Read an application's site configuration."""
        return self.web.config_path(domain).read_text()

    def domains(self, domain: str = "example.com") -> list[tuple[str, str]]:
        """List an application's domains as (name, kind)."""
        return [(record.domain, record.kind) for record in self.store.list_domains(domain)]

    def reloaded(self) -> bool:
        """Whether nginx was reloaded since the last deploy."""
        return ("systemctl", "reload", "nginx") in self.runner.calls

    def certbot_orders(self) -> list[tuple[str, ...]]:
        """The certbot issuance commands run since the last deploy."""
        return [call for call in self.runner.calls if call[:2] == ("certbot", "certonly")]


@pytest.fixture
def machine(
    tmp_path: Path,
    store: WASMStore,
    web: NginxManager,
    certs: CertManager,
    runner: FakeRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> Machine:
    """A machine whose deployers the domains module builds through the registry."""
    fake = Machine(tmp_path, store, web, certs, runner)
    monkeypatch.setattr(domains, "get_deployer", fake.deployer)
    return fake


def requested_names(order: tuple[str, ...]) -> list[str]:
    """The ``-d`` arguments of a certbot command."""
    return [order[index + 1] for index, arg in enumerate(order) if arg == "-d"]


# ---------------------------------------------------------------------------
# Adding
# ---------------------------------------------------------------------------


def test_an_alias_is_served_like_the_primary_in_place(machine: Machine) -> None:
    app = machine.deploy()

    change = domains.add_domain("example.com", "shop.example.com", "alias")

    assert machine.domains() == [("example.com", "primary"), ("shop.example.com", "alias")]
    config = machine.config()
    assert "server_name example.com shop.example.com;" in config
    # Rendered exactly as the deploy did: the static directory it detected.
    assert f"root {app.app_path}/public;" in config
    assert machine.reloaded()
    assert change.tls is False
    assert change.certificate_issued is False
    assert machine.certbot_orders() == []


def test_a_redirect_on_a_release_app_points_at_current(machine: Machine) -> None:
    """The site of a release app serves current/, whichever release is active."""
    app = machine.deploy(layout="releases")

    domains.add_domain("example.com", "old.example.org", "redirect")

    config = machine.config()
    assert f"root {app.app_path}/current/public;" in config
    assert "server_name old.example.org;" in config
    assert "return 301 http://example.com$request_uri;" in config
    assert "server_name example.com;" in config


def test_a_proxied_app_keeps_its_port(machine: Machine) -> None:
    machine.deploy(app_type="nodejs")

    domains.add_domain("example.com", "shop.example.com")

    config = machine.config()
    assert "proxy_pass http://127.0.0.1:3000;" in config
    assert "server_name example.com shop.example.com;" in config


def test_a_tls_site_expands_its_certificate_to_every_domain(machine: Machine) -> None:
    """--expand under the same lineage, with the redirect in it: it is served on 443 too."""
    machine.deploy(tls=True)
    domains.add_domain("example.com", "www.example.com", "redirect", issue_cert=False)
    machine.runner.calls.clear()

    change = domains.add_domain("example.com", "shop.example.com", "alias")

    (order,) = machine.certbot_orders()
    assert order[:4] == ("certbot", "certonly", "--cert-name", "example.com")
    assert "--expand" in order
    assert requested_names(order) == ["example.com", "shop.example.com", "www.example.com"]
    assert change.tls is True
    assert change.certificate_issued is True
    config = machine.config()
    assert "listen 443 ssl http2;" in config
    assert "return 301 https://example.com$request_uri;" in config


def test_a_failed_order_keeps_the_domain_and_reports_certbot_verbatim(
    machine: Machine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    The certbot failure text happens to say "Challenge failed", which is also
    what triggers the DNS diagnosis in ``CertManager`` - so DNS is pinned here
    as pointing here, to keep this test about the one thing it is for:
    certbot's own words surviving unparaphrased.
    """
    machine.deploy(tls=True)
    machine.runner.script(
        ["certbot", "certonly"],
        stderr="Challenge failed for domain shop.example.com\nType: dns\n",
        exit_code=1,
    )
    monkeypatch.setattr(domains, "check_dns", lambda *a, **kw: SimpleNamespace(points_here=True))

    change = domains.add_domain("example.com", "shop.example.com")

    assert change.certificate_issued is False
    assert change.certificate_error == "Challenge failed for domain shop.example.com\nType: dns"
    assert ("shop.example.com", "alias") in machine.domains()
    # The certificate it had keeps serving the names it covers.
    assert "listen 443 ssl http2;" in machine.config()


def test_adding_a_name_again_retries_the_certificate(machine: Machine) -> None:
    """Once DNS points here, the same command that failed is the one to run again."""
    machine.deploy(tls=True)
    domains.add_domain("example.com", "shop.example.com", issue_cert=False)

    change = domains.add_domain("example.com", "shop.example.com")

    assert machine.domains() == [("example.com", "primary"), ("shop.example.com", "alias")]
    (order,) = machine.certbot_orders()
    assert requested_names(order) == ["example.com", "shop.example.com"]
    assert change.certificate_issued is True


def test_a_failed_retry_keeps_the_name_it_already_had(machine: Machine) -> None:
    machine.deploy()
    domains.add_domain("example.com", "shop.example.com")
    machine.runner.script(["nginx", "-t"], stderr=NGINX_REJECTS, exit_code=1)

    with pytest.raises(ValidationError):
        domains.add_domain("example.com", "shop.example.com")

    assert ("shop.example.com", "alias") in machine.domains()


def test_adding_a_name_again_in_another_role_is_refused(machine: Machine) -> None:
    machine.deploy()
    domains.add_domain("example.com", "shop.example.com")

    with pytest.raises(DomainConflictError, match=re.escape("already an alias of example.com")):
        domains.add_domain("example.com", "shop.example.com", "redirect")


def test_the_certificate_can_be_left_for_later(machine: Machine) -> None:
    machine.deploy(tls=True)

    change = domains.add_domain("example.com", "shop.example.com", issue_cert=False)

    assert machine.certbot_orders() == []
    assert change.certificate_issued is False
    assert change.tls is True


def test_issuing_later_covers_every_domain(machine: Machine) -> None:
    machine.deploy(tls=True)
    domains.add_domain("example.com", "shop.example.com", issue_cert=False)

    change = domains.issue_certificate("example.com")

    (order,) = machine.certbot_orders()
    assert requested_names(order) == ["example.com", "shop.example.com"]
    assert change.certificate_issued is True


def test_issuing_for_a_site_without_tls_is_refused(machine: Machine) -> None:
    machine.deploy()

    with pytest.raises(DeploymentError, match="not served over TLS"):
        domains.issue_certificate("example.com")

    assert machine.certbot_orders() == []


def test_a_configuration_nginx_refuses_leaves_nothing_behind(machine: Machine) -> None:
    """Not the row, not the file, and no reload of a configuration that fails its test."""
    machine.deploy()
    before = machine.config()
    machine.runner.script(["nginx", "-t"], stderr=NGINX_REJECTS, exit_code=1)

    with pytest.raises(ValidationError) as refused:
        domains.add_domain("example.com", "shop.example.com")

    assert refused.value.details == NGINX_REJECTS
    assert machine.config() == before
    assert machine.domains() == [("example.com", "primary")]
    assert not machine.reloaded()


def test_a_name_with_a_site_of_its_own_is_refused(machine: Machine) -> None:
    machine.deploy()
    machine.web.create_site("shop.example.com", "proxy", {"port": 4000})

    with pytest.raises(DomainConflictError, match="site of its own"):
        domains.add_domain("example.com", "shop.example.com")

    assert machine.domains() == [("example.com", "primary")]


def test_another_applications_name_is_refused(machine: Machine) -> None:
    machine.deploy("example.com")
    machine.deploy("other.com")
    domains.add_domain("other.com", "shop.example.com")

    with pytest.raises(DomainConflictError, match=re.escape("other.com")):
        domains.add_domain("example.com", "shop.example.com")


def test_an_unknown_application_is_refused(machine: Machine) -> None:
    with pytest.raises(WASMError, match="Application not found"):
        domains.add_domain("ghost.example.com", "shop.example.com")


@pytest.mark.parametrize("app_type", ["monorepo", "docker-compose"])
def test_types_that_write_their_own_configuration_are_refused(
    machine: Machine, store: WASMStore, app_type: str
) -> None:
    store.create_app(App(domain="example.com", app_type=app_type, app_path="/srv/x"))

    with pytest.raises(ValidationError, match="do not support aliases"):
        domains.add_domain("example.com", "shop.example.com")

    assert [record.domain for record in store.list_domains("example.com")] == ["example.com"]


# ---------------------------------------------------------------------------
# Removing
# ---------------------------------------------------------------------------


def test_removing_an_alias_stops_serving_it_and_revokes_nothing(machine: Machine) -> None:
    machine.deploy(tls=True)
    domains.add_domain("example.com", "shop.example.com")
    machine.runner.calls.clear()

    change = domains.remove_domain("example.com", "shop.example.com")

    assert machine.domains() == [("example.com", "primary")]
    assert "shop.example.com" not in machine.config()
    assert machine.reloaded()
    assert [call for call in machine.runner.calls if call and call[0] == "certbot"] == []
    assert (machine.certs.LIVE_DIR / "example.com/fullchain.pem").exists()
    assert change.tls is True


def test_the_primary_cannot_be_removed(machine: Machine) -> None:
    machine.deploy()
    before = machine.config()

    with pytest.raises(DomainError, match="primary"):
        domains.remove_domain("example.com", "example.com")

    assert machine.config() == before
    assert not machine.reloaded()


def test_removing_a_name_the_application_does_not_have_is_an_error(machine: Machine) -> None:
    machine.deploy()

    with pytest.raises(DomainError, match=re.escape("not a domain of example.com")):
        domains.remove_domain("example.com", "shop.example.com")


def test_a_refused_removal_puts_the_row_back(machine: Machine) -> None:
    machine.deploy()
    domains.add_domain("example.com", "old.example.org", "redirect")
    before = machine.config()
    machine.runner.script(["nginx", "-t"], stderr=NGINX_REJECTS, exit_code=1)

    with pytest.raises(ValidationError):
        domains.remove_domain("example.com", "old.example.org")

    assert ("old.example.org", "redirect") in machine.domains()
    assert machine.config() == before


# ---------------------------------------------------------------------------
# Applications deployed before domains had rows
# ---------------------------------------------------------------------------


def serve_like_1x(machine: Machine, app: App) -> None:
    """Rewrite the site as a 1.x ``--www`` deploy left it: www served, no row for it."""
    text = machine.web.render_config(
        app.domain,
        "static",
        {"server_names": f"{app.domain} www.{app.domain}", "static_dir": f"{app.app_path}/public"},
    )
    machine.web.config_path(app.domain).write_text(text)


def test_the_www_a_1x_deploy_served_is_kept_when_a_name_is_added(machine: Machine) -> None:
    app = machine.deploy()
    serve_like_1x(machine, app)

    change = domains.add_domain("example.com", "shop.example.com")

    assert change.adopted == ("www.example.com",)
    assert machine.domains() == [
        ("example.com", "primary"),
        ("shop.example.com", "alias"),
        ("www.example.com", "alias"),
    ]
    assert "server_name example.com shop.example.com www.example.com;" in machine.config()


def test_the_www_a_1x_deploy_served_can_be_made_a_redirect(machine: Machine) -> None:
    """Asking for www as a redirect is not refused because it was already served."""
    app = machine.deploy()
    serve_like_1x(machine, app)

    change = domains.add_domain("example.com", "www.example.com", "redirect")

    assert change.adopted == ()
    assert machine.domains() == [("example.com", "primary"), ("www.example.com", "redirect")]
    assert "server_name www.example.com;" in machine.config()


def test_the_www_a_1x_deploy_served_can_be_removed(machine: Machine) -> None:
    app = machine.deploy()
    serve_like_1x(machine, app)

    domains.remove_domain("example.com", "www.example.com")

    assert machine.domains() == [("example.com", "primary")]
    assert "www.example.com" not in machine.config()


def test_listing_reads_the_store(machine: Machine) -> None:
    machine.deploy()
    domains.add_domain("example.com", "shop.example.com")

    assert [record.domain for record in domains.list_domains("example.com")] == [
        "example.com",
        "shop.example.com",
    ]
    with pytest.raises(WASMError, match="Application not found"):
        domains.list_domains("ghost.example.com")


# ---------------------------------------------------------------------------
# The deployer: --www and the certificate step
# ---------------------------------------------------------------------------


def configured_static(machine: Machine, *, include_www: bool, ssl: bool = False) -> Any:
    """A static deployer for an application that is already registered."""
    root = machine.tmp_path / "apps/example.com"
    (root / "public").mkdir(parents=True, exist_ok=True)
    (root / "public/index.html").write_text("hi")
    if machine.store.get_app("example.com") is None:
        machine.store.create_app(App(domain="example.com", app_type="static", app_path=str(root)))
    deployer = machine.wire(StaticDeployer(runner=machine.runner))
    deployer.configure("example.com", str(root), app_path=root, include_www=include_www, ssl=ssl)
    deployer.pre_install()
    return deployer


def test_create_with_www_records_www_as_a_redirect(machine: Machine) -> None:
    deployer = configured_static(machine, include_www=True)

    deployer.create_site(with_ssl=False)

    assert machine.domains() == [("example.com", "primary"), ("www.example.com", "redirect")]
    config = machine.config()
    assert "server_name example.com;" in config
    assert "server_name www.example.com;" in config
    assert "return 301 http://example.com$request_uri;" in config


def test_create_with_www_keeps_an_alias_the_operator_chose(machine: Machine) -> None:
    deployer = configured_static(machine, include_www=True)
    machine.store.add_domain("example.com", "www.example.com", "alias")

    deployer.create_site(with_ssl=False)

    assert machine.domains() == [("example.com", "primary"), ("www.example.com", "alias")]


def test_a_redeploy_without_www_keeps_the_domains_the_store_has(machine: Machine) -> None:
    deployer = configured_static(machine, include_www=False)
    machine.store.add_domain("example.com", "shop.example.com", "alias")

    deployer.create_site(with_ssl=False)

    assert "server_name example.com shop.example.com;" in machine.config()


def test_the_certificate_step_asks_for_every_domain(machine: Machine) -> None:
    deployer = configured_static(machine, include_www=True, ssl=True)
    machine.store.add_domain("example.com", "shop.example.com", "alias")
    deployer.create_site(with_ssl=False)

    deployer.obtain_certificate()

    (order,) = machine.certbot_orders()
    assert requested_names(order) == ["example.com", "shop.example.com", "www.example.com"]


def test_a_certificate_that_cannot_be_extended_keeps_tls(machine: Machine) -> None:
    """One alias whose DNS is not ready must not take TLS off the names that work."""
    deployer = configured_static(machine, include_www=False, ssl=True)
    machine.store.add_domain("example.com", "shop.example.com", "alias")
    put_certificate(machine.certs, "example.com")
    machine.runner.script(["certbot", "certonly"], stderr="Challenge failed", exit_code=1)
    deployer.create_site(with_ssl=False)

    deployer._step_certificate()

    assert deployer._ssl_obtained is True
    assert "listen 443 ssl http2;" in machine.config()


def test_without_a_certificate_a_failed_order_still_serves_http(machine: Machine) -> None:
    deployer = configured_static(machine, include_www=False, ssl=True)
    machine.runner.script(["certbot", "certonly"], stderr="Challenge failed", exit_code=1)
    deployer.create_site(with_ssl=False)

    deployer._step_certificate()

    assert deployer._ssl_obtained is False
    assert "listen 443" not in machine.config()


def test_a_rehearsal_records_no_www_and_does_not_fail(machine: Machine) -> None:
    """Under --dry-run the app row was rolled back; there is nothing to attach www to."""
    root = machine.tmp_path / "apps/example.com"
    (root / "public").mkdir(parents=True)
    deployer = machine.wire(StaticDeployer(runner=machine.runner))
    deployer.configure("example.com", str(root), app_path=root, include_www=True)
    deployer.pre_install()

    deployer.create_site(with_ssl=False)

    assert machine.store.list_domains("example.com") == []


# ---------------------------------------------------------------------------
# DNS
# ---------------------------------------------------------------------------


def resolver(*addresses: str) -> Any:
    """A getaddrinfo stand-in answering with the given addresses."""

    def resolve(host: str, port: Any, *args: Any, **kwargs: Any) -> list[tuple[Any, ...]]:
        return [
            (
                socket.AF_INET6 if ":" in address else socket.AF_INET,
                socket.SOCK_STREAM,
                6,
                "",
                (address, 0),
            )
            for address in addresses
        ]

    return resolve


def nxdomain(host: str, port: Any, *args: Any, **kwargs: Any) -> Any:
    """A getaddrinfo stand-in for a name that does not exist."""
    raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")


HERE = ("203.0.113.5", "2001:db8::5")


def test_a_name_that_resolves_here_points_here() -> None:
    check = domains.check_dns(
        "Shop.Example.com",
        resolver=resolver("203.0.113.5", "203.0.113.5"),
        local_addresses=lambda: HERE,
    )

    assert check.domain == "shop.example.com"
    assert check.resolved_addresses == ("203.0.113.5",)
    assert check.expected_addresses == HERE
    assert check.points_here is True


def test_an_aaaa_record_pointing_elsewhere_does_not_point_here() -> None:
    """Let's Encrypt prefers IPv6; a wrong AAAA fails the order even with a right A."""
    check = domains.check_dns(
        "shop.example.com",
        resolver=resolver("203.0.113.5", "2001:db8::99"),
        local_addresses=lambda: HERE,
    )

    assert check.resolved_addresses == ("203.0.113.5", "2001:db8::99")
    assert check.points_here is False


def test_a_name_that_does_not_resolve_points_nowhere() -> None:
    check = domains.check_dns("shop.example.com", resolver=nxdomain, local_addresses=lambda: HERE)

    assert check.resolved_addresses == ()
    assert check.points_here is False


def test_a_name_that_is_not_a_domain_is_refused_before_it_is_resolved() -> None:
    with pytest.raises(DomainError):
        domains.check_dns("bad domain", resolver=nxdomain, local_addresses=lambda: HERE)


def test_the_machines_addresses_leave_out_what_no_dns_record_should_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def entry(family: int, address: str) -> SimpleNamespace:
        return SimpleNamespace(family=family, address=address)

    fake_psutil = SimpleNamespace(
        net_if_addrs=lambda: {
            "lo": [entry(socket.AF_INET, "127.0.0.1"), entry(socket.AF_INET6, "::1")],
            "eth0": [
                entry(socket.AF_INET, "203.0.113.5"),
                entry(socket.AF_INET6, "fe80::1%eth0"),
                entry(socket.AF_INET6, "2001:db8::5"),
                entry(socket.AF_PACKET, "aa:bb:cc:dd:ee:ff"),
            ],
            "docker0": [entry(socket.AF_INET, "172.17.0.1")],
        }
    )
    monkeypatch.setitem(sys.modules, "psutil", fake_psutil)

    assert domains.machine_addresses() == ("172.17.0.1", "203.0.113.5", "2001:db8::5")


def test_without_psutil_the_machines_addresses_are_an_actionable_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "psutil", None)

    with pytest.raises(DependencyError, match="psutil"):
        domains.machine_addresses()
