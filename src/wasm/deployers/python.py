"""
Python deployer for WASM.
"""

import os
from pathlib import Path
from typing import Dict, List
import re

from wasm.deployers.base import BaseDeployer
from wasm.deployers.registry import DeployerRegistry


class PythonDeployer(BaseDeployer):
    """
    Deployer for Python web applications.
    
    Handles deployment of Django, Flask, FastAPI, and other
    Python web frameworks using Gunicorn as the WSGI/ASGI server.
    """
    
    APP_TYPE = "python"
    DISPLAY_NAME = "Python (Django/Flask/FastAPI)"
    
    DETECTION_FILES = [
        "requirements.txt",
        "pyproject.toml",
        "setup.py",
        "Pipfile",
    ]
    
    DEFAULT_PORT = 8000
    
    SYSTEM_DEPS = ["python3", "pip3"]
    
    def __init__(self, verbose: bool = False):
        """Initialize Python deployer."""
        super().__init__(verbose=verbose)
        self.framework = "generic"
        self.wsgi_app = None
        self.asgi_app = None
        self.use_poetry = False
        self.use_pipenv = False
        self.venv_path = None
    
    def detect(self, path: Path) -> bool:
        """Detect if path contains a Python project."""
        for f in self.DETECTION_FILES:
            if (path / f).exists():
                return True
        return False
    
    def _detect_framework(self) -> str:
        """Detect the Python framework used."""
        requirements = self.app_path / "requirements.txt"
        pyproject = self.app_path / "pyproject.toml"
        
        deps_content = ""
        
        if requirements.exists():
            deps_content = requirements.read_text().lower()
        elif pyproject.exists():
            deps_content = pyproject.read_text().lower()
        
        if "django" in deps_content:
            return "django"
        elif "fastapi" in deps_content:
            return "fastapi"
        elif "flask" in deps_content:
            return "flask"
        elif "starlette" in deps_content:
            return "starlette"
        
        return "generic"
    
    def _detect_app_module(self) -> tuple:
        """Detect the WSGI/ASGI application module."""
        wsgi_app = None
        asgi_app = None
        
        # Check for common patterns
        if self.framework == "django":
            # Look for wsgi.py or asgi.py
            for root, dirs, files in os.walk(self.app_path):
                root = Path(root)
                if "wsgi.py" in files:
                    # Get module path
                    rel_path = root.relative_to(self.app_path)
                    module = str(rel_path).replace("/", ".")
                    wsgi_app = f"{module}.wsgi:application"
                if "asgi.py" in files:
                    rel_path = root.relative_to(self.app_path)
                    module = str(rel_path).replace("/", ".")
                    asgi_app = f"{module}.asgi:application"
        
        elif self.framework == "flask":
            # Check for app.py, main.py, or __init__.py
            for filename in ["app.py", "main.py", "application.py"]:
                if (self.app_path / filename).exists():
                    content = (self.app_path / filename).read_text()
                    # Look for Flask app instance
                    match = re.search(r"(\w+)\s*=\s*Flask\(", content)
                    if match:
                        app_var = match.group(1)
                        module = filename.replace(".py", "")
                        wsgi_app = f"{module}:{app_var}"
                        break
        
        elif self.framework == "fastapi":
            # Check for main.py or app.py
            for filename in ["main.py", "app.py", "application.py"]:
                if (self.app_path / filename).exists():
                    content = (self.app_path / filename).read_text()
                    match = re.search(r"(\w+)\s*=\s*FastAPI\(", content)
                    if match:
                        app_var = match.group(1)
                        module = filename.replace(".py", "")
                        asgi_app = f"{module}:{app_var}"
                        break
        
        return wsgi_app, asgi_app
    
    def _detect_package_manager(self) -> tuple:
        """Detect Python package manager."""
        use_poetry = (self.app_path / "poetry.lock").exists()
        use_pipenv = (self.app_path / "Pipfile.lock").exists()
        return use_poetry, use_pipenv
    
    def get_install_command(self) -> List[str]:
        """Get dependency installation command."""
        if self.use_poetry:
            return ["poetry", "install", "--no-dev"]
        elif self.use_pipenv:
            return ["pipenv", "install", "--deploy"]
        else:
            venv_pip = self.venv_path / "bin" / "pip"
            return [str(venv_pip), "install", "-r", "requirements.txt"]
    
    def get_build_command(self) -> List[str]:
        """Get build command (collect static for Django)."""
        if self.framework == "django":
            venv_python = self.venv_path / "bin" / "python"
            return [str(venv_python), "manage.py", "collectstatic", "--noinput"]
        return []
    
    def get_start_command(self) -> str:
        """Get start command using Gunicorn."""
        venv_gunicorn = self.venv_path / "bin" / "gunicorn"
        
        if self.asgi_app:
            # Use uvicorn workers for ASGI
            return (
                f"{venv_gunicorn} {self.asgi_app} "
                f"-w 4 -k uvicorn.workers.UvicornWorker "
                f"-b 0.0.0.0:{self.port}"
            )
        elif self.wsgi_app:
            return (
                f"{venv_gunicorn} {self.wsgi_app} "
                f"-w 4 -b 0.0.0.0:{self.port}"
            )
        else:
            # Fallback
            return f"{venv_gunicorn} app:app -w 4 -b 0.0.0.0:{self.port}"
    
    def pre_install(self) -> bool:
        """Pre-installation hook - create virtual environment."""
        self.framework = self._detect_framework()
        self.use_poetry, self.use_pipenv = self._detect_package_manager()
        
        self.logger.debug(f"Framework: {self.framework}")
        self.logger.debug(f"Poetry: {self.use_poetry}, Pipenv: {self.use_pipenv}")
        
        # Create virtual environment
        self.venv_path = self.app_path / "venv"
        
        if not self.use_poetry and not self.use_pipenv:
            self.logger.substep("Creating virtual environment...")
            result = self._run(
                ["python3", "-m", "venv", str(self.venv_path)],
                cwd=self.app_path,
            )
            if not result.success:
                self.logger.warning("Failed to create virtual environment")
                return False
        
        return True
    
    def post_install(self) -> bool:
        """Post-installation hook - detect app module and install gunicorn."""
        # Detect application module
        self.wsgi_app, self.asgi_app = self._detect_app_module()
        self.logger.debug(f"WSGI app: {self.wsgi_app}")
        self.logger.debug(f"ASGI app: {self.asgi_app}")
        
        # Install gunicorn
        venv_pip = self.venv_path / "bin" / "pip"
        packages = ["gunicorn"]
        
        if self.asgi_app:
            packages.append("uvicorn")
        
        self.logger.substep("Installing gunicorn...")
        result = self._run([str(venv_pip), "install"] + packages)
        if not result.success:
            self.logger.warning("Failed to install gunicorn")
        
        return True
    
    def get_template_context(self) -> Dict:
        """Get template context with Python specifics."""
        context = super().get_template_context()
        context.update({
            "is_python": True,
            "framework": self.framework,
            "venv_path": str(self.venv_path),
        })
        return context


# Register the deployer
DeployerRegistry.register(PythonDeployer)
