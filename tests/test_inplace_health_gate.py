# Copyright (c) 2024-2026 Yago Lopez Prado
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Docker Compose stacks and monorepos pass the health gate, and go back when they fail it.

Neither can build releases, so 2.0 updated both in place and reported a
failed health check instead of rolling it back. An update now records what
serves before it builds - the commit of the tree, and for a stack the image
of every running container - and puts exactly that back when the new build
does not answer: the tree is checked out at the previous commit, a stack's
containers are recreated from the images they ran, a monorepo is rebuilt from
its previous commit, and the error carries the probes and the processes' own
output verbatim.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from wasm.core.exceptions import DeploymentError, DockerError, WASMError
from wasm.core.runner import CommandResult, FakeRunner
from wasm.core.store import App, Service, WASMStore
from wasm.deployers import docker_compose, lifecycle, monorepo
from wasm.deployers.docker_compose import (
    PREVIOUS_TAG,
    DockerComposeDeployer,
    parse_compose_ps,
)
from wasm.deployers.interface import UpdateResult
from wasm.deployers.monorepo import MonorepoDeployer

DOMAIN = "stack.example.com"
PREVIOUS = "1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b"

COMPOSE_FILE = """\
services:
  web:
    build: .
    ports:
      - "8080:80"
  db:
    image: postgres:16
    volumes:
      - data:/var/lib/postgresql/data
volumes:
  data: {}
"""

#: What ``docker inspect`` answers for the two running containers.
INSPECTED = "sha256:0ld0web|stack-web|web|stack\nsha256:0ld0db|postgres:16|db|stack\n"

#: Every argument that would reach a volume, directly or by taking the stack down.
VOLUME_ARGUMENTS = {"down", "-v", "--volumes", "volume", "rm", "-V", "--renew-anon-volumes"}


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[WASMStore]:
    """A store in the test directory, where the lifecycle and both deployers look."""
    WASMStore.reset_instance()
    instance = WASMStore(tmp_path / "wasm.db")
    monkeypatch.setattr(lifecycle, "get_store", lambda: instance)
    monkeypatch.setattr(monorepo, "get_store", lambda: instance)
    monkeypatch.setattr(docker_compose, "get_store", lambda: instance)
    yield instance
    WASMStore.reset_instance()


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch: pytest.MonkeyPatch) -> None:
    """Nothing here needs real time to pass."""
    monkeypatch.setattr(docker_compose.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(monorepo.time, "sleep", lambda seconds: None)


class Probe:
    """
    The HTTP probe, answering from a script instead of the network.

    Attributes:
        urls: Every URL asked, in order.
        failing: URLs that do not answer; a test empties it to "fix" them.
    """

    def __init__(self) -> None:
        self.urls: list[str] = []
        self.failing: set[str] = set()
        self.fail_rounds = 1
        self._seen: dict[str, int] = {}

    def __call__(
        self,
        url: str,
        *,
        retries: int = 5,
        delay: float = 2.0,
        on_attempt: Callable[[str], None] | None = None,
        accept: Callable[[int], bool] | None = None,
        within: float | None = None,
    ) -> bool:
        self.urls.append(url)
        self._seen[url] = self._seen.get(url, 0) + 1
        if url in self.failing and self._seen[url] <= self.fail_rounds:
            if on_attempt is not None:
                for attempt in range(1, retries + 1):
                    on_attempt(f"Health check attempt {attempt} failed: HTTP 502")
            return False
        return True


class Stack(FakeRunner):
    """
    Docker and git as a stack being updated sees them.

    Attributes:
        build_fails: ``docker compose build`` fails.
        up_fails: The ``up`` that recreates from the new images fails.
        previous_up_fails: The ``up`` that recreates from the previous images fails.
        tag_fails: Restoring an image's tag fails.
        states: Container states answered by ``ps -a``, one list per call;
            the last one repeats.
    """

    def __init__(self) -> None:
        super().__init__()
        self.build_fails = False
        self.up_fails = False
        self.previous_up_fails = False
        self.tag_fails = False
        self.states: list[list[dict[str, Any]]] = [
            [
                {"Service": "web", "Name": "stack-web-1", "State": "running", "Health": ""},
                {"Service": "db", "Name": "stack-db-1", "State": "running", "Health": ""},
            ]
        ]

    def _lookup(self, argv: Any, user: str | None = None, env: Any = None) -> CommandResult:
        result = super()._lookup(argv, user, env)
        args = result.argv

        def answer(stdout: str = "", stderr: str = "", code: int = 0) -> CommandResult:
            return replace(result, stdout=stdout, stderr=stderr, exit_code=code)

        if args[:2] == ("docker", "inspect"):
            return answer(INSPECTED)
        if args[:3] == ("docker", "image", "tag"):
            restoring = not args[4].endswith(f":{PREVIOUS_TAG}")
            if restoring and self.tag_fails:
                return answer(
                    stderr=f"Error response from daemon: No such image: {args[3]}", code=1
                )
            return answer()
        if args[:2] == ("docker", "compose"):
            if "ps" in args and "-q" in args:
                return answer("c0ffee01c0ffee01\nc0ffee02c0ffee02\n")
            if "ps" in args:
                states = self.states[0] if len(self.states) == 1 else self.states.pop(0)
                return answer("\n".join(json.dumps(state) for state in states))
            if "build" in args and self.build_fails:
                return answer(stderr="failed to solve: npm ci exited with 1", code=1)
            if "logs" in args:
                return answer("web-1  | Error: Cannot find module 'express'\n")
            if "up" in args and "--no-build" in args and self.previous_up_fails:
                return answer(
                    stderr="Bind for 0.0.0.0:8080 failed: port is already allocated", code=1
                )
            if "up" in args and "--no-build" not in args and self.up_fails:
                return answer(stderr="container stack-web-1 exited (1)", code=1)
        if args[0] == "git" and "--verify" in args:
            return answer(PREVIOUS + "\n")
        if args[0] == "journalctl":
            return answer("docker compose up -d: done\n")
        return result

    def argv_with(self, *words: str) -> list[tuple[str, ...]]:
        """Every call containing all the given words."""
        return [call for call in self.calls if all(word in call for word in words)]

    def index_of(self, *words: str) -> int:
        """Position of the first call containing all the given words."""
        return next(i for i, call in enumerate(self.calls) if all(word in call for word in words))


@pytest.fixture
def stack_runner() -> Iterator[Stack]:
    """The stack runner, installed as the process-wide one too (the gate's journal)."""
    from wasm.core.runner import set_runner

    fake = Stack()
    set_runner(fake)
    yield fake
    set_runner(None)


@pytest.fixture
def probe(monkeypatch: pytest.MonkeyPatch) -> Probe:
    """The HTTP probe every gate here uses."""
    fake = Probe()
    monkeypatch.setattr(docker_compose, "wait_until_healthy", fake)
    monkeypatch.setattr(monorepo, "wait_until_healthy", fake)
    return fake


def compose_app(store: WASMStore, root: Path, *, port: int = 8080) -> App:
    """A deployed stack at ``root``: its tree, a checkout, and its row."""
    root.mkdir(parents=True, exist_ok=True)
    (root / ".git").mkdir(exist_ok=True)
    (root / "docker-compose.yml").write_text(COMPOSE_FILE)
    return store.create_app(
        App(
            domain=DOMAIN,
            app_type="docker-compose",
            app_path=str(root),
            port=port,
            status="running",
        )
    )


def compose_deployer(
    root: Path, runner: FakeRunner, previous: str | None = PREVIOUS
) -> DockerComposeDeployer:
    """The deployer lifecycle._rebuild_compose builds, configured the same way."""
    deployer = DockerComposeDeployer(runner=runner)
    deployer.app_path = root
    deployer.app_name = root.name
    deployer.domain = DOMAIN
    deployer.previous_commit = previous
    return deployer


# ---------------------------------------------------------------------------
# Docker Compose
# ---------------------------------------------------------------------------


def test_a_stack_that_answers_keeps_its_new_containers(
    tmp_path: Path, store: WASMStore, stack_runner: Stack, probe: Probe
) -> None:
    """What serves is recorded before the build, and nothing is put back."""
    root = tmp_path / "stack"
    compose_app(store, root)

    result = compose_deployer(root, stack_runner).update()

    assert result.is_static is True
    assert probe.urls == ["http://127.0.0.1:8080/"]
    ps = stack_runner.index_of("ps", "-q")
    keep = stack_runner.index_of("docker", "image", "tag", "sha256:0ld0web")
    build = stack_runner.index_of("compose", "build")
    up = stack_runner.index_of("up", "-d", "--remove-orphans")
    assert ps < keep < build < up
    assert stack_runner.argv_with("docker", "image", "tag", "sha256:0ld0web")[0][-1] == (
        f"stack-web:{PREVIOUS_TAG}"
    )
    assert not stack_runner.argv_with("checkout")
    assert not stack_runner.argv_with("--no-build")
    row = store.list_deployments(DOMAIN)[0]
    assert row.status == "success"


def test_the_gate_asks_the_path_and_statuses_the_application_set(
    tmp_path: Path, store: WASMStore, stack_runner: Stack, probe: Probe
) -> None:
    """The stack is judged by its own health check, not by a hardcoded '/'."""
    root = tmp_path / "stack"
    compose_app(store, root)
    store.set_app_health(DOMAIN, path="/healthz", expect="200-299", timeout=10)

    compose_deployer(root, stack_runner).update()

    assert probe.urls == ["http://127.0.0.1:8080/healthz"]


def test_a_stack_that_does_not_answer_goes_back_to_what_served(
    tmp_path: Path, store: WASMStore, stack_runner: Stack, probe: Probe
) -> None:
    """The previous commit and the previous images, with the evidence verbatim."""
    root = tmp_path / "stack"
    compose_app(store, root)
    probe.failing.add("http://127.0.0.1:8080/")

    with pytest.raises(DeploymentError) as caught:
        compose_deployer(root, stack_runner).update()

    error = caught.value
    assert "did not pass its health check" in error.message
    assert "running again" in error.message
    details = error.details or ""
    assert "Health check attempts 1-15 failed: HTTP 502" in details
    # The containers' own output, read before they were replaced.
    assert "Error: Cannot find module 'express'" in details
    logs = stack_runner.index_of("logs", "--tail", "40")
    checkout = stack_runner.index_of("checkout", "--force", "--detach", PREVIOUS)
    retag = stack_runner.index_of("docker", "image", "tag", "sha256:0ld0web", "stack-web")
    previous_up = stack_runner.index_of("up", "-d", "--no-build", "--remove-orphans")
    assert logs < checkout < previous_up
    assert retag < previous_up
    assert stack_runner.argv_with("docker", "image", "tag", "sha256:0ld0db", "postgres:16")
    # Recreated as the project the containers belonged to, whatever the file now says.
    assert stack_runner.calls[previous_up][:4] == ("docker", "compose", "-p", "stack")
    app = store.get_app(DOMAIN)
    assert app is not None and app.status == "running"
    row = store.list_deployments(DOMAIN)[0]
    assert row.status == "failed"
    assert "HTTP 502" in (row.error or "")


def test_going_back_never_touches_a_volume(
    tmp_path: Path, store: WASMStore, stack_runner: Stack, probe: Probe
) -> None:
    """The stack's databases live in its volumes: no down, no -v, no volume command."""
    root = tmp_path / "stack"
    compose_app(store, root)
    probe.failing.add("http://127.0.0.1:8080/")

    with pytest.raises(DeploymentError):
        compose_deployer(root, stack_runner).update()

    docker_calls = stack_runner.calls_to("docker")
    assert docker_calls
    for call in docker_calls:
        assert not VOLUME_ARGUMENTS & set(call), call


def test_a_failure_while_going_back_is_reported_not_swallowed(
    tmp_path: Path, store: WASMStore, stack_runner: Stack, probe: Probe
) -> None:
    """Every step of the way back is attempted, and each one that failed is named."""
    root = tmp_path / "stack"
    compose_app(store, root)
    probe.failing.add("http://127.0.0.1:8080/")
    probe.fail_rounds = 2
    stack_runner.tag_fails = True
    stack_runner.previous_up_fails = True

    with pytest.raises(DeploymentError) as caught:
        compose_deployer(root, stack_runner).update()

    error = caught.value
    assert "could not be put back" in error.message
    details = error.details or ""
    assert "No such image: sha256:0ld0web" in details
    assert "port is already allocated" in details
    # The tree still went back even though the images did not.
    assert stack_runner.argv_with("checkout", "--force", "--detach", PREVIOUS)
    app = store.get_app(DOMAIN)
    assert app is not None and app.status == "failed"


def test_a_stack_put_back_that_does_not_answer_either_says_so(
    tmp_path: Path, store: WASMStore, stack_runner: Stack, probe: Probe
) -> None:
    """Restored is not the same as serving."""
    root = tmp_path / "stack"
    compose_app(store, root)
    probe.failing.add("http://127.0.0.1:8080/")
    probe.fail_rounds = 2

    with pytest.raises(DeploymentError) as caught:
        compose_deployer(root, stack_runner).update()

    assert "not answering either" in caught.value.message
    app = store.get_app(DOMAIN)
    assert app is not None and app.status == "failed"


def test_a_recreate_that_fails_goes_back_with_dockers_own_words(
    tmp_path: Path, store: WASMStore, stack_runner: Stack, probe: Probe
) -> None:
    """up -d failing is a failed gate like any other."""
    root = tmp_path / "stack"
    compose_app(store, root)
    stack_runner.up_fails = True

    with pytest.raises(DeploymentError) as caught:
        compose_deployer(root, stack_runner).update()

    assert "container stack-web-1 exited (1)" in (caught.value.details or "")
    assert stack_runner.argv_with("up", "-d", "--no-build")
    assert probe.urls == ["http://127.0.0.1:8080/"]


def test_a_failed_build_puts_the_tree_and_the_tags_back_and_recreates_nothing(
    tmp_path: Path, store: WASMStore, stack_runner: Stack, probe: Probe
) -> None:
    """The containers were never touched; the next start must not run a half-built stack."""
    root = tmp_path / "stack"
    compose_app(store, root)
    stack_runner.build_fails = True

    with pytest.raises(DockerError) as caught:
        compose_deployer(root, stack_runner).update()

    assert "npm ci exited with 1" in (caught.value.details or "")
    assert stack_runner.argv_with("checkout", "--force", "--detach", PREVIOUS)
    assert stack_runner.argv_with("docker", "image", "tag", "sha256:0ld0web", "stack-web")
    assert not stack_runner.argv_with("up")
    assert probe.urls == []


def test_a_headless_stack_is_judged_by_its_containers(
    tmp_path: Path, store: WASMStore, stack_runner: Stack, probe: Probe
) -> None:
    """A worker has no port to ask; a container that keeps restarting is the answer."""
    root = tmp_path / "stack"
    compose_app(store, root, port=0)
    crashing = [
        {"Service": "worker", "Name": "stack-worker-1", "State": "restarting", "ExitCode": 1},
    ]
    healthy = [{"Service": "worker", "Name": "stack-worker-1", "State": "running"}]
    stack_runner.states = [crashing] * docker_compose.CONTAINER_CHECK_ATTEMPTS + [healthy]

    with pytest.raises(DeploymentError) as caught:
        compose_deployer(root, stack_runner).update()

    assert probe.urls == []
    assert "stack-worker-1" in (caught.value.details or "")
    assert "restarting" in (caught.value.details or "")
    assert "running again" in caught.value.message


def test_a_one_shot_container_that_exited_cleanly_is_not_a_failure(
    tmp_path: Path, store: WASMStore, stack_runner: Stack, probe: Probe
) -> None:
    """A migration container that ran and exited 0 did its job."""
    root = tmp_path / "stack"
    compose_app(store, root)
    stack_runner.states = [
        [
            {"Service": "web", "Name": "stack-web-1", "State": "running"},
            {"Service": "migrate", "Name": "stack-migrate-1", "State": "exited", "ExitCode": 0},
        ]
    ]

    compose_deployer(root, stack_runner).update()

    assert not stack_runner.argv_with("checkout")


def test_without_a_previous_commit_the_compose_file_cannot_go_back(
    tmp_path: Path, store: WASMStore, stack_runner: Stack, probe: Probe
) -> None:
    """A tree that is not a checkout gets its images back, and is told the rest."""
    root = tmp_path / "stack"
    compose_app(store, root)
    probe.failing.add("http://127.0.0.1:8080/")

    with pytest.raises(DeploymentError) as caught:
        compose_deployer(root, stack_runner, previous=None).update()

    assert not stack_runner.argv_with("checkout")
    assert stack_runner.argv_with("docker", "image", "tag", "sha256:0ld0web", "stack-web")
    assert "compose file" in (caught.value.details or "")


@pytest.mark.parametrize(
    "stdout",
    [
        # Compose before 2.21: one JSON array.
        '[{"Service": "web", "State": "running"}, {"Service": "db", "State": "exited"}]',
        # 2.21 and later: one object per line.
        '{"Service": "web", "State": "running"}\n{"Service": "db", "State": "exited"}\n',
    ],
)
def test_ps_output_is_read_in_every_compose_v2_format(stdout: str) -> None:
    """The format changed under the same flag; both are the same containers."""
    assert [c["Service"] for c in parse_compose_ps(stdout)] == ["web", "db"]


def test_rebuild_compose_passes_the_commit_that_served(
    tmp_path: Path, store: WASMStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The commit is read before the pull moves it, and reaches the deployer."""
    root = tmp_path / "stack"
    compose_app(store, root)
    seen: dict[str, Any] = {}
    calls: list[str] = []

    class Deployer:
        def __init__(self, verbose: bool = False) -> None:
            self.logger = None

        def update(self, on_step: Any = None) -> UpdateResult:
            seen["previous_commit"] = getattr(self, "previous_commit", "unset")
            return UpdateResult("docker compose", False, True, "")

    monkeypatch.setattr(lifecycle, "DockerComposeDeployer", Deployer)
    monkeypatch.setattr(
        lifecycle,
        "RollbackManager",
        lambda verbose=False: SimpleNamespace(create_pre_deploy_backup=lambda **kw: None),
    )

    def repo_info(path: Path) -> dict[str, Any]:
        calls.append("repo_info")
        return {"commit": "abc1234"}

    monkeypatch.setattr(
        lifecycle,
        "SourceManager",
        lambda verbose=False: SimpleNamespace(
            get_repo_info=repo_info, pull=lambda path, branch=None: calls.append("pull")
        ),
    )
    monkeypatch.setattr(
        lifecycle,
        "ServiceManager",
        lambda verbose=False: SimpleNamespace(get_service_config=lambda name: None),
    )

    lifecycle.update_app(DOMAIN)

    assert calls == ["repo_info", "pull"]
    assert seen["previous_commit"] == "abc1234"


# ---------------------------------------------------------------------------
# Monorepo
# ---------------------------------------------------------------------------


class Units:
    """The service manager, as the workspaces' units answer it."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.inactive: set[str] = set()

    def restart(self, name: str) -> None:
        self.calls.append(("restart", name))

    def logs(self, name: str, lines: int = 50) -> str:
        return f"{name}: Error: listen EADDRINUSE"

    def get_status(self, name: str) -> dict[str, Any]:
        return {"exists": True, "active": name not in self.inactive}


class Workspaces(FakeRunner):
    """pnpm and git for a monorepo; the build fails from a given call on."""

    def __init__(self) -> None:
        super().__init__()
        self.builds = 0
        self.fail_build_from: int | None = None

    def _lookup(self, argv: Any, user: str | None = None, env: Any = None) -> CommandResult:
        result = super()._lookup(argv, user, env)
        args = result.argv
        if args[:2] == ("pnpm", "build"):
            self.builds += 1
            if self.fail_build_from is not None and self.builds >= self.fail_build_from:
                return replace(result, exit_code=1, stderr="Type error: 'x' is not assignable")
        if args[0] == "git" and "--verify" in args:
            return replace(result, stdout=PREVIOUS + "\n")
        return result


MONO = "mono.example.com"


def mono_app(store: WASMStore, root: Path, *, git: bool = True) -> App:
    """A deployed monorepo with two workspaces, each its own unit on its own port."""
    (root / "apps" / "web").mkdir(parents=True)
    (root / "apps" / "web" / "package.json").write_text('{"name": "web"}')
    (root / "apps" / "api").mkdir(parents=True)
    (root / "apps" / "api" / "package.json").write_text('{"name": "api"}')
    (root / "package.json").write_text(
        json.dumps(
            {
                "name": "mono",
                "workspaces": ["apps/*"],
                "scripts": {"db:generate": "prisma generate", "db:migrate": "prisma migrate"},
            }
        )
    )
    (root / "pnpm-workspace.yaml").write_text("packages:\n  - apps/*\n")
    if git:
        (root / ".git").mkdir()
    app = store.create_app(App(domain=MONO, app_type="monorepo", app_path=str(root), port=3001))
    for name, port in (("mono-example-com-web", 3001), ("mono-example-com-api", 3002)):
        store.create_service(
            Service(name=name, app_id=app.id, unit_file=f"/x/{name}.service", port=port)
        )
    return app


def mono_deployer(
    root: Path, runner: FakeRunner, units: Units, previous: str | None = PREVIOUS
) -> MonorepoDeployer:
    """The deployer lifecycle._rebuild_monorepo builds."""
    deployer = MonorepoDeployer(runner=runner)
    deployer.configure(MONO, str(root), app_path=root)
    deployer.service_manager = units  # type: ignore[assignment]
    deployer.previous_commit = previous
    return deployer


@pytest.fixture
def workspaces() -> Iterator[Workspaces]:
    """The workspace runner, installed as the process-wide one too."""
    from wasm.core.runner import set_runner

    fake = Workspaces()
    set_runner(fake)
    yield fake
    set_runner(None)


def test_every_workspace_is_probed_on_its_own_port_after_every_restart(
    tmp_path: Path, store: WASMStore, workspaces: Workspaces, probe: Probe
) -> None:
    """One workspace down is an application down, however many siblings answer."""
    root = tmp_path / "mono"
    mono_app(store, root)
    units = Units()

    result = mono_deployer(root, workspaces, units).update()

    assert set(probe.urls) == {"http://127.0.0.1:3001/", "http://127.0.0.1:3002/"}
    # Every unit is restarted before any is probed: the shortest mix of old and new.
    assert units.calls == [
        ("restart", "mono-example-com-api"),
        ("restart", "mono-example-com-web"),
    ]
    assert result.restarted == ("mono-example-com-api", "mono-example-com-web")
    assert not [c for c in workspaces.calls if "checkout" in c]


def test_a_workspace_that_does_not_answer_puts_the_previous_commit_back(
    tmp_path: Path, store: WASMStore, workspaces: Workspaces, probe: Probe
) -> None:
    """Checked out, rebuilt, restarted, probed again; the evidence names the unit."""
    root = tmp_path / "mono"
    mono_app(store, root)
    units = Units()
    probe.failing.add("http://127.0.0.1:3002/")

    with pytest.raises(DeploymentError) as caught:
        mono_deployer(root, workspaces, units).update()

    error = caught.value
    assert f"commit {PREVIOUS[:7]} is serving again" in error.message
    details = error.details or ""
    assert "Health check attempts 1-15 failed: HTTP 502" in details
    assert "mono-example-com-api: Error: listen EADDRINUSE" in details
    checkout = next(i for i, c in enumerate(workspaces.calls) if "checkout" in c)
    assert PREVIOUS in workspaces.calls[checkout]
    rebuilt = [i for i, c in enumerate(workspaces.calls) if c[:2] == ("pnpm", "build")]
    assert len(rebuilt) == 2 and rebuilt[1] > checkout
    assert [c for c in units.calls if c[0] == "restart"] == [
        ("restart", "mono-example-com-api"),
        ("restart", "mono-example-com-web"),
    ] * 2
    # A database migration cannot be undone, and the old tree's is a no-op at best.
    assert len([c for c in workspaces.calls if c[:2] == ("pnpm", "db:migrate")]) == 1
    assert len([c for c in workspaces.calls if c[:2] == ("pnpm", "db:generate")]) == 2
    assert store.list_deployments(MONO)[0].status == "failed"


def test_a_previous_commit_that_does_not_rebuild_is_reported(
    tmp_path: Path, store: WASMStore, workspaces: Workspaces, probe: Probe
) -> None:
    """Nothing is restarted on a half-built tree; the build's own words are in the error."""
    root = tmp_path / "mono"
    mono_app(store, root)
    units = Units()
    probe.failing.add("http://127.0.0.1:3001/")
    workspaces.fail_build_from = 2

    with pytest.raises(DeploymentError) as caught:
        mono_deployer(root, workspaces, units).update()

    assert "could not be put back" in caught.value.message
    assert "Type error: 'x' is not assignable" in (caught.value.details or "")
    assert len([c for c in units.calls if c[0] == "restart"]) == 2


def test_a_monorepo_that_is_not_a_checkout_has_nothing_to_go_back_to(
    tmp_path: Path, store: WASMStore, workspaces: Workspaces, probe: Probe
) -> None:
    """Said as such, with the backup that is the way back."""
    root = tmp_path / "mono"
    mono_app(store, root, git=False)
    units = Units()
    probe.failing.add("http://127.0.0.1:3001/")

    with pytest.raises(DeploymentError) as caught:
        mono_deployer(root, workspaces, units, previous=None).update()

    assert not [c for c in workspaces.calls if "checkout" in c]
    assert f"wasm rollback {MONO}" in (caught.value.details or "")


def test_a_unit_without_a_port_is_judged_by_systemd(
    tmp_path: Path, store: WASMStore, workspaces: Workspaces, probe: Probe
) -> None:
    """A worker workspace has nothing to ask over HTTP; it has to be running."""
    root = tmp_path / "mono"
    app = mono_app(store, root)
    store.create_service(
        Service(name="mono-example-com-worker", app_id=app.id, unit_file="/x/w.service")
    )
    units = Units()
    units.inactive.add("mono-example-com-worker")

    with pytest.raises(DeploymentError) as caught:
        mono_deployer(root, workspaces, units).update()

    assert "mono-example-com-worker" in (caught.value.details or "")


def test_update_app_does_not_restart_what_the_monorepo_already_gated(
    tmp_path: Path, store: WASMStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The deployer restarted and probed every unit; a second restart is downtime for nothing."""
    root = tmp_path / "mono"
    mono_app(store, root)
    restarts: list[str] = []
    seen: dict[str, Any] = {}

    class Deployer:
        def __init__(self, verbose: bool = False) -> None:
            pass

        def update(self, on_step: Any = None) -> UpdateResult:
            seen["previous_commit"] = getattr(self, "previous_commit", "unset")
            return UpdateResult(
                "pnpm", False, False, "", restarted=("mono-example-com-web", "mono-example-com-api")
            )

    monkeypatch.setattr(lifecycle, "MonorepoDeployer", Deployer)
    monkeypatch.setattr(
        lifecycle,
        "RollbackManager",
        lambda verbose=False: SimpleNamespace(create_pre_deploy_backup=lambda **kw: None),
    )
    monkeypatch.setattr(
        lifecycle,
        "SourceManager",
        lambda verbose=False: SimpleNamespace(
            get_repo_info=lambda path: {"commit": "abc1234"}, pull=lambda path, branch=None: True
        ),
    )
    monkeypatch.setattr(
        lifecycle,
        "ServiceManager",
        lambda verbose=False: SimpleNamespace(restart=restarts.append),
    )

    outcome = lifecycle.update_app(MONO)

    assert restarts == []
    assert outcome.restarted == ("mono-example-com-web", "mono-example-com-api")
    assert outcome.active is True
    assert seen["previous_commit"] == "abc1234"


def test_other_types_do_not_pay_for_reading_the_commit(
    tmp_path: Path, store: WASMStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only the types that go back by commit need to know it."""
    root = tmp_path / "node"
    (root / ".git").mkdir(parents=True)
    store.create_app(App(domain="node.example.com", app_type="nodejs", app_path=str(root)))

    def refuse(path: Path) -> dict[str, Any]:
        raise WASMError("the commit was read for a type that does not need it")

    monkeypatch.setattr(
        lifecycle,
        "SourceManager",
        lambda verbose=False: SimpleNamespace(
            get_repo_info=refuse, pull=lambda path, branch=None: True
        ),
    )
    monkeypatch.setattr(
        lifecycle,
        "RollbackManager",
        lambda verbose=False: SimpleNamespace(create_pre_deploy_backup=lambda **kw: None),
    )

    class Deployer:
        def configure(self, **kwargs: Any) -> None:
            pass

        def update(self, on_step: Any = None) -> UpdateResult:
            return UpdateResult("npm", False, True, "")

    monkeypatch.setattr(lifecycle, "get_deployer", lambda app_type, verbose=False: Deployer())

    lifecycle.update_app("node.example.com")
