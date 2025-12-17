"""
Argument parser for WASM CLI.
"""

import argparse
from typing import Optional

from wasm import __version__


# Webapp actions that are now top-level commands
WEBAPP_ACTIONS = [
    "create", "new", "deploy",
    "list", "ls",
    "status", "info",
    "restart", "stop", "start",
    "update", "upgrade",
    "delete", "remove", "rm",
    "logs",
]


def create_parser() -> argparse.ArgumentParser:
    """
    Create the main argument parser for WASM.
    
    Returns:
        Configured ArgumentParser instance.
    """
    parser = argparse.ArgumentParser(
        prog="wasm",
        description="WASM - Web App System Management\n"
                   "Deploy, manage, and monitor web applications with ease.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  wasm create -d example.com -s git@github.com:user/app.git -t nextjs
  wasm list
  wasm status example.com
  wasm site list
  wasm service status myapp
  wasm cert create -d example.com

For more information, visit: https://github.com/your-org/wasm
        """,
    )
    
    # Global arguments
    parser.add_argument(
        "--version", "-V",
        action="version",
        version=f"WASM {__version__}",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Enable verbose output",
    )
    parser.add_argument(
        "--interactive", "-i",
        action="store_true",
        help="Run in interactive mode",
    )
    parser.add_argument(
        "--no-color",
        action="store_true",
        help="Disable colored output",
    )
    
    # Subparsers for commands
    subparsers = parser.add_subparsers(
        dest="command",
        title="commands",
        description="Available commands",
        metavar="<command>",
    )
    
    # Webapp commands (top-level, default behavior)
    _add_webapp_commands(subparsers)
    
    # Site commands
    _add_site_parser(subparsers)
    
    # Service commands
    _add_service_parser(subparsers)
    
    # Cert commands
    _add_cert_parser(subparsers)
    
    # Monitor commands
    _add_monitor_parser(subparsers)
    
    # Setup commands
    _add_setup_parser(subparsers)
    
    return parser


def _add_webapp_commands(subparsers) -> None:
    """Add webapp commands as top-level commands."""
    
    # create (aliases: new, deploy)
    create = subparsers.add_parser(
        "create",
        aliases=["new", "deploy"],
        help="Deploy a new web application",
    )
    create.add_argument(
        "--domain", "-d",
        required=True,
        help="Target domain name",
    )
    create.add_argument(
        "--source", "-s",
        required=True,
        help="Source (Git URL or local path)",
    )
    create.add_argument(
        "--type", "-t",
        choices=["nextjs", "nodejs", "vite", "python", "static", "auto"],
        default="auto",
        help="Application type (default: auto-detect)",
    )
    create.add_argument(
        "--port", "-p",
        type=int,
        help="Application port",
    )
    create.add_argument(
        "--webserver", "-w",
        choices=["nginx", "apache"],
        default="nginx",
        help="Web server to use (default: nginx)",
    )
    create.add_argument(
        "--branch", "-b",
        help="Git branch to deploy",
    )
    create.add_argument(
        "--no-ssl",
        action="store_true",
        help="Skip SSL certificate configuration",
    )
    create.add_argument(
        "--env-file",
        help="Path to environment file",
    )
    create.add_argument(
        "--package-manager", "--pm",
        choices=["npm", "pnpm", "bun", "auto"],
        default="auto",
        help="Package manager to use (default: auto-detect)",
    )
    
    # list (alias: ls)
    subparsers.add_parser(
        "list",
        aliases=["ls"],
        help="List deployed applications",
    )
    
    # status (alias: info)
    status = subparsers.add_parser(
        "status",
        aliases=["info"],
        help="Show application status",
    )
    status.add_argument(
        "domain",
        help="Application domain",
    )
    
    # restart
    restart = subparsers.add_parser(
        "restart",
        help="Restart an application",
    )
    restart.add_argument(
        "domain",
        help="Application domain",
    )
    
    # stop
    stop = subparsers.add_parser(
        "stop",
        help="Stop an application",
    )
    stop.add_argument(
        "domain",
        help="Application domain",
    )
    
    # start
    start = subparsers.add_parser(
        "start",
        help="Start an application",
    )
    start.add_argument(
        "domain",
        help="Application domain",
    )
    
    # update (alias: upgrade)
    update = subparsers.add_parser(
        "update",
        aliases=["upgrade"],
        help="Update an application (pull and rebuild)",
    )
    update.add_argument(
        "domain",
        help="Application domain",
    )
    update.add_argument(
        "--source", "-s",
        help="New source URL (optional, uses original if not specified)",
    )
    update.add_argument(
        "--branch", "-b",
        help="Git branch to update from",
    )
    update.add_argument(
        "--package-manager", "--pm",
        choices=["npm", "pnpm", "bun", "auto"],
        default="auto",
        help="Package manager to use (default: auto-detect)",
    )
    
    # delete (aliases: remove, rm)
    delete = subparsers.add_parser(
        "delete",
        aliases=["remove", "rm"],
        help="Delete an application",
    )
    delete.add_argument(
        "domain",
        help="Application domain",
    )
    delete.add_argument(
        "--force", "-f",
        action="store_true",
        help="Skip confirmation",
    )
    delete.add_argument(
        "--keep-files",
        action="store_true",
        help="Keep application files",
    )
    
    # logs
    logs = subparsers.add_parser(
        "logs",
        help="View application logs",
    )
    logs.add_argument(
        "domain",
        help="Application domain",
    )
    logs.add_argument(
        "--follow", "-f",
        action="store_true",
        help="Follow log output",
    )
    logs.add_argument(
        "--lines", "-n",
        type=int,
        default=50,
        help="Number of lines to show",
    )


def _add_site_parser(subparsers) -> None:
    """Add site subcommands."""
    site = subparsers.add_parser(
        "site",
        help="Manage web server sites",
        description="Manage Nginx/Apache virtual hosts",
    )
    
    site_sub = site.add_subparsers(
        dest="action",
        title="actions",
        metavar="<action>",
    )
    
    # site create
    create = site_sub.add_parser(
        "create",
        help="Create a new site configuration",
    )
    create.add_argument(
        "--domain", "-d",
        required=True,
        help="Domain name",
    )
    create.add_argument(
        "--webserver", "-w",
        choices=["nginx", "apache"],
        default="nginx",
        help="Web server",
    )
    create.add_argument(
        "--template", "-t",
        default="proxy",
        help="Configuration template",
    )
    create.add_argument(
        "--port", "-p",
        type=int,
        default=3000,
        help="Backend port for proxy",
    )
    
    # site list
    list_cmd = site_sub.add_parser(
        "list",
        aliases=["ls"],
        help="List all sites",
    )
    list_cmd.add_argument(
        "--webserver", "-w",
        choices=["nginx", "apache", "all"],
        default="all",
        help="Filter by web server",
    )
    
    # site enable
    enable = site_sub.add_parser(
        "enable",
        help="Enable a site",
    )
    enable.add_argument(
        "domain",
        help="Domain name",
    )
    
    # site disable
    disable = site_sub.add_parser(
        "disable",
        help="Disable a site",
    )
    disable.add_argument(
        "domain",
        help="Domain name",
    )
    
    # site delete
    delete = site_sub.add_parser(
        "delete",
        aliases=["remove", "rm"],
        help="Delete a site configuration",
    )
    delete.add_argument(
        "domain",
        help="Domain name",
    )
    delete.add_argument(
        "--force", "-f",
        action="store_true",
        help="Skip confirmation",
    )
    
    # site show
    show = site_sub.add_parser(
        "show",
        aliases=["cat"],
        help="Show site configuration",
    )
    show.add_argument(
        "domain",
        help="Domain name",
    )


def _add_service_parser(subparsers) -> None:
    """Add service subcommands."""
    service = subparsers.add_parser(
        "service",
        aliases=["svc"],
        help="Manage systemd services",
        description="Manage systemd services",
    )
    
    service_sub = service.add_subparsers(
        dest="action",
        title="actions",
        metavar="<action>",
    )
    
    # service create
    create = service_sub.add_parser(
        "create",
        help="Create a new service",
    )
    create.add_argument(
        "--name", "-n",
        required=True,
        help="Service name",
    )
    create.add_argument(
        "--command", "-c",
        required=True,
        help="Command to execute",
    )
    create.add_argument(
        "--directory", "-d",
        required=True,
        help="Working directory",
    )
    create.add_argument(
        "--user", "-u",
        default="www-data",
        help="User to run as",
    )
    create.add_argument(
        "--description",
        help="Service description",
    )
    
    # service list
    list_cmd = service_sub.add_parser(
        "list",
        aliases=["ls"],
        help="List managed services",
    )
    list_cmd.add_argument(
        "--all", "-a",
        action="store_true",
        help="Show all system services",
    )
    
    # service status
    status = service_sub.add_parser(
        "status",
        aliases=["info"],
        help="Show service status",
    )
    status.add_argument(
        "name",
        help="Service name",
    )
    
    # service start
    start = service_sub.add_parser(
        "start",
        help="Start a service",
    )
    start.add_argument(
        "name",
        help="Service name",
    )
    
    # service stop
    stop = service_sub.add_parser(
        "stop",
        help="Stop a service",
    )
    stop.add_argument(
        "name",
        help="Service name",
    )
    
    # service restart
    restart = service_sub.add_parser(
        "restart",
        help="Restart a service",
    )
    restart.add_argument(
        "name",
        help="Service name",
    )
    
    # service logs
    logs = service_sub.add_parser(
        "logs",
        help="View service logs",
    )
    logs.add_argument(
        "name",
        help="Service name",
    )
    logs.add_argument(
        "--follow", "-f",
        action="store_true",
        help="Follow log output",
    )
    logs.add_argument(
        "--lines", "-n",
        type=int,
        default=50,
        help="Number of lines to show",
    )
    
    # service delete
    delete = service_sub.add_parser(
        "delete",
        aliases=["remove", "rm"],
        help="Delete a service",
    )
    delete.add_argument(
        "name",
        help="Service name",
    )
    delete.add_argument(
        "--force", "-f",
        action="store_true",
        help="Skip confirmation",
    )


def _add_cert_parser(subparsers) -> None:
    """Add cert subcommands."""
    cert = subparsers.add_parser(
        "cert",
        aliases=["ssl", "certificate"],
        help="Manage SSL certificates",
        description="Manage Let's Encrypt SSL certificates",
    )
    
    cert_sub = cert.add_subparsers(
        dest="action",
        title="actions",
        metavar="<action>",
    )
    
    # cert create
    create = cert_sub.add_parser(
        "create",
        aliases=["obtain", "new"],
        help="Obtain a new certificate",
    )
    create.add_argument(
        "--domain", "-d",
        required=True,
        action="append",
        help="Domain name (can be specified multiple times)",
    )
    create.add_argument(
        "--email", "-e",
        help="Email for registration",
    )
    create.add_argument(
        "--webroot", "-w",
        help="Webroot path",
    )
    create.add_argument(
        "--standalone",
        action="store_true",
        help="Use standalone mode",
    )
    create.add_argument(
        "--nginx",
        action="store_true",
        help="Use Nginx plugin",
    )
    create.add_argument(
        "--apache",
        action="store_true",
        help="Use Apache plugin",
    )
    create.add_argument(
        "--dry-run",
        action="store_true",
        help="Test without obtaining",
    )
    
    # cert list
    cert_sub.add_parser(
        "list",
        aliases=["ls"],
        help="List all certificates",
    )
    
    # cert info
    info = cert_sub.add_parser(
        "info",
        aliases=["show"],
        help="Show certificate info",
    )
    info.add_argument(
        "domain",
        help="Domain name",
    )
    
    # cert renew
    renew = cert_sub.add_parser(
        "renew",
        help="Renew certificates",
    )
    renew.add_argument(
        "--domain", "-d",
        help="Specific domain to renew",
    )
    renew.add_argument(
        "--force",
        action="store_true",
        help="Force renewal",
    )
    renew.add_argument(
        "--dry-run",
        action="store_true",
        help="Test without renewing",
    )
    
    # cert revoke
    revoke = cert_sub.add_parser(
        "revoke",
        help="Revoke a certificate",
    )
    revoke.add_argument(
        "domain",
        help="Domain name",
    )
    revoke.add_argument(
        "--delete",
        action="store_true",
        default=True,
        help="Delete after revoking",
    )
    
    # cert delete
    delete = cert_sub.add_parser(
        "delete",
        aliases=["remove", "rm"],
        help="Delete a certificate",
    )
    delete.add_argument(
        "domain",
        help="Domain name",
    )
    delete.add_argument(
        "--force", "-f",
        action="store_true",
        help="Skip confirmation",
    )


def _add_monitor_parser(subparsers) -> None:
    """Add monitor subcommands."""
    monitor = subparsers.add_parser(
        "monitor",
        aliases=["mon"],
        help="AI-powered process security monitoring",
        description="Monitor system processes for suspicious activity using AI analysis",
    )
    
    monitor_sub = monitor.add_subparsers(
        dest="action",
        title="actions",
        metavar="<action>",
    )
    
    # monitor status
    monitor_sub.add_parser(
        "status",
        aliases=["info"],
        help="Show monitor service status",
    )
    
    # monitor scan
    scan = monitor_sub.add_parser(
        "scan",
        help="Run a single security scan",
    )
    scan.add_argument(
        "--dry-run",
        action="store_true",
        help="Don't terminate processes, just report",
    )
    
    # monitor run
    monitor_sub.add_parser(
        "run",
        help="Run monitor continuously (foreground)",
    )
    
    # monitor enable (main command - installs if needed)
    monitor_sub.add_parser(
        "enable",
        help="Enable monitor (installs dependencies and service if needed)",
    )
    
    # monitor install (optional - just installs without enabling)
    monitor_sub.add_parser(
        "install",
        help="Install monitor service only (without enabling)",
    )
    
    # monitor disable
    monitor_sub.add_parser(
        "disable",
        help="Disable and stop monitor service",
    )
    
    # monitor uninstall
    monitor_sub.add_parser(
        "uninstall",
        help="Uninstall monitor service",
    )
    
    # monitor test-email
    monitor_sub.add_parser(
        "test-email",
        help="Send a test email to verify notification settings",
    )
    
    # monitor config
    monitor_sub.add_parser(
        "config",
        help="Show current monitor configuration",
    )


def _add_setup_parser(subparsers) -> None:
    """Add setup subcommands."""
    setup = subparsers.add_parser(
        "setup",
        help="Initial setup and configuration",
        description="Setup WASM directories, permissions, and shell completions",
    )
    
    setup_sub = setup.add_subparsers(
        dest="action",
        title="actions",
        metavar="<action>",
    )
    
    # setup init
    setup_sub.add_parser(
        "init",
        help="Initialize WASM directories and configuration (requires sudo)",
    )
    
    # setup completions
    completions = setup_sub.add_parser(
        "completions",
        help="Install shell completions",
    )
    completions.add_argument(
        "--shell", "-s",
        choices=["bash", "zsh", "fish"],
        help="Shell to install completions for (auto-detected if not specified)",
    )
    completions.add_argument(
        "--user-only", "-u",
        action="store_true",
        help="Install for current user only (no sudo required)",
    )
    
    # setup permissions
    setup_sub.add_parser(
        "permissions",
        help="Check and display permission status",
    )
    
    # setup ssh
    ssh_parser = setup_sub.add_parser(
        "ssh",
        help="Setup SSH key for Git authentication",
        description="Generate SSH keys and display instructions for adding to Git providers",
    )
    ssh_parser.add_argument(
        "--generate", "-g",
        action="store_true",
        help="Generate a new SSH key if none exists",
    )
    ssh_parser.add_argument(
        "--type", "-t",
        choices=["ed25519", "rsa", "ecdsa"],
        default="ed25519",
        dest="key_type",
        help="Type of SSH key to generate (default: ed25519)",
    )
    ssh_parser.add_argument(
        "--test", "-T",
        dest="test_host",
        metavar="HOST",
        help="Test SSH connection to a host (e.g., github.com)",
    )
    ssh_parser.add_argument(
        "--show", "-S",
        action="store_true",
        help="Show the public key",
    )
    
    # setup doctor
    setup_sub.add_parser(
        "doctor",
        help="Run system diagnostics and check for issues",
        description="Comprehensive check of all dependencies and configurations",
    )


def parse_args(args: Optional[list] = None) -> argparse.Namespace:
    """
    Parse command line arguments.
    
    Args:
        args: Arguments to parse (defaults to sys.argv).
        
    Returns:
        Parsed arguments namespace.
    """
    parser = create_parser()
    return parser.parse_args(args)
