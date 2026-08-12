"""
Static site deployer for WASM.
"""

from pathlib import Path
from typing import ClassVar

from wasm.core.fs import FileSystem
from wasm.core.logger import Icons
from wasm.core.runner import CommandRunner
from wasm.deployers.base import BaseDeployer
from wasm.deployers.pipeline import DeployStep
from wasm.deployers.registry import DeployerRegistry


class StaticDeployer(BaseDeployer):
    """
    Deployer for static HTML/CSS/JS sites.

    Serves static files directly through Nginx or Apache
    without any build process or application server.
    """

    APP_TYPE = "static"
    DISPLAY_NAME = "Static Site"

    DETECTION_FILES: ClassVar[list[str]] = ["index.html"]

    DEFAULT_PORT = 80  # Not used for static sites

    # See DEFAULT_DETECTION_PRIORITY in interface.py for the full order.
    DETECTION_PRIORITY = 10

    SYSTEM_DEPS: ClassVar[list[str]] = []

    def __init__(
        self,
        verbose: bool = False,
        runner: CommandRunner | None = None,
        fs: FileSystem | None = None,
    ):
        """
        Initialize the static deployer.

        Args:
            verbose: Enable verbose logging.
            runner: Command runner used for installs and builds. Defaults to the
                process-wide runner.
            fs: Filesystem every change goes through. Defaults to the
                process-wide one.
        """
        super().__init__(verbose=verbose, runner=runner, fs=fs)
        self.static_dir: Path | None = None

    def detect(self, path: Path) -> bool:
        """Detect if path contains a static site."""
        # Check for index.html
        if (path / "index.html").exists():
            # Make sure it's not a framework project
            framework_files = [
                "package.json",
                "requirements.txt",
                "pyproject.toml",
                "Cargo.toml",
                "go.mod",
            ]
            for f in framework_files:
                if (path / f).exists():
                    return False
            return True

        return False

    def get_install_command(self) -> list[str]:
        """No installation needed for static sites."""
        return []

    def get_build_command(self) -> list[str]:
        """No build needed for static sites."""
        return []

    def get_start_command(self) -> str:
        """No start command for static sites."""
        return ""

    def get_nginx_template(self) -> str:
        """Get Nginx template for static sites."""
        return "static"

    def get_apache_template(self) -> str:
        """Get Apache template for static sites."""
        return "static"

    def pre_install(self) -> bool:
        """Determine static directory."""
        # Check for common static directories
        static_dirs = ["public", "dist", "build", "www", "html", "."]

        for dir_name in static_dirs:
            dir_path = self.app_path / dir_name
            if dir_name == ".":
                dir_path = self.app_path

            if (dir_path / "index.html").exists():
                self.static_dir = dir_path
                break

        if not self.static_dir:
            self.static_dir = self.app_path

        self.logger.debug(f"Static directory: {self.static_dir}")
        return True

    def get_template_context(self) -> dict:
        """Get template context for static site."""
        context = super().get_template_context()
        context.update(
            {
                "is_static": True,
                "static_dir": str(self.static_dir or self.app_path),
            }
        )
        return context

    def create_service(self) -> bool:
        """No service needed for static sites."""
        self.logger.substep("Static site - no service needed")
        return True

    def stop(self) -> bool:
        """No stop needed for static sites."""
        return True

    def restart(self) -> bool:
        """No restart needed for static sites."""
        return True

    def health_check(self, retries: int = 5, delay: float = 2.0) -> bool:
        """Check if static site files exist."""
        index_path = (self.static_dir or self.app_path) / "index.html"
        if index_path.exists():
            self.logger.debug("Static site verified")
            return True
        return False

    def build_pipeline(self) -> list[DeployStep]:
        """
        Describe the shorter workflow a static site needs.

        There is nothing to install, nothing to build and no service to run, so
        those steps are dropped rather than made into no-ops. This used to be a
        full copy of ``deploy()`` with its own try/except and its own store
        updates, which is how it ended up without any rollback at all.

        Returns:
            The steps to execute, each with the undo that reverses it.
        """
        return [
            DeployStep(
                title="Fetching source code",
                icon=Icons.DOWNLOAD,
                run=self._step_fetch,
                undo=self.remove_source,
            ),
            DeployStep(
                title="Preparing static files",
                icon=Icons.FOLDER,
                run=self.pre_install,
            ),
            DeployStep(
                title="Creating site configuration",
                icon=Icons.GLOBE,
                run=lambda: self.create_site(with_ssl=False),
                undo=self.remove_site,
            ),
            DeployStep(
                title="Obtaining SSL certificate",
                icon=Icons.LOCK,
                run=self._step_certificate,
                skip_if=lambda: not self.ssl,
            ),
            DeployStep(
                title="Verifying deployment",
                icon=Icons.CHECK,
                run=self._step_start,
            ),
        ]

    def start(self) -> bool:
        """
        Nothing to start: the web server serves the files directly.

        Returns:
            True.
        """
        return True


# Register the deployer
DeployerRegistry.register(StaticDeployer)
