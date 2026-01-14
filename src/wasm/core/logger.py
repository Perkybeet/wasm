"""
Custom logging system for WASM.

Provides a rich, user-friendly logging experience with support for:
- Step-by-step progress indicators
- Verbose mode for detailed output
- Color-coded output
- Structured log messages
"""

import logging
import sys
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Optional, TextIO


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
    INFO = "ℹ"
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
        log_file: Optional[Path] = None,
        stream: TextIO = sys.stdout,
    ):
        """
        Initialize the logger.
        
        Args:
            verbose: Enable verbose output (shows debug messages).
            no_color: Disable colored output.
            log_file: Optional file path to write logs to.
            stream: Output stream (defaults to stdout).
        """
        self.verbose = verbose
        self.stream = stream  # Must be set before _supports_color() is called
        self.no_color = no_color or not self._supports_color()
        self.log_file = log_file
        self._current_step = 0
        self._total_steps = 0
    
    def _supports_color(self) -> bool:
        """Check if the terminal supports color output."""
        if not hasattr(self.stream, "isatty"):
            return False
        if not self.stream.isatty():
            return False
        return True
    
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
    
    def blank(self) -> None:
        """Print a blank line."""
        self._write("")
    
    def header(self, title: str) -> None:
        """
        Print a header/title.
        
        Args:
            title: Header text.
        """
        line = "─" * 50
        self._write("")
        self._write(self._colorize(line, Colors.CYAN))
        self._write(self._colorize(f"  {title}", Colors.CYAN + Colors.BOLD))
        self._write(self._colorize(line, Colors.CYAN))
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
    
    def table(self, headers: list, rows: list) -> None:
        """
        Print a formatted table.
        
        Args:
            headers: List of column headers.
            rows: List of rows (each row is a list of values).
        """
        if not rows:
            return
        
        # Calculate column widths
        all_rows = [headers] + rows
        col_widths = []
        for col_idx in range(len(headers)):
            max_width = max(len(str(row[col_idx])) for row in all_rows)
            col_widths.append(max_width + 2)
        
        # Print header
        header_line = ""
        for idx, header in enumerate(headers):
            header_line += self._colorize(str(header).ljust(col_widths[idx]), Colors.BOLD)
        self._write(header_line)
        
        # Print separator
        separator = "─" * sum(col_widths)
        self._write(self._colorize(separator, Colors.GRAY))
        
        # Print rows
        for row in rows:
            row_line = ""
            for idx, cell in enumerate(row):
                row_line += str(cell).ljust(col_widths[idx])
            self._write(row_line)
    
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
