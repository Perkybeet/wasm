# Copyright (c) 2024-2025 Yago López Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
The inventory database.

WASM keeps what it deployed in a small SQLite file so that a later command does
not have to re-derive it from nginx configs and unit files. These subcommands
create that file, fill it from a server that was set up before WASM existed,
reconcile it with what systemd actually reports, and dump it.

Both entry points, the Click commands below and the legacy
:func:`handle_store` that ``wasm.cli.parser`` still calls, run the same private
functions, so the two paths cannot drift apart while the migration finishes.
"""

from __future__ import annotations

import json
import re
import sqlite3
from argparse import Namespace
from pathlib import Path

import click

from wasm.cli.app import Context, pass_context
from wasm.core.config import Config, secure_write
from wasm.core.exceptions import ConfigError, WASMError
from wasm.core.logger import Logger

#: Where a proxying vhost declares the port the application listens on.
_PROXY_PASS = re.compile(r"proxy_pass\s+http://(?:127\.0\.0\.1|localhost):(\d+)")


def _detect_app_type(app_path: Path) -> str:
    """
    Work out what kind of project lives in a directory.

    Detection order, most specific first: a Next.js config or dependency, a Vite
    config or dependency, any ``package.json`` at all, a Python project marker,
    then a bare ``index.html``.

    Args:
        app_path: Application root.

    Returns:
        One of nextjs, vite, nodejs, python, static or unknown.
    """
    next_configs = ["next.config.js", "next.config.ts", "next.config.mjs"]
    for config_file in next_configs:
        if (app_path / config_file).exists():
            return "nextjs"

    package_json = app_path / "package.json"
    if package_json.exists():
        try:
            pkg = json.loads(package_json.read_text(encoding="utf-8"))
            all_deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
            if "next" in all_deps:
                return "nextjs"
            if "vite" in all_deps:
                return "vite"
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, AttributeError):
            # A malformed package.json only costs precision here; the directory
            # is still a Node.js project and is reported as one below.
            pass

    vite_configs = ["vite.config.js", "vite.config.ts"]
    for config_file in vite_configs:
        if (app_path / config_file).exists():
            return "vite"

    if package_json.exists():
        return "nodejs"

    python_markers = ["requirements.txt", "setup.py", "pyproject.toml", "Pipfile"]
    for marker in python_markers:
        if (app_path / marker).exists():
            return "python"

    if (app_path / "index.html").exists():
        return "static"

    return "unknown"


def _store_init(verbose: bool) -> int:
    """
    Create the database, or reopen an existing one.

    The schema is created with ``IF NOT EXISTS``, so running this on a populated
    store is safe and keeps every record.

    Args:
        verbose: Print the detail of each step.

    Returns:
        Exit code.

    Raises:
        ConfigError: If the database cannot be created or opened.
    """
    logger = Logger(verbose=verbose)

    from wasm.core.store import WASMStore, get_store

    logger.header("WASM Store Initialization")

    try:
        WASMStore.reset_instance()
        store = get_store()
    except (OSError, sqlite3.Error) as exc:
        raise ConfigError(
            f"Failed to initialize store: {exc}",
            details="Check that WASM can write to /var/lib/wasm or ~/.local/share/wasm.",
        ) from exc

    logger.success(f"Store initialized at: {store.db_path}")
    logger.info("Schema version: 1")
    return 0


def _store_stats(json_output: bool, verbose: bool) -> int:
    """
    Count what the store holds.

    Args:
        json_output: Emit the counts as JSON instead of a report.
        verbose: Print the detail of each step.

    Returns:
        Exit code.

    Raises:
        ConfigError: If the store cannot be read.
    """
    logger = Logger(verbose=verbose)

    from wasm.core.store import get_store

    try:
        store = get_store()
        stats = store.get_statistics()
    except (OSError, sqlite3.Error, KeyError) as exc:
        raise ConfigError(
            f"Failed to get statistics: {exc}",
            details="Run 'wasm store init' to create the database.",
        ) from exc

    if json_output:
        click.echo(json.dumps(stats, indent=2))
        return 0

    logger.header("WASM Store Statistics")
    logger.blank()

    logger.key_value("Database Path", str(store.db_path))
    logger.blank()

    logger.info("Resources:")
    logger.key_value("  Applications", str(stats["total_apps"]))
    logger.key_value("    Running", str(stats["running_apps"]))
    logger.key_value("  Sites", str(stats["total_sites"]))
    logger.key_value("  Services", str(stats["total_services"]))
    logger.key_value("  Databases", str(stats["total_databases"]))

    if stats["apps_by_type"]:
        logger.blank()
        logger.info("Applications by Type:")
        for app_type, count in stats["apps_by_type"].items():
            logger.key_value(f"  {app_type}", str(count))

    if stats["databases_by_engine"]:
        logger.blank()
        logger.info("Databases by Engine:")
        for engine, count in stats["databases_by_engine"].items():
            logger.key_value(f"  {engine}", str(count))

    return 0


def _store_import(verbose: bool) -> int:
    """
    Record applications this server was already running.

    Scans the nginx and Apache vhosts and the systemd units, and adds anything
    the store does not know about yet. Existing records are left alone, except
    for an application whose type was never worked out.

    Args:
        verbose: Print the detail of each step.

    Returns:
        Exit code.
    """
    logger = Logger(verbose=verbose)

    from wasm.core.config import (
        APACHE_SITES_AVAILABLE,
        NGINX_SITES_AVAILABLE,
        NGINX_SITES_ENABLED,
        SYSTEMD_DIR,
    )
    from wasm.core.store import App, AppStatus, Service, Site, get_store
    from wasm.core.utils import domain_to_app_name

    store = get_store()
    config = Config()

    logger.header("Import Legacy Applications")
    logger.blank()

    imported_apps = 0
    imported_sites = 0
    imported_services = 0

    if NGINX_SITES_AVAILABLE.exists():
        logger.step(1, 3, "Scanning Nginx sites")
        for config_file in NGINX_SITES_AVAILABLE.iterdir():
            if config_file.is_file() and config_file.name != "default":
                domain = config_file.name
                site_exists = store.get_site(domain) is not None

                enabled = (NGINX_SITES_ENABLED / domain).exists()

                is_static = False
                proxy_port = None
                try:
                    content = config_file.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError) as exc:
                    # A vhost we cannot read is still worth recording; only the
                    # port and the static/proxy distinction are lost.
                    logger.debug(f"Could not read {config_file}: {exc}")
                else:
                    match = _PROXY_PASS.search(content)
                    if match:
                        proxy_port = int(match.group(1))
                    elif "proxy_pass" not in content:
                        is_static = True

                # The directory may be named after the domain, after the
                # sanitised app name, or with the historical wasm- prefix.
                app_name = domain_to_app_name(domain)
                possible_paths = [
                    config.apps_directory / app_name,
                    config.apps_directory / domain,
                    config.apps_directory / f"wasm-{app_name}",
                ]

                app_path = None
                for path in possible_paths:
                    if path.exists():
                        app_path = path
                        break

                app_id = None
                if app_path:
                    app_type = _detect_app_type(app_path)

                    existing_app = store.get_app(domain)
                    if not existing_app:
                        app = App(
                            domain=domain,
                            app_type=app_type,
                            app_path=str(app_path),
                            webserver="nginx",
                            ssl_enabled=(NGINX_SITES_ENABLED / domain).exists(),
                            status=AppStatus.UNKNOWN.value,
                            is_static=is_static,
                            port=proxy_port,
                        )
                        app = store.create_app(app)
                        app_id = app.id
                        imported_apps += 1
                        logger.substep(f"Imported app: {domain} ({app_type})")
                    elif existing_app.app_type == "unknown" and app_type != "unknown":
                        existing_app.app_type = app_type
                        store.update_app(existing_app)
                        app_id = existing_app.id
                        imported_apps += 1
                        logger.substep(f"Updated app type: {domain} -> {app_type}")
                    else:
                        app_id = existing_app.id

                if not site_exists:
                    site = Site(
                        app_id=app_id,
                        domain=domain,
                        webserver="nginx",
                        config_path=str(config_file),
                        enabled=enabled,
                        is_static=is_static,
                        proxy_port=proxy_port,
                    )
                    store.create_site(site)
                    imported_sites += 1
    else:
        logger.step(1, 3, "No Nginx sites found")

    if APACHE_SITES_AVAILABLE.exists():
        logger.step(2, 3, "Scanning Apache sites")
        for config_file in APACHE_SITES_AVAILABLE.iterdir():
            if config_file.is_file() and not config_file.name.startswith("000-"):
                domain = config_file.name.replace(".conf", "")

                if store.get_site(domain):
                    continue

                site = Site(
                    domain=domain,
                    webserver="apache",
                    config_path=str(config_file),
                    enabled=True,
                )
                store.create_site(site)
                imported_sites += 1
    else:
        logger.step(2, 3, "No Apache sites found")

    logger.step(3, 3, "Scanning systemd services")
    if SYSTEMD_DIR.exists():
        service_files = list(SYSTEMD_DIR.glob("wasm-*.service"))
        # Units deployed after the rename carry no prefix, so they are matched
        # on shape instead: a domain turned into a name always has a hyphen,
        # and a template unit (with @) never is one.
        for unit in SYSTEMD_DIR.glob("*.service"):
            name = unit.stem
            if name.startswith("wasm-") or "-" not in name:
                continue
            if "@" not in name and name.count("-") >= 1:
                service_files.append(unit)

        for service_file in service_files:
            service_name = service_file.stem
            app_name = service_name[5:] if service_name.startswith("wasm-") else service_name

            if store.get_service(app_name):
                continue

            working_dir = ""
            command = ""
            user = "www-data"
            group = "www-data"
            port = None
            env: dict[str, str] = {}

            try:
                content = service_file.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                logger.debug(f"Could not read {service_file}: {exc}")
            else:
                wd_match = re.search(r"WorkingDirectory=(.+)", content)
                if wd_match:
                    working_dir = wd_match.group(1)

                exec_match = re.search(r"ExecStart=(.+)", content)
                if exec_match:
                    command = exec_match.group(1)

                user_match = re.search(r"User=(.+)", content)
                if user_match:
                    user = user_match.group(1)

                group_match = re.search(r"Group=(.+)", content)
                if group_match:
                    group = group_match.group(1)

                for env_match in re.finditer(r"Environment=\"?([^=]+)=([^\"]+)\"?", content):
                    env[env_match.group(1)] = env_match.group(2)
                    if env_match.group(1) == "PORT" and env_match.group(2).isdigit():
                        port = int(env_match.group(2))

            # A unit is named after the domain with the dots turned into
            # hyphens, so this is how a service finds the app it belongs to.
            linked_app = store.get_app(app_name.replace("-", "."))
            app_id = linked_app.id if linked_app else None

            service = Service(
                app_id=app_id,
                name=app_name,
                unit_file=str(service_file),
                working_directory=working_dir,
                command=command,
                user=user,
                group=group,
                port=port,
                environment=env,
            )
            store.create_service(service)
            imported_services += 1
            logger.substep(f"Imported service: {app_name}")

    logger.blank()
    logger.success("Import complete!")
    logger.key_value("  Apps", str(imported_apps))
    logger.key_value("  Sites", str(imported_sites))
    logger.key_value("  Services", str(imported_services))

    return 0


def _store_export(output: str | None, verbose: bool) -> int:
    """
    Dump the whole store as JSON.

    A service record carries the environment it runs with, so the dump can
    contain credentials. Written to a file it is created 0600; written to the
    terminal it is in clear, which is what a pipe into another tool needs.

    Args:
        output: Destination path, or None to write to stdout.
        verbose: Print the detail of each step.

    Returns:
        Exit code.

    Raises:
        ConfigError: If the store cannot be read.
        SecurityError: If the destination is a symlink.
    """
    logger = Logger(verbose=verbose)

    from wasm.core.store import get_store

    try:
        store = get_store()
        data = {
            "apps": [app.to_dict() for app in store.list_apps()],
            "sites": [site.to_dict() for site in store.list_sites()],
            "services": [svc.to_dict() for svc in store.list_services()],
            "databases": [db.to_dict() for db in store.list_databases()],
            "statistics": store.get_statistics(),
        }
    except (OSError, sqlite3.Error, KeyError) as exc:
        raise ConfigError(
            f"Export failed: {exc}",
            details="Run 'wasm store init' to create the database.",
        ) from exc

    payload = json.dumps(data, indent=2, default=str)

    if output:
        # secure_parent is off: the operator chose this directory and it is not
        # WASM's to lock down.
        secure_write(Path(output), payload, secure_parent=False)
        logger.success(f"Exported to: {output}")
        logger.info("The dump may contain service credentials; it is owner-readable only.")
    else:
        click.echo(payload)

    return 0


def _store_sync(verbose: bool) -> int:
    """
    Match the recorded service states to what systemd reports.

    Args:
        verbose: Print the detail of each step.

    Returns:
        Exit code.
    """
    logger = Logger(verbose=verbose)

    from wasm.core.store import AppStatus, get_store
    from wasm.managers.service_manager import ServiceManager

    store = get_store()
    service_manager = ServiceManager(verbose=verbose)

    logger.header("Sync Service States")

    synced_services = 0
    synced_apps = 0

    for service in store.list_services():
        systemd_status = service_manager.get_status(service.name)

        active = systemd_status.get("active", False)
        enabled = systemd_status.get("enabled", False)

        if (service.status == "active") != active or service.enabled != enabled:
            store.update_service_status(service.name, active=active, enabled=enabled)
            logger.substep(f"Updated service {service.name}: active={active}, enabled={enabled}")
            synced_services += 1

        if service.app_id:
            app = store.get_app_by_id(service.app_id)
            if app:
                new_status = AppStatus.RUNNING.value if active else AppStatus.STOPPED.value
                if app.status != new_status:
                    store.update_app_status(app.domain, new_status)
                    logger.substep(f"Updated app {app.domain}: {new_status}")
                    synced_apps += 1

    logger.blank()
    logger.success(f"Sync complete! Updated {synced_services} services, {synced_apps} apps.")

    return 0


def _store_path() -> int:
    """
    Print where the database file lives.

    Returns:
        Exit code.
    """
    from wasm.core.store import get_store

    click.echo(str(get_store().db_path))
    return 0


@click.group(name="store")
def cli() -> None:
    """Inspect and maintain the database WASM records deployments in."""


@cli.command("init")
@pass_context
def init(state: Context) -> None:
    """Create the database, or reopen an existing one without touching it."""
    _store_init(state.verbose)


@cli.command("stats")
@pass_context
def stats(state: Context) -> None:
    """Count the applications, sites, services and databases on record."""
    _store_stats(state.json_output, state.verbose)


@cli.command("import")
@pass_context
def import_(state: Context) -> None:
    """Record applications this server was already running before WASM."""
    _store_import(state.verbose)


@cli.command("export")
@click.option(
    "-o",
    "--output",
    default=None,
    type=click.Path(dir_okay=False, writable=True, path_type=str),
    help="Write to this file instead of stdout.",
)
@pass_context
def export(state: Context, output: str | None) -> None:
    """Dump every record as JSON, to a file or to stdout."""
    _store_export(output, state.verbose)


@cli.command("sync")
@pass_context
def sync(state: Context) -> None:
    """Match the recorded service states to what systemd reports."""
    _store_sync(state.verbose)


@cli.command("path")
def path() -> None:
    """Print the location of the database file."""
    _store_path()


def handle_store(args: Namespace) -> int:
    """
    Run a store action from the argparse namespace.

    Kept while ``wasm.cli.parser`` is still wired to argparse. It shares every
    private function with the Click commands above.

    Args:
        args: Parsed command-line arguments.

    Returns:
        Exit code (0 for success, non-zero for error).
    """
    verbose = getattr(args, "verbose", False)
    logger = Logger(verbose=verbose)

    action = getattr(args, "action", None)
    if not action:
        logger.error("store requires an action", details="Use: wasm store --help")
        return 1

    try:
        if action == "init":
            return _store_init(verbose)
        if action == "stats":
            return _store_stats(getattr(args, "json", False), verbose)
        if action == "import":
            return _store_import(verbose)
        if action == "export":
            return _store_export(getattr(args, "output", None), verbose)
        if action == "sync":
            return _store_sync(verbose)
        if action == "path":
            return _store_path()
    except WASMError as exc:
        logger.error(exc.message, details=exc.details)
        return 1

    logger.error(f"Unknown store action: {action}", details="Use: wasm store --help")
    return 1
