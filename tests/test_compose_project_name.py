# Copyright (c) 2024-2026 Yago Lopez Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
Tests for the Docker Compose project name (Finding 11: volume naming).

Without ``-p``, Compose names a project - and therefore every named volume,
network and container - after the directory of the first ``-f`` file it is
given. WASM always ran ``docker compose`` with ``app_path`` as the working
directory and an absolute ``-f`` built from it, so that name has always been
implicit and has always matched what :func:`compose_project_name` computes.
These tests exist so a future change to ``_compose()``, ``_run()`` or the
compose file layout cannot silently rename a project on an app that already
has data in its named volumes: a rename means the next ``docker compose up``
creates new, empty volumes instead of reusing the ones with the data.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from wasm.core.runner import FakeRunner
from wasm.core.store import WASMStore
from wasm.deployers.docker_compose import DockerComposeDeployer, compose_project_name

# ---------------------------------------------------------------------------
# compose_project_name(): the pure derivation
# ---------------------------------------------------------------------------


def test_a_dotted_domain_yields_the_project_name_1x_produced(tmp_path: Path) -> None:
    """
    ``shop.example.com`` deploys to ``shop-example-com``; that directory,
    with the compose file directly inside it, is exactly what Compose (and
    therefore 1.x, which ran the same ``-f``/cwd combination) already named
    the project after.
    """
    app_path = tmp_path / "shop-example-com"
    app_path.mkdir()
    compose_path = app_path / "docker-compose.yml"

    assert compose_project_name(app_path, compose_path) == "shop-example-com"


def test_without_a_discovered_file_it_falls_back_to_app_path(tmp_path: Path) -> None:
    """Before discovery runs, there is no compose file yet: normalise app_path."""
    app_path = tmp_path / "not-yet-discovered"

    assert compose_project_name(app_path, None) == "not-yet-discovered"


def test_a_legacy_wasm_prefixed_directory_keeps_its_own_name(tmp_path: Path) -> None:
    """
    Apps from before 0.14.1 live in ``wasm-<name>``. The project name has
    always come from the real directory on disk, not from a freshly computed
    app name, so it stays ``wasm-shop-example-com`` - matching whatever the
    containers and volumes are already labelled with today.
    """
    app_path = tmp_path / "wasm-shop-example-com"
    app_path.mkdir()
    compose_path = app_path / "docker-compose.yml"

    assert compose_project_name(app_path, compose_path) == "wasm-shop-example-com"


def test_invalid_characters_are_stripped_like_compose_strips_them(tmp_path: Path) -> None:
    """Upper case and punctuation are not valid in a Compose project name."""
    app_path = tmp_path / "My.App!"
    compose_path = app_path / "docker-compose.yml"

    assert compose_project_name(app_path, compose_path) == "myapp"


def test_a_name_that_is_entirely_invalid_falls_back_to_default(tmp_path: Path) -> None:
    """A directory named only punctuation normalises to nothing; do not return ''."""
    app_path = tmp_path / "___"
    compose_path = app_path / "docker-compose.yml"

    assert compose_project_name(app_path, compose_path) == "default"


def test_a_compose_file_in_a_subdirectory_keeps_the_subdirectory_name(tmp_path: Path) -> None:
    """
    A compose file named ``deploy/docker-compose.yml`` makes Compose - and
    therefore 1.x - use ``deploy`` as the project, not the app's own
    directory. That is a latent collision risk between two apps that both
    put their compose file under a directory called ``deploy``, but
    repointing it at ``app_path`` instead would itself be an unannounced
    project rename for every application already deployed this way, which is
    the one thing this function exists to never do. The safe fix for the
    collision is naming the subdirectory after the app when it is created,
    not renaming it after the fact.
    """
    app_path = tmp_path / "shop-example-com"
    compose_path = app_path / "deploy" / "docker-compose.yml"

    assert compose_project_name(app_path, compose_path) == "deploy"


# ---------------------------------------------------------------------------
# _compose(): every invocation carries the same -p
# ---------------------------------------------------------------------------


def test_compose_argv_carries_the_project_name(tmp_path: Path) -> None:
    """``-p`` is pinned before ``-f``, using the same derivation."""
    deployer = DockerComposeDeployer()
    deployer.configure("shop.example.com", "src", app_path=tmp_path / "shop-example-com")
    deployer.compose_path = tmp_path / "shop-example-com" / "docker-compose.yml"

    assert deployer._compose("up", "-d") == [
        "docker",
        "compose",
        "-p",
        "shop-example-com",
        "-f",
        str(tmp_path / "shop-example-com" / "docker-compose.yml"),
        "up",
        "-d",
    ]


def test_compose_argv_has_no_project_flag_before_discovery(tmp_path: Path) -> None:
    """No compose file yet: neither ``-p`` nor ``-f`` are meaningful."""
    deployer = DockerComposeDeployer()
    deployer.configure("shop.example.com", "src", app_path=tmp_path / "shop-example-com")

    assert deployer._compose("ps") == ["docker", "compose", "ps"]


# ---------------------------------------------------------------------------
# deploy / update / delete: the same app, the same project, every time
# ---------------------------------------------------------------------------


@pytest.fixture
def store(tmp_path: Path):
    """An isolated store, installed as the process-wide singleton."""
    WASMStore.reset_instance()
    instance = WASMStore(tmp_path / "wasm.db")
    yield instance
    WASMStore.reset_instance()


class _FakeServiceManager:
    """Stands in for ServiceManager: no real systemd unit is touched."""

    def create_service(self, name: str, **kwargs: object) -> bool:
        return True

    def enable(self, name: str) -> bool:
        return True

    def start(self, name: str) -> bool:
        return True

    def stop(self, name: str) -> bool:
        return True

    def get_status(self, name: str) -> dict[str, object]:
        return {"exists": True}

    def delete_service(self, name: str) -> bool:
        return True


class _FakeWebServer:
    """Stands in for NginxManager: no real site is touched."""

    def site_exists(self, domain: str) -> bool:
        return False

    def delete_site(self, domain: str) -> bool:
        return True

    def reload(self) -> bool:
        return True


def test_deploy_and_a_later_delete_use_the_same_project_for_one_app(
    tmp_path: Path,
    store: WASMStore,
    runner: FakeRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A stack deployed once must be torn down under the same project it was
    brought up under, or ``down`` (and a later re-``up``) operates on a
    project Compose has never heard of, leaving the original containers,
    networks and volumes orphaned.
    """
    from wasm.deployers import docker_compose as compose_module

    monkeypatch.setattr(compose_module, "ServiceManager", lambda **kw: _FakeServiceManager())
    monkeypatch.setattr(compose_module, "NginxManager", lambda **kw: _FakeWebServer())

    app_path = tmp_path / "shop-example-com"
    app_path.mkdir()
    (app_path / "docker-compose.yml").write_text("services:\n  db:\n    image: postgres\n")

    deployer = DockerComposeDeployer(runner=runner)
    # The source is already in the directory, as the auto deployer leaves it;
    # a directory with files in it is only deployed into when asked.
    deployer.configure("shop.example.com", str(app_path), app_path=app_path, replace_existing=True)
    deployer.source_already_fetched = True
    deployer.deploy()

    deploy_calls = [c for c in runner.calls if c[:2] == ("docker", "compose")]
    assert deploy_calls, "expected at least one docker compose invocation during deploy"
    for call in deploy_calls:
        assert call[2:4] == ("-p", "shop-example-com")

    runner.calls.clear()

    deleter = DockerComposeDeployer(runner=runner)
    deleter.app_path = app_path
    deleter.app_name = "shop-example-com"
    deleter.domain = "shop.example.com"
    deleter._discover_compose_file()
    deleter.delete()

    delete_calls = [c for c in runner.calls if c[:2] == ("docker", "compose")]
    assert delete_calls, "expected the down invocation during delete"
    for call in delete_calls:
        assert call[2:4] == ("-p", "shop-example-com")


# ---------------------------------------------------------------------------
# A stack that names its own project keeps that name
# ---------------------------------------------------------------------------


def test_a_compose_file_that_names_its_project_is_not_overridden(tmp_path: Path) -> None:
    """
    A top-level name: is what 1.x and the unit use; -p would override it.

    Pinned to the directory instead, the next ``up`` would start the stack on
    new, empty volumes.
    """
    app_path = tmp_path / "shop-example-com"
    app_path.mkdir()
    compose_path = app_path / "docker-compose.yml"
    compose_path.write_text("name: myshop\nservices:\n  db:\n    image: postgres\n")

    assert compose_project_name(app_path, compose_path) is None


def test_compose_project_name_in_the_project_env_is_not_overridden(tmp_path: Path) -> None:
    app_path = tmp_path / "shop-example-com"
    app_path.mkdir()
    compose_path = app_path / "docker-compose.yml"
    compose_path.write_text("services:\n  db:\n    image: postgres\n")
    (app_path / ".env").write_text("# settings\nexport COMPOSE_PROJECT_NAME=legacy_shop\n")

    assert compose_project_name(app_path, compose_path) is None


def test_argv_carries_no_project_flag_for_a_stack_that_names_itself(tmp_path: Path) -> None:
    app_path = tmp_path / "shop-example-com"
    app_path.mkdir()
    (app_path / "docker-compose.yml").write_text("name: myshop\nservices: {}\n")
    deployer = DockerComposeDeployer(runner=FakeRunner())
    deployer.app_path = app_path
    deployer.compose_path = app_path / "docker-compose.yml"

    argv = deployer._compose("up", "-d")

    assert "-p" not in argv
    assert argv[:4] == ["docker", "compose", "-f", str(app_path / "docker-compose.yml")]


def test_down_finds_the_compose_file_and_keeps_volumes(tmp_path: Path) -> None:
    """Deletion takes the stack down through the same argv as every other command."""
    app_path = tmp_path / "shop-example-com"
    app_path.mkdir()
    (app_path / "docker-compose.prod.yml").write_text("services: {}\n")
    runner = FakeRunner()
    deployer = DockerComposeDeployer(runner=runner)
    deployer.app_path = app_path

    assert deployer.down() is True
    assert deployer.down(remove_volumes=True) is True

    compose = str(app_path / "docker-compose.prod.yml")
    assert runner.calls == [
        ("docker", "compose", "-p", "shop-example-com", "-f", compose, "down", "--remove-orphans"),
        (
            "docker",
            "compose",
            "-p",
            "shop-example-com",
            "-f",
            compose,
            "down",
            "--volumes",
            "--remove-orphans",
        ),
    ]
