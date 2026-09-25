# Copyright (c) 2024-2026 Yago Lopez Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
Commands for reading, upgrading and editing WASM's configuration file.

``upgrade``, ``show`` and ``path`` write nothing: bulk editing ``config.yaml``,
which holds the MySQL root password and the SMTP account, belongs in an
editor, on a file the operator can review before saving.

``get`` and ``set`` are the exception, addressed one dotted key at a time -
the same shape the panel's own settings page tells an operator to use, for
example ``wasm config set apps.directory /var/www/apps``. ``set`` goes through
:meth:`~wasm.core.config.Config.set`, which carries the same rule
:mod:`wasm.web.api.config` enforces on its own typed endpoints (a webserver
WASM has no manager for, a port or timeout out of range), so a value the panel
would reject is rejected here too. A secret value is never printed back by
either command: both redact it exactly as :func:`~wasm.core.config.redact_secrets`
does for the panel.
"""

from __future__ import annotations

from argparse import Namespace
from collections.abc import Callable
from typing import Any, NoReturn

import click
import yaml

from wasm.cli.app import Context, enable_dry_run, pass_context
from wasm.core.config import DEFAULT_CONFIG_PATH, Config, redact_secrets
from wasm.core.exceptions import ConfigError
from wasm.core.logger import Logger, set_colors_disabled

#: Marks "no default and no stored value" apart from a key genuinely holding
#: None, since Config.get(key, default) cannot otherwise tell the two apart.
_MISSING = object()

#: Command line words a boolean setting accepts, compared case-insensitively.
#: 'wasm config set' only ever has a string to work with, so a boolean key's
#: current value is what tells 'true' apart from a new string setting.
_TRUE_WORDS = frozenset({"1", "true", "yes", "on"})
_FALSE_WORDS = frozenset({"0", "false", "no", "off"})

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


def _redacted(key: str, value: Any) -> Any:
    """
    Show a value the way the panel would: secrets replaced, everything else as-is.

    Args:
        key: Dotted key the value was read from or written to.
        value: The value to display.

    Returns:
        ``value``, or :data:`~wasm.core.config.REDACTED` when the key's leaf
        name marks it as a secret.
    """
    leaf = key.rsplit(".", 1)[-1]
    return redact_secrets({leaf: value})[leaf]


def _run_get(key: str, logger: Logger) -> int:
    """
    Print one configuration value, addressed by its dotted key.

    Args:
        key: Dotted key, such as ``apps.directory`` or ``monitor.smtp.host``.
        logger: Logger for the error when the key does not exist.

    Returns:
        Exit code.
    """
    value = Config().get(key, _MISSING)
    if value is _MISSING:
        logger.error(f"No such configuration key: {key}")
        return 1

    shown = _redacted(key, value)
    if isinstance(shown, (dict, list)):
        click.echo(yaml.dump(shown, default_flow_style=False, sort_keys=False).rstrip("\n"))
    else:
        click.echo(shown)
    return 0


def _coerce_cli_value(existing: Any, raw: str) -> Any:
    """
    Parse a command line value using the type the key already holds.

    ``wasm config set`` only ever has a string to work with - argv has no other
    type - so a boolean or numeric setting has to be recovered from the shape
    it already has, the default included, or ``wasm config set ssl.enabled
    false`` would store the literal string ``"false"``, which is truthy.

    Args:
        existing: Current or default value for the key, or :data:`_MISSING`
            for a key with no default.
        raw: The value exactly as typed on the command line.

    Returns:
        The value cast to match ``existing``, or ``raw`` unchanged when there
        is nothing to match against, or the key holds a string, a mapping or a
        sequence.

    Raises:
        ConfigError: When ``existing`` is a boolean or a number and ``raw``
            cannot be parsed as one.
    """
    if existing is _MISSING or isinstance(existing, (str, dict, list)):
        return raw

    if isinstance(existing, bool):
        lowered = raw.strip().lower()
        if lowered in _TRUE_WORDS:
            return True
        if lowered in _FALSE_WORDS:
            return False
        raise ConfigError(
            f"Expected a boolean, got {raw!r}",
            details=f"Use one of: {', '.join(sorted(_TRUE_WORDS | _FALSE_WORDS))}.",
        )

    if isinstance(existing, int):
        try:
            return int(raw)
        except ValueError as exc:
            raise ConfigError(f"Expected a whole number, got {raw!r}") from exc

    if isinstance(existing, float):
        try:
            return float(raw)
        except ValueError as exc:
            raise ConfigError(f"Expected a number, got {raw!r}") from exc

    return raw


def _run_set(key: str, raw_value: str, logger: Logger) -> int:
    """
    Set one configuration value, addressed by its dotted key, and save it.

    Goes through :meth:`~wasm.core.config.Config.set`, so a value the panel's
    own endpoints would reject - an unsupported webserver, a port or a timeout
    out of range - is rejected here in the same words.

    Args:
        key: Dotted key, such as ``apps.directory`` or ``web.port``.
        raw_value: The new value, exactly as typed on the command line.
        logger: Logger for progress and errors.

    Returns:
        Exit code.
    """
    config = Config()
    existing = config.get(key, _MISSING)

    try:
        value = _coerce_cli_value(existing, raw_value)
        config.set(key, value)
    except ConfigError as exc:
        logger.error(str(exc))
        return 1

    try:
        path = config.write()
    except PermissionError as exc:
        logger.error(f"Permission denied writing to {DEFAULT_CONFIG_PATH}: {exc}")
        return 1
    except (OSError, yaml.YAMLError) as exc:
        logger.error(f"Failed to save configuration: {exc}")
        return 1

    logger.success(f"Set {key} = {_redacted(key, config.get(key))}")
    logger.key_value("Config file", str(path))
    return 0


def handle_config(args: Namespace) -> int:
    """
    Dispatch a config action parsed by argparse.

    ``wasm.cli.parser`` is gone and nothing calls this in production; it is
    kept, and tested directly, sharing every function with the Click commands
    below so there is one implementation of each action. ``get`` and ``set``
    never existed in the argparse tree and are not routed here: they are
    reached only through the Click commands.

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
    logger.info("  get        Print one configuration value")
    logger.info("  set        Set one configuration value")
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


@cli.command("get")
@click.argument("key")
@global_flags
@pass_context
def get(ctx: Context, key: str) -> None:
    """
    Print one configuration value, addressed by its dotted key.

    For example: 'wasm config get monitor.smtp.host'. A secret value is
    printed as *** rather than in the clear, exactly as the panel shows it.
    """
    _exit(_run_get(key, ctx.logger))


@cli.command("set")
@click.argument("key")
@click.argument("value")
@global_flags
@pass_context
def set_(ctx: Context, key: str, value: str) -> None:
    """
    Set one configuration value, addressed by its dotted key, and save it.

    For example: 'wasm config set apps.directory /var/www/apps'. The value is
    checked against the same rule the panel applies to that key, when it has
    one, so an unsupported webserver or a port out of range is refused here
    too rather than written and discovered later.
    """
    _exit(_run_set(key, value, ctx.logger))
