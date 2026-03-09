"""
Deployer registry for WASM.

Handles registration and detection of application deployers.
"""

from pathlib import Path
from typing import Dict, List, Optional, Type

from wasm.deployers.base import BaseDeployer


class DeployerRegistry:
    """
    Registry for application deployers.
    
    Manages available deployers and handles automatic detection.
    """
    
    _deployers: Dict[str, Type[BaseDeployer]] = {}
    
    @classmethod
    def register(cls, deployer_class: Type[BaseDeployer]) -> None:
        """
        Register a deployer class.
        
        Args:
            deployer_class: Deployer class to register.
        """
        cls._deployers[deployer_class.APP_TYPE] = deployer_class
    
    @classmethod
    def get(cls, app_type: str) -> Optional[Type[BaseDeployer]]:
        """
        Get a deployer class by type.
        
        Args:
            app_type: Application type.
            
        Returns:
            Deployer class or None.
        """
        return cls._deployers.get(app_type.lower())
    
    @classmethod
    def list_types(cls) -> List[str]:
        """
        List all registered application types.
        
        Returns:
            List of application type names.
        """
        return list(cls._deployers.keys())
    
    @classmethod
    def list_deployers(cls) -> List[Dict]:
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
            }
            for d in cls._deployers.values()
        ]
    
    @classmethod
    def detect(cls, path: Path, verbose: bool = False) -> Optional[str]:
        """
        Detect application type from path.
        
        Args:
            path: Path to check.
            verbose: Enable verbose output.
            
        Returns:
            Detected application type or None.
        """
        for app_type, deployer_class in cls._deployers.items():
            deployer = deployer_class(verbose=verbose)
            if deployer.detect(path):
                return app_type
        
        return None


def get_deployer(app_type: str, verbose: bool = False) -> BaseDeployer:
    """
    Get a deployer instance by type.
    
    Args:
        app_type: Application type.
        verbose: Enable verbose output.
        
    Returns:
        Deployer instance.
        
    Raises:
        ValueError: If app type is not supported.
    """
    # Import deployers to ensure registration
    _import_deployers()
    
    deployer_class = DeployerRegistry.get(app_type)
    if not deployer_class:
        available = ", ".join(DeployerRegistry.list_types())
        raise ValueError(
            f"Unsupported application type: {app_type}. "
            f"Available types: {available}"
        )
    
    return deployer_class(verbose=verbose)


def detect_app_type(path: Path, verbose: bool = False) -> Optional[str]:
    """
    Detect application type from path.
    
    Args:
        path: Path to check.
        verbose: Enable verbose output.
        
    Returns:
        Detected application type or None.
    """
    # Import deployers to ensure registration
    _import_deployers()
    
    return DeployerRegistry.detect(path, verbose=verbose)


def _import_deployers() -> None:
    """Import all deployer modules to trigger registration."""
    # Monorepo must be imported first for detection priority
    from wasm.deployers import monorepo
    # Docker Compose after monorepo but before individual deployers
    from wasm.deployers import docker_compose
    from wasm.deployers import nextjs
    from wasm.deployers import nodejs
    from wasm.deployers import vite
    from wasm.deployers import python
    from wasm.deployers import static
