# Copyright (c) 2024-2026 Yago Lopez Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
Tests for scheduled backups: the API and the backups page section.

The scheduler writes root-owned systemd units and runs systemctl, so what is
asserted here is the exact argv the manager builds and the exact unit files
it writes - through the FakeRunner and a systemd directory inside tmp_path,
never a real process. The API is a thin client of the scheduler; the section
is a thin client of the API; these tests hold both to that.

The refusal that matters most: a calendar expression the scheduler would not
write into a unit answers 422 with the scheduler's own words, and nothing is
written. A schedule that is half-created is a backup that silently never
happens, which is the worst state a backup feature can have.
"""

# The web fixtures are imported from test_web_views rather than replicated, so
# there stays one definition of "a signed-in panel client". Ruff reads a test
# parameter named after an imported fixture as a redefinition; here it is the
# mechanism.
# ruff: noqa: F811

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.test_web_views import (  # noqa: F401  (pytest resolves fixtures by name)
    MISSING_MARKER,
    anonymous,
    app,
    body_of,
    client,
    config_file,
    deploy,
    store,
)
from wasm.core.runner import FakeRunner
from wasm.core.store import WASMStore
from wasm.managers.backup_scheduler import BackupScheduler

#: What ``systemctl list-timers --no-legend`` prints for one WASM timer. The
#: columns are the human ones a real systemd emits; the manager must find the
#: unit by its suffix, not by position, or the service in the ACTIVATES column
#: is mistaken for the timer.
LIST_TIMERS_LINE = (
    "Sat 2026-08-15 02:00:00 UTC 5h left "
    "Fri 2026-08-14 02:00:00 UTC 19h ago "
    "wasm-backup-example-com.timer wasm-backup-example-com.service\n"
)

#: What ``systemctl show`` answers about that timer.
SHOW_TIMER_OUTPUT = (
    "Description=WASM backup timer for example.com\n"
    "TimersCalendar={ OnCalendar=*-*-* 02:00:00 ; next_elapse=Sat 2026-08-15 02:00:00 UTC }\n"
    "LastTriggerUSec=Fri 2026-08-14 02:00:00 UTC\n"
    "NextElapseUSecRealtime=Sat 2026-08-15 02:00:00 UTC\n"
)

#: A schedule that would append a directive to a root-owned unit file.
INJECTED_CALENDAR = "daily\nOnBootSec=1s"


@pytest.fixture
def systemd_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """
    Point the scheduler's unit directory into the sandbox.

    The API instantiates its own scheduler per request, so the class
    attribute is patched rather than an instance.

    Args:
        tmp_path: Per-test temporary directory.
        monkeypatch: Patching helper, scoped to the test.

    Returns:
        The directory unit files land in.
    """
    path = tmp_path / "systemd"
    path.mkdir()
    monkeypatch.setattr(BackupScheduler, "SYSTEMD_DIR", path)
    return path


def scripted_timer(runner: FakeRunner) -> None:
    """
    Make systemctl report one scheduled backup for example.com.

    Args:
        runner: The fake command runner.
    """
    runner.script(("systemctl", "list-timers"), stdout=LIST_TIMERS_LINE)
    runner.script(("systemctl", "show", "wasm-backup-example-com.timer"), stdout=SHOW_TIMER_OUTPUT)


def written_units(systemd_dir: Path) -> tuple[Path, Path]:
    """
    Args:
        systemd_dir: The patched unit directory.

    Returns:
        The paths the timer and service units land at.
    """
    return (
        systemd_dir / "wasm-backup-example-com.timer",
        systemd_dir / "wasm-backup-example-com.service",
    )


# ------------------------------------------------------------------- the API


def test_creating_a_schedule_writes_both_units_and_enables_the_timer(
    client: TestClient, runner: FakeRunner, systemd_dir: Path
) -> None:
    """POST writes the timer/service pair through the manager and enables it."""
    response = client.post(
        "/api/backup-schedules",
        json={"domain": "example.com", "schedule": "daily", "retention_count": 5},
    )

    assert response.status_code == 201, response.text
    timer, service = written_units(systemd_dir)
    assert "OnCalendar=*-*-* 02:00:00" in timer.read_text()
    assert "wasm backup create example.com" in service.read_text()
    assert ("systemctl", "daemon-reload") in runner.calls
    assert ("systemctl", "enable", "--now", "wasm-backup-example-com.timer") in runner.calls


def test_the_created_schedule_is_echoed_back(
    client: TestClient, runner: FakeRunner, systemd_dir: Path
) -> None:
    """The response carries the schedule as created, retention included."""
    response = client.post(
        "/api/backup-schedules",
        json={"domain": "example.com", "schedule": "weekly", "retention_count": 5},
    )

    created = response.json()["schedule"]
    assert created["domain"] == "example.com"
    assert created["schedule"] == "weekly"
    assert created["on_calendar"] == "Mon *-*-* 02:00:00"
    assert created["retention_count"] == 5


def test_listing_reports_each_timer_with_its_next_run(
    client: TestClient, runner: FakeRunner
) -> None:
    """GET reads systemd through the manager: domain, calendar and run times."""
    scripted_timer(runner)

    payload = client.get("/api/backup-schedules").json()

    assert payload["total"] == 1
    schedule = payload["schedules"][0]
    assert schedule["domain"] == "example.com"
    assert schedule["schedule"] == "daily"
    assert schedule["on_calendar"] == "*-*-* 02:00:00"
    assert schedule["next_run"] == "Sat 2026-08-15 02:00:00 UTC"
    assert schedule["last_run"] == "Fri 2026-08-14 02:00:00 UTC"


def test_deleting_a_schedule_stops_the_timer_and_removes_both_units(
    client: TestClient, runner: FakeRunner, systemd_dir: Path
) -> None:
    """DELETE tears down exactly what create built."""
    timer, service = written_units(systemd_dir)
    timer.write_text("[Timer]\n")
    service.write_text("[Service]\n")

    response = client.delete("/api/backup-schedules/example.com")

    assert response.status_code == 200, response.text
    assert not timer.exists() and not service.exists()
    assert ("systemctl", "stop", "wasm-backup-example-com.timer") in runner.calls
    assert ("systemctl", "disable", "wasm-backup-example-com.timer") in runner.calls
    assert ("systemctl", "daemon-reload") in runner.calls


def test_deleting_a_schedule_that_does_not_exist_answers_404(
    client: TestClient, runner: FakeRunner, systemd_dir: Path
) -> None:
    """A delete that removed nothing must not report success."""
    runner.script(("systemctl", "is-enabled"), exit_code=1)

    response = client.delete("/api/backup-schedules/example.com")

    assert response.status_code == 404
    assert ("systemctl", "stop", "wasm-backup-example-com.timer") not in runner.calls


def test_an_injected_calendar_answers_422_with_the_schedulers_refusal(
    client: TestClient, runner: FakeRunner, systemd_dir: Path
) -> None:
    """
    The expression that would append a directive to a root-owned unit is
    refused by the request model, in the scheduler's own words, and nothing
    is written or enabled.
    """
    response = client.post(
        "/api/backup-schedules",
        json={"domain": "example.com", "schedule": INJECTED_CALENDAR},
    )

    assert response.status_code == 422
    assert "Invalid backup schedule" in response.text
    assert list(systemd_dir.iterdir()) == []
    assert not any(call[:2] == ("systemctl", "enable") for call in runner.calls)


def test_the_api_demands_a_session(anonymous: TestClient) -> None:
    """The schedule endpoints are not a hole in the fence."""
    assert anonymous.get("/api/backup-schedules").status_code in (401, 403)
    assert anonymous.post("/api/backup-schedules", json={"domain": "a.com"}).status_code in (
        401,
        403,
    )
    assert anonymous.delete("/api/backup-schedules/a.com").status_code in (401, 403)


# --------------------------------------------------------------- the section


def test_the_backups_page_loads_the_section(client: TestClient) -> None:
    """The page hands the browser the section's address."""
    body = body_of(client, "/backups")

    assert 'hx-get="/backups/schedules"' in body
    assert "Scheduled backups" in body


def test_the_section_renders_each_schedule(
    client: TestClient, runner: FakeRunner, store: WASMStore
) -> None:
    """The table names the domain, the calendar in mono and the next run."""
    deploy(store)
    scripted_timer(runner)

    body = body_of(client, "/backups/schedules")

    assert MISSING_MARKER not in body
    assert "example.com" in body
    assert "*-*-* 02:00:00" in body
    assert "Sat 2026-08-15 02:00:00 UTC" in body


def test_deleting_from_the_section_confirms_and_names_the_domain(
    client: TestClient, runner: FakeRunner, store: WASMStore
) -> None:
    """The Delete button asks first, and the question is not "are you sure"."""
    deploy(store)
    scripted_timer(runner)

    body = body_of(client, "/backups/schedules")

    assert 'hx-delete="/api/backup-schedules/example.com"' in body
    assert "Delete the backup schedule for example.com?" in body


def test_the_form_offers_the_deployed_applications(client: TestClient, store: WASMStore) -> None:
    """The select is built from what is deployed, not typed free-hand."""
    deploy(store)
    deploy(store, domain="other.example.com")

    body = body_of(client, "/backups/schedules")

    assert '<option value="example.com"' in body
    assert '<option value="other.example.com"' in body


def test_creating_from_the_form_reaches_the_manager(
    client: TestClient, runner: FakeRunner, store: WASMStore, systemd_dir: Path
) -> None:
    """The htmx form drives the same create the JSON API drives."""
    deploy(store)

    response = client.post(
        "/backups/schedules",
        data={
            "domain": "example.com",
            "schedule": "daily",
            "retention_count": "7",
            "retention_days": "30",
        },
    )

    assert response.status_code == 200, response.text
    assert "Backup schedule created for example.com" in response.text
    timer, service = written_units(systemd_dir)
    assert timer.exists() and service.exists()
    assert ("systemctl", "enable", "--now", "wasm-backup-example-com.timer") in runner.calls


def test_a_refused_calendar_renders_inline_and_writes_nothing(
    client: TestClient, runner: FakeRunner, store: WASMStore, systemd_dir: Path
) -> None:
    """
    htmx does not swap an error status, so the refusal answers 200 with the
    scheduler's words on the fragment - and no unit half-written behind it.
    """
    deploy(store)

    response = client.post(
        "/backups/schedules",
        data={
            "domain": "example.com",
            "schedule": "custom",
            "on_calendar": INJECTED_CALENDAR,
            "retention_count": "7",
            "retention_days": "30",
        },
    )

    assert response.status_code == 200
    assert "Invalid backup schedule" in response.text
    assert list(systemd_dir.iterdir()) == []


def test_the_section_demands_a_session(anonymous: TestClient) -> None:
    """The section redirects to the sign-in form, like every page."""
    response = anonymous.get("/backups/schedules")

    assert response.status_code == 303
    assert response.headers["location"] == "/login"
