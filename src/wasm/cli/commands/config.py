"""
Config command handlers for WASM.

Commands for managing WASM configuration files.
"""

from argparse import Namespace

from wasm.core.config import Config, DEFAULT_CONFIG_PATH
from wasm.core.logger import Logger


def handle_config(args: Namespace) -> int:
    """
    Handle config commands.

    Args:
        args: Parsed arguments.

    Returns:
        Exit code.
    """
    action = args.action

    handlers = {
        "upgrade": _handle_upgrade,
        "show": _handle_show,
        "path": _handle_path,
    }

    handler = handlers.get(action)
    if handler:
        return handler(args)

    # No action specified, show help
    logger = Logger(verbose=getattr(args, "verbose", False))
    logger.info("Usage: wasm config <command>")
    logger.info("")
    logger.info("Commands:")
    logger.info("  upgrade    Upgrade config file with new defaults")
    logger.info("  show       Show current configuration")
    logger.info("  path       Show config file path")
    return 0


def _handle_upgrade(args: Namespace) -> int:
    """Handle config upgrade command."""
    quiet = getattr(args, "quiet", False)
    logger = Logger(verbose=getattr(args, "verbose", False))

    if not quiet:
        logger.info("Upgrading configuration file...")

    config = Config()
    result = config.upgrade()

    if "error" in result:
        if not quiet:
            logger.error(f"Failed to upgrade config: {result['error']}")
        return 1

    if result["upgraded"]:
        if not quiet:
            logger.success(f"Configuration upgraded! Added {len(result['added_keys'])} new option(s):")
            for key in result["added_keys"][:10]:  # Show first 10
                logger.info(f"  + {key}")
            if len(result["added_keys"]) > 10:
                logger.info(f"  ... and {len(result['added_keys']) - 10} more")
    else:
        if not quiet:
            logger.success("Configuration is already up to date")

    return 0


def _handle_show(args: Namespace) -> int:
    """Handle config show command."""
    import yaml

    logger = Logger(verbose=getattr(args, "verbose", False))
    config = Config()

    logger.header("Current Configuration")
    print(yaml.dump(config.to_dict(), default_flow_style=False, sort_keys=False))

    return 0


def _handle_path(args: Namespace) -> int:
    """Handle config path command."""
    logger = Logger(verbose=getattr(args, "verbose", False))

    logger.key_value("Config file", str(DEFAULT_CONFIG_PATH))
    logger.key_value("Exists", "Yes" if DEFAULT_CONFIG_PATH.exists() else "No")

    return 0
