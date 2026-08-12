# Copyright (c) 2024-2026 Yago Lopez Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
Tests for the command helpers in :mod:`wasm.core.utils`.

These pin down the three properties the old implementation did not have: no
string splitting, no shell, and no unbounded wait.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

import pytest

from wasm.core import utils
from wasm.core.exceptions import SecurityError
from wasm.core.runner import CommandResult, CommandRunner, FakeRunner


class RecordingRunner(FakeRunner):
    """A FakeRunner that also remembers the timeout each call asked for."""

    def __init__(self) -> None:
        super().__init__()
        self.timeouts: list[int] = []

    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        timeout: int = 60,
        input: str | None = None,
        user: str | None = None,
        check: bool = False,
        secrets: Sequence[str] = (),
    ) -> CommandResult:
        self.timeouts.append(timeout)
        return super().run(
            argv,
            cwd=cwd,
            env=env,
            timeout=timeout,
            input=input,
            user=user,
            check=check,
            secrets=secrets,
        )


# run_command ---------------------------------------------------------------


def test_run_command_rejects_a_string(runner: FakeRunner) -> None:
    """A command line as one string is the bug this function used to have."""
    with pytest.raises(ValueError, match="sequence of arguments"):
        utils.run_command("git clone https://example.com/repo.git")

    assert runner.calls == []


def test_run_command_does_not_split_arguments(runner: FakeRunner) -> None:
    """A path with a space stays one argument."""
    utils.run_command(["cp", "/var/www/my app/index.html", "/tmp/out"])

    assert runner.calls[-1] == ("cp", "/var/www/my app/index.html", "/tmp/out")


def test_run_command_rejects_an_empty_vector(runner: FakeRunner) -> None:
    """An empty argv can never be executed and must not reach the runner."""
    with pytest.raises(ValueError):
        utils.run_command([])


def test_run_command_has_no_shell_parameter() -> None:
    """No caller can ask for a shell, because there is nothing to ask."""
    assert "shell" not in inspect.signature(utils.run_command).parameters


def test_run_command_applies_a_finite_default_timeout() -> None:
    """The default deadline is a number, not None."""
    default = inspect.signature(utils.run_command).parameters["timeout"].default

    assert isinstance(default, int)
    assert default > 0


def test_run_command_passes_the_default_timeout_to_the_runner() -> None:
    """A caller that says nothing still gets a deadline."""
    recorder = RecordingRunner()

    utils.run_command(["nginx", "-t"], runner=recorder)

    assert recorder.timeouts == [utils.DEFAULT_COMMAND_TIMEOUT]


def test_run_command_forwards_an_explicit_timeout() -> None:
    """An explicit deadline reaches the runner unchanged."""
    recorder = RecordingRunner()

    utils.run_command(["git", "clone", "x"], timeout=42, runner=recorder)

    assert recorder.timeouts == [42]


def test_run_command_forwards_cwd_and_env(tmp_path: Path) -> None:
    """Working directory and environment reach the runner."""
    seen: dict[str, object] = {}

    class Capturing(FakeRunner):
        def run(self, argv, **kwargs):  # type: ignore[no-untyped-def]
            seen.update(kwargs)
            return super().run(argv, **kwargs)

    utils.run_command(["ls"], cwd=tmp_path, env={"A": "b"}, runner=Capturing())

    assert seen["cwd"] == tmp_path
    assert seen["env"] == {"A": "b"}


def test_run_command_goes_through_the_process_wide_runner(runner: FakeRunner) -> None:
    """With no runner argument, --dry-run and the test fake still apply."""
    runner.script(["systemctl", "is-active"], stdout="active")

    result = utils.run_command(["systemctl", "is-active", "wasm-x"])

    assert result.output == "active"
    assert runner.ran("systemctl", "is-active", "wasm-x")


def test_utils_does_not_import_subprocess() -> None:
    """The single seam is the runner; utils must not open a second one."""
    source = Path(utils.__file__).read_text()

    assert "import subprocess" not in source
    assert "shell=True" not in source


# run_command_sudo ----------------------------------------------------------


def test_run_command_sudo_does_not_prepend_sudo(runner: FakeRunner) -> None:
    """WASM requires root (decision D6), so the sudo prefix is gone."""
    utils.run_command_sudo(["systemctl", "daemon-reload"])

    assert runner.calls[-1] == ("systemctl", "daemon-reload")
    assert not any(call[0] == "sudo" for call in runner.calls)


def test_run_command_sudo_rejects_a_string(runner: FakeRunner) -> None:
    """The deprecated shim inherits the argv-only rule."""
    with pytest.raises(ValueError):
        utils.run_command_sudo("rm -rf /")


# run_trusted_installer -----------------------------------------------------


def test_trusted_installer_refuses_an_unlisted_url(runner: FakeRunner) -> None:
    """Only the whitelist may be fetched and executed."""
    with pytest.raises(SecurityError):
        utils.run_trusted_installer("https://evil.example.com/install.sh")

    assert runner.calls == []


def test_trusted_installer_uses_no_shell_pipeline(runner: FakeRunner) -> None:
    """The script is downloaded, then fed to bash on stdin."""
    url = "https://bun.sh/install"
    runner.script(["curl"], stdout="#!/bin/sh\necho hi\n")

    utils.run_trusted_installer(url)

    assert runner.calls[0] == ("curl", "-fsSL", url)
    assert runner.calls[1] == ("bash", "-s")
    assert runner.inputs[-1] == "#!/bin/sh\necho hi\n"
    assert not any("|" in arg for call in runner.calls for arg in call)


def test_trusted_installer_stops_when_the_download_fails(runner: FakeRunner) -> None:
    """A failed download must not be executed as an empty script."""
    runner.script(["curl"], exit_code=22, stderr="404")

    result = utils.run_trusted_installer("https://bun.sh/install")

    assert not result.success
    assert not runner.ran("bash", "-s")


# File helpers --------------------------------------------------------------


def test_file_helpers_never_shell_out(tmp_path: Path, runner: FakeRunner) -> None:
    """``sudo=True`` used to mean cp/mv/rm/ln subprocesses; now it means nothing."""
    target = tmp_path / "nested" / "config"

    assert utils.write_file(target, "server {}\n", sudo=True)
    assert utils.read_file(target, sudo=True) == "server {}\n"
    assert utils.copy_file(target, tmp_path / "copy", sudo=True)
    assert utils.create_symlink(target, tmp_path / "link", sudo=True)
    assert utils.remove_file(target, sudo=True)
    assert utils.remove_directory(tmp_path / "nested", sudo=True)

    assert runner.calls == []


def test_create_symlink_replaces_an_existing_link(tmp_path: Path) -> None:
    """Enabling a site twice must not fail on the second attempt."""
    first = tmp_path / "a"
    second = tmp_path / "b"
    first.write_text("1")
    second.write_text("2")
    link = tmp_path / "link"

    assert utils.create_symlink(first, link)
    assert utils.create_symlink(second, link)
    assert link.resolve() == second


# Miscellaneous helpers -----------------------------------------------------


@pytest.mark.parametrize(
    ("domain", "expected"),
    [
        ("example.com", "example-com"),
        ("My-App.Example.COM", "my-app-example-com"),
        ("weird__name!.com", "weird-name-com"),
    ],
)
def test_domain_to_app_name(domain: str, expected: str) -> None:
    """Service and directory names are derived deterministically."""
    assert utils.domain_to_app_name(domain) == expected


def test_format_bytes_does_not_mutate_its_integer_argument() -> None:
    """The loop used to assign a float back into an int-annotated parameter."""
    assert utils.format_bytes(512) == "512.0 B"
    assert utils.format_bytes(2048) == "2.0 KB"


def test_run_command_signature_is_a_runner_facade() -> None:
    """run_command must stay a thin wrapper, not grow its own execution logic."""
    signature = inspect.signature(utils.run_command)

    assert list(signature.parameters) == ["command", "cwd", "env", "timeout", "runner"]
    assert isinstance(signature.parameters["runner"].default, type(None))


def test_run_command_accepts_any_runner() -> None:
    """Injection is what makes the deployers testable at all."""
    assert isinstance(FakeRunner(), CommandRunner)
    assert isinstance(utils.run_command, Callable)
