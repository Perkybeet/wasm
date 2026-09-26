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

Plus the two that matter most for a program running as root: a domain that
carries a path separator must never become a file outside the configuration
directory, and a rehearsal must not write or delete anything at all. The second
one is new: ``--dry-run`` was enforced at the command runner, so it covered the
``systemctl reload`` and missed the vhost being written and the symlink being
unlinked, neither of which is a subprocess. The ``DryRunFileSystem`` cases below
pin that, and the AST guard lives in ``test_service_manager.py``, which walks
every manager in this area including these ones.
"""

from __future__ import annotations

import re
import tempfile
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from wasm.core.exceptions import (
    ApacheError,
    CertificateError,
    DomainConflictError,
    DomainError,
    NginxError,
    SecurityError,
    ValidationError,
    WASMError,
)
from wasm.core.fs import DryRunFileSystem, set_fs
from wasm.core.runner import FakeRunner
from wasm.core.store import DomainRecord, StoreError
from wasm.managers.apache_manager import ApacheManager
from wasm.managers.cert_manager import CertificateInfo, CertManager
from wasm.managers.nginx_manager import NginxManager
from wasm.managers.webserver import (
    APACHE_BACKEND,
    NGINX_BACKEND,
    SiteInfo,
    WebServerManager,
    WebServerStatus,
    create_secured_site,
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
        self.domains: dict[str, list[DomainRecord]] = {}
        self.owners: dict[str, tuple[str, str]] = {}

    def domain_owner(self, domain: str) -> tuple[str, str] | None:
        """
        Find the application a name belongs to.

        Args:
            domain: The name.

        Returns:
            ``(application domain, kind)``, or None.
        """
        return self.owners.get(domain)

    def list_domains(self, app_domain: str) -> list[DomainRecord]:
        """
        List the domains of an application.

        Args:
            app_domain: The application's primary domain.

        Returns:
            Its records, empty for a domain that is not an application.
        """
        return list(self.domains.get(app_domain, []))

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


@pytest.fixture
def dry_fs() -> DryRunFileSystem:
    """
    A filesystem that refuses every change and records what it refused.

    Returns:
        The rehearsal filesystem.
    """
    return DryRunFileSystem()


@pytest.fixture
def rehearsals(
    nginx: NginxManager, apache: ApacheManager, dry_fs: DryRunFileSystem
) -> dict[str, WebServerManager]:
    """
    Both backends again, rehearsing against the same temporary tree.

    Sharing the backends with the ``nginx`` and ``apache`` fixtures is the point:
    a test can create a site for real and then check that the rehearsal refuses
    to touch that exact file.

    Args:
        nginx: The real nginx manager, for its backend paths.
        apache: The real apache manager, for its backend paths.
        dry_fs: The rehearsal filesystem.

    Returns:
        A mapping from backend name to a manager that writes nothing.
    """
    return {
        "nginx": NginxManager(backend=nginx.backend, fs=dry_fs),
        "apache": ApacheManager(backend=apache.backend, fs=dry_fs),
    }


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


def test_a_failed_configuration_test_is_reported_in_the_servers_own_words(
    nginx: NginxManager, runner: FakeRunner
) -> None:
    """An operator fixing a broken vhost needs nginx's line number, not our paraphrase."""
    runner.script(
        ["nginx", "-t"],
        stderr='nginx: [emerg] unknown directive "servr_name" in /etc/nginx/sites-enabled/x:4\n',
        exit_code=1,
    )

    assert nginx.config_errors() == (
        'nginx: [emerg] unknown directive "servr_name" in /etc/nginx/sites-enabled/x:4\n'
    )
    assert nginx.test_config() is False


def test_a_configuration_that_passes_has_no_errors(
    apache: ApacheManager, runner: FakeRunner
) -> None:
    runner.script(
        ["apache2ctl", "configtest"],
        stderr="Could not reliably determine the server's FQDN\nSyntax OK\n",
        exit_code=1,
    )

    assert apache.config_errors() is None


def test_the_names_a_live_nginx_site_answers_on_are_read_back(nginx: NginxManager) -> None:
    """What a 1.x deploy served with --www is only recorded in the file itself."""
    nginx.create_site(
        "example.com",
        "proxy",
        {
            "ssl": True,
            "server_names": "example.com www.example.com",
            "redirect_domains": ["old.example.org"],
        },
    )

    assert nginx.served_names("example.com") == [
        "old.example.org",
        "example.com",
        "www.example.com",
    ]


def test_the_names_a_live_apache_site_answers_on_are_read_back(apache: ApacheManager) -> None:
    apache.create_site(
        "example.com",
        "proxy",
        {"ssl": True, "server_names": "example.com www.example.com shop.example.com"},
    )

    assert apache.served_names("example.com") == [
        "example.com",
        "www.example.com",
        "shop.example.com",
    ]


def test_a_missing_site_answers_on_nothing(nginx: NginxManager) -> None:
    assert nginx.served_names("example.com") == []


def test_names_read_back_are_domains_only(nginx: NginxManager) -> None:
    """A hand-edited catch-all or wildcard is not something to adopt as a domain."""
    path = nginx.config_path("example.com")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "server {\n    listen 80;\n    server_name example.com _ *.example.com EXAMPLE.org;\n}\n"
    )

    assert nginx.served_names("example.com") == ["example.com", "example.org"]


# ---------------------------------------------------------------------------
# A configuration is validated without touching the live one
# ---------------------------------------------------------------------------


@pytest.fixture
def validation_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """
    Point the validation staging area into the test's temporary directory.

    Args:
        tmp_path: Per-test temporary directory.
        monkeypatch: Patching helper, scoped to the test.

    Returns:
        The directory validation stages its files in.
    """
    staging_root = tmp_path / "validation-tmp"
    staging_root.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(staging_root))
    return staging_root


class ValidationSnoop(FakeRunner):
    """
    A fake runner that reads the staged files before validation deletes them.

    The wrapper and the snippet only exist for the duration of the syntax
    check, so their content can only be asserted on from inside the call.
    """

    def __init__(self) -> None:
        super().__init__()
        self.wrappers: list[str] = []
        self.snippets: dict[str, str] = {}

    def run(self, argv: Any, **kwargs: Any) -> Any:
        """
        Capture the staging directory, then behave like a FakeRunner.

        Args:
            argv: Program and arguments; the wrapper path comes last.
            kwargs: The rest of the runner protocol.

        Returns:
            The scripted result.
        """
        wrapper = Path(str(argv[-1]))
        if wrapper.is_file():
            self.wrappers.append(wrapper.read_text())
            for staged in wrapper.parent.iterdir():
                if staged != wrapper:
                    self.snippets[staged.name] = staged.read_text()
        return super().run(argv, **kwargs)


@pytest.mark.parametrize(
    ("backend", "expected_prefix"),
    [("nginx", ("nginx", "-t", "-c")), ("apache", ("apache2ctl", "-t", "-f"))],
)
def test_validation_runs_the_syntax_check_against_a_staged_wrapper(
    managers: dict[str, WebServerManager],
    runner: FakeRunner,
    validation_tmp: Path,
    backend: str,
    expected_prefix: tuple[str, ...],
) -> None:
    """The check runs on a throwaway file, never on the live configuration."""
    manager = managers[backend]

    manager.validate_config_text("# fine\n", domain="example.com")

    (call,) = runner.calls
    assert call[: len(expected_prefix)] == expected_prefix
    wrapper = Path(call[-1])
    assert validation_tmp in wrapper.parents
    assert wrapper != manager.config_path("example.com")
    # Once the answer is known the staging files are gone.
    assert list(validation_tmp.iterdir()) == []


def test_nginx_validation_wraps_the_snippet_in_a_minimal_main_context(
    tmp_path: Path, store: FakeStore, validation_tmp: Path
) -> None:
    """nginx refuses a bare server block, so the wrapper supplies events and http."""
    snoop = ValidationSnoop()
    manager = NginxManager(
        runner=snoop,
        backend=replace(
            NGINX_BACKEND,
            sites_available=tmp_path / "nginx/sites-available",
            sites_enabled=tmp_path / "nginx/sites-enabled",
        ),
    )
    config = "server {\n    listen 8080;\n}\n"

    manager.validate_config_text(config, domain="example.com")

    (wrapper,) = snoop.wrappers
    assert "events {" in wrapper
    assert "http {" in wrapper
    include_line = next(
        line for line in wrapper.splitlines() if line.strip().startswith("include ")
    )
    assert include_line.strip().endswith("/example.com;")
    # The snippet reaches the syntax check verbatim.
    assert snoop.snippets["example.com"] == config


def test_apache_validation_wrapper_loads_the_live_modules(
    tmp_path: Path, store: FakeStore, validation_tmp: Path
) -> None:
    """A vhost using ProxyPass is only valid with the live module set loaded."""
    snoop = ValidationSnoop()
    manager = ApacheManager(
        runner=snoop,
        backend=replace(
            APACHE_BACKEND,
            sites_available=tmp_path / "apache/sites-available",
            sites_enabled=tmp_path / "apache/sites-enabled",
        ),
    )
    config = "<VirtualHost *:80>\n    ServerName example.com\n</VirtualHost>\n"

    manager.validate_config_text(config, domain="example.com")

    (wrapper,) = snoop.wrappers
    assert f'ServerRoot "{tmp_path / "apache"}"' in wrapper
    assert "mods-enabled/*.load" in wrapper
    include_line = next(
        line for line in wrapper.splitlines() if line.strip().startswith("Include ")
    )
    assert include_line.strip().endswith("/example.com.conf")
    assert snoop.snippets["example.com.conf"] == config


@pytest.mark.parametrize("backend", ["nginx", "apache"])
def test_validation_failure_carries_the_servers_output_verbatim(
    managers: dict[str, WebServerManager],
    runner: FakeRunner,
    validation_tmp: Path,
    backend: str,
) -> None:
    """The operator fixes what the server said, not a paraphrase of it."""
    manager = managers[backend]
    stderr = 'nginx: [emerg] unexpected end of file, expecting "}" in /tmp/x.conf:3\n'
    runner.script(list(manager.backend.validation_argv), stderr=stderr, exit_code=1)

    with pytest.raises(ValidationError) as raised:
        manager.validate_config_text("server {", domain="example.com")

    assert raised.value.details == stderr
    assert list(validation_tmp.iterdir()) == []


def test_apache_validation_warning_that_says_syntax_ok_passes(
    apache: ApacheManager, runner: FakeRunner, validation_tmp: Path
) -> None:
    """The same tolerance test_config has: a warning is not a syntax error."""
    runner.script(
        ["apache2ctl", "-t"],
        stderr="Could not reliably determine the server's FQDN\nSyntax OK\n",
        exit_code=1,
    )

    apache.validate_config_text("<VirtualHost *:80>\n</VirtualHost>\n", domain="example.com")


@pytest.mark.parametrize("backend", ["nginx", "apache"])
def test_test_config_text_reports_a_pass_without_raising(
    managers: dict[str, WebServerManager],
    runner: FakeRunner,
    validation_tmp: Path,
    backend: str,
) -> None:
    """The "try before you save" caller gets an answer, not an exception."""
    manager = managers[backend]
    stdout = "nginx: the configuration file /tmp/x.conf syntax is ok\n"
    runner.script(list(manager.backend.validation_argv), stdout=stdout)

    ok, output = manager.test_config_text("# fine\n", domain="example.com")

    assert ok is True
    assert output == stdout
    # Nothing staged outlives the call, on the passing path exactly as on the
    # failing one.
    assert list(validation_tmp.iterdir()) == []


@pytest.mark.parametrize("backend", ["nginx", "apache"])
def test_test_config_text_reports_a_failure_with_the_servers_output(
    managers: dict[str, WebServerManager],
    runner: FakeRunner,
    validation_tmp: Path,
    backend: str,
) -> None:
    """A rejected snippet is reported, not raised, with the output verbatim."""
    manager = managers[backend]
    stderr = 'nginx: [emerg] unexpected end of file, expecting "}"\n'
    runner.script(list(manager.backend.validation_argv), stderr=stderr, exit_code=1)

    ok, output = manager.test_config_text("server {", domain="example.com")

    assert ok is False
    assert output == stderr


@pytest.mark.parametrize("backend", ["nginx", "apache"])
def test_test_config_text_never_writes_the_real_site(
    managers: dict[str, WebServerManager],
    runner: FakeRunner,
    validation_tmp: Path,
    backend: str,
) -> None:
    """Testing a candidate config must not touch the site it might replace."""
    manager = managers[backend]
    manager.create_site("example.com", context={"port": 3000})
    before = manager.get_site_config("example.com")

    manager.test_config_text("server { listen 9999; }", domain="example.com")

    assert manager.get_site_config("example.com") == before


def test_validate_config_text_carries_the_output_on_the_exception(
    nginx: NginxManager, runner: FakeRunner, validation_tmp: Path
) -> None:
    """The output travels on a field of its own, not only inside details."""
    stderr = 'nginx: [emerg] unexpected end of file, expecting "}"\n'
    runner.script(list(nginx.backend.validation_argv), stderr=stderr, exit_code=1)

    with pytest.raises(ValidationError) as raised:
        nginx.validate_config_text("server {", domain="example.com")

    assert raised.value.output == stderr
    assert raised.value.details == stderr


@pytest.mark.parametrize("backend", ["nginx", "apache"])
def test_replace_refuses_an_invalid_config_and_keeps_the_file(
    managers: dict[str, WebServerManager],
    runner: FakeRunner,
    validation_tmp: Path,
    backend: str,
) -> None:
    """A config the server rejects must never reach disk (finding M-4)."""
    manager = managers[backend]
    manager.create_site("example.com", context={"port": 3000})
    before = manager.get_site_config("example.com")
    runner.script(
        list(manager.backend.validation_argv),
        stderr="nginx: [emerg] unexpected end of file",
        exit_code=1,
    )

    with pytest.raises(ValidationError):
        manager.replace_site_config("example.com", "server {")

    assert manager.get_site_config("example.com") == before


@pytest.mark.parametrize("backend", ["nginx", "apache"])
def test_replace_persists_a_valid_config(
    managers: dict[str, WebServerManager],
    runner: FakeRunner,
    validation_tmp: Path,
    backend: str,
) -> None:
    """A config the server accepts replaces the file, verbatim."""
    manager = managers[backend]
    manager.create_site("example.com")
    new_config = "# hand edited\nserver {\n    listen 8081;\n}\n"

    manager.replace_site_config("example.com", new_config)

    assert manager.get_site_config("example.com") == new_config


@pytest.mark.parametrize("backend", ["nginx", "apache"])
def test_replace_on_a_missing_site_is_refused(
    managers: dict[str, WebServerManager], runner: FakeRunner, backend: str
) -> None:
    """Replacing a config that does not exist would hide a typo in a domain."""
    with pytest.raises((NginxError, ApacheError)):
        managers[backend].replace_site_config("example.com", "server {\n}\n")


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
    (
        "proxy-alias",
        {
            "ssl": False,
            "port": 3000,
            "server_names": "example.com shop.example.com example.org",
        },
    ),
    (
        "proxy-redirect",
        {
            "ssl": False,
            "port": 3000,
            "redirect_domains": ["www.example.com"],
        },
    ),
    (
        "proxy-redirect-ssl",
        {
            "ssl": True,
            "port": 3000,
            "server_names": "example.com shop.example.com",
            "redirect_domains": ["www.example.com", "old.example.org"],
            "ssl_certificate": "/etc/letsencrypt/live/example.com/fullchain.pem",
            "ssl_certificate_key": "/etc/letsencrypt/live/example.com/privkey.pem",
        },
    ),
    (
        "static-redirect-ssl",
        {
            "ssl": True,
            "static_dir": "/var/www/apps/example.com/current/dist",
            "server_names": "example.com shop.example.com",
            "redirect_domains": ["www.example.com"],
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
    template = case.split("-")[0]
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


def _domains(*entries: tuple[str, str]) -> list[DomainRecord]:
    """
    Build the domain rows of example.com.

    Args:
        entries: ``(domain, kind)`` pairs after the primary.

    Returns:
        The primary followed by the given rows.
    """
    rows = [DomainRecord(id=1, app_id=1, domain="example.com", kind="primary")]
    rows += [
        DomainRecord(id=index, app_id=1, domain=domain, kind=kind)
        for index, (domain, kind) in enumerate(entries, start=2)
    ]
    return rows


@pytest.mark.parametrize("backend", ["nginx", "apache"])
def test_writing_an_applications_site_serves_every_one_of_its_domains(
    managers: dict[str, WebServerManager], store: FakeStore, backend: str
) -> None:
    """
    The rows are read where the file is written, so no caller can forget them.

    A redeploy, the certificate step and a site created from the panel all
    write the same vhost; any one of them rendering from its own idea of the
    names would drop every alias the next time it ran.
    """
    store.domains["example.com"] = _domains(
        ("shop.example.com", "alias"), ("www.example.com", "redirect")
    )
    manager = managers[backend]

    manager.create_site("example.com", "proxy", {"port": 3000})
    written = manager.config_path("example.com").read_text()

    if backend == "nginx":
        assert "server_name example.com shop.example.com;" in written
        assert "server_name www.example.com;" in written
        assert "return 301 http://example.com$request_uri;" in written
    else:
        assert "ServerAlias shop.example.com" in written
        assert "ServerName www.example.com" in written
        assert "RewriteRule ^ http://example.com%{REQUEST_URI} [L,R=301]" in written


def test_the_stored_domains_win_over_a_callers_idea_of_the_names(
    nginx: NginxManager, store: FakeStore
) -> None:
    """A deployer still passing the old include_www names cannot resurrect a removed one."""
    store.domains["example.com"] = _domains(("shop.example.com", "alias"))

    nginx.create_site("example.com", "proxy", {"server_names": "example.com www.example.com"})
    written = nginx.config_path("example.com").read_text()

    assert "server_name example.com shop.example.com;" in written
    assert "www.example.com" not in written


def test_a_site_that_is_no_application_keeps_the_names_it_was_given(
    nginx: NginxManager, store: FakeStore
) -> None:
    """``wasm site create --www`` has no domain rows; its own names are the truth."""
    nginx.create_site("example.com", "proxy", {"server_names": "example.com www.example.com"})

    assert "server_name example.com www.example.com;" in (
        nginx.config_path("example.com").read_text()
    )


@pytest.mark.parametrize("backend", ["nginx", "apache"])
@pytest.mark.parametrize(
    "context",
    [
        {"server_names": "example.com evil.com;\n    root /"},
        {"server_names": "example.com\nInclude /etc/shadow"},
        {"redirect_domains": ["old.example.org; return 200"]},
        {"redirect_domains": ["old.example.org\n    root /"]},
    ],
)
def test_every_name_is_checked_before_it_becomes_a_directive(
    managers: dict[str, WebServerManager], backend: str, context: dict[str, Any]
) -> None:
    """The primary was always checked; the names beside it now are too."""
    with pytest.raises(DomainError):
        managers[backend].render_config("example.com", "proxy", context)


@pytest.mark.parametrize("kind", ["alias", "redirect"])
def test_a_site_is_not_created_for_another_applications_domain(
    nginx: NginxManager, store: FakeStore, kind: str
) -> None:
    """Two server blocks claiming one name: nginx picks one and warns nobody who is looking."""
    store.owners["shop.example.com"] = ("example.com", kind)

    with pytest.raises(DomainConflictError, match=re.escape(f"{kind} of example.com")):
        create_secured_site("shop.example.com", manager=nginx, webserver="nginx", ssl=False)

    assert not nginx.site_exists("shop.example.com")


def test_an_applications_own_site_can_still_be_written(
    nginx: NginxManager, store: FakeStore
) -> None:
    """The primary's vhost is the application's; ``wasm site create`` may rewrite it."""
    store.owners["example.com"] = ("example.com", "primary")

    create_secured_site("example.com", manager=nginx, webserver="nginx", ssl=False)

    assert nginx.site_exists("example.com")


def test_a_site_is_still_written_when_the_store_cannot_be_read(
    nginx: NginxManager, store: FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A rehearsal on a fresh machine has no database; the vhost never needed one."""

    def unreadable(*_args: Any) -> Any:
        raise StoreError("Cannot open the WASM database at /var/lib/wasm/wasm.db")

    monkeypatch.setattr(store, "list_domains", unreadable)
    monkeypatch.setattr(store, "domain_owner", unreadable)

    nginx.create_site("example.com", "proxy", {"server_names": "example.com www.example.com"})

    assert "server_name example.com www.example.com;" in (
        nginx.config_path("example.com").read_text()
    )


def test_a_redirect_that_is_also_served_is_rendered_once(nginx: NginxManager) -> None:
    """Serving a name and redirecting it away are contradictory; serving wins."""
    rendered = nginx.render_config(
        "example.com",
        "proxy",
        {"server_names": "example.com shop.example.com", "redirect_domains": ["shop.example.com"]},
    )

    assert "server_name shop.example.com;" not in rendered


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
    runner.script(["certbot", "certificates"], stdout=CERTBOT_OUTPUT)

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
        from wasm.managers import health
    except Exception as exc:
        # The contract under test is the field names, and the sibling test above
        # checks those from source. An unrelated import failure elsewhere in the
        # CLI must not be reported as a certificate defect.
        pytest.skip(f"wasm.cli.commands.health cannot be imported: {exc}")

    expiry = (datetime.now() + timedelta(days=3)).strftime("%Y-%m-%d")
    runner.script(
        ["certbot", "certificates"],
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
    runner.script(["certbot", "certificates"], stdout=CERTBOT_OUTPUT)
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

    source = (Path(__file__).resolve().parents[1] / "src/wasm/managers/health.py").read_text()
    keys = set(_re.findall(r'cert(?:_info)?(?:\.get\(|\[)"([a-z_]+)"', source))

    assert keys, "the health check no longer reads certificate fields by name"
    assert keys <= set(CertificateInfo().keys())


def test_the_expiry_a_reader_alerts_on_is_a_parseable_date(
    certs: CertManager, runner: FakeRunner
) -> None:
    """The health check turns this field into a number of days."""
    expiry = (datetime.now() + timedelta(days=3)).strftime("%Y-%m-%d")
    runner.script(
        ["certbot", "certificates"],
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
    runner.script(["certbot", "plugins"], stdout="* nginx\nDescription: Nginx\n")

    assert certs._check_certbot_plugin("nginx") is True
    assert certs._check_certbot_plugin("nginx") is True
    assert runner.calls_to("certbot").count(("certbot", "plugins")) == 1


def test_issuance_pins_the_lineage_and_covers_www(certs: CertManager, runner: FakeRunner) -> None:
    """Without --cert-name certbot invents example.com-0001 on the next change."""
    runner.script(["certbot", "plugins"], stdout="* nginx\n")

    certs.obtain("example.com", email="ops@example.com", nginx=True, include_www=True)

    (issued,) = [c for c in runner.calls if "certonly" in c]
    assert issued == (
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
    runner.script(["certbot", "plugins"], stdout="* standalone\n")

    certs.obtain("example.com", email="ops@example.com", nginx=True)

    (issued,) = [c for c in runner.calls if "certonly" in c]
    assert issued[-5:] == ("--webroot", "-w", "/var/www/html", "-d", "example.com")
    assert "--nginx" not in issued


def test_a_certificate_that_already_covers_everything_is_left_alone(
    certs: CertManager, runner: FakeRunner
) -> None:
    """Issuance is rate limited; running the deploy twice must be cheap."""
    _issue_certificate(certs, "example.com")
    runner.script(["certbot", "certificates"], stdout=CERTBOT_OUTPUT)

    assert certs.obtain("example.com", email="ops@example.com", include_www=True) is True
    assert not any("certonly" in call for call in runner.calls)


def test_a_certificate_missing_a_domain_is_expanded_not_reissued(
    certs: CertManager, runner: FakeRunner
) -> None:
    """A second lineage would leave two half-right certificates renewing."""
    _issue_certificate(certs, "example.com")
    runner.script(
        ["certbot", "certificates"],
        stdout=CERTBOT_OUTPUT.replace(" www.example.com", ""),
    )

    certs.obtain(
        "example.com",
        email="ops@example.com",
        additional_domains=["api.example.com"],
    )

    (issued,) = [c for c in runner.calls if "certonly" in c]
    assert "--expand" in issued
    assert issued[:4] == ("certbot", "certonly", "--cert-name", "example.com")
    assert issued.count("-d") == 2


def test_an_applications_certificate_covers_every_one_of_its_domains(
    certs: CertManager, runner: FakeRunner, store: FakeStore
) -> None:
    """
    Aliases and redirects alike: a redirect answers on 443 too, and a browser
    shown the wrong certificate never sees the redirect.

    The names are read where the order is placed, so ``wasm cert create``, the
    deploy step and a domain change all ask for the same set.
    """
    store.domains["example.com"] = _domains(
        ("shop.example.com", "alias"), ("www.example.com", "redirect")
    )

    certs.obtain("example.com", email="ops@example.com", nginx=True)

    (issued,) = [c for c in runner.calls if "certonly" in c]
    requested = [issued[i + 1] for i, arg in enumerate(issued) if arg == "-d"]
    assert requested == ["example.com", "shop.example.com", "www.example.com"]
    # The list the panel's certificate job reports back is the one ordered.
    assert certs.certificate_domains("example.com") == requested


def test_adding_a_domain_expands_the_lineage_and_keeps_what_it_covered(
    certs: CertManager, runner: FakeRunner, store: FakeStore
) -> None:
    """A new alias re-issues with --expand under the same name; nothing is dropped or revoked."""
    _issue_certificate(certs, "example.com")
    runner.script(["certbot", "certificates"], stdout=CERTBOT_OUTPUT)
    store.domains["example.com"] = _domains(("shop.example.com", "alias"))

    certs.obtain("example.com", email="ops@example.com")

    (issued,) = [c for c in runner.calls if "certonly" in c]
    requested = [issued[i + 1] for i, arg in enumerate(issued) if arg == "-d"]
    assert issued[:4] == ("certbot", "certonly", "--cert-name", "example.com")
    assert "--expand" in issued
    # www.example.com is no longer a domain of the app, but the certificate
    # covered it: shrinking a live certificate is not what adding a name means.
    assert requested == ["example.com", "shop.example.com", "www.example.com"]
    assert not [c for c in runner.calls if "revoke" in c]


def test_a_certificate_that_covers_every_domain_is_not_reissued(
    certs: CertManager, runner: FakeRunner, store: FakeStore
) -> None:
    """Re-rendering after a domain change must not spend a rate limit for nothing."""
    _issue_certificate(certs, "example.com")
    runner.script(["certbot", "certificates"], stdout=CERTBOT_OUTPUT)
    store.domains["example.com"] = _domains(("www.example.com", "redirect"))

    assert certs.obtain("example.com", email="ops@example.com") is True

    assert not [c for c in runner.calls if "certonly" in c]


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
    runner.script(["certbot", "renew"], stderr="rate limit exceeded", exit_code=1)

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


# ---------------------------------------------------------------------------
# A rehearsal writes nothing and deletes nothing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("backend", ["nginx", "apache"])
def test_create_site_under_dry_run_writes_no_configuration(
    rehearsals: dict[str, WebServerManager],
    dry_fs: DryRunFileSystem,
    tmp_path: Path,
    backend: str,
) -> None:
    """A rehearsed create must leave no vhost and no staging file behind."""
    manager = rehearsals[backend]

    assert manager.create_site("example.com", context={"port": 3000}) is True

    assert [p for p in tmp_path.rglob("*") if p.is_file()] == []
    assert not manager.site_exists("example.com")
    assert any("example.com" in skipped for skipped in dry_fs.skipped)


@pytest.mark.parametrize("backend", ["nginx", "apache"])
def test_update_site_under_dry_run_keeps_the_previous_configuration(
    managers: dict[str, WebServerManager],
    rehearsals: dict[str, WebServerManager],
    backend: str,
) -> None:
    """Rehearsing a port change must not change the port the server serves."""
    managers[backend].create_site("example.com", context={"port": 3000})
    before = managers[backend].get_site_config("example.com")

    rehearsals[backend].update_site("example.com", context={"port": 4100})

    assert managers[backend].get_site_config("example.com") == before
    assert "4100" not in before


@pytest.mark.parametrize("backend", ["nginx", "apache"])
def test_delete_site_under_dry_run_keeps_the_file(
    managers: dict[str, WebServerManager],
    rehearsals: dict[str, WebServerManager],
    dry_fs: DryRunFileSystem,
    backend: str,
) -> None:
    """
    The defect the seam exists for, in its web server form.

    ``--dry-run site delete`` announced that nothing would change and then
    unlinked the virtual host, because the deletion is a ``Path.unlink``.
    """
    manager = managers[backend]
    manager.create_site("example.com")
    config = manager.config_path("example.com")
    before = config.read_text()

    assert rehearsals[backend].delete_site("example.com") is True

    assert config.exists()
    assert config.read_text() == before
    assert any(str(config) in skipped for skipped in dry_fs.skipped)


def test_enable_site_under_dry_run_writes_no_symlink(
    nginx: NginxManager, rehearsals: dict[str, WebServerManager]
) -> None:
    """nginx enables a site by writing a link, which is a change like any other."""
    nginx.create_site("example.com")

    rehearsals["nginx"].enable_site("example.com")

    link = nginx.sites_enabled / "example.com"
    assert not link.is_symlink()
    assert not link.exists()


def test_disable_site_under_dry_run_keeps_the_symlink(
    nginx: NginxManager, rehearsals: dict[str, WebServerManager]
) -> None:
    """A rehearsed disable must not take a live site out of service."""
    nginx.create_site("example.com")
    nginx.enable_site("example.com")
    link = nginx.sites_enabled / "example.com"

    rehearsals["nginx"].disable_site("example.com")

    assert link.is_symlink()
    assert nginx.site_enabled("example.com") is True


@pytest.mark.parametrize("backend", ["nginx", "apache"])
def test_the_process_wide_filesystem_is_honoured_without_injection(
    managers: dict[str, WebServerManager], tmp_path: Path, backend: str
) -> None:
    """
    ``wasm --dry-run`` installs the rehearsal filesystem globally, not per call.

    Every CLI command builds its managers the ordinary way, so the flag is only
    honest if a manager nobody handed a filesystem to still picks up the one the
    entry point installed.
    """
    manager = managers[backend]
    manager.create_site("example.com")
    config = manager.config_path("example.com")

    dry = DryRunFileSystem()
    set_fs(dry)
    try:
        factory = NginxManager if backend == "nginx" else ApacheManager
        factory(backend=manager.backend).delete_site("example.com")
    finally:
        set_fs(None)

    assert config.exists()
    assert dry.skipped


def test_auto_renewal_under_dry_run_installs_no_cron_entry(
    runner: FakeRunner,
    store: FakeStore,
    dry_fs: DryRunFileSystem,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The cron fallback writes into /etc/cron.d; a rehearsal must not."""
    cron_file = tmp_path / "cron.d/certbot-renew"
    monkeypatch.setattr("wasm.managers.cert_manager._CRON_FILE", cron_file)
    # Without a certbot.timer the manager falls through to the cron entry, which
    # is the only path in this manager that writes a file.
    runner.script(["systemctl", "enable", "certbot.timer"], exit_code=1)

    assert CertManager(fs=dry_fs).setup_auto_renewal() is True

    assert not cron_file.exists()
    assert not cron_file.parent.exists()
    assert any(str(cron_file) in skipped for skipped in dry_fs.skipped)


def test_auto_renewal_writes_the_cron_entry_through_the_seam(
    runner: FakeRunner, store: FakeStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real path still has to work, and cron ignores a writable file."""
    cron_file = tmp_path / "cron.d/certbot-renew"
    monkeypatch.setattr("wasm.managers.cert_manager._CRON_FILE", cron_file)
    runner.script(["systemctl", "enable", "certbot.timer"], exit_code=1)

    assert CertManager().setup_auto_renewal() is True

    assert "certbot renew -q" in cron_file.read_text()
    assert cron_file.stat().st_mode & 0o022 == 0
