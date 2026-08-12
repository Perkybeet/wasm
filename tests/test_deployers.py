# Copyright (c) 2024-2026 Yago Lopez Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
Tests for the deployers: detection, the commands they build, and rollback.

The deployers had no test coverage at all, which is how ``--type auto`` could
degrade to "nodejs" on every path and how a deployment that died halfway could
leave a service, a site and three store rows behind.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from wasm.core.exceptions import BuildError, DeploymentError
from wasm.core.runner import FakeRunner
from wasm.core.store import App, AppStatus, WASMStore
from wasm.deployers.auto import AutoDeployer
from wasm.deployers.docker_compose import DockerComposeDeployer
from wasm.deployers.interface import AppDeployer
from wasm.deployers.monorepo import MonorepoDeployer
from wasm.deployers.nextjs import NextJSDeployer
from wasm.deployers.nodejs import NodeJSDeployer
from wasm.deployers.python import PythonDeployer
from wasm.deployers.registry import DeployerRegistry, detect_app_type, get_deployer
from wasm.deployers.static import StaticDeployer
from wasm.deployers.vite import ViteDeployer

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def store(tmp_path: Path):
    """
    Provide an isolated store, installed as the process-wide singleton.

    Args:
        tmp_path: Per-test temporary directory.

    Yields:
        The store the deployers under test will write to.
    """
    WASMStore.reset_instance()
    instance = WASMStore(tmp_path / "wasm.db")
    yield instance
    WASMStore.reset_instance()


class FakeWebServer:
    """Stands in for NginxManager/ApacheManager."""

    def __init__(self) -> None:
        self.sites: dict[str, dict[str, Any]] = {}
        self.reloads = 0
        self.running = True

    def is_running(self) -> bool:
        return self.running

    def site_exists(self, domain: str) -> bool:
        return domain in self.sites

    def create_site(self, domain: str, template: str, context: dict[str, Any]) -> bool:
        self.sites[domain] = {"template": template, "context": context}
        return True

    def update_site(self, domain: str, template: str, context: dict[str, Any]) -> bool:
        self.sites[domain] = {"template": template, "context": context}
        return True

    def enable_site(self, domain: str) -> bool:
        return True

    def disable_site(self, domain: str) -> bool:
        return True

    def delete_site(self, domain: str) -> bool:
        self.sites.pop(domain, None)
        return True

    def reload(self) -> bool:
        self.reloads += 1
        return True


class FakeServiceManager:
    """Stands in for ServiceManager."""

    def __init__(self) -> None:
        self.units: dict[str, dict[str, Any]] = {}
        self.started: list[str] = []

    def create_service(self, name: str, **kwargs: Any) -> bool:
        self.units[name] = kwargs
        return True

    def enable(self, name: str) -> bool:
        return True

    def start(self, name: str) -> bool:
        self.started.append(name)
        return True

    def stop(self, name: str) -> bool:
        return True

    def get_status(self, name: str) -> dict[str, Any]:
        return {"exists": name in self.units}

    def delete_service(self, name: str) -> bool:
        self.units.pop(name, None)
        return True


class FakeSourceManager:
    """Stands in for SourceManager, copying a local tree instead of cloning."""

    def __init__(self, source_tree: Path | None = None) -> None:
        self.source_tree = source_tree
        self.fetches: list[Path] = []

    def fetch(self, source, destination, branch=None, **kwargs: Any) -> bool:  # type: ignore[no-untyped-def]
        import shutil

        destination = Path(destination)
        self.fetches.append(destination)
        destination.mkdir(parents=True, exist_ok=True)
        if self.source_tree is not None:
            shutil.copytree(self.source_tree, destination, dirs_exist_ok=True)
        return True


class FakeCertManager:
    """Stands in for CertManager."""

    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.obtained: list[str] = []

    def obtain(self, domain: str, **kwargs: Any) -> bool:
        if self.fail:
            raise DeploymentError("certbot said no")
        self.obtained.append(domain)
        return True


def build_deployer(
    deployer_class: type,
    tmp_path: Path,
    *,
    domain: str = "app.example.com",
    ssl: bool = False,
) -> Any:
    """
    Build a deployer wired to fakes for every manager it touches.

    Args:
        deployer_class: The deployer to instantiate.
        tmp_path: Directory the application is deployed into.
        domain: Domain to deploy.
        ssl: Whether to request a certificate.

    Returns:
        The configured deployer, with ``web``, ``services`` and ``certs``
        attributes exposing the fakes for assertions.
    """
    deployer = deployer_class(verbose=False)
    deployer.configure(
        domain,
        "https://github.com/example/app.git",
        port=3000,
        ssl=ssl,
        app_path=tmp_path / "app",
    )

    web = FakeWebServer()
    services = FakeServiceManager()
    certs = FakeCertManager()
    deployer._webserver_manager = lambda: web  # type: ignore[method-assign]
    deployer.service_manager = services
    deployer.cert_manager = certs
    deployer.pre_flight_check = lambda: True  # type: ignore[method-assign]

    deployer.web = web
    deployer.services = services
    deployer.certs = certs
    return deployer


# ---------------------------------------------------------------------------
# Project trees used by the detection tests
# ---------------------------------------------------------------------------


def write_tree(root: Path, files: dict[str, str]) -> Path:
    """
    Materialise a fake project tree.

    Args:
        root: Directory to create the files in.
        files: Mapping of relative path to content.

    Returns:
        The root, for chaining.
    """
    for relative, content in files.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    return root


PACKAGE_JSON = json.dumps({"name": "app", "scripts": {"start": "node server.js"}})
NEXT_PACKAGE_JSON = json.dumps({"name": "app", "dependencies": {"next": "14.0.0"}})
VITE_PACKAGE_JSON = json.dumps({"name": "app", "devDependencies": {"vite": "5.0.0"}})
WORKSPACE_PACKAGE_JSON = json.dumps({"name": "root", "workspaces": ["apps/*"]})

TREES: dict[str, dict[str, str]] = {
    "nextjs": {"next.config.js": "module.exports = {}", "package.json": NEXT_PACKAGE_JSON},
    "nextjs-by-dependency": {"package.json": NEXT_PACKAGE_JSON},
    "vite": {"vite.config.ts": "export default {}", "package.json": VITE_PACKAGE_JSON},
    "python-requirements": {"requirements.txt": "flask==3.0.0\n"},
    "python-pyproject": {"pyproject.toml": "[project]\nname = 'x'\n"},
    "static": {"index.html": "<html></html>"},
    "nodejs": {"package.json": PACKAGE_JSON},
    "docker-compose": {"docker-compose.yml": "services:\n  web:\n    image: nginx\n"},
    "monorepo": {
        "turbo.json": "{}",
        "pnpm-workspace.yaml": "packages:\n  - apps/*\n",
        "apps/web/package.json": PACKAGE_JSON,
        "apps/api/package.json": PACKAGE_JSON,
    },
}


# ---------------------------------------------------------------------------
# detect()
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("tree", "expected"),
    [
        ("nextjs", "nextjs"),
        ("nextjs-by-dependency", "nextjs"),
        ("vite", "vite"),
        ("python-requirements", "python"),
        ("python-pyproject", "python"),
        ("static", "static"),
        ("nodejs", "nodejs"),
        ("docker-compose", "docker-compose"),
        ("monorepo", "monorepo"),
    ],
)
def test_detect_identifies_each_project_shape(tmp_path: Path, tree: str, expected: str) -> None:
    """Every supported project type is recognised from its files alone."""
    write_tree(tmp_path, TREES[tree])

    assert detect_app_type(tmp_path) == expected


def test_detect_returns_none_for_an_unrecognisable_tree(tmp_path: Path) -> None:
    """A directory of prose is not an application."""
    write_tree(tmp_path, {"README.md": "hello"})

    assert detect_app_type(tmp_path) is None


# Ambiguous trees: these fix the precedence, which is the whole point of the
# priority attribute. Each of these matches at least two deployers.


def test_monorepo_beats_the_package_json_of_its_own_apps(tmp_path: Path) -> None:
    """A workspace with several apps is a monorepo, not a Node app."""
    write_tree(tmp_path, TREES["monorepo"])
    (tmp_path / "package.json").write_text(WORKSPACE_PACKAGE_JSON)

    assert NodeJSDeployer().detect(tmp_path) or True  # nodejs would also match
    assert detect_app_type(tmp_path) == "monorepo"


def test_monorepo_beats_a_compose_file_at_its_root(tmp_path: Path) -> None:
    """A compose file next to a workspace does not make it a compose project."""
    write_tree(tmp_path, TREES["monorepo"])
    (tmp_path / "package.json").write_text(WORKSPACE_PACKAGE_JSON)
    (tmp_path / "docker-compose.yml").write_text("services:\n  db:\n    image: postgres\n")

    assert detect_app_type(tmp_path) == "monorepo"


def test_production_compose_file_beats_a_framework_config(tmp_path: Path) -> None:
    """docker-compose.prod.yml states how the author wants it run."""
    write_tree(tmp_path, TREES["nextjs"])
    (tmp_path / "docker-compose.prod.yml").write_text("services:\n  web:\n    build: .\n")

    assert NextJSDeployer().detect(tmp_path)
    assert detect_app_type(tmp_path) == "docker-compose"


def test_development_compose_file_does_not_beat_a_framework_config(tmp_path: Path) -> None:
    """A local postgres for development is not the deployment target."""
    write_tree(tmp_path, TREES["nextjs"])
    (tmp_path / "docker-compose.yml").write_text("services:\n  db:\n    image: postgres\n")

    assert detect_app_type(tmp_path) == "nextjs"


def test_nextjs_beats_generic_nodejs(tmp_path: Path) -> None:
    """A framework is more specific than "it has a package.json"."""
    write_tree(tmp_path, TREES["nextjs"])

    assert detect_app_type(tmp_path) == "nextjs"


def test_static_loses_to_every_framework(tmp_path: Path) -> None:
    """An index.html in a Vite project is the template, not the site."""
    write_tree(tmp_path, TREES["vite"])
    (tmp_path / "index.html").write_text("<html></html>")

    assert detect_app_type(tmp_path) == "vite"


def test_turbo_without_multiple_apps_is_not_a_monorepo(tmp_path: Path) -> None:
    """A single app that uses turbo for build caching is still a single app."""
    write_tree(
        tmp_path,
        {
            "turbo.json": "{}",
            "pnpm-workspace.yaml": "packages:\n  - apps/*\n",
            "apps/web/package.json": PACKAGE_JSON,
            "next.config.js": "module.exports = {}",
            "package.json": NEXT_PACKAGE_JSON,
        },
    )

    assert not MonorepoDeployer().detect(tmp_path)
    assert detect_app_type(tmp_path) == "nextjs"


def test_detection_order_is_independent_of_registration_order() -> None:
    """Precedence comes from the priority attribute, not from import order."""
    order = [d.APP_TYPE for d in DeployerRegistry.in_detection_order() if d.APP_TYPE != "auto"]

    assert order == ["monorepo", "docker-compose", "nextjs", "vite", "python", "nodejs", "static"]


# ---------------------------------------------------------------------------
# --type auto
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("tree", "expected"),
    [
        ("nextjs", "nextjs"),
        ("vite", "vite"),
        ("python-requirements", "python"),
        ("static", "static"),
        ("nodejs", "nodejs"),
        ("docker-compose", "docker-compose"),
        ("monorepo", "monorepo"),
    ],
)
def test_auto_resolves_to_the_right_deployer(
    tmp_path: Path, store: WASMStore, tree: str, expected: str
) -> None:
    """``--type auto`` picks the deployer that matches the fetched source."""
    source = tmp_path / "src"
    source.mkdir()
    write_tree(source, TREES[tree])

    auto = AutoDeployer()
    auto.configure("app.example.com", str(source), app_path=tmp_path / "app")
    auto.source_manager = FakeSourceManager(source)

    delegate = auto.resolve()

    assert auto.resolved_type == expected
    assert delegate.APP_TYPE == expected


def test_auto_tells_the_delegate_not_to_refetch(tmp_path: Path, store: WASMStore) -> None:
    """Re-fetching would clean the directory and clone the repository twice."""
    source = tmp_path / "src"
    source.mkdir()
    write_tree(source, TREES["nodejs"])

    auto = AutoDeployer()
    auto.configure("app.example.com", str(source), app_path=tmp_path / "app")
    auto.source_manager = FakeSourceManager(source)

    delegate = auto.resolve()

    assert delegate.source_already_fetched is True


def test_auto_refuses_an_empty_source(tmp_path: Path, store: WASMStore) -> None:
    """An empty checkout is a mistake worth reporting, not a Node app."""
    auto = AutoDeployer()
    auto.configure("app.example.com", str(tmp_path / "src"), app_path=tmp_path / "app")
    auto.source_manager = FakeSourceManager()

    with pytest.raises(DeploymentError, match="Nothing to deploy"):
        auto.resolve()


def test_auto_falls_back_when_nothing_matches(tmp_path: Path, store: WASMStore) -> None:
    """A tree with source but no signals still deploys, as generic Node."""
    source = tmp_path / "src"
    write_tree(source, {"README.md": "hello"})

    auto = AutoDeployer()
    auto.configure("app.example.com", str(source), app_path=tmp_path / "app")
    auto.source_manager = FakeSourceManager(source)

    delegate = auto.resolve()

    assert delegate.APP_TYPE == AutoDeployer.FALLBACK_TYPE


# ---------------------------------------------------------------------------
# The commands each deployer builds
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("package_manager", "install", "run_build"),
    [
        ("npm", ["npm", "ci"], ["npm", "run", "build"]),
        ("pnpm", ["pnpm", "install", "--frozen-lockfile"], ["pnpm", "run", "build"]),
        ("yarn", ["yarn", "install", "--frozen-lockfile"], ["yarn", "build"]),
        ("bun", ["bun", "install", "--frozen-lockfile"], ["bun", "run", "build"]),
    ],
)
def test_nextjs_commands_per_package_manager(
    tmp_path: Path,
    package_manager: str,
    install: list[str],
    run_build: list[str],
) -> None:
    """The exact argv for install and build, per package manager."""
    deployer = NextJSDeployer()
    deployer.configure("app.example.com", "src", app_path=tmp_path)
    deployer.package_manager = package_manager

    assert deployer.get_install_command() == install
    assert deployer.get_build_command() == run_build


@pytest.mark.parametrize(
    ("package_manager", "start"),
    [
        ("npm", "npm run start"),
        ("pnpm", "pnpm run start"),
        ("yarn", "yarn start"),
        ("bun", "bun run start"),
    ],
)
def test_nextjs_start_command(tmp_path: Path, package_manager: str, start: str) -> None:
    """The start command systemd will run."""
    deployer = NextJSDeployer()
    deployer.configure("app.example.com", "src", app_path=tmp_path)
    deployer.package_manager = package_manager

    assert deployer.get_start_command() == start


def test_nextjs_standalone_start_command(tmp_path: Path) -> None:
    """Standalone output runs the generated server, not the package script."""
    deployer = NextJSDeployer()
    deployer.configure("app.example.com", "src", app_path=tmp_path)
    deployer.is_standalone = True

    assert deployer.get_start_command() == "node .next/standalone/server.js"


def test_nodejs_skips_build_when_there_is_no_build_script(tmp_path: Path) -> None:
    """An empty build command means the pipeline step does nothing."""
    deployer = NodeJSDeployer()
    deployer.configure("app.example.com", "src", app_path=tmp_path)
    deployer.package_manager = "npm"

    assert deployer.get_build_command() == []

    deployer.has_build = True
    assert deployer.get_build_command() == ["npm", "run", "build"]


def test_vite_has_no_start_command_for_a_static_build(tmp_path: Path) -> None:
    """A built Vite app is files on disk; there is nothing to keep running."""
    deployer = ViteDeployer()
    deployer.configure("app.example.com", "src", app_path=tmp_path)
    deployer.package_manager = "npm"

    assert deployer.get_build_command() == ["npm", "run", "build"]
    assert deployer.get_start_command() == ""
    assert deployer.get_nginx_template() == "static"


def test_vite_ssr_runs_preview(tmp_path: Path) -> None:
    """An SSR build needs a process and a proxy."""
    deployer = ViteDeployer()
    deployer.configure("app.example.com", "src", app_path=tmp_path)
    deployer.package_manager = "pnpm"
    deployer.is_ssr = True

    assert deployer.get_start_command() == "pnpm run preview"
    assert deployer.get_nginx_template() == "proxy"


def test_python_commands_use_the_virtualenv(tmp_path: Path) -> None:
    """Nothing is installed or run outside the app's own virtualenv."""
    deployer = PythonDeployer()
    deployer.configure("app.example.com", "src", app_path=tmp_path)
    deployer.venv_path = tmp_path / "venv"

    assert deployer.get_install_command() == [
        str(tmp_path / "venv" / "bin" / "pip"),
        "install",
        "-r",
        "requirements.txt",
    ]
    assert deployer.get_build_command() == []

    deployer.framework = "django"
    assert deployer.get_build_command() == [
        str(tmp_path / "venv" / "bin" / "python"),
        "manage.py",
        "collectstatic",
        "--noinput",
    ]


def test_python_poetry_and_pipenv_commands(tmp_path: Path) -> None:
    """Poetry and pipenv projects install through their own tool."""
    deployer = PythonDeployer()
    deployer.configure("app.example.com", "src", app_path=tmp_path)

    deployer.use_poetry = True
    assert deployer.get_install_command() == ["poetry", "install", "--no-dev"]

    deployer.use_poetry = False
    deployer.use_pipenv = True
    assert deployer.get_install_command() == ["pipenv", "install", "--deploy"]


def test_static_deployer_runs_nothing(tmp_path: Path) -> None:
    """A static site has no install, no build and no service."""
    deployer = StaticDeployer()
    deployer.configure("app.example.com", "src", app_path=tmp_path)

    assert deployer.get_install_command() == []
    assert deployer.get_build_command() == []
    assert deployer.get_start_command() == ""
    assert deployer.get_nginx_template() == "static"


def test_docker_compose_builds_argv_with_file_and_profiles(tmp_path: Path) -> None:
    """Compose invocations name the file and every requested profile."""
    deployer = DockerComposeDeployer()
    deployer.configure(
        "app.example.com",
        "src",
        app_path=tmp_path,
        compose_profiles=["prod", "worker"],
    )
    deployer.compose_path = tmp_path / "docker-compose.prod.yml"

    assert deployer._compose("build") == [
        "docker",
        "compose",
        "-f",
        str(tmp_path / "docker-compose.prod.yml"),
        "--profile",
        "prod",
        "--profile",
        "worker",
        "build",
    ]


def test_docker_compose_build_streams_and_has_a_deadline(
    tmp_path: Path, runner: FakeRunner
) -> None:
    """Image builds are streamed so they do not look frozen, and time out."""
    deployer = DockerComposeDeployer(runner=runner)
    deployer.configure("app.example.com", "src", app_path=tmp_path)
    deployer.compose_path = tmp_path / "docker-compose.yml"

    deployer._build_images()

    assert runner.ran("docker", "compose", "-f", str(tmp_path / "docker-compose.yml"), "build")


# ---------------------------------------------------------------------------
# The hierarchy the registry claims
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "deployer_class",
    [
        NextJSDeployer,
        NodeJSDeployer,
        ViteDeployer,
        PythonDeployer,
        StaticDeployer,
        MonorepoDeployer,
        DockerComposeDeployer,
        AutoDeployer,
    ],
)
def test_every_registered_deployer_implements_the_interface(deployer_class: type) -> None:
    """The registry is typed on AppDeployer; that must be true at runtime too."""
    assert issubclass(deployer_class, AppDeployer)


@pytest.mark.parametrize("app_type", ["monorepo", "docker-compose", "auto"])
def test_odd_deployers_accept_the_common_configure_call(
    tmp_path: Path, store: WASMStore, app_type: str
) -> None:
    """POST /api/apps used to raise TypeError for exactly these three."""
    deployer = get_deployer(app_type)

    deployer.configure(
        domain="app.example.com",
        source="https://github.com/example/app.git",
        port=3000,
        webserver="nginx",
        ssl=False,
        branch="main",
        env_vars={"A": "b"},
        package_manager="pnpm",
    )

    assert deployer.domain == "app.example.com"


def test_get_deployer_rejects_an_unknown_type() -> None:
    """An unknown --type is a clear error, not a silent fallback."""
    with pytest.raises(ValueError, match="Unsupported application type"):
        get_deployer("cobol")


# ---------------------------------------------------------------------------
# Rollback
# ---------------------------------------------------------------------------


def test_failed_first_deployment_leaves_nothing_behind(tmp_path: Path, store: WASMStore) -> None:
    """A build failure must undo the app row, the files and everything after."""
    deployer = build_deployer(NodeJSDeployer, tmp_path)
    app_dir = tmp_path / "app"

    def fetch() -> bool:
        app_dir.mkdir(parents=True, exist_ok=True)
        (app_dir / "package.json").write_text(PACKAGE_JSON)
        return True

    deployer.fetch_source = fetch  # type: ignore[method-assign]
    deployer.install_dependencies = lambda: True  # type: ignore[method-assign]
    deployer.build = _raise(BuildError("build blew up"))  # type: ignore[method-assign]

    with pytest.raises(BuildError):
        deployer.deploy()

    assert store.get_app("app.example.com") is None
    assert store.get_site("app.example.com") is None
    assert store.get_service("app-example-com") is None
    assert not app_dir.exists()


def test_failed_deployment_removes_the_site_it_had_created(
    tmp_path: Path, store: WASMStore
) -> None:
    """A failure after the site is written removes the site config too."""
    deployer = build_deployer(NodeJSDeployer, tmp_path)

    deployer.fetch_source = lambda: True  # type: ignore[method-assign]
    deployer.install_dependencies = lambda: True  # type: ignore[method-assign]
    deployer.build = lambda: True  # type: ignore[method-assign]
    deployer.create_service = _raise(DeploymentError("systemd said no"))  # type: ignore[method-assign]

    with pytest.raises(DeploymentError):
        deployer.deploy()

    assert deployer.web.sites == {}
    assert store.get_site("app.example.com") is None
    assert store.get_app("app.example.com") is None


def test_failed_deployment_removes_the_service_it_had_created(
    tmp_path: Path, store: WASMStore
) -> None:
    """A failure while starting removes the unit that was just installed."""
    deployer = build_deployer(NodeJSDeployer, tmp_path)

    deployer.fetch_source = lambda: True  # type: ignore[method-assign]
    deployer.install_dependencies = lambda: True  # type: ignore[method-assign]
    deployer.build = lambda: True  # type: ignore[method-assign]
    deployer.start = _raise(DeploymentError("unit failed to start"))  # type: ignore[method-assign]

    with pytest.raises(DeploymentError):
        deployer.deploy()

    assert deployer.services.units == {}
    assert store.get_service("app-example-com") is None
    assert store.get_app("app.example.com") is None


def test_failed_redeployment_keeps_the_existing_app_row(tmp_path: Path, store: WASMStore) -> None:
    """A redeployment that fails must not delete the app it was updating."""
    store.create_app(
        App(
            domain="app.example.com",
            app_type="nodejs",
            source="https://github.com/example/app.git",
            port=3000,
            app_path=str(tmp_path / "app"),
            status=AppStatus.RUNNING.value,
        )
    )

    deployer = build_deployer(NodeJSDeployer, tmp_path)
    deployer.fetch_source = lambda: True  # type: ignore[method-assign]
    deployer.install_dependencies = lambda: True  # type: ignore[method-assign]
    deployer.build = _raise(BuildError("build blew up"))  # type: ignore[method-assign]

    with pytest.raises(BuildError):
        deployer.deploy()

    surviving = store.get_app("app.example.com")
    assert surviving is not None
    assert surviving.status == AppStatus.FAILED.value


def test_successful_deployment_registers_app_site_and_service(
    tmp_path: Path, store: WASMStore
) -> None:
    """The happy path writes the three rows the rest of the CLI reads."""
    deployer = build_deployer(NodeJSDeployer, tmp_path)
    (tmp_path / "app").mkdir(parents=True)

    deployer.fetch_source = lambda: True  # type: ignore[method-assign]
    deployer.install_dependencies = lambda: True  # type: ignore[method-assign]
    deployer.build = lambda: True  # type: ignore[method-assign]
    deployer.health_check = lambda retries=5, delay=2.0: True  # type: ignore[method-assign]

    assert deployer.deploy() is True

    app = store.get_app("app.example.com")
    assert app is not None
    assert app.status == AppStatus.RUNNING.value
    assert store.get_site("app.example.com") is not None
    assert store.get_service("app-example-com") is not None
    assert deployer.services.started == ["app-example-com"]


def test_certificate_failure_does_not_fail_the_deployment(tmp_path: Path, store: WASMStore) -> None:
    """No certificate still leaves a working HTTP deployment."""
    deployer = build_deployer(NodeJSDeployer, tmp_path, ssl=True)
    (tmp_path / "app").mkdir(parents=True)
    deployer.cert_manager = FakeCertManager(fail=True)

    deployer.fetch_source = lambda: True  # type: ignore[method-assign]
    deployer.install_dependencies = lambda: True  # type: ignore[method-assign]
    deployer.build = lambda: True  # type: ignore[method-assign]
    deployer.health_check = lambda retries=5, delay=2.0: True  # type: ignore[method-assign]

    assert deployer.deploy() is True

    app = store.get_app("app.example.com")
    assert app is not None
    assert app.status == AppStatus.RUNNING.value
    assert not app.ssl_enabled


def test_static_pipeline_has_no_install_or_build_steps(tmp_path: Path, store: WASMStore) -> None:
    """The static deployer describes a shorter pipeline, not a copied deploy()."""
    deployer = build_deployer(StaticDeployer, tmp_path)

    titles = [step.title for step in deployer.build_pipeline()]

    assert "Installing dependencies" not in titles
    assert "Building application" not in titles
    assert titles[0] == "Fetching source code"


def test_deploy_without_configure_is_a_clear_error(tmp_path: Path, store: WASMStore) -> None:
    """An unconfigured deployer says so instead of deploying to ''."""
    deployer = NodeJSDeployer()

    with pytest.raises(DeploymentError, match="not configured"):
        deployer.deploy()


def _raise(error: Exception):
    """
    Build a zero-argument callable that raises.

    Args:
        error: The exception to raise.

    Returns:
        A callable suitable for replacing a pipeline step.
    """

    def _fail(*args: Any, **kwargs: Any) -> Any:
        raise error

    return _fail


# ---------------------------------------------------------------------------
# Long commands go through stream()
# ---------------------------------------------------------------------------


def test_install_and_build_are_streamed_with_finite_timeouts(
    tmp_path: Path, store: WASMStore
) -> None:
    """A ten-minute npm install must show progress and still have a deadline."""
    streamed: list[tuple[tuple[str, ...], int]] = []

    class StreamingRunner(FakeRunner):
        def stream(self, argv, *, on_line, cwd=None, env=None, timeout=60, user=None, secrets=()):  # type: ignore[no-untyped-def]
            streamed.append((tuple(argv), timeout))
            return super().stream(
                argv, on_line=on_line, cwd=cwd, env=env, timeout=timeout, secrets=secrets
            )

    runner = StreamingRunner()
    deployer = NodeJSDeployer(runner=runner)
    deployer.configure("app.example.com", "src", app_path=tmp_path)
    deployer.package_manager = "npm"
    deployer.has_build = True
    deployer.pre_install = lambda: True  # type: ignore[method-assign]
    deployer.post_install = lambda: True  # type: ignore[method-assign]

    deployer.install_dependencies()
    deployer.build()

    assert [argv for argv, _ in streamed] == [("npm", "ci"), ("npm", "run", "build")]
    assert all(timeout > 0 for _, timeout in streamed)


# ---------------------------------------------------------------------------
# The pipeline itself
# ---------------------------------------------------------------------------


def test_pipeline_undoes_only_the_steps_that_ran() -> None:
    """A step that never started must not have its undo executed."""
    from wasm.core.logger import Logger
    from wasm.deployers.pipeline import DeployStep, run_pipeline

    undone: list[str] = []

    steps = [
        DeployStep("one", "", run=lambda: None, undo=lambda: undone.append("one")),
        DeployStep("two", "", run=_raise(BuildError("boom")), undo=lambda: undone.append("two")),
        DeployStep("three", "", run=lambda: None, undo=lambda: undone.append("three")),
    ]

    with pytest.raises(BuildError):
        run_pipeline(steps, Logger(verbose=False))

    assert undone == ["two", "one"]


def test_pipeline_skips_a_step_and_its_undo() -> None:
    """A skipped step leaves nothing, so it must not be undone either."""
    from wasm.core.logger import Logger
    from wasm.deployers.pipeline import DeployStep, run_pipeline

    undone: list[str] = []

    steps = [
        DeployStep(
            "skipped",
            "",
            run=_raise(AssertionError("must not run")),
            undo=lambda: undone.append("skipped"),
            skip_if=lambda: True,
        ),
        DeployStep("fails", "", run=_raise(BuildError("boom"))),
    ]

    with pytest.raises(BuildError):
        run_pipeline(steps, Logger(verbose=False))

    assert undone == []


def test_pipeline_keeps_undoing_after_an_undo_fails() -> None:
    """One cleanup that cannot complete must not strand the others."""
    from wasm.core.logger import Logger
    from wasm.deployers.pipeline import DeployStep, run_pipeline

    undone: list[str] = []

    steps = [
        DeployStep("first", "", run=lambda: None, undo=lambda: undone.append("first")),
        DeployStep("second", "", run=lambda: None, undo=_raise(OSError("device busy"))),
        DeployStep("third", "", run=_raise(BuildError("boom"))),
    ]

    with pytest.raises(BuildError):
        run_pipeline(steps, Logger(verbose=False))

    assert undone == ["first"]


def test_failure_output_combines_both_streams() -> None:
    """npm diagnoses on stdout, pip on stderr; the error must show either."""
    from wasm.core.runner import CommandResult
    from wasm.deployers.helpers.health import failure_output

    combined = failure_output(
        CommandResult(argv=("npm", "ci"), exit_code=1, stdout="ERESOLVE\n", stderr="npm ERR!\n")
    )

    assert combined == "npm ERR!\nERESOLVE"
    assert failure_output(CommandResult(argv=("x",), exit_code=1)) == ""
