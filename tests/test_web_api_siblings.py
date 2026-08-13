"""
Tests for the web API modules that surround ``services.py``.

``services.py`` was fixed on its own; its siblings carried the same defects.
Five classes of defect are pinned here, one test class each:

- **The event loop.** Roughly a hundred handlers were ``async def`` while doing
  synchronous work - certbot, systemctl, ``apt``, tar. One of them running is
  the whole panel frozen: no other request, no WebSocket frame, no heartbeat.
- **Path traversal.** The panel runs as root and builds paths from names that
  arrive in a URL or a JSON body.
- **Unreachable routes.** ``POST /api/certs/renew-all`` was declared after
  ``/{domain}`` and could never be matched.
- **A second naming scheme.** The sites API wrote ``sites-available/example_com``
  while every manager, the CLI and the store use ``sites-available/example.com``,
  so a site created from the panel was invisible to the rest of the product.
- **Acting on arbitrary processes.** ``POST /system/processes/{pid}/kill`` sent
  signals as root to any pid on the host.
"""

from __future__ import annotations

import ast
import inspect
import tempfile
from importlib import import_module
from pathlib import Path
from typing import Any
from urllib.parse import quote

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from wasm.core.exceptions import DomainError
from wasm.web import jobs as jobs_module
from wasm.web.api import apps as apps_api
from wasm.web.api import backups as backups_api
from wasm.web.api import certs as certs_api
from wasm.web.api import databases as databases_api
from wasm.web.api import jobs as jobs_api
from wasm.web.api import sites as sites_api
from wasm.web.api import system as system_api
from wasm.web.api.auth import get_current_session
from wasm.web.api.deps import strict_domain

#: The aggregate router module, imported by path because the package re-exports
#: the router object itself under the same name.
router_module = import_module("wasm.web.api.router")

#: Every module this agent owns, plus the job system they all queue work on.
API_MODULES = [
    apps_api,
    backups_api,
    certs_api,
    databases_api,
    jobs_api,
    sites_api,
    system_api,
]

#: Source files that must not execute processes on their own.
OWNED_SOURCES = [Path(module.__file__) for module in [*API_MODULES, jobs_module, router_module]]

#: Payloads that must never be accepted as a domain or a name.
TRAVERSAL_NAMES = [
    "..",
    "../../etc/nginx/sites-enabled/default",
    "/etc/nginx/sites-available/evil",
    "..%2f..%2fevil",
    "%2e%2e%2fevil",
    "evil\x00.conf",
    "evil name",
    "evil\nname",
    "evil\r\nserver_name x",
    "./evil",
    ".hidden/../evil",
    "evil/../..",
    "sub/dir",
]


def _path_segment(name: str) -> str:
    """
    Percent-encode a payload so it survives the client untouched.

    httpx removes dot segments and refuses control characters while building
    the request line, so an unencoded payload would never reach the
    application and the test would prove nothing about the server.

    Args:
        name: The raw payload.

    Returns:
        The payload encoded as a single path segment.
    """
    return quote(name, safe="")


class RecordingManager:
    """Base for stand-ins that record calls instead of touching the system."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """
        Args:
            *args: Ignored, kept for signature compatibility.
            **kwargs: Ignored, kept for signature compatibility.
        """
        self.calls: list[tuple[str, ...]] = []


class FakeStore:
    """Store stand-in that knows about nothing."""

    def list_sites(self) -> list[Any]:
        """Return no sites, so the API falls back to the manager."""
        return []

    def get_site(self, domain: str) -> None:
        """Report that the site is not in the store."""
        return None

    def create_site(self, site: Any) -> None:
        """Accept a site registration and forget it."""

    def update_site(self, site: Any) -> None:
        """Accept a site update and forget it."""

    def delete_site(self, domain: str) -> bool:
        """Accept a site deletion and forget it."""
        return True

    def list_apps(self) -> list[Any]:
        """Report no applications."""
        return []

    def get_app(self, domain: str) -> None:
        """Report that the application is unknown."""
        return None


@pytest.fixture
def sandbox_nginx(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """
    Build nginx managers that write into a throwaway configuration tree.

    Args:
        tmp_path: Per-test temporary directory.
        monkeypatch: Patching helper, scoped to the test.

    Returns:
        A factory producing managers bound to the sandbox backend.
    """
    import dataclasses

    from wasm.managers.nginx_manager import NginxManager
    from wasm.managers.webserver import NGINX_BACKEND

    available = tmp_path / "etc" / "nginx" / "sites-available"
    enabled = tmp_path / "etc" / "nginx" / "sites-enabled"
    available.mkdir(parents=True)
    enabled.mkdir(parents=True)

    backend = dataclasses.replace(NGINX_BACKEND, sites_available=available, sites_enabled=enabled)

    def make(verbose: bool = False, **kwargs: Any) -> NginxManager:
        """
        Build a manager pinned to the sandbox tree.

        Args:
            verbose: Ignored, kept for signature compatibility.
            **kwargs: Ignored, kept for signature compatibility.

        Returns:
            The manager.
        """
        manager = NginxManager(verbose=verbose, backend=backend)
        # The store is a cached_property, so seeding it keeps SQLite out of the
        # test without patching module globals.
        manager.__dict__["store"] = FakeStore()
        return manager

    monkeypatch.setattr(sites_api, "MANAGERS", {"nginx": make})
    monkeypatch.setattr(sites_api, "detect_webserver", lambda: "nginx")
    monkeypatch.setattr(sites_api, "get_store", FakeStore)
    return make


@pytest.fixture
def site_dirs(sandbox_nginx: Any) -> tuple[Path, Path]:
    """
    Expose the sandbox configuration directories.

    Args:
        sandbox_nginx: Factory producing managers bound to the sandbox.

    Returns:
        Tuple of the sites-available and sites-enabled directories.
    """
    manager = sandbox_nginx()
    return manager.sites_available, manager.backend.sites_enabled


def _client(*routers: Any) -> TestClient:
    """
    Build a test client for one or more routers, with authentication stubbed.

    Args:
        *routers: ``(router, prefix)`` pairs to mount.

    Returns:
        A client whose requests are already authenticated.
    """
    app = FastAPI()
    for router, prefix in routers:
        app.include_router(router, prefix=prefix)
    app.dependency_overrides[get_current_session] = lambda: {"session_id": "test"}
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def sites_client(site_dirs: tuple[Path, Path], runner: Any) -> TestClient:
    """
    Build a client for the sites router.

    Args:
        site_dirs: Fixture redirecting nginx writes into the sandbox.
        runner: The fake command runner, installed process-wide.

    Returns:
        The client.
    """
    return _client((sites_api.router, "/api/sites"))


@pytest.fixture
def certs_client(runner: Any) -> TestClient:
    """
    Build a client for the certificates router.

    Args:
        runner: The fake command runner, installed process-wide.

    Returns:
        The client.
    """
    return _client((certs_api.router, "/api/certs"))


class TestEventLoopSafety:
    """Handlers block on external programs, so they must not run on the loop."""

    @pytest.mark.parametrize("module", API_MODULES, ids=lambda m: m.__name__)
    def test_no_handler_is_a_coroutine_function(self, module: Any) -> None:
        """Every route handler is sync, so FastAPI runs it in the threadpool."""
        offenders = [
            route.endpoint.__name__
            for route in module.router.routes
            if inspect.iscoroutinefunction(getattr(route, "endpoint", None))
        ]
        assert offenders == [], f"these handlers would freeze the event loop: {offenders}"

    def test_the_assembled_router_has_no_coroutine_handler_in_owned_modules(self) -> None:
        """The same holds once the sub-routers are mounted together."""
        owned = {Path(module.__file__).name for module in API_MODULES}
        offenders = [
            route.endpoint.__name__
            for route in router_module.router.routes
            if isinstance(route, APIRoute)
            and Path(inspect.getfile(route.endpoint)).name in owned
            and inspect.iscoroutinefunction(route.endpoint)
        ]
        assert offenders == [], f"these handlers would freeze the event loop: {offenders}"


class TestNoDirectProcessExecution:
    """The command runner is the only way these modules may run a program."""

    @pytest.mark.parametrize("source", OWNED_SOURCES, ids=lambda p: p.name)
    def test_module_does_not_import_subprocess(self, source: Path) -> None:
        """A module that imports subprocess has its own, untimed, unlogged runner."""
        tree = ast.parse(source.read_text())
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        assert "subprocess" not in imported, (
            f"{source.name} executes processes itself instead of using CommandRunner"
        )

    def test_job_functions_do_not_shell_out_to_the_wasm_binary(self) -> None:
        """The job system drives the managers, not a second copy of the product."""
        source = Path(jobs_module.__file__).read_text()
        assert '"wasm"' not in source, (
            "web/jobs.py still launches the wasm binary instead of calling the managers"
        )
        assert "Popen" not in source


class TestNoProcessKillEndpoint:
    """Decision D5 takes acting on processes out of the product."""

    def test_system_router_has_no_kill_route(self) -> None:
        """No route may signal a process."""
        paths = [route.path for route in system_api.router.routes]
        assert not any("kill" in path for path in paths), paths

    def test_system_module_never_signals_a_process(self) -> None:
        """Not even indirectly, through psutil."""
        source = Path(system_api.__file__).read_text()
        for forbidden in (".terminate()", ".kill()", "send_signal", "os.kill"):
            assert forbidden not in source, f"system.py still calls {forbidden}"

    def test_kill_endpoint_is_gone_from_the_http_surface(self) -> None:
        """A client that still calls it gets a 404, not a dead process."""
        client = _client((system_api.router, "/api/system"))
        response = client.post("/api/system/processes/1/kill")
        assert response.status_code == 404, response.text


class TestRouteOrdering:
    """A literal path declared after a parametrised one can never be matched."""

    def test_renew_all_is_reachable(self, certs_client: TestClient, monkeypatch) -> None:
        """POST /api/certs/renew-all must queue a renewal, not look up a domain."""
        queued: dict[str, Any] = {}

        def fake_create_job(**kwargs: Any) -> Any:
            queued.update(kwargs)
            return jobs_module.Job(
                id="deadbeef",
                type=kwargs["job_type"],
                name=kwargs["name"],
                description=kwargs["description"],
            )

        monkeypatch.setattr(
            certs_api,
            "get_job_manager",
            lambda: type("M", (), {"create_job": staticmethod(fake_create_job)})(),
        )

        response = certs_client.post("/api/certs/renew-all")

        assert response.status_code == 202, response.text
        assert queued["kwargs"] == {"domain": None, "force": False}
        assert response.json()["job_id"] == "deadbeef"

    def test_renew_all_is_declared_before_the_domain_route(self) -> None:
        """The ordering itself is pinned, so a future edit cannot bury it again."""
        paths = [route.path for route in certs_api.router.routes]
        assert paths.index("/renew-all") < paths.index("/{domain}")

    @pytest.mark.parametrize(
        ("module", "literal", "parametrised"),
        [
            (backups_api, "/storage", "/{backup_id}"),
            (sites_api, "/reload", "/{domain}"),
            (jobs_api, "/jobs/active", "/jobs/{job_id}"),
            (jobs_api, "/jobs/cleanup", "/jobs/{job_id}"),
            (databases_api, "/users/grant", "/users/{engine}"),
        ],
    )
    def test_literal_routes_come_first(self, module: Any, literal: str, parametrised: str) -> None:
        """Every literal route is registered before the parametrised one it shares a prefix with."""
        paths = [route.path for route in module.router.routes]
        assert paths.index(literal) < paths.index(parametrised), paths


class TestSiteNamingMatchesTheManager:
    """The divergence that made panel-created sites invisible to the CLI."""

    def test_api_and_manager_produce_the_same_file_name(
        self, sites_client: TestClient, sandbox_nginx: Any, site_dirs: tuple[Path, Path]
    ) -> None:
        """Creating a site through the API lands on the manager's own path."""
        available, enabled = site_dirs

        response = sites_client.post(
            "/api/sites",
            json={"domain": "panel.example.com", "template": "proxy", "port": 3000},
        )
        assert response.status_code == 200, response.text

        sandbox_nginx().create_site("cli.example.com", template="proxy")

        from_api = {p.name for p in available.iterdir()} - {"cli.example.com"}
        assert from_api == {"panel.example.com"}, (
            "the API writes a different file name than the manager, which is what made "
            f"panel-created sites invisible to the CLI: {sorted(from_api)}"
        )
        assert (enabled / "panel.example.com").exists()

    def test_created_site_is_visible_to_the_manager(
        self, sites_client: TestClient, sandbox_nginx: Any
    ) -> None:
        """The manager finds what the panel created, and finds it enabled."""
        response = sites_client.post("/api/sites", json={"domain": "panel.example.com"})
        assert response.status_code == 200, response.text

        manager = sandbox_nginx()
        assert manager.site_exists("panel.example.com")
        assert manager.site_enabled("panel.example.com")


class TestSiteConfigValidation:
    """
    PUT /{domain}/config must not persist a configuration the server rejects.

    It used to write whatever arrived, so a typo in a hand-edited config sat
    on disk until the next reload took the site down (finding M-4).
    """

    @pytest.fixture(autouse=True)
    def staging_tmp(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        """
        Keep validation's staging files inside the test's temporary directory.

        Args:
            tmp_path: Per-test temporary directory.
            monkeypatch: Patching helper, scoped to the test.

        Returns:
            The staging directory.
        """
        staging = tmp_path / "validation-tmp"
        staging.mkdir()
        monkeypatch.setattr(tempfile, "tempdir", str(staging))
        return staging

    def _created(self, sites_client: TestClient) -> str:
        """
        Create a site to edit.

        Args:
            sites_client: The authenticated client.

        Returns:
            The domain of the created site.
        """
        response = sites_client.post(
            "/api/sites", json={"domain": "panel.example.com", "enable": False}
        )
        assert response.status_code == 200, response.text
        return "panel.example.com"

    def test_an_invalid_config_is_refused_with_the_servers_own_words(
        self, sites_client: TestClient, sandbox_nginx: Any, runner: Any
    ) -> None:
        """The client gets nginx's output verbatim and the file is untouched."""
        domain = self._created(sites_client)
        before = sandbox_nginx().get_site_config(domain)
        stderr = 'nginx: [emerg] unexpected end of file, expecting "}"\n'
        runner.script(["nginx", "-t", "-c"], stderr=stderr, exit_code=1)

        response = sites_client.put(f"/api/sites/{domain}/config", json={"config": "server {"})

        assert response.status_code == 400, response.text
        body = response.json()
        assert body["error"] == "ValidationError"
        assert stderr.strip() in (body["hint"] or ""), body
        assert sandbox_nginx().get_site_config(domain) == before

    def test_a_valid_config_is_validated_and_then_persisted(
        self, sites_client: TestClient, sandbox_nginx: Any, runner: Any
    ) -> None:
        """The happy path still writes, and only after the syntax check ran."""
        domain = self._created(sites_client)
        new_config = "# hand edited\nserver {\n    listen 8081;\n}\n"

        response = sites_client.put(f"/api/sites/{domain}/config", json={"config": new_config})

        assert response.status_code == 200, response.text
        assert sandbox_nginx().get_site_config(domain) == new_config
        assert any(call[:3] == ("nginx", "-t", "-c") for call in runner.calls), runner.calls


class TestTraversal:
    """Every endpoint that takes a name or a domain must refuse to leave its tree."""

    @pytest.mark.parametrize("name", TRAVERSAL_NAMES)
    def test_strict_domain_rejects_traversal(self, name: str) -> None:
        """The shared validator refuses every payload, rather than normalising it."""
        with pytest.raises(DomainError):
            strict_domain(name)

    @pytest.mark.parametrize("name", TRAVERSAL_NAMES)
    def test_site_endpoints_reject_traversal(
        self, sites_client: TestClient, site_dirs: tuple[Path, Path], tmp_path: Path, name: str
    ) -> None:
        """No site endpoint may act on a path outside sites-available."""
        available, _ = site_dirs
        segment = _path_segment(name)

        for method, path, body in (
            ("get", f"/api/sites/{segment}", None),
            ("get", f"/api/sites/{segment}/config", None),
            ("put", f"/api/sites/{segment}/config", {"config": "server {}"}),
            ("post", f"/api/sites/{segment}/enable", None),
            ("post", f"/api/sites/{segment}/disable", None),
            ("delete", f"/api/sites/{segment}", None),
        ):
            response = getattr(sites_client, method)(path, **({"json": body} if body else {}))
            assert 400 <= response.status_code < 500, (
                f"{method.upper()} {path} accepted {name!r}: {response.status_code}"
            )

        assert list(available.iterdir()) == []

    @pytest.mark.parametrize("name", TRAVERSAL_NAMES)
    def test_create_site_rejects_traversal(
        self, sites_client: TestClient, site_dirs: tuple[Path, Path], name: str
    ) -> None:
        """A domain in the body is no more trusted than one in the path."""
        available, _ = site_dirs

        response = sites_client.post("/api/sites", json={"domain": name})

        assert 400 <= response.status_code < 500, (
            f"{name!r} was accepted with {response.status_code}: {response.text}"
        )
        assert list(available.iterdir()) == []

    @pytest.mark.parametrize("name", TRAVERSAL_NAMES)
    def test_cert_endpoints_reject_traversal(self, certs_client: TestClient, name: str) -> None:
        """Certificate names become paths under /etc/letsencrypt."""
        segment = _path_segment(name)

        for method, path in (
            ("get", f"/api/certs/{segment}"),
            ("post", f"/api/certs/{segment}"),
            ("post", f"/api/certs/{segment}/renew"),
            ("post", f"/api/certs/{segment}/revoke"),
            ("delete", f"/api/certs/{segment}"),
        ):
            response = getattr(certs_client, method)(path)
            assert 400 <= response.status_code < 500, (
                f"{method.upper()} {path} accepted {name!r}: {response.status_code}"
            )

    @pytest.mark.parametrize("name", TRAVERSAL_NAMES)
    def test_app_endpoints_reject_traversal(self, name: str, runner: Any) -> None:
        """Application domains become unit names and directories."""
        client = _client((apps_api.router, "/api/apps"))
        segment = _path_segment(name)

        for method, path in (
            ("get", f"/api/apps/{segment}"),
            ("get", f"/api/apps/{segment}/logs"),
            ("post", f"/api/apps/{segment}/start"),
            ("post", f"/api/apps/{segment}/stop"),
            ("post", f"/api/apps/{segment}/restart"),
            ("delete", f"/api/apps/{segment}"),
        ):
            response = getattr(client, method)(path)
            assert 400 <= response.status_code < 500, (
                f"{method.upper()} {path} accepted {name!r}: {response.status_code}"
            )

    @pytest.mark.parametrize("name", TRAVERSAL_NAMES)
    def test_backup_endpoints_reject_traversal(self, name: str, runner: Any) -> None:
        """A backup id names a file inside the backup directory."""
        client = _client((backups_api.router, "/api/backups"))
        segment = _path_segment(name)

        for method, path in (
            ("get", f"/api/backups/{segment}"),
            ("post", f"/api/backups/{segment}/verify"),
            ("post", f"/api/backups/{segment}/restore"),
            ("delete", f"/api/backups/{segment}"),
        ):
            response = getattr(client, method)(path)
            assert 400 <= response.status_code < 500, (
                f"{method.upper()} {path} accepted {name!r}: {response.status_code}"
            )

    @pytest.mark.parametrize("name", TRAVERSAL_NAMES)
    def test_job_endpoints_reject_traversal(self, name: str) -> None:
        """Job identifiers are hexadecimal, so nothing else is accepted."""
        client = _client((jobs_api.router, "/api"))
        segment = _path_segment(name)

        for method, path in (
            ("get", f"/api/jobs/{segment}"),
            ("post", f"/api/jobs/{segment}/cancel"),
        ):
            response = getattr(client, method)(path)
            assert 400 <= response.status_code < 500, (
                f"{method.upper()} {path} accepted {name!r}: {response.status_code}"
            )


class TestDatabaseConsole:
    """The SQL console is kept, but only as an explicit, bounded one."""

    def test_read_mode_refuses_a_write(self) -> None:
        """A statement that changes data needs mode='write'."""
        with pytest.raises(Exception) as excinfo:
            databases_api._check_read_only("postgres", "DELETE FROM users")
        assert "read-only" in str(excinfo.value).lower()

    @pytest.mark.parametrize(
        "statement",
        ["SELECT 1", "show tables", "EXPLAIN SELECT 1", "WITH x AS (SELECT 1) SELECT 1"],
    )
    def test_read_mode_accepts_reads(self, statement: str) -> None:
        """Read statements pass."""
        databases_api._check_read_only("postgres", statement)

    def test_read_mode_is_refused_for_engines_without_a_known_grammar(self) -> None:
        """WASM does not pretend to know what a read is in Redis."""
        with pytest.raises(Exception) as excinfo:
            databases_api._check_read_only("redis", "GET key")
        assert "read-only mode is not available" in str(excinfo.value).lower()

    def test_a_second_statement_is_refused(self) -> None:
        """One statement per request, so a SELECT cannot carry a DROP."""
        with pytest.raises(Exception) as excinfo:
            databases_api._reject_multiple_statements("SELECT 1; DROP TABLE users")
        assert "one statement" in str(excinfo.value).lower()

    def test_a_trailing_semicolon_is_fine(self) -> None:
        """The common case still works."""
        assert databases_api._reject_multiple_statements("SELECT 1;") == "SELECT 1"

    def test_output_is_bounded(self) -> None:
        """A large result set cannot pull the panel over."""
        text, truncated, rows = databases_api._truncate("\n".join(str(i) for i in range(500)), 10)

        assert truncated is True
        assert rows == 10
        assert text.splitlines() == [str(i) for i in range(10)]

    def test_the_query_endpoint_defaults_to_read_mode(self) -> None:
        """A client that says nothing gets the safe behaviour."""
        assert (
            databases_api.QueryRequest(database="db", engine="postgres", query="x").mode == "read"
        )


class TestErrorBoundary:
    """One translation of manager errors, not one per handler."""

    def test_wasm_error_becomes_a_client_error_not_a_500(self, sites_client: TestClient) -> None:
        """A rejected domain is a 400 with the manager's own message."""
        response = sites_client.post("/api/sites", json={"domain": "not a domain"})

        assert response.status_code == 400, response.text
        body = response.json()
        assert body["error"] == "DomainError"
        assert "not a domain" in body["detail"]

    def test_the_boundary_carries_the_managers_hint(self, sites_client: TestClient) -> None:
        """When the error knows how to fix itself, the client is told."""
        response = sites_client.post("/api/sites", json={"domain": "evil/../.."})

        assert response.status_code == 400, response.text
        assert response.json()["hint"]

    def test_every_owned_router_installs_the_boundary(self) -> None:
        """A router built without it would answer 500 for a validation failure."""
        from wasm.web.api.deps import WASMErrorRoute

        for module in API_MODULES:
            assert module.router.route_class is WASMErrorRoute, module.__name__
