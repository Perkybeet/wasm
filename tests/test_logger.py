"""Tests for logger."""

import pytest
from io import StringIO

from wasm.core.logger import Logger, Colors, Icons, LogLevel


class TestLogger:
    """Tests for Logger class."""
    
    def test_logger_creation(self):
        """Test logger can be created."""
        logger = Logger()
        assert logger is not None
    
    def test_verbose_mode(self):
        """Test verbose mode configuration."""
        logger = Logger(verbose=True)
        assert logger.verbose is True
        
        logger = Logger(verbose=False)
        assert logger.verbose is False
    
    def test_no_color_mode(self):
        """Test no color mode configuration."""
        logger = Logger(no_color=True)
        assert logger.no_color is True
    
    def test_info_output(self):
        """Test info message output."""
        stream = StringIO()
        logger = Logger(verbose=False, stream=stream)
        logger.info("Test message")
        output = stream.getvalue()
        assert "Test message" in output
    
    def test_debug_hidden_without_verbose(self):
        """Test debug messages are hidden without verbose."""
        stream = StringIO()
        logger = Logger(verbose=False, stream=stream)
        logger.debug("Debug message")
        output = stream.getvalue()
        assert "Debug message" not in output
    
    def test_debug_shown_with_verbose(self):
        """Test debug messages are shown with verbose."""
        stream = StringIO()
        logger = Logger(verbose=True, stream=stream)
        logger.debug("Debug message")
        output = stream.getvalue()
        assert "Debug message" in output
    
    def test_error_output(self):
        """Test error message output."""
        stream = StringIO()
        logger = Logger(verbose=False, stream=stream)
        logger.error("Error message")
        output = stream.getvalue()
        assert "Error message" in output
    
    def test_success_output(self):
        """Test success message output."""
        stream = StringIO()
        logger = Logger(verbose=False, stream=stream)
        logger.success("Success message")
        output = stream.getvalue()
        assert "Success message" in output
    
    def test_warning_output(self):
        """Test warning message output."""
        stream = StringIO()
        logger = Logger(verbose=False, stream=stream)
        logger.warning("Warning message")
        output = stream.getvalue()
        assert "Warning message" in output
    
    def test_step_format(self):
        """Test step message format."""
        stream = StringIO()
        logger = Logger(verbose=False, stream=stream)
        logger.step(1, 5, "First step")
        output = stream.getvalue()
        assert "[1/5]" in output
        assert "First step" in output
    
    def test_custom_stream(self):
        """Test logger with custom stream."""
        stream = StringIO()
        logger = Logger(stream=stream)
        logger.info("Test message")
        output = stream.getvalue()
        assert "Test message" in output


class TestColors:
    """Tests for Colors class."""
    
    def test_color_codes_exist(self):
        """Test that color codes are defined."""
        assert Colors.RESET is not None
        assert Colors.RED is not None
        assert Colors.GREEN is not None
        assert Colors.YELLOW is not None
        assert Colors.BLUE is not None


class TestIcons:
    """Tests for Icons class."""
    
    def test_icon_codes_exist(self):
        """Test that icons are defined."""
        assert Icons.SUCCESS is not None
        assert Icons.ERROR is not None
        assert Icons.WARNING is not None
        assert Icons.INFO is not None


class TestLogLevel:
    """Tests for LogLevel enum."""
    
    def test_log_levels_order(self):
        """Test that log levels are in order."""
        assert LogLevel.DEBUG.value < LogLevel.INFO.value
        assert LogLevel.INFO.value < LogLevel.WARNING.value
        assert LogLevel.WARNING.value < LogLevel.ERROR.value
