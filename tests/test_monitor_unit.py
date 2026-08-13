# Copyright (c) 2024-2026 Yago Lopez Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
Tests for the systemd unit the monitor installs.

This unit was found on a production server having failed to start 2379
consecutive times, once every thirty seconds, since the day it was installed.
The monitor had never run once, and nothing said so: the service was "enabled"
and ``systemctl list-units`` showed it as activating, which reads like a slow
start rather than a machine that has been failing all month.

The cause was one line. The unit combined ``ProtectSystem=strict`` with
``ReadWritePaths=/var/lib/wasm /var/log/wasm``, and ``ReadWritePaths`` does not
create anything: on a machine where WASM had not yet written a database there
was no ``/var/lib/wasm``, so systemd could not build the mount namespace and
exited 226 before the process existed.

``StateDirectory=`` and ``LogsDirectory=`` are the directives that create a
path, set its ownership and add it to the writable set, in that order, before
the unit runs. That is the whole fix, and this file is what keeps it.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from wasm.core.runner import FakeRunner
from wasm.monitor.process_monitor import ProcessMonitor

#: Paths systemd will not create on a unit's behalf. Naming one of these in a
#: unit that also sets ProtectSystem=strict is the defect this file is about.
UNCREATED_DIRECTIVES = ("ReadWritePaths", "ReadOnlyPaths", "InaccessiblePaths")


@pytest.fixture
def unit(runner: FakeRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """
    Render the unit the monitor would install.

    The rendering looks the wasm executable up on PATH, because systemd has no
    PATH of its own; a bin directory is planted so the test does not depend on
    WASM being installed system-wide on the machine running it.

    Args:
        runner: The fake command runner, so nothing reaches a real process.
        tmp_path: Per-test temporary directory.
        monkeypatch: Patching helper, scoped to the test.

    Returns:
        The unit file body.
    """
    binary = tmp_path / "bin" / "wasm"
    binary.parent.mkdir(parents=True)
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o755)
    monkeypatch.setenv("PATH", str(binary.parent))

    monitor = ProcessMonitor(verbose=False)
    return monitor._unit_content()


def directives(unit: str) -> dict[str, list[str]]:
    """
    Read a unit into directive names and their values.

    Args:
        unit: The unit file body.

    Returns:
        Every directive, with the values it was given.
    """
    found: dict[str, list[str]] = {}
    for line in unit.splitlines():
        line = line.strip()
        if not line or line.startswith(("#", "[")) or "=" not in line:
            continue
        name, value = line.split("=", 1)
        found.setdefault(name.strip(), []).append(value.strip())
    return found


def test_the_unit_declares_no_path_systemd_will_not_create(unit: str) -> None:
    """
    The regression, stated as the rule that would have prevented it.

    A writable path that does not exist yet is not a permission the unit gains;
    it is a namespace systemd cannot build, and the service never starts.
    """
    named = directives(unit)
    offenders = {key: named[key] for key in UNCREATED_DIRECTIVES if key in named}

    assert not offenders, (
        f"the unit names paths systemd will not create: {offenders}. "
        "Use StateDirectory=/LogsDirectory=/CacheDirectory=, which are created "
        "before the unit starts and are writable without further declaration."
    )


def test_the_unit_gets_its_state_directory_created_for_it(unit: str) -> None:
    """The monitor writes /var/lib/wasm/observations.db and must be able to."""
    assert directives(unit).get("StateDirectory") == ["wasm"]


def test_the_unit_gets_its_log_directory_created_for_it(unit: str) -> None:
    """
    Args:
        unit: The rendered unit.
    """
    assert directives(unit).get("LogsDirectory") == ["wasm"]


def test_the_unit_is_still_confined(unit: str) -> None:
    """
    The fix must not be "remove the hardening until it starts".

    The monitor reads every process on the machine as root; the sandbox is the
    reason that is acceptable.
    """
    named = directives(unit)

    assert named.get("ProtectSystem") == ["strict"]
    assert named.get("NoNewPrivileges") == ["true"]
    assert named.get("ProtectHome") == ["read-only"]
    assert named.get("PrivateDevices") == ["true"]
    assert named.get("RestrictSUIDSGID") == ["true"]


def test_the_unit_restarts_but_not_instantly(unit: str) -> None:
    """
    A crash loop with no delay is a busy loop. Thirty seconds is what let the
    broken unit fail 2379 times without taking the machine down with it.
    """
    named = directives(unit)

    assert named.get("Restart") == ["always"]
    assert int(named["RestartSec"][0]) >= 5


def test_the_unit_runs_an_absolute_path(unit: str) -> None:
    """
    systemd has no PATH of its own worth relying on, and a unit is the one
    place a relative program name fails only after installation.
    """
    exec_start = directives(unit)["ExecStart"][0]

    assert exec_start.startswith("/"), exec_start


def test_the_unit_says_how_to_reinstall_it(unit: str) -> None:
    """
    It is generated, and the operator meets it in /etc/systemd/system with no
    other clue about where it came from.
    """
    assert "wasm monitor install" in unit


def test_every_directive_sits_under_a_section(unit: str) -> None:
    """
    A directive above the first [Section] header is silently ignored, which is
    how hardening gets switched off without anyone noticing.
    """
    seen_section = False
    for line in unit.splitlines():
        line = line.strip()
        if line.startswith("["):
            seen_section = True
        elif line and not line.startswith("#") and "=" in line:
            assert seen_section, f"{line!r} is outside any section"


def test_the_rendered_unit_parses_as_the_service_it_claims_to_be(unit: str) -> None:
    """A net that reads nothing would pass every test above."""
    assert re.search(r"^\[Unit\]$", unit, re.MULTILINE)
    assert re.search(r"^\[Service\]$", unit, re.MULTILINE)
    assert re.search(r"^\[Install\]$", unit, re.MULTILINE)
    assert directives(unit)["Type"] == ["simple"]
