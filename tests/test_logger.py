"""Tests for logger."""

from io import StringIO

from wasm.core.logger import Colors, Icons, Logger, LogLevel


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


class TestStreamResolution:
    """The logger has to write where output is being captured right now."""

    def test_output_follows_a_redirected_stdout(self):
        """
        The default stream is resolved per write, not bound at import.

        ``stream: TextIO = sys.stdout`` in the signature captured whatever
        stdout was when the module was first imported, so a test runner or a
        contextlib.redirect_stdout saw nothing and the real terminal got the
        output instead.
        """
        import contextlib

        logger = Logger()
        buffer = StringIO()
        with contextlib.redirect_stdout(buffer):
            logger.info("Redirected message")

        assert "Redirected message" in buffer.getvalue()

    def test_an_explicit_stream_still_wins(self):
        """Passing a stream keeps output there regardless of sys.stdout."""
        import contextlib

        explicit = StringIO()
        logger = Logger(stream=explicit)
        elsewhere = StringIO()
        with contextlib.redirect_stdout(elsewhere):
            logger.info("Explicit message")

        assert "Explicit message" in explicit.getvalue()
        assert elsewhere.getvalue() == ""


class TestCheck:
    """The health check's result lines."""

    def test_a_check_shows_what_was_checked_and_what_was_found(self):
        """
        Args: none.
        """
        stream = StringIO()
        logger = Logger(stream=stream)
        logger.check("Disk Space", "135.1GB free", "ok")
        output = stream.getvalue()

        assert "Disk Space:" in output
        assert "135.1GB free" in output

    def test_an_unknown_outcome_does_not_raise(self):
        """A health check must not fail because of its own formatting."""
        stream = StringIO()
        logger = Logger(stream=stream)
        logger.check("Something", "value", "not-an-outcome")

        assert "Something:" in stream.getvalue()

    def test_no_color_leaves_no_escape_codes(self):
        """``wasm --no-color health`` wrote escape codes anyway."""
        stream = StringIO()
        logger = Logger(stream=stream, no_color=True)
        logger.check("Nginx", "Running", "ok")

        assert "\033[" not in stream.getvalue()


class TestTable:
    """Tables carry the state colour; that is what makes a list scannable."""

    def test_every_row_and_header_is_shown(self):
        """
        Args: none.
        """
        stream = StringIO()
        logger = Logger(stream=stream, no_color=True)
        logger.table(["Domain", "Status"], [["example.com", "Running"]])
        output = stream.getvalue()

        assert "Domain" in output
        assert "example.com" in output
        assert "Running" in output

    def test_an_empty_table_prints_nothing(self):
        """
        Args: none.
        """
        stream = StringIO()
        logger = Logger(stream=stream, no_color=True)
        logger.table(["Domain"], [])

        assert stream.getvalue() == ""

    def test_a_state_carries_its_colour(self):
        """
        Args: none.
        """
        from wasm.core.logger import state

        assert state("Running").style == "green"
        assert state("Failed").style == "bold red"
        assert state("Whatever it is").style == "", "an unknown state gets no arbitrary colour"

    def test_square_brackets_in_a_value_are_not_markup(self):
        """
        Rich reads ``[bold]`` as markup, and cells hold arbitrary text.

        A domain or an error message containing brackets would otherwise be
        swallowed or would recolour the rest of the row.
        """
        stream = StringIO()
        logger = Logger(stream=stream, no_color=True)
        logger.table(["Message"], [["[red]not a style[/red]"]])

        assert "[red]not a style[/red]" in stream.getvalue()
