# Copyright (c) 2024-2026 Yago Lopez Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
Tests for the ``wasm cert`` command tree.

What is pinned here is the surface an operator types. ``tests/contracts/
cli_surface.json`` froze every command, alias and option the argparse tree
offered, and those spellings live in scripts and in the published
documentation, so this file asserts that each one still resolves and still
reaches certbot with the same argument vector.

The other half is what Click now does instead of hand-written checks: a missing
required option, a value that is not a domain and a webroot that does not exist
are usage errors, reported before certbot, the filesystem or the store is
touched.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import click
import pytest
import yaml
from click.testing import CliRunner, Result

from wasm.cli.app import cli as root_cli
from wasm.cli.commands import cert as cert_module
from wasm.core.exceptions import CertificateError
from wasm.core.logger import Logger
from wasm.core.runner import FakeRunner
from wasm.managers.cert_manager import CertManager

CERTBOT_OUTPUT = (
    "Found the following certs:\n"
    "  Certificate Name: shop.tld\n"
    "    Domains: shop.tld www.shop.tld\n"
    "    Expiry Date: 2026-11-30 10:00:00+00:00 (VALID: 60 days)\n"
    "    Certificate Path: /etc/letsencrypt/live/shop.tld/fullchain.pem\n"
    "    Private Key Path: /etc/letsencrypt/live/shop.tld/privkey.pem\n"
)

#: Flags that belong to ``wasm`` itself. A subcommand that declares one of them
#: again is the shadowing defect the Click migration exists to remove: argparse
#: let the subparser default overwrite the value the user had already set.
GLOBAL_FLAGS = frozenset({"-v", "--verbose", "--dry-run", "--json", "--no-color"})

#: Every command of the group, with the alternative spellings the contract
#: freezes.
COMMANDS: dict[str, tuple[str, ...]] = {
    "create": ("new", "obtain"),
    "list": ("ls",),
    "info": ("show",),
    "renew": (),
    "revoke": (),
    "delete": ("remove", "rm"),
}


class _Store:
    """A store that records nothing, so a command never touches SQLite."""

    def update_site_ssl(self, **_kwargs: Any) -> None:
        """Accept and discard a TLS state update."""

    def get_app(self, _domain: str) -> None:
        """
        Look up an application record.

        Args:
            _domain: Domain name.

        Returns:
            Always None: no test here has a deployed application.
        """
        return None


@pytest.fixture
def log(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """
    Collect what the commands report to the operator.

    :class:`~wasm.core.logger.Logger` binds ``sys.stdout`` as a default
    argument when its module is imported, so its output does not travel through
    the stream ``CliRunner`` installs and cannot be read from the result. The
    write is intercepted instead.

    Args:
        monkeypatch: Patching helper, scoped to the test.

    Returns:
        The list the log lines are appended to.
    """
    lines: list[str] = []
    monkeypatch.setattr(
        Logger,
        "_write",
        lambda self, message, newline=True: lines.append(message),
    )
    return lines


@pytest.fixture
def live_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, runner: FakeRunner) -> Path:
    """
    Point the manager at a disposable letsencrypt tree and store.

    Args:
        tmp_path: Per-test temporary directory.
        monkeypatch: Patching helper, scoped to the test.
        runner: The fake command runner, installed process-wide.

    Returns:
        The directory that stands in for /etc/letsencrypt/live.
    """
    monkeypatch.setattr("wasm.managers.cert_manager.get_store", lambda: _Store())
    live = tmp_path / "letsencrypt/live"
    monkeypatch.setattr(CertManager, "LETSENCRYPT_DIR", tmp_path / "letsencrypt")
    monkeypatch.setattr(CertManager, "LIVE_DIR", live)
    return live


def _put_certificate_on_disk(live: Path, domain: str) -> None:
    """
    Create the files a live lineage has.

    Args:
        live: The directory standing in for /etc/letsencrypt/live.
        domain: Lineage name.
    """
    (live / domain).mkdir(parents=True, exist_ok=True)
    for name in ("fullchain.pem", "privkey.pem", "cert.pem", "chain.pem"):
        (live / domain / name).write_text("-----BEGIN CERTIFICATE-----\n")


def _invoke(args: list[str], **kwargs: Any) -> Result:
    """
    Run the whole CLI, as a user would.

    Args:
        args: Arguments after the program name.
        **kwargs: Passed to :meth:`CliRunner.invoke`, notably ``input``.

    Returns:
        The invocation result.
    """
    return CliRunner().invoke(root_cli, args, **kwargs)


def _certbot_calls(runner: FakeRunner, subcommand: str) -> list[tuple[str, ...]]:
    """
    Collect the certbot invocations of one kind.

    Args:
        runner: The fake command runner.
        subcommand: The certbot subcommand to filter on.

    Returns:
        The matching argument vectors, in order.
    """
    return [call for call in runner.calls if subcommand in call and "certbot" in call]


@pytest.fixture
def isolated_panel_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """
    Point the layered configuration at a per-test file.

    ``panel_url`` reads ``web.*`` through the Config singleton, which would
    otherwise read the developer's real ``/etc/wasm/config.yaml`` and make
    these tests depend on the machine they run on. The self-signed TLS pair
    path is pinned the same way, so a machine that has actually run
    ``wasm web start --self-signed`` does not turn "http" into "https" under
    a test.

    Args:
        tmp_path: Per-test temporary directory.
        monkeypatch: Patching helper, scoped to the test.

    Yields:
        The configuration file the test may write.
    """
    from wasm.cli.commands import web as web_module
    from wasm.core import config as config_module

    path = tmp_path / "config.yaml"
    monkeypatch.setattr(config_module, "DEFAULT_CONFIG_PATH", path)
    monkeypatch.setattr(web_module, "PANEL_TLS_CERT", tmp_path / "panel-tls" / "panel.crt")
    monkeypatch.setattr(web_module, "PANEL_TLS_KEY", tmp_path / "panel-tls" / "panel.key")
    config_module.Config.reset_instance()
    yield path
    config_module.Config.reset_instance()


def _configure_panel(path: Path, **settings: Any) -> None:
    """
    Declare a configured, reachable panel in the isolated config file.

    Args:
        path: The isolated configuration file.
        **settings: Overrides for the ``web`` section; ``enabled``, ``host``
            and ``port`` fall back to a plain local panel when not given.
    """
    from wasm.core.config import Config

    settings.setdefault("enabled", True)
    settings.setdefault("host", "127.0.0.1")
    settings.setdefault("port", 8080)
    path.write_text(yaml.safe_dump({"web": settings}), encoding="utf-8")
    Config.reset_instance()


# -- The surface --------------------------------------------------------------


@pytest.mark.parametrize("group", ["cert", "ssl", "certificate"])
def test_the_group_answers_to_every_name_it_ever_had(group: str) -> None:
    """'ssl' and 'certificate' are in scripts and in the documentation."""
    result = _invoke([group, "--help"])

    assert result.exit_code == 0
    assert "Usage: cli cert" in result.output


@pytest.mark.parametrize(
    "name",
    [name for canonical, aliases in COMMANDS.items() for name in (canonical, *aliases)],
)
def test_every_command_and_alias_documents_itself(name: str) -> None:
    """Losing one spelling is a breaking change for someone's script."""
    result = _invoke(["cert", name, "--help"])

    assert result.exit_code == 0
    assert result.output.startswith("Usage:")


@pytest.mark.parametrize(("canonical", "aliases"), COMMANDS.items())
def test_an_alias_is_the_same_command(canonical: str, aliases: tuple[str, ...]) -> None:
    """An alias must not become a copy that drifts from the original."""
    ctx = click.Context(cert_module.cli)
    target = cert_module.cli.get_command(ctx, canonical)

    assert target is not None
    for alias in aliases:
        assert cert_module.cli.get_command(ctx, alias) is target


def test_the_help_lists_the_canonical_names_only() -> None:
    """Six commands and their aliases would be twelve lines of noise."""
    result = _invoke(["cert", "--help"])

    for canonical in COMMANDS:
        assert f"  {canonical}" in result.output


def test_no_command_redeclares_a_global_flag() -> None:
    """
    The defect the migration removes.

    ``--dry-run`` was declared on the root parser and again on the subparsers,
    so argparse's subparser default overwrote the value the user asked for and
    a rehearsal ran for real.
    """
    ctx = click.Context(cert_module.cli)
    commands = [cert_module.cli, *(cert_module.cli.get_command(ctx, n) for n in COMMANDS)]

    for command in commands:
        assert command is not None
        declared = {
            opt
            for param in command.params
            if isinstance(param, click.Option)
            for opt in param.opts + param.secondary_opts
        }
        assert not declared & GLOBAL_FLAGS, f"{command.name} redeclares a global flag"


def test_a_global_flag_still_reaches_the_command(
    live_dir: Path, runner: FakeRunner, log: list[str]
) -> None:
    """
    ``wasm -v cert list`` sets the verbosity the command runs with.

    The flag is declared once, on the root, and read from the shared context.
    Nothing in this subtree can overwrite it.
    """
    runner.script(["sudo", "certbot", "certificates"], exit_code=1)

    assert _invoke(["ssl", "ls"]).exit_code == 0
    assert not [line for line in log if "Could not read" in line]

    log.clear()
    assert _invoke(["-v", "ssl", "ls"]).exit_code == 0
    assert [line for line in log if "Could not read" in line]


# -- Usage errors -------------------------------------------------------------


def test_create_without_a_domain_is_a_usage_error(runner: FakeRunner) -> None:
    """Click reports the missing option; it used to be an AttributeError."""
    result = _invoke(["cert", "create"])

    assert result.exit_code == 2
    assert "--domain" in result.output
    assert runner.calls == []


@pytest.mark.parametrize("command", ["info", "revoke", "delete"])
def test_a_command_without_its_domain_is_a_usage_error(command: str, runner: FakeRunner) -> None:
    """The domain is the whole argument of these three."""
    result = _invoke(["cert", command])

    assert result.exit_code == 2
    assert "DOMAIN" in result.output
    assert runner.calls == []


@pytest.mark.parametrize("domain", ["not a domain", "../../etc/passwd", "shop..tld", ""])
def test_a_domain_that_is_not_one_is_refused_before_certbot(
    domain: str, runner: FakeRunner
) -> None:
    """The lineage name becomes a directory under /etc/letsencrypt."""
    result = _invoke(["cert", "create", "-d", domain])

    assert result.exit_code == 2
    assert runner.calls == []


def test_a_webroot_that_does_not_exist_is_refused_before_certbot(
    runner: FakeRunner,
) -> None:
    """Certbot cannot write a challenge into a directory that is not there."""
    result = _invoke(["cert", "create", "-d", "shop.tld", "-w", "/nonexistent/webroot"])

    assert result.exit_code == 2
    assert runner.calls == []


def test_an_unknown_subcommand_is_a_usage_error(runner: FakeRunner) -> None:
    """A typo must not be read as a domain."""
    result = _invoke(["cert", "renewal"])

    assert result.exit_code == 2
    assert runner.calls == []


def test_certbot_missing_is_reported_with_the_command_that_installs_it(
    runner: FakeRunner,
) -> None:
    """A tool that is not installed is the most common first-run failure."""
    runner.only_knows("nginx")

    result = _invoke(["cert", "list"])

    assert result.exit_code != 0
    assert runner.calls == []
    assert isinstance(result.exception, CertificateError)
    assert "apt install certbot" in str(result.exception.details)


# -- What each command actually runs -----------------------------------------


def test_create_pins_the_lineage_and_carries_every_domain(
    live_dir: Path, runner: FakeRunner, tmp_path: Path
) -> None:
    """The argv is the contract between this CLI and certbot."""
    webroot = tmp_path / "webroot"
    webroot.mkdir()

    result = _invoke(
        [
            "cert",
            "obtain",
            "-d",
            "Shop.TLD",
            "--domain",
            "www.shop.tld",
            "-e",
            "ops@shop.tld",
            "-w",
            str(webroot),
        ]
    )

    assert result.exit_code == 0
    (issued,) = _certbot_calls(runner, "certonly")
    assert issued == (
        "sudo",
        "certbot",
        "certonly",
        "--cert-name",
        "shop.tld",
        "--email",
        "ops@shop.tld",
        "--non-interactive",
        "--agree-tos",
        "--webroot",
        "-w",
        str(webroot),
        "-d",
        "shop.tld",
        "-d",
        "www.shop.tld",
    )


def test_create_asks_for_the_plugin_the_operator_named(live_dir: Path, runner: FakeRunner) -> None:
    """--nginx and --apache choose how control of the domain is proved."""
    runner.script(["sudo", "certbot", "plugins"], stdout="* apache\nDescription: Apache\n")

    result = _invoke(["cert", "new", "-d", "shop.tld", "--apache"])

    assert result.exit_code == 0
    (issued,) = _certbot_calls(runner, "certonly")
    assert "--apache" in issued


def test_create_expands_only_when_asked(live_dir: Path, runner: FakeRunner) -> None:
    """--expand rewrites an existing certificate instead of leaving it alone."""
    _put_certificate_on_disk(live_dir, "shop.tld")
    runner.script(["sudo", "certbot", "certificates"], stdout=CERTBOT_OUTPUT)

    without = _invoke(["cert", "create", "-d", "shop.tld", "--standalone"])
    assert without.exit_code == 0
    assert _certbot_calls(runner, "certonly") == []

    with_flag = _invoke(["cert", "create", "-d", "shop.tld", "--standalone", "--expand"])
    assert with_flag.exit_code == 0
    (issued,) = _certbot_calls(runner, "certonly")
    assert "--expand" in issued
    assert "--standalone" in issued


def test_list_shows_what_certbot_reports(
    live_dir: Path, runner: FakeRunner, log: list[str]
) -> None:
    """The table is how an operator finds out what is about to expire."""
    runner.script(["sudo", "certbot", "certificates"], stdout=CERTBOT_OUTPUT)

    result = _invoke(["cert", "list"])

    assert result.exit_code == 0
    assert any("shop.tld" in line for line in log)
    assert any("2026-11-30" in line for line in log)


def test_list_says_so_when_there_is_nothing(
    live_dir: Path, runner: FakeRunner, log: list[str]
) -> None:
    """An empty table reads as a broken command."""
    runner.script(["sudo", "certbot", "certificates"], stdout="No certificates found.\n")

    result = _invoke(["ssl", "ls"])

    assert result.exit_code == 0
    assert any("No certificates found" in line for line in log)


def test_list_open_prints_the_configured_panel_url_without_a_display(
    live_dir: Path,
    runner: FakeRunner,
    log: list[str],
    isolated_panel_config: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--open`` prints the panel URL and never touches xdg-open without a display."""
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    runner.script(["sudo", "certbot", "certificates"], stdout="No certificates found.\n")
    _configure_panel(isolated_panel_config)

    result = _invoke(["cert", "list", "--open"])

    assert result.exit_code == 0
    assert any("http://127.0.0.1:8080/certificates" in line for line in log)
    assert not runner.calls_to("xdg-open")


def test_list_open_launches_xdg_open_when_a_display_is_present(
    live_dir: Path,
    runner: FakeRunner,
    log: list[str],
    isolated_panel_config: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With a display available, ``--open`` hands the URL to xdg-open."""
    monkeypatch.setenv("DISPLAY", ":0")
    runner.script(["sudo", "certbot", "certificates"], stdout="No certificates found.\n")
    _configure_panel(isolated_panel_config)

    result = _invoke(["cert", "list", "--open"])

    assert result.exit_code == 0
    assert runner.calls_to("xdg-open") == [("xdg-open", "http://127.0.0.1:8080/certificates")]


def test_list_open_without_a_configured_panel_warns_and_exits_clean(
    live_dir: Path,
    runner: FakeRunner,
    log: list[str],
    isolated_panel_config: Path,
) -> None:
    """The panel is off by default, so ``--open`` warns instead of guessing a URL."""
    runner.script(["sudo", "certbot", "certificates"], stdout="No certificates found.\n")

    result = _invoke(["cert", "list", "--open"])

    assert result.exit_code == 0
    assert any("not configured" in line for line in log)
    assert not runner.calls_to("xdg-open")


def test_info_reports_the_domains_and_the_validity_window(
    live_dir: Path, runner: FakeRunner, log: list[str]
) -> None:
    """``cert show`` answers 'what does this certificate actually cover'."""
    _put_certificate_on_disk(live_dir, "shop.tld")
    runner.script(["sudo", "certbot", "certificates"], stdout=CERTBOT_OUTPUT)
    runner.script(
        ["openssl", "x509"],
        stdout="notBefore=Sep  1 10:00:00 2026 GMT\nnotAfter=Nov 30 10:00:00 2026 GMT\n",
    )

    result = _invoke(["cert", "show", "shop.tld"])

    assert result.exit_code == 0
    assert any("www.shop.tld" in line for line in log)
    assert any("Nov 30 10:00:00 2026 GMT" in line for line in log)


def test_info_on_an_unknown_domain_fails_without_issuing(
    live_dir: Path, runner: FakeRunner
) -> None:
    """Reading is not a reason to obtain anything."""
    runner.script(["sudo", "certbot", "certificates"], stdout="No certificates found.\n")

    result = _invoke(["cert", "info", "other.tld"])

    assert result.exit_code != 0
    assert _certbot_calls(runner, "certonly") == []


def test_renew_names_the_lineage_when_a_domain_is_given(live_dir: Path, runner: FakeRunner) -> None:
    """Renewing one site must not renew, or skip, the others."""
    result = _invoke(["cert", "renew", "-d", "shop.tld", "--force"])

    assert result.exit_code == 0
    assert runner.calls == [
        (
            "sudo",
            "certbot",
            "renew",
            "--non-interactive",
            "--cert-name",
            "shop.tld",
            "--force-renewal",
        )
    ]


def test_renew_without_a_domain_warns_before_forcing_everything(
    live_dir: Path, runner: FakeRunner, log: list[str]
) -> None:
    """Forcing every lineage spends the rate limit of every domain."""
    result = _invoke(["cert", "renew", "--force"])

    assert result.exit_code == 0
    assert any("rate limit" in line for line in log)
    assert runner.calls == [("sudo", "certbot", "renew", "--non-interactive", "--force-renewal")]


# -- Destructive commands -----------------------------------------------------


def test_revoke_names_the_certificate_and_the_consequence(
    live_dir: Path, runner: FakeRunner
) -> None:
    """'Are you sure?' tells the operator nothing about what happens next."""
    _put_certificate_on_disk(live_dir, "shop.tld")

    result = _invoke(["cert", "revoke", "shop.tld"], input="y\n")

    assert result.exit_code == 0
    assert "Revoke the certificate for shop.tld" in result.output
    assert "Browsers will reject shop.tld" in result.output
    (revoked,) = _certbot_calls(runner, "revoke")
    assert revoked == (
        "sudo",
        "certbot",
        "revoke",
        "--cert-path",
        str(live_dir / "shop.tld/fullchain.pem"),
        "--non-interactive",
        "--delete-after-revoke",
    )


def test_revoke_can_keep_the_files(live_dir: Path, runner: FakeRunner) -> None:
    """The old --delete defaulted to on with no way to turn it off."""
    _put_certificate_on_disk(live_dir, "shop.tld")

    result = _invoke(["cert", "revoke", "shop.tld", "--keep-files"], input="y\n")

    assert result.exit_code == 0
    (revoked,) = _certbot_calls(runner, "revoke")
    assert "--delete-after-revoke" not in revoked


def test_declining_the_revocation_leaves_the_certificate_alone(
    live_dir: Path, runner: FakeRunner
) -> None:
    """Revocation cannot be undone, so the default answer is no."""
    _put_certificate_on_disk(live_dir, "shop.tld")

    result = _invoke(["cert", "revoke", "shop.tld"], input="\n")

    assert result.exit_code == 0
    assert _certbot_calls(runner, "revoke") == []


def test_delete_asks_before_removing_the_files(live_dir: Path, runner: FakeRunner) -> None:
    """Deleting is not revoking, and the prompt has to say which one it is."""
    _put_certificate_on_disk(live_dir, "shop.tld")

    result = _invoke(["cert", "rm", "shop.tld"], input="y\n")

    assert result.exit_code == 0
    assert "Delete the certificate files for shop.tld" in result.output
    assert "It is not revoked" in result.output
    assert _certbot_calls(runner, "delete") == [
        ("sudo", "certbot", "delete", "--cert-name", "shop.tld", "--non-interactive")
    ]


def test_declining_the_deletion_leaves_the_files_alone(live_dir: Path, runner: FakeRunner) -> None:
    """An empty answer at the prompt is 'no'."""
    _put_certificate_on_disk(live_dir, "shop.tld")

    result = _invoke(["cert", "remove", "shop.tld"], input="\n")

    assert result.exit_code == 0
    assert _certbot_calls(runner, "delete") == []


def test_delete_force_does_not_ask(live_dir: Path, runner: FakeRunner) -> None:
    """--force is what the unattended callers use."""
    _put_certificate_on_disk(live_dir, "shop.tld")

    result = _invoke(["cert", "delete", "shop.tld", "-f"])

    assert result.exit_code == 0
    assert "Delete the certificate files" not in result.output
    assert _certbot_calls(runner, "delete") != []
