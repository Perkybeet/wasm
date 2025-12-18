"""
Service command handlers for WASM.
"""

import sys
from argparse import Namespace

from wasm.core.logger import Logger
from wasm.core.exceptions import WASMError
from wasm.managers.service_manager import ServiceManager


def handle_service(args: Namespace) -> int:
    """
    Handle service commands.
    
    Args:
        args: Parsed arguments.
        
    Returns:
        Exit code.
    """
    action = args.action
    
    handlers = {
        "create": _handle_create,
        "list": _handle_list,
        "ls": _handle_list,
        "status": _handle_status,
        "info": _handle_status,
        "start": _handle_start,
        "stop": _handle_stop,
        "restart": _handle_restart,
        "logs": _handle_logs,
        "delete": _handle_delete,
        "remove": _handle_delete,
        "rm": _handle_delete,
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
        return 1
    except Exception as e:
        logger = Logger(verbose=args.verbose)
        logger.error(f"Unexpected error: {e}")
        if args.verbose:
            import traceback
            traceback.print_exc()
        return 1


def _handle_create(args: Namespace) -> int:
    """Handle service create command."""
    logger = Logger(verbose=args.verbose)
    manager = ServiceManager(verbose=args.verbose)
    
    logger.info(f"Creating service: {args.name}")
    
    manager.create_service(
        name=args.name,
        command=args.exec_command,
        working_directory=args.directory,
        user=args.user,
        description=args.description,
    )
    
    logger.success(f"Service created: {args.name}")
    return 0


def _handle_list(args: Namespace) -> int:
    """Handle service list command."""
    logger = Logger(verbose=args.verbose)
    manager = ServiceManager(verbose=args.verbose)
    
    logger.header("Managed Services")
    
    services = manager.list_services(all_services=getattr(args, "all", False))
    
    if not services:
        logger.info("No services found")
        return 0
    
    headers = ["Name", "Status", "State"]
    rows = []
    
    for svc in services:
        status = "🟢" if svc["active"] == "active" else "🔴"
        rows.append([svc["name"], status, svc["sub"]])
    
    logger.table(headers, rows)
    
    return 0


def _handle_status(args: Namespace) -> int:
    """Handle service status command."""
    logger = Logger(verbose=args.verbose)
    manager = ServiceManager(verbose=args.verbose)
    
    status = manager.get_status(args.name)
    
    logger.header(f"Service: {args.name}")
    
    if not status["exists"]:
        logger.warning("Service not found")
        return 1
    
    logger.key_value("Name", status["name"])
    logger.key_value("Active", "Yes" if status["active"] else "No")
    logger.key_value("Enabled", "Yes" if status["enabled"] else "No")
    
    if status.get("pid") and status["pid"] != "0":
        logger.key_value("PID", status["pid"])
    if status.get("uptime"):
        logger.key_value("Started", status["uptime"])
    
    return 0


def _handle_start(args: Namespace) -> int:
    """Handle service start command."""
    logger = Logger(verbose=args.verbose)
    manager = ServiceManager(verbose=args.verbose)
    
    logger.info(f"Starting service: {args.name}")
    manager.start(args.name)
    logger.success(f"Service started: {args.name}")
    
    return 0


def _handle_stop(args: Namespace) -> int:
    """Handle service stop command."""
    logger = Logger(verbose=args.verbose)
    manager = ServiceManager(verbose=args.verbose)
    
    logger.info(f"Stopping service: {args.name}")
    manager.stop(args.name)
    logger.success(f"Service stopped: {args.name}")
    
    return 0


def _handle_restart(args: Namespace) -> int:
    """Handle service restart command."""
    logger = Logger(verbose=args.verbose)
    manager = ServiceManager(verbose=args.verbose)
    
    logger.info(f"Restarting service: {args.name}")
    manager.restart(args.name)
    logger.success(f"Service restarted: {args.name}")
    
    return 0


def _handle_logs(args: Namespace) -> int:
    """Handle service logs command."""
    manager = ServiceManager(verbose=args.verbose)
    
    if args.follow:
        import subprocess
        try:
            service_name = manager._get_service_name(args.name)
            subprocess.run([
                "journalctl",
                "-u", service_name,
                "-f",
                "-n", str(args.lines),
            ])
        except KeyboardInterrupt:
            pass
    else:
        logs = manager.logs(args.name, lines=args.lines)
        print(logs)
    
    return 0


def _handle_delete(args: Namespace) -> int:
    """Handle service delete command."""
    logger = Logger(verbose=args.verbose)
    manager = ServiceManager(verbose=args.verbose)
    
    if not args.force:
        response = input(f"Delete service '{args.name}'? [y/N] ")
        if response.lower() != "y":
            logger.info("Aborted")
            return 0
    
    logger.info(f"Deleting service: {args.name}")
    manager.delete_service(args.name)
    logger.success(f"Service deleted: {args.name}")
    
    return 0
