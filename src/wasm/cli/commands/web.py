# Copyright (c) 2024-2026 Yago Lopez Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
The ``wasm web`` command group.

This is the only way the panel is started in practice, so the security posture
of a deployment is decided here. Two rules follow from the panel being a root
shell with a login form:

- **Everything ``SecurityConfig`` can enforce is reachable from the command
  line.** A flag that only exists in Python is a flag nobody sets, which is how
  a panel ends up running with ``require_https=False`` and an empty whitelist.
- **Binding beyond loopback without protection is an error, not a warning.** A
  warning scrolls past; the operator wanted the panel up, and it comes up. So
  ``--host 0.0.0.0`` is refused unless the panel terminates TLS itself or only
  answers a declared list of addresses. The decision is made about the address
  that would be bound, not about the string typed: "", ``*``, ``0``, ``::`` and
  a name that resolves to 0.0.0.0 are all the same socket, and a set of
  known-good host strings recognised one of them.
- **``wasm web token`` reports; it does not rotate.** The command people run to
  look the root credential up cannot be the command that revokes it. Issuing
  takes ``--new`` and a confirmation that names what stops working.

Two structural notes about the Click migration:

- The command bodies hold no logic. Every command parses its options and hands
  them to a private ``_start`` / ``_stop`` / ``_token`` helper, which is also
  what the surviving ``handle_web`` argparse entry point calls. One
  implementation, two front doors, until the argparse tree is deleted.
- ``--verbose``, ``--dry-run`` and ``--no-color`` are accepted after the
  subcommand, as they always were, but they do not become per-command
  parameters: :func:`global_flags` declares them with ``expose_value=False`` and
  folds them into the shared :class:`~wasm.cli.app.Context`. That is what
  distinguishes re-exposing a global flag from redeclaring it, and redeclaring
  it is the argparse bug this migration exists to remove.
"""

from __future__ import annotations

import importlib.util
import ipaddress
import os
import signal
import socket
import sys
import time
from argparse import ArgumentParser, Namespace
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, NoReturn, TypeVar

import click

from wasm.cli.app import Context, enable_dry_run, pass_context
from wasm.core.exceptions import SecurityError, WASMError
from wasm.core.fs import get_fs
from wasm.core.logger import Logger, set_colors_disabled
from wasm.core.runner import get_runner

if TYPE_CHECKING:
    from wasm.web.auth import SecurityConfig

# PID file location
PID_FILE = Path("/var/run/wasm-web.pid")
PID_FILE_USER = Path.home() / ".wasm" / "web.pid"

#: The address a socket is given when it should answer on every interface. It
#: is named here to be recognised and refused, never bound by default.
ALL_INTERFACES = "0.0.0.0"  # noqa: S104

#: Spellings of "every interface" that no resolver accepts, so they cannot be
#: classified by looking them up. The empty string is the one that mattered: a
#: version of this module kept a set of loopback *strings* with "" in it, and
#: ``wasm web start --host ""`` walked past the refusal below and bound the
#: root panel to every interface in cleartext.
WILDCARD_SPELLINGS = frozenset({"", "*"})

#: Package installation is slow on a cold cache but must not hang a session.
INSTALL_TIMEOUT = 900

#: Seconds between stopping and starting again, so the port is released.
RESTART_PAUSE = 1

F = TypeVar("F", bound=Callable[..., Any])


def get_pid_file() -> Path:
    """
    Return the PID file this process may write.

    Returns:
        ``/var/run/wasm-web.pid`` for root, a path under ``~/.wasm`` otherwise.
    """
    if os.geteuid() == 0:
        return PID_FILE
    return PID_FILE_USER


# ---------------------------------------------------------------------------
# Global flags, re-exposed rather than redeclared
# ---------------------------------------------------------------------------


def _adopt_global_flag(attribute: str) -> Callable[[click.Context, click.Parameter, bool], None]:
    """
    Build the callback that folds a global flag into the shared context.

    Args:
        attribute: Name of the :class:`~wasm.cli.app.Context` field to set.

    Returns:
        A Click option callback.
    """

    def callback(ctx: click.Context, param: click.Parameter, value: bool) -> None:
        if not value:
            return
        state = ctx.ensure_object(Context)
        setattr(state, attribute, True)

        # The root callback has already run by the time a subcommand's options
        # are parsed, so a flag typed after the subcommand name has to apply
        # its own side effects. Nothing here is a per-command decision: it is
        # the same global effect, reached late.
        if attribute == "verbose":
            # The cached logger was built with the old verbosity.
            state._logger = None
        elif attribute == "no_color":
            set_colors_disabled(True)
        elif attribute == "dry_run":
            # Both seams are swapped by the one helper. Wiring the runner here
            # by hand is what left the filesystem seam untouched, so a
            # rehearsal still wrote PID files and rotated tokens for real.
            enable_dry_run(state)

    return callback


#: Applied in declaration order, so verbosity is adopted before anything that
#: logs during parsing.
_GLOBAL_FLAGS = (
    click.option(
        "-v",
        "--verbose",
        is_flag=True,
        expose_value=False,
        is_eager=True,
        callback=_adopt_global_flag("verbose"),
        help="Show the detail of each step.",
    ),
    click.option(
        "--dry-run",
        is_flag=True,
        expose_value=False,
        is_eager=True,
        callback=_adopt_global_flag("dry_run"),
        help="Rehearse without changing anything. Read-only checks still run.",
    ),
    click.option(
        "--no-color",
        is_flag=True,
        expose_value=False,
        is_eager=True,
        callback=_adopt_global_flag("no_color"),
        help="Never emit colour.",
    ),
)


def global_flags(command: F) -> F:
    """
    Accept the global flags after this command's name.

    ``wasm web start --verbose`` has always worked and is in scripts, so the
    flags stay spellable here. They carry ``expose_value=False``: the value goes
    to the shared context and never reaches the command as a parameter, which is
    what stops a subcommand from overwriting what the user typed before it.

    Args:
        command: The command function being decorated.

    Returns:
        The same function, with the global options attached.
    """
    for option in reversed(_GLOBAL_FLAGS):
        command = option(command)
    return command


def _exit(code: int) -> NoReturn:
    """
    End the current command with an exit status.

    Args:
        code: Process exit status.

    Raises:
        click.exceptions.Exit: Always; this is how Click unwinds a command.
    """
    click.get_current_context().exit(code)


# ---------------------------------------------------------------------------
# How the panel is exposed
# ---------------------------------------------------------------------------

#: Address family alias, for the helpers below.
IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address


def _strip_brackets(host: str) -> str:
    """
    Remove the brackets an IPv6 literal is often written with.

    Args:
        host: The spelling the operator typed.

    Returns:
        The spelling without a surrounding ``[]`` pair.
    """
    candidate = host.strip()
    if len(candidate) > 1 and candidate.startswith("[") and candidate.endswith("]"):
        return candidate[1:-1]
    return candidate


def _host_addresses(host: str) -> tuple[IPAddress, ...]:
    """
    Return every address a host spelling would end up bound to.

    Comparing host strings is what made this decision wrong: "" , "*", "0",
    "0.0.0.0", "::" and a name in ``/etc/hosts`` pointing at 0.0.0.0 are six
    spellings of the same socket, and a set of known-good strings misses five
    of them. The resolver is asked instead, and its answer is classified with
    :mod:`ipaddress`.

    Args:
        host: The spelling the operator typed.

    Returns:
        The addresses, empty when the spelling cannot be resolved. Empty means
        "unknown", and every caller treats unknown as exposed.
    """
    candidate = _strip_brackets(host)
    if candidate in WILDCARD_SPELLINGS:
        return (ipaddress.ip_address(ALL_INTERFACES),)

    try:
        return (ipaddress.ip_address(candidate),)
    except ValueError:
        pass

    try:
        # A resolution covers names and also the inet_aton spellings ipaddress
        # refuses but a socket accepts, such as "0" (INADDR_ANY) and "127.1".
        infos = socket.getaddrinfo(candidate, None, type=socket.SOCK_STREAM)
    except (OSError, UnicodeError, ValueError):
        return ()

    addresses = []
    for info in infos:
        # A link-local sockaddr carries a %scope suffix that ipaddress refuses.
        text = str(info[4][0]).split("%", 1)[0]
        try:
            addresses.append(ipaddress.ip_address(text))
        except ValueError:
            return ()
    return tuple(addresses)


def _is_loopback_host(host: str) -> bool:
    """
    Report whether only this machine could reach a panel bound to a host.

    Args:
        host: The spelling the operator typed.

    Returns:
        True only when the spelling is known to resolve to loopback addresses
        and nothing else. A spelling that cannot be resolved is not loopback,
        because guessing in the other direction publishes a root shell.
    """
    addresses = _host_addresses(host)
    return bool(addresses) and all(address.is_loopback for address in addresses)


def _normalize_host(host: str) -> str:
    """
    Canonicalise a host spelling so what is checked is what is reported.

    Args:
        host: The spelling the operator typed.

    Returns:
        The canonical spelling. Every way of writing "every interface" becomes
        the address it binds, so the refusal below names a real address instead
        of quoting an empty string back at the operator.
    """
    candidate = _strip_brackets(host)
    addresses = _host_addresses(candidate)
    if not addresses:
        return candidate
    if candidate in WILDCARD_SPELLINGS or all(address.is_unspecified for address in addresses):
        return str(addresses[0])
    try:
        return str(ipaddress.ip_address(candidate))
    except ValueError:
        # A name: binding it is the resolver's business, not this module's.
        return candidate


@dataclass(frozen=True)
class StartOptions:
    """
    Everything that decides how reachable a panel is.

    Attributes:
        host: Interface to bind to.
        port: TCP port to listen on.
        daemon: Whether to detach into the background.
        require_https: Whether the panel terminates TLS itself.
        tls_cert: Path to the certificate chain.
        tls_key: Path to the private key.
        allow_ip: Addresses or CIDRs allowed to connect, empty for anyone.
        trusted_proxy: Peers whose forwarding headers are believed.
    """

    host: str = "127.0.0.1"
    port: int = 8080
    daemon: bool = False
    require_https: bool = False
    tls_cert: str | None = None
    tls_key: str | None = None
    allow_ip: tuple[str, ...] = ()
    trusted_proxy: tuple[str, ...] = ()


def add_start_arguments(parser: ArgumentParser) -> None:
    """
    Register the options that decide how exposed a panel is.

    The legacy argparse parser calls this for ``web start`` and ``web restart``
    so that both accept exactly the same security flags.

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


def _start_options(args: Namespace) -> StartOptions:
    """
    Read start options off an argparse namespace.

    Args:
        args: Parsed command line arguments.

    Returns:
        The same options the Click command builds directly.
    """
    return StartOptions(
        host=getattr(args, "host", None) or "127.0.0.1",
        port=getattr(args, "port", None) or 8080,
        daemon=bool(getattr(args, "daemon", False)),
        require_https=bool(getattr(args, "require_https", False)),
        tls_cert=getattr(args, "tls_cert", None),
        tls_key=getattr(args, "tls_key", None),
        allow_ip=tuple(getattr(args, "allow_ip", None) or ()),
        trusted_proxy=tuple(getattr(args, "trusted_proxy", None) or ()),
    )


def _build_security_config(options: StartOptions) -> SecurityConfig:
    """
    Turn start options into a security configuration.

    Args:
        options: How the operator asked for the panel to be exposed.

    Returns:
        The configuration the server should run with.

    Raises:
        SecurityError: When the requested combination would put a root panel on
            a network unprotected.
    """
    from wasm.web.auth import SecurityConfig

    # Normalised first, so the exposure decision, the message and the address
    # that is finally bound all talk about the same thing.
    host = _normalize_host(options.host)
    port = options.port
    whitelist = list(options.allow_ip)
    proxies = list(options.trusted_proxy)
    local_only = _is_loopback_host(host)

    if options.require_https and not (options.tls_cert and options.tls_key):
        raise SecurityError(
            "--require-https needs a certificate and a private key",
            details=(
                "Pass --tls-cert and --tls-key. For a public domain: "
                "'certbot certonly --standalone -d panel.example.com' then "
                "--tls-cert /etc/letsencrypt/live/panel.example.com/fullchain.pem "
                "--tls-key /etc/letsencrypt/live/panel.example.com/privkey.pem."
            ),
        )

    if not local_only and not options.require_https and not whitelist:
        unresolved = (
            f"WASM could not resolve {host!r}, so it cannot show that only this machine "
            "would reach the panel, and treats it as exposed.\n"
            if not _host_addresses(host)
            else ""
        )
        raise SecurityError(
            f"Refusing to expose the WASM panel on {host} without TLS or an IP whitelist",
            details=(
                "The panel drives systemd, nginx and certbot as root, so this would put a "
                "root shell on the network in cleartext.\n"
                f"{unresolved}"
                "Pick one:\n"
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
        require_https=options.require_https,
        ssl_certfile=options.tls_cert,
        ssl_keyfile=options.tls_key,
        ip_whitelist=whitelist,
        trusted_proxies=proxies,
    )

    if not local_only:
        # The Host header of a panel reachable by address or by name is not
        # something we can enumerate for the operator.
        config.allowed_hosts = []

    return config


def build_security_config(args: Namespace) -> SecurityConfig:
    """
    Turn parsed argparse arguments into a security configuration.

    Args:
        args: Parsed command line arguments.

    Returns:
        The configuration the server should run with.

    Raises:
        SecurityError: When the requested combination would put a root panel on
            a network unprotected.
    """
    return _build_security_config(_start_options(args))


# ---------------------------------------------------------------------------
# Dependencies
# ---------------------------------------------------------------------------

#: Import name to the packages that provide it, as (system, pip). Only what the
#: panel actually imports belongs here: a dependency check that asks for
#: packages nothing imports sends operators to install software they do not
#: need, and makes a real missing dependency harder to see.
WEB_DEPENDENCIES = {
    "fastapi": ("python3-fastapi", "fastapi>=0.109.0"),
    "uvicorn": ("python3-uvicorn", "uvicorn[standard]>=0.27.0"),
    "jose": ("python3-jose", "python-jose[cryptography]>=3.3.0"),
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
    """
    Suggest how to install the missing packages on this distribution.

    Args:
        missing_apt: Missing system package names.
        missing_pip: Missing pip requirements.

    Returns:
        Command lines to show the operator, pip last as the fallback.
    """
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
    """
    Report whether this interpreter refuses unmanaged installs (PEP 668).

    Returns:
        True when the standard library carries an EXTERNALLY-MANAGED marker.
    """
    import sysconfig

    stdlib_path = sysconfig.get_path("stdlib")
    if stdlib_path:
        marker = Path(stdlib_path) / "EXTERNALLY-MANAGED"
        return marker.exists()
    return False


def _install_with_pip(packages: list[str], verbose: bool = False, force: bool = False) -> bool:
    """
    Install packages with pip.

    Args:
        packages: Pip requirement specifiers.
        verbose: Show verbose output.
        force: Pass --break-system-packages even if the marker is absent.

    Returns:
        True when the installation succeeded.
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
    """
    Install packages with apt.

    Args:
        packages: System package names.
        verbose: Show verbose output.

    Returns:
        True when the installation succeeded.
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
    """
    Offer to install the missing dependencies right now.

    Args:
        missing_apt: Missing system package names.
        missing_pip: Missing pip requirements.
        verbose: Show verbose output.

    Returns:
        True when the operator accepted and the installation succeeded.
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


# ---------------------------------------------------------------------------
# What the commands actually do
# ---------------------------------------------------------------------------


def _start(options: StartOptions, verbose: bool, *, dry_run: bool = False) -> int:
    """
    Start the panel, refusing an unsafe exposure first.

    Args:
        options: How the operator asked for the panel to be exposed.
        verbose: Whether to log verbosely.
        dry_run: Report what would be served instead of serving it.

    Returns:
        Exit code.

    Raises:
        SecurityError: When the requested exposure is not protected.
    """
    logger = Logger(verbose=verbose)

    # Dependencies first: SecurityConfig lives in wasm.web.auth, which imports
    # fastapi, so building the configuration on a host without the panel's
    # packages would answer a missing dependency with an ImportError traceback.
    all_installed, missing_apt, missing_pip = _check_dependencies()
    if not all_installed:
        logger.error("Web dependencies not installed")
        logger.info(f"Missing packages: {', '.join(missing_apt)}")

        # Offer to install automatically. A rehearsal never offers: accepting
        # would install packages, which is exactly what it promised not to do.
        if not dry_run and _prompt_install(missing_apt, missing_pip, verbose):
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

    # Refuses an unsafe exposure before a stale PID file is removed or a socket
    # is bound. Everything this call does is a read, so a rehearsal reaches it
    # too and reports the same refusal a real run would.
    config = _build_security_config(options)
    host = config.host

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
            get_fs().remove(pid_file, missing_ok=True)

    if config.ip_whitelist:
        logger.info(f"Only these clients may connect: {', '.join(config.ip_whitelist)}")
    if config.trusted_proxies:
        logger.info(f"Forwarding headers believed from: {', '.join(config.trusted_proxies)}")
    elif _is_loopback_host(host) and not config.require_https:
        logger.warning(
            "Serving plain HTTP on loopback. Behind a TLS proxy, pass --trusted-proxy "
            "so the session cookie is issued with the Secure flag."
        )

    if dry_run:
        # Serving is neither a subprocess nor a file write, so neither seam
        # would have stopped it: a rehearsal that binds a root panel to a port
        # is the defect this flag exists to prevent.
        scheme = "https" if config.require_https else "http"
        placement = "in the background" if options.daemon else "in the foreground"
        logger.info(f"would serve the panel {placement} at {scheme}://{host}:{config.port}")
        logger.info(f"would record the process in {pid_file}")
        return 0

    if options.daemon:
        return _start_daemon(config, verbose)
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
    fs = get_fs()
    pid_file = get_pid_file()
    fs.make_dir(pid_file.parent)
    fs.write_text(pid_file, str(os.getpid()))

    try:
        run_server(host=config.host, port=config.port, config=config, show_token=True)
        return 0
    finally:
        fs.remove(pid_file, missing_ok=True)


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

    fs = get_fs()

    log_file = Path("/var/log/wasm/web.log")
    if not log_file.parent.exists():
        log_file = Path.home() / ".wasm" / "web.log"
    fs.make_dir(log_file.parent)

    with open(log_file, "a") as log:
        os.dup2(log.fileno(), sys.stdout.fileno())
        os.dup2(log.fileno(), sys.stderr.fileno())

    # Write PID file
    pid_file = get_pid_file()
    fs.make_dir(pid_file.parent)
    fs.write_text(pid_file, str(os.getpid()))

    # Start server
    try:
        from wasm.web.server import run_server

        run_server(host=config.host, port=config.port, config=config, show_token=False)
    finally:
        fs.remove(pid_file, missing_ok=True)

    os._exit(0)


def _stop(verbose: bool, *, dry_run: bool = False) -> int:
    """
    Stop the running panel.

    Args:
        verbose: Whether to log verbosely.
        dry_run: Report the signal instead of sending it.

    Returns:
        Exit code.
    """
    logger = Logger(verbose=verbose)
    fs = get_fs()

    pid_file = get_pid_file()

    if not pid_file.exists():
        logger.info("Web server is not running")
        return 0

    try:
        pid = int(pid_file.read_text().strip())

        if dry_run:
            # A signal is neither a subprocess nor a file write, so nothing
            # below the CLI would have held it back.
            logger.info(f"would send SIGTERM to PID {pid} and remove {pid_file}")
            return 0

        # Send SIGTERM
        os.kill(pid, signal.SIGTERM)
        logger.success(f"Web server stopped (PID: {pid})")

        # Remove PID file
        fs.remove(pid_file, missing_ok=True)

        return 0

    except ProcessLookupError:
        logger.info("Web server is not running (stale PID file removed)")
        fs.remove(pid_file, missing_ok=True)
        return 0
    except ValueError:
        logger.error("Invalid PID file")
        fs.remove(pid_file, missing_ok=True)
        return 1
    except PermissionError:
        logger.error("Permission denied. Try running with sudo.")
        return 1


def _status(verbose: bool) -> int:
    """
    Report whether the panel is running.

    Args:
        verbose: Whether to log verbosely.

    Returns:
        Exit code.
    """
    logger = Logger(verbose=verbose)

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

        # Extra detail is a nicety; psutil may be missing or the process may
        # have exited between the signal and the query.
        try:
            import psutil

            proc = psutil.Process(pid)
            logger.key_value("Memory", f"{proc.memory_info().rss / 1024 / 1024:.1f} MB")
            logger.key_value("Started", str(proc.create_time()))
        except Exception:
            pass

        return 0

    except ProcessLookupError:
        logger.key_value("Status", "not running (stale PID)")
        get_fs().remove(pid_file, missing_ok=True)
        return 0
    except ValueError:
        logger.error("Invalid PID file")
        return 1


def _restart(options: StartOptions, verbose: bool, *, dry_run: bool = False) -> int:
    """
    Stop the panel and start it again with new options.

    Args:
        options: How the operator asked for the panel to be exposed.
        verbose: Whether to log verbosely.
        dry_run: Report both halves instead of performing them.

    Returns:
        Exit code.

    Raises:
        SecurityError: When the requested exposure is not protected.
    """
    logger = Logger(verbose=verbose)

    logger.info("Restarting web server...")

    _stop(verbose, dry_run=dry_run)

    # The old process needs a moment to release the listening socket. There is
    # no socket to wait for when nothing was stopped.
    if not dry_run:
        time.sleep(RESTART_PAUSE)

    return _start(options, verbose, dry_run=dry_run)


def _confirm_token_change(config: SecurityConfig, regenerate: bool) -> None:
    """
    Ask before invalidating credentials that are already in use.

    Nothing is asked when there is nothing to invalidate: the first token and
    the first signing key cost nobody anything.

    Args:
        config: Configuration naming the files that would be rewritten.
        regenerate: Whether the signing key and sessions go too.

    Raises:
        click.Abort: When the operator declines.
    """
    in_use = config.token_file.exists() or (regenerate and config.secret_file.exists())
    if not in_use:
        return

    if regenerate:
        message = (
            f"Rotate the signing key in {config.secret_file} and issue a new access token? "
            "Every open panel session is logged out and the current token stops working"
        )
    else:
        message = (
            f"Replace the access token recorded in {config.token_file}? "
            "The token currently in use stops working immediately"
        )

    click.confirm(message, abort=True)


def _token_status(config: SecurityConfig, logger: Logger) -> int:
    """
    Report what is known about the access token, changing nothing.

    Args:
        config: Configuration naming the files that hold the credentials.
        logger: Logger for the report.

    Returns:
        Exit code.
    """
    logger.header("WASM Web Access Token")

    if not config.token_file.exists():
        logger.key_value("Status", "no token has been issued yet")
        logger.blank()
        logger.info("Issue the first one with: wasm web token --new")
        return 0

    issued = datetime.fromtimestamp(config.token_file.stat().st_mtime)
    logger.key_value("Status", "issued")
    logger.key_value("Token file", str(config.token_file))
    logger.key_value("Issued", issued.isoformat(sep=" ", timespec="seconds"))
    logger.blank()
    logger.info(
        "The token is stored as a salted hash, so it cannot be read back here. "
        "If you have lost it, issue a new one with 'wasm web token --new'; "
        "the token currently in use stops working."
    )
    return 0


def _token(
    regenerate: bool,
    confirm: bool,
    verbose: bool,
    *,
    issue: bool = False,
    dry_run: bool = False,
) -> int:
    """
    Report the state of the access token, or issue a new one on request.

    Showing is the default because that is what operators run this for. The
    stored token is a salted hash and cannot be read back, so issuing a new one
    is the only way to hold a token again - and it invalidates the one in use,
    which is far too much to do to somebody who only typed ``wasm web token``.
    So it takes ``--new`` (or ``--regenerate``) and a confirmation.

    Args:
        regenerate: Also rotate the signing key, revoking every session.
            Implies issuing a new token.
        confirm: Ask before invalidating credentials that are already in use.
        verbose: Whether to log verbosely.
        issue: Issue a new access token, replacing the one in use.
        dry_run: Report what would be replaced instead of replacing it.

    Returns:
        Exit code.

    Raises:
        click.Abort: When confirmation is asked for and declined.
    """
    logger = Logger(verbose=verbose)

    all_installed, missing_apt, missing_pip = _check_dependencies()
    if not all_installed:
        logger.error("Web dependencies not installed")
        logger.info(f"Missing packages: {', '.join(missing_apt)}")
        logger.blank()
        logger.info("Install with one of the following:")
        for instruction in _get_install_instructions(missing_apt, missing_pip):
            logger.info(f"  {instruction}")
        logger.blank()
        logger.info("Or run: wasm web install")
        return 1

    from wasm.web.auth import SecurityConfig, TokenManager

    config = SecurityConfig()

    if not (issue or regenerate):
        return _token_status(config, logger)

    if confirm:
        _confirm_token_change(config, regenerate)

    if dry_run:
        # TokenManager writes the signing key the moment it is constructed, so
        # the rehearsal has to stop before that and not after.
        logger.info(f"would issue a new access token and rewrite {config.token_file}")
        if regenerate:
            logger.info(f"would rotate the signing key in {config.secret_file}")
            logger.info(f"would revoke every session in {config.session_db}")
        return 0

    token_manager = TokenManager(config)

    if regenerate:
        new_token = token_manager.rotate_secrets()
    else:
        new_token = token_manager.generate_master_token()

    logger.success("New access token issued")
    logger.blank()
    print(f"Access Token: {new_token}")
    logger.blank()
    logger.info("Paste it into the login form. It is never accepted in a URL.")

    if regenerate:
        logger.warning("All existing sessions have been revoked")
    else:
        logger.warning("The token issued previously no longer works")

    logger.info("Restart the web server to apply the new token")

    return 0


def _install(use_apt: bool, use_pip: bool, verbose: bool) -> int:
    """
    Install the packages the panel needs.

    Args:
        use_apt: Install system packages.
        use_pip: Install pip requirements.
        verbose: Whether to log verbosely.

    Returns:
        Exit code.
    """
    logger = Logger(verbose=verbose)

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
        if _install_with_apt(missing_apt, verbose):
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
                if _install_with_pip(remaining_pip, verbose):
                    logger.success("Web dependencies installed successfully!")
                    return 0
        logger.error("Failed to install dependencies")
        return 1

    if use_pip:
        logger.info("Installing with pip...")
        if _install_with_pip(missing_pip, verbose):
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


# ---------------------------------------------------------------------------
# The Click command tree
# ---------------------------------------------------------------------------


def _exposure_options(command: F) -> F:
    """
    Attach the options shared by ``web start`` and ``web restart``.

    Both commands bring the panel up, so both have to be able to say the same
    things about how it is exposed. Declaring them once is what keeps the two
    from drifting apart.

    Args:
        command: The command function being decorated.

    Returns:
        The same function, with the exposure options attached.
    """
    options = (
        click.option(
            "-H",
            "--host",
            default="127.0.0.1",
            show_default=True,
            metavar="ADDR",
            help="Interface to bind to. Anything but loopback needs --require-https or --allow-ip.",
        ),
        click.option(
            "-p",
            "--port",
            type=click.INT,
            default=8080,
            show_default=True,
            help="Port to listen on.",
        ),
        click.option("-d", "--daemon", is_flag=True, help="Run in the background."),
        click.option(
            "--require-https",
            is_flag=True,
            help="Serve TLS from the panel and refuse cleartext. Needs --tls-cert and --tls-key.",
        ),
        click.option(
            "--tls-cert",
            type=click.Path(exists=True, dir_okay=False, readable=True),
            metavar="PATH",
            help="Certificate chain to serve.",
        ),
        click.option(
            "--tls-key",
            type=click.Path(exists=True, dir_okay=False, readable=True),
            metavar="PATH",
            help="Private key for the certificate.",
        ),
        click.option(
            "--allow-ip",
            multiple=True,
            metavar="ADDR/CIDR",
            help="Only answer this address or network. Repeat to allow several.",
        ),
        click.option(
            "--trusted-proxy",
            multiple=True,
            metavar="ADDR/CIDR",
            help="Believe forwarding headers from this peer. Declare the proxy that "
            "terminates TLS so the session cookie is issued with Secure.",
        ),
    )
    for option in reversed(options):
        command = option(command)
    return command


@click.group(name="web")
@global_flags
def cli() -> None:
    """
    Run the browser panel for this server.

    The panel acts as root, so it listens on 127.0.0.1 unless you give it TLS
    or a list of addresses allowed to reach it.
    """


@cli.command("start")
@_exposure_options
@global_flags
@pass_context
def start_command(
    ctx: Context,
    host: str,
    port: int,
    daemon: bool,
    require_https: bool,
    tls_cert: str | None,
    tls_key: str | None,
    allow_ip: tuple[str, ...],
    trusted_proxy: tuple[str, ...],
) -> NoReturn:
    """Start the panel and print an access token."""
    options = StartOptions(
        host=host,
        port=port,
        daemon=daemon,
        require_https=require_https,
        tls_cert=tls_cert,
        tls_key=tls_key,
        allow_ip=tuple(allow_ip),
        trusted_proxy=tuple(trusted_proxy),
    )
    _exit(_start(options, ctx.verbose, dry_run=ctx.dry_run))


@cli.command("stop")
@global_flags
@pass_context
def stop_command(ctx: Context) -> NoReturn:
    """Stop the running panel."""
    _exit(_stop(ctx.verbose, dry_run=ctx.dry_run))


@cli.command("status")
@global_flags
@pass_context
def status_command(ctx: Context) -> NoReturn:
    """Show whether the panel is running."""
    _exit(_status(ctx.verbose))


@cli.command("restart")
@_exposure_options
@global_flags
@pass_context
def restart_command(
    ctx: Context,
    host: str,
    port: int,
    daemon: bool,
    require_https: bool,
    tls_cert: str | None,
    tls_key: str | None,
    allow_ip: tuple[str, ...],
    trusted_proxy: tuple[str, ...],
) -> NoReturn:
    """Stop the panel and start it again."""
    options = StartOptions(
        host=host,
        port=port,
        daemon=daemon,
        require_https=require_https,
        tls_cert=tls_cert,
        tls_key=tls_key,
        allow_ip=tuple(allow_ip),
        trusted_proxy=tuple(trusted_proxy),
    )
    _exit(_restart(options, ctx.verbose, dry_run=ctx.dry_run))


@cli.command("token")
@click.option(
    "--new",
    "--rotate",
    "issue",
    is_flag=True,
    help="Issue a new access token. The token currently in use stops working.",
)
@click.option(
    "-r",
    "--regenerate",
    is_flag=True,
    help="Issue a new token and rotate the signing key, logging out every open session.",
)
@click.option("-y", "--yes", "assume_yes", is_flag=True, help="Do not ask for confirmation.")
@global_flags
@pass_context
def token_command(ctx: Context, issue: bool, regenerate: bool, assume_yes: bool) -> NoReturn:
    """
    Show the state of the access token, or issue a new one with --new.

    Showing is the default: issuing a token invalidates the one the panel is
    being used with right now, and nobody types 'wasm web token' meaning that.
    """
    _exit(
        _token(
            regenerate=regenerate,
            confirm=not assume_yes,
            verbose=ctx.verbose,
            issue=issue,
            dry_run=ctx.dry_run,
        )
    )


@cli.command("install")
@click.option("--apt", "use_apt", is_flag=True, help="Install the distribution packages.")
@click.option("--pip", "use_pip", is_flag=True, help="Install into this Python with pip.")
@global_flags
@pass_context
def install_command(ctx: Context, use_apt: bool, use_pip: bool) -> NoReturn:
    """Install the packages the panel needs."""
    if use_apt and use_pip:
        raise click.UsageError("Choose one of --apt or --pip, not both.")
    _exit(_install(use_apt, use_pip, ctx.verbose))


# ---------------------------------------------------------------------------
# Legacy argparse entry point
# ---------------------------------------------------------------------------


def _handle_start(args: Namespace) -> int:
    """
    Handle ``web start`` from the argparse tree.

    Args:
        args: Parsed arguments.

    Returns:
        Exit code.
    """
    return _start(_start_options(args), args.verbose, dry_run=bool(getattr(args, "dry_run", False)))


def _handle_stop(args: Namespace) -> int:
    """
    Handle ``web stop`` from the argparse tree.

    Args:
        args: Parsed arguments.

    Returns:
        Exit code.
    """
    return _stop(args.verbose, dry_run=bool(getattr(args, "dry_run", False)))


def _handle_status(args: Namespace) -> int:
    """
    Handle ``web status`` from the argparse tree.

    Args:
        args: Parsed arguments.

    Returns:
        Exit code.
    """
    return _status(args.verbose)


def _handle_restart(args: Namespace) -> int:
    """
    Handle ``web restart`` from the argparse tree.

    Args:
        args: Parsed arguments.

    Returns:
        Exit code.
    """
    return _restart(
        _start_options(args), args.verbose, dry_run=bool(getattr(args, "dry_run", False))
    )


def _handle_token(args: Namespace) -> int:
    """
    Handle ``web token`` from the argparse tree.

    The default is a report here too. Two front doors that disagree on whether
    a bare ``token`` invalidates the credential in use is worse than either
    behaviour on its own.

    Args:
        args: Parsed arguments.

    Returns:
        Exit code.
    """
    return _token(
        regenerate=bool(getattr(args, "regenerate", False)),
        confirm=False,
        verbose=args.verbose,
        issue=bool(getattr(args, "new", False)),
        dry_run=bool(getattr(args, "dry_run", False)),
    )


def _handle_install(args: Namespace) -> int:
    """
    Handle ``web install`` from the argparse tree.

    Args:
        args: Parsed arguments.

    Returns:
        Exit code.
    """
    return _install(
        bool(getattr(args, "apt", False)),
        bool(getattr(args, "pip", False)),
        args.verbose,
    )


def handle_web(args: Namespace) -> int:
    """
    Route a ``web`` subcommand parsed by the legacy argparse tree.

    Kept until ``wasm.cli.parser`` is deleted; it shares every implementation
    with the Click commands above rather than duplicating them.

    Args:
        args: Parsed arguments.

    Returns:
        Exit code.
    """
    action = args.action

    handlers: dict[str, Callable[[Namespace], int]] = {
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


__all__ = [
    "StartOptions",
    "add_start_arguments",
    "build_security_config",
    "cli",
    "get_pid_file",
    "global_flags",
    "handle_web",
]
