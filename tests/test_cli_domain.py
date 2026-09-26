# Copyright (c) 2024-2026 Yago Lopez Prado
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Tests for ``wasm domain``.

The command decides nothing: :mod:`wasm.deployers.domains` does, and is
covered in ``tests/test_domains.py``. Pinned here is the translation: the
arguments that reach it, ``--json`` on either side of ``list``, the DNS
warning before a name is added, certbot's words when an order fails, and a
refusal becoming exit code 1 with the operation's own words.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from click.testing import CliRunner, Result

from wasm.cli.app import cli as root_cli
from wasm.cli.app import main
from wasm.cli.commands import domain as domain_module
from wasm.core.exceptions import DependencyError, DomainError
from wasm.core.logger import Logger
from wasm.core.store import DomainRecord
from wasm.deployers.domains import DnsCheck, DomainChange

APP = "example.com"
PRIMARY = DomainRecord(id=1, app_id=1, domain=APP, kind="primary", created_at="2026-09-25")
ALIAS = DomainRecord(id=2, app_id=1, domain="shop.example.com", kind="alias", created_at="t1")
HERE = DnsCheck("shop.example.com", ("203.0.113.5",), ("203.0.113.5",), True)
ELSEWHERE = DnsCheck("shop.example.com", ("203.0.113.5",), ("198.51.100.7",), False)


@pytest.fixture
def log(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Collect what the command reports to the operator."""
    lines: list[str] = []
    monkeypatch.setattr(Logger, "_write", lambda self, message, newline=True: lines.append(message))
    return lines


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, tuple[Any, ...], dict[str, Any]]]:
    """Stub the operations, recording how they were called."""
    recorded: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
    change = DomainChange(app=APP, domains=(PRIMARY, ALIAS), tls=False)

    def stub(name: str, result: Any) -> Any:
        def fake(*args: Any, **kwargs: Any) -> Any:
            recorded.append((name, args, kwargs))
            return result

        return fake

    monkeypatch.setattr(domain_module, "list_domains", stub("list", [PRIMARY, ALIAS]))
    monkeypatch.setattr(domain_module, "add_domain", stub("add", change))
    monkeypatch.setattr(domain_module, "remove_domain", stub("remove", change))
    monkeypatch.setattr(domain_module, "check_dns", stub("dns", HERE))
    return recorded


def invoke(args: list[str]) -> Result:
    """Run the real root group."""
    return CliRunner().invoke(root_cli, args)


@pytest.mark.parametrize(
    "args", [["domain", "list", APP, "--json"], ["--json", "domain", "list", APP]]
)
def test_list_prints_json_on_either_side_of_the_command(calls: list[Any], args: list[str]) -> None:
    result = invoke(args)

    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == {
        "app": APP,
        "items": [
            {"domain": APP, "kind": "primary", "created_at": "2026-09-25"},
            {"domain": "shop.example.com", "kind": "alias", "created_at": "t1"},
        ],
    }
    assert calls == [("list", (APP,), {})]


def test_list_shows_a_table_for_a_human(
    calls: list[Any], log: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    tables: list[tuple[list[Any], list[Any]]] = []
    monkeypatch.setattr(
        Logger, "table", lambda self, headers, rows, justify=None: tables.append((headers, rows))
    )

    result = invoke(["domain", "list", APP])

    assert result.exit_code == 0, result.output
    assert [row[:2] for row in tables[0][1]] == [[APP, "primary"], ["shop.example.com", "alias"]]


@pytest.mark.parametrize(
    ("extra", "kind", "issue_cert"),
    [
        ([], "alias", True),
        (["--kind", "redirect"], "redirect", True),
        (["--no-cert"], "alias", False),
    ],
)
def test_add_reaches_the_operation_with_what_was_asked(
    calls: list[Any], log: list[str], extra: list[str], kind: str, issue_cert: bool
) -> None:
    result = invoke(["domain", "add", APP, "shop.example.com", *extra])

    assert result.exit_code == 0, result.output
    (add,) = [call for call in calls if call[0] == "add"]
    assert add[1] == (APP, "shop.example.com", kind)
    assert add[2]["issue_cert"] is issue_cert


def test_add_warns_when_the_name_does_not_resolve_here(
    calls: list[Any], log: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(domain_module, "check_dns", lambda name: ELSEWHERE)

    result = invoke(["domain", "add", APP, "shop.example.com"])

    assert result.exit_code == 0, result.output
    text = "\n".join(log)
    assert "does not resolve to this server" in text
    assert "198.51.100.7" in text
    assert "203.0.113.5" in text


def test_add_goes_ahead_when_the_addresses_cannot_be_listed(
    calls: list[Any], log: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_psutil(name: str) -> DnsCheck:
        raise DependencyError("psutil is needed to list this server's addresses")

    monkeypatch.setattr(domain_module, "check_dns", no_psutil)

    result = invoke(["domain", "add", APP, "shop.example.com"])

    assert result.exit_code == 0, result.output
    assert [call[0] for call in calls] == ["add"]


def test_a_failed_order_shows_certbot_verbatim_and_how_to_retry(
    calls: list[Any], log: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    change = DomainChange(
        app=APP,
        domains=(PRIMARY, ALIAS),
        tls=True,
        certificate_error="Challenge failed for domain shop.example.com",
    )
    monkeypatch.setattr(domain_module, "add_domain", lambda *a, **k: change)

    result = invoke(["domain", "add", APP, "shop.example.com"])

    assert result.exit_code == 0, result.output
    text = "\n".join(log)
    assert "Challenge failed for domain shop.example.com" in text
    assert f"wasm domain add {APP} shop.example.com" in text


def test_remove_reaches_the_operation(calls: list[Any], log: list[str]) -> None:
    result = invoke(["domain", "remove", APP, "shop.example.com"])

    assert result.exit_code == 0, result.output
    assert [call[:2] for call in calls] == [("remove", (APP, "shop.example.com"))]


def test_removing_from_a_tls_site_says_nothing_was_revoked(
    log: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    change = DomainChange(app=APP, domains=(PRIMARY,), tls=True)
    monkeypatch.setattr(domain_module, "remove_domain", lambda *a, **k: change)

    result = invoke(["domain", "remove", APP, "shop.example.com"])

    assert result.exit_code == 0, result.output
    assert any("not revoked" in line for line in log)


def test_a_refusal_exits_1_with_the_operations_words(
    log: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(*args: Any, **kwargs: Any) -> None:
        raise DomainError(
            "example.com is the primary domain and cannot be removed",
            details="Delete the application to stop serving it: wasm delete example.com",
        )

    monkeypatch.setattr(domain_module, "remove_domain", refuse)

    assert main(["domain", "remove", APP, APP]) == 1
    assert any("cannot be removed" in line for line in log)
    assert any("wasm delete example.com" in line for line in log)
