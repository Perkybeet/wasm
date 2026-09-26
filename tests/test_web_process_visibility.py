# Copyright (c) 2024-2026 Yago López Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
Process command lines must not reach a credential below admin scope.

``GET /api/system/processes`` and ``GET /api/monitor/processes`` both build
their response from a process's argv, and argv routinely carries a secret a
process was started with - a database password, a bearer token - as a plain
CLI flag. A ``read``-scoped API token is meant to observe the machine, not to
read every other process's secrets off it, so both endpoints must fall back
to the process name alone for anything short of admin. These tests exercise
the real app and real credentials, the way tests/test_web_auth.py does, so
the scope check under test is the one FastAPI actually runs, not a stand-in.
"""

from __future__ import annotations

import types
from pathlib import Path
from typing import Any

import psutil
import pytest
from fastapi.testclient import TestClient

from tests.test_web_auth import bearer, build_client, issue_token, login
from wasm.monitor.models import ProcessInfo
from wasm.web.api import monitor as monitor_api
from wasm.web.server import get_token_manager

#: What a secret-carrying process looks like on the wire: a password passed
#: as a CLI flag, exactly the shape this guard exists for.
_SECRET_COMMAND = "/usr/bin/mysql -psecretpw"


class _FakeProcess:
    """Stand-in for ``psutil.Process`` as returned by ``process_iter``."""

    def __init__(self, **info: Any) -> None:
        """
        Args:
            info: The fields ``process_iter`` would expose.
        """
        self.info = info


def _fake_secret_process() -> _FakeProcess:
    """
    Build a fake psutil process whose argv carries a password.

    Returns:
        The fake process, shaped like ``psutil.process_iter`` output.
    """
    return _FakeProcess(
        pid=4242,
        name="mysqld",
        username="mysql",
        cpu_percent=1.0,
        memory_percent=2.0,
        memory_info=types.SimpleNamespace(rss=1024 * 1024),
        status="running",
        cmdline=["/usr/bin/mysql", "-psecretpw"],
    )


@pytest.fixture
def admin_client_and_reader(sandbox: Path) -> tuple[TestClient, str]:
    """
    Sign in as admin and mint a ``read``-scoped API token from that session.

    Args:
        sandbox: Per-test temporary directory.

    Returns:
        The signed-in client (its cookie session carries admin scope) and the
        read token issued from it.
    """
    client = build_client(sandbox)
    master = get_token_manager().generate_master_token()
    csrf = login(client, master)["csrf_token"]
    issued = issue_token(client, csrf, master, name="reader", scope="read")
    return client, issued["token"]


def test_system_processes_hide_the_command_line_from_a_read_token(
    admin_client_and_reader: tuple[TestClient, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A read token sees the process name; only admin sees the argv."""
    client, read_token = admin_client_and_reader
    monkeypatch.setattr(
        psutil,
        "process_iter",
        lambda attrs=None, *args, **kwargs: iter([_fake_secret_process()]),
    )

    read_response = client.get("/api/system/processes", headers=bearer(read_token))
    assert read_response.status_code == 200, read_response.text
    read_body = read_response.json()
    assert read_body["processes"][0]["name"] == "mysqld"
    assert read_body["processes"][0]["command"] is None
    assert "secretpw" not in read_response.text

    admin_response = client.get("/api/system/processes")
    assert admin_response.status_code == 200, admin_response.text
    admin_body = admin_response.json()
    assert admin_body["processes"][0]["command"] == _SECRET_COMMAND


def test_monitor_processes_hide_the_command_line_from_a_read_token(
    admin_client_and_reader: tuple[TestClient, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The monitor's own process listing is gated the same way as system's."""
    client, read_token = admin_client_and_reader
    fake = ProcessInfo(
        pid=4242,
        name="mysqld",
        user="mysql",
        cpu_percent=1.0,
        memory_percent=2.0,
        command=_SECRET_COMMAND,
        status="running",
    )
    monkeypatch.setattr(monitor_api, "list_processes", lambda: [fake])

    read_response = client.get("/api/monitor/processes", headers=bearer(read_token))
    assert read_response.status_code == 200, read_response.text
    read_body = read_response.json()
    assert read_body["processes"][0]["name"] == "mysqld"
    assert read_body["processes"][0]["command"] == ""
    assert "secretpw" not in read_response.text

    admin_response = client.get("/api/monitor/processes")
    assert admin_response.status_code == 200, admin_response.text
    admin_body = admin_response.json()
    assert admin_body["processes"][0]["command"] == _SECRET_COMMAND


def test_monitor_observations_hide_the_command_line_from_a_read_token(
    admin_client_and_reader: tuple[TestClient, str],
) -> None:
    """
    Stored observations are gated by the *reader's* scope, not the scanner's.

    ``POST /api/monitor/scan`` already requires admin scope at the auth
    chokepoint (mutations default to admin unless explicitly downgraded), so
    a read token can never trigger a scan. It can, however, read back what an
    earlier admin-run scan wrote to the store through this GET endpoint, so
    the redaction has to be applied here regardless of who ran the scan.
    """
    from wasm.monitor.models import SIGNAL_RESOURCE_USAGE, ProcessObservation
    from wasm.monitor.observation_store import ObservationStore

    client, read_token = admin_client_and_reader
    fake = ProcessInfo(
        pid=4242,
        name="mysqld",
        user="mysql",
        cpu_percent=99.0,
        memory_percent=90.0,
        command=_SECRET_COMMAND,
        status="running",
    )
    observation = ProcessObservation(
        process=fake,
        signal=SIGNAL_RESOURCE_USAGE,
        severity="warning",
        detail="high CPU",
    )
    ObservationStore().save(observation)

    read_response = client.get("/api/monitor/observations", headers=bearer(read_token))
    assert read_response.status_code == 200, read_response.text
    read_body = read_response.json()
    assert read_body["observations"][0]["process_name"] == "mysqld"
    assert read_body["observations"][0]["command"] is None
    assert "secretpw" not in read_response.text

    admin_response = client.get("/api/monitor/observations")
    assert admin_response.status_code == 200, admin_response.text
    admin_body = admin_response.json()
    assert admin_body["observations"][0]["command"] == _SECRET_COMMAND


def test_observation_entry_helper_hides_command_below_admin_scope() -> None:
    """
    ``_observation_entry`` (the scan response) applies the same gate.

    Unreachable through the live scan endpoint today - it already requires
    admin - but the helper is shared vocabulary with ``_row_to_entry``
    (the observations endpoint), and consistency here is what keeps a future
    relaxation of the scan endpoint's own scope requirement from silently
    reopening this leak.
    """
    from wasm.monitor.models import SIGNAL_RESOURCE_USAGE, ProcessObservation

    fake = ProcessInfo(pid=1, name="mysqld", command=_SECRET_COMMAND)
    observation = ProcessObservation(
        process=fake, signal=SIGNAL_RESOURCE_USAGE, severity="warning", detail="high CPU"
    )

    read_entry = monitor_api._observation_entry(observation, {"scope": "read"})
    admin_entry = monitor_api._observation_entry(observation, {"scope": "admin"})

    assert read_entry.command is None
    assert admin_entry.command == _SECRET_COMMAND
