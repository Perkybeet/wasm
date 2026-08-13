# Copyright (c) 2024-2026 Yago Lopez Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
The nine commands that act on a deployed application.

They are top level (``wasm create``, ``wasm logs``) rather than nested under a
``webapp`` group because that is how they have always been typed. The Click
group below only exists as a container: :mod:`wasm.cli.app` picks the command
whose name the user typed out of it.

Each command is a thin shell around a private function that takes explicit
arguments. That seam is what lets the argparse entry point
(:func:`handle_webapp`, still called by :mod:`wasm.cli.parser`) and the Click
commands share one implementation instead of drifting into two.

Everything this module needs is imported here rather than inside the handlers.
An import that only exists inside one function is a NameError waiting for the
next caller, which is exactly how ``site delete`` lost its certificate cleanup.
"""

from __future__ import annotations

import re
import sys
import time
from argparse import Namespace
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeVar

import click

from wasm.cli.app import Context, enable_dry_run, pass_context
from wasm.core.app_state import RUNNING, STATIC, resolve_states
from wasm.core.config import Config
from wasm.core.dependencies import check_deployment_ready
from wasm.core.exceptions import DeploymentError, ServiceError, WASMError
from wasm.core.logger import Logger, set_colors_disabled, state, styled
from wasm.core.runner import (
    CommandResult,
    get_runner,
)
from wasm.core.store import get_store
from wasm.core.utils import domain_to_app_name, remove_directory
from wasm.deployers import detect_app_type, get_deployer
from wasm.deployers.docker_compose import DockerComposeDeployer
from wasm.deployers.monorepo import MonorepoDeployer
from wasm.deployers.registry import available_types
from wasm.managers.apache_manager import ApacheManager
from wasm.managers.backup_manager import RollbackManager
from wasm.managers.cert_manager import CertManager
from wasm.managers.nginx_manager import NginxManager
from wasm.managers.service_manager import ServiceManager
from wasm.managers.source_manager import SourceManager
from wasm.validators.domain import should_include_www, validate_domain
from wasm.validators.port import find_available_port, validate_port

# Constants for .env file parsing
MAX_ENV_FILE_SIZE = 1024 * 1024  # 1MB max
MAX_ENV_LINE_LENGTH = 10000
VALID_ENV_KEY_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

#: Following logs is interactive and ends with Ctrl+C, but the runner insists
#: on a deadline. A day is long enough to be indistinguishable from forever.
_FOLLOW_TIMEOUT = 86400

#: Docker Compose pulls images and rebuilds; it needs room.
_COMPOSE_TIMEOUT = 1800

#: Application types ``create`` accepts, in the order they are offered. Read
#: from the registry rather than typed out here: a hand-written copy is a copy
#: that goes stale the first time a deployer is added, and it did.
APP_TYPES = [entry["type"] for entry in available_types()]

#: Node package managers ``create`` and ``update`` accept.
PACKAGE_MANAGERS = ["npm", "pnpm", "bun", "auto"]

#: Web servers a site can be fronted by.
WEBSERVERS = ["nginx", "apache"]

#: Present participle and past participle of each service operation, so the
#: three commands that only differ in a verb are one implementation.
_SERVICE_VERBS: dict[str, tuple[str, str]] = {
    "start": ("Starting", "started"),
    "stop": ("Stopping", "stopped"),
    "restart": ("Restarting", "restarted"),
}

_F = TypeVar("_F", bound=Callable[..., Any])


def _follow(argv: list[str], cwd: Path | None = None) -> CommandResult | None:
    """
    Stream a long-running command until the user interrupts it.

    Args:
        argv: Program and arguments.
        cwd: Working directory.

    Returns:
        The command outcome, or None when the user pressed Ctrl+C.
    """
    try:
        return get_runner().stream(argv, on_line=print, cwd=cwd, timeout=_FOLLOW_TIMEOUT)
    except KeyboardInterrupt:
        return None


def _read_env_file(env_file: Path, logger: Logger) -> dict[str, str]:
    """
    Read KEY=value pairs from an environment file.

    Malformed lines are reported and skipped rather than aborting the
    deployment, because one stray line in a long file should not cost the user
    the whole run.

    Args:
        env_file: File to read.
        logger: Logger of the current command.

    Returns:
        The variables found, in file order.

    Raises:
        DeploymentError: When the file is missing, too large to be an
            environment file, or cannot be decoded.
    """
    if not env_file.exists():
        raise DeploymentError(
            f"Environment file not found: {env_file}",
            details="Check the path given to --env-file, or omit the option.",
        )

    file_size = env_file.stat().st_size
    if file_size > MAX_ENV_FILE_SIZE:
        raise DeploymentError(
            f"Environment file too large: {file_size} bytes (max: {MAX_ENV_FILE_SIZE})",
            details="An environment file holds KEY=value lines. Check you did "
            "not pass a build artefact or an archive by mistake.",
        )

    env_vars: dict[str, str] = {}
    try:
        with open(env_file) as handle:
            for line_num, raw_line in enumerate(handle, 1):
                if len(raw_line) > MAX_ENV_LINE_LENGTH:
                    logger.warning(f"Line {line_num} exceeds max length, skipping")
                    continue

                line = raw_line.strip()
                if not line or line.startswith("#"):
                    continue

                if "=" not in line:
                    logger.warning(f"Line {line_num}: invalid format (no '='), skipping")
                    continue

                key, value = line.split("=", 1)
                key = key.strip()
                value = value.strip()

                if not VALID_ENV_KEY_PATTERN.match(key):
                    logger.warning(f"Line {line_num}: invalid key '{key}', skipping")
                    continue

                # Quotes survive into systemd's Environment= and break it, so
                # they are stripped here rather than at the service file.
                if value.startswith('"') and value.endswith('"'):
                    value = value[1:-1]
                elif value.startswith("'") and value.endswith("'"):
                    value = value[1:-1]

                env_vars[key] = value
    except (OSError, UnicodeDecodeError) as e:
        raise DeploymentError(
            f"Failed to read environment file {env_file}: {e}",
            details="Check the file is readable and is UTF-8 text.",
        ) from e

    return env_vars


def _create_app(
    *,
    logger: Logger,
    domain: str,
    source: str,
    app_type: str = "auto",
    port: int | None = None,
    webserver: str = "nginx",
    branch: str | None = None,
    ssl: bool = True,
    www: bool = False,
    env_file: Path | None = None,
    package_manager: str = "auto",
    subdomains: tuple[str, ...] = (),
    workspaces: tuple[str, ...] = (),
    skip_database: bool = False,
    compose_file: str | None = None,
    compose_profiles: tuple[str, ...] = (),
) -> int:
    """
    Deploy an application.

    Args:
        logger: Logger of the current command.
        domain: Domain the application is served on.
        source: Git URL or local directory.
        app_type: Application type, or ``auto`` to detect it.
        port: Port to listen on. A free one is chosen when None.
        webserver: ``nginx`` or ``apache``.
        branch: Git branch to deploy.
        ssl: Request a certificate.
        www: Also serve and certify the ``www.`` subdomain.
        env_file: File of KEY=value pairs for the application environment.
        package_manager: Node package manager, or ``auto``.
        subdomains: ``app:subdomain`` mappings, for a monorepo.
        workspaces: Monorepo workspaces to deploy; empty means all.
        skip_database: Skip database provisioning, for a monorepo.
        compose_file: Compose file to use, for a Docker Compose project.
        compose_profiles: Compose profiles to activate.

    Returns:
        Exit code.

    Raises:
        WASMError: When validation or any deployment step fails.
    """
    domain = validate_domain(domain)

    if port:
        port = validate_port(port)
    else:
        port = find_available_port(preferred=3000)
        if not port:
            raise DeploymentError(
                "No available port found",
                details="Free a port in the range WASM allocates from, or pass --port.",
            )

    # The real type is settled after the source is fetched; nodejs is only the
    # value the readiness check is run against.
    if app_type == "auto":
        app_type = "nodejs"

    can_deploy, missing, warnings = check_deployment_ready(
        app_type=app_type,
        package_manager=package_manager,
        verbose=logger.verbose,
    )

    for warning in warnings:
        logger.warning(warning)

    if not can_deploy:
        logger.error("System is not ready for deployment")
        logger.blank()
        logger.info("Missing requirements:")
        for item in missing:
            logger.error(f"  - {item}")
        logger.blank()
        logger.info("To fix these issues, run:")
        logger.info("  sudo wasm setup init")
        logger.blank()
        logger.info("Or for detailed diagnostics:")
        logger.info("  wasm setup doctor")
        return 1

    env_vars = _read_env_file(env_file, logger) if env_file else {}

    logger.header("WASM Deployment")
    logger.key_value("Domain", domain)
    logger.key_value("Source", source)
    logger.key_value("Type", app_type)
    logger.key_value("Port", str(port))
    logger.key_value("Package Manager", package_manager)
    logger.key_value("SSL", "Yes" if ssl else "No")
    if ssl and www and should_include_www(domain):
        logger.key_value("WWW", f"www.{domain} included")
    logger.blank()

    if app_type == "monorepo":
        return _create_monorepo(
            logger=logger,
            domain=domain,
            source=source,
            webserver=webserver,
            ssl=ssl,
            branch=branch,
            env_vars=env_vars,
            subdomains=subdomains,
            workspaces=workspaces,
            skip_database=skip_database,
        )

    if app_type == "docker-compose":
        return _create_docker_compose(
            logger=logger,
            domain=domain,
            source=source,
            webserver=webserver,
            ssl=ssl,
            branch=branch,
            env_vars=env_vars,
            compose_file=compose_file,
            compose_profiles=compose_profiles,
            port=port,
        )

    deployer = get_deployer(app_type, verbose=logger.verbose)
    deployer.configure(
        domain=domain,
        source=source,
        port=port,
        webserver=webserver,
        ssl=ssl,
        branch=branch,
        env_vars=env_vars,
        package_manager=package_manager,
        include_www=www,
    )
    deployer.deploy()

    return 0


def _create_monorepo(
    *,
    logger: Logger,
    domain: str,
    source: str,
    webserver: str,
    ssl: bool,
    branch: str | None,
    env_vars: dict[str, str],
    subdomains: tuple[str, ...],
    workspaces: tuple[str, ...],
    skip_database: bool,
) -> int:
    """
    Deploy every deployable workspace of a monorepo.

    Args:
        logger: Logger of the current command.
        domain: Validated domain.
        source: Git URL or local directory.
        webserver: ``nginx`` or ``apache``.
        ssl: Request certificates.
        branch: Git branch to deploy.
        env_vars: Environment variables for the deployment.
        subdomains: ``app:subdomain`` mappings.
        workspaces: Workspaces to deploy; empty means all.
        skip_database: Skip database provisioning.

    Returns:
        Exit code.

    Raises:
        WASMError: When a deployment step fails.
    """
    subdomain_overrides: dict[str, str] = {}
    for mapping in subdomains:
        if ":" in mapping:
            app_name, subdomain = mapping.split(":", 1)
            subdomain_overrides[app_name] = subdomain
        else:
            logger.warning(f"Invalid subdomain mapping: {mapping} (expected app:subdomain)")

    deployer = MonorepoDeployer(verbose=logger.verbose)
    deployer.configure(
        domain=domain,
        source=source,
        webserver=webserver,
        ssl=ssl,
        branch=branch,
        env_vars=env_vars,
        subdomain_overrides=subdomain_overrides,
        workspace_filter=list(workspaces) or None,
        skip_database=skip_database,
    )
    deployer.deploy()

    return 0


def _create_docker_compose(
    *,
    logger: Logger,
    domain: str,
    source: str,
    webserver: str,
    ssl: bool,
    branch: str | None,
    env_vars: dict[str, str],
    compose_file: str | None,
    compose_profiles: tuple[str, ...],
    port: int | None,
) -> int:
    """
    Deploy a Docker Compose project.

    Args:
        logger: Logger of the current command.
        domain: Validated domain.
        source: Git URL or local directory.
        webserver: ``nginx`` or ``apache``.
        ssl: Request a certificate.
        branch: Git branch to deploy.
        env_vars: Environment variables for the deployment.
        compose_file: Compose file to use, relative to the project.
        compose_profiles: Compose profiles to activate.
        port: Port the proxy forwards to.

    Returns:
        Exit code.

    Raises:
        WASMError: When a deployment step fails.
    """
    deployer = DockerComposeDeployer(verbose=logger.verbose)
    deployer.configure(
        domain=domain,
        source=source,
        webserver=webserver,
        ssl=ssl,
        branch=branch,
        env_vars=env_vars,
        compose_file=compose_file,
        compose_profiles=list(compose_profiles) or None,
        port=port,
    )
    deployer.deploy()

    return 0


def _list_apps(logger: Logger) -> int:
    """
    Show every deployed application.

    Args:
        logger: Logger of the current command.

    Returns:
        Exit code.
    """
    store = get_store()

    logger.header("Deployed Applications")

    apps = store.list_apps()

    if not apps:
        logger.info("No applications deployed")
        logger.blank()
        logger.info("Deploy an application with:")
        logger.info("  wasm deploy -d example.com -s https://github.com/user/repo")
        return 0

    # Asked of systemd and of the port, not read from the status column. That
    # column is written at deploy time and never again, so it called everything
    # Running while health, which did ask, reported half of them stopped.
    states = resolve_states(apps, ServiceManager(verbose=logger.verbose))

    headers = ["Domain", "Type", "Status", "Port", "SSL"]
    rows = []

    for app in apps:
        current = states[app.domain]
        rows.append(
            [
                styled(app.domain, "bold"),
                app.app_type,
                state(current.label),
                styled(app.port, "") if app.port else state("static"),
                state("yes") if app.ssl_enabled else state("no"),
            ]
        )

    logger.table(headers, rows, justify=["left", "left", "left", "right", "left"])

    running = sum(1 for s in states.values() if s.label == RUNNING)
    static = sum(1 for s in states.values() if s.label == STATIC)
    unhealthy = [(domain, s) for domain, s in states.items() if not s.healthy]

    logger.blank()
    logger.info(f"Total: {len(apps)} apps ({running} running, {static} static)")

    # The reason belongs next to the list. Making the operator run a second
    # command to find out why something says Stopped is how the contradiction
    # between these two commands went unnoticed for as long as it did.
    if unhealthy:
        logger.blank()
        logger.warning(f"{len(unhealthy)} need attention:")
        for domain, current in unhealthy:
            logger.list_item(f"{domain} - {current.detail or current.label.lower()}")

    return 0


def _show_status(domain: str, logger: Logger) -> int:
    """
    Show what is known about one application.

    Args:
        domain: Application domain.
        logger: Logger of the current command.

    Returns:
        Exit code.

    Raises:
        WASMError: When the domain is not a valid domain.
    """
    service_manager = ServiceManager(verbose=logger.verbose)
    store = get_store()

    domain = validate_domain(domain)
    app_name = domain_to_app_name(domain)

    app_data = store.get_app_with_relations(domain)

    if not app_data or not app_data["app"]:
        # An app deployed before the store existed is still a real app; report
        # what systemd knows rather than claiming it is missing.
        status = service_manager.get_status(app_name)
        if not status["exists"]:
            logger.warning(f"Application not found: {domain}")
            return 1

        logger.header(f"Status: {domain}")
        logger.warning("Legacy app (not in store)")
        logger.key_value("Service", status["name"])
        logger.key_value("Active", "Yes" if status["active"] else "No")
        logger.key_value("Enabled", "Yes" if status["enabled"] else "No")
        return 0

    app = app_data["app"]
    site = app_data["site"]
    service = app_data["service"]
    databases = app_data["databases"]

    logger.header(f"Status: {domain}")

    logger.key_value("Type", app.app_type)
    logger.key_value("Status", app.status)
    logger.key_value("Path", app.app_path)
    logger.key_value("Static", "Yes" if app.is_static else "No")

    if app.port:
        logger.key_value("Port", str(app.port))

    if app.source:
        logger.key_value("Source", app.source)
        if app.branch:
            logger.key_value("Branch", app.branch)

    if app.deployed_at:
        logger.key_value("Deployed", app.deployed_at)

    if site:
        logger.blank()
        logger.info("Site Configuration:")
        logger.key_value("  Web Server", site.webserver)
        logger.key_value("  SSL", "Yes" if site.ssl_enabled else "No")
        logger.key_value("  Config", site.config_path)

    if service:
        logger.blank()
        logger.info("Service:")
        logger.key_value("  Name", service.name)

        systemd_status = service_manager.get_status(app_name)
        logger.key_value("  Active", "Yes" if systemd_status.get("active") else "No")
        logger.key_value("  Enabled", "Yes" if systemd_status.get("enabled") else "No")

        if systemd_status.get("pid"):
            logger.key_value("  PID", systemd_status["pid"])
        if systemd_status.get("uptime"):
            logger.key_value("  Started", systemd_status["uptime"])

    if databases:
        logger.blank()
        logger.info(f"Databases ({len(databases)}):")
        for db in databases:
            logger.key_value(f"  {db.engine}", db.name)

    return 0


def _control_service(domain: str, action: str, logger: Logger) -> int:
    """
    Start, stop or restart the service behind an application.

    Args:
        domain: Application domain.
        action: ``start``, ``stop`` or ``restart``.
        logger: Logger of the current command.

    Returns:
        Exit code.

    Raises:
        WASMError: When the domain is invalid or systemd refuses the operation.
    """
    present, past = _SERVICE_VERBS[action]

    store = get_store()
    domain = validate_domain(domain)
    app_name = domain_to_app_name(domain)

    app = store.get_app(domain)
    if app and app.is_static:
        logger.info(f"Static application - no service to {action}: {domain}")
        return 0

    service_manager = ServiceManager(verbose=logger.verbose)

    if not service_manager.service_exists(app_name):
        logger.warning(f"Service not found for: {domain}")
        logger.info("This may be a static application or the service was not created")
        return 1

    operations = {
        "start": service_manager.start,
        "stop": service_manager.stop,
        "restart": service_manager.restart,
    }

    logger.info(f"{present} {domain}...")
    operations[action](app_name)
    logger.success(f"Application {past}: {domain}")

    return 0


def _update_app(
    domain: str,
    *,
    logger: Logger,
    source: str | None = None,
    branch: str | None = None,
    package_manager: str = "auto",
) -> int:
    """
    Rebuild a deployed application from its source, then restart it.

    The service is only restarted once the new build succeeded, so a broken
    build leaves the previous one serving traffic.

    Args:
        domain: Application domain.
        logger: Logger of the current command.
        source: Fetch from this source instead of the recorded one.
        branch: Git branch to update from.
        package_manager: Node package manager, or ``auto``.

    Returns:
        Exit code.

    Raises:
        WASMError: When the application is unknown or a step fails.
    """
    config = Config()
    store = get_store()

    domain = validate_domain(domain)

    # The store holds the real path, which is what keeps legacy apps deployed
    # under a wasm- prefix updatable.
    app = store.get_app(domain)

    if app and app.app_path:
        app_path = Path(app.app_path)
        app_name = app_path.name
    else:
        app_name = domain_to_app_name(domain)
        app_path = config.apps_directory / app_name

    if not app_path.exists():
        raise WASMError(
            f"Application not found: {domain}",
            details=f"Nothing is deployed at {app_path}. Deploy it with: wasm create -d {domain}",
        )

    logger.header(f"Updating: {domain}")
    logger.blank()

    total_steps = 7

    logger.step(1, total_steps, "Creating pre-update backup")
    try:
        rollback_manager = RollbackManager(verbose=logger.verbose)
        backup = rollback_manager.create_pre_deploy_backup(
            domain=domain, description="Pre-update automatic backup"
        )
        if backup:
            logger.substep(f"Backup created: {backup.id}")
        else:
            logger.substep("No existing app to backup")
    except (WASMError, OSError) as e:
        logger.substep(f"Backup skipped: {e}")

    source_manager = SourceManager(verbose=logger.verbose)

    if source:
        logger.step(2, total_steps, "Fetching from new source")
        logger.substep(f"Source: {source}")
        # A forced fetch wipes the tree, and the environment file is the one
        # thing in it that is not in version control.
        env_backup = None
        env_file = app_path / ".env"
        if env_file.exists():
            env_backup = env_file.read_text()

        source_manager.fetch(source, app_path, branch=branch, force=True)

        if env_backup:
            env_file.write_text(env_backup)
            logger.substep("Restored .env file")
    else:
        logger.step(2, total_steps, "Pulling latest changes")
        source_manager.pull(app_path, branch=branch)

    logger.step(3, total_steps, "Detecting application type")

    # The initial deploy already settled the type; re-detecting can only change
    # its mind for the worse on a tree that now has build output in it.
    stored_type = app.app_type if app else None
    if stored_type and stored_type != "unknown":
        app_type = stored_type
        logger.substep(f"Detected: {app_type}")
    else:
        detected = detect_app_type(app_path, verbose=logger.verbose)
        if not detected:
            app_type = "nodejs"
            logger.substep(f"Using default: {app_type}")
        else:
            app_type = detected
            logger.substep(f"Detected: {app_type}")

    if app_type == "monorepo":
        return _update_monorepo(app_path, app_name, domain, logger, total_steps)

    if app_type == "docker-compose":
        return _update_docker_compose(app_path, app_name, domain, logger)

    deployer = get_deployer(app_type, verbose=logger.verbose)
    deployer.configure(
        domain=domain,
        source=str(app_path),
        app_path=app_path,
        package_manager=package_manager,
    )

    # One call, not a second copy of the deploy pipeline. The deployer reports
    # each step as it starts so the numbering here stays the CLI's business.
    step = iter(range(4, total_steps + 1))
    result = deployer.update(on_step=lambda message: logger.step(next(step), total_steps, message))

    logger.substep(f"Package manager: {result.package_manager}")
    if result.prisma_updated:
        logger.substep("Prisma updated")

    logger.step(7, total_steps, "Restarting application")
    service_manager = ServiceManager(verbose=logger.verbose)

    if result.is_static:
        logger.substep("Static application - no service restart needed")
        logger.success(f"Application updated successfully: {domain}")
        logger.blank()
        logger.key_value("Type", "Static")
        logger.key_value("Package Manager", result.package_manager)
        return 0

    status = service_manager.get_status(app_name)
    if not status.get("exists"):
        logger.warning("Service not found - application may need to be redeployed")
        logger.info(f"Try: wasm create -d {domain}")
        return 0

    logger.substep("Minimal downtime during restart...")
    service_manager.restart(app_name)

    # systemd reports the unit active the instant it forks, so give the process
    # a moment to fail before believing the health check.
    time.sleep(2)

    status = service_manager.get_status(app_name)
    if status.get("active"):
        logger.success(f"Application updated successfully: {domain}")
        logger.blank()
        logger.key_value("Status", "Running")
        logger.key_value("Package Manager", result.package_manager)
        if result.prisma_updated:
            logger.key_value("Prisma", "Updated")
    else:
        logger.warning("Application restarted but may not be running correctly")
        logger.info(f"Check logs with: wasm logs {domain}")

    return 0


def _update_monorepo(
    app_path: Path,
    app_name: str,
    domain: str,
    logger: Logger,
    total_steps: int,
) -> int:
    """
    Rebuild every workspace of a monorepo and restart their services.

    Args:
        app_path: Directory holding the application.
        app_name: Directory name of the application.
        domain: Validated domain.
        logger: Logger of the current command.
        total_steps: Number of steps reported to the user.

    Returns:
        Exit code.

    Raises:
        WASMError: When a build step fails.
    """
    deployer = MonorepoDeployer(verbose=logger.verbose)
    deployer.app_path = app_path
    deployer.app_name = app_name
    deployer.domain = domain
    deployer.package_manager = "pnpm"

    logger.step(4, total_steps, "Installing dependencies")
    deployer._install_dependencies()

    logger.step(5, total_steps, "Running database migrations")
    deployer._run_prisma_migrations()

    logger.step(6, total_steps, "Building applications")
    deployer._set_permissions()
    deployer._build_all()

    logger.step(7, total_steps, "Restarting applications")
    service_manager = ServiceManager(verbose=logger.verbose)

    store = get_store()
    app = store.get_app(domain)

    restarted = []
    if app:
        all_services = store.list_services()
        services = [s for s in all_services if s.app_id == app.id]
        for service in services:
            logger.substep(f"Restarting {service.name}")
            try:
                service_manager.restart(service.name)
                restarted.append(service.name)
            except ServiceError as e:
                logger.warning(f"Failed to restart {service.name}: {e}")
    else:
        status = service_manager.get_status(app_name)
        if status.get("exists"):
            service_manager.restart(app_name)
            restarted.append(app_name)

    if restarted:
        time.sleep(3)
        logger.success(f"Monorepo updated successfully: {domain}")
        logger.blank()
        for name in restarted:
            logger.key_value("Restarted", name)
    else:
        logger.warning("No services found to restart")
        logger.info(f"Try redeploying: wasm create -d {domain}")

    return 0


def _update_docker_compose(
    app_path: Path,
    app_name: str,
    domain: str,
    logger: Logger,
) -> int:
    """
    Rebuild the images of a Docker Compose project and recreate its containers.

    Args:
        app_path: Directory holding the application.
        app_name: Directory name of the application.
        domain: Validated domain.
        logger: Logger of the current command.

    Returns:
        Exit code.

    Raises:
        WASMError: When the compose file cannot be found or a build fails.
    """
    deployer = DockerComposeDeployer(verbose=logger.verbose)
    deployer.app_path = app_path
    deployer.app_name = app_name
    deployer.domain = domain

    deployer._discover_compose_file()

    logger.step(4, 5, "Rebuilding Docker images")
    deployer._build_images()

    logger.step(5, 5, "Restarting containers")
    cmd = ["docker", "compose"]
    if deployer.compose_path:
        cmd.extend(["-f", str(deployer.compose_path)])
    cmd.extend(["up", "-d", "--remove-orphans"])
    result = get_runner().run(cmd, cwd=app_path, timeout=_COMPOSE_TIMEOUT)

    if result.success:
        logger.success(f"Docker Compose app updated: {domain}")
    else:
        logger.warning("Update may have issues. Check with: docker compose ps")
        logger.warning(result.stderr)

    return 0


def _delete_app(
    domain: str,
    *,
    logger: Logger,
    force: bool = False,
    keep_files: bool = False,
    dry_run: bool = False,
) -> int:
    """
    Remove an application, its service, its site and its certificate.

    Args:
        domain: Application domain.
        logger: Logger of the current command.
        force: Do not ask for confirmation.
        keep_files: Leave the application directory on disk.
        dry_run: Only report what would be removed.

    Returns:
        Exit code.

    Raises:
        WASMError: When the domain is invalid.
    """
    config = Config()
    store = get_store()

    domain = validate_domain(domain)
    app_name = domain_to_app_name(domain)
    app_path = config.apps_directory / app_name

    app = store.get_app(domain)
    app_exists_on_disk = app_path.exists()

    if not app and not app_exists_on_disk:
        logger.warning(f"Application not found: {domain}")
        return 1

    # The store rows are deleted directly rather than through the runner, so a
    # dry run has to stop here instead of relying on the execution seam.
    if dry_run:
        return _preview_delete(
            domain=domain,
            app_name=app_name,
            app_path=app_path,
            logger=logger,
            keep_files=keep_files,
            registered=app is not None,
            app_exists_on_disk=app_exists_on_disk,
        )

    if not force:
        consequences = [f"the {app_name} service", "its site configuration", "its certificate"]
        if not keep_files:
            consequences.append(str(app_path))
        if app:
            consequences.append("its database records")
        logger.warning(f"This removes {', '.join(consequences)}.")
        if not click.confirm(f"Delete the application {domain}?", default=False):
            logger.info("Aborted")
            return 0

    logger.header(f"Deleting: {domain}")

    total_steps = 6

    if app and app.app_type == "docker-compose":
        logger.step(1, total_steps, "Stopping Docker Compose containers")
        for compose_name in ["docker-compose.prod.yml", "docker-compose.yml", "compose.yml"]:
            compose_file = app_path / compose_name
            if compose_file.exists():
                get_runner().run(
                    ["docker", "compose", "-f", str(compose_file), "down", "--remove-orphans"],
                    cwd=app_path,
                    timeout=_COMPOSE_TIMEOUT,
                )
                break
    else:
        logger.step(1, total_steps, "Stopping service")

    service_manager = ServiceManager(verbose=logger.verbose)
    try:
        service_manager.delete_service(app_name)
    except ServiceError as e:
        logger.warning(f"Failed to delete service: {e}")

    logger.step(2, total_steps, "Removing site configuration")
    try:
        nginx = NginxManager(verbose=logger.verbose)
        if nginx.site_exists(domain):
            nginx.delete_site(domain)
            nginx.reload()
    except WASMError as e:
        logger.warning(f"Failed to remove nginx site configuration: {e}")

    try:
        apache = ApacheManager(verbose=logger.verbose)
        if apache.site_exists(domain):
            apache.delete_site(domain)
            apache.reload()
    except WASMError as e:
        logger.warning(f"Failed to remove apache site configuration: {e}")

    logger.step(3, total_steps, "Removing SSL certificate")
    cert_manager = CertManager(verbose=logger.verbose)
    if cert_manager.is_installed() and cert_manager.cert_exists(domain):
        try:
            cert_manager.delete(domain)
            logger.substep(f"Certificate deleted: {domain}")
        except WASMError as e:
            logger.warning(f"Failed to delete certificate: {e}")
    else:
        logger.substep("No certificate found")

    if not keep_files:
        logger.step(4, total_steps, "Removing application files")
        remove_directory(app_path, sudo=True)
    else:
        logger.step(4, total_steps, "Keeping application files")

    logger.step(5, total_steps, "Removing from database")
    if app:
        store.delete_site(domain)
        store.delete_service(app_name)
        store.delete_app(domain)

    logger.step(6, total_steps, "Cleanup complete")
    logger.success(f"Application deleted: {domain}")

    return 0


def _preview_delete(
    *,
    domain: str,
    app_name: str,
    app_path: Path,
    logger: Logger,
    keep_files: bool,
    registered: bool,
    app_exists_on_disk: bool,
) -> int:
    """
    Report what deleting an application would remove.

    Args:
        domain: Validated domain.
        app_name: Directory and service name of the application.
        app_path: Directory holding the application.
        logger: Logger of the current command.
        keep_files: Whether the files would be kept.
        registered: Whether the application has store records.
        app_exists_on_disk: Whether the application directory exists.

    Returns:
        Exit code.
    """
    logger.header(f"Dry-run: Would delete {domain}")
    logger.blank()
    logger.info("The following actions would be performed:")
    logger.blank()

    service_manager = ServiceManager(verbose=logger.verbose)
    try:
        status = service_manager.get_status(app_name)
        if status.get("exists"):
            logger.key_value("Stop and remove service", app_name)
    except ServiceError as e:
        logger.debug(f"Could not query service {app_name}: {e}")

    nginx = NginxManager(verbose=logger.verbose)
    if nginx.site_exists(domain):
        logger.key_value("Remove nginx config", f"/etc/nginx/sites-available/{domain}")

    apache = ApacheManager(verbose=logger.verbose)
    if apache.site_exists(domain):
        logger.key_value("Remove apache config", f"/etc/apache2/sites-available/{domain}.conf")

    if app_exists_on_disk:
        if keep_files:
            logger.key_value("Keep app files", str(app_path))
        else:
            logger.key_value("Remove app files", str(app_path))

    if registered:
        logger.key_value("Remove from database", f"App, Site, and Service records for {domain}")

    logger.blank()
    logger.info("Run without --dry-run to execute these actions.")
    return 0


def _show_logs(domain: str, *, logger: Logger, follow: bool = False, lines: int = 50) -> int:
    """
    Print the recent log of an application.

    Args:
        domain: Application domain.
        logger: Logger of the current command.
        follow: Keep streaming until interrupted.
        lines: How many recent lines to show.

    Returns:
        Exit code.

    Raises:
        WASMError: When the domain is invalid.
    """
    service_manager = ServiceManager(verbose=logger.verbose)

    domain = validate_domain(domain)
    app_name = domain_to_app_name(domain)

    store = get_store()
    app = store.get_app(domain)

    if app and app.app_type == "docker-compose":
        config = Config()
        app_path = Path(app.app_path) if app.app_path else config.apps_directory / app_name

        compose_file = None
        for name in ["docker-compose.prod.yml", "docker-compose.yml", "compose.yml"]:
            candidate = app_path / name
            if candidate.exists():
                compose_file = str(candidate)
                break

        cmd = ["docker", "compose"]
        if compose_file:
            cmd.extend(["-f", compose_file])
        cmd.extend(["logs", "--tail", str(lines)])

        if follow:
            cmd.append("-f")
            _follow(cmd, cwd=app_path)
        else:
            result = get_runner().run(cmd, cwd=app_path, timeout=_COMPOSE_TIMEOUT)
            print(result.stdout if result.success else result.stderr)
        return 0

    # Resolves both the legacy wasm-* unit names and the current ones.
    service_name = service_manager._resolve_service_name(app_name)

    if follow:
        followed = _follow(
            [
                "journalctl",
                "-u",
                f"{service_name}.service",
                "-f",
                "-n",
                str(lines),
            ]
        )
        if followed is not None and not followed.success and not followed.timed_out:
            logger.error(f"Failed to follow the journal: {followed.stderr}")
            return 1
    else:
        logs = service_manager.logs(app_name, lines=lines)
        print(logs)

    return 0


# ---------------------------------------------------------------------------
# argparse entry point, still used by wasm.cli.parser
# ---------------------------------------------------------------------------


def handle_webapp(args: Namespace) -> int:
    """
    Handle webapp commands.

    Args:
        args: Parsed arguments.

    Returns:
        Exit code.
    """
    action = args.action

    handlers = {
        "create": _handle_create,
        "new": _handle_create,
        "deploy": _handle_create,
        "list": _handle_list,
        "ls": _handle_list,
        "status": _handle_status,
        "info": _handle_status,
        "restart": _handle_restart,
        "stop": _handle_stop,
        "start": _handle_start,
        "update": _handle_update,
        "upgrade": _handle_update,
        "delete": _handle_delete,
        "remove": _handle_delete,
        "rm": _handle_delete,
        "logs": _handle_logs,
    }

    handler = handlers.get(action)
    if not handler:
        print(f"Unknown action: {action}", file=sys.stderr)
        return 1

    try:
        return handler(args)
    except WASMError as e:
        logger = Logger(verbose=args.verbose)
        logger.error(e.message)
        if e.details:
            # Print details preserving formatting (for SSH guidance, command output, etc.)
            logger.blank()
            # Limit output to avoid flooding the terminal
            detail_lines = e.details.split("\n")
            max_lines = 50 if args.verbose else 20
            for line in detail_lines[:max_lines]:
                print(f"  {line}")
            if len(detail_lines) > max_lines:
                print(
                    f"  ... ({len(detail_lines) - max_lines} more lines, use --verbose for full output)"
                )
            logger.blank()
        return 1
    except Exception as e:
        logger = Logger(verbose=args.verbose)
        logger.error(f"Unexpected error: {e}")
        logger.debug(f"Unhandled {type(e).__name__} in webapp {action}")
        if args.verbose:
            import traceback

            traceback.print_exc()
        return 1


def _handle_create(args: Namespace) -> int:
    """
    Handle webapp create command.

    Args:
        args: Parsed arguments.

    Returns:
        Exit code.
    """
    env_file = getattr(args, "env_file", None)
    return _create_app(
        logger=Logger(verbose=args.verbose),
        domain=args.domain,
        source=args.source,
        app_type=args.type,
        port=args.port,
        webserver=args.webserver,
        branch=args.branch,
        ssl=not args.no_ssl,
        www=getattr(args, "www", False),
        env_file=Path(env_file) if env_file else None,
        package_manager=getattr(args, "package_manager", "auto") or "auto",
        subdomains=tuple(getattr(args, "subdomains", None) or ()),
        workspaces=tuple(getattr(args, "workspaces", None) or ()),
        skip_database=getattr(args, "no_database", False),
        compose_file=getattr(args, "compose_file", None),
        compose_profiles=tuple(getattr(args, "compose_profiles", None) or ()),
    )


def _handle_list(args: Namespace) -> int:
    """
    Handle webapp list command.

    Args:
        args: Parsed arguments.

    Returns:
        Exit code.
    """
    return _list_apps(Logger(verbose=args.verbose))


def _handle_status(args: Namespace) -> int:
    """
    Handle webapp status command.

    Args:
        args: Parsed arguments.

    Returns:
        Exit code.
    """
    return _show_status(args.domain, Logger(verbose=args.verbose))


def _handle_restart(args: Namespace) -> int:
    """
    Handle webapp restart command.

    Args:
        args: Parsed arguments.

    Returns:
        Exit code.
    """
    return _control_service(args.domain, "restart", Logger(verbose=args.verbose))


def _handle_stop(args: Namespace) -> int:
    """
    Handle webapp stop command.

    Args:
        args: Parsed arguments.

    Returns:
        Exit code.
    """
    return _control_service(args.domain, "stop", Logger(verbose=args.verbose))


def _handle_start(args: Namespace) -> int:
    """
    Handle webapp start command.

    Args:
        args: Parsed arguments.

    Returns:
        Exit code.
    """
    return _control_service(args.domain, "start", Logger(verbose=args.verbose))


def _handle_update(args: Namespace) -> int:
    """
    Handle webapp update command.

    Args:
        args: Parsed arguments.

    Returns:
        Exit code.
    """
    return _update_app(
        args.domain,
        logger=Logger(verbose=args.verbose),
        source=getattr(args, "source", None),
        branch=getattr(args, "branch", None),
        package_manager=getattr(args, "package_manager", "auto") or "auto",
    )


def _handle_delete(args: Namespace) -> int:
    """
    Handle webapp delete command.

    Args:
        args: Parsed arguments.

    Returns:
        Exit code.
    """
    return _delete_app(
        args.domain,
        logger=Logger(verbose=args.verbose),
        force=args.force,
        keep_files=args.keep_files,
        dry_run=getattr(args, "dry_run", False),
    )


def _handle_logs(args: Namespace) -> int:
    """
    Handle webapp logs command.

    Args:
        args: Parsed arguments.

    Returns:
        Exit code.
    """
    return _show_logs(
        args.domain,
        logger=Logger(verbose=args.verbose),
        follow=args.follow,
        lines=args.lines,
    )


# ---------------------------------------------------------------------------
# Click command tree
# ---------------------------------------------------------------------------


def _fold_verbose(ctx: click.Context, param: click.Parameter, value: bool | None) -> None:
    """
    Record ``--verbose`` typed after the command name.

    Args:
        ctx: Click context.
        param: The option being processed.
        value: True when the flag was given, None when it was not.
    """
    if value:
        ctx.ensure_object(Context).verbose = True


def _fold_no_color(ctx: click.Context, param: click.Parameter, value: bool | None) -> None:
    """
    Record ``--no-color`` typed after the command name.

    Args:
        ctx: Click context.
        param: The option being processed.
        value: True when the flag was given, None when it was not.
    """
    if value:
        ctx.ensure_object(Context).no_color = True
        set_colors_disabled(True)


def _fold_dry_run(ctx: click.Context, param: click.Parameter, value: bool | None) -> None:
    """
    Record ``--dry-run`` typed after the command name.

    Both seams have to be swapped, not just the command runner: a deletion is
    a filesystem call and never reaches a subprocess, which is how a rehearsal
    came to announce that nothing would change and then delete the archive.
    :func:`~wasm.cli.app.enable_dry_run` is the one place that knows what
    "rehearsal" means, so this defers to it rather than repeating the wiring
    and drifting from it.

    Args:
        ctx: Click context.
        param: The option being processed.
        value: True when the flag was given, None when it was not.
    """
    if not value:
        return
    state = ctx.ensure_object(Context)
    state.dry_run = True
    enable_dry_run(state)


def _global_flags(command: _F) -> _F:
    """
    Accept the global flags after the command name as well as before it.

    They are the same flags the root group declares and they end up in the same
    place, :class:`wasm.cli.app.Context`. Nothing here binds a parameter of the
    command function, so a late ``--verbose`` can only turn verbosity on and can
    never overwrite what the user asked for before the command name, which is
    the defect this migration exists to remove.

    Args:
        command: The command function being decorated.

    Returns:
        The decorated function.
    """
    for option in (
        click.option(
            "-v",
            "--verbose",
            is_flag=True,
            default=None,
            is_eager=True,
            expose_value=False,
            hidden=True,
            callback=_fold_verbose,
            help="Show the detail of each step.",
        ),
        click.option(
            "--dry-run",
            is_flag=True,
            default=None,
            is_eager=True,
            expose_value=False,
            hidden=True,
            callback=_fold_dry_run,
            help="Rehearse without changing anything.",
        ),
        click.option(
            "--no-color",
            is_flag=True,
            default=None,
            is_eager=True,
            expose_value=False,
            hidden=True,
            callback=_fold_no_color,
            help="Never emit colour.",
        ),
    ):
        command = option(command)
    return command


def _exit(code: int) -> None:
    """
    End the command with a handler's exit code.

    Click ignores what a command callback returns, so a non-zero code has to go
    through the context or every failure would report success to the shell.

    Args:
        code: Exit code. Zero returns normally.
    """
    if code:
        click.get_current_context().exit(code)


@click.group()
def cli() -> None:
    """Commands that act on a deployed application."""


@cli.command()
@click.option("-d", "--domain", required=True, help="Domain the application is served on.")
@click.option("-s", "--source", required=True, help="Git URL or directory to deploy from.")
@click.option(
    "-t",
    "--type",
    "app_type",
    type=click.Choice(APP_TYPES),
    default="auto",
    show_default=True,
    help="Application type. Detected from the source when left on auto.",
)
@click.option(
    "-p",
    "--port",
    type=click.INT,
    help="Port the application listens on. A free one is chosen when omitted.",
)
@click.option(
    "-w",
    "--webserver",
    type=click.Choice(WEBSERVERS),
    default="nginx",
    show_default=True,
    help="Web server that fronts the application.",
)
@click.option("-b", "--branch", help="Git branch to deploy.")
@click.option("--no-ssl", is_flag=True, help="Serve over plain HTTP, without a certificate.")
@click.option("--www", is_flag=True, help="Also serve and certify the www subdomain.")
@click.option(
    "--env-file",
    type=click.Path(exists=True, dir_okay=False, readable=True, path_type=Path),
    help="File of KEY=value lines to install as the application environment.",
)
@click.option(
    "--package-manager",
    "--pm",
    "package_manager",
    type=click.Choice(PACKAGE_MANAGERS),
    default="auto",
    show_default=True,
    help="Node package manager. Detected from the lockfile when left on auto.",
)
@click.option(
    "--subdomains",
    metavar="APP:SUBDOMAIN",
    multiple=True,
    help="Serve a monorepo app on this subdomain. Repeat once per app.",
)
@click.option(
    "--workspaces",
    metavar="NAME",
    multiple=True,
    help="Deploy only this monorepo workspace. Repeat to select several.",
)
@click.option("--no-database", is_flag=True, help="Skip database provisioning for a monorepo.")
@click.option(
    "--compose-file",
    help="Compose file to use, relative to the project. Detected when omitted.",
)
@click.option(
    "--compose-profiles",
    metavar="PROFILE",
    multiple=True,
    help="Activate this Docker Compose profile. Repeat to activate several.",
)
@_global_flags
@pass_context
def create(
    ctx: Context,
    domain: str,
    source: str,
    app_type: str,
    port: int | None,
    webserver: str,
    branch: str | None,
    no_ssl: bool,
    www: bool,
    env_file: Path | None,
    package_manager: str,
    subdomains: tuple[str, ...],
    workspaces: tuple[str, ...],
    no_database: bool,
    compose_file: str | None,
    compose_profiles: tuple[str, ...],
) -> None:
    """
    Deploy a web application and put it online.

    Fetches the source, builds it, runs it under systemd, publishes it on the
    domain and obtains a certificate for it.
    """
    _exit(
        _create_app(
            logger=ctx.logger,
            domain=domain,
            source=source,
            app_type=app_type,
            port=port,
            webserver=webserver,
            branch=branch,
            ssl=not no_ssl,
            www=www,
            env_file=env_file,
            package_manager=package_manager,
            subdomains=subdomains,
            workspaces=workspaces,
            skip_database=no_database,
            compose_file=compose_file,
            compose_profiles=compose_profiles,
        ),
    )


@cli.command(name="list")
@_global_flags
@pass_context
def list_apps(ctx: Context) -> None:
    """List the applications deployed on this server."""
    _exit(_list_apps(ctx.logger))


@cli.command()
@click.argument("domain")
@_global_flags
@pass_context
def status(ctx: Context, domain: str) -> None:
    """Show how an application is configured and whether it is running."""
    _exit(_show_status(domain, ctx.logger))


@cli.command()
@click.argument("domain")
@_global_flags
@pass_context
def start(ctx: Context, domain: str) -> None:
    """Start an application that is stopped."""
    _exit(_control_service(domain, "start", ctx.logger))


@cli.command()
@click.argument("domain")
@_global_flags
@pass_context
def stop(ctx: Context, domain: str) -> None:
    """Stop an application and leave it stopped."""
    _exit(_control_service(domain, "stop", ctx.logger))


@cli.command()
@click.argument("domain")
@_global_flags
@pass_context
def restart(ctx: Context, domain: str) -> None:
    """Restart an application, picking up its current build and environment."""
    _exit(_control_service(domain, "restart", ctx.logger))


@cli.command()
@click.argument("domain")
@click.option("-s", "--source", help="Fetch from this source instead of the recorded one.")
@click.option("-b", "--branch", help="Git branch to update from.")
@click.option(
    "--package-manager",
    "--pm",
    "package_manager",
    type=click.Choice(PACKAGE_MANAGERS),
    default="auto",
    show_default=True,
    help="Node package manager. Detected from the lockfile when left on auto.",
)
@_global_flags
@pass_context
def update(
    ctx: Context,
    domain: str,
    source: str | None,
    branch: str | None,
    package_manager: str,
) -> None:
    """
    Pull the latest code, rebuild and restart an application.

    A backup is taken first, and the service is only restarted once the new
    build succeeded.
    """
    _exit(
        _update_app(
            domain,
            logger=ctx.logger,
            source=source,
            branch=branch,
            package_manager=package_manager,
        ),
    )


@cli.command()
@click.argument("domain")
@click.option("-f", "-y", "--force", is_flag=True, help="Delete without asking for confirmation.")
@click.option("--keep-files", is_flag=True, help="Leave the application directory on disk.")
@_global_flags
@pass_context
def delete(ctx: Context, domain: str, force: bool, keep_files: bool) -> None:
    """
    Delete an application and everything deployed with it.

    Removes the service, the site configuration, the certificate, the
    application directory and the database records.
    """
    _exit(
        _delete_app(
            domain,
            logger=ctx.logger,
            force=force,
            keep_files=keep_files,
            dry_run=ctx.dry_run,
        ),
    )


@cli.command()
@click.argument("domain")
@click.option("-f", "--follow", is_flag=True, help="Keep streaming until you press Ctrl+C.")
@click.option(
    "-n",
    "--lines",
    type=click.INT,
    default=50,
    show_default=True,
    help="How many recent lines to show.",
)
@_global_flags
@pass_context
def logs(ctx: Context, domain: str, follow: bool, lines: int) -> None:
    """Show what an application has been writing to its log."""
    _exit(_show_logs(domain, logger=ctx.logger, follow=follow, lines=lines))
