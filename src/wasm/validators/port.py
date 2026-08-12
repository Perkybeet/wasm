"""
Port validation for WASM.
"""

import socket

from wasm.core.exceptions import PortError

# Port ranges
MIN_PORT = 1
MAX_PORT = 65535
PRIVILEGED_PORT = 1024
COMMON_PORTS = {
    80: "HTTP",
    443: "HTTPS",
    22: "SSH",
    21: "FTP",
    25: "SMTP",
    53: "DNS",
    3306: "MySQL",
    5432: "PostgreSQL",
    6379: "Redis",
    27017: "MongoDB",
}

# Default ports by app type
DEFAULT_PORTS = {
    "nextjs": 3000,
    "nodejs": 3000,
    "vite": 5173,
    "python": 8000,
    "django": 8000,
    "flask": 5000,
    "fastapi": 8000,
    "static": 80,
}


def is_valid_port(port: int) -> tuple[bool, str]:
    """
    Check if a port number is valid.

    Args:
        port: Port number to validate.

    Returns:
        Tuple of (is_valid, error_message).
    """
    if not isinstance(port, int):
        return False, "Port must be an integer"

    if port < MIN_PORT or port > MAX_PORT:
        return False, f"Port must be between {MIN_PORT} and {MAX_PORT}"

    return True, ""


def check_port(port) -> bool:
    """
    Check if a port is valid (boolean only, accepts strings).

    Args:
        port: Port number (int or string).

    Returns:
        True if valid, False otherwise.
    """
    if isinstance(port, str):
        try:
            port = int(port)
        except (ValueError, TypeError):
            return False

    is_valid, _ = is_valid_port(port)
    return is_valid


def is_port_available(port: int, host: str = "127.0.0.1") -> bool:
    """
    Check if a port is available (not in use).

    Args:
        port: Port number to check.
        host: Host to check on.

    Returns:
        True if port is available.
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(1)
            result = sock.connect_ex((host, port))
            return result != 0
    except Exception:
        return False


def validate_port(
    port: int,
    check_available: bool = True,
    allow_privileged: bool = False,
) -> int:
    """
    Validate a port number.

    Args:
        port: Port number to validate.
        check_available: Check if port is available.
        allow_privileged: Allow privileged ports (< 1024).

    Returns:
        Validated port number.

    Raises:
        PortError: If port is invalid or unavailable.
    """
    # Handle string input
    if isinstance(port, str):
        try:
            port = int(port)
        except ValueError:
            raise PortError(f"Invalid port number: '{port}'")

    # Validate range
    is_valid, error = is_valid_port(port)
    if not is_valid:
        raise PortError(error)

    # Check privileged
    if not allow_privileged and port < PRIVILEGED_PORT:
        if port not in [80, 443]:  # Allow common web ports
            raise PortError(
                f"Port {port} is a privileged port (< {PRIVILEGED_PORT}). "
                "Use a port >= 1024 or run with elevated privileges."
            )

    # Check availability
    if check_available and not is_port_available(port):
        service = COMMON_PORTS.get(port, "unknown service")
        raise PortError(
            f"Port {port} is already in use",
            details=f"Common service on this port: {service}",
        )

    return port


def find_available_port(
    start: int = 3000,
    end: int = 9000,
    preferred: int | None = None,
) -> int | None:
    """
    Find an available port.

    Args:
        start: Start of range to search.
        end: End of range to search.
        preferred: Preferred port to try first.

    Returns:
        Available port number or None.
    """
    # Try preferred port first
    if preferred and is_port_available(preferred):
        return preferred

    # Search range
    for port in range(start, end):
        if is_port_available(port):
            return port

    return None


def get_default_port(app_type: str) -> int:
    """
    Get the default port for an application type.

    Args:
        app_type: Application type.

    Returns:
        Default port number.
    """
    return DEFAULT_PORTS.get(app_type.lower(), 3000)


def parse_port_string(port_str: str) -> int:
    """
    Parse a port from string, handling various formats.

    Args:
        port_str: Port string (e.g., "3000", ":3000", "http://localhost:3000").

    Returns:
        Port number.

    Raises:
        PortError: If parsing fails.
    """
    port_str = port_str.strip()

    # Handle URL format
    if "://" in port_str:
        # Extract port from URL
        import re

        match = re.search(r":(\d+)", port_str.split("://")[1])
        if match:
            return int(match.group(1))
        # Default ports for protocols
        if port_str.startswith("https"):
            return 443
        return 80

    # Handle :port format
    if port_str.startswith(":"):
        port_str = port_str[1:]

    try:
        return int(port_str)
    except ValueError:
        raise PortError(f"Cannot parse port from: '{port_str}'")
