# Copyright (c) 2024-2026 Yago Lopez Prado
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
The compose file a Docker Compose deployment names stays inside the application.

``compose_file`` arrives from the CLI (``--compose-file``) and from the API's
deploy body, and both reach :meth:`DockerComposeDeployer.configure`. It used
to be joined to the application directory unchecked, so ``/etc/shadow``,
``../../other-app/docker-compose.yml`` or a symlink committed to the repository
could hand ``docker compose -f`` - run as root - a file from anywhere on the
host. It is held to the rule persistent paths already follow: relative, no
``..``, resolved inside the application directory, and present.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from wasm.core.exceptions import DeploymentError
from wasm.core.runner import FakeRunner
from wasm.deployers.docker_compose import DockerComposeDeployer, compose_project_name

DOMAIN = "stack.example.com"
COMPOSE = "services:\n  web:\n    image: nginx\n"


def configured(app_path: Path, runner: FakeRunner, **options: str) -> DockerComposeDeployer:
    """A compose deployer configured over ``app_path`` with the given options."""
    deployer = DockerComposeDeployer(runner=runner)
    deployer.configure(DOMAIN, str(app_path), app_path=app_path, **options)
    return deployer


@pytest.mark.parametrize(
    "compose_file",
    [
        "/etc/passwd",
        "../x.yml",
        "sub/../../x.yml",
        "sub/../x.yml",
        "..",
        ".",
        "docker/\x00compose.yml",
    ],
)
def test_configure_refuses_a_compose_file_outside_the_application(
    tmp_path: Path, runner: FakeRunner, compose_file: str
) -> None:
    """Refused before anything is fetched, so the CLI and the API both get it."""
    deployer = DockerComposeDeployer(runner=runner)

    with pytest.raises(DeploymentError, match=r"(?i)compose file") as caught:
        deployer.configure(DOMAIN, "src", app_path=tmp_path, compose_file=compose_file)

    assert caught.value.details
    assert runner.calls == []


def test_a_symlink_in_the_application_pointing_outside_is_refused(
    tmp_path: Path, runner: FakeRunner
) -> None:
    """A repository is untrusted input: a committed link is not a way out."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "compose.yml").write_text(COMPOSE)
    app_path = tmp_path / "app"
    app_path.mkdir()
    (app_path / "docker").symlink_to(outside)
    deployer = configured(app_path, runner, compose_file="docker/compose.yml")

    with pytest.raises(DeploymentError, match=r"(?i)outside"):
        deployer._discover_compose_file()

    assert deployer.compose_path is None


def test_a_linked_compose_file_pointing_outside_is_refused(
    tmp_path: Path, runner: FakeRunner
) -> None:
    """The file itself, not only a directory on the way, may be the link."""
    (tmp_path / "shadow").write_text(COMPOSE)
    app_path = tmp_path / "app"
    app_path.mkdir()
    (app_path / "compose.prod.yml").symlink_to(tmp_path / "shadow")
    deployer = configured(app_path, runner, compose_file="compose.prod.yml")

    with pytest.raises(DeploymentError, match=r"(?i)outside"):
        deployer._discover_compose_file()


def test_a_discovered_compose_file_pointing_outside_is_refused(
    tmp_path: Path, runner: FakeRunner
) -> None:
    """Discovery by the usual names walks into the same repository."""
    (tmp_path / "shadow").write_text(COMPOSE)
    app_path = tmp_path / "app"
    app_path.mkdir()
    (app_path / "docker-compose.yml").symlink_to(tmp_path / "shadow")
    deployer = configured(app_path, runner)

    with pytest.raises(DeploymentError, match=r"(?i)outside"):
        deployer._discover_compose_file()


def test_a_missing_compose_file_is_refused(tmp_path: Path, runner: FakeRunner) -> None:
    """Named but absent: the deploy stops with what to do about it."""
    deployer = configured(tmp_path, runner, compose_file="docker/compose.prod.yml")

    with pytest.raises(DeploymentError, match=r"(?i)not found") as caught:
        deployer._discover_compose_file()

    assert caught.value.details


def test_a_directory_named_as_the_compose_file_is_refused(
    tmp_path: Path, runner: FakeRunner
) -> None:
    """``docker compose -f`` needs a file; a directory would fail later, less clearly."""
    (tmp_path / "docker").mkdir()
    deployer = configured(tmp_path, runner, compose_file="docker")

    with pytest.raises(DeploymentError, match=r"(?i)not found"):
        deployer._discover_compose_file()


def test_a_relative_compose_file_inside_the_application_is_used(
    tmp_path: Path, runner: FakeRunner
) -> None:
    """The ordinary case: a file in a subdirectory reaches ``docker compose -f``."""
    (tmp_path / "docker").mkdir()
    (tmp_path / "docker" / "compose.prod.yml").write_text(COMPOSE)
    # A default name at the root must not win over the one asked for.
    (tmp_path / "docker-compose.yml").write_text(COMPOSE)
    deployer = configured(tmp_path, runner, compose_file="./docker/compose.prod.yml")

    deployer._discover_compose_file()
    deployer._build_images()

    expected = tmp_path / "docker" / "compose.prod.yml"
    assert deployer.compose_path == expected
    # Preserved, not repointed at app_path: see test_compose_project_name.py
    # for why a subdirectory keeps naming the project after itself.
    assert runner.ran(
        "docker",
        "compose",
        "-p",
        compose_project_name(tmp_path, expected),
        "-f",
        str(expected),
        "build",
    )


def test_a_link_that_stays_inside_the_application_is_accepted(
    tmp_path: Path, runner: FakeRunner
) -> None:
    """Only a way out is refused; a link between two files of the project is fine."""
    (tmp_path / "docker").mkdir()
    (tmp_path / "docker" / "compose.prod.yml").write_text(COMPOSE)
    (tmp_path / "compose.yml").symlink_to(tmp_path / "docker" / "compose.prod.yml")
    deployer = configured(tmp_path, runner, compose_file="compose.yml")

    deployer._discover_compose_file()

    assert deployer.compose_path == tmp_path / "compose.yml"


# ---------------------------------------------------------------------------
# An update uses the compose file the deploy chose
# ---------------------------------------------------------------------------


def rendered_unit(compose_file: str | None) -> str:
    """The docker-compose unit exactly as ServiceManager renders it."""
    from wasm.managers.service_manager import ServiceManager

    env = ServiceManager().jinja_env
    assert env is not None
    return env.get_template("docker-compose.service.j2").render(
        name="stack-example-com",
        description="Docker Compose app: stack.example.com",
        working_directory="/var/www/apps/stack-example-com",
        environment={},
        compose_file=compose_file,
    )


@pytest.mark.parametrize(
    "compose_file",
    ["docker/compose.prod.yml", 'odd "name" 100%.yml', "back\\slash.yml"],
)
def test_the_compose_file_is_read_back_from_the_unit_the_deploy_wrote(compose_file: str) -> None:
    """Whatever the template escaped, reading it back gives the same relative path."""
    from wasm.deployers.docker_compose import compose_file_from_unit

    assert compose_file_from_unit(rendered_unit(compose_file)) == compose_file


def test_a_unit_without_a_compose_file_names_none() -> None:
    from wasm.deployers.docker_compose import compose_file_from_unit

    assert compose_file_from_unit(rendered_unit(None)) is None
    assert compose_file_from_unit(None) is None
    assert compose_file_from_unit("") is None


def test_an_update_rebuilds_with_the_compose_file_the_deploy_chose(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without this an update rediscovered only the root-level default names."""
    from wasm.deployers import lifecycle
    from wasm.deployers.interface import UpdateResult
    from wasm.managers.service_manager import ServiceManager

    seen: list[str | None] = []

    def update(self: DockerComposeDeployer, on_step: object = None) -> UpdateResult:
        seen.append(self.compose_file)
        return UpdateResult(
            package_manager="", prisma_updated=False, is_static=False, start_command=""
        )

    monkeypatch.setattr(
        ServiceManager,
        "get_service_config",
        lambda self, name: rendered_unit("docker/compose.prod.yml"),
    )
    monkeypatch.setattr(DockerComposeDeployer, "update", update)

    lifecycle._rebuild_compose(DOMAIN, tmp_path, "stack-example-com", None, False, "manual")

    assert seen == ["docker/compose.prod.yml"]
