"""
Tests for backup manager.
"""

import json
import tempfile
from pathlib import Path
from unittest import mock

import pytest

from wasm.managers.backup_manager import (
    BackupManager,
    RollbackManager,
    BackupMetadata,
    BackupError,
)


class TestBackupMetadata:
    """Tests for BackupMetadata dataclass."""
    
    def test_to_dict(self):
        """Test conversion to dictionary."""
        metadata = BackupMetadata(
            id="test-app_20240101_120000",
            domain="test.com",
            app_name="wasm-test-com",
            created_at="2024-01-01T12:00:00",
            size_bytes=1024000,
            app_type="nextjs",
            version="1.0.0",
            description="Test backup",
            includes_env=True,
            includes_node_modules=False,
            git_commit="abc123",
            git_branch="main",
            checksum="sha256abc",
            tags=["manual", "pre-deploy"],
        )
        
        result = metadata.to_dict()
        
        assert result["id"] == "test-app_20240101_120000"
        assert result["domain"] == "test.com"
        assert result["app_name"] == "wasm-test-com"
        assert result["created_at"] == "2024-01-01T12:00:00"
        assert result["size_bytes"] == 1024000
        assert result["app_type"] == "nextjs"
        assert result["version"] == "1.0.0"
        assert result["description"] == "Test backup"
        assert result["includes_env"] is True
        assert result["includes_node_modules"] is False
        assert result["git_commit"] == "abc123"
        assert result["git_branch"] == "main"
        assert result["checksum"] == "sha256abc"
        assert result["tags"] == ["manual", "pre-deploy"]
    
    def test_from_dict(self):
        """Test creation from dictionary."""
        data = {
            "id": "test-app_20240101_120000",
            "domain": "test.com",
            "app_name": "wasm-test-com",
            "created_at": "2024-01-01T12:00:00",
            "size_bytes": 1024000,
            "app_type": "nextjs",
            "version": "1.0.0",
            "description": "Test backup",
            "includes_env": True,
            "includes_node_modules": False,
            "git_commit": "abc123",
            "git_branch": "main",
            "checksum": "sha256abc",
            "tags": ["manual"],
        }
        
        metadata = BackupMetadata.from_dict(data)
        
        assert metadata.id == "test-app_20240101_120000"
        assert metadata.domain == "test.com"
        assert metadata.app_name == "wasm-test-com"
        assert metadata.size_bytes == 1024000
        assert metadata.tags == ["manual"]
    
    def test_from_dict_with_defaults(self):
        """Test creation from minimal dictionary."""
        data = {
            "id": "test-app_20240101_120000",
            "domain": "test.com",
            "app_name": "wasm-test-com",
            "created_at": "2024-01-01T12:00:00",
        }
        
        metadata = BackupMetadata.from_dict(data)
        
        assert metadata.size_bytes == 0
        assert metadata.app_type == "unknown"
        assert metadata.includes_env is False
        assert metadata.git_commit is None
        assert metadata.tags == []
    
    def test_size_human_bytes(self):
        """Test human-readable size for bytes."""
        metadata = BackupMetadata(
            id="test", domain="test.com", app_name="test",
            created_at="2024-01-01", size_bytes=500,
            app_type="static", version="1.0.0",
            description="", includes_env=False, includes_node_modules=False,
        )
        assert "500.0 B" in metadata.size_human
    
    def test_size_human_kilobytes(self):
        """Test human-readable size for kilobytes."""
        metadata = BackupMetadata(
            id="test", domain="test.com", app_name="test",
            created_at="2024-01-01", size_bytes=2048,
            app_type="static", version="1.0.0",
            description="", includes_env=False, includes_node_modules=False,
        )
        assert "KB" in metadata.size_human
    
    def test_size_human_megabytes(self):
        """Test human-readable size for megabytes."""
        metadata = BackupMetadata(
            id="test", domain="test.com", app_name="test",
            created_at="2024-01-01", size_bytes=5 * 1024 * 1024,
            app_type="static", version="1.0.0",
            description="", includes_env=False, includes_node_modules=False,
        )
        assert "MB" in metadata.size_human
    
    def test_size_human_gigabytes(self):
        """Test human-readable size for gigabytes."""
        metadata = BackupMetadata(
            id="test", domain="test.com", app_name="test",
            created_at="2024-01-01", size_bytes=2 * 1024 * 1024 * 1024,
            app_type="static", version="1.0.0",
            description="", includes_env=False, includes_node_modules=False,
        )
        assert "GB" in metadata.size_human
    
    def test_age_just_now(self):
        """Test age display for recent backups."""
        from datetime import datetime
        
        metadata = BackupMetadata(
            id="test", domain="test.com", app_name="test",
            created_at=datetime.now().isoformat(),
            size_bytes=0, app_type="static", version="1.0.0",
            description="", includes_env=False, includes_node_modules=False,
        )
        assert "just now" in metadata.age or "minutes ago" in metadata.age
    
    def test_age_invalid_date(self):
        """Test age display with invalid date."""
        metadata = BackupMetadata(
            id="test", domain="test.com", app_name="test",
            created_at="invalid-date",
            size_bytes=0, app_type="static", version="1.0.0",
            description="", includes_env=False, includes_node_modules=False,
        )
        assert metadata.age == "unknown"


class TestBackupManager:
    """Tests for BackupManager."""
    
    @pytest.fixture
    def manager(self):
        """Create a backup manager instance."""
        return BackupManager(verbose=False)
    
    def test_init(self, manager):
        """Test manager initialization."""
        assert manager is not None
        assert manager.backup_dir is not None
        assert manager.max_backups > 0
        assert manager.BACKUP_VERSION == "1.0.0"
    
    def test_generate_backup_id(self, manager):
        """Test backup ID generation."""
        backup_id = manager._generate_backup_id("test.example.com")
        
        assert backup_id.startswith("test-example-com_")
        assert len(backup_id) > 20  # Should have timestamp
    
    def test_detect_app_type_nextjs(self, manager):
        """Test Next.js app detection."""
        with tempfile.TemporaryDirectory() as tmp:
            app_path = Path(tmp)
            (app_path / "next.config.js").touch()
            
            result = manager._detect_app_type(app_path)
            assert result == "nextjs"
    
    def test_detect_app_type_nextjs_mjs(self, manager):
        """Test Next.js app detection with .mjs config."""
        with tempfile.TemporaryDirectory() as tmp:
            app_path = Path(tmp)
            (app_path / "next.config.mjs").touch()
            
            result = manager._detect_app_type(app_path)
            assert result == "nextjs"
    
    def test_detect_app_type_vite(self, manager):
        """Test Vite app detection."""
        with tempfile.TemporaryDirectory() as tmp:
            app_path = Path(tmp)
            (app_path / "vite.config.js").touch()
            
            result = manager._detect_app_type(app_path)
            assert result == "vite"
    
    def test_detect_app_type_vite_ts(self, manager):
        """Test Vite app detection with TypeScript config."""
        with tempfile.TemporaryDirectory() as tmp:
            app_path = Path(tmp)
            (app_path / "vite.config.ts").touch()
            
            result = manager._detect_app_type(app_path)
            assert result == "vite"
    
    def test_detect_app_type_python_requirements(self, manager):
        """Test Python app detection via requirements.txt."""
        with tempfile.TemporaryDirectory() as tmp:
            app_path = Path(tmp)
            (app_path / "requirements.txt").touch()
            
            result = manager._detect_app_type(app_path)
            assert result == "python"
    
    def test_detect_app_type_python_pyproject(self, manager):
        """Test Python app detection via pyproject.toml."""
        with tempfile.TemporaryDirectory() as tmp:
            app_path = Path(tmp)
            (app_path / "pyproject.toml").touch()
            
            result = manager._detect_app_type(app_path)
            assert result == "python"
    
    def test_detect_app_type_nodejs(self, manager):
        """Test Node.js app detection."""
        with tempfile.TemporaryDirectory() as tmp:
            app_path = Path(tmp)
            (app_path / "package.json").touch()
            
            result = manager._detect_app_type(app_path)
            assert result == "nodejs"
    
    def test_detect_app_type_static(self, manager):
        """Test static site detection."""
        with tempfile.TemporaryDirectory() as tmp:
            app_path = Path(tmp)
            (app_path / "index.html").touch()
            
            result = manager._detect_app_type(app_path)
            assert result == "static"
    
    def test_detect_app_type_unknown(self, manager):
        """Test unknown app type."""
        with tempfile.TemporaryDirectory() as tmp:
            app_path = Path(tmp)
            
            result = manager._detect_app_type(app_path)
            assert result == "unknown"
    
    def test_build_exclude_list_default(self, manager):
        """Test default exclude list."""
        excludes = manager._build_exclude_list()
        
        assert "node_modules" in excludes
        assert ".git" in excludes
        assert "__pycache__" in excludes
        assert ".next/cache" in excludes
    
    def test_build_exclude_list_include_node_modules(self, manager):
        """Test exclude list with node_modules included."""
        excludes = manager._build_exclude_list(include_node_modules=True)
        
        assert "node_modules" not in excludes
        assert ".git" in excludes
    
    def test_build_exclude_list_include_build(self, manager):
        """Test exclude list with build artifacts included."""
        excludes = manager._build_exclude_list(include_build=True)
        
        # .next/cache should be removed, allowing builds
        assert ".next/cache" not in excludes
        assert "dist" not in excludes
        assert "build" not in excludes
    
    def test_build_exclude_list_custom_excludes(self, manager):
        """Test exclude list with custom patterns."""
        excludes = manager._build_exclude_list(custom_excludes=["*.secret", "private/"])
        
        assert "*.secret" in excludes
        assert "private/" in excludes
        assert "node_modules" in excludes  # Still has defaults
    
    def test_get_app_backup_dir(self, manager):
        """Test getting app-specific backup directory."""
        result = manager._get_app_backup_dir("wasm-test-com")
        
        assert str(result).endswith("wasm-test-com")
        assert manager.backup_dir in result.parents or manager.backup_dir == result.parent


class TestBackupManagerWithMocks:
    """Tests for BackupManager with mocked system calls."""
    
    @pytest.fixture
    def manager(self):
        """Create a backup manager instance."""
        return BackupManager(verbose=False)
    
    @mock.patch("wasm.managers.backup_manager.run_command_sudo")
    @mock.patch("wasm.managers.backup_manager.run_command")
    def test_create_backup_app_not_found(self, mock_run, mock_sudo, manager):
        """Test creating backup for non-existent app."""
        with pytest.raises(BackupError) as exc_info:
            manager.create("nonexistent.domain.com")
        
        assert "Application not found" in str(exc_info.value)
    
    @mock.patch("wasm.managers.backup_manager.run_command_sudo")
    def test_verify_backup_not_found(self, mock_sudo, manager):
        """Test verifying non-existent backup."""
        mock_sudo.return_value = mock.Mock(success=False, stdout="", stderr="")
        
        # Use a temp directory that exists but is empty
        with tempfile.TemporaryDirectory() as tmp:
            manager.backup_dir = Path(tmp)
            result = manager.verify("nonexistent-backup")
        
        assert result["valid"] is False
        assert any("not found" in err.lower() for err in result["errors"])
    
    @mock.patch("wasm.managers.backup_manager.run_command_sudo")
    def test_get_storage_usage_empty(self, mock_sudo, manager):
        """Test storage usage with no backups."""
        mock_sudo.return_value = mock.Mock(success=True, stdout="0")
        
        # Set backup_dir to a non-existent path
        manager.backup_dir = Path("/nonexistent/backup/dir")
        result = manager.get_storage_usage()
        
        assert result["total_size_bytes"] == 0
        assert result["total_backups"] == 0
        assert result["by_app"] == {}


class TestRollbackManager:
    """Tests for RollbackManager."""
    
    @pytest.fixture
    def manager(self):
        """Create a rollback manager instance."""
        return RollbackManager(verbose=False)
    
    def test_init(self, manager):
        """Test manager initialization."""
        assert manager is not None
        assert manager.backup_manager is not None
        assert manager.service_manager is not None
    
    @mock.patch("wasm.managers.backup_manager.BackupManager.list_backups")
    def test_rollback_no_backups(self, mock_list_backups, manager):
        """Test rollback with no available backups."""
        mock_list_backups.return_value = []
        
        with pytest.raises(BackupError) as exc_info:
            manager.rollback("test.com")
        
        assert "No backups found" in str(exc_info.value)
    
    @mock.patch("wasm.managers.backup_manager.BackupManager.get_backup")
    def test_rollback_backup_not_found(self, mock_get_backup, manager):
        """Test rollback with specific backup that doesn't exist."""
        mock_get_backup.return_value = None
        
        with pytest.raises(BackupError) as exc_info:
            manager.rollback("test.com", backup_id="nonexistent-id")
        
        assert "Backup not found" in str(exc_info.value)
    
    @mock.patch("wasm.managers.backup_manager.BackupManager.list_backups")
    def test_list_rollback_points(self, mock_list, manager):
        """Test listing rollback points."""
        mock_backups = [
            BackupMetadata(
                id="test1", domain="test.com", app_name="test",
                created_at="2024-01-01", size_bytes=100,
                app_type="nodejs", version="1.0.0",
                description="", includes_env=True, includes_node_modules=False,
            ),
            BackupMetadata(
                id="test2", domain="test.com", app_name="test",
                created_at="2024-01-02", size_bytes=200,
                app_type="nodejs", version="1.0.0",
                description="", includes_env=True, includes_node_modules=False,
            ),
        ]
        mock_list.return_value = mock_backups
        
        result = manager.list_rollback_points("test.com")
        
        assert len(result) == 2
        assert result[0].id == "test1"
        mock_list.assert_called_once_with(domain="test.com")


class TestBackupCLIHandlers:
    """Tests for backup CLI handlers."""
    
    @mock.patch("wasm.cli.commands.backup.BackupManager")
    def test_backup_create_missing_domain(self, mock_manager):
        """Test backup create without domain."""
        from argparse import Namespace
        from wasm.cli.commands.backup import _backup_create
        
        args = Namespace(domain=None, verbose=False)
        result = _backup_create(args, verbose=False)
        
        assert result == 1  # Error exit code
    
    @mock.patch("wasm.cli.commands.backup.BackupManager")
    def test_backup_list_empty(self, mock_manager_cls):
        """Test listing backups when empty."""
        from argparse import Namespace
        from wasm.cli.commands.backup import _backup_list
        
        mock_manager = mock.Mock()
        mock_manager.list_backups.return_value = []
        mock_manager_cls.return_value = mock_manager
        
        args = Namespace(
            domain=None, tags=None, limit=None, 
            json=False, verbose=False
        )
        result = _backup_list(args, verbose=False)
        
        assert result == 0  # Success but empty
    
    @mock.patch("wasm.cli.commands.backup.BackupManager")
    def test_backup_list_json_output(self, mock_manager_cls, capsys):
        """Test listing backups with JSON output."""
        from argparse import Namespace
        from wasm.cli.commands.backup import _backup_list
        
        mock_metadata = BackupMetadata(
            id="test-backup", domain="test.com", app_name="test",
            created_at="2024-01-01", size_bytes=1000,
            app_type="nodejs", version="1.0.0",
            description="Test", includes_env=True, includes_node_modules=False,
        )
        
        mock_manager = mock.Mock()
        mock_manager.list_backups.return_value = [mock_metadata]
        mock_manager_cls.return_value = mock_manager
        
        args = Namespace(
            domain="test.com", tags=None, limit=None,
            json=True, verbose=False
        )
        result = _backup_list(args, verbose=False)
        
        captured = capsys.readouterr()
        assert result == 0
        assert "test-backup" in captured.out
        
        # Verify valid JSON
        output_data = json.loads(captured.out)
        assert len(output_data) == 1
        assert output_data[0]["id"] == "test-backup"
    
    @mock.patch("wasm.cli.commands.backup.BackupManager")
    def test_backup_info_not_found(self, mock_manager_cls):
        """Test backup info for non-existent backup."""
        from argparse import Namespace
        from wasm.cli.commands.backup import _backup_info
        
        mock_manager = mock.Mock()
        mock_manager.get_backup.return_value = None
        mock_manager_cls.return_value = mock_manager
        
        args = Namespace(backup_id="nonexistent", json=False, verbose=False)
        result = _backup_info(args, verbose=False)
        
        assert result == 1  # Error exit code
    
    @mock.patch("wasm.cli.commands.backup.BackupManager")
    def test_backup_verify_valid(self, mock_manager_cls):
        """Test verifying a valid backup."""
        from argparse import Namespace
        from wasm.cli.commands.backup import _backup_verify
        
        mock_manager = mock.Mock()
        mock_manager.verify.return_value = {
            "valid": True,
            "errors": [],
            "warnings": [],
            "checksum_verified": True,
            "archive_valid": True,
            "file_count": 100,
        }
        mock_manager_cls.return_value = mock_manager
        
        args = Namespace(backup_id="test-backup", verbose=False)
        result = _backup_verify(args, verbose=False)
        
        assert result == 0  # Success
    
    @mock.patch("wasm.cli.commands.backup.BackupManager")
    def test_backup_verify_invalid(self, mock_manager_cls):
        """Test verifying an invalid backup."""
        from argparse import Namespace
        from wasm.cli.commands.backup import _backup_verify
        
        mock_manager = mock.Mock()
        mock_manager.verify.return_value = {
            "valid": False,
            "errors": ["Checksum mismatch"],
            "warnings": [],
        }
        mock_manager_cls.return_value = mock_manager
        
        args = Namespace(backup_id="test-backup", verbose=False)
        result = _backup_verify(args, verbose=False)
        
        assert result == 1  # Error


class TestBackupIntegration:
    """Integration-style tests (still mocked but testing full flows)."""
    
    @mock.patch("wasm.cli.commands.backup.RollbackManager")
    def test_rollback_flow(self, mock_rollback_cls):
        """Test full rollback flow."""
        from argparse import Namespace
        from wasm.cli.commands.backup import handle_rollback
        
        mock_metadata = BackupMetadata(
            id="test-backup", domain="test.com", app_name="test",
            created_at="2024-01-01", size_bytes=1000,
            app_type="nodejs", version="1.0.0",
            description="Pre-deploy backup", includes_env=True, 
            includes_node_modules=False,
        )
        
        mock_rollback = mock.Mock()
        mock_rollback.list_rollback_points.return_value = [mock_metadata]
        mock_rollback.create_pre_deploy_backup.return_value = mock_metadata
        mock_rollback.rollback.return_value = True
        mock_rollback_cls.return_value = mock_rollback
        
        args = Namespace(
            domain="test.com", backup_id=None, 
            no_rebuild=False, verbose=False
        )
        result = handle_rollback(args)
        
        assert result == 0  # Success
        mock_rollback.rollback.assert_called_once()
