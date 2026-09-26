# Copyright (c) 2024-2026 Yago Lopez Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
The directory a deploy writes into, and whether it may.

``wasm create`` over a domain whose directory already existed used to destroy
it. The in-place fetch empties its target before cloning, so an application
deployed in place lost its ``.env`` and every file it had written; a Docker
Compose project lost the data its services bind-mount from the tree. And a
deploy that failed afterwards ran its undo, which deleted the whole directory,
including when WASM had no record of it because the store had moved.

Every deployer asks :func:`claim_deploy_target` before it fetches anything.
A directory that is missing or empty is the deploy's to fill. One that holds
files is refused with the way forward, unless the deploy only adds to it (an
application already on releases gets a new release beside the ones it has)
or the operator asked to replace it. Either way the answer records whether
the directory was there before, so a failed deploy never removes a directory
it did not create.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from wasm.core.exceptions import DeploymentError
from wasm.core.fs import FileSystem
from wasm.core.logger import Logger
from wasm.core.store import App
from wasm.deployers.helpers.layout import RELEASES


@dataclass(frozen=True)
class DeployTarget:
    """
    The application directory, as a deploy found it.

    Attributes:
        path: The directory.
        existed: It was there before the deploy.
        had_files: It held something before the deploy.
    """

    path: Path
    existed: bool
    had_files: bool

    @property
    def created_here(self) -> bool:
        """Whether this deploy created the directory, and may remove it on failure."""
        return not self.existed

    def undo_fetch(self, fs: FileSystem, logger: Logger) -> None:
        """
        Take back what a failed deploy fetched into the directory, and nothing more.

        A directory this deploy created is removed. One that was there and
        empty (a mount point, a directory made ahead of time) is emptied and
        kept. One that held files is left exactly as it is: they were not
        this deploy's to delete.

        Args:
            fs: The filesystem the removal goes through.
            logger: Where what was kept is reported.
        """
        if not self.path.exists():
            return
        try:
            if self.created_here:
                fs.remove_tree(self.path)
            elif not self.had_files:
                for entry in sorted(self.path.iterdir()):
                    if entry.is_dir() and not entry.is_symlink():
                        fs.remove_tree(entry)
                    else:
                        fs.remove(entry)
            else:
                logger.warning(
                    f"{self.path} held files before this deploy; it is left as it is now"
                )
        except OSError as error:
            # An undo runs after something already failed; a directory that
            # will not go away must not hide that failure.
            logger.warning(f"Could not clean up {self.path}: {error}")


def claim_deploy_target(
    path: Path,
    *,
    domain: str,
    existing: App | None,
    replace: bool,
) -> DeployTarget:
    """
    Decide whether a deploy may write into an application directory.

    Args:
        path: The application directory.
        domain: The domain being deployed.
        existing: The application's store row, when WASM has one.
        replace: The operator asked to deploy over whatever is there
            (``wasm create --force``).

    Returns:
        How the directory was found.

    Raises:
        DeploymentError: It holds files and the deploy would replace them,
            and replacing was not asked for.
    """
    existed = os.path.lexists(path)
    if existed and (path.is_symlink() or not path.is_dir()):
        raise DeploymentError(
            f"{path} is not a directory",
            details="WASM deploys into a real directory. Move what is there aside and retry.",
        )
    had_files = existed and any(path.iterdir())
    target = DeployTarget(path=path, existed=existed, had_files=had_files)
    if not had_files or replace:
        return target
    # A redeploy of an application on releases builds one more release next
    # to the ones it has; its shared/ and the release serving stay as they are.
    if existing is not None and existing.layout == RELEASES:
        return target

    if existing is not None:
        raise DeploymentError(
            f"{domain} is already deployed in place at {path}",
            details=f"A deploy would replace everything in {path}, its .env and the files the "
            f"application wrote included. To bring it up to date, run: wasm update {domain}. "
            f"To replace it anyway, back it up first (wasm backup create {domain}) and deploy "
            "again with --force.",
        )
    raise DeploymentError(
        f"{path} already exists and is not empty",
        details=f"WASM has no record of an application in it. If one was deployed there, the "
        "store may have moved: compare 'wasm store path' with where it used to be before "
        f"deploying anything. Otherwise move {path} aside, or deploy with --force to deploy "
        "over it: in place its contents are replaced, on releases a release is added beside "
        "them.",
    )
