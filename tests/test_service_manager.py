"""
Ownership and safety tests for :class:`wasm.managers.service_manager.ServiceManager`.

ServiceManager is the only door to systemd, and it runs as root. Every test here
exists because a real bypass was found: ``wasm service delete ssh`` stopped and
disabled sshd, ``stop``/``start``/``enable``/``disable``/``logs`` had no
ownership check at all, and ``create_service`` accepted a name with a path
separator, which turned ``/etc/systemd/system/<name>.service`` into an arbitrary
root-owned file write.

The rule the whole file asserts: a unit is WASM's only when systemd loads it
from the directory WASM manages, it carries a WASM signal (prefix, store record
or marker) and it does not shadow a unit shipped by the distribution.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from wasm.core.exceptions import SecurityError, ServiceError, ValidationError
from wasm.core.runner import FakeRunner
from wasm.core.store import Service
from wasm.managers.service_manager import WASM_UNIT_MARKER, ServiceManager

#: A unit file body that carries no WASM marker, like every distribution unit.
FOREIGN_UNIT = (
    "[Unit]\nDescription=OpenBSD Secure Shell server\n\n[Service]\nExecStart=/usr/sbin/sshd\n"
)

#: Verbs that change systemd state. No test with a foreign unit may produce one.
MUTATING_VERBS = frozenset(
    {"start", "stop", "restart", "reload", "enable", "disable", "mask", "kill"}
)


class FakeStore:
    """In-memory stand-in for the SQLite store."""

    def __init__(self) -> None:
        self.services: dict[str, Service] = {}
        self.deleted: list[str] = []

    def list_services(self, **_kwargs: object) -> list[Service]:
        """Return every recorded service."""
        return list(self.services.values())

    def get_service(self, name: str) -> Service | None:
        """Return a service by name, or None."""
        return self.services.get(name)

    def create_service(self, service: Service) -> Service:
        """Record a new service."""
        self.services[service.name] = service
        return service

    def update_service(self, service: Service) -> Service:
        """Replace a recorded service."""
        self.services[service.name] = service
        return service

    def update_service_status(self, name: str, status: str, **_kwargs: object) -> bool:
        """Record a status change."""
        service = self.services.get(name)
        if service is None:
            return False
        service.status = status
        return True

    def delete_service(self, name: str) -> bool:
        """Forget a service."""
        self.deleted.append(name)
        return self.services.pop(name, None) is not None


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> FakeStore:
    """
    Replace the SQLite store used by the service manager.

    Args:
        monkeypatch: Patching helper, scoped to the test.

    Returns:
        The in-memory store the manager will talk to.
    """
    fake = FakeStore()
    monkeypatch.setattr("wasm.managers.service_manager.get_store", lambda: fake)
    return fake


@pytest.fixture
def unit_dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """
    Point every unit directory the manager knows about at a temporary tree.

    Args:
        tmp_path: Per-test temporary directory.
        monkeypatch: Patching helper, scoped to the test.

    Returns:
        Mapping with the managed directory under ``managed`` and the two
        directories the distribution owns under ``distro`` and ``runtime``.
    """
    managed = tmp_path / "etc/systemd/system"
    runtime = tmp_path / "run/systemd/system"
    distro = tmp_path / "usr/lib/systemd/system"
    for directory in (managed, runtime, distro):
        directory.mkdir(parents=True)

    monkeypatch.setattr(ServiceManager, "SYSTEMD_DIR", managed)
    monkeypatch.setattr(ServiceManager, "UNIT_SEARCH_DIRS", (managed, runtime, distro))
    return {"managed": managed, "runtime": runtime, "distro": distro}


@pytest.fixture
def manager(runner: FakeRunner, store: FakeStore, unit_dirs: dict[str, Path]) -> ServiceManager:
    """
    Build a manager wired to the fake runner, store and unit directories.

    Args:
        runner: The fake command runner installed process-wide.
        store: The in-memory store.
        unit_dirs: The temporary unit directories.

    Returns:
        The manager under test.
    """
    return ServiceManager()


@pytest.fixture
def foreign_ssh(unit_dirs: dict[str, Path], runner: FakeRunner) -> Path:
    """
    Install an ``ssh.service`` that systemd loads from the distribution tree.

    Args:
        unit_dirs: The temporary unit directories.
        runner: The fake runner, scripted with the FragmentPath systemd reports.

    Returns:
        Path of the distribution unit file.
    """
    path = unit_dirs["distro"] / "ssh.service"
    path.write_text(FOREIGN_UNIT)
    runner.script(
        ["systemctl", "show", "-p", "FragmentPath", "ssh.service"],
        stdout=f"FragmentPath={path}\n",
    )
    return path


def owned_unit(unit_dirs: dict[str, Path], name: str = "wasm-example") -> Path:
    """
    Write a unit file that WASM owns.

    Args:
        unit_dirs: The temporary unit directories.
        name: Unit name without the ``.service`` suffix.

    Returns:
        Path of the created unit file.
    """
    path = unit_dirs["managed"] / f"{name}.service"
    path.write_text(f"# {WASM_UNIT_MARKER}\n[Unit]\nDescription=x\n")
    return path


def mutating_calls(runner: FakeRunner) -> list[tuple[str, ...]]:
    """
    Return every recorded call that would change systemd state.

    Args:
        runner: The fake runner.

    Returns:
        The offending argument vectors.
    """
    offending = []
    for call in runner.calls:
        verbs = set(call) & MUTATING_VERBS
        if call and call[0] in {"systemctl", "sudo"} and verbs:
            offending.append(call)
    return offending


# ---------------------------------------------------------------------------
# The bypass that started this: delete/stop/disable on a distribution unit
# ---------------------------------------------------------------------------


def test_delete_refuses_a_unit_loaded_from_the_distribution_tree(
    manager: ServiceManager, runner: FakeRunner, foreign_ssh: Path
) -> None:
    """The guard used to be skipped when the unit file was not under /etc."""
    with pytest.raises(ServiceError) as raised:
        manager.delete_service("ssh")

    assert "does not manage" in str(raised.value)
    assert foreign_ssh.exists()
    assert mutating_calls(runner) == []


def test_delete_does_not_stop_or_disable_a_foreign_unit(
    manager: ServiceManager, runner: FakeRunner, foreign_ssh: Path
) -> None:
    """Refusing after stop+disable would still have taken sshd down."""
    with pytest.raises(ServiceError):
        manager.delete_service("ssh")

    assert not runner.ran("systemctl", "stop", "ssh.service")
    assert not runner.ran("systemctl", "disable", "ssh.service")


@pytest.mark.parametrize(
    "operation",
    [
        "delete_service",
        "start",
        "stop",
        "restart",
        "enable",
        "disable",
        "logs",
        "get_service_config",
        "get_status",
    ],
)
def test_no_operation_touches_a_foreign_unit(
    manager: ServiceManager, runner: FakeRunner, foreign_ssh: Path, operation: str
) -> None:
    """Every entry point, not just delete, must refuse a unit WASM does not own."""
    with pytest.raises(ServiceError):
        getattr(manager, operation)("ssh")

    assert mutating_calls(runner) == []
    assert foreign_ssh.read_text() == FOREIGN_UNIT


def test_update_config_refuses_a_foreign_unit(
    manager: ServiceManager, runner: FakeRunner, foreign_ssh: Path
) -> None:
    """Rewriting a distribution unit is arbitrary root code execution on boot."""
    with pytest.raises(ServiceError):
        manager.update_config("ssh", "[Service]\nExecStart=/bin/false\n")

    assert foreign_ssh.read_text() == FOREIGN_UNIT
    assert mutating_calls(runner) == []


def test_follow_logs_refuses_a_foreign_unit(
    manager: ServiceManager, runner: FakeRunner, foreign_ssh: Path
) -> None:
    """Following journalctl is still an operation on someone else's unit."""
    with pytest.raises(ServiceError):
        manager.follow_logs("ssh", on_line=lambda line: None)

    assert not runner.calls_to("journalctl")


def test_wasm_prefix_does_not_win_over_the_fragment_path(
    manager: ServiceManager, runner: FakeRunner, unit_dirs: dict[str, Path]
) -> None:
    """A distribution unit called wasm-something is still not ours."""
    path = unit_dirs["distro"] / "wasm-thing.service"
    path.write_text(FOREIGN_UNIT)
    runner.script(
        ["systemctl", "show", "-p", "FragmentPath", "wasm-thing.service"],
        stdout=f"FragmentPath={path}\n",
    )

    assert manager.is_managed("wasm-thing") is False
    with pytest.raises(ServiceError):
        manager.restart("wasm-thing")


def test_a_unit_shadowing_a_distribution_unit_is_not_managed(
    manager: ServiceManager, runner: FakeRunner, unit_dirs: dict[str, Path]
) -> None:
    """A file in our directory that eclipses a distro unit is not ours to touch."""
    (unit_dirs["distro"] / "nginx.service").write_text(FOREIGN_UNIT)
    (unit_dirs["managed"] / "nginx.service").write_text(f"# {WASM_UNIT_MARKER}\n")

    assert manager.is_managed("nginx") is False


def test_ownership_survives_systemd_being_unreachable(
    manager: ServiceManager, runner: FakeRunner, unit_dirs: dict[str, Path]
) -> None:
    """Without a FragmentPath answer, the directory search has to decide alone."""
    (unit_dirs["distro"] / "cron.service").write_text(FOREIGN_UNIT)
    runner.script(["systemctl", "show"], exit_code=1, stderr="Failed to connect to bus")

    with pytest.raises(ServiceError):
        manager.stop("cron")

    assert mutating_calls(runner) == []


def test_delete_of_an_unknown_unit_is_a_no_op(manager: ServiceManager, runner: FakeRunner) -> None:
    """Rolling back a failed deploy deletes units that were never created."""
    manager.delete_service("wasm-never-created")

    assert mutating_calls(runner) == []


def test_resolving_a_prefixed_name_keeps_the_prefix(manager: ServiceManager) -> None:
    """Stripping 'wasm-' from an inspected name threw away an ownership signal."""
    assert manager._resolve_service_name("wasm-example-com") == "wasm-example-com"
    assert manager._resolve_service_name("example-com.service") == "example-com"


def test_resolving_prefers_the_legacy_unit_that_exists(
    manager: ServiceManager, unit_dirs: dict[str, Path]
) -> None:
    """Servers upgraded from the prefixed scheme keep working."""
    owned_unit(unit_dirs, name="wasm-legacy")

    assert manager._resolve_service_name("legacy") == "wasm-legacy"


# ---------------------------------------------------------------------------
# create_service: name and environment validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "../../../etc/cron.d/evil",
        "sub/dir",
        "/etc/systemd/system/evil",
        "wasm-\x00evil",
        "wasm-evil\nUser=root",
        "..",
        "",
        "a" * 200,
    ],
)
def test_create_rejects_unsafe_names(
    manager: ServiceManager, runner: FakeRunner, unit_dirs: dict[str, Path], name: str
) -> None:
    """A unit name becomes a path and a systemctl argument; it must be inert."""
    with pytest.raises((ValidationError, SecurityError)):
        manager.create_service(name=name, command="/usr/bin/true", working_directory="/srv")

    written = [p for p in unit_dirs["managed"].rglob("*") if p.is_file()]
    assert written == []
    assert mutating_calls(runner) == []


def test_create_refuses_to_shadow_a_distribution_unit(
    manager: ServiceManager, runner: FakeRunner, unit_dirs: dict[str, Path]
) -> None:
    """Writing /etc/systemd/system/ssh.service silently replaces sshd."""
    (unit_dirs["distro"] / "ssh.service").write_text(FOREIGN_UNIT)

    with pytest.raises(ServiceError) as raised:
        manager.create_service(name="ssh", command="/usr/bin/true", working_directory="/srv")

    assert "ssh" in str(raised.value)
    assert not (unit_dirs["managed"] / "ssh.service").exists()


def test_create_rejects_an_injecting_environment_value(
    manager: ServiceManager, unit_dirs: dict[str, Path]
) -> None:
    """A newline in a value used to append User=root to the unit."""
    with pytest.raises(ValidationError):
        manager.create_service(
            name="example",
            command="/usr/bin/true",
            working_directory="/srv",
            environment={"X": 'y"\nUser=root\nExecStartPre=/bin/sh -c "id"'},
        )

    assert not (unit_dirs["managed"] / "example.service").exists()


def test_create_rejects_an_invalid_environment_name(
    manager: ServiceManager, unit_dirs: dict[str, Path]
) -> None:
    """A name carrying '=' smuggles a second directive value."""
    with pytest.raises(ValidationError):
        manager.create_service(
            name="example",
            command="/usr/bin/true",
            working_directory="/srv",
            environment={"BAD NAME=x": "y"},
        )

    assert not (unit_dirs["managed"] / "example.service").exists()


def test_create_writes_the_unit_and_registers_it(
    manager: ServiceManager, runner: FakeRunner, store: FakeStore, unit_dirs: dict[str, Path]
) -> None:
    """The happy path: a 0644 unit file, a daemon-reload and a store record."""
    manager.create_service(
        name="example",
        command="/usr/bin/node server.js",
        working_directory="/var/www/apps/example",
        environment={"PORT": "3000"},
    )

    unit = unit_dirs["managed"] / "example.service"
    content = unit.read_text()
    assert WASM_UNIT_MARKER in content
    assert "PORT=3000" in content
    assert oct(unit.stat().st_mode & 0o777) == "0o644"
    assert runner.ran("systemctl", "daemon-reload")
    assert "example" in store.services


def test_create_refuses_an_existing_managed_unit(
    manager: ServiceManager, unit_dirs: dict[str, Path]
) -> None:
    """Recreating a unit would silently discard the running configuration."""
    owned_unit(unit_dirs)

    with pytest.raises(ServiceError):
        manager.create_service(
            name="wasm-example", command="/usr/bin/true", working_directory="/srv"
        )


# ---------------------------------------------------------------------------
# Exact argv for every operation on a unit we own
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("operation", "expected"),
    [
        ("start", ("systemctl", "start", "wasm-example.service")),
        ("stop", ("systemctl", "stop", "wasm-example.service")),
        ("restart", ("systemctl", "restart", "wasm-example.service")),
        ("enable", ("systemctl", "enable", "wasm-example.service")),
        ("disable", ("systemctl", "disable", "wasm-example.service")),
    ],
)
def test_argv_of_lifecycle_operations(
    manager: ServiceManager,
    runner: FakeRunner,
    unit_dirs: dict[str, Path],
    operation: str,
    expected: tuple[str, ...],
) -> None:
    """systemctl is invoked with the full unit name and without sudo."""
    owned_unit(unit_dirs)

    getattr(manager, operation)("wasm-example")

    assert expected in runner.calls
    assert not any(call[0] == "sudo" for call in runner.calls)


def test_argv_of_logs(
    manager: ServiceManager, runner: FakeRunner, unit_dirs: dict[str, Path]
) -> None:
    """journalctl is scoped to the unit and paging is disabled."""
    owned_unit(unit_dirs)
    runner.script(["journalctl"], stdout="line one\n")

    assert manager.logs("wasm-example", lines=10) == "line one\n"
    assert (
        "journalctl",
        "-u",
        "wasm-example.service",
        "-n",
        "10",
        "--no-pager",
    ) in runner.calls


def test_follow_logs_streams_instead_of_shelling_out(
    manager: ServiceManager, runner: FakeRunner, unit_dirs: dict[str, Path]
) -> None:
    """--follow used to call subprocess.run directly, bypassing the runner."""
    owned_unit(unit_dirs)
    runner.script(["journalctl"], stdout="first\nsecond\n")
    seen: list[str] = []

    manager.follow_logs("wasm-example", on_line=seen.append, lines=5)

    assert seen == ["first", "second"]
    assert ("journalctl", "-u", "wasm-example.service", "-n", "5", "-f") in runner.calls


def test_status_of_an_owned_unit(
    manager: ServiceManager, runner: FakeRunner, unit_dirs: dict[str, Path]
) -> None:
    """Status is read with is-active, is-enabled and show."""
    owned_unit(unit_dirs)
    runner.script(["systemctl", "is-active"], stdout="active\n")
    runner.script(["systemctl", "is-enabled"], stdout="enabled\n")
    runner.script(["systemctl", "show", "wasm-example.service"], stdout="MainPID=42\n")

    status = manager.get_status("wasm-example")

    assert status["active"] is True
    assert status["enabled"] is True
    assert status["pid"] == "42"
    assert status["exists"] is True


def test_status_of_an_unknown_unit_reports_absence(
    manager: ServiceManager, runner: FakeRunner
) -> None:
    """A unit that does not exist anywhere is reported, not refused."""
    status = manager.get_status("wasm-missing")

    assert status["exists"] is False
    assert status["active"] is False
    assert mutating_calls(runner) == []


def test_stop_of_an_unknown_unit_is_a_no_op(manager: ServiceManager, runner: FakeRunner) -> None:
    """Teardown paths call stop before knowing whether the unit was created."""
    assert manager.stop("wasm-missing") is True
    assert mutating_calls(runner) == []


def test_start_of_an_unknown_unit_raises(manager: ServiceManager, runner: FakeRunner) -> None:
    """Starting something that does not exist is a real error, not a no-op."""
    with pytest.raises(ServiceError):
        manager.start("wasm-missing")

    assert mutating_calls(runner) == []


def test_delete_removes_an_owned_unit(
    manager: ServiceManager, runner: FakeRunner, store: FakeStore, unit_dirs: dict[str, Path]
) -> None:
    """The happy path still works, without shelling out to rm."""
    unit = owned_unit(unit_dirs)
    store.create_service(Service(name="wasm-example", unit_file=str(unit)))

    manager.delete_service("wasm-example")

    assert not unit.exists()
    assert runner.ran("systemctl", "stop", "wasm-example.service")
    assert runner.ran("systemctl", "disable", "wasm-example.service")
    assert runner.ran("systemctl", "daemon-reload")
    assert not runner.calls_to("rm")
    assert store.deleted == ["wasm-example"]


# ---------------------------------------------------------------------------
# Atomic writes
# ---------------------------------------------------------------------------


def test_create_leaves_no_temporary_file_behind(
    manager: ServiceManager, unit_dirs: dict[str, Path]
) -> None:
    """A leftover temp file in the unit directory is loaded by systemd."""
    manager.create_service(name="example", command="/usr/bin/true", working_directory="/srv")

    names = sorted(p.name for p in unit_dirs["managed"].iterdir())
    assert names == ["example.service"]


def test_a_failed_write_leaves_the_previous_unit_intact(
    manager: ServiceManager, unit_dirs: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A crash halfway through must never produce a half-written unit."""
    unit = owned_unit(unit_dirs)
    original = unit.read_text()

    def explode(src: object, dst: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", explode)

    with pytest.raises(ServiceError):
        manager.update_config("wasm-example", "[Service]\nExecStart=/bin/false\n")

    assert unit.read_text() == original
    assert sorted(p.name for p in unit_dirs["managed"].iterdir()) == ["wasm-example.service"]


def test_update_config_can_be_reverted(manager: ServiceManager, unit_dirs: dict[str, Path]) -> None:
    """update_config returns the previous body so a caller can roll back."""
    unit = owned_unit(unit_dirs)
    original = unit.read_text()

    previous = manager.update_config(
        "wasm-example", f"# {WASM_UNIT_MARKER}\n[Service]\nExecStart=/bin/true\n"
    )

    assert previous == original
    manager.update_config("wasm-example", previous)
    assert unit.read_text() == original


def test_update_config_refuses_a_body_without_the_marker(
    manager: ServiceManager, unit_dirs: dict[str, Path]
) -> None:
    """Dropping the marker would make WASM lose ownership of its own unit."""
    unit = owned_unit(unit_dirs)
    original = unit.read_text()

    with pytest.raises(ServiceError):
        manager.update_config("wasm-example", "[Service]\nExecStart=/bin/true\n")

    assert unit.read_text() == original


# ---------------------------------------------------------------------------
# Listing
# ---------------------------------------------------------------------------


def test_list_services_excludes_foreign_units(
    manager: ServiceManager, runner: FakeRunner, unit_dirs: dict[str, Path], foreign_ssh: Path
) -> None:
    """systemd may still answer with units outside the requested patterns."""
    owned_unit(unit_dirs)
    runner.script(
        ["systemctl", "list-units"],
        stdout=(
            "UNIT LOAD ACTIVE SUB DESCRIPTION\n"
            "ssh.service loaded active running OpenBSD Secure Shell server\n"
            "wasm-example.service loaded active running WASM managed service\n"
        ),
    )

    names = [service["name"] for service in manager.list_services()]

    assert names == ["wasm-example"]


def test_list_services_query_is_scoped(manager: ServiceManager, runner: FakeRunner) -> None:
    """Asking for '*' is what surfaced ssh and cron as deletable in the panel."""
    manager.list_services()

    argv = next(call for call in runner.calls if call[:2] == ("systemctl", "list-units"))
    assert "*" not in argv
    assert "wasm-*" in argv
