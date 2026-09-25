# Copyright (c) 2024-2026 Yago Lopez Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
Tests for ``/api/databases/...``.

Ported from the deleted server-rendered databases screen (tests/test_web_databases_pages.py),
which drove these same endpoints through form posts and htmx fragments; this
module drives them directly as the JSON API a browser or any other client
actually talks to now. What is defended:

- **The listing tells the truth about every engine**, installed or not,
  running or stopped.
- **A drop or a delete requires elevation** (``DELETE`` endpoints are gated by
  ``require_elevated``, unlike everything else here that only needs a
  session) - the page's "confirm by typed name" ceremony was a browser-only
  safety net the API never had; the API's equivalent safety net is D5
  elevation, already covered by tests/test_web_sudo.py, and re-affirmed here
  per-endpoint.
- **A new user's password is printed exactly once**, in the creation response,
  never in a listing.
- **The console is read-only unless the caller opts in**, and a write from a
  cookie session needs a recent sudo confirmation.

The connection-string page test is not ported: unlike the page, which always
masked the password, ``POST /api/databases/connection-string`` builds the
string from whatever password the caller supplies (see the endpoint's own
docstring) - there is no masking behaviour left at this layer to pin.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from wasm.managers.database.base import BackupInfo, BaseDatabaseManager, DatabaseInfo, UserInfo
from wasm.web.auth import CSRF_HEADER_NAME, SecurityConfig
from wasm.web.server import create_app, get_token_manager

#: The password the fake engine issues when the operator does not choose one.
ISSUED_PASSWORD = "Once-0nly-Generated-9!"

#: What the fake engine answers to any statement.
QUERY_OUTPUT = " count \n-------\n    42\n(1 row)"


def make_engine(
    backup_dir: Path,
    *,
    engine: str = "postgresql",
    display: str = "PostgreSQL",
    installed: bool = True,
    running: bool = True,
    seeded: bool = True,
) -> type[BaseDatabaseManager]:
    """
    Build a fresh fake engine manager class.

    State lives on the class because the API instantiates a manager per call:
    two requests must see the same databases, or a create would vanish on the
    very next read. A fresh class per test keeps tests isolated.

    Args:
        backup_dir: Where the engine pretends to keep its dumps.
        engine: Engine name the registry would answer to.
        display: Human-readable engine name.
        installed: Whether the engine starts installed.
        running: Whether the engine starts running.
        seeded: Whether to start with one database and one user.

    Returns:
        The fake manager class.
    """

    class Fake(BaseDatabaseManager):
        ENGINE_NAME = engine
        DISPLAY_NAME = display
        DEFAULT_PORT = 5432
        SERVICE_NAME = engine
        CLIENT_BINARY = "fake-client"
        VALID_PRIVILEGES = frozenset({"ALL PRIVILEGES", "DELETE", "INSERT", "SELECT", "UPDATE"})
        DEFAULT_PRIVILEGES = ("ALL PRIVILEGES",)
        BACKUP_DIR = backup_dir

        state = {"installed": installed, "running": running}
        dbs: dict[str, dict[str, Any]] = (
            {"appdb": {"owner": "app", "size": "12 MB"}} if seeded else {}
        )
        users: dict[str, dict[str, Any]] = (
            {"app": {"databases": ["appdb"], "privileges": ["ALL PRIVILEGES"]}} if seeded else {}
        )
        calls: list[tuple[Any, ...]] = []

        def is_installed(self) -> bool:
            return type(self).state["installed"]

        def is_running(self) -> bool:
            return type(self).state["running"]

        def get_version(self) -> str | None:
            return "16.3" if self.is_installed() else None

        def start(self) -> None:
            type(self).calls.append(("start",))
            type(self).state["running"] = True

        def stop(self) -> None:
            type(self).calls.append(("stop",))
            type(self).state["running"] = False

        def restart(self) -> None:
            type(self).calls.append(("restart",))
            type(self).state["running"] = True

        def create_database(self, name, owner=None, encoding=None, **kwargs):
            cls = type(self)
            cls.calls.append(("create_database", name, owner))
            cls.dbs[name] = {"owner": owner, "size": None}
            return DatabaseInfo(name=name, engine=cls.ENGINE_NAME, owner=owner)

        def drop_database(self, name, force=False):
            cls = type(self)
            cls.calls.append(("drop_database", name))
            cls.dbs.pop(name, None)

        def database_exists(self, name):
            return name in type(self).dbs

        def list_databases(self):
            cls = type(self)
            return [
                DatabaseInfo(
                    name=name,
                    engine=cls.ENGINE_NAME,
                    size=entry.get("size"),
                    owner=entry.get("owner"),
                )
                for name, entry in cls.dbs.items()
            ]

        def get_database_info(self, name):
            entry = type(self).dbs[name]
            return DatabaseInfo(
                name=name,
                engine=type(self).ENGINE_NAME,
                size=entry.get("size"),
                owner=entry.get("owner"),
            )

        def create_user(self, username, password=None, host="localhost", **kwargs):
            cls = type(self)
            cls.calls.append(("create_user", username, password))
            cls.users[username] = {"databases": [], "privileges": []}
            user = UserInfo(username=username, engine=cls.ENGINE_NAME, host=host)
            return user, password or ISSUED_PASSWORD

        def drop_user(self, username, host="localhost"):
            cls = type(self)
            cls.calls.append(("drop_user", username))
            cls.users.pop(username, None)

        def user_exists(self, username, host="localhost"):
            return username in type(self).users

        def list_users(self):
            cls = type(self)
            return [
                UserInfo(
                    username=name,
                    engine=cls.ENGINE_NAME,
                    databases=list(entry["databases"]),
                    privileges=list(entry["privileges"]),
                )
                for name, entry in cls.users.items()
            ]

        def grant_privileges(self, username, database, privileges=None, host="localhost"):
            type(self).calls.append(
                ("grant", username, database, tuple(privileges) if privileges else None)
            )

        def revoke_privileges(self, username, database, privileges=None, host="localhost"):
            type(self).calls.append(
                ("revoke", username, database, tuple(privileges) if privileges else None)
            )

        def backup(self, database, output_path=None, compress=True, **kwargs):
            cls = type(self)
            cls.calls.append(("backup", database))
            return BackupInfo(
                path=cls.BACKUP_DIR / f"{cls.ENGINE_NAME}-{database}-20260101_120000.sql.gz",
                database=database,
                engine=cls.ENGINE_NAME,
                size=2048,
                created=datetime(2026, 1, 1, 12, 0),
                compressed=True,
            )

        def restore(self, database, backup_path, drop_existing=False, **kwargs):
            type(self).calls.append(("restore", database, Path(backup_path).name, drop_existing))

        def execute_query(self, database, query, **kwargs):
            type(self).calls.append(("query", database, query, kwargs.get("read_only")))
            return True, QUERY_OUTPUT

        def get_connection_string(self, database, username, password, host="localhost"):
            port = type(self).DEFAULT_PORT
            return f"postgresql://{username}:{password}@{host}:{port}/{database}"

    return Fake


def wire(monkeypatch: pytest.MonkeyPatch, engine_classes: list[type]) -> None:
    """
    Stand the fake engines in front of the databases API module.

    Args:
        monkeypatch: Patching helper, scoped to the test.
        engine_classes: The fake manager classes to expose.
    """
    import wasm.web.api.databases as db_api

    by_name = {cls.ENGINE_NAME: cls for cls in engine_classes}

    def fake_get(engine: str, verbose: bool = False):
        cls = by_name.get(engine.lower())
        return cls(verbose=verbose) if cls else None

    class FakeRegistry:
        @staticmethod
        def list_engines() -> list[str]:
            return list(by_name)

        @staticmethod
        def get_installed(verbose: bool = False) -> list[Any]:
            return [cls() for cls in by_name.values() if cls.state["installed"]]

    monkeypatch.setattr(db_api, "get_db_manager", fake_get)
    monkeypatch.setattr(db_api, "DatabaseRegistry", FakeRegistry)


@pytest.fixture
def engines(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> dict[str, type[BaseDatabaseManager]]:
    """
    A running PostgreSQL fake and an uninstalled MySQL fake, wired in.

    Args:
        monkeypatch: Patching helper, scoped to the test.
        tmp_path: Per-test temporary directory, for the dump directories.

    Returns:
        The fake classes by engine name.
    """
    postgres = make_engine(tmp_path / "pg-dumps")
    mysql = make_engine(
        tmp_path / "my-dumps",
        engine="mysql",
        display="MySQL",
        installed=False,
        running=False,
        seeded=False,
    )
    wire(monkeypatch, [postgres, mysql])
    return {"postgresql": postgres, "mysql": mysql}


@pytest.fixture
def db(engines: dict[str, type[BaseDatabaseManager]]) -> type[BaseDatabaseManager]:
    """
    The running engine, which is the one most tests poke.

    Args:
        engines: The wired fake engines.

    Returns:
        The PostgreSQL fake class.
    """
    return engines["postgresql"]


@pytest.fixture
def app(tmp_path: Path, runner: object) -> FastAPI:
    """
    Args:
        tmp_path: Per-test temporary directory.
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


@pytest.fixture
def anonymous(app: FastAPI) -> TestClient:
    """
    Args:
        app: The application.

    Returns:
        A client with no session.
    """
    return TestClient(app, client=("testclient", 50000), follow_redirects=False)


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


# ------------------------------------------------------------------ engines


def test_the_listing_tells_the_truth_about_every_engine(client: TestClient, engines) -> None:
    """Installed-and-running and not-installed read differently."""
    body = client.get("/api/databases/engines").json()
    by_name = {entry["name"]: entry for entry in body["engines"]}

    assert by_name["postgresql"]["installed"] is True
    assert by_name["postgresql"]["running"] is True
    assert by_name["postgresql"]["version"] == "16.3"
    assert by_name["mysql"]["installed"] is False
    assert by_name["mysql"]["running"] is False


def test_engine_endpoints_demand_a_session(anonymous: TestClient, engines) -> None:
    """A mutation route is not a hole in the fence."""
    assert anonymous.get("/api/databases/engines").status_code in (401, 403)
    assert anonymous.post("/api/databases/engines/postgresql/stop").status_code in (401, 403)


def test_engine_stop_and_start_call_the_right_api(client: TestClient, db) -> None:
    """The actions drive the service through the manager and report the outcome."""
    stopped = client.post("/api/databases/engines/postgresql/stop")
    assert stopped.status_code == 200, stopped.text
    assert ("stop",) in db.calls
    assert db.state["running"] is False

    started = client.post("/api/databases/engines/postgresql/start")
    assert started.status_code == 200, started.text
    assert ("start",) in db.calls
    assert db.state["running"] is True


def test_installing_queues_a_job(
    client: TestClient, engines, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Install runs the distribution package manager, so it is a queued job."""
    import wasm.web.api.databases as db_api

    created: list[dict[str, Any]] = []

    def create_job(**kwargs: Any) -> SimpleNamespace:
        created.append(kwargs)
        return SimpleNamespace(
            id="job-42",
            status=SimpleNamespace(value="pending"),
            to_dict=lambda: {"id": "job-42", "status": "pending"},
        )

    monkeypatch.setattr(db_api, "get_job_manager", lambda: SimpleNamespace(create_job=create_job))

    response = client.post("/api/databases/engines/mysql/install")

    assert response.status_code == 202, response.text
    assert created, "no job was queued"
    assert created[0]["kwargs"] == {"engine": "mysql", "action": "install"}


def test_installing_an_installed_engine_is_refused(client: TestClient, db) -> None:
    """The API answers 409, in the manager's own words."""
    response = client.post("/api/databases/engines/postgresql/install")

    assert response.status_code == 409, response.text
    assert "already installed" in response.text


# --------------------------------------------------------------- databases


def test_creating_a_database_goes_through_the_manager(client: TestClient, db) -> None:
    """The create endpoint drives the same manager the console does."""
    response = client.post(
        "/api/databases/databases", json={"name": "newdb", "engine": "postgresql"}
    )

    assert response.status_code == 200, response.text
    assert ("create_database", "newdb", None) in db.calls
    assert "newdb" in db.dbs
    assert response.json()["name"] == "newdb"


def test_dropping_a_database_requires_elevation(client: TestClient, db) -> None:
    """Unlike every other database endpoint, DELETE needs a recent sudo confirmation."""
    response = client.delete("/api/databases/databases/postgresql/appdb")

    assert response.status_code == 403, response.text
    assert not [call for call in db.calls if call[0] == "drop_database"]


def test_dropping_a_database_drops_it_once_elevated(client: TestClient, db) -> None:
    """The DELETE actually removes it, once the caller has confirmed sudo mode."""
    elevate(client)

    response = client.delete("/api/databases/databases/postgresql/appdb")

    assert response.status_code == 200, response.text
    assert ("drop_database", "appdb") in db.calls
    assert "appdb" not in db.dbs


# ------------------------------------------------------------------ console


def test_the_console_is_read_only_by_default(client: TestClient, db) -> None:
    """No ``mode``, no writes: the statement runs in read mode."""
    response = client.post(
        "/api/databases/query",
        json={"database": "appdb", "engine": "postgresql", "query": "SELECT 1"},
    )

    assert response.status_code == 200, response.text
    assert ("query", "appdb", "SELECT 1", True) in db.calls
    assert "42" in response.json()["output"]


def test_a_write_statement_in_read_mode_is_refused(client: TestClient, db) -> None:
    """A write statement in read mode is refused before it reaches the engine."""
    response = client.post(
        "/api/databases/query",
        json={"database": "appdb", "engine": "postgresql", "query": "DELETE FROM t"},
    )

    assert response.status_code >= 400, response.text
    assert not [call for call in db.calls if call[0] == "query"], (
        "a refused statement must never reach the engine"
    )


def test_a_write_statement_needs_elevation(client: TestClient, db) -> None:
    """A cookie session that has not confirmed sudo mode cannot write, even opted in."""
    response = client.post(
        "/api/databases/query",
        json={
            "database": "appdb",
            "engine": "postgresql",
            "query": "DELETE FROM t",
            "mode": "write",
        },
    )

    assert response.status_code == 403, response.text
    assert response.json()["error"] == "elevation_required"
    assert not [call for call in db.calls if call[0] == "query"]


def test_the_console_writes_once_elevated(client: TestClient, db) -> None:
    """The explicit ``mode="write"``, from an elevated session, reaches the engine."""
    elevate(client)

    response = client.post(
        "/api/databases/query",
        json={
            "database": "appdb",
            "engine": "postgresql",
            "query": "DELETE FROM t",
            "mode": "write",
        },
    )

    assert response.status_code == 200, response.text
    assert ("query", "appdb", "DELETE FROM t", False) in db.calls


def test_an_engine_with_no_structured_client_answers_with_empty_columns(
    client: TestClient, db
) -> None:
    """
    An engine that never overrode execute_query_structured() falls back
    cleanly: the legacy output is unchanged and the new fields are just empty.
    """
    response = client.post(
        "/api/databases/query",
        json={"database": "appdb", "engine": "postgresql", "query": "SELECT 1"},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert "42" in body["output"]
    assert body["columns"] == []
    assert body["rows"] == []
    assert body["row_count"] == 0


def test_a_structured_engine_returns_columns_and_rows(client: TestClient, db) -> None:
    """An engine that parses its own client output exposes it structured."""

    def execute_query_structured(self, database, query, *, read_only=False, max_rows=1000):
        from wasm.managers.database.base import StructuredQueryResult

        type(self).calls.append(("query_structured", database, query, read_only))
        return StructuredQueryResult(
            output="id,name\n1,Alice\n",
            columns=["id", "name"],
            rows=[["1", "Alice"]],
            row_count=1,
            duration_ms=4.2,
            truncated=False,
        )

    db.SUPPORTS_STRUCTURED_QUERY = True
    db.execute_query_structured = execute_query_structured

    response = client.post(
        "/api/databases/query",
        json={"database": "appdb", "engine": "postgresql", "query": "SELECT * FROM users"},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["columns"] == ["id", "name"]
    assert body["rows"] == [["1", "Alice"]]
    assert body["row_count"] == 1
    assert body["duration_ms"] == 4.2
    assert ("query_structured", "appdb", "SELECT * FROM users", True) in db.calls
    # Never falls back to the plain execute_query() as well: one execution.
    assert not [call for call in db.calls if call[0] == "query"]


# ------------------------------------------------------------- privileges


def test_engine_privileges_lists_the_managers_own_whitelist(client: TestClient, db) -> None:
    """The console's grant dialog reads its options from the manager, not a copy."""
    response = client.get("/api/databases/engines/postgresql/privileges")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["engine"] == "postgresql"
    assert set(body["privileges"]) == {"ALL PRIVILEGES", "DELETE", "INSERT", "SELECT", "UPDATE"}


def test_engine_privileges_for_an_unknown_engine_is_refused(client: TestClient) -> None:
    """
    The allowlist is the registry, same as every other engine endpoint.

    Not pinned to a specific status: every ``/engines/{engine}/...`` route in
    this module refuses an unknown name through the same ``get_manager()``,
    and this endpoint is a client of it like the rest, not a special case.
    """
    response = client.get("/api/databases/engines/nosuchengine/privileges")

    assert response.status_code >= 400, response.text
    assert "nosuchengine" in response.text


# ---------------------------------------------------------------------- users


def test_a_new_user_password_is_shown_exactly_once(client: TestClient, db) -> None:
    """The response to the creation carries it; the listing never does."""
    response = client.post("/api/databases/users", json={"username": "svc", "engine": "postgresql"})

    assert response.status_code == 200, response.text
    assert ("create_user", "svc", None) in db.calls, "an empty field asks for a generated one"
    assert response.json()["password"] == ISSUED_PASSWORD

    listing = client.get("/api/databases/users/postgresql").json()
    assert "svc" in [user["username"] for user in listing["users"]]
    assert ISSUED_PASSWORD not in client.get("/api/databases/users/postgresql").text


def test_deleting_a_user_requires_elevation(client: TestClient, db) -> None:
    """The delete-user route is gated the same way the drop-database route is."""
    response = client.delete("/api/databases/users/postgresql/app")

    assert response.status_code == 403, response.text
    assert not [call for call in db.calls if call[0] == "drop_user"]

    elevate(client)
    elevated = client.delete("/api/databases/users/postgresql/app")

    assert elevated.status_code == 200, elevated.text
    assert ("drop_user", "app") in db.calls


def test_grant_and_revoke_pass_the_selected_privilege(client: TestClient, db) -> None:
    """The privilege list travels to the manager's own whitelist check."""
    granted = client.post(
        "/api/databases/users/grant",
        json={
            "username": "app",
            "database": "appdb",
            "engine": "postgresql",
            "privileges": ["SELECT"],
        },
    )
    assert granted.status_code == 200, granted.text
    assert ("grant", "app", "appdb", ("SELECT",)) in db.calls

    revoked = client.post(
        "/api/databases/users/revoke",
        json={"username": "app", "database": "appdb", "engine": "postgresql"},
    )
    assert revoked.status_code == 200, revoked.text
    assert ("revoke", "app", "appdb", None) in db.calls, (
        "an omitted list means the engine's default set"
    )


# ------------------------------------------------------------------- backups


def test_backing_up_a_database_reports_the_dump(client: TestClient, db) -> None:
    """The dump goes through the manager and names the file it wrote."""
    response = client.post(
        "/api/databases/backups", json={"database": "appdb", "engine": "postgresql"}
    )

    assert response.status_code == 200, response.text
    assert ("backup", "appdb") in db.calls
    assert response.json()["path"].endswith("postgresql-appdb-20260101_120000.sql.gz")


def test_restoring_needs_a_backup_that_actually_exists(client: TestClient, db) -> None:
    """A backup name that resolves nowhere on disk is a 404, not a restore."""
    response = client.post(
        "/api/databases/backups/restore",
        json={
            "database": "appdb",
            "engine": "postgresql",
            "backup_name": "does-not-exist.sql.gz",
        },
    )

    assert response.status_code == 404, response.text
    assert not [call for call in db.calls if call[0] == "restore"]


def test_restoring_an_existing_backup_reaches_the_manager(client: TestClient, db) -> None:
    """The named dump, once it is really on disk, is restored through the manager."""
    db.BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    dump = "postgresql-appdb-20260101_120000.sql.gz"
    (db.BACKUP_DIR / dump).write_bytes(b"not really a dump")

    response = client.post(
        "/api/databases/backups/restore",
        json={"database": "appdb", "engine": "postgresql", "backup_name": dump},
    )

    assert response.status_code == 200, response.text
    assert ("restore", "appdb", dump, False) in db.calls
