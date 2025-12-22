# Copyright (c) 2024-2025 Yago López Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
Base manager class for WASM.

Provides common functionality for all managers.
"""

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Optional

from wasm.core.config import Config
from wasm.core.logger import Logger
from wasm.core.utils import run_command, run_command_sudo, CommandResult


class BaseManager(ABC):
    """
    Abstract base class for all managers.
    
    Provides common functionality like configuration access,
    logging, and command execution.
    """
    
    def __init__(self, verbose: bool = False):
        """
        Initialize the manager.
        
        Args:
            verbose: Enable verbose logging.
        """
        self.config = Config()
        self.logger = Logger(verbose=verbose)
        self.verbose = verbose
    
    def _run(
        self,
        command: list,
        cwd: Optional[Path] = None,
        env: Optional[dict] = None,
        timeout: Optional[int] = None,
    ) -> CommandResult:
        """
        Execute a command.
        
        Args:
            command: Command to execute.
            cwd: Working directory.
            env: Environment variables.
            timeout: Command timeout.
            
        Returns:
            CommandResult with execution results.
        """
        self.logger.debug(f"Running: {' '.join(command)}")
        result = run_command(command, cwd=cwd, env=env, timeout=timeout)
        
        if not result.success:
            self.logger.debug(f"Command failed: {result.stderr}")
        
        return result
    
    def _run_sudo(
        self,
        command: list,
        cwd: Optional[Path] = None,
        env: Optional[dict] = None,
        timeout: Optional[int] = None,
    ) -> CommandResult:
        """
        Execute a command with sudo.
        
        Args:
            command: Command to execute.
            cwd: Working directory.
            env: Environment variables.
            timeout: Command timeout.
            
        Returns:
            CommandResult with execution results.
        """
        self.logger.debug(f"Running (sudo): {' '.join(command)}")
        result = run_command_sudo(command, cwd=cwd, env=env, timeout=timeout)
        
        if not result.success:
            self.logger.debug(f"Command failed: {result.stderr}")
        
        return result
    
    @abstractmethod
    def is_installed(self) -> bool:
        """
        Check if the managed service/tool is installed.
        
        Returns:
            True if installed.
        """
        pass
    
    @abstractmethod
    def get_version(self) -> Optional[str]:
        """
        Get the version of the managed service/tool.
        
        Returns:
            Version string or None.
        """
        pass
