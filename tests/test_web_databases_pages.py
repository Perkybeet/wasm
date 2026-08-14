# Copyright (c) 2024-2026 Yago Lopez Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
Tests for the databases screen and its htmx adapters.

The screen drives real engines through the JSON API's own functions, so these
tests stand a fake engine manager in front of that API and then talk to the
panel exactly as a browser would: form posts, form-encoded bodies, fragments
back. What is asserted is the contract that matters:

- **The page tells the truth about every engine**, installed or not, running
  or stopped, and the actions it offers match that state.
- **Destruction confirms by name, server-side.** A drop or a restore with the
  wrong name typed answers 200 with the refusal inline and touches nothing.
- **The console is read-only unless the operator opted in.** The checkbox is
  the explicit ``mode="write"``; without it a write statement is refused and
  never reaches the engine.
- **A new user's password is printed exactly once.** The engine stores a
  hash, so no later screen can repeat it - and none may try.
- **A connection string never carries a real password.** There is none on the
  server to print, and the mask says so.
"""

# The web fixtures are imported from test_web_views rather than replicated, so
# there stays one definition of "a signed-in panel client". Ruff reads a test
# parameter named after an imported fixture as a redefinition; here it is the
# mechanism.
# ruff: noqa: F811

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from tests.test_web_views import (  # noqa: F401  (pytest resolves fixtures by name)
    anonymous,
    app,
    body_of,
    client,
    config_file,
    store,
)
from wasm.managers.database.base import (
    BackupInfo,
    BaseDatabaseManager,
    DatabaseInfo,
    UserInfo,
)

#: The password the fake engine issues when the operator does not choose one.
#: It must appear in exactly one response, ever.
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
    very next render. A fresh class per test keeps tests isolated.

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

    Both lookups the API and the views use are patched in that module's own
    namespace: the per-engine resolver and the registry the listings iterate.

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


def drops_recorded(db: type[BaseDatabaseManager]) -> list[tuple[Any, ...]]:
    """
    Args:
        db: The fake engine class.

    Returns:
        Every drop_database call the engine received.
    """
    return [call for call in db.calls if call[0] == "drop_database"]


# ------------------------------------------------------------------ the page


def test_the_page_draws_each_engine_with_its_state(client, engines) -> None:
    """Installed-and-running and not-installed read differently, in words."""
    page = body_of(client, "/databases")

    assert "PostgreSQL" in page
    assert ">running</span>" in page
    assert "16.3" in page
    assert "MySQL" in page
    assert "not installed" in page

    # The actions match the state: a running engine offers Stop and Restart,
    # an absent one offers Install and nothing else.
    assert 'hx-post="/databases/engines/postgresql/stop"' in page
    assert 'hx-post="/databases/engines/postgresql/restart"' in page
    assert 'hx-post="/databases/engines/mysql/install"' in page
    assert 'hx-post="/databases/engines/mysql/stop"' not in page


def test_the_page_lists_what_the_running_engine_holds(client, db) -> None:
    """The database row carries the facts an operator scans for."""
    page = body_of(client, "/databases")

    assert "appdb" in page
    assert "12 MB" in page
    assert 'hx-post="/databases/postgresql/appdb/drop"' in page
    assert 'value="postgresql:appdb"' in page, "the console does not offer the database"


def test_an_adapter_demands_a_session(anonymous, engines) -> None:
    """A mutation route is not a hole in the fence."""
    response = anonymous.post("/databases/console", data={"query": "SELECT 1"})
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


# ------------------------------------------------------------ create and drop


def test_creating_a_database_goes_through_the_manager(client, db) -> None:
    """The inline form creates through the same code path the JSON API uses."""
    response = client.post("/databases/postgresql/create", data={"name": "newdb", "owner": ""})

    assert response.status_code == 200, response.text
    assert ("create_database", "newdb", None) in db.calls
    assert "newdb" in db.dbs
    assert "newdb" in response.text


def test_dropping_with_the_wrong_name_is_refused_and_drops_nothing(client, db) -> None:
    """The GitHub pattern, enforced server-side: the typed name must match."""
    response = client.post("/databases/postgresql/appdb/drop", data={"confirm": "appd"})

    assert response.status_code == 200, response.text
    assert "does not match" in response.text
    assert "Nothing was dropped" in response.text
    assert "appdb" in db.dbs, "a mismatched confirmation must not drop"
    assert not drops_recorded(db), "the manager must never hear about a refused drop"


def test_dropping_with_the_exact_name_drops(client, db) -> None:
    """Typing the exact name is the whole ceremony; then it really goes."""
    response = client.post("/databases/postgresql/appdb/drop", data={"confirm": "appdb"})

    assert response.status_code == 200, response.text
    assert ("drop_database", "appdb") in db.calls
    assert "appdb" not in db.dbs
    assert "dropped" in response.text


# ----------------------------------------------------------------- the console


def test_the_console_is_read_only_by_default(client, db) -> None:
    """No checkbox, no writes: the statement runs in read mode."""
    response = client.post(
        "/databases/console", data={"target": "postgresql:appdb", "query": "SELECT 1"}
    )

    assert response.status_code == 200, response.text
    assert ("query", "appdb", "SELECT 1", True) in db.calls
    assert "42" in response.text, "the engine's output never reached the screen"


def test_the_console_refuses_a_write_without_the_opt_in(client, db) -> None:
    """A write statement in read mode is refused before it reaches the engine."""
    response = client.post(
        "/databases/console", data={"target": "postgresql:appdb", "query": "DELETE FROM t"}
    )

    assert response.status_code == 200, response.text
    assert "not read-only" in response.text
    assert not [call for call in db.calls if call[0] == "query"], (
        "a refused statement must never reach the engine"
    )
    # The refusal keeps what was typed, so the operator corrects it in place.
    assert "DELETE FROM t" in response.text


def test_the_console_writes_only_with_the_checkbox(client, db) -> None:
    """Ticking "Allow writes" is the explicit mode='write' the API demands."""
    response = client.post(
        "/databases/console",
        data={"target": "postgresql:appdb", "query": "DELETE FROM t", "allow_writes": "yes"},
    )

    assert response.status_code == 200, response.text
    assert ("query", "appdb", "DELETE FROM t", False) in db.calls


# ---------------------------------------------------------------------- users


def test_a_new_user_password_is_shown_exactly_once(client, db) -> None:
    """The response to the creation carries it; no later screen may."""
    response = client.post(
        "/databases/postgresql/users/create", data={"username": "svc", "password": ""}
    )

    assert response.status_code == 200, response.text
    assert ("create_user", "svc", None) in db.calls, "an empty field asks for a generated one"
    assert ISSUED_PASSWORD in response.text

    page = body_of(client, "/databases")
    assert "svc" in page, "the new user is missing from the listing"
    assert ISSUED_PASSWORD not in page, "a password may only ever be printed once"


def test_a_chosen_password_is_also_shown_only_once(client, db) -> None:
    """The operator's own password gets the same one-time treatment."""
    chosen = "Chosen-secret-77!"
    response = client.post(
        "/databases/postgresql/users/create", data={"username": "svc", "password": chosen}
    )

    assert response.status_code == 200, response.text
    assert chosen in response.text
    assert chosen not in body_of(client, "/databases")


def test_deleting_a_user_goes_through_the_manager(client, db) -> None:
    """The row's delete calls the same function the JSON API exposes."""
    response = client.post("/databases/postgresql/users/app/delete")

    assert response.status_code == 200, response.text
    assert ("drop_user", "app") in db.calls
    assert "app" not in db.users


def test_grant_and_revoke_pass_the_selected_privilege(client, db) -> None:
    """The select's value travels to the manager's whitelist check as a list."""
    granted = client.post(
        "/databases/postgresql/users/app/grant",
        data={"database": "appdb", "privilege": "SELECT"},
    )
    assert granted.status_code == 200, granted.text
    assert ("grant", "app", "appdb", ("SELECT",)) in db.calls

    revoked = client.post(
        "/databases/postgresql/users/app/revoke",
        data={"database": "appdb", "privilege": ""},
    )
    assert revoked.status_code == 200, revoked.text
    assert ("revoke", "app", "appdb", None) in db.calls, (
        "an empty selection means the engine's default set"
    )


# -------------------------------------------------------------- engine actions


def test_engine_stop_and_start_call_the_right_api(client, db) -> None:
    """The buttons drive the service through the API and redraw the truth."""
    stopped = client.post("/databases/engines/postgresql/stop")
    assert stopped.status_code == 200, stopped.text
    assert ("stop",) in db.calls
    assert db.state["running"] is False
    assert ">stopped</span>" in stopped.text

    started = client.post("/databases/engines/postgresql/start")
    assert started.status_code == 200, started.text
    assert ("start",) in db.calls
    assert db.state["running"] is True
    assert ">running</span>" in started.text


def test_installing_queues_a_job_and_says_so(client, engines, monkeypatch) -> None:
    """Install is a job: 202 behind the scenes, "background" on the screen."""
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

    response = client.post("/databases/engines/mysql/install")

    assert response.status_code == 200, response.text
    assert "background" in response.text
    assert created, "no job was queued"
    assert created[0]["kwargs"] == {"engine": "mysql", "action": "install"}


def test_installing_an_installed_engine_is_refused_inline(client, db) -> None:
    """The API's 409 comes back as a problem block, not a frozen screen."""
    response = client.post("/databases/engines/postgresql/install")

    assert response.status_code == 200, response.text
    assert "already installed" in response.text


# ------------------------------------------------------------------- backups


def test_backing_up_a_database_reports_the_dump(client, db) -> None:
    """The row action dumps through the manager and names the file it wrote."""
    response = client.post("/databases/postgresql/appdb/backup")

    assert response.status_code == 200, response.text
    assert ("backup", "appdb") in db.calls
    assert "postgresql-appdb-20260101_120000.sql.gz" in response.text


def test_restoring_demands_the_database_name(client, db) -> None:
    """A restore replaces data, so the wrong name typed restores nothing."""
    db.BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    dump = "postgresql-appdb-20260101_120000.sql.gz"
    (db.BACKUP_DIR / dump).write_bytes(b"not really a dump")

    refused = client.post(
        "/databases/postgresql/restore",
        data={"backup_name": dump, "database": "appdb", "confirm": "nope"},
    )
    assert refused.status_code == 200, refused.text
    assert "Nothing was restored" in refused.text
    assert not [call for call in db.calls if call[0] == "restore"]

    accepted = client.post(
        "/databases/postgresql/restore",
        data={"backup_name": dump, "database": "appdb", "confirm": "appdb"},
    )
    assert accepted.status_code == 200, accepted.text
    assert ("restore", "appdb", dump, False) in db.calls
    assert "restored" in accepted.text


# ---------------------------------------------------------- connection string


def test_the_connection_string_is_masked(client, db) -> None:
    """The panel prints the shape of the string, never a credential."""
    response = client.get(
        "/databases/postgresql/appdb/connection-string", params={"username": "app"}
    )

    assert response.status_code == 200, response.text
    assert "postgresql://app:***@localhost:5432/appdb" in response.text
    assert ISSUED_PASSWORD not in response.text
    assert "masked" in response.text, "the screen must say why the password is not there"


def test_the_connection_string_needs_a_user_named(client, db) -> None:
    """An empty user is refused with the reason, not answered with a guess."""
    response = client.get("/databases/postgresql/appdb/connection-string", params={"username": ""})

    assert response.status_code == 200, response.text
    assert "Name the user" in response.text
    assert "postgresql://" not in response.text, "no string may be built for nobody"
