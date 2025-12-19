"""
Web interface command handlers for WASM.

Commands for starting and managing the web dashboard.
"""

import os
import sys
import signal
import subprocess
from argparse import Namespace
from pathlib import Path
from typing import Optional

from wasm.core.logger import Logger
from wasm.core.exceptions import WASMError


# PID file location
PID_FILE = Path("/var/run/wasm-web.pid")
PID_FILE_USER = Path.home() / ".wasm" / "web.pid"


def get_pid_file() -> Path:
    """Get the appropriate PID file path."""
    if os.geteuid() == 0:
        return PID_FILE
    return PID_FILE_USER


def handle_web(args: Namespace) -> int:
    """
    Handle web commands.
    
    Args:
        args: Parsed arguments.
        
    Returns:
        Exit code.
    """
    action = args.action
    
    handlers = {
        "start": _handle_start,
        "stop": _handle_stop,
        "status": _handle_status,
        "restart": _handle_restart,
        "token": _handle_token,
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
    except KeyboardInterrupt:
        print("\nShutting down...")
        return 0
    except Exception as e:
        logger = Logger(verbose=args.verbose)
        logger.error(f"Unexpected error: {e}")
        if args.verbose:
            import traceback
            traceback.print_exc()
        return 1


def _check_dependencies() -> bool:
    """Check if web dependencies are installed."""
    try:
        import fastapi
        import uvicorn
        import jose
        return True
    except ImportError:
        return False


def _handle_start(args: Namespace) -> int:
    """Handle web start command."""
    logger = Logger(verbose=args.verbose)
    
    # Check dependencies
    if not _check_dependencies():
        logger.error("Web dependencies not installed")
        logger.info("Install with: pip install wasm-cli[web]")
        return 1
    
    # Check if already running
    pid_file = get_pid_file()
    if pid_file.exists():
        try:
            pid = int(pid_file.read_text().strip())
            # Check if process exists
            os.kill(pid, 0)
            logger.warning(f"Web server already running (PID: {pid})")
            logger.info("Use 'wasm web stop' to stop it first")
            return 1
        except (ProcessLookupError, ValueError):
            # Process not running, remove stale PID file
            pid_file.unlink(missing_ok=True)
    
    # Get configuration
    host = getattr(args, 'host', '127.0.0.1') or '127.0.0.1'
    port = getattr(args, 'port', 8080) or 8080
    daemon = getattr(args, 'daemon', False)
    
    # Security warning for 0.0.0.0
    if host == '0.0.0.0':
        logger.warning("⚠️  Binding to 0.0.0.0 exposes the server to all network interfaces!")
        logger.warning("⚠️  Make sure your firewall is configured properly.")
    
    if daemon:
        # Run in background
        return _start_daemon(host, port, args.verbose)
    else:
        # Run in foreground
        return _start_foreground(host, port)


def _start_foreground(host: str, port: int) -> int:
    """Start the web server in foreground."""
    from wasm.web.server import run_server
    from wasm.web.auth import SecurityConfig
    
    # Create PID file
    pid_file = get_pid_file()
    pid_file.parent.mkdir(parents=True, exist_ok=True)
    pid_file.write_text(str(os.getpid()))
    
    try:
        # Configure security
        config = SecurityConfig(
            host=host,
            port=port,
            rate_limit_enabled=True,
        )
        
        # Allow all hosts if binding to 0.0.0.0
        if host == '0.0.0.0':
            config.allowed_hosts = []
        
        # Run server
        run_server(
            host=host,
            port=port,
            config=config,
            show_token=True,
        )
        return 0
    finally:
        pid_file.unlink(missing_ok=True)


def _start_daemon(host: str, port: int, verbose: bool) -> int:
    """Start the web server as a daemon."""
    logger = Logger(verbose=verbose)
    
    # Fork process
    pid = os.fork()
    
    if pid > 0:
        # Parent process
        logger.success(f"Web server started in background (PID: {pid})")
        logger.info(f"Server running at http://{host}:{port}")
        logger.info("Use 'wasm web status' to check status")
        logger.info("Use 'wasm web stop' to stop the server")
        return 0
    
    # Child process
    os.setsid()
    
    # Second fork
    pid = os.fork()
    if pid > 0:
        os._exit(0)
    
    # Redirect standard file descriptors
    sys.stdout.flush()
    sys.stderr.flush()
    
    with open('/dev/null', 'r') as devnull:
        os.dup2(devnull.fileno(), sys.stdin.fileno())
    
    log_file = Path("/var/log/wasm/web.log")
    if not log_file.parent.exists():
        log_file = Path.home() / ".wasm" / "web.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)
    
    with open(log_file, 'a') as log:
        os.dup2(log.fileno(), sys.stdout.fileno())
        os.dup2(log.fileno(), sys.stderr.fileno())
    
    # Write PID file
    pid_file = get_pid_file()
    pid_file.parent.mkdir(parents=True, exist_ok=True)
    pid_file.write_text(str(os.getpid()))
    
    # Start server
    try:
        from wasm.web.server import run_server
        from wasm.web.auth import SecurityConfig
        
        config = SecurityConfig(host=host, port=port)
        if host == '0.0.0.0':
            config.allowed_hosts = []
        
        run_server(host=host, port=port, config=config, show_token=False)
    finally:
        pid_file.unlink(missing_ok=True)
    
    os._exit(0)


def _handle_stop(args: Namespace) -> int:
    """Handle web stop command."""
    logger = Logger(verbose=args.verbose)
    
    pid_file = get_pid_file()
    
    if not pid_file.exists():
        logger.info("Web server is not running")
        return 0
    
    try:
        pid = int(pid_file.read_text().strip())
        
        # Send SIGTERM
        os.kill(pid, signal.SIGTERM)
        logger.success(f"Web server stopped (PID: {pid})")
        
        # Remove PID file
        pid_file.unlink(missing_ok=True)
        
        return 0
        
    except ProcessLookupError:
        logger.info("Web server is not running (stale PID file removed)")
        pid_file.unlink(missing_ok=True)
        return 0
    except ValueError:
        logger.error("Invalid PID file")
        pid_file.unlink(missing_ok=True)
        return 1
    except PermissionError:
        logger.error("Permission denied. Try running with sudo.")
        return 1


def _handle_status(args: Namespace) -> int:
    """Handle web status command."""
    logger = Logger(verbose=args.verbose)
    
    pid_file = get_pid_file()
    
    logger.header("WASM Web Interface Status")
    
    if not pid_file.exists():
        logger.key_value("Status", "🔴 Not running")
        return 0
    
    try:
        pid = int(pid_file.read_text().strip())
        
        # Check if process is running
        os.kill(pid, 0)
        
        logger.key_value("Status", "🟢 Running")
        logger.key_value("PID", str(pid))
        
        # Try to get more info
        try:
            import psutil
            proc = psutil.Process(pid)
            logger.key_value("Memory", f"{proc.memory_info().rss / 1024 / 1024:.1f} MB")
            logger.key_value("Started", proc.create_time())
        except Exception:
            pass
        
        return 0
        
    except ProcessLookupError:
        logger.key_value("Status", "🔴 Not running (stale PID)")
        pid_file.unlink(missing_ok=True)
        return 0
    except ValueError:
        logger.error("Invalid PID file")
        return 1


def _handle_restart(args: Namespace) -> int:
    """Handle web restart command."""
    logger = Logger(verbose=args.verbose)
    
    logger.info("Restarting web server...")
    
    # Stop first
    _handle_stop(args)
    
    # Brief pause
    import time
    time.sleep(1)
    
    # Start again
    return _handle_start(args)


def _handle_token(args: Namespace) -> int:
    """Handle token regeneration."""
    logger = Logger(verbose=args.verbose)
    
    if not _check_dependencies():
        logger.error("Web dependencies not installed")
        return 1
    
    from wasm.web.auth import TokenManager, SecurityConfig
    
    config = SecurityConfig()
    token_manager = TokenManager(config)
    
    if getattr(args, 'regenerate', False):
        # Regenerate token
        new_token = token_manager.rotate_secrets()
        logger.success("New access token generated")
        logger.blank()
        print(f"🔐 Access Token: {new_token}")
        logger.blank()
        logger.warning("All existing sessions have been revoked")
        logger.info("Restart the web server to apply the new token")
    else:
        # Show current token info
        logger.info("Use --regenerate to generate a new token")
        logger.info("This will revoke all existing sessions")
    
    return 0
