# Copyright (c) 2024-2026 Yago Lopez Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
Commands for reading and upgrading WASM's configuration file.

Nothing here writes a value: ``config.yaml`` holds the MySQL root password and
the SMTP account, and editing it belongs in an editor, on a file the operator
can review before saving. What these commands do is say where the file is, show
what WASM believes is in it, and add the keys a newer WASM expects without
touching the ones already set.
"""

from __future__ import annotations

from argparse import Namespace
from collections.abc import Callable
from typing import Any, NoReturn

import click

from wasm.cli.app import Context, enable_dry_run, pass_context
from wasm.core.config import DEFAULT_CONFIG_PATH, Config
from wasm.core.logger import Logger, set_colors_disabled

#: How many added keys the upgrade lists before summarising the rest.
MAX_LISTED_KEYS = 10


def _fold_into_context(attribute: str) -> Callable[[click.Context, click.Parameter, bool], bool]:
    """
    Build the callback that records a global flag on the shared context.

    Args:
        attribute: Name of the :class:`~wasm.cli.app.Context` attribute to set.

    Returns:
        A Click option callback.
    """

    def fold(ctx: click.Context, param: click.Parameter, value: bool) -> bool:
        if not value:
            return value
        state = ctx.ensure_object(Context)
        setattr(state, attribute, True)
        if attribute == "no_color":
            set_colors_disabled(True)
        elif attribute == "dry_run":
            enable_dry_run(state)
        return value

    return fold


def global_flags(command: Callable[..., Any]) -> Callable[..., Any]:
    """
    Re-offer the root group's flags on a subcommand.

    ``wasm config show --verbose`` is in scripts, in the published documentation
    and in muscle memory, so the flags have to keep parsing after the subcommand
    name. None of these options owns a value: they are eager, they do not reach
    the command function, and their callbacks only ever switch the shared
    context on. A subcommand therefore cannot undo a flag the user set before
    the subcommand name, which is exactly how ``wasm --dry-run monitor scan``
    used to run for real.

    Args:
        command: The function being decorated into a Click command.

    Returns:
        The decorated function.
    """
    options = [
        click.option(
            "-v",
            "--verbose",
            is_flag=True,
            is_eager=True,
            expose_value=False,
            callback=_fold_into_context("verbose"),
            help="Show the detail of each step.",
        ),
        click.option(
            "--dry-run",
            is_flag=True,
            is_eager=True,
            expose_value=False,
            callback=_fold_into_context("dry_run"),
            help="Rehearse without changing anything.",
        ),
        click.option(
            "--no-color",
            is_flag=True,
            is_eager=True,
            expose_value=False,
            callback=_fold_into_context("no_color"),
            help="Never emit colour.",
        ),
    ]
    for option in reversed(options):
        command = option(command)
    return command


def _exit(code: int) -> NoReturn:
    """
    Leave the command with a status the calling shell can test.

    Args:
        code: Process exit status.

    Raises:
        click.exceptions.Exit: Always; this is how Click unwinds.
    """
    click.get_current_context().exit(code)


def _run_upgrade(logger: Logger, quiet: bool) -> int:
    """
    Add the options a newer WASM expects, keeping every value already set.

    Args:
        logger: Logger used to report progress.
        quiet: Say nothing; for scripts that only read the exit status.

    Returns:
        Exit code.
    """
    if not quiet:
        logger.info(f"Upgrading {DEFAULT_CONFIG_PATH}...")

    result = Config().upgrade()

    if "error" in result:
        # Reported even when quiet: a script that silently ignored a failed
        # upgrade would go on to read options that are not there.
        logger.error(f"Could not upgrade {DEFAULT_CONFIG_PATH}: {result['error']}")
        return 1

    if quiet:
        return 0

    added: list[str] = result["added_keys"]
    if not result["upgraded"]:
        logger.success("Configuration is already up to date")
        return 0

    logger.success(f"Configuration upgraded. Added {len(added)} new option(s):")
    for key in added[:MAX_LISTED_KEYS]:
        logger.list_item(f"+ {key}")
    if len(added) > MAX_LISTED_KEYS:
        logger.info(f"  ... and {len(added) - MAX_LISTED_KEYS} more")

    return 0


def _run_show(logger: Logger) -> int:
    """
    Print the configuration WASM is actually running with.

    Args:
        logger: Logger used to report progress.

    Returns:
        Exit code.
    """
    import yaml

    logger.header("Current Configuration")
    click.echo(yaml.dump(Config().to_dict(), default_flow_style=False, sort_keys=False))
    return 0


def _run_path(logger: Logger) -> int:
    """
    Say where the configuration file is and whether it exists yet.

    Args:
        logger: Logger used to report progress.

    Returns:
        Exit code.
    """
    logger.key_value("Config file", str(DEFAULT_CONFIG_PATH))
    logger.key_value("Exists", "Yes" if DEFAULT_CONFIG_PATH.exists() else "No")
    return 0


def handle_config(args: Namespace) -> int:
    """
    Dispatch a config action parsed by argparse.

    Kept while the argparse parser is still wired up; both entry points call the
    same functions, so there is one implementation of each action.

    Args:
        args: Parsed arguments.

    Returns:
        Exit code.
    """
    logger = Logger(verbose=getattr(args, "verbose", False))
    action = getattr(args, "action", None)

    if action == "upgrade":
        return _run_upgrade(logger, quiet=getattr(args, "quiet", False))
    if action == "show":
        return _run_show(logger)
    if action == "path":
        return _run_path(logger)

    logger.info("Usage: wasm config <command>")
    logger.blank()
    logger.info("Commands:")
    logger.info("  upgrade    Add the options a newer WASM expects")
    logger.info("  show       Show the configuration in effect")
    logger.info("  path       Show where the configuration file lives")
    return 0


@click.group("config")
@global_flags
def cli() -> None:
    """Read and upgrade WASM's configuration file."""


@cli.command("upgrade")
@click.option(
    "-q",
    "--quiet",
    is_flag=True,
    help="Say nothing unless it fails. Use this in provisioning scripts.",
)
@global_flags
@pass_context
def upgrade(ctx: Context, quiet: bool) -> None:
    """
    Add the options a newer WASM expects.

    Values already set are kept exactly as they are.
    """
    _exit(_run_upgrade(ctx.logger, quiet=quiet))


@cli.command("show")
@global_flags
@pass_context
def show(ctx: Context) -> None:
    """
    Show the configuration in effect.

    This is the merge of the defaults, the file and any WASM_* environment
    variable, which is what WASM actually reads.
    """
    _exit(_run_show(ctx.logger))


@cli.command("path")
@global_flags
@pass_context
def path(ctx: Context) -> None:
    """Show where the configuration file lives, and whether it exists."""
    _exit(_run_path(ctx.logger))
