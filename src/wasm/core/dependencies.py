"""
System dependency management for WASM.

Handles checking, installing, and managing system and runtime dependencies
needed for deploying various types of applications.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import ClassVar, TypedDict

from wasm.core.exceptions import SecurityError
from wasm.core.runner import CommandRunner, get_runner
from wasm.core.utils import TRUSTED_INSTALLER_URLS, run_trusted_installer

#: Package installs pull from the network and unpack; a minute is not enough
#: and no deadline at all is how a deploy hangs on a stalled mirror.
INSTALL_TIMEOUT = 600

#: System probes ("is the docker daemon up?") answer immediately or not at all.
PROBE_TIMEOUT = 20


class PackageManagerInfo(TypedDict):
    """Static facts about a Node.js package manager."""

    lock_file: str
    install_cmd: str
    comes_with_node: bool


class PackageManagerSummary(TypedDict):
    """Installed state of a single Node.js package manager."""

    installed: bool
    version: str | None


class RuntimeSummary(TypedDict):
    """Installed state of a language runtime and its package managers."""

    installed: bool
    version: str | None
    package_managers: dict[str, PackageManagerSummary]


class SetupSummary(TypedDict):
    """System readiness report produced by :meth:`DependencyChecker.get_setup_summary`."""

    system_ready: bool
    webserver: str | None
    nodejs: RuntimeSummary
    python: RuntimeSummary
    missing_required: list[str]
    missing_optional: list[str]
    recommendations: list[str]


def _chained_commands(script: str) -> list[list[str]]:
    """
    Split an install recipe into the argument vectors that follow the installer.

    Recipes in this module are written as human-readable shell one-liners
    ("curl ... | bash - && apt-get install -y nodejs"). Only the part after the
    installer pipeline is turned into commands, and each is a plain argument
    vector: no quoting, no redirection, no substitution.

    Args:
        script: The recipe string from a :class:`Dependency`.

    Returns:
        One argument vector per chained command, in order.
    """
    if "&&" not in script:
        return []
    vectors: list[list[str]] = []
    for chunk in script.split("&&")[1:]:
        # ``sudo`` in the recipes predates decision D6 (WASM runs as root).
        words = [w for w in chunk.split() if w and w != "sudo"]
        if words:
            vectors.append(words)
    return vectors


def _npm_global_install_argv(script: str) -> list[str] | None:
    """
    Recognise the ``npm install -g <package>`` recipe shape.

    Args:
        script: The recipe string from a :class:`Dependency`.

    Returns:
        The argument vector to run, or None when the recipe is a different
        shape and must not be executed.
    """
    words = script.split()
    if words[:2] != ["npm", "install"]:
        return None
    # Anything with shell metacharacters is not the simple shape it looks like.
    if any(ch in script for ch in "|;&><$`\n"):
        return None
    return words


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
    install_apt: str | None = None  # apt package name
    install_script: str | None = None  # Custom install script/URL
    version_flag: str = "--version"
    min_version: str | None = None


# Core system dependencies
SYSTEM_DEPENDENCIES: list[Dependency] = [
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
WEBSERVER_DEPENDENCIES: list[Dependency] = [
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
NODEJS_DEPENDENCIES: list[Dependency] = [
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

# Docker dependencies
DOCKER_DEPENDENCIES: list[Dependency] = [
    Dependency(
        name="docker",
        command="docker",
        description="Container runtime",
        required=False,
        category="docker",
        install_apt="docker.io",
    ),
    Dependency(
        name="docker-compose",
        command="docker",
        description="Docker Compose (v2 plugin)",
        required=False,
        category="docker",
        version_flag="compose version",
    ),
]

# Python dependencies
PYTHON_DEPENDENCIES: list[Dependency] = [
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
    ALL_DEPENDENCIES: ClassVar[dict[str, list[Dependency]]] = {
        "system": SYSTEM_DEPENDENCIES,
        "webserver": WEBSERVER_DEPENDENCIES,
        "nodejs": NODEJS_DEPENDENCIES,
        "python": PYTHON_DEPENDENCIES,
        "docker": DOCKER_DEPENDENCIES,
    }

    # Package manager info
    PACKAGE_MANAGERS: ClassVar[dict[str, PackageManagerInfo]] = {
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

    def __init__(self, verbose: bool = False, runner: CommandRunner | None = None):
        """
        Initialize the dependency checker.

        Args:
            verbose: Enable verbose output.
            runner: Command runner used to probe the system. Defaults to the
                process-wide runner.
        """
        self.verbose = verbose
        self._runner = runner

    @property
    def runner(self) -> CommandRunner:
        """The command runner this checker probes the system with."""
        return self._runner if self._runner is not None else get_runner()

    def check_command(self, command: str) -> bool:
        """
        Check if a command exists in PATH.

        Args:
            command: Command name to check.

        Returns:
            True if command exists.
        """
        return self.runner.exists(command)

    def get_version(self, command: str, version_flag: str = "--version") -> str | None:
        """
        Get the version of an installed command.

        Args:
            command: Command name.
            version_flag: Flag to get version.

        Returns:
            Version string, or None when the command is absent or silent.
        """
        # Version probes are quick; a program that does not answer promptly is
        # more likely to be waiting on something than to be slow.
        result = self.runner.run([command, version_flag], timeout=15)
        if result.success:
            output = result.stdout.strip() or result.stderr.strip()
            return output.split("\n")[0] if output else None
        return None

    def check_dependency(self, dep: Dependency) -> tuple[DependencyStatus, str | None]:
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
        categories: list[str] | None = None,
    ) -> dict[str, dict[str, tuple[DependencyStatus, str | None]]]:
        """
        Check all dependencies in specified categories.

        Args:
            categories: Categories to check (None for all).

        Returns:
            Dict of category -> {name: (status, version)}.
        """
        if categories is None:
            categories = list(self.ALL_DEPENDENCIES.keys())

        results: dict[str, dict[str, tuple[DependencyStatus, str | None]]] = {}

        for category in categories:
            deps = self.ALL_DEPENDENCIES.get(category, [])
            results[category] = {}

            for dep in deps:
                status, version = self.check_dependency(dep)
                results[category][dep.name] = (status, version)

        return results

    def check_package_manager(self, pm: str) -> tuple[bool, str | None, str]:
        """
        Check if a specific package manager is available.

        Args:
            pm: Package manager name (npm, pnpm, yarn, bun).

        Returns:
            Tuple of (is_installed, version, install_instructions).
        """
        is_installed = self.check_command(pm)
        version = self.get_version(pm) if is_installed else None

        pm_info = self.PACKAGE_MANAGERS.get(pm)
        install_cmd = pm_info["install_cmd"] if pm_info else f"npm install -g {pm}"

        install_instructions = f"Install with: {install_cmd}"

        return is_installed, version, install_instructions

    def detect_required_package_manager(self, app_path: Path) -> str | None:
        """
        Detect which package manager a project requires based on lock files.

        Args:
            app_path: Path to the application.

        Returns:
            Package manager name or None if not detected.
        """
        for pm, info in self.PACKAGE_MANAGERS.items():
            if (app_path / info["lock_file"]).exists():
                return pm

        # Default to npm if package.json exists
        if (app_path / "package.json").exists():
            return "npm"

        return None

    def get_missing_required(self) -> list[Dependency]:
        """
        Get list of missing required dependencies.

        Returns:
            List of missing required dependencies.
        """
        missing = []

        for deps in self.ALL_DEPENDENCIES.values():
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
        app_path: Path | None = None,
    ) -> tuple[bool, list[str], list[str]]:
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

        elif app_type == "docker-compose":
            # Need Docker and Docker Compose
            if not self.check_command("docker"):
                missing.append("docker: Docker is required for docker-compose deployments")
            else:
                # Verify Docker daemon is running
                result = self.runner.run(["docker", "info"], timeout=PROBE_TIMEOUT)
                if not result.success:
                    missing.append(
                        "docker: Docker daemon is not running. Start with: systemctl start docker"
                    )

                # Check Docker Compose v2
                result = self.runner.run(["docker", "compose", "version"], timeout=PROBE_TIMEOUT)
                if not result.success:
                    missing.append("docker compose: Docker Compose v2 plugin is required")

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
                result = self.runner.run(["certbot", "plugins"], timeout=PROBE_TIMEOUT)
                if result.success and "* nginx" not in result.stdout:
                    warnings.append(
                        "certbot nginx plugin not installed. "
                        "Webroot method will be used for SSL. "
                        "For faster SSL setup, install: apt install python3-certbot-nginx"
                    )

        can_deploy = len(missing) == 0
        return can_deploy, missing, warnings

    def get_available_package_managers(self) -> list[str]:
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

    def install_dependency(self, dep: Dependency) -> tuple[bool, str]:
        """
        Install a dependency.

        Only two installation shapes are supported, and both are argument
        vectors: an apt package, or a whitelisted installer script fetched and
        piped to bash. The previous third shape, handing an arbitrary
        ``install_script`` string to ``sh -c``, is gone: the strings came from a
        table in this module, but the mechanism accepted anything, so a future
        entry (or a future caller building a Dependency) got a shell for free.

        Args:
            dep: Dependency to install.

        Returns:
            Tuple of (success, message).
        """
        if dep.install_apt:
            result = self.runner.run(
                ["apt-get", "install", "-y", dep.install_apt], timeout=INSTALL_TIMEOUT
            )
            if result.success:
                return True, f"Installed {dep.name} via apt"
            return False, f"Failed to install {dep.name}: {result.stderr}"

        if not dep.install_script:
            return False, f"No installation method available for {dep.name}"

        script = dep.install_script

        # A trusted installer URL: fetch it, run it, then run whatever the
        # recipe chains after it as its own argument vector.
        for url in TRUSTED_INSTALLER_URLS:
            if url not in script:
                continue
            try:
                result = run_trusted_installer(url, runner=self.runner)
            except SecurityError as e:
                return False, f"Failed to install {dep.name}: {e}"
            if not result.success:
                return False, f"Failed to install {dep.name}: {result.stderr}"

            for follow_up in _chained_commands(script):
                result = self.runner.run(follow_up, timeout=INSTALL_TIMEOUT)
                if not result.success:
                    return False, f"Failed to install {dep.name}: {result.stderr}"
            return True, f"Installed {dep.name}"

        argv = _npm_global_install_argv(script)
        if argv is not None:
            result = self.runner.run(argv, timeout=INSTALL_TIMEOUT)
            if result.success:
                return True, f"Installed {dep.name}"
            return False, f"Failed to install {dep.name}: {result.stderr}"

        return (
            False,
            f"Unsupported install recipe for {dep.name}: {script}",
        )

    def install_package_manager(self, pm: str) -> tuple[bool, str]:
        """
        Install a Node.js package manager.

        Args:
            pm: Package manager name.

        Returns:
            Tuple of (success, message).
        """
        # First verify npm/node is available
        if not self.check_command("npm"):
            return (
                False,
                "npm is required to install other package managers. Please install Node.js first.",
            )

        pm_info = self.PACKAGE_MANAGERS.get(pm)
        if not pm_info:
            return False, f"Unknown package manager: {pm}"

        if pm == "bun":
            # Bun has its own trusted installer
            try:
                result = run_trusted_installer("https://bun.sh/install", runner=self.runner)
            except SecurityError as e:
                return False, f"Failed to install bun: {e}"
        else:
            # Install via npm globally
            result = self.runner.run(["npm", "install", "-g", pm], timeout=INSTALL_TIMEOUT)

        if result.success:
            return True, f"Successfully installed {pm}"

        return False, f"Failed to install {pm}: {result.stderr}"

    def get_setup_summary(self) -> SetupSummary:
        """
        Get a comprehensive summary of system setup status.

        Returns:
            The setup status, keyed as described by :class:`SetupSummary`.
        """
        nodejs: RuntimeSummary = {
            "installed": False,
            "version": None,
            "package_managers": {},
        }
        python: RuntimeSummary = {"installed": False, "version": None, "package_managers": {}}
        summary: SetupSummary = {
            "system_ready": True,
            "webserver": None,
            "nodejs": nodejs,
            "python": python,
            "missing_required": [],
            "missing_optional": [],
            "recommendations": [],
        }

        # Check system deps
        for dep in SYSTEM_DEPENDENCIES:
            status, _version = self.check_dependency(dep)
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
            summary["recommendations"].append("Install nginx: apt install nginx")

        # Check Node.js
        if self.check_command("node"):
            nodejs["installed"] = True
            nodejs["version"] = self.get_version("node")

            # Check package managers
            for pm in ["npm", "pnpm", "yarn", "bun"]:
                is_installed, version, _ = self.check_package_manager(pm)
                nodejs["package_managers"][pm] = {
                    "installed": is_installed,
                    "version": version,
                }
        else:
            summary["recommendations"].append(
                "Install Node.js: wasm setup init (installs the NodeSource 20.x release)"
            )

        # Check Python
        if self.check_command("python3"):
            python["installed"] = True
            python["version"] = self.get_version("python3")

        # Check certbot
        if not self.check_command("certbot"):
            summary["missing_optional"].append("certbot")
            summary["recommendations"].append(
                "Install certbot for SSL: apt install certbot python3-certbot-nginx"
            )

        return summary


def check_deployment_ready(
    app_type: str,
    package_manager: str = "auto",
    app_path: Path | None = None,
    verbose: bool = False,
    runner: CommandRunner | None = None,
) -> tuple[bool, list[str], list[str]]:
    """
    Quick check if system is ready for deployment.

    Args:
        app_type: Application type.
        package_manager: Package manager to use.
        app_path: Path to application.
        verbose: Enable verbose output.
        runner: Command runner used to probe the system. Defaults to the
            process-wide runner.

    Returns:
        Tuple of (ready, missing, warnings).
    """
    checker = DependencyChecker(verbose=verbose, runner=runner)
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
