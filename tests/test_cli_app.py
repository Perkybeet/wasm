# Copyright (c) 2024-2026 Yago Lopez Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
Tests for ``wasm app``.

``migrate`` and ``limits`` decide nothing themselves: the migration is
:mod:`wasm.deployers.migrate` (``tests/test_migrate.py``) and the limits are
the service manager's (``tests/test_resource_limits.py``). Pinned here: what
reaches them, the confirmation before a migration, and that a rehearsal or a
refusal changes nothing.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import pytest
from click.testing import CliRunner, Result

from wasm.cli.app import cli as root_cli
from wasm.cli.commands import app as app_module
from wasm.core.logger import Logger
from wasm.core.runner import set_runner
from wasm.deployers.migrate import MigrationPlan, MigrationResult, TreeCount

DOMAIN = "shop.example.com"
COUNT = TreeCount(files=12, bytes=3400, links=1)
PLAN = MigrationPlan(
    domain=DOMAIN,
    app_path="/var/www/apps/shop-example-com",
    release_id="20260925-120000-aaaaaaa",
    commit="a" * 40,
    persistent=("uploads",),
    persistent_source="git",
    env_files=(".env",),
    unit="shop-example-com",
    unit_rewrite=True,
    site_rewrite=False,
    untracked_files=(),
    warnings=("a warning the operator must see",),
    count=COUNT,
)


@pytest.fixture(autouse=True)
def _real_runner() -> Iterator[None]:
    """--dry-run installs a rehearsing runner process-wide; put the default back."""
    yield
    set_runner(None)


@pytest.fixture
def log(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Collect what the command reports to the operator."""
    lines: list[str] = []
    monkeypatch.setattr(Logger, "_write", lambda self, message, newline=True: lines.append(message))
    return lines


@pytest.fixture
def engine(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Any, ...]]:
    """Stand in for the planner and the migration, recording what reached them."""
    calls: list[tuple[Any, ...]] = []

    def plan(domain: str, persist: Any) -> MigrationPlan:
        calls.append(("plan", domain, persist))
        return PLAN

    def run(domain: str, plan: MigrationPlan, **kwargs: Any) -> MigrationResult:
        calls.append(("migrate", domain, kwargs["trigger"]))
        from wasm.core.fs import is_rehearsal

        return MigrationResult(
            domain=domain,
            release_id="" if is_rehearsal() else plan.release_id,
            persistent=plan.persistent,
            env_files=plan.env_files,
            before=COUNT,
            after=COUNT,
            unit_rewritten=not is_rehearsal(),
            site_rewritten=False,
            rehearsed=is_rehearsal(),
            deployment_id=None,
        )

    monkeypatch.setattr(app_module, "plan_migration", plan)
    monkeypatch.setattr(app_module, "migrate", run)
    return calls


def invoke(args: list[str], stdin: str | None = None) -> Result:
    """Run the real root group."""
    return CliRunner().invoke(root_cli, args, input=stdin)


def test_migrate_shows_the_plan_and_stops_when_not_confirmed(
    engine: list[tuple[Any, ...]], log: list[str]
) -> None:
    """Nothing runs until the operator says yes."""
    result = invoke(["app", "migrate", DOMAIN], stdin="n\n")

    assert result.exit_code != 0
    assert engine == [("plan", DOMAIN, None)]
    assert any("a warning the operator must see" in line for line in log)
    assert any("uploads" in line for line in log)


def test_migrate_with_yes_runs_as_the_cli_with_the_named_paths(
    engine: list[tuple[Any, ...]], log: list[str]
) -> None:
    """--persist replaces detection; the trigger is cli."""
    result = invoke(
        ["app", "migrate", DOMAIN, "--persist", "uploads", "--persist", "data", "--yes"]
    )

    assert result.exit_code == 0, result.output
    assert engine == [("plan", DOMAIN, ["uploads", "data"]), ("migrate", DOMAIN, "cli")]
    assert any(f"runs from release {PLAN.release_id}" in line for line in log)


def test_a_rehearsed_migration_asks_nothing_and_says_nothing_changed(
    engine: list[tuple[Any, ...]], log: list[str]
) -> None:
    """--dry-run needs no confirmation: it cannot change anything."""
    result = invoke(["--dry-run", "app", "migrate", DOMAIN])

    assert result.exit_code == 0, result.output
    assert engine[-1] == ("migrate", DOMAIN, "cli")
    assert any("nothing was changed" in line for line in log)


def test_json_without_yes_prints_the_plan_and_changes_nothing(
    engine: list[tuple[Any, ...]],
) -> None:
    """A script gets the plan; acting needs --yes."""
    result = invoke(["--json", "app", "migrate", DOMAIN])

    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["plan"]["persistent"] == ["uploads"]
    assert [call[0] for call in engine] == ["plan"]


# ---------------------------------------------------------------------------
# wasm app limits
# ---------------------------------------------------------------------------


@pytest.fixture
def limited(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> list[tuple[Any, ...]]:
    """An application with a memory limit, and the lifecycle call recorded."""
    from wasm.core.store import App, WASMStore
    from wasm.deployers.lifecycle import LimitsChange

    WASMStore.reset_instance()
    store = WASMStore(tmp_path / "wasm.db")
    store.create_app(App(domain=DOMAIN, app_type="nodejs", memory_max_mb=512, tasks_max=100))
    monkeypatch.setattr(app_module, "get_store", lambda: store)
    calls: list[tuple[Any, ...]] = []

    def apply(domain: str, limits: Any, *, restart: bool) -> LimitsChange:
        calls.append((domain, limits, restart))
        return LimitsChange(domain=domain, limits=limits, units=("shop",), restarted=restart)

    monkeypatch.setattr(app_module, "set_resource_limits", apply)
    yield calls
    WASMStore.reset_instance()


def test_limits_not_named_keep_their_value_and_none_removes_one(
    limited: list[tuple[Any, ...]], log: list[str]
) -> None:
    """--cpu 50% adds one, --tasks none removes one, the memory limit stays."""
    from wasm.managers.service_manager import ResourceLimits

    result = invoke(["app", "limits", DOMAIN, "--cpu", "50%", "--tasks", "none", "--restart"])

    assert result.exit_code == 0, result.output
    assert limited == [
        (DOMAIN, ResourceLimits(memory_max_mb=512, cpu_quota_percent=50, tasks_max=None), True)
    ]
    assert any("MemoryMax=512M, CPUQuota=50%" in line for line in log)


@pytest.mark.parametrize(
    ("value", "megabytes"), [("512M", 512), ("512", 512), ("2G", 2048), ("1gb", 1024)]
)
def test_memory_is_read_as_operators_write_it(
    limited: list[tuple[Any, ...]], value: str, megabytes: int
) -> None:
    """Megabytes by default, gigabytes with G."""
    assert invoke(["app", "limits", DOMAIN, "--memory", value]).exit_code == 0
    assert limited[-1][1].memory_max_mb == megabytes


def test_without_options_the_limits_are_shown_and_nothing_changes(
    limited: list[tuple[Any, ...]], log: list[str]
) -> None:
    """Reading is the default."""
    result = invoke(["app", "limits", DOMAIN])

    assert result.exit_code == 0, result.output
    assert limited == []
    assert any("MemoryMax=512M, TasksMax=100" in line for line in log)


def test_a_value_that_is_not_a_size_is_a_usage_error(limited: list[tuple[Any, ...]]) -> None:
    """Refused by Click before anything is asked of the machine."""
    result = invoke(["app", "limits", DOMAIN, "--memory", "lots"])

    assert result.exit_code == 2
    assert "512M, 2G or none" in result.output
    assert limited == []


# ---------------------------------------------------------------------------
# wasm app health
# ---------------------------------------------------------------------------


@pytest.fixture
def checked(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> Any:
    """An application with a health path, in a store the command and lifecycle share."""
    from wasm.core.store import App, WASMStore
    from wasm.deployers import lifecycle

    WASMStore.reset_instance()
    store = WASMStore(tmp_path / "wasm.db")
    store.create_app(App(domain=DOMAIN, app_type="nodejs", port=3100, app_path=str(tmp_path)))
    store.set_app_health(DOMAIN, path="/healthz", expect=None, timeout=None)
    monkeypatch.setattr(app_module, "get_store", lambda: store)
    monkeypatch.setattr(lifecycle, "get_store", lambda: store)
    yield store
    WASMStore.reset_instance()


def test_health_without_options_shows_the_settings_and_their_defaults(
    checked: Any, log: list[str]
) -> None:
    result = invoke(["app", "health", DOMAIN])

    assert result.exit_code == 0, result.output
    text = "\n".join(log)
    assert "/healthz" in text
    assert "below 500" in text
    assert "30" in text


def test_health_options_not_named_keep_their_value(checked: Any) -> None:
    result = invoke(["app", "health", DOMAIN, "--expect", "200-399", "--timeout", "90"])

    assert result.exit_code == 0, result.output
    app = checked.get_app(DOMAIN)
    assert (app.health_path, app.health_expect, app.health_timeout) == ("/healthz", "200-399", 90)


def test_health_reset_goes_back_to_the_defaults(checked: Any) -> None:
    result = invoke(["app", "health", DOMAIN, "--reset"])

    assert result.exit_code == 0, result.output
    app = checked.get_app(DOMAIN)
    assert (app.health_path, app.health_expect, app.health_timeout) == (None, None, None)


def test_health_json_prints_the_settings(checked: Any) -> None:
    result = invoke(["--json", "app", "health", DOMAIN, "--timeout", "45"])

    assert result.exit_code == 0, result.output
    body = json.loads(result.output)
    assert body["domain"] == DOMAIN
    assert (body["path"], body["expect"], body["timeout"]) == ("/healthz", None, 45)
    assert body["effective"] == {
        "path": "/healthz",
        "expect": "any status below 500",
        "timeout": 45,
    }


def test_health_refuses_a_bad_value_and_changes_nothing(checked: Any, log: list[str]) -> None:
    from wasm.cli.app import main

    assert main(["app", "health", DOMAIN, "--path", "http://evil.example.com/"]) == 1
    assert checked.get_app(DOMAIN).health_path == "/healthz"


def test_health_reset_with_a_value_is_a_usage_error(checked: Any) -> None:
    result = invoke(["app", "health", DOMAIN, "--reset", "--timeout", "10"])

    assert result.exit_code == 2
