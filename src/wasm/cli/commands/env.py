# Copyright (c) 2024-2025 Yago López Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
Environment variable CLI commands for WASM.

Provides CLI handlers for managing application environment variables:
- Interactive configuration from .env.example templates
- Viewing current values with secret masking
- Exporting variables to files
"""

import sys
from argparse import Namespace
from pathlib import Path

from wasm.core.config import Config
from wasm.core.logger import Logger
from wasm.core.exceptions import WASMError
from wasm.core.utils import domain_to_app_name
from wasm.deployers.helpers.env_manager import EnvManager, EnvConfig


def handle_env(args: Namespace) -> int:
    """
    Handle wasm env <action> commands.

    Routes to the appropriate sub-handler based on the action.

    Args:
        args: Parsed command-line arguments.

    Returns:
        Exit code (0 for success, non-zero for error).
    """
    action = getattr(args, "action", None)
    verbose = getattr(args, "verbose", False)

    if not action:
        print("Error: env requires an action", file=sys.stderr)
        print("Use: wasm env --help", file=sys.stderr)
        return 1

    handlers = {
        "configure": _env_configure,
        "config": _env_configure,
        "setup": _env_configure,
        "show": _env_show,
        "list": _env_show,
        "ls": _env_show,
        "export": _env_export,
    }

    handler = handlers.get(action)
    if not handler:
        print(f"Unknown env action: {action}", file=sys.stderr)
        return 1

    try:
        return handler(args, verbose)
    except WASMError as e:
        logger = Logger(verbose=verbose)
        logger.error(e.message)
        if e.details:
            print(f"  {e.details}")
        return 1


def _env_configure(args: Namespace, verbose: bool) -> int:
    """
    Run interactive environment configuration.

    Discovers variables from .env.example files, prompts the user
    for values, auto-generates secrets, and writes .env files.

    Args:
        args: Parsed arguments (requires domain).
        verbose: Whether to enable verbose output.

    Returns:
        Exit code.
    """
    logger = Logger(verbose=verbose)
    config = Config()

    domain = getattr(args, "domain", None)
    if not domain:
        logger.error("Domain is required")
        return 1

    app_name = domain_to_app_name(domain)
    app_path = config.apps_directory / app_name

    if not app_path.exists():
        logger.error(f"Application not found: {domain}")
        return 1

    manager = EnvManager(verbose=verbose)

    logger.header(f"Environment Configuration: {domain}")

    # Discover variables
    variables = manager.discover(app_path)
    if not variables:
        logger.info("No .env.example files found")
        return 0

    logger.info(f"Found {len(variables)} variables")

    # Load existing values
    existing = manager.get_current_values(app_path)

    # Prompt for values
    values = manager.prompt_variables(variables, existing)

    # Write .env file
    written = manager.write_env_files(app_path, values)
    for path in written:
        logger.success(f"Written: {path}")

    # Save config
    env_config = EnvConfig(variables=variables)
    manager.save_config(app_path, env_config)

    return 0


def _env_show(args: Namespace, verbose: bool) -> int:
    """
    Display current environment variables.

    Shows all variables from the application's .env file, with
    secret values masked by default.

    Args:
        args: Parsed arguments (requires domain, optional --unmask).
        verbose: Whether to enable verbose output.

    Returns:
        Exit code.
    """
    logger = Logger(verbose=verbose)
    config = Config()

    domain = getattr(args, "domain", None)
    if not domain:
        logger.error("Domain is required")
        return 1

    unmask = getattr(args, "unmask", False)

    app_name = domain_to_app_name(domain)
    app_path = config.apps_directory / app_name

    if not app_path.exists():
        logger.error(f"Application not found: {domain}")
        return 1

    manager = EnvManager(verbose=verbose)
    values = manager.get_current_values(app_path)

    if not values:
        logger.info(f"No environment variables found for {domain}")
        return 0

    logger.header(f"Environment: {domain}")

    for key in sorted(values.keys()):
        value = values[key]
        if not unmask:
            value = manager.mask_value(key, value)
        logger.key_value(f"  {key}", value)

    return 0


def _env_export(args: Namespace, verbose: bool) -> int:
    """
    Export environment variables to a file.

    Reads the application's .env file and writes it to the
    specified output path.

    Args:
        args: Parsed arguments (requires domain, optional --output).
        verbose: Whether to enable verbose output.

    Returns:
        Exit code.
    """
    logger = Logger(verbose=verbose)
    config = Config()

    domain = getattr(args, "domain", None)
    output = getattr(args, "output", ".env")

    if not domain:
        logger.error("Domain is required")
        return 1

    app_name = domain_to_app_name(domain)
    app_path = config.apps_directory / app_name

    if not app_path.exists():
        logger.error(f"Application not found: {domain}")
        return 1

    manager = EnvManager(verbose=verbose)
    values = manager.get_current_values(app_path)

    if not values:
        logger.info(f"No environment variables found for {domain}")
        return 0

    output_path = Path(output)
    manager._write_single_env_file(output_path, values)
    logger.success(f"Exported {len(values)} variables to {output_path}")

    return 0
