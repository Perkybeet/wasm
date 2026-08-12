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

import logging
import queue
import shutil
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any

from wasm.core.exceptions import (
    BackupError,
    CertificateError,
    DeploymentError,
    RollbackError,
    WASMError,
)
from wasm.core.store import get_store
from wasm.core.utils import domain_to_app_name

logger = logging.getLogger(__name__)

#: How many jobs may run at the same time. Deploys are IO and CPU heavy and
#: they compete with the panel itself for the machine.
MAX_CONCURRENT_JOBS = 3

#: Only the last N log entries of a job are serialised, so a chatty build does
#: not turn every poll of the jobs API into a megabyte of JSON.
MAX_SERIALISED_LOGS = 100


class JobStatus(str, Enum):
    """Job execution status."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


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
        self._initialized = True

        self._start_worker()

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
        )

        self._jobs[job_id] = job
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

    def _notify_subscribers(self, job: Job) -> None:
        """
        Push a job snapshot to everyone watching it.

        Args:
            job: The job that changed.
        """
        for callback in [*self._subscribers.get(job.id, []), *self._global_subscribers]:
            # A subscriber is a WebSocket push that can fail at any moment; one
            # dead client must not stop the others from being notified.
            try:
                callback(job)
            except Exception:
                logger.debug("Job subscriber failed for job %s", job.id, exc_info=True)

    def cleanup_old_jobs(self, max_age_hours: int = 24) -> int:
        """
        Forget finished jobs older than a cutoff.

        Args:
            max_age_hours: Age above which a finished job is dropped.

        Returns:
            How many jobs were removed.
        """
        cutoff = datetime.now().timestamp() - (max_age_hours * 3600)
        finished = (JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED)

        to_remove = [
            job_id
            for job_id, job in self._jobs.items()
            if job.status in finished and job.completed_at and job.completed_at.timestamp() < cutoff
        ]

        for job_id in to_remove:
            del self._jobs[job_id]
            self._subscribers.pop(job_id, None)

        return len(to_remove)


_job_manager: JobManager | None = None


def get_job_manager() -> JobManager:
    """
    Get the process-wide job manager.

    Returns:
        The job manager, created on first use.
    """
    global _job_manager
    if _job_manager is None:
        _job_manager = JobManager()
    return _job_manager


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
    job_context: JobContext | None = None,
) -> dict[str, Any]:
    """
    Deploy an application through the deployer registry.

    Args:
        domain: Target domain.
        source: Git URL or local path.
        app_type: Application type, or ``auto`` to detect it.
        port: Application port, assigned by the deployer when omitted.
        branch: Git branch.
        env_vars: Environment variables for the service.
        webserver: Web server to configure.
        ssl: Whether to obtain a certificate.
        job_context: Injected by the job manager.

    Returns:
        Summary of the deployment.

    Raises:
        DeploymentError: When the deployer reports failure.
    """
    from wasm.deployers import get_deployer

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

    A rollback point is taken and the application's own deployer is run again
    with the settings recorded in the store. Re-running is the update: the
    deployer refreshes the source, reinstalls, rebuilds and rewrites the site
    in place rather than recreating it.

    Args:
        domain: Domain of the application to update.
        job_context: Injected by the job manager.

    Returns:
        Summary of the update.

    Raises:
        DeploymentError: When the application is unknown or the deploy fails.
    """
    from wasm.deployers import get_deployer
    from wasm.managers.backup_manager import RollbackManager

    context = _require_context(job_context)
    context.set_metadata("domain", domain)

    app = get_store().get_app(domain)
    if app is None:
        raise DeploymentError(
            f"Application not found: {domain}",
            details="Deploy it first, or check 'wasm list' for the exact domain.",
        )

    context.update("Creating rollback point", 10)
    RollbackManager(verbose=False).create_pre_deploy_backup(domain)

    deployer = get_deployer(app.app_type, verbose=False)
    deployer.configure(
        domain=domain,
        source=app.source,
        port=app.port,
        webserver=app.webserver,
        ssl=app.ssl_enabled,
        branch=app.branch,
        env_vars=app.env_vars,
    )

    context.update("Redeploying", 30)
    if not deployer.deploy():
        raise DeploymentError(
            f"Update failed for {domain}",
            details="Roll back with 'wasm rollback' or inspect the job log.",
        )

    context.update("Update complete", 100)
    return {"domain": domain, "status": "updated"}


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
    from wasm.managers.cert_manager import CertManager
    from wasm.managers.nginx_manager import NginxManager
    from wasm.managers.service_manager import ServiceManager

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

    context.update("Removing site configuration", 45)
    try:
        NginxManager(verbose=False).delete_site(domain)
    except WASMError as exc:
        context.log(f"Site removal reported: {exc}", "warning")

    if remove_ssl:
        context.update("Removing certificate", 65)
        try:
            CertManager(verbose=False).delete(domain)
        except WASMError as exc:
            context.log(f"Certificate removal reported: {exc}", "warning")

    if remove_files and app.app_path:
        context.update("Removing files", 85)
        app_path = Path(app.app_path)
        if app_path.is_dir():
            shutil.rmtree(app_path, ignore_errors=True)

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
    job_context: JobContext | None = None,
) -> dict[str, Any]:
    """
    Restore an application from a backup.

    Args:
        backup_id: Identifier of the backup to restore.
        target_domain: Domain to restore into, defaulting to the backup's own.
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

    if not manager.restore(backup_id=backup_id, target_domain=domain):
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

    if not RollbackManager(verbose=False).rollback(domain=domain, backup_id=backup_id):
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
    webserver: str = "nginx",
    include_www: bool = False,
    job_context: JobContext | None = None,
) -> dict[str, Any]:
    """
    Obtain a certificate for a domain.

    Args:
        domain: Primary domain of the certificate.
        email: Registration email.
        webserver: Web server whose certbot plugin to use.
        include_www: Also cover the ``www`` subdomain.
        job_context: Injected by the job manager.

    Returns:
        Summary of the issuance.

    Raises:
        CertificateError: When certbot fails.
    """
    from wasm.managers.cert_manager import CertManager

    context = _require_context(job_context)
    context.set_metadata("domain", domain)
    context.update("Requesting certificate", 20)

    domains = [domain, f"www.{domain}"] if include_www else [domain]
    if not CertManager(verbose=False).create(domains=domains, email=email, webserver=webserver):
        raise CertificateError(
            f"Certificate issuance failed for {domain}",
            details="Check that the domain resolves to this host and port 80 is reachable.",
        )

    context.update("Certificate created", 100)
    return {"domain": domain, "domains": domains, "status": "certificate_created"}


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
