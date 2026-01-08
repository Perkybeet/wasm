"""
Systemd service manager for WASM.
"""

import re
from pathlib import Path
from typing import Dict, List, Optional

from jinja2 import Environment, PackageLoader, TemplateNotFound

from wasm.core.config import SYSTEMD_DIR
from wasm.core.exceptions import ServiceError, TemplateError
from wasm.core.store import get_store, Service
from wasm.core.utils import read_file, remove_file, write_file
from wasm.managers.base_manager import BaseManager


class ServiceManager(BaseManager):
    """
    Manager for systemd services.
    
    Handles creating, starting, stopping, and managing systemd services.
    """
    
    SYSTEMD_DIR = SYSTEMD_DIR
    SERVICE_PREFIX = "wasm-"
    
    def __init__(self, verbose: bool = False):
        """Initialize service manager."""
        super().__init__(verbose=verbose)
        self.store = get_store()
        
        try:
            self.jinja_env = Environment(
                loader=PackageLoader("wasm", "templates/systemd"),
                trim_blocks=True,
                lstrip_blocks=True,
            )
        except Exception:
            self.jinja_env = None
    
    def is_installed(self) -> bool:
        """Check if systemd is available."""
        result = self._run(["which", "systemctl"])
        return result.success
    
    def get_version(self) -> Optional[str]:
        """Get systemd version."""
        result = self._run(["systemctl", "--version"])
        if result.success:
            match = re.search(r"systemd (\d+)", result.stdout)
            if match:
                return match.group(1)
        return None
    
    def _get_service_name(self, name: str) -> str:
        """
        Get full service name with prefix.
        
        Args:
            name: Base service name.
            
        Returns:
            Full service name.
        """
        if name.startswith(self.SERVICE_PREFIX):
            return name
        return f"{self.SERVICE_PREFIX}{name}"
    
    def _get_service_file(self, name: str) -> Path:
        """
        Get service file path.
        
        Args:
            name: Service name.
            
        Returns:
            Path to service file.
        """
        service_name = self._get_service_name(name)
        if not service_name.endswith(".service"):
            service_name = f"{service_name}.service"
        return self.SYSTEMD_DIR / service_name
    
    def daemon_reload(self) -> bool:
        """
        Reload systemd daemon.
        
        Returns:
            True if successful.
        """
        result = self._run_sudo(["systemctl", "daemon-reload"])
        return result.success
    
    def service_exists(self, name: str) -> bool:
        """
        Check if a service exists.
        
        Args:
            name: Service name.
            
        Returns:
            True if service exists.
        """
        service_file = self._get_service_file(name)
        return service_file.exists()
    
    def list_services(self, all_services: bool = False) -> List[Dict]:
        """
        List WASM-managed services.
        
        Args:
            all_services: Include all system services.
            
        Returns:
            List of service information dictionaries.
        """
        services = []
        
        # List services starting with our prefix
        pattern = "*" if all_services else f"{self.SERVICE_PREFIX}*"
        result = self._run([
            "systemctl", "list-units",
            "--type=service",
            "--all",
            "--no-pager",
            "--plain",
            pattern,
        ])
        
        if not result.success:
            return services
        
        for line in result.stdout.strip().split("\n"):
            if not line or line.startswith("UNIT"):
                continue
            
            parts = line.split()
            if len(parts) >= 4:
                unit_name = parts[0]
                if unit_name.endswith(".service"):
                    name = unit_name.replace(".service", "")
                    if all_services or name.startswith(self.SERVICE_PREFIX):
                        services.append({
                            "name": name,
                            "load": parts[1] if len(parts) > 1 else "unknown",
                            "active": parts[2] if len(parts) > 2 else "unknown",
                            "sub": parts[3] if len(parts) > 3 else "unknown",
                        })
        
        return services
    
    def get_status(self, name: str) -> Dict:
        """
        Get service status.
        
        Args:
            name: Service name.
            
        Returns:
            Dictionary with status information.
        """
        service_name = self._get_service_name(name)
        
        # Check if active
        result = self._run(["systemctl", "is-active", service_name])
        is_active = result.stdout.strip() == "active"
        
        # Check if enabled
        result = self._run(["systemctl", "is-enabled", service_name])
        is_enabled = result.stdout.strip() == "enabled"
        
        # Get detailed status
        result = self._run(["systemctl", "show", service_name, "--no-pager"])
        details = {}
        if result.success:
            for line in result.stdout.split("\n"):
                if "=" in line:
                    key, value = line.split("=", 1)
                    details[key] = value
        
        return {
            "name": service_name,
            "exists": self.service_exists(name),
            "active": is_active,
            "enabled": is_enabled,
            "pid": details.get("MainPID", ""),
            "memory": details.get("MemoryCurrent", ""),
            "uptime": details.get("ActiveEnterTimestamp", ""),
        }
    
    def create_service(
        self,
        name: str,
        command: str,
        working_directory: str,
        user: Optional[str] = None,
        group: Optional[str] = None,
        environment: Optional[Dict[str, str]] = None,
        description: Optional[str] = None,
        template: str = "app",
    ) -> bool:
        """
        Create a new systemd service.
        
        Args:
            name: Service name.
            command: Command to execute.
            working_directory: Working directory for the service.
            user: User to run service as.
            group: Group to run service as.
            environment: Environment variables.
            description: Service description.
            template: Template name.
            
        Returns:
            True if service was created successfully.
            
        Raises:
            ServiceError: If creation fails.
        """
        service_name = self._get_service_name(name)
        
        if self.service_exists(name):
            raise ServiceError(f"Service already exists: {service_name}")
        
        if not self.jinja_env:
            raise ServiceError("Template environment not initialized")
        
        # Prepare context
        ctx = {
            "name": service_name,
            "description": description or f"WASM managed service: {name}",
            "command": command,
            "working_directory": working_directory,
            "user": user or self.config.service_user,
            "group": group or self.config.service_group,
            "environment": environment or {},
        }
        
        # Render template
        try:
            template_obj = self.jinja_env.get_template(f"{template}.service.j2")
            service_content = template_obj.render(**ctx)
        except TemplateNotFound:
            raise TemplateError(f"Template not found: {template}.service.j2")
        except Exception as e:
            raise TemplateError(f"Template rendering failed: {e}")
        
        # Write service file
        service_file = self._get_service_file(name)
        if not write_file(service_file, service_content, sudo=True):
            raise ServiceError(f"Failed to write service file: {service_file}")
        
        # Reload daemon
        self.daemon_reload()
        
        # Register in store
        try:
            svc = Service(
                name=service_name,
                unit_file=str(service_file),
                command=command,
                working_directory=working_directory,
                user=ctx["user"],
                group=ctx["group"],
                environment=environment,
                status="inactive",
                enabled=False,
            )
            self.store.create_service(svc)
        except Exception as e:
            self.logger.debug(f"Could not register service in store: {e}")
        
        self.logger.debug(f"Created service: {service_name}")
        return True
    
    def delete_service(self, name: str) -> bool:
        """
        Delete a service.
        
        Args:
            name: Service name.
            
        Returns:
            True if service was deleted.
        """
        service_name = self._get_service_name(name)
        
        # Stop service if running
        self.stop(name)
        
        # Disable service
        self.disable(name)
        
        # Remove service file
        service_file = self._get_service_file(name)
        if service_file.exists():
            if not remove_file(service_file, sudo=True):
                raise ServiceError(f"Failed to delete service: {service_name}")
        
        # Reload daemon
        self.daemon_reload()
        
        # Remove from store
        try:
            self.store.delete_service(service_name)
        except Exception as e:
            self.logger.debug(f"Could not remove service from store: {e}")
        
        self.logger.debug(f"Deleted service: {service_name}")
        return True
    
    def start(self, name: str) -> bool:
        """
        Start a service.
        
        Args:
            name: Service name.
            
        Returns:
            True if service started successfully.
        """
        service_name = self._get_service_name(name)
        result = self._run_sudo(["systemctl", "start", service_name])
        
        if not result.success:
            raise ServiceError(
                f"Failed to start service: {service_name}",
                details=result.stderr,
            )
        
        # Update store status
        try:
            self.store.update_service_status(service_name, "active")
        except Exception as e:
            self.logger.debug(f"Could not update service status in store: {e}")
        
        return True
    
    def stop(self, name: str) -> bool:
        """
        Stop a service.
        
        Args:
            name: Service name.
            
        Returns:
            True if service stopped successfully.
        """
        service_name = self._get_service_name(name)
        result = self._run_sudo(["systemctl", "stop", service_name])
        
        # Update store status
        try:
            self.store.update_service_status(service_name, "inactive")
        except Exception as e:
            self.logger.debug(f"Could not update service status in store: {e}")
        
        return result.success
    
    def restart(self, name: str) -> bool:
        """
        Restart a service.
        
        Args:
            name: Service name.
            
        Returns:
            True if service restarted successfully.
        """
        service_name = self._get_service_name(name)
        result = self._run_sudo(["systemctl", "restart", service_name])
        
        if not result.success:
            raise ServiceError(
                f"Failed to restart service: {service_name}",
                details=result.stderr,
            )
        
        # Update store status
        try:
            self.store.update_service_status(service_name, "active")
        except Exception as e:
            self.logger.debug(f"Could not update service status in store: {e}")
        
        return True
    
    def enable(self, name: str) -> bool:
        """
        Enable a service to start on boot.
        
        Args:
            name: Service name.
            
        Returns:
            True if service was enabled.
        """
        service_name = self._get_service_name(name)
        result = self._run_sudo(["systemctl", "enable", service_name])
        
        # Update store
        try:
            svc = self.store.get_service(service_name)
            if svc:
                svc.enabled = True
                self.store.update_service(svc)
        except Exception as e:
            self.logger.debug(f"Could not update service in store: {e}")
        
        return result.success
    
    def disable(self, name: str) -> bool:
        """
        Disable a service from starting on boot.
        
        Args:
            name: Service name.
            
        Returns:
            True if service was disabled.
        """
        service_name = self._get_service_name(name)
        result = self._run_sudo(["systemctl", "disable", service_name])
        
        # Update store
        try:
            svc = self.store.get_service(service_name)
            if svc:
                svc.enabled = False
                self.store.update_service(svc)
        except Exception as e:
            self.logger.debug(f"Could not update service in store: {e}")
        
        return result.success
    
    def logs(
        self,
        name: str,
        lines: int = 50,
        follow: bool = False,
    ) -> str:
        """
        Get service logs.
        
        Args:
            name: Service name.
            lines: Number of lines to return.
            follow: Follow log output (not supported in this method).
            
        Returns:
            Log output.
        """
        service_name = self._get_service_name(name)
        
        cmd = ["journalctl", "-u", service_name, "-n", str(lines), "--no-pager"]
        result = self._run(cmd)
        
        return result.stdout if result.success else result.stderr
    
    def get_service_config(self, name: str) -> Optional[str]:
        """
        Get service configuration content.
        
        Args:
            name: Service name.
            
        Returns:
            Service file content or None.
        """
        service_file = self._get_service_file(name)
        return read_file(service_file, sudo=True)
