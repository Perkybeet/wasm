"""Tests for port validator."""

import pytest
from unittest.mock import patch

from wasm.validators.port import (
    is_valid_port,
    validate_port,
    is_port_available,
    get_default_port,
    check_port,
    find_available_port,
)
from wasm.core.exceptions import PortError


class TestIsValidPort:
    """Tests for is_valid_port function."""
    
    def test_valid_ports(self):
        """Test valid port numbers."""
        valid_ports = [1, 80, 443, 3000, 8080, 65535]
        for port in valid_ports:
            is_valid, _ = is_valid_port(port)
            assert is_valid is True, f"Expected {port} to be valid"
    
    def test_invalid_ports(self):
        """Test invalid port numbers."""
        invalid_ports = [0, -1, 65536, 100000]
        for port in invalid_ports:
            is_valid, _ = is_valid_port(port)
            assert is_valid is False, f"Expected {port} to be invalid"
    
    def test_non_integer_port(self):
        """Test non-integer port returns invalid."""
        is_valid, error = is_valid_port("3000")
        assert is_valid is False
        assert "integer" in error.lower()


class TestCheckPort:
    """Tests for check_port function (boolean only, accepts strings)."""
    
    def test_valid_integer_port(self):
        """Test valid integer port."""
        assert check_port(3000) is True
        assert check_port(80) is True
    
    def test_valid_string_port(self):
        """Test valid string port."""
        assert check_port("3000") is True
        assert check_port("8080") is True
    
    def test_invalid_string_port(self):
        """Test invalid string port."""
        assert check_port("invalid") is False
        assert check_port("") is False
    
    def test_invalid_range(self):
        """Test port outside valid range."""
        assert check_port(0) is False
        assert check_port(70000) is False


class TestValidatePort:
    """Tests for validate_port function."""
    
    @patch("wasm.validators.port.is_port_available")
    def test_valid_port_returns_int(self, mock_available):
        """Test that validate_port returns integer."""
        mock_available.return_value = True
        assert validate_port(3000) == 3000
        assert validate_port("8080") == 8080
    
    @patch("wasm.validators.port.is_port_available")
    def test_string_port_converted(self, mock_available):
        """Test string port is converted to int."""
        mock_available.return_value = True
        result = validate_port("3000")
        assert isinstance(result, int)
        assert result == 3000
    
    def test_invalid_port_raises_error(self):
        """Test that validate_port raises PortError for invalid ports."""
        with pytest.raises(PortError):
            validate_port(0, check_available=False)
        
        with pytest.raises(PortError):
            validate_port(70000, check_available=False)
    
    def test_invalid_string_raises_error(self):
        """Test that invalid string raises PortError."""
        with pytest.raises(PortError):
            validate_port("not-a-port")


class TestIsPortAvailable:
    """Tests for is_port_available function."""
    
    def test_function_exists(self):
        """Test function is callable."""
        assert callable(is_port_available)
    
    @patch("socket.socket")
    def test_available_port(self, mock_socket):
        """Test available port detection."""
        mock_instance = mock_socket.return_value.__enter__.return_value
        mock_instance.connect_ex.return_value = 1  # Connection failed = port available
        assert is_port_available(9999) is True
    
    @patch("socket.socket")
    def test_unavailable_port(self, mock_socket):
        """Test unavailable port detection."""
        mock_instance = mock_socket.return_value.__enter__.return_value
        mock_instance.connect_ex.return_value = 0  # Connection succeeded = port in use
        assert is_port_available(80) is False


class TestFindAvailablePort:
    """Tests for find_available_port function."""
    
    @patch("wasm.validators.port.is_port_available")
    def test_finds_preferred_if_available(self, mock_available):
        """Test preferred port is returned if available."""
        mock_available.return_value = True
        result = find_available_port(preferred=3000)
        assert result == 3000
    
    @patch("wasm.validators.port.is_port_available")
    def test_finds_next_available(self, mock_available):
        """Test finds next available port if preferred is taken."""
        # First call (preferred) returns False, subsequent return True
        mock_available.side_effect = [False, True]
        result = find_available_port(start=3000, preferred=3000)
        assert result == 3000  # Falls back to range search starting at 3000


class TestGetDefaultPort:
    """Tests for get_default_port function."""
    
    def test_app_type_ports(self):
        """Test default ports for app types."""
        assert get_default_port("nextjs") == 3000
        assert get_default_port("nodejs") == 3000
        assert get_default_port("vite") == 5173
        assert get_default_port("python") == 8000
        assert get_default_port("static") == 80
    
    def test_unknown_app_type(self):
        """Test default port for unknown app type."""
        assert get_default_port("unknown") == 3000
    
    def test_case_insensitive(self):
        """Test app type matching is case insensitive."""
        assert get_default_port("NextJS") == 3000
        assert get_default_port("PYTHON") == 8000
