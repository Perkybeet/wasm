# Copyright (c) 2024-2026 Yago Lopez Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
Backup schedules API endpoints.

A thin client of :class:`~wasm.managers.backup_scheduler.BackupScheduler`,
which owns the timer/service unit pair, the systemctl calls and every rule
about what may be written into a root-owned unit file. Three decisions live
here rather than in the handlers' bodies:

- **The calendar is validated in the request model**, through the scheduler's
  own :func:`~wasm.managers.backup_scheduler.validate_calendar`, so a bad
  expression answers ``422`` with the scheduler's exact refusal instead of
  becoming a half-written schedule. There is no second definition of a valid
  expression for the two to disagree over.
- **Retention is forwarded, not enforced.** ``retention_count`` and
  ``retention_days`` are part of the scheduler's contract and travel with the
  request; listings do not repeat them because systemd holds no record of
  them to read back.
- **Every mutation is audited** to ``wasm.audit`` with the session that asked
  for it, like every other mutation the panel can perform.
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator

from wasm.core.exceptions import BackupError
from wasm.core.utils import domain_to_app_name
from wasm.managers.backup_scheduler import (
    SCHEDULE_ALIASES,
    BackupSchedule,
    BackupScheduler,
    validate_calendar,
)
from wasm.web.api.auth import get_current_session
from wasm.web.api.deps import WASMErrorRoute, strict_domain

router = APIRouter(route_class=WASMErrorRoute)

audit_log = logging.getLogger("wasm.audit")

#: The alias each expansion came from, so a listing can say "daily" instead of
#: making an operator parse ``*-*-* 02:00:00``.
_ALIAS_BY_CALENDAR = {calendar: alias for alias, calendar in SCHEDULE_ALIASES.items()}


class BackupScheduleInfo(BaseModel):
    """
    One backup schedule as systemd reports it.

    Attributes:
        domain: Domain the schedule backs up.
        app_name: Application name the unit names are built from.
        timer: Timer unit name, without its ``.timer`` suffix.
        schedule: The schedule in words: an alias such as ``daily`` when the
            expression matches one, ``custom`` otherwise.
        on_calendar: The systemd ``OnCalendar`` expression, verbatim.
        next_run: When the timer fires next, as systemd prints it, or
            ``pending`` when it cannot say.
        last_run: When the timer last fired, or ``never``.
        retention_count: Backups to keep, when known. systemd keeps no record
            of it, so listings report None.
        retention_days: Maximum backup age in days, when known.
    """

    domain: str
    app_name: str
    timer: str
    schedule: str
    on_calendar: str
    next_run: str
    last_run: str
    retention_count: int | None = None
    retention_days: int | None = None


class ScheduleListResponse(BaseModel):
    """Response for listing backup schedules."""

    schedules: list[BackupScheduleInfo]
    total: int


class CreateScheduleRequest(BaseModel):
    """Request to schedule automatic backups for an application."""

    domain: str = Field(..., description="Domain of the app to back up")
    schedule: str = Field(
        default="daily",
        description="hourly, daily, weekly, monthly or a systemd OnCalendar expression",
    )
    retention_count: int = Field(default=7, ge=1, le=365, description="Backups to keep")
    retention_days: int = Field(default=30, ge=1, le=3650, description="Max age in days")
    include_databases: bool = Field(default=True, description="Dump databases too")

    @field_validator("schedule")
    @classmethod
    def _schedule_the_scheduler_accepts(cls, value: str) -> str:
        """
        Refuse here what the scheduler would refuse, in the scheduler's words.

        Args:
            value: The alias or calendar expression as it arrived.

        Returns:
            The value unchanged; the scheduler expands the alias itself.

        Raises:
            ValueError: When the scheduler would not write this into a unit
                file. FastAPI answers it as a 422 with the message as detail.
        """
        try:
            validate_calendar(value)
        except BackupError as exc:
            raise ValueError(f"{exc}. {exc.details}".strip()) from exc
        return value


class ScheduleActionResponse(BaseModel):
    """Response for a schedule action that completed immediately."""

    success: bool
    message: str
    schedule: BackupScheduleInfo | None = None


def _to_info(entry: dict[str, str]) -> BackupScheduleInfo:
    """
    Convert one of the scheduler's listing entries into the API model.

    Args:
        entry: A dictionary as :meth:`BackupScheduler.list_schedules` builds
            it.

    Returns:
        The API representation.
    """
    on_calendar = entry.get("on_calendar", "")
    return BackupScheduleInfo(
        domain=entry.get("domain") or entry.get("app_name", ""),
        app_name=entry.get("app_name", ""),
        timer=entry.get("timer", ""),
        schedule=_ALIAS_BY_CALENDAR.get(on_calendar, "custom" if on_calendar else "unknown"),
        on_calendar=on_calendar,
        next_run=entry.get("next_run", "pending"),
        last_run=entry.get("last_run", "never"),
    )


@router.get("", response_model=ScheduleListResponse)
def list_schedules(
    session: Annotated[dict, Depends(get_current_session)],
) -> ScheduleListResponse:
    """
    List every scheduled backup on this machine, with its next run.

    Args:
        session: The authenticated session.

    Returns:
        The schedules as systemd reports them.
    """
    entries = BackupScheduler(verbose=False).list_schedules()
    return ScheduleListResponse(
        schedules=[_to_info(entry) for entry in entries], total=len(entries)
    )


@router.post("", response_model=ScheduleActionResponse, status_code=201)
def create_schedule(
    data: CreateScheduleRequest, session: Annotated[dict, Depends(get_current_session)]
) -> ScheduleActionResponse:
    """
    Schedule automatic backups of an application on a systemd timer.

    Scheduling the same domain again rewrites its unit pair, so this is also
    how a schedule is changed.

    Args:
        data: The schedule request. Its calendar expression was already
            checked against the scheduler's own rules by the request model.
        session: The authenticated session.

    Returns:
        The action outcome, carrying the schedule as created.

    Raises:
        BackupError: When a unit cannot be written or the timer cannot be
            enabled.
    """
    domain = strict_domain(data.domain)
    schedule = BackupSchedule(
        domain=domain,
        app_name=domain_to_app_name(domain),
        schedule=data.schedule,
        include_databases=data.include_databases,
        retention_count=data.retention_count,
        retention_days=data.retention_days,
    )

    audit_log.info(
        "create_backup_schedule domain=%s schedule=%s session=%s",
        domain,
        schedule.on_calendar,
        session.get("session_id", "unknown"),
    )
    BackupScheduler(verbose=False).create_schedule(schedule)

    return ScheduleActionResponse(
        success=True,
        message=f"Backup schedule created for {domain}",
        schedule=BackupScheduleInfo(
            domain=domain,
            app_name=schedule.app_name,
            timer=schedule.timer_name,
            schedule=data.schedule if data.schedule in SCHEDULE_ALIASES else "custom",
            on_calendar=schedule.on_calendar,
            next_run="pending",
            last_run="never",
            retention_count=data.retention_count,
            retention_days=data.retention_days,
        ),
    )


@router.delete("/{domain}", response_model=ScheduleActionResponse)
def delete_schedule(
    domain: str, session: Annotated[dict, Depends(get_current_session)]
) -> ScheduleActionResponse:
    """
    Remove an application's backup schedule.

    Args:
        domain: Domain whose schedule is removed.
        session: The authenticated session.

    Returns:
        The action outcome.

    Raises:
        HTTPException: 404 when no schedule exists for the domain, so deleting
            a schedule that was never created does not report success.
    """
    validated = strict_domain(domain)
    scheduler = BackupScheduler(verbose=False)
    if scheduler.get_schedule(validated) is None:
        raise HTTPException(status_code=404, detail=f"No backup schedule for {validated}")

    audit_log.info(
        "delete_backup_schedule domain=%s session=%s",
        validated,
        session.get("session_id", "unknown"),
    )
    scheduler.remove_schedule(validated)

    return ScheduleActionResponse(success=True, message=f"Backup schedule removed for {validated}")
