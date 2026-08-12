"""Deployers for WASM."""

from wasm.deployers.base import BaseDeployer
from wasm.deployers.docker_compose import DockerComposeDeployer
from wasm.deployers.monorepo import MonorepoDeployer
from wasm.deployers.registry import DeployerRegistry, detect_app_type, get_deployer

__all__ = [
    "BaseDeployer",
    "DeployerRegistry",
    "DockerComposeDeployer",
    "MonorepoDeployer",
    "detect_app_type",
    "get_deployer",
]
