# Copyright (c) 2024-2025 Yago López Prado
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Environment variables of a deployed application.

The three actions share one rule: a ``.env`` holds credentials, so nothing here
prints a value in clear unless the operator asked for it with ``--unmask``, and
every file written goes through
:meth:`~wasm.deployers.helpers.env_manager.EnvManager.write_env_file`,
which creates it 0600 rather than letting the process umask decide.

Which file is the application's ``.env`` is not decided here: on the release
layout it is ``shared/.env``, and :mod:`wasm.deployers.helpers.app_env` reads
and writes it wherever it is, for this command and the panel alike.

Both entry points, the Click commands below and the legacy :func:`handle_env`,
run the same private functions, so the two paths cannot drift apart.
``wasm.cli.parser`` is gone and nothing calls :func:`handle_env` in
production anymore; it is kept, and tested directly, for the same reason.
"""

from __future__ import annotations

import json
from argparse import Namespace
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import click

from wasm.cli.app import Context, WasmGroup, json_option, pass_context
from wasm.core.config import REDACTED, redact_secrets
from wasm.core.exceptions import EnvConfigError, WASMError
from wasm.core.logger import Logger
from wasm.core.store import App
from wasm.deployers.helpers.app_env import find_app, read_app_env, write_app_env
from wasm.deployers.helpers.env_manager import EnvConfig, EnvManager, redact_url_credentials
from wasm.deployers.helpers.layout import code_path_for, env_file_for

#: Historical spellings of the subcommand names. They are in scripts and in the
#: published documentation, so dropping one is a breaking change.
ENV_ALIASES: dict[str, str] = {
    "config": "configure",
    "setup": "configure",
    "list": "show",
    "ls": "show",
}


class AliasedGroup(WasmGroup):
    """
    A group that also answers to the previous names of its commands.

    Attributes:
        aliases: Alternative spelling to the canonical command name.
    """

    def __init__(self, *args: Any, aliases: dict[str, str] | None = None, **kwargs: Any) -> None:
        """
        Args:
            *args: Passed to click.Group.
            aliases: Alternative spelling to the canonical command name.
            **kwargs: Passed to click.Group.
        """
        super().__init__(*args, **kwargs)
        self.aliases = aliases or {}

    def get_command(self, ctx: click.Context, name: str) -> click.Command | None:
        """
        Look a command up, resolving an alias first.

        Args:
            ctx: Click context.
            name: Name the user typed.

        Returns:
            The command, or None if there is no such name.
        """
        return super().get_command(ctx, self.aliases.get(name, name))

    def resolve_command(
        self, ctx: click.Context, args: list[str]
    ) -> tuple[str | None, click.Command | None, list[str]]:
        """
        Resolve a command, reporting the name the user actually typed.

        Args:
            ctx: Click context.
            args: Remaining arguments.

        Returns:
            The typed name, the command and the arguments left to parse.
        """
        _, command, remaining = super().resolve_command(ctx, args)
        return (command.name if command else None), command, remaining


def _looks_secret(name: str) -> bool:
    """
    Check a variable name against the deployer's secret patterns.

    Args:
        name: Environment variable name.

    Returns:
        True if the value behind this name must not be shown.
    """
    upper = name.upper()
    return any(pattern in upper for pattern in EnvManager.SECRET_PATTERNS)


def _redact(values: Mapping[str, str]) -> dict[str, str]:
    """
    Replace every secret value with a placeholder.

    Three classifiers are combined because each one misses what the others
    catch: :func:`~wasm.core.config.redact_secrets` splits the name into words,
    :data:`~wasm.deployers.helpers.env_manager.EnvManager.SECRET_PATTERNS`
    matches substrings such as ``_PASS``, and
    :func:`~wasm.deployers.helpers.env_manager.redact_url_credentials` catches a
    password that only appears inside the value, including the user-less
    ``redis://:password@host`` form.

    The placeholder is fixed width, so the output never reveals the length of a
    secret nor whether one is set at all.

    Args:
        values: Variable name to value.

    Returns:
        A new mapping safe to print.
    """
    by_word: Any = redact_secrets(dict(values))
    safe: dict[str, str] = {}
    for key, value in values.items():
        if by_word.get(key) == REDACTED or _looks_secret(key):
            safe[key] = REDACTED
        else:
            safe[key] = redact_url_credentials(value)
    return safe


def _app(domain: str) -> App:
    """
    Locate a deployed application.

    Args:
        domain: Domain the application is served on.

    Returns:
        The application, as the store records it or as its directory shows.

    Raises:
        EnvConfigError: If the domain is empty or nothing is deployed there.
    """
    if not domain:
        raise EnvConfigError(
            "No domain given",
            details="Name the application, for example: wasm env show example.com",
        )

    app = find_app(domain)
    if app is None:
        raise EnvConfigError(
            f"Application not found: {domain}",
            details="Nothing is deployed at that domain. Run 'wasm list' to see what is.",
        )
    return app


def _env_configure(domain: str, verbose: bool) -> int:
    """
    Ask for every variable the application declares and write its .env.

    Args:
        domain: Domain the application is served on.
        verbose: Print the detail of each step.

    Returns:
        Exit code.

    Raises:
        EnvConfigError: If nothing is deployed at this domain.
    """
    logger = Logger(verbose=verbose)
    app = _app(domain)
    manager = EnvManager(verbose=verbose)

    logger.header(f"Environment Configuration: {domain}")

    # The variables are declared by the code (.env.example), which on the
    # release layout is the active release; the values live in the .env,
    # which is in shared/.
    variables = manager.discover(code_path_for(app))
    if not variables:
        logger.info("No .env.example files found")
        return 0

    logger.info(f"Found {len(variables)} variables")

    existing = read_app_env(app, manager=manager)
    values = manager.prompt_variables(variables, existing)

    # Written 0600 and owned by the service account, never left readable by
    # the web server user.
    logger.success(f"Written: {write_app_env(app, values, manager=manager, logger=logger)}")

    manager.save_config(env_file_for(app).parent, EnvConfig(variables=variables))
    return 0


def _env_show(domain: str, unmask: bool, verbose: bool, *, json_output: bool = False) -> int:
    """
    Print the variables currently set for an application.

    Args:
        domain: Domain the application is served on.
        unmask: Print secret values in clear.
        verbose: Print the detail of each step.
        json_output: Print the variables as JSON instead of a report. Secrets
            are redacted exactly as they are for a human, unless ``unmask``
            says otherwise.

    Returns:
        Exit code.

    Raises:
        EnvConfigError: If nothing is deployed at this domain.
    """
    logger = Logger(verbose=verbose)
    values = read_app_env(_app(domain), manager=EnvManager(verbose=verbose))

    shown = dict(values) if unmask else _redact(values)

    if json_output:
        click.echo(json.dumps({"domain": domain, "variables": shown, "redacted": not unmask}))
        return 0

    if not values:
        logger.info(f"No environment variables found for {domain}")
        return 0

    logger.header(f"Environment: {domain}")

    if unmask:
        logger.warning("Printing secrets in clear. Check who can see this terminal.")

    for key in sorted(shown):
        logger.key_value(f"  {key}", shown[key])

    if not unmask:
        logger.blank()
        logger.info(f"Secrets are shown as {REDACTED}. Add --unmask to read them.")

    return 0


def _env_export(domain: str, output: str, verbose: bool) -> int:
    """
    Copy an application's variables into a file.

    Args:
        domain: Domain the application is served on.
        output: Destination path.
        verbose: Print the detail of each step.

    Returns:
        Exit code.

    Raises:
        EnvConfigError: If nothing is deployed at this domain.
        SecurityError: If the destination is a symlink.
    """
    logger = Logger(verbose=verbose)
    manager = EnvManager(verbose=verbose)
    values = read_app_env(_app(domain), manager=manager)

    if not values:
        logger.info(f"No environment variables found for {domain}")
        return 0

    output_path = Path(output)
    # The export carries the secrets in clear, so it is written through the
    # same 0600 seam as the deployed .env rather than with a plain write.
    manager.write_env_file(output_path, values)
    logger.success(f"Exported {len(values)} variables to {output_path}")
    logger.info("The file holds secrets in clear and is readable by its owner only.")
    return 0


@click.group(
    cls=AliasedGroup,
    aliases=ENV_ALIASES,
    name="env",
    epilog="Also accepts: configure as 'config' or 'setup', show as 'list' or 'ls'.",
)
def cli() -> None:
    """Read and set the environment variables of a deployed application."""


@cli.command("configure")
@click.argument("domain")
@pass_context
def configure(state: Context, domain: str) -> None:
    """Ask for each variable the application declares and save its .env."""
    _env_configure(domain, state.verbose)


@cli.command("show")
@click.argument("domain")
@click.option(
    "--unmask",
    is_flag=True,
    # Explicit, not implied: click 8.5 reports an unset flag default as a
    # sentinel, and the guarantee that secrets stay masked must not depend on
    # which click a distribution ships.
    default=False,
    help="Print secret values in clear instead of hiding them.",
)
@json_option("Print the variables as JSON.")
@pass_context
def show(state: Context, domain: str, unmask: bool) -> None:
    """List the variables an application runs with, secrets hidden."""
    _env_show(domain, unmask, state.verbose, json_output=state.json_output)


@cli.command("export")
@click.argument("domain")
@click.option(
    "-o",
    "--output",
    default=".env",
    show_default=True,
    type=click.Path(dir_okay=False, writable=True, path_type=str),
    help="Where to write the file.",
)
@pass_context
def export(state: Context, domain: str, output: str) -> None:
    """Write an application's variables to a file, owner-readable only."""
    _env_export(domain, output, state.verbose)


def handle_env(args: Namespace) -> int:
    """
    Run an env action from the argparse namespace.

    ``wasm.cli.parser`` is gone and nothing calls this in production; it is
    kept, and tested directly, sharing every private function with the Click
    commands above so the two cannot drift apart.

    Args:
        args: Parsed command-line arguments.

    Returns:
        Exit code (0 for success, non-zero for error).
    """
    verbose = getattr(args, "verbose", False)
    logger = Logger(verbose=verbose)

    action = getattr(args, "action", None)
    if not action:
        logger.error("env requires an action", details="Use: wasm env --help")
        return 1

    canonical = ENV_ALIASES.get(action, action)
    domain = getattr(args, "domain", "")

    try:
        if canonical == "configure":
            return _env_configure(domain, verbose)
        if canonical == "show":
            return _env_show(domain, getattr(args, "unmask", False), verbose)
        if canonical == "export":
            return _env_export(domain, getattr(args, "output", ".env"), verbose)
    except WASMError as exc:
        logger.error(exc.message, details=exc.details)
        return 1

    logger.error(f"Unknown env action: {action}", details="Use: wasm env --help")
    return 1
