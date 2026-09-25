"""
Background job system for the WASM web interface.

Long operations - a deploy, a certbot round trip, an ``apt install``, a backup
of a whole application - cannot run inside a request: the panel would hold the
connection open for minutes and, when the handler is ``async``, would freeze
the event loop for every other client at the same time. They are queued here
and the endpoint answers ``202 Accepted`` with a job id.

**These jobs call the managers directly.** They used to spawn the ``wasm``
binary with :mod:`subprocess` and scrape its console output for progress, which
made the web layer a third implementation of the product: it needed the CLI
installed on ``PATH``, it lost every typed error, it reported progress by
matching English words in log lines, and it ran as whatever user the panel ran
as instead of through the shared command runner. The job functions below are
thin compositions of :mod:`wasm.managers` and :mod:`wasm.deployers`, so the
panel and the CLI now perform the same operations through the same code.
"""

from __future__ import annotations

import json
import logging
import queue
import sqlite3
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, TextIO

from wasm.core.exceptions import (
    BackupError,
    DeploymentError,
    RollbackError,
    WASMError,
)
from wasm.core.fs import SECRET_DIR_MODE, SECRET_MODE, get_fs
from wasm.core.store import JobRecord, get_store
from wasm.core.utils import domain_to_app_name

logger = logging.getLogger(__name__)

#: How many jobs may run at the same time. Deploys are IO and CPU heavy and
#: they compete with the panel itself for the machine.
MAX_CONCURRENT_JOBS = 3

#: Only the last N log entries of a job are serialised, so a chatty build does
#: not turn every poll of the jobs API into a megabyte of JSON.
MAX_SERIALISED_LOGS = 100

#: Directory job logs live in, a sibling of the ``deploy-logs`` directory
#: :class:`wasm.deployers.recorder.DeploymentRecorder` uses, both next to the
#: store's database file.
JOB_LOG_DIR_NAME = "job-logs"

#: What persisting a job transition can fail with: the store's own errors, the
#: SQLite errors underneath it, and filesystem trouble around the log file.
#: Persisting must never fail the job it records - the job is real work on the
#: machine, and its history is only an account of it, the same boundary
#: :class:`wasm.deployers.recorder.DeploymentRecorder` draws for deployments.
_RECORDING_ERRORS: tuple[type[Exception], ...] = (WASMError, OSError, sqlite3.Error)

#: The reason recorded on every job a panel restart orphaned. A job in
#: ``pending`` or ``running`` when the process starts was not resumed - the
#: thread that was running it is gone - and this is the whole review focus of
#: Task 1.7: it must reappear as failed, never stay "running" forever.
INTERRUPTED_REASON = "Interrupted by a panel restart"


class JobStatus(str, Enum):
    """Job execution status."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


#: Statuses that close a job's log file and stop expecting further transitions.
FINISHED_STATUSES = frozenset({JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED})


class JobType(str, Enum):
    """Types of background jobs."""

    DEPLOY = "deploy"
    UPDATE = "update"
    BACKUP = "backup"
    RESTORE = "restore"
    CERT_CREATE = "cert_create"
    CERT_RENEW = "cert_renew"
    SERVICE_ACTION = "service_action"
    SITE_ACTION = "site_action"
    DELETE = "delete"
    CUSTOM = "custom"


@dataclass
class JobLogEntry:
    """
    A single log entry for a job.

    Attributes:
        timestamp: When the entry was recorded.
        level: One of ``info``, ``warning``, ``error`` or ``success``.
        message: The message itself.
        step: Progress value at the time of the entry.
    """

    timestamp: datetime
    level: str
    message: str
    step: int | None = None

    def to_dict(self) -> dict[str, Any]:
        """
        Render the entry as JSON-serialisable data.

        Returns:
            The entry as a dictionary.
        """
        return {
            "timestamp": self.timestamp.isoformat(),
            "level": self.level,
            "message": self.message,
            "step": self.step,
        }


@dataclass
class Job:
    """
    A unit of work executed off the request path.

    Attributes:
        id: Short identifier handed to the client.
        type: What kind of operation this is.
        name: Short human-readable name.
        description: Longer description.
        status: Current status.
        progress: Progress between 0 and ``total_steps``.
        total_steps: Denominator of ``progress``.
        current_step: Name of the step in progress.
        created_at: When the job was queued.
        started_at: When the worker picked it up.
        completed_at: When it finished, whatever the outcome.
        result: Value returned by the job function.
        error: Error message when the job failed.
        logs: Everything the job reported.
        metadata: Free-form context, such as the domain being deployed.
        actor: Who queued the job - a session id prefix, an API token name,
            or ``master`` - never a secret. None for a job the system queued
            on its own, such as a webhook-triggered deploy.
    """

    id: str
    type: JobType
    name: str
    description: str
    status: JobStatus = JobStatus.PENDING
    progress: int = 0
    total_steps: int = 100
    current_step: str = ""
    created_at: datetime = field(default_factory=datetime.now)
    started_at: datetime | None = None
    completed_at: datetime | None = None
    result: Any | None = None
    error: str | None = None
    logs: list[JobLogEntry] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    actor: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """
        Render the job as JSON-serialisable data.

        Returns:
            The job as a dictionary, with its log tail.
        """
        return {
            "id": self.id,
            "type": self.type.value,
            "name": self.name,
            "description": self.description,
            "status": self.status.value,
            "progress": self.progress,
            "total_steps": self.total_steps,
            "current_step": self.current_step,
            "created_at": self.created_at.isoformat(),
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "result": self.result,
            "error": self.error,
            "logs": [log.to_dict() for log in self.logs[-MAX_SERIALISED_LOGS:]],
            "metadata": self.metadata,
            "actor": self.actor,
        }

    def add_log(self, message: str, level: str = "info", step: int | None = None) -> None:
        """
        Append a log entry to the job.

        Args:
            message: The message to record.
            level: Severity, one of ``info``, ``warning``, ``error``, ``success``.
            step: Progress value to attach, defaulting to the current progress.
        """
        self.logs.append(
            JobLogEntry(
                timestamp=datetime.now(),
                level=level,
                message=message,
                step=step if step is not None else self.progress,
            )
        )


class JobContext:
    """
    Handle a job function uses to report progress.

    Usage in a job function::

        def my_job(domain: str, job_context: JobContext) -> dict[str, str]:
            job_context.update("Starting", 10)
            job_context.log("Fetched source", "success")
            return {"domain": domain}
    """

    def __init__(self, job: Job, notify: Callable[[Job], None]):
        """
        Args:
            job: The job being executed.
            notify: Callback invoked after every change, used to push updates
                to subscribed WebSocket clients.
        """
        self._job = job
        self._notify = notify

    @property
    def job_id(self) -> str:
        """Identifier of the running job."""
        return self._job.id

    @property
    def is_cancelled(self) -> bool:
        """True once the job has been cancelled."""
        return self._job.status == JobStatus.CANCELLED

    def update(self, step_name: str, progress: int) -> None:
        """
        Record the step in progress.

        Args:
            step_name: Name of the step.
            progress: Progress value, clamped to the job's total.
        """
        self._job.current_step = step_name
        self._job.progress = min(progress, self._job.total_steps)
        self._job.add_log(step_name, "info", progress)
        self._notify(self._job)

    def log(self, message: str, level: str = "info") -> None:
        """
        Record a message without changing progress.

        Args:
            message: The message.
            level: Severity.
        """
        self._job.add_log(message, level)
        self._notify(self._job)

    def set_metadata(self, key: str, value: Any) -> None:
        """
        Attach context to the job.

        Args:
            key: Metadata key.
            value: JSON-serialisable value.
        """
        self._job.metadata[key] = value


class JobManager:
    """
    Runs queued jobs on a worker thread, at most :data:`MAX_CONCURRENT_JOBS` at
    a time, and notifies subscribers of every state change.
    """

    _instance: JobManager | None = None

    #: Guards the singleton's one-time setup: ``__init__`` runs on every
    #: ``JobManager()`` call, but only the first one may build the queue.
    _initialized: bool = False

    def __new__(cls) -> JobManager:
        """
        Return the process-wide job manager.

        Returns:
            The singleton instance.
        """
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self) -> None:
        """Initialise the queue and start the worker, once per process."""
        if self._initialized:
            return

        self._jobs: dict[str, Job] = {}
        self._job_queue: queue.Queue[
            tuple[str, Callable[..., Any], tuple[Any, ...], dict[str, Any]]
        ] = queue.Queue()
        self._max_concurrent = MAX_CONCURRENT_JOBS
        self._running_count = 0
        self._lock = threading.Lock()
        self._subscribers: dict[str, list[Callable[[Job], None]]] = {}
        self._global_subscribers: list[Callable[[Job], None]] = []
        self._worker_thread: threading.Thread | None = None
        self._shutdown = False
        self._log_handles: dict[str, TextIO] = {}
        self._log_paths: dict[str, str] = {}
        self._initialized = True

        self._fail_interrupted_jobs()
        self._start_worker()

    @classmethod
    def reset_instance(cls) -> None:
        """
        Drop the singleton, so the next call to ``JobManager()`` starts fresh.

        This is what lets a test - and, conceptually, a real process restart -
        simulate "the panel came back up": the next construction runs
        :meth:`_fail_interrupted_jobs` again, against whatever the store says
        was pending or running.

        The worker thread of the discarded instance is asked to stop; nothing
        joins it; it is daemonic so the process does not wait on it either.
        """
        if cls._instance is not None:
            cls._instance._shutdown = True
        cls._instance = None

    def _fail_interrupted_jobs(self) -> None:
        """
        Mark jobs the previous process left pending or running as failed.

        Called once, at startup: whatever thread was running those jobs is
        gone, and a job stuck at "running" forever is how a history screen
        comes to lie about the state of the machine. Recording is an error
        boundary of its own - a store that cannot be reached at startup must
        not stop the panel from starting.
        """
        try:
            changed = get_store().fail_interrupted_jobs(INTERRUPTED_REASON)
        except _RECORDING_ERRORS as exc:
            logger.warning("Could not check for interrupted jobs at startup: %s", exc)
            return
        if changed:
            logger.warning("%d job(s) marked failed after a panel restart", changed)

    def _start_worker(self) -> None:
        """Start the background worker thread if it is not already running."""
        if self._worker_thread is None or not self._worker_thread.is_alive():
            self._shutdown = False
            self._worker_thread = threading.Thread(target=self._worker_loop, daemon=True)
            self._worker_thread.start()

    def _worker_loop(self) -> None:
        """Pull jobs off the queue and execute them until shutdown."""
        while not self._shutdown:
            try:
                job_id, func, args, kwargs = self._job_queue.get(timeout=1.0)
            except queue.Empty:
                continue

            with self._lock:
                if self._running_count >= self._max_concurrent:
                    self._job_queue.put((job_id, func, args, kwargs))
                    continue
                self._running_count += 1

            try:
                self._execute_job(job_id, func, args, kwargs)
            finally:
                with self._lock:
                    self._running_count -= 1

    def _execute_job(
        self,
        job_id: str,
        func: Callable[..., Any],
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
    ) -> None:
        """
        Run one job and record its outcome.

        Args:
            job_id: Identifier of the queued job.
            func: The job function.
            args: Positional arguments for the function.
            kwargs: Keyword arguments for the function.
        """
        job = self._jobs.get(job_id)
        if not job:
            return

        job.status = JobStatus.RUNNING
        job.started_at = datetime.now()
        job.add_log("Job started", "info")
        self._notify_subscribers(job)

        try:
            call_kwargs = dict(kwargs)
            call_kwargs["job_context"] = JobContext(job, self._notify_subscribers)
            job.result = func(*args, **call_kwargs)
            job.status = JobStatus.COMPLETED
            job.progress = job.total_steps
            job.add_log("Job completed successfully", "success")
        # This is the worker's error boundary: a job function is arbitrary
        # product code and a crash here must mark the job failed, never kill
        # the only worker thread.
        except Exception as exc:
            logger.exception("Job %s failed", job_id)
            job.status = JobStatus.FAILED
            job.error = str(exc)
            job.add_log(f"Job failed: {exc}", "error")
        finally:
            job.completed_at = datetime.now()
            self._notify_subscribers(job)

    def create_job(
        self,
        job_type: JobType,
        name: str,
        description: str,
        func: Callable[..., Any],
        args: tuple[Any, ...] = (),
        kwargs: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
        total_steps: int = 100,
        actor: str | None = None,
    ) -> Job:
        """
        Create and queue a background job.

        Args:
            job_type: Type of job.
            name: Short name.
            description: Detailed description.
            func: Function to execute. It must accept a ``job_context`` keyword.
            args: Positional arguments for the function.
            kwargs: Keyword arguments for the function.
            metadata: Additional job metadata.
            total_steps: Denominator for progress reporting.
            actor: Who queued the job, as recorded by the endpoint that
                called this - see :attr:`Job.actor`.

        Returns:
            The queued job.
        """
        job_id = str(uuid.uuid4())[:8]

        job = Job(
            id=job_id,
            type=job_type,
            name=name,
            description=description,
            total_steps=total_steps,
            metadata=metadata or {},
            actor=actor,
        )

        self._jobs[job_id] = job
        self._open_log(job_id)
        self._job_queue.put((job_id, func, args, kwargs or {}))
        self._notify_subscribers(job)

        return job

    def get_job(self, job_id: str) -> Job | None:
        """
        Look a job up by identifier.

        Args:
            job_id: The identifier.

        Returns:
            The job, or None when it is unknown or already cleaned up.
        """
        return self._jobs.get(job_id)

    def get_all_jobs(self, limit: int = 50) -> list[Job]:
        """
        List jobs, most recent first.

        Args:
            limit: Maximum number of jobs to return.

        Returns:
            The most recent jobs.
        """
        jobs = sorted(self._jobs.values(), key=lambda j: j.created_at, reverse=True)
        return jobs[:limit]

    def get_active_jobs(self) -> list[Job]:
        """
        List jobs that are queued or running.

        Returns:
            The active jobs.
        """
        return [
            job
            for job in self._jobs.values()
            if job.status in (JobStatus.PENDING, JobStatus.RUNNING)
        ]

    def cancel_job(self, job_id: str) -> bool:
        """
        Cancel a job that has not started yet.

        A running job is not interrupted: it is halfway through changing the
        system, and there is no safe generic point to stop it.

        Args:
            job_id: The identifier.

        Returns:
            True when the job moved to cancelled.
        """
        job = self._jobs.get(job_id)
        if not job or job.status != JobStatus.PENDING:
            return False

        job.status = JobStatus.CANCELLED
        job.completed_at = datetime.now()
        job.add_log("Job cancelled", "warning")
        self._notify_subscribers(job)
        return True

    def subscribe(self, job_id: str, callback: Callable[[Job], None]) -> None:
        """
        Receive updates for one job.

        Args:
            job_id: The job to watch.
            callback: Called with the job after every change.
        """
        self._subscribers.setdefault(job_id, []).append(callback)

    def subscribe_all(self, callback: Callable[[Job], None]) -> None:
        """
        Receive updates for every job.

        Args:
            callback: Called with the job after every change.
        """
        self._global_subscribers.append(callback)

    def unsubscribe(self, job_id: str, callback: Callable[[Job], None]) -> None:
        """
        Stop receiving updates for one job.

        Args:
            job_id: The job being watched.
            callback: The callback to remove.
        """
        if job_id in self._subscribers:
            try:
                self._subscribers[job_id].remove(callback)
            except ValueError:
                pass

    def unsubscribe_all(self, callback: Callable[[Job], None]) -> None:
        """
        Stop receiving updates for every job.

        The counterpart to :meth:`subscribe_all`, and not optional: the panel's
        event stream subscribes once per open browser tab and an operator
        leaves the panel open for days across reconnections. Without a way to
        withdraw, every one of those leaves a callback holding a queue that
        nothing will ever read again.

        Args:
            callback: The callback to remove. Removing one that was never
                registered is not an error, so a stream can unsubscribe on the
                way out without tracking whether it got that far.
        """
        try:
            self._global_subscribers.remove(callback)
        except ValueError:
            pass

    def _notify_subscribers(self, job: Job) -> None:
        """
        Persist the job's current state and push a snapshot to everyone watching it.

        Persistence happens here, at the one chokepoint every transition and
        every log line already passes through, rather than being sprinkled
        across every place that changes a job: a step this method does not see
        is a step neither the store nor a panel restart will ever know about.

        Args:
            job: The job that changed.
        """
        self._persist(job)

        for callback in [*self._subscribers.get(job.id, []), *self._global_subscribers]:
            # A subscriber is a WebSocket push that can fail at any moment; one
            # dead client must not stop the others from being notified.
            try:
                callback(job)
            except Exception:
                logger.debug("Job subscriber failed for job %s", job.id, exc_info=True)

    def _persist(self, job: Job) -> None:
        """
        Write a job's current state to the store and its newest line to disk.

        Tries an update first and falls back to an insert when the row does
        not exist yet, so the very first notification - queueing the job,
        before it has run a single step - is what creates the row. A store
        that cannot be reached must not fail the job it is only recording.

        Args:
            job: The job that changed.
        """
        if job.logs:
            latest = job.logs[-1]
            self._write_log_line(job.id, latest.message, latest.level)

        domain = job.metadata.get("domain")
        started_at = job.started_at.isoformat() if job.started_at else None
        finished_at = job.completed_at.isoformat() if job.completed_at else None
        result_json = self._safe_json(job.result) if job.result is not None else None
        log_path = self._log_paths.get(job.id)

        try:
            store = get_store()
            updated = store.update_job(
                job.id,
                status=job.status.value,
                progress=job.progress,
                domain=domain,
                error=job.error,
                result_json=result_json,
                started_at=started_at,
                finished_at=finished_at,
                log_path=log_path,
            )
            if not updated:
                store.create_job(
                    JobRecord(
                        id=job.id,
                        type=job.type.value,
                        name=job.name,
                        description=job.description,
                        status=job.status.value,
                        progress=job.progress,
                        total_steps=job.total_steps,
                        domain=domain,
                        error=job.error,
                        result_json=result_json,
                        started_at=started_at,
                        finished_at=finished_at,
                        log_path=log_path,
                        actor=job.actor,
                    )
                )
        except _RECORDING_ERRORS as exc:
            logger.warning("Could not persist job %s: %s", job.id, exc)

        if job.status in FINISHED_STATUSES:
            self._close_log(job.id)
            self._log_paths.pop(job.id, None)

    @staticmethod
    def _safe_json(value: Any) -> str:
        """
        Serialise a job's result for storage, without ever raising.

        Args:
            value: The job function's return value.

        Returns:
            JSON text. A value that ``json.dumps`` refuses is stringified
            first rather than losing the whole row over one field the caller
            could not have predicted.
        """
        try:
            return json.dumps(value)
        except TypeError:
            return json.dumps(str(value))

    def _log_root(self) -> Path:
        """
        Returns:
            Where job logs live: a ``job-logs`` directory next to the store's
            database file, the sibling of
            :class:`wasm.deployers.recorder.DeploymentRecorder`'s
            ``deploy-logs``. Resolved fresh on every call rather than cached,
            because the store singleton it reads from can be swapped out from
            under a long-lived manager - in tests, and in principle across a
            reconfiguration.
        """
        return get_store().db_path.parent / JOB_LOG_DIR_NAME

    def _open_log(self, job_id: str) -> None:
        """
        Create the job's log file through the filesystem seam and open it.

        Mirrors :class:`wasm.deployers.recorder.DeploymentRecorder`: the file
        is created empty via the seam so its mode is applied at creation and a
        rehearsal leaves nothing behind, then appended to with a plain handle.

        Args:
            job_id: Identifier of the job the log belongs to.
        """
        fs = get_fs()
        try:
            directory = self._log_root()
            fs.make_dir(directory, mode=SECRET_DIR_MODE, parents=True)
            path = directory / f"{job_id}.log"
            fs.write_text(path, "", mode=SECRET_MODE)
            if not path.exists():
                # The seam declined to create it (a rehearsal); nothing to log to.
                return
            self._log_handles[job_id] = path.open("a", encoding="utf-8")
            self._log_paths[job_id] = str(path)
        except _RECORDING_ERRORS as exc:
            logger.warning("Could not open the log file for job %s: %s", job_id, exc)

    def _write_log_line(self, job_id: str, message: str, level: str) -> None:
        """
        Append one timestamped line to the job's captured log.

        Args:
            job_id: Identifier of the job the line belongs to.
            message: The log message.
            level: Severity the line was reported at.
        """
        handle = self._log_handles.get(job_id)
        if handle is None:
            return
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            for piece in message.replace("\r", "").split("\n"):
                handle.write(f"[{stamp}] [{level.upper()}] {piece}\n")
            handle.flush()
        except OSError as exc:
            logger.warning("Could not write to the log file for job %s: %s", job_id, exc)
            self._close_log(job_id)

    def _close_log(self, job_id: str) -> None:
        """
        Close a job's log handle, tolerating one that is already gone.

        Args:
            job_id: Identifier of the job whose log is being closed.
        """
        handle = self._log_handles.pop(job_id, None)
        if handle is None:
            return
        try:
            handle.close()
        except OSError as exc:
            logger.debug("Could not close the log file for job %s: %s", job_id, exc)

    def cleanup_old_jobs(self, max_age_hours: int = 24) -> int:
        """
        Forget finished jobs older than a cutoff.

        Args:
            max_age_hours: Age above which a finished job is dropped.

        Returns:
            How many jobs were removed.
        """
        cutoff = datetime.now().timestamp() - (max_age_hours * 3600)

        to_remove = [
            job_id
            for job_id, job in self._jobs.items()
            if job.status in FINISHED_STATUSES
            and job.completed_at
            and job.completed_at.timestamp() < cutoff
        ]

        for job_id in to_remove:
            del self._jobs[job_id]
            self._subscribers.pop(job_id, None)
            self._close_log(job_id)

        return len(to_remove)


def get_job_manager() -> JobManager:
    """
    Get the process-wide job manager.

    ``JobManager()`` is already the singleton constructor - ``__new__`` and
    ``__init__`` guard the one-time setup themselves - so this used to cache
    it a second time in a module global. That second cache was a second
    answer to "which manager is current": it kept handing out the previous
    instance after :meth:`JobManager.reset_instance` had already moved on,
    which is exactly the moment a test - or a real restart - needs the new
    one.

    Returns:
        The job manager, created on first use.
    """
    return JobManager()


def _require_context(job_context: JobContext | None) -> JobContext:
    """
    Assert that the job manager supplied a context.

    Args:
        job_context: The context the manager injects.

    Returns:
        The context.

    Raises:
        ValueError: When the function was called outside the job manager.
    """
    if job_context is None:
        raise ValueError("job_context is required; job functions run under the job manager")
    return job_context


def deploy_app_job(
    domain: str,
    source: str,
    app_type: str,
    port: int | None = None,
    branch: str | None = None,
    env_vars: dict[str, str] | None = None,
    webserver: str = "nginx",
    ssl: bool = True,
    subdomain_overrides: dict[str, str] | None = None,
    workspace_filter: list[str] | None = None,
    skip_database: bool = False,
    compose_file: str | None = None,
    compose_profiles: list[str] | None = None,
    layout: str | None = None,
    job_context: JobContext | None = None,
) -> dict[str, Any]:
    """
    Deploy an application through the deployer registry.

    The deployer-specific options are handed to ``configure`` for every type:
    the deployer interface's contract is that a deployer ignores the options
    that do not concern it, so the monorepo knobs reach the monorepo deployer
    and cost a Next.js one nothing.

    Args:
        domain: Target domain.
        source: Git URL or local path.
        app_type: Application type, or ``auto`` to detect it.
        port: Application port, assigned by the deployer when omitted.
        branch: Git branch.
        env_vars: Environment variables for the service.
        webserver: Web server to configure.
        ssl: Whether to obtain a certificate.
        subdomain_overrides: Monorepo: workspace name to subdomain overrides.
        workspace_filter: Monorepo: deploy only these workspaces.
        skip_database: Monorepo: skip database provisioning.
        compose_file: Docker Compose: compose file, relative to the project.
        compose_profiles: Docker Compose: profiles to activate.
        layout: ``inplace`` or ``releases``; None for the server's
            configured layout.
        job_context: Injected by the job manager.

    Returns:
        Summary of the deployment.

    Raises:
        DeploymentError: When the deployer reports failure.
    """
    from wasm.deployers import get_deployer
    from wasm.deployers.helpers.layout import CONFIGURED as CONFIGURED_LAYOUT

    context = _require_context(job_context)
    context.set_metadata("domain", domain)
    context.set_metadata("app_type", app_type)

    context.update("Preparing deployment", 5)
    deployer = get_deployer(app_type, verbose=False)
    deployer.configure(
        domain=domain,
        source=source,
        port=port,
        webserver=webserver,
        ssl=ssl,
        branch=branch,
        env_vars=env_vars or {},
        subdomain_overrides=subdomain_overrides or {},
        workspace_filter=workspace_filter,
        skip_database=skip_database,
        compose_file=compose_file,
        compose_profiles=compose_profiles,
        trigger="panel",
        layout=layout or CONFIGURED_LAYOUT,
    )

    context.update("Deploying", 10)
    if not deployer.deploy():
        raise DeploymentError(
            f"Deployment failed for {domain}",
            details="Check the job log and 'journalctl -u wasm-*' for the failing step.",
        )

    context.update("Deployment complete", 100)
    return {"domain": domain, "app_type": app_type, "port": port, "status": "deployed"}


def update_app_job(
    domain: str,
    job_context: JobContext | None = None,
) -> dict[str, Any]:
    """
    Update a deployed application.

    Args:
        domain: Domain of the application to update.
        job_context: Injected by the job manager.

    Returns:
        Summary of the update.

    Raises:
        WASMError: When the application is unknown or a step fails.
    """
    return run_update(domain, trigger="panel", job_context=job_context)


def run_update(domain: str, *, trigger: str, job_context: JobContext | None) -> dict[str, Any]:
    """
    Run the shared update sequence as a job, reporting its phases as progress.

    This used to re-run the whole deploy pipeline, whose fetch deletes the
    application directory and clones it again: the ``.env`` edited here, the
    files the application had written into its own tree and its generated
    secrets were replaced on every update. The sequence is now
    :func:`wasm.deployers.lifecycle.update_app`, the same one the CLI runs.

    Args:
        domain: Domain of the application to update.
        trigger: Who asked for it, recorded in the deployment history.
        job_context: Injected by the job manager.

    Returns:
        Summary of the update.

    Raises:
        WASMError: When the application is unknown or a step fails.
    """
    from wasm.deployers.lifecycle import update_app

    context = _require_context(job_context)
    context.set_metadata("domain", domain)

    if get_store().get_app(domain) is None:
        raise DeploymentError(
            f"Application not found: {domain}",
            details="Deploy it first, or check 'wasm list' for the exact domain.",
        )

    outcome = update_app(
        domain,
        trigger=trigger,
        on_phase=lambda index, total, message: context.update(message, 100 * (index - 1) // total),
        on_step=context.log,
    )

    if outcome.restarted and not outcome.active:
        context.log("Restarted, but the unit is not running: check its logs", "warning")

    context.update("Update complete", 100)
    return {
        "domain": domain,
        "status": "updated",
        "trigger": trigger,
        "restarted": list(outcome.restarted),
        "active": outcome.active,
    }


def delete_app_job(
    domain: str,
    remove_files: bool = True,
    remove_ssl: bool = True,
    job_context: JobContext | None = None,
) -> dict[str, Any]:
    """
    Remove an application, its service, its site and optionally its files.

    Args:
        domain: Domain of the application.
        remove_files: Also delete the application directory.
        remove_ssl: Also delete the certificate.
        job_context: Injected by the job manager.

    Returns:
        Summary of what was removed.

    Raises:
        DeploymentError: When the application is unknown.
    """
    from wasm.managers.apache_manager import ApacheManager
    from wasm.managers.cert_manager import CertManager
    from wasm.managers.nginx_manager import NginxManager
    from wasm.managers.service_manager import ServiceManager
    from wasm.managers.webserver import delete_site_completely

    context = _require_context(job_context)
    context.set_metadata("domain", domain)

    store = get_store()
    app = store.get_app(domain)
    if app is None:
        raise DeploymentError(
            f"Application not found: {domain}",
            details="Nothing to delete; check 'wasm list' for the exact domain.",
        )

    app_name = domain_to_app_name(domain)

    context.update("Stopping service", 20)
    service_manager = ServiceManager(verbose=False)
    try:
        service_manager.stop(app_name)
        service_manager.disable(app_name)
        service_manager.delete_service(app_name)
    except WASMError as exc:
        context.log(f"Service removal reported: {exc}", "warning")

    # Walks nginx and apache and, unless remove_ssl says otherwise, the
    # certificate. This used to touch nginx only, so an app whose site had
    # been recreated on apache - or migrated between the two - left a vhost
    # behind that no delete request from the panel ever reached.
    context.update("Removing site configuration and certificate", 55)
    deletion = delete_site_completely(
        domain,
        nginx=NginxManager(verbose=False),
        apache=ApacheManager(verbose=False),
        cert_manager=CertManager(verbose=False),
        delete_certificate=remove_ssl,
    )
    context.log(
        f"Site removal: nginx={deletion.nginx_removed} apache={deletion.apache_removed} "
        f"certificate={deletion.certificate_removed}",
        "info",
    )

    if remove_files and app.app_path:
        context.update("Removing files", 85)
        app_path = Path(app.app_path)
        if app_path.is_dir():
            # Through the filesystem seam, not shutil.rmtree directly: that is
            # the one execution path --dry-run cannot make honest, and the
            # seam is also what lets a test assert on the removal without
            # touching a real directory.
            try:
                get_fs().remove_tree(app_path)
            except OSError as exc:
                context.log(f"Could not remove {app_path}: {exc}", "warning")

    store.delete_app(domain)
    context.update("Deletion complete", 100)

    return {
        "domain": domain,
        "status": "deleted",
        "files_removed": remove_files,
        "ssl_removed": remove_ssl,
    }


def backup_app_job(
    domain: str,
    description: str = "",
    include_env: bool = True,
    include_node_modules: bool = False,
    include_build: bool = False,
    include_databases: bool = False,
    include_docker_volumes: bool = False,
    schemas: list[str] | None = None,
    redis_method: str = "rdb",
    tags: list[str] | None = None,
    job_context: JobContext | None = None,
) -> dict[str, Any]:
    """
    Create a backup of an application.

    Args:
        domain: Domain of the application.
        description: Free-form description stored with the backup.
        include_env: Include ``.env`` files.
        include_node_modules: Include ``node_modules``.
        include_build: Include build artefacts.
        include_databases: Include database dumps.
        include_docker_volumes: Include the application's named Docker
            volumes.
        schemas: PostgreSQL schemas to dump instead of whole databases. Not
            supported inside a self-contained backup; see
            :meth:`~wasm.managers.backup_manager.BackupManager.create`.
        redis_method: How to capture Redis, ``rdb`` or ``aof``.
        tags: Tags to store with the backup.
        job_context: Injected by the job manager.

    Returns:
        Identifier, path and size of the new backup.

    Raises:
        BackupError: When the backup manager fails.
    """
    from wasm.managers.backup_manager import BackupManager

    context = _require_context(job_context)
    context.set_metadata("domain", domain)
    context.update("Creating backup", 20)

    metadata = BackupManager(verbose=False).create(
        domain=domain,
        description=description,
        include_env=include_env,
        include_node_modules=include_node_modules,
        include_build=include_build,
        include_databases=include_databases,
        include_docker_volumes=include_docker_volumes,
        schemas=schemas,
        redis_method=redis_method,
        tags=tags or [],
    )

    context.update("Backup complete", 100)
    return {
        "domain": domain,
        "status": "backup_created",
        "backup_id": metadata.id,
        "size": metadata.size_bytes,
    }


def restore_backup_job(
    backup_id: str,
    target_domain: str | None = None,
    restore_env: bool = True,
    verify: bool = True,
    job_context: JobContext | None = None,
) -> dict[str, Any]:
    """
    Restore an application from a backup.

    Args:
        backup_id: Identifier of the backup to restore.
        target_domain: Domain to restore into, defaulting to the backup's own.
        restore_env: Restore the ``.env`` files from the archive.
        verify: Check the archive against its recorded checksum before
            restoring.
        job_context: Injected by the job manager.

    Returns:
        Summary of the restore.

    Raises:
        BackupError: When the backup is unknown or the restore fails.
    """
    from wasm.managers.backup_manager import BackupManager

    context = _require_context(job_context)
    context.set_metadata("backup_id", backup_id)

    manager = BackupManager(verbose=False)
    backup = manager.get_backup(backup_id)
    if backup is None:
        raise BackupError(
            f"Backup not found: {backup_id}",
            details="List the available backups with 'wasm backup list'.",
        )

    domain = target_domain or backup.domain
    context.set_metadata("domain", domain)
    context.update("Restoring backup", 30)

    if not manager.restore(
        backup_id=backup_id,
        target_domain=domain,
        restore_env=restore_env,
        verify_checksum=verify,
    ):
        raise BackupError(
            f"Restore failed for backup {backup_id}",
            details="Verify the archive with 'wasm backup verify' and retry.",
        )

    context.update("Restore complete", 100)
    return {"domain": domain, "backup_id": backup_id, "status": "restored"}


def rollback_app_job(
    domain: str,
    backup_id: str | None = None,
    job_context: JobContext | None = None,
) -> dict[str, Any]:
    """
    Roll an application back to a previous backup.

    Args:
        domain: Domain of the application.
        backup_id: Backup to roll back to, defaulting to the most recent one.
        job_context: Injected by the job manager.

    Returns:
        Summary of the rollback.

    Raises:
        RollbackError: When the rollback fails.
    """
    from wasm.managers.backup_manager import RollbackManager

    context = _require_context(job_context)
    context.set_metadata("domain", domain)
    context.set_metadata("backup_id", backup_id)
    context.update("Rolling back", 20)

    if not RollbackManager(verbose=False).rollback(
        domain=domain, backup_id=backup_id, trigger="panel"
    ):
        raise RollbackError(
            f"Rollback failed for {domain}",
            details="Check that a backup exists with 'wasm backup list'.",
        )

    context.update("Rollback complete", 100)
    return {"domain": domain, "backup_id": backup_id, "status": "rolled_back"}


def database_engine_job(
    engine: str,
    action: str,
    purge: bool = False,
    job_context: JobContext | None = None,
) -> dict[str, Any]:
    """
    Install or uninstall a database engine.

    Both actions drive the distribution package manager, which downloads,
    unpacks and configures; that is minutes of work and it must not happen on
    a request.

    Args:
        engine: Engine name, as registered in the database registry.
        action: Either ``install`` or ``uninstall``.
        purge: Also remove configuration and data when uninstalling.
        job_context: Injected by the job manager.

    Returns:
        Summary of the operation.

    Raises:
        DatabaseEngineError: When the engine is unknown or the package manager
            fails.
    """
    from wasm.core.exceptions import DatabaseEngineError
    from wasm.managers.database import get_db_manager

    context = _require_context(job_context)
    context.set_metadata("engine", engine)

    manager = get_db_manager(engine, verbose=False)
    if manager is None:
        raise DatabaseEngineError(
            f"Unknown database engine: {engine}",
            details="Check the engine list at GET /api/databases/engines.",
        )

    if action == "install":
        context.update(f"Installing {manager.DISPLAY_NAME}", 20)
        manager.install()
    elif action == "uninstall":
        context.update(f"Uninstalling {manager.DISPLAY_NAME}", 20)
        manager.uninstall(purge=purge)
    else:
        raise DatabaseEngineError(
            f"Unsupported engine action: {action}",
            details="Use 'install' or 'uninstall'.",
        )

    context.update("Complete", 100)
    return {"engine": engine, "action": action, "status": "completed"}


def cert_create_job(
    domain: str,
    email: str | None = None,
    domains: list[str] | None = None,
    method: str | None = None,
    webroot: str | None = None,
    include_www: bool = False,
    expand: bool = False,
    job_context: JobContext | None = None,
) -> dict[str, Any]:
    """
    Obtain a certificate for a domain.

    Reaches :meth:`~wasm.managers.cert_manager.CertManager.obtain` exactly the
    way ``wasm cert create`` does: this used to call the narrower ``.create()``
    convenience wrapper, which has no ``standalone`` option and always forced
    a webserver plugin, so a panel-issued certificate could not use the
    webroot or standalone methods the CLI has always offered.

    Args:
        domain: Primary domain of the certificate.
        email: Registration email.
        domains: Extra domains (SANs) to cover, beyond ``domain`` and the
            ``www`` alias ``include_www`` may add.
        method: How to prove control of the domain: ``nginx``, ``apache``,
            ``webroot`` or ``standalone``. None lets WASM pick the method
            that suits the web server it finds running, the CLI's own
            default when none of its method flags are given.
        webroot: Webroot path. Used when ``method`` is ``webroot``, or
            defaulted to :data:`~wasm.managers.cert_manager.DEFAULT_WEBROOT`
            when ``method`` is ``webroot`` and no path was given.
        include_www: Also cover the ``www`` subdomain.
        expand: Expand an existing certificate even when it already covers
            every requested domain.
        job_context: Injected by the job manager.

    Returns:
        Summary of the issuance.

    Raises:
        CertificateError: When certbot fails.
    """
    from wasm.managers.cert_manager import DEFAULT_WEBROOT, CertManager

    context = _require_context(job_context)
    context.set_metadata("domain", domain)
    context.update("Requesting certificate", 20)

    webroot_path: Path | None
    if webroot:
        webroot_path = Path(webroot)
    elif method == "webroot":
        webroot_path = DEFAULT_WEBROOT
    else:
        webroot_path = None

    manager = CertManager(verbose=False)
    manager.obtain(
        domain,
        email=email,
        webroot=webroot_path,
        standalone=method == "standalone",
        nginx=method == "nginx",
        apache=method == "apache",
        additional_domains=list(domains) if domains else None,
        expand=expand,
        include_www=include_www,
    )

    covered = manager.certificate_domains(domain, domains, include_www)
    context.update("Certificate created", 100)
    return {"domain": domain, "domains": covered, "status": "certificate_created"}


def cert_renew_job(
    domain: str | None = None,
    force: bool = False,
    job_context: JobContext | None = None,
) -> dict[str, Any]:
    """
    Renew one certificate, or every certificate that is due.

    Args:
        domain: Certificate name to renew, or None for all of them.
        force: Renew even when the certificate is not due yet.
        job_context: Injected by the job manager.

    Returns:
        Summary of the renewal.

    Raises:
        CertificateError: When certbot fails.
    """
    from wasm.managers.cert_manager import CertManager

    context = _require_context(job_context)
    context.set_metadata("domain", domain or "all")
    context.update("Renewing certificates", 20)

    CertManager(verbose=False).renew(domain=domain, force=force)

    context.update("Renewal complete", 100)
    return {"domain": domain, "status": "renewed"}
