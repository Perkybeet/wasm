# Copyright (c) 2024-2026 Yago Lopez Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
Tests for the paths that were broken between modules.

The bugs covered here share one shape: a caller reaches across a module
boundary and asks for something the callee never offered. A method that does
not exist, a dictionary key spelled differently on each side, a name imported
inside another function. Every one of them survived in production because a
broad ``except Exception`` downgraded it to a cosmetic warning.

The first test is the important one: it walks the AST of the whole package and
checks that every cross-module call actually resolves to a method that exists.
It is the only test here that can catch the *next* bug of this class.
"""

from __future__ import annotations

import ast
import importlib
import json
from argparse import Namespace
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from wasm.core.runner import FakeRunner

SRC_ROOT = Path(__file__).resolve().parents[1] / "src" / "wasm"

# Classes whose call sites are checked statically. The key is the class name as
# written in the source; the value is the import path.
_TRACKED_CLASSES: dict[str, str] = {
    "WASMStore": "wasm.core.store",
    "ServiceManager": "wasm.managers.service_manager",
    "CertManager": "wasm.managers.cert_manager",
    "BackupManager": "wasm.managers.backup_manager",
    "RollbackManager": "wasm.managers.backup_manager",
    "NginxManager": "wasm.managers.nginx_manager",
    "ApacheManager": "wasm.managers.apache_manager",
    "SourceManager": "wasm.managers.source_manager",
}

# Factory functions that return an instance of a tracked class.
_FACTORIES: dict[str, str] = {"get_store": "WASMStore"}

# Call sites that are still broken and live in modules outside the scope of
# this fix. The assertion is a subset check, so fixing one of these elsewhere
# does not break this test; adding a new one does.
_KNOWN_GAPS: frozenset[tuple[str, str]] = frozenset(
    {
        # BaseDeployer._rollback() calls the method health.py used to call:
        # ServiceManager exposes get_status, never status.
        ("deployers/base.py", "service_manager.status"),
        # BaseDeployer._preflight_checks() probes the web server with
        # is_running(); Nginx/ApacheManager only expose get_status().
        ("deployers/base.py", "manager.is_running"),
    }
)


def _resolve_class(name: str) -> type:
    """
    Import a tracked class by its source-level name.

    Args:
        name: Class name as written in the source.

    Returns:
        The imported class object.
    """
    return getattr(importlib.import_module(_TRACKED_CLASSES[name]), name)


def _constructed_class(value: ast.expr) -> str | None:
    """
    Report which tracked class an expression constructs, if any.

    Args:
        value: The right-hand side of an assignment.

    Returns:
        The tracked class name, or None when the expression builds something
        this test does not track.
    """
    if not isinstance(value, ast.Call):
        return None
    func = value.func
    called = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
    if called in _TRACKED_CLASSES:
        return called
    return _FACTORIES.get(called or "")


def _binding_targets(node: ast.Assign) -> list[tuple[str, bool]]:
    """
    Extract the assignment targets this test can reason about.

    Args:
        node: An assignment statement.

    Returns:
        Pairs of (name, is_attribute_of_self).
    """
    targets = []
    for target in node.targets:
        if isinstance(target, ast.Name):
            targets.append((target.id, False))
        elif (
            isinstance(target, ast.Attribute)
            and isinstance(target.value, ast.Name)
            and target.value.id == "self"
        ):
            targets.append((target.attr, True))
    return targets


def _source_files() -> list[Path]:
    """
    List the package sources to analyse.

    Returns:
        Every Python file shipped in the package.
    """
    return sorted(p for p in SRC_ROOT.rglob("*.py") if "__pycache__" not in p.parts)


def _collect_self_bindings(trees: dict[Path, ast.Module]) -> dict[str, set[str]]:
    """
    Map ``self.<attribute>`` names to the tracked classes assigned to them.

    These are collected across the whole package because a subclass uses what
    its base class assigned in ``__init__``.

    Args:
        trees: Parsed modules keyed by path.

    Returns:
        Attribute name to the set of classes it is ever built from.
    """
    attributes: dict[str, set[str]] = {}
    for tree in trees.values():
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign):
                continue
            class_name = _constructed_class(node.value)
            if class_name is None:
                continue
            for name, is_attribute in _binding_targets(node):
                if is_attribute:
                    attributes.setdefault(name, set()).add(class_name)
    return attributes


def _shadowed_attributes(tree: ast.Module) -> set[str]:
    """
    Attribute names a module defines for itself.

    ``self.store`` is a WASMStore in the managers and an observation store
    behind a property in the monitor. When a module defines the name, the
    package-wide binding says nothing about it.

    Args:
        tree: The parsed module.

    Returns:
        Names the module defines as a method, a property, or an attribute
        holding something this test does not track.
    """
    shadowed: set[str] = set()
    tracked: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            shadowed.add(node.name)
        elif isinstance(node, ast.Assign):
            is_tracked = _constructed_class(node.value) is not None
            for name, is_attribute in _binding_targets(node):
                if not is_attribute:
                    continue
                (tracked if is_tracked else shadowed).add(name)
        elif (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Attribute)
            and isinstance(node.target.value, ast.Name)
            and node.target.value.id == "self"
        ):
            shadowed.add(node.target.attr)
    return shadowed - tracked


class _CallChecker(ast.NodeVisitor):
    """
    Resolve method calls on locally constructed managers.

    Local names are scoped per function: ``manager`` means a database engine in
    one method and a web server in the next, and conflating the two produces
    noise instead of findings. Assignments are recorded as they are visited, so
    a call only resolves against a binding that precedes it.
    """

    def __init__(
        self,
        path: str,
        self_bindings: dict[str, set[str]],
        shadowed: set[str],
    ) -> None:
        """
        Args:
            path: Package-relative path of the module being visited.
            self_bindings: Package-wide ``self.<attribute>`` bindings.
            shadowed: Attribute names this module defines for itself.
        """
        self.path = path
        self._self_bindings = self_bindings
        self._shadowed = shadowed
        self._scopes: list[dict[str, set[str]]] = [{}]
        self.missing: list[tuple[str, int, str]] = []

    def _lookup(self, name: str) -> set[str]:
        """
        Find the classes a local name may hold.

        Args:
            name: Variable name.

        Returns:
            Candidate class names, innermost scope first.
        """
        for scope in reversed(self._scopes):
            if name in scope:
                return scope[name]
        return set()

    def _visit_scope(self, node: ast.AST) -> None:
        """
        Visit a function body in its own name scope.

        Args:
            node: The function definition node.
        """
        self._scopes.append({})
        self.generic_visit(node)
        self._scopes.pop()

    visit_FunctionDef = _visit_scope
    visit_AsyncFunctionDef = _visit_scope

    def visit_Assign(self, node: ast.Assign) -> None:
        """
        Record an assignment that constructs a tracked class.

        Args:
            node: The assignment statement.
        """
        class_name = _constructed_class(node.value)
        if class_name is not None:
            for name, is_attribute in _binding_targets(node):
                if not is_attribute:
                    self._scopes[-1].setdefault(name, set()).add(class_name)
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        """
        Check that the called method exists on the receiver's class.

        Args:
            node: The call expression.
        """
        func = node.func
        if isinstance(func, ast.Attribute):
            base = func.value
            candidates: set[str] = set()
            if isinstance(base, ast.Name):
                candidates = self._lookup(base.id)
                receiver = base.id
            elif (
                isinstance(base, ast.Attribute)
                and isinstance(base.value, ast.Name)
                and base.value.id == "self"
            ):
                receiver = base.attr
                if base.attr not in self._shadowed:
                    candidates = self._self_bindings.get(base.attr, set())
            else:
                receiver = ""
            if candidates and not any(hasattr(_resolve_class(c), func.attr) for c in candidates):
                self.missing.append((self.path, node.lineno, f"{receiver}.{func.attr}"))
        self.generic_visit(node)


def _find_missing_methods() -> list[tuple[str, int, str]]:
    """
    Find calls to methods that the receiving class does not define.

    Returns:
        Tuples of (relative file, line number, ``receiver.method``).
    """
    trees = {path: ast.parse(path.read_text(), str(path)) for path in _source_files()}
    self_bindings = _collect_self_bindings(trees)

    missing: list[tuple[str, int, str]] = []
    for path, tree in trees.items():
        checker = _CallChecker(
            str(path.relative_to(SRC_ROOT)), self_bindings, _shadowed_attributes(tree)
        )
        checker.visit(tree)
        missing.extend(checker.missing)
    return missing


class FakeStore:
    """A stand-in for :class:`~wasm.core.store.WASMStore`."""

    def __init__(self) -> None:
        self.apps: dict[str, Any] = {}
        self.databases: list[Any] = []
        self.services: list[Any] = []

    def get_app(self, domain: str) -> Any | None:
        """
        Look up an application by domain.

        Args:
            domain: Domain name.

        Returns:
            The stored app or None.
        """
        return self.apps.get(domain)

    def list_apps(self, **_kwargs: Any) -> list[Any]:
        """
        List stored applications.

        Returns:
            Every stored app.
        """
        return list(self.apps.values())

    def list_databases(self, app_id: int | None = None, **_kwargs: Any) -> list[Any]:
        """
        List databases, optionally filtered by app.

        Args:
            app_id: Owning application id.

        Returns:
            The matching databases.
        """
        if app_id is None:
            return list(self.databases)
        return [d for d in self.databases if getattr(d, "app_id", None) == app_id]

    def list_services(self, **_kwargs: Any) -> list[Any]:
        """
        List registered services.

        Returns:
            Every registered service.
        """
        return list(self.services)

    def get_service(self, name: str) -> Any | None:
        """
        Look up a service by name.

        Args:
            name: Unit name without the ``.service`` suffix.

        Returns:
            The stored service or None.
        """
        return next((s for s in self.services if s.name == name), None)

    def update_service_status(self, *_args: Any, **_kwargs: Any) -> None:
        """Accept a status update and ignore it."""

    def delete_service(self, *_args: Any, **_kwargs: Any) -> bool:
        """
        Accept a service deletion.

        Returns:
            Always True.
        """
        return True

    def update_site_ssl(self, *_args: Any, **_kwargs: Any) -> None:
        """Accept an SSL update and ignore it."""

    def update_app(self, app: Any) -> Any:
        """
        Accept an app update.

        Args:
            app: The app to store.

        Returns:
            The same app.
        """
        return app


@pytest.fixture
def fake_store(monkeypatch: pytest.MonkeyPatch) -> FakeStore:
    """
    Replace the SQLite store with an in-memory stand-in everywhere.

    Args:
        monkeypatch: Patching helper, scoped to the test.

    Returns:
        The fake store shared by every manager built during the test.
    """
    store = FakeStore()
    modules = (
        "wasm.core.store",
        "wasm.managers.service_manager",
        "wasm.managers.cert_manager",
        "wasm.managers.backup_manager",
    )
    for module_name in modules:
        module = importlib.import_module(module_name)
        if hasattr(module, "get_store"):
            monkeypatch.setattr(module, "get_store", lambda: store)
        if hasattr(module, "WASMStore"):
            monkeypatch.setattr(module, "WASMStore", lambda *a, **k: store)
    return store


# ---------------------------------------------------------------------------
# The structural test
# ---------------------------------------------------------------------------


def test_cross_module_calls_resolve_to_existing_methods() -> None:
    """Every tracked cross-module call must name a method that exists."""
    missing = _find_missing_methods()
    unexpected = [m for m in missing if (m[0], m[2]) not in _KNOWN_GAPS]

    assert not unexpected, "Calls to methods that do not exist:\n" + "\n".join(
        f"  {path}:{line} -> {call}" for path, line, call in unexpected
    )


# ---------------------------------------------------------------------------
# wasm health
# ---------------------------------------------------------------------------


@pytest.fixture
def health_environment(monkeypatch: pytest.MonkeyPatch, fake_store: FakeStore) -> FakeStore:
    """
    Pin down everything ``wasm health`` touches except the service check.

    Args:
        monkeypatch: Patching helper, scoped to the test.
        fake_store: The in-memory store.

    Returns:
        The store, so a test can register applications.
    """
    from wasm.managers.apache_manager import ApacheManager
    from wasm.managers.nginx_manager import NginxManager

    monkeypatch.setattr(NginxManager, "is_installed", lambda self: True)
    monkeypatch.setattr(NginxManager, "get_status", lambda self: {"active": True})
    monkeypatch.setattr(ApacheManager, "is_installed", lambda self: False)
    return fake_store


def test_health_counts_a_running_application(
    runner: FakeRunner,
    health_environment: FakeStore,
    capfd: pytest.CaptureFixture[str],
) -> None:
    """A deployed app whose unit is active must be reported as running."""
    runner.script(["systemctl", "is-active"], stdout="active\n")
    health_environment.apps["example.com"] = SimpleNamespace(domain="example.com")

    from wasm.cli.commands.health import handle_health

    handle_health(Namespace(verbose=False))

    out = capfd.readouterr().out
    assert "1/1 running" in out
    assert "0/1" not in out


@pytest.mark.parametrize(
    ("days_left", "expected_exit_code"),
    [(20, 0), (3, 1)],
    ids=["warning", "issue"],
)
def test_health_warns_about_a_certificate_close_to_expiry(
    runner: FakeRunner,
    health_environment: FakeStore,
    capfd: pytest.CaptureFixture[str],
    days_left: int,
    expected_exit_code: int,
) -> None:
    """The expiry check must read the key CertManager actually writes."""
    expiry = (datetime.now() + timedelta(days=days_left)).strftime("%Y-%m-%d")
    runner.script(
        ["sudo", "certbot", "certificates"],
        stdout=(
            "Found the following certs:\n"
            "  Certificate Name: example.com\n"
            "    Domains: example.com www.example.com\n"
            f"    Expiry Date: {expiry} 12:00:00+00:00 (VALID: {days_left} days)\n"
            "    Certificate Path: /etc/letsencrypt/live/example.com/fullchain.pem\n"
            "    Private Key Path: /etc/letsencrypt/live/example.com/privkey.pem\n"
        ),
    )

    from wasm.cli.commands.health import handle_health

    exit_code = handle_health(Namespace(verbose=False))

    assert "1 total, 1 expiring soon" in capfd.readouterr().out
    assert exit_code == expected_exit_code


# ---------------------------------------------------------------------------
# wasm site delete
# ---------------------------------------------------------------------------


def test_site_delete_removes_the_certificate(
    runner: FakeRunner,
    fake_store: FakeStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``site delete`` must reach the certificate manager, not a NameError."""
    from wasm.cli.commands import site as site_command
    from wasm.managers.apache_manager import ApacheManager
    from wasm.managers.cert_manager import CertManager
    from wasm.managers.nginx_manager import NginxManager

    deleted: list[str] = []
    monkeypatch.setattr(NginxManager, "site_exists", lambda self, domain: True)
    monkeypatch.setattr(NginxManager, "delete_site", lambda self, domain: True)
    monkeypatch.setattr(NginxManager, "reload", lambda self: True)
    monkeypatch.setattr(ApacheManager, "site_exists", lambda self, domain: False)
    monkeypatch.setattr(CertManager, "is_installed", lambda self: True)
    monkeypatch.setattr(CertManager, "cert_exists", lambda self, domain: True)
    monkeypatch.setattr(CertManager, "delete", lambda self, domain: deleted.append(domain))

    exit_code = site_command._handle_delete(
        Namespace(domain="example.com", force=True, verbose=False)
    )

    assert exit_code == 0
    assert deleted == ["example.com"]


# ---------------------------------------------------------------------------
# wasm service list / delete
# ---------------------------------------------------------------------------


_UNIT_LISTING = (
    "UNIT LOAD ACTIVE SUB DESCRIPTION\n"
    "ssh.service loaded active running OpenBSD Secure Shell server\n"
    "cron.service loaded active running Regular background program\n"
    "wasm-example-com.service loaded active running WASM managed service\n"
)


def test_service_list_excludes_units_wasm_does_not_manage(
    runner: FakeRunner, fake_store: FakeStore
) -> None:
    """Listing services must never surface ssh, cron or any other system unit."""
    from wasm.managers.service_manager import ServiceManager

    runner.script(["systemctl", "list-units"], stdout=_UNIT_LISTING)
    manager = ServiceManager()

    names = [service["name"] for service in manager.list_services()]

    assert "ssh" not in names
    assert "cron" not in names
    assert "wasm-example-com" in names


def test_service_list_asks_systemd_only_for_managed_units(
    runner: FakeRunner, fake_store: FakeStore
) -> None:
    """The systemd query itself must be scoped, not a bare wildcard."""
    from wasm.managers.service_manager import ServiceManager

    runner.script(["systemctl", "list-units"], stdout=_UNIT_LISTING)
    ServiceManager().list_services()

    argv = next(call for call in runner.calls if call[:2] == ("systemctl", "list-units"))
    assert "*" not in argv
    assert "wasm-*" in argv


def test_delete_service_refuses_a_foreign_unit(
    runner: FakeRunner, fake_store: FakeStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A unit WASM did not write must never be deleted."""
    from wasm.core.exceptions import ServiceError
    from wasm.managers.service_manager import ServiceManager

    (tmp_path / "ssh.service").write_text("[Unit]\nDescription=OpenBSD Secure Shell server\n")
    monkeypatch.setattr(ServiceManager, "SYSTEMD_DIR", tmp_path)

    with pytest.raises(ServiceError) as raised:
        ServiceManager().delete_service("ssh")

    assert "does not manage" in str(raised.value)
    assert (tmp_path / "ssh.service").exists()
    assert not runner.ran("sudo", "systemctl", "stop", "ssh")


# ---------------------------------------------------------------------------
# certbot privileges
# ---------------------------------------------------------------------------


def test_certbot_plugin_probe_runs_with_privileges(
    runner: FakeRunner, fake_store: FakeStore
) -> None:
    """``certbot plugins`` returns nothing useful unless it runs as root."""
    from wasm.managers.cert_manager import CertManager

    runner.script(["sudo", "certbot", "plugins"], stdout="* nginx\nDescription: Nginx Web Server\n")

    assert CertManager()._check_certbot_plugin("nginx") is True
    assert runner.ran("sudo", "certbot", "plugins")


# ---------------------------------------------------------------------------
# backups
# ---------------------------------------------------------------------------


class FakeDatabaseManager:
    """A database engine manager that records what it was asked to do."""

    def __init__(self) -> None:
        self.backed_up: list[str] = []
        self.restored: list[tuple[str, Path]] = []
        self.installed = True

    def is_installed(self) -> bool:
        """
        Report the engine as installed.

        Returns:
            The configured availability.
        """
        return self.installed

    def backup(self, database: str, compress: bool = True, **_kwargs: Any) -> Any:
        """
        Pretend to dump a database.

        Args:
            database: Database name.
            compress: Ignored.

        Returns:
            An object shaped like BackupInfo.
        """
        self.backed_up.append(database)
        return SimpleNamespace(
            path=Path(f"/var/backups/wasm/{database}.sql.gz"),
            size=2048,
            created=datetime(2026, 1, 1, 12, 0, 0),
        )

    def restore(self, database: str, backup_path: Path, **_kwargs: Any) -> bool:
        """
        Pretend to restore a database.

        Args:
            database: Target database name.
            backup_path: Dump to load.

        Returns:
            Always True.
        """
        self.restored.append((database, Path(backup_path)))
        return True


@pytest.fixture
def database_engine(monkeypatch: pytest.MonkeyPatch) -> FakeDatabaseManager:
    """
    Install a fake engine manager in the database registry.

    Args:
        monkeypatch: Patching helper, scoped to the test.

    Returns:
        The fake engine manager.
    """
    from wasm.managers.database.registry import DatabaseRegistry

    engine = FakeDatabaseManager()
    monkeypatch.setattr(
        DatabaseRegistry, "get", classmethod(lambda cls, name, verbose=False: engine)
    )
    return engine


def _build_backup_manager(tmp_path: Path, apps_dir: Path) -> Any:
    """
    Build a BackupManager pointed at a temporary tree.

    Args:
        tmp_path: Root for the backup directory.
        apps_dir: Directory holding application sources.

    Returns:
        The configured manager.
    """
    from wasm.managers.backup_manager import BackupManager

    manager = BackupManager()
    manager.backup_dir = tmp_path / "backups"
    manager.backup_dir.mkdir(parents=True, exist_ok=True)
    manager.config = SimpleNamespace(
        apps_directory=apps_dir,
        service_user="www-data",
        get=lambda key, default=None: default,
    )
    return manager


def _script_backup_commands(runner: FakeRunner) -> None:
    """
    Script the shell commands a backup run cannot infer on its own.

    Args:
        runner: The fake runner to configure.
    """
    runner.script(["stat", "-c", "%s"], stdout="4096\n")
    runner.script(["sudo", "stat", "-c", "%s"], stdout="4096\n")
    runner.script(["sudo", "sha256sum"], stdout="d0d0caca  backup.tar.gz\n")


def test_backup_with_databases_reports_what_it_actually_stored(
    runner: FakeRunner,
    fake_store: FakeStore,
    database_engine: FakeDatabaseManager,
    tmp_path: Path,
) -> None:
    """``--include-databases`` must dump the databases it claims to include."""
    apps_dir = tmp_path / "apps"
    (apps_dir / "example-com").mkdir(parents=True)
    fake_store.apps["example.com"] = SimpleNamespace(id=1, domain="example.com")
    fake_store.databases = [SimpleNamespace(app_id=1, engine="postgres", name="example_db")]

    _script_backup_commands(runner)
    manager = _build_backup_manager(tmp_path, apps_dir)

    metadata = manager.create(domain="example.com", include_databases=True)

    assert database_engine.backed_up == ["example_db"]
    assert [entry["name"] for entry in metadata.database_backups] == ["example_db"]
    assert metadata.includes_databases is True


def test_backup_without_databases_does_not_claim_it_stored_any(
    runner: FakeRunner,
    fake_store: FakeStore,
    database_engine: FakeDatabaseManager,
    tmp_path: Path,
) -> None:
    """Metadata must not say ``includes_databases`` when nothing was dumped."""
    apps_dir = tmp_path / "apps"
    (apps_dir / "example-com").mkdir(parents=True)

    _script_backup_commands(runner)
    manager = _build_backup_manager(tmp_path, apps_dir)

    metadata = manager.create(domain="example.com", include_databases=True)

    assert metadata.database_backups == []
    assert metadata.includes_databases is False


def test_restore_puts_the_databases_back(
    runner: FakeRunner,
    fake_store: FakeStore,
    database_engine: FakeDatabaseManager,
    tmp_path: Path,
) -> None:
    """A backup that carries database dumps must restore them too."""
    apps_dir = tmp_path / "apps"
    apps_dir.mkdir()
    backup_dir = tmp_path / "backups" / "example-com"
    backup_dir.mkdir(parents=True)

    metadata = {
        "id": "example-com_20260101_120000",
        "domain": "example.com",
        "app_name": "example-com",
        "created_at": "2026-01-01T12:00:00",
        "size_bytes": 4096,
        "app_type": "nextjs",
        "version": "1.0.0",
        "description": "",
        "includes_env": True,
        "includes_node_modules": False,
        "includes_databases": True,
        "database_backups": [
            {
                "engine": "postgres",
                "name": "example_db",
                "backup_path": "/var/backups/wasm/example_db.sql.gz",
                "size_bytes": 2048,
                "created": "2026-01-01T12:00:00",
            }
        ],
    }
    metadata_file = backup_dir / f"{metadata['id']}.json"
    metadata_file.write_text(json.dumps(metadata))

    runner.script(["sudo", "cat", str(metadata_file)], stdout=json.dumps(metadata))
    runner.script(["sudo", "ls"], stdout="example-com\n")

    manager = _build_backup_manager(tmp_path, apps_dir)
    manager.backup_dir = tmp_path / "backups"

    manager.restore(metadata["id"], stop_service=False, verify_checksum=False)

    assert database_engine.restored == [("example_db", Path("/var/backups/wasm/example_db.sql.gz"))]
