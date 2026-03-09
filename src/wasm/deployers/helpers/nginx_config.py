# Copyright (c) 2024-2025 Yago López Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
Advanced Nginx configuration builder for WASM.

Supports multi-route path-based proxying, WebSocket upgrade,
rate limiting, and custom security headers via wasm.nginx.yaml
project configuration files.
"""

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Any

import yaml

from wasm.core.logger import Logger


@dataclass
class NginxRoute:
    """A single Nginx location route."""
    path: str = "/"
    upstream_port: int = 3000
    upstream_name: str = ""
    websocket: bool = False
    rate_limit: str = ""
    rate_limit_burst: int = 5
    buffer_size: str = ""
    timeout: int = 60
    strip_prefix: bool = False


@dataclass
class NginxAdvancedConfig:
    """Advanced Nginx configuration with multiple routes."""
    routes: List[NginxRoute] = field(default_factory=list)
    global_rate_limit: str = ""
    security_headers: Dict[str, str] = field(default_factory=dict)
    custom_directives: List[str] = field(default_factory=list)


class NginxConfigBuilder:
    """
    Builder for advanced Nginx configurations.

    Reads wasm.nginx.yaml project files or auto-derives routes
    from Docker Compose port mappings.
    """

    CONFIG_FILENAMES = ["wasm.nginx.yaml", "wasm.nginx.yml", "nginx.yaml", "nginx.yml"]

    def __init__(self, verbose: bool = False):
        self.verbose = verbose
        self.logger = Logger(verbose=verbose)

    def detect(self, app_path: Path) -> Optional[Path]:
        """
        Find a wasm.nginx.yaml config file.

        Args:
            app_path: Application root path.

        Returns:
            Path to config file, or None if not found.
        """
        for name in self.CONFIG_FILENAMES:
            config_path = app_path / name
            if config_path.exists():
                return config_path
        return None

    def parse(self, config_path: Path) -> NginxAdvancedConfig:
        """
        Parse a wasm.nginx.yaml config file.

        Args:
            config_path: Path to the YAML config file.

        Returns:
            Parsed NginxAdvancedConfig.

        Raises:
            ValueError: If config is invalid.
        """
        try:
            data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        except yaml.YAMLError as e:
            raise ValueError(f"Invalid YAML in {config_path}: {e}")

        if not data or not isinstance(data, dict):
            raise ValueError(f"Empty or invalid config in {config_path}")

        config = NginxAdvancedConfig()

        # Parse routes
        for route_data in data.get("routes", []):
            route = NginxRoute(
                path=route_data.get("path", "/"),
                upstream_port=route_data.get("port", 3000),
                upstream_name=route_data.get("name", ""),
                websocket=route_data.get("websocket", False),
                rate_limit=route_data.get("rate_limit", ""),
                rate_limit_burst=route_data.get("rate_limit_burst", 5),
                buffer_size=route_data.get("buffer_size", ""),
                timeout=route_data.get("timeout", 60),
                strip_prefix=route_data.get("strip_prefix", False),
            )
            if not route.upstream_name:
                route.upstream_name = route.path.strip("/").replace("/", "-") or "default"
            config.routes.append(route)

        config.global_rate_limit = data.get("rate_limit", "")
        config.security_headers = data.get("security_headers", {})
        config.custom_directives = data.get("custom_directives", [])

        return config

    def from_docker_compose(
        self,
        compose_path: Path,
        domain: str,
    ) -> NginxAdvancedConfig:
        """
        Auto-derive Nginx routes from Docker Compose port mappings.

        Args:
            compose_path: Path to docker-compose.yml.
            domain: Target domain.

        Returns:
            NginxAdvancedConfig with routes derived from compose services.
        """
        try:
            data = yaml.safe_load(compose_path.read_text(encoding="utf-8"))
        except yaml.YAMLError as e:
            raise ValueError(f"Invalid Docker Compose file: {e}")

        config = NginxAdvancedConfig()
        services = data.get("services", {})

        for svc_name, svc_data in services.items():
            ports = svc_data.get("ports", [])
            for port_mapping in ports:
                port_str = str(port_mapping)
                # Parse "HOST:CONTAINER" or just "PORT"
                parts = port_str.split(":")
                if len(parts) == 2:
                    host_port = int(parts[0])
                elif len(parts) == 1:
                    host_port = int(parts[0])
                else:
                    # "IP:HOST:CONTAINER"
                    host_port = int(parts[1])

                route = NginxRoute(
                    path="/",
                    upstream_port=host_port,
                    upstream_name=svc_name,
                )
                config.routes.append(route)

        # If multiple routes, assign path prefixes based on service name
        if len(config.routes) > 1:
            for route in config.routes:
                if route.upstream_name != config.routes[0].upstream_name:
                    route.path = f"/{route.upstream_name}"

        return config

    def build_context(
        self,
        config: NginxAdvancedConfig,
        domain: str,
        ssl: bool = False,
        app_path: str = "",
    ) -> Dict[str, Any]:
        """
        Build Jinja2 template context from configuration.

        Args:
            config: Advanced Nginx configuration.
            domain: Target domain.
            ssl: Whether SSL is enabled.
            app_path: Application path.

        Returns:
            Dictionary for Jinja2 template rendering.
        """
        # Collect unique upstreams (by port)
        upstreams = {}
        for route in config.routes:
            key = f"upstream_{route.upstream_name}"
            if key not in upstreams:
                upstreams[key] = {
                    "name": route.upstream_name,
                    "port": route.upstream_port,
                }

        # Collect unique rate limit zones
        rate_limit_zones = {}
        if config.global_rate_limit:
            rate_limit_zones["global"] = config.global_rate_limit
        for route in config.routes:
            if route.rate_limit:
                zone_name = f"zone_{route.upstream_name}"
                rate_limit_zones[zone_name] = route.rate_limit

        # Build route contexts
        route_contexts = []
        for route in config.routes:
            ctx = {
                "path": route.path,
                "upstream_name": route.upstream_name,
                "upstream_port": route.upstream_port,
                "websocket": route.websocket,
                "rate_limit": route.rate_limit,
                "rate_limit_burst": route.rate_limit_burst,
                "rate_limit_zone": f"zone_{route.upstream_name}" if route.rate_limit else "",
                "buffer_size": route.buffer_size,
                "timeout": route.timeout,
                "strip_prefix": route.strip_prefix,
            }
            route_contexts.append(ctx)

        # Default security headers
        security_headers = {
            "X-Frame-Options": "SAMEORIGIN",
            "X-Content-Type-Options": "nosniff",
            "X-XSS-Protection": "1; mode=block",
            "Referrer-Policy": "strict-origin-when-cross-origin",
        }
        security_headers.update(config.security_headers)

        return {
            "domain": domain,
            "ssl": ssl,
            "ssl_certificate": f"/etc/letsencrypt/live/{domain}/fullchain.pem",
            "ssl_certificate_key": f"/etc/letsencrypt/live/{domain}/privkey.pem",
            "app_path": app_path or f"/var/www/apps/{domain}",
            "upstreams": upstreams,
            "routes": route_contexts,
            "rate_limit_zones": rate_limit_zones,
            "security_headers": security_headers,
            "custom_directives": config.custom_directives,
        }

    def validate(self, config: NginxAdvancedConfig) -> List[str]:
        """
        Validate an advanced Nginx configuration.

        Args:
            config: Configuration to validate.

        Returns:
            List of validation error messages (empty if valid).
        """
        errors = []

        if not config.routes:
            errors.append("No routes defined")
            return errors

        # Check for duplicate paths
        paths = [r.path for r in config.routes]
        seen = set()
        for path in paths:
            if path in seen:
                errors.append(f"Duplicate route path: {path}")
            seen.add(path)

        # Validate ports
        for route in config.routes:
            if not (1 <= route.upstream_port <= 65535):
                errors.append(f"Invalid port {route.upstream_port} for path {route.path}")

        # Validate rate limit format (e.g., "100r/s", "10r/m")
        rate_limit_pattern = re.compile(r"^\d+r/[sm]$")
        if config.global_rate_limit and not rate_limit_pattern.match(config.global_rate_limit):
            errors.append(f"Invalid rate limit format: {config.global_rate_limit} (expected: NNr/s or NNr/m)")

        for route in config.routes:
            if route.rate_limit and not rate_limit_pattern.match(route.rate_limit):
                errors.append(f"Invalid rate limit format for {route.path}: {route.rate_limit}")

        return errors
