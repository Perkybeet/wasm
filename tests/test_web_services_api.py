"""
Tests for the services web API.

Two defect classes are pinned here:

- **Path traversal into the systemd unit directory.** The panel runs as root, so
  a service name that escapes ``/etc/systemd/system`` means arbitrary unit files
  written as root, which is full control of the machine. Every endpoint that
  accepts a name must refuse to leave the unit directory.
- **Blocking the event loop.** The handlers shell out to systemctl/journalctl
  synchronously; declared ``async def`` they would freeze the whole panel.
"""

from __future__ import annotations

import inspect
from pathlib import Path
from typing import ClassVar
from urllib.parse import quote

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from wasm.core.exceptions import SecurityError, ValidationError
from wasm.managers.service_manager import WASM_UNIT_MARKER, ServiceManager
from wasm.validators.names import (
    MAX_SERVICE_NAME_LENGTH,
    resolve_within,
    validate_app_name,
    validate_database_name,
    validate_database_user,
    validate_filename,
    validate_service_name,
)
from wasm.web.api import services as services_api
from wasm.web.api.auth import get_current_session

#: Names that must never reach the filesystem as a unit file path.
TRAVERSAL_NAMES = [
    "..",
    "../",
    "../../lib/systemd/system/sshd",
    "../../../lib/systemd/system/sshd",
    "/etc/systemd/system/evil",
    "/lib/systemd/system/sshd",
    "..%2f..%2fevil",
    "%2e%2e%2fevil",
    "%252e%252e%252fevil",
    "evil\x00.service",
    "evil name",
    "evil\nname",
    "evil\r\nExecStart=/bin/sh",
    "wasm-../../evil",
    "./evil",
    ".hidden/../evil",
]

#: A unit body an operator could legitimately paste into advanced mode. It
#: carries the marker, without which the manager refuses to write it.
RAW_UNIT = f"# {WASM_UNIT_MARKER}\n[Service]\nExecStart=/bin/true\n"


class FakeServiceManager:
    """Stand-in for ServiceManager that records calls instead of running systemctl."""

    instances: ClassVar[list[FakeServiceManager]] = []

    def __init__(self, verbose: bool = False):
        """
        Args:
            verbose: Ignored, kept for signature compatibility.
        """
        self.calls: list[tuple[str, ...]] = []
        FakeServiceManager.instances.append(self)

    def daemon_reload(self) -> bool:
        """Record a daemon-reload request."""
        self.calls.append(("daemon-reload",))
        return True

    def enable(self, name: str) -> bool:
        """Record an enable request."""
        self.calls.append(("enable", name))
        return True

    def disable(self, name: str) -> bool:
        """Record a disable request."""
        self.calls.append(("disable", name))
        return True

    def stop(self, name: str) -> bool:
        """Record a stop request."""
        self.calls.append(("stop", name))
        return True

    def logs(self, name: str, lines: int = 50) -> str:
        """Return canned log output."""
        self.calls.append(("logs", name))
        return f"logs for {name}"


@pytest.fixture
def unit_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """
    Point the API at a throwaway systemd unit directory.

    Args:
        tmp_path: Per-test temporary directory.
        monkeypatch: Patching helper, scoped to the test.

    Returns:
        The directory unit files are expected to be written into.
    """
    directory = tmp_path / "etc" / "systemd" / "system"
    directory.mkdir(parents=True)
    monkeypatch.setattr(services_api, "SYSTEMD_UNIT_DIR", directory, raising=False)
    return directory


@pytest.fixture
def fake_manager(monkeypatch: pytest.MonkeyPatch) -> type[FakeServiceManager]:
    """
    Replace ServiceManager inside the API module with a recording stub.

    Args:
        monkeypatch: Patching helper, scoped to the test.

    Returns:
        The stub class, whose ``instances`` list holds every manager built.
    """
    FakeServiceManager.instances = []
    monkeypatch.setattr(services_api, "ServiceManager", FakeServiceManager, raising=False)
    return FakeServiceManager


@pytest.fixture
def client(unit_dir: Path) -> TestClient:
    """
    Build a test client for the services router with authentication stubbed out.

    Args:
        unit_dir: Fixture that redirects unit writes into the sandbox.

    Returns:
        A client whose requests are already authenticated.
    """
    app = FastAPI()
    app.include_router(services_api.router, prefix="/api/services")
    app.dependency_overrides[get_current_session] = lambda: {"session_id": "test"}
    return TestClient(app, raise_server_exceptions=False)


def _path_segment(name: str) -> str:
    """
    Percent-encode a payload so it survives the client untouched.

    httpx removes dot segments and rejects control characters while building the
    request line, so an unencoded payload would never reach the application and
    the test would prove nothing about the server.

    Args:
        name: The raw payload.

    Returns:
        The payload encoded as a single path segment.
    """
    return quote(name, safe="")


def _units_outside(unit_dir: Path, root: Path) -> list[Path]:
    """
    List unit files that were created anywhere but the unit directory.

    Args:
        unit_dir: The only directory writes are allowed in.
        root: Sandbox root to scan.

    Returns:
        Offending paths, empty when containment held.
    """
    return [p for p in root.rglob("*.service") if unit_dir not in p.parents]


class TestCreateServiceTraversal:
    """The reported vulnerability: unit name taken from the JSON body."""

    def test_create_rejects_traversal_and_writes_nothing_outside(
        self, client: TestClient, unit_dir: Path, tmp_path: Path
    ) -> None:
        """A body name escaping the unit directory must be refused, not written."""
        victim_dir = tmp_path / "lib" / "systemd" / "system"
        victim_dir.mkdir(parents=True)
        victim = victim_dir / "sshd.service"
        original = "[Service]\nExecStart=/usr/sbin/sshd -D\n"
        victim.write_text(original)

        response = client.post(
            "/api/services",
            json={"name": "../../../lib/systemd/system/sshd", "raw_content": RAW_UNIT},
        )

        assert 400 <= response.status_code < 500, (
            f"expected a client error, got {response.status_code}: {response.text}"
        )
        assert victim.read_text() == original
        assert _units_outside(unit_dir, tmp_path) == [victim]

    @pytest.mark.parametrize("name", TRAVERSAL_NAMES)
    def test_create_rejects_every_traversal_payload(
        self, client: TestClient, unit_dir: Path, tmp_path: Path, name: str
    ) -> None:
        """No traversal payload may create a unit file outside the unit directory."""
        response = client.post("/api/services", json={"name": name, "raw_content": RAW_UNIT})

        assert 400 <= response.status_code < 500, (
            f"{name!r} was accepted with {response.status_code}: {response.text}"
        )
        assert _units_outside(unit_dir, tmp_path) == []
        assert list(unit_dir.iterdir()) == []


class TestConfigEndpointsTraversal:
    """Read, update and delete of unit files must stay inside the unit directory."""

    @pytest.mark.parametrize("name", TRAVERSAL_NAMES)
    def test_update_config_rejects_traversal(
        self, client: TestClient, unit_dir: Path, tmp_path: Path, name: str
    ) -> None:
        """PUT /{name}/config must not write through a traversing name."""
        response = client.put(
            f"/api/services/{_path_segment(name)}/config", json={"config": RAW_UNIT}
        )

        assert 400 <= response.status_code < 500, (
            f"{name!r} was accepted with {response.status_code}: {response.text}"
        )
        assert _units_outside(unit_dir, tmp_path) == []

    @pytest.mark.parametrize("name", TRAVERSAL_NAMES)
    def test_get_config_rejects_traversal(self, client: TestClient, name: str) -> None:
        """GET /{name}/config must not read files outside the unit directory."""
        response = client.get(f"/api/services/{_path_segment(name)}/config")

        assert 400 <= response.status_code < 500, (
            f"{name!r} was accepted with {response.status_code}: {response.text}"
        )

    @pytest.mark.parametrize("name", TRAVERSAL_NAMES)
    def test_delete_rejects_traversal(
        self, client: TestClient, unit_dir: Path, name: str, fake_manager
    ) -> None:
        """DELETE /{name} must not unlink files outside the unit directory."""
        response = client.delete(f"/api/services/{_path_segment(name)}")

        assert 400 <= response.status_code < 500, (
            f"{name!r} was accepted with {response.status_code}: {response.text}"
        )

    @pytest.mark.parametrize("name", TRAVERSAL_NAMES)
    def test_logs_rejects_traversal(self, client: TestClient, name: str, fake_manager) -> None:
        """GET /{name}/logs must not accept an unvalidated unit name."""
        response = client.get(f"/api/services/{_path_segment(name)}/logs")

        assert 400 <= response.status_code < 500, (
            f"{name!r} was accepted with {response.status_code}: {response.text}"
        )


class TestSymlinkEscape:
    """A symlink inside the unit directory must not become a write primitive."""

    def test_update_config_refuses_symlink_pointing_outside(
        self, client: TestClient, unit_dir: Path, tmp_path: Path
    ) -> None:
        """Writing through a unit file that is a symlink out of the tree is refused."""
        outside = tmp_path / "outside.service"
        original = "[Service]\nExecStart=/usr/sbin/sshd -D\n"
        outside.write_text(original)
        (unit_dir / "evil.service").symlink_to(outside)

        response = client.put("/api/services/evil/config", json={"config": RAW_UNIT})

        assert 400 <= response.status_code < 500, (
            f"expected a client error, got {response.status_code}: {response.text}"
        )
        assert outside.read_text() == original

    def test_delete_refuses_symlink_pointing_outside(
        self, client: TestClient, unit_dir: Path, tmp_path: Path, fake_manager
    ) -> None:
        """Deleting through an escaping symlink is refused and the target survives."""
        outside = tmp_path / "outside.service"
        outside.write_text("[Service]\n")
        (unit_dir / "evil.service").symlink_to(outside)

        response = client.delete("/api/services/evil")

        assert 400 <= response.status_code < 500, (
            f"expected a client error, got {response.status_code}: {response.text}"
        )
        assert outside.exists()


@pytest.fixture
def sandboxed_manager(unit_dir: Path, monkeypatch: pytest.MonkeyPatch, runner):
    """
    Point the real ServiceManager at the sandbox.

    The happy-path tests use the real manager rather than a stub, because the
    guarantee worth testing is that the endpoint delegates every write to it.
    A stub would pass whether or not the endpoint still wrote the file itself,
    which is exactly the hole this endpoint had.

    Args:
        unit_dir: The sandbox unit directory.
        monkeypatch: Patching helper, scoped to the test.
        runner: The FakeRunner fixture, so systemctl is never invoked.

    Returns:
        The FakeRunner, for asserting on the commands the manager issued.
    """
    monkeypatch.setattr(ServiceManager, "SYSTEMD_DIR", unit_dir)
    monkeypatch.setattr(ServiceManager, "UNIT_SEARCH_DIRS", (unit_dir,))
    monkeypatch.setattr(services_api, "ServiceManager", ServiceManager, raising=False)
    return runner


class TestHappyPath:
    """Legitimate requests keep working and land inside the unit directory."""

    def test_create_writes_unit_inside_unit_dir(
        self, client: TestClient, unit_dir: Path, sandboxed_manager
    ) -> None:
        """A well-formed name creates the unit file in the configured directory."""
        response = client.post(
            "/api/services",
            json={"name": "my-app", "command": "/usr/bin/node /var/www/apps/my-app/server.js"},
        )

        assert response.status_code == 200, response.text
        unit = unit_dir / "my-app.service"
        assert unit.exists()
        assert "ExecStart=/usr/bin/node /var/www/apps/my-app/server.js" in unit.read_text()

    def test_create_goes_through_the_manager(
        self, client: TestClient, unit_dir: Path, sandboxed_manager
    ) -> None:
        """
        The endpoint must not write the unit itself.

        Writing here is how raw_content could put arbitrary content at a unit
        path as root while the manager's ownership guard looked on.
        """
        client.post("/api/services", json={"name": "my-app", "command": "/bin/true"})

        assert sandboxed_manager.ran("systemctl", "daemon-reload")

    def test_update_config_writes_inside_unit_dir(
        self, client: TestClient, unit_dir: Path, sandboxed_manager
    ) -> None:
        """Updating an existing unit rewrites exactly that file."""
        unit = unit_dir / "my-app.service"
        unit.write_text(RAW_UNIT)

        response = client.put("/api/services/my-app/config", json={"config": RAW_UNIT})

        assert response.status_code == 200, response.text
        assert unit.read_text() == RAW_UNIT

    def test_update_config_refuses_a_body_that_drops_the_marker(
        self, client: TestClient, unit_dir: Path, sandboxed_manager
    ) -> None:
        """A rewrite cannot orphan the unit from WASM's own management."""
        unit = unit_dir / "my-app.service"
        unit.write_text(RAW_UNIT)

        response = client.put(
            "/api/services/my-app/config",
            json={"config": "[Service]\nExecStart=/bin/true\n"},
        )

        assert 400 <= response.status_code < 500, response.text
        assert unit.read_text() == RAW_UNIT

    def test_create_rejects_injected_directives_in_simple_mode(
        self, client: TestClient, unit_dir: Path, sandboxed_manager
    ) -> None:
        """Newlines in simple-mode fields must not smuggle extra unit directives."""
        response = client.post(
            "/api/services",
            json={
                "name": "my-app",
                "command": "/bin/true\nExecStartPost=/bin/sh -c 'id > /tmp/pwned'",
            },
        )

        assert 400 <= response.status_code < 500, response.text
        assert not (unit_dir / "my-app.service").exists()


class TestEventLoopSafety:
    """Handlers block on systemctl/journalctl, so they must not run on the loop."""

    def test_no_handler_is_a_coroutine_function(self) -> None:
        """Every route handler is sync, so FastAPI runs it in the threadpool."""
        offenders = [
            route.endpoint.__name__
            for route in services_api.router.routes
            if inspect.iscoroutinefunction(getattr(route, "endpoint", None))
        ]
        assert offenders == [], f"these handlers would freeze the event loop: {offenders}"


#: Payloads no identifier validator may ever accept, whatever the flavour.
UNSAFE_IDENTIFIERS = [
    "",
    ".",
    "..",
    "../evil",
    "evil/../..",
    "/absolute",
    "sub/dir",
    "back\\slash",
    "with space",
    "with\nnewline",
    "with\x00nul",
    "with\x7fdel",
    "-leading-hyphen",
    ".leading-dot",
    "quote'name",
    'double"name',
    "semi;colon",
    "dollar$name",
    "%2e%2e%2f",
]


class TestNameValidators:
    """The allowlist validators shared by the whole application."""

    @pytest.mark.parametrize("name", ["my-app", "wasm-example.com", "app_1", "getty@tty1", "a"])
    def test_service_name_accepts_valid_names(self, name: str) -> None:
        """Unit names made of the systemd alphabet pass through unchanged."""
        assert validate_service_name(name) == name

    @pytest.mark.parametrize("name", UNSAFE_IDENTIFIERS)
    def test_service_name_rejects_unsafe_names(self, name: str) -> None:
        """Anything outside the allowlist is refused."""
        with pytest.raises(ValidationError):
            validate_service_name(name)

    def test_service_name_rejects_overlong_names(self) -> None:
        """Length is capped so a name cannot blow past filesystem limits."""
        with pytest.raises(ValidationError):
            validate_service_name("a" * (MAX_SERVICE_NAME_LENGTH + 1))

    @pytest.mark.parametrize("name", ["my-app", "app.example.com", "app_1"])
    def test_app_name_accepts_valid_names(self, name: str) -> None:
        """Application names allow the same inert punctuation as unit names."""
        assert validate_app_name(name) == name

    @pytest.mark.parametrize("name", [*UNSAFE_IDENTIFIERS, "at@sign"])
    def test_app_name_rejects_unsafe_names(self, name: str) -> None:
        """Application names are single path components only."""
        with pytest.raises(ValidationError):
            validate_app_name(name)

    @pytest.mark.parametrize("name", ["wasm", "my_db", "_private", "db2"])
    def test_database_name_accepts_valid_names(self, name: str) -> None:
        """SQL identifiers may contain letters, digits and underscores."""
        assert validate_database_name(name) == name

    @pytest.mark.parametrize(
        "name", [*UNSAFE_IDENTIFIERS, "my-db", "my.db", "1db", "db--comment", "db; DROP TABLE x"]
    )
    def test_database_name_rejects_unsafe_names(self, name: str) -> None:
        """Nothing that could terminate or extend an identifier is accepted."""
        with pytest.raises(ValidationError):
            validate_database_name(name)

    @pytest.mark.parametrize("name", ["wasm_user", "_svc", "u1"])
    def test_database_user_accepts_valid_names(self, name: str) -> None:
        """Database users follow the same rule as database names."""
        assert validate_database_user(name) == name

    @pytest.mark.parametrize("name", [*UNSAFE_IDENTIFIERS, "user-name", "user'--"])
    def test_database_user_rejects_unsafe_names(self, name: str) -> None:
        """Unsafe user names are refused."""
        with pytest.raises(ValidationError):
            validate_database_user(name)

    @pytest.mark.parametrize("name", ["backup.tar.gz", "site_1.conf", "a-b_c.d"])
    def test_filename_accepts_valid_names(self, name: str) -> None:
        """Ordinary file names are accepted."""
        assert validate_filename(name) == name

    @pytest.mark.parametrize(
        "name", [*UNSAFE_IDENTIFIERS, "trailing.", "double..dot", "con", "NUL", "com1", "LPT9"]
    )
    def test_filename_rejects_unsafe_and_reserved_names(self, name: str) -> None:
        """Traversal primitives and reserved device names are refused."""
        with pytest.raises(ValidationError):
            validate_filename(name)


class TestResolveWithin:
    """Containment of a path inside a base directory."""

    def test_returns_path_inside_base(self, tmp_path: Path) -> None:
        """An ordinary relative path resolves inside the base."""
        resolved = resolve_within(tmp_path, "unit.service")

        assert resolved == (tmp_path / "unit.service").resolve()

    def test_allows_nested_relative_path(self, tmp_path: Path) -> None:
        """Sub-directories are fine as long as the result stays inside."""
        resolved = resolve_within(tmp_path, "sub/dir/file.conf")

        assert resolved.is_relative_to(tmp_path.resolve())

    @pytest.mark.parametrize(
        "candidate",
        [".", "..", "../escape", "../../etc/shadow", "/etc/shadow", "sub/../../escape"],
    )
    def test_rejects_escaping_candidates(self, tmp_path: Path, candidate: str) -> None:
        """Climbing out of the base, or ignoring it entirely, is a security error."""
        base = tmp_path / "base"
        base.mkdir()

        with pytest.raises(SecurityError):
            resolve_within(base, candidate)

    def test_rejects_symlink_pointing_outside(self, tmp_path: Path) -> None:
        """Resolution happens before comparison, so a symlink cannot smuggle a path out."""
        base = tmp_path / "base"
        base.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        (base / "link").symlink_to(outside)

        with pytest.raises(SecurityError):
            resolve_within(base, "link/secret.conf")

    def test_allows_symlink_pointing_inside(self, tmp_path: Path) -> None:
        """A symlink that stays within the base is not an escape."""
        base = tmp_path / "base"
        (base / "real").mkdir(parents=True)
        (base / "link").symlink_to(base / "real")

        assert resolve_within(base, "link/file.conf").is_relative_to(base.resolve())

    def test_rejects_nul_byte(self, tmp_path: Path) -> None:
        """A NUL byte truncates the path at the syscall boundary, so it is refused."""
        with pytest.raises(ValidationError):
            resolve_within(tmp_path, "unit\x00.service")

    def test_rejects_empty_candidate(self, tmp_path: Path) -> None:
        """An empty candidate would silently return the base directory itself."""
        with pytest.raises(ValidationError):
            resolve_within(tmp_path, "")
