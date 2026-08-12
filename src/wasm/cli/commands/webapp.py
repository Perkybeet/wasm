"""
Web application command handlers for WASM.

Everything this module needs is imported here rather than inside the handlers.
An import that only exists inside one function is a NameError waiting for the
next caller, which is exactly how ``site delete`` lost its certificate cleanup.
"""

import re
import sys
import time
from argparse import Namespace
from pathlib import Path

from wasm.core.config import Config
from wasm.core.dependencies import check_deployment_ready
from wasm.core.exceptions import DeploymentError, ServiceError, WASMError
from wasm.core.logger import Logger
from wasm.core.runner import CommandResult, get_runner
from wasm.core.store import AppStatus, get_store
from wasm.core.utils import domain_to_app_name, remove_directory
from wasm.deployers import detect_app_type, get_deployer
from wasm.deployers.docker_compose import DockerComposeDeployer
from wasm.deployers.monorepo import MonorepoDeployer
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
    logger = Logger(verbose=args.verbose)

    # Validate domain
    domain = validate_domain(args.domain)

    # Validate/find port
    port = args.port
    if port:
        port = validate_port(port)
    else:
        port = find_available_port(preferred=3000)
        if not port:
            raise DeploymentError("No available port found")

    # Determine app type
    app_type = args.type
    if app_type == "auto":
        # Will auto-detect after fetching source
        app_type = "nodejs"  # Default fallback

    # Get package manager preference
    package_manager = getattr(args, "package_manager", "auto") or "auto"

    # =========================================================================
    # Pre-deployment verification
    # =========================================================================
    can_deploy, missing, warnings = check_deployment_ready(
        app_type=app_type,
        package_manager=package_manager,
        verbose=args.verbose,
    )

    # Show warnings (non-blocking)
    for warning in warnings:
        logger.warning(warning)

    # Check critical requirements
    if not can_deploy:
        logger.error("System is not ready for deployment")
        logger.blank()
        logger.info("Missing requirements:")
        for item in missing:
            logger.error(f"  ✗ {item}")
        logger.blank()
        logger.info("To fix these issues, run:")
        logger.info("  sudo wasm setup init")
        logger.blank()
        logger.info("Or for detailed diagnostics:")
        logger.info("  wasm setup doctor")
        return 1

    # Load environment variables from file
    env_vars = {}
    if args.env_file:
        env_path = Path(args.env_file)
        if env_path.exists():
            try:
                # Check file size
                file_size = env_path.stat().st_size
                if file_size > MAX_ENV_FILE_SIZE:
                    logger.error(
                        f"Environment file too large: {file_size} bytes (max: {MAX_ENV_FILE_SIZE})"
                    )
                    return 1

                with open(env_path) as f:
                    for line_num, line in enumerate(f, 1):
                        # Check line length
                        if len(line) > MAX_ENV_LINE_LENGTH:
                            logger.warning(f"Line {line_num} exceeds max length, skipping")
                            continue

                        line = line.strip()
                        if not line or line.startswith("#"):
                            continue

                        if "=" not in line:
                            logger.warning(f"Line {line_num}: invalid format (no '='), skipping")
                            continue

                        key, value = line.split("=", 1)
                        key = key.strip()
                        value = value.strip()

                        # Validate key format
                        if not VALID_ENV_KEY_PATTERN.match(key):
                            logger.warning(f"Line {line_num}: invalid key '{key}', skipping")
                            continue

                        # Remove surrounding quotes if present
                        if value.startswith('"') and value.endswith('"'):
                            value = value[1:-1]
                        elif value.startswith("'") and value.endswith("'"):
                            value = value[1:-1]

                        env_vars[key] = value
            except (OSError, UnicodeDecodeError) as e:
                logger.error(f"Failed to read environment file {env_path}: {e}")
                return 1

    # Print deployment header
    logger.header("WASM Deployment")
    logger.key_value("Domain", domain)
    logger.key_value("Source", args.source)
    logger.key_value("Type", app_type)
    logger.key_value("Port", str(port))
    logger.key_value("Package Manager", package_manager)
    logger.key_value("SSL", "Yes" if not args.no_ssl else "No")
    include_www = getattr(args, "www", False)
    if not args.no_ssl and include_www:
        if should_include_www(domain):
            logger.key_value("WWW", f"www.{domain} included")
    logger.blank()

    # Handle monorepo deployments specially
    if app_type == "monorepo":
        return _handle_monorepo_create(args, domain, env_vars, logger)

    # Handle docker-compose deployments specially
    if app_type == "docker-compose":
        return _handle_docker_compose_create(args, domain, env_vars, logger)

    # Get deployer
    deployer = get_deployer(app_type, verbose=args.verbose)

    # Configure deployer
    deployer.configure(
        domain=domain,
        source=args.source,
        port=port,
        webserver=args.webserver,
        ssl=not args.no_ssl,
        branch=args.branch,
        env_vars=env_vars,
        package_manager=package_manager,
        include_www=getattr(args, "www", False),
    )

    # Run deployment
    deployer.deploy()

    return 0


def _handle_monorepo_create(args: Namespace, domain: str, env_vars: dict, logger: Logger) -> int:
    """
    Handle monorepo deployment specially.

    Args:
        args: Parsed arguments.
        domain: Validated domain.
        env_vars: Environment variables for the deployment.
        logger: Logger of the current command.

    Returns:
        Exit code.
    """
    # Parse subdomain overrides
    subdomain_overrides = {}
    if getattr(args, "subdomains", None):
        for mapping in args.subdomains:
            if ":" in mapping:
                app_name, subdomain = mapping.split(":", 1)
                subdomain_overrides[app_name] = subdomain
            else:
                logger.warning(f"Invalid subdomain mapping: {mapping} (expected app:subdomain)")

    # Get workspace filter
    workspace_filter = getattr(args, "workspaces", None)

    # Get skip_database flag
    skip_database = getattr(args, "no_database", False)

    # Create and configure deployer
    deployer = MonorepoDeployer(verbose=args.verbose)

    deployer.configure(
        domain=domain,
        source=args.source,
        webserver=args.webserver,
        ssl=not args.no_ssl,
        branch=args.branch,
        env_vars=env_vars,
        subdomain_overrides=subdomain_overrides,
        workspace_filter=workspace_filter,
        skip_database=skip_database,
    )

    # Run deployment
    deployer.deploy()

    return 0


def _handle_docker_compose_create(
    args: Namespace, domain: str, env_vars: dict, logger: Logger
) -> int:
    """
    Handle Docker Compose deployment.

    Args:
        args: Parsed arguments.
        domain: Validated domain.
        env_vars: Environment variables for the deployment.
        logger: Logger of the current command.

    Returns:
        Exit code.
    """
    deployer = DockerComposeDeployer(verbose=args.verbose)

    deployer.configure(
        domain=domain,
        source=args.source,
        webserver=args.webserver,
        ssl=not args.no_ssl,
        branch=args.branch,
        env_vars=env_vars,
        compose_file=getattr(args, "compose_file", None),
        compose_profiles=getattr(args, "compose_profiles", None),
        port=args.port,
    )

    deployer.deploy()

    return 0


def _handle_list(args: Namespace) -> int:
    """
    Handle webapp list command.

    Args:
        args: Parsed arguments.

    Returns:
        Exit code.
    """
    logger = Logger(verbose=args.verbose)

    store = get_store()

    logger.header("Deployed Applications")

    apps = store.list_apps()

    if not apps:
        logger.info("No applications deployed")
        logger.blank()
        logger.info("Deploy an application with:")
        logger.info("  wasm deploy -d example.com -s https://github.com/user/repo")
        return 0

    # Prepare table data
    headers = ["Domain", "Type", "Status", "Port", "SSL"]
    rows = []

    for app in apps:
        # Determine status emoji
        if app.status == AppStatus.RUNNING.value:
            status_str = "🟢 Running"
        elif app.status == AppStatus.STOPPED.value:
            status_str = "🔴 Stopped"
        elif app.status == AppStatus.DEPLOYING.value:
            status_str = "🟡 Deploying"
        elif app.status == AppStatus.FAILED.value:
            status_str = "❌ Failed"
        else:
            status_str = "⚪ Unknown"

        port_str = str(app.port) if app.port else "static"
        ssl_str = "✓" if app.ssl_enabled else "✗"

        rows.append([app.domain, app.app_type, status_str, port_str, ssl_str])

    logger.table(headers, rows)

    # Show summary
    logger.blank()
    running = sum(1 for a in apps if a.status == AppStatus.RUNNING.value)
    static = sum(1 for a in apps if a.is_static)
    logger.info(f"Total: {len(apps)} apps ({running} running, {static} static)")

    return 0


def _handle_status(args: Namespace) -> int:
    """
    Handle webapp status command.

    Args:
        args: Parsed arguments.

    Returns:
        Exit code.
    """
    logger = Logger(verbose=args.verbose)
    service_manager = ServiceManager(verbose=args.verbose)

    store = get_store()

    domain = validate_domain(args.domain)
    app_name = domain_to_app_name(domain)

    # First check the store
    app_data = store.get_app_with_relations(domain)

    if not app_data or not app_data["app"]:
        # Fallback to systemd check for legacy apps
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

    # App info
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

    # Site info
    if site:
        logger.blank()
        logger.info("Site Configuration:")
        logger.key_value("  Web Server", site.webserver)
        logger.key_value("  SSL", "Yes" if site.ssl_enabled else "No")
        logger.key_value("  Config", site.config_path)

    # Service info (for non-static apps)
    if service:
        logger.blank()
        logger.info("Service:")
        logger.key_value("  Name", service.name)

        # Get live status from systemd
        systemd_status = service_manager.get_status(app_name)
        logger.key_value("  Active", "Yes" if systemd_status.get("active") else "No")
        logger.key_value("  Enabled", "Yes" if systemd_status.get("enabled") else "No")

        if systemd_status.get("pid"):
            logger.key_value("  PID", systemd_status["pid"])
        if systemd_status.get("uptime"):
            logger.key_value("  Started", systemd_status["uptime"])

    # Database info
    if databases:
        logger.blank()
        logger.info(f"Databases ({len(databases)}):")
        for db in databases:
            logger.key_value(f"  {db.engine}", db.name)

    return 0


def _handle_restart(args: Namespace) -> int:
    """
    Handle webapp restart command.

    Args:
        args: Parsed arguments.

    Returns:
        Exit code.
    """
    logger = Logger(verbose=args.verbose)

    store = get_store()

    domain = validate_domain(args.domain)
    app_name = domain_to_app_name(domain)

    # Check if app is static (no service to restart)
    app = store.get_app(domain)
    if app and app.is_static:
        logger.info(f"Static application - no service to restart: {domain}")
        return 0

    service_manager = ServiceManager(verbose=args.verbose)

    # Verify service exists
    if not service_manager.service_exists(app_name):
        logger.warning(f"Service not found for: {domain}")
        logger.info("This may be a static application or the service was not created")
        return 1

    logger.info(f"Restarting {domain}...")
    service_manager.restart(app_name)
    logger.success(f"Application restarted: {domain}")

    return 0


def _handle_stop(args: Namespace) -> int:
    """
    Handle webapp stop command.

    Args:
        args: Parsed arguments.

    Returns:
        Exit code.
    """
    logger = Logger(verbose=args.verbose)

    store = get_store()

    domain = validate_domain(args.domain)
    app_name = domain_to_app_name(domain)

    # Check if app is static (no service to stop)
    app = store.get_app(domain)
    if app and app.is_static:
        logger.info(f"Static application - no service to stop: {domain}")
        return 0

    service_manager = ServiceManager(verbose=args.verbose)

    # Verify service exists
    if not service_manager.service_exists(app_name):
        logger.warning(f"Service not found for: {domain}")
        logger.info("This may be a static application or the service was not created")
        return 1

    logger.info(f"Stopping {domain}...")
    service_manager.stop(app_name)
    logger.success(f"Application stopped: {domain}")

    return 0


def _handle_start(args: Namespace) -> int:
    """
    Handle webapp start command.

    Args:
        args: Parsed arguments.

    Returns:
        Exit code.
    """
    logger = Logger(verbose=args.verbose)

    store = get_store()

    domain = validate_domain(args.domain)
    app_name = domain_to_app_name(domain)

    # Check if app is static (no service to start)
    app = store.get_app(domain)
    if app and app.is_static:
        logger.info(f"Static application - no service to start: {domain}")
        return 0

    service_manager = ServiceManager(verbose=args.verbose)

    # Verify service exists
    if not service_manager.service_exists(app_name):
        logger.warning(f"Service not found for: {domain}")
        logger.info("This may be a static application or the service was not created")
        return 1

    logger.info(f"Starting {domain}...")
    service_manager.start(app_name)
    logger.success(f"Application started: {domain}")

    return 0


def _handle_update(args: Namespace) -> int:
    """
    Handle webapp update command with zero-downtime strategy.

    Strategy:
    1. Create pre-update backup (for rollback)
    2. Pull/fetch new code
    3. Install dependencies (new packages)
    4. Generate Prisma if needed
    5. Build application
    6. Only then restart service (minimal downtime)
    """
    logger = Logger(verbose=args.verbose)
    config = Config()

    store = get_store()

    domain = validate_domain(args.domain)

    # Consultar BD primero para obtener el app_path real
    app = store.get_app(domain)

    if app and app.app_path:
        # Usar el path almacenado en la BD (soporta apps legacy con prefijo wasm-)
        app_path = Path(app.app_path)
        app_name = app_path.name
    else:
        # Fallback para apps no registradas en BD
        app_name = domain_to_app_name(domain)
        app_path = config.apps_directory / app_name

    if not app_path.exists():
        raise WASMError(f"Application not found: {domain}")

    # Get package manager preference
    package_manager = getattr(args, "package_manager", "auto") or "auto"

    logger.header(f"Updating: {domain}")
    logger.blank()

    total_steps = 7

    # Step 1: Create pre-update backup for potential rollback
    logger.step(1, total_steps, "Creating pre-update backup")
    try:
        rollback_manager = RollbackManager(verbose=args.verbose)
        backup = rollback_manager.create_pre_deploy_backup(
            domain=domain, description="Pre-update automatic backup"
        )
        if backup:
            logger.substep(f"Backup created: {backup.id}")
        else:
            logger.substep("No existing app to backup")
    except (WASMError, OSError) as e:
        logger.substep(f"Backup skipped: {e}")

    # Step 2: Pull latest changes or fetch from new source
    source_manager = SourceManager(verbose=args.verbose)

    new_source = getattr(args, "source", None)

    if new_source:
        logger.step(2, total_steps, "Fetching from new source")
        logger.substep(f"Source: {new_source}")
        # For new source, we need to handle it differently
        # Back up current .env if exists
        env_backup = None
        env_file = app_path / ".env"
        if env_file.exists():
            env_backup = env_file.read_text()

        # Fetch to a temp location first, then sync
        source_manager.fetch(new_source, app_path, branch=args.branch, force=True)

        # Restore .env if it was backed up
        if env_backup:
            env_file.write_text(env_backup)
            logger.substep("Restored .env file")
    else:
        logger.step(2, total_steps, "Pulling latest changes")
        source_manager.pull(app_path, branch=args.branch)

    # Step 3: Detect app type and configure deployer
    logger.step(3, total_steps, "Detecting application type")

    # Prefer stored app type from database (initial deploy already determined it correctly)
    stored_type = app.app_type if app else None
    if stored_type and stored_type != "unknown":
        app_type = stored_type
        logger.substep(f"Detected: {app_type}")
    else:
        app_type = detect_app_type(app_path, verbose=args.verbose)
        if not app_type:
            app_type = "nodejs"
            logger.substep(f"Using default: {app_type}")
        else:
            logger.substep(f"Detected: {app_type}")

    # Handle monorepo updates with dedicated flow
    if app_type == "monorepo":
        return _handle_monorepo_update(args, app_path, app_name, domain, logger, total_steps)

    # Handle docker-compose updates
    if app_type == "docker-compose":
        return _handle_docker_compose_update(args, app_path, app_name, domain, logger)

    deployer = get_deployer(app_type, verbose=args.verbose)
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

    # Step 7: Restart service (only if not static)
    logger.step(7, total_steps, "Restarting application")
    service_manager = ServiceManager(verbose=args.verbose)

    # Check if this is a static app (no service to restart)
    is_static = result.is_static

    if is_static:
        logger.substep("Static application - no service restart needed")
        logger.success(f"Application updated successfully: {domain}")
        logger.blank()
        logger.key_value("Type", "Static")
        logger.key_value("Package Manager", result.package_manager)
    else:
        # Check if service exists before trying to restart
        status = service_manager.get_status(app_name)
        if not status.get("exists"):
            logger.warning("Service not found - application may need to be redeployed")
            logger.info(f"Try: wasm create -d {domain}")
        else:
            logger.substep("Minimal downtime during restart...")
            service_manager.restart(app_name)

            # Quick health check
            import time

            time.sleep(2)  # Give the app a moment to start

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


def _handle_monorepo_update(
    args: Namespace,
    app_path: Path,
    app_name: str,
    domain: str,
    logger: Logger,
    total_steps: int,
) -> int:
    """
    Handle update for monorepo applications.

    Args:
        args: Parsed arguments.
        app_path: Directory holding the application.
        app_name: Directory name of the application.
        domain: Validated domain.
        logger: Logger of the current command.
        total_steps: Number of steps reported to the user.

    Returns:
        Exit code.
    """
    deployer = MonorepoDeployer(verbose=args.verbose)
    deployer.app_path = app_path
    deployer.app_name = app_name
    deployer.domain = domain
    deployer.package_manager = "pnpm"

    # Step 4: Install dependencies
    logger.step(4, total_steps, "Installing dependencies")
    deployer._install_dependencies()

    # Step 5: Run Prisma migrations
    logger.step(5, total_steps, "Running database migrations")
    deployer._run_prisma_migrations()

    # Step 6: Build all workspaces
    logger.step(6, total_steps, "Building applications")
    deployer._set_permissions()
    deployer._build_all()

    # Step 7: Restart all workspace services
    logger.step(7, total_steps, "Restarting applications")
    service_manager = ServiceManager(verbose=args.verbose)

    # Find all services for this monorepo
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
        # Fallback: restart by app_name pattern
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


def _handle_docker_compose_update(
    args: Namespace,
    app_path: Path,
    app_name: str,
    domain: str,
    logger: Logger,
) -> int:
    """
    Handle update for Docker Compose applications.

    Args:
        args: Parsed arguments.
        app_path: Directory holding the application.
        app_name: Directory name of the application.
        domain: Validated domain.
        logger: Logger of the current command.

    Returns:
        Exit code.
    """
    deployer = DockerComposeDeployer(verbose=args.verbose)
    deployer.app_path = app_path
    deployer.app_name = app_name
    deployer.domain = domain

    # Discover compose file
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


def _handle_delete(args: Namespace) -> int:
    """
    Handle webapp delete command.

    Args:
        args: Parsed arguments.

    Returns:
        Exit code.
    """
    logger = Logger(verbose=args.verbose)
    config = Config()

    store = get_store()

    domain = validate_domain(args.domain)
    app_name = domain_to_app_name(domain)
    app_path = config.apps_directory / app_name

    # Check if app exists in store or filesystem
    app = store.get_app(domain)
    app_exists_on_disk = app_path.exists()

    if not app and not app_exists_on_disk:
        logger.warning(f"Application not found: {domain}")
        return 1

    # Dry-run mode: show what would be deleted
    dry_run = getattr(args, "dry_run", False)
    if dry_run:
        logger.header(f"Dry-run: Would delete {domain}")
        logger.blank()
        logger.info("The following actions would be performed:")
        logger.blank()

        # Check service
        service_manager = ServiceManager(verbose=args.verbose)
        try:
            status = service_manager.get_status(app_name)
            if status.get("exists"):
                logger.key_value("Stop and remove service", app_name)
        except ServiceError as e:
            logger.debug(f"Could not query service {app_name}: {e}")

        # Check nginx
        nginx = NginxManager(verbose=args.verbose)
        if nginx.site_exists(domain):
            logger.key_value("Remove nginx config", f"/etc/nginx/sites-available/{domain}")

        # Check apache
        apache = ApacheManager(verbose=args.verbose)
        if apache.site_exists(domain):
            logger.key_value("Remove apache config", f"/etc/apache2/sites-available/{domain}.conf")

        # Check files
        if app_exists_on_disk:
            if not args.keep_files:
                logger.key_value("Remove app files", str(app_path))
            else:
                logger.key_value("Keep app files", str(app_path))

        # Check store records
        if app:
            logger.key_value("Remove from database", f"App, Site, and Service records for {domain}")

        logger.blank()
        logger.info("Run without --dry-run to execute these actions.")
        return 0

    # Confirmation
    if not args.force:
        response = input(f"Delete application '{domain}'? [y/N] ")
        if response.lower() != "y":
            logger.info("Aborted")
            return 0

    logger.header(f"Deleting: {domain}")

    total_steps = 6

    # Stop Docker Compose containers if applicable
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

    # Delete systemd service
    service_manager = ServiceManager(verbose=args.verbose)
    try:
        service_manager.delete_service(app_name)
    except ServiceError as e:
        logger.warning(f"Failed to delete service: {e}")

    # Delete site configuration
    logger.step(2, total_steps, "Removing site configuration")
    try:
        nginx = NginxManager(verbose=args.verbose)
        if nginx.site_exists(domain):
            nginx.delete_site(domain)
            nginx.reload()
    except WASMError as e:
        logger.warning(f"Failed to remove nginx site configuration: {e}")

    try:
        apache = ApacheManager(verbose=args.verbose)
        if apache.site_exists(domain):
            apache.delete_site(domain)
            apache.reload()
    except WASMError as e:
        logger.warning(f"Failed to remove apache site configuration: {e}")

    # Delete SSL certificate
    logger.step(3, total_steps, "Removing SSL certificate")
    cert_manager = CertManager(verbose=args.verbose)
    if cert_manager.is_installed() and cert_manager.cert_exists(domain):
        try:
            cert_manager.delete(domain)
            logger.substep(f"Certificate deleted: {domain}")
        except WASMError as e:
            logger.warning(f"Failed to delete certificate: {e}")
    else:
        logger.substep("No certificate found")

    # Delete files
    if not args.keep_files:
        logger.step(4, total_steps, "Removing application files")
        remove_directory(app_path, sudo=True)
    else:
        logger.step(4, total_steps, "Keeping application files")

    # Delete from store
    logger.step(5, total_steps, "Removing from database")
    if app:
        store.delete_site(domain)
        store.delete_service(app_name)
        store.delete_app(domain)

    logger.step(6, total_steps, "Cleanup complete")
    logger.success(f"Application deleted: {domain}")

    return 0


def _handle_logs(args: Namespace) -> int:
    """
    Handle webapp logs command.

    Args:
        args: Parsed arguments.

    Returns:
        Exit code.
    """
    logger = Logger(verbose=args.verbose)
    service_manager = ServiceManager(verbose=args.verbose)

    domain = validate_domain(args.domain)
    app_name = domain_to_app_name(domain)

    # Check if this is a docker-compose app
    store = get_store()
    app = store.get_app(domain)

    if app and app.app_type == "docker-compose":
        config = Config()
        app_path = Path(app.app_path) if app.app_path else config.apps_directory / app_name

        # Find compose file
        compose_file = None
        for name in ["docker-compose.prod.yml", "docker-compose.yml", "compose.yml"]:
            cf = app_path / name
            if cf.exists():
                compose_file = str(cf)
                break

        cmd = ["docker", "compose"]
        if compose_file:
            cmd.extend(["-f", compose_file])
        cmd.extend(["logs", "--tail", str(args.lines)])

        if args.follow:
            cmd.append("-f")
            _follow(cmd, cwd=app_path)
        else:
            result = get_runner().run(cmd, cwd=app_path, timeout=_COMPOSE_TIMEOUT)
            print(result.stdout if result.success else result.stderr)
        return 0

    # Get resolved service name (handles both legacy wasm-* and new format)
    service_name = service_manager._resolve_service_name(app_name)

    if args.follow:
        result = _follow(
            [
                "journalctl",
                "-u",
                f"{service_name}.service",
                "-f",
                "-n",
                str(args.lines),
            ]
        )
        if result is not None and not result.success and not result.timed_out:
            logger.error(f"Failed to follow the journal: {result.stderr}")
            return 1
    else:
        logs = service_manager.logs(app_name, lines=args.lines)
        print(logs)

    return 0
