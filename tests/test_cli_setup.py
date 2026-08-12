# Copyright (c) 2024-2026 Yago Lopez Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
Tests for the ``wasm setup`` command group after the move to Click.

Two things are pinned here. The first is the command surface: every command and
option the argparse tree published is in scripts and in the documentation, so
the contract in ``tests/contracts/cli_surface.json`` is checked directly rather
than by eye. The second is that ``setup init`` tells the truth: it used to print
"Setup Complete!" and exit 0 after failing every single install, on every
distribution that is not Debian.
"""

from __future__ import annotations

import json
import sys
from argparse import Namespace
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import click
import pytest
from click.testing import CliRunner

from wasm.cli import app as app_module
from wasm.cli.app import cli as root_cli
from wasm.cli.commands import setup as setup_cmd
from wasm.core.fs import DryRunFileSystem, get_fs, set_fs
from wasm.core.logger import Logger
from wasm.core.runner import DryRunRunner, FakeRunner, get_runner

CONTRACT = json.loads(
    (Path(__file__).parent / "contracts/cli_surface.json").read_text(encoding="utf-8")
)

#: Command paths the frozen surface promises under this subtree.
CONTRACT_COMMANDS = sorted(key for key in CONTRACT if key == "setup" or key.startswith("setup "))

#: Flags the root group owns. A subcommand may re-offer them, never own them.
GLOBAL_OPTS = {"-v", "--verbose", "--dry-run", "--no-color", "--json"}


@dataclass
class Invocation:
    """
    What one run of the CLI produced.

    Attributes:
        exit_code: The status the process would have exited with.
        output: Everything the user would have seen.
        exception: The exception that escaped, if any.
    """

    exit_code: int
    output: str
    exception: BaseException | None


#: Type of the ``wasm`` fixture, for the signatures below.
Wasm = Callable[..., Invocation]


class _TestLogger(Logger):
    """
    A logger that writes wherever stdout points when it is built.

    :class:`Logger` binds its stream as a default argument, so every instance
    writes to the interpreter's stdout as it was at import time. Under a test
    that is neither the stream Click's runner reads back nor the one pytest
    hands to capsys, which would make every assertion on output vacuous.
    """

    def __init__(
        self,
        verbose: bool = False,
        no_color: bool = False,
        log_file: Path | None = None,
        stream: Any = None,
    ):
        """
        Args:
            verbose: Show debug messages.
            no_color: Disable colour.
            log_file: Optional file to mirror output into.
            stream: Where to write; defaults to stdout as it is right now.
        """
        super().__init__(
            verbose=verbose,
            no_color=no_color,
            log_file=log_file,
            stream=stream if stream is not None else sys.stdout,
        )


@pytest.fixture(autouse=True)
def _readable_output(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Make everything a command prints readable by the test.

    Args:
        monkeypatch: Patching helper, scoped to the test.
    """
    monkeypatch.setattr(app_module, "Logger", _TestLogger)
    monkeypatch.setattr(setup_cmd, "Logger", _TestLogger)


@pytest.fixture(autouse=True)
def _real_filesystem() -> None:
    """
    Put the real filesystem back after every test.

    ``--dry-run`` installs a rehearsing filesystem process-wide, exactly like
    the command runner. Half the tests in this file pass that flag, so without
    this the first of them would silently disable every write in the rest of
    the session.
    """
    set_fs(None)
    try:
        yield
    finally:
        set_fs(None)


@pytest.fixture
def ssh_home(sandbox: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """
    Point the SSH helpers at an empty directory.

    ``wasm.validators.ssh`` resolves ~/.ssh once, when it is imported, so
    setting HOME is not enough to keep a test off the developer's own keys.

    Args:
        sandbox: Isolated filesystem root.
        monkeypatch: Patching helper, scoped to the test.

    Returns:
        The sandboxed .ssh directory, which does not exist yet.
    """
    import wasm.validators.ssh as ssh_module

    ssh_dir = sandbox / ".ssh"
    monkeypatch.setattr(ssh_module, "DEFAULT_SSH_DIR", ssh_dir)
    return ssh_dir


@pytest.fixture
def wasm() -> Wasm:
    """
    Run the real command tree the way the console script does.

    Returns:
        A callable taking the arguments after the program name.
    """
    cli_runner = CliRunner()

    def invoke(*args: str) -> Invocation:
        result = cli_runner.invoke(root_cli, list(args))
        return Invocation(
            exit_code=result.exit_code,
            output=result.output,
            exception=result.exception,
        )

    return invoke


class _FakeSummary(dict):
    """A setup summary with nothing installed."""


def _empty_summary() -> dict[str, Any]:
    """
    Build the summary a bare machine produces.

    Returns:
        The same shape :class:`~wasm.core.dependencies.DependencyChecker`
        returns.
    """
    return _FakeSummary(
        system_ready=False,
        webserver=None,
        nodejs={"installed": False, "version": None, "package_managers": {}},
        python={"installed": False, "version": None, "package_managers": {}},
        missing_required=[],
        missing_optional=[],
        recommendations=[],
    )


class _FakeChecker:
    """Stands in for the dependency checker, so no probing reaches the machine."""

    def __init__(self, verbose: bool = False, runner: Any = None):
        """
        Args:
            verbose: Ignored; matches the real signature.
            runner: Ignored; matches the real signature.
        """
        self.verbose = verbose

    def get_setup_summary(self) -> dict[str, Any]:
        """
        Report a machine with nothing installed.

        Returns:
            The summary.
        """
        return _empty_summary()

    def get_version(self, command: str, version_flag: str = "--version") -> str:
        """
        Report a plausible version for any command.

        Args:
            command: Program name.
            version_flag: Ignored.

        Returns:
            A version string.
        """
        return f"{command} 1.0"


@pytest.fixture
def prepared(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, Any]:
    """
    Point setup at a sandbox and make the machine look empty.

    Args:
        monkeypatch: Patching helper, scoped to the test.
        tmp_path: Per-test temporary directory.

    Returns:
        A dict holding the sandboxed paths and the set of installed programs,
        which a test mutates to describe the machine it wants.
    """
    installed: set[str] = set()
    monkeypatch.setattr(setup_cmd, "command_exists", lambda name: name in installed)
    monkeypatch.setattr(setup_cmd.os, "geteuid", lambda: 0)
    monkeypatch.setattr(setup_cmd, "DEFAULT_APPS_DIR", tmp_path / "var/www/apps")
    monkeypatch.setattr(setup_cmd, "DEFAULT_LOG_DIR", tmp_path / "var/log/wasm")
    monkeypatch.setattr(setup_cmd, "DEFAULT_CONFIG_PATH", tmp_path / "etc/wasm/config.yaml")
    monkeypatch.setattr(setup_cmd, "MAN_PAGE_DIR", tmp_path / "usr/share/man/man1")

    import wasm.core.config as core_config
    import wasm.core.dependencies as dependencies
    import wasm.core.utils as core_utils

    monkeypatch.setattr(dependencies, "DependencyChecker", _FakeChecker)
    monkeypatch.setattr(
        core_utils, "get_system_info", lambda: {"os": "Test Linux", "kernel": "6.0.0-test"}
    )

    saved: dict[str, Any] = {}
    # What Config.save() answers. A fixture that always says True cannot tell a
    # wizard that checks the result from one that ignores it, which is how the
    # unchecked save survived a review with green tests.
    outcome = {"save": True}

    class _FakeConfig:
        """Records what setup asked to persist, without touching /etc."""

        def set(self, key: str, value: Any) -> None:
            """
            Record a configuration value.

            Args:
                key: Dotted configuration key.
                value: Value to store.
            """
            saved[key] = value

        def save(self) -> bool:
            """
            Report what the test asked this save to do.

            Returns:
                True unless the test set ``prepared["save_succeeds"] = False``.
            """
            return outcome["save"]

    monkeypatch.setattr(core_config, "Config", _FakeConfig)

    class _Prepared(dict):
        """A dict whose ``save_succeeds`` key drives the fake Config."""

        def __setitem__(self, key: str, value: Any) -> None:
            """
            Store a value, forwarding the save outcome to the fake Config.

            Args:
                key: Setting name.
                value: New value.
            """
            if key == "save_succeeds":
                outcome["save"] = bool(value)
            super().__setitem__(key, value)

    return _Prepared(
        installed=installed,
        saved=saved,
        root=tmp_path,
        save_succeeds=True,
    )


# ---------------------------------------------------------------------------
# Command surface
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("command", CONTRACT_COMMANDS)
def test_every_contract_command_answers_help(wasm: Wasm, command: str) -> None:
    """Every command the argparse tree published still exists and documents itself."""
    result = wasm(*command.split(" "), "--help")

    assert result.exit_code == 0, result.output
    assert command.split(" ")[-1] in result.output or "Usage:" in result.output


@pytest.mark.parametrize("command", CONTRACT_COMMANDS)
def test_every_contract_option_survived(wasm: Wasm, command: str) -> None:
    """No option disappeared: they are in scripts that must keep running."""
    node: click.Command | None = root_cli
    ctx = click.Context(root_cli)
    for name in command.split(" "):
        assert isinstance(node, click.Group)
        node = node.get_command(ctx, name)
        assert node is not None, f"wasm {command} is gone"

    assert node is not None
    declared = {
        opt
        for param in node.params
        if isinstance(param, click.Option)
        for opt in param.opts + param.secondary_opts
    }
    assert set(CONTRACT[command]["options"]) <= declared


def test_no_subcommand_owns_a_global_flag() -> None:
    """
    A subcommand may re-offer a global flag; it may never hold the value.

    This is the defect the migration exists to remove: argparse let ``wasm site
    create`` reset the ``--dry-run`` the user asked for before the subcommand
    name. Here the flags are eager, never reach the command function, and only
    ever switch the shared context on.
    """
    ctx = click.Context(setup_cmd.cli)
    offenders: list[str] = []

    for name in setup_cmd.cli.list_commands(ctx):
        command = setup_cmd.cli.get_command(ctx, name)
        assert command is not None
        for param in command.params:
            if not isinstance(param, click.Option):
                continue
            if not GLOBAL_OPTS.intersection(param.opts):
                continue
            if param.expose_value or not param.is_eager:
                offenders.append(f"setup {name}: {param.opts}")

    assert offenders == []


def test_no_setup_command_offers_json(wasm: Wasm) -> None:
    """
    --json is not offered where nothing builds a structured payload.

    A flag that parses and then prints a table is worse than no flag: a script
    that trusts it breaks on output it cannot read.
    """
    ctx = click.Context(setup_cmd.cli)
    for name in setup_cmd.cli.list_commands(ctx):
        command = setup_cmd.cli.get_command(ctx, name)
        assert command is not None
        assert "--json" not in {opt for param in command.params for opt in param.opts}


def test_dry_run_after_the_subcommand_still_rehearses(wasm: Wasm, runner: FakeRunner) -> None:
    """`wasm setup permissions --dry-run` rehearses, it does not just parse."""
    wasm("setup", "permissions", "--dry-run")

    assert isinstance(get_runner(), DryRunRunner)


def test_dry_run_before_the_subcommand_is_not_undone(wasm: Wasm, runner: FakeRunner) -> None:
    """
    The flag survives the subcommand, which is the whole point of the move.

    Under argparse the subparser's own default overwrote it, so
    ``wasm --dry-run monitor scan`` ran for real.
    """
    wasm("--dry-run", "setup", "permissions")

    assert isinstance(get_runner(), DryRunRunner)


def test_unknown_option_is_a_usage_error(wasm: Wasm) -> None:
    """A typo gets a usage message and exit code 2, never a traceback."""
    result = wasm("setup", "doctor", "--wat")

    assert result.exit_code == 2
    assert "no such option" in result.output.lower()


def test_unknown_subcommand_is_a_usage_error(wasm: Wasm) -> None:
    """`wasm setup nonsense` fails as a usage error."""
    result = wasm("setup", "nonsense")

    assert result.exit_code == 2


def test_bare_setup_lists_its_commands(wasm: Wasm) -> None:
    """`wasm setup` with no action shows what it can do."""
    result = wasm("setup")

    assert "doctor" in result.output
    assert "completions" in result.output


# ---------------------------------------------------------------------------
# Values are validated before anything is touched
# ---------------------------------------------------------------------------


def test_unsupported_shell_is_rejected(wasm: Wasm, runner: FakeRunner) -> None:
    """An unsupported shell is a usage error, not a half-written file."""
    result = wasm("setup", "completions", "--shell", "powershell")

    assert result.exit_code == 2
    assert "powershell" in result.output
    assert runner.calls == []


def test_unsupported_key_type_is_rejected(wasm: Wasm, runner: FakeRunner) -> None:
    """ssh-keygen is never reached with an algorithm it does not know."""
    result = wasm("setup", "ssh", "--type", "dsa")

    assert result.exit_code == 2
    assert runner.calls == []


# ---------------------------------------------------------------------------
# setup completions
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("shell", "marker"),
    [
        # Click asks bash for its version before emitting the script, which is
        # a real process and the only reason for the opt-out.
        pytest.param("bash", "_WASM_COMPLETE=bash_complete", marks=pytest.mark.allow_subprocess),
        pytest.param("zsh", "#compdef wasm"),
        pytest.param("fish", "_WASM_COMPLETE=fish_complete"),
    ],
)
def test_completions_are_generated_from_the_tree(wasm: Wasm, shell: str, marker: str) -> None:
    """The script comes from Click, so it cannot fall behind the commands."""
    result = wasm("setup", "completions", "--shell", shell, "--stdout")

    assert result.exit_code == 0
    assert marker in result.output


@pytest.mark.allow_subprocess
def test_completions_cover_a_subcommand_added_after_the_handwritten_scripts(
    wasm: Wasm,
) -> None:
    """
    Completion is generated, so it knows commands no handwritten file listed.

    The shipped scripts were synchronised with 108 subcommands by memory. This
    asserts the generator is wired to the real tree instead.
    """
    result = wasm("setup", "completions", "--shell", "bash", "--stdout")

    assert "complete" in result.output
    assert "wasm" in result.output


@pytest.mark.allow_subprocess
def test_completions_install_for_the_user(wasm: Wasm, sandbox: Path) -> None:
    """--user-only writes where the shell looks, and needs no root."""
    result = wasm("setup", "completions", "--shell", "bash", "--user-only")

    target = sandbox / ".local/share/bash-completion/completions/wasm"
    assert result.exit_code == 0, result.output
    assert target.read_text(encoding="utf-8") == setup_cmd.completion_source("bash")
    assert "source" in result.output


def test_completions_zsh_install_explains_fpath(wasm: Wasm, sandbox: Path) -> None:
    """A zsh user is told the one thing that makes the file take effect."""
    result = wasm("setup", "completions", "--shell", "zsh", "--user-only")

    assert result.exit_code == 0, result.output
    assert (sandbox / ".zsh/completions/_wasm").exists()
    assert "fpath" in result.output
    assert "compinit" in result.output


def test_system_wide_completions_need_root(wasm: Wasm, monkeypatch: pytest.MonkeyPatch) -> None:
    """Without root it says how to proceed instead of dying on a write."""
    monkeypatch.setattr(setup_cmd.os, "geteuid", lambda: 1000)

    result = wasm("setup", "completions", "--shell", "fish")

    assert result.exit_code == 1
    assert "--user-only" in result.output


def test_completions_without_a_detectable_shell(
    wasm: Wasm, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When $SHELL says nothing useful, the error names the fix."""
    monkeypatch.setenv("SHELL", "/usr/bin/ksh")

    result = wasm("setup", "completions")

    assert result.exit_code == 1
    assert "--shell" in result.output


# ---------------------------------------------------------------------------
# setup init
# ---------------------------------------------------------------------------


def test_init_requires_root(wasm: Wasm, monkeypatch: pytest.MonkeyPatch) -> None:
    """Without root it stops before touching anything, and says how to retry."""
    monkeypatch.setattr(setup_cmd.os, "geteuid", lambda: 1000)

    result = wasm("setup", "init")

    assert result.exit_code == 1
    assert "sudo wasm setup init" in result.output


def test_init_says_so_when_no_package_manager_is_supported(
    wasm: Wasm, runner: FakeRunner, prepared: dict[str, Any]
) -> None:
    """
    On a distribution WASM cannot install for, it says which ones it can.

    The previous version assumed apt-get and reported progress while every
    install failed silently.
    """
    result = wasm("setup", "init", "--yes")

    assert result.exit_code == 1
    assert "apt-get" in result.output
    assert "dnf" in result.output
    assert runner.calls == []


def test_init_installs_with_apt(wasm: Wasm, runner: FakeRunner, prepared: dict[str, Any]) -> None:
    """On a Debian family machine the exact apt argv is what gets run."""
    prepared["installed"].add("apt-get")

    result = wasm("setup", "init", "--yes")

    assert result.exit_code == 0, result.output
    assert ("apt-get", "update") in runner.calls
    assert ("apt-get", "install", "-y", "git") in runner.calls
    assert ("apt-get", "install", "-y", "nginx") in runner.calls


def test_init_installs_with_dnf(wasm: Wasm, runner: FakeRunner, prepared: dict[str, Any]) -> None:
    """On Fedora it uses dnf and the Fedora package names, not apt."""
    prepared["installed"].add("dnf")

    result = wasm("setup", "init", "--yes")

    assert result.exit_code == 0, result.output
    assert ("dnf", "install", "-y", "git") in runner.calls
    assert not any(call[0] == "apt-get" for call in runner.calls)


def test_init_starts_the_web_server_under_its_real_unit_name(
    wasm: Wasm, runner: FakeRunner, prepared: dict[str, Any]
) -> None:
    """Apache is apache2 on Debian and httpd on Fedora; the unit name follows."""
    prepared["installed"].add("dnf")

    result = wasm("setup", "init", "--yes")

    assert result.exit_code == 0, result.output
    assert ("systemctl", "enable", "nginx") in runner.calls


def test_init_reports_failure_instead_of_claiming_success(
    wasm: Wasm, runner: FakeRunner, prepared: dict[str, Any]
) -> None:
    """
    A failed install is a failed setup.

    This is the regression that mattered: the wizard printed "Setup Complete!"
    and exited 0 with nothing installed.
    """
    prepared["installed"].add("apt-get")
    runner.script(["apt-get", "install"], stderr="E: Unable to locate package", exit_code=100)

    result = wasm("setup", "init", "--yes")

    assert result.exit_code == 1
    assert "Setup finished with problems" in result.output
    assert "Setup complete" not in result.output
    assert "Unable to locate package" in result.output


def test_init_creates_the_directories_it_owns(
    wasm: Wasm, runner: FakeRunner, prepared: dict[str, Any]
) -> None:
    """The apps, log and config directories exist afterwards."""
    prepared["installed"].add("apt-get")

    result = wasm("setup", "init", "--yes")

    root = prepared["root"]
    assert result.exit_code == 0, result.output
    assert (root / "var/www/apps").is_dir()
    assert (root / "var/log/wasm").is_dir()
    assert (root / "etc/wasm").is_dir()


def test_init_keeps_the_config_directory_private(
    wasm: Wasm, runner: FakeRunner, prepared: dict[str, Any]
) -> None:
    """config.yaml holds credentials, so its directory stays owner-only."""
    prepared["installed"].add("apt-get")

    wasm("setup", "init", "--yes")

    mode = (prepared["root"] / "etc/wasm").stat().st_mode
    assert mode & 0o077 == 0


def test_init_records_the_web_server_the_deployers_understand(
    wasm: Wasm, runner: FakeRunner, prepared: dict[str, Any]
) -> None:
    """The value written is the one the deployers compare against."""
    prepared["installed"].add("apt-get")

    wasm("setup", "init", "--yes")

    assert prepared["saved"]["webserver"] in ("nginx", "apache")


@pytest.mark.parametrize(
    ("detected", "expected"),
    [("apache2", "apache"), ("httpd", "apache"), ("nginx", "nginx"), (None, "nginx")],
)
def test_the_web_server_name_is_the_one_the_deployers_compare_against(
    detected: str | None, expected: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    The dependency checker reports a package name; config holds a WASM name.

    Writing "apache2" into config.yaml configured the machine for a web server
    the deployers, which compare against "apache", never recognise.
    """
    monkeypatch.setattr(setup_cmd, "command_exists", lambda name: False)
    summary = _empty_summary()
    summary["webserver"] = detected

    assert setup_cmd._default_choices(summary)["webserver_choice"] == expected


def test_init_with_yes_asks_nothing(
    wasm: Wasm, runner: FakeRunner, prepared: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """--yes is what makes the wizard usable from a provisioning script."""
    prepared["installed"].add("apt-get")

    def _refuse(summary: Any) -> None:
        raise AssertionError("--yes must not prompt")

    monkeypatch.setattr(setup_cmd, "_interactive_setup_prompts", _refuse)
    monkeypatch.setattr(setup_cmd, "_prompts_possible", lambda: True)

    assert wasm("setup", "init", "--yes").exit_code == 0


def test_init_cancelled_at_a_prompt_changes_nothing(
    wasm: Wasm, runner: FakeRunner, prepared: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Answering Ctrl-C to a question stops before the first install."""
    prepared["installed"].add("apt-get")
    monkeypatch.setattr(setup_cmd, "_prompts_possible", lambda: True)
    monkeypatch.setattr(setup_cmd, "_interactive_setup_prompts", lambda summary: None)

    result = wasm("setup", "init")

    assert result.exit_code == 130
    assert not any(call[0] == "apt-get" for call in runner.calls)


# ---------------------------------------------------------------------------
# setup ssh
# ---------------------------------------------------------------------------


def test_ssh_without_a_key_says_how_to_make_one(wasm: Wasm, ssh_home: Path) -> None:
    """No key and no --generate is an error that names the command to run."""
    result = wasm("setup", "ssh")

    assert result.exit_code == 1
    assert "wasm setup ssh --generate" in result.output


def test_ssh_generate_builds_the_expected_keygen_command(
    wasm: Wasm, runner: FakeRunner, ssh_home: Path
) -> None:
    """The algorithm the user asked for is the one ssh-keygen is given."""
    wasm("setup", "ssh", "--generate", "--type", "rsa")

    keygen = [call for call in runner.calls if call[0] == "ssh-keygen"]
    assert keygen, runner.calls
    assert keygen[0][:3] == ("ssh-keygen", "-t", "rsa")


def test_ssh_test_failure_is_reported(wasm: Wasm, runner: FakeRunner, ssh_home: Path) -> None:
    """A failed connection test exits non-zero so a script can react."""
    ssh_home.mkdir()
    (ssh_home / "id_ed25519").write_text("key", encoding="utf-8")
    (ssh_home / "id_ed25519.pub").write_text("ssh-ed25519 AAAA test", encoding="utf-8")
    runner.script(["ssh"], stderr="Permission denied (publickey).", exit_code=255)

    result = wasm("setup", "ssh", "--test", "github.com")

    assert result.exit_code == 1
    assert "github.com" in result.output


# ---------------------------------------------------------------------------
# setup doctor and setup permissions
# ---------------------------------------------------------------------------


def test_doctor_fails_when_a_requirement_is_missing(
    wasm: Wasm, runner: FakeRunner, prepared: dict[str, Any]
) -> None:
    """Missing git is an issue, and an issue is a non-zero exit."""
    result = wasm("setup", "doctor")

    assert result.exit_code == 1
    assert "git" in result.output


def test_doctor_suggests_the_local_package_manager(
    wasm: Wasm, runner: FakeRunner, prepared: dict[str, Any]
) -> None:
    """The fix it prints is one the operator can paste on this machine."""
    prepared["installed"].add("zypper")

    result = wasm("setup", "doctor")

    assert "zypper --non-interactive install git" in result.output


def test_doctor_passes_on_a_prepared_machine(
    wasm: Wasm, runner: FakeRunner, prepared: dict[str, Any]
) -> None:
    """With everything in place, doctor exits 0."""
    prepared["installed"].update(
        {"apt-get", "git", "curl", "nginx", "node", "npm", "certbot", "python3", "pip3"}
    )
    (prepared["root"] / "var/www/apps").mkdir(parents=True)
    (prepared["root"] / "var/log/wasm").mkdir(parents=True)
    (prepared["root"] / "etc/wasm").mkdir(parents=True)
    (prepared["root"] / "etc/wasm/config.yaml").write_text("webserver: nginx", encoding="utf-8")
    runner.script(["systemctl", "is-active"], stdout="active\n")

    result = wasm("setup", "doctor")

    assert result.exit_code == 0, result.output


def test_permissions_reports_missing_directories(
    wasm: Wasm, runner: FakeRunner, prepared: dict[str, Any]
) -> None:
    """The check names the directory and the command that creates it."""
    result = wasm("setup", "permissions")

    assert result.exit_code == 0
    assert "sudo wasm setup init" in result.output


# ---------------------------------------------------------------------------
# The argparse entry point still works while it is still wired up
# ---------------------------------------------------------------------------


def test_handle_setup_still_dispatches(runner: FakeRunner, prepared: dict[str, Any]) -> None:
    """Both entry points call the same implementation, so neither drifts."""
    assert setup_cmd.handle_setup(Namespace(action="permissions", verbose=False)) == 0


def test_handle_setup_rejects_an_unknown_action(runner: FakeRunner) -> None:
    """An action that does not exist is an error, not a silent success."""
    assert setup_cmd.handle_setup(Namespace(action="teleport", verbose=False)) == 1


# ---------------------------------------------------------------------------
# setup init tells the truth about the configuration file
# ---------------------------------------------------------------------------


def test_init_fails_when_the_configuration_cannot_be_saved(
    wasm: Wasm, runner: FakeRunner, prepared: dict[str, Any]
) -> None:
    """
    A configuration that was not written is a setup that did not finish.

    ``Config.save()`` reports failure by returning False, having logged the
    reason. Ignoring that result is how the wizard came to announce a config
    file it never managed to write, and exit 0 while doing it.
    """
    prepared["installed"].add("apt-get")
    prepared["save_succeeds"] = False

    result = wasm("setup", "init", "--yes")

    assert result.exit_code == 1
    assert "Setup complete" not in result.output
    assert "Setup finished with problems" in result.output
    assert str(prepared["root"] / "etc/wasm/config.yaml") in result.output


def test_init_does_not_claim_to_have_written_a_configuration_it_did_not(
    wasm: Wasm, runner: FakeRunner, prepared: dict[str, Any]
) -> None:
    """The failure path must not print Created or Updated for that same file."""
    prepared["installed"].add("apt-get")
    prepared["save_succeeds"] = False
    config_path = str(prepared["root"] / "etc/wasm/config.yaml")

    output = wasm("setup", "init", "--yes").output

    claims = [line for line in output.splitlines() if config_path in line]
    assert claims, output
    assert not any("Created" in line or "Updated" in line for line in claims), claims


def test_init_succeeds_when_the_configuration_is_saved(
    wasm: Wasm, runner: FakeRunner, prepared: dict[str, Any]
) -> None:
    """
    The other half of the pair, so the test above cannot pass for free.

    Without this, "always fails" would satisfy the assertion just as well as
    "reports what happened".
    """
    prepared["installed"].add("apt-get")
    prepared["save_succeeds"] = True

    result = wasm("setup", "init", "--yes")

    assert result.exit_code == 0, result.output
    assert "Setup complete" in result.output


def test_init_reports_every_failed_install_by_name(
    wasm: Wasm, runner: FakeRunner, prepared: dict[str, Any]
) -> None:
    """
    All installs failing must not be reported as a complete setup.

    This is the original defect in its strongest form: nothing was installed,
    and the wizard said "Setup Complete!" and exited 0.
    """
    prepared["installed"].add("apt-get")
    runner.script(["apt-get", "install"], stderr="E: Unable to locate package", exit_code=100)

    result = wasm("setup", "init", "--yes")

    assert result.exit_code == 1
    assert "Git" in result.output
    assert "nginx" in result.output
    assert "Certbot" in result.output
    assert "step(s) did not complete" in result.output


# ---------------------------------------------------------------------------
# --dry-run changes nothing, including what setup writes
# ---------------------------------------------------------------------------


def _tree(root: Path) -> set[Path]:
    """
    List everything under a directory.

    Args:
        root: Directory to walk.

    Returns:
        Every path below ``root``, relative paths included.
    """
    return set(root.rglob("*"))


@pytest.mark.parametrize("where", ["before", "after"])
def test_init_dry_run_creates_nothing(
    wasm: Wasm, runner: FakeRunner, prepared: dict[str, Any], where: str
) -> None:
    """
    ``setup init --dry-run`` must leave the machine exactly as it found it.

    It used to create /etc/wasm, write config.yaml and install the man page,
    because the flag only swapped the command runner and every one of those is
    a plain filesystem call. The flag is checked on both sides of the
    subcommand name: when it came after, setup turned the rehearsal on through
    its own copy of the wiring, which swapped one seam and not the other.
    """
    prepared["installed"].add("apt-get")
    root = prepared["root"]
    before = _tree(root)

    argv = ("--dry-run", "setup", "init", "--yes")
    if where == "after":
        argv = ("setup", "init", "--yes", "--dry-run")
    result = wasm(*argv)

    assert isinstance(get_fs(), DryRunFileSystem)
    assert _tree(root) == before, "a rehearsal changed the filesystem"
    assert not (root / "etc").exists()
    assert not (root / "var").exists()
    assert not (root / "usr").exists()
    assert result.exit_code == 0, result.output


def test_init_dry_run_does_not_overwrite_an_existing_configuration(
    wasm: Wasm, runner: FakeRunner, prepared: dict[str, Any]
) -> None:
    """The credentials already on disk must survive the rehearsal untouched."""
    config_path = prepared["root"] / "etc/wasm/config.yaml"
    config_path.parent.mkdir(parents=True)
    config_path.write_text("webserver: apache\n", encoding="utf-8")
    prepared["installed"].add("apt-get")

    wasm("--dry-run", "setup", "init", "--yes")

    assert config_path.read_text(encoding="utf-8") == "webserver: apache\n"


def test_init_dry_run_says_what_it_would_have_written(
    wasm: Wasm, runner: FakeRunner, prepared: dict[str, Any]
) -> None:
    """A rehearsal that reports nothing is indistinguishable from a no-op."""
    prepared["installed"].add("apt-get")

    output = wasm("--dry-run", "setup", "init", "--yes").output

    assert "would create directory" in output
    assert str(prepared["root"] / "var/www/apps") in output


def test_completions_dry_run_installs_nothing(wasm: Wasm, sandbox: Path) -> None:
    """The completion script is a file too, and a rehearsal must not write it."""
    result = wasm("--dry-run", "setup", "completions", "--shell", "zsh", "--user-only")

    target = sandbox / ".zsh/completions/_wasm"
    assert result.exit_code == 0, result.output
    assert not target.exists()
    assert not target.parent.exists()


def test_dry_run_after_the_subcommand_swaps_the_filesystem_too(
    wasm: Wasm, runner: FakeRunner
) -> None:
    """
    Both seams, or the flag is a promise the program cannot keep.

    ``setup`` re-offers the global flags so they keep parsing after the
    subcommand name. Its own copy of the wiring swapped the command runner
    only, which is precisely the defect the seam exists to remove.
    """
    wasm("setup", "permissions", "--dry-run")

    assert isinstance(get_runner(), DryRunRunner)
    assert isinstance(get_fs(), DryRunFileSystem)


def test_man_page_is_installed_through_the_seam(
    wasm: Wasm, runner: FakeRunner, prepared: dict[str, Any]
) -> None:
    """
    A real run still installs the man page; only the rehearsal does not.

    Without this, routing the copy through the seam could have quietly stopped
    installing anything at all and the dry-run test above would still pass.
    """
    source = Path(setup_cmd.__file__).resolve().parents[4] / "man" / "wasm.1"
    prepared["installed"].add("apt-get")

    wasm("setup", "init", "--yes")

    installed = prepared["root"] / "usr/share/man/man1/wasm.1"
    assert installed.read_text(encoding="utf-8") == source.read_text(encoding="utf-8")
    assert ("mandb", "-q") in runner.calls
