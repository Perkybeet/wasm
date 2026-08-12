# Copyright (c) 2024-2026 Yago Lopez Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
Tests for the ``wasm site`` command group after the move to Click.

Three things are pinned here:

- **The published surface.** The commands, the aliases and the options are read
  straight out of ``tests/contracts/cli_surface.json``, so a rename shows up as
  a failing test rather than as a support ticket.
- **What each command actually does.** The managers are either real ones
  pointed at a temporary configuration tree, with the exact argv asserted
  through the FakeRunner, or recorders that capture the call sequence. The old
  handlers were reachable only through argparse and were therefore untested.
- **The flags a subcommand must not own.** ``--verbose``, ``--dry-run``,
  ``--json`` and ``--no-color`` belong to the root group and to the shared
  context. A subcommand that redeclares one of them is the shadowing bug the
  migration exists to remove.
"""

from __future__ import annotations

import json
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

import click
import pytest
from click.testing import CliRunner, Result

from wasm.cli.app import Context
from wasm.cli.commands import site as site_cli
from wasm.core.logger import Logger
from wasm.core.runner import FakeRunner
from wasm.managers.apache_manager import ApacheManager
from wasm.managers.nginx_manager import NginxManager
from wasm.managers.webserver import APACHE_BACKEND, NGINX_BACKEND

CONTRACT = json.loads(
    (Path(__file__).parent / "contracts/cli_surface.json").read_text(encoding="utf-8")
)

#: ``site create``, ``site list``, ... exactly as the argparse tree offered them.
CONTRACT_COMMANDS = sorted(key for key in CONTRACT if key.startswith("site "))

#: (command, alias) for every alternative spelling the contract froze.
CONTRACT_ALIASES = sorted(
    (key.split(" ")[-1], alias) for key in CONTRACT_COMMANDS for alias in CONTRACT[key]["aliases"]
)

#: Flags that live on the root group and on the shared context. No subcommand
#: may declare one: that is how ``wasm --dry-run site delete`` used to lose its
#: dry run between the root parser and the subparser.
GLOBAL_FLAGS = frozenset({"--verbose", "-v", "--dry-run", "--json", "--no-color"})


class FakeStore:
    """A store that accepts site records and forgets them."""

    def get_site(self, domain: str) -> None:
        """
        Look up a site record.

        Args:
            domain: Domain name.

        Returns:
            None, because this store never keeps anything.
        """
        return None

    def create_site(self, site: Any) -> Any:
        """
        Accept a new site record.

        Args:
            site: The record.

        Returns:
            The record, unchanged.
        """
        return site

    def update_site(self, site: Any) -> None:
        """
        Accept an update to a site record.

        Args:
            site: The record.
        """

    def delete_site(self, domain: str) -> None:
        """
        Accept the removal of a site record.

        Args:
            domain: Domain name.
        """


class RecordingManager:
    """
    A web server manager that records what it was asked to do.

    Used for ``site create``, where the interesting behaviour is the order of
    the calls: the site is written without SSL, the certificate is obtained,
    and only then is the configuration rewritten with the certificate paths.
    """

    def __init__(self, *, exists: bool = False) -> None:
        """
        Args:
            exists: Whether the site is already configured on this server.
        """
        self.exists = exists
        self.calls: list[tuple[str, Any]] = []

    def site_exists(self, domain: str) -> bool:
        """
        Report whether the site is configured.

        Args:
            domain: Domain name.

        Returns:
            The value this recorder was built with.
        """
        return self.exists

    def create_site(self, domain: str, template: str, context: dict[str, Any]) -> bool:
        """
        Record a site creation.

        Args:
            domain: Domain name.
            template: Template name.
            context: Template variables.

        Returns:
            True.
        """
        self.calls.append(("create_site", (domain, template, dict(context))))
        self.exists = True
        return True

    def update_site(self, domain: str, template: str, context: dict[str, Any]) -> bool:
        """
        Record a site rewrite.

        Args:
            domain: Domain name.
            template: Template name.
            context: Template variables.

        Returns:
            True.
        """
        self.calls.append(("update_site", (domain, template, dict(context))))
        return True

    def enable_site(self, domain: str) -> bool:
        """
        Record enabling a site.

        Args:
            domain: Domain name.

        Returns:
            True.
        """
        self.calls.append(("enable_site", domain))
        return True

    def reload(self) -> bool:
        """
        Record a web server reload.

        Returns:
            True.
        """
        self.calls.append(("reload", None))
        return True

    @property
    def names(self) -> list[str]:
        """The names of the operations performed, in order."""
        return [name for name, _ in self.calls]


class FakeCertManager:
    """A certificate manager that answers from memory."""

    def __init__(
        self,
        *,
        installed: bool = True,
        has_cert: bool = False,
        valid: bool = True,
        covers: bool = True,
    ) -> None:
        """
        Args:
            installed: Whether certbot is present.
            has_cert: Whether a certificate already exists for the domain.
            valid: Whether that certificate is valid.
            covers: Whether that certificate covers every requested domain.
        """
        self._installed = installed
        self.has_cert = has_cert
        self._valid = valid
        self._covers = covers
        self.obtained: list[tuple[str, dict[str, Any]]] = []
        self.deleted: list[str] = []

    def is_installed(self) -> bool:
        """
        Report whether certbot is available.

        Returns:
            True when it is.
        """
        return self._installed

    def cert_exists(self, domain: str) -> bool:
        """
        Report whether a certificate exists.

        Args:
            domain: Domain name.

        Returns:
            True when one exists.
        """
        return self.has_cert

    def test_cert(self, domain: str) -> dict[str, bool]:
        """
        Report the health of the existing certificate.

        Args:
            domain: Domain name.

        Returns:
            A mapping with the ``valid`` key.
        """
        return {"valid": self._valid}

    def cert_covers_domains(self, domain: str, required: list[str]) -> bool:
        """
        Report whether the certificate covers every domain asked for.

        Args:
            domain: Lineage name.
            required: Domains that must be covered.

        Returns:
            True when they are.
        """
        return self._covers

    def obtain(self, domain: str, **kwargs: Any) -> bool:
        """
        Record an issuance request.

        Args:
            domain: Primary domain.
            **kwargs: The rest of the issuance options.

        Returns:
            True.
        """
        self.obtained.append((domain, kwargs))
        self.has_cert = True
        return True

    def get_cert_path(self, domain: str) -> dict[str, Path]:
        """
        Report where the certificate lives.

        Args:
            domain: Domain name.

        Returns:
            The fullchain and private key paths.
        """
        base = Path("/etc/letsencrypt/live") / domain
        return {"fullchain": base / "fullchain.pem", "privkey": base / "privkey.pem"}

    def delete(self, domain: str) -> bool:
        """
        Record a deletion.

        Args:
            domain: Domain name.

        Returns:
            True.
        """
        self.deleted.append(domain)
        return True


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> FakeStore:
    """
    Keep the managers away from the real SQLite database.

    Args:
        monkeypatch: Patching helper, scoped to the test.

    Returns:
        The fake store.
    """
    fake = FakeStore()
    monkeypatch.setattr("wasm.managers.webserver.get_store", lambda: fake)
    return fake


@pytest.fixture
def webservers(
    tmp_path: Path,
    runner: FakeRunner,
    store: FakeStore,
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, Any]:
    """
    Point the command group at real managers over a temporary config tree.

    Args:
        tmp_path: Per-test temporary directory.
        runner: The fake command runner.
        store: The fake store.
        monkeypatch: Patching helper, scoped to the test.

    Returns:
        The nginx and apache managers the commands will use.
    """
    nginx = NginxManager(
        backend=replace(
            NGINX_BACKEND,
            sites_available=tmp_path / "nginx/sites-available",
            sites_enabled=tmp_path / "nginx/sites-enabled",
        )
    )
    apache = ApacheManager(
        backend=replace(
            APACHE_BACKEND,
            sites_available=tmp_path / "apache/sites-available",
            sites_enabled=tmp_path / "apache/sites-enabled",
        )
    )
    monkeypatch.setattr(site_cli, "NginxManager", lambda **kwargs: nginx)
    monkeypatch.setattr(site_cli, "ApacheManager", lambda **kwargs: apache)
    return {"nginx": nginx, "apache": apache}


@pytest.fixture
def certs(monkeypatch: pytest.MonkeyPatch) -> FakeCertManager:
    """
    Replace the certificate manager the commands build.

    Args:
        monkeypatch: Patching helper, scoped to the test.

    Returns:
        The fake certificate manager every command in the test will get.
    """
    fake = FakeCertManager()
    monkeypatch.setattr(site_cli, "CertManager", lambda **kwargs: fake)
    return fake


def write_site(manager: Any, domain: str, *, enabled: bool = False) -> Path:
    """
    Put a virtual host file on disk, bypassing the templates.

    Args:
        manager: The manager owning the configuration tree.
        domain: Domain name.
        enabled: Also link it into the enabled directory.

    Returns:
        Path of the configuration file.
    """
    config: Path = manager.config_path(domain)
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(f"# {domain}\n")
    if enabled:
        link = manager.sites_enabled / config.name
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(config)
    return config


class LiveStdout:
    """
    A stream that resolves ``sys.stdout`` at write time.

    :class:`~wasm.core.logger.Logger` takes its stream as a default argument,
    which binds whatever ``sys.stdout`` was when the module was imported. That
    is not the stream Click's test runner installs, so without this indirection
    nothing a command logs would be visible to a test.
    """

    def write(self, text: str) -> int:
        """
        Write to whichever stream is current.

        Args:
            text: Text to write.

        Returns:
            Number of characters written.
        """
        return sys.stdout.write(text)

    def flush(self) -> None:
        """Flush the current stream."""
        sys.stdout.flush()

    def isatty(self) -> bool:
        """
        Report that this is not a terminal, so no colour is emitted.

        Returns:
            False.
        """
        return False


def invoke(args: list[str], **kwargs: Any) -> Result:
    """
    Run the site group with Click's test runner.

    The shared context is built here, exactly as the root group builds it, so
    the commands are exercised through the same seam they use in production.

    Args:
        args: Arguments after ``wasm site``.
        **kwargs: Passed to ``CliRunner.invoke``.

    Returns:
        The Click result.
    """
    state = Context(_logger=Logger(stream=LiveStdout()))
    return CliRunner().invoke(site_cli.cli, args, obj=state, **kwargs)


# ---------------------------------------------------------------------------
# The published surface
# ---------------------------------------------------------------------------


def test_the_contract_lists_the_commands_this_file_checks() -> None:
    """A guard against the contract being read from the wrong place."""
    assert CONTRACT_COMMANDS == [
        "site create",
        "site delete",
        "site disable",
        "site enable",
        "site list",
        "site show",
    ]
    assert CONTRACT_ALIASES == [
        ("delete", "remove"),
        ("delete", "rm"),
        ("list", "ls"),
        ("show", "cat"),
    ]


@pytest.mark.parametrize("command", CONTRACT_COMMANDS)
def test_every_command_answers_help(command: str) -> None:
    """Every command the argparse tree offered still exists and documents itself."""
    name = command.split(" ")[-1]
    result = invoke([name, "--help"])

    assert result.exit_code == 0, result.output
    assert name in result.output


@pytest.mark.parametrize(("command", "alias"), CONTRACT_ALIASES)
def test_every_alias_resolves_to_its_command(command: str, alias: str) -> None:
    """The alternative spellings are in scripts and in muscle memory."""
    ctx = click.Context(site_cli.cli)

    assert site_cli.cli.get_command(ctx, alias) is site_cli.cli.get_command(ctx, command)
    assert invoke([alias, "--help"]).exit_code == 0


@pytest.mark.parametrize("command", CONTRACT_COMMANDS)
def test_every_option_the_contract_froze_is_still_offered(command: str) -> None:
    """Losing an option is a breaking change for the scripts that pass it."""
    name = command.split(" ")[-1]
    ctx = click.Context(site_cli.cli)
    subcommand = site_cli.cli.get_command(ctx, name)
    assert subcommand is not None

    offered = {opt for param in subcommand.params for opt in param.opts + param.secondary_opts}
    expected = set(CONTRACT[command]["options"]) - GLOBAL_FLAGS

    assert expected <= offered


def test_aliases_are_not_listed_twice_in_the_help() -> None:
    """Six operations should read as six lines, not as ten."""
    ctx = click.Context(site_cli.cli)

    assert site_cli.cli.list_commands(ctx) == [
        "create",
        "delete",
        "disable",
        "enable",
        "list",
        "show",
    ]


def test_no_command_redeclares_a_global_flag() -> None:
    """
    The shadowing bug, pinned.

    ``--verbose`` and friends belong to the root group and reach the command
    through the shared context. A subcommand that declares its own copy gets a
    default that overwrites what the user typed before the command name.
    """
    ctx = click.Context(site_cli.cli)
    commands = [site_cli.cli, *(site_cli.cli.get_command(ctx, n) for n in site_cli.cli.commands)]

    for command in commands:
        assert command is not None
        declared = {opt for param in command.params for opt in param.opts + param.secondary_opts}
        assert not declared & GLOBAL_FLAGS, f"{command.name} redeclares a global flag"


def test_the_group_is_exposed_as_cli_for_the_lazy_loader() -> None:
    """``wasm.cli.app`` imports the module and looks for an object called cli."""
    assert isinstance(site_cli.cli, click.Group)
    assert site_cli.cli.name == "site"


# ---------------------------------------------------------------------------
# Bad input is refused before anything is touched
# ---------------------------------------------------------------------------


def test_create_without_a_domain_is_a_usage_error(runner: FakeRunner) -> None:
    """A missing required option is Click's job, not a traceback's."""
    result = invoke(["create"])

    assert result.exit_code == 2
    assert "--domain" in result.output
    assert runner.calls == []


@pytest.mark.parametrize("missing", ["enable", "disable", "delete", "show"])
def test_commands_that_need_a_domain_refuse_to_run_without_one(
    missing: str, runner: FakeRunner
) -> None:
    """Every domain-taking command reports usage rather than crashing."""
    result = invoke([missing])

    assert result.exit_code == 2
    assert "DOMAIN" in result.output
    assert runner.calls == []


@pytest.mark.parametrize(
    "args",
    [
        ["create", "-d", "example.com", "-p", "not-a-port"],
        ["create", "-d", "example.com", "-p", "0"],
        ["create", "-d", "example.com", "-p", "70000"],
        ["create", "-d", "example.com", "-w", "iis"],
        ["list", "-w", "iis"],
    ],
)
def test_invalid_values_are_refused_before_the_server_is_touched(
    args: list[str], runner: FakeRunner
) -> None:
    """A port that is not a port must never reach a template."""
    result = invoke(args)

    assert result.exit_code == 2
    assert runner.calls == []


# ---------------------------------------------------------------------------
# What the commands do
# ---------------------------------------------------------------------------


def test_create_writes_the_site_enables_it_and_reloads(
    monkeypatch: pytest.MonkeyPatch, certs: FakeCertManager
) -> None:
    """The first pass must be plain HTTP: certbot validates over port 80."""
    manager = RecordingManager()
    monkeypatch.setattr(site_cli, "_get_manager", lambda webserver, verbose=False: manager)

    result = invoke(["create", "-d", "example.com", "-p", "8080", "--no-ssl"])

    assert result.exit_code == 0
    assert manager.names == ["create_site", "enable_site", "reload"]
    _, (domain, template, context) = manager.calls[0]
    assert domain == "example.com"
    assert template == "proxy"
    assert context == {"port": 8080, "ssl": False, "server_names": "example.com"}
    assert certs.obtained == []


def test_create_obtains_a_certificate_then_rewrites_the_site_with_it(
    monkeypatch: pytest.MonkeyPatch, certs: FakeCertManager
) -> None:
    """The certificate paths are only written once the certificate exists."""
    manager = RecordingManager()
    monkeypatch.setattr(site_cli, "_get_manager", lambda webserver, verbose=False: manager)

    result = invoke(["create", "-d", "example.com"])

    assert result.exit_code == 0
    assert manager.names == ["create_site", "enable_site", "reload", "update_site", "reload"]
    assert certs.obtained == [
        ("example.com", {"nginx": True, "apache": False, "additional_domains": None})
    ]
    _, (_, _, context) = manager.calls[3]
    assert context["ssl"] is True
    assert context["ssl_certificate"].endswith("example.com/fullchain.pem")
    assert context["ssl_certificate_key"].endswith("example.com/privkey.pem")


def test_create_with_www_serves_and_certifies_both_names(
    monkeypatch: pytest.MonkeyPatch, certs: FakeCertManager
) -> None:
    """A certificate without www.<domain> breaks the link half the world types."""
    manager = RecordingManager()
    monkeypatch.setattr(site_cli, "_get_manager", lambda webserver, verbose=False: manager)

    result = invoke(["create", "-d", "example.com", "--www"])

    assert result.exit_code == 0
    _, (_, _, context) = manager.calls[0]
    assert context["server_names"] == "example.com www.example.com"
    assert certs.obtained[0][1]["additional_domains"] == ["www.example.com"]


def test_create_falls_back_to_plain_http_when_certbot_is_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A server without certbot still gets a working site, and is told why."""
    manager = RecordingManager()
    monkeypatch.setattr(site_cli, "_get_manager", lambda webserver, verbose=False: manager)
    monkeypatch.setattr(site_cli, "CertManager", lambda **kwargs: FakeCertManager(installed=False))

    result = invoke(["create", "-d", "example.com"])

    assert result.exit_code == 0
    assert manager.names == ["create_site", "enable_site", "reload"]
    assert "certbot" in result.output.lower()


def test_create_updates_an_existing_site_instead_of_failing(
    monkeypatch: pytest.MonkeyPatch, certs: FakeCertManager
) -> None:
    """Re-running create on a configured domain used to raise 'already exists'."""
    manager = RecordingManager(exists=True)
    monkeypatch.setattr(site_cli, "_get_manager", lambda webserver, verbose=False: manager)

    result = invoke(["create", "-d", "example.com", "--no-ssl"])

    assert result.exit_code == 0
    assert manager.names == ["update_site", "reload"]


def test_create_selects_the_apache_manager(
    monkeypatch: pytest.MonkeyPatch, certs: FakeCertManager
) -> None:
    """-w apache must reach the apache manager and the apache certbot plugin."""
    seen: list[str] = []
    manager = RecordingManager()

    def factory(webserver: str, verbose: bool = False) -> RecordingManager:
        seen.append(webserver)
        return manager

    monkeypatch.setattr(site_cli, "_get_manager", factory)

    result = invoke(["create", "-d", "example.com", "-w", "apache"])

    assert result.exit_code == 0
    assert seen == ["apache"]
    assert certs.obtained[0][1] == {
        "nginx": False,
        "apache": True,
        "additional_domains": None,
    }


def test_enable_links_the_site_and_reloads_nginx(
    webservers: dict[str, Any], runner: FakeRunner
) -> None:
    """The exact argv matters: a reload of a broken config takes the box down."""
    nginx = webservers["nginx"]
    write_site(nginx, "example.com")

    result = invoke(["enable", "example.com"])

    assert result.exit_code == 0
    assert nginx.site_enabled("example.com")
    assert runner.calls == [("nginx", "-t"), ("systemctl", "reload", "nginx")]


def test_enable_falls_through_to_apache(webservers: dict[str, Any], runner: FakeRunner) -> None:
    """A site nginx does not have may still be an apache site."""
    apache = webservers["apache"]
    write_site(apache, "example.com")

    result = invoke(["enable", "example.com"])

    assert result.exit_code == 0
    assert runner.calls == [
        ("a2ensite", "example.com.conf"),
        ("apache2ctl", "configtest"),
        ("systemctl", "reload", "apache2"),
    ]


def test_enable_reports_an_unknown_site(webservers: dict[str, Any], runner: FakeRunner) -> None:
    """Nothing is reloaded when there is nothing to enable."""
    result = invoke(["enable", "example.com"])

    assert result.exit_code != 0
    assert isinstance(result.exception, Exception)
    assert "Site not found" in str(result.exception)
    assert runner.calls == []


def test_disable_unlinks_the_site(webservers: dict[str, Any], runner: FakeRunner) -> None:
    """Disabling leaves the configuration file where it was."""
    nginx = webservers["nginx"]
    config = write_site(nginx, "example.com", enabled=True)

    result = invoke(["disable", "example.com"])

    assert result.exit_code == 0
    assert not nginx.site_enabled("example.com")
    assert config.exists()
    assert runner.calls == [("nginx", "-t"), ("systemctl", "reload", "nginx")]


def test_disable_says_so_when_the_site_was_already_off(
    webservers: dict[str, Any], runner: FakeRunner
) -> None:
    """Disabling twice is not an error, and must not reload anything."""
    write_site(webservers["nginx"], "example.com")

    result = invoke(["disable", "example.com"])

    assert result.exit_code == 0
    assert "not enabled" in result.output
    assert runner.calls == []


def test_list_shows_the_sites_of_both_web_servers(
    webservers: dict[str, Any], runner: FakeRunner
) -> None:
    """One table, both backends, so an operator sees the whole machine."""
    write_site(webservers["nginx"], "nginx-site.com")
    write_site(webservers["apache"], "apache-site.com")

    result = invoke(["list"])

    assert result.exit_code == 0
    printed = result.output
    assert "nginx-site.com" in printed
    assert "apache-site.com" in printed


def test_list_can_be_narrowed_to_one_web_server(
    webservers: dict[str, Any], runner: FakeRunner
) -> None:
    """-w nginx must not report the apache sites."""
    write_site(webservers["nginx"], "nginx-site.com")
    write_site(webservers["apache"], "apache-site.com")

    result = invoke(["list", "-w", "nginx"])

    assert result.exit_code == 0
    printed = result.output
    assert "nginx-site.com" in printed
    assert "apache-site.com" not in printed


def test_list_of_an_empty_server_is_not_an_error(
    webservers: dict[str, Any], runner: FakeRunner
) -> None:
    """A fresh machine has no sites, which is not a failure."""
    result = invoke(["list"])

    assert result.exit_code == 0
    assert "No sites found" in result.output


def test_show_prints_the_configuration_file(webservers: dict[str, Any], runner: FakeRunner) -> None:
    """``site cat`` is what an operator reaches for before editing by hand."""
    write_site(webservers["nginx"], "example.com")

    result = invoke(["show", "example.com"])

    assert result.exit_code == 0
    assert "# example.com" in result.output


def test_show_reports_an_unknown_site(webservers: dict[str, Any], runner: FakeRunner) -> None:
    """A missing site is an error with a hint, not an empty stdout."""
    result = invoke(["show", "example.com"])

    assert result.exit_code != 0
    assert "Site not found" in str(result.exception)


# ---------------------------------------------------------------------------
# Deletion asks first, and says exactly what it will destroy
# ---------------------------------------------------------------------------


def test_delete_names_the_domain_and_the_consequences_before_asking(
    webservers: dict[str, Any], runner: FakeRunner, certs: FakeCertManager
) -> None:
    """'Are you sure?' tells an operator nothing at three in the morning."""
    config = write_site(webservers["nginx"], "example.com", enabled=True)

    result = invoke(["delete", "example.com"], input="n\n")

    assert result.exit_code == 0
    assert "example.com" in result.output
    assert "certificate" in result.output.lower()
    assert config.exists()
    assert runner.calls == []
    assert certs.deleted == []


def test_delete_removes_the_site_and_its_certificate_when_confirmed(
    webservers: dict[str, Any], runner: FakeRunner, certs: FakeCertManager
) -> None:
    """A deleted site must not leave a certificate renewing forever."""
    certs.has_cert = True
    config = write_site(webservers["nginx"], "example.com", enabled=True)

    result = invoke(["delete", "example.com"], input="y\n")

    assert result.exit_code == 0
    assert not config.exists()
    assert certs.deleted == ["example.com"]
    assert runner.calls == [("nginx", "-t"), ("systemctl", "reload", "nginx")]


@pytest.mark.parametrize("flag", ["--force", "-f", "-y"])
def test_delete_skips_the_prompt_when_forced(
    flag: str, webservers: dict[str, Any], runner: FakeRunner, certs: FakeCertManager
) -> None:
    """Scripts pass -y, and all three spellings have always worked."""
    config = write_site(webservers["nginx"], "example.com")

    result = invoke(["delete", "example.com", flag])

    assert result.exit_code == 0
    assert not config.exists()


def test_delete_reports_an_unknown_site(
    webservers: dict[str, Any], runner: FakeRunner, certs: FakeCertManager
) -> None:
    """Confirming the deletion of a site that is not there is still an error."""
    result = invoke(["delete", "example.com", "--force"])

    assert result.exit_code != 0
    assert "Site not found" in str(result.exception)


# ---------------------------------------------------------------------------
# The argparse entry point keeps working until the parser is cut over
# ---------------------------------------------------------------------------


def test_the_legacy_handler_runs_the_same_code(
    webservers: dict[str, Any], runner: FakeRunner
) -> None:
    """``wasm.cli.parser`` and the interactive menu still dispatch through it."""
    from argparse import Namespace

    write_site(webservers["nginx"], "example.com")

    exit_code = site_cli.handle_site(
        Namespace(action="enable", domain="example.com", verbose=False)
    )

    assert exit_code == 0
    assert webservers["nginx"].site_enabled("example.com")
    assert runner.calls == [("nginx", "-t"), ("systemctl", "reload", "nginx")]


def test_the_legacy_handler_turns_a_wasm_error_into_an_exit_code(
    webservers: dict[str, Any], runner: FakeRunner
) -> None:
    """The argparse path reports failures as exit codes, as it always has."""
    from argparse import Namespace

    exit_code = site_cli.handle_site(Namespace(action="show", domain="example.com", verbose=False))

    assert exit_code == 1
