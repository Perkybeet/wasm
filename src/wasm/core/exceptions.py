"""
Custom exceptions for WASM.

This module defines a hierarchy of exceptions used throughout the application
to provide clear and actionable error messages.
"""


class WASMError(Exception):
    """
    Base exception for all WASM errors.
    
    All custom exceptions should inherit from this class.
    """
    
    def __init__(self, message: str, details: str = ""):
        self.message = message
        self.details = details
        super().__init__(self.message)
    
    def __str__(self) -> str:
        if self.details:
            return f"{self.message}\n  Details: {self.details}"
        return self.message


class ConfigError(WASMError):
    """Raised when there's a configuration error."""
    pass


class ValidationError(WASMError):
    """Raised when input validation fails."""
    pass


class DeploymentError(WASMError):
    """Raised when deployment fails at any step."""
    pass


class BuildError(DeploymentError):
    """Raised when application build fails."""
    pass


class SourceError(WASMError):
    """Raised when source fetching fails (git clone, download, etc.)."""
    pass


class ServiceError(WASMError):
    """Raised when systemd service operations fail."""
    pass


class SiteError(WASMError):
    """Raised when site configuration fails."""
    pass


class NginxError(SiteError):
    """Raised when Nginx operations fail."""
    pass


class ApacheError(SiteError):
    """Raised when Apache operations fail."""
    pass


class CertificateError(WASMError):
    """Raised when SSL certificate operations fail."""
    pass


class CommandError(WASMError):
    """Raised when a shell command execution fails."""
    
    def __init__(self, message: str, command: str = "", exit_code: int = 0, stderr: str = ""):
        self.command = command
        self.exit_code = exit_code
        self.stderr = stderr
        details = ""
        if command:
            details += f"Command: {command}\n"
        if exit_code:
            details += f"Exit code: {exit_code}\n"
        if stderr:
            details += f"Error output: {stderr}"
        super().__init__(message, details.strip())


class DependencyError(WASMError):
    """Raised when a required dependency is missing."""
    pass


class PermissionError(WASMError):
    """Raised when there are insufficient permissions."""
    pass


class PortError(WASMError):
    """Raised when there are port-related issues."""
    pass


class DomainError(WASMError):
    """Raised when there are domain-related issues."""
    pass


class TemplateError(WASMError):
    """Raised when template rendering fails."""
    pass


class RollbackError(WASMError):
    """Raised when rollback operation fails."""
    pass


class MonitorError(WASMError):
    """Raised when process monitoring operations fail."""
    pass


class AIAnalysisError(WASMError):
    """Raised when AI analysis fails."""
    pass


class EmailError(WASMError):
    """Raised when email notification fails."""
    pass


class SSHError(WASMError):
    """
    Raised when SSH authentication or configuration fails.
    
    Provides detailed guidance for resolving SSH issues.
    """
    pass


class SetupError(WASMError):
    """
    Raised when required setup/configuration is missing.
    
    Used when prerequisites are not met (e.g., missing SSH keys,
    missing dependencies, etc.)
    """
    pass
