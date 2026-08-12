"""
Web interface command handlers for WASM.

This is the only way the panel is started in practice, so the security posture
of a deployment is decided here. Two rules follow from the panel being a root
shell with a login form:

- **Everything ``SecurityConfig`` can enforce is reachable from the command
  line.** A flag that only exists in Python is a flag nobody sets, which is how
  a panel ends up running with ``require_https=False`` and an empty whitelist.
- **Binding beyond loopback without protection is an error, not a warning.** A
  warning scrolls past; the operator wanted the panel up, and it comes up. So
  ``--host 0.0.0.0`` is refused unless the panel terminates TLS itself or only
  answers a declared list of addresses.
"""

from __future__ import annotations

import importlib.util
import os
import signal
import sys
from argparse import ArgumentParser, Namespace
from pathlib import Path
from typing import TYPE_CHECKING

from wasm.core.exceptions import SecurityError, WASMError
from wasm.core.logger import Logger
from wasm.core.runner import get_runner

if TYPE_CHECKING:
    from wasm.web.auth import SecurityConfig

# PID file location
PID_FILE = Path("/var/run/wasm-web.pid")
PID_FILE_USER = Path.home() / ".wasm" / "web.pid"

#: Addresses that only the machine itself can reach. Anything else is exposed
#: to a network and has to justify itself.
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost", ""})

#: Package installation is slow on a cold cache but must not hang a session.
INSTALL_TIMEOUT = 900


def get_pid_file() -> Path:
    """Get the appropriate PID file path."""
    if os.geteuid() == 0:
        return PID_FILE
    return PID_FILE_USER


def add_start_arguments(parser: ArgumentParser) -> None:
    """
    Register the options that decide how exposed a panel is.

    The CLI parser calls this for ``web start`` and ``web restart`` so that both
    accept exactly the same security flags.

    Args:
        parser: The subcommand parser to extend.
    """
    parser.add_argument(
        "--host",
        "-H",
        default="127.0.0.1",
        help="Host to bind to (default: 127.0.0.1). Anything else requires --require-https "
        "or --allow-ip",
    )
    parser.add_argument("--port", "-p", type=int, default=8080, help="Port to listen on")
    parser.add_argument("--daemon", "-d", action="store_true", help="Run in background as daemon")
    parser.add_argument(
        "--require-https",
        action="store_true",
        help="Serve TLS directly and refuse cleartext requests. Needs --tls-cert and --tls-key",
    )
    parser.add_argument("--tls-cert", default=None, help="Path to the TLS certificate chain")
    parser.add_argument("--tls-key", default=None, help="Path to the TLS private key")
    parser.add_argument(
        "--allow-ip",
        action="append",
        default=None,
        metavar="ADDR/CIDR",
        help="Only answer this address or network. Repeatable",
    )
    parser.add_argument(
        "--trusted-proxy",
        action="append",
        default=None,
        metavar="ADDR/CIDR",
        help="Believe X-Forwarded-For and X-Forwarded-Proto from this peer. Declare your "
        "TLS terminating proxy here so the session cookie is issued with Secure",
    )


def build_security_config(args: Namespace) -> SecurityConfig:
    """
    Turn parsed arguments into a security configuration.

    Args:
        args: Parsed command line arguments.

    Returns:
        The configuration the server should run with.

    Raises:
        SecurityError: When the requested combination would put a root panel on
            a network unprotected.
    """
    from wasm.web.auth import SecurityConfig

    host = getattr(args, "host", "127.0.0.1") or "127.0.0.1"
    port = getattr(args, "port", 8080) or 8080
    whitelist = list(getattr(args, "allow_ip", None) or [])
    proxies = list(getattr(args, "trusted_proxy", None) or [])
    require_https = bool(getattr(args, "require_https", False))
    cert = getattr(args, "tls_cert", None)
    key = getattr(args, "tls_key", None)

    if require_https and not (cert and key):
        raise SecurityError(
            "--require-https needs a certificate and a private key",
            details=(
                "Pass --tls-cert and --tls-key. For a public domain: "
                "'certbot certonly --standalone -d panel.example.com' then "
                "--tls-cert /etc/letsencrypt/live/panel.example.com/fullchain.pem "
                "--tls-key /etc/letsencrypt/live/panel.example.com/privkey.pem."
            ),
        )

    if host not in LOOPBACK_HOSTS and not require_https and not whitelist:
        raise SecurityError(
            f"Refusing to expose the WASM panel on {host} without TLS or an IP whitelist",
            details=(
                "The panel drives systemd, nginx and certbot as root, so this would put a "
                "root shell on the network in cleartext. Pick one:\n"
                "  - keep it local and reach it over SSH: "
                f"wasm web start --host 127.0.0.1 --port {port} "
                f"(then 'ssh -L {port}:127.0.0.1:{port} user@server')\n"
                "  - terminate TLS in the panel: --require-https --tls-cert CERT --tls-key KEY\n"
                "  - restrict who may connect: --allow-ip 10.0.0.0/24 (repeatable)\n"
                "If a reverse proxy already terminates TLS, bind to 127.0.0.1 and declare it "
                "with --trusted-proxy so the session cookie is issued with the Secure flag."
            ),
        )

    config = SecurityConfig(
        host=host,
        port=port,
        rate_limit_enabled=True,
        require_https=require_https,
        ssl_certfile=cert,
        ssl_keyfile=key,
        ip_whitelist=whitelist,
        trusted_proxies=proxies,
    )

    if host not in LOOPBACK_HOSTS:
        # The Host header of a panel reachable by address or by name is not
        # something we can enumerate for the operator.
        config.allowed_hosts = []

    return config


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
    """
    Check whether the web dependencies are importable.

    Returns:
        Whether everything is present, the missing system packages, and the
        missing pip requirements, in that order.
    """
    missing_apt: list[str] = []
    missing_pip: list[str] = []

    for module, (apt_package, pip_requirement) in WEB_DEPENDENCIES.items():
        # find_spec asks the import system without executing the module, so a
        # dependency check cannot have side effects of its own.
        if importlib.util.find_spec(module) is None:
            missing_apt.append(apt_package)
            missing_pip.append(pip_requirement)

    return (not missing_apt, missing_apt, missing_pip)


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

    cmd = [sys.executable, "-m", "pip", "install", "--user"]
    # Add --break-system-packages for externally managed environments
    if force or _is_externally_managed():
        cmd.append("--break-system-packages")
    cmd.extend(packages)

    logger.info(f"Installing: {' '.join(packages)}")

    result = get_runner().run(cmd, timeout=INSTALL_TIMEOUT)
    if not result.success:
        logger.error(result.stderr or result.stdout or f"pip exited with {result.exit_code}")
        return False
    return True


def _install_with_apt(packages: list[str], verbose: bool = False) -> bool:
    """Install packages using apt.

    Args:
        packages: List of apt package names
        verbose: Show verbose output

    Returns:
        True if installation succeeded
    """
    logger = Logger(verbose=verbose)

    logger.info(f"Installing: {' '.join(packages)}")

    # WASM requires root; there is no sudo to escalate with and nothing to
    # escalate from. See the v1 design note on privilege.
    result = get_runner().run(
        ["apt-get", "install", "-y", *packages],
        timeout=INSTALL_TIMEOUT,
        env={"DEBIAN_FRONTEND": "noninteractive"},
    )
    if not result.success:
        logger.error(result.stderr or result.stdout or f"apt-get exited with {result.exit_code}")
        return False
    return True


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

    # Refuses an unsafe exposure before anything is bound or written.
    config = build_security_config(args)
    host = config.host

    if config.ip_whitelist:
        logger.info(f"Only these clients may connect: {', '.join(config.ip_whitelist)}")
    if config.trusted_proxies:
        logger.info(f"Forwarding headers believed from: {', '.join(config.trusted_proxies)}")
    elif host in LOOPBACK_HOSTS and not config.require_https:
        logger.warning(
            "Serving plain HTTP on loopback. Behind a TLS proxy, pass --trusted-proxy "
            "so the session cookie is issued with the Secure flag."
        )

    if getattr(args, "daemon", False):
        return _start_daemon(config, args.verbose)
    return _start_foreground(config)


def _start_foreground(config: SecurityConfig) -> int:
    """
    Start the web server in the foreground.

    Args:
        config: The security configuration to serve with.

    Returns:
        Exit code.
    """
    from wasm.web.server import run_server

    # Create PID file
    pid_file = get_pid_file()
    pid_file.parent.mkdir(parents=True, exist_ok=True)
    pid_file.write_text(str(os.getpid()))

    try:
        run_server(host=config.host, port=config.port, config=config, show_token=True)
        return 0
    finally:
        pid_file.unlink(missing_ok=True)


def _start_daemon(config: SecurityConfig, verbose: bool) -> int:
    """
    Start the web server as a daemon.

    Args:
        config: The security configuration to serve with.
        verbose: Whether to log verbosely.

    Returns:
        Exit code.
    """
    logger = Logger(verbose=verbose)
    scheme = "https" if config.require_https else "http"

    # Fork process
    pid = os.fork()

    if pid > 0:
        # Parent process
        logger.success(f"Web server started in background (PID: {pid})")
        logger.info(f"Server running at {scheme}://{config.host}:{config.port}")
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

    with open("/dev/null") as devnull:
        os.dup2(devnull.fileno(), sys.stdin.fileno())

    log_file = Path("/var/log/wasm/web.log")
    if not log_file.parent.exists():
        log_file = Path.home() / ".wasm" / "web.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)

    with open(log_file, "a") as log:
        os.dup2(log.fileno(), sys.stdout.fileno())
        os.dup2(log.fileno(), sys.stderr.fileno())

    # Write PID file
    pid_file = get_pid_file()
    pid_file.parent.mkdir(parents=True, exist_ok=True)
    pid_file.write_text(str(os.getpid()))

    # Start server
    try:
        from wasm.web.server import run_server

        run_server(host=config.host, port=config.port, config=config, show_token=False)
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
        logger.key_value("Status", "not running")
        return 0

    try:
        pid = int(pid_file.read_text().strip())

        # Check if process is running
        os.kill(pid, 0)

        logger.key_value("Status", "running")
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
        logger.key_value("Status", "not running (stale PID)")
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

    from wasm.web.auth import SecurityConfig, TokenManager

    config = SecurityConfig()
    token_manager = TokenManager(config)

    if getattr(args, "regenerate", False):
        # Regenerate token
        new_token = token_manager.rotate_secrets()
        logger.success("New access token generated")
        logger.blank()
        print(f"Access Token: {new_token}")
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
    use_apt = getattr(args, "apt", False)
    use_pip = getattr(args, "pip", False)

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

            use_apt = choice == "1"
            use_pip = choice == "2"

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
