# Copyright (c) 2024-2026 Yago Lopez Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
Deployer registry for WASM.

Registration, lookup, and the detection order that makes ``--type auto`` mean
something. ``_import_deployers`` used to have an empty body with two comments
about import order, so nothing was ever registered by the time anyone asked;
``detect()`` then walked an empty dict and returned None, and the CLI quietly
deployed every repository as a generic Node app.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar

from wasm.deployers.interface import AppDeployer


class DeployerRegistry:
    """
    Registry for application deployers.

    Manages available deployers and handles automatic detection.
    """

    _deployers: ClassVar[dict[str, type[AppDeployer]]] = {}

    @classmethod
    def register(cls, deployer_class: type[AppDeployer]) -> None:
        """
        Register a deployer class.

        Args:
            deployer_class: Deployer class to register.
        """
        cls._deployers[deployer_class.APP_TYPE] = deployer_class

    @classmethod
    def get(cls, app_type: str) -> type[AppDeployer] | None:
        """
        Get a deployer class by type.

        Args:
            app_type: Application type.

        Returns:
            Deployer class or None.
        """
        return cls._deployers.get(app_type.lower())

    @classmethod
    def list_types(cls) -> list[str]:
        """
        List all registered application types.

        Returns:
            List of application type names.
        """
        return list(cls._deployers.keys())

    @classmethod
    def list_deployers(cls) -> list[dict[str, Any]]:
        """
        List all registered deployers with info.

        Returns:
            List of deployer information dictionaries.
        """
        return [
            {
                "type": d.APP_TYPE,
                "name": d.DISPLAY_NAME,
                "detection_files": d.DETECTION_FILES,
                "priority": d.DETECTION_PRIORITY,
            }
            for d in cls.in_detection_order()
        ]

    @classmethod
    def in_detection_order(cls) -> list[type[AppDeployer]]:
        """
        Return the registered deployers in the order detection must try them.

        Highest ``DETECTION_PRIORITY`` first, ties broken by type name so the
        result never depends on which module happened to be imported first.

        Returns:
            The deployer classes, most specific first.
        """
        return sorted(
            cls._deployers.values(),
            key=lambda d: (-d.DETECTION_PRIORITY, d.APP_TYPE),
        )

    @classmethod
    def detect(cls, path: Path, verbose: bool = False) -> str | None:
        """
        Detect application type from a directory of source code.

        Args:
            path: Path to check. Must already contain the fetched source.
            verbose: Enable verbose output.

        Returns:
            The detected application type, or None when nothing recognised it.
        """
        for deployer_class in cls.in_detection_order():
            if deployer_class.APP_TYPE == "auto":
                continue
            deployer = deployer_class(verbose=verbose)
            if deployer.detect(path):
                return deployer_class.APP_TYPE

        return None


def get_deployer(app_type: str, verbose: bool = False) -> AppDeployer:
    """
    Get a deployer instance by type.

    Args:
        app_type: Application type. ``auto`` returns the deployer that decides
            for itself once the source is on disk.
        verbose: Enable verbose output.

    Returns:
        Deployer instance.

    Raises:
        ValueError: If app type is not supported.
    """
    _import_deployers()

    deployer_class = DeployerRegistry.get(app_type)
    if not deployer_class:
        available = ", ".join(sorted(DeployerRegistry.list_types()))
        raise ValueError(f"Unsupported application type: {app_type}. Available types: {available}")

    return deployer_class(verbose=verbose)


def detect_app_type(path: Path, verbose: bool = False) -> str | None:
    """
    Detect application type from path.

    Args:
        path: Path to check.
        verbose: Enable verbose output.

    Returns:
        Detected application type or None.
    """
    _import_deployers()

    return DeployerRegistry.detect(path, verbose=verbose)


def _import_deployers() -> None:
    """
    Import every deployer module so that registration has happened.

    Import order is deliberately irrelevant: precedence comes from
    ``DETECTION_PRIORITY``, not from who registered first.
    """
    from wasm.deployers import (  # noqa: F401
        auto,
        docker_compose,
        monorepo,
        nextjs,
        nodejs,
        python,
        static,
        vite,
    )
