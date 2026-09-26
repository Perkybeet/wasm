#!/usr/bin/env python3
"""
Real-machine integration harness for WASM.

Builds the wheel from the working tree, builds a systemd-under-Docker image,
starts a privileged container, installs WASM into it from the wheel exactly
as an operator would, and runs a set of scenarios over the real CLI against
real nginx, real systemd and a real SQLite store.

This file is intentionally excluded from pytest's default collection: it
does not match ``test_*.py`` (see ``[tool.pytest.ini_options]`` in
``pyproject.toml``), and it drives Docker rather than being a unit test.

Usage:
    .venv/bin/python tests/integration/run.py
    .venv/bin/python tests/integration/run.py --keep
    .venv/bin/python tests/integration/run.py --scenario node_app_update

Requires Docker (tested against Docker 29) with a working systemd-in-Docker
setup: cgroup v2, ``--privileged``, ``--cgroupns=host`` and the host cgroup
filesystem bind-mounted. run.py wires all of that itself.
"""

from __future__ import annotations

import argparse
import difflib
import json
import re
import secrets
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
INTEGRATION_DIR = Path(__file__).resolve().parent
FIXTURES_DIR = INTEGRATION_DIR / "fixtures"
VENV_PYTHON = REPO_ROOT / ".venv" / "bin" / "python"
DOCKERFILE = INTEGRATION_DIR / "Dockerfile.systemd"
IMAGE_TAG = "wasm-integration:latest"

#: How long the whole container gets to reach "running" or "degraded".
SYSTEMD_READY_TIMEOUT = 90

#: How long a `pip install '<wheel>[all]'` may take with a cold cache.
PIP_INSTALL_TIMEOUT = 600

#: How long a deploy (fetch + install + build + certbot-less site + start)
#: may take. Node dependency installs are the slow part.
DEPLOY_TIMEOUT = 300

#: The repository the release scenarios deploy from, and its git:// URL.
RELEASE_REPO = "/root/fixtures/node-rel"
RELEASE_URL = "git://127.0.0.1/node-rel"

#: The release scenarios' application.
RELEASE_DOMAIN = "rel.test"
RELEASE_ROOT = "/var/www/apps/rel-test"

#: The store, wherever this install put it: the system location when
#: /var/lib/wasm exists, root's own otherwise.
WASM_DB = "$(ls /var/lib/wasm/wasm.db /root/.local/share/wasm/wasm.db 2>/dev/null | head -1)"

#: The released version the upgrade rehearsal (--upgrade) starts from. This is
#: the most important pre-release scenario for 2.0: a real 1.x server, with
#: real applications, upgraded in place with nothing but `pip install` over
#: the same venv, exactly as docs/UPGRADING-2.0.md describes.
UPGRADE_FROM_VERSION = "1.6.5"

UPGRADE_NODE_DOMAIN = "upg-node.test"
UPGRADE_NODE_APP = "upg-node-test"
UPGRADE_NODE_ROOT = f"/var/www/apps/{UPGRADE_NODE_APP}"

UPGRADE_STATIC_DOMAIN = "upg-static.test"
UPGRADE_STATIC_APP = "upg-static-test"
UPGRADE_STATIC_ROOT = f"/var/www/apps/{UPGRADE_STATIC_APP}"

UPGRADE_COMPOSE_DOMAIN = "upg-compose.test"
UPGRADE_COMPOSE_APP = "upg-compose-test"
UPGRADE_COMPOSE_ROOT = f"/var/www/apps/{UPGRADE_COMPOSE_APP}"

#: The query behind :func:`_store_apps_snapshot`: columns that exist in both
#: the 1.6.5 (schema v3) and 2.0 (schema v8) apps table, in a stable order, so
#: the row for each application can be compared byte-for-byte across the
#: upgrade. `layout` and the other v5+ columns are checked separately, since
#: 1.6.5 does not have them. A plain literal - not built with an f-string -
#: like every other query in this file, so ruff's hardcoded-sql check (S608,
#: which cannot tell a literal from an injection) has nothing to flag.
APPS_SNAPSHOT_QUERY = (
    "SELECT domain, app_type, source, branch, port, webserver, ssl_enabled, status, "
    "is_static, created_at, deployed_at FROM apps ORDER BY domain"
)


class HarnessError(RuntimeError):
    """Raised for failures in the setup phases (build, install), not scenarios."""


# ---------------------------------------------------------------------------
# Host-side process helpers
# ---------------------------------------------------------------------------


def sh(
    argv: list[str], *, timeout: int = 120, check: bool = True
) -> subprocess.CompletedProcess[str]:
    """Run a command on the host (not in the container) and capture output."""
    proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    if check and proc.returncode != 0:
        raise HarnessError(
            f"command failed (exit {proc.returncode}): {' '.join(argv)}\n"
            f"--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}"
        )
    return proc


def docker_exec(
    container: str, script: str, *, timeout: int = 120, check: bool = True
) -> subprocess.CompletedProcess[str]:
    """Run a shell script inside the container through `docker exec`."""
    proc = subprocess.run(
        ["docker", "exec", container, "bash", "-lc", script],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if check and proc.returncode != 0:
        raise HarnessError(
            f"command failed in container (exit {proc.returncode}): {script}\n"
            f"--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}"
        )
    return proc


# ---------------------------------------------------------------------------
# Setup phases
# ---------------------------------------------------------------------------


def build_wheel(workdir: Path) -> Path:
    """Build the wheel with the repo's own venv, installing `build` if needed."""
    python = VENV_PYTHON if VENV_PYTHON.exists() else Path(sys.executable)
    print(f"[setup] building wheel with {python}")

    probe = subprocess.run([str(python), "-c", "import build"], capture_output=True, text=True)
    if probe.returncode != 0:
        print("[setup] installing the `build` package into the repo venv")
        sh([str(python), "-m", "pip", "install", "build"], timeout=180)

    outdir = workdir / "dist"
    outdir.mkdir(parents=True, exist_ok=True)
    sh(
        [str(python), "-m", "build", "--wheel", "--outdir", str(outdir), str(REPO_ROOT)],
        timeout=180,
    )
    wheels = sorted(outdir.glob("*.whl"))
    if not wheels:
        raise HarnessError(f"`python -m build` produced no wheel in {outdir}")
    wheel = wheels[-1]
    print(f"[setup] built {wheel.name}")
    return wheel


def build_image() -> None:
    """Build the systemd-under-Docker image scenarios run against."""
    print(f"[setup] building image {IMAGE_TAG}")
    sh(
        ["docker", "build", "-t", IMAGE_TAG, "-f", str(DOCKERFILE), str(INTEGRATION_DIR)],
        timeout=600,
    )


def start_container(name: str) -> None:
    """Start the privileged, systemd-as-PID-1 container."""
    print(f"[setup] starting container {name}")
    sh(
        [
            "docker",
            "run",
            "-d",
            "--name",
            name,
            "--privileged",
            "--cgroupns=host",
            "-v",
            "/sys/fs/cgroup:/sys/fs/cgroup:rw",
            "--tmpfs",
            "/run",
            "--tmpfs",
            "/run/lock",
            IMAGE_TAG,
        ],
        timeout=60,
    )


def wait_for_systemd(name: str, timeout: int = SYSTEMD_READY_TIMEOUT) -> str:
    """Poll `systemctl is-system-running` until it settles or the deadline passes."""
    print("[setup] waiting for systemd to report running/degraded")
    deadline = time.time() + timeout
    last = "<never answered>"
    while time.time() < deadline:
        result = docker_exec(name, "systemctl is-system-running || true", timeout=15, check=False)
        last = result.stdout.strip() or last
        if last in ("running", "degraded"):
            print(f"[setup] systemd is {last}")
            return last
        time.sleep(2)
    journal = docker_exec(name, "journalctl -xb --no-pager | tail -100", timeout=15, check=False)
    raise HarnessError(
        f"systemd never reached running/degraded in {timeout}s (last state: {last!r})\n"
        f"--- journalctl -xb (tail) ---\n{journal.stdout}"
    )


def install_wasm(name: str, wheel: Path) -> None:
    """Install WASM into a dedicated venv, exactly as documented for operators."""
    print(f"[setup] copying {wheel.name} into the container")
    sh(["docker", "cp", str(wheel), f"{name}:/tmp/{wheel.name}"], timeout=60)

    print("[setup] creating /opt/wasm venv and installing the wheel")
    docker_exec(name, "python3 -m venv /opt/wasm", timeout=60)
    docker_exec(name, "/opt/wasm/bin/pip install --quiet --upgrade pip", timeout=120)
    docker_exec(
        name,
        f"/opt/wasm/bin/pip install --quiet '/tmp/{wheel.name}[all]'",
        timeout=PIP_INSTALL_TIMEOUT,
    )
    docker_exec(name, "ln -sf /opt/wasm/bin/wasm /usr/local/bin/wasm", timeout=15)
    result = docker_exec(name, "wasm --help", timeout=30)
    print("[setup] wasm --help:")
    print(result.stdout)


def install_fixtures(name: str) -> None:
    """Copy the fixture apps into the container and turn each into a git repo."""
    print("[setup] copying fixtures into the container")
    sh(["docker", "cp", str(FIXTURES_DIR), f"{name}:/root/fixtures"], timeout=60)

    # `docker cp` preserves the numeric uid of the files on the host, which
    # is almost never root's. git then refuses to touch the tree ("detected
    # dubious ownership") and so would WASM's own fetch step were these ever
    # used as a source through anything but a straight directory copy.
    docker_exec(name, "chown -R root:root /root/fixtures", timeout=15)
    docker_exec(name, "git config --global --add safe.directory '*'", timeout=15)

    # The nodejs deployer always installs with `npm ci`
    # (helpers/package_manager.py:187), which refuses to run without a
    # committed lockfile. Real Node projects ship one; this fixture needs one
    # too, generated fresh so it matches whatever npm version the image has.
    docker_exec(
        name,
        "cd /root/fixtures/node-app && npm install --package-lock-only",
        timeout=60,
    )

    for app in ("static-site", "node-app"):
        script = (
            f"cd /root/fixtures/{app} && "
            "git init -q && "
            "git config user.email wasm-it@example.com && "
            "git config user.name 'WASM Integration' && "
            "git add -A && "
            "git commit -q -m 'initial fixture'"
        )
        docker_exec(name, script, timeout=30)
    print("[setup] fixtures are local git repositories under /root/fixtures")

    # The release scenarios deploy from a git remote, so the repository cache,
    # the fetch and the export run for real. A copy of the node fixture keeps
    # their commits out of the in-place scenarios' repository, and git daemon
    # serves it over git:// on loopback: WASM refuses file:// on purpose.
    docker_exec(name, f"cp -a /root/fixtures/node-app {RELEASE_REPO}", timeout=30)
    # A dependency, so there is a node_modules for an update to reuse. A local
    # one: npm links it from the tree and never touches the network.
    docker_exec(
        name,
        f"cd {RELEASE_REPO} && mkdir local-dep && "
        'echo \'{"name": "local-dep", "version": "1.0.0"}\' > local-dep/package.json && '
        "echo 'module.exports = 1;' > local-dep/index.js && "
        "npm pkg set dependencies.local-dep=file:./local-dep && "
        "npm install --package-lock-only && "
        "git add -A && git commit -q -m 'add a local dependency'",
        timeout=60,
    )
    docker_exec(
        name,
        "git daemon --reuseaddr --export-all --base-path=/root/fixtures "
        "--listen=127.0.0.1 --detach /root/fixtures",
        timeout=30,
    )
    docker_exec(name, f"git ls-remote --exit-code {RELEASE_URL} HEAD", timeout=30)
    print(f"[setup] {RELEASE_REPO} is served at {RELEASE_URL}")


def install_wasm_from_pypi(name: str, version: str) -> None:
    """Install a released version of WASM from PyPI: the upgrade rehearsal's starting point.

    Mirrors :func:`install_wasm`, but pulls the package straight from the
    index instead of copying in a locally built wheel, because the point of
    ``--upgrade`` is to start from what an operator actually has installed
    today.

    Args:
        name: Container name.
        version: The exact ``wasm-cli`` version to install, e.g. "1.6.5".
    """
    print(f"[setup] creating /opt/wasm venv and installing wasm-cli=={version} from PyPI")
    docker_exec(name, "python3 -m venv /opt/wasm", timeout=60)
    docker_exec(name, "/opt/wasm/bin/pip install --quiet --upgrade pip", timeout=120)
    docker_exec(
        name,
        f"/opt/wasm/bin/pip install --quiet 'wasm-cli[all]=={version}'",
        timeout=PIP_INSTALL_TIMEOUT,
    )
    docker_exec(name, "ln -sf /opt/wasm/bin/wasm /usr/local/bin/wasm", timeout=15)
    result = docker_exec(name, "wasm --version", timeout=30)
    print(f"[setup] installed {result.stdout.strip()}")


def upgrade_wasm_to_wheel(name: str, wheel: Path) -> None:
    """Upgrade the venv in place to the locally built wheel, the way an operator would.

    On a real release the wheel's version is newer than what PyPI serves and
    a plain ``pip install --upgrade 'wasm-cli[all]'`` replaces it. This
    working tree's ``pyproject.toml`` has not been bumped past
    :data:`UPGRADE_FROM_VERSION` yet (see the final report), so the wheel
    built from it can carry the *same* version number as the PyPI release
    already installed; a bare ``pip install`` of a same-version local file is
    a no-op ("Requirement already satisfied"), which would leave the old
    package in place and silently turn this whole rehearsal into a no-op too.
    ``--force-reinstall`` makes the replacement happen regardless of what the
    version string says, which is what actually matters here: whether the
    new code runs correctly over data the old code created.
    ``--no-deps`` is safe because the two versions declare the exact same
    ``[all]`` dependencies (checked against 1.6.5's METADATA before writing
    this), so nothing new needs fetching; without it every upgrade run would
    re-resolve and reinstall fastapi/uvicorn/etc. for no reason.

    Args:
        name: Container name.
        wheel: Path to the wheel built from the working tree, on the host.
    """
    print(f"[setup] copying {wheel.name} into the container for the upgrade")
    sh(["docker", "cp", str(wheel), f"{name}:/tmp/{wheel.name}"], timeout=60)
    docker_exec(
        name,
        f"/opt/wasm/bin/pip install --quiet --force-reinstall --no-deps '/tmp/{wheel.name}[all]'",
        timeout=PIP_INSTALL_TIMEOUT,
    )
    result = docker_exec(name, "wasm --version", timeout=30)
    print(f"[setup] upgraded to {result.stdout.strip()}")


def remove_container(name: str) -> None:
    """Best-effort container teardown; never raises."""
    subprocess.run(["docker", "rm", "-f", name], capture_output=True, text=True, timeout=60)


# ---------------------------------------------------------------------------
# Scenario framework
# ---------------------------------------------------------------------------


@dataclass
class Scenario:
    """Collaborator scenarios use to run commands and assert on them.

    Every command run through :meth:`run` is kept, in order, as evidence -
    printed verbatim after the scenario's PASS/FAIL line.
    """

    container: str
    evidence: list[str] = field(default_factory=list)

    def run(
        self, script: str, *, timeout: int = 60, check: bool = True, label: str | None = None
    ) -> subprocess.CompletedProcess[str]:
        proc = docker_exec(self.container, script, timeout=timeout, check=False)
        block = [f"$ {label or script}"]
        if proc.stdout:
            block.append(proc.stdout.rstrip("\n"))
        if proc.stderr.strip():
            block.append("--- stderr ---")
            block.append(proc.stderr.rstrip("\n"))
        block.append(f"(exit={proc.returncode})")
        self.evidence.append("\n".join(block))
        if check and proc.returncode != 0:
            raise AssertionError(f"command failed (exit {proc.returncode}): {label or script}")
        return proc

    def check(self, condition: bool, message: str) -> None:
        if not condition:
            raise AssertionError(message)


ScenarioFn = Callable[[Scenario], None]
SCENARIOS: list[tuple[str, ScenarioFn]] = []


def scenario(name: str) -> Callable[[ScenarioFn], ScenarioFn]:
    def decorator(fn: ScenarioFn) -> ScenarioFn:
        SCENARIOS.append((name, fn))
        return fn

    return decorator


# ---------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------


@scenario("static_site_create_and_serve")
def scenario_static_site(sc: Scenario) -> None:
    """A static site deploys with --no-ssl and nginx serves it and its image."""
    sc.run(
        "wasm create -d static.test -s /root/fixtures/static-site -t static --no-ssl",
        timeout=DEPLOY_TIMEOUT,
        label="wasm create -d static.test -s /root/fixtures/static-site -t static --no-ssl",
    )

    page = sc.run(
        "curl -sS -H 'Host: static.test' http://127.0.0.1/",
        timeout=30,
        label="curl -H 'Host: static.test' http://127.0.0.1/",
    )
    sc.check(
        "WASM Integration Static Fixture" in page.stdout,
        f"index page missing the fixture marker text: {page.stdout!r}",
    )

    image = sc.run(
        "curl -sS -o /dev/null -w '%{http_code} %{size_download}' "
        "-H 'Host: static.test' http://127.0.0.1/logo.png",
        timeout=30,
        label="curl -H 'Host: static.test' http://127.0.0.1/logo.png",
    )
    code, _, size = image.stdout.strip().partition(" ")
    sc.check(
        code == "200" and int(size or "0") > 0,
        f"image fetch failed or was empty: {image.stdout!r}",
    )

    # Planted in the served tree so this proves nginx itself refuses them, not just
    # that nothing happens to be there. Written here rather than shipped in the fixture
    # because git cannot track a file inside a directory named .git.
    sc.run(
        "root=$(readlink -e /var/www/apps/static-test/current || echo /var/www/apps/static-test) && "
        'echo SECRET=integration > "$root/.env" && '
        'mkdir -p "$root/.git" && echo \'[core]\' > "$root/.git/config"',
        timeout=30,
        label="plant .env and .git/config in the served tree",
    )
    for path in ("/.env", "/.git/config"):
        hidden = sc.run(
            f"curl -sS -o /dev/null -w '%{{http_code}}' -H 'Host: static.test' "
            f"http://127.0.0.1{path}",
            timeout=30,
            label=f"curl -H 'Host: static.test' http://127.0.0.1{path}",
        )
        sc.check(
            hidden.stdout.strip() in ("403", "404"),
            f"{path} must answer 403 or 404, got {hidden.stdout.strip()!r}",
        )

    challenge = sc.run(
        "mkdir -p /var/www/html/.well-known/acme-challenge && "
        "echo wasm-integration-token > /var/www/html/.well-known/acme-challenge/probe && "
        "curl -sS -H 'Host: static.test' http://127.0.0.1/.well-known/acme-challenge/probe",
        timeout=30,
        label="curl -H 'Host: static.test' http://127.0.0.1/.well-known/acme-challenge/probe",
    )
    sc.check(
        "wasm-integration-token" in challenge.stdout,
        f"ACME's own well-known path must stay reachable: {challenge.stdout!r}",
    )


@scenario("node_app_create_update_and_data_survival")
def scenario_node_app_update(sc: Scenario) -> None:
    """A Node app deploys, is updated to a new commit, and keeps its own data.

    This is the v1.6.3 data-loss regression check: an update must not erase
    ``.env`` or anything the running application wrote into its own tree
    (uploads among them). It deploys with the 1.x in-place layout, which is
    what every application deployed before 2.0 is on, and checks that an
    update leaves it there.
    """
    sc.run(
        "wasm create -d node.test -s /root/fixtures/node-app -t nodejs --no-ssl --layout inplace",
        timeout=DEPLOY_TIMEOUT,
        label="wasm create -d node.test -s /root/fixtures/node-app -t nodejs --no-ssl "
        "--layout inplace",
    )

    page = sc.run(
        "curl -sS -H 'Host: node.test' http://127.0.0.1/",
        timeout=30,
        label="curl -H 'Host: node.test' http://127.0.0.1/",
    )
    sc.check(page.stdout.strip() == "ok 1", f"expected 'ok 1', got {page.stdout!r}")

    env_before = sc.run(
        "test -f /var/www/apps/node-test/.env && echo present",
        timeout=15,
        check=False,
        label="test -f /var/www/apps/node-test/.env",
    )
    sc.check(
        env_before.stdout.strip() == "present",
        ".env was not created from .env.example on deploy",
    )

    upload = sc.run(
        "curl -sS -H 'Host: node.test' --data-binary 'integration-harness-upload' "
        "http://127.0.0.1/upload",
        timeout=30,
        label="curl -H 'Host: node.test' --data-binary ... http://127.0.0.1/upload",
    )
    sc.check("uploaded" in upload.stdout, f"upload did not succeed: {upload.stdout!r}")

    sc.run(
        "cd /root/fixtures/node-app && echo 2 > VERSION && "
        "git add VERSION && git commit -q -m 'bump version to 2'",
        timeout=30,
        label="(fixture repo) echo 2 > VERSION; git commit",
    )

    sc.run("wasm update node.test", timeout=DEPLOY_TIMEOUT, label="wasm update node.test")

    page2 = sc.run(
        "curl -sS -H 'Host: node.test' http://127.0.0.1/",
        timeout=30,
        label="curl -H 'Host: node.test' http://127.0.0.1/ (after update)",
    )
    sc.check(
        page2.stdout.strip() == "ok 2",
        f"expected 'ok 2' after update, got {page2.stdout!r}",
    )

    upload_after = sc.run(
        "cat /var/www/apps/node-test/uploads/upload.txt",
        timeout=15,
        check=False,
        label="cat /var/www/apps/node-test/uploads/upload.txt (after update)",
    )
    sc.check(
        upload_after.returncode == 0 and "integration-harness-upload" in upload_after.stdout,
        "the uploaded file did not survive `wasm update` - this is the v1.6.3 "
        "data-loss regression `wasm update` is supposed to have fixed",
    )

    env_after = sc.run(
        "test -f /var/www/apps/node-test/.env && echo present",
        timeout=15,
        check=False,
        label="test -f /var/www/apps/node-test/.env (after update)",
    )
    sc.check(env_after.stdout.strip() == "present", ".env did not survive `wasm update`")

    listing = sc.run(
        "ls -A /var/www/apps/node-test",
        timeout=15,
        label="ls -A /var/www/apps/node-test (after update)",
    )
    layout = sc.run(
        store_query("SELECT layout FROM apps WHERE domain = 'node.test'"),
        timeout=15,
        label="SELECT layout FROM apps WHERE domain = 'node.test' (after update)",
    )
    sc.check(
        not {"releases", "current", "shared", "repo"} & set(listing.stdout.split())
        and layout.stdout.strip() == "inplace",
        f"the in-place app was moved onto releases by an update: {listing.stdout!r} "
        f"{layout.stdout!r}",
    )


@scenario("update_hands_tree_back_to_www_data")
def scenario_ownership(sc: Scenario) -> None:
    """After an update, nothing under the app tree is still owned by root.

    This is the v1.6.2 regression check (EACCES at runtime because the tree
    was handed back to the service user only on deploy, not on update).
    node_modules is excluded per the task brief, though the current
    implementation (helpers/permissions.py:hand_over_tree) chowns the whole
    tree including it.
    """
    result = sc.run(
        "find /var/www/apps/node-test -not -path '*/node_modules/*' "
        "-not -path '*/node_modules' -user root",
        timeout=30,
        check=False,
        label="find /var/www/apps/node-test -not -path '*/node_modules/*' -user root",
    )
    sc.check(result.returncode == 0, f"find failed: {result.stderr}")
    sc.check(
        result.stdout.strip() == "",
        f"root-owned files remain under the app tree after update:\n{result.stdout}",
    )


def store_query(query: str) -> str:
    """Return the shell command that runs a read-only query against the store."""
    return f'sqlite3 {WASM_DB} "{query}"'


def active_release(sc: Scenario, label: str) -> str:
    """Return what ``current`` points at, as ``releases/<id>``."""
    link = sc.run(f"readlink {RELEASE_ROOT}/current", timeout=15, check=False, label=label)
    return link.stdout.strip()


def commit_fixture(sc: Scenario, script: str, message: str) -> None:
    """Change the release fixture repository and commit it."""
    sc.run(
        f"cd {RELEASE_REPO} && {script} && git add -A && git commit -q -m '{message}'",
        timeout=30,
        label=f"(fixture repo) {script}; git commit -m '{message}'",
    )


@scenario("release_app_create_and_serve")
def scenario_release_create(sc: Scenario) -> None:
    """A Node app created on the release layout builds releases/<id> behind current.

    Deployed from a git remote, so the repository cache, the fetch and the
    export into the release all run for real; ``uploads`` is a persistent
    path that lives in shared/.
    """
    create = (
        f"wasm create -d {RELEASE_DOMAIN} -s {RELEASE_URL} -t nodejs --no-ssl "
        "--layout releases --persist uploads"
    )
    sc.run(create, timeout=DEPLOY_TIMEOUT, label=create)

    tree = sc.run(
        f"ls -A {RELEASE_ROOT}; ls {RELEASE_ROOT}/releases; "
        f"test -d {RELEASE_ROOT}/repo/.git && echo repo-is-a-clone; "
        f"test -e {RELEASE_ROOT}/current/.git || echo release-has-no-git",
        timeout=15,
        label=f"ls -A {RELEASE_ROOT}; ls releases; repo/.git; current/.git",
    )
    for expected in ("releases", "current", "shared", "repo", "repo-is-a-clone"):
        sc.check(expected in tree.stdout.split(), f"{expected} missing: {tree.stdout!r}")
    sc.check("release-has-no-git" in tree.stdout, "the release was exported with its .git")

    current = active_release(sc, f"readlink {RELEASE_ROOT}/current")
    sc.check(current.startswith("releases/"), f"current is not a release link: {current!r}")

    links = sc.run(
        f"readlink {RELEASE_ROOT}/current/.env {RELEASE_ROOT}/current/uploads; "
        f"stat -c '%a %U' {RELEASE_ROOT}/shared/.env",
        timeout=15,
        label="readlink current/.env current/uploads; stat shared/.env",
    )
    sc.check(
        links.stdout.split()[:2] == ["../../shared/.env", "../../shared/uploads"],
        f".env and uploads are not linked into shared/: {links.stdout!r}",
    )
    sc.check(links.stdout.split()[2:] == ["600", "www-data"], f"shared/.env: {links.stdout!r}")

    unit = sc.run(
        "systemctl cat rel-test | grep -E '^(WorkingDirectory|ExecStart)='",
        timeout=15,
        label="systemctl cat rel-test | grep WorkingDirectory/ExecStart",
    )
    sc.check(
        f"WorkingDirectory={RELEASE_ROOT}/current" in unit.stdout,
        f"the unit does not run from current: {unit.stdout!r}",
    )

    page = sc.run(
        f"curl -sS -H 'Host: {RELEASE_DOMAIN}' http://127.0.0.1/",
        timeout=30,
        label=f"curl -H 'Host: {RELEASE_DOMAIN}' http://127.0.0.1/",
    )
    sc.check(page.stdout.strip() == "ok 1", f"expected 'ok 1', got {page.stdout!r}")

    rows = sc.run(
        store_query(
            "SELECT a.layout, r.status FROM releases r "
            "JOIN apps a ON a.id = r.app_id WHERE a.domain = 'rel.test'"
        ),
        timeout=15,
        label="SELECT layout, release status FROM the store",
    )
    sc.check(rows.stdout.split() == ["releases|active"], f"store rows: {rows.stdout!r}")


@scenario("release_app_update_keeps_uploads")
def scenario_release_update(sc: Scenario) -> None:
    """An update builds a new release; the upload the old one took is still served.

    The lockfile does not change between the two commits, so the new release
    takes its node_modules from the active one instead of installing.
    """
    before = active_release(sc, f"readlink {RELEASE_ROOT}/current (before update)")

    upload = sc.run(
        f"curl -sS -H 'Host: {RELEASE_DOMAIN}' --data-binary 'release-harness-upload' "
        "http://127.0.0.1/upload",
        timeout=30,
        label=f"curl -H 'Host: {RELEASE_DOMAIN}' --data-binary ... http://127.0.0.1/upload",
    )
    sc.check("uploaded" in upload.stdout, f"upload did not succeed: {upload.stdout!r}")
    sc.run(
        f"cat {RELEASE_ROOT}/shared/uploads/upload.txt",
        timeout=15,
        label="cat shared/uploads/upload.txt (the upload landed in shared/)",
    )

    commit_fixture(sc, "echo 2 > VERSION", "bump version to 2")
    sc.run(
        f"wasm update {RELEASE_DOMAIN}",
        timeout=DEPLOY_TIMEOUT,
        label=f"wasm update {RELEASE_DOMAIN}",
    )

    # The console shows substeps only with -v; the captured deploy log has all.
    log_path = sc.run(
        store_query(
            "SELECT log_path FROM deployments WHERE domain = 'rel.test' ORDER BY id DESC LIMIT 1"
        ),
        timeout=15,
        label="SELECT the deploy log of the update",
    ).stdout.strip()
    log = sc.run(
        f"grep -E 'Dependencies reused|npm ci|Activated release' {log_path}",
        timeout=15,
        check=False,
        label=f"grep 'Dependencies reused|npm ci|Activated release' {log_path}",
    )
    sc.check(
        f"Dependencies reused from {before.split('/')[-1]}" in log.stdout
        and "npm ci" not in log.stdout,
        "the update installed again although the lockfile did not change",
    )

    after = active_release(sc, f"readlink {RELEASE_ROOT}/current (after update)")
    sc.check(after != before, f"current still points at {before}")
    count = sc.run(f"ls {RELEASE_ROOT}/releases | wc -l", timeout=15, label="ls releases | wc -l")
    sc.check(count.stdout.strip() == "2", f"expected 2 releases, found {count.stdout.strip()}")

    page = sc.run(
        f"curl -sS -H 'Host: {RELEASE_DOMAIN}' http://127.0.0.1/",
        timeout=30,
        label=f"curl -H 'Host: {RELEASE_DOMAIN}' http://127.0.0.1/ (after update)",
    )
    sc.check(page.stdout.strip() == "ok 2", f"expected 'ok 2' after update, got {page.stdout!r}")

    kept = sc.run(
        f"cat {RELEASE_ROOT}/current/uploads/upload.txt",
        timeout=15,
        check=False,
        label="cat current/uploads/upload.txt (through the new release)",
    )
    sc.check(
        kept.returncode == 0 and "release-harness-upload" in kept.stdout,
        "the upload is not reachable from the new release",
    )

    owners = sc.run(
        f"find {RELEASE_ROOT}/{after} {RELEASE_ROOT}/shared -user root "
        "-not -path '*/node_modules/*' -not -path '*/node_modules'",
        timeout=30,
        check=False,
        label=f"find {after} shared -user root (not node_modules)",
    )
    sc.check(owners.stdout.strip() == "", f"root-owned files remain:\n{owners.stdout}")


@scenario("release_app_broken_commit_rolls_back")
def scenario_release_rollback(sc: Scenario) -> None:
    """A release whose server throws on start fails the health gate and is rolled back.

    The previous release serves again without the operator doing anything,
    the update fails with the process's own words, and the store says which
    release failed.
    """
    before = active_release(sc, f"readlink {RELEASE_ROOT}/current (before the broken commit)")

    commit_fixture(
        sc,
        "echo 3 > VERSION && sed -i '1i throw new Error(\"broken on purpose\");' server.js",
        "break the server",
    )
    result = sc.run(
        f"wasm update {RELEASE_DOMAIN}",
        timeout=DEPLOY_TIMEOUT,
        check=False,
        label=f"wasm update {RELEASE_DOMAIN} (broken commit)",
    )
    output = result.stdout + result.stderr
    sc.check(result.returncode != 0, "wasm update reported success for a release that never ran")
    sc.check("did not pass its health check" in output, "the failure does not say what failed")
    sc.check(
        f"{before.split('/')[-1]} is active again" in output,
        "the failure does not say the previous release is back",
    )

    after = active_release(sc, f"readlink {RELEASE_ROOT}/current (after the failed update)")
    sc.check(after == before, f"current moved from {before} to {after}")

    page = sc.run(
        f"curl -sS -H 'Host: {RELEASE_DOMAIN}' http://127.0.0.1/",
        timeout=30,
        label=f"curl -H 'Host: {RELEASE_DOMAIN}' http://127.0.0.1/ (after the rollback)",
    )
    sc.check(page.stdout.strip() == "ok 2", f"the previous release is not serving: {page.stdout!r}")

    history = sc.run(
        store_query(
            "SELECT status, error FROM deployments "
            "WHERE domain = 'rel.test' ORDER BY id DESC LIMIT 1"
        ),
        timeout=15,
        label="SELECT the last deployment of rel.test",
    )
    sc.check(
        history.stdout.startswith("failed|") and "broken on purpose" in history.stdout,
        "the deployment record does not carry the process's own error",
    )
    statuses = sc.run(
        store_query(
            "SELECT r.status FROM releases r JOIN apps a ON a.id = r.app_id "
            "WHERE a.domain = 'rel.test' ORDER BY r.created_at"
        ),
        timeout=15,
        label="SELECT every release status of rel.test",
    )
    sc.check(
        statuses.stdout.split() == ["superseded", "active", "failed"],
        f"release statuses: {statuses.stdout.split()!r}",
    )

    # Leave the fixture deployable for anything that runs after this.
    sc.run(
        f"cd {RELEASE_REPO} && git revert --no-edit HEAD",
        timeout=30,
        label="(fixture repo) git revert --no-edit HEAD",
    )


@scenario("status_and_list")
def scenario_status_and_list(sc: Scenario) -> None:
    """`wasm status` and `wasm list` succeed and mention the deployed apps."""
    status = sc.run("wasm status node.test", timeout=30, label="wasm status node.test")
    sc.check(status.returncode == 0, "wasm status node.test failed")

    listing = sc.run("wasm list", timeout=30, label="wasm list")
    sc.check(listing.returncode == 0, "wasm list failed")
    sc.check(
        "node.test" in listing.stdout and "static.test" in listing.stdout,
        f"wasm list did not mention both deployed apps: {listing.stdout!r}",
    )


#: The strict policy the console is served under (plan, Global Constraints).
#: Trusted Types may follow it; nothing may relax it.
CONSOLE_CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "font-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; "
    "form-action 'self'; object-src 'none'"
)

PANEL_URL = "http://127.0.0.1:8080"


def http_head_and_body(sc: Scenario, path: str) -> tuple[int, dict[str, str], str]:
    """Fetch a panel path inside the container; return status, headers, body."""
    proc = sc.run(
        f"curl -sS -D - {PANEL_URL}{path}",
        timeout=15,
        check=False,
        label=f"curl -D - {PANEL_URL}{path} (body truncated in evidence)",
    )
    # Keep the evidence readable: the console bundle is not something to print.
    if len(sc.evidence[-1]) > 4000:
        sc.evidence[-1] = sc.evidence[-1][:4000] + "\n... (truncated)"
    head, _, body = proc.stdout.replace("\r\n", "\n").partition("\n\n")
    lines = head.splitlines()
    sc.check(bool(lines), f"no response for {path}: {proc.stderr!r}")
    status = int(lines[0].split()[1])
    headers = {}
    for line in lines[1:]:
        name, _, value = line.partition(":")
        headers[name.strip().lower()] = value.strip()
    return status, headers, body


@scenario("web_panel_serves_the_console")
def scenario_web_panel(sc: Scenario) -> None:
    """`wasm web start` answers /health and serves the console from the wheel.

    The console's Vite build ships inside the package (web/static), so this
    is also the packaging check: a build that was not committed, or a glob
    that stopped matching, is a blank page here and nowhere else before an
    operator opens it.
    """
    sc.run("wasm web start --daemon", timeout=60, label="wasm web start --daemon")

    health: subprocess.CompletedProcess[str] | None = None
    deadline = time.time() + 30
    while time.time() < deadline:
        health = sc.run(
            f"curl -sS -o /dev/null -w '%{{http_code}}' {PANEL_URL}/health",
            timeout=15,
            check=False,
            label=f"curl -o /dev/null -w '%{{http_code}}' {PANEL_URL}/health",
        )
        if health.stdout.strip() == "200":
            break
        time.sleep(1)

    sc.check(
        health is not None and health.stdout.strip() == "200",
        f"panel /health did not return 200 within 30s: {health.stdout if health else None!r}",
    )

    body = sc.run(
        f"curl -sS {PANEL_URL}/health",
        timeout=15,
        check=False,
        label=f"curl {PANEL_URL}/health",
    )
    sc.check('"status"' in body.stdout, f"unexpected /health body: {body.stdout!r}")

    try:
        # The console, at the root and at a deep link a reload lands on.
        for path in ("/", "/apps/example.com/deployments"):
            status, headers, html = http_head_and_body(sc, path)
            sc.check(status == 200, f"GET {path} answered {status}")
            sc.check('<div id="root">' in html, f"GET {path} is not the console: {html[:200]!r}")
            sc.check(
                headers.get("content-type", "").startswith("text/html"),
                f"GET {path} content-type {headers.get('content-type')!r}",
            )
            sc.check(
                headers.get("cache-control") == "no-store",
                f"GET {path} cache-control {headers.get('cache-control')!r}",
            )
            csp = headers.get("content-security-policy", "")
            sc.check(
                csp.startswith(CONSOLE_CSP) and "unsafe-inline" not in csp,
                f"GET {path} is not served under the strict policy: {csp!r}",
            )

        # Every asset the console names loads, and is cached for good.
        _, _, html = http_head_and_body(sc, "/")
        assets = sorted(set(re.findall(r'(?:src|href)="(/assets/[^"]+)"', html)))
        sc.check(bool(assets), "the console names no /assets/ file")
        status_lines = sc.run(
            "for p in "
            + " ".join(assets)
            + f'; do curl -sS -o /dev/null -w "%{{http_code}} $p\\n" {PANEL_URL}$p; done',
            timeout=60,
            check=False,
            label=f"curl every one of the {len(assets)} assets index.html names",
        ).stdout.splitlines()
        failed = [line for line in status_lines if not line.startswith("200 ")]
        sc.check(
            len(status_lines) == len(assets) and not failed,
            f"assets that did not load: {failed or status_lines}",
        )
        status, headers, _ = http_head_and_body(sc, assets[0])
        sc.check(
            "immutable" in headers.get("cache-control", ""),
            f"{assets[0]} cache-control {headers.get('cache-control')!r}",
        )
        status, _, _ = http_head_and_body(sc, "/favicon.svg")
        sc.check(status == 200, f"/favicon.svg answered {status}")

        # A machine path is never shadowed by the console.
        status, headers, _ = http_head_and_body(sc, "/api/does-not-exist")
        sc.check(
            status in (401, 404) and headers.get("content-type", "").startswith("application/json"),
            f"/api/does-not-exist answered {status} {headers.get('content-type')!r}",
        )
    finally:
        sc.run("wasm web stop", timeout=30, check=False, label="wasm web stop")


# ---------------------------------------------------------------------------
# Instant rollback, migration and resource limits
# ---------------------------------------------------------------------------

#: The in-place application the migration scenario moves onto releases.
INPLACE_DOMAIN = "node.test"
INPLACE_ROOT = "/var/www/apps/node-test"


def tree_census(sc: Scenario, root: str, label: str) -> str:
    """Count the regular files under a directory and their bytes, following nothing."""
    return sc.run(
        f"find {root} -type f -printf '%s\\n' | awk '{{n++; s+=$1}} END {{print n, s}}'",
        timeout=30,
        label=label,
    ).stdout.strip()


@scenario("release_app_instant_rollback")
def scenario_instant_rollback(sc: Scenario) -> None:
    """`wasm releases rollback` serves the previous release at once, with nothing rebuilt.

    Runs after the release scenarios, which leave two releases on disk: the
    first serving "ok 1" and the second, active, serving "ok 2".
    """
    listing = sc.run(
        f"wasm releases list {RELEASE_DOMAIN} --json",
        timeout=30,
        label=f"wasm releases list {RELEASE_DOMAIN} --json",
    )
    items = json.loads(listing.stdout)["items"]
    on_disk = [item for item in items if item["on_disk"]]
    sc.check(len(on_disk) == 2, f"expected two releases on disk: {items!r}")
    newer, older = on_disk[0]["id"], on_disk[1]["id"]
    sc.check(on_disk[0]["active"], f"the newest release is not the active one: {items!r}")

    timed = sc.run(
        f"start=$(date +%s%N); wasm releases rollback {RELEASE_DOMAIN}; "
        'echo "elapsed_ms=$(( ($(date +%s%N) - start) / 1000000 ))"',
        timeout=120,
        label=f"wasm releases rollback {RELEASE_DOMAIN} (timed)",
    )
    elapsed = int(timed.stdout.rsplit("elapsed_ms=", 1)[1].strip())
    sc.check(elapsed < 30_000, f"the rollback took {elapsed} ms")
    sc.check(
        active_release(sc, "readlink current (after the rollback)") == f"releases/{older}",
        "current does not point at the previous release",
    )
    page = sc.run(
        f"curl -sS -H 'Host: {RELEASE_DOMAIN}' http://127.0.0.1/",
        timeout=30,
        label=f"curl -H 'Host: {RELEASE_DOMAIN}' http://127.0.0.1/ (after the rollback)",
    )
    sc.check(page.stdout.strip() == "ok 1", f"expected 'ok 1', got {page.stdout!r}")

    row = sc.run(
        store_query(
            "SELECT status, triggered_by, log_path FROM deployments "
            "WHERE domain = 'rel.test' ORDER BY id DESC LIMIT 1"
        ),
        timeout=15,
        label="SELECT the deployment row of the rollback",
    ).stdout.strip()
    status, trigger, log_path = row.split("|")
    sc.check((status, trigger) == ("success", "cli"), f"deployment row: {row!r}")
    rebuilt = sc.run(
        f"grep -cE 'npm (ci|install)|Installing' {log_path} || true",
        timeout=15,
        label="grep the rollback log for an install",
    )
    sc.check(rebuilt.stdout.strip() == "0", "the rollback installed something")
    statuses = dict(
        line.split("=", 1)
        for line in sc.run(
            store_query(
                "SELECT r.id || '=' || r.status FROM releases r JOIN apps a ON a.id = r.app_id "
                "WHERE a.domain = 'rel.test'"
            ),
            timeout=15,
            label="SELECT the status of every release of rel.test",
        ).stdout.split()
    )
    sc.check(
        (statuses.get(older), statuses.get(newer)) == ("active", "rolled_back"),
        f"release statuses: {statuses!r}",
    )

    # And forward again, by id.
    sc.run(
        f"wasm releases rollback {RELEASE_DOMAIN} {newer}",
        timeout=120,
        label=f"wasm releases rollback {RELEASE_DOMAIN} {newer}",
    )
    page = sc.run(
        f"curl -sS -H 'Host: {RELEASE_DOMAIN}' http://127.0.0.1/",
        timeout=30,
        label=f"curl -H 'Host: {RELEASE_DOMAIN}' http://127.0.0.1/ (forward again)",
    )
    sc.check(page.stdout.strip() == "ok 2", f"expected 'ok 2', got {page.stdout!r}")


@scenario("inplace_app_migrate_keeps_uploads")
def scenario_migrate(sc: Scenario) -> None:
    """An in-place app with an upload moves onto releases, keeps serving and keeps the upload.

    node.test was deployed in place and took an upload into its own tree
    (uploads/, gitignored) in the first Node scenario. A rehearsal changes
    nothing; the migration moves every file, none copied or lost; the upload
    ends in shared/ and is still reachable; an update afterwards builds a
    release that sees it too.
    """
    before_listing = sc.run(f"ls -A {INPLACE_ROOT}", timeout=15, label=f"ls -A {INPLACE_ROOT}")
    before = tree_census(sc, INPLACE_ROOT, "count files and bytes before the migration")

    sc.run(
        f"wasm --dry-run app migrate {INPLACE_DOMAIN}",
        timeout=60,
        label=f"wasm --dry-run app migrate {INPLACE_DOMAIN}",
    )
    sc.check(
        sc.run(f"ls -A {INPLACE_ROOT}", timeout=15, label="ls -A (after the rehearsal)").stdout
        == before_listing.stdout,
        "the rehearsal changed the application directory",
    )

    sc.run(
        f"wasm app migrate {INPLACE_DOMAIN} --yes",
        timeout=DEPLOY_TIMEOUT,
        label=f"wasm app migrate {INPLACE_DOMAIN} --yes",
    )
    after = tree_census(sc, INPLACE_ROOT, "count files and bytes after the migration")
    sc.check(after == before, f"files and bytes before {before!r}, after {after!r}")

    layout = sc.run(
        store_query(
            "SELECT layout || ' ' || persistent_paths FROM apps WHERE domain = 'node.test'"
        ),
        timeout=15,
        label="SELECT layout, persistent_paths FROM apps WHERE domain = 'node.test'",
    )
    sc.check(
        layout.stdout.strip().startswith("releases") and "uploads" in layout.stdout,
        f"store row: {layout.stdout!r}",
    )
    tree = sc.run(
        f"ls -A {INPLACE_ROOT}; readlink {INPLACE_ROOT}/current {INPLACE_ROOT}/current/uploads "
        f"{INPLACE_ROOT}/current/.env; cat {INPLACE_ROOT}/shared/uploads/upload.txt",
        timeout=15,
        label="the migrated tree: entries, links and the upload in shared/",
    )
    for expected in ("current", "releases", "shared", "../../shared/uploads", "../../shared/.env"):
        sc.check(expected in tree.stdout.split(), f"{expected} missing: {tree.stdout!r}")
    sc.check("integration-harness-upload" in tree.stdout, "the upload is not in shared/uploads")
    unit = sc.run(
        "systemctl cat node-test | grep -E '^WorkingDirectory='",
        timeout=15,
        label="systemctl cat node-test | grep WorkingDirectory",
    )
    sc.check(
        f"WorkingDirectory={INPLACE_ROOT}/current" in unit.stdout,
        f"the unit does not run from current: {unit.stdout!r}",
    )
    page = sc.run(
        f"curl -sS -H 'Host: {INPLACE_DOMAIN}' http://127.0.0.1/",
        timeout=30,
        label=f"curl -H 'Host: {INPLACE_DOMAIN}' http://127.0.0.1/ (after the migration)",
    )
    sc.check(page.stdout.strip() == "ok 2", f"expected 'ok 2', got {page.stdout!r}")

    # A new upload lands in shared/, through the release.
    sc.run(
        f"curl -sS -H 'Host: {INPLACE_DOMAIN}' --data-binary 'after-migration' "
        "http://127.0.0.1/upload",
        timeout=30,
        label=f"curl -H 'Host: {INPLACE_DOMAIN}' --data-binary ... /upload (after the migration)",
    )
    sc.check(
        "after-migration"
        in sc.run(
            f"cat {INPLACE_ROOT}/shared/uploads/upload.txt",
            timeout=15,
            label="cat shared/uploads/upload.txt",
        ).stdout,
        "an upload after the migration did not land in shared/",
    )

    # The migrated application keeps updating, now as releases.
    sc.run(
        "cd /root/fixtures/node-app && echo 3 > VERSION && "
        "git add VERSION && git commit -q -m 'bump version to 3'",
        timeout=30,
        label="(fixture repo) echo 3 > VERSION; git commit",
    )
    sc.run(
        f"wasm update {INPLACE_DOMAIN}",
        timeout=DEPLOY_TIMEOUT,
        label=f"wasm update {INPLACE_DOMAIN} (after the migration)",
    )
    count = sc.run(f"ls {INPLACE_ROOT}/releases | wc -l", timeout=15, label="ls releases | wc -l")
    sc.check(count.stdout.strip() == "2", f"expected 2 releases, found {count.stdout.strip()}")
    page = sc.run(
        f"curl -sS -H 'Host: {INPLACE_DOMAIN}' http://127.0.0.1/",
        timeout=30,
        label=f"curl -H 'Host: {INPLACE_DOMAIN}' http://127.0.0.1/ (after the update)",
    )
    sc.check(page.stdout.strip() == "ok 3", f"expected 'ok 3', got {page.stdout!r}")
    kept = sc.run(
        f"cat {INPLACE_ROOT}/current/uploads/upload.txt",
        timeout=15,
        check=False,
        label="cat current/uploads/upload.txt (through the new release)",
    )
    sc.check("after-migration" in kept.stdout, "the new release does not see the upload")


@scenario("resource_limits_in_systemd")
def scenario_limits(sc: Scenario) -> None:
    """`wasm app limits` puts the limits in the unit, and systemd reports them.

    ``--restart`` passes the same health gate as a deploy, so the application
    answers again the moment the command returns.
    """
    serving = sc.run(
        f"curl -sS -H 'Host: {RELEASE_DOMAIN}' http://127.0.0.1/",
        timeout=30,
        label=f"curl -H 'Host: {RELEASE_DOMAIN}' http://127.0.0.1/ (before the limits)",
    ).stdout.strip()
    sc.check(serving.startswith("ok "), f"{RELEASE_DOMAIN} is not serving: {serving!r}")
    sc.run(
        f"wasm app limits {RELEASE_DOMAIN} --memory 512M --cpu 50% --tasks 256 --restart",
        timeout=60,
        label=f"wasm app limits {RELEASE_DOMAIN} --memory 512M --cpu 50% --tasks 256 --restart",
    )
    shown = sc.run(
        "systemctl show rel-test -p MemoryMax,CPUQuotaPerSecUSec,TasksMax",
        timeout=15,
        label="systemctl show rel-test -p MemoryMax,CPUQuotaPerSecUSec,TasksMax",
    )
    properties = dict(line.split("=", 1) for line in shown.stdout.split())
    sc.check(
        properties
        == {"MemoryMax": str(512 * 1024 * 1024), "CPUQuotaPerSecUSec": "500ms", "TasksMax": "256"},
        f"systemd reports {properties!r}",
    )
    sc.check(
        sc.run(
            "systemctl is-active rel-test", timeout=15, check=False, label="is-active"
        ).stdout.strip()
        == "active",
        "the application did not come back under its limits",
    )
    page = sc.run(
        f"curl -sS -H 'Host: {RELEASE_DOMAIN}' http://127.0.0.1/",
        timeout=30,
        label=f"curl -H 'Host: {RELEASE_DOMAIN}' http://127.0.0.1/ (under limits)",
    )
    sc.check(page.stdout.strip() == serving, f"expected {serving!r}, got {page.stdout!r}")
    row = sc.run(
        store_query(
            "SELECT memory_max_mb || ' ' || cpu_quota_percent || ' ' || tasks_max "
            "FROM apps WHERE domain = 'rel.test'"
        ),
        timeout=15,
        label="SELECT the limits of rel.test",
    )
    sc.check(row.stdout.strip() == "512 50 256", f"store row: {row.stdout!r}")

    # Removing one removes its directive.
    sc.run(
        f"wasm app limits {RELEASE_DOMAIN} --memory none",
        timeout=60,
        label=f"wasm app limits {RELEASE_DOMAIN} --memory none",
    )
    unit = sc.run(
        "systemctl cat rel-test | grep -E '^(MemoryMax|CPUQuota|TasksMax)=' || true",
        timeout=15,
        label="systemctl cat rel-test | grep the limit directives",
    )
    sc.check(
        unit.stdout.split() == ["CPUQuota=50%", "TasksMax=256"],
        f"directives after removing the memory limit: {unit.stdout!r}",
    )


def curl_host(sc: Scenario, host: str, path: str = "/", *, head: bool = False) -> str:
    """Ask nginx on loopback for ``path`` as ``host``; return the body, or the headers."""
    flags = "-sS -I" if head else "-sS"
    return sc.run(
        f"curl {flags} -H 'Host: {host}' 'http://127.0.0.1{path}'",
        timeout=30,
        check=False,
        label=f"curl {flags} -H 'Host: {host}' http://127.0.0.1{path}",
    ).stdout


def check_alias_and_redirect(sc: Scenario, primary: str, alias: str, redirect: str) -> None:
    """Give an app an alias and a redirect, and see nginx serve and redirect them."""
    sc.run(
        f"wasm domain add {primary} {alias} --kind alias",
        timeout=60,
        label=f"wasm domain add {primary} {alias} --kind alias",
    )
    sc.run(
        f"wasm domain add {primary} {redirect} --kind redirect",
        timeout=60,
        label=f"wasm domain add {primary} {redirect} --kind redirect",
    )
    listed = sc.run(
        f"wasm domain list {primary} --json",
        timeout=30,
        label=f"wasm domain list {primary} --json",
    )
    kinds = {item["domain"]: item["kind"] for item in json.loads(listed.stdout)["items"]}
    sc.check(
        kinds == {primary: "primary", alias: "alias", redirect: "redirect"},
        f"wasm domain list says {kinds!r}",
    )

    served = curl_host(sc, primary).strip()
    sc.check(served.startswith("ok "), f"{primary} is not serving: {served!r}")
    through_alias = curl_host(sc, alias).strip()
    sc.check(through_alias == served, f"{alias} served {through_alias!r}, not {served!r}")

    headers = curl_host(sc, redirect, "/some/path?q=1", head=True)
    status_line = headers.splitlines()[0] if headers else ""
    location = re.search(r"^location:\s*(\S+)", headers, re.IGNORECASE | re.MULTILINE)
    sc.check(" 301" in status_line, f"{redirect} answered {status_line!r}, not a 301")
    sc.check(
        location is not None and location.group(1) == f"http://{primary}/some/path?q=1",
        f"{redirect} redirected to {location.group(1) if location else None!r}",
    )
    sc.run("nginx -t", timeout=15, label="nginx -t (after the domain changes)")


@scenario("domains_alias_redirect_and_removal")
def scenario_domains(sc: Scenario) -> None:
    """An app answers on an alias and redirects another name; removing the alias stops it.

    Once in place and once on releases: the site of a release app is written
    against ``current``, and a domain change must render it that way too.
    The primary cannot be removed, and a removal takes effect at once.
    """
    sc.run(
        "wasm create -d dom.test -s /root/fixtures/node-app -t nodejs --no-ssl --layout inplace",
        timeout=DEPLOY_TIMEOUT,
        label="wasm create -d dom.test -s /root/fixtures/node-app -t nodejs --no-ssl "
        "--layout inplace",
    )
    check_alias_and_redirect(sc, "dom.test", "alias.dom.test", "old-dom.test")

    sc.run(
        "wasm domain remove dom.test alias.dom.test",
        timeout=60,
        label="wasm domain remove dom.test alias.dom.test",
    )
    after = curl_host(sc, "alias.dom.test").strip()
    sc.check(
        not after.startswith("ok "),
        f"alias.dom.test is still served by dom.test after its removal: {after!r}",
    )
    sc.check(
        curl_host(sc, "dom.test").strip().startswith("ok "),
        "dom.test stopped serving when its alias was removed",
    )
    config = sc.run(
        "cat /etc/nginx/sites-available/dom.test",
        timeout=15,
        label="cat /etc/nginx/sites-available/dom.test (after the removal)",
    )
    sc.check("alias.dom.test" not in config.stdout, "the removed alias is still in the site")

    refused = sc.run(
        "wasm domain remove dom.test dom.test",
        timeout=30,
        check=False,
        label="wasm domain remove dom.test dom.test (the primary)",
    )
    sc.check(refused.returncode == 1, "removing the primary domain was not refused")

    sc.run(
        f"wasm create -d reldom.test -s {RELEASE_URL} -t nodejs --no-ssl --layout releases",
        timeout=DEPLOY_TIMEOUT,
        label=f"wasm create -d reldom.test -s {RELEASE_URL} -t nodejs --no-ssl --layout releases",
    )
    check_alias_and_redirect(sc, "reldom.test", "alias.reldom.test", "old-reldom.test")


# ---------------------------------------------------------------------------
# Upgrade rehearsal: 1.6.5 -> 2.0 (--upgrade mode; not part of the default suite)
# ---------------------------------------------------------------------------
#
# This is the most important pre-release scenario for 2.0: a real 1.6.5
# server, with real applications, upgraded in place. It needs a different
# setup sequence to every scenario above (install the released package
# first, upgrade to the working tree's wheel second), so it runs in its own
# container through `run.py --upgrade` instead of being one more entry in
# SCENARIOS: a plain `run.py` invocation, and CI, keep running exactly the
# scenarios they always have.


#: The stack the Compose health gate scenario deploys, served by git daemon.
COMPOSE_GATE_REPO = "/root/fixtures/compose-gate"
COMPOSE_GATE_URL = "git://127.0.0.1/compose-gate"
COMPOSE_GATE_DOMAIN = "compose-gate.test"
COMPOSE_GATE_ROOT = "/var/www/apps/compose-gate-test"
COMPOSE_GATE_PROJECT = "compose-gate-test"


def compose_gate_web(sc: Scenario, script: str, label: str) -> subprocess.CompletedProcess[str]:
    """Run a shell command inside the stack's web container."""
    container = (
        "$(docker ps -q "
        f"-f label=com.docker.compose.project={COMPOSE_GATE_PROJECT} "
        "-f label=com.docker.compose.service=web)"
    )
    return sc.run(f"docker exec {container} sh -c '{script}'", timeout=30, label=label)


@scenario("compose_update_rolls_back")
def scenario_compose_rollback(sc: Scenario) -> None:
    """A stack whose update does not answer goes back to the images and commit that served.

    The web service is built from the repository, so going back needs the
    image the old container ran, not a rebuild. A file written into a named
    volume before the update must still be there after the way back.
    Skipped, with a note, when Docker is not reachable inside the container.
    """
    probe = sc.run(
        "command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1 "
        "&& echo available || echo unavailable",
        timeout=30,
        check=False,
        label="check whether Docker is reachable inside the container",
    )
    if probe.stdout.strip() != "available":
        sc.evidence.append(
            "NOTE: Docker is not reachable inside the integration container "
            "(tests/integration/Dockerfile.systemd does not install it), so this scenario "
            "is SKIPPED. Its behaviour is covered with a fake runner by "
            "tests/test_inplace_health_gate.py."
        )
        return

    sc.run(
        f"mkdir -p {COMPOSE_GATE_REPO}/html && cd {COMPOSE_GATE_REPO} && "
        "printf 'FROM nginx:alpine\\nCOPY html /usr/share/nginx/html\\n' > Dockerfile && "
        "printf 'gate v1' > html/index.html && "
        'printf \'services:\\n  web:\\n    build: .\\n    ports:\\n      - "18091:80"\\n'
        "    volumes:\\n      - data:/data\\nvolumes:\\n  data: {}\\n' > docker-compose.yml && "
        "git init -q && git config user.email wasm-it@example.com && "
        "git config user.name 'WASM Integration' && git add -A && git commit -q -m 'gate v1'",
        timeout=30,
        label="(fixture repo) a stack whose web image is built from the repository",
    )
    first = sc.run(
        f"git -C {COMPOSE_GATE_REPO} rev-parse HEAD", timeout=15, label="the first commit"
    ).stdout.strip()

    create = (
        f"wasm create -d {COMPOSE_GATE_DOMAIN} -s {COMPOSE_GATE_URL} -t docker-compose --no-ssl"
    )
    sc.run(create, timeout=DEPLOY_TIMEOUT, label=create)
    page = sc.run(
        f"curl -sS -H 'Host: {COMPOSE_GATE_DOMAIN}' http://127.0.0.1/",
        timeout=30,
        label=f"curl -H 'Host: {COMPOSE_GATE_DOMAIN}' http://127.0.0.1/",
    )
    sc.check("gate v1" in page.stdout, f"the stack did not serve v1: {page.stdout!r}")
    compose_gate_web(sc, "echo kept > /data/marker", "write a marker into the named volume")

    # nginx refuses to start on a broken configuration: the container exits.
    sc.run(
        f"cd {COMPOSE_GATE_REPO} && printf 'gate v2' > html/index.html && "
        "printf 'FROM nginx:alpine\\nCOPY html /usr/share/nginx/html\\n"
        "RUN echo broken > /etc/nginx/conf.d/broken.conf\\n' > Dockerfile && "
        "git add -A && git commit -q -m 'gate v2: nginx cannot start'",
        timeout=30,
        label="(fixture repo) commit a version whose container cannot start",
    )
    update = sc.run(
        f"wasm update {COMPOSE_GATE_DOMAIN}",
        timeout=DEPLOY_TIMEOUT,
        check=False,
        label=f"wasm update {COMPOSE_GATE_DOMAIN}",
    )
    output = update.stdout + update.stderr
    sc.check(update.returncode != 0, "an update that does not answer must fail")
    sc.check(
        "did not pass its health check" in output and "running again" in output,
        f"the update did not say it went back: {output!r}",
    )

    page = sc.run(
        f"curl -sS -H 'Host: {COMPOSE_GATE_DOMAIN}' http://127.0.0.1/",
        timeout=30,
        label=f"curl -H 'Host: {COMPOSE_GATE_DOMAIN}' http://127.0.0.1/",
    )
    sc.check("gate v1" in page.stdout, f"v1 is not serving again: {page.stdout!r}")
    marker = compose_gate_web(sc, "cat /data/marker", "read the marker back from the volume")
    sc.check(marker.stdout.strip() == "kept", f"the named volume lost its data: {marker.stdout!r}")
    head = sc.run(
        f"git -C {COMPOSE_GATE_ROOT} rev-parse HEAD", timeout=15, label="the tree's commit"
    )
    sc.check(head.stdout.strip() == first, f"the tree is not back on v1: {head.stdout!r}")
    kept = sc.run(
        f"docker image ls --format '{{{{.Repository}}}}:{{{{.Tag}}}}' {COMPOSE_GATE_PROJECT}-web",
        timeout=15,
        label=f"docker image ls {COMPOSE_GATE_PROJECT}-web",
    )
    sc.check(
        f"{COMPOSE_GATE_PROJECT}-web:wasm-previous" in kept.stdout,
        f"the image that served was not kept: {kept.stdout!r}",
    )
    row = sc.run(
        store_query(
            "SELECT status FROM deployments WHERE domain = 'compose-gate.test' "
            "ORDER BY id DESC LIMIT 1"
        ),
        timeout=15,
        label="SELECT the update's history row",
    )
    sc.check(row.stdout.strip() == "failed", f"the update's row: {row.stdout!r}")


@dataclass
class UpgradeApp:
    """One application the upgrade rehearsal deploys with 1.6.5 and re-checks after 2.0."""

    label: str
    domain: str
    app_name: str
    root: str
    #: Static sites have no process and therefore no systemd unit
    #: (deployers/static.py: create_service "No service needed").
    has_unit: bool = True

    @property
    def unit(self) -> str:
        return self.app_name


def _app_tree_checksums(sc: Scenario, root: str, label: str) -> str:
    """Sorted sha256sum of every regular file under root, node_modules and .git excluded."""
    return sc.run(
        f"find {root} -type f -not -path '*/node_modules/*' -not -path '*/node_modules' "
        "-not -path '*/.git/*' -not -path '*/.git' "
        "-print0 | sort -z | xargs -0 sha256sum",
        timeout=60,
        label=label,
    ).stdout


def _app_tree_ownership(sc: Scenario, root: str, label: str) -> str:
    """user:group:mode for every entry under root, node_modules and .git excluded."""
    return sc.run(
        f"find {root} -not -path '*/node_modules/*' -not -path '*/node_modules' "
        "-not -path '*/.git/*' -not -path '*/.git' "
        "-printf '%u:%g:%m %P\\n' | sort",
        timeout=30,
        label=label,
    ).stdout


def _unified_diff(before: str, after: str, label: str) -> str:
    """A unified diff between two snapshots, or an explicit statement that there is none."""
    diff = "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=f"1.6.5/{label}",
            tofile=f"2.0/{label}",
        )
    )
    return diff if diff else f"(no differences: {label})"


def _snapshot_app(sc: Scenario, app: UpgradeApp, when: str) -> dict[str, str]:
    """Capture the nginx site, the unit (if any), ownership and checksums of one app."""
    nginx = sc.run(
        f"cat /etc/nginx/sites-available/{app.domain}",
        timeout=15,
        label=f"[{when}] cat /etc/nginx/sites-available/{app.domain}",
    ).stdout
    unit = ""
    if app.has_unit:
        unit = sc.run(
            f"systemctl cat {app.unit}",
            timeout=15,
            label=f"[{when}] systemctl cat {app.unit}",
        ).stdout
    ownership = _app_tree_ownership(sc, app.root, f"[{when}] ownership of {app.root}")
    checksums = _app_tree_checksums(
        sc, app.root, f"[{when}] sha256sum of {app.root} (node_modules excluded)"
    )
    return {"nginx": nginx, "unit": unit, "ownership": ownership, "checksums": checksums}


def _store_apps_snapshot(sc: Scenario, label: str) -> str:
    """A dump of every app row's columns shared by schema v3 (1.6.5) and v8 (2.0)."""
    return sc.run(store_query(APPS_SNAPSHOT_QUERY), timeout=15, label=label).stdout


def run_upgrade_rehearsal(sc: Scenario, wheel: Path) -> None:
    """Deploy with 1.6.5, upgrade to the local 2.0 wheel in place, and check every promise.

    Deploys a Node app with an uploads directory and an in-place ``.env``, a
    static site served on the apex and ``www``, and - only when Docker is
    reachable inside the container - a Docker Compose app, all with the
    released 1.6.5 CLI. Snapshots nginx, the unit, ownership and a checksum
    of each application's tree, and the store. Upgrades the very same venv to
    the wheel built from this working tree, exactly as ``pip install
    --upgrade`` would on a real server. Then checks, with real commands
    against real nginx, real systemd and the real store: nothing was
    converted implicitly, every application still serves, its tree and
    configuration are byte-identical unless the change is one
    docs/UPGRADING-2.0.md documents, and the store migrated without losing a
    row. Finally it exercises what 2.0 adds for a 1.x application - `wasm
    update` still works in place, and `wasm app migrate` moves it onto the
    release layout keeping its uploads - and starts the console for the
    first time on this "upgraded" server.

    Args:
        sc: Scenario the rehearsal runs its commands and checks through.
        wheel: Path to the wheel built from the working tree, on the host.
    """
    container = sc.container

    apps = [
        UpgradeApp("node", UPGRADE_NODE_DOMAIN, UPGRADE_NODE_APP, UPGRADE_NODE_ROOT),
        UpgradeApp(
            "static", UPGRADE_STATIC_DOMAIN, UPGRADE_STATIC_APP, UPGRADE_STATIC_ROOT, has_unit=False
        ),
    ]

    # --- Deploy every application with the released 1.6.5 CLI --------------

    sc.run(
        f"wasm create -d {UPGRADE_NODE_DOMAIN} -s /root/fixtures/node-app -t nodejs --no-ssl",
        timeout=DEPLOY_TIMEOUT,
        label=f"[1.6.5] wasm create -d {UPGRADE_NODE_DOMAIN} -s /root/fixtures/node-app "
        "-t nodejs --no-ssl",
    )
    page = sc.run(
        f"curl -sS -H 'Host: {UPGRADE_NODE_DOMAIN}' http://127.0.0.1/",
        timeout=30,
        label=f"[1.6.5] curl -H 'Host: {UPGRADE_NODE_DOMAIN}' http://127.0.0.1/",
    )
    sc.check(page.stdout.strip() == "ok 1", f"expected 'ok 1', got {page.stdout!r}")
    sc.check(
        sc.run(
            f"test -f {UPGRADE_NODE_ROOT}/.env && echo present",
            timeout=15,
            check=False,
            label=f"[1.6.5] test -f {UPGRADE_NODE_ROOT}/.env",
        ).stdout.strip()
        == "present",
        ".env was not created from .env.example on deploy",
    )
    upload = sc.run(
        f"curl -sS -H 'Host: {UPGRADE_NODE_DOMAIN}' --data-binary 'pre-upgrade-upload' "
        "http://127.0.0.1/upload",
        timeout=30,
        label=f"[1.6.5] curl -H 'Host: {UPGRADE_NODE_DOMAIN}' --data-binary ... http://127.0.0.1/upload",
    )
    sc.check("uploaded" in upload.stdout, f"upload did not succeed: {upload.stdout!r}")

    sc.run(
        f"wasm create -d {UPGRADE_STATIC_DOMAIN} -s /root/fixtures/static-site -t static "
        "--no-ssl --www",
        timeout=DEPLOY_TIMEOUT,
        label=f"[1.6.5] wasm create -d {UPGRADE_STATIC_DOMAIN} -s /root/fixtures/static-site "
        "-t static --no-ssl --www",
    )
    apex = sc.run(
        f"curl -sS -H 'Host: {UPGRADE_STATIC_DOMAIN}' http://127.0.0.1/",
        timeout=30,
        label=f"[1.6.5] curl -H 'Host: {UPGRADE_STATIC_DOMAIN}' http://127.0.0.1/",
    )
    sc.check(
        "WASM Integration Static Fixture" in apex.stdout,
        f"the apex did not serve the fixture: {apex.stdout!r}",
    )
    www = sc.run(
        f"curl -sS -H 'Host: www.{UPGRADE_STATIC_DOMAIN}' http://127.0.0.1/",
        timeout=30,
        label=f"[1.6.5] curl -H 'Host: www.{UPGRADE_STATIC_DOMAIN}' http://127.0.0.1/",
    )
    sc.check(
        "WASM Integration Static Fixture" in www.stdout,
        f"--www did not also serve the fixture on 1.6.5: {www.stdout!r}",
    )

    docker_probe = sc.run(
        "command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1 "
        "&& echo available || echo unavailable",
        timeout=30,
        check=False,
        label="check whether Docker is reachable inside the container",
    )
    docker_available = docker_probe.stdout.strip() == "available"
    if docker_available:
        sc.run(
            "mkdir -p /root/fixtures/compose-app/html && "
            "cat > /root/fixtures/compose-app/docker-compose.yml <<'EOF'\n"
            "services:\n"
            "  web:\n"
            "    image: nginx:alpine\n"
            "    ports:\n"
            '      - "18090:80"\n'
            "    volumes:\n"
            "      - ./html:/usr/share/nginx/html:ro\n"
            "EOF\n"
            "printf '<h1>compose-ok</h1>' > /root/fixtures/compose-app/html/index.html",
            timeout=30,
            label="write the docker-compose fixture (Docker is available)",
        )
        sc.run(
            f"wasm create -d {UPGRADE_COMPOSE_DOMAIN} -s /root/fixtures/compose-app "
            "-t docker-compose --no-ssl",
            timeout=DEPLOY_TIMEOUT,
            label=f"[1.6.5] wasm create -d {UPGRADE_COMPOSE_DOMAIN} -s /root/fixtures/compose-app "
            "-t docker-compose --no-ssl",
        )
        compose_page = sc.run(
            f"curl -sS -H 'Host: {UPGRADE_COMPOSE_DOMAIN}' http://127.0.0.1/",
            timeout=30,
            label=f"[1.6.5] curl -H 'Host: {UPGRADE_COMPOSE_DOMAIN}' http://127.0.0.1/",
        )
        sc.check(
            "compose-ok" in compose_page.stdout,
            f"the docker-compose app did not serve: {compose_page.stdout!r}",
        )
        apps.append(
            UpgradeApp(
                "docker-compose", UPGRADE_COMPOSE_DOMAIN, UPGRADE_COMPOSE_APP, UPGRADE_COMPOSE_ROOT
            )
        )
    else:
        sc.evidence.append(
            "NOTE: Docker is not reachable inside the integration container "
            "(tests/integration/Dockerfile.systemd does not install it), so the "
            "docker-compose leg of the upgrade rehearsal is SKIPPED, as instructed. "
            "The deploy, checksum, ownership, nginx/unit diff, `wasm status` and "
            "serving checks below run for the Node and static-site applications only."
        )

    # --- Snapshot everything before touching the package --------------------

    before_snapshots = {app.label: _snapshot_app(sc, app, "1.6.5") for app in apps}
    before_apps_rows = _store_apps_snapshot(sc, "[1.6.5] SELECT every app row's shared columns")
    sc.run(
        f"sha256sum {WASM_DB}",
        timeout=15,
        label="[1.6.5] sha256sum of the store (recorded as evidence; the migration below "
        "rewrites the file, so this is not compared against the post-upgrade checksum)",
    )

    # --- Upgrade -------------------------------------------------------------

    upgrade_wasm_to_wheel(container, wheel)
    version_after = sc.run("wasm --version", timeout=30, label="[2.0] wasm --version").stdout

    listing = sc.run("wasm list", timeout=30, label="[2.0] wasm list")
    for app in apps:
        sc.check(
            app.domain in listing.stdout,
            f"wasm list does not mention {app.domain}: {listing.stdout!r}",
        )

    health = sc.run("wasm health", timeout=60, check=False, label="[2.0] wasm health")
    sc.check(
        health.returncode == 0,
        f"wasm health reported issues after the upgrade:\n{health.stdout}\n{health.stderr}",
    )

    for app in apps:
        status = sc.run(
            f"wasm status {app.domain}", timeout=30, label=f"[2.0] wasm status {app.domain}"
        )
        sc.check(status.returncode == 0, f"wasm status {app.domain} failed after the upgrade")

    # --- Every application still serves, unchanged ---------------------------

    page = sc.run(
        f"curl -sS -H 'Host: {UPGRADE_NODE_DOMAIN}' http://127.0.0.1/",
        timeout=30,
        label=f"[2.0] curl -H 'Host: {UPGRADE_NODE_DOMAIN}' http://127.0.0.1/",
    )
    sc.check(
        page.stdout.strip() == "ok 1",
        f"the node app stopped serving after the upgrade: {page.stdout!r}",
    )
    apex = sc.run(
        f"curl -sS -H 'Host: {UPGRADE_STATIC_DOMAIN}' http://127.0.0.1/",
        timeout=30,
        label=f"[2.0] curl -H 'Host: {UPGRADE_STATIC_DOMAIN}' http://127.0.0.1/",
    )
    sc.check(
        "WASM Integration Static Fixture" in apex.stdout,
        f"the apex stopped serving after the upgrade: {apex.stdout!r}",
    )
    www = sc.run(
        f"curl -sS -H 'Host: www.{UPGRADE_STATIC_DOMAIN}' http://127.0.0.1/",
        timeout=30,
        label=f"[2.0] curl -H 'Host: www.{UPGRADE_STATIC_DOMAIN}' http://127.0.0.1/",
    )
    sc.check(
        "WASM Integration Static Fixture" in www.stdout,
        f"www stopped serving after the upgrade: {www.stdout!r}",
    )
    if docker_available:
        compose_page = sc.run(
            f"curl -sS -H 'Host: {UPGRADE_COMPOSE_DOMAIN}' http://127.0.0.1/",
            timeout=30,
            label=f"[2.0] curl -H 'Host: {UPGRADE_COMPOSE_DOMAIN}' http://127.0.0.1/",
        )
        sc.check(
            "compose-ok" in compose_page.stdout,
            f"the docker-compose app stopped serving after the upgrade: {compose_page.stdout!r}",
        )

    # --- nginx, units and app trees: unchanged, or print the diff verbatim ---

    after_snapshots = {app.label: _snapshot_app(sc, app, "2.0") for app in apps}
    for app in apps:
        before = before_snapshots[app.label]
        after = after_snapshots[app.label]
        for key in ("nginx", "unit", "ownership", "checksums"):
            if key == "unit" and not app.has_unit:
                continue
            diff = _unified_diff(before[key], after[key], f"{app.label}/{key}")
            sc.evidence.append(f"$ diff {app.label}/{key} (1.6.5 vs 2.0)\n{diff}")
            sc.check(
                not diff.startswith("---"),
                f"{app.label}'s {key} changed across the upgrade, and that is not one of the "
                f"changes docs/UPGRADING-2.0.md documents:\n{diff}",
            )

    # --- The store migrated: schema version, rows intact, layout=inplace ----

    schema = sc.run(
        store_query("SELECT MAX(version) FROM schema_version"),
        timeout=15,
        label="[2.0] SELECT MAX(version) FROM schema_version",
    )
    sc.check(
        schema.stdout.strip() == "8", f"the store did not migrate to schema v8: {schema.stdout!r}"
    )

    after_apps_rows = _store_apps_snapshot(sc, "[2.0] SELECT every app row's shared columns")
    sc.check(
        after_apps_rows == before_apps_rows,
        "app rows changed across the upgrade in a column common to both schemas:\n"
        f"before:\n{before_apps_rows}\nafter:\n{after_apps_rows}",
    )

    layouts = sc.run(
        store_query("SELECT domain || '=' || layout FROM apps ORDER BY domain"),
        timeout=15,
        label="[2.0] SELECT domain, layout FROM apps ORDER BY domain",
    )
    for app in apps:
        sc.check(
            f"{app.domain}=inplace" in layouts.stdout.split(),
            f"{app.domain} was not migrated onto layout=inplace: {layouts.stdout!r}",
        )

    # --- `wasm update` still works in place on a 1.x application -------------

    sc.run(
        "cd /root/fixtures/node-app && echo 2 > VERSION && "
        "git add VERSION && git commit -q -m 'bump version to 2 (post-upgrade)'",
        timeout=30,
        label="(fixture repo) echo 2 > VERSION; git commit",
    )
    sc.run(
        f"wasm update {UPGRADE_NODE_DOMAIN}",
        timeout=DEPLOY_TIMEOUT,
        label=f"[2.0] wasm update {UPGRADE_NODE_DOMAIN}",
    )
    page = sc.run(
        f"curl -sS -H 'Host: {UPGRADE_NODE_DOMAIN}' http://127.0.0.1/",
        timeout=30,
        label=f"[2.0] curl -H 'Host: {UPGRADE_NODE_DOMAIN}' http://127.0.0.1/ (after wasm update)",
    )
    sc.check(
        page.stdout.strip() == "ok 2", f"expected 'ok 2' after the update, got {page.stdout!r}"
    )

    upload_after = sc.run(
        f"cat {UPGRADE_NODE_ROOT}/uploads/upload.txt",
        timeout=15,
        check=False,
        label=f"cat {UPGRADE_NODE_ROOT}/uploads/upload.txt (after wasm update)",
    )
    sc.check(
        upload_after.returncode == 0 and "pre-upgrade-upload" in upload_after.stdout,
        "the pre-upgrade upload did not survive `wasm update`",
    )
    sc.check(
        sc.run(
            f"test -f {UPGRADE_NODE_ROOT}/.env && echo present",
            timeout=15,
            check=False,
            label=f"test -f {UPGRADE_NODE_ROOT}/.env (after wasm update)",
        ).stdout.strip()
        == "present",
        ".env did not survive `wasm update`",
    )
    layout_after_update = sc.run(
        store_query("SELECT layout FROM apps WHERE domain = 'upg-node.test'"),
        timeout=15,
        label="[2.0] SELECT layout FROM apps WHERE domain = upg-node.test (after wasm update)",
    )
    sc.check(
        layout_after_update.stdout.strip() == "inplace",
        f"wasm update moved the in-place app onto releases by itself: {layout_after_update.stdout!r}",
    )

    # --- `--dry-run app migrate` changes nothing ------------------------------

    before_listing = sc.run(
        f"ls -A {UPGRADE_NODE_ROOT}",
        timeout=15,
        label=f"ls -A {UPGRADE_NODE_ROOT} (before the migration rehearsal)",
    ).stdout
    sc.run(
        f"wasm --dry-run app migrate {UPGRADE_NODE_DOMAIN}",
        timeout=60,
        label=f"[2.0] wasm --dry-run app migrate {UPGRADE_NODE_DOMAIN}",
    )
    after_listing = sc.run(
        f"ls -A {UPGRADE_NODE_ROOT}",
        timeout=15,
        label=f"ls -A {UPGRADE_NODE_ROOT} (after the migration rehearsal)",
    ).stdout
    sc.check(
        after_listing == before_listing,
        "the --dry-run migration rehearsal changed the app directory",
    )

    # --- `app migrate --yes` moves it onto releases, keeping uploads and serving --

    sc.run(
        f"wasm app migrate {UPGRADE_NODE_DOMAIN} --yes",
        timeout=DEPLOY_TIMEOUT,
        label=f"[2.0] wasm app migrate {UPGRADE_NODE_DOMAIN} --yes",
    )
    tree = sc.run(
        f"ls -A {UPGRADE_NODE_ROOT}; readlink {UPGRADE_NODE_ROOT}/current "
        f"{UPGRADE_NODE_ROOT}/current/uploads {UPGRADE_NODE_ROOT}/current/.env; "
        f"cat {UPGRADE_NODE_ROOT}/shared/uploads/upload.txt",
        timeout=15,
        label="the migrated tree: entries, links and the upload in shared/",
    )
    for expected in ("current", "releases", "shared", "../../shared/uploads", "../../shared/.env"):
        sc.check(
            expected in tree.stdout.split(),
            f"{expected} missing after the migration: {tree.stdout!r}",
        )
    sc.check("pre-upgrade-upload" in tree.stdout, "the pre-upgrade upload is not in shared/uploads")

    page = sc.run(
        f"curl -sS -H 'Host: {UPGRADE_NODE_DOMAIN}' http://127.0.0.1/",
        timeout=30,
        label=f"[2.0] curl -H 'Host: {UPGRADE_NODE_DOMAIN}' http://127.0.0.1/ (after app migrate)",
    )
    sc.check(
        page.stdout.strip() == "ok 2", f"expected 'ok 2' after the migration, got {page.stdout!r}"
    )

    layout_after_migrate = sc.run(
        store_query("SELECT layout FROM apps WHERE domain = 'upg-node.test'"),
        timeout=15,
        label="[2.0] SELECT layout FROM apps WHERE domain = upg-node.test (after app migrate --yes)",
    )
    sc.check(
        layout_after_migrate.stdout.strip() == "releases",
        f"the store still says inplace after app migrate: {layout_after_migrate.stdout!r}",
    )

    # --- `wasm web start` and the console load --------------------------------

    sc.run("wasm web start --daemon", timeout=60, label="[2.0] wasm web start --daemon")
    try:
        health_probe: subprocess.CompletedProcess[str] | None = None
        deadline = time.time() + 30
        while time.time() < deadline:
            health_probe = sc.run(
                f"curl -sS -o /dev/null -w '%{{http_code}}' {PANEL_URL}/health",
                timeout=15,
                check=False,
                label=f"curl -o /dev/null -w '%{{http_code}}' {PANEL_URL}/health",
            )
            if health_probe.stdout.strip() == "200":
                break
            time.sleep(1)
        sc.check(
            health_probe is not None and health_probe.stdout.strip() == "200",
            f"panel /health did not return 200 within 30s: "
            f"{health_probe.stdout if health_probe else None!r}",
        )

        status, headers, html = http_head_and_body(sc, "/")
        sc.check(status == 200, f"GET / answered {status}")
        sc.check('<div id="root">' in html, f"GET / is not the console: {html[:200]!r}")
        csp = headers.get("content-security-policy", "")
        sc.check(
            csp.startswith(CONSOLE_CSP) and "unsafe-inline" not in csp,
            f"GET / is not served under the strict policy: {csp!r}",
        )

        session_status, _, session_body = http_head_and_body(sc, "/api/auth/session")
        sc.check(session_status == 200, f"GET /api/auth/session answered {session_status}")
        session = json.loads(session_body)
        sc.check(
            session.get("authenticated") is False,
            f"/api/auth/session did not say authenticated=false for an anonymous caller: "
            f"{session_body!r}",
        )
    finally:
        sc.run("wasm web stop", timeout=30, check=False, label="[2.0] wasm web stop")

    sc.evidence.append(
        f"NOTE: `wasm --version` after the upgrade: {version_after.strip()!r}. This working "
        f"tree's pyproject.toml is still at {UPGRADE_FROM_VERSION} (not yet bumped for the 2.0 "
        "release - see scripts/release.py and the Releasing section of CLAUDE.md), so the "
        "version string genuinely does not change across this rehearsal even though the code "
        "does; see upgrade_wasm_to_wheel's docstring for how the upgrade step compensates."
    )


def run_upgrade_mode(wheel: Path, *, keep: bool) -> int:
    """Run the 1.6.5 -> 2.0 upgrade rehearsal (see :func:`run_upgrade_rehearsal`) end to end.

    Args:
        wheel: Path to the wheel built from the working tree, on the host.
        keep: Do not remove the container when done.

    Returns:
        0 if the rehearsal passed, 1 otherwise.
    """
    started_at = time.monotonic()
    name = random_container_name()
    sc = Scenario(container=name)
    failed = False
    error: str | None = None
    setup_error: str | None = None

    try:
        try:
            start_container(name)
            wait_for_systemd(name)
            install_fixtures(name)
            install_wasm_from_pypi(name, UPGRADE_FROM_VERSION)
        except HarnessError as exc:
            setup_error = str(exc)
        else:
            print("\n=== upgrade_1_6_5_to_2_0 ===")
            try:
                run_upgrade_rehearsal(sc, wheel)
            except AssertionError as exc:
                failed = True
                error = str(exc)
            except HarnessError as exc:
                failed = True
                error = str(exc)
            else:
                print("PASS: upgrade_1_6_5_to_2_0")
    finally:
        if sc.evidence:
            print("--- evidence ---")
            print("\n\n".join(sc.evidence))
        if keep:
            print(f"\n[teardown] --keep given, leaving container {name} running")
        else:
            print(f"\n[teardown] removing container {name}")
            remove_container(name)

    elapsed = time.monotonic() - started_at
    if setup_error is not None:
        print(f"\n[setup] FAILED before the rehearsal ran: {setup_error}")
        print(f"\n[summary] 0/0 scenario(s) run, setup failed, {elapsed:.1f}s total")
        return 1
    if failed:
        print(f"FAIL: upgrade_1_6_5_to_2_0: {error}")
    print(f"\n[summary] 1 scenario(s) run, {1 if failed else 0} failure(s), {elapsed:.1f}s total")
    return 1 if failed else 0


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def random_container_name() -> str:
    return f"wasm-it-{secrets.token_hex(4)}"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--keep", action="store_true", help="Do not remove the container when done."
    )
    parser.add_argument(
        "--scenario",
        action="append",
        default=None,
        metavar="NAME",
        help="Run only scenarios whose name contains this substring. May repeat.",
    )
    parser.add_argument(
        "--skip-wheel-build",
        action="store_true",
        help="Reuse the newest wheel already in the working directory instead of rebuilding.",
    )
    parser.add_argument(
        "--skip-image-build",
        action="store_true",
        help=f"Reuse the existing {IMAGE_TAG} image instead of rebuilding it.",
    )
    parser.add_argument(
        "--upgrade",
        action="store_true",
        help=(
            "Run only the 1.6.5 -> 2.0 upgrade rehearsal, in its own container, instead of "
            "the regular scenario suite. --scenario is ignored when this is given."
        ),
    )
    return parser.parse_args()


def selected_scenarios(names: list[str] | None) -> list[tuple[str, ScenarioFn]]:
    if not names:
        return list(SCENARIOS)
    selected = [(name, fn) for name, fn in SCENARIOS if any(pattern in name for pattern in names)]
    if not selected:
        raise HarnessError(f"no scenario matches {names!r}; available: {[n for n, _ in SCENARIOS]}")
    return selected


def main() -> int:
    args = parse_args()
    started_at = time.monotonic()

    workdir = INTEGRATION_DIR / ".build"
    workdir.mkdir(exist_ok=True)

    if args.skip_wheel_build:
        wheels = sorted((workdir / "dist").glob("*.whl"))
        if not wheels:
            raise HarnessError("--skip-wheel-build given but no wheel exists yet")
        wheel = wheels[-1]
        print(f"[setup] reusing existing wheel {wheel.name}")
    else:
        wheel = build_wheel(workdir)

    if not args.skip_image_build:
        build_image()
    else:
        print(f"[setup] reusing existing image {IMAGE_TAG}")

    if args.upgrade:
        return run_upgrade_mode(wheel, keep=args.keep)

    name = random_container_name()
    failures = 0
    attempted = 0
    setup_error: str | None = None

    try:
        try:
            start_container(name)
            wait_for_systemd(name)
            install_wasm(name, wheel)
            install_fixtures(name)
            to_run = selected_scenarios(args.scenario)
        except HarnessError as exc:
            setup_error = str(exc)
        else:
            print(f"\n[scenarios] running {len(to_run)} scenario(s): {[n for n, _ in to_run]}")

            for sc_name, fn in to_run:
                print(f"\n=== {sc_name} ===")
                attempted += 1
                sc = Scenario(container=name)
                try:
                    fn(sc)
                except AssertionError as exc:
                    failures += 1
                    print(f"FAIL: {sc_name}: {exc}")
                except HarnessError as exc:
                    failures += 1
                    print(f"FAIL: {sc_name} (command error): {exc}")
                else:
                    print(f"PASS: {sc_name}")
                finally:
                    if sc.evidence:
                        print("--- evidence ---")
                        print("\n\n".join(sc.evidence))
    finally:
        if args.keep:
            print(f"\n[teardown] --keep given, leaving container {name} running")
        else:
            print(f"\n[teardown] removing container {name}")
            remove_container(name)

    elapsed = time.monotonic() - started_at
    if setup_error is not None:
        print(f"\n[setup] FAILED before any scenario ran: {setup_error}")
        print(f"\n[summary] 0/0 scenario(s) run, setup failed, {elapsed:.1f}s total")
        return 1

    print(f"\n[summary] {attempted} scenario(s) run, {failures} failure(s), {elapsed:.1f}s total")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
