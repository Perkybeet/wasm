# Copyright (c) 2024-2025 Yago López Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""Tests for the EnvManager helper."""

import json
import tempfile
from pathlib import Path

import pytest

from wasm.deployers.helpers.env_manager import (
    EnvManager,
    EnvVariable,
    EnvConfig,
)


@pytest.fixture
def env_manager():
    """Create an EnvManager instance."""
    return EnvManager(verbose=False)


@pytest.fixture
def temp_app(tmp_path):
    """Create a temporary application directory with .env.example."""
    env_example = tmp_path / ".env.example"
    env_example.write_text(
        "# Database connection URL\n"
        "DATABASE_URL=postgresql://localhost:5432/mydb\n"
        "\n"
        "# JWT secret for authentication\n"
        "JWT_SECRET=\n"
        "\n"
        "# Application port\n"
        "PORT=3000\n"
        "\n"
        "# API key for external service\n"
        "API_KEY=\n"
        "\n"
        "NODE_ENV=production\n"
    )
    return tmp_path


class TestEnvManagerParsing:
    """Tests for .env.example parsing."""

    def test_parse_env_example(self, env_manager, temp_app):
        """Test parsing a basic .env.example file."""
        variables = env_manager.discover(temp_app)
        assert len(variables) == 5

        names = [v.name for v in variables]
        assert "DATABASE_URL" in names
        assert "JWT_SECRET" in names
        assert "PORT" in names
        assert "API_KEY" in names
        assert "NODE_ENV" in names

    def test_defaults_parsed(self, env_manager, temp_app):
        """Test that default values are parsed correctly."""
        variables = env_manager.discover(temp_app)
        by_name = {v.name: v for v in variables}

        assert by_name["DATABASE_URL"].default == "postgresql://localhost:5432/mydb"
        assert by_name["PORT"].default == "3000"
        assert by_name["NODE_ENV"].default == "production"
        assert by_name["JWT_SECRET"].default == ""

    def test_descriptions_parsed(self, env_manager, temp_app):
        """Test that comments become descriptions."""
        variables = env_manager.discover(temp_app)
        by_name = {v.name: v for v in variables}

        assert "Database" in by_name["DATABASE_URL"].description
        assert "JWT" in by_name["JWT_SECRET"].description

    def test_quoted_defaults(self, env_manager, tmp_path):
        """Test parsing quoted default values."""
        env_file = tmp_path / ".env.example"
        env_file.write_text(
            'QUOTED_DOUBLE="hello world"\n'
            "QUOTED_SINGLE='hello world'\n"
            "UNQUOTED=hello\n"
        )

        variables = env_manager.discover(tmp_path)
        by_name = {v.name: v for v in variables}

        assert by_name["QUOTED_DOUBLE"].default == "hello world"
        assert by_name["QUOTED_SINGLE"].default == "hello world"
        assert by_name["UNQUOTED"].default == "hello"

    def test_empty_file(self, env_manager, tmp_path):
        """Test parsing an empty file."""
        env_file = tmp_path / ".env.example"
        env_file.write_text("")
        variables = env_manager.discover(tmp_path)
        assert len(variables) == 0

    def test_no_env_example(self, env_manager, tmp_path):
        """Test when no .env.example exists."""
        variables = env_manager.discover(tmp_path)
        assert len(variables) == 0

    def test_subdirectory_scanning(self, env_manager, tmp_path):
        """Test that subdirectories are scanned."""
        apps_dir = tmp_path / "apps" / "backend"
        apps_dir.mkdir(parents=True)

        (tmp_path / ".env.example").write_text("ROOT_VAR=1\n")
        (apps_dir / ".env.example").write_text("BACKEND_VAR=2\n")

        variables = env_manager.discover(tmp_path)
        names = [v.name for v in variables]
        assert "ROOT_VAR" in names
        assert "BACKEND_VAR" in names

    def test_deduplication(self, env_manager, tmp_path):
        """Test that duplicate variable names are deduplicated."""
        apps_dir = tmp_path / "apps" / "api"
        apps_dir.mkdir(parents=True)

        (tmp_path / ".env.example").write_text("DATABASE_URL=pg://root\n")
        (apps_dir / ".env.example").write_text("DATABASE_URL=pg://api\n")

        variables = env_manager.discover(tmp_path)
        db_vars = [v for v in variables if v.name == "DATABASE_URL"]
        assert len(db_vars) == 1
        # Root takes precedence
        assert db_vars[0].default == "pg://root"


class TestCategoryDetection:
    """Tests for variable category detection."""

    def test_database_category(self, env_manager):
        assert env_manager._detect_category("DATABASE_URL") == "Database"
        assert env_manager._detect_category("DB_HOST") == "Database"
        assert env_manager._detect_category("POSTGRES_PASSWORD") == "Database"

    def test_auth_category(self, env_manager):
        assert env_manager._detect_category("JWT_SECRET") == "Authentication"
        assert env_manager._detect_category("AUTH_PROVIDER") == "Authentication"
        assert env_manager._detect_category("SESSION_TIMEOUT") == "Authentication"

    def test_email_category(self, env_manager):
        assert env_manager._detect_category("SMTP_HOST") == "Email"
        assert env_manager._detect_category("MAIL_FROM") == "Email"

    def test_server_category(self, env_manager):
        assert env_manager._detect_category("PORT") == "Server"
        assert env_manager._detect_category("HOST") == "Server"
        assert env_manager._detect_category("NODE_ENV") == "Server"

    def test_unknown_category(self, env_manager):
        assert env_manager._detect_category("CUSTOM_THING") == "General"


class TestSecretDetection:
    """Tests for secret detection."""

    def test_password_detected(self, env_manager):
        assert env_manager._is_secret("DATABASE_PASSWORD") is True
        assert env_manager._is_secret("ADMIN_PASSWORD") is True

    def test_secret_detected(self, env_manager):
        assert env_manager._is_secret("JWT_SECRET") is True
        assert env_manager._is_secret("CLIENT_SECRET") is True

    def test_token_detected(self, env_manager):
        assert env_manager._is_secret("ACCESS_TOKEN") is True
        assert env_manager._is_secret("REFRESH_TOKEN") is True

    def test_api_key_detected(self, env_manager):
        assert env_manager._is_secret("API_KEY") is True
        assert env_manager._is_secret("STRIPE_API_KEY") is True

    def test_non_secret(self, env_manager):
        assert env_manager._is_secret("PORT") is False
        assert env_manager._is_secret("NODE_ENV") is False
        assert env_manager._is_secret("DATABASE_URL") is False


class TestSecretGeneration:
    """Tests for secret generation."""

    def test_generates_string(self, env_manager):
        secret = env_manager.generate_secret()
        assert isinstance(secret, str)
        assert len(secret) > 0

    def test_unique_secrets(self, env_manager):
        secrets = {env_manager.generate_secret() for _ in range(10)}
        assert len(secrets) == 10

    def test_custom_length(self, env_manager):
        short = env_manager.generate_secret(8)
        long = env_manager.generate_secret(64)
        assert len(short) < len(long)


class TestNonInteractivePrompt:
    """Tests for non-interactive variable filling."""

    def test_uses_defaults(self, env_manager):
        variables = [
            EnvVariable(name="PORT", default="3000"),
            EnvVariable(name="NODE_ENV", default="production"),
        ]
        result = env_manager.prompt_non_interactive(variables)
        assert result["PORT"] == "3000"
        assert result["NODE_ENV"] == "production"

    def test_generates_secrets(self, env_manager):
        variables = [
            EnvVariable(name="JWT_SECRET", secret=True, default=""),
        ]
        result = env_manager.prompt_non_interactive(variables)
        assert result["JWT_SECRET"] != ""
        assert len(result["JWT_SECRET"]) > 10

    def test_keeps_default_for_secrets_with_default(self, env_manager):
        variables = [
            EnvVariable(name="JWT_SECRET", secret=True, default="my-fixed-secret"),
        ]
        result = env_manager.prompt_non_interactive(variables)
        assert result["JWT_SECRET"] == "my-fixed-secret"


class TestEnvFileWriting:
    """Tests for .env file writing."""

    def test_write_env_file(self, env_manager, tmp_path):
        values = {"PORT": "3000", "NODE_ENV": "production"}
        written = env_manager.write_env_files(tmp_path, values)

        assert len(written) == 1
        content = written[0].read_text()
        assert "NODE_ENV=production" in content
        assert "PORT=3000" in content

    def test_write_with_mapping(self, env_manager, tmp_path):
        values = {
            "PORT": "3000",
            "DATABASE_URL": "pg://localhost",
            "API_KEY": "secret123",
        }
        mapping = {
            ".env": ["PORT", "DATABASE_URL"],
            "apps/api/.env": ["API_KEY"],
        }

        written = env_manager.write_env_files(tmp_path, values, mapping)
        assert len(written) == 2

    def test_values_sorted(self, env_manager, tmp_path):
        values = {"ZZZ": "last", "AAA": "first", "MMM": "middle"}
        env_manager.write_env_files(tmp_path, values)

        content = (tmp_path / ".env").read_text()
        lines = [l for l in content.strip().split("\n") if l]
        assert lines[0] == "AAA=first"
        assert lines[-1] == "ZZZ=last"


class TestMasking:
    """Tests for value masking."""

    def test_mask_secret(self, env_manager):
        assert env_manager.mask_value("API_KEY", "sk_live_abc123def456") == "sk_l****"

    def test_no_mask_non_secret(self, env_manager):
        assert env_manager.mask_value("PORT", "3000") == "3000"

    def test_short_secret_not_masked(self, env_manager):
        assert env_manager.mask_value("API_KEY", "abc") == "abc"


class TestEnvConfig:
    """Tests for EnvConfig serialization."""

    def test_save_and_load(self, env_manager, tmp_path):
        config = EnvConfig(
            variables=[
                EnvVariable(name="PORT", default="3000", category="Server"),
            ],
            files={".env": ["PORT"]},
        )

        env_manager.save_config(tmp_path, config)
        loaded = env_manager.load_config(tmp_path)

        assert loaded is not None
        assert len(loaded.variables) == 1
        assert loaded.variables[0].name == "PORT"
        assert loaded.files == {".env": ["PORT"]}

    def test_load_missing(self, env_manager, tmp_path):
        assert env_manager.load_config(tmp_path) is None

    def test_load_invalid_json(self, env_manager, tmp_path):
        wasm_dir = tmp_path / ".wasm"
        wasm_dir.mkdir()
        (wasm_dir / "env-config.json").write_text("invalid json")
        assert env_manager.load_config(tmp_path) is None


class TestCurrentValues:
    """Tests for reading current .env values."""

    def test_read_env(self, env_manager, tmp_path):
        (tmp_path / ".env").write_text("PORT=3000\nNODE_ENV=production\n")
        values = env_manager.get_current_values(tmp_path)
        assert values == {"PORT": "3000", "NODE_ENV": "production"}

    def test_read_quoted_values(self, env_manager, tmp_path):
        (tmp_path / ".env").write_text('DB_URL="postgres://localhost"\n')
        values = env_manager.get_current_values(tmp_path)
        assert values["DB_URL"] == "postgres://localhost"

    def test_skip_comments(self, env_manager, tmp_path):
        (tmp_path / ".env").write_text("# comment\nPORT=3000\n")
        values = env_manager.get_current_values(tmp_path)
        assert "PORT" in values
        assert len(values) == 1

    def test_missing_env(self, env_manager, tmp_path):
        values = env_manager.get_current_values(tmp_path)
        assert values == {}
