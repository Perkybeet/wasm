# Copyright (c) 2024-2026 Yago Lopez Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
Tests for ``wasm releases``.

The command decides nothing: listing and activating are
:func:`wasm.deployers.lifecycle.list_releases` and
:func:`~wasm.deployers.lifecycle.activate_release`, covered in
``tests/test_release_activation.py``. Pinned here is the translation: the
arguments that reach the lifecycle (trigger ``cli`` included), ``--json`` on
either side of the command name, and a refusal becoming exit code 1 with the
lifecycle's own words.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner, Result

from wasm.cli.app import cli as root_cli
from wasm.cli.app import main
from wasm.cli.commands import releases as releases_module
from wasm.core.exceptions import DeploymentError
from wasm.core.logger import Logger
from wasm.deployers.lifecycle import ReleaseActivation, ReleaseInfo
from wasm.deployers.releases import Release

DOMAIN = "rel.example.com"
OLD = "20260925-100000-aaaaaaa"
NEW = "20260925-110000-bbbbbbb"


def release(release_id: str, *, active: bool) -> Release:
    """A release as ReleaseManager describes one."""
    return Release(
        id=release_id,
        path=Path("/var/www/apps/rel-example-com/releases") / release_id,
        commit=release_id.rsplit("-", 1)[1],
        created_at="2026-09-25T10:00:00+00:00",
        active=active,
    )


LISTED = [
    ReleaseInfo(
        id=NEW,
        commit="bbbbbbb",
        created_at="2026-09-25T11:00:00+00:00",
        activated_at="2026-09-25T11:00:05",
        status="active",
        active=True,
        on_disk=True,
    ),
    ReleaseInfo(
        id=OLD,
        commit="aaaaaaa",
        created_at="2026-09-25T10:00:00+00:00",
        activated_at="2026-09-25T10:00:05",
        status="superseded",
        active=False,
        on_disk=True,
    ),
]


@pytest.fixture
def log(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Collect what the command reports to the operator."""
    lines: list[str] = []
    monkeypatch.setattr(Logger, "_write", lambda self, message, newline=True: lines.append(message))
    return lines


def invoke(args: list[str]) -> Result:
    """Run the real root group."""
    return CliRunner().invoke(root_cli, args)


@pytest.mark.parametrize(
    "args", [["releases", "list", DOMAIN, "--json"], ["--json", "releases", "list", DOMAIN]]
)
def test_list_prints_json_on_either_side_of_the_command(
    monkeypatch: pytest.MonkeyPatch, args: list[str]
) -> None:
    """The items are the lifecycle's, field for field."""
    asked: list[str] = []
    monkeypatch.setattr(
        releases_module, "list_releases", lambda domain: asked.append(domain) or LISTED
    )

    result = invoke(args)

    assert result.exit_code == 0, result.output
    assert asked == [DOMAIN]
    body = json.loads(result.output)
    assert body["domain"] == DOMAIN
    assert [(item["id"], item["active"], item["status"]) for item in body["items"]] == [
        (NEW, True, "active"),
        (OLD, False, "superseded"),
    ]


def test_list_marks_the_active_release_for_a_human(
    monkeypatch: pytest.MonkeyPatch, log: list[str]
) -> None:
    """The table names every release and says how to go back."""
    monkeypatch.setattr(releases_module, "list_releases", lambda domain: LISTED)
    tables: list[tuple[list[Any], list[Any]]] = []
    monkeypatch.setattr(
        Logger, "table", lambda self, headers, rows, justify=None: tables.append((headers, rows))
    )

    result = invoke(["releases", "list", DOMAIN])

    assert result.exit_code == 0, result.output
    rows = tables[0][1]
    assert [row[:2] for row in rows] == [["*", NEW], ["", OLD]]
    assert any(f"wasm releases rollback {DOMAIN}" in line for line in log)


@pytest.mark.parametrize(("extra", "expected"), [([], None), ([OLD], OLD)])
def test_rollback_asks_the_lifecycle_as_the_cli(
    monkeypatch: pytest.MonkeyPatch, log: list[str], extra: list[str], expected: str | None
) -> None:
    """Without an id, the one before the active one; the trigger is always cli."""
    calls: list[tuple[str, str | None, str]] = []

    def activate(domain: str, release_id: str | None = None, **kwargs: Any) -> ReleaseActivation:
        calls.append((domain, release_id, kwargs["trigger"]))
        return ReleaseActivation(
            domain=domain,
            release=release(OLD, active=True),
            previous=release(NEW, active=False),
            changed=True,
            went_back=True,
            deployment_id=3,
        )

    monkeypatch.setattr(releases_module, "activate_release", activate)

    result = invoke(["releases", "rollback", DOMAIN, *extra])

    assert result.exit_code == 0, result.output
    assert calls == [(DOMAIN, expected, "cli")]
    assert any(f"Rolled back to release {OLD}" in line for line in log)
    assert any(f"wasm releases rollback {DOMAIN} {NEW}" in line for line in log)


def test_a_refused_rollback_exits_1_with_the_lifecycle_words(
    monkeypatch: pytest.MonkeyPatch, log: list[str]
) -> None:
    """The refusal and its fix, verbatim, and a failing exit code for scripts."""

    def refuse(domain: str, release_id: str | None = None, **kwargs: Any) -> None:
        raise DeploymentError(
            f"{domain} is deployed in place and has no releases",
            details=f"Move it onto releases with 'wasm app migrate {domain}'.",
        )

    monkeypatch.setattr(releases_module, "activate_release", refuse)

    assert main(["releases", "rollback", DOMAIN]) == 1
    assert any("deployed in place" in line for line in log)
    assert any("wasm app migrate" in line for line in log)
