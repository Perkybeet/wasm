"""
Tests for systemd unit rendering and environment validation.

These tests pin down the fix for the unit-injection hole: values that reach a
systemd unit (environment variables, descriptions, commands, paths) used to be
interpolated verbatim, so a newline inside any of them appended arbitrary
directives to the unit, including ``User=root`` and ``ExecStartPre=``.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from jinja2 import Environment, PackageLoader

from wasm.core.store import App, Service, Site, WASMStore
from wasm.deployers.base import BaseDeployer
from wasm.managers.service_manager import ServiceManager
from wasm.validators.environment import (
    EnvironmentValidationError,
    escape_systemd_value,
    validate_env_name,
    validate_environment,
    validate_unit_value,
)

#: The payload that used to escape the quoted Environment= value and inject a
#: second User= plus an ExecStartPre= running arbitrary shell.
INJECTION_PAYLOAD = 'x"\nUser=root\nExecStartPre=/bin/sh -c "id > /tmp/pwned"\nEnvironment="Y=y'


@pytest.fixture
def jinja() -> Environment:
    """
    Provide a Jinja environment configured exactly like ServiceManager's.

    Returns:
        The environment loading templates from ``wasm/templates/systemd``.
    """
    # Autoescape stays off, as in ServiceManager: HTML escaping would corrupt
    # unit files. Injection is handled by validation plus the systemd-specific
    # escaping macros, which is what these tests exercise.
    return Environment(  # noqa: S701
        loader=PackageLoader("wasm", "templates/systemd"),
        trim_blocks=True,
        lstrip_blocks=True,
    )


def _directive_lines(unit: str, directive: str) -> list[str]:
    """
    Collect the lines of a rendered unit that set a given directive.

    Args:
        unit: The rendered unit file.
        directive: Directive name without the trailing ``=``.

    Returns:
        Every matching line, stripped.
    """
    return [line.strip() for line in unit.splitlines() if line.strip().startswith(f"{directive}=")]


# ---------------------------------------------------------------------------
# Template injection
# ---------------------------------------------------------------------------


def test_app_service_env_value_cannot_inject_directives(jinja: Environment) -> None:
    """A malicious env value must not add directives to the unit."""
    unit = jinja.get_template("app.service.j2").render(
        name="app-com",
        description="WASM: app.com",
        command="/usr/bin/node server.js",
        working_directory="/var/www/apps/app-com",
        user="www-data",
        group="www-data",
        environment={"EVIL": INJECTION_PAYLOAD},
    )

    assert _directive_lines(unit, "ExecStartPre") == []
    assert _directive_lines(unit, "User") == ["User=www-data"]
    assert len(_directive_lines(unit, "Environment")) == 1


def test_docker_compose_env_value_cannot_inject_directives(jinja: Environment) -> None:
    """The docker-compose unit must be as hardened as the app unit."""
    unit = jinja.get_template("docker-compose.service.j2").render(
        name="app-com",
        description="WASM: app.com",
        working_directory="/var/www/apps/app-com",
        environment={"EVIL": INJECTION_PAYLOAD},
    )

    assert _directive_lines(unit, "ExecStartPre") == [
        "ExecStartPre=/usr/bin/docker compose pull --ignore-pull-failures"
    ]
    assert _directive_lines(unit, "User") == ["User=root"]


def test_app_service_scalar_fields_cannot_inject_directives(jinja: Environment) -> None:
    """Description, command and working directory are interpolated too."""
    unit = jinja.get_template("app.service.j2").render(
        name="app-com",
        description="WASM: app.com\nUser=root",
        command="/usr/bin/node server.js\nExecStartPre=/bin/sh -c 'id'",
        working_directory="/var/www/apps/app-com\nUser=root",
        user="www-data",
        group="www-data",
        environment={},
    )

    assert _directive_lines(unit, "ExecStartPre") == []
    assert _directive_lines(unit, "User") == ["User=www-data"]
    assert _directive_lines(unit, "WorkingDirectory") == [
        "WorkingDirectory=/var/www/apps/app-com User=root"
    ]


def test_backup_templates_cannot_inject_directives(jinja: Environment) -> None:
    """The backup service and timer interpolate the domain and the schedule."""
    service = jinja.get_template("backup-service.j2").render(
        domain="app.com\nExecStartPre=/bin/sh -c 'id'"
    )
    assert _directive_lines(service, "ExecStartPre") == []
    assert len(_directive_lines(service, "ExecStart")) == 1

    timer = jinja.get_template("backup-timer.j2").render(
        domain="app.com", schedule="daily\nUnit=evil.service"
    )
    assert _directive_lines(timer, "Unit") == []


def test_valid_unit_render_snapshot(jinja: Environment) -> None:
    """Pin the rendered output of a well-formed unit."""
    unit = jinja.get_template("app.service.j2").render(
        name="app-com",
        description="WASM: app.com (nextjs)",
        command="/usr/bin/npm run start",
        working_directory="/var/www/apps/app-com",
        user="www-data",
        group="www-data",
        environment={"PORT": "3000", "NODE_ENV": "production"},
    )

    assert unit == (
        "# Systemd service for app-com\n"
        "# Generated by WASM\n"
        "\n"
        "[Unit]\n"
        "Description=WASM: app.com (nextjs)\n"
        "Documentation=https://github.com/Perkybeet/wasm\n"
        "After=network.target\n"
        "\n"
        "[Service]\n"
        "Type=simple\n"
        "User=www-data\n"
        "Group=www-data\n"
        "WorkingDirectory=/var/www/apps/app-com\n"
        "\n"
        "# Environment\n"
        'Environment="PORT=3000"\n'
        'Environment="NODE_ENV=production"\n'
        "\n"
        "# Command\n"
        "ExecStart=/usr/bin/npm run start\n"
        "\n"
        "# Restart policy\n"
        "Restart=always\n"
        "RestartSec=10\n"
        "\n"
        "# Security\n"
        "NoNewPrivileges=true\n"
        "PrivateTmp=true\n"
        "\n"
        "# Logging\n"
        "StandardOutput=journal\n"
        "StandardError=journal\n"
        "SyslogIdentifier=app-com\n"
        "\n"
        "# Resource limits\n"
        "LimitNOFILE=65535\n"
        "\n"
        "[Install]\n"
        "WantedBy=multi-user.target"
    )


# ---------------------------------------------------------------------------
# Environment validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    ["1PORT", "PORT-1", "PORT 1", "", "PO=RT", "PORT\n", "pórt", "PORT.SUB"],
)
def test_invalid_env_names_are_rejected(name: str) -> None:
    """Only POSIX environment identifiers are accepted."""
    with pytest.raises(EnvironmentValidationError):
        validate_env_name(name)


@pytest.mark.parametrize("name", ["PORT", "_PORT", "node_env", "A1", "_"])
def test_valid_env_names_are_accepted(name: str) -> None:
    """Names matching [A-Za-z_][A-Za-z0-9_]* pass through unchanged."""
    assert validate_env_name(name) == name


@pytest.mark.parametrize(
    "value",
    [
        INJECTION_PAYLOAD,
        "a\nb",
        "a\rb",
        "a\x00b",
        "a\x1bb",
        "a\x7fb",
        "a\tb",
    ],
)
def test_control_characters_in_values_are_rejected(value: str) -> None:
    """Control characters, newlines above all, terminate a systemd directive."""
    with pytest.raises(EnvironmentValidationError):
        validate_environment({"EVIL": value})


def test_quotes_and_percent_and_unicode_are_kept_verbatim() -> None:
    """Escapable characters stay in the value; escaping happens at render time."""
    env = validate_environment(
        {
            "JSON": '{"a": 1}',
            "PCT": "100%",
            "BACKSLASH": "C:\\tmp",
            "UNICODE": "cafe\u0301 \u00f1",
            "EMPTY": "",
        }
    )
    assert env == {
        "JSON": '{"a": 1}',
        "PCT": "100%",
        "BACKSLASH": "C:\\tmp",
        "UNICODE": "cafe\u0301 \u00f1",
        "EMPTY": "",
    }


def test_non_string_values_are_coerced() -> None:
    """The HTTP API accepts free-form JSON, so scalars must survive."""
    assert validate_environment({"PORT": 3000, "DEBUG": True}) == {
        "PORT": "3000",
        "DEBUG": "True",
    }


def test_structured_values_are_rejected() -> None:
    """A dict or list cannot become an environment variable."""
    with pytest.raises(EnvironmentValidationError):
        validate_environment({"CONF": {"a": 1}})


def test_escape_systemd_value_escapes_quotes_backslashes_and_specifiers() -> None:
    """Escaping targets the three characters systemd re-interprets."""
    assert escape_systemd_value('a"b') == 'a\\"b'
    assert escape_systemd_value("a\\b") == "a\\\\b"
    assert escape_systemd_value("100%") == "100%%"
    assert escape_systemd_value('C:\\p "x" 50%') == 'C:\\\\p \\"x\\" 50%%'


def test_validate_unit_value_rejects_newlines() -> None:
    """Scalar directives are single-line by definition."""
    with pytest.raises(EnvironmentValidationError):
        validate_unit_value("WASM: app.com\nUser=root", field="Description")

    assert validate_unit_value("WASM: app.com", field="Description") == "WASM: app.com"


def test_env_escaping_survives_a_render(jinja: Environment) -> None:
    """A validated value with quotes renders as an escaped, single directive."""
    env = validate_environment({"JSON": '{"a": 1}', "PCT": "50%"})
    unit = jinja.get_template("app.service.j2").render(
        name="app-com",
        description="d",
        command="/usr/bin/true",
        working_directory="/tmp",
        user="www-data",
        group="www-data",
        environment=env,
    )

    assert 'Environment="JSON={\\"a\\": 1}"' in unit
    assert 'Environment="PCT=50%%"' in unit


# ---------------------------------------------------------------------------
# Deployer integration
# ---------------------------------------------------------------------------


class _Deployer(BaseDeployer):
    """Minimal concrete deployer used to exercise BaseDeployer methods."""

    APP_TYPE = "test"

    @classmethod
    def detect(cls, path: Path) -> bool:
        """Never auto-detects."""
        return False

    def get_install_command(self) -> list[str]:
        """Returns an inert command."""
        return ["true"]

    def get_build_command(self) -> list[str]:
        """Returns an inert command."""
        return ["true"]

    def get_start_command(self) -> str:
        """Returns an inert command."""
        return "/usr/bin/true"


def _bare_deployer() -> _Deployer:
    """
    Build a deployer without running its side-effecting constructor.

    Returns:
        A deployer with only the attributes the tested methods touch.
    """
    deployer = object.__new__(_Deployer)
    deployer.verbose = False
    deployer.logger = MagicMock()
    deployer.store = MagicMock(spec=WASMStore)
    # The registrar is created lazily from the store, so the private slot has
    # to exist on an instance built without running __init__.
    deployer._registrar = None
    deployer.service_manager = MagicMock(spec=ServiceManager)
    deployer.domain = "app.com"
    deployer.app_name = "app-com"
    deployer.app_path = Path("/var/www/apps/app-com")
    deployer.webserver = "nginx"
    deployer.port = 3000
    deployer.env_vars = {}
    deployer.config = MagicMock(service_user="www-data", service_group="www-data")
    return deployer


def test_create_service_rejects_injected_environment() -> None:
    """A malicious env var must abort service creation, not reach the unit."""
    deployer = _bare_deployer()
    deployer.env_vars = {"EVIL": INJECTION_PAYLOAD}
    deployer._resolve_absolute_path = lambda command: command

    with pytest.raises(EnvironmentValidationError):
        deployer.create_service()

    deployer.service_manager.create_service.assert_not_called()


def test_create_service_passes_validated_environment() -> None:
    """A well-formed environment reaches the service manager unchanged."""
    deployer = _bare_deployer()
    deployer.env_vars = {"API_URL": "https://api.example.com"}
    deployer._resolve_absolute_path = lambda command: command
    deployer.store.get_app.return_value = None
    deployer.store.get_service.return_value = None

    assert deployer.create_service() is True

    kwargs = deployer.service_manager.create_service.call_args.kwargs
    assert kwargs["environment"] == {
        "API_URL": "https://api.example.com",
        "PORT": "3000",
        "NODE_ENV": "production",
    }


def test_rollback_uses_the_service_status_method_that_exists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rollback must call get_status; ``status`` does not exist on the manager."""
    deployer = _bare_deployer()
    monkeypatch.setattr("wasm.deployers.base.NginxManager", MagicMock())
    deployer.service_manager.get_status.return_value = {"exists": True}
    deployer.store.get_service.return_value = None
    deployer.store.get_site.return_value = None
    deployer.store.get_app.return_value = None

    assert deployer.rollback(keep_files=True) is True

    deployer.service_manager.get_status.assert_called_once_with("app-com")
    deployer.service_manager.delete_service.assert_called_once_with("app-com")


def test_rollback_deletes_store_records_by_natural_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The store deletes by name/domain, so passing row ids removes nothing."""
    deployer = _bare_deployer()
    monkeypatch.setattr("wasm.deployers.base.NginxManager", MagicMock())
    deployer.service_manager.get_status.return_value = {"exists": False}
    deployer.store.get_service.return_value = Service(id=3, name="app-com")
    deployer.store.get_site.return_value = Site(id=7, domain="app.com")
    deployer.store.get_app.return_value = App(id=11, domain="app.com")

    assert deployer.rollback(keep_files=True) is True

    deployer.store.delete_service.assert_called_once_with("app-com")
    deployer.store.delete_site.assert_called_once_with("app.com")
    deployer.store.delete_app.assert_called_once_with("app.com")
