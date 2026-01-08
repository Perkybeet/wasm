"""
Web application command handlers for WASM.
"""

import sys
from argparse import Namespace
from pathlib import Path
from typing import Dict, Optional

from wasm.core.config import Config
from wasm.core.logger import Logger
from wasm.core.exceptions import WASMError, DeploymentError
from wasm.core.utils import domain_to_app_name
from wasm.deployers import get_deployer, detect_app_type
from wasm.managers.service_manager import ServiceManager
from wasm.managers.nginx_manager import NginxManager
from wasm.managers.apache_manager import ApacheManager
from wasm.validators.domain import validate_domain
from wasm.validators.port import validate_port, find_available_port


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
                print(f"  ... ({len(detail_lines) - max_lines} more lines, use --verbose for full output)")
            logger.blank()
        return 1
    except Exception as e:
        logger = Logger(verbose=args.verbose)
        logger.error(f"Unexpected error: {e}")
        if args.verbose:
            import traceback
            traceback.print_exc()
        return 1


def _handle_create(args: Namespace) -> int:
    """Handle webapp create command."""
    logger = Logger(verbose=args.verbose)
    config = Config()
    
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
    from wasm.core.dependencies import check_deployment_ready
    
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
            with open(env_path) as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        key, value = line.split("=", 1)
                        env_vars[key.strip()] = value.strip()
    
    # Print deployment header
    logger.header("WASM Deployment")
    logger.key_value("Domain", domain)
    logger.key_value("Source", args.source)
    logger.key_value("Type", app_type)
    logger.key_value("Port", str(port))
    logger.key_value("Package Manager", package_manager)
    logger.key_value("SSL", "Yes" if not args.no_ssl else "No")
    logger.blank()
    
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
    )
    
    # Run deployment
    deployer.deploy()
    
    return 0


def _handle_list(args: Namespace) -> int:
    """Handle webapp list command."""
    logger = Logger(verbose=args.verbose)
    
    from wasm.core.store import get_store, AppStatus
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
    """Handle webapp status command."""
    logger = Logger(verbose=args.verbose)
    service_manager = ServiceManager(verbose=args.verbose)
    
    from wasm.core.store import get_store
    store = get_store()
    
    domain = validate_domain(args.domain)
    app_name = domain_to_app_name(domain)
    
    # First check the store
    app_data = store.get_app_with_relations(domain)
    
    if not app_data or not app_data['app']:
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
    
    app = app_data['app']
    site = app_data['site']
    service = app_data['service']
    databases = app_data['databases']
    
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
    """Handle webapp restart command."""
    logger = Logger(verbose=args.verbose)
    service_manager = ServiceManager(verbose=args.verbose)
    
    domain = validate_domain(args.domain)
    app_name = domain_to_app_name(domain)
    
    logger.info(f"Restarting {domain}...")
    service_manager.restart(app_name)
    logger.success(f"Application restarted: {domain}")
    
    return 0


def _handle_stop(args: Namespace) -> int:
    """Handle webapp stop command."""
    logger = Logger(verbose=args.verbose)
    service_manager = ServiceManager(verbose=args.verbose)
    
    domain = validate_domain(args.domain)
    app_name = domain_to_app_name(domain)
    
    logger.info(f"Stopping {domain}...")
    service_manager.stop(app_name)
    logger.success(f"Application stopped: {domain}")
    
    return 0


def _handle_start(args: Namespace) -> int:
    """Handle webapp start command."""
    logger = Logger(verbose=args.verbose)
    service_manager = ServiceManager(verbose=args.verbose)
    
    domain = validate_domain(args.domain)
    app_name = domain_to_app_name(domain)
    
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
    
    domain = validate_domain(args.domain)
    app_name = domain_to_app_name(domain)
    app_path = config.apps_directory / app_name
    
    if not app_path.exists():
        raise WASMError(f"Application not found: {domain}")
    
    # Get package manager preference
    package_manager = getattr(args, "package_manager", "auto") or "auto"
    
    logger.header(f"Updating: {domain}")
    logger.info("Zero-downtime update strategy")
    logger.blank()
    
    total_steps = 7
    
    # Step 1: Create pre-update backup for potential rollback
    logger.step(1, total_steps, "Creating pre-update backup")
    try:
        from wasm.managers.backup_manager import RollbackManager
        rollback_manager = RollbackManager(verbose=args.verbose)
        backup = rollback_manager.create_pre_deploy_backup(
            domain=domain,
            description="Pre-update automatic backup"
        )
        if backup:
            logger.substep(f"Backup created: {backup.id}")
        else:
            logger.substep("No existing app to backup")
    except Exception as e:
        logger.substep(f"Backup skipped: {e}")
    
    # Step 2: Pull latest changes or fetch from new source
    from wasm.managers.source_manager import SourceManager
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
    app_type = detect_app_type(app_path, verbose=args.verbose)
    if not app_type:
        app_type = "nodejs"
        logger.substep(f"Using default: {app_type}")
    else:
        logger.substep(f"Detected: {app_type}")
    
    deployer = get_deployer(app_type, verbose=args.verbose)
    deployer.app_path = app_path
    deployer.app_name = app_name
    deployer.domain = domain
    deployer._package_manager = package_manager
    
    # Run pre_install to detect package manager and prisma
    deployer.pre_install()
    logger.substep(f"Package manager: {deployer.package_manager}")
    if deployer.has_prisma:
        logger.substep("Prisma detected")
    
    # Step 4: Install dependencies (without stopping the app)
    logger.step(4, total_steps, "Installing dependencies")
    deployer.install_dependencies()
    
    # Step 5: Generate Prisma and run migrations if needed
    if deployer.has_prisma:
        logger.step(5, total_steps, "Updating Prisma")
        deployer.generate_prisma()
        deployer.run_prisma_migrate(deploy=True)
    else:
        logger.step(5, total_steps, "Prisma not detected, skipping")
    
    # Step 6: Build application (without stopping the app)
    logger.step(6, total_steps, "Building application")
    deployer.build()
    
    # Step 7: Restart service - this is the only moment of downtime
    logger.step(7, total_steps, "Restarting application")
    logger.substep("Minimal downtime during restart...")
    service_manager = ServiceManager(verbose=args.verbose)
    service_manager.restart(app_name)
    
    # Quick health check
    import time
    time.sleep(2)  # Give the app a moment to start
    
    status = service_manager.get_status(app_name)
    if status.get("active"):
        logger.success(f"Application updated successfully: {domain}")
        logger.blank()
        logger.key_value("Status", "Running")
        logger.key_value("Package Manager", deployer.package_manager)
        if deployer.has_prisma:
            logger.key_value("Prisma", "Updated")
    else:
        logger.warning("Application restarted but may not be running correctly")
        logger.info(f"Check logs with: wasm logs {domain}")
    
    return 0


def _handle_delete(args: Namespace) -> int:
    """Handle webapp delete command."""
    logger = Logger(verbose=args.verbose)
    config = Config()
    
    from wasm.core.store import get_store
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
    
    # Confirmation
    if not args.force:
        response = input(f"Delete application '{domain}'? [y/N] ")
        if response.lower() != "y":
            logger.info("Aborted")
            return 0
    
    logger.header(f"Deleting: {domain}")
    
    total_steps = 5
    
    # Stop and delete service
    logger.step(1, total_steps, "Stopping service")
    service_manager = ServiceManager(verbose=args.verbose)
    try:
        service_manager.delete_service(app_name)
    except Exception:
        pass
    
    # Delete site configuration
    logger.step(2, total_steps, "Removing site configuration")
    try:
        nginx = NginxManager(verbose=args.verbose)
        if nginx.site_exists(domain):
            nginx.delete_site(domain)
            nginx.reload()
    except Exception:
        pass
    
    try:
        apache = ApacheManager(verbose=args.verbose)
        if apache.site_exists(domain):
            apache.delete_site(domain)
            apache.reload()
    except Exception:
        pass
    
    # Delete files
    if not args.keep_files:
        logger.step(3, total_steps, "Removing application files")
        from wasm.core.utils import remove_directory
        remove_directory(app_path, sudo=True)
    else:
        logger.step(3, total_steps, "Keeping application files")
    
    # Delete from store
    logger.step(4, total_steps, "Removing from database")
    if app:
        store.delete_site(domain)
        store.delete_service(app_name)
        store.delete_app(domain)
    
    logger.step(5, total_steps, "Cleanup complete")
    logger.success(f"Application deleted: {domain}")
    
    return 0


def _handle_logs(args: Namespace) -> int:
    """Handle webapp logs command."""
    logger = Logger(verbose=args.verbose)
    service_manager = ServiceManager(verbose=args.verbose)
    
    domain = validate_domain(args.domain)
    app_name = domain_to_app_name(domain)
    
    # Get service name (ServiceManager adds prefix internally)
    service_name = f"wasm-{app_name}"
    
    if args.follow:
        # Use journalctl directly for follow mode
        import subprocess
        try:
            subprocess.run([
                "journalctl",
                "-u", f"{service_name}.service",
                "-f",
                "-n", str(args.lines),
            ])
        except KeyboardInterrupt:
            pass
    else:
        logs = service_manager.logs(app_name, lines=args.lines)
        print(logs)
    
    return 0
