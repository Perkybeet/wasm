# Copyright (c) 2024-2026 Yago Lopez Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
Deployment history recording: one row per attempt, with its build log captured.

This lives with the pipeline rather than in the callers on purpose (rule 3):
``BaseDeployer.deploy()``, ``BaseDeployer.update()`` and
``RollbackManager.rollback()`` all record through :class:`DeploymentRecorder`,
so the CLI, the panel and a webhook share one implementation and only differ in
the ``trigger`` they pass down. A per-caller copy is how the panel and the CLI
would come to disagree about what happened on the machine.

Two guarantees shape everything here:

- **Recording never fails a deployment.** The history is an account of the
  operation, not part of it. Every store or filesystem failure inside the
  recorder is caught at this boundary, logged as a warning, and the deployment
  continues; a broken history database must not take deploys down with it.
- **The captured log is complete even when the console is quiet.** The build
  output the runner streams goes through ``Logger.debug``, which drops it
  before it is printed unless ``--verbose`` was given. :class:`CapturingLogger`
  therefore mirrors the suppressed detail to the recording sink as well, so the
  file always holds what the operator will need after a failure.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import TextIO

from wasm.core.exceptions import WASMError
from wasm.core.fs import DryRunFileSystem, FileSystem, get_fs
from wasm.core.logger import Icons, Logger
from wasm.core.store import DeploymentStatus, WASMStore

#: How many history rows, and their log files, survive per domain.
DEFAULT_KEEP = 20

#: Modes for the captured logs. Build output can echo secrets, so the files are
#: not world readable; group access stays so an admin group can read them.
LOG_DIR_MODE = 0o750
LOG_FILE_MODE = 0o640

#: Answers ``(git_commit, git_branch)`` for the deployed tree, or Nones.
GitInfo = Callable[[], tuple[str | None, str | None]]

#: What the recorder treats as "the history could not be written": the store's
#: own errors, the SQLite errors underneath it, and filesystem trouble around
#: the log file. Anything else is a bug and must surface.
_RECORDING_ERRORS = (WASMError, OSError, sqlite3.Error)


class CapturingLogger(Logger):
    """
    A logger whose complete output can be mirrored to a sink.

    ``Logger.debug``, ``substep`` and ``command_output`` return before writing
    anything unless the logger is verbose, so a plain logger cannot feed a
    build log: the streamed npm output would only be captured when the operator
    happened to pass ``--verbose``. This subclass sends the suppressed detail
    to the sink too, formatted the way the verbose console would have shown it,
    while the console keeps exactly the behaviour it had.
    """

    def __init__(
        self,
        verbose: bool = False,
        no_color: bool = False,
        log_file: Path | None = None,
        stream: TextIO | None = None,
    ):
        """
        Initialize the logger.

        Args:
            verbose: Enable verbose console output.
            no_color: Disable colored output.
            log_file: Optional file path to write logs to.
            stream: Output stream, as in :class:`~wasm.core.logger.Logger`.
        """
        super().__init__(verbose=verbose, no_color=no_color, log_file=log_file, stream=stream)
        self._sink: Callable[[str], None] | None = None

    def attach_sink(self, sink: Callable[[str], None]) -> None:
        """
        Mirror every line this logger emits to a callable.

        Args:
            sink: Called once per line, colour codes already stripped.
        """
        self._sink = sink

    def detach_sink(self) -> None:
        """Stop mirroring. Safe to call when no sink is attached."""
        self._sink = None

    def _write(self, message: str, newline: bool = True) -> None:
        """
        Write a message, mirroring it to the sink when one is attached.

        Args:
            message: The rendered message.
            newline: Whether a newline follows.
        """
        if self._sink is not None:
            self._sink(self._strip_ansi(message))
        super()._write(message, newline)

    def debug(self, message: str) -> None:
        """
        Log a debug message, capturing it even when the console drops it.

        Args:
            message: Debug message.
        """
        if self._sink is not None and not self.verbose:
            self._sink(f"      [DEBUG] {message}")
        super().debug(message)

    def substep(self, message: str) -> None:
        """
        Log a substep, capturing it even when the console drops it.

        Args:
            message: Substep description.
        """
        if self._sink is not None and not self.verbose:
            self._sink(f"      {Icons.ARROW} {message}")
        super().substep(message)

    def command_output(self, stdout: str, stderr: str) -> None:
        """
        Log command output, capturing it even when the console drops it.

        Args:
            stdout: Command standard output.
            stderr: Command standard error output.
        """
        if self._sink is not None and not self.verbose:
            for stream_text in (stdout, stderr):
                if not stream_text or not stream_text.strip():
                    continue
                for line in stream_text.rstrip("\n").split("\n"):
                    self._sink(f"        {line}")
        super().command_output(stdout, stderr)


class DeploymentRecorder:
    """
    Records one deployment attempt: a history row plus a captured build log.

    Lifecycle: :meth:`start` creates the row in ``running`` state, opens
    ``{log_root}/{domain}/{deployment_id}.log`` through the filesystem seam
    (0640 inside a 0750 directory) and attaches itself to the logger so every
    pipeline line lands in the file with a timestamp. :meth:`finish_success` or
    :meth:`finish_failure` closes the row with the outcome and the failing
    step's error verbatim, then rotates: the oldest rows beyond ``keep`` are
    pruned and their log files deleted with them.

    **Rollback semantics.** A rollback is recorded as its own deployment row -
    it is an operation somebody triggered, with its own outcome and its own
    log - never by rewriting the history of the deploy it reverts. On success
    the caller additionally flips the most recent ``success`` row of the
    domain, the build the rollback discards, to ``rolled_back`` via
    :meth:`mark_previous_success_rolled_back`; when no such row exists (the
    history predates recording) nothing is marked and the rollback row alone
    tells the story.

    **Error boundary.** Recording never fails the deployment it records. Every
    public method catches the store, SQLite and filesystem errors, reports one
    warning through the logger, and lets the operation continue unrecorded.

    Under ``--dry-run`` nothing is recorded at all: a rehearsal that leaves
    history rows behind is a rehearsal that changed the machine.
    """

    def __init__(
        self,
        store: WASMStore,
        domain: str,
        trigger: str,
        *,
        logger: Logger,
        fs: FileSystem | None = None,
        git_info: GitInfo | None = None,
        log_root: Path | None = None,
        keep: int = DEFAULT_KEEP,
    ) -> None:
        """
        Initialize the recorder for one deployment attempt.

        Args:
            store: The store the history row is written to.
            domain: Domain being deployed.
            trigger: What initiated the run: ``cli``, ``panel`` or ``webhook``.
            logger: The logger the pipeline reports through. When it is a
                :class:`CapturingLogger` its full output is captured to the
                log file; a plain logger still gets a row, just no line capture.
            fs: Filesystem the log file and directory are created through.
                Defaults to the process-wide one.
            git_info: Optional callable answering the commit and branch of the
                deployed tree, asked once when the recording finishes, because
                the checkout only exists after the fetch step has run.
            log_root: Where the logs live. Defaults to ``deploy-logs`` next to
                the store's database file, so the logs land in ``/var/lib/wasm``
                on a system install and inside ``tmp_path`` in a test, without
                either having to say so.
            keep: How many history rows and log files survive per domain.
        """
        self._store = store
        self._domain = domain
        self._trigger = trigger
        self._logger = logger
        self._fs = fs if fs is not None else get_fs()
        self._git_info = git_info
        self._log_root = log_root if log_root is not None else store.db_path.parent / "deploy-logs"
        self._keep = keep
        self._deployment_id: int | None = None
        self._log_path: Path | None = None
        self._handle: TextIO | None = None
        self._captured: list[CapturingLogger] = []
        self._finished = False

    @property
    def deployment_id(self) -> int | None:
        """The id of the history row, or None when nothing is being recorded."""
        return self._deployment_id

    def start(self, *, git_branch: str | None = None) -> None:
        """
        Create the history row and open the captured log.

        Args:
            git_branch: Branch requested for the deployment, when known. The
                branch actually checked out overrides it at finish time.
        """
        if isinstance(self._fs, DryRunFileSystem):
            self._logger.debug("Rehearsal: deployment history is not recorded")
            return

        try:
            deployment_id = self._store.record_deployment_start(
                self._domain, self._trigger, git_branch=git_branch
            )
            self._deployment_id = deployment_id
            self._open_log(deployment_id)
        except _RECORDING_ERRORS as exc:
            self._abandon(exc)
            return

        self.also_capture(self._logger)

    def also_capture(self, logger: Logger) -> None:
        """
        Mirror another component's logger into this recording.

        A rollback rebuilds through a deployer of its own; attaching that
        deployer's logger here puts the rebuild output in the rollback's log.

        Args:
            logger: The logger to capture. A logger that is not a
                :class:`CapturingLogger` is left alone.
        """
        if isinstance(logger, CapturingLogger) and self._handle is not None:
            logger.attach_sink(self._write_line)
            self._captured.append(logger)

    def annotate(self, *, git_commit: str | None = None, git_branch: str | None = None) -> None:
        """
        Record facts learned while the deployment runs.

        Args:
            git_commit: Commit being deployed (short hash), when known.
            git_branch: Branch being deployed, when known.
        """
        if self._deployment_id is None:
            return
        try:
            self._store.annotate_deployment(
                self._deployment_id, git_commit=git_commit, git_branch=git_branch
            )
        except _RECORDING_ERRORS as exc:
            self._warn(exc)

    def mark_previous_success_rolled_back(self) -> None:
        """
        Flip the domain's most recent ``success`` row to ``rolled_back``.

        Called by the rollback path once the restore succeeded: the newest
        successful deployment is the build the rollback just discarded. The
        recording's own row is never a candidate. When no ``success`` row
        exists there is nothing to mark, and that is not an error.
        """
        try:
            for record in self._store.list_deployments(self._domain, limit=self._keep):
                if record.id is None or record.id == self._deployment_id:
                    continue
                if record.status == DeploymentStatus.SUCCESS.value:
                    self._store.mark_deployment_rolled_back(record.id)
                    return
        except _RECORDING_ERRORS as exc:
            self._warn(exc)

    def finish_success(self) -> None:
        """Close the recording with a ``success`` outcome and rotate."""
        self._finish(DeploymentStatus.SUCCESS.value, None)

    def finish_failure(self, error: BaseException | str) -> None:
        """
        Close the recording with a ``failed`` outcome and rotate.

        Args:
            error: What the failing step raised. Stored verbatim - for a
                :class:`~wasm.core.exceptions.WASMError` that includes its
                details, which carry the build tool's own output.
        """
        self._finish(DeploymentStatus.FAILED.value, str(error))

    # Internals -------------------------------------------------------------

    def _open_log(self, deployment_id: int) -> None:
        """
        Create the log file through the seam and open it for appending.

        The file is created empty via the filesystem seam so the mode is
        applied at creation and a rehearsal leaves nothing behind; appending
        afterwards uses a plain handle, the same pattern the store uses for
        its database file.

        Args:
            deployment_id: Id of the row this log belongs to, which names it.
        """
        directory = self._log_root / self._domain
        self._fs.make_dir(directory, mode=LOG_DIR_MODE, parents=True)
        path = directory / f"{deployment_id}.log"
        self._fs.write_text(path, "", mode=LOG_FILE_MODE)
        if not path.exists():
            # The seam declined to create it; record the row without a log.
            return
        self._log_path = path
        self._handle = path.open("a", encoding="utf-8")
        self._store.annotate_deployment(deployment_id, log_path=str(path))

    def _write_line(self, line: str) -> None:
        """
        Append one timestamped line to the captured log.

        Args:
            line: The line, colour codes already stripped.
        """
        if self._handle is None:
            return
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            # A rendered table or rule arrives as one multi-line message;
            # every line of the file gets its own timestamp regardless.
            for piece in line.replace("\r", "").split("\n"):
                self._handle.write(f"[{stamp}] {piece}\n")
            self._handle.flush()
        except OSError as exc:
            # Capture must never break the build it is recording.
            self._close_log()
            self._warn(exc)

    def _finish(self, status: str, error: str | None) -> None:
        """
        Detach, close the row with its outcome, and rotate the history.

        Args:
            status: Final status for the row.
            error: The failure, verbatim, when there was one.
        """
        self._detach()
        self._close_log()
        if self._deployment_id is None or self._finished:
            return
        self._finished = True
        try:
            git_commit, git_branch = self._collect_git_info()
            if git_commit or git_branch:
                self._store.annotate_deployment(
                    self._deployment_id, git_commit=git_commit, git_branch=git_branch
                )
            self._store.finish_deployment(self._deployment_id, status, error=error)
            self._rotate()
        except _RECORDING_ERRORS as exc:
            self._warn(exc)

    def _collect_git_info(self) -> tuple[str | None, str | None]:
        """
        Ask the caller-provided reader for the deployed commit and branch.

        Returns:
            ``(commit, branch)``, each None when unknown. A reader that fails
            is reported at debug level and treated as unknown: missing git
            metadata must not cost the row its outcome.
        """
        if self._git_info is None:
            return None, None
        try:
            return self._git_info()
        except (WASMError, OSError) as exc:
            self._logger.debug(f"Could not read git information: {exc}")
            return None, None

    def _rotate(self) -> None:
        """Prune rows beyond ``keep`` and delete the log files they leave behind."""
        self._store.prune_deployments(self._domain, keep=self._keep)

        directory = self._log_root / self._domain
        if not directory.is_dir():
            return

        survivors = {
            record.id for record in self._store.list_deployments(self._domain, limit=self._keep)
        }
        for candidate in directory.glob("*.log"):
            try:
                candidate_id = int(candidate.stem)
            except ValueError:
                # Not one of ours; leave it alone.
                continue
            if candidate_id not in survivors:
                self._fs.remove(candidate)

    def _detach(self) -> None:
        """Withdraw the sink from every logger it was attached to."""
        for logger in self._captured:
            logger.detach_sink()
        self._captured = []

    def _close_log(self) -> None:
        """Close the log handle, tolerating a handle that is already gone."""
        if self._handle is None:
            return
        try:
            self._handle.close()
        except OSError as exc:
            self._logger.debug(f"Could not close the deployment log: {exc}")
        self._handle = None

    def _warn(self, exc: BaseException) -> None:
        """
        Report that the history could not be written, and carry on.

        Args:
            exc: What went wrong.
        """
        self._logger.warning(f"Deployment history could not be recorded: {exc}")

    def _abandon(self, exc: BaseException) -> None:
        """
        Give up on recording this deployment.

        Args:
            exc: What went wrong.
        """
        self._warn(exc)
        self._close_log()
        self._deployment_id = None
        self._log_path = None
