# Copyright (c) 2024-2026 Yago Lopez Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
Cron jobs API endpoints.

A thin client of :class:`~wasm.managers.cron_manager.CronManager`, which owns
the timer/service unit pair, the systemctl calls, the ownership guard and
every rule about what may be written into a root-owned unit file. Three
decisions live here rather than in the handlers' bodies:

- **The calendar is validated in the request model**, through the scheduler's
  single :func:`~wasm.managers.backup_scheduler.validate_calendar` (as
  re-worded by :func:`~wasm.managers.cron_manager.validate_cron_calendar`),
  so a bad expression answers ``422`` with the refusal instead of becoming a
  half-written unit pair.
- **The command travels as one line and runs as an argv.** The manager splits
  it with shlex and writes it token by token; no shell exists anywhere in the
  path, and the API does not pretend otherwise.
- **Every mutation is audited** to ``wasm.audit`` with the session that asked
  for it, like every other mutation the panel can perform.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, field_validator

from wasm.core.exceptions import ServiceError
from wasm.managers.backup_scheduler import SCHEDULE_ALIASES
from wasm.managers.cron_manager import CronJob, CronManager, validate_cron_calendar
from wasm.web.api.auth import get_current_session
from wasm.web.api.deps import WASMErrorRoute

router = APIRouter(route_class=WASMErrorRoute)

audit_log = logging.getLogger("wasm.audit")

#: The alias each expansion came from, so a listing can say "daily" instead of
#: making an operator parse ``*-*-* 02:00:00``.
_ALIAS_BY_CALENDAR = {calendar: alias for alias, calendar in SCHEDULE_ALIASES.items()}


class CronJobInfo(BaseModel):
    """
    One cron job as systemd reports it.

    Attributes:
        name: Job name the ``wasm-cron-{name}`` unit names are built from.
        command: The unit's ``ExecStart`` value, verbatim.
        user: Unix user the command runs as.
        working_directory: Directory the command runs in, empty when unset.
        app_domain: Domain of the associated application, empty when none.
        schedule: The schedule in words: an alias such as ``daily`` when the
            expression matches one, ``custom`` otherwise.
        on_calendar: The systemd ``OnCalendar`` expression, verbatim.
        enabled: Whether the timer is enabled.
        next_run: When the timer fires next, as systemd prints it, or
            ``pending`` when it cannot say.
        last_run: When the timer last fired, or ``never``.
        last_exit_code: Exit code of the last run, or None when the job never
            ran.
        last_result: Systemd's ``Result`` for the last run, or ``never ran``.
    """

    name: str
    command: str
    user: str
    working_directory: str
    app_domain: str
    schedule: str
    on_calendar: str
    enabled: bool
    next_run: str
    last_run: str
    last_exit_code: int | None
    last_result: str


class CronJobListResponse(BaseModel):
    """Response for listing cron jobs."""

    jobs: list[CronJobInfo]
    total: int


class CreateCronJobRequest(BaseModel):
    """Request to create (or rewrite) a cron job."""

    name: str = Field(..., min_length=1, max_length=64, description="Job name")
    command: str = Field(
        ...,
        min_length=1,
        description="One command line, split with shlex and run without a shell",
    )
    schedule: str = Field(
        default="daily",
        description="hourly, daily, weekly, monthly or a systemd OnCalendar expression",
    )
    user: str | None = Field(
        default=None, description="Unix user to run as; the configured service_user by default"
    )
    working_directory: str | None = Field(default=None, description="Absolute directory to run in")
    app_domain: str | None = Field(
        default=None,
        description="Associate with a deployed app; its directory becomes the default",
    )

    @field_validator("schedule")
    @classmethod
    def _schedule_the_manager_accepts(cls, value: str) -> str:
        """
        Refuse here what the manager would refuse, in the manager's words.

        Args:
            value: The alias or calendar expression as it arrived.

        Returns:
            The value unchanged; the manager expands the alias itself.

        Raises:
            ValueError: When the manager would not write this into a unit
                file. FastAPI answers it as a 422 with the message as detail.
        """
        try:
            validate_cron_calendar(value)
        except ServiceError as exc:
            raise ValueError(f"{exc}. {exc.details}".strip()) from exc
        return value


class CronActionResponse(BaseModel):
    """Response for a cron job action that completed immediately."""

    success: bool
    message: str
    job: CronJobInfo | None = None


class CronRunInfo(BaseModel):
    """
    One recorded execution of a cron job.

    Attributes:
        started: When the run started, in UTC, or empty when the journal did
            not say.
        exit_code: The run's exit code, or None while it is unknown.
        success: Whether the run succeeded, or None while it is unknown.
        output: The job's own output for this run, verbatim.
    """

    started: str
    exit_code: int | None
    success: bool | None
    output: str


class CronRunsResponse(BaseModel):
    """Response for a cron job's execution history."""

    name: str
    runs: list[CronRunInfo]
    total: int


def _to_info(entry: dict[str, Any]) -> CronJobInfo:
    """
    Convert one of the manager's listing entries into the API model.

    Args:
        entry: A dictionary as :meth:`CronManager.list_jobs` builds it.

    Returns:
        The API representation.
    """
    on_calendar = entry.get("on_calendar", "")
    return CronJobInfo(
        name=entry.get("name", ""),
        command=entry.get("command", ""),
        user=entry.get("user", ""),
        working_directory=entry.get("working_directory", ""),
        app_domain=entry.get("app_domain", ""),
        schedule=_ALIAS_BY_CALENDAR.get(on_calendar, "custom" if on_calendar else "unknown"),
        on_calendar=on_calendar,
        enabled=bool(entry.get("enabled")),
        next_run=entry.get("next_run", "pending"),
        last_run=entry.get("last_run", "never"),
        last_exit_code=entry.get("last_exit_code"),
        last_result=entry.get("last_result", "never ran"),
    )


@router.get("", response_model=CronJobListResponse)
def list_jobs(session: Annotated[dict, Depends(get_current_session)]) -> CronJobListResponse:
    """
    List every WASM cron job with its next run and last result.

    Args:
        session: The authenticated session.

    Returns:
        The jobs as systemd reports them.
    """
    entries = CronManager(verbose=False).list_jobs()
    return CronJobListResponse(jobs=[_to_info(entry) for entry in entries], total=len(entries))


@router.post("", response_model=CronActionResponse, status_code=201)
def create_job(
    data: CreateCronJobRequest, session: Annotated[dict, Depends(get_current_session)]
) -> CronActionResponse:
    """
    Create a cron job as a systemd timer, or rewrite one WASM already owns.

    Args:
        data: The job request. Its calendar expression was already checked
            against the manager's own rules by the request model.
        session: The authenticated session.

    Returns:
        The action outcome, carrying the job as created.

    Raises:
        ServiceError: When a value is unusable, a unit belongs to someone
            else, or the timer cannot be written or enabled.
    """
    audit_log.info(
        "create_cron_job name=%s schedule=%s session=%s",
        data.name,
        data.schedule,
        session.get("session_id", "unknown"),
    )
    manager = CronManager(verbose=False)
    created = manager.create_job(
        CronJob(
            name=data.name,
            command=data.command,
            schedule=data.schedule,
            user=data.user,
            working_directory=data.working_directory,
            app_domain=data.app_domain,
        )
    )
    entry = manager.get_job(created.name)

    return CronActionResponse(
        success=True,
        message=f"Cron job {created.name} created",
        job=_to_info(entry) if entry else None,
    )


@router.delete("/{name}", response_model=CronActionResponse)
def delete_job(
    name: str, session: Annotated[dict, Depends(get_current_session)]
) -> CronActionResponse:
    """
    Remove a cron job's timer and service units.

    Args:
        name: Job name.
        session: The authenticated session.

    Returns:
        The action outcome.

    Raises:
        HTTPException: 404 when no owned job exists for the name, so deleting
            a job that was never created does not report success.
        ServiceError: When the units are not WASM's.
    """
    manager = CronManager(verbose=False)
    if manager.get_job(name) is None:
        raise HTTPException(status_code=404, detail=f"No cron job named {name}")

    audit_log.info("delete_cron_job name=%s session=%s", name, session.get("session_id", "unknown"))
    manager.delete_job(name)

    return CronActionResponse(success=True, message=f"Cron job {name} deleted")


@router.post("/{name}/run", response_model=CronActionResponse)
def run_job(
    name: str, session: Annotated[dict, Depends(get_current_session)]
) -> CronActionResponse:
    """
    Start a job's service immediately, outside its schedule.

    Args:
        name: Job name.
        session: The authenticated session.

    Returns:
        The action outcome. The run's own result lands in the job's history,
        exactly as a scheduled run would.

    Raises:
        ServiceError: When the job is unknown, foreign, or refuses to start.
    """
    audit_log.info("run_cron_job name=%s session=%s", name, session.get("session_id", "unknown"))
    unit = CronManager(verbose=False).run_now(name)
    return CronActionResponse(success=True, message=f"Started {unit}")


@router.post("/{name}/enable", response_model=CronActionResponse)
def enable_job(
    name: str, session: Annotated[dict, Depends(get_current_session)]
) -> CronActionResponse:
    """
    Enable a job's timer.

    Args:
        name: Job name.
        session: The authenticated session.

    Returns:
        The action outcome.

    Raises:
        ServiceError: When the job is unknown, foreign, or systemd refuses.
    """
    audit_log.info("enable_cron_job name=%s session=%s", name, session.get("session_id", "unknown"))
    unit = CronManager(verbose=False).enable_job(name)
    return CronActionResponse(success=True, message=f"Enabled {unit}")


@router.post("/{name}/disable", response_model=CronActionResponse)
def disable_job(
    name: str, session: Annotated[dict, Depends(get_current_session)]
) -> CronActionResponse:
    """
    Disable a job's timer, keeping the unit files.

    Args:
        name: Job name.
        session: The authenticated session.

    Returns:
        The action outcome.

    Raises:
        ServiceError: When the job is unknown, foreign, or systemd refuses.
    """
    audit_log.info(
        "disable_cron_job name=%s session=%s", name, session.get("session_id", "unknown")
    )
    unit = CronManager(verbose=False).disable_job(name)
    return CronActionResponse(success=True, message=f"Disabled {unit}")


@router.get("/{name}/runs", response_model=CronRunsResponse)
def job_runs(
    name: str,
    session: Annotated[dict, Depends(get_current_session)],
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
) -> CronRunsResponse:
    """
    Read a job's recent executions from the journal.

    Args:
        name: Job name.
        session: The authenticated session.
        limit: Most runs to return.

    Returns:
        The runs, newest first, each with its exit code and its own output
        verbatim.

    Raises:
        ServiceError: When the job is unknown or foreign.
    """
    runs = CronManager(verbose=False).runs(name, limit=limit)
    return CronRunsResponse(
        name=name,
        runs=[
            CronRunInfo(
                started=run["started"],
                exit_code=run["exit_code"],
                success=run["success"],
                output="\n".join(run["lines"]),
            )
            for run in runs
        ],
        total=len(runs),
    )
