# Copyright (c) 2024-2026 Yago Lopez Prado
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Tests for ``/api/cron``.

Ported from the deleted server-rendered cron page, which was a thin client of
this same JSON API. What is asserted is the exact argv systemctl receives and
the exact unit files written into a sandboxed systemd directory - never a
real process.

The refusals matter most: a calendar the manager would not write answers with
the manager's own words and nothing half-written, and a unit WASM does not
own is refused rather than acted on.

This file is also read by the ``pydantic-v1`` CI job: the request model's
calendar validator goes through :mod:`wasm.web.pydantic_compat`, so it has to
keep passing under both pydantic major versions.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from wasm.core.runner import FakeRunner
from wasm.core.store import WASMStore
from wasm.managers.cron_manager import CronManager
from wasm.web.auth import CSRF_HEADER_NAME, SecurityConfig
from wasm.web.server import create_app, get_token_manager

#: What ``systemctl list-unit-files`` prints for one enabled WASM cron timer.
LIST_UNIT_FILES_LINE = "wasm-cron-cleanup.timer enabled enabled\n"

#: What ``systemctl show`` answers about that timer.
SHOW_TIMER_OUTPUT = (
    "TimersCalendar={ OnCalendar=*-*-* 02:00:00 ; next_elapse=Sat 2026-08-15 02:00:00 UTC }\n"
    "LastTriggerUSec=Fri 2026-08-14 02:00:00 UTC\n"
    "NextElapseUSecRealtime=Sat 2026-08-15 02:00:00 UTC\n"
)

#: What ``systemctl show`` answers about the service after a failing run.
SHOW_SERVICE_FAILED = (
    "ExecMainStatus=2\nExecMainExitTimestamp=Fri 2026-08-14 02:00:05 UTC\nResult=exit-code\n"
)

#: A schedule that would append a directive to a root-owned unit file.
INJECTED_CALENDAR = "daily\nOnBootSec=1s"

#: Journal JSON for one run whose output carries markup, so the escaping the
#: page used to be responsible for stays proven at the source: the API hands
#: this text back verbatim, and it is the caller's job to escape it now.
JOURNAL_OUTPUT = "\n".join(
    [
        '{"__REALTIME_TIMESTAMP": "1700000000000000",'
        ' "MESSAGE": "ERROR: disk full <b>boom-11</b>", "_SYSTEMD_INVOCATION_ID": "aaa"}',
        '{"__REALTIME_TIMESTAMP": "1700000001000000", "UNIT": "wasm-cron-cleanup.service",'
        ' "INVOCATION_ID": "aaa", "EXIT_CODE": "exited", "EXIT_STATUS": "2"}',
    ]
)


@pytest.fixture
def store(tmp_path: Path) -> Iterator[WASMStore]:
    """
    Args:
        tmp_path: Per-test temporary directory.

    Yields:
        A store of this test's own.
    """
    WASMStore.reset_instance()
    instance = WASMStore(tmp_path / "wasm.db")
    try:
        yield instance
    finally:
        instance.close()
        WASMStore.reset_instance()


@pytest.fixture
def app(tmp_path: Path, store: WASMStore, runner: FakeRunner) -> FastAPI:
    """
    Args:
        tmp_path: Per-test temporary directory.
        store: The store fixture.
        runner: The fake command runner, so no manager reaches a real process.

    Returns:
        The application.
    """
    return create_app(SecurityConfig(state_dir=tmp_path / "state", rate_limit_requests=5000))


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    """
    Args:
        app: The application.

    Returns:
        A signed-in client carrying the CSRF header.
    """
    signed_in = TestClient(app, client=("testclient", 50000), follow_redirects=False)
    token = get_token_manager().generate_master_token()
    response = signed_in.post("/api/auth/login", json={"token": token})
    assert response.status_code == 200, response.text
    signed_in.headers[CSRF_HEADER_NAME] = response.json()["csrf_token"]
    return signed_in


def elevate(client: TestClient) -> None:
    """
    Confirm sudo mode for the signed-in client.

    Args:
        client: A signed-in client.
    """
    response = client.post(
        "/api/auth/elevate", json={"token": get_token_manager().generate_master_token()}
    )
    assert response.status_code == 200, response.text


@pytest.fixture
def anonymous(app: FastAPI) -> TestClient:
    """
    Args:
        app: The application.

    Returns:
        A client with no session.
    """
    return TestClient(app, client=("testclient", 50000), follow_redirects=False)


@pytest.fixture
def systemd_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """
    Point the manager's unit directory into the sandbox.

    The API instantiates its own manager per request, so the class attribute
    is patched rather than an instance.

    Args:
        tmp_path: Per-test temporary directory.
        monkeypatch: Patching helper, scoped to the test.

    Returns:
        The directory unit files land in.
    """
    path = tmp_path / "systemd"
    path.mkdir()
    monkeypatch.setattr(CronManager, "SYSTEMD_DIR", path)
    return path


def write_owned_pair(systemd_dir: Path) -> tuple[Path, Path]:
    """
    Write a unit pair the way the manager would, marker included.

    Args:
        systemd_dir: The patched unit directory.

    Returns:
        The timer and service paths.
    """
    timer = systemd_dir / "wasm-cron-cleanup.timer"
    service = systemd_dir / "wasm-cron-cleanup.service"
    timer.write_text("# Generated by WASM\n[Timer]\nOnCalendar=*-*-* 02:00:00\n")
    service.write_text(
        "# Generated by WASM\n# Application: example.com\n[Service]\nUser=www-data\n"
        "ExecStart=/usr/bin/find /tmp/caches -delete\n"
    )
    return timer, service


def scripted_job(runner: FakeRunner) -> None:
    """
    Make systemctl report one cron job named cleanup.

    Args:
        runner: The fake command runner.
    """
    runner.script(("systemctl", "list-unit-files"), stdout=LIST_UNIT_FILES_LINE)
    runner.script(("systemctl", "show", "wasm-cron-cleanup.timer"), stdout=SHOW_TIMER_OUTPUT)
    runner.script(("systemctl", "show", "wasm-cron-cleanup.service"), stdout=SHOW_SERVICE_FAILED)


def test_the_api_demands_a_session(anonymous: TestClient) -> None:
    """The cron endpoints are not a hole in the fence."""
    assert anonymous.get("/api/cron").status_code in (401, 403)
    assert anonymous.post("/api/cron", json={"name": "x", "command": "/bin/true"}).status_code in (
        401,
        403,
    )
    assert anonymous.delete("/api/cron/x").status_code in (401, 403)
    assert anonymous.post("/api/cron/x/run").status_code in (401, 403)
    assert anonymous.post("/api/cron/x/enable").status_code in (401, 403)
    assert anonymous.post("/api/cron/x/disable").status_code in (401, 403)
    assert anonymous.get("/api/cron/x/runs").status_code in (401, 403)


def test_the_json_api_lists_jobs_with_their_last_result(
    client: TestClient, runner: FakeRunner, systemd_dir: Path
) -> None:
    """A Bearer client reads the listing with the job's schedule and last exit."""
    write_owned_pair(systemd_dir)
    scripted_job(runner)

    payload = client.get("/api/cron").json()

    assert payload["total"] == 1
    entry = payload["jobs"][0]
    assert entry["name"] == "cleanup"
    assert entry["schedule"] == "daily"
    assert entry["on_calendar"] == "*-*-* 02:00:00"
    assert entry["last_exit_code"] == 2
    assert entry["enabled"] is True


def test_the_json_api_reports_history(
    client: TestClient, runner: FakeRunner, systemd_dir: Path
) -> None:
    """GET /api/cron/{name}/runs carries the exit code and the output verbatim."""
    write_owned_pair(systemd_dir)
    runner.script(("journalctl",), stdout=JOURNAL_OUTPUT)

    payload = client.get("/api/cron/cleanup/runs").json()

    assert payload["total"] == 1
    run = payload["runs"][0]
    assert run["exit_code"] == 2
    assert run["success"] is False
    assert "ERROR: disk full" in run["output"]
    # Verbatim: the API does no HTML escaping of its own, that is a client concern.
    assert "<b>boom-11</b>" in run["output"]


#: What ``systemd-analyze calendar --iterations=5`` prints for a daily schedule.
CALENDAR_PREVIEW_OUTPUT = (
    "  Original form: daily\n"
    "Normalized form: *-*-* 00:00:00\n"
    "    Next elapse: Thu 2026-01-01 00:00:00 UTC\n"
    "       (in UTC): Thu 2026-01-01 00:00:00 UTC\n"
    "       From now: 10h left\n"
    "\n"
    "      Iter. #2: Fri 2026-01-02 00:00:00 UTC\n"
    "       (in UTC): Fri 2026-01-02 00:00:00 UTC\n"
    "       From now: 1 day 10h left\n"
    "\n"
    "      Iter. #3: Sat 2026-01-03 00:00:00 UTC\n"
    "       (in UTC): Sat 2026-01-03 00:00:00 UTC\n"
    "       From now: 2 days 10h left\n"
    "\n"
    "      Iter. #4: Sun 2026-01-04 00:00:00 UTC\n"
    "       (in UTC): Sun 2026-01-04 00:00:00 UTC\n"
    "       From now: 3 days 10h left\n"
    "\n"
    "      Iter. #5: Mon 2026-01-05 00:00:00 UTC\n"
    "       (in UTC): Mon 2026-01-05 00:00:00 UTC\n"
    "       From now: 4 days 10h left\n"
)


def test_preview_returns_the_normalised_calendar_and_five_next_runs(
    client: TestClient, runner: FakeRunner
) -> None:
    """The preview the new-job dialog shows before anything is written."""
    runner.script(
        ["systemd-analyze", "calendar", "--iterations=5", "*-*-* 02:00:00"],
        stdout=CALENDAR_PREVIEW_OUTPUT,
    )

    response = client.post("/api/cron/preview", json={"schedule": "daily"})

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["calendar"] == "*-*-* 00:00:00"
    assert body["next_runs"] == [
        "2026-01-01T00:00:00+00:00",
        "2026-01-02T00:00:00+00:00",
        "2026-01-03T00:00:00+00:00",
        "2026-01-04T00:00:00+00:00",
        "2026-01-05T00:00:00+00:00",
    ]


def test_preview_runs_systemd_analyze_with_the_expanded_calendar(
    client: TestClient, runner: FakeRunner
) -> None:
    """An alias is expanded before it reaches systemd-analyze, like the manager itself."""
    runner.script(
        ["systemd-analyze", "calendar", "--iterations=5", "*-*-* 02:00:00"],
        stdout=CALENDAR_PREVIEW_OUTPUT.replace("00:00:00", "02:00:00"),
    )

    response = client.post("/api/cron/preview", json={"schedule": "daily"})

    assert response.status_code == 200, response.text
    assert runner.ran("systemd-analyze", "calendar", "--iterations=5", "*-*-* 02:00:00")


def test_preview_refuses_an_injected_calendar_before_running_anything(
    client: TestClient, runner: FakeRunner
) -> None:
    """The existing validator runs first, exactly like job creation."""
    response = client.post("/api/cron/preview", json={"schedule": INJECTED_CALENDAR})

    assert response.status_code == 422
    assert "Invalid cron schedule" in response.text
    assert runner.calls == []


def test_an_injected_calendar_answers_422_with_the_managers_refusal(
    client: TestClient, runner: FakeRunner, systemd_dir: Path
) -> None:
    """The request model refuses what the manager would refuse, at 422."""
    # Creating or rewriting a job is a sudo-mode action.
    elevate(client)
    response = client.post(
        "/api/cron",
        json={"name": "cleanup", "command": "/usr/bin/true", "schedule": INJECTED_CALENDAR},
    )

    assert response.status_code == 422
    assert "Invalid cron schedule" in response.text
    assert list(systemd_dir.iterdir()) == []


def test_creating_a_job_writes_the_units_and_enables_the_timer(
    client: TestClient, runner: FakeRunner, systemd_dir: Path
) -> None:
    """POST writes the timer/service pair through the manager and enables it."""
    # Creating or rewriting a job is a sudo-mode action.
    elevate(client)
    response = client.post(
        "/api/cron",
        json={
            "name": "cleanup",
            "command": "/usr/bin/find /tmp/caches -delete",
            "schedule": "daily",
            "user": "www-data",
            "working_directory": "/srv/caches",
        },
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["success"] is True
    assert body["job"]["name"] == "cleanup"
    timer = systemd_dir / "wasm-cron-cleanup.timer"
    service = systemd_dir / "wasm-cron-cleanup.service"
    assert "OnCalendar=*-*-* 02:00:00" in timer.read_text()
    assert "ExecStart=/usr/bin/find /tmp/caches -delete" in service.read_text()
    assert ("systemctl", "enable", "--now", "wasm-cron-cleanup.timer") in runner.calls


def test_run_now_starts_the_service(
    client: TestClient, runner: FakeRunner, systemd_dir: Path
) -> None:
    """POST .../run drives one systemctl start of the service unit."""
    write_owned_pair(systemd_dir)
    scripted_job(runner)

    response = client.post("/api/cron/cleanup/run")

    assert response.status_code == 200, response.text
    assert ("systemctl", "start", "--no-block", "wasm-cron-cleanup.service") in runner.calls
    assert "Started" in response.json()["message"]


def test_enable_and_disable_drive_the_timer(
    client: TestClient, runner: FakeRunner, systemd_dir: Path
) -> None:
    """Each endpoint posts to the right verb and reports what it did."""
    write_owned_pair(systemd_dir)
    scripted_job(runner)

    disabled = client.post("/api/cron/cleanup/disable")
    enabled = client.post("/api/cron/cleanup/enable")

    assert disabled.status_code == 200, disabled.text
    assert enabled.status_code == 200, enabled.text
    assert ("systemctl", "disable", "--now", "wasm-cron-cleanup.timer") in runner.calls
    assert ("systemctl", "enable", "--now", "wasm-cron-cleanup.timer") in runner.calls
    assert "Disabled" in disabled.json()["message"]
    assert "Enabled" in enabled.json()["message"]


def test_deleting_a_job_requires_elevation(
    client: TestClient, runner: FakeRunner, systemd_dir: Path
) -> None:
    """Removing a job's units needs a recent sudo confirmation, like any other delete."""
    write_owned_pair(systemd_dir)
    scripted_job(runner)

    response = client.delete("/api/cron/cleanup")

    assert response.status_code == 403, response.text
    assert response.json()["error"] == "elevation_required"
    assert not any(call[:2] == ("systemctl", "stop") for call in runner.calls)


def test_deleting_removes_both_units_and_stops_the_timer(
    client: TestClient, runner: FakeRunner, systemd_dir: Path
) -> None:
    """DELETE tears down exactly what create built, once elevated."""
    timer, service = write_owned_pair(systemd_dir)
    scripted_job(runner)
    elevate(client)

    response = client.delete("/api/cron/cleanup")

    assert response.status_code == 200, response.text
    assert not timer.exists() and not service.exists()
    assert ("systemctl", "stop", "wasm-cron-cleanup.timer") in runner.calls


def test_deleting_a_job_that_does_not_exist_answers_404(
    client: TestClient, runner: FakeRunner, systemd_dir: Path
) -> None:
    """A delete that removed nothing must not report success."""
    elevate(client)

    response = client.delete("/api/cron/ghost")

    assert response.status_code == 404
    assert "ghost" in response.text
    assert not any(call[:2] == ("systemctl", "stop") for call in runner.calls)


def test_deleting_a_job_with_the_master_token_is_exempt(
    app: FastAPI, runner: FakeRunner, systemd_dir: Path
) -> None:
    """Automation presenting the master token needs no elevation either."""
    timer, service = write_owned_pair(systemd_dir)
    scripted_job(runner)
    token = get_token_manager().generate_master_token()
    anon = TestClient(app, client=("testclient", 50000), follow_redirects=False)

    response = anon.delete("/api/cron/cleanup", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200, response.text
    assert not timer.exists() and not service.exists()


def test_a_foreign_unit_is_refused_from_the_api_too(
    client: TestClient, runner: FakeRunner, systemd_dir: Path
) -> None:
    """The ownership guard's words reach the caller; nothing is started."""
    (systemd_dir / "wasm-cron-cleanup.timer").write_text("[Timer]\nOnCalendar=hourly\n")

    response = client.post("/api/cron/cleanup/run")

    assert response.status_code >= 400
    assert "does not manage" in response.text
    assert not any(call[:2] == ("systemctl", "start") for call in runner.calls)
