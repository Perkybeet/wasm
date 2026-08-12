"""
Utility functions for WASM.

Common helper functions for shell commands, file operations,
string manipulation, and other utilities.

The command helpers here are a thin facade over
:class:`~wasm.core.runner.CommandRunner`. They exist so that legacy call sites
keep working, not as a second way to reach the machine: every one of them
delegates, which is what makes ``--dry-run``, the timeout policy and the
"no real subprocess in tests" guarantee hold for the whole program.
"""

from __future__ import annotations

import os
import re
import shutil
from collections.abc import Mapping, Sequence
from pathlib import Path

from wasm.core.runner import DEFAULT_TIMEOUT, CommandResult, CommandRunner, get_runner

__all__ = [
    "DEFAULT_COMMAND_TIMEOUT",
    "TRUSTED_INSTALLER_URLS",
    "CommandResult",
    "check_root",
    "command_exists",
    "copy_file",
    "create_symlink",
    "domain_to_app_name",
    "ensure_directory",
    "find_available_port",
    "format_bytes",
    "format_duration",
    "get_system_info",
    "is_port_in_use",
    "legacy_app_name",
    "read_file",
    "remove_directory",
    "remove_file",
    "run_command",
    "run_trusted_installer",
    "sanitize_name",
    "validate_url",
    "write_file",
]

#: Deadline used when a caller does not pass one. A finite default is the whole
#: point: the previous ``timeout=None`` meant 89% of the call sites could hang
#: a deploy forever.
DEFAULT_COMMAND_TIMEOUT = DEFAULT_TIMEOUT

#: Installer scripts are fetched and piped to bash; give them room to compile.
INSTALLER_TIMEOUT = 300


def run_command(
    command: Sequence[str],
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
    timeout: int = DEFAULT_COMMAND_TIMEOUT,
    *,
    runner: CommandRunner | None = None,
) -> CommandResult:
    """
    Execute an external command through the process-wide command runner.

    Args:
        command: Program and arguments. Must be a sequence; a bare string is
            rejected because splitting one is how quoted paths and injected
            arguments used to become separate words.
        cwd: Working directory for the command.
        env: Extra environment variables, merged over the current environment.
        timeout: Deadline in seconds. Always finite.
        runner: Runner to execute with. Defaults to the process-wide runner,
            which is what honours ``--dry-run``.

    Returns:
        The command outcome.

    Raises:
        ValueError: If ``command`` is a string or an empty sequence.
    """
    if isinstance(command, (str, bytes)):
        raise ValueError(
            "run_command expects a sequence of arguments, not a string. "
            "Pass ['git', 'clone', url] instead of 'git clone ' + url; "
            "string splitting breaks quoted paths and hides injection."
        )
    active = runner if runner is not None else get_runner()
    return active.run(list(command), cwd=cwd, env=env, timeout=timeout)


def run_command_sudo(
    command: Sequence[str],
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
    timeout: int = DEFAULT_COMMAND_TIMEOUT,
) -> CommandResult:
    """
    Deprecated. Execute a command as the current (root) account.

    Decision D6 of the v1 design is that WASM requires root, so prefixing
    argument vectors with ``sudo`` bought nothing and hid the requirement: on a
    root shell it forked an extra process, and on a non-root shell it produced a
    password prompt in the middle of a deploy. This shim survives only so that
    call sites outside this module keep importing; it no longer adds ``sudo``.
    New code must call :func:`run_command` or the runner directly.

    Args:
        command: Program and arguments.
        cwd: Working directory for the command.
        env: Extra environment variables.
        timeout: Deadline in seconds.

    Returns:
        The command outcome.

    Raises:
        ValueError: If ``command`` is a string or an empty sequence.
    """
    return run_command(command, cwd=cwd, env=env, timeout=timeout)


# Whitelist of trusted installer URLs
TRUSTED_INSTALLER_URLS = frozenset(
    [
        "https://deb.nodesource.com/setup_20.x",
        "https://deb.nodesource.com/setup_22.x",
        "https://bun.sh/install",
        "https://get.pnpm.io/install.sh",
    ]
)


def run_trusted_installer(
    url: str,
    timeout: int = INSTALLER_TIMEOUT,
    *,
    runner: CommandRunner | None = None,
) -> CommandResult:
    """
    Download a whitelisted installer script and execute it with bash.

    The script is fetched first and handed to ``bash -s`` on stdin, so there is
    no shell pipeline and no point at which the URL is reinterpreted as shell
    syntax. Only the whitelisted URLs are accepted.

    Args:
        url: URL of the installer script (must be in the whitelist).
        timeout: Deadline in seconds for each of the two steps.
        runner: Runner to execute with. Defaults to the process-wide runner.

    Returns:
        The outcome of the installer script, or of the download when it failed.

    Raises:
        SecurityError: If the URL is not in the trusted whitelist.
    """
    from wasm.core.exceptions import SecurityError

    if url not in TRUSTED_INSTALLER_URLS:
        raise SecurityError(
            f"Untrusted installer URL: {url}",
            "Only the following URLs are allowed:\n"
            + "\n".join(f"  - {u}" for u in sorted(TRUSTED_INSTALLER_URLS)),
        )

    active = runner if runner is not None else get_runner()
    download = active.run(["curl", "-fsSL", url], timeout=timeout)
    if not download.success:
        return download

    # ``bash -s`` reads the program from stdin, so the script never becomes part
    # of an argument vector and never reaches a shell as text to be parsed.
    return active.run(["bash", "-s"], input=download.stdout, timeout=timeout)


def command_exists(command: str) -> bool:
    """
    Check if a command exists in PATH.

    Args:
        command: Command name to check.

    Returns:
        True if command exists, False otherwise.
    """
    return shutil.which(command) is not None


def sanitize_name(name: str) -> str:
    """
    Sanitize a name for use as filename or service name.

    Converts domain names or other strings to safe identifiers.
    Example: "my-app.example.com" -> "my-app-example-com"

    Args:
        name: Name to sanitize.

    Returns:
        Sanitized name.
    """
    # Replace dots and special chars with hyphens
    sanitized = re.sub(r"[^a-zA-Z0-9-]", "-", name.lower())
    # Remove consecutive hyphens
    sanitized = re.sub(r"-+", "-", sanitized)
    # Remove leading/trailing hyphens
    sanitized = sanitized.strip("-")
    return sanitized


def domain_to_app_name(domain: str) -> str:
    """
    Convert a domain to an application name.

    Args:
        domain: Domain name (e.g., "myapp.example.com").

    Returns:
        Application name (e.g., "myapp-example-com").
    """
    return sanitize_name(domain)


def legacy_app_name(domain: str) -> str:
    """
    Get legacy app name format (with wasm- prefix).

    Used for backwards compatibility with apps created before v0.14.1.

    Args:
        domain: Domain name (e.g., "myapp.example.com").

    Returns:
        Legacy application name (e.g., "wasm-myapp-example-com").
    """
    return f"wasm-{sanitize_name(domain)}"


def ensure_directory(path: Path, mode: int = 0o755) -> bool:
    """
    Ensure a directory exists, creating it if necessary.

    Args:
        path: Directory path.
        mode: Permission mode for new directories.

    Returns:
        True if directory exists or was created.
    """
    try:
        path.mkdir(parents=True, exist_ok=True, mode=mode)
        return True
    except OSError:
        return False


def ensure_directory_sudo(path: Path, owner: str = "www-data", group: str = "www-data") -> bool:
    """
    Ensure a directory exists and belongs to the given account.

    Args:
        path: Directory path.
        owner: Owner user name.
        group: Owner group name.

    Returns:
        True if the directory exists and ownership was applied.
    """
    import grp
    import pwd

    try:
        path.mkdir(parents=True, exist_ok=True)
        uid = pwd.getpwnam(owner).pw_uid
        gid = grp.getgrnam(group).gr_gid
        os.chown(path, uid, gid)
        return True
    except (OSError, KeyError):
        return False


def copy_file(src: Path, dest: Path, sudo: bool = False) -> bool:
    """
    Copy a file.

    Args:
        src: Source file path.
        dest: Destination file path.
        sudo: Ignored. WASM already runs as root, so shelling out to ``sudo cp``
            only added a process and a PATH dependency.

    Returns:
        True if successful.
    """
    try:
        shutil.copy2(src, dest)
        return True
    except OSError:
        return False


def write_file(path: Path, content: str, sudo: bool = False, mode: int = 0o644) -> bool:
    """
    Write content to a file.

    Args:
        path: File path.
        content: Content to write.
        sudo: Ignored. See :func:`copy_file`.
        mode: File permission mode.

    Returns:
        True if successful.
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        path.chmod(mode)
        return True
    except OSError:
        return False


def read_file(path: Path, sudo: bool = False) -> str | None:
    """
    Read content from a file.

    Args:
        path: File path.
        sudo: Ignored. See :func:`copy_file`.

    Returns:
        File content or None if it could not be read.
    """
    try:
        return path.read_text()
    except OSError:
        return None


def remove_file(path: Path, sudo: bool = False) -> bool:
    """
    Remove a file.

    Args:
        path: File path.
        sudo: Ignored. See :func:`copy_file`.

    Returns:
        True if successful.
    """
    try:
        path.unlink(missing_ok=True)
        return True
    except OSError:
        return False


def remove_directory(path: Path, sudo: bool = False) -> bool:
    """
    Remove a directory recursively.

    Args:
        path: Directory path.
        sudo: Ignored. See :func:`copy_file`.

    Returns:
        True if successful.
    """
    try:
        shutil.rmtree(path, ignore_errors=True)
        return True
    except OSError:
        return False


def create_symlink(source: Path, link: Path, sudo: bool = False) -> bool:
    """
    Create a symbolic link, replacing any existing one.

    Args:
        source: Source path.
        link: Link path.
        sudo: Ignored. See :func:`copy_file`.

    Returns:
        True if successful.
    """
    try:
        link.unlink(missing_ok=True)
        link.symlink_to(source)
        return True
    except OSError:
        return False


def find_available_port(start: int = 3000, end: int = 9000) -> int | None:
    """
    Find an available port in the given range.

    Args:
        start: Start of port range.
        end: End of port range.

    Returns:
        Available port number or None.
    """
    import socket

    for port in range(start, end):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.bind(("127.0.0.1", port))
                return port
        except OSError:
            continue

    return None


def is_port_in_use(port: int) -> bool:
    """
    Check if a port is in use.

    Args:
        port: Port number to check.

    Returns:
        True if port is in use.
    """
    import socket

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", port))
            return False
    except OSError:
        return True


def get_system_info() -> dict[str, str]:
    """
    Get basic system information.

    Returns:
        Dictionary with system information.
    """
    info = {}

    # OS info
    result = run_command(["lsb_release", "-d", "-s"])
    info["os"] = result.stdout.strip() if result.success else "Unknown"

    # Kernel
    result = run_command(["uname", "-r"])
    info["kernel"] = result.stdout.strip() if result.success else "Unknown"

    # Check for nginx
    result = run_command(["nginx", "-v"])
    info["nginx"] = result.stderr.split("/")[1].strip() if result.success else "Not installed"

    # Check for apache
    result = run_command(["apache2", "-v"])
    if result.success:
        match = re.search(r"Apache/(\S+)", result.stdout)
        info["apache"] = match.group(1) if match else "Installed"
    else:
        info["apache"] = "Not installed"

    # Node.js
    result = run_command(["node", "--version"])
    info["nodejs"] = result.stdout.strip() if result.success else "Not installed"

    # Python
    result = run_command(["python3", "--version"])
    info["python"] = result.stdout.strip() if result.success else "Not installed"

    return info


def check_root() -> bool:
    """
    Check if running as root.

    Returns:
        True if running as root.
    """
    return os.geteuid() == 0


def validate_url(url: str) -> bool:
    """
    Validate a URL.

    Args:
        url: URL to validate.

    Returns:
        True if valid URL.
    """
    url_pattern = re.compile(
        r"^(https?|git|ssh)://"
        r"(?:(?:[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?\.)+[A-Z]{2,6}\.?|"
        r"localhost|"
        r"\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})"
        r"(?::\d+)?"
        r"(?:/?|[/?]\S+)$",
        re.IGNORECASE,
    )

    # Also check for git SSH format
    git_ssh_pattern = re.compile(r"^git@[\w.-]+:[\w./-]+\.git$")

    return bool(url_pattern.match(url) or git_ssh_pattern.match(url))


def format_bytes(bytes_size: int) -> str:
    """
    Format bytes to human readable string.

    Args:
        bytes_size: Size in bytes.

    Returns:
        Human readable size string.
    """
    size = float(bytes_size)
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if size < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} PB"


def format_duration(seconds: float) -> str:
    """
    Format seconds to human readable duration.

    Args:
        seconds: Duration in seconds.

    Returns:
        Human readable duration string.
    """
    if seconds < 60:
        return f"{seconds:.1f}s"
    elif seconds < 3600:
        minutes = int(seconds // 60)
        secs = seconds % 60
        return f"{minutes}m {secs:.0f}s"
    else:
        hours = int(seconds // 3600)
        minutes = int((seconds % 3600) // 60)
        return f"{hours}h {minutes}m"
