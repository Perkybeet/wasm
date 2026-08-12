# Copyright (c) 2024-2026 Yago Lopez Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
Checks that run before a deployment is allowed to start.

Validating the machine is not the deployer's job; the deployer only needs the
answer. Each check returns the problems it found, so a run reports every issue
at once rather than failing on the first one and hiding the other three.
"""

from __future__ import annotations

import shutil
import socket
from pathlib import Path

from wasm.core.exceptions import WASMError
from wasm.core.runner import CommandRunner

#: A repository probe answers in seconds or is not going to answer.
GIT_PROBE_TIMEOUT = 30

#: Below this, a build fails halfway through with a confusing error instead of
#: an out-of-space one.
MINIMUM_FREE_GB = 1.0

_GIT_SCHEMES = ("git@", "https://", "http://", "git://")


def missing_programs(runner: CommandRunner, programs: list[str]) -> list[str]:
    """
    Report which of the required programs are not installed.

    Args:
        runner: Runner whose PATH lookup decides.
        programs: Executable names the deployment needs.

    Returns:
        The names that were not found.
    """
    return [program for program in programs if not runner.exists(program)]


def repository_unreachable(runner: CommandRunner, source: str) -> list[str]:
    """
    Check that a git source can be reached with the current credentials.

    Args:
        runner: Runner used to invoke git.
        source: Source URL or local path. Local paths are not probed.

    Returns:
        The problems found, empty when the repository answers.
    """
    if not source.startswith(_GIT_SCHEMES):
        return []

    probe = runner.run(["git", "ls-remote", "--exit-code", source], timeout=GIT_PROBE_TIMEOUT)
    if probe.success:
        return []

    issues = [f"Repository not accessible: {source}"]
    if "Permission denied" in str(probe.stderr):
        issues.append("Check SSH key configuration: wasm setup ssh --test")
    return issues


def insufficient_disk_space(directory: Path) -> list[str]:
    """
    Check that there is room to clone and build.

    Args:
        directory: Directory the application will be written to.

    Returns:
        The problems found, empty when there is room or the check is impossible.
    """
    if not directory.exists():
        return []
    try:
        free_gb = shutil.disk_usage(str(directory)).free / (1024**3)
    except OSError:
        # An unreadable mount point is not evidence of a full disk.
        return []
    if free_gb < MINIMUM_FREE_GB:
        return [f"Insufficient disk space: {free_gb:.1f}GB free (need 1GB minimum)"]
    return []


def port_taken(port: int, *, allowed_owner_port: int | None) -> list[str]:
    """
    Check that nothing else is already listening on the port.

    Args:
        port: Port the application will bind.
        allowed_owner_port: Port recorded for the app being redeployed. When it
            matches, the listener is this application's own previous process.

    Returns:
        The problems found, empty when the port is free or ours.
    """
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        in_use = probe.connect_ex(("127.0.0.1", port)) == 0
    finally:
        probe.close()

    if in_use and allowed_owner_port != port:
        return [f"Port {port} is already in use"]
    return []


def webserver_down(manager: object, name: str) -> list[str]:
    """
    Check that the web server the site will be added to is running.

    Args:
        manager: Nginx or Apache manager.
        name: Web server name, for the message.

    Returns:
        The problems found, empty when it is running or cannot be queried.
    """
    try:
        running = bool(manager.is_running())  # type: ignore[attr-defined]
    except (WASMError, OSError, AttributeError):
        # An unanswerable question is not a failed check; the site step will
        # report the real problem with a real error message.
        return []
    return [] if running else [f"{name} is not running"]
