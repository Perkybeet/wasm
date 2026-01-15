"""
Utility functions for WASM.

Common helper functions for shell commands, file operations,
string manipulation, and other utilities.
"""

import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Union


@dataclass
class CommandResult:
    """Result of a shell command execution."""
    
    success: bool
    stdout: str
    stderr: str
    exit_code: int
    command: str
    
    def __bool__(self) -> bool:
        """Allow using result in boolean context."""
        return self.success


def run_command(
    command: Union[str, List[str]],
    cwd: Optional[Path] = None,
    env: Optional[Dict[str, str]] = None,
    capture_output: bool = True,
    timeout: Optional[int] = None,
    shell: bool = False,
) -> CommandResult:
    """
    Execute a shell command.
    
    Args:
        command: Command to execute (string or list of arguments).
        cwd: Working directory for the command.
        env: Environment variables to set.
        capture_output: Whether to capture stdout/stderr.
        timeout: Command timeout in seconds.
        shell: Whether to run command through shell.
        
    Returns:
        CommandResult with execution results.
    """
    # Prepare command
    if isinstance(command, str) and not shell:
        cmd_list = command.split()
    else:
        cmd_list = command
    
    cmd_str = command if isinstance(command, str) else " ".join(command)
    
    # Prepare environment
    run_env = os.environ.copy()
    if env:
        run_env.update(env)
    
    try:
        result = subprocess.run(
            cmd_list,
            cwd=cwd,
            env=run_env,
            capture_output=capture_output,
            text=True,
            timeout=timeout,
            shell=shell,
        )
        
        return CommandResult(
            success=result.returncode == 0,
            stdout=result.stdout or "",
            stderr=result.stderr or "",
            exit_code=result.returncode,
            command=cmd_str,
        )
    except subprocess.TimeoutExpired:
        return CommandResult(
            success=False,
            stdout="",
            stderr=f"Command timed out after {timeout} seconds",
            exit_code=-1,
            command=cmd_str,
        )
    except FileNotFoundError:
        return CommandResult(
            success=False,
            stdout="",
            stderr=f"Command not found: {cmd_list[0] if cmd_list else command}",
            exit_code=127,
            command=cmd_str,
        )
    except Exception as e:
        return CommandResult(
            success=False,
            stdout="",
            stderr=str(e),
            exit_code=-1,
            command=cmd_str,
        )


def run_command_sudo(
    command: Union[str, List[str]],
    cwd: Optional[Path] = None,
    env: Optional[Dict[str, str]] = None,
    timeout: Optional[int] = None,
) -> CommandResult:
    """
    Execute a shell command with sudo.
    
    Args:
        command: Command to execute.
        cwd: Working directory.
        env: Environment variables.
        timeout: Command timeout in seconds.
        
    Returns:
        CommandResult with execution results.
    """
    if isinstance(command, str):
        cmd_list = ["sudo"] + command.split()
    else:
        cmd_list = ["sudo"] + list(command)
    
    return run_command(cmd_list, cwd=cwd, env=env, timeout=timeout)


# Whitelist of trusted installer URLs
TRUSTED_INSTALLER_URLS = frozenset([
    "https://deb.nodesource.com/setup_20.x",
    "https://deb.nodesource.com/setup_22.x",
    "https://bun.sh/install",
    "https://get.pnpm.io/install.sh",
])


def run_trusted_installer(
    url: str,
    timeout: Optional[int] = 300,
) -> CommandResult:
    """
    Execute a trusted installation script via curl | bash.

    Only allows whitelisted URLs to prevent command injection.
    This function is designed for installing well-known development tools
    (Node.js, Bun, pnpm) from their official sources.

    Args:
        url: URL of the installer script (must be in whitelist).
        timeout: Command timeout in seconds (default: 5 minutes).

    Returns:
        CommandResult with execution results.

    Raises:
        SecurityError: If URL is not in the trusted whitelist.
    """
    from wasm.core.exceptions import SecurityError

    if url not in TRUSTED_INSTALLER_URLS:
        raise SecurityError(
            f"Untrusted installer URL: {url}",
            f"Only the following URLs are allowed:\n"
            + "\n".join(f"  - {u}" for u in sorted(TRUSTED_INSTALLER_URLS))
        )

    # Use shell=True only for pipe, but URL is validated above
    return run_command(
        f"curl -fsSL '{url}' | bash",
        shell=True,
        timeout=timeout,
    )


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
    except Exception:
        return False


def ensure_directory_sudo(path: Path, owner: str = "www-data", group: str = "www-data") -> bool:
    """
    Ensure a directory exists using sudo.
    
    Args:
        path: Directory path.
        owner: Owner user.
        group: Owner group.
        
    Returns:
        True if successful.
    """
    result = run_command_sudo(["mkdir", "-p", str(path)])
    if not result.success:
        return False
    
    result = run_command_sudo(["chown", f"{owner}:{group}", str(path)])
    return result.success


def copy_file(src: Path, dest: Path, sudo: bool = False) -> bool:
    """
    Copy a file.
    
    Args:
        src: Source file path.
        dest: Destination file path.
        sudo: Use sudo for the operation.
        
    Returns:
        True if successful.
    """
    if sudo:
        return run_command_sudo(["cp", str(src), str(dest)]).success
    
    try:
        shutil.copy2(src, dest)
        return True
    except Exception:
        return False


def write_file(path: Path, content: str, sudo: bool = False, mode: int = 0o644) -> bool:
    """
    Write content to a file.
    
    Args:
        path: File path.
        content: Content to write.
        sudo: Use sudo for the operation.
        mode: File permission mode.
        
    Returns:
        True if successful.
    """
    if sudo:
        # Write to temp file, then move with sudo
        import tempfile
        try:
            with tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".tmp") as f:
                f.write(content)
                temp_path = f.name
            
            result = run_command_sudo(["mv", temp_path, str(path)])
            if result.success:
                run_command_sudo(["chmod", oct(mode)[2:], str(path)])
            return result.success
        except Exception:
            return False
    
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        path.chmod(mode)
        return True
    except Exception:
        return False


def read_file(path: Path, sudo: bool = False) -> Optional[str]:
    """
    Read content from a file.
    
    Args:
        path: File path.
        sudo: Use sudo for the operation.
        
    Returns:
        File content or None if failed.
    """
    if sudo:
        result = run_command_sudo(["cat", str(path)])
        return result.stdout if result.success else None
    
    try:
        return path.read_text()
    except Exception:
        return None


def remove_file(path: Path, sudo: bool = False) -> bool:
    """
    Remove a file.
    
    Args:
        path: File path.
        sudo: Use sudo for the operation.
        
    Returns:
        True if successful.
    """
    if sudo:
        return run_command_sudo(["rm", "-f", str(path)]).success
    
    try:
        path.unlink(missing_ok=True)
        return True
    except Exception:
        return False


def remove_directory(path: Path, sudo: bool = False) -> bool:
    """
    Remove a directory recursively.
    
    Args:
        path: Directory path.
        sudo: Use sudo for the operation.
        
    Returns:
        True if successful.
    """
    if sudo:
        return run_command_sudo(["rm", "-rf", str(path)]).success
    
    try:
        shutil.rmtree(path, ignore_errors=True)
        return True
    except Exception:
        return False


def create_symlink(source: Path, link: Path, sudo: bool = False) -> bool:
    """
    Create a symbolic link.
    
    Args:
        source: Source path.
        link: Link path.
        sudo: Use sudo for the operation.
        
    Returns:
        True if successful.
    """
    if sudo:
        # Remove existing link first
        run_command_sudo(["rm", "-f", str(link)])
        return run_command_sudo(["ln", "-s", str(source), str(link)]).success
    
    try:
        link.unlink(missing_ok=True)
        link.symlink_to(source)
        return True
    except Exception:
        return False


def find_available_port(start: int = 3000, end: int = 9000) -> Optional[int]:
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


def get_system_info() -> Dict[str, str]:
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
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if bytes_size < 1024:
            return f"{bytes_size:.1f} {unit}"
        bytes_size /= 1024
    return f"{bytes_size:.1f} PB"


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
