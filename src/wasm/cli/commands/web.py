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
        "install": _handle_install,
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


# Mapping from import name to package names (apt, pip)
WEB_DEPENDENCIES = {
    "fastapi": ("python3-fastapi", "fastapi>=0.109.0"),
    "uvicorn": ("python3-uvicorn", "uvicorn[standard]>=0.27.0"),
    "jose": ("python3-jose", "python-jose[cryptography]>=3.3.0"),
    "passlib": ("python3-passlib", "passlib[bcrypt]>=1.7.4"),
    "aiofiles": ("python3-aiofiles", "aiofiles>=23.0.0"),
    "psutil": ("python3-psutil", "psutil>=5.9.0"),
}


def _check_dependencies() -> tuple[bool, list[str], list[str]]:
    """Check if web dependencies are installed.
    
    Returns:
        Tuple of (all_installed, missing_apt_packages, missing_pip_packages)
    """
    missing_apt = []
    missing_pip = []
    
    try:
        import fastapi
    except ImportError:
        missing_apt.append("python3-fastapi")
        missing_pip.append("fastapi>=0.109.0")
    
    try:
        import uvicorn
    except ImportError:
        missing_apt.append("python3-uvicorn")
        missing_pip.append("uvicorn[standard]>=0.27.0")
    
    try:
        import jose
    except ImportError:
        missing_apt.append("python3-jose")
        missing_pip.append("python-jose[cryptography]>=3.3.0")
    
    try:
        import passlib
    except ImportError:
        missing_apt.append("python3-passlib")
        missing_pip.append("passlib[bcrypt]>=1.7.4")
    
    try:
        import aiofiles
    except ImportError:
        missing_apt.append("python3-aiofiles")
        missing_pip.append("aiofiles>=23.0.0")
    
    try:
        import psutil
    except ImportError:
        missing_apt.append("python3-psutil")
        missing_pip.append("psutil>=5.9.0")
    
    return (len(missing_apt) == 0, missing_apt, missing_pip)


def _get_install_instructions(missing_apt: list[str], missing_pip: list[str]) -> list[str]:
    """Get installation instructions based on the system."""
    instructions = []
    
    # Check if running on a Debian-based system
    if Path("/etc/debian_version").exists():
        instructions.append(f"sudo apt install {' '.join(missing_apt)}")
    # Check if running on a Fedora/RHEL-based system
    elif Path("/etc/fedora-release").exists() or Path("/etc/redhat-release").exists():
        # Fedora uses different package names
        instructions.append(f"sudo dnf install {' '.join(missing_apt)}")
    # Check if running on openSUSE
    elif Path("/etc/SuSE-release").exists() or Path("/etc/os-release").exists():
        try:
            with open("/etc/os-release") as f:
                if "opensuse" in f.read().lower():
                    instructions.append(f"sudo zypper install {' '.join(missing_apt)}")
        except Exception:
            pass
    
    # Always add pip as fallback option
    instructions.append(f"pip install {' '.join(missing_pip)}")
    
    return instructions


def _is_externally_managed() -> bool:
    """Check if Python environment is externally managed (PEP 668)."""
    # Check for EXTERNALLY-MANAGED marker file
    import sysconfig
    stdlib_path = sysconfig.get_path("stdlib")
    if stdlib_path:
        marker = Path(stdlib_path) / "EXTERNALLY-MANAGED"
        return marker.exists()
    return False


def _install_with_pip(packages: list[str], verbose: bool = False, force: bool = False) -> bool:
    """Install packages using pip.
    
    Args:
        packages: List of pip package specifications
        verbose: Show verbose output
        force: Use --break-system-packages for externally managed environments
        
    Returns:
        True if installation succeeded
    """
    logger = Logger(verbose=verbose)
    
    try:
        cmd = [sys.executable, "-m", "pip", "install", "--user"] + packages
        
        # Add --break-system-packages for externally managed environments
        if force or _is_externally_managed():
            cmd.insert(4, "--break-system-packages")
        
        logger.info(f"Installing: {' '.join(packages)}")
        
        result = subprocess.run(
            cmd,
            capture_output=not verbose,
            text=True,
        )
        
        if result.returncode != 0:
            if not verbose and result.stderr:
                logger.error(result.stderr)
            return False
        
        return True
    except Exception as e:
        logger.error(f"Failed to install packages: {e}")
        return False


def _install_with_apt(packages: list[str], verbose: bool = False) -> bool:
    """Install packages using apt.
    
    Args:
        packages: List of apt package names
        verbose: Show verbose output
        
    Returns:
        True if installation succeeded
    """
    logger = Logger(verbose=verbose)
    
    try:
        cmd = ["sudo", "apt-get", "install", "-y"] + packages
        
        logger.info(f"Installing: {' '.join(packages)}")
        
        result = subprocess.run(
            cmd,
            capture_output=not verbose,
            text=True,
        )
        
        if result.returncode != 0:
            if not verbose and result.stderr:
                logger.error(result.stderr)
            return False
        
        return True
    except Exception as e:
        logger.error(f"Failed to install packages: {e}")
        return False


def _prompt_install(missing_apt: list[str], missing_pip: list[str], verbose: bool = False) -> bool:
    """Prompt user to install missing dependencies.
    
    Args:
        missing_apt: List of missing apt packages
        missing_pip: List of missing pip packages
        verbose: Show verbose output
        
    Returns:
        True if user chose to install and installation succeeded
    """
    logger = Logger(verbose=verbose)
    
    # Check if we're in an interactive terminal
    if not sys.stdin.isatty():
        return False
    
    logger.blank()
    print("Would you like to install the missing dependencies now?")
    print("")
    
    # Determine installation method
    is_debian = Path("/etc/debian_version").exists()
    
    if is_debian:
        print("  [1] Using apt (system packages, recommended)")
        print("  [2] Using pip (user packages)")
        print("  [n] No, show manual instructions")
        print("")
        
        try:
            choice = input("Your choice [1/2/n]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("")
            return False
        
        if choice == "1":
            return _install_with_apt(missing_apt, verbose)
        elif choice == "2":
            return _install_with_pip(missing_pip, verbose)
        else:
            return False
    else:
        print("  [y] Yes, install with pip")
        print("  [n] No, show manual instructions")
        print("")
        
        try:
            choice = input("Your choice [y/n]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("")
            return False
        
        if choice == "y":
            return _install_with_pip(missing_pip, verbose)
        else:
            return False


def _handle_start(args: Namespace) -> int:
    """Handle web start command."""
    logger = Logger(verbose=args.verbose)
    
    # Check dependencies
    all_installed, missing_apt, missing_pip = _check_dependencies()
    if not all_installed:
        logger.error("Web dependencies not installed")
        logger.info(f"Missing packages: {', '.join(missing_apt)}")
        
        # Offer to install automatically
        if _prompt_install(missing_apt, missing_pip, args.verbose):
            # Re-check after installation
            all_installed, _, _ = _check_dependencies()
            if all_installed:
                logger.success("Dependencies installed successfully!")
                logger.blank()
            else:
                logger.error("Some dependencies could not be installed")
                return 1
        else:
            logger.blank()
            logger.info("Install manually with one of the following:")
            for instruction in _get_install_instructions(missing_apt, missing_pip):
                logger.info(f"  {instruction}")
            logger.blank()
            logger.info("Or run: wasm web install")
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
    
    all_installed, missing_apt, missing_pip = _check_dependencies()
    if not all_installed:
        logger.error("Web dependencies not installed")
        logger.info(f"Missing packages: {', '.join(missing_apt)}")
        logger.info("")
        logger.info("Install with one of the following:")
        for instruction in _get_install_instructions(missing_apt, missing_pip):
            logger.info(f"  {instruction}")
        logger.blank()
        logger.info("Or run: wasm web install")
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


def _handle_install(args: Namespace) -> int:
    """Handle web install command - install web dependencies."""
    logger = Logger(verbose=args.verbose)
    
    logger.header("Installing WASM Web Dependencies")
    
    # Check if already installed
    all_installed, missing_apt, missing_pip = _check_dependencies()
    if all_installed:
        logger.success("All web dependencies are already installed!")
        return 0
    
    logger.info(f"Missing packages: {', '.join(missing_apt)}")
    logger.blank()
    
    # Determine installation method
    is_debian = Path("/etc/debian_version").exists()
    use_apt = getattr(args, 'apt', False)
    use_pip = getattr(args, 'pip', False)
    
    # If neither specified, prompt or use default
    if not use_apt and not use_pip:
        if is_debian and sys.stdin.isatty():
            print("Choose installation method:")
            print("  [1] apt (system packages, recommended)")
            print("  [2] pip (user packages)")
            print("")
            try:
                choice = input("Your choice [1/2]: ").strip()
            except (EOFError, KeyboardInterrupt):
                print("")
                return 1
            
            use_apt = (choice == "1")
            use_pip = (choice == "2")
            
            if not use_apt and not use_pip:
                logger.error("Invalid choice")
                return 1
        else:
            # Default to pip for non-Debian or non-interactive
            use_pip = True
    
    if use_apt:
        logger.info("Installing with apt...")
        if _install_with_apt(missing_apt, args.verbose):
            # Verify installation
            all_installed, _, _ = _check_dependencies()
            if all_installed:
                logger.success("Web dependencies installed successfully!")
                logger.blank()
                logger.info("You can now start the web server with: wasm web start")
                return 0
            else:
                logger.warning("Some packages may not be available via apt")
                logger.info("Falling back to pip for remaining packages...")
                _, _, remaining_pip = _check_dependencies()
                if _install_with_pip(remaining_pip, args.verbose):
                    logger.success("Web dependencies installed successfully!")
                    return 0
        logger.error("Failed to install dependencies")
        return 1
    
    if use_pip:
        logger.info("Installing with pip...")
        if _install_with_pip(missing_pip, args.verbose):
            # Verify installation
            all_installed, _, _ = _check_dependencies()
            if all_installed:
                logger.success("Web dependencies installed successfully!")
                logger.blank()
                logger.info("You can now start the web server with: wasm web start")
                return 0
        logger.error("Failed to install dependencies")
        return 1
    
    return 1
