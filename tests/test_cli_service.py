"""
Surface and routing tests for ``wasm service``.

Two things are being pinned here. The first is the command surface: every
subcommand and every historical spelling of it is in scripts and in the
published documentation, so a name that stops resolving is a breaking change.
The second is that the group reaches systemd only through
:class:`~wasm.managers.service_manager.ServiceManager`, which is where the
ownership guard lives; ``service logs --follow`` used to shell out directly and
therefore ran with no deadline, no dry run and no ownership check.
"""

from __future__ import annotations

import ast
import io
from argparse import Namespace
from pathlib import Path

import click
import pytest
from click.testing import CliRunner, Result

from wasm.cli.app import cli as root
from wasm.cli.app import main as app_main
from wasm.cli.commands import service as service_module
from wasm.core.logger import Logger
from wasm.core.runner import FakeRunner
from wasm.core.store import Service
from wasm.managers.service_manager import WASM_UNIT_MARKER, ServiceManager

#: Flags the root group owns. A subcommand that declares one of them shadows
#: the value the user set before the subcommand name, which is the defect the
#: move to Click exists to remove.
GLOBAL_FLAGS = frozenset({"--verbose", "-v", "--dry-run", "--json", "--no-color"})

#: Canonical subcommand name to every spelling that must still reach it.
SPELLINGS: dict[str, tuple[str, ...]] = {
    "create": ("create",),
    "list": ("list", "ls"),
    "status": ("status", "info"),
    "start": ("start",),
    "stop": ("stop",),
    "restart": ("restart",),
    "logs": ("logs",),
    "delete": ("delete", "remove", "rm"),
}

ALL_SPELLINGS = [name for names in SPELLINGS.values() for name in names]


class FakeStore:
    """In-memory stand-in for the SQLite store."""

    def __init__(self) -> None:
        self.services: dict[str, Service] = {}
        self.deleted: list[str] = []

    def list_services(self, **_kwargs: object) -> list[Service]:
        """Return every recorded service."""
        return list(self.services.values())

    def get_service(self, name: str) -> Service | None:
        """Return a service by name, or None."""
        return self.services.get(name)

    def create_service(self, service: Service) -> Service:
        """Record a new service."""
        self.services[service.name] = service
        return service

    def update_service(self, service: Service) -> Service:
        """Replace a recorded service."""
        self.services[service.name] = service
        return service

    def update_service_status(self, name: str, status: str, **_kwargs: object) -> bool:
        """Record a status change."""
        service = self.services.get(name)
        if service is None:
            return False
        service.status = status
        return True

    def delete_service(self, name: str) -> bool:
        """Forget a service."""
        self.deleted.append(name)
        return self.services.pop(name, None) is not None


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> FakeStore:
    """
    Replace the SQLite store the service manager talks to.

    Args:
        monkeypatch: Patching helper, scoped to the test.

    Returns:
        The in-memory store.
    """
    fake = FakeStore()
    monkeypatch.setattr("wasm.managers.service_manager.get_store", lambda: fake)
    return fake


@pytest.fixture
def unit_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """
    Point the manager's unit directories at a temporary tree.

    Args:
        tmp_path: Per-test temporary directory.
        monkeypatch: Patching helper, scoped to the test.

    Returns:
        The directory WASM manages.
    """
    managed = tmp_path / "etc/systemd/system"
    distro = tmp_path / "usr/lib/systemd/system"
    for directory in (managed, distro):
        directory.mkdir(parents=True)

    monkeypatch.setattr(ServiceManager, "SYSTEMD_DIR", managed)
    monkeypatch.setattr(ServiceManager, "UNIT_SEARCH_DIRS", (managed, distro))
    return managed


@pytest.fixture
def owned(unit_dir: Path, store: FakeStore, runner: FakeRunner) -> str:
    """
    Install a unit that WASM owns, so the ownership guard lets operations through.

    Args:
        unit_dir: The managed unit directory.
        store: The in-memory store.
        runner: The fake runner.

    Returns:
        The service name to pass on the command line.
    """
    name = "wasm-example"
    (unit_dir / f"{name}.service").write_text(
        f"# {WASM_UNIT_MARKER}\n[Unit]\nDescription=x\n\n[Service]\nExecStart=/usr/bin/true\n"
    )
    runner.script(
        ["systemctl", "show", "-p", "FragmentPath", f"{name}.service"],
        stdout=f"FragmentPath={unit_dir / f'{name}.service'}\n",
    )
    return name


@pytest.fixture
def logged(monkeypatch: pytest.MonkeyPatch) -> io.StringIO:
    """
    Capture what the command prints through the logger.

    :class:`~wasm.core.logger.Logger` binds ``sys.stdout`` as a default argument
    at import time, so neither CliRunner nor capsys ever sees its output.

    Args:
        monkeypatch: Patching helper, scoped to the test.

    Returns:
        The buffer the logger writes to.
    """
    buffer = io.StringIO()
    monkeypatch.setattr(service_module, "Logger", lambda **kwargs: Logger(stream=buffer, **kwargs))
    return buffer


def invoke(*args: str, input: str | None = None) -> Result:
    """
    Run the real command tree, exactly as the console script would.

    Args:
        *args: Command line arguments after ``wasm``.
        input: Text fed to stdin, for confirmation prompts.

    Returns:
        The Click result.
    """
    return CliRunner().invoke(root, list(args), input=input)


def group() -> click.Group:
    """
    Return the service group as the root resolves it.

    Returns:
        The Click group registered for ``wasm service``.
    """
    resolved = root.get_command(click.Context(root), "service")
    assert isinstance(resolved, click.Group)
    return resolved


# Surface -------------------------------------------------------------------


def test_group_is_reachable_by_name_and_alias() -> None:
    for name in ("service", "svc"):
        result = invoke(name, "--help")
        assert result.exit_code == 0, result.output
        assert "systemd" in result.output


@pytest.mark.parametrize("name", ALL_SPELLINGS)
def test_every_spelling_answers_to_help(name: str) -> None:
    result = invoke("service", name, "--help")
    assert result.exit_code == 0, result.output
    assert f"service {name}" in result.output


@pytest.mark.parametrize("name", ALL_SPELLINGS)
def test_every_spelling_is_reachable_through_the_group_alias(name: str) -> None:
    assert invoke("svc", name, "--help").exit_code == 0


@pytest.mark.parametrize(("canonical", "spellings"), sorted(SPELLINGS.items()))
def test_aliases_resolve_to_the_same_command(canonical: str, spellings: tuple[str, ...]) -> None:
    ctx = click.Context(group())
    target = group().get_command(ctx, canonical)
    assert target is not None
    for spelling in spellings:
        assert group().get_command(ctx, spelling) is target


def test_help_lists_canonical_names_only() -> None:
    """Aliases stay resolvable but do not clutter the listing."""
    assert set(group().list_commands(click.Context(group()))) == set(SPELLINGS)

    output = invoke("service", "--help").output
    for canonical in SPELLINGS:
        assert canonical in output


def test_unknown_subcommand_is_a_usage_error() -> None:
    result = invoke("service", "reboot")
    assert result.exit_code == 2
    assert "reboot" in result.output


def test_no_subcommand_redeclares_a_global_flag() -> None:
    """
    The root group owns the global flags.

    Declaring one again on a subcommand is what let argparse's subparser default
    overwrite the value set before the subcommand name.
    """
    ctx = click.Context(group())
    offenders: dict[str, list[str]] = {}
    for name in group().list_commands(ctx):
        command = group().get_command(ctx, name)
        assert command is not None
        clashes = [
            opt
            for param in command.params
            if isinstance(param, click.Option)
            for opt in param.opts + param.secondary_opts
            if opt in GLOBAL_FLAGS
        ]
        if clashes:
            offenders[name] = clashes
    assert not offenders, f"subcommands redeclaring global flags: {offenders}"


@pytest.mark.parametrize("flag", ["-v", "--verbose"])
def test_global_verbose_survives_the_subcommand(
    flag: str, runner: FakeRunner, unit_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``wasm -v service list`` must still be verbose once inside the command."""
    seen: list[bool] = []

    def spy(self: ServiceManager, all_services: bool = False) -> list[dict]:
        seen.append(self.verbose)
        return []

    monkeypatch.setattr(ServiceManager, "list_services", spy)

    assert invoke(flag, "service", "list").exit_code == 0
    assert seen == [True]


# Argument validation -------------------------------------------------------


@pytest.mark.parametrize("name", ["status", "info", "start", "stop", "restart", "logs", "rm"])
def test_missing_name_is_a_usage_error(name: str, runner: FakeRunner) -> None:
    result = invoke("service", name)
    assert result.exit_code == 2
    assert "NAME" in result.output
    assert runner.calls == []


@pytest.mark.parametrize("missing", ["--name", "--command", "--directory"])
def test_create_requires_every_mandatory_option(missing: str, runner: FakeRunner) -> None:
    supplied = {
        "--name": "app",
        "--command": "/usr/bin/node server.js",
        "--directory": "/var/www/apps/app",
    }
    del supplied[missing]
    argv = [item for pair in supplied.items() for item in pair]

    result = invoke("service", "create", *argv)

    assert result.exit_code == 2
    assert missing in result.output
    assert runner.calls == []


def test_non_numeric_line_count_is_rejected_before_anything_runs(runner: FakeRunner) -> None:
    result = invoke("service", "logs", "wasm-example", "--lines", "many")
    assert result.exit_code == 2
    assert runner.calls == []


def test_directory_that_is_a_file_is_rejected(runner: FakeRunner, tmp_path: Path) -> None:
    regular = tmp_path / "not-a-directory"
    regular.write_text("")

    result = invoke("service", "create", "-n", "app", "-c", "/usr/bin/true", "-d", str(regular))

    assert result.exit_code == 2
    assert runner.calls == []


# Routing -------------------------------------------------------------------


@pytest.mark.parametrize("verb", ["start", "stop", "restart"])
def test_lifecycle_verbs_reach_systemctl_through_the_manager(
    verb: str, runner: FakeRunner, owned: str
) -> None:
    result = invoke("service", verb, owned)

    assert result.exit_code == 0, result.output
    assert runner.ran("systemctl", verb, f"{owned}.service")


def test_list_asks_systemd_only_for_units_wasm_owns(runner: FakeRunner, unit_dir: Path) -> None:
    assert invoke("service", "list").exit_code == 0
    listing = runner.calls_to("systemctl")[0]
    assert "list-units" in listing
    assert "wasm-*" in listing
    assert "*" not in listing


def test_list_all_opts_into_the_whole_system(runner: FakeRunner, unit_dir: Path) -> None:
    assert invoke("service", "list", "--all").exit_code == 0
    assert runner.calls_to("systemctl")[0][-1] == "*"


def test_status_of_a_missing_service_exits_non_zero(
    runner: FakeRunner, unit_dir: Path, logged: io.StringIO
) -> None:
    result = invoke("service", "status", "wasm-absent")
    assert result.exit_code == 1
    assert "not found" in logged.getvalue().lower()


def test_status_reports_a_running_service(
    runner: FakeRunner, owned: str, logged: io.StringIO
) -> None:
    runner.script(["systemctl", "is-active", f"{owned}.service"], stdout="active\n")
    runner.script(["systemctl", "is-enabled", f"{owned}.service"], stdout="enabled\n")
    runner.script(["systemctl", "show", f"{owned}.service"], stdout="MainPID=4242\n")

    result = invoke("service", "info", owned)

    assert result.exit_code == 0
    assert "4242" in logged.getvalue()


def test_logs_asks_the_journal_for_the_requested_lines(runner: FakeRunner, owned: str) -> None:
    runner.script(["journalctl"], stdout="a log line\n")

    result = invoke("service", "logs", owned, "-n", "5")

    assert result.exit_code == 0, result.output
    assert runner.ran("journalctl", "-u", f"{owned}.service", "-n", "5", "--no-pager")
    assert "a log line" in result.output


def test_follow_streams_through_the_runner_instead_of_spawning_journalctl(
    runner: FakeRunner, owned: str
) -> None:
    """
    The direct ``subprocess.run`` this replaced was the one execution path with
    no deadline, no dry run and no audit record.
    """
    runner.script(["journalctl"], stdout="first\nsecond\n")

    result = invoke("service", "logs", owned, "--follow", "--lines", "2")

    assert result.exit_code == 0, result.output
    assert runner.ran("journalctl", "-u", f"{owned}.service", "-n", "2", "-f")
    assert result.output.splitlines()[-2:] == ["first", "second"]


def test_module_does_not_import_subprocess() -> None:
    tree = ast.parse(Path(service_module.__file__).read_text(encoding="utf-8"))
    imported = {
        name
        for node in ast.walk(tree)
        for name in (
            [alias.name for alias in node.names]
            if isinstance(node, ast.Import)
            else [node.module or ""]
            if isinstance(node, ast.ImportFrom)
            else []
        )
    }
    assert not {n for n in imported if n == "subprocess" or n.startswith("subprocess.")}


def test_create_delegates_to_the_manager(
    runner: FakeRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, object] = {}

    def spy(self: ServiceManager, **kwargs: object) -> None:
        seen.update(kwargs)

    monkeypatch.setattr(ServiceManager, "create_service", spy)

    result = invoke(
        "service",
        "create",
        "-n",
        "app",
        "-c",
        "/usr/bin/node server.js",
        "-d",
        "/var/www/apps/app",
        "-u",
        "deploy",
        "--description",
        "The app",
    )

    assert result.exit_code == 0, result.output
    assert seen == {
        "name": "app",
        "command": "/usr/bin/node server.js",
        "working_directory": "/var/www/apps/app",
        "user": "deploy",
        "description": "The app",
    }


def test_create_defaults_to_the_web_server_account(
    runner: FakeRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, object] = {}
    monkeypatch.setattr(ServiceManager, "create_service", lambda self, **kw: seen.update(kw))

    invoke("service", "create", "-n", "app", "-c", "/usr/bin/true", "-d", "/var/www/apps/app")

    assert seen["user"] == "www-data"


# Deletion ------------------------------------------------------------------


def test_delete_names_the_service_and_the_consequence(runner: FakeRunner, owned: str) -> None:
    result = invoke("service", "delete", owned, input="n\n")

    assert owned in result.output
    assert "delete its unit file" in result.output


def test_declining_the_prompt_changes_nothing(
    runner: FakeRunner, owned: str, logged: io.StringIO
) -> None:
    result = invoke("service", "delete", owned, input="n\n")

    assert result.exit_code == 0
    assert "Aborted" in logged.getvalue()
    assert not runner.ran("systemctl", "stop", f"{owned}.service")
    assert (Path(ServiceManager.SYSTEMD_DIR) / f"{owned}.service").exists()


def test_accepting_the_prompt_removes_the_unit(runner: FakeRunner, owned: str) -> None:
    result = invoke("service", "delete", owned, input="y\n")

    assert result.exit_code == 0, result.output
    assert runner.ran("systemctl", "stop", f"{owned}.service")
    assert runner.ran("systemctl", "disable", f"{owned}.service")
    assert not (Path(ServiceManager.SYSTEMD_DIR) / f"{owned}.service").exists()


@pytest.mark.parametrize("flag", ["--force", "-f", "-y"])
def test_force_skips_the_prompt(flag: str, runner: FakeRunner, owned: str) -> None:
    result = invoke("service", "rm", owned, flag)

    assert result.exit_code == 0, result.output
    assert "?" not in result.output
    assert runner.ran("systemctl", "disable", f"{owned}.service")


def test_delete_refuses_a_unit_wasm_does_not_own(
    runner: FakeRunner, unit_dir: Path, store: FakeStore, tmp_path: Path
) -> None:
    """The ownership guard lives in the manager, so the command must go through it."""
    distro = tmp_path / "usr/lib/systemd/system/ssh.service"
    distro.write_text("[Unit]\nDescription=OpenBSD Secure Shell server\n")
    runner.script(
        ["systemctl", "show", "-p", "FragmentPath", "ssh.service"],
        stdout=f"FragmentPath={distro}\n",
    )

    # Through app.main, which is the boundary that turns a WASMError into an
    # exit code; CliRunner alone would only report the exception.
    assert app_main(["service", "delete", "ssh", "--force"]) == 1
    assert not runner.ran("systemctl", "stop", "ssh.service")
    assert distro.exists()


def test_argparse_path_still_reaches_the_same_implementation(
    runner: FakeRunner, owned: str
) -> None:
    """
    ``wasm.cli.parser`` still routes through :func:`handle_service`.

    It has to keep working, and it has to keep calling the same code as the
    Click commands rather than a second copy of it.
    """
    args = Namespace(action="ls", all=False, verbose=False)

    assert service_module.handle_service(args) == 0
    assert runner.calls_to("systemctl")[0][1] == "list-units"


def test_argparse_path_reports_a_refused_unit_as_an_exit_code(
    runner: FakeRunner, unit_dir: Path, store: FakeStore, tmp_path: Path
) -> None:
    distro = tmp_path / "usr/lib/systemd/system/ssh.service"
    distro.write_text("[Unit]\nDescription=OpenBSD Secure Shell server\n")
    runner.script(
        ["systemctl", "show", "-p", "FragmentPath", "ssh.service"],
        stdout=f"FragmentPath={distro}\n",
    )

    args = Namespace(action="rm", name="ssh", force=True, verbose=False)

    assert service_module.handle_service(args) == 1
    assert distro.exists()
