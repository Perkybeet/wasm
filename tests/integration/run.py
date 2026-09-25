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


@scenario("web_panel_starts_and_answers_health")
def scenario_web_panel(sc: Scenario) -> None:
    """`wasm web start` binds on 127.0.0.1 and GET /health returns 200."""
    sc.run("wasm web start --daemon", timeout=60, label="wasm web start --daemon")

    health: subprocess.CompletedProcess[str] | None = None
    deadline = time.time() + 30
    while time.time() < deadline:
        health = sc.run(
            "curl -sS -o /dev/null -w '%{http_code}' http://127.0.0.1:8080/health",
            timeout=15,
            check=False,
            label="curl -o /dev/null -w '%{http_code}' http://127.0.0.1:8080/health",
        )
        if health.stdout.strip() == "200":
            break
        time.sleep(1)

    sc.check(
        health is not None and health.stdout.strip() == "200",
        f"panel /health did not return 200 within 30s: {health.stdout if health else None!r}",
    )

    body = sc.run(
        "curl -sS http://127.0.0.1:8080/health",
        timeout=15,
        check=False,
        label="curl http://127.0.0.1:8080/health",
    )
    sc.check('"status"' in body.stdout, f"unexpected /health body: {body.stdout!r}")

    sc.run("wasm web stop", timeout=30, check=False, label="wasm web stop")


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
