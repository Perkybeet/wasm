# Copyright (c) 2024-2026 Yago Lopez Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
Ownership hand-over for deployed trees.

The whole deployment runs as root, but the generated unit runs as the
configured service user. That user must be able to write into the app
directory: pnpm creates a temporary file in the project root on every
``pnpm run``, and Next.js writes into ``.next`` at runtime, so a root-owned
tree fails at start with EACCES and systemd restarts it forever.

This is the one implementation both the base pipeline and the monorepo
deployer use; it used to exist only in the monorepo deployer, which is why
every other app type shipped with the bug above.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from wasm.core.fs import SECRET_MODE, FileSystem
from wasm.core.logger import Logger
from wasm.core.runner import CommandRunner

#: Deadline for the recursive chown and chmod over a deployed tree.
_PERMISSIONS_TIMEOUT = 60


def hand_over_tree(
    app_path: Path,
    *,
    user: str,
    group: str,
    runner: CommandRunner,
    fs: FileSystem,
    logger: Logger,
    env_files: Iterable[Path] = (),
) -> None:
    """
    Make ``user`` the owner of the deployed tree, keeping secrets private.

    Args:
        app_path: Root of the deployed application.
        user: Account the service runs as.
        group: Group the service runs as.
        runner: Runner the chown and chmod execute through.
        fs: Filesystem used to restore the mode of the secret files.
        logger: Logger for the non-fatal failures.
        env_files: Files holding secrets whose mode must come back to 0600
            after the recursive chmod.
    """
    result = runner.run(
        ["chown", "-R", f"{user}:{group}", str(app_path)],
        timeout=_PERMISSIONS_TIMEOUT,
    )
    # Not fatal: the build is good, and an app that never writes runs fine. One
    # that does fails with EACCES in its own log, far from here, so this is the
    # only place the operator can be told the cause.
    if not result.success:
        logger.warning(
            f"Could not hand {app_path} over to {user}:{group}; the service may fail "
            f"with EACCES: {result.stderr.strip()}"
        )

    # Directories need the execute bit and the built assets must stay readable
    # by the web server, whatever umask the fetch and the build left behind.
    result = runner.run(
        ["chmod", "-R", "u+rwX,g+rX,o+rX", str(app_path)],
        timeout=_PERMISSIONS_TIMEOUT,
    )
    if not result.success:
        logger.warning(
            f"Could not make {app_path} readable by the web server: {result.stderr.strip()}"
        )

    # That -R also put o+r on the .env files, which hold the secrets this
    # deployment was given. The chown above has just made the service account
    # their owner, so 0600 is readable by the application and by nobody else.
    for env_file in env_files:
        if env_file.exists():
            fs.chmod(env_file, SECRET_MODE)


def hand_over_file(
    path: Path,
    *,
    user: str,
    group: str,
    mode: int,
    runner: CommandRunner,
    logger: Logger,
) -> bool:
    """
    Make ``user:group`` the owner of a single file and set its mode.

    Unlike :func:`hand_over_tree`, a failure here is not something the caller
    can shrug off: a restore that goes on to report "restored" over a file
    still owned by root, or a database engine that reports success while its
    own account cannot read the snapshot it was just handed, is the silent
    failure CLAUDE.md rule 2 exists to remove. So this returns whether it
    actually worked instead of only logging a warning, and the chmod does not
    run at all when the chown failed - changing the mode of a file still
    owned by the wrong account would not make it usable and would bury the
    real failure under a second, unrelated-looking log line.

    Args:
        path: File to hand over.
        user: Account that must own it.
        group: Group that must own it.
        mode: Permission bits to apply.
        runner: Runner the chown and chmod execute through.
        logger: Logger for the failure, when there is one.

    Returns:
        True if both the chown and the chmod succeeded.
    """
    chown = runner.run(
        ["chown", f"{user}:{group}", str(path)],
        timeout=_PERMISSIONS_TIMEOUT,
    )
    if not chown.success:
        logger.warning(f"Could not hand {path} over to {user}:{group}: {chown.stderr.strip()}")
        return False

    chmod = runner.run(
        ["chmod", f"{mode:o}", str(path)],
        timeout=_PERMISSIONS_TIMEOUT,
    )
    if not chmod.success:
        logger.warning(f"Could not set permissions on {path}: {chmod.stderr.strip()}")
        return False

    return True
