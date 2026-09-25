#!/usr/bin/env python3
"""
Run the real WASM panel against a seeded, sandboxed machine.

This is the backend the console is developed and tested against: the real
FastAPI application from :func:`wasm.web.server.create_app`, served by uvicorn
so its lifespan runs and the ``/events`` stream and the WebSockets work, over
a store seeded by :func:`tests.panel_factory.seed_console_state` - running,
stopped, failed and static applications, deployments with captured build
logs, sites, certificates, databases, backups, cron jobs and a job history.

Nothing touches the machine it runs on:

- every path WASM reads or writes (the store, the config file, the panel's
  secrets and sessions, backups, systemd units, nginx sites, certificates) is
  redirected into a temporary directory, removed on exit;
- a :class:`SandboxFileSystem` refuses any write that would still land
  outside that directory;
- a :class:`ConsoleRunner` answers ``systemctl``, ``journalctl``, ``nginx``
  and ``certbot`` from an in-memory model of the seeded machine, so starting
  or stopping an application from the console changes what the next status
  query reports, and no process is ever spawned.

It prints exactly one JSON line on stdout once the server accepts
connections, then serves until SIGINT or SIGTERM::

    {"url": "http://127.0.0.1:43127", "token": "...", "totp_secret": null}

Develop the console against it::

    python scripts/console_server.py --port 8080      # terminal 1
    cd panel && npm run dev                           # terminal 2

Vite proxies ``/api``, ``/events``, ``/hooks`` and ``/ws`` to ``VITE_BACKEND``
(default ``http://127.0.0.1:8080``); sign in with the printed token. The
Playwright suite in ``panel/e2e`` starts one of these per worker.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import ipaddress
import json
import os
import shutil
import signal
import socket
import sys
import tarfile
import tempfile
import threading
import time
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from fnmatch import fnmatch
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent

#: Most commands a :class:`ConsoleRunner` remembers. It runs for as long as a
#: developer leaves it up, and an unbounded call log is a slow memory leak.
CALL_HISTORY = 256

#: Seconds uvicorn waits for open connections at shutdown. The event stream
#: never ends by itself, so without a bound a Ctrl+C would wait for the tab.
GRACEFUL_SHUTDOWN_SECONDS = 2


# ---------------------------------------------------------------------------
# Arguments
# ---------------------------------------------------------------------------


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """
    Parse the command line.

    Args:
        argv: Arguments, defaulting to ``sys.argv[1:]``.

    Returns:
        The parsed options.
    """
    parser = argparse.ArgumentParser(
        description="Serve the real WASM panel over a seeded, sandboxed machine."
    )
    parser.add_argument(
        "--host", default="127.0.0.1", help="loopback address to bind (default 127.0.0.1)"
    )
    parser.add_argument(
        "--port", type=int, default=0, help="port to bind; 0 picks a free one (default)"
    )
    parser.add_argument(
        "--keep", action="store_true", help="keep the sandbox directory on exit and print it"
    )
    parser.add_argument(
        "--totp",
        action="store_true",
        help="enable two-factor sign-in and print its secret as totp_secret",
    )
    parser.add_argument(
        "--static-dir",
        type=Path,
        default=None,
        help=(
            "serve the console build from this directory instead of wasm/web/static, "
            "so several builds can be exercised side by side (development only)"
        ),
    )
    args = parser.parse_args(argv)
    try:
        loopback = ipaddress.ip_address(args.host).is_loopback
    except ValueError:
        loopback = args.host == "localhost"
    if not loopback:
        # The token printed on stdout opens a panel with root semantics over the
        # sandbox; it is never meant to be reachable from another machine.
        parser.error(f"--host must be a loopback address, not {args.host}")
    return args


# ---------------------------------------------------------------------------
# The sandbox
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Sandbox:
    """
    Where the sandboxed machine keeps everything.

    Attributes:
        root: The temporary directory holding all of it.
    """

    root: Path

    @property
    def home(self) -> Path:
        """The HOME every per-user path resolves under."""
        return self.root / "home"

    @property
    def etc(self) -> Path:
        """Stands in for ``/etc``."""
        return self.root / "etc"

    @property
    def var(self) -> Path:
        """Stands in for ``/var``."""
        return self.root / "var"

    @property
    def state_dir(self) -> Path:
        """The panel's secrets, sessions and audit log."""
        return self.etc / "wasm"

    @property
    def config_file(self) -> Path:
        """The WASM configuration file."""
        return self.etc / "wasm" / "config.yaml"

    @property
    def store_file(self) -> Path:
        """The SQLite store; job and deploy logs live beside it."""
        return self.var / "lib" / "wasm" / "wasm.db"

    @property
    def systemd_dir(self) -> Path:
        """Stands in for ``/etc/systemd/system``."""
        return self.etc / "systemd" / "system"

    @property
    def backup_dir(self) -> Path:
        """Application backups."""
        return self.var / "backups" / "wasm"

    @property
    def apps_dir(self) -> Path:
        """Stands in for ``/var/www/apps``."""
        return self.var / "www" / "apps"

    def contains(self, path: Path) -> bool:
        """
        Report whether a path lies inside the sandbox.

        Args:
            path: Any path.

        Returns:
            True when it resolves under :attr:`root`.
        """
        try:
            Path(os.path.abspath(path)).relative_to(self.root)
        except ValueError:
            return False
        return True


def prepare_environment(sandbox: Sandbox) -> None:
    """
    Point every per-user path at the sandbox, before WASM is imported.

    ``wasm.core.store`` computes its per-user database path from ``HOME`` at
    import time, so this has to run first.

    Args:
        sandbox: The sandbox.
    """
    for directory in (
        sandbox.home,
        sandbox.state_dir,
        sandbox.systemd_dir,
        sandbox.store_file.parent,
        sandbox.backup_dir,
        sandbox.apps_dir,
        sandbox.var / "log" / "wasm",
    ):
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.environ["HOME"] = str(sandbox.home)
    os.environ["XDG_DATA_HOME"] = str(sandbox.home / ".local" / "share")
    os.environ["XDG_CONFIG_HOME"] = str(sandbox.home / ".config")
    os.environ["XDG_CACHE_HOME"] = str(sandbox.home / ".cache")
    os.environ["WASM_WEB_STATE_DIR"] = str(sandbox.state_dir)
    # The repository's own code, never an installed copy, and tests.panel_factory.
    for entry in (str(REPO), str(REPO / "src")):
        if entry not in sys.path:
            sys.path.insert(0, entry)


def redirect_system_paths(sandbox: Sandbox) -> None:
    """
    Repoint every system path WASM binds at import time into the sandbox.

    Module constants are rebound on the module that reads them; class
    attributes on the class. A path this misses is still covered by
    :class:`SandboxFileSystem` for writes, and is only ever read.

    Args:
        sandbox: The sandbox.
    """
    import wasm.core.config as config_module
    import wasm.core.store as store_module
    import wasm.managers.diagnose as diagnose_module
    import wasm.managers.webserver as webserver_module
    import wasm.monitor.observation_store as observations_module
    import wasm.monitor.timeseries as timeseries_module
    from wasm.managers.backup_manager import BackupManager
    from wasm.managers.backup_scheduler import BackupScheduler
    from wasm.managers.cert_manager import CertManager
    from wasm.managers.cron_manager import CronManager
    from wasm.managers.database.base import BaseDatabaseManager
    from wasm.managers.service_manager import ServiceManager

    etc, var = sandbox.etc, sandbox.var
    config_module.DEFAULT_CONFIG_PATH = sandbox.config_file
    config_module.DEFAULT_APPS_DIR = sandbox.apps_dir
    config_module.DEFAULT_LOG_DIR = var / "log" / "wasm"
    config_module.NGINX_SITES_AVAILABLE = etc / "nginx" / "sites-available"
    config_module.NGINX_SITES_ENABLED = etc / "nginx" / "sites-enabled"
    config_module.APACHE_SITES_AVAILABLE = etc / "apache2" / "sites-available"
    config_module.APACHE_SITES_ENABLED = etc / "apache2" / "sites-enabled"
    config_module.SYSTEMD_DIR = sandbox.systemd_dir

    store_module.DEFAULT_DB_PATH = sandbox.store_file
    store_module.USER_DB_PATH = sandbox.home / ".local" / "share" / "wasm" / "wasm.db"
    timeseries_module.SYSTEM_DB_PATH = var / "lib" / "wasm" / "metrics.db"
    observations_module.SYSTEM_DB_PATH = var / "lib" / "wasm" / "observations.db"
    diagnose_module.NGINX_ERROR_LOG = var / "log" / "nginx" / "error.log"

    for name in ("NGINX_BACKEND", "APACHE_BACKEND"):
        backend = getattr(webserver_module, name, None)
        if backend is None:
            continue
        server = "nginx" if name == "NGINX_BACKEND" else "apache2"
        setattr(
            webserver_module,
            name,
            replace(
                backend,
                sites_available=etc / server / "sites-available",
                sites_enabled=etc / server / "sites-enabled",
            ),
        )

    ServiceManager.SYSTEMD_DIR = sandbox.systemd_dir
    # Only the sandbox: the real /usr/lib/systemd/system would make every
    # seeded unit look shadowed by a system unit, or not, depending on the host.
    ServiceManager.UNIT_SEARCH_DIRS = (sandbox.systemd_dir,)
    CronManager.SYSTEMD_DIR = sandbox.systemd_dir
    BackupScheduler.SYSTEMD_DIR = sandbox.systemd_dir
    BackupManager.DEFAULT_BACKUP_DIR = sandbox.backup_dir
    BaseDatabaseManager.BACKUP_DIR = sandbox.backup_dir / "databases"
    CertManager.LETSENCRYPT_DIR = etc / "letsencrypt"
    CertManager.LIVE_DIR = etc / "letsencrypt" / "live"

    for directory in (
        etc / "nginx" / "sites-available",
        etc / "nginx" / "sites-enabled",
        var / "log" / "nginx",
    ):
        directory.mkdir(parents=True, exist_ok=True)


def write_config(sandbox: Sandbox) -> None:
    """
    Write the WASM configuration the sandboxed machine runs with.

    Args:
        sandbox: The sandbox.
    """
    import yaml

    config = {
        "apps_directory": str(sandbox.apps_dir),
        "webserver": "nginx",
        "service_user": "www-data",
        "service_group": "www-data",
        "ssl": {"enabled": True, "provider": "certbot", "email": "ops@arennalabs.com"},
        "logging": {"level": "info", "file": str(sandbox.var / "log" / "wasm" / "wasm.log")},
        "backup": {"directory": str(sandbox.backup_dir), "max_per_app": 10},
    }
    sandbox.config_file.write_text(yaml.safe_dump(config, sort_keys=True), encoding="utf-8")
    sandbox.config_file.chmod(0o600)


def forbid_real_processes() -> None:
    """
    Make any process spawn that bypasses the runner fail instead of running.

    The runner is the only sanctioned way to start a process (CLAUDE.md rule
    1), and here it is a fake. A code path that spawns through asyncio
    directly would otherwise reach the developer's real journal or systemd.
    """

    async def refuse(*args: Any, **kwargs: Any) -> Any:
        raise OSError(f"console_server runs no real process: {args[:1]!r}")

    asyncio.create_subprocess_exec = refuse  # type: ignore[assignment]
    asyncio.create_subprocess_shell = refuse  # type: ignore[assignment]


def make_sandbox_filesystem(sandbox: Sandbox) -> Any:
    """
    Build the filesystem that writes inside the sandbox and nowhere else.

    Args:
        sandbox: The sandbox.

    Returns:
        A :class:`wasm.core.fs.RealFileSystem` that skips, and reports, any
        change outside the sandbox.
    """
    from wasm.core.fs import RealFileSystem

    class SandboxFileSystem(RealFileSystem):
        """A real filesystem confined to the sandbox."""

        def __init__(self) -> None:
            """Start with nothing skipped."""
            self.skipped: deque[str] = deque(maxlen=CALL_HISTORY)

        def _inside(self, action: str, *paths: Path) -> bool:
            """
            Allow a change only when every path it touches is in the sandbox.

            Args:
                action: What would have happened, for the report.
                paths: The paths the change touches.

            Returns:
                True when the change may go ahead.
            """
            if all(sandbox.contains(path) for path in paths):
                return True
            message = f"skipped {action} outside the sandbox: {', '.join(map(str, paths))}"
            self.skipped.append(message)
            print(f"console_server: {message}", file=sys.stderr, flush=True)
            return False

        def write_text(self, path: Path, content: str, *, mode: int = 0o644) -> None:
            if self._inside("write", path):
                super().write_text(path, content, mode=mode)

        def make_dir(
            self, path: Path, *, mode: int = 0o755, parents: bool = True, exist_ok: bool = True
        ) -> None:
            if self._inside("mkdir", path):
                super().make_dir(path, mode=mode, parents=parents, exist_ok=exist_ok)

        def remove(self, path: Path, *, missing_ok: bool = True) -> None:
            if self._inside("remove", path):
                super().remove(path, missing_ok=missing_ok)

        def remove_tree(self, path: Path) -> None:
            if self._inside("remove_tree", path):
                super().remove_tree(path)

        def move(self, source: Path, destination: Path) -> None:
            if self._inside("move", source, destination):
                super().move(source, destination)

        def copy_tree(self, source: Path, destination: Path) -> None:
            if self._inside("copy_tree", destination):
                super().copy_tree(source, destination)

        def chmod(self, path: Path, mode: int) -> None:
            if self._inside("chmod", path):
                super().chmod(path, mode)

        def symlink(self, target: Path, link: Path) -> None:
            if self._inside("symlink", link):
                super().symlink(target, link)

    return SandboxFileSystem()


# ---------------------------------------------------------------------------
# The machine the runner answers for
# ---------------------------------------------------------------------------


@dataclass
class Unit:
    """
    One systemd unit of the modelled machine.

    Attributes:
        active: ``active``, ``inactive`` or ``failed``.
        enabled: Whether it starts at boot.
        pid: Main PID while active.
        since: When it last changed state.
        restarts: How many times systemd restarted it.
        managed: Whether it is one of WASM's units (listed by ``list-units``).
    """

    active: str
    enabled: bool = True
    pid: int = 0
    since: datetime = field(default_factory=datetime.now)
    restarts: int = 0
    managed: bool = True

    @property
    def sub(self) -> str:
        """The sub-state systemd reports beside the active state."""
        return {"active": "running", "failed": "failed"}.get(self.active, "dead")


#: Programs the modelled machine has on PATH.
INSTALLED_PROGRAMS = (
    "systemctl",
    "journalctl",
    "nginx",
    "certbot",
    "git",
    "node",
    "npm",
    "psql",
    "pg_dump",
    "mysql",
    "mysqldump",
    "redis-cli",
    "tar",
)

#: What each database client prints for ``--version``.
CLIENT_VERSIONS = {
    "psql": "psql (PostgreSQL) 16.4 (Ubuntu 16.4-0ubuntu0.24.04.2)\n",
    "mysql": "mysql  Ver 8.0.39-0ubuntu0.24.04.2 for Linux on x86_64 ((Ubuntu))\n",
    "redis-cli": "redis-cli 7.0.15\n",
}

#: What a Next.js app writes to the journal, for the logs views.
JOURNAL_LINES = (
    "Started {unit}.service - {domain}.",
    "   ▲ Next.js 15.2.4",
    "   - Local:        http://localhost:{port}",
    " ✓ Starting...",
    " ✓ Ready in 412ms",
    "GET / 200 in 38ms",
    "GET /api/health 200 in 3ms",
    "GET /_next/static/chunks/main-app.js 200 in 2ms",
)

#: What a crashing one writes before systemd gives up on it.
FAILED_JOURNAL_LINES = (
    "Started {unit}.service - {domain}.",
    "Error: Cannot find module '/var/www/apps/{domain}/current/.next/standalone/server.js'",
    "    at Module._resolveFilename (node:internal/modules/cjs/loader:1225:15)",
    "{unit}.service: Main process exited, code=exited, status=1/FAILURE",
    "{unit}.service: Failed with result 'exit-code'.",
    "{unit}.service: Scheduled restart job, restart counter is at 5.",
    "{unit}.service: Start request repeated too quickly.",
)


def make_runner(
    units: dict[str, Unit],
    ports: dict[str, int],
    domains: dict[str, str],
    certs: list[str],
    systemd_dir: Path,
) -> Any:
    """
    Build the runner that answers for the modelled machine.

    Args:
        units: Unit name (without ``.service``) to its state; mutated by
            ``systemctl start``, ``stop`` and ``restart``.
        ports: Unit name to the port its application listens on.
        domains: Unit name to its application's domain.
        certs: Domains holding a certificate, for ``certbot certificates``.
        systemd_dir: The sandboxed unit directory, whose cron timers
            ``systemctl list-unit-files`` reports.

    Returns:
        A :class:`wasm.core.runner.FakeRunner` answering from the model.
    """
    from wasm.core.runner import CommandResult, FakeRunner, runuser_prefix

    lock = threading.Lock()

    def unit_of(argument: str) -> str:
        return argument.removesuffix(".service")

    def ok(argv: tuple[str, ...], stdout: str = "", exit_code: int = 0) -> CommandResult:
        return CommandResult(argv=argv, exit_code=exit_code, stdout=stdout, stderr="")

    class ConsoleRunner(FakeRunner):
        """A FakeRunner that behaves like the seeded machine."""

        def __init__(self) -> None:
            """Start with a bounded call history."""
            super().__init__()
            self.calls = deque(maxlen=CALL_HISTORY)  # type: ignore[assignment]
            self.inputs = deque(maxlen=CALL_HISTORY)  # type: ignore[assignment]
            self._stdin = threading.local()
            # What the modelled machine has installed. MongoDB and Docker are
            # deliberately absent, so the console shows an engine to install.
            self.only_knows(*INSTALLED_PROGRAMS)

        def run(self, argv: Sequence[str], **kwargs: Any) -> CommandResult:
            # SQL reaches the database clients on stdin, never in argv.
            self._stdin.value = kwargs.get("input") or ""
            return super().run(argv, **kwargs)

        def _lookup(self, argv: Sequence[str], user: str | None = None) -> CommandResult:
            args = tuple(str(a) for a in argv)
            recorded = (*runuser_prefix(user), *args) if user is not None else args
            self.calls.append(recorded)
            # Answered as the command it runs: switching the account (runuser -u
            # postgres -- psql) asks the same question of the machine.
            program = args[0] if args else ""
            with lock:
                if program == "systemctl":
                    return self._systemctl(args)
                if program == "journalctl":
                    return self._journal(args)
            if program == "nginx":
                if "-v" in args:
                    return CommandResult(args, 0, "", "nginx version: nginx/1.24.0 (Ubuntu)\n")
                return CommandResult(
                    args,
                    0,
                    "",
                    "nginx: the configuration file /etc/nginx/nginx.conf syntax is ok\n"
                    "nginx: configuration file /etc/nginx/nginx.conf test is successful\n",
                )
            if "--version" in args and program in CLIENT_VERSIONS:
                return ok(args, CLIENT_VERSIONS[program])
            if program in ("psql", "mysql", "redis-cli"):
                return ok(args, self._sql(program, getattr(self._stdin, "value", "")))
            if program == "certbot" and args[1:2] == ("certificates",):
                return ok(args, self._certificates())
            if program == "certbot" and "--version" in args:
                return ok(args, "certbot 2.9.0\n")
            return ok(args)

        def _systemctl(self, args: tuple[str, ...]) -> CommandResult:
            verb = next((a for a in args[1:] if not a.startswith("-")), "")
            targets = [a for a in args[2:] if not a.startswith("-") and a != verb]
            if verb == "--version" or "--version" in args:
                return ok(args, "systemd 255 (255.4-1ubuntu8)\n")
            if verb == "list-unit-files":
                return ok(args, self._unit_files(targets))
            if verb == "list-units":
                return ok(args, self._list_units(targets))
            name = unit_of(targets[-1]) if targets else ""
            unit = units.get(name)
            if verb == "is-active":
                state = unit.active if unit else "inactive"
                return ok(args, f"{state}\n", 0 if state == "active" else 3)
            if verb == "is-enabled":
                enabled = unit is not None and unit.enabled
                return ok(args, "enabled\n" if enabled else "disabled\n", 0 if enabled else 1)
            if verb == "show":
                if "-p" in args:
                    return ok(args, "FragmentPath=\n")
                if any(a.startswith("--property=") for a in args):
                    return ok(args, self._cron_properties(targets[-1] if targets else ""))
                return ok(args, self._show(name, unit))
            if unit is not None and verb in ("start", "restart"):
                unit.active, unit.since, unit.pid = "active", datetime.now(), 40000 + len(name)
                return ok(args)
            if unit is not None and verb == "stop":
                unit.active, unit.since, unit.pid = "inactive", datetime.now(), 0
                return ok(args)
            if unit is not None and verb in ("enable", "disable"):
                unit.enabled = verb == "enable"
                return ok(args)
            if verb in ("start", "stop", "restart") and unit is None:
                return CommandResult(
                    args, 5, "", f"Failed to {verb} {targets[-1]}: Unit not found.\n"
                )
            return ok(args)

        @staticmethod
        def _show(name: str, unit: Unit | None) -> str:
            if unit is None:
                return "LoadState=not-found\nActiveState=inactive\nSubState=dead\n"
            since = unit.since.strftime("%a %Y-%m-%d %H:%M:%S UTC")
            return (
                f"Id={name}.service\n"
                "LoadState=loaded\n"
                f"ActiveState={unit.active}\n"
                f"SubState={unit.sub}\n"
                f"MainPID={unit.pid}\n"
                f"MemoryCurrent={96 * 1024 * 1024 if unit.active == 'active' else 0}\n"
                f"ActiveEnterTimestamp={since if unit.active == 'active' else ''}\n"
                f"NRestarts={unit.restarts}\n"
                f"Result={'exit-code' if unit.active == 'failed' else 'success'}\n"
            )

        @staticmethod
        def _sql(program: str, statement: str) -> str:
            if program == "psql" and "pg_database" in statement:
                return (
                    "postgres|UTF8|7553827\n"
                    "arennalabs_production|UTF8|48218931\n"
                    "arennalabs_staging|UTF8|9120563\n"
                )
            if program == "mysql" and "SCHEMATA" in statement:
                return (
                    "information_schema\tutf8mb3\n"
                    "mysql\tutf8mb4\n"
                    "picconia_wp\tutf8mb4\n"
                    "cittek_shop\tutf8mb4\n"
                )
            if program == "redis-cli" and "keyspace" in statement:
                return "# Keyspace\ndb0:keys=1284,expires=210,avg_ttl=0\ndb2:keys=37,expires=0\n"
            if program == "redis-cli" and "databases" in statement:
                return "databases\n16\n"
            return ""

        @staticmethod
        def _unit_files(patterns: list[str]) -> str:
            lines = []
            for path in sorted(systemd_dir.glob("*")):
                if patterns and not any(fnmatch(path.name, p) for p in patterns):
                    continue
                lines.append(f"{path.name} enabled enabled")
            return "\n".join(lines) + "\n"

        @staticmethod
        def _cron_properties(unit: str) -> str:
            now = datetime.now()
            stamp = "%a %Y-%m-%d %H:%M:%S UTC"
            if unit.endswith(".timer"):
                calendar = "*-*-* 02:00:00"
                try:
                    text = (systemd_dir / unit).read_text(encoding="utf-8")
                    calendar = next(
                        line.split("=", 1)[1]
                        for line in text.splitlines()
                        if line.startswith("OnCalendar=")
                    )
                except (OSError, StopIteration):
                    pass
                return (
                    f"TimersCalendar={{ OnCalendar={calendar} ; next_elapse=n/a }}\n"
                    f"LastTriggerUSec={(now - timedelta(hours=9)).strftime(stamp)}\n"
                    f"NextElapseUSecRealtime={(now + timedelta(hours=15)).strftime(stamp)}\n"
                )
            failed = "cleanup" in unit
            return (
                f"ExecMainStatus={1 if failed else 0}\n"
                f"ExecMainExitTimestamp={(now - timedelta(hours=9)).strftime(stamp)}\n"
                f"Result={'exit-code' if failed else 'success'}\n"
            )

        @staticmethod
        def _list_units(patterns: list[str]) -> str:
            lines = ["UNIT LOAD ACTIVE SUB DESCRIPTION"]
            for name, unit in sorted(units.items()):
                if not unit.managed:
                    continue
                if patterns and not any(
                    fnmatch(f"{name}.service", p) or fnmatch(name, p) for p in patterns
                ):
                    continue
                description = domains.get(name, name)
                lines.append(f"{name}.service loaded {unit.active} {unit.sub} {description}")
            return "\n".join(lines) + "\n"

        @staticmethod
        def _journal(args: tuple[str, ...]) -> CommandResult:
            name = ""
            if "-u" in args:
                index = args.index("-u")
                name = unit_of(args[index + 1]) if index + 1 < len(args) else ""
            unit = units.get(name)
            template = FAILED_JOURNAL_LINES if unit and unit.active == "failed" else JOURNAL_LINES
            start = datetime.now() - timedelta(minutes=len(template))
            lines = [
                f"{(start + timedelta(minutes=i)).strftime('%b %d %H:%M:%S')} arenna "
                f"{name or 'systemd'}[{unit.pid if unit and unit.pid else 1}]: "
                + line.format(unit=name, domain=domains.get(name, name), port=ports.get(name, 3000))
                for i, line in enumerate(template)
            ]
            return CommandResult(args, 0, "\n".join(lines) + "\n", "")

        @staticmethod
        def _certificates() -> str:
            blocks = ["Saving debug log to /var/log/letsencrypt/letsencrypt.log", "", "-" * 79]
            blocks.append("Found the following certs:")
            for offset, domain in enumerate(certs):
                expiry = datetime.now() + timedelta(days=12 + offset * 17)
                days = (expiry - datetime.now()).days
                blocks += [
                    f"  Certificate Name: {domain}",
                    "    Serial Number: 4a3f9c2e1b7d",
                    "    Key Type: ECDSA",
                    f"    Domains: {domain} www.{domain}",
                    f"    Expiry Date: {expiry.strftime('%Y-%m-%d %H:%M:%S')}+00:00 "
                    f"(VALID: {days} days)",
                    f"    Certificate Path: /etc/letsencrypt/live/{domain}/fullchain.pem",
                    f"    Private Key Path: /etc/letsencrypt/live/{domain}/privkey.pem",
                ]
            blocks.append("-" * 79)
            return "\n".join(blocks) + "\n"

    return ConsoleRunner()


# ---------------------------------------------------------------------------
# Seeding what the store cannot hold
# ---------------------------------------------------------------------------


def seed_machine(
    sandbox: Sandbox,
) -> tuple[dict[str, Unit], dict[str, int], dict[str, str], list[str]]:
    """
    Seed the store, then the files and units that live beside it.

    Args:
        sandbox: The sandbox.

    Returns:
        The unit model, each unit's port, each unit's domain, and the domains
        holding certificates - what :func:`make_runner` answers from.
    """
    from tests.panel_factory import seed_console_state
    from wasm.core.store import get_store
    from wasm.managers.service_manager import WASM_UNIT_MARKER

    store = get_store(sandbox.store_file)
    state = seed_console_state(store)

    units: dict[str, Unit] = {}
    ports: dict[str, int] = {}
    domains: dict[str, str] = {}
    apps = {app.domain: app for app in store.list_apps()}
    for service in store.list_services():
        app = next((a for a in apps.values() if a.id == service.app_id), None)
        if app is None:
            continue
        failed = app.domain in state.failed_domains
        active = "failed" if failed else ("active" if service.status == "active" else "inactive")
        units[service.name] = Unit(
            active=active,
            enabled=service.enabled,
            pid=41000 + (service.id or 0) if active == "active" else 0,
            since=datetime.now() - timedelta(hours=6, minutes=(service.id or 0) * 7),
            restarts=5 if failed else 0,
        )
        ports[service.name] = service.port or 3000
        domains[service.name] = app.domain
        # The file that makes ServiceManager treat it as a unit WASM owns and
        # that exists, which is what start, stop and restart require.
        (sandbox.systemd_dir / f"{service.name}.service").write_text(
            f"# {WASM_UNIT_MARKER}\n"
            "[Unit]\n"
            f"Description={app.domain}\n\n"
            "[Service]\n"
            f"WorkingDirectory={service.working_directory}\n"
            f"ExecStart={service.command}\n"
            f"Environment=PORT={service.port}\n",
            encoding="utf-8",
        )

    # The database engines, answered as system units WASM does not own.
    for engine, active in (
        ("postgresql", "active"),
        ("mysql", "active"),
        ("redis-server", "inactive"),
    ):
        units[engine] = Unit(
            active=active, pid=900 + len(engine) if active == "active" else 0, managed=False
        )

    seed_backups(sandbox, state.backup_domains + state.static_domains[:1])
    seed_cron(state.domains[0])
    return units, ports, domains, list(state.cert_domains)


def seed_backups(sandbox: Sandbox, domains: list[str]) -> None:
    """
    Write backups the way :class:`~wasm.managers.backup_manager.BackupManager` lists them.

    A metadata file beside a real, tiny archive: the manager only lists a
    backup whose archive is present.

    Args:
        sandbox: The sandbox.
        domains: Domains to give backups to, two each.
    """
    from wasm.core.utils import domain_to_app_name
    from wasm.managers.backup_manager import BackupMetadata

    now = datetime.now()
    for index, domain in enumerate(domains):
        app_name = domain_to_app_name(domain)
        directory = sandbox.backup_dir / app_name
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        for age_days, tags in ((index + 1, ["scheduled"]), (index + 8, ["pre-deploy"])):
            created = now - timedelta(days=age_days, hours=index)
            backup_id = f"{app_name}_{created.strftime('%Y%m%d_%H%M%S')}"
            archive = directory / f"{backup_id}.tar.gz"
            payload = directory / f"{backup_id}.README"
            payload.write_text(f"Seeded backup of {domain}\n", encoding="utf-8")
            with tarfile.open(archive, "w:gz") as tar:
                tar.add(payload, arcname="app/README")
            payload.unlink()
            metadata = BackupMetadata(
                id=backup_id,
                domain=domain,
                app_name=app_name,
                created_at=created.isoformat(),
                size_bytes=archive.stat().st_size,
                app_type="nextjs",
                version="2.0.0",
                description="Nightly backup" if "scheduled" in tags else "Before deploy",
                includes_env=True,
                includes_node_modules=False,
                git_commit="9f2c41a",
                git_branch="main",
                tags=tags,
            )
            (directory / f"{backup_id}.json").write_text(
                json.dumps(metadata.to_dict(), indent=2), encoding="utf-8"
            )


def seed_cron(domain: str) -> None:
    """
    Create cron jobs through :class:`~wasm.managers.cron_manager.CronManager` itself.

    The units are written into the sandboxed systemd directory; enabling the
    timer is answered by the fake runner.

    Args:
        domain: The application the first job belongs to.
    """
    from wasm.managers.cron_manager import CronJob, CronManager

    manager = CronManager()
    for job in (
        CronJob(
            name="sitemap",
            command="/usr/bin/node scripts/sitemap.js",
            schedule="daily",
            user="www-data",
            app_domain=domain,
        ),
        CronJob(
            name="cleanup-tmp",
            command="/usr/bin/find /tmp -name 'upload-*' -mtime +2 -delete",
            schedule="*-*-* 03:30:00",
            user="root",
        ),
    ):
        manager.create_job(job)


# ---------------------------------------------------------------------------
# Serving
# ---------------------------------------------------------------------------


def bind(host: str, port: int) -> socket.socket:
    """
    Bind the listening socket before the server starts.

    Binding here, rather than letting uvicorn do it, is what makes ``--port
    0`` race-free: the port printed is the port held.

    Args:
        host: Loopback address.
        port: Port, or 0 for a free one.

    Returns:
        The listening socket.
    """
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    sock = socket.socket(family, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((host, port))
    sock.listen(128)
    sock.set_inheritable(True)
    return sock


def enable_totp() -> str:
    """
    Turn on two-factor sign-in with a freshly enrolled secret.

    Returns:
        The secret, so a test can compute codes.
    """
    from wasm.core import totp
    from wasm.web.server import get_token_manager

    manager = get_token_manager()
    secret = manager.begin_totp_enrollment()
    if manager.confirm_totp_enrollment(totp.totp_now(secret)) is None:
        raise RuntimeError("two-factor enrolment did not accept its own code")
    return secret


def use_console_build(static_dir: Path) -> None:
    """
    Point the server at a console build other than the committed one.

    Development and E2E only: parallel work on the console builds into private
    directories, because two builds racing into ``wasm/web/static`` would serve
    each other half-written chunks. Production always serves the committed build.

    Args:
        static_dir: A directory holding a Vite build (``index.html`` + ``assets/``).

    Raises:
        SystemExit: When the directory holds no build.
    """
    from wasm.web import server

    static_dir = static_dir.resolve()
    if not (static_dir / "index.html").is_file():
        raise SystemExit(f"{static_dir} holds no console build (no index.html)")
    server.STATIC_DIR = static_dir
    server.ASSETS_DIR = static_dir / "assets"
    server.INDEX_HTML = static_dir / "index.html"


def serve(args: argparse.Namespace, sandbox: Sandbox) -> None:
    """
    Seed the machine and serve the panel until interrupted.

    Args:
        args: The parsed options.
        sandbox: The sandbox, already prepared.
    """
    import uvicorn

    from wasm.core.fs import set_fs
    from wasm.core.runner import set_runner
    from wasm.web.auth import SecurityConfig
    from wasm.web.server import create_app, get_token_manager

    redirect_system_paths(sandbox)
    write_config(sandbox)
    forbid_real_processes()
    set_fs(make_sandbox_filesystem(sandbox))

    # The runner has to exist before seeding (cron enables its timer through
    # it) and needs the seeded units to answer for, so it is built over empty
    # maps that seeding fills in place.
    units: dict[str, Unit] = {}
    ports: dict[str, int] = {}
    domains: dict[str, str] = {}
    certs: list[str] = []
    set_runner(make_runner(units, ports, domains, certs, sandbox.systemd_dir))
    # Managers report progress on stdout, which belongs to the one JSON line
    # the caller parses.
    with contextlib.redirect_stdout(sys.stderr):
        seeded_units, seeded_ports, seeded_domains, seeded_certs = seed_machine(sandbox)
    units.update(seeded_units)
    ports.update(seeded_ports)
    domains.update(seeded_domains)
    certs.extend(seeded_certs)

    config = SecurityConfig(
        host=args.host,
        port=args.port,
        state_dir=sandbox.state_dir,
        # One browser drives every page of the suite from one address; the
        # production limit would throttle the tests, not an attacker.
        rate_limit_requests=100_000,
    )
    if args.static_dir is not None:
        use_console_build(args.static_dir)
    app = create_app(config)
    token = get_token_manager().generate_master_token()
    totp_secret = enable_totp() if args.totp else None

    sock = bind(args.host, args.port)
    host, port = sock.getsockname()[:2]
    display_host = f"[{host}]" if ":" in host else host
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            log_level="warning",
            access_log=False,
            server_header=False,
            timeout_graceful_shutdown=GRACEFUL_SHUTDOWN_SECONDS,
        )
    )

    def announce() -> None:
        """Print the connection details once the server accepts requests."""
        while not server.started and not server.should_exit:
            time.sleep(0.02)
        if server.started:
            line = {
                "url": f"http://{display_host}:{port}",
                "token": token,
                "totp_secret": totp_secret,
            }
            if args.keep:
                line["sandbox"] = str(sandbox.root)
            print(json.dumps(line), flush=True)

    threading.Thread(target=announce, name="announce", daemon=True).start()
    # uvicorn shuts down gracefully on SIGTERM and then re-raises it against
    # the previous handler; the default one would kill the process before
    # main() removes the sandbox. Exiting normally instead runs the cleanup.
    signal.signal(signal.SIGTERM, lambda signum, frame: sys.exit(0))
    server.run(sockets=[sock])


def main(argv: Sequence[str] | None = None) -> int:
    """
    Run the console server.

    Args:
        argv: Arguments, defaulting to ``sys.argv[1:]``.

    Returns:
        The exit status.
    """
    args = parse_args(argv)
    sandbox = Sandbox(Path(tempfile.mkdtemp(prefix="wasm-console-")).resolve())
    try:
        prepare_environment(sandbox)
        serve(args, sandbox)
    finally:
        if args.keep:
            print(f"console_server: sandbox kept at {sandbox.root}", file=sys.stderr)
        else:
            shutil.rmtree(sandbox.root, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
