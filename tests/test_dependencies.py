"""
Tests for the dependencies module.
"""

from unittest.mock import patch

from wasm.core.dependencies import (
    DependencyChecker,
    check_deployment_ready,
    get_package_manager_install_hint,
)


class TestDependencyChecker:
    """Tests for DependencyChecker class."""

    def test_init(self):
        """Test DependencyChecker initialization."""
        checker = DependencyChecker()
        assert checker.verbose is False

        checker = DependencyChecker(verbose=True)
        assert checker.verbose is True

    def test_check_command_exists(self):
        """Test checking if a command exists."""
        checker = DependencyChecker()
        # Python should always exist on a system running these tests
        assert checker.check_command("python3") is True
        # Non-existent command
        assert checker.check_command("nonexistent_cmd_xyz") is False

    def test_get_version(self, runner):
        """The first line of the program's own output is its version."""
        runner.script(["python3", "--version"], stdout="Python 3.12.3\n")
        checker = DependencyChecker(runner=runner)

        assert checker.get_version("python3") == "Python 3.12.3"

    def test_get_version_reads_stderr_when_stdout_is_empty(self, runner):
        """Several tools, including older java and node, answer on stderr."""
        runner.script(["oldtool", "--version"], stdout="", stderr="oldtool 1.2\n")
        checker = DependencyChecker(runner=runner)

        assert checker.get_version("oldtool") == "oldtool 1.2"

    def test_get_version_nonexistent(self, runner):
        """A program that is not installed has no version."""
        runner.script(["nonexistent_cmd_xyz"], exit_code=127)
        checker = DependencyChecker(runner=runner)

        assert checker.get_version("nonexistent_cmd_xyz") is None

    def test_get_version_is_bounded(self, runner):
        """A version probe cannot hang a deploy."""
        checker = DependencyChecker(runner=runner)
        checker.get_version("python3")

        assert runner.calls == [("python3", "--version")]

    def test_detect_required_package_manager(self, tmp_path):
        """Test package manager detection from lock files."""
        checker = DependencyChecker()

        # Test pnpm detection
        (tmp_path / "pnpm-lock.yaml").touch()
        assert checker.detect_required_package_manager(tmp_path) == "pnpm"

        # Clean up and test yarn
        (tmp_path / "pnpm-lock.yaml").unlink()
        (tmp_path / "yarn.lock").touch()
        assert checker.detect_required_package_manager(tmp_path) == "yarn"

        # Clean up and test bun
        (tmp_path / "yarn.lock").unlink()
        (tmp_path / "bun.lockb").touch()
        assert checker.detect_required_package_manager(tmp_path) == "bun"

        # Clean up and test npm (default with package.json)
        (tmp_path / "bun.lockb").unlink()
        (tmp_path / "package.json").touch()
        assert checker.detect_required_package_manager(tmp_path) == "npm"

        # No package.json, no lock files
        (tmp_path / "package.json").unlink()
        assert checker.detect_required_package_manager(tmp_path) is None

    def test_check_package_manager(self, runner):
        """A present package manager reports itself installed, with a version."""
        runner.only_knows("npm").script(["npm", "--version"], stdout="10.8.2")
        checker = DependencyChecker(runner=runner)

        is_installed, version, hint = checker.check_package_manager("npm")

        assert is_installed is True
        assert version == "10.8.2"
        assert "install" in hint.lower()

    def test_check_package_manager_when_absent(self, runner):
        """An absent package manager reports how to install it."""
        runner.only_knows("npm")
        checker = DependencyChecker(runner=runner)

        is_installed, version, hint = checker.check_package_manager("pnpm")

        assert is_installed is False
        assert "install" in hint.lower()

    def test_get_setup_summary(self, runner):
        """Test getting system setup summary."""
        checker = DependencyChecker(runner=runner)
        summary = checker.get_setup_summary()

        # Check required keys exist
        assert "system_ready" in summary
        assert "webserver" in summary
        assert "nodejs" in summary
        assert "python" in summary
        assert "missing_required" in summary
        assert "missing_optional" in summary
        assert "recommendations" in summary

        # Check nodejs structure
        assert "installed" in summary["nodejs"]
        assert "package_managers" in summary["nodejs"]


class TestCheckDeploymentReady:
    """Tests for check_deployment_ready function."""

    @patch("wasm.core.dependencies.DependencyChecker.check_command")
    def test_nodejs_app_requires_node(self, mock_check, runner):
        """Test that nodejs apps require node."""

        # Mock node as not installed
        def side_effect(cmd):
            if cmd == "node":
                return False
            return True

        mock_check.side_effect = side_effect

        can_deploy, missing, warnings = check_deployment_ready("nodejs", "npm", runner=runner)
        assert can_deploy is False
        assert any("node" in m.lower() for m in missing)

    @patch("wasm.core.dependencies.DependencyChecker.check_command")
    def test_python_app_requires_python(self, mock_check, runner):
        """Test that python apps require python3."""

        def side_effect(cmd):
            if cmd == "python3":
                return False
            return True

        mock_check.side_effect = side_effect

        can_deploy, missing, warnings = check_deployment_ready("python", "auto", runner=runner)
        assert can_deploy is False
        assert any("python" in m.lower() for m in missing)

    @patch("wasm.core.dependencies.DependencyChecker.check_command")
    def test_missing_webserver(self, mock_check, runner):
        """Test that missing webserver is reported."""

        def side_effect(cmd):
            if cmd in ["nginx", "apache2"]:
                return False
            return True

        mock_check.side_effect = side_effect

        can_deploy, missing, warnings = check_deployment_ready("static", "auto", runner=runner)
        assert can_deploy is False
        assert any("nginx" in m.lower() or "apache" in m.lower() for m in missing)

    @patch("wasm.core.dependencies.DependencyChecker.check_command")
    @patch("wasm.core.dependencies.DependencyChecker.get_available_package_managers")
    def test_unavailable_pm_with_alternatives_shows_warning(
        self, mock_available, mock_check, runner
    ):
        """Test that unavailable PM shows warning but allows deployment when alternatives exist."""
        mock_check.return_value = True  # Everything installed
        mock_available.return_value = ["npm", "pnpm"]  # Available PMs

        can_deploy, missing, warnings = check_deployment_ready("nextjs", "bun", runner=runner)
        # Should allow deployment (bun not available but npm/pnpm are)
        assert can_deploy is True
        assert len(missing) == 0
        # Should have a warning about bun not being available
        assert len(warnings) > 0
        assert any("bun" in w.lower() and "available" in w.lower() for w in warnings)

    @patch("wasm.core.dependencies.DependencyChecker.check_command")
    @patch("wasm.core.dependencies.DependencyChecker.get_available_package_managers")
    def test_no_package_managers_blocks_deployment(self, mock_available, mock_check, runner):
        """Test that having no package managers blocks JS app deployment."""

        def check_side_effect(cmd):
            if cmd in ["npm", "pnpm", "yarn", "bun"]:
                return False
            return True

        mock_check.side_effect = check_side_effect
        mock_available.return_value = []  # No PMs available

        can_deploy, missing, warnings = check_deployment_ready("nextjs", "npm", runner=runner)
        assert can_deploy is False
        assert any("package manager" in m.lower() for m in missing)


class TestGetPackageManagerInstallHint:
    """Tests for get_package_manager_install_hint function."""

    def test_npm_hint(self):
        """Test npm installation hint."""
        hint = get_package_manager_install_hint("npm")
        assert "Node.js" in hint

    def test_pnpm_hint(self):
        """Test pnpm installation hint."""
        hint = get_package_manager_install_hint("pnpm")
        assert "pnpm" in hint
        assert "npm install" in hint or "curl" in hint

    def test_yarn_hint(self):
        """Test yarn installation hint."""
        hint = get_package_manager_install_hint("yarn")
        assert "yarn" in hint

    def test_bun_hint(self):
        """Test bun installation hint."""
        hint = get_package_manager_install_hint("bun")
        assert "bun" in hint
        assert "curl" in hint

    def test_unknown_pm_hint(self):
        """Test unknown package manager hint."""
        hint = get_package_manager_install_hint("unknown_pm")
        assert "npm install" in hint
        assert "unknown_pm" in hint
