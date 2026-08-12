"""
Custom logging system for WASM.

Provides a rich, user-friendly logging experience with support for:
- Step-by-step progress indicators
- Verbose mode for detailed output
- Color-coded output
- Structured log messages

Color handling is process-wide on purpose: command handlers build their own
:class:`Logger` instances deep in the call stack, so the only way for a
top-level ``--no-color`` to reach them is a module-level override installed by
:func:`set_colors_disabled`.
"""

import io
import json
import logging
import os
import shutil
import sys
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any, TextIO, cast

from rich.box import SIMPLE_HEAD
from rich.console import Console
from rich.table import Table
from rich.text import Text

if TYPE_CHECKING:  # pragma: no cover - imported for types only
    from rich.console import JustifyMethod

_colors_disabled: bool = False

#: Width used when the output is not a terminal. Rich would otherwise assume
#: eighty columns and wrap, which turns a piped table into something no tool
#: downstream can parse and breaks assertions on long lines in the tests.
OFFLINE_WIDTH = 200

#: What each state is allowed to look like.
#:
#: Colour encodes state and nothing else, so green always means the same thing
#: wherever it appears and an operator can scan a column without reading it.
#: Decorative colour is what made the previous output hard to skim: everything
#: was cyan, so nothing stood out.
STATE_STYLES: dict[str, str] = {
    "running": "green",
    "active": "green",
    "enabled": "green",
    "installed": "green",
    "valid": "green",
    "ok": "green",
    "yes": "green",
    "deploying": "cyan",
    "building": "cyan",
    "pending": "cyan",
    "stopped": "yellow",
    "inactive": "yellow",
    "expiring": "yellow",
    "degraded": "yellow",
    "restarting": "bold yellow",
    "no answer": "bold red",
    "failed": "bold red",
    "error": "bold red",
    "expired": "bold red",
    "missing": "bold red",
    "no": "dim",
    "none": "dim",
    "static": "dim",
    "unknown": "dim",
    "disabled": "dim",
    "n/a": "dim",
}


def state(value: Any) -> Text:
    """
    Render a state so its colour matches its meaning.

    Args:
        value: The state, matched case-insensitively against STATE_STYLES.
            Anything unrecognised is rendered without colour rather than being
            given an arbitrary one.

    Returns:
        Text ready to be placed in a table cell.
    """
    text = str(value)
    return Text(text, style=STATE_STYLES.get(text.strip().lower(), ""))


def styled(value: Any, style: str) -> Text:
    """
    Render a value with an explicit style.

    Args:
        value: What to show.
        style: A Rich style, for example "bold" or "dim".

    Returns:
        Text ready to be placed in a table cell.
    """
    return Text(str(value), style=style)


def set_colors_disabled(disabled: bool) -> None:
    """
    Turn colored output off (or back on) for every logger created afterwards.

    This is what ``wasm --no-color`` calls. It is process-wide because handlers
    instantiate their own loggers and never see the parsed CLI arguments.

    Args:
        disabled: True to strip colors from all output.
    """
    global _colors_disabled
    _colors_disabled = disabled


def colors_enabled(stream: TextIO) -> bool:
    """
    Decide whether colored output is appropriate for a stream.

    Colors are suppressed when ``--no-color`` was used, when the NO_COLOR
    environment variable is set to a non-empty value (see https://no-color.org)
    or when the stream is not a terminal (piped or redirected output).

    Args:
        stream: The stream the logger writes to.

    Returns:
        True when ANSI escape codes should be emitted.
    """
    if _colors_disabled:
        return False
    if os.environ.get("NO_COLOR", ""):
        return False
    isatty = getattr(stream, "isatty", None)
    if isatty is None:
        return False
    return bool(isatty())


class Colors:
    """ANSI color codes for terminal output."""

    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"

    # Colors
    RED = "\033[31m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    BLUE = "\033[34m"
    MAGENTA = "\033[35m"
    CYAN = "\033[36m"
    WHITE = "\033[37m"
    GRAY = "\033[90m"

    # Bright colors
    BRIGHT_RED = "\033[91m"
    BRIGHT_GREEN = "\033[92m"
    BRIGHT_YELLOW = "\033[93m"
    BRIGHT_BLUE = "\033[94m"
    BRIGHT_MAGENTA = "\033[95m"
    BRIGHT_CYAN = "\033[96m"


class Icons:
    """Unicode icons for log messages."""

    SUCCESS = "✓"
    ERROR = "✗"
    WARNING = "⚠"
    INFO = "i"
    ARROW = "→"
    BULLET = "•"
    ROCKET = "🚀"
    PACKAGE = "📦"
    DOWNLOAD = "📥"
    BUILD = "🔨"
    GLOBE = "🌐"
    LOCK = "🔒"
    GEAR = "⚙️"
    CHECK = "✅"
    CROSS = "❌"
    CLOCK = "⏱"
    FOLDER = "📁"
    FILE = "📄"
    DATABASE = "🗄️"
    SEARCH = "🔍"


#: Marker and colour for each check outcome. The glyphs are the ones every
#: other command uses, so a check result reads the same as a command result.
CHECK_MARKERS: dict[str, tuple[str, str]] = {
    "ok": (Icons.SUCCESS, Colors.GREEN),
    "warning": (Icons.WARNING, Colors.YELLOW),
    "error": (Icons.ERROR, Colors.RED),
    "info": (Icons.INFO, Colors.BLUE),
}


class LogLevel(Enum):
    """Log levels for filtering output."""

    DEBUG = 0
    INFO = 1
    STEP = 2
    SUCCESS = 3
    WARNING = 4
    ERROR = 5


class Logger:
    """
    Custom logger with rich output formatting.

    Provides step-by-step progress indicators, color-coded output,
    and support for verbose mode.

    Example:
        logger = Logger(verbose=True)
        logger.step(1, 5, "Cloning repository")
        logger.debug("Clone URL: git@github.com:user/repo.git")
        logger.success("Repository cloned successfully")
    """

    def __init__(
        self,
        verbose: bool = False,
        no_color: bool = False,
        log_file: Path | None = None,
        stream: TextIO | None = None,
    ):
        """
        Initialize the logger.

        Args:
            verbose: Enable verbose output (shows debug messages).
            no_color: Disable colored output.
            log_file: Optional file path to write logs to.
            stream: Output stream. None means "whatever sys.stdout is when the
                line is written", which is not the same as passing sys.stdout:
                a default argument is bound once, at import, so anything that
                replaces sys.stdout afterwards - a test runner capturing
                output, contextlib.redirect_stdout, the daemon reopening its
                streams - was written straight past.
        """
        self.verbose = verbose
        self._stream = stream  # Must be set before colors_enabled() is called
        self.no_color = no_color or not colors_enabled(self.stream)
        self.log_file = log_file
        self._current_step = 0
        self._total_steps = 0

    @property
    def stream(self) -> TextIO:
        """
        Where output goes.

        Returns:
            The stream given at construction, or the current sys.stdout.
        """
        return self._stream if self._stream is not None else sys.stdout

    @stream.setter
    def stream(self, stream: TextIO | None) -> None:
        """
        Redirect this logger.

        Args:
            stream: The new stream, or None to follow sys.stdout again.
        """
        self._stream = stream

    def _colorize(self, text: str, color: str) -> str:
        """Apply color to text if colors are enabled."""
        if self.no_color:
            return text
        return f"{color}{text}{Colors.RESET}"

    def _write(self, message: str, newline: bool = True) -> None:
        """Write message to output stream and optionally to log file."""
        end = "\n" if newline else ""
        print(message, file=self.stream, end=end, flush=True)

        if self.log_file:
            try:
                timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                # Strip ANSI codes for file output
                clean_message = self._strip_ansi(message)
                with open(self.log_file, "a") as f:
                    f.write(f"[{timestamp}] {clean_message}{end}")
            except OSError as e:
                # Use standard logging as fallback since we're in the logger itself
                logging.getLogger(__name__).debug(f"Failed to write to log file: {e}")

    def _strip_ansi(self, text: str) -> str:
        """Remove ANSI escape codes from text."""
        import re

        ansi_pattern = re.compile(r"\033\[[0-9;]*m")
        return ansi_pattern.sub("", text)

    def _width(self) -> int:
        """
        Decide how wide rendered output may be.

        Returns:
            The terminal width when writing to one, and OFFLINE_WIDTH otherwise.
        """
        if self.no_color:
            return OFFLINE_WIDTH
        isatty = getattr(self.stream, "isatty", None)
        if isatty is None or not isatty():
            return OFFLINE_WIDTH
        return shutil.get_terminal_size(fallback=(100, 24)).columns

    def _render(self, renderable: Any) -> None:
        """
        Draw a Rich renderable and send it through the one write path.

        Rich is given its own buffer rather than the output stream so that the
        result still reaches :meth:`_write`, which is what writes the log file
        and strips the colour codes out of it. Two write paths would mean tables
        never appearing in the log.

        Args:
            renderable: Anything Rich can draw.
        """
        buffer = io.StringIO()
        console = Console(
            file=buffer,
            width=self._width(),
            force_terminal=not self.no_color,
            no_color=self.no_color,
            # Rich's highlighter colours numbers, paths and UUIDs wherever it
            # finds them. In a table of ports and domains that paints almost
            # every cell, which is exactly the noise that makes colour stop
            # meaning anything.
            highlight=False,
            markup=False,
            emoji=False,
            soft_wrap=False,
        )
        console.print(renderable)
        self._write(buffer.getvalue().rstrip("\n"))

    def step(self, current: int, total: int, message: str, icon: str = "") -> None:
        """
        Log a step in a multi-step process.

        Args:
            current: Current step number.
            total: Total number of steps.
            message: Step description.
            icon: Optional icon to display.
        """
        self._current_step = current
        self._total_steps = total

        step_indicator = self._colorize(f"[{current}/{total}]", Colors.CYAN + Colors.BOLD)
        icon_str = f" {icon}" if icon else ""
        msg = self._colorize(f"{message}...", Colors.WHITE)

        self._write(f"{step_indicator}{icon_str} {msg}")

    def substep(self, message: str) -> None:
        """
        Log a substep (indented under current step).

        Args:
            message: Substep description.
        """
        if not self.verbose:
            return

        arrow = self._colorize(Icons.ARROW, Colors.GRAY)
        msg = self._colorize(message, Colors.GRAY)
        self._write(f"      {arrow} {msg}")

    def debug(self, message: str) -> None:
        """
        Log a debug message (only shown in verbose mode).

        Args:
            message: Debug message.
        """
        if not self.verbose:
            return

        prefix = self._colorize("[DEBUG]", Colors.GRAY)
        msg = self._colorize(message, Colors.GRAY)
        self._write(f"      {prefix} {msg}")

    def command_output(self, stdout: str, stderr: str) -> None:
        """
        Log command stdout/stderr (only shown in verbose mode).

        Args:
            stdout: Command standard output.
            stderr: Command standard error output.
        """
        if not self.verbose:
            return

        for stream in (stdout, stderr):
            if not stream or not stream.strip():
                continue
            for line in stream.rstrip("\n").split("\n"):
                text = self._colorize(f"        {line}", Colors.GRAY)
                self._write(text)

    def info(self, message: str) -> None:
        """
        Log an informational message.

        Args:
            message: Info message.
        """
        icon = self._colorize(Icons.INFO, Colors.BLUE)
        self._write(f"{icon} {message}")

    def success(self, message: str) -> None:
        """
        Log a success message.

        Args:
            message: Success message.
        """
        icon = self._colorize(Icons.SUCCESS, Colors.GREEN + Colors.BOLD)
        msg = self._colorize(message, Colors.GREEN)
        self._write(f"{icon} {msg}")

    def warning(self, message: str) -> None:
        """
        Log a warning message.

        Args:
            message: Warning message.
        """
        icon = self._colorize(Icons.WARNING, Colors.YELLOW + Colors.BOLD)
        msg = self._colorize(message, Colors.YELLOW)
        self._write(f"{icon} {msg}")

    def error(self, message: str, details: str = "") -> None:
        """
        Log an error message.

        Args:
            message: Error message.
            details: Optional error details.
        """
        icon = self._colorize(Icons.ERROR, Colors.RED + Colors.BOLD)
        msg = self._colorize(message, Colors.RED)
        self._write(f"{icon} {msg}")

        if details:
            detail_lines = details.strip().split("\n")
            for line in detail_lines:
                detail = self._colorize(f"  {line}", Colors.DIM + Colors.RED)
                self._write(detail)

    def check(self, key: str, value: str, outcome: str = "info") -> None:
        """
        Print the result of one check.

        Args:
            key: What was checked.
            value: What was found.
            outcome: One of "ok", "warning", "error" or "info". Anything else
                is treated as information rather than raising: a health check
                is the last command that should fail because of its own output.
        """
        icon, colour = CHECK_MARKERS.get(outcome, CHECK_MARKERS["info"])
        marker = self._colorize(icon, colour + Colors.BOLD)
        name = self._colorize(f"{key}:", Colors.BOLD)
        self._write(f"  {marker} {name} {self._colorize(value, colour)}")

    def blank(self) -> None:
        """Print a blank line."""
        self._write("")

    def header(self, title: str) -> None:
        """
        Print a header/title.

        Args:
            title: Header text.
        """
        from rich.rule import Rule

        self._write("")
        self._render(Rule(Text(title, style="bold"), align="left", style="cyan"))
        self._write("")

    def section(self, title: str) -> None:
        """
        Print a section title.

        Args:
            title: Section title.
        """
        self._write("")
        self._write(self._colorize(f"▸ {title}", Colors.BOLD))

    def key_value(self, key: str, value: str, indent: int = 2) -> None:
        """
        Print a key-value pair.

        Args:
            key: Key name.
            value: Value.
            indent: Indentation spaces.
        """
        spaces = " " * indent
        k = self._colorize(f"{key}:", Colors.GRAY)
        self._write(f"{spaces}{k} {value}")

    def list_item(self, item: str, indent: int = 2) -> None:
        """
        Print a list item.

        Args:
            item: Item text.
            indent: Indentation spaces.
        """
        spaces = " " * indent
        bullet = self._colorize(Icons.BULLET, Colors.CYAN)
        self._write(f"{spaces}{bullet} {item}")

    def progress(self, message: str, current: int, total: int) -> None:
        """
        Print a progress bar.

        Args:
            message: Progress message.
            current: Current progress.
            total: Total progress.
        """
        percentage = int((current / total) * 100)
        bar_length = 30
        filled_length = int(bar_length * current / total)

        bar = "█" * filled_length + "░" * (bar_length - filled_length)
        bar_colored = self._colorize(bar, Colors.CYAN)

        self._write(f"\r  {message} {bar_colored} {percentage}%", newline=False)

        if current >= total:
            self._write("")

    def table(self, headers: list, rows: list, justify: Sequence[str] | None = None) -> None:
        """
        Print a table.

        Cells are plain values or, where the value means something, the Text
        that :func:`state` and :func:`styled` return. Passing Text is how a
        column gets colour: this method never guesses what a value means.

        Args:
            headers: Column headers.
            rows: Rows, each a list of cells.
            justify: Optional per-column alignment, one of "left", "right" or
                "center". Numbers read better right-aligned.
        """
        if not rows:
            return

        table = Table(
            box=SIMPLE_HEAD,
            header_style="bold",
            border_style="dim",
            show_edge=False,
            pad_edge=False,
            expand=False,
        )
        for index, header in enumerate(headers):
            alignment = justify[index] if justify and index < len(justify) else "left"
            table.add_column(str(header), justify=cast("JustifyMethod", alignment), overflow="fold")

        for row in rows:
            table.add_row(*(cell if isinstance(cell, Text) else Text(str(cell)) for cell in row))

        self._render(table)

    def box(self, title: str, content: list) -> None:
        """
        Print content in a box.

        Args:
            title: Box title.
            content: List of content lines.
        """
        max_len = max(len(title), max(len(line) for line in content)) + 4

        top = "┌" + "─" * max_len + "┐"
        bottom = "└" + "─" * max_len + "┘"

        self._write(self._colorize(top, Colors.CYAN))
        self._write(self._colorize(f"│  {title.ljust(max_len - 2)}│", Colors.CYAN + Colors.BOLD))
        self._write(self._colorize("├" + "─" * max_len + "┤", Colors.CYAN))

        for line in content:
            self._write(self._colorize(f"│  {line.ljust(max_len - 2)}│", Colors.CYAN))

        self._write(self._colorize(bottom, Colors.CYAN))


class OutputFormat(Enum):
    """How a command result should be rendered."""

    TEXT = "text"
    JSON = "json"


class Presenter:
    """
    Render a command result either as human text or as machine-readable JSON.

    This is the seam a machine-readable ``--json`` needs: a handler builds the
    data once and hands it over, instead of calling print() directly. No CLI
    flag selects JSON yet, so every caller currently gets TEXT; see the module
    docs of the CLI parser for the migration status.

    Example:
        presenter = Presenter(logger)
        presenter.emit({"domain": "example.com", "status": "running"})
    """

    def __init__(
        self,
        logger: Logger | None = None,
        output_format: OutputFormat = OutputFormat.TEXT,
        stream: TextIO = sys.stdout,
    ):
        """
        Initialize the presenter.

        Args:
            logger: Logger used for text rendering (created if omitted).
            output_format: Rendering mode.
            stream: Stream JSON payloads are written to.
        """
        self.logger = logger if logger is not None else Logger(stream=stream)
        self.output_format = output_format
        self.stream = stream

    @property
    def is_json(self) -> bool:
        """
        Report whether this presenter emits JSON.

        Returns:
            True when the output format is JSON.
        """
        return self.output_format is OutputFormat.JSON

    def emit(
        self,
        payload: Mapping[str, Any],
        text: Callable[[Logger], None] | None = None,
    ) -> None:
        """
        Emit a single structured result.

        Args:
            payload: The data describing the result. Must be JSON serialisable.
            text: Optional custom text renderer. Defaults to one key-value line
                per top-level entry.
        """
        if self.is_json:
            self._dump(payload)
            return

        if text is not None:
            text(self.logger)
            return

        for key, value in payload.items():
            self.logger.key_value(key, str(value))

    def emit_table(
        self,
        headers: Sequence[str],
        rows: Sequence[Sequence[Any]],
        key: str = "items",
    ) -> None:
        """
        Emit a tabular result.

        Args:
            headers: Column headers, also used as JSON object keys.
            rows: Row values, aligned with the headers.
            key: Top-level key wrapping the rows in JSON mode.
        """
        if self.is_json:
            self._dump({key: [dict(zip(headers, row, strict=False)) for row in rows]})
            return

        self.logger.table(list(headers), [list(row) for row in rows])

    def emit_error(self, message: str, details: str = "") -> None:
        """
        Emit an error in the active format.

        Args:
            message: What went wrong.
            details: How to fix it.
        """
        if self.is_json:
            self._dump({"error": message, "details": details})
            return

        self.logger.error(message, details)

    def _dump(self, payload: Mapping[str, Any]) -> None:
        """
        Write a JSON payload to the stream.

        Args:
            payload: The data to serialise.
        """
        print(json.dumps(payload, default=str), file=self.stream, flush=True)
