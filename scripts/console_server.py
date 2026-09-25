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
import re
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
    import wasm.monitor.process_monitor as process_monitor_module
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
    # ProcessMonitor.install_service() writes its unit through
    # wasm.core.utils.write_file(), plain pathlib rather than the fs seam
    # (SandboxFileSystem never sees the call, so it cannot refuse it). Without
    # this, "Install monitor" in the console would write
    # /etc/systemd/system/wasm-monitor.service on whatever machine runs this
    # script - exactly what the module docstring promises never happens.
    process_monitor_module.SYSTEMD_DIR = sandbox.systemd_dir
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
    "systemd-analyze",
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

# ---------------------------------------------------------------------------
# Console: the Databases page needs more than `list_databases` out of the
# fake SQL clients - the detail page (owner, size, table count) and the
# users list run their own queries, and the SQL console needs a query that
# answers with rows instead of nothing. Each engine manager's exact SQL text
# is matched by a fragment unique to that query, so the generic listing
# queries below (kept as they were) still answer everything else.
# ---------------------------------------------------------------------------

#: PostgreSQL databases the demo machine knows about: owner and size in bytes,
#: matching what `arennalabs_production` and `arennalabs_staging` list as.
_PG_DATABASES: dict[str, tuple[str, int]] = {
    "postgres": ("postgres", 7553827),
    "arennalabs_production": ("wasm_app", 48218931),
    "arennalabs_staging": ("wasm_app", 9120563),
}

#: PostgreSQL roles `list_users` reports: name, superuser, createdb, createrole,
#: and (5th column) the databases it may connect to, comma-separated - the same
#: shape `PostgresManager.list_users`'s combined query now answers in one row.
_PG_USERS = (
    "postgres|t|t|t|postgres,arennalabs_production,arennalabs_staging\n"
    "wasm_app|f|f|f|arennalabs_production,arennalabs_staging\n"
    "wasm_readonly|f|f|f|arennalabs_production,arennalabs_staging\n"
)

#: A read the SQL console can run against any PostgreSQL database, pipe-separated
#: the way `psql -t -A` prints it (no header row: see databases.py's docstring).
_PG_DEMO_ROWS = "1024|maria@example.com|129.90\n1025|jon@example.com|54.00\n1026|priya@example.com|312.40\n"

#: The same read, in the structured console's shape: a header row, comma
#: separated, the way `psql --csv` prints it (see execute_query_structured).
_PG_DEMO_ROWS_CSV = (
    "id,email,total\n1024,maria@example.com,129.90\n1025,jon@example.com,54.00\n"
    "1026,priya@example.com,312.40\n"
)

#: MySQL/MariaDB schemas the demo machine knows about: size in bytes, table count.
_MYSQL_DATABASES: dict[str, tuple[int, int]] = {
    "picconia_wp": (52_428_800, 18),
    "cittek_shop": (23_068_672, 11),
}

#: MySQL users `list_users` reports: name, host and (3rd column) the databases
#: it has database-level grants on, comma-separated - the same shape
#: `MySQLManager.list_users`'s combined query now answers in one row.
_MYSQL_USERS = "root\tlocalhost\t\npicconia\t%\tpicconia_wp\ncittek\t%\tcittek_shop\n"

#: The same read as `_PG_DEMO_ROWS`, tab-separated the way `mysql -N -B` prints it.
_MYSQL_DEMO_ROWS = "1024\tmaria@example.com\t129.90\n1025\tjon@example.com\t54.00\n1026\tpriya@example.com\t312.40\n"

#: The same read, in the structured console's shape: a header row, tab
#: separated, the way `mysql -B` (without -N) prints it.
_MYSQL_DEMO_ROWS_HEADERS = (
    "id\temail\ttotal\n1024\tmaria@example.com\t129.90\n1025\tjon@example.com\t54.00\n"
    "1026\tpriya@example.com\t312.40\n"
)

#: Matches the literal a `WHERE datname = '...'` or `WHERE SCHEMA_NAME = '...'`
#: clause quotes, so a canned answer can be specific to the database it was
#: asked about instead of always answering as the first one in the list.
_SQL_LITERAL = re.compile(r"=\s*'([^']*)'")


def _sql_literal(statement: str) -> str | None:
    """
    Args:
        statement: SQL text containing a single-quoted literal.

    Returns:
        The literal's contents, or None when the statement has none.
    """
    match = _SQL_LITERAL.search(statement)
    return match.group(1) if match else None


def _sql_identifier(statement: str, quote: str, *, after: str | None = None) -> str | None:
    """
    Extracts a quoted identifier from a CREATE/DROP DATABASE statement.

    Args:
        statement: SQL text.
        quote: The quote character the engine uses for identifiers: `"` for
            PostgreSQL (`_escape_identifier`), `` ` `` for MySQL/MariaDB.
        after: When given, only matches the identifier immediately following
            this keyword (`"OWNER"`), for the optional owner clause; the
            first quoted identifier in the statement otherwise.

    Returns:
        The identifier, with its doubled escape quote un-escaped, or None
        when the statement holds no such identifier.
    """
    lead = rf"{re.escape(after)}\s+" if after else ""
    q = re.escape(quote)
    match = re.search(rf"{lead}{q}((?:[^{q}]|{q}{q})*){q}", statement)
    if not match:
        return None
    return match.group(1).replace(quote + quote, quote)


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

    def base_name(name: str) -> str:
        """The unit's name stripped of `.service` or `.timer`, for tracking that applies to both."""
        return name.removesuffix(".service").removesuffix(".timer")

    # Enable/disable of a unit `_systemctl` does not otherwise model (a cron timer, or a
    # service created through the API this run rather than seeded into `units`): systemctl
    # enable/disable always succeeds against a real unit file, and `list-unit-files` and
    # `is-enabled` must be able to answer it afterwards. Keyed by base_name() so a `.timer`
    # and its paired `.service` - and a plain service - all agree. Absent means "enabled": a
    # freshly written unit is enabled by default, matching what `wasm service create` and
    # `wasm cron create` do on a real machine before this ever runs.
    enabled_overrides: dict[str, bool] = {}

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
            # Per-instance copies: the Databases page can create and drop
            # databases, and a stateless dict would have `create_database`
            # succeed and then have its own follow-up `get_database_info`
            # report "does not exist" (PostgresManager re-checks existence).
            self._pg_databases: dict[str, tuple[str, int]] = dict(_PG_DATABASES)
            self._mysql_databases: dict[str, tuple[int, int]] = dict(_MYSQL_DATABASES)

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
                # The structured SQL console asks for a header row: psql with
                # `--csv`, mysql with `-B` alone (no `-N`). Every other caller
                # of these clients keeps the old headerless shape.
                headers = "--csv" in args or (
                    program == "mysql" and "-B" in args and "-N" not in args
                )
                return ok(
                    args, self._sql(program, getattr(self._stdin, "value", ""), headers=headers)
                )
            if program == "certbot" and args[1:2] == ("certificates",):
                return ok(args, self._certificates())
            if program == "certbot" and "--version" in args:
                return ok(args, "certbot 2.9.0\n")
            if program == "systemd-analyze" and args[1:2] == ("calendar",):
                return ok(args, self._systemd_analyze_calendar(args))
            if program == "systemd-analyze" and args[1:2] == ("verify",):
                return self._systemd_analyze_verify(args)
            if program == "systemd-analyze" and "--version" in args:
                return ok(args, "systemd 255 (255.4-1ubuntu8)\n")
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
                enabled = unit.enabled if unit is not None else enabled_overrides.get(base_name(name), True)
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
            if verb in ("enable", "disable"):
                if unit is not None:
                    unit.enabled = verb == "enable"
                # Recorded regardless of whether `unit` models this name, so a cron timer or
                # an ad-hoc created service (see the comment on enabled_overrides) is tracked
                # exactly like one `units` already knows.
                enabled_overrides[base_name(name)] = verb == "enable"
                return ok(args)
            if verb in ("start", "stop", "restart") and unit is None:
                # Cron job units (wasm-cron-{name}.service) are written to the sandboxed
                # systemd directory but never registered in the live `units` model: they have
                # no persistent active/inactive state worth modelling, only a one-shot run.
                # "wasm cron run" starts one by name, so a unit that genuinely exists on disk
                # succeeds here; only a name with no unit file at all is "not found".
                if (systemd_dir / f"{name}.service").exists():
                    return ok(args)
                return CommandResult(
                    args, 5, "", f"Failed to {verb} {targets[-1]}: Unit not found.\n"
                )
            return ok(args)

        @staticmethod
        def _show(name: str, unit: Unit | None) -> str:
            if unit is None:
                # A unit written to disk but never given live state here (created through the
                # API this run, or a cron timer) is loaded and inactive, the way systemd
                # reports one nothing has started - never plain "not-found". Either way,
                # MainPID always appears as a number: ServiceInfo.pid is `int | None`, and
                # `details.get("MainPID", "")` used to default to "", which pydantic refused
                # to parse as an int and turned GET /api/services into a 500 the moment a
                # service was created without a modelled Unit.
                if (systemd_dir / f"{name}.service").exists():
                    return "LoadState=loaded\nActiveState=inactive\nSubState=dead\nMainPID=0\nMemoryCurrent=0\nResult=success\n"
                return "LoadState=not-found\nActiveState=inactive\nSubState=dead\nMainPID=0\n"
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

        def _sql(self, program: str, statement: str, *, headers: bool = False) -> str:
            # An instance method, not a staticmethod: `self._pg_databases` and
            # `self._mysql_databases` start as copies of the module's seed data
            # and are mutated by CREATE/DROP DATABASE below, so a database the
            # Databases page just created answers to every query about it
            # afterwards - including PostgresManager.create_database's own
            # follow-up call to get_database_info, which re-checks existence.
            #
            # More specific matches first: PostgresManager.get_database_info's owner
            # query and database_exists' existence check both contain "pg_database"
            # as a substring (inside "pg_database_size" and "FROM pg_database"), so
            # they would otherwise fall through to the generic listing below.
            #
            # list_databases()'s own query joins pg_roles the same way
            # get_database_info()'s does (for the owner column), so both contain
            # "d.datdba = r.oid"; list_databases() is the one with no per-database
            # WHERE literal ("datistemplate = false" instead of "datname = '...'")
            # and must be checked first, or its listing query would be mistaken for
            # a lookup of one database and answer as though `name` were None.
            if program == "psql" and "d.datdba = r.oid" in statement and "datistemplate" in statement:
                return "".join(
                    f"{name}|UTF8|{size}|{owner}\n" for name, (owner, size) in self._pg_databases.items()
                )
            if program == "psql" and "d.datdba = r.oid" in statement:
                name = _sql_literal(statement)
                info = self._pg_databases.get(name or "")
                return f"{name}|UTF8|{info[1]}|{info[0]}\n" if info else ""
            if program == "psql" and statement.lstrip().startswith("SELECT 1 FROM pg_database"):
                return "1\n" if _sql_literal(statement) in self._pg_databases else ""
            if program == "psql" and statement.lstrip().startswith("CREATE DATABASE"):
                name = _sql_identifier(statement, '"')
                if name is not None:
                    owner = _sql_identifier(statement, '"', after="OWNER") or "postgres"
                    self._pg_databases[name] = (owner, 8192)
                return ""
            if program == "psql" and statement.lstrip().startswith("DROP DATABASE"):
                name = _sql_identifier(statement, '"')
                if name is not None:
                    self._pg_databases.pop(name, None)
                return ""
            # list_users()'s combined query aliases every column ("r.rolsuper"
            # rather than the old bare "rolsuper"), so it no longer contains
            # that literal; matched instead on `has_database_privilege`, the
            # function unique to this query's databases-per-role column - see
            # _PG_USERS for its (now 5-column) shape.
            if program == "psql" and "has_database_privilege" in statement:
                return _PG_USERS
            if program == "psql" and "FROM demo_orders" in statement:
                return _PG_DEMO_ROWS_CSV if headers else _PG_DEMO_ROWS
            if program == "psql" and "pg_database" in statement:
                return "".join(f"{name}|UTF8|{size}\n" for name, (_owner, size) in self._pg_databases.items())
            # Same reasoning for MySQL: get_database_info's size query sums
            # bare "DATA_LENGTH + INDEX_LENGTH" columns (single table, no
            # alias needed); list_databases()'s own query joins
            # INFORMATION_SCHEMA.TABLES aliased "t" for the same sum, so it
            # reads "t.DATA_LENGTH + t.INDEX_LENGTH" instead and is matched
            # separately, checked first since the alias makes it more specific.
            if program == "mysql" and "t.DATA_LENGTH + t.INDEX_LENGTH" in statement:
                seeded = "".join(
                    f"{name}\tutf8mb4\t{size}\n" for name, (size, _tables) in self._mysql_databases.items()
                )
                return "information_schema\tutf8mb3\t0\nmysql\tutf8mb4\t0\n" + seeded
            if program == "mysql" and "DATA_LENGTH + INDEX_LENGTH" in statement:
                info = self._mysql_databases.get(_sql_literal(statement) or "")
                return f"{info[0]}\t{info[1]}\n" if info else "0\t0\n"
            if program == "mysql" and statement.lstrip().startswith("SELECT SCHEMA_NAME FROM"):
                name = _sql_literal(statement)
                return f"{name}\n" if name in self._mysql_databases else ""
            if program == "mysql" and statement.lstrip().startswith("SELECT DEFAULT_CHARACTER_SET_NAME"):
                return "utf8mb4\n"
            if program == "mysql" and statement.lstrip().startswith("CREATE DATABASE"):
                name = _sql_identifier(statement, "`")
                if name is not None:
                    self._mysql_databases[name] = (0, 0)
                return ""
            if program == "mysql" and statement.lstrip().startswith("DROP DATABASE"):
                name = _sql_identifier(statement, "`")
                if name is not None:
                    self._mysql_databases.pop(name, None)
                return ""
            # list_users()'s combined query still contains this fragment (the
            # join is "FROM mysql.user u"), with a 3rd column of comma
            # separated databases appended - see _MYSQL_USERS.
            if program == "mysql" and "mysql.user" in statement:
                return _MYSQL_USERS
            if program == "mysql" and "FROM demo_orders" in statement:
                return _MYSQL_DEMO_ROWS_HEADERS if headers else _MYSQL_DEMO_ROWS
            if program == "mysql" and "SCHEMATA" in statement:
                return "information_schema\tutf8mb3\nmysql\tutf8mb4\n" + "".join(
                    f"{name}\tutf8mb4\n" for name in self._mysql_databases
                )
            if program == "redis-cli" and "keyspace" in statement:
                return "# Keyspace\ndb0:keys=1284,expires=210,avg_ttl=0\ndb2:keys=37,expires=0\n"
            if program == "redis-cli" and "databases" in statement:
                return "databases\n16\n"
            return ""

        @staticmethod
        def _systemd_analyze_calendar(args: tuple[str, ...]) -> str:
            """
            Fake `systemd-analyze calendar --iterations=N <expr>` for the cron
            job dialog's "next N runs" preview.

            Not real calendar arithmetic - the console only needs believable,
            parseable, always-in-the-future dates to prove the round trip
            works, not the exact semantics of an arbitrary OnCalendar
            expression. Every run is a day apart starting tomorrow at 02:00,
            regardless of what the expression actually says.
            """
            expr = args[-1] if len(args) > 1 else "*-*-* 02:00:00"
            iterations = 5
            for arg in args:
                if arg.startswith("--iterations="):
                    iterations = int(arg.split("=", 1)[1])
            start = datetime.now().replace(hour=2, minute=0, second=0, microsecond=0) + timedelta(
                days=1
            )
            lines = [f"  Original form: {expr}", f"Normalized form: {expr}"]
            for index in range(iterations):
                stamp = (start + timedelta(days=index)).strftime("%a %Y-%m-%d %H:%M:%S UTC")
                label = "Next elapse" if index == 0 else f"Iter. #{index + 1}"
                lines.append(f"      {label}: {stamp}")
            return "\n".join(lines) + "\n"

        @staticmethod
        def _systemd_analyze_verify(args: tuple[str, ...]) -> CommandResult:
            """
            Fake `systemd-analyze verify <path>` for the unit editor's "test
            configuration" button.

            Reads the candidate unit the caller staged (through the real,
            sandboxed filesystem - `ServiceManager.verify_unit` writes it
            before running this) and reports the one failure a hand-edited
            unit realistically hits: no `ExecStart=`. Anything else is
            reported as a clean pass, matching systemd-analyze's own silence
            on success.
            """
            path = Path(args[-1]) if len(args) > 1 else None
            content = path.read_text() if path and path.exists() else ""
            if not re.search(r"^ExecStart=\S", content, re.MULTILINE):
                name = path.name if path else "unit"
                return CommandResult(
                    args,
                    1,
                    "",
                    f"{name}: Service has no ExecStart= setting. Refusing.\n",
                )
            return ok(args)

        @staticmethod
        def _unit_files(patterns: list[str]) -> str:
            lines = []
            for path in sorted(systemd_dir.glob("*")):
                if patterns and not any(fnmatch(path.name, p) for p in patterns):
                    continue
                state = "enabled" if enabled_overrides.get(base_name(path.name), True) else "disabled"
                lines.append(f"{path.name} {state} {state}")
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
    # First, so the tabs' old deploys get lower ids than the ones seeded as of now.
    tabs_history = seed_app_tabs_history(store)
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
    seed_monitor(sandbox, units)
    seed_job_history(sandbox, store, state.domains[0])
    seed_app_tabs(sandbox, store, units, ports, domains, tabs_history)
    seed_domains_and_sources(sandbox, store, units, list(state.cert_domains))
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
# Console: the Server page's monitor card needs an installed, running unit
# and a small findings history to show status, observations and the
# acknowledge flow; the Activity page needs at least one job with a captured
# log, since none of tests.panel_factory.seed_console_state's jobs record one.
# ---------------------------------------------------------------------------


def seed_monitor(sandbox: Sandbox, units: dict[str, Unit]) -> None:
    """
    Install the monitor's unit and give it a short findings history.

    Mirrors a machine where ``wasm monitor install --enable`` has already run
    for a while: the unit active and enabled (answered by the fake runner,
    the same way an application's unit is), and a few observations already on
    record - a couple still open, one already acknowledged - for the
    console's monitor card, the acknowledge flow, and the overview's "Needs
    attention" (Task 3.1, already wired to the same endpoint).

    Args:
        sandbox: The sandbox.
        units: The modelled machine's units, mutated in place so the fake
            runner reports the monitor unit as installed, active and enabled.
    """
    from wasm.monitor.models import (
        SEVERITY_NOTICE,
        SEVERITY_WARNING,
        SIGNAL_NAME_PATTERN,
        SIGNAL_RESOURCE_USAGE,
        ProcessInfo,
        ProcessObservation,
    )
    from wasm.monitor.observation_store import ObservationStore
    from wasm.monitor.process_monitor import ProcessMonitor

    unit_name = ProcessMonitor.SERVICE_NAME
    units[unit_name] = Unit(
        active="active", enabled=True, pid=2114, since=datetime.now() - timedelta(days=6)
    )
    (sandbox.systemd_dir / f"{unit_name}.service").write_text(
        "[Unit]\nDescription=WASM resource monitor\nAfter=network.target\n\n"
        "[Service]\nType=simple\nExecStart=/usr/bin/wasm monitor run\nRestart=always\n\n"
        "[Install]\nWantedBy=multi-user.target\n",
        encoding="utf-8",
    )

    now = datetime.now()
    store = ObservationStore()
    store.save_many(
        [
            ProcessObservation(
                process=ProcessInfo(
                    pid=41823,
                    name="node",
                    user="www-data",
                    cpu_percent=92.4,
                    memory_percent=18.2,
                    command="node server.js",
                ),
                signal=SIGNAL_RESOURCE_USAGE,
                severity=SEVERITY_WARNING,
                detail="CPU above 90% for more than five minutes",
                observed_at=now - timedelta(hours=2),
            ),
            ProcessObservation(
                process=ProcessInfo(
                    pid=9931,
                    name="xmrig",
                    user="www-data",
                    cpu_percent=100.0,
                    memory_percent=2.1,
                    command="xmrig -o pool.example.com:4444",
                ),
                signal=SIGNAL_NAME_PATTERN,
                severity=SEVERITY_WARNING,
                detail="Process name matches a known cryptominer pattern",
                observed_at=now - timedelta(hours=5),
            ),
            ProcessObservation(
                process=ProcessInfo(
                    pid=512,
                    name="nc",
                    user="root",
                    cpu_percent=0.1,
                    memory_percent=0.1,
                    command="nc -lvp 4444",
                ),
                signal=SIGNAL_NAME_PATTERN,
                severity=SEVERITY_NOTICE,
                detail="Listener started with a raw networking tool",
                observed_at=now - timedelta(days=2),
            ),
        ]
    )
    # One already-dismissed row, so the observations list and "Needs
    # attention" show the difference between open and acknowledged findings.
    acknowledged_id = store.save(
        ProcessObservation(
            process=ProcessInfo(
                pid=7710,
                name="ffmpeg",
                user="www-data",
                cpu_percent=88.0,
                memory_percent=6.4,
                command="ffmpeg -i input.mp4 -f null -",
            ),
            signal=SIGNAL_RESOURCE_USAGE,
            severity=SEVERITY_NOTICE,
            detail="Short-lived CPU spike during a video conversion",
            observed_at=now - timedelta(days=3),
        )
    )
    store.acknowledge(acknowledged_id)


def seed_job_history(sandbox: Sandbox, store: Any, domain: str) -> None:
    """
    Add one job with a captured log, for the Activity page's log drawer.

    :func:`tests.panel_factory.seed_console_state` seeds a small job history
    already (a deploy, a backup, a cert renewal, a failed update), none of
    which record ``log_path``: nothing wrote a file for them to point at.
    This adds one more, the way the real job manager does - a text file next
    to the store, referenced by the record - so opening a job's log on the
    Activity page has something real to show instead of every row answering
    "no log captured".

    Args:
        sandbox: The sandbox.
        store: The seeded store.
        domain: The application the job ran against.
    """
    from wasm.core.store import JobRecord

    log_dir = sandbox.store_file.parent / "job-logs"
    log_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    unit = domain.replace(".", "-")
    log_path = log_dir / "seed-update-0001.log"
    log_path.write_text(
        f"==> Updating {domain}\n"
        "Fetching origin\n"
        "HEAD is now at 9f2c41a Update dependencies\n"
        "Installing dependencies\n"
        f"Restarting wasm-{unit}.service\n"
        f"Update of {domain} finished\n",
        encoding="utf-8",
    )
    started = datetime.now() - timedelta(minutes=45)
    store.create_job(
        JobRecord(
            # Job ids are validated as hexadecimal (JOB_ID_PATTERN in
            # web/api/jobs.py) wherever one names a single job, unlike
            # tests.panel_factory's own "seed0000" ids, which only ever
            # appear in the list.
            id="c0ffee01",
            type="update",
            name=f"Update {domain}",
            description=f"Seeded update of {domain} with a captured log",
            status="completed",
            progress=100,
            domain=domain,
            created_at=started.isoformat(),
            started_at=started.isoformat(),
            finished_at=(started + timedelta(seconds=38)).isoformat(),
            log_path=str(log_path),
        )
    )


# ---------------------------------------------------------------------------
# Console: an application's tabs (deployments, logs, metrics, environment,
# diagnose, settings) need applications whose trees are real, in the sandbox:
# one on the release layout with releases to roll back to, one in place whose
# update runs the real update sequence (slowly enough to watch it stream), and
# two in place that can be migrated to releases. Their journals stream over
# /ws/logs from a model of the machine, since no real journalctl may run.
# ---------------------------------------------------------------------------

#: On the release layout: three releases on disk, one failed build that was
#: removed, resource limits, webhook deliveries and a long deployment history.
TABS_RELEASE_APP = "tienda.cittek.es"

#: In place, with a real tree: its .env is read and written, and an update runs
#: the real update sequence with the build output streamed a line at a time.
TABS_LIVE_APP = "pedidos.cittek.es"

#: In place, to be migrated to releases. A migration cannot be undone through
#: the API, so the E2E suite migrates one per theme project.
TABS_MIGRATE_APPS = ("blog.cittek.es", "docs.cittek.es")

#: Seconds between the lines of a streamed npm install or build of the live app.
TABS_BUILD_LINE_DELAY = 0.35

#: Seconds between the request lines the modelled journal appends while followed.
TABS_JOURNAL_TICK = 1.5

#: What npm prints while installing and building the live app.
_TABS_NPM_INSTALL = (
    "npm warn deprecated inflight@1.0.6: This module is not supported, and leaks memory.",
    "npm warn deprecated glob@7.2.3: Glob versions prior to v9 are no longer supported",
    "",
    "added 214 packages, and audited 215 packages in 6s",
    "",
    "38 packages are looking for funding",
    "  run `npm fund` for details",
    "",
    "found 0 vulnerabilities",
)
_TABS_NPM_BUILD = (
    "",
    "> pedidos@2.4.0 build",
    "> tsc -p tsconfig.build.json && node scripts/copy-assets.mjs",
    "",
    "Compiling 142 files...",
    "Copied 18 assets to dist/public",
    "Build finished in 4.2s",
)

#: What the modelled journal appends while the Logs tab follows an app.
_TABS_REQUESTS = (
    "GET / 200 in 38ms",
    "GET /api/orders?page=1 200 in 41ms",
    "POST /api/cart 201 in 87ms",
    "GET /_next/static/chunks/app/page-4f1c.js 200 in 2ms",
    "GET /api/health 200 in 3ms",
    "warn: slow query on orders (1204ms): SELECT * FROM orders WHERE status = $1",
    "GET /checkout 200 in 112ms",
    "Error: connect ECONNREFUSED 127.0.0.1:6379 (redis cache unavailable, serving from database)",
    "GET /api/products/88 200 in 19ms",
    "POST /api/checkout 200 in 342ms",
)


def _tabs_serve_ok() -> int:
    """
    Answer HTTP on a free loopback port, as an application that is up would.

    The health gate probes ``http://127.0.0.1:<port>/`` after activating a
    release or migrating; with nothing listening, every rollback and every
    migration would be undone as unhealthy.

    Returns:
        The port.
    """
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Ok(BaseHTTPRequestHandler):
        """Answers 200 to anything."""

        def do_GET(self) -> None:
            body = b"ok\n"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_HEAD(self) -> None:
            self.send_response(200)
            self.end_headers()

        def log_message(self, format: str, *args: Any) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Ok)
    threading.Thread(target=server.serve_forever, name="tabs-health", daemon=True).start()
    return int(server.server_address[1])


def _tabs_node_tree(root: Path, name: str, *, env: str, uploads: bool) -> None:
    """
    Write a small Node application: the files an update and a migration read.

    Args:
        root: Where the tree goes.
        name: Its package name.
        env: The ``.env`` it holds, verbatim.
        uploads: Whether it has written user uploads into its own tree.
    """
    root.mkdir(parents=True, exist_ok=True)
    (root / "package.json").write_text(
        json.dumps(
            {
                "name": name,
                "version": "2.4.0",
                "private": True,
                "scripts": {"build": "tsc -p tsconfig.build.json", "start": "node dist/server.js"},
                "dependencies": {"express": "^4.21.0", "pg": "^8.13.0"},
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    (root / "package-lock.json").write_text('{"lockfileVersion": 3}\n', encoding="utf-8")
    (root / "dist").mkdir(exist_ok=True)
    (root / "dist" / "server.js").write_text(
        "require('http').createServer((q, s) => s.end('ok')).listen(process.env.PORT);\n",
        encoding="utf-8",
    )
    env_file = root / ".env"
    env_file.write_text(env, encoding="utf-8")
    env_file.chmod(0o600)
    if uploads:
        for relative, content in (
            ("uploads/2026/09/invoice-1024.pdf", "%PDF-1.7 seeded invoice\n"),
            ("uploads/2026/09/logo.png", "seeded image\n"),
            ("storage/sessions/sess_4f1c", "seeded session\n"),
        ):
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")


def _tabs_register(
    sandbox: Sandbox,
    store: Any,
    units: dict[str, Unit],
    ports: dict[str, int],
    domains: dict[str, str],
    app: Any,
    *,
    working_directory: Path,
) -> None:
    """
    Record an application, its unit and its site, and model the unit as running.

    Args:
        sandbox: The sandbox.
        store: The seeded store.
        units: The modelled units, mutated in place.
        ports: Each unit's port, mutated in place.
        domains: Each unit's domain, mutated in place.
        app: The application row to create.
        working_directory: Where its unit runs from.
    """
    from wasm.core.store import Service, Site
    from wasm.core.utils import domain_to_app_name
    from wasm.managers.service_manager import WASM_UNIT_MARKER

    created = store.create_app(app)
    unit = domain_to_app_name(app.domain)
    unit_file = sandbox.systemd_dir / f"{unit}.service"
    command = "/usr/bin/node dist/server.js"
    unit_file.write_text(
        f"# {WASM_UNIT_MARKER}\n"
        "[Unit]\n"
        f"Description={app.domain}\n"
        "After=network.target\n\n"
        "[Service]\n"
        "Type=simple\n"
        "User=www-data\n"
        f"WorkingDirectory={working_directory}\n"
        f"ExecStart={command}\n"
        f"Environment=PORT={app.port}\n"
        "Restart=on-failure\n\n"
        "[Install]\n"
        "WantedBy=multi-user.target\n",
        encoding="utf-8",
    )
    store.create_service(
        Service(
            app_id=created.id,
            name=unit,
            unit_file=str(unit_file),
            working_directory=str(working_directory),
            command=command,
            status="active",
            enabled=True,
            port=app.port,
        )
    )
    site_file = sandbox.etc / "nginx" / "sites-available" / app.domain
    site_file.write_text(
        "server {\n"
        f"    server_name {app.domain};\n"
        "    listen 80;\n"
        "    location / {\n"
        f"        proxy_pass http://127.0.0.1:{app.port};\n"
        "        proxy_set_header Host $host;\n"
        "    }\n"
        "}\n",
        encoding="utf-8",
    )
    store.create_site(
        Site(
            app_id=created.id,
            domain=app.domain,
            webserver="nginx",
            config_path=str(site_file),
            enabled=True,
            proxy_port=app.port,
            ssl_enabled=True,
        )
    )
    units[unit] = Unit(
        active="active",
        enabled=True,
        pid=43000 + (created.id or 0),
        since=datetime.now() - timedelta(hours=2, minutes=(created.id or 0) * 3),
    )
    ports[unit] = int(app.port)
    domains[unit] = app.domain


def _tabs_stamped(start: datetime, steps: Sequence[tuple[float, str]]) -> str:
    """
    Render a captured build log the way :class:`DeploymentRecorder` writes it.

    Args:
        start: When the deployment started.
        steps: Seconds after the start, and the line.

    Returns:
        The log, one ``[YYYY-MM-DD HH:MM:SS] line`` per line.
    """
    return "".join(
        f"[{(start + timedelta(seconds=offset)).strftime('%Y-%m-%d %H:%M:%S')}] {line}\n"
        for offset, line in steps
    )


def _tabs_release_log(
    kind: str, *, source: str, commit: str, release_id: str, previous: str | None, port: int
) -> tuple[list[tuple[float, str]], str | None]:
    """
    The captured log of one deploy of the release app, and its error when it failed.

    Args:
        kind: ``ok``, ``reused`` (same lockfile), ``build`` (a type error) or
            ``health`` (the release never answered).
        source: The repository.
        commit: The commit built.
        release_id: The release it built.
        previous: The release that was serving.
        port: The application's port.

    Returns:
        The lines with their offsets in seconds, and the error text or None.
    """
    lines: list[tuple[float, str]] = [
        (0, "[1/9] 📥 Fetching source into a new release..."),
        (0.4, f"      → Source: {source} (main)"),
        (2.1, f"      → HEAD is now at {commit}"),
        (2.3, f"      → Release: {release_id}"),
        (2.4, "      → Linked .env to shared/.env"),
        (2.4, "      → Linked uploads to shared/uploads"),
    ]
    if kind == "reused":
        lines += [
            (2.6, "[2/9] 📦 Installing dependencies..."),
            (3.9, f"      → Dependencies reused from {previous}: the lockfile did not change"),
        ]
        at = 4.0
    else:
        lines += [
            (2.6, "[2/9] 📦 Installing dependencies..."),
            (2.7, "      → Running: npm ci"),
            (15.8, "added 812 packages, and audited 813 packages in 13s"),
            (15.9, "found 0 vulnerabilities"),
        ]
        at = 16.0
    lines += [
        (at, "[3/9] 🔨 Building application..."),
        (at + 0.1, "      → Running: npm run build"),
        (at + 1.4, "   ▲ Next.js 15.2.4"),
        (at + 1.5, "   Creating an optimized production build ..."),
    ]
    if kind == "build":
        lines += [
            (at + 24.2, "Failed to compile."),
            (at + 24.2, ""),
            (at + 24.2, "./app/checkout/page.tsx:42:7"),
            (at + 24.2, "Type error: Property 'total' does not exist on type 'Order'."),
            (at + 24.3, "✗ Deployment failed: npm run build exited with status 1"),
            (
                at + 24.4,
                f"      → Removed release {release_id}; {previous or 'nothing'} keeps serving",
            ),
        ]
        return lines, (
            "npm run build exited with status 1\n"
            "./app/checkout/page.tsx:42:7\n"
            "Type error: Property 'total' does not exist on type 'Order'."
        )
    at += 27.0
    lines += [
        (at - 5.6, " ✓ Compiled successfully in 21.4s"),
        (at - 1.2, " ✓ Generating static pages (38/38)"),
        (at, "[4/9] 🔒 Setting permissions..."),
        (at + 0.8, "[5/9] 🌐 Creating site configuration..."),
        (at + 1.0, "[6/9] 🔒 Obtaining SSL certificate..."),
        (at + 1.1, "      → The existing certificate keeps serving what it covers"),
        (at + 1.3, "[7/9] ⚙️ Creating systemd service..."),
        (at + 1.6, "[8/9] 🚀 Activating release..."),
        (
            at + 1.7,
            f"      → Activated release {release_id}"
            + (f" (was {previous})" if previous is not None else ""),
        ),
        (at + 1.8, f"      → Checking: http://127.0.0.1:{port}/"),
    ]
    if kind == "health":
        lines += [
            (at + 31.8, f"⚠ Release {release_id} did not pass its health check"),
            (at + 31.9, f"      → Going back to release {previous}"),
            (at + 34.0, f"✗ Deployment failed: release {release_id} did not answer"),
        ]
        return lines, (
            f"Release {release_id} did not pass its health check: "
            f"http://127.0.0.1:{port}/ did not answer after 15 attempts "
            "([Errno 111] Connection refused)\n"
            "-- journal --\n"
            "Error: Cannot find module '/var/www/apps/tienda-cittek-es/current/.next/standalone/server.js'\n"
            "tienda-cittek-es.service: Main process exited, code=exited, status=1/FAILURE"
        )
    lines += [
        (at + 3.9, "[9/9] 🩺 Health check..."),
        (at + 4.0, f"✓ Release {release_id} answered 200 in 84 ms"),
        (at + 4.1, "✓ Deployed tienda.cittek.es"),
    ]
    return lines, None


def _tabs_inplace_log(
    domain: str, *, commit: str, source: str, port: int
) -> list[tuple[float, str]]:
    """
    The captured log of a successful in-place deploy of a Node application.

    Args:
        domain: The application.
        commit: The commit deployed.
        source: The repository.
        port: The application's port.

    Returns:
        The lines with their offsets in seconds.
    """
    return [
        (0, "[1/8] 📥 Fetching source code..."),
        (0.3, f"      → Source: {source}"),
        (1.8, f"      → HEAD is now at {commit}"),
        (2.0, "[2/8] 📦 Installing dependencies..."),
        (2.1, "      → Running: npm ci"),
        (9.4, "added 214 packages, and audited 215 packages in 7s"),
        (9.6, "[3/8] 🔨 Building application..."),
        (9.7, "      → Running: npm run build"),
        (14.1, "Build finished in 4.2s"),
        (14.2, "[4/8] 🔒 Setting permissions..."),
        (14.9, "[5/8] 🌐 Creating site configuration..."),
        (15.1, "[6/8] 🔒 Obtaining SSL certificate..."),
        (15.2, "      → The existing certificate keeps serving what it covers"),
        (15.4, "[7/8] ⚙️ Creating systemd service..."),
        (15.9, "[8/8] 🚀 Starting application..."),
        (18.2, f"      → Checking: http://127.0.0.1:{port}/"),
        (18.4, f"✓ Deployed {domain}"),
    ]


def _tabs_deployment(
    store: Any,
    domain: str,
    *,
    trigger: str,
    commit: str,
    started: datetime,
    lines: Sequence[tuple[float, str]],
    status: str,
    error: str | None = None,
) -> int:
    """
    Record one finished deployment at a moment in the past, with its captured log.

    Args:
        store: The seeded store.
        domain: The application.
        trigger: ``cli``, ``panel`` or ``webhook``.
        commit: The commit deployed.
        started: When it started.
        lines: Its log, as offsets in seconds and lines; the last offset is
            when it finished.
        status: How it ended.
        error: What went wrong, verbatim, when it failed.

    Returns:
        The deployment id.
    """
    deployment_id = store.record_deployment_start(
        domain, trigger, git_commit=commit, git_branch="main"
    )
    seconds = max(offset for offset, _ in lines) if lines else 0.0
    log_dir = store.db_path.parent / "deploy-logs" / domain
    log_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    log_path = log_dir / f"{deployment_id}.log"
    log_path.write_text(_tabs_stamped(started, lines), encoding="utf-8")
    log_path.chmod(0o600)
    # finish_deployment stamps "now"; history is written with the times it had.
    with store._transaction() as cursor:
        cursor.execute(
            "UPDATE deployments SET status = ?, error = ?, started_at = ?, finished_at = ?, "
            "duration_s = ?, log_path = ? WHERE id = ?",
            (
                status,
                error,
                started.isoformat(),
                (started + timedelta(seconds=seconds)).isoformat(),
                round(seconds, 1),
                str(log_path),
                deployment_id,
            ),
        )
    return int(deployment_id)


@dataclass
class _TabsApp:
    """
    What the first seeding pass decided for one of the tabs' applications.

    Attributes:
        port: The port its health listener holds.
        deploys: Its deployments, oldest first: when, commit, kind, status and
            the release each built.
    """

    port: int
    deploys: list[tuple[datetime, str, str, str, str]] = field(default_factory=list)

    @property
    def starts(self) -> list[datetime]:
        """When each deployment started."""
        return [started for started, *_ in self.deploys]


#: The release app's history, oldest first: days ago, trigger, commit, kind, status.
_TABS_RELEASE_HISTORY = (
    (27.2, "webhook", "3c1e9a0", "ok", "success"),
    (24.1, "webhook", "7f2d4b1", "ok", "success"),
    (21.0, "panel", "7f2d4b1", "reused", "success"),
    (18.3, "webhook", "a94c0e2", "build", "failed"),
    (18.28, "webhook", "b1e7f93", "ok", "success"),
    (15.4, "cli", "b1e7f93", "reused", "success"),
    (12.2, "webhook", "c5d2a61", "ok", "rolled_back"),
    (9.1, "webhook", "d08e4f7", "health", "failed"),
    (7.3, "webhook", "e3b9c12", "ok", "success"),
    (5.2, "webhook", "f41a8d3", "ok", "success"),
    (3.1, "cli", "0c7e5b9", "ok", "success"),
    (1.05, "webhook", "19d3f6e", "reused", "success"),
    (0.09, "webhook", "2a8b7c4", "ok", "success"),
)

#: The releases of the release app still on disk, by commit.
_TABS_ON_DISK = frozenset({"0c7e5b9", "19d3f6e", "2a8b7c4"})

#: The in-place apps' histories, oldest first: days ago, trigger, commit.
_TABS_INPLACE_HISTORY: dict[str, tuple[tuple[float, str, str], ...]] = {
    TABS_LIVE_APP: (
        (20.4, "cli", "4e1f2a7"),
        (6.2, "panel", "8b3c9d0"),
        (2.1, "webhook", "5a7e1c3"),
    ),
    **dict.fromkeys(TABS_MIGRATE_APPS, ((41.0, "cli", "9d4e2b8"),)),
}


def _tabs_release_id(started: datetime, commit: str) -> str:
    """
    Args:
        started: When the release was built.
        commit: The commit it was built from.

    Returns:
        Its id, the way :class:`ReleaseManager` names a release directory.
    """
    from datetime import timezone

    return f"{started.astimezone(timezone.utc).strftime('%Y%m%d-%H%M%S')}-{commit}"


def seed_app_tabs_history(store: Any) -> dict[str, _TabsApp]:
    """
    Record the tabs' applications' deployment history, before anything else is seeded.

    Their history lies days in the past, and the API orders deployments by id:
    recorded after :func:`seed_console_state`'s deploys of "now", they would
    list as the newest on the overview. Recorded first, ids follow time. Each
    app's health listener starts here too, since its port is in the logs.

    Args:
        store: The store.

    Returns:
        Per domain, its port and its deployments.
    """
    now = datetime.now()
    apps: dict[str, _TabsApp] = {}

    source = "https://github.com/cittek/tienda.git"
    release = _TabsApp(port=_tabs_serve_ok())
    previous: str | None = None
    for days, trigger, commit, kind, status in _TABS_RELEASE_HISTORY:
        started = now - timedelta(days=days)
        rid = _tabs_release_id(started, commit)
        lines, error = _tabs_release_log(
            kind, source=source, commit=commit, release_id=rid, previous=previous, port=release.port
        )
        _tabs_deployment(
            store,
            TABS_RELEASE_APP,
            trigger=trigger,
            commit=commit,
            started=started,
            lines=lines,
            status=status,
            error=error,
        )
        release.deploys.append((started, commit, kind, status, rid))
        if status == "success":
            previous = rid
    apps[TABS_RELEASE_APP] = release

    from wasm.core.utils import domain_to_app_name

    for domain, history in _TABS_INPLACE_HISTORY.items():
        seeded = _TabsApp(port=_tabs_serve_ok())
        for days, trigger, commit in history:
            started = now - timedelta(days=days)
            _tabs_deployment(
                store,
                domain,
                trigger=trigger,
                commit=commit,
                started=started,
                lines=_tabs_inplace_log(
                    domain,
                    commit=commit,
                    source="https://github.com/cittek/" + domain_to_app_name(domain),
                    port=seeded.port,
                ),
                status="success",
            )
            seeded.deploys.append((started, commit, "ok", "success", ""))
        apps[domain] = seeded
    return apps


def _tabs_release_app(
    sandbox: Sandbox,
    store: Any,
    units: dict[str, Unit],
    ports: dict[str, int],
    domains: dict[str, str],
    seeded: _TabsApp,
) -> None:
    """
    Seed the release-layout application: its tree, its releases, its unit and site.

    Args:
        sandbox: The sandbox.
        store: The seeded store.
        units: The modelled units, mutated in place.
        ports: Each unit's port, mutated in place.
        domains: Each unit's domain, mutated in place.
        seeded: Its port and history, from :func:`seed_app_tabs_history`.
    """
    from datetime import timezone

    from wasm.core.store import App, ReleaseRecord
    from wasm.core.utils import domain_to_app_name

    domain = TABS_RELEASE_APP
    root = sandbox.apps_dir / domain_to_app_name(domain)
    port = seeded.port
    env = (
        "NODE_ENV=production\n"
        f"PORT={port}\n"
        "DATABASE_URL=postgres://tienda:K9v2xQ7mLp@127.0.0.1:5432/tienda\n"
        "REDIS_URL=redis://127.0.0.1:6379/2\n"
        "SESSION_SECRET=6b1f0e4c2d9a8b7e5f3c1a0d9e8f7a6b\n"
        "STRIPE_SECRET_KEY=sk_live_51Hx8cittekTiendaSeeded\n"
        "STRIPE_PUBLISHABLE_KEY=pk_live_51Hx8cittekTiendaSeeded\n"
        "NEXT_PUBLIC_SITE_URL=https://tienda.cittek.es\n"
        "MAIL_FROM=Tienda Cittek <pedidos@cittek.es>\n"
        "LOG_LEVEL=info\n"
    )
    shared = root / "shared"
    (shared / "uploads" / "products").mkdir(parents=True, exist_ok=True)
    (shared / "uploads" / "products" / "88.webp").write_text("seeded image\n", encoding="utf-8")
    (shared / ".env").write_text(env, encoding="utf-8")
    (shared / ".env").chmod(0o600)
    (root / "repo").mkdir(parents=True, exist_ok=True)
    (root / "releases").mkdir(parents=True, exist_ok=True)

    app = App(
        domain=domain,
        app_type="nextjs",
        source="https://github.com/cittek/tienda.git",
        branch="main",
        port=port,
        app_path=str(root),
        status="running",
        ssl_enabled=True,
        layout="releases",
        keep_releases=5,
        persistent_paths=["uploads"],
        memory_max_mb=512,
        cpu_quota_percent=150,
        tasks_max=256,
    )
    _tabs_register(sandbox, store, units, ports, domains, app, working_directory=root / "current")
    stored = store.get_app(domain)
    store.set_webhook_secret(domain, "seeded-webhook-secret-not-shown")
    if stored is None or stored.id is None:
        return

    serving: str | None = None
    for number, (started, commit, kind, status, rid) in enumerate(seeded.deploys, start=1):
        if commit in _TABS_ON_DISK and status == "success":
            release_dir = root / "releases" / rid
            release_dir.mkdir(parents=True, exist_ok=True)
            (release_dir / "package.json").write_text(
                json.dumps({"name": "tienda", "version": f"3.{number}.0"}) + "\n",
                encoding="utf-8",
            )
            (release_dir / ".next").mkdir(exist_ok=True)
            (release_dir / ".next" / "BUILD_ID").write_text(commit + "\n", encoding="utf-8")
            for name, target in (
                (".env", "../../shared/.env"),
                ("uploads", "../../shared/uploads"),
            ):
                link = release_dir / name
                if not link.is_symlink():
                    link.symlink_to(target)
        if (status == "success" and commit in _TABS_ON_DISK) or kind == "health":
            store.record_release(
                ReleaseRecord(
                    id=rid,
                    app_id=stored.id,
                    git_commit=commit,
                    created_at=started.astimezone(timezone.utc).isoformat(),
                    activated_at=None
                    if kind == "health"
                    else (started + timedelta(minutes=1)).astimezone(timezone.utc).isoformat(),
                    status="failed" if kind == "health" else "superseded",
                    path=str(root / "releases" / rid),
                )
            )
        if status == "success":
            serving = rid

    if serving is not None:
        (root / "current").symlink_to(Path("releases") / serving)
        store.mark_release_active(stored.id, serving)


def _tabs_inplace_app(
    sandbox: Sandbox,
    store: Any,
    units: dict[str, Unit],
    ports: dict[str, int],
    domains: dict[str, str],
    domain: str,
    seeded: _TabsApp,
) -> Path:
    """
    Seed an application deployed in place, with a real tree in the sandbox.

    Args:
        sandbox: The sandbox.
        store: The seeded store.
        units: The modelled units, mutated in place.
        ports: Each unit's port, mutated in place.
        domains: Each unit's domain, mutated in place.
        domain: The application.
        seeded: Its port and history, from :func:`seed_app_tabs_history`.

    Returns:
        Its directory.
    """
    from wasm.core.store import App
    from wasm.core.utils import domain_to_app_name

    name = domain_to_app_name(domain)
    root = sandbox.apps_dir / name
    # Recorded as a local directory: an update of a tree that is not a git
    # checkout fetches its recorded source again, which here is this copy.
    source_dir = sandbox.root / "sources" / name
    port = seeded.port
    env = (
        "# Written by wasm env configure\n"
        "NODE_ENV=production\n"
        f"PORT={port}\n"
        f"DATABASE_URL=postgres://{name.split('-')[0]}:Wm4p8Zr2@127.0.0.1:5432/{name.split('-')[0]}\n"
        "JWT_SECRET=2f7c9e1a4b6d8f0a3c5e7b9d1f2a4c6e\n"
        f"PUBLIC_URL=https://{domain}\n"
        "SMTP_HOST=smtp.cittek.es\n"
        "SMTP_PASSWORD=\n"
        "FEATURE_FLAGS=checkout-v2,fast-search\n"
    )
    for tree in (root, source_dir):
        _tabs_node_tree(tree, name.split("-")[0], env=env, uploads=tree == root)
    app = App(
        domain=domain,
        app_type="nodejs",
        source=str(source_dir),
        branch="main",
        port=port,
        app_path=str(root),
        status="running",
        ssl_enabled=True,
    )
    _tabs_register(sandbox, store, units, ports, domains, app, working_directory=root)
    return root


def _tabs_metrics(domain: str, deploys: Sequence[datetime], *, base_mb: float) -> None:
    """
    Write thirty days of CPU and memory history for an application.

    The collector reads an app's cgroup, which no sandboxed unit has, so the
    history the Metrics tab draws is written here: denser the more recent,
    as the store's own tiers keep it, with memory falling back after every
    deploy (the process restarted) and a CPU burst while each one warmed up.

    Args:
        domain: The application.
        deploys: When it was deployed.
        base_mb: Its resident memory just after a start, in MB.
    """
    import math

    from wasm.web.metrics_collector import get_metrics_store

    store = get_metrics_store()
    now = int(time.time())
    cpu_name, mem_name = f"app.{domain}.cpu.percent", f"app.{domain}.mem.bytes"
    stamps = [
        *range(now - 30 * 86_400, now - 86_400, 1_800),
        *range(now - 86_400, now - 3_600, 120),
        *range(now - 3_600, now, 10),
    ]
    deploy_times = sorted(int(moment.timestamp()) for moment in deploys)
    for stamp in stamps:
        since = [stamp - t for t in deploy_times if t <= stamp]
        age = min(since) if since else 30 * 86_400
        daily = math.sin((stamp % 86_400) / 86_400 * 2 * math.pi - math.pi / 2)
        wobble = math.sin(stamp / 977.0) * 0.6 + math.sin(stamp / 331.0) * 0.4
        cpu = 3.2 + 2.4 * daily + 1.1 * wobble + (38.0 * math.exp(-age / 240.0))
        memory_mb = base_mb + min(age / 3_600, 96) * 0.9 + 6 * wobble
        store.record_many(
            [(cpu_name, max(0.1, cpu)), (mem_name, memory_mb * 1024 * 1024)], ts=stamp
        )
    store.consolidate(now=now)


def _tabs_slow_live_builds(root: Path) -> None:
    """
    Stream npm's output for the live app a line at a time, as a real build does.

    The runner answers every command at once; an update would be over before
    the console could show it running. Only npm under the live app's tree is
    slowed down; everything else is answered as before.

    Args:
        root: The live app's directory.
    """
    from wasm.core.runner import CommandResult, get_runner

    runner = get_runner()
    original = runner.stream

    def stream(argv: Sequence[str], *, on_line: Any, cwd: Path | None = None, **kwargs: Any) -> Any:
        args = tuple(str(a) for a in argv)
        inside = cwd is not None and (Path(cwd) == root or root in Path(cwd).parents)
        if not (inside and "npm" in args):
            return original(argv, on_line=on_line, cwd=cwd, **kwargs)
        runner.calls.append(args)
        output = _TABS_NPM_BUILD if "build" in args else _TABS_NPM_INSTALL
        for line in output:
            time.sleep(TABS_BUILD_LINE_DELAY)
            on_line(line)
        return CommandResult(argv=args, exit_code=0, stdout="\n".join(output) + "\n", stderr="")

    runner.stream = stream  # type: ignore[method-assign]


class _TabsJournalFollow:
    """
    ``journalctl -u <unit> -f`` over the modelled machine, as asyncio sees a process.

    The logs WebSocket spawns journalctl itself; here it gets this instead: a
    backlog in ``short-iso`` form from the unit's state, then a request line
    every :data:`TABS_JOURNAL_TICK` seconds while the unit is active.
    """

    def __init__(
        self,
        args: Sequence[str],
        units: dict[str, Unit],
        domains: dict[str, str],
        ports: dict[str, int],
    ) -> None:
        """
        Args:
            args: The journalctl argv.
            units: The modelled units.
            domains: Each unit's domain.
            ports: Each unit's port.
        """
        self.returncode: int | None = None
        self.stdout = asyncio.StreamReader()
        self.stderr = asyncio.StreamReader()
        self.stderr.feed_eof()
        self._unit = args[args.index("-u") + 1].removesuffix(".service") if "-u" in args else ""
        backlog = int(args[args.index("-n") + 1]) if "-n" in args else 10
        self._state = units.get(self._unit)
        self._domain = domains.get(self._unit, self._unit)
        self._port = ports.get(self._unit, 3000)
        self._count = 0
        for line in self._backlog()[-backlog:]:
            self.stdout.feed_data(f"{line}\n".encode())
        self._task: asyncio.Task[None] | None = (
            asyncio.get_running_loop().create_task(self._follow())
            if "-f" in args and self._state is not None and self._state.active == "active"
            else None
        )

    def _line(self, moment: datetime, message: str) -> str:
        pid = self._state.pid if self._state and self._state.pid else 1
        stamp = moment.astimezone().strftime("%Y-%m-%dT%H:%M:%S%z")
        return f"{stamp} arenna {self._unit}[{pid}]: {message}"

    def _backlog(self) -> list[str]:
        failed = self._state is not None and self._state.active == "failed"
        template = FAILED_JOURNAL_LINES if failed else JOURNAL_LINES
        now = datetime.now()
        start = now - timedelta(minutes=70)
        lines = [
            self._line(
                start + timedelta(seconds=i),
                text.format(unit=self._unit, domain=self._domain, port=self._port),
            )
            for i, text in enumerate(template)
        ]
        if not failed:
            for i in range(96):
                moment = start + timedelta(seconds=40 * (i + 1))
                lines.append(self._line(moment, _TABS_REQUESTS[i % len(_TABS_REQUESTS)]))
        return lines

    async def _follow(self) -> None:
        while self.returncode is None:
            await asyncio.sleep(TABS_JOURNAL_TICK)
            message = _TABS_REQUESTS[(self._count * 3) % len(_TABS_REQUESTS)]
            self._count += 1
            self.stdout.feed_data(f"{self._line(datetime.now(), message)}\n".encode())

    def terminate(self) -> None:
        """Stop following, as SIGTERM would."""
        if self.returncode is None:
            self.returncode = -15
            if self._task is not None:
                self._task.cancel()
            self.stdout.feed_eof()

    def kill(self) -> None:
        """Stop following, as SIGKILL would."""
        self.terminate()

    async def wait(self) -> int:
        """
        Returns:
            The exit status.
        """
        return self.returncode if self.returncode is not None else 0


def _tabs_journal_model(
    units: dict[str, Unit], domains: dict[str, str], ports: dict[str, int]
) -> None:
    """
    Answer the logs WebSocket's ``journalctl -f`` from the modelled machine.

    Every other spawn is still refused by :func:`forbid_real_processes`.

    Args:
        units: The modelled units.
        domains: Each unit's domain.
        ports: Each unit's port.
    """
    refuse = asyncio.create_subprocess_exec

    async def spawn(*argv: Any, **kwargs: Any) -> Any:
        args = [str(a) for a in argv]
        if args and args[0] == "journalctl":
            return _TabsJournalFollow(args, units, domains, ports)
        return await refuse(*argv, **kwargs)

    asyncio.create_subprocess_exec = spawn  # type: ignore[assignment]


def _tabs_diagnose_model(units: dict[str, Unit], ports: dict[str, int]) -> None:
    """
    Answer the questions ``wasm diagnose`` asks that the runner could not.

    ``ss -ltnpH`` lists the port of every active modelled unit, owned by its
    main PID, ``systemctl show -p A,B <unit>`` answers the properties asked
    for from the unit's state, and the kernel log (``journalctl -k``) has no
    OOM kills. Without them every app diagnoses as "down: not listening",
    whatever its state.

    Args:
        units: The modelled units.
        ports: Each unit's port.
    """
    from wasm.core.runner import CommandResult, get_runner

    runner = get_runner()
    original = runner.run

    def run(argv: Sequence[str], **kwargs: Any) -> Any:
        args = tuple(str(a) for a in argv)
        if args[:2] == ("ss", "-ltnpH"):
            runner.calls.append(args)
            listening = [
                f"LISTEN 0 511 127.0.0.1:{ports[name]} 0.0.0.0:* "
                f'users:(("node",pid={unit.pid},fd=19))'
                for name, unit in sorted(units.items())
                if unit.active == "active" and name in ports
            ]
            return CommandResult(args, 0, "\n".join(listening) + "\n", "")
        if args[:1] == ("journalctl",) and "-k" in args:
            # The kernel log of the modelled machine has no OOM kills in it; without
            # this, every failed app's diagnosis names memory as the probable cause.
            runner.calls.append(args)
            return CommandResult(args, 0, "", "")
        if args[:3] == ("systemctl", "show", "-p") and len(args) >= 5:
            unit = units.get(args[-1].removesuffix(".service"))
            if unit is not None:
                runner.calls.append(args)
                failed = unit.active == "failed"
                known = {
                    "ActiveState": unit.active,
                    "SubState": unit.sub,
                    "Result": "exit-code" if failed else "success",
                    "ExecMainStatus": "1" if failed else "0",
                    "NRestarts": str(unit.restarts),
                    "MainPID": str(unit.pid),
                }
                wanted = args[3].split(",")
                return CommandResult(
                    args, 0, "".join(f"{key}={known.get(key, '')}\n" for key in wanted), ""
                )
        return original(argv, **kwargs)

    runner.run = run  # type: ignore[method-assign]


def _tabs_port_model(units: dict[str, Unit], ports: dict[str, int]) -> None:
    """
    Say a port answers when an active modelled unit listens on it.

    An application's state checks that its port accepts connections
    (:func:`wasm.core.app_state.port_answers`). The seeded units run no
    process, so without this every running app reads "No answer"; a port a
    real listener holds (the health servers above) still answers for real.

    Args:
        units: The modelled units.
        ports: Each unit's port.
    """
    import wasm.core.app_state as app_state_module

    real = app_state_module.port_answers

    def port_answers(port: int, *args: Any, **kwargs: Any) -> bool:
        modelled = any(
            unit.active == "active" and ports.get(name) == port for name, unit in units.items()
        )
        return modelled or real(port, *args, **kwargs)

    app_state_module.port_answers = port_answers  # type: ignore[assignment]


def seed_app_tabs(
    sandbox: Sandbox,
    store: Any,
    units: dict[str, Unit],
    ports: dict[str, int],
    domains: dict[str, str],
    history: dict[str, _TabsApp],
) -> None:
    """
    Seed what an application's tabs need beyond :func:`seed_console_state`.

    Args:
        sandbox: The sandbox.
        store: The seeded store.
        units: The modelled units, mutated in place.
        ports: Each unit's port, mutated in place.
        domains: Each unit's domain, mutated in place.
        history: What :func:`seed_app_tabs_history` recorded first.
    """
    _tabs_release_app(sandbox, store, units, ports, domains, history[TABS_RELEASE_APP])
    live_root = _tabs_inplace_app(
        sandbox, store, units, ports, domains, TABS_LIVE_APP, history[TABS_LIVE_APP]
    )
    for domain in TABS_MIGRATE_APPS:
        _tabs_inplace_app(sandbox, store, units, ports, domains, domain, history[domain])
    # The in-place app rolls back to a backup: give it the two a machine would have.
    seed_backups(sandbox, [TABS_LIVE_APP])
    _tabs_metrics(TABS_RELEASE_APP, history[TABS_RELEASE_APP].starts, base_mb=182)
    _tabs_metrics(TABS_LIVE_APP, history[TABS_LIVE_APP].starts, base_mb=96)
    _tabs_slow_live_builds(live_root)
    _tabs_journal_model(units, domains, ports)
    _tabs_diagnose_model(units, ports)
    _tabs_port_model(units, ports)


# ---------------------------------------------------------------------------
# Console: the Domains and certificates page, an application's Domains tab and
# the new-app wizard need a web server, certificates and DNS that behave, and
# sources to point the wizard at. Every seeded site gets a real configuration
# file rendered by the managers' own templates, so the editor has something to
# load; nginx's syntax check reads the files it is pointed at, so a broken edit
# fails the way nginx fails; certbot issues, expands, renews, revokes and
# deletes lineages on disk, and refuses a name the modelled DNS does not point
# here; that DNS answers from a fixed zone instead of the real network; and
# /var/www/src holds a Next.js project and a static site to inspect and deploy.
# ---------------------------------------------------------------------------

#: This machine's public addresses, as the modelled DNS knows them.
DOMAINS_MACHINE_ADDRESSES = ("203.0.113.10", "2001:db8::10")

#: Where a name that points at another server resolves.
DOMAINS_ELSEWHERE_ADDRESSES = ("198.51.100.23",)

#: Names under a seeded zone starting with these point at another server.
DOMAINS_ELSEWHERE_PREFIXES = ("old.", "legacy.")

#: Names under a seeded zone starting with these have no record yet.
DOMAINS_UNRESOLVED_PREFIXES = ("new.", "soon.")

#: The wizard's sources, under the sandbox's stand-in for /var/www/src.
WIZARD_SOURCES = ("storefront", "landing")

_DOMAINS_PEM = "-----BEGIN CERTIFICATE-----\nc2FuZGJveA==\n-----END CERTIFICATE-----\n"

_DOMAINS_CERTBOT_LOG = "Saving debug log to /var/log/letsencrypt/letsencrypt.log"

#: What certbot adds after a failed challenge, per authenticator.
_DOMAINS_CERTBOT_HINTS = {
    "nginx": "The Certificate Authority failed to verify the temporary {server} configuration "
    "changes made by Certbot. Ensure the listed domains point to this {server} server and "
    "that it is accessible from the internet.",
    "webroot": "The Certificate Authority failed to download the temporary challenge files "
    "created by Certbot. Ensure that the listed domains serve their content from the provided "
    "--webroot-path/-w and that files created there can be downloaded from the internet.",
    "standalone": "The Certificate Authority failed to download the challenge files from the "
    "temporary standalone webserver started by Certbot on port 80. Ensure that the listed "
    "domains point to this machine and that it can accept inbound connections from the "
    "internet.",
}
_DOMAINS_CERTBOT_HINTS["apache"] = _DOMAINS_CERTBOT_HINTS["nginx"]


def _domains_resolve(name: str, zones: frozenset[str]) -> tuple[str, ...]:
    """
    Answer a name from the modelled DNS.

    Args:
        name: The name to resolve.
        zones: The registrable domains the machine's applications live under.

    Returns:
        The addresses it resolves to; empty when it has no record.
    """
    name = name.strip().lower().rstrip(".")
    if not any(name == zone or name.endswith(f".{zone}") for zone in zones):
        return ()
    if name.startswith(DOMAINS_UNRESOLVED_PREFIXES):
        return ()
    if name.startswith(DOMAINS_ELSEWHERE_PREFIXES):
        return DOMAINS_ELSEWHERE_ADDRESSES
    return DOMAINS_MACHINE_ADDRESSES


def _domains_points_here(name: str, zones: frozenset[str]) -> bool:
    """
    Args:
        name: A domain.
        zones: The modelled DNS zones.

    Returns:
        True when every address the name resolves to is this machine's.
    """
    resolved = _domains_resolve(name, zones)
    return bool(resolved) and set(resolved) <= set(DOMAINS_MACHINE_ADDRESSES)


#: Arguments the simple directives of WASM's templates take: (fewest, most).
#: A lost semicolon joins two statements into one, which nginx reports as the
#: wrong number of arguments of the first.
_DOMAINS_NGINX_ARITY: dict[str, tuple[int, int]] = {
    "access_log": (1, 4),
    "add_header": (2, 3),
    "allow": (1, 1),
    "client_max_body_size": (1, 1),
    "deny": (1, 1),
    "error_log": (1, 2),
    "expires": (1, 2),
    "gzip": (1, 1),
    "gzip_comp_level": (1, 1),
    "gzip_proxied": (1, 9),
    "gzip_types": (1, 64),
    "gzip_vary": (1, 1),
    "include": (1, 1),
    "index": (1, 16),
    "listen": (1, 16),
    "proxy_cache_bypass": (1, 16),
    "proxy_connect_timeout": (1, 1),
    "proxy_http_version": (1, 1),
    "proxy_pass": (1, 1),
    "proxy_read_timeout": (1, 1),
    "proxy_send_timeout": (1, 1),
    "proxy_set_header": (2, 2),
    "return": (1, 2),
    "root": (1, 1),
    "server_name": (1, 64),
    "ssl_certificate": (1, 1),
    "ssl_certificate_key": (1, 1),
    "ssl_ciphers": (1, 1),
    "ssl_prefer_server_ciphers": (1, 1),
    "ssl_protocols": (1, 8),
    "ssl_session_cache": (1, 2),
    "ssl_session_timeout": (1, 1),
    "ssl_stapling": (1, 1),
    "ssl_stapling_verify": (1, 1),
    "try_files": (2, 16),
}


def _domains_nginx_syntax(text: str) -> tuple[str, int] | None:
    """
    Check a configuration the way nginx's parser does.

    The grammar - statements end in ``;``, blocks open with ``{`` after a
    directive and close with ``}``, strings are quoted, ``#`` starts a
    comment, ``${var}`` belongs to its token - and the argument count of the
    simple directives WASM's templates use. That is enough for the mistakes
    a hand edit makes (a lost semicolon, a lost brace), reported in nginx's
    own words.

    Args:
        text: The configuration.

    Returns:
        ``(message, line)`` for the first error, or None when it parses.
    """
    depth = 0
    statement: list[str] = []
    line = 1
    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        if char == "\n":
            line += 1
            index += 1
        elif char in " \t\r":
            index += 1
        elif char == "#":
            while index < length and text[index] != "\n":
                index += 1
        elif char == ";":
            if not statement:
                return 'unexpected ";"', line
            fewest, most = _DOMAINS_NGINX_ARITY.get(statement[0], (0, 1024))
            if not fewest <= len(statement) - 1 <= most:
                return f'invalid number of arguments in "{statement[0]}" directive', line
            statement = []
            index += 1
        elif char == "{":
            if not statement:
                return 'unexpected "{"', line
            depth += 1
            statement = []
            index += 1
        elif char == "}":
            if statement or depth == 0:
                return 'unexpected "}"', line
            depth -= 1
            index += 1
        elif char in "\"'":
            start, start_line = index, line
            index += 1
            while index < length and text[index] != char:
                if text[index] == "\\":
                    index += 1
                if index < length and text[index] == "\n":
                    line += 1
                index += 1
            if index >= length:
                return 'unexpected end of file, expecting ";" or "}"', start_line
            index += 1
            statement.append(text[start:index])
        else:
            start = index
            while index < length and text[index] not in " \t\r\n;{}\"'":
                if text.startswith("${", index):
                    close = text.find("}", index)
                    index = length if close == -1 else close + 1
                    continue
                index += 1
            statement.append(text[start:index])
    if statement:
        return 'unexpected end of file, expecting ";" or "}"', line
    if depth > 0:
        return 'unexpected end of file, expecting "}"', line
    return None


class _DomainsWebTools:
    """
    nginx's configuration test and certbot, answered from the sandbox's files.

    Attributes:
        sandbox: The sandbox.
        zones: The modelled DNS zones.
        lineages: Certificate name to the domains it covers.
        expiry: Certificate name to when it expires.
        revoked: Certificate names revoked but not deleted.
    """

    def __init__(self, sandbox: Sandbox, zones: frozenset[str]) -> None:
        """
        Args:
            sandbox: The sandbox.
            zones: The modelled DNS zones.
        """
        self.sandbox = sandbox
        self.zones = zones
        self.lineages: dict[str, list[str]] = {}
        self.expiry: dict[str, datetime] = {}
        self.revoked: set[str] = set()
        self._lock = threading.Lock()

    @property
    def live_dir(self) -> Path:
        """The sandbox's /etc/letsencrypt/live."""
        return self.sandbox.etc / "letsencrypt" / "live"

    def shown(self, path: Path | str) -> str:
        """
        Args:
            path: A path inside the sandbox.

        Returns:
            The path as the machine it stands in for would print it.
        """
        text = str(path)
        root = str(self.sandbox.root)
        return text[len(root) :] if text.startswith(root) else text

    def seed(self, names: Sequence[str]) -> None:
        """
        Issue the seeded certificates: the name and its www, the expiry
        :func:`make_runner` always gave them (12, 29, 46... days).

        Args:
            names: Certificate names, in the order the store seeded them.
        """
        for offset, name in enumerate(names):
            self._issue(
                name, [name, f"www.{name}"], datetime.now() + timedelta(days=12 + offset * 17)
            )

    def _issue(self, name: str, domains: list[str], expires: datetime) -> None:
        """
        Record a lineage and write its files.

        Args:
            name: Certificate name.
            domains: Domains it covers.
            expires: When it expires.
        """
        self.lineages[name] = list(domains)
        self.expiry[name] = expires
        self.revoked.discard(name)
        directory = self.live_dir / name
        directory.mkdir(parents=True, exist_ok=True)
        for file in ("fullchain.pem", "cert.pem", "chain.pem", "privkey.pem"):
            (directory / file).write_text(_DOMAINS_PEM, encoding="utf-8")

    def _forget(self, name: str) -> None:
        """
        Drop a lineage and its files.

        Args:
            name: Certificate name.
        """
        self.lineages.pop(name, None)
        self.expiry.pop(name, None)
        self.revoked.discard(name)
        shutil.rmtree(self.live_dir / name, ignore_errors=True)

    def answer(self, args: tuple[str, ...]) -> Any:
        """
        Answer a command this model owns.

        Args:
            args: The argv.

        Returns:
            A CommandResult, or None for a command it leaves to the runner.
        """
        if args[:1] == ("nginx",) and "-t" in args:
            return self._nginx_test(args)
        if args[:1] == ("certbot",) and len(args) > 1:
            with self._lock:
                return self._certbot(args)
        return None

    # -- nginx -----------------------------------------------------------

    def _nginx_test(self, args: tuple[str, ...]) -> Any:
        """
        ``nginx -t``, or ``nginx -t -c <wrapper>`` for a staged configuration.

        Args:
            args: The argv.

        Returns:
            nginx's answer.
        """
        from wasm.core.runner import CommandResult

        if "-c" in args and args.index("-c") + 1 < len(args):
            main = Path(args[args.index("-c") + 1])
            try:
                wrapper = main.read_text(encoding="utf-8")
            except OSError as exc:
                return CommandResult(
                    args,
                    1,
                    "",
                    f'nginx: [emerg] open() "{self.shown(main)}" failed ({exc.strerror})\n',
                )
            files = [Path(match) for match in re.findall(r"include\s+([^;\s]+);", wrapper)]
            main_shown = self.shown(main)
        else:
            enabled = self.sandbox.etc / "nginx" / "sites-enabled"
            files = sorted(enabled.iterdir()) if enabled.is_dir() else []
            main_shown = "/etc/nginx/nginx.conf"
        for file in files:
            try:
                text = file.read_text(encoding="utf-8")
            except OSError:
                continue
            problem = _domains_nginx_syntax(text)
            if problem is not None:
                message, line = problem
                return CommandResult(
                    args,
                    1,
                    "",
                    f"nginx: [emerg] {message} in {self.shown(file)}:{line}\n"
                    f"nginx: configuration file {main_shown} test failed\n",
                )
        return CommandResult(
            args,
            0,
            "",
            f"nginx: the configuration file {main_shown} syntax is ok\n"
            f"nginx: configuration file {main_shown} test is successful\n",
        )

    # -- certbot ---------------------------------------------------------

    def _certbot(self, args: tuple[str, ...]) -> Any:
        """
        Args:
            args: A certbot argv.

        Returns:
            certbot's answer, or None for a verb this model does not own.
        """
        verb = args[1]
        if verb == "certificates":
            return self._certificates(args)
        if verb == "certonly":
            return self._certonly(args)
        if verb == "renew":
            return self._renew(args)
        if verb == "revoke":
            return self._revoke(args)
        if verb == "delete":
            return self._delete(args)
        return None

    @staticmethod
    def _options(args: tuple[str, ...], flag: str) -> list[str]:
        """
        Args:
            args: The argv.
            flag: An option that takes a value.

        Returns:
            Every value given to it, in order.
        """
        return [args[i + 1] for i, arg in enumerate(args[:-1]) if arg == flag]

    def _certificates(self, args: tuple[str, ...]) -> Any:
        from wasm.core.runner import CommandResult

        now = datetime.now()
        blocks = [_DOMAINS_CERTBOT_LOG, "", "-" * 79]
        if not self.lineages:
            blocks = [_DOMAINS_CERTBOT_LOG, "", "No certificates found."]
            return CommandResult(args, 0, "\n".join(blocks) + "\n", "")
        blocks.append("Found the following certs:")
        for name, domains in self.lineages.items():
            expiry = self.expiry[name]
            days = (expiry - now).days
            state = (
                "INVALID: REVOKED"
                if name in self.revoked
                else ("INVALID: EXPIRED" if days < 0 else f"VALID: {days} days")
            )
            blocks += [
                f"  Certificate Name: {name}",
                "    Serial Number: 4a3f9c2e1b7d",
                "    Key Type: ECDSA",
                f"    Domains: {' '.join(domains)}",
                f"    Expiry Date: {expiry.strftime('%Y-%m-%d %H:%M:%S')}+00:00 ({state})",
                f"    Certificate Path: /etc/letsencrypt/live/{name}/fullchain.pem",
                f"    Private Key Path: /etc/letsencrypt/live/{name}/privkey.pem",
            ]
        blocks.append("-" * 79)
        return CommandResult(args, 0, "\n".join(blocks) + "\n", "")

    def _authenticator(self, args: tuple[str, ...]) -> str:
        for flag in ("--nginx", "--apache", "--webroot", "--standalone"):
            if flag in args:
                return flag.removeprefix("--")
        return "nginx"

    def _certonly(self, args: tuple[str, ...]) -> Any:
        from wasm.core.runner import CommandResult

        domains = self._options(args, "-d")
        names = self._options(args, "--cert-name")
        name = names[0] if names else (domains[0] if domains else "")
        if not domains:
            return CommandResult(
                args, 1, "", "certbot: error: at least one domain must be given with -d\n"
            )
        requested = (
            f"{domains[0]}"
            if len(domains) == 1
            else f"{domains[0]} and {len(domains) - 1} more domains"
        )
        problems = []
        for domain in domains:
            resolved = _domains_resolve(domain, self.zones)
            if not resolved:
                problems.append(
                    f"  Domain: {domain}\n  Type:   dns\n"
                    f"  Detail: DNS problem: NXDOMAIN looking up A for {domain} - check that "
                    "a DNS record exists for this domain; DNS problem: NXDOMAIN looking up "
                    f"AAAA for {domain} - check that a DNS record exists for this domain"
                )
            elif not _domains_points_here(domain, self.zones):
                problems.append(
                    f"  Domain: {domain}\n  Type:   unauthorized\n"
                    f"  Detail: {resolved[0]}: Invalid response from "
                    f"http://{domain}/.well-known/acme-challenge/"
                    "b3NfUvw9GQeD1Yq8hZk2xJpTcLmR: 404"
                )
        if problems:
            authenticator = self._authenticator(args)
            hint = _DOMAINS_CERTBOT_HINTS.get(authenticator, _DOMAINS_CERTBOT_HINTS["nginx"])
            hint = hint.replace("{server}", authenticator)
            return CommandResult(
                args,
                1,
                "",
                f"{_DOMAINS_CERTBOT_LOG}\nRequesting a certificate for {requested}\n\n"
                f"Certbot failed to authenticate some domains (authenticator: {authenticator})."
                " The Certificate Authority reported these problems:\n"
                + "\n\n".join(problems)
                + f"\n\nHint: {hint}\n\nSome challenges have failed.\nAsk for help or search "
                "for solutions at https://community.letsencrypt.org. See the logfile "
                "/var/log/letsencrypt/letsencrypt.log or re-run Certbot with -v for more "
                "details.\n",
            )
        if "--dry-run" in args:
            return CommandResult(
                args,
                0,
                f"{_DOMAINS_CERTBOT_LOG}\nSimulating a certificate request for {requested}\n"
                "The dry run was successful.\n",
                "",
            )
        expires = datetime.now() + timedelta(days=90)
        self._issue(name, domains, expires)
        return CommandResult(
            args,
            0,
            f"{_DOMAINS_CERTBOT_LOG}\nRequesting a certificate for {requested}\n\n"
            "Successfully received certificate.\n"
            f"Certificate is saved at: /etc/letsencrypt/live/{name}/fullchain.pem\n"
            f"Key is saved at:         /etc/letsencrypt/live/{name}/privkey.pem\n"
            f"This certificate expires on {expires.strftime('%Y-%m-%d')}.\n"
            "These files will be updated when the certificate renews.\n"
            "Certbot has set up a scheduled task to automatically renew this certificate "
            "in the background.\n",
            "",
        )

    def _renew(self, args: tuple[str, ...]) -> Any:
        from wasm.core.runner import CommandResult

        names = self._options(args, "--cert-name") or list(self.lineages)
        unknown = [name for name in names if name not in self.lineages]
        if unknown:
            return CommandResult(
                args,
                1,
                "",
                f"No certificate found with name {unknown[0]} (expected "
                f"/etc/letsencrypt/renewal/{unknown[0]}.conf).\n",
            )
        force = "--force-renewal" in args
        dry = "--dry-run" in args
        renewed, skipped = [], []
        for name in names:
            days = (self.expiry[name] - datetime.now()).days
            if force or days < 30:
                if not dry:
                    self._issue(name, self.lineages[name], datetime.now() + timedelta(days=90))
                renewed.append(name)
            else:
                skipped.append(name)
        rule = "- " * 39 + "-"
        lines = [_DOMAINS_CERTBOT_LOG, ""]
        for name in names:
            lines += [rule, f"Processing /etc/letsencrypt/renewal/{name}.conf", rule]
            if name in renewed:
                lines.append(
                    f"Renewing an existing certificate for {' and '.join(self.lineages[name][:2])}"
                )
            else:
                lines.append("Certificate not yet due for renewal")
            lines.append("")
        lines.append(rule)
        if skipped:
            lines.append("The following certificates are not due for renewal yet:")
            lines += [
                f"  /etc/letsencrypt/live/{name}/fullchain.pem expires on "
                f"{self.expiry[name].strftime('%Y-%m-%d')} (skipped)"
                for name in skipped
            ]
        if renewed:
            lines.append(
                "Congratulations, all simulated renewals succeeded:"
                if dry
                else "Congratulations, all renewals succeeded:"
            )
            lines += [f"  /etc/letsencrypt/live/{name}/fullchain.pem (success)" for name in renewed]
        else:
            lines.append("No renewals were attempted.")
        lines.append(rule)
        return CommandResult(args, 0, "\n".join(lines) + "\n", "")

    def _revoke(self, args: tuple[str, ...]) -> Any:
        from wasm.core.runner import CommandResult

        paths = self._options(args, "--cert-path")
        name = Path(paths[0]).parent.name if paths else ""
        if name not in self.lineages:
            return CommandResult(
                args,
                1,
                "",
                f"{_DOMAINS_CERTBOT_LOG}\nCould not read the certificate at {paths[:1]}.\n",
            )
        if "--delete-after-revoke" in args:
            self._forget(name)
        else:
            self.revoked.add(name)
        return CommandResult(
            args,
            0,
            f"{_DOMAINS_CERTBOT_LOG}\nCongratulations! You have successfully revoked the "
            f"certificate that was located at /etc/letsencrypt/live/{name}/fullchain.pem.\n",
            "",
        )

    def _delete(self, args: tuple[str, ...]) -> Any:
        from wasm.core.runner import CommandResult

        names = self._options(args, "--cert-name")
        name = names[0] if names else ""
        if name not in self.lineages:
            return CommandResult(
                args,
                1,
                "",
                f"No certificate found with name {name} (expected "
                f"/etc/letsencrypt/renewal/{name}.conf).\n",
            )
        self._forget(name)
        return CommandResult(
            args,
            0,
            f"{_DOMAINS_CERTBOT_LOG}\nDeleted all files relating to certificate {name}.\n",
            "",
        )


def _domains_redirect_managers() -> None:
    """
    Point the nginx and Apache managers' default backends at the sandbox.

    :func:`redirect_system_paths` rebinds the backends on
    ``wasm.managers.webserver``, but ``NginxManager`` and ``ApacheManager``
    imported them by name before that, so a manager built without a backend
    (the sites API, a deployer rendering its site) still read and wrote the
    real /etc/nginx.
    """
    import wasm.managers.apache_manager as apache_module
    import wasm.managers.nginx_manager as nginx_module
    import wasm.managers.webserver as webserver_module

    nginx_module.NGINX_BACKEND = webserver_module.NGINX_BACKEND  # type: ignore[attr-defined]
    apache_module.APACHE_BACKEND = webserver_module.APACHE_BACKEND  # type: ignore[attr-defined]
    for manager, backend in (
        (nginx_module.NginxManager, webserver_module.NGINX_BACKEND),
        (apache_module.ApacheManager, webserver_module.APACHE_BACKEND),
    ):
        manager.SITES_AVAILABLE = backend.sites_available
        manager.SITES_ENABLED = backend.sites_enabled


def _domains_site_files(store: Any) -> None:
    """
    Give every seeded site that has no configuration file one, rendered by
    the manager's own template, and enable the ones the store says are.

    Args:
        store: The seeded store.
    """
    from wasm.managers.nginx_manager import NginxManager

    manager = NginxManager(verbose=False)
    apps = {app.id: app for app in store.list_apps()}
    for site in store.list_sites():
        if site.webserver != "nginx":
            continue
        app = apps.get(site.app_id)
        static = app is not None and app.app_type == "static"
        if not manager.site_exists(site.domain):
            manager.create_site(
                site.domain,
                "static" if static else "proxy",
                {
                    "port": site.proxy_port or 3000,
                    "ssl": bool(site.ssl_certificate),
                    "app_path": f"/var/www/apps/{site.domain}",
                },
            )
        if site.enabled and not manager.site_enabled(site.domain):
            manager.enable_site(site.domain)


def _domains_wizard_sources(sandbox: Sandbox) -> None:
    """
    Write the projects the new-app wizard is pointed at, in /var/www/src.

    ``storefront`` is a Next.js project whose ``.env.example`` has required
    values, defaults and credentials; ``landing`` is a static site.

    Args:
        sandbox: The sandbox.
    """
    root = sandbox.var / "www" / "src"
    storefront = root / WIZARD_SOURCES[0]
    (storefront / "app").mkdir(parents=True, exist_ok=True)
    (storefront / "public" / "uploads").mkdir(parents=True, exist_ok=True)
    (storefront / "package.json").write_text(
        json.dumps(
            {
                "name": "storefront",
                "version": "1.4.2",
                "private": True,
                "scripts": {"dev": "next dev", "build": "next build", "start": "next start"},
                "dependencies": {"next": "15.2.4", "react": "19.0.0", "react-dom": "19.0.0"},
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    (storefront / "package-lock.json").write_text(
        '{\n  "name": "storefront",\n  "lockfileVersion": 3,\n  "requires": true,\n'
        '  "packages": {}\n}\n',
        encoding="utf-8",
    )
    (storefront / "next.config.js").write_text(
        "/** @type {import('next').NextConfig} */\nmodule.exports = { output: 'standalone' };\n",
        encoding="utf-8",
    )
    (storefront / ".env.example").write_text(
        "# Orders and customers\n"
        "DATABASE_URL=\n"
        "# Signs session cookies\n"
        "NEXTAUTH_SECRET=\n"
        "NEXTAUTH_URL=https://example.com\n"
        "STRIPE_SECRET_KEY=\n"
        "SMTP_HOST=smtp.example.com\n"
        "SMTP_PASSWORD=\n"
        "UPLOADS_DIR=public/uploads\n"
        "LOG_LEVEL=info\n",
        encoding="utf-8",
    )
    (storefront / "app" / "page.js").write_text(
        "export default function Home() {\n  return <h1>Storefront</h1>;\n}\n", encoding="utf-8"
    )
    (storefront / "public" / "uploads" / ".gitkeep").write_text("", encoding="utf-8")

    landing = root / WIZARD_SOURCES[1]
    landing.mkdir(parents=True, exist_ok=True)
    (landing / "index.html").write_text(
        '<!doctype html>\n<html lang="en">\n<head><meta charset="utf-8"><title>Landing</title>'
        '<link rel="stylesheet" href="styles.css"></head>\n<body><h1>Coming soon</h1></body>\n'
        "</html>\n",
        encoding="utf-8",
    )
    (landing / "styles.css").write_text("body { font-family: system-ui; }\n", encoding="utf-8")


def seed_domains_and_sources(
    sandbox: Sandbox, store: Any, units: dict[str, Unit], cert_domains: list[str]
) -> None:
    """
    Seed the web server, certificates, DNS and sources the domain pages and the wizard need.

    Temporary files move into the sandbox too: the wizard's inspection clones
    into one, and the web server check stages the configuration it tests in
    another, which the modelled nginx has to be able to read. nginx itself
    runs, as a system unit WASM does not own: a deploy's pre-flight checks
    refuse to start on a machine whose web server is down.

    Args:
        sandbox: The sandbox.
        store: The seeded store.
        units: The modelled units, given the nginx one.
        cert_domains: The domains the store seeded with a certificate.
    """
    import functools
    import socket as socket_module

    import wasm.deployers.domains as domains_core
    import wasm.web.api.domains as domains_api
    from wasm.core.runner import get_runner

    units.setdefault("nginx", Unit(active="active", pid=880, managed=False))
    scratch = sandbox.root / "tmp"
    scratch.mkdir(mode=0o700, exist_ok=True)
    tempfile.tempdir = str(scratch)
    _domains_redirect_managers()

    zones = frozenset(".".join(app.domain.split(".")[-2:]) for app in store.list_apps())
    tools = _DomainsWebTools(sandbox, zones)
    tools.seed(cert_domains)

    runner = get_runner()
    original = runner.run

    def run(argv: Sequence[str], **kwargs: Any) -> Any:
        args = tuple(str(a) for a in argv)
        answer = tools.answer(args)
        if answer is None:
            return original(argv, **kwargs)
        runner.calls.append(args)
        return answer

    runner.run = run  # type: ignore[method-assign]

    def resolver(host: str, port: Any, *args: Any, **kwargs: Any) -> list[tuple[Any, ...]]:
        addresses = _domains_resolve(host, zones)
        if not addresses:
            raise socket_module.gaierror(socket_module.EAI_NONAME, "Name or service not known")
        return [
            (
                socket_module.AF_INET6 if ":" in address else socket_module.AF_INET,
                socket_module.SOCK_STREAM,
                6,
                "",
                (address, 0),
            )
            for address in addresses
        ]

    domains_api.check_dns = functools.partial(  # type: ignore[assignment]
        domains_core.check_dns,
        resolver=resolver,
        local_addresses=lambda: DOMAINS_MACHINE_ADDRESSES,
    )

    _domains_site_files(store)
    _domains_wizard_sources(sandbox)


# ---------------------------------------------------------------------------
# Settings (the console's Settings pages)
# ---------------------------------------------------------------------------

#: Seeded API tokens: (name, scope, lifetime in hours or None, revoked).
SETTINGS_API_TOKENS: tuple[tuple[str, str, int | None, bool], ...] = (
    ("ci-deploy", "deploy", 90 * 24, False),
    ("grafana-read", "read", None, False),
    ("old-backup-script", "admin", None, True),
)


def seed_settings_api_tokens() -> None:
    """
    Issue the API tokens the Settings > API tokens page lists.

    A live deploy token, a read token that never expires and a revoked admin
    one, so each state the table draws exists. The clear tokens are discarded:
    nothing in the suite authenticates with them.
    """
    from wasm.web.server import get_token_manager

    manager = get_token_manager()
    for name, scope, hours, revoked in SETTINGS_API_TOKENS:
        issued = manager.create_api_token(name, scope, hours)
        if revoked:
            manager.revoke_api_token(int(issued["id"]))


def pin_settings_update_check() -> str:
    """
    Answer the update check from the sandbox instead of GitHub.

    ``GET /api/system/version`` asks the GitHub releases API when its cache is
    older than five minutes, which would make the About page depend on the
    network and on whatever was released last. The latest release is pinned
    to the next minor version of the one installed, so the page always shows
    an update to offer.

    Returns:
        The version the check reports as released.
    """
    from wasm import __version__
    from wasm.core.update_checker import UpdateChecker

    numbers = [int(part) for part in re.findall(r"\d+", __version__)[:2]] + [0, 0]
    latest = f"{numbers[0]}.{numbers[1] + 1}.0"

    def fetch_latest(cls: type[UpdateChecker]) -> str:
        """Report the pinned release."""
        return latest

    UpdateChecker._fetch_latest_version = classmethod(fetch_latest)  # type: ignore[method-assign,assignment]
    return latest


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
    seed_settings_api_tokens()
    pin_settings_update_check()

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
