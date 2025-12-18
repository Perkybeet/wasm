"""
System dependency management for WASM.

Handles checking, installing, and managing system and runtime dependencies
needed for deploying various types of applications.
"""

import shutil
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from wasm.core.utils import run_command, run_command_sudo, command_exists


class DependencyStatus(Enum):
    """Status of a dependency check."""
    INSTALLED = "installed"
    NOT_INSTALLED = "not_installed"
    OUTDATED = "outdated"
    UNKNOWN = "unknown"


@dataclass
class Dependency:
    """Represents a system dependency."""
    name: str
    command: str  # Command to check if installed
    description: str
    required: bool = True
    category: str = "system"  # system, nodejs, python, webserver
    install_apt: Optional[str] = None  # apt package name
    install_script: Optional[str] = None  # Custom install script/URL
    version_flag: str = "--version"
    min_version: Optional[str] = None


# Core system dependencies
SYSTEM_DEPENDENCIES: List[Dependency] = [
    Dependency(
        name="git",
        command="git",
        description="Version control system",
        required=True,
        category="system",
        install_apt="git",
    ),
    Dependency(
        name="curl",
        command="curl",
        description="Data transfer tool",
        required=True,
        category="system",
        install_apt="curl",
    ),
    Dependency(
        name="wget",
        command="wget",
        description="Network downloader",
        required=False,
        category="system",
        install_apt="wget",
    ),
]

# Web server dependencies
WEBSERVER_DEPENDENCIES: List[Dependency] = [
    Dependency(
        name="nginx",
        command="nginx",
        description="High-performance web server",
        required=False,
        category="webserver",
        install_apt="nginx",
        version_flag="-v",
    ),
    Dependency(
        name="apache2",
        command="apache2",
        description="Apache HTTP Server",
        required=False,
        category="webserver",
        install_apt="apache2",
        version_flag="-v",
    ),
    Dependency(
        name="certbot",
        command="certbot",
        description="Let's Encrypt SSL certificate tool",
        required=False,
        category="webserver",
        install_apt="certbot",
    ),
]

# Node.js runtime and package managers
NODEJS_DEPENDENCIES: List[Dependency] = [
    Dependency(
        name="node",
        command="node",
        description="Node.js JavaScript runtime",
        required=False,
        category="nodejs",
        install_script="curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash - && sudo apt-get install -y nodejs",
        min_version="18.0.0",
    ),
    Dependency(
        name="npm",
        command="npm",
        description="Node Package Manager (comes with Node.js)",
        required=False,
        category="nodejs",
    ),
    Dependency(
        name="pnpm",
        command="pnpm",
        description="Fast, disk space efficient package manager",
        required=False,
        category="nodejs",
        install_script="npm install -g pnpm",
    ),
    Dependency(
        name="yarn",
        command="yarn",
        description="Fast, reliable, and secure dependency management",
        required=False,
        category="nodejs",
        install_script="npm install -g yarn",
    ),
    Dependency(
        name="bun",
        command="bun",
        description="Fast all-in-one JavaScript runtime & toolkit",
        required=False,
        category="nodejs",
        install_script="curl -fsSL https://bun.sh/install | bash",
    ),
]

# Python dependencies
PYTHON_DEPENDENCIES: List[Dependency] = [
    Dependency(
        name="python3",
        command="python3",
        description="Python programming language",
        required=False,
        category="python",
        install_apt="python3",
        min_version="3.10",
    ),
    Dependency(
        name="pip3",
        command="pip3",
        description="Python package installer",
        required=False,
        category="python",
        install_apt="python3-pip",
    ),
    Dependency(
        name="python3-venv",
        command="python3",
        description="Python virtual environment support",
        required=False,
        category="python",
        install_apt="python3-venv",
    ),
]


class DependencyChecker:
    """
    Utility class to check and manage system dependencies.
    """
    
    # All known dependencies by category
    ALL_DEPENDENCIES: Dict[str, List[Dependency]] = {
        "system": SYSTEM_DEPENDENCIES,
        "webserver": WEBSERVER_DEPENDENCIES,
        "nodejs": NODEJS_DEPENDENCIES,
        "python": PYTHON_DEPENDENCIES,
    }
    
    # Package manager info
    PACKAGE_MANAGERS = {
        "npm": {
            "lock_file": "package-lock.json",
            "install_cmd": "npm install -g npm",
            "comes_with_node": True,
        },
        "pnpm": {
            "lock_file": "pnpm-lock.yaml",
            "install_cmd": "npm install -g pnpm",
            "comes_with_node": False,
        },
        "yarn": {
            "lock_file": "yarn.lock",
            "install_cmd": "npm install -g yarn",
            "comes_with_node": False,
        },
        "bun": {
            "lock_file": "bun.lockb",
            "install_cmd": "curl -fsSL https://bun.sh/install | bash",
            "comes_with_node": False,
        },
    }
    
    def __init__(self, verbose: bool = False):
        """
        Initialize the dependency checker.
        
        Args:
            verbose: Enable verbose output.
        """
        self.verbose = verbose
    
    def check_command(self, command: str) -> bool:
        """
        Check if a command exists in PATH.
        
        Args:
            command: Command name to check.
            
        Returns:
            True if command exists.
        """
        return command_exists(command)
    
    def get_version(self, command: str, version_flag: str = "--version") -> Optional[str]:
        """
        Get the version of an installed command.
        
        Args:
            command: Command name.
            version_flag: Flag to get version.
            
        Returns:
            Version string or None.
        """
        result = run_command([command, version_flag])
        if result.success:
            # Try to extract version from output
            output = result.stdout.strip() or result.stderr.strip()
            return output.split("\n")[0] if output else None
        return None
    
    def check_dependency(self, dep: Dependency) -> Tuple[DependencyStatus, Optional[str]]:
        """
        Check the status of a single dependency.
        
        Args:
            dep: Dependency to check.
            
        Returns:
            Tuple of (status, version).
        """
        if not self.check_command(dep.command):
            return DependencyStatus.NOT_INSTALLED, None
        
        version = self.get_version(dep.command, dep.version_flag)
        return DependencyStatus.INSTALLED, version
    
    def check_all_dependencies(
        self,
        categories: Optional[List[str]] = None,
    ) -> Dict[str, Dict[str, Tuple[DependencyStatus, Optional[str]]]]:
        """
        Check all dependencies in specified categories.
        
        Args:
            categories: Categories to check (None for all).
            
        Returns:
            Dict of category -> {name: (status, version)}.
        """
        if categories is None:
            categories = list(self.ALL_DEPENDENCIES.keys())
        
        results: Dict[str, Dict[str, Tuple[DependencyStatus, Optional[str]]]] = {}
        
        for category in categories:
            deps = self.ALL_DEPENDENCIES.get(category, [])
            results[category] = {}
            
            for dep in deps:
                status, version = self.check_dependency(dep)
                results[category][dep.name] = (status, version)
        
        return results
    
    def check_package_manager(self, pm: str) -> Tuple[bool, Optional[str], str]:
        """
        Check if a specific package manager is available.
        
        Args:
            pm: Package manager name (npm, pnpm, yarn, bun).
            
        Returns:
            Tuple of (is_installed, version, install_instructions).
        """
        is_installed = self.check_command(pm)
        version = self.get_version(pm) if is_installed else None
        
        pm_info = self.PACKAGE_MANAGERS.get(pm, {})
        install_cmd = pm_info.get("install_cmd", f"npm install -g {pm}")
        
        install_instructions = f"Install with: {install_cmd}"
        
        return is_installed, version, install_instructions
    
    def detect_required_package_manager(self, app_path: Path) -> Optional[str]:
        """
        Detect which package manager a project requires based on lock files.
        
        Args:
            app_path: Path to the application.
            
        Returns:
            Package manager name or None if not detected.
        """
        for pm, info in self.PACKAGE_MANAGERS.items():
            lock_file = info.get("lock_file")
            if lock_file and (app_path / lock_file).exists():
                return pm
        
        # Default to npm if package.json exists
        if (app_path / "package.json").exists():
            return "npm"
        
        return None
    
    def get_missing_required(self) -> List[Dependency]:
        """
        Get list of missing required dependencies.
        
        Returns:
            List of missing required dependencies.
        """
        missing = []
        
        for category, deps in self.ALL_DEPENDENCIES.items():
            for dep in deps:
                if dep.required:
                    status, _ = self.check_dependency(dep)
                    if status == DependencyStatus.NOT_INSTALLED:
                        missing.append(dep)
        
        return missing
    
    def verify_deployment_requirements(
        self,
        app_type: str,
        package_manager: str = "auto",
        app_path: Optional[Path] = None,
    ) -> Tuple[bool, List[str], List[str]]:
        """
        Verify all requirements for a deployment are met.
        
        Args:
            app_type: Type of application (nextjs, nodejs, python, static).
            package_manager: Requested package manager.
            app_path: Path to application (for detection).
            
        Returns:
            Tuple of (can_deploy, missing_deps, warnings).
        """
        missing = []
        warnings = []
        
        # Check basic system deps
        for dep in SYSTEM_DEPENDENCIES:
            if dep.required and not self.check_command(dep.command):
                missing.append(f"{dep.name}: {dep.description}")
        
        # Check app-type specific deps
        if app_type in ["nextjs", "nodejs", "vite"]:
            # Need Node.js
            if not self.check_command("node"):
                missing.append("node: Node.js runtime is required for this app type")
            else:
                # Node.js is available, check package managers
                available_pms = self.get_available_package_managers()
                
                if not available_pms:
                    missing.append("No package manager available. Install Node.js with npm.")
                else:
                    # Determine required package manager
                    required_pm = package_manager
                    if required_pm == "auto" and app_path:
                        required_pm = self.detect_required_package_manager(app_path) or "npm"
                    
                    if required_pm and required_pm != "auto" and required_pm not in available_pms:
                        # The requested/detected PM is not available, but others are
                        available_list = ", ".join(available_pms)
                        warnings.append(
                            f"Package manager '{required_pm}' not installed. "
                            f"Available: {available_list}. "
                            f"Use --pm to specify one, or install {required_pm}."
                        )
        
        elif app_type == "python":
            if not self.check_command("python3"):
                missing.append("python3: Python 3 runtime is required for this app type")
            if not self.check_command("pip3"):
                warnings.append("pip3: Python package manager is recommended")
        
        # Check webserver
        has_nginx = self.check_command("nginx")
        has_apache = self.check_command("apache2")
        
        if not has_nginx and not has_apache:
            missing.append("nginx/apache2: A web server is required")
        
        # Check certbot for SSL
        if not self.check_command("certbot"):
            warnings.append("certbot: SSL certificate tool not found. SSL will be unavailable.")
        else:
            # Check if certbot nginx plugin is available when using nginx
            if has_nginx:
                result = run_command(["certbot", "plugins"])
                if result.success and "* nginx" not in result.stdout:
                    warnings.append(
                        "certbot nginx plugin not installed. "
                        "Webroot method will be used for SSL. "
                        "For faster SSL setup, install: sudo apt install python3-certbot-nginx"
                    )
        
        can_deploy = len(missing) == 0
        return can_deploy, missing, warnings
    
    def get_available_package_managers(self) -> List[str]:
        """
        Get list of available/installed package managers.
        
        Returns:
            List of installed package manager names.
        """
        available = []
        for pm in ["npm", "pnpm", "yarn", "bun"]:
            if self.check_command(pm):
                available.append(pm)
        return available
    
    def install_dependency(self, dep: Dependency) -> Tuple[bool, str]:
        """
        Install a dependency.
        
        Args:
            dep: Dependency to install.
            
        Returns:
            Tuple of (success, message).
        """
        if dep.install_apt:
            result = run_command_sudo(["apt-get", "install", "-y", dep.install_apt])
            if result.success:
                return True, f"Installed {dep.name} via apt"
            return False, f"Failed to install {dep.name}: {result.stderr}"
        
        if dep.install_script:
            # Run install script
            result = run_command(dep.install_script, shell=True)
            if result.success:
                return True, f"Installed {dep.name}"
            return False, f"Failed to install {dep.name}: {result.stderr}"
        
        return False, f"No installation method available for {dep.name}"
    
    def install_package_manager(self, pm: str) -> Tuple[bool, str]:
        """
        Install a Node.js package manager.
        
        Args:
            pm: Package manager name.
            
        Returns:
            Tuple of (success, message).
        """
        # First verify npm/node is available
        if not self.check_command("npm"):
            return False, "npm is required to install other package managers. Please install Node.js first."
        
        pm_info = self.PACKAGE_MANAGERS.get(pm)
        if not pm_info:
            return False, f"Unknown package manager: {pm}"
        
        install_cmd = pm_info.get("install_cmd", "")
        
        if pm == "bun":
            # Bun has its own installer
            result = run_command(install_cmd, shell=True)
        else:
            # Install via npm globally
            result = run_command_sudo(["npm", "install", "-g", pm])
        
        if result.success:
            return True, f"Successfully installed {pm}"
        
        return False, f"Failed to install {pm}: {result.stderr}"
    
    def get_setup_summary(self) -> Dict[str, any]:
        """
        Get a comprehensive summary of system setup status.
        
        Returns:
            Dict with setup status information.
        """
        summary = {
            "system_ready": True,
            "webserver": None,
            "nodejs": {"installed": False, "version": None, "package_managers": {}},
            "python": {"installed": False, "version": None},
            "missing_required": [],
            "missing_optional": [],
            "recommendations": [],
        }
        
        # Check system deps
        for dep in SYSTEM_DEPENDENCIES:
            status, version = self.check_dependency(dep)
            if status == DependencyStatus.NOT_INSTALLED:
                if dep.required:
                    summary["system_ready"] = False
                    summary["missing_required"].append(dep.name)
                else:
                    summary["missing_optional"].append(dep.name)
        
        # Check webserver
        if self.check_command("nginx"):
            summary["webserver"] = "nginx"
        elif self.check_command("apache2"):
            summary["webserver"] = "apache2"
        else:
            summary["system_ready"] = False
            summary["missing_required"].append("webserver (nginx or apache2)")
            summary["recommendations"].append("Install nginx: sudo apt install nginx")
        
        # Check Node.js
        if self.check_command("node"):
            summary["nodejs"]["installed"] = True
            summary["nodejs"]["version"] = self.get_version("node")
            
            # Check package managers
            for pm in ["npm", "pnpm", "yarn", "bun"]:
                is_installed, version, _ = self.check_package_manager(pm)
                summary["nodejs"]["package_managers"][pm] = {
                    "installed": is_installed,
                    "version": version,
                }
        else:
            summary["recommendations"].append(
                "Install Node.js: curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash - && sudo apt install -y nodejs"
            )
        
        # Check Python
        if self.check_command("python3"):
            summary["python"]["installed"] = True
            summary["python"]["version"] = self.get_version("python3")
        
        # Check certbot
        if not self.check_command("certbot"):
            summary["missing_optional"].append("certbot")
            summary["recommendations"].append("Install certbot for SSL: sudo apt install certbot python3-certbot-nginx")
        
        return summary


def check_deployment_ready(
    app_type: str,
    package_manager: str = "auto",
    app_path: Optional[Path] = None,
    verbose: bool = False,
) -> Tuple[bool, List[str], List[str]]:
    """
    Quick check if system is ready for deployment.
    
    Args:
        app_type: Application type.
        package_manager: Package manager to use.
        app_path: Path to application.
        verbose: Enable verbose output.
        
    Returns:
        Tuple of (ready, missing, warnings).
    """
    checker = DependencyChecker(verbose=verbose)
    return checker.verify_deployment_requirements(app_type, package_manager, app_path)


def get_package_manager_install_hint(pm: str) -> str:
    """
    Get installation instructions for a package manager.
    
    Args:
        pm: Package manager name.
        
    Returns:
        Installation instructions string.
    """
    hints = {
        "npm": "npm comes with Node.js. Install Node.js first.",
        "pnpm": "Install pnpm: npm install -g pnpm\n  Or: curl -fsSL https://get.pnpm.io/install.sh | sh",
        "yarn": "Install yarn: npm install -g yarn",
        "bun": "Install bun: curl -fsSL https://bun.sh/install | bash",
    }
    return hints.get(pm, f"Install {pm} using npm: npm install -g {pm}")
