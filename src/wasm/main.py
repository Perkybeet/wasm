"""
Main entry point for WASM CLI.
"""

import sys

from wasm.cli.parser import parse_args, WEBAPP_ACTIONS
from wasm.cli.interactive import InteractiveMode
from wasm.cli.commands import (
    handle_webapp,
    handle_site,
    handle_service,
    handle_cert,
    handle_setup,
)
from wasm.cli.commands.monitor import handle_monitor
from wasm.core.logger import Logger
from wasm.core.exceptions import WASMError


def main() -> int:
    """
    Main entry point for WASM.
    
    Returns:
        Exit code.
    """
    args = parse_args()
    
    # Interactive mode
    if args.interactive:
        try:
            interactive = InteractiveMode(verbose=args.verbose)
            return interactive.run()
        except WASMError as e:
            logger = Logger(verbose=args.verbose)
            logger.error(str(e))
            return 1
    
    # No command provided
    if not args.command:
        from wasm.cli.parser import create_parser
        parser = create_parser()
        parser.print_help()
        return 0
    
    # Route to appropriate handler
    command = args.command
    
    # Webapp actions are now top-level commands
    if command in WEBAPP_ACTIONS:
        # Set action for the webapp handler
        args.action = command
        return handle_webapp(args)
    
    elif command == "site":
        if not args.action:
            print("Error: site requires an action", file=sys.stderr)
            print("Use: wasm site --help", file=sys.stderr)
            return 1
        return handle_site(args)
    
    elif command in ["service", "svc"]:
        if not args.action:
            print("Error: service requires an action", file=sys.stderr)
            print("Use: wasm service --help", file=sys.stderr)
            return 1
        return handle_service(args)
    
    elif command in ["cert", "ssl", "certificate"]:
        if not args.action:
            print("Error: cert requires an action", file=sys.stderr)
            print("Use: wasm cert --help", file=sys.stderr)
            return 1
        return handle_cert(args)
    
    elif command == "setup":
        if not args.action:
            print("Error: setup requires an action", file=sys.stderr)
            print("Use: wasm setup --help", file=sys.stderr)
            return 1
        return handle_setup(args)
    
    elif command in ["monitor", "mon"]:
        if not args.action:
            print("Error: monitor requires an action", file=sys.stderr)
            print("Use: wasm monitor --help", file=sys.stderr)
            return 1
        return handle_monitor(args)
    
    else:
        print(f"Unknown command: {command}", file=sys.stderr)
        return 1


def cli():
    """CLI entry point for setuptools console_scripts."""
    sys.exit(main())


if __name__ == "__main__":
    cli()
