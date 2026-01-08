# Copyright (c) 2024-2025 Yago López Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

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

For more information, visit: https://github.com/Perkybeet/wasm
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
    
    # Backup commands
    _add_backup_parser(subparsers)
    
    # Rollback command
    _add_rollback_parser(subparsers)
    
    # Database commands
    _add_db_parser(subparsers)
    
    # Store commands
    _add_store_parser(subparsers)
    
    # Web interface commands
    _add_web_parser(subparsers)
    
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
        "--force", "-f", "-y",
        action="store_true",
        help="Skip confirmation (yes to all)",
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
        "--force", "-f", "-y",
        action="store_true",
        help="Skip confirmation (yes to all)",
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
        dest="exec_command",
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
        "--force", "-f", "-y",
        action="store_true",
        help="Skip confirmation (yes to all)",
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
    scan.add_argument(
        "--force-ai",
        action="store_true",
        help="Force AI analysis even if no suspicious processes are found",
    )
    scan.add_argument(
        "--all",
        action="store_true",
        help="Analyze ALL processes with AI (expensive, use sparingly)",
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


def _add_backup_parser(subparsers) -> None:
    """Add backup subcommands."""
    backup = subparsers.add_parser(
        "backup",
        aliases=["bak"],
        help="Manage application backups",
        description="Create, list, restore, and manage application backups",
    )
    
    backup_sub = backup.add_subparsers(
        dest="action",
        title="actions",
        metavar="<action>",
    )
    
    # backup create
    create = backup_sub.add_parser(
        "create",
        aliases=["new"],
        help="Create a backup of an application",
    )
    create.add_argument(
        "domain",
        help="Domain name of the application to backup",
    )
    create.add_argument(
        "--description", "-m",
        default="",
        help="Description or note for this backup",
    )
    create.add_argument(
        "--no-env",
        action="store_true",
        help="Exclude .env files from backup",
    )
    create.add_argument(
        "--include-node-modules",
        action="store_true",
        help="Include node_modules (warning: large!)",
    )
    create.add_argument(
        "--include-build",
        action="store_true",
        help="Include build artifacts (.next, dist, build)",
    )
    create.add_argument(
        "--tags", "-t",
        help="Comma-separated tags for the backup",
    )
    
    # backup list
    list_cmd = backup_sub.add_parser(
        "list",
        aliases=["ls"],
        help="List backups",
    )
    list_cmd.add_argument(
        "domain",
        nargs="?",
        help="Filter by domain (optional)",
    )
    list_cmd.add_argument(
        "--tags", "-t",
        help="Filter by tags (comma-separated)",
    )
    list_cmd.add_argument(
        "--limit", "-n",
        type=int,
        help="Maximum number of backups to show",
    )
    list_cmd.add_argument(
        "--json",
        action="store_true",
        help="Output in JSON format",
    )
    
    # backup restore
    restore = backup_sub.add_parser(
        "restore",
        help="Restore from a backup",
    )
    restore.add_argument(
        "backup_id",
        help="Backup ID to restore",
    )
    restore.add_argument(
        "--target-domain",
        help="Restore to a different domain (optional)",
    )
    restore.add_argument(
        "--no-env",
        action="store_true",
        help="Don't restore .env files (keep current)",
    )
    restore.add_argument(
        "--no-verify",
        action="store_true",
        help="Skip checksum verification",
    )
    restore.add_argument(
        "--force", "-f",
        action="store_true",
        help="Skip confirmation prompt",
    )
    
    # backup delete
    delete = backup_sub.add_parser(
        "delete",
        aliases=["remove", "rm"],
        help="Delete a backup",
    )
    delete.add_argument(
        "backup_id",
        help="Backup ID to delete",
    )
    delete.add_argument(
        "--force", "-f", "-y",
        action="store_true",
        help="Skip confirmation (yes to all)",
    )
    
    # backup verify
    verify = backup_sub.add_parser(
        "verify",
        aliases=["check"],
        help="Verify a backup's integrity",
    )
    verify.add_argument(
        "backup_id",
        help="Backup ID to verify",
    )
    
    # backup info
    info = backup_sub.add_parser(
        "info",
        aliases=["show"],
        help="Show detailed backup information",
    )
    info.add_argument(
        "backup_id",
        help="Backup ID",
    )
    info.add_argument(
        "--json",
        action="store_true",
        help="Output in JSON format",
    )
    
    # backup storage
    storage = backup_sub.add_parser(
        "storage",
        help="Show backup storage usage",
    )
    storage.add_argument(
        "--json",
        action="store_true",
        help="Output in JSON format",
    )


def _add_rollback_parser(subparsers) -> None:
    """Add rollback command."""
    rollback = subparsers.add_parser(
        "rollback",
        aliases=["rb"],
        help="Rollback an application to a previous state",
        description="Quick rollback to the most recent backup or a specific backup",
    )
    rollback.add_argument(
        "domain",
        help="Domain name of the application to rollback",
    )
    rollback.add_argument(
        "backup_id",
        nargs="?",
        help="Specific backup ID (defaults to most recent)",
    )
    rollback.add_argument(
        "--no-rebuild",
        action="store_true",
        help="Don't rebuild after restore",
    )


def _add_db_parser(subparsers) -> None:
    """Add database management commands."""
    db = subparsers.add_parser(
        "db",
        aliases=["database"],
        help="Database management",
        description="Install, manage, and configure database engines (MySQL, PostgreSQL, Redis, MongoDB)",
    )
    
    db_sub = db.add_subparsers(
        dest="action",
        title="actions",
        description="Database actions",
        metavar="<action>",
    )
    
    # Common arguments for engine
    engine_help = "Database engine (mysql, postgresql, redis, mongodb)"
    
    # ==================== Engine Management ====================
    
    # db install
    install = db_sub.add_parser(
        "install",
        help="Install a database engine",
    )
    install.add_argument(
        "engine",
        help=engine_help,
    )
    
    # db uninstall
    uninstall = db_sub.add_parser(
        "uninstall",
        help="Uninstall a database engine",
    )
    uninstall.add_argument(
        "engine",
        help=engine_help,
    )
    uninstall.add_argument(
        "--purge",
        action="store_true",
        help="Remove all data and configuration",
    )
    uninstall.add_argument(
        "--force", "-f", "-y",
        action="store_true",
        help="Skip confirmation",
    )
    
    # db status
    status = db_sub.add_parser(
        "status",
        help="Show database engine status",
    )
    status.add_argument(
        "engine",
        nargs="?",
        help=f"{engine_help} (omit to show all)",
    )
    status.add_argument(
        "--json",
        action="store_true",
        help="Output in JSON format",
    )
    
    # db start
    start = db_sub.add_parser(
        "start",
        help="Start a database engine",
    )
    start.add_argument(
        "engine",
        help=engine_help,
    )
    
    # db stop
    stop = db_sub.add_parser(
        "stop",
        help="Stop a database engine",
    )
    stop.add_argument(
        "engine",
        help=engine_help,
    )
    
    # db restart
    restart = db_sub.add_parser(
        "restart",
        help="Restart a database engine",
    )
    restart.add_argument(
        "engine",
        help=engine_help,
    )
    
    # db engines
    engines = db_sub.add_parser(
        "engines",
        help="List available database engines",
    )
    engines.add_argument(
        "--json",
        action="store_true",
        help="Output in JSON format",
    )
    
    # ==================== Database Management ====================
    
    # db create
    create = db_sub.add_parser(
        "create",
        help="Create a new database",
    )
    create.add_argument(
        "name",
        help="Database name",
    )
    create.add_argument(
        "--engine", "-e",
        required=True,
        help=engine_help,
    )
    create.add_argument(
        "--owner", "-o",
        help="Database owner (user)",
    )
    create.add_argument(
        "--encoding",
        help="Character encoding (default: UTF8)",
    )
    
    # db drop
    drop = db_sub.add_parser(
        "drop",
        help="Drop a database",
    )
    drop.add_argument(
        "name",
        help="Database name",
    )
    drop.add_argument(
        "--engine", "-e",
        required=True,
        help=engine_help,
    )
    drop.add_argument(
        "--force", "-f", "-y",
        action="store_true",
        help="Skip confirmation",
    )
    
    # db list
    list_cmd = db_sub.add_parser(
        "list",
        aliases=["ls"],
        help="List databases",
    )
    list_cmd.add_argument(
        "--engine", "-e",
        help=f"{engine_help} (omit to list all)",
    )
    list_cmd.add_argument(
        "--json",
        action="store_true",
        help="Output in JSON format",
    )
    
    # db info
    info = db_sub.add_parser(
        "info",
        help="Show database information",
    )
    info.add_argument(
        "name",
        help="Database name",
    )
    info.add_argument(
        "--engine", "-e",
        required=True,
        help=engine_help,
    )
    info.add_argument(
        "--json",
        action="store_true",
        help="Output in JSON format",
    )
    
    # ==================== User Management ====================
    
    # db user-create
    user_create = db_sub.add_parser(
        "user-create",
        help="Create a database user",
    )
    user_create.add_argument(
        "username",
        help="Username",
    )
    user_create.add_argument(
        "--engine", "-e",
        required=True,
        help=engine_help,
    )
    user_create.add_argument(
        "--password", "-p",
        help="Password (generated if not provided)",
    )
    user_create.add_argument(
        "--database", "-d",
        help="Grant access to this database",
    )
    user_create.add_argument(
        "--host",
        default="localhost",
        help="Host restriction (default: localhost)",
    )
    
    # db user-delete
    user_delete = db_sub.add_parser(
        "user-delete",
        help="Delete a database user",
    )
    user_delete.add_argument(
        "username",
        help="Username",
    )
    user_delete.add_argument(
        "--engine", "-e",
        required=True,
        help=engine_help,
    )
    user_delete.add_argument(
        "--host",
        default="localhost",
        help="Host restriction",
    )
    user_delete.add_argument(
        "--force", "-f", "-y",
        action="store_true",
        help="Skip confirmation",
    )
    
    # db user-list
    user_list = db_sub.add_parser(
        "user-list",
        help="List database users",
    )
    user_list.add_argument(
        "--engine", "-e",
        required=True,
        help=engine_help,
    )
    user_list.add_argument(
        "--json",
        action="store_true",
        help="Output in JSON format",
    )
    
    # db grant
    grant = db_sub.add_parser(
        "grant",
        help="Grant privileges to a user",
    )
    grant.add_argument(
        "username",
        help="Username",
    )
    grant.add_argument(
        "database",
        help="Database name",
    )
    grant.add_argument(
        "--engine", "-e",
        required=True,
        help=engine_help,
    )
    grant.add_argument(
        "--privileges",
        help="Comma-separated list of privileges (default: ALL)",
    )
    grant.add_argument(
        "--host",
        default="localhost",
        help="Host restriction",
    )
    
    # db revoke
    revoke = db_sub.add_parser(
        "revoke",
        help="Revoke privileges from a user",
    )
    revoke.add_argument(
        "username",
        help="Username",
    )
    revoke.add_argument(
        "database",
        help="Database name",
    )
    revoke.add_argument(
        "--engine", "-e",
        required=True,
        help=engine_help,
    )
    revoke.add_argument(
        "--privileges",
        help="Comma-separated list of privileges (default: ALL)",
    )
    revoke.add_argument(
        "--host",
        default="localhost",
        help="Host restriction",
    )
    
    # ==================== Backup & Restore ====================
    
    # db backup
    backup = db_sub.add_parser(
        "backup",
        help="Backup a database",
    )
    backup.add_argument(
        "database",
        help="Database name",
    )
    backup.add_argument(
        "--engine", "-e",
        required=True,
        help=engine_help,
    )
    backup.add_argument(
        "--output", "-o",
        help="Output file path",
    )
    backup.add_argument(
        "--no-compress",
        action="store_true",
        help="Don't compress the backup",
    )
    
    # db restore
    restore = db_sub.add_parser(
        "restore",
        help="Restore a database from backup",
    )
    restore.add_argument(
        "database",
        help="Database name",
    )
    restore.add_argument(
        "file",
        help="Backup file path",
    )
    restore.add_argument(
        "--engine", "-e",
        required=True,
        help=engine_help,
    )
    restore.add_argument(
        "--drop",
        action="store_true",
        help="Drop existing database before restore",
    )
    restore.add_argument(
        "--force", "-f", "-y",
        action="store_true",
        help="Skip confirmation",
    )
    
    # db backups
    backups = db_sub.add_parser(
        "backups",
        help="List available backups",
    )
    backups.add_argument(
        "--engine", "-e",
        help=engine_help,
    )
    backups.add_argument(
        "--database", "-d",
        help="Filter by database name",
    )
    backups.add_argument(
        "--json",
        action="store_true",
        help="Output in JSON format",
    )
    
    # ==================== Query & Connection ====================
    
    # db query
    query = db_sub.add_parser(
        "query",
        help="Execute a query",
    )
    query.add_argument(
        "database",
        help="Database name",
    )
    query.add_argument(
        "query",
        help="Query to execute",
    )
    query.add_argument(
        "--engine", "-e",
        required=True,
        help=engine_help,
    )
    
    # db connect
    connect = db_sub.add_parser(
        "connect",
        help="Connect to a database interactively",
    )
    connect.add_argument(
        "--engine", "-e",
        required=True,
        help=engine_help,
    )
    connect.add_argument(
        "--database", "-d",
        help="Database name",
    )
    connect.add_argument(
        "--username", "-u",
        help="Username",
    )
    
    # db connection-string
    conn_string = db_sub.add_parser(
        "connection-string",
        help="Generate a connection string",
    )
    conn_string.add_argument(
        "database",
        help="Database name",
    )
    conn_string.add_argument(
        "username",
        help="Username",
    )
    conn_string.add_argument(
        "--engine", "-e",
        required=True,
        help=engine_help,
    )
    conn_string.add_argument(
        "--password", "-p",
        help="Password (shown as placeholder if not provided)",
    )
    conn_string.add_argument(
        "--host",
        default="localhost",
        help="Host (default: localhost)",
    )

    # db config
    config = db_sub.add_parser(
        "config",
        help="Configure database engine credentials",
    )
    config.add_argument(
        "--engine", "-e",
        required=True,
        help=engine_help,
    )
    config.add_argument(
        "--user", "-u",
        help="Admin username (e.g. root)",
    )
    config.add_argument(
        "--password", "-p",
        help="Admin password",
    )


def _add_web_parser(subparsers) -> None:
    """Add web interface commands."""
    web = subparsers.add_parser(
        "web",
        help="Web dashboard interface",
        description="Start, stop, and manage the WASM web dashboard",
    )
    
    web_sub = web.add_subparsers(
        dest="action",
        title="actions",
        description="Web interface actions",
        metavar="<action>",
    )
    
    # web start
    start = web_sub.add_parser(
        "start",
        help="Start the web dashboard server",
    )
    start.add_argument(
        "--host", "-H",
        default="127.0.0.1",
        help="Host to bind to (default: 127.0.0.1, use 0.0.0.0 for all interfaces)",
    )
    start.add_argument(
        "--port", "-p",
        type=int,
        default=8080,
        help="Port to listen on (default: 8080)",
    )
    start.add_argument(
        "--daemon", "-d",
        action="store_true",
        help="Run in background as daemon",
    )
    
    # web stop
    web_sub.add_parser(
        "stop",
        help="Stop the web dashboard server",
    )
    
    # web status
    web_sub.add_parser(
        "status",
        help="Show web dashboard status",
    )
    
    # web restart
    restart = web_sub.add_parser(
        "restart",
        help="Restart the web dashboard server",
    )
    restart.add_argument(
        "--host", "-H",
        default="127.0.0.1",
        help="Host to bind to",
    )
    restart.add_argument(
        "--port", "-p",
        type=int,
        default=8080,
        help="Port to listen on",
    )
    restart.add_argument(
        "--daemon", "-d",
        action="store_true",
        help="Run in background as daemon",
    )
    
    # web token
    token = web_sub.add_parser(
        "token",
        help="Manage access tokens",
    )
    token.add_argument(
        "--regenerate", "-r",
        action="store_true",
        help="Generate a new access token (revokes all sessions)",
    )
    
    # web install
    install = web_sub.add_parser(
        "install",
        help="Install web dashboard dependencies",
    )
    install.add_argument(
        "--apt",
        action="store_true",
        help="Use apt to install system packages (Debian/Ubuntu)",
    )
    install.add_argument(
        "--pip",
        action="store_true",
        help="Use pip to install user packages",
    )


def _add_store_parser(subparsers) -> None:
    """Add store management commands."""
    store = subparsers.add_parser(
        "store",
        help="Manage WASM persistence store",
        description="Initialize, manage, and query the SQLite persistence store",
    )
    
    store_sub = store.add_subparsers(
        dest="action",
        title="actions",
        description="Store actions",
        metavar="<action>",
    )
    
    # store init
    store_sub.add_parser(
        "init",
        help="Initialize or reinitialize the store database",
    )
    
    # store stats
    stats = store_sub.add_parser(
        "stats",
        help="Show store statistics",
    )
    stats.add_argument(
        "--json",
        action="store_true",
        help="Output as JSON",
    )
    
    # store import
    store_sub.add_parser(
        "import",
        help="Import legacy apps from systemd services and nginx configs",
    )
    
    # store export
    export = store_sub.add_parser(
        "export",
        help="Export store data to JSON",
    )
    export.add_argument(
        "--output", "-o",
        help="Output file (stdout if not specified)",
    )
    
    # store sync
    store_sub.add_parser(
        "sync",
        help="Sync store with actual systemd service states",
    )
    
    # store path
    store_sub.add_parser(
        "path",
        help="Show the database file path",
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
