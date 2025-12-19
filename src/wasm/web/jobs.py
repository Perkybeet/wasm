"""
Background job system for WASM Web Interface.

Provides async task execution with real-time progress updates
to prevent blocking the web server during long operations.
"""

import asyncio
import uuid
import time
import subprocess
import threading
import queue
from datetime import datetime
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional, Callable, Any
from pathlib import Path


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
    """A single log entry for a job."""
    timestamp: datetime
    level: str  # info, warning, error, success
    message: str
    step: Optional[int] = None
    
    def to_dict(self) -> dict:
        return {
            "timestamp": self.timestamp.isoformat(),
            "level": self.level,
            "message": self.message,
            "step": self.step,
        }


@dataclass
class Job:
    """Represents a background job."""
    id: str
    type: JobType
    name: str
    description: str
    status: JobStatus = JobStatus.PENDING
    progress: int = 0
    total_steps: int = 100
    current_step: str = ""
    created_at: datetime = field(default_factory=datetime.now)
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    result: Optional[Any] = None
    error: Optional[str] = None
    logs: List[JobLogEntry] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)
    
    def to_dict(self) -> dict:
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
            "logs": [log.to_dict() for log in self.logs[-100:]],  # Last 100 logs
            "metadata": self.metadata,
        }
    
    def add_log(self, message: str, level: str = "info", step: Optional[int] = None):
        """Add a log entry to the job."""
        self.logs.append(JobLogEntry(
            timestamp=datetime.now(),
            level=level,
            message=message,
            step=step or self.progress,
        ))


class JobManager:
    """
    Manages background jobs with async execution.
    
    Features:
    - Non-blocking job execution in thread pool
    - Real-time progress updates via callbacks
    - Job persistence and history
    - Concurrent job limiting
    """
    
    _instance: Optional['JobManager'] = None
    
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance
    
    def __init__(self):
        if self._initialized:
            return
        
        self._jobs: Dict[str, Job] = {}
        self._job_queue: queue.Queue = queue.Queue()
        self._max_concurrent = 3
        self._running_count = 0
        self._lock = threading.Lock()
        self._subscribers: Dict[str, List[Callable]] = {}  # job_id -> callbacks
        self._global_subscribers: List[Callable] = []  # For all job updates
        self._worker_thread: Optional[threading.Thread] = None
        self._shutdown = False
        self._initialized = True
        
        # Start worker thread
        self._start_worker()
    
    def _start_worker(self):
        """Start the background worker thread."""
        if self._worker_thread is None or not self._worker_thread.is_alive():
            self._shutdown = False
            self._worker_thread = threading.Thread(target=self._worker_loop, daemon=True)
            self._worker_thread.start()
    
    def _worker_loop(self):
        """Main worker loop that processes jobs from the queue."""
        while not self._shutdown:
            try:
                # Get job from queue with timeout
                job_id, func, args, kwargs = self._job_queue.get(timeout=1.0)
                
                with self._lock:
                    if self._running_count >= self._max_concurrent:
                        # Re-queue if at capacity
                        self._job_queue.put((job_id, func, args, kwargs))
                        continue
                    self._running_count += 1
                
                try:
                    self._execute_job(job_id, func, args, kwargs)
                finally:
                    with self._lock:
                        self._running_count -= 1
                        
            except queue.Empty:
                continue
            except Exception as e:
                print(f"Worker error: {e}")
    
    def _execute_job(self, job_id: str, func: Callable, args: tuple, kwargs: dict):
        """Execute a job and handle its lifecycle."""
        job = self._jobs.get(job_id)
        if not job:
            return
        
        job.status = JobStatus.RUNNING
        job.started_at = datetime.now()
        job.add_log("Job started", "info")
        self._notify_subscribers(job)
        
        try:
            # Create a job context for the function
            context = JobContext(job, self._notify_subscribers)
            kwargs['job_context'] = context
            
            result = func(*args, **kwargs)
            
            job.status = JobStatus.COMPLETED
            job.result = result
            job.progress = job.total_steps
            job.add_log("Job completed successfully", "success")
            
        except Exception as e:
            job.status = JobStatus.FAILED
            job.error = str(e)
            job.add_log(f"Job failed: {e}", "error")
        
        finally:
            job.completed_at = datetime.now()
            self._notify_subscribers(job)
    
    def create_job(
        self,
        job_type: JobType,
        name: str,
        description: str,
        func: Callable,
        args: tuple = (),
        kwargs: dict = None,
        metadata: dict = None,
        total_steps: int = 100,
    ) -> Job:
        """
        Create and queue a new background job.
        
        Args:
            job_type: Type of job
            name: Short name for the job
            description: Detailed description
            func: Function to execute (must accept job_context kwarg)
            args: Positional arguments for func
            kwargs: Keyword arguments for func
            metadata: Additional job metadata
            total_steps: Total steps for progress calculation
            
        Returns:
            The created Job object
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
    
    def get_job(self, job_id: str) -> Optional[Job]:
        """Get a job by ID."""
        return self._jobs.get(job_id)
    
    def get_all_jobs(self, limit: int = 50) -> List[Job]:
        """Get all jobs, most recent first."""
        jobs = sorted(
            self._jobs.values(),
            key=lambda j: j.created_at,
            reverse=True
        )
        return jobs[:limit]
    
    def get_active_jobs(self) -> List[Job]:
        """Get all running or pending jobs."""
        return [
            job for job in self._jobs.values()
            if job.status in [JobStatus.PENDING, JobStatus.RUNNING]
        ]
    
    def cancel_job(self, job_id: str) -> bool:
        """Cancel a pending or running job."""
        job = self._jobs.get(job_id)
        if not job:
            return False
        
        if job.status == JobStatus.PENDING:
            job.status = JobStatus.CANCELLED
            job.completed_at = datetime.now()
            job.add_log("Job cancelled", "warning")
            self._notify_subscribers(job)
            return True
        
        # Can't cancel running jobs easily without process management
        return False
    
    def subscribe(self, job_id: str, callback: Callable[[Job], None]):
        """Subscribe to updates for a specific job."""
        if job_id not in self._subscribers:
            self._subscribers[job_id] = []
        self._subscribers[job_id].append(callback)
    
    def subscribe_all(self, callback: Callable[[Job], None]):
        """Subscribe to updates for all jobs."""
        self._global_subscribers.append(callback)
    
    def unsubscribe(self, job_id: str, callback: Callable):
        """Unsubscribe from job updates."""
        if job_id in self._subscribers:
            try:
                self._subscribers[job_id].remove(callback)
            except ValueError:
                pass
    
    def _notify_subscribers(self, job: Job):
        """Notify all subscribers of a job update."""
        # Job-specific subscribers
        for callback in self._subscribers.get(job.id, []):
            try:
                callback(job)
            except Exception as e:
                print(f"Subscriber error: {e}")
        
        # Global subscribers
        for callback in self._global_subscribers:
            try:
                callback(job)
            except Exception as e:
                print(f"Global subscriber error: {e}")
    
    def cleanup_old_jobs(self, max_age_hours: int = 24):
        """Remove completed jobs older than max_age_hours."""
        cutoff = datetime.now().timestamp() - (max_age_hours * 3600)
        
        to_remove = [
            job_id for job_id, job in self._jobs.items()
            if job.status in [JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED]
            and job.completed_at
            and job.completed_at.timestamp() < cutoff
        ]
        
        for job_id in to_remove:
            del self._jobs[job_id]
            if job_id in self._subscribers:
                del self._subscribers[job_id]


class JobContext:
    """
    Context object passed to job functions for progress reporting.
    
    Usage in job function:
        def my_job(arg1, arg2, job_context: JobContext):
            job_context.update("Starting...", 10)
            # do work
            job_context.log("Step 1 complete", "success")
            job_context.update("Processing...", 50)
            # more work
            job_context.update("Finishing...", 90)
    """
    
    def __init__(self, job: Job, notify_callback: Callable):
        self._job = job
        self._notify = notify_callback
    
    @property
    def job_id(self) -> str:
        return self._job.id
    
    @property
    def is_cancelled(self) -> bool:
        return self._job.status == JobStatus.CANCELLED
    
    def update(self, step_name: str, progress: int):
        """Update job progress and current step."""
        self._job.current_step = step_name
        self._job.progress = min(progress, self._job.total_steps)
        self._job.add_log(step_name, "info", progress)
        self._notify(self._job)
    
    def log(self, message: str, level: str = "info"):
        """Add a log message without changing progress."""
        self._job.add_log(message, level)
        self._notify(self._job)
    
    def set_metadata(self, key: str, value: Any):
        """Set metadata on the job."""
        self._job.metadata[key] = value


# Global job manager instance
_job_manager: Optional[JobManager] = None


def get_job_manager() -> JobManager:
    """Get the global job manager instance."""
    global _job_manager
    if _job_manager is None:
        _job_manager = JobManager()
    return _job_manager


# ============ Job Functions ============

def deploy_app_job(
    domain: str,
    source: str,
    app_type: str,
    port: Optional[int] = None,
    branch: str = "main",
    env_vars: Optional[Dict[str, str]] = None,
    job_context: Optional[JobContext] = None,
) -> dict:
    """
    Deploy an application as a background job.
    
    This is the job function that wraps the actual deployment logic.
    """
    import subprocess
    import shlex
    
    if job_context is None:
        raise ValueError("job_context is required")
    
    job_context.set_metadata("domain", domain)
    job_context.set_metadata("source", source)
    job_context.set_metadata("app_type", app_type)
    
    # Build the wasm command (--verbose is a global flag, must come before the subcommand)
    cmd = ["wasm", "--verbose", "create", "-d", domain, "-s", source, "-t", app_type]
    
    if port:
        cmd.extend(["-p", str(port)])
    if branch:
        cmd.extend(["-b", branch])
    
    job_context.update("Starting deployment...", 5)
    job_context.log(f"Command: {' '.join(cmd)}")
    
    # Execute with real-time output
    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    
    step_patterns = {
        "Fetching source": 10,
        "Installing dependencies": 30,
        "Building": 50,
        "Creating service": 70,
        "Configuring": 80,
        "SSL": 90,
        "Deployment complete": 100,
    }
    
    current_progress = 5
    
    for line in process.stdout:
        line = line.strip()
        if not line:
            continue
        
        # Determine log level
        level = "info"
        if "error" in line.lower() or "failed" in line.lower():
            level = "error"
        elif "warning" in line.lower() or "⚠" in line:
            level = "warning"
        elif "✓" in line or "success" in line.lower():
            level = "success"
        
        job_context.log(line, level)
        
        # Update progress based on patterns
        for pattern, progress in step_patterns.items():
            if pattern.lower() in line.lower():
                current_progress = progress
                job_context.update(pattern, progress)
                break
        
        if job_context.is_cancelled:
            process.terminate()
            raise Exception("Job cancelled by user")
    
    process.wait()
    
    if process.returncode != 0:
        raise Exception(f"Deployment failed with exit code {process.returncode}")
    
    job_context.update("Deployment complete", 100)
    
    return {
        "domain": domain,
        "app_type": app_type,
        "status": "deployed",
    }


def update_app_job(
    domain: str,
    job_context: Optional[JobContext] = None,
) -> dict:
    """Update an application as a background job."""
    if job_context is None:
        raise ValueError("job_context is required")
    
    job_context.set_metadata("domain", domain)
    job_context.update("Starting update...", 5)
    
    cmd = ["wasm", "--verbose", "update", domain]
    
    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    
    for line in process.stdout:
        line = line.strip()
        if line:
            level = "error" if "error" in line.lower() else "info"
            job_context.log(line, level)
    
    process.wait()
    
    if process.returncode != 0:
        raise Exception(f"Update failed with exit code {process.returncode}")
    
    job_context.update("Update complete", 100)
    
    return {"domain": domain, "status": "updated"}


def delete_app_job(
    domain: str,
    remove_files: bool = True,
    remove_ssl: bool = True,
    job_context: Optional[JobContext] = None,
) -> dict:
    """Delete an application as a background job."""
    if job_context is None:
        raise ValueError("job_context is required")
    
    job_context.set_metadata("domain", domain)
    job_context.update("Starting deletion...", 10)
    
    cmd = ["wasm", "--verbose", "delete", domain, "-y"]
    
    if not remove_files:
        cmd.append("--keep-files")
    if not remove_ssl:
        cmd.append("--keep-ssl")
    
    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    
    for line in process.stdout:
        line = line.strip()
        if line:
            job_context.log(line, "info")
    
    process.wait()
    
    if process.returncode != 0:
        raise Exception(f"Deletion failed with exit code {process.returncode}")
    
    job_context.update("Deletion complete", 100)
    
    return {"domain": domain, "status": "deleted"}


def backup_app_job(
    domain: str,
    job_context: Optional[JobContext] = None,
) -> dict:
    """Create a backup as a background job."""
    if job_context is None:
        raise ValueError("job_context is required")
    
    job_context.set_metadata("domain", domain)
    job_context.update("Creating backup...", 20)
    
    cmd = ["wasm", "--verbose", "backup", "create", domain]
    
    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    
    backup_path = None
    for line in process.stdout:
        line = line.strip()
        if line:
            job_context.log(line, "info")
            if "backup" in line.lower() and "/" in line:
                backup_path = line
    
    process.wait()
    
    if process.returncode != 0:
        raise Exception(f"Backup failed with exit code {process.returncode}")
    
    job_context.update("Backup complete", 100)
    
    return {"domain": domain, "status": "backup_created", "path": backup_path}


def rollback_app_job(
    domain: str,
    backup_id: Optional[str] = None,
    job_context: Optional[JobContext] = None,
) -> dict:
    """Rollback an application as a background job."""
    if job_context is None:
        raise ValueError("job_context is required")
    
    job_context.set_metadata("domain", domain)
    job_context.update("Starting rollback...", 10)
    
    cmd = ["wasm", "--verbose", "rollback", domain]
    if backup_id:
        cmd.extend(["--backup", backup_id])
    
    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    
    for line in process.stdout:
        line = line.strip()
        if line:
            job_context.log(line, "info")
    
    process.wait()
    
    if process.returncode != 0:
        raise Exception(f"Rollback failed with exit code {process.returncode}")
    
    job_context.update("Rollback complete", 100)
    
    return {"domain": domain, "status": "rolled_back"}


def cert_create_job(
    domain: str,
    email: Optional[str] = None,
    job_context: Optional[JobContext] = None,
) -> dict:
    """Create SSL certificate as a background job."""
    if job_context is None:
        raise ValueError("job_context is required")
    
    job_context.set_metadata("domain", domain)
    job_context.update("Requesting certificate...", 20)
    
    cmd = ["wasm", "--verbose", "cert", "create", "-d", domain]
    if email:
        cmd.extend(["-e", email])
    
    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    
    for line in process.stdout:
        line = line.strip()
        if line:
            job_context.log(line, "info")
    
    process.wait()
    
    if process.returncode != 0:
        raise Exception(f"Certificate creation failed with exit code {process.returncode}")
    
    job_context.update("Certificate created", 100)
    
    return {"domain": domain, "status": "certificate_created"}
