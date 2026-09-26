"""
The command surface is a contract.

``tests/contracts/cli_surface.json`` was generated from the argparse tree before
the migration to Click. Every command, alias and option in it is in somebody's
shell history, somebody's deploy script and the published documentation, so
losing one is a breaking change whether or not anyone meant it.

These tests are what made a 108-subcommand migration safe to do at all.
"""

from __future__ import annotations

import json
from pathlib import Path

import click
import pytest
from click.testing import CliRunner

from wasm.cli.app import ALIASES, cli

CONTRACT_FILE = Path(__file__).parent / "contracts/cli_surface.json"
CONTRACT: dict[str, dict] = json.loads(CONTRACT_FILE.read_text(encoding="utf-8"))

#: Every command path the argparse tree exposed, deepest last.
COMMAND_PATHS = sorted(key for key in CONTRACT if key)


def resolve(path: str) -> click.Command | None:
    """
    Walk the Click tree to the command at a space-separated path.

    Args:
        path: Command path such as ``"backup schedule create"``.

    Returns:
        The command, or None when some segment does not resolve.
    """
    command: click.Command | None = cli
    for name in path.split(" "):
        if not isinstance(command, click.Group):
            return None
        command = command.get_command(click.Context(command), name)
        if command is None:
            return None
    return command


@pytest.fixture
def runner_cli() -> CliRunner:
    """Return a Click test runner."""
    return CliRunner()


@pytest.mark.parametrize("path", COMMAND_PATHS)
def test_every_command_still_exists(path: str):
    """A command that existed before the migration still exists."""
    assert resolve(path) is not None, f"'wasm {path}' no longer exists"


@pytest.mark.parametrize("path", COMMAND_PATHS)
def test_every_option_still_exists(path: str):
    """
    An option that existed before the migration still exists.

    Global flags are the exception: they moved to the root, which is the whole
    point, because declaring them again on a subcommand is what let argparse
    overwrite --dry-run with its own default and run a real scan.
    """
    global_flags = {"--verbose", "-v", "--dry-run", "--json", "--no-color"}
    command = resolve(path)
    assert command is not None

    present = {
        opt
        for param in command.params
        if isinstance(param, click.Option)
        for opt in param.opts + param.secondary_opts
    }
    expected = set(CONTRACT[path]["options"]) - global_flags
    missing = expected - present

    assert not missing, f"'wasm {path}' lost options: {sorted(missing)}"


@pytest.mark.parametrize(
    ("path", "alias"),
    [(path, alias) for path in COMMAND_PATHS for alias in CONTRACT[path]["aliases"]],
)
def test_every_alias_still_resolves(path: str, alias: str):
    """An alias that existed before the migration still reaches its command."""
    parent = " ".join(path.split(" ")[:-1])
    target = path.split(" ")[-1]

    alias_path = f"{parent} {alias}".strip()
    resolved = resolve(alias_path)

    assert resolved is not None, f"alias 'wasm {alias_path}' no longer resolves"
    assert resolved.name in (target, alias), (
        f"alias 'wasm {alias_path}' reaches '{resolved.name}', expected '{target}'"
    )
    assert ALIASES.get(alias, target) in (target, alias) or parent


@pytest.mark.parametrize("path", COMMAND_PATHS)
def test_every_command_has_help(path: str, runner_cli: CliRunner):
    """
    Every command explains itself.

    A command with no help text is a command nobody outside the codebase can
    use, and the help is the only documentation most operators will read.
    """
    command = resolve(path)
    assert command is not None

    text = (command.help or command.short_help or "").strip()
    assert text, f"'wasm {path}' has no help text"

    result = runner_cli.invoke(cli, [*path.split(" "), "--help"])
    assert result.exit_code == 0, f"'wasm {path} --help' failed:\n{result.output}"


class TestGlobalFlags:
    """
    A global flag can be given on either side of the subcommand name, and the
    subcommand can never turn it off.

    Declaring --dry-run on the root and again on a subparser is what made
    'wasm --dry-run monitor scan' run a real scan that terminated processes:
    argparse let the subparser's default overwrite the value the user had
    already given.

    The fix is not to forbid the flag after the command name, which people
    type and which their scripts contain. It is that the subcommand's copy
    writes to the shared context and can only ever set it, never clear it, so
    both orders mean the same thing and neither can undo the other.
    """

    GLOBAL = ("--verbose", "--dry-run", "--json", "--no-color")

    #: Commands that legitimately own an option of the same name for a
    #: different purpose.
    OWN_FLAG_ALLOWED = {
        # certbot's own rehearsal against the staging directory, which is a
        # different thing from WASM's --dry-run and predates it.
        ("cert", "create"),
        ("cert", "renew"),
    }

    def walk(self, command: click.Command, path: tuple[str, ...] = ()):
        """
        Yield every command in the tree with its path.

        Args:
            command: Root to walk from.
            path: Names leading here.

        Yields:
            Tuples of path and command.
        """
        yield path, command
        if isinstance(command, click.Group):
            ctx = click.Context(command)
            for name in command.list_commands(ctx):
                sub = command.get_command(ctx, name)
                if sub is not None:
                    yield from self.walk(sub, (*path, name))

    def test_no_subcommand_binds_a_global_flag_to_its_own_variable(self):
        """
        A subcommand's copy of a global flag must not become a parameter.

        The moment it does, it has a default, and the default overwrites what
        the user typed before the command name. That is the exact shape of the
        original bug, so it is the shape the test looks for: not the flag's
        presence, but whether it carries a value into the command.
        """
        offenders = []
        for path, command in self.walk(cli):
            if not path or path[:2] in self.OWN_FLAG_ALLOWED or path[-1:] in self.OWN_FLAG_ALLOWED:
                continue
            for param in command.params:
                if not isinstance(param, click.Option):
                    continue
                clash = set(param.opts) & set(self.GLOBAL)
                if clash and param.expose_value:
                    offenders.append(f"wasm {' '.join(path)}: {sorted(clash)}")

        assert not offenders, (
            "These subcommands bind a global flag to their own parameter, so its "
            "default overwrites what the user set before the command name. Use "
            "expose_value=False with a callback that only sets the shared "
            "context:\n" + "\n".join(f"  {o}" for o in offenders)
        )

    @pytest.mark.parametrize("flag", GLOBAL)
    def test_the_root_declares_it(self, flag: str):
        present = {opt for param in cli.params for opt in getattr(param, "opts", [])}
        assert flag in present

    def test_a_global_flag_set_before_the_subcommand_reaches_it(self, runner_cli: CliRunner):
        """
        This is the regression that mattered.

        'wasm --dry-run <anything>' has to mean a dry run for that command, not
        for the three that happened to read the flag.
        """
        from wasm.cli.app import Context

        seen: dict[str, bool] = {}

        @cli.command("probe-dry-run", hidden=True)
        @click.pass_context
        def probe(ctx: click.Context) -> None:
            """Record what the global state says."""
            seen["dry_run"] = ctx.ensure_object(Context).dry_run

        try:
            runner_cli.invoke(cli, ["--dry-run", "probe-dry-run"], standalone_mode=False)
            assert seen.get("dry_run") is True
        finally:
            cli.commands.pop("probe-dry-run", None)


class TestErrorBoundary:
    """
    ``main()`` is the one place a ``WASMError`` becomes an exit code, so a
    field it drops is invisible everywhere the CLI is the front end.

    A tool's own words - psql's, nginx's or systemd's - explain a failure
    better than any paraphrase, which is why ``WASMError`` carries them in a
    separate ``output`` field rather than folding them into ``details``. The
    web API already prints it verbatim in ``ErrorResponse``
    (:func:`wasm.web.api.deps.error_response`); the CLI boundary has to as
    well, or an operator at a terminal sees less than a script hitting the
    same failure over the API.
    """

    def test_the_tools_own_output_is_printed_verbatim(self, capsys: pytest.CaptureFixture[str]):
        from wasm.cli.app import main
        from wasm.core.exceptions import WASMError

        @cli.command("probe-error-output", hidden=True)
        def probe() -> None:
            raise WASMError(
                "the command failed",
                details="try again with --force",
                output='nginx: [emerg] unexpected "}" in /etc/nginx/nginx.conf:12',
            )

        try:
            exit_code = main(["probe-error-output"])
        finally:
            cli.commands.pop("probe-error-output", None)

        captured = capsys.readouterr()
        assert exit_code == 1
        assert "the command failed" in captured.out
        assert "try again with --force" in captured.out
        assert 'nginx: [emerg] unexpected "}" in /etc/nginx/nginx.conf:12' in captured.out
        # The fix is said once: str(exc) folds details in, and the boundary printed both.
        assert captured.out.count("try again with --force") == 1

    def test_no_output_field_prints_no_extra_block(self, capsys: pytest.CaptureFixture[str]):
        """A WASMError with nothing to show verbatim adds nothing extra."""
        from wasm.cli.app import main
        from wasm.core.exceptions import WASMError

        @cli.command("probe-error-no-output", hidden=True)
        def probe() -> None:
            raise WASMError("the command failed")

        try:
            exit_code = main(["probe-error-no-output"])
        finally:
            cli.commands.pop("probe-error-no-output", None)

        captured = capsys.readouterr()
        assert exit_code == 1
        assert "the command failed" in captured.out


#: Commands whose help, or whose siblings in 2.1, promise the global flags
#: after the command name. 'wasm backup import DIR --dry-run' was a usage
#: error while the help text told the operator to use exactly that.
AFTER_THE_NAME = ("app health", "releases keep", "notify telegram-chats", "backup import")


@pytest.mark.parametrize("path", AFTER_THE_NAME)
def test_these_commands_accept_the_global_flags_after_their_name(path: str) -> None:
    command = resolve(path)
    assert command is not None
    declared = {opt for param in command.params for opt in param.opts}

    for flag in ("--verbose", "--dry-run", "--no-color"):
        assert flag in declared, f"'wasm {path} {flag}' is a usage error"
