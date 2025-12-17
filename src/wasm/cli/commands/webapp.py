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
        logger.error(str(e))
        if e.details:
            # Print details preserving formatting (for SSH guidance, etc.)
            print()
            for line in e.details.split("\n"):
                print(f"  {line}")
            print()
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
    service_manager = ServiceManager(verbose=args.verbose)
    
    logger.header("Deployed Applications")
    
    services = service_manager.list_services()
    
    if not services:
        logger.info("No applications deployed")
        return 0
    
    # Prepare table data
    headers = ["Domain", "Status", "Service"]
    rows = []
    
    for svc in services:
        name = svc["name"]
        # Extract domain from service name
        if name.startswith("wasm-"):
            domain = name[5:].replace("-", ".")
        else:
            domain = name
        
        status = "🟢 Running" if svc["active"] == "active" else "🔴 Stopped"
        rows.append([domain, status, name])
    
    logger.table(headers, rows)
    
    return 0


def _handle_status(args: Namespace) -> int:
    """Handle webapp status command."""
    logger = Logger(verbose=args.verbose)
    service_manager = ServiceManager(verbose=args.verbose)
    
    domain = validate_domain(args.domain)
    app_name = domain_to_app_name(domain)
    
    status = service_manager.get_status(app_name)
    
    logger.header(f"Status: {domain}")
    
    if not status["exists"]:
        logger.warning(f"Application not found: {domain}")
        return 1
    
    logger.key_value("Service", status["name"])
    logger.key_value("Active", "Yes" if status["active"] else "No")
    logger.key_value("Enabled", "Yes" if status["enabled"] else "No")
    
    if status.get("pid"):
        logger.key_value("PID", status["pid"])
    if status.get("uptime"):
        logger.key_value("Started", status["uptime"])
    
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
    1. Pull/fetch new code
    2. Install dependencies (new packages)
    3. Generate Prisma if needed
    4. Build application
    5. Only then restart service (minimal downtime)
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
    
    total_steps = 6
    
    # Step 1: Pull latest changes or fetch from new source
    from wasm.managers.source_manager import SourceManager
    source_manager = SourceManager(verbose=args.verbose)
    
    new_source = getattr(args, "source", None)
    
    if new_source:
        logger.step(1, total_steps, "Fetching from new source")
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
        logger.step(1, total_steps, "Pulling latest changes")
        source_manager.pull(app_path, branch=args.branch)
    
    # Step 2: Detect app type and configure deployer
    logger.step(2, total_steps, "Detecting application type")
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
    
    # Step 3: Install dependencies (without stopping the app)
    logger.step(3, total_steps, "Installing dependencies")
    deployer.install_dependencies()
    
    # Step 4: Generate Prisma and run migrations if needed
    if deployer.has_prisma:
        logger.step(4, total_steps, "Updating Prisma")
        deployer.generate_prisma()
        deployer.run_prisma_migrate(deploy=True)
    else:
        logger.step(4, total_steps, "Prisma not detected, skipping")
    
    # Step 5: Build application (without stopping the app)
    logger.step(5, total_steps, "Building application")
    deployer.build()
    
    # Step 6: Restart service - this is the only moment of downtime
    logger.step(6, total_steps, "Restarting application")
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
    
    domain = validate_domain(args.domain)
    app_name = domain_to_app_name(domain)
    app_path = config.apps_directory / app_name
    
    # Confirmation
    if not args.force:
        response = input(f"Delete application '{domain}'? [y/N] ")
        if response.lower() != "y":
            logger.info("Aborted")
            return 0
    
    logger.header(f"Deleting: {domain}")
    
    # Stop and delete service
    logger.step(1, 4, "Stopping service")
    service_manager = ServiceManager(verbose=args.verbose)
    try:
        service_manager.delete_service(app_name)
    except Exception:
        pass
    
    # Delete site configuration
    logger.step(2, 4, "Removing site configuration")
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
        logger.step(3, 4, "Removing application files")
        from wasm.core.utils import remove_directory
        remove_directory(app_path, sudo=True)
    else:
        logger.step(3, 4, "Keeping application files")
    
    logger.step(4, 4, "Cleanup complete")
    logger.success(f"Application deleted: {domain}")
    
    return 0


def _handle_logs(args: Namespace) -> int:
    """Handle webapp logs command."""
    logger = Logger(verbose=args.verbose)
    service_manager = ServiceManager(verbose=args.verbose)
    
    domain = validate_domain(args.domain)
    app_name = domain_to_app_name(domain)
    
    if args.follow:
        # Use journalctl directly for follow mode
        import subprocess
        try:
            subprocess.run([
                "journalctl",
                "-u", f"{app_name}.service",
                "-f",
                "-n", str(args.lines),
            ])
        except KeyboardInterrupt:
            pass
    else:
        logs = service_manager.logs(app_name, lines=args.lines)
        print(logs)
    
    return 0
