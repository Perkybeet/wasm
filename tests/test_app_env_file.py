# Copyright (c) 2024-2026 Yago Lopez Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
Tests for where an application's environment lives once it is deployed.

The reported defect: ``wasm create --env-file`` and ``POST /api/apps`` with
``env_vars`` wrote every variable into the unit's ``Environment=``. Unit
files are 0644 and ``systemctl show`` prints ``Environment=`` to any local
user, so a DATABASE_URL given at create time was readable by every account on
the machine. The variables now go to the application's env file (0600, the one
:func:`~wasm.deployers.helpers.layout.env_file_for` names) and the unit points
at it with ``EnvironmentFile=``; only the non-secret values the unit decides
itself (PORT, NODE_ENV) stay inline.

``EnvironmentFile=`` overrides ``Environment=`` in systemd, the opposite of
what dotenv did when the application read ``.env`` itself, so these tests also
pin that the env file WASM writes never carries a PORT that would move the
application off the port its site proxies to.
"""

from __future__ import annotations

import stat
from pathlib import Path
from typing import Any

import pytest

from tests.test_deployers import (  # noqa: F401 - store is a fixture
    FakeServiceManager,
    build_deployer,
    store,
)
from wasm.cli.commands.webapp import _read_env_file
from wasm.core.fs import SECRET_MODE, RecordingFileSystem
from wasm.core.logger import Logger
from wasm.core.runner import FakeRunner
from wasm.core.store import MonorepoWorkspace, Service, WASMStore
from wasm.deployers.helpers.env_manager import EnvManager
from wasm.deployers.helpers.layout import RELEASES
from wasm.deployers.monorepo import MonorepoDeployer
from wasm.deployers.nodejs import NodeJSDeployer
from wasm.deployers.static import StaticDeployer
from wasm.validators.environment import EnvironmentValidationError

SECRET_URL = "postgres://app:s3cret@db.internal/app"


def _deployer(tmp_path: Path, **env_vars: str) -> Any:
    """
    Build a Node deployer whose fetch only creates the directory.

    Args:
        tmp_path: Directory the application is deployed into.
        **env_vars: Variables given at create time.

    Returns:
        The deployer, with ``warnings`` collecting what it warned about.
    """
    deployer = build_deployer(NodeJSDeployer, tmp_path)
    deployer.env_vars = dict(env_vars)
    deployer.fetch_source = lambda: deployer.app_path.mkdir(parents=True, exist_ok=True) or True
    deployer._resolve_absolute_path = lambda command: command
    deployer.warnings = []
    deployer.logger.warning = deployer.warnings.append
    return deployer


def _env_file(tmp_path: Path) -> Path:
    """
    Args:
        tmp_path: Directory the application is deployed into.

    Returns:
        Where an in-place application keeps its ``.env``.
    """
    return tmp_path / "app" / ".env"


# ---------------------------------------------------------------------------
# Create-time variables
# ---------------------------------------------------------------------------


def test_create_time_variables_land_in_the_env_file_not_the_unit(
    tmp_path: Path,
    store: WASMStore,  # noqa: F811 - the fixture imported above
) -> None:
    deployer = _deployer(tmp_path, DATABASE_URL=SECRET_URL, API_KEY="k-123")

    deployer._step_fetch()
    deployer.create_service()

    env_file = _env_file(tmp_path)
    assert EnvManager().read_env_file(env_file) == {"DATABASE_URL": SECRET_URL, "API_KEY": "k-123"}
    assert stat.S_IMODE(env_file.stat().st_mode) == SECRET_MODE

    unit = deployer.services.units["app-example-com"]
    assert unit["environment"] == {"PORT": "3000", "NODE_ENV": "production"}
    assert unit["environment_file"] == str(env_file)
    # The store's copy of the unit's environment must not become the second
    # place the secret lives.
    assert SECRET_URL not in str(store.get_service("app-example-com").environment)


def test_values_already_in_the_env_file_are_kept_and_create_time_ones_win(
    tmp_path: Path,
    store: WASMStore,  # noqa: F811
) -> None:
    deployer = _deployer(tmp_path, DATABASE_URL=SECRET_URL)
    env_file = _env_file(tmp_path)
    env_file.parent.mkdir(parents=True)
    env_file.write_text("DATABASE_URL=postgres://old\nSESSION_SECRET=keep-me\n")

    deployer._step_fetch()

    assert EnvManager().read_env_file(env_file) == {
        "DATABASE_URL": SECRET_URL,
        "SESSION_SECRET": "keep-me",
    }


def test_the_unit_s_own_variables_are_not_written_to_the_env_file(
    tmp_path: Path,
    store: WASMStore,  # noqa: F811
) -> None:
    """
    systemd lets ``EnvironmentFile=`` override ``Environment=``: a PORT in the
    file would move the application off the port its site proxies to.
    """
    deployer = _deployer(tmp_path, PORT="9999", NODE_ENV="development", FEATURE="on")

    deployer._step_fetch()

    assert EnvManager().read_env_file(_env_file(tmp_path)) == {"FEATURE": "on"}
    assert any("PORT" in warning for warning in deployer.warnings)
    assert any("NODE_ENV" in warning for warning in deployer.warnings)


def test_an_injected_value_is_refused_before_it_reaches_the_env_file(
    tmp_path: Path,
    store: WASMStore,  # noqa: F811
) -> None:
    """systemd reads the file line by line: a newline would add a variable."""
    deployer = _deployer(tmp_path, EVIL="x\nLD_PRELOAD=/tmp/evil.so")

    with pytest.raises(EnvironmentValidationError):
        deployer._step_fetch()

    assert not _env_file(tmp_path).exists()


def test_generated_values_from_env_example_stay_out_of_the_unit(
    tmp_path: Path,
    store: WASMStore,  # noqa: F811
) -> None:
    """They used to be copied into env_vars "so they're available for systemd"."""
    deployer = _deployer(tmp_path)
    app_dir = tmp_path / "app"
    app_dir.mkdir()
    (app_dir / ".env.example").write_text("SESSION_SECRET=\nPORT=3000\n")

    deployer._step_fetch()
    deployer.create_service()

    written = EnvManager().read_env_file(_env_file(tmp_path))
    assert written["SESSION_SECRET"]
    assert "PORT" not in written
    unit = deployer.services.units["app-example-com"]
    assert unit["environment"] == {"PORT": "3000", "NODE_ENV": "production"}


def test_releases_keep_create_time_variables_in_shared(
    tmp_path: Path,
    store: WASMStore,  # noqa: F811
) -> None:
    """On releases the env file is ``shared/.env``, linked into every release."""
    deployer = _deployer(tmp_path, DATABASE_URL=SECRET_URL)
    deployer._layout = RELEASES
    (tmp_path / "app" / "shared").mkdir(parents=True)

    deployer._prepare_env()

    assert EnvManager().read_env_file(tmp_path / "app" / "shared" / ".env") == {
        "DATABASE_URL": SECRET_URL
    }


def test_a_static_site_never_gets_an_env_file_in_what_is_served(
    tmp_path: Path,
    store: WASMStore,  # noqa: F811
) -> None:
    """
    A static site has no process to load the file, and its directory may be
    the one the web server serves - Apache does not refuse dotfiles - so a
    ``.env`` written there would publish the secrets in it.
    """
    deployer = build_deployer(StaticDeployer, tmp_path)
    deployer.env_vars = {"API_KEY": "k-123"}
    app_dir = tmp_path / "app"
    app_dir.mkdir()
    (app_dir / "index.html").write_text("<html></html>")
    (app_dir / ".env.example").write_text("API_KEY=\n")

    deployer._prepare_env()

    assert not (app_dir / ".env").exists()


def test_a_static_site_still_refuses_an_injected_value(
    tmp_path: Path,
    store: WASMStore,  # noqa: F811
) -> None:
    """Its build still runs with the variables, so they are still validated."""
    deployer = build_deployer(StaticDeployer, tmp_path)
    deployer.env_vars = {"EVIL": "x\ny"}

    with pytest.raises(EnvironmentValidationError):
        deployer._prepare_env()


# ---------------------------------------------------------------------------
# Redeploying an application deployed before
# ---------------------------------------------------------------------------


def test_a_redeploy_moves_the_old_unit_s_inline_variables_to_the_env_file(
    tmp_path: Path,
    store: WASMStore,  # noqa: F811
) -> None:
    """
    The unit written by an earlier version carried the secrets inline, and
    the application ran with them. Rewriting the unit without them would
    silently drop them, so they move to the env file first.
    """
    store.create_service(
        Service(
            name="app-example-com",
            environment={"PORT": "3000", "NODE_ENV": "production", "DATABASE_URL": SECRET_URL},
        )
    )
    deployer = _deployer(tmp_path)

    deployer._step_fetch()
    deployer.create_service()

    assert EnvManager().read_env_file(_env_file(tmp_path)) == {"DATABASE_URL": SECRET_URL}
    assert deployer.services.units["app-example-com"]["environment"] == {
        "PORT": "3000",
        "NODE_ENV": "production",
    }


def test_a_conflicting_port_left_in_an_env_file_is_dropped_on_redeploy(
    tmp_path: Path,
    store: WASMStore,  # noqa: F811
) -> None:
    """
    ``.env.example`` often says PORT=3000, and the env file WASM generated
    from it kept that. Harmless while dotenv never overrode the unit; fatal
    for the second application on a server once systemd reads the file.
    """
    deployer = _deployer(tmp_path)
    deployer.port = 3001
    env_file = _env_file(tmp_path)
    env_file.parent.mkdir(parents=True)
    env_file.write_text("PORT=3000\nKEEP=1\n")

    deployer._step_fetch()
    deployer.create_service()

    assert EnvManager().read_env_file(env_file) == {"KEEP": "1"}
    assert stat.S_IMODE(env_file.stat().st_mode) == SECRET_MODE
    owner = f"{deployer.config.service_user}:{deployer.config.service_group}"
    assert deployer.runner.ran("chown", owner, str(env_file))


def test_an_env_file_that_agrees_with_the_unit_is_left_alone(
    tmp_path: Path,
    store: WASMStore,  # noqa: F811
) -> None:
    """Nothing to move and nothing in conflict: the operator's file is not rewritten."""
    deployer = _deployer(tmp_path)
    env_file = _env_file(tmp_path)
    env_file.parent.mkdir(parents=True)
    original = "# hand written\nPORT=3000\nKEEP=1\n"
    env_file.write_text(original)

    deployer._step_fetch()
    deployer.create_service()

    assert env_file.read_text() == original


# ---------------------------------------------------------------------------
# Monorepos: one unit and one env file per workspace
# ---------------------------------------------------------------------------


def test_monorepo_units_load_the_workspace_env_file_instead_of_inlining(
    tmp_path: Path,
    store: WASMStore,  # noqa: F811
) -> None:
    deployer = MonorepoDeployer(runner=FakeRunner(), fs=RecordingFileSystem())
    deployer.configure(
        "example.com", "src", app_path=tmp_path / "app", env_vars={"API_KEY": "k-123"}
    )
    deployer.workspaces = [
        MonorepoWorkspace(name="web", path="apps/web", subdomain="www", port=3001)
    ]
    services = FakeServiceManager()
    deployer.service_manager = services
    deployer._path_resolver.resolve_command = lambda command: command

    deployer._configure_environment()
    deployer._create_services()

    env_file = tmp_path / "app" / "apps" / "web" / ".env.production"
    assert EnvManager().read_env_file(env_file)["API_KEY"] == "k-123"
    unit = next(iter(services.units.values()))
    assert "API_KEY" not in unit["environment"]
    assert unit["environment"]["PORT"] == "3001"
    assert unit["environment_file"] == str(env_file)


def test_monorepo_refuses_an_injected_value_before_writing_the_env_file(
    tmp_path: Path,
    store: WASMStore,  # noqa: F811
) -> None:
    """The unit no longer validates these, so the env file writer has to."""
    deployer = MonorepoDeployer(runner=FakeRunner(), fs=RecordingFileSystem())
    deployer.configure(
        "example.com", "src", app_path=tmp_path / "app", env_vars={"EVIL": "x\nLD_PRELOAD=/x"}
    )
    deployer.workspaces = [
        MonorepoWorkspace(name="web", path="apps/web", subdomain="www", port=3001)
    ]

    with pytest.raises(EnvironmentValidationError):
        deployer._configure_environment()

    assert not (tmp_path / "app" / "apps" / "web" / ".env.production").exists()


# ---------------------------------------------------------------------------
# One grammar for every reader
# ---------------------------------------------------------------------------


def test_the_env_file_option_accepts_export_lines(tmp_path: Path) -> None:
    """
    ``--env-file`` had a parser of its own that read ``export FOO=bar`` as a
    key named ``export FOO`` and dropped it; ``wasm env`` did not.
    """
    env_file = tmp_path / "vars.env"
    env_file.write_text("export FOO=bar\nexport\tQUOTED='a b'\nPLAIN=1\n# comment\nBAD-KEY=x\n")
    logger = Logger()
    warnings: list[str] = []
    logger.warning = warnings.append  # type: ignore[method-assign]

    assert _read_env_file(env_file, logger) == {"FOO": "bar", "QUOTED": "a b", "PLAIN": "1"}
    assert any("BAD-KEY" in warning for warning in warnings)


def test_a_backslash_survives_systemd_and_dotenv_alike() -> None:
    """
    systemd consumes a backslash in an unquoted value; dotenv and
    :meth:`EnvManager.read_env_file` keep it. Single quotes are literal to
    all three, so a value with one is written inside them.
    """
    assert EnvManager._quote_if_needed("pa\\ss") == "'pa\\ss'"
    assert EnvManager._quote_if_needed("plain") == "plain"


def test_a_backslash_round_trips_through_the_env_file(tmp_path: Path) -> None:
    manager = EnvManager()
    env_file = tmp_path / ".env"

    manager.write_env_file(env_file, {"PASSWORD": "pa\\ss"})

    assert manager.read_env_file(env_file) == {"PASSWORD": "pa\\ss"}
