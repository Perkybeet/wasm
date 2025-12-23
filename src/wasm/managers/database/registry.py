# Copyright (c) 2024-2025 Yago López Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
Database manager registry for WASM.

Provides registration and lookup of database engine managers.
"""

from typing import Dict, List, Optional, Type

from wasm.managers.database.base import BaseDatabaseManager


class DatabaseRegistry:
    """
    Registry for database engine managers.
    
    Manages registration and retrieval of database managers.
    """
    
    _managers: Dict[str, Type[BaseDatabaseManager]] = {}
    _aliases: Dict[str, str] = {}
    
    @classmethod
    def register(cls, manager_class: Type[BaseDatabaseManager], aliases: List[str] = None) -> None:
        """
        Register a database manager.
        
        Args:
            manager_class: Database manager class.
            aliases: Optional list of aliases.
        """
        engine_name = manager_class.ENGINE_NAME.lower()
        cls._managers[engine_name] = manager_class
        
        if aliases:
            for alias in aliases:
                cls._aliases[alias.lower()] = engine_name
    
    @classmethod
    def get(cls, engine: str, verbose: bool = False) -> Optional[BaseDatabaseManager]:
        """
        Get a database manager instance by engine name.
        
        Args:
            engine: Engine name or alias.
            verbose: Enable verbose logging.
            
        Returns:
            Database manager instance or None.
        """
        engine_lower = engine.lower()
        
        # Check aliases first
        if engine_lower in cls._aliases:
            engine_lower = cls._aliases[engine_lower]
        
        manager_class = cls._managers.get(engine_lower)
        if manager_class:
            return manager_class(verbose=verbose)
        
        return None
    
    @classmethod
    def list_engines(cls) -> List[str]:
        """
        List all registered database engines.
        
        Returns:
            List of engine names.
        """
        return list(cls._managers.keys())
    
    @classmethod
    def get_all_managers(cls, verbose: bool = False) -> List[BaseDatabaseManager]:
        """
        Get instances of all registered managers.
        
        Args:
            verbose: Enable verbose logging.
            
        Returns:
            List of manager instances.
        """
        return [cls.get(engine, verbose) for engine in cls.list_engines()]
    
    @classmethod
    def get_installed(cls, verbose: bool = False) -> List[BaseDatabaseManager]:
        """
        Get managers for installed database engines.
        
        Args:
            verbose: Enable verbose logging.
            
        Returns:
            List of manager instances for installed engines.
        """
        installed = []
        for engine in cls.list_engines():
            manager = cls.get(engine, verbose)
            if manager and manager.is_installed():
                installed.append(manager)
        return installed


def get_db_manager(engine: str, verbose: bool = False) -> Optional[BaseDatabaseManager]:
    """
    Convenience function to get a database manager.
    
    Args:
        engine: Engine name or alias.
        verbose: Enable verbose logging.
        
    Returns:
        Database manager instance or None.
    """
    return DatabaseRegistry.get(engine, verbose)
