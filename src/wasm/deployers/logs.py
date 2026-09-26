# Copyright (c) 2024-2026 Yago Lopez Prado
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Deployment log reading: the one place a deployment's captured build log is
read back off disk.

:class:`~wasm.deployers.recorder.DeploymentRecorder` writes the log and stores
its path on the history row; this module is the only reader, used by both the
server-rendered deployment detail page (:mod:`wasm.web.views.deployments`) and
the JSON API (:mod:`wasm.web.api.deployments`). A second implementation is how
one of the two surfaces would end up reading a path it should have refused.

The stored path is data, not an instruction: it is only followed when it
resolves inside the deployment log directory next to the store's own
database, which is where the recorder writes. A row pointing anywhere else is
refused and reported, never read.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from wasm.core.exceptions import SecurityError, ValidationError
from wasm.validators.names import resolve_within

if TYPE_CHECKING:
    from wasm.core.store import DeploymentRecord

log = logging.getLogger(__name__)

#: Bytes of a captured log returned when the caller does not ask for a
#: narrower tail, counted from the end of the file. Deployment logs are
#: rotated at twenty per application, so the whole file fits in memory well
#: under this cap for the overwhelming majority of builds; this only protects
#: against the rare build tool that will not stop talking.
DEFAULT_TAIL_BYTES = 512 * 1024


@dataclass
class LogRead:
    """
    The result of reading a deployment's captured log.

    Attributes:
        content: The log text, or an empty string when nothing was read.
        truncated: True when ``content`` is the tail of a larger file.
        missing_reason: Why ``content`` is empty, in words fit to show an
            operator. ``None`` when ``content`` holds the log.
    """

    content: str
    truncated: bool
    missing_reason: str | None


def read_deployment_log(record: DeploymentRecord, *, tail: int | None = None) -> LogRead:
    """
    Read a deployment's captured build log from disk.

    Args:
        record: The history row whose log is being read.
        tail: Maximum bytes of the file to return, counted from the end.
            Defaults to :data:`DEFAULT_TAIL_BYTES`.

    Returns:
        The log, or the honest reason there is nothing to show.
    """
    if not record.log_path:
        return LogRead(
            content="",
            truncated=False,
            missing_reason="No build log was captured for this deployment.",
        )

    from wasm.core.store import get_store

    root = get_store().db_path.parent / "deploy-logs"
    try:
        path = resolve_within(root, record.log_path)
    except (SecurityError, ValidationError):
        log.warning(
            "Deployment %s records a log path outside %s; refusing to read it",
            record.id,
            root,
        )
        return LogRead(
            content="",
            truncated=False,
            missing_reason=(
                "The recorded log path is not under the deployment log directory, "
                "so the panel will not read it."
            ),
        )

    if not path.is_file():
        return LogRead(
            content="", truncated=False, missing_reason="The captured log is no longer on disk."
        )

    try:
        # read_bytes rather than an open() of our own: this module is audited
        # against touching the filesystem outside the seam for writes, and a
        # whole-name check cannot tell a read-only handle from a writable one.
        data = path.read_bytes()
    except OSError as exc:
        log.warning("Could not read the captured log for deployment %s: %s", record.id, exc)
        return LogRead(
            content="", truncated=False, missing_reason=f"The captured log could not be read: {exc}"
        )

    limit = tail if tail is not None else DEFAULT_TAIL_BYTES
    if len(data) > limit:
        text = data[-limit:].decode("utf-8", errors="replace")
        # Drop the partial line the cut landed inside.
        return LogRead(content=text.split("\n", 1)[-1], truncated=True, missing_reason=None)
    return LogRead(
        content=data.decode("utf-8", errors="replace"), truncated=False, missing_reason=None
    )
