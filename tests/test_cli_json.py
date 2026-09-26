# Copyright (c) 2024-2026 Yago Lopez Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
``--json`` across the whole command tree.

``wasm list``, ``wasm status`` and ``wasm logs`` were once the only three
handlers that honoured the flag: everything else in fourteen other modules
accepted it before the subcommand name (the root group declares it
unconditionally) and then silently printed for a human anyway, which is a
payload no script could rely on. Every leaf command now does one of two
things, enforced once by :class:`~wasm.cli.app.WasmCommand` rather than
per command: it builds a real payload through :func:`~wasm.cli.app.json_option`,
or it refuses with a usage error. :func:`test_every_leaf_command_handles_json`
is the sweep that checks this holds for the entire tree, not only the
commands exercised individually elsewhere in this file and in each module's
own test file (``test_cli_cert.py``, ``test_cli_service.py``, ...).

The flag has to work on either side of the command name, because that is true
of every other global flag and a script that learned ``wasm --json list``
should not have to learn a second spelling for ``wasm list --json``.

Fixtures and spies for ``list``/``status``/``logs`` come from
:mod:`tests.test_cli_webapp`, which already builds the store and service
manager doubles these commands are exercised against; duplicating them here
would be a second definition of the same fakes drifting from the first.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import click
import pytest
from click.testing import CliRunner

from tests.test_cli_webapp import ServiceSpy, StoreSpy, console, make_app, services, store
from wasm.cli.app import Context, WasmCommand
from wasm.cli.app import cli as root_cli
from wasm.cli.commands import webapp

# Re-exported so pytest discovers them as fixtures in this module too.
__all__ = ["console", "services", "store"]


@pytest.fixture
def cli_runner() -> CliRunner:
    """Return a Click test runner."""
    return CliRunner()


def _relations(**overrides: Any) -> dict[str, Any]:
    """
    Build a store relations payload ``status`` can succeed against.

    Args:
        **overrides: Fields of the app row to override.

    Returns:
        The relations mapping ``get_app_with_relations`` returns.
    """
    return {
        "app": make_app(**overrides),
        "site": SimpleNamespace(
            webserver="nginx",
            ssl_enabled=True,
            config_path="/etc/nginx/sites-available/example.com",
        ),
        "service": SimpleNamespace(name="example-com"),
        "databases": [],
    }


# ---------------------------------------------------------------------------
# wasm list --json
# ---------------------------------------------------------------------------


def test_list_json_before_the_command_name(
    cli_runner: CliRunner, store: StoreSpy, services: ServiceSpy
) -> None:
    """The root's --json, given before the command, produces the payload too."""
    store.apps["example.com"] = make_app()

    result = cli_runner.invoke(root_cli, ["--json", "list"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["items"][0]["domain"] == "example.com"


def test_list_json_after_the_command_name(
    cli_runner: CliRunner, store: StoreSpy, services: ServiceSpy
) -> None:
    """--json after 'list' works the same as before it."""
    store.apps["example.com"] = make_app()

    result = cli_runner.invoke(webapp.cli.commands["list"], ["--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload == {
        "items": [
            {
                "domain": "example.com",
                "type": "nextjs",
                "status": "Running",
                "healthy": True,
                "detail": None,
                "port": 3000,
                "ssl_enabled": True,
            }
        ]
    }


def test_list_json_reports_an_empty_machine(cli_runner: CliRunner, store: StoreSpy) -> None:
    """No applications is {"items": []}, not the human "No applications" text."""
    result = cli_runner.invoke(webapp.cli.commands["list"], ["--json"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == {"items": []}


def test_list_without_json_still_prints_a_table(
    cli_runner: CliRunner, store: StoreSpy, services: ServiceSpy
) -> None:
    """The default stays human-readable; --json is opt-in."""
    store.apps["example.com"] = make_app()

    result = cli_runner.invoke(webapp.cli.commands["list"], [])

    assert result.exit_code == 0, result.output
    with pytest.raises(json.JSONDecodeError):
        json.loads(result.output)


def test_list_json_and_open_are_rejected_together(
    cli_runner: CliRunner, store: StoreSpy, services: ServiceSpy
) -> None:
    """Mixing a URL banner into a JSON stream would corrupt the payload."""
    result = cli_runner.invoke(webapp.cli.commands["list"], ["--json", "--open"])

    assert result.exit_code == 2
    assert "cannot be combined" in result.output


# ---------------------------------------------------------------------------
# wasm status --json
# ---------------------------------------------------------------------------


def test_status_json_reports_the_app(
    cli_runner: CliRunner, store: StoreSpy, services: ServiceSpy
) -> None:
    """The structured payload carries the same facts the key/value report shows."""
    store.relations["example.com"] = _relations()

    result = cli_runner.invoke(webapp.cli.commands["status"], ["example.com", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["app"]["domain"] == "example.com"
    assert payload["app"]["type"] == "nextjs"
    assert payload["app"]["site"]["webserver"] == "nginx"
    assert payload["app"]["service"]["name"] == "example-com"
    assert payload["app"]["databases"] == []


def test_status_json_of_an_unknown_app_is_a_plain_error(
    cli_runner: CliRunner, store: StoreSpy, services: ServiceSpy
) -> None:
    """A missing app is still the ordinary error path, not an empty JSON body."""
    services.status = {"exists": False, "active": False, "enabled": False, "name": "example-com"}

    result = cli_runner.invoke(webapp.cli.commands["status"], ["example.com", "--json"])

    assert result.exit_code == 1
    with pytest.raises(json.JSONDecodeError):
        json.loads(result.output)


def test_status_json_reports_a_legacy_app(
    cli_runner: CliRunner, store: StoreSpy, services: ServiceSpy
) -> None:
    """An app deployed before the store existed still gets a structured payload."""
    services.status = {"exists": True, "active": True, "enabled": True, "name": "example-com"}

    result = cli_runner.invoke(webapp.cli.commands["status"], ["example.com", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["app"]["legacy"] is True
    assert payload["app"]["service"] == "example-com"


def test_status_json_before_the_command_name(
    cli_runner: CliRunner, store: StoreSpy, services: ServiceSpy
) -> None:
    """The root's --json also drives 'wasm status'."""
    store.relations["example.com"] = _relations()

    result = cli_runner.invoke(root_cli, ["--json", "status", "example.com"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["app"]["domain"] == "example.com"


# ---------------------------------------------------------------------------
# wasm logs --json
# ---------------------------------------------------------------------------


def test_logs_json_reports_the_lines(
    cli_runner: CliRunner, store: StoreSpy, services: ServiceSpy
) -> None:
    """The journal's own output is split into a lines array, verbatim."""
    services.journal = "line one\nline two"

    result = cli_runner.invoke(webapp.cli.commands["logs"], ["example.com", "--json"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == {"lines": ["line one", "line two"]}


def test_logs_json_and_follow_are_rejected_together(
    cli_runner: CliRunner, store: StoreSpy, services: ServiceSpy
) -> None:
    """A stream has no single JSON payload, so the combination is a usage error."""
    result = cli_runner.invoke(webapp.cli.commands["logs"], ["example.com", "--json", "--follow"])

    assert result.exit_code == 2
    assert "cannot be combined" in result.output


def test_logs_json_and_open_are_rejected_together(
    cli_runner: CliRunner, store: StoreSpy, services: ServiceSpy
) -> None:
    """--json and --open both want stdout for something different."""
    result = cli_runner.invoke(webapp.cli.commands["logs"], ["example.com", "--json", "--open"])

    assert result.exit_code == 2
    assert "cannot be combined" in result.output


def test_logs_json_after_the_domain_argument(
    cli_runner: CliRunner, store: StoreSpy, services: ServiceSpy
) -> None:
    """--json parses after the positional DOMAIN, not just at the very end."""
    services.journal = "one line"

    result = cli_runner.invoke(webapp.cli.commands["logs"], ["--json", "example.com"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == {"lines": ["one line"]}


# ---------------------------------------------------------------------------
# Every leaf command, not only the ones exercised above
# ---------------------------------------------------------------------------


def _leaf_commands() -> list[tuple[str, click.Command]]:
    """
    Walk the real command tree and collect every leaf (non-group) command.

    Every module is imported through :class:`~wasm.cli.app.LazyGroup`, exactly
    as a real invocation would, so this sees the tree as it is actually built,
    aliases resolved to one canonical entry each.

    Returns:
        ``(space-separated path, command)`` pairs, in a stable order.
    """
    leaves: list[tuple[str, click.Command]] = []

    def walk(command: click.Command, path: list[str], parent_ctx: click.Context) -> None:
        if isinstance(command, click.Group):
            ctx = click.Context(
                command, parent=parent_ctx, info_name=command.name or path[-1:] or "cli"
            )
            for name in sorted(command.list_commands(ctx)):
                sub = command.get_command(ctx, name)
                if sub is not None:
                    walk(sub, [*path, name], ctx)
        else:
            leaves.append((" ".join(path), command))

    walk(root_cli, [], click.Context(root_cli))
    return leaves


LEAF_COMMANDS = _leaf_commands()


def _declares_json_option(command: click.Command) -> bool:
    """
    Report whether a command opted into ``--json`` with :func:`json_option`.

    Args:
        command: The command to inspect.

    Returns:
        True when the option is declared, whether or not the command is a
        :class:`~wasm.cli.app.WasmCommand` (``diagnose`` and ``health`` are
        bare ``click.Command``\\ s that declare it directly).
    """
    from wasm.cli.app import _adopt_json

    return any(
        isinstance(param, click.Option) and param.callback is _adopt_json
        for param in command.params
    )


@pytest.mark.parametrize(
    "path", [path for path, _ in LEAF_COMMANDS], ids=[path for path, _ in LEAF_COMMANDS]
)
def test_every_leaf_command_handles_json(path: str) -> None:
    """
    Every leaf command either builds a JSON payload or refuses the flag.

    A command that declares :func:`~wasm.cli.app.json_option` is trusted to
    build a real payload - that behaviour is pinned for each one individually
    above and in every module's own test file. Everything else has to refuse
    ``--json`` outright: constructing the command's context with the shared
    ``Context`` already carrying ``json_output=True`` (rather than parsing
    ``["--json"]``, which would fail on a command's other required arguments
    for a reason that has nothing to do with this) reaches
    :meth:`~wasm.cli.app.WasmCommand.invoke`'s check before any argument
    would ever be read, so this holds regardless of what else the command
    needs.
    """
    command = dict(LEAF_COMMANDS)[path]

    if _declares_json_option(command):
        return

    ctx = click.Context(command, info_name=path, obj=Context(json_output=True))
    with pytest.raises(click.UsageError):
        command.invoke(ctx)


def test_every_leaf_command_without_json_support_is_a_wasm_command() -> None:
    """
    The refusal in the test above only fires through
    :class:`~wasm.cli.app.WasmCommand`. A plain ``click.Command`` that does
    not declare ``json_option`` would accept ``--json`` before its name
    (the root group parses it unconditionally) and silently print for a
    human anyway - the defect this whole file exists to catch. ``diagnose``
    and ``health`` are the only two bare commands in the tree, and both
    declare the option directly.
    """
    offenders = [
        path
        for path, command in LEAF_COMMANDS
        if not _declares_json_option(command) and not isinstance(command, WasmCommand)
    ]

    assert offenders == []
