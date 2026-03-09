# Copyright (c) 2024-2025 Yago López Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""Tests for the NginxConfigBuilder helper."""

import tempfile
from pathlib import Path

import pytest

from wasm.deployers.helpers.nginx_config import (
    NginxConfigBuilder,
    NginxRoute,
    NginxAdvancedConfig,
)


@pytest.fixture
def builder():
    """Create a NginxConfigBuilder instance."""
    return NginxConfigBuilder(verbose=False)


@pytest.fixture
def sample_config():
    """Create a sample advanced config."""
    return NginxAdvancedConfig(
        routes=[
            NginxRoute(
                path="/api",
                upstream_port=3001,
                upstream_name="backend",
            ),
            NginxRoute(
                path="/socket.io",
                upstream_port=3001,
                upstream_name="backend-ws",
                websocket=True,
            ),
            NginxRoute(
                path="/",
                upstream_port=3000,
                upstream_name="frontend",
            ),
        ],
        global_rate_limit="100r/s",
        security_headers={
            "Content-Security-Policy": "default-src 'self'",
        },
    )


class TestDetection:
    """Tests for config file detection."""

    def test_detect_wasm_nginx_yaml(self, builder, tmp_path):
        (tmp_path / "wasm.nginx.yaml").write_text("routes: []")
        result = builder.detect(tmp_path)
        assert result is not None
        assert result.name == "wasm.nginx.yaml"

    def test_detect_nginx_yaml(self, builder, tmp_path):
        (tmp_path / "nginx.yaml").write_text("routes: []")
        result = builder.detect(tmp_path)
        assert result is not None
        assert result.name == "nginx.yaml"

    def test_detect_none(self, builder, tmp_path):
        result = builder.detect(tmp_path)
        assert result is None

    def test_detect_priority(self, builder, tmp_path):
        """wasm.nginx.yaml takes priority over nginx.yaml."""
        (tmp_path / "wasm.nginx.yaml").write_text("routes: []")
        (tmp_path / "nginx.yaml").write_text("routes: []")
        result = builder.detect(tmp_path)
        assert result.name == "wasm.nginx.yaml"


class TestParsing:
    """Tests for YAML config parsing."""

    def test_parse_basic(self, builder, tmp_path):
        config_file = tmp_path / "wasm.nginx.yaml"
        config_file.write_text(
            "routes:\n"
            "  - path: /api\n"
            "    port: 3001\n"
            "    name: backend\n"
            "  - path: /\n"
            "    port: 3000\n"
            "    name: frontend\n"
        )
        config = builder.parse(config_file)

        assert len(config.routes) == 2
        assert config.routes[0].path == "/api"
        assert config.routes[0].upstream_port == 3001
        assert config.routes[0].upstream_name == "backend"
        assert config.routes[1].path == "/"
        assert config.routes[1].upstream_port == 3000

    def test_parse_websocket(self, builder, tmp_path):
        config_file = tmp_path / "wasm.nginx.yaml"
        config_file.write_text(
            "routes:\n"
            "  - path: /ws\n"
            "    port: 3001\n"
            "    websocket: true\n"
        )
        config = builder.parse(config_file)
        assert config.routes[0].websocket is True

    def test_parse_rate_limit(self, builder, tmp_path):
        config_file = tmp_path / "wasm.nginx.yaml"
        config_file.write_text(
            "rate_limit: '50r/s'\n"
            "routes:\n"
            "  - path: /\n"
            "    port: 3000\n"
            "    rate_limit: '10r/s'\n"
        )
        config = builder.parse(config_file)
        assert config.global_rate_limit == "50r/s"
        assert config.routes[0].rate_limit == "10r/s"

    def test_parse_security_headers(self, builder, tmp_path):
        config_file = tmp_path / "wasm.nginx.yaml"
        config_file.write_text(
            "routes:\n"
            "  - path: /\n"
            "    port: 3000\n"
            "security_headers:\n"
            "  X-Custom-Header: 'test-value'\n"
        )
        config = builder.parse(config_file)
        assert config.security_headers["X-Custom-Header"] == "test-value"

    def test_parse_strip_prefix(self, builder, tmp_path):
        config_file = tmp_path / "wasm.nginx.yaml"
        config_file.write_text(
            "routes:\n"
            "  - path: /api\n"
            "    port: 3001\n"
            "    strip_prefix: true\n"
        )
        config = builder.parse(config_file)
        assert config.routes[0].strip_prefix is True

    def test_parse_invalid_yaml(self, builder, tmp_path):
        config_file = tmp_path / "wasm.nginx.yaml"
        config_file.write_text("routes:\n  - path: /\n    port: {\nbad")
        with pytest.raises(ValueError, match="Invalid YAML"):
            builder.parse(config_file)

    def test_parse_empty_file(self, builder, tmp_path):
        config_file = tmp_path / "wasm.nginx.yaml"
        config_file.write_text("")
        with pytest.raises(ValueError, match="Empty or invalid"):
            builder.parse(config_file)

    def test_auto_name_generation(self, builder, tmp_path):
        config_file = tmp_path / "wasm.nginx.yaml"
        config_file.write_text(
            "routes:\n"
            "  - path: /api/v1\n"
            "    port: 3001\n"
        )
        config = builder.parse(config_file)
        assert config.routes[0].upstream_name == "api-v1"


class TestDockerCompose:
    """Tests for Docker Compose auto-derivation."""

    def test_from_docker_compose(self, builder, tmp_path):
        compose_file = tmp_path / "docker-compose.yml"
        compose_file.write_text(
            "services:\n"
            "  web:\n"
            "    image: nginx\n"
            "    ports:\n"
            "      - '8080:80'\n"
            "  api:\n"
            "    image: node\n"
            "    ports:\n"
            "      - '3001:3000'\n"
        )
        config = builder.from_docker_compose(compose_file, "test.local")
        assert len(config.routes) == 2

    def test_single_service(self, builder, tmp_path):
        compose_file = tmp_path / "docker-compose.yml"
        compose_file.write_text(
            "services:\n"
            "  web:\n"
            "    image: nginx\n"
            "    ports:\n"
            "      - '8080:80'\n"
        )
        config = builder.from_docker_compose(compose_file, "test.local")
        assert len(config.routes) == 1
        assert config.routes[0].upstream_port == 8080


class TestContextBuilding:
    """Tests for Jinja2 context building."""

    def test_build_basic_context(self, builder, sample_config):
        ctx = builder.build_context(sample_config, "example.com", ssl=True)

        assert ctx["domain"] == "example.com"
        assert ctx["ssl"] is True
        assert len(ctx["routes"]) == 3
        assert len(ctx["upstreams"]) > 0
        assert "X-Frame-Options" in ctx["security_headers"]
        assert ctx["security_headers"]["Content-Security-Policy"] == "default-src 'self'"

    def test_context_upstreams(self, builder, sample_config):
        ctx = builder.build_context(sample_config, "example.com")

        # Should have unique upstreams
        upstream_names = [u["name"] for u in ctx["upstreams"].values()]
        assert "frontend" in upstream_names
        assert "backend" in upstream_names

    def test_context_rate_limit_zones(self, builder, sample_config):
        ctx = builder.build_context(sample_config, "example.com")
        assert "global" in ctx["rate_limit_zones"]
        assert ctx["rate_limit_zones"]["global"] == "100r/s"

    def test_context_websocket_route(self, builder, sample_config):
        ctx = builder.build_context(sample_config, "example.com")
        ws_routes = [r for r in ctx["routes"] if r["websocket"]]
        assert len(ws_routes) == 1
        assert ws_routes[0]["path"] == "/socket.io"


class TestValidation:
    """Tests for configuration validation."""

    def test_valid_config(self, builder, sample_config):
        errors = builder.validate(sample_config)
        assert len(errors) == 0

    def test_no_routes(self, builder):
        config = NginxAdvancedConfig()
        errors = builder.validate(config)
        assert any("No routes" in e for e in errors)

    def test_duplicate_paths(self, builder):
        config = NginxAdvancedConfig(
            routes=[
                NginxRoute(path="/api", upstream_port=3000),
                NginxRoute(path="/api", upstream_port=3001),
            ]
        )
        errors = builder.validate(config)
        assert any("Duplicate" in e for e in errors)

    def test_invalid_port(self, builder):
        config = NginxAdvancedConfig(
            routes=[
                NginxRoute(path="/", upstream_port=0),
            ]
        )
        errors = builder.validate(config)
        assert any("Invalid port" in e for e in errors)

    def test_invalid_port_too_high(self, builder):
        config = NginxAdvancedConfig(
            routes=[
                NginxRoute(path="/", upstream_port=99999),
            ]
        )
        errors = builder.validate(config)
        assert any("Invalid port" in e for e in errors)

    def test_invalid_rate_limit(self, builder):
        config = NginxAdvancedConfig(
            routes=[
                NginxRoute(path="/", upstream_port=3000),
            ],
            global_rate_limit="invalid",
        )
        errors = builder.validate(config)
        assert any("rate limit" in e.lower() for e in errors)

    def test_valid_rate_limit_formats(self, builder):
        config = NginxAdvancedConfig(
            routes=[
                NginxRoute(path="/", upstream_port=3000, rate_limit="10r/s"),
            ],
            global_rate_limit="100r/m",
        )
        errors = builder.validate(config)
        assert len(errors) == 0
