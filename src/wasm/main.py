# Copyright (c) 2024-2025 Yago López Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
Main entry point for WASM CLI.

Command routing lives in the parser: every command parser carries a ``func``
default pointing at its handler, so this module only has to resolve global
flags and call it.
"""

import logging
import sys

from wasm.cli.interactive import InteractiveMode
from wasm.cli.parser import create_parser, parse_args
from wasm.core.exceptions import WASMError
from wasm.core.logger import Logger, set_colors_disabled

log = logging.getLogger(__name__)


def main(argv: list[str] | None = None) -> int:
    """
    Main entry point for WASM.

    Args:
        argv: Command line arguments (defaults to sys.argv).

    Returns:
        Exit code.
    """
    args = parse_args(argv)

    # Handlers build their own loggers, so the flag has to be applied globally.
    if args.no_color:
        set_colors_disabled(True)

    if args.changelog:
        from wasm.cli.commands.version import show_changelog

        show_changelog()
        return 0

    if args.interactive:
        try:
            interactive = InteractiveMode(verbose=args.verbose)
            return interactive.run()
        except WASMError as e:
            logger = Logger(verbose=args.verbose)
            logger.error(str(e))
            return 1

    # No command provided: argparse cannot require one because bare `wasm`
    # must show the help instead of failing.
    handler = getattr(args, "func", None)
    if handler is None:
        create_parser().print_help()
        return 0

    return handler(args)


def cli() -> None:
    """CLI entry point for setuptools console_scripts."""
    # The update check runs in parallel with the command itself.
    checker = None
    try:
        from wasm.core.update_checker import UpdateChecker
    except ImportError as e:
        log.debug("Update checker unavailable: %s", e)
    else:
        checker = UpdateChecker
        checker.start_background_check()

    exit_code = main()

    # Wait up to 0.3s for the background check (usually done by now).
    if checker is not None:
        checker.show_update_if_available(timeout=0.3)

    sys.exit(exit_code)


if __name__ == "__main__":
    cli()
