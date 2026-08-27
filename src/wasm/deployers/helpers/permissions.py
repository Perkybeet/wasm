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
    if not result.success:
        logger.debug(f"chown failed: {result.stderr}")

    # Directories need the execute bit and the built assets must stay readable
    # by the web server, whatever umask the fetch and the build left behind.
    runner.run(
        ["chmod", "-R", "u+rwX,g+rX,o+rX", str(app_path)],
        timeout=_PERMISSIONS_TIMEOUT,
    )

    # That -R also put o+r on the .env files, which hold the secrets this
    # deployment was given. The chown above has just made the service account
    # their owner, so 0600 is readable by the application and by nobody else.
    for env_file in env_files:
        if env_file.exists():
            fs.chmod(env_file, SECRET_MODE)
