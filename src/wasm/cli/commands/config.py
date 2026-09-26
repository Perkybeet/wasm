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
example ``wasm config set apps_directory /var/www/apps``. ``set`` goes through
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
from wasm.core.config import (
    DEFAULT_CONFIG_PATH,
    NO_DEFAULT,
    Config,
    coerce_config_value,
    redact_secrets,
)
from wasm.core.exceptions import ConfigError
from wasm.core.logger import Logger, set_colors_disabled

#: Marks "no default and no stored value" apart from a key genuinely holding
#: None, since Config.get(key, default) cannot otherwise tell the two apart.
#: The same sentinel :func:`~wasm.core.config.coerce_config_value` uses for
#: "no schema to match", so a key with no default falls through to its JSON
#: scalar/list parsing rather than being treated as a string.
_MISSING = NO_DEFAULT

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

    Dumps through :func:`~wasm.core.config.redact_secrets` first, the same
    helper 'config get' and 'config set' already use: this prints the whole
    tree at once, so skipping it would put every credential in the file - the
    MySQL root password and the SMTP account included - on the operator's
    terminal and in their shell history.

    Args:
        logger: Logger used to report progress.

    Returns:
        Exit code.
    """
    logger.header("Current Configuration")
    redacted = redact_secrets(Config().to_dict())
    click.echo(yaml.dump(redacted, default_flow_style=False, sort_keys=False))
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
        key: Dotted key, such as ``apps_directory`` or ``monitor.smtp.host``.
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


def _parse_list_value(raw: str) -> list[str]:
    """
    Split a comma-separated command line value into a list.

    This is what ``--list`` asks for: argv has no native list type, and
    typing ``wasm config set notifications.allow_private_hosts
    internal.example,partner.example --list`` is the documented way to give a
    list value to a key such as ``notifications.allow_private_hosts``, which
    has no default for :func:`_coerce_cli_value` to recognise as one.

    Empty items are dropped, so a trailing comma or repeated commas do not
    store a value nobody typed.

    Args:
        raw: The value exactly as typed after ``--list``.

    Returns:
        The items, whitespace trimmed, in the order given.
    """
    return [item.strip() for item in raw.split(",") if item.strip()]


def _coerce_cli_value(existing: Any, raw: str) -> Any:
    """
    Parse a command line value using the key's schema.

    ``wasm config set`` only ever has a string to work with - argv has no other
    type - so a boolean, numeric or list setting has to be recovered from the
    shape it already has, the default included, or ``wasm config set
    ssl.enabled false`` would store the literal string ``"false"``, which is
    truthy. A key whose current or default value is a list also accepts a
    JSON array (``wasm config set monitor.email_recipients
    '["a@example.com"]'``) without needing ``--list``, since argv already
    hands over one string a command line has no other way to shape.

    A key with no default at all - ``existing`` is :data:`_MISSING` - is
    parsed as a JSON scalar (``true``, ``false``, ``null``, a number) or a
    JSON array instead, falling back to the plain string it looks like when it
    is not valid JSON or parses to something else, such as an object. This is
    the same coercion :func:`~wasm.web.api.config.patch_config` applies to a
    string value arriving over ``PATCH /api/config``, so a key without a
    schema does not behave differently depending on which front end wrote it.

    Args:
        existing: Current or default value for the key, or :data:`_MISSING`
            for a key with no default.
        raw: The value exactly as typed on the command line.

    Returns:
        The value cast to match ``existing``, a JSON scalar or list recovered
        from ``raw`` when there is no default to match, or ``raw`` unchanged
        when nothing else applies.

    Raises:
        ConfigError: When ``existing`` is a boolean or a number and ``raw``
            cannot be parsed as one.
    """
    return coerce_config_value(existing, raw)


def _run_set(key: str, raw_value: str, logger: Logger, *, as_list: bool = False) -> int:
    """
    Set one configuration value, addressed by its dotted key, and save it.

    Goes through :meth:`~wasm.core.config.Config.set`, so a value the panel's
    own endpoints would reject - an unsupported webserver, a port or a timeout
    out of range - is rejected here in the same words.

    Args:
        key: Dotted key, such as ``apps_directory`` or ``web.port``.
        raw_value: The new value, exactly as typed on the command line.
        logger: Logger for progress and errors.
        as_list: Treat ``raw_value`` as a comma-separated list, for a key such
            as ``notifications.allow_private_hosts`` that has no default value
            :func:`_coerce_cli_value` could otherwise recognise as one.

    Returns:
        Exit code.
    """
    config = Config()
    existing = config.get(key, _MISSING)

    try:
        value = _parse_list_value(raw_value) if as_list else _coerce_cli_value(existing, raw_value)
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
@click.option(
    "--list",
    "as_list",
    is_flag=True,
    help=(
        "Treat VALUE as a comma-separated list, for a key such as "
        "notifications.allow_private_hosts."
    ),
)
@global_flags
@pass_context
def set_(ctx: Context, key: str, value: str, as_list: bool) -> None:
    """
    Set one configuration value, addressed by its dotted key, and save it.

    For example: 'wasm config set apps_directory /var/www/apps'. The value is
    checked against the same rule the panel applies to that key, when it has
    one, so an unsupported webserver or a port out of range is refused here
    too rather than written and discovered later. 'apps.directory' is accepted
    as a deprecated alias for 'apps_directory' and is normalised to it, on
    both 'get' and 'set'.

    A key needing a list value takes it two ways: 'wasm config set
    notifications.allow_private_hosts internal.example,partner.example
    --list' splits VALUE on commas, and a key that already holds a list (for
    example monitor.email_recipients) also accepts a JSON array such as
    '["a@example.com"]' without --list.

    VALUE is coerced by the key's schema: a key with a default is parsed as
    that default's type (a boolean, a whole number, a decimal or a list), so
    'wasm config set ssl.enabled false' stores False, not the string "false".
    A key with no default, such as monitor.notify, is parsed as a JSON scalar
    or list instead - true, false, null, a number, or a JSON array - and
    falls back to a plain string when VALUE is not valid JSON.
    """
    _exit(_run_set(key, value, ctx.logger, as_list=as_list))
