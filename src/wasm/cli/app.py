# Copyright (c) 2024-2026 Yago Lopez Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
The command tree.

Click rather than argparse, for three reasons that were all real defects:

- **Global flags were shadowed.** ``--dry-run`` was declared on the root parser
  and again on several subparsers with the same dest, so argparse's subparser
  default overwrote the value the user asked for and ``wasm --dry-run monitor
  scan`` ran a real scan. Click keeps global state on the context, where a
  subcommand cannot silently overwrite it.
- **Shell completion was written by hand.** 2,295 lines across bash, zsh and
  fish, synchronised with 108 subcommands by memory, and therefore wrong.
  Click generates it from the tree.
- **The routing table was a 90-line if/elif chain**, plus twelve copies of
  "X requires an action".

Click and not Typer: the choice is packaging, not ergonomics. ``python3-click``
exists on every distribution WASM builds for and has no runtime dependency on
Linux, while Typer's ``annotated-doc`` is absent from Ubuntu 24.04, Fedora 42
and Debian trixie, which would make the .deb and .rpm unbuildable.

Subcommand modules are imported only when their command is invoked, so
``wasm --help`` and tab completion stay instant and one broken optional import
cannot take the whole CLI down with it.
"""

from __future__ import annotations

import importlib
import logging
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, TypeVar

import click

from wasm import __version__
from wasm.core.exceptions import WASMError
from wasm.core.fs import DryRunFileSystem, set_fs
from wasm.core.logger import Colors, Logger, set_colors_disabled
from wasm.core.runner import DryRunRunner, SubprocessRunner, set_runner

log = logging.getLogger(__name__)

#: Command group to the module that defines it. The value is the module path;
#: the attribute is always ``cli``.
COMMAND_MODULES: dict[str, str] = {
    "2fa": "wasm.cli.commands.twofa",
    "app": "wasm.cli.commands.app",
    "backup": "wasm.cli.commands.backup",
    "cert": "wasm.cli.commands.cert",
    "config": "wasm.cli.commands.config",
    "cron": "wasm.cli.commands.cron",
    "db": "wasm.cli.commands.db",
    "diagnose": "wasm.cli.commands.diagnose",
    "domain": "wasm.cli.commands.domain",
    "env": "wasm.cli.commands.env",
    "health": "wasm.cli.commands.health",
    "monitor": "wasm.cli.commands.monitor",
    "notify": "wasm.cli.commands.notify",
    "releases": "wasm.cli.commands.releases",
    "rollback": "wasm.cli.commands.backup",
    "service": "wasm.cli.commands.service",
    "sessions": "wasm.cli.commands.sessions",
    "setup": "wasm.cli.commands.setup",
    "site": "wasm.cli.commands.site",
    "store": "wasm.cli.commands.store",
    "token": "wasm.cli.commands.token",
    "web": "wasm.cli.commands.web",
}

#: Commands that act on a deployed application. They are top level rather than
#: under a ``webapp`` group because that is how they have always been typed.
WEBAPP_COMMANDS: dict[str, str] = dict.fromkeys(
    ("create", "delete", "list", "logs", "restart", "start", "status", "stop", "update"),
    "wasm.cli.commands.webapp",
)

#: Alternative spellings, kept because they are in muscle memory, scripts and
#: the published documentation. Removing one is a breaking change.
ALIASES: dict[str, str] = {
    "bak": "backup",
    "certificate": "cert",
    "database": "db",
    "deploy": "create",
    "info": "status",
    "ls": "list",
    "mon": "monitor",
    "new": "create",
    "rb": "rollback",
    "remove": "delete",
    "rm": "delete",
    "ssl": "cert",
    "svc": "service",
    "upgrade": "update",
}


@dataclass
class Context:
    """
    Global state, carried on the Click context.

    Living here rather than on the parsed arguments is what stops a subcommand
    from overwriting a flag the user set before the subcommand name.

    Attributes:
        verbose: Print the detail of each step.
        dry_run: Rehearse without changing the machine.
        json_output: Emit machine-readable output where a command supports it.
        no_color: Never emit ANSI escapes.
    """

    verbose: bool = False
    dry_run: bool = False
    json_output: bool = False
    no_color: bool = False
    #: Set once the seams have been swapped, so a subcommand that also
    #: accepts --dry-run does not announce the rehearsal twice.
    dry_run_active: bool = False
    _logger: Logger | None = field(default=None, repr=False)

    @property
    def logger(self) -> Logger:
        """The logger every command in this invocation shares."""
        if self._logger is None:
            self._logger = Logger(verbose=self.verbose)
        return self._logger


pass_context = click.make_pass_decorator(Context, ensure=True)


_F = TypeVar("_F", bound=Callable[..., Any])


def _adopt_json(click_ctx: click.Context, _param: click.Parameter, value: bool | None) -> None:
    """
    Fold a ``--json`` typed after a command's name into the shared context.

    Args:
        click_ctx: The command's own Click context.
        _param: The option that triggered this callback.
        value: Whether ``--json`` was given.
    """
    if value:
        click_ctx.ensure_object(Context).json_output = True


def json_option(help_text: str = "Print machine-readable JSON.") -> Callable[[_F], _F]:
    """
    Accept ``--json`` after the command name, for commands that build a payload.

    ``--json`` before the command name already works: the root group declares
    it and stores it on the shared :class:`Context`. Declaring it again on a
    command as an ordinary option would give it a default that overwrites what
    the user set before the command name - the argparse-era bug
    ``tests/test_cli_surface.py::TestGlobalFlags`` exists to catch. This option
    has ``expose_value=False`` and can only ever set the flag, never clear it.

    Args:
        help_text: The option's help line.

    Returns:
        A decorator adding the option to a command.
    """

    def decorate(command: _F) -> _F:
        return click.option(
            "--json",
            is_flag=True,
            default=None,
            is_eager=True,
            expose_value=False,
            callback=_adopt_json,
            help=help_text,
        )(command)

    return decorate


class WasmCommand(click.Command):
    """
    A leaf command that refuses ``--json`` unless it explicitly supports it.

    ``--json`` typed before the subcommand name always parses: the root group
    declares it unconditionally, for every invocation, so it can be folded
    into :class:`Context` before the subcommand's own options are even
    looked at. That is exactly what let it go silently inert - a command
    that never checks the shared context for it accepted the flag and
    printed its ordinary human output regardless, for every command in
    fourteen modules that never opted in.

    Every module's group uses :class:`WasmGroup` as its ``command_class``,
    which makes this the class every leaf command in the tree is built from,
    so "does this command handle --json" is a property of the tree instead
    of something ninety call sites each have to remember to check. A command
    opts in with :func:`json_option`; anything else gets a clear refusal
    instead of a flag that does nothing.
    """

    def invoke(self, ctx: click.Context) -> Any:
        state = ctx.ensure_object(Context)
        if state.json_output and not self._declares_json_option():
            raise click.UsageError(
                f"'{ctx.command_path}' has no JSON output; drop --json to run it."
            )
        return super().invoke(ctx)

    def _declares_json_option(self) -> bool:
        """
        Report whether this command opted into ``--json`` via :func:`json_option`.

        Checking for the option by its callback's identity, rather than by a
        separate marker a command would have to set by hand, is what keeps
        this in one place: a command that wants ``--json`` declares the
        option it already needs to declare for the after-the-name spelling
        to parse, and nothing else.

        Returns:
            True when :func:`json_option` was used to declare this command.
        """
        return any(
            isinstance(param, click.Option) and param.callback is _adopt_json
            for param in self.params
        )


class WasmGroup(click.Group):
    """
    The one group class every command module's tree is built from.

    Setting ``command_class`` here, instead of passing ``cls=WasmCommand`` at
    each of the roughly ninety ``@group.command(...)`` call sites across the
    tree, is what makes ``--json`` handling a property of how a group is
    declared rather than something each command has to opt into remembering
    - the same reasoning that put ``--dry-run`` on the execution seam instead
    of in every command that changes something.

    ``group_class = type`` carries this class - or whichever subclass a
    module defines for its own alias resolution, since every such subclass
    now inherits from this one - down to a nested group such as
    ``backup schedule``, so the property holds however deep a command sits.
    """

    command_class = WasmCommand
    group_class = type


def enable_dry_run(state: Context) -> None:
    """
    Turn the invocation into a rehearsal.

    Both seams are swapped, and both are needed. Swapping only the command
    runner is what let ``wasm --dry-run backup delete <id> --force`` announce
    that nothing would change and then delete the archive, because a deletion
    is a ``Path.unlink`` and never reaches a subprocess.

    Exported so that a subcommand which accepts ``--dry-run`` after its own
    name turns it on the same way, rather than each one repeating the wiring
    and drifting.

    Args:
        state: The shared context, already marked as a dry run.
    """
    logger = state.logger
    if state.dry_run_active:
        return
    state.dry_run_active = True

    logger.warning("Dry run: nothing on this machine will be changed")
    set_runner(
        DryRunRunner(
            SubprocessRunner(),
            on_skip=lambda cmd: logger.info(f"would run: {' '.join(cmd)}"),
        )
    )
    set_fs(DryRunFileSystem(on_skip=logger.info))


def _fold_global_flag(attribute: str) -> Callable[[click.Context, click.Parameter, bool], None]:
    """
    Build the callback that folds one global flag into the shared context.

    ``--verbose``, ``--dry-run`` and ``--no-color`` all have always worked
    typed after a subcommand's name, which means every subcommand needs its
    own copy of these three options. Before this, four modules
    (``config.py``, ``setup.py``, ``webapp.py``, ``web.py``) each grew a
    private, near-identical version of this callback, differing only in
    incidental details - ``hidden=True`` here, ``default=None`` there, a
    cache invalidation for the logger in one and not the others - that made
    the duplication easy to miss in review. This is the one place that
    decides what each flag does; :func:`global_flags` is the one decorator
    that offers them.

    Args:
        attribute: Name of the :class:`Context` attribute to set.

    Returns:
        A Click option callback.
    """

    def fold(ctx: click.Context, _param: click.Parameter, value: bool) -> None:
        if not value:
            return
        state = ctx.ensure_object(Context)
        setattr(state, attribute, True)

        # The root callback has already run by the time a subcommand's own
        # options are parsed, so a flag typed after the subcommand name has
        # to apply its own side effect here.
        if attribute == "verbose":
            # A logger already built with the old verbosity is stale.
            state._logger = None
        elif attribute == "no_color":
            set_colors_disabled(True)
        elif attribute == "dry_run":
            enable_dry_run(state)

    return fold


#: Applied in declaration order, so verbosity is adopted before anything that
#: logs during parsing.
_GLOBAL_FLAGS = (
    click.option(
        "-v",
        "--verbose",
        is_flag=True,
        is_eager=True,
        expose_value=False,
        callback=_fold_global_flag("verbose"),
        help="Show the detail of each step.",
    ),
    click.option(
        "--dry-run",
        is_flag=True,
        is_eager=True,
        expose_value=False,
        callback=_fold_global_flag("dry_run"),
        help="Rehearse without changing anything. Read-only checks still run.",
    ),
    click.option(
        "--no-color",
        is_flag=True,
        is_eager=True,
        expose_value=False,
        callback=_fold_global_flag("no_color"),
        help="Never emit colour.",
    ),
)


def global_flags(command: _F) -> _F:
    """
    Accept ``--verbose``/``--dry-run``/``--no-color`` after a command's name.

    They are the same flags the root group declares and they end up in the
    same place, the shared :class:`Context`. Nothing here binds a parameter
    of the command function: a late flag can only ever turn a setting on, and
    can never overwrite what the user asked for before the command name,
    which is the argparse-era bug this whole migration exists to remove.

    Args:
        command: The command function being decorated.

    Returns:
        The same function, with the three options attached.
    """
    for option in reversed(_GLOBAL_FLAGS):
        command = option(command)
    return command


class LazyGroup(click.Group):
    """
    A group whose subcommands are imported on use.

    Importing all thirteen command modules to print ``--help`` costs startup
    time on every invocation and makes any one broken optional dependency fatal
    for the entire CLI, including ``wasm --version``.
    """

    def __init__(self, *args: Any, lazy: dict[str, str] | None = None, **kwargs: Any):
        """
        Args:
            *args: Passed to click.Group.
            lazy: Command name to the module that defines it.
            **kwargs: Passed to click.Group.
        """
        super().__init__(*args, **kwargs)
        self._lazy = lazy or {}

    def list_commands(self, ctx: click.Context) -> list[str]:
        return sorted({*super().list_commands(ctx), *self._lazy})

    def get_command(self, ctx: click.Context, name: str) -> click.Command | None:
        resolved = ALIASES.get(name, name)

        found = super().get_command(ctx, resolved)
        if found is not None:
            return found

        module_path = self._lazy.get(resolved)
        if module_path is None:
            return None

        module = importlib.import_module(module_path)
        command = getattr(module, "cli", None)
        if command is None:
            raise click.ClickException(
                f"{module_path} does not expose a 'cli' command. This is a bug in WASM."
            )
        # A group module can define several top-level commands; pick the one
        # whose name matches so `wasm rollback` and `wasm backup` can share a
        # module without one shadowing the other.
        if isinstance(command, click.Group) and resolved in command.commands:
            return command.commands[resolved]
        if getattr(command, "name", None) not in (None, resolved) and isinstance(
            command, click.Group
        ):
            return command
        return command

    def resolve_command(
        self, ctx: click.Context, args: list[str]
    ) -> tuple[str | None, click.Command | None, list[str]]:
        # Report the name the user typed, not the one it resolves to, so an
        # error message quotes what they actually wrote.
        _, command, remaining = super().resolve_command(ctx, args)
        return (command.name if command else None), command, remaining


@click.group(
    cls=LazyGroup,
    lazy={**COMMAND_MODULES, **WEBAPP_COMMANDS},
    context_settings={"help_option_names": ["-h", "--help"], "max_content_width": 100},
    invoke_without_command=True,
)
@click.version_option(__version__, "-V", "--version", prog_name="WASM")
@click.option("-v", "--verbose", is_flag=True, help="Show the detail of each step.")
@click.option(
    "--dry-run",
    is_flag=True,
    help="Rehearse without changing anything. Read-only checks still run.",
)
@click.option("--json", "json_output", is_flag=True, help="Emit JSON where supported.")
@click.option("--no-color", is_flag=True, help="Never emit colour.")
@click.option("--changelog", is_flag=True, help="Show what changed in this release.")
@click.option("-i", "--interactive", is_flag=True, help="Start the interactive menu.")
@click.pass_context
def cli(
    ctx: click.Context,
    verbose: bool,
    dry_run: bool,
    json_output: bool,
    no_color: bool,
    changelog: bool,
    interactive: bool,
) -> None:
    """
    Deploy and manage web applications on this server.

    Run a command with --help to see what it takes, for example
    'wasm create --help'.
    """
    state = ctx.ensure_object(Context)
    state.verbose = verbose or state.verbose
    state.dry_run = dry_run or state.dry_run
    state.json_output = json_output or state.json_output
    state.no_color = no_color or state.no_color

    if state.no_color:
        set_colors_disabled(True)

    # --dry-run is enforced at the execution seam rather than in each command.
    # Wiring it per command is what left it honoured in three code paths and
    # silently ignored in every destructive one.
    if state.dry_run:
        enable_dry_run(state)

    if changelog:
        from wasm.cli.commands.version import show_changelog

        show_changelog()
        ctx.exit(0)

    if interactive:
        from wasm.cli.interactive import InteractiveMode

        ctx.exit(InteractiveMode(verbose=state.verbose).run())

    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())
        ctx.exit(0)


def main(argv: list[str] | None = None) -> int:
    """
    Run the CLI and turn a WASM error into an exit code.

    The boundary lives here so that no command has to wrap itself, which is
    what produced three hundred blind excepts across the tree.

    Args:
        argv: Arguments, defaulting to sys.argv.

    Returns:
        Process exit code.
    """
    try:
        return cli.main(args=argv, standalone_mode=False) or 0
    except click.ClickException as exc:
        exc.show()
        return exc.exit_code
    except click.Abort:
        click.echo("Cancelled", err=True)
        return 130
    except WASMError as exc:
        logger = Logger(verbose="-v" in (argv or sys.argv) or "--verbose" in (argv or sys.argv))
        logger.error(exc.message)
        if exc.details:
            logger.info(exc.details)
        # A system error is never paraphrased: nginx's, systemd's or psql's
        # own output is shown verbatim, exactly like the API's ErrorResponse
        # carries it in its own "output" field (wasm.web.api.deps.error_response).
        if exc.output:
            logger.blank()
            for line in exc.output.rstrip("\n").splitlines():
                logger._write(logger._colorize(line, Colors.DIM))
        return 1
    except KeyboardInterrupt:
        click.echo("\nInterrupted", err=True)
        return 130


def entrypoint() -> None:
    """Console script entry point."""
    checker = None
    try:
        from wasm.core.update_checker import UpdateChecker
    except ImportError as exc:
        log.debug("Update checker unavailable: %s", exc)
    else:
        checker = UpdateChecker
        checker.start_background_check()

    exit_code = main()

    if checker is not None:
        checker.show_update_if_available(timeout=0.3)

    sys.exit(exit_code)
