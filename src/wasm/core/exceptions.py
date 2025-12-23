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


class OutOfMemoryError(BuildError):
    """
    Raised when build fails due to Out of Memory (OOM) condition.
    
    This is detected when the process exits with code 137 (128 + SIGKILL)
    which typically indicates the OOM killer terminated the process.
    """
    
    def __init__(self, message: str = "Build killed due to insufficient memory", details: str = ""):
        suggestions = """
The build process was killed by the system (exit code 137), typically caused by
insufficient RAM. Next.js/Turbopack builds can require 2-4GB+ of memory.

Solutions to try:

1. Add swap space (if not already configured):
   sudo fallocate -l 4G /swapfile
   sudo chmod 600 /swapfile
   sudo mkswap /swapfile
   sudo swapon /swapfile
   echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab

2. Limit Node.js memory usage:
   Add to .env file: NODE_OPTIONS="--max-old-space-size=1536"
   Then redeploy: wasm update <domain>

3. Build locally and deploy pre-built:
   - Build on your local machine: npm run build
   - Commit the .next folder (remove from .gitignore)
   - Push changes and update: wasm update <domain>

4. Use a server with more RAM (recommended: 2GB+ for Next.js apps)

5. Disable Turbopack (if using Next.js 15+):
   In next.config.js, ensure you're not using experimental turbo features
   for production builds."""
        
        if details:
            full_details = f"{details}\n{suggestions}"
        else:
            full_details = suggestions
        
        super().__init__(message, full_details)


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


class DatabaseError(WASMError):
    """Base exception for database operations."""
    pass


class DatabaseConnectionError(DatabaseError):
    """Raised when database connection fails."""
    pass


class DatabaseNotFoundError(DatabaseError):
    """Raised when a database does not exist."""
    pass


class DatabaseExistsError(DatabaseError):
    """Raised when trying to create a database that already exists."""
    pass


class DatabaseUserError(DatabaseError):
    """Raised when database user operations fail."""
    pass


class DatabaseEngineError(DatabaseError):
    """Raised when database engine operations fail (install, start, stop)."""
    pass


class DatabaseBackupError(DatabaseError):
    """Raised when database backup/restore operations fail."""
    pass


class DatabaseQueryError(DatabaseError):
    """Raised when a database query fails."""
    pass
