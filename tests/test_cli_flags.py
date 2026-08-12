# Copyright (c) 2024-2025 Yago López Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
Tests for the CLI flag plumbing.

These tests exercise :func:`wasm.cli.parser.parse_args`, which is pure: it never
touches the filesystem nor spawns processes. The routing tests only inspect the
``func`` default that argparse attaches to the namespace, so no handler ever
runs.
"""

from __future__ import annotations

import argparse
import io
import json
from collections.abc import Iterator
from typing import TextIO

import pytest

from wasm.cli.parser import create_parser, parse_args
from wasm.core import logger as logger_module
from wasm.core.logger import (
    Logger,
    OutputFormat,
    Presenter,
    colors_enabled,
    set_colors_disabled,
)


class _FakeTTY(io.StringIO):
    """A stream that claims to be a terminal."""

    def isatty(self) -> bool:
        """
        Report the stream as a terminal.

        Returns:
            Always True.
        """
        return True


def _iter_parsers(parser: argparse.ArgumentParser) -> Iterator[argparse.ArgumentParser]:
    """
    Walk the whole parser tree, yielding every parser exactly once.

    Args:
        parser: Root parser to walk.

    Yields:
        The root parser and every (transitively) nested subparser. Parsers
        registered under several aliases are yielded once.
    """
    yield parser
    seen: set[int] = set()
    for action in parser._actions:
        if not isinstance(action, argparse._SubParsersAction):
            continue
        for subparser in action.choices.values():
            if id(subparser) in seen:
                continue
            seen.add(id(subparser))
            yield from _iter_parsers(subparser)


def _global_dests(root: argparse.ArgumentParser) -> set[str]:
    """
    Collect the destinations declared by the root parser's global flags.

    Args:
        root: The root parser.

    Returns:
        The set of dests owned by global flags (the subcommand dest excluded).
    """
    return {
        action.dest
        for action in root._actions
        if action.dest is not argparse.SUPPRESS
        and not isinstance(action, argparse._SubParsersAction)
    }


# ---------------------------------------------------------------------------
# Global flag shadowing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "argv",
    [
        ["--dry-run", "monitor", "scan"],
        ["--dry-run", "mon", "scan"],
        ["--dry-run", "cert", "create", "-d", "example.com"],
        ["--dry-run", "cert", "renew"],
        ["--dry-run", "site", "create", "-d", "example.com"],
        ["--dry-run", "delete", "example.com"],
        ["--dry-run", "backup", "create", "example.com"],
    ],
)
def test_global_dry_run_survives_subcommands(argv: list[str]) -> None:
    """A global --dry-run must never be reset to False by a subparser default."""
    assert parse_args(argv).dry_run is True


@pytest.mark.parametrize(
    "argv",
    [
        ["monitor", "scan", "--dry-run"],
        ["cert", "create", "-d", "example.com", "--dry-run"],
        ["cert", "renew", "--dry-run"],
        ["site", "create", "-d", "example.com", "--dry-run"],
    ],
)
def test_dry_run_still_accepted_after_the_subcommand(argv: list[str]) -> None:
    """The flag keeps working when written after the subcommand."""
    assert parse_args(argv).dry_run is True


def test_dry_run_defaults_to_false() -> None:
    """Without the flag, dry-run stays off."""
    assert parse_args(["monitor", "scan"]).dry_run is False


@pytest.mark.parametrize(
    "argv",
    [
        ["--verbose", "monitor", "scan"],
        ["--verbose", "site", "list"],
        ["--verbose", "db", "status"],
        ["--verbose", "backup", "list"],
    ],
)
def test_global_verbose_survives_subcommands(argv: list[str]) -> None:
    """A global --verbose must reach the handler namespace."""
    assert parse_args(argv).verbose is True


def test_no_subparser_shadows_a_global_dest() -> None:
    """
    Regression guard: no subparser may redefine a global flag with a default.

    A subparser is allowed to re-offer a global flag (that is how ``wasm site
    create --dry-run`` works), but only with ``argparse.SUPPRESS`` as default,
    so an unused flag leaves the value parsed by the root parser alone.
    """
    root = create_parser()
    globals_ = _global_dests(root)
    offenders: list[str] = []

    for parser in list(_iter_parsers(root))[1:]:
        for action in parser._actions:
            if action.dest in globals_ and action.default is not argparse.SUPPRESS:
                offenders.append(f"{parser.prog}: {action.dest}={action.default!r}")

    assert offenders == []


def test_every_global_flag_is_available_on_leaf_parsers() -> None:
    """Propagated flags are present all the way down the tree."""
    root = create_parser()
    site_create = root.parse_args(["site", "create", "-d", "x.com"])
    assert site_create.dry_run is False
    assert site_create.verbose is False
    assert site_create.no_color is False


# ---------------------------------------------------------------------------
# --json
# ---------------------------------------------------------------------------


def test_json_is_not_a_global_flag() -> None:
    """
    The root parser must not advertise --json.

    Only a handful of subcommands build a structured payload; a global flag
    would promise machine-readable output for the ~80 that do not.
    """
    root = create_parser()
    assert [action.dest for action in root._actions if action.dest == "json"] == []

    with pytest.raises(SystemExit) as excinfo:
        parse_args(["--json", "list"])
    assert excinfo.value.code != 0


@pytest.mark.parametrize(
    "argv",
    [
        ["backup", "list", "--json"],
        ["backup", "storage", "--json"],
        ["db", "status", "--json"],
        ["store", "stats", "--json"],
    ],
)
def test_json_stays_on_the_commands_that_implement_it(argv: list[str]) -> None:
    """Subcommands that really emit JSON keep their flag."""
    assert parse_args(argv).json is True


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("argv", "handler_name"),
    [
        (["site", "list"], "handle_site"),
        (["service", "list"], "handle_service"),
        (["svc", "list"], "handle_service"),
        (["cert", "list"], "handle_cert"),
        (["ssl", "list"], "handle_cert"),
        (["certificate", "list"], "handle_cert"),
        (["monitor", "status"], "handle_monitor"),
        (["mon", "status"], "handle_monitor"),
        (["backup", "list"], "handle_backup"),
        (["bak", "list"], "handle_backup"),
        (["rollback", "example.com"], "handle_rollback"),
        (["rb", "example.com"], "handle_rollback"),
        (["db", "status"], "handle_db"),
        (["database", "status"], "handle_db"),
        (["setup", "init"], "handle_setup"),
        (["web", "status"], "handle_web"),
        (["env", "show", "example.com"], "handle_env"),
        (["store", "stats"], "handle_store"),
        (["config", "show"], "handle_config"),
        (["health"], "handle_health"),
        (["list"], "handle_webapp"),
        (["ls"], "handle_webapp"),
        (["status", "example.com"], "handle_webapp"),
        (["logs", "example.com"], "handle_webapp"),
        (["rm", "example.com"], "handle_webapp"),
    ],
)
def test_commands_route_to_their_handler(argv: list[str], handler_name: str) -> None:
    """Every command and alias carries the handler argparse should dispatch to."""
    args = parse_args(argv)
    assert args.func.__name__ == handler_name


@pytest.mark.parametrize(
    ("argv", "expected_action"),
    [
        (["list"], "list"),
        (["ls"], "list"),
        (["new", "-d", "x.com", "-s", "/tmp/x"], "create"),
        (["deploy", "-d", "x.com", "-s", "/tmp/x"], "create"),
        (["upgrade", "example.com"], "update"),
        (["remove", "example.com"], "delete"),
        (["info", "example.com"], "status"),
    ],
)
def test_webapp_aliases_set_a_known_action(argv: list[str], expected_action: str) -> None:
    """Top-level webapp commands still hand an action to handle_webapp."""
    from wasm.cli.commands.webapp import handle_webapp

    args = parse_args(argv)
    assert args.func is handle_webapp
    assert args.action == expected_action


def test_backup_without_action_defaults_to_list() -> None:
    """`wasm backup` keeps listing backups instead of erroring."""
    args = parse_args(["backup"])
    assert args.func.__name__ == "handle_backup"
    assert args.action == "list"


def test_backup_action_overrides_the_default() -> None:
    """An explicit backup action wins over the list default."""
    assert parse_args(["backup", "create", "example.com"]).action == "create"


@pytest.mark.parametrize(
    "command",
    ["site", "service", "cert", "setup", "monitor", "web", "db", "env", "store"],
)
def test_command_without_action_exits_with_an_error(
    command: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """argparse itself reports the missing action, with a usable message."""
    with pytest.raises(SystemExit) as excinfo:
        parse_args([command])

    assert excinfo.value.code != 0
    stderr = capsys.readouterr().err
    assert "action" in stderr
    assert "--help" in stderr or "usage" in stderr


def test_no_command_leaves_func_unset() -> None:
    """Bare `wasm` has nothing to dispatch, so main can print help."""
    args = parse_args([])
    assert args.command is None
    assert getattr(args, "func", None) is None


# ---------------------------------------------------------------------------
# Color handling
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_color_override() -> Iterator[None]:
    """Keep the process-wide color override from leaking between tests."""
    set_colors_disabled(False)
    yield
    set_colors_disabled(False)


def _tty() -> TextIO:
    """
    Build a stream that pretends to be a terminal.

    Returns:
        A writable stream whose isatty() is True.
    """
    return _FakeTTY()


def test_colors_enabled_on_a_tty(monkeypatch: pytest.MonkeyPatch) -> None:
    """A terminal without NO_COLOR gets colors."""
    monkeypatch.delenv("NO_COLOR", raising=False)
    stream = _tty()
    assert colors_enabled(stream) is True
    assert Logger(stream=stream).no_color is False


def test_no_color_env_disables_colors(monkeypatch: pytest.MonkeyPatch) -> None:
    """The NO_COLOR convention (https://no-color.org) is honoured."""
    monkeypatch.setenv("NO_COLOR", "1")
    stream = _tty()
    assert colors_enabled(stream) is False
    logger = Logger(stream=stream)
    assert logger.no_color is True
    logger.success("done")
    assert "\033[" not in stream.getvalue()


def test_empty_no_color_env_is_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    """An empty NO_COLOR must not disable colors, per the convention."""
    monkeypatch.setenv("NO_COLOR", "")
    assert colors_enabled(_tty()) is True


def test_non_tty_disables_colors(monkeypatch: pytest.MonkeyPatch) -> None:
    """Redirected output stays free of escape codes."""
    monkeypatch.delenv("NO_COLOR", raising=False)
    stream = io.StringIO()
    assert colors_enabled(stream) is False
    logger = Logger(stream=stream)
    assert logger.no_color is True
    logger.error("boom")
    assert "\033[" not in stream.getvalue()


def test_set_colors_disabled_applies_to_new_loggers(monkeypatch: pytest.MonkeyPatch) -> None:
    """The --no-color override reaches loggers built later by the handlers."""
    monkeypatch.delenv("NO_COLOR", raising=False)
    set_colors_disabled(True)
    stream = _tty()
    logger = Logger(stream=stream)
    assert logger.no_color is True
    logger.info("hello")
    assert "\033[" not in stream.getvalue()


def test_main_connects_no_color_to_the_logger(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`wasm --no-color` must actually disable colors for every handler."""
    monkeypatch.delenv("NO_COLOR", raising=False)
    from wasm.main import main

    assert main(["--no-color"]) == 0
    capsys.readouterr()
    assert logger_module.colors_enabled(_tty()) is False


def test_main_without_no_color_leaves_colors_alone(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """No flag, no override."""
    monkeypatch.delenv("NO_COLOR", raising=False)
    from wasm.main import main

    assert main([]) == 0
    capsys.readouterr()
    assert logger_module.colors_enabled(_tty()) is True


# ---------------------------------------------------------------------------
# Presentation layer
# ---------------------------------------------------------------------------


def test_presenter_text_mode_writes_human_output() -> None:
    """Text mode goes through the logger."""
    stream = io.StringIO()
    presenter = Presenter(Logger(stream=stream), OutputFormat.TEXT, stream)

    presenter.emit({"domain": "example.com", "status": "running"})

    output = stream.getvalue()
    assert "domain: example.com" in output
    assert "status: running" in output


def test_presenter_json_mode_writes_one_json_document() -> None:
    """JSON mode emits a payload a script can parse."""
    stream = io.StringIO()
    presenter = Presenter(Logger(stream=stream), OutputFormat.JSON, stream)

    presenter.emit({"domain": "example.com", "port": 3000})

    assert json.loads(stream.getvalue()) == {"domain": "example.com", "port": 3000}


def test_presenter_json_table_uses_headers_as_keys() -> None:
    """Rows become objects keyed by the column headers."""
    stream = io.StringIO()
    presenter = Presenter(Logger(stream=stream), OutputFormat.JSON, stream)

    presenter.emit_table(["domain", "port"], [["a.com", 3000], ["b.com", 3001]])

    assert json.loads(stream.getvalue()) == {
        "items": [
            {"domain": "a.com", "port": 3000},
            {"domain": "b.com", "port": 3001},
        ]
    }


def test_presenter_json_error_is_structured() -> None:
    """Errors stay machine readable in JSON mode."""
    stream = io.StringIO()
    presenter = Presenter(Logger(stream=stream), OutputFormat.JSON, stream)

    presenter.emit_error("nginx reload failed", details="run nginx -t")

    assert json.loads(stream.getvalue()) == {
        "error": "nginx reload failed",
        "details": "run nginx -t",
    }
