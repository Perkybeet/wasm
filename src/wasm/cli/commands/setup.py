"""
Setup command handlers for WASM - initial setup, completions, permissions, and doctor.
"""

import os
import sys
import shutil
from argparse import Namespace
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from wasm.core.logger import Logger
from wasm.core.exceptions import WASMError
from wasm.core.config import DEFAULT_APPS_DIR, DEFAULT_LOG_DIR, DEFAULT_CONFIG_PATH
from wasm.core.utils import command_exists, run_command, run_command_sudo


def handle_setup(args: Namespace) -> int:
    """
    Handle setup commands.
    
    Args:
        args: Parsed arguments.
        
    Returns:
        Exit code.
    """
    action = args.action
    
    handlers = {
        "completions": _handle_completions,
        "init": _handle_init,
        "permissions": _handle_permissions,
        "ssh": _handle_ssh,
        "doctor": _handle_doctor,
    }
    
    handler = handlers.get(action)
    if not handler:
        print(f"Unknown action: {action}", file=sys.stderr)
        return 1
    
    try:
        return handler(args)
    except WASMError as e:
        logger = Logger(verbose=args.verbose)
        logger.error(str(e))
        return 1
    except Exception as e:
        logger = Logger(verbose=args.verbose)
        logger.error(f"Unexpected error: {e}")
        if args.verbose:
            import traceback
            traceback.print_exc()
        return 1


def _handle_completions(args: Namespace) -> int:
    """Handle completions setup command."""
    logger = Logger(verbose=args.verbose)
    shell = args.shell
    
    # Auto-detect shell if not specified
    if not shell:
        shell = _detect_shell()
        if not shell:
            logger.error("Could not detect shell. Please specify with --shell")
            return 1
    
    logger.header("WASM Shell Completions Setup")
    logger.key_value("Shell", shell)
    logger.blank()
    
    # Get completion script path (from cli/commands/ up to wasm/completions/)
    completions_dir = Path(__file__).parent.parent.parent / "completions"
    
    if shell == "bash":
        return _install_bash_completions(logger, completions_dir, args.user_only)
    elif shell == "zsh":
        return _install_zsh_completions(logger, completions_dir, args.user_only)
    elif shell == "fish":
        return _install_fish_completions(logger, completions_dir, args.user_only)
    else:
        logger.error(f"Unsupported shell: {shell}")
        return 1


def _detect_shell() -> str | None:
    """Detect the current shell."""
    shell_path = os.environ.get("SHELL", "")
    if "bash" in shell_path:
        return "bash"
    elif "zsh" in shell_path:
        return "zsh"
    elif "fish" in shell_path:
        return "fish"
    return None


def _install_bash_completions(logger: Logger, completions_dir: Path, user_only: bool) -> int:
    """Install bash completions."""
    source_file = completions_dir / "wasm.bash"
    
    if not source_file.exists():
        logger.error("Bash completion script not found")
        return 1
    
    if user_only:
        target_dir = Path.home() / ".local" / "share" / "bash-completion" / "completions"
        target_file = target_dir / "wasm"
    else:
        target_dir = Path("/etc/bash_completion.d")
        target_file = target_dir / "wasm"
        
        if os.geteuid() != 0:
            logger.error("System-wide installation requires root privileges")
            logger.info("Use --user-only for user-local installation, or run with sudo")
            return 1
    
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy(source_file, target_file)
        logger.success(f"Installed bash completions to {target_file}")
        logger.info("Restart your shell or run: source ~/.bashrc")
        return 0
    except PermissionError:
        logger.error(f"Permission denied writing to {target_file}")
        logger.info("Try running with sudo or use --user-only")
        return 1


def _install_zsh_completions(logger: Logger, completions_dir: Path, user_only: bool) -> int:
    """Install zsh completions."""
    source_file = completions_dir / "_wasm"
    
    if not source_file.exists():
        logger.error("Zsh completion script not found")
        return 1
    
    if user_only:
        target_dir = Path.home() / ".zsh" / "completions"
        target_file = target_dir / "_wasm"
        fpath_hint = 'Add to .zshrc: fpath=(~/.zsh/completions $fpath)'
    else:
        target_dir = Path("/usr/share/zsh/site-functions")
        if not target_dir.exists():
            target_dir = Path("/usr/local/share/zsh/site-functions")
        target_file = target_dir / "_wasm"
        fpath_hint = None
        
        if os.geteuid() != 0:
            logger.error("System-wide installation requires root privileges")
            logger.info("Use --user-only for user-local installation, or run with sudo")
            return 1
    
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy(source_file, target_file)
        logger.success(f"Installed zsh completions to {target_file}")
        if fpath_hint:
            logger.info(fpath_hint)
        logger.info("Run: autoload -Uz compinit && compinit")
        return 0
    except PermissionError:
        logger.error(f"Permission denied writing to {target_file}")
        logger.info("Try running with sudo or use --user-only")
        return 1


def _install_fish_completions(logger: Logger, completions_dir: Path, user_only: bool) -> int:
    """Install fish completions."""
    source_file = completions_dir / "wasm.fish"
    
    if not source_file.exists():
        logger.error("Fish completion script not found")
        return 1
    
    if user_only:
        target_dir = Path.home() / ".config" / "fish" / "completions"
        target_file = target_dir / "wasm.fish"
    else:
        target_dir = Path("/usr/share/fish/vendor_completions.d")
        target_file = target_dir / "wasm.fish"
        
        if os.geteuid() != 0:
            logger.error("System-wide installation requires root privileges")
            logger.info("Use --user-only for user-local installation, or run with sudo")
            return 1
    
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy(source_file, target_file)
        logger.success(f"Installed fish completions to {target_file}")
        logger.info("Completions should work immediately or run: exec fish")
        return 0
    except PermissionError:
        logger.error(f"Permission denied writing to {target_file}")
        logger.info("Try running with sudo or use --user-only")
        return 1


def _handle_init(args: Namespace) -> int:
    """
    Handle initial system setup - comprehensive guided wizard.
    
    This is the main setup command that guides users through:
    1. System requirements check
    2. Directory creation
    3. Web server selection and installation
    4. Node.js and package managers setup
    5. SSL configuration
    6. Configuration file creation
    """
    logger = Logger(verbose=args.verbose)
    
    # Check for root
    if os.geteuid() != 0:
        logger.error("Initial setup requires root privileges")
        logger.info("Run: sudo wasm setup init")
        return 1
    
    logger.header("WASM Initial Setup Wizard")
    logger.info("This wizard will configure your system for deploying web applications.")
    logger.blank()
    
    # Check if inquirer is available for interactive mode
    try:
        import inquirer
        from inquirer.themes import GreenPassion
        has_inquirer = True
    except ImportError:
        has_inquirer = False
        logger.warning("Interactive mode unavailable (inquirer not installed)")
        logger.info("Running in non-interactive mode with defaults")
    
    # Import dependency checker
    from wasm.core.dependencies import DependencyChecker
    checker = DependencyChecker(verbose=args.verbose)
    
    # =========================================================================
    # Phase 1: System Analysis
    # =========================================================================
    logger.step(1, 6, "Analyzing system requirements")
    
    summary = checker.get_setup_summary()
    
    # Display current status
    logger.blank()
    logger.info("Current System Status:")
    
    # OS Info
    from wasm.core.utils import get_system_info
    sys_info = get_system_info()
    logger.key_value("  OS", sys_info.get("os", "Unknown"))
    logger.key_value("  Kernel", sys_info.get("kernel", "Unknown"))
    
    # Web server
    if summary["webserver"]:
        logger.key_value("  Web Server", f"✓ {summary['webserver']}")
    else:
        logger.key_value("  Web Server", "✗ Not installed")
    
    # Node.js
    if summary["nodejs"]["installed"]:
        logger.key_value("  Node.js", f"✓ {summary['nodejs']['version']}")
        installed_pms = [pm for pm, info in summary["nodejs"]["package_managers"].items() if info["installed"]]
        logger.key_value("  Package Managers", ", ".join(installed_pms) if installed_pms else "npm only")
    else:
        logger.key_value("  Node.js", "✗ Not installed")
    
    # Python
    if summary["python"]["installed"]:
        logger.key_value("  Python", f"✓ {summary['python']['version']}")
    else:
        logger.key_value("  Python", "✗ Not installed")
    
    # Git
    if command_exists("git"):
        logger.key_value("  Git", "✓ Installed")
    else:
        logger.key_value("  Git", "✗ Not installed")
    
    # Certbot
    if command_exists("certbot"):
        logger.key_value("  Certbot", "✓ Installed")
    else:
        logger.key_value("  Certbot", "✗ Not installed")
    
    logger.blank()
    
    # =========================================================================
    # Phase 2: Interactive Configuration (if available)
    # =========================================================================
    config_choices = {
        "install_git": not command_exists("git"),
        "install_webserver": summary["webserver"] is None,
        "webserver_choice": "nginx",
        "install_nodejs": not summary["nodejs"]["installed"],
        "package_managers": ["npm"],  # Default to npm
        "install_certbot": not command_exists("certbot"),
        "ssl_email": "",
    }
    
    if has_inquirer:
        logger.step(2, 6, "Configuration options")
        config_choices = _interactive_setup_prompts(logger, summary, checker)
        if config_choices is None:
            logger.info("Setup cancelled")
            return 0
    else:
        logger.step(2, 6, "Using default configuration")
    
    # =========================================================================
    # Phase 3: Install System Dependencies
    # =========================================================================
    logger.step(3, 6, "Installing system dependencies")
    
    # Update package lists first
    logger.substep("Updating package lists...")
    run_command_sudo(["apt-get", "update"])
    
    # Install git if needed
    if config_choices.get("install_git") and not command_exists("git"):
        logger.substep("Installing Git...")
        result = run_command_sudo(["apt-get", "install", "-y", "git"])
        if result.success:
            logger.success("Git installed")
        else:
            logger.warning(f"Failed to install Git: {result.stderr}")
    
    # Install web server if needed
    if config_choices.get("install_webserver"):
        ws = config_choices.get("webserver_choice", "nginx")
        logger.substep(f"Installing {ws}...")
        
        result = run_command_sudo(["apt-get", "install", "-y", ws])
        if result.success:
            logger.success(f"{ws} installed")
            # Enable and start the service
            run_command_sudo(["systemctl", "enable", ws])
            run_command_sudo(["systemctl", "start", ws])
        else:
            logger.warning(f"Failed to install {ws}: {result.stderr}")
    
    # Install certbot if needed
    if config_choices.get("install_certbot") and not command_exists("certbot"):
        logger.substep("Installing Certbot...")
        ws = config_choices.get("webserver_choice", "nginx")
        certbot_pkg = f"python3-certbot-{ws}" if ws in ["nginx", "apache"] else "certbot"
        
        result = run_command_sudo(["apt-get", "install", "-y", "certbot", certbot_pkg])
        if result.success:
            logger.success("Certbot installed")
        else:
            logger.warning(f"Failed to install Certbot: {result.stderr}")
    
    # =========================================================================
    # Phase 4: Install Node.js and Package Managers
    # =========================================================================
    logger.step(4, 6, "Setting up Node.js environment")
    
    if config_choices.get("install_nodejs") and not command_exists("node"):
        logger.substep("Installing Node.js 20.x LTS...")
        
        # Install Node.js from NodeSource
        result = run_command(
            "curl -fsSL https://deb.nodesource.com/setup_20.x | bash -",
            shell=True
        )
        if result.success:
            result = run_command_sudo(["apt-get", "install", "-y", "nodejs"])
            if result.success:
                logger.success("Node.js installed")
            else:
                logger.warning(f"Failed to install Node.js: {result.stderr}")
        else:
            logger.warning("Failed to setup Node.js repository. Please install manually.")
    
    # Install package managers
    selected_pms = config_choices.get("package_managers", ["npm"])
    
    for pm in selected_pms:
        if pm == "npm":
            continue  # npm comes with Node.js
        
        if command_exists(pm):
            logger.substep(f"{pm} already installed")
            continue
        
        logger.substep(f"Installing {pm}...")
        
        if pm == "pnpm":
            result = run_command_sudo(["npm", "install", "-g", "pnpm"])
        elif pm == "yarn":
            result = run_command_sudo(["npm", "install", "-g", "yarn"])
        elif pm == "bun":
            # Bun has its own installer
            result = run_command("curl -fsSL https://bun.sh/install | bash", shell=True)
        else:
            result = run_command_sudo(["npm", "install", "-g", pm])
        
        if result.success:
            logger.success(f"{pm} installed")
        else:
            logger.warning(f"Failed to install {pm}: {result.stderr}")
    
    # =========================================================================
    # Phase 5: Create WASM Directories
    # =========================================================================
    logger.step(5, 6, "Creating WASM directories")
    
    # Create apps directory
    logger.substep(f"Creating apps directory: {DEFAULT_APPS_DIR}")
    try:
        DEFAULT_APPS_DIR.mkdir(parents=True, exist_ok=True)
        os.chmod(DEFAULT_APPS_DIR, 0o755)
        logger.success(f"Created {DEFAULT_APPS_DIR}")
    except Exception as e:
        logger.warning(f"Failed to create apps directory: {e}")
    
    # Create log directory
    logger.substep(f"Creating log directory: {DEFAULT_LOG_DIR}")
    try:
        DEFAULT_LOG_DIR.mkdir(parents=True, exist_ok=True)
        os.chmod(DEFAULT_LOG_DIR, 0o755)
        logger.success(f"Created {DEFAULT_LOG_DIR}")
    except Exception as e:
        logger.warning(f"Failed to create log directory: {e}")
    
    # Create config directory
    config_dir = DEFAULT_CONFIG_PATH.parent
    logger.substep(f"Creating config directory: {config_dir}")
    try:
        config_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(config_dir, 0o755)
        logger.success(f"Created {config_dir}")
    except Exception as e:
        logger.warning(f"Failed to create config directory: {e}")
    
    # =========================================================================
    # Phase 6: Create Configuration File
    # =========================================================================
    logger.step(6, 6, "Creating configuration file")
    
    if not DEFAULT_CONFIG_PATH.exists():
        try:
            from wasm.core.config import Config
            config = Config()
            
            # Update config with user choices
            ssl_email = config_choices.get("ssl_email", "")
            if ssl_email:
                config.set("ssl.email", ssl_email)
            
            webserver = config_choices.get("webserver_choice", "nginx")
            config.set("webserver", webserver)
            
            # Save enabled package managers to config
            pms = config_choices.get("package_managers", ["npm"])
            config.set("nodejs.package_managers", pms)
            
            config.save()
            logger.success(f"Created {DEFAULT_CONFIG_PATH}")
        except Exception as e:
            logger.warning(f"Could not create config file: {e}")
    else:
        logger.info(f"Config file already exists: {DEFAULT_CONFIG_PATH}")
    
    # =========================================================================
    # Final Summary
    # =========================================================================
    logger.blank()
    logger.header("Setup Complete!")
    logger.blank()
    
    # Re-check status
    final_summary = checker.get_setup_summary()
    
    logger.info("Final System Status:")
    if final_summary["webserver"]:
        logger.key_value("  Web Server", f"✓ {final_summary['webserver']}")
    
    if command_exists("node"):
        node_ver = checker.get_version("node")
        logger.key_value("  Node.js", f"✓ {node_ver}")
        
        installed_pms = []
        for pm in ["npm", "pnpm", "yarn", "bun"]:
            if command_exists(pm):
                installed_pms.append(pm)
        logger.key_value("  Package Managers", ", ".join(installed_pms))
    
    if command_exists("certbot"):
        logger.key_value("  SSL (Certbot)", "✓ Ready")
    
    if command_exists("git"):
        logger.key_value("  Git", "✓ Ready")
    
    logger.blank()
    logger.info("Next steps:")
    logger.info("  1. Deploy your first app: wasm create -d example.com -s <git-url> -t nextjs")
    logger.info("  2. Setup SSH for Git: wasm setup ssh --generate")
    logger.info("  3. Install shell completions: wasm setup completions")
    logger.info("  4. Run diagnostics: wasm setup doctor")
    logger.blank()
    
    return 0


def _interactive_setup_prompts(
    logger: Logger,
    summary: Dict,
    checker
) -> Optional[Dict]:
    """
    Interactive prompts for setup wizard.
    
    Args:
        logger: Logger instance.
        summary: System summary from checker.
        checker: DependencyChecker instance.
        
    Returns:
        Configuration choices dict or None if cancelled.
    """
    import inquirer
    from inquirer.themes import GreenPassion
    
    questions = []
    
    # Git installation
    if not command_exists("git"):
        questions.append(
            inquirer.Confirm(
                "install_git",
                message="Git is not installed. Install it now?",
                default=True,
            )
        )
    
    # Web server selection
    if summary["webserver"] is None:
        questions.append(
            inquirer.Confirm(
                "install_webserver",
                message="No web server found. Install one?",
                default=True,
            )
        )
        questions.append(
            inquirer.List(
                "webserver_choice",
                message="Select web server to install",
                choices=[
                    ("Nginx (recommended)", "nginx"),
                    ("Apache", "apache2"),
                ],
                default="nginx",
                ignore=lambda answers: not answers.get("install_webserver", True),
            )
        )
    else:
        # Store existing webserver choice
        questions.append(
            inquirer.List(
                "webserver_choice",
                message="Detected web server",
                choices=[(summary["webserver"], summary["webserver"])],
                default=summary["webserver"],
            )
        )
    
    # Node.js installation
    if not summary["nodejs"]["installed"]:
        questions.append(
            inquirer.Confirm(
                "install_nodejs",
                message="Node.js is not installed. Install it now? (Required for JS apps)",
                default=True,
            )
        )
    
    # Package managers selection
    pm_choices = [
        ("npm (default, comes with Node.js)", "npm"),
        ("pnpm (fast, efficient)", "pnpm"),
        ("yarn (reliable, feature-rich)", "yarn"),
        ("bun (ultra-fast runtime)", "bun"),
    ]
    
    # Pre-select installed ones
    default_pms = ["npm"]
    if summary["nodejs"]["installed"]:
        for pm, info in summary["nodejs"]["package_managers"].items():
            if info["installed"] and pm not in default_pms:
                default_pms.append(pm)
    
    questions.append(
        inquirer.Checkbox(
            "package_managers",
            message="Select package managers to install/enable (Space to toggle, Enter to confirm)",
            choices=pm_choices,
            default=default_pms,
        )
    )
    
    # Certbot installation
    if not command_exists("certbot"):
        questions.append(
            inquirer.Confirm(
                "install_certbot",
                message="Certbot (SSL) is not installed. Install it now?",
                default=True,
            )
        )
    
    # SSL email
    questions.append(
        inquirer.Text(
            "ssl_email",
            message="Email for SSL certificates (optional, press Enter to skip)",
            default="",
        )
    )
    
    try:
        answers = inquirer.prompt(questions, theme=GreenPassion())
        return answers
    except KeyboardInterrupt:
        return None


def _handle_permissions(args: Namespace) -> int:
    """Handle permissions check and fix."""
    logger = Logger(verbose=args.verbose)
    
    logger.header("WASM Permissions Check")
    logger.blank()
    
    issues = []
    
    # Check apps directory
    if DEFAULT_APPS_DIR.exists():
        if os.access(DEFAULT_APPS_DIR, os.W_OK):
            logger.success(f"Apps directory writable: {DEFAULT_APPS_DIR}")
        else:
            logger.warning(f"Apps directory not writable: {DEFAULT_APPS_DIR}")
            issues.append(("apps_dir", DEFAULT_APPS_DIR))
    else:
        logger.warning(f"Apps directory does not exist: {DEFAULT_APPS_DIR}")
        issues.append(("apps_dir_missing", DEFAULT_APPS_DIR))
    
    # Check log directory
    if DEFAULT_LOG_DIR.exists():
        if os.access(DEFAULT_LOG_DIR, os.W_OK):
            logger.success(f"Log directory writable: {DEFAULT_LOG_DIR}")
        else:
            logger.warning(f"Log directory not writable: {DEFAULT_LOG_DIR}")
            issues.append(("log_dir", DEFAULT_LOG_DIR))
    else:
        logger.warning(f"Log directory does not exist: {DEFAULT_LOG_DIR}")
        issues.append(("log_dir_missing", DEFAULT_LOG_DIR))
    
    # Check config
    config_dir = DEFAULT_CONFIG_PATH.parent
    if config_dir.exists():
        if os.access(config_dir, os.R_OK):
            logger.success(f"Config directory readable: {config_dir}")
        else:
            logger.warning(f"Config directory not readable: {config_dir}")
            issues.append(("config_dir", config_dir))
    
    # Check nginx/apache access
    nginx_available = Path("/etc/nginx/sites-available")
    if nginx_available.exists():
        if os.access(nginx_available, os.W_OK):
            logger.success(f"Nginx sites-available writable")
        else:
            logger.info(f"Nginx sites-available requires sudo")
    
    # Check systemd access
    systemd_dir = Path("/etc/systemd/system")
    if systemd_dir.exists():
        if os.access(systemd_dir, os.W_OK):
            logger.success(f"Systemd directory writable")
        else:
            logger.info(f"Systemd directory requires sudo")
    
    logger.blank()
    
    if issues:
        logger.warning("Some directories need to be created or have permissions fixed")
        logger.info("Run: sudo wasm setup init")
    else:
        logger.success("All permissions OK!")
        logger.info("Note: Operations that modify nginx/systemd still require sudo")
    
    return 0


def _handle_ssh(args: Namespace) -> int:
    """Handle SSH key setup and verification."""
    import os
    from wasm.validators.ssh import (
        ssh_key_exists,
        get_public_key,
        generate_ssh_key,
        test_ssh_connection,
        get_ssh_directory,
        get_all_ssh_keys,
    )
    
    logger = Logger(verbose=args.verbose)
    
    logger.header("WASM SSH Setup")
    logger.blank()
    
    # Check if SSH key exists
    key_exists, key_path = ssh_key_exists()
    ssh_dir = get_ssh_directory()
    
    # Show current SSH status
    logger.key_value("SSH Directory", str(ssh_dir))
    
    if key_exists:
        logger.key_value("SSH Key Found", str(key_path))
        all_keys = get_all_ssh_keys()
        if len(all_keys) > 1:
            logger.key_value("Additional Keys", ", ".join(str(k.name) for k in all_keys[1:]))
    else:
        logger.key_value("SSH Key Found", "None")
    
    logger.blank()
    
    # If --generate flag is set or no key exists, offer to generate
    generate_flag = getattr(args, "generate", False)
    key_type = getattr(args, "key_type", "ed25519")
    
    if not key_exists:
        if generate_flag:
            logger.step(1, 3, "Generating SSH key")
            hostname = os.uname().nodename
            success, new_key_path, msg = generate_ssh_key(
                key_type=key_type,
                comment=f"wasm@{hostname}",
            )
            
            if success and new_key_path:
                logger.success(f"SSH key generated: {new_key_path}")
                key_path = new_key_path
                key_exists = True
            else:
                logger.error(msg)
                return 1
        else:
            logger.warning("No SSH key found on this system")
            logger.blank()
            logger.info("To generate a new SSH key, run:")
            logger.info("  wasm setup ssh --generate")
            logger.blank()
            logger.info("Or manually with:")
            logger.info(f"  ssh-keygen -t {key_type}")
            return 1
    
    # Show public key if --show flag or after generation
    show_flag = getattr(args, "show", False)
    public_key = get_public_key(key_path)
    
    if show_flag or (generate_flag and key_exists):
        if public_key:
            logger.info("Your public SSH key:")
            logger.blank()
            print("─" * 70)
            print(public_key)
            print("─" * 70)
            logger.blank()
            
            # Provide instructions based on the host
            logger.info("Add this key to your Git provider:")
            logger.blank()
            logger.info("  GitHub:    https://github.com/settings/keys")
            logger.info("  GitLab:    https://gitlab.com/-/user_settings/ssh_keys")
            logger.info("  Bitbucket: https://bitbucket.org/account/settings/ssh-keys/")
            logger.blank()
        else:
            logger.warning("Could not read public key")
    
    # Test connection if --test flag is set
    test_host = getattr(args, "test_host", None)
    if test_host:
        logger.info(f"Testing SSH connection to {test_host}...")
        success, msg = test_ssh_connection(test_host)
        
        if success:
            logger.success(f"SSH connection to {test_host} successful!")
        else:
            logger.error(f"SSH connection failed: {msg}")
            logger.blank()
            
            if public_key:
                logger.info("Make sure your public key is added to your Git provider.")
                logger.info("Your public key:")
                logger.blank()
                print("─" * 70)
                print(public_key)
                print("─" * 70)
            
            return 1
    
    # Final summary
    if key_exists and not show_flag and not test_host:
        logger.success("SSH key is configured")
        logger.blank()
        logger.info("Useful commands:")
        logger.info("  wasm setup ssh --show       # Show your public key")
        logger.info("  wasm setup ssh --test github.com  # Test connection")
    
    return 0


def _handle_doctor(args: Namespace) -> int:
    """
    Handle doctor command - comprehensive system diagnostics.
    
    Checks all dependencies, configurations, and provides actionable
    recommendations to fix any issues.
    """
    logger = Logger(verbose=args.verbose)
    
    logger.header("WASM System Diagnostics")
    logger.blank()
    
    from wasm.core.dependencies import DependencyChecker
    checker = DependencyChecker(verbose=args.verbose)
    
    issues_found = 0
    warnings_found = 0
    
    # =========================================================================
    # Section 1: Core Dependencies
    # =========================================================================
    logger.info("═══ Core Dependencies ═══")
    logger.blank()
    
    core_deps = [
        ("git", "Git version control", "sudo apt install git"),
        ("curl", "Data transfer tool", "sudo apt install curl"),
    ]
    
    for cmd, desc, fix in core_deps:
        if command_exists(cmd):
            version = checker.get_version(cmd)
            logger.success(f"{cmd}: {version or 'OK'}")
        else:
            logger.error(f"{cmd}: NOT INSTALLED")
            logger.info(f"  Fix: {fix}")
            issues_found += 1
    
    logger.blank()
    
    # =========================================================================
    # Section 2: Web Server
    # =========================================================================
    logger.info("═══ Web Server ═══")
    logger.blank()
    
    nginx_installed = command_exists("nginx")
    apache_installed = command_exists("apache2")
    
    if nginx_installed:
        version = checker.get_version("nginx", "-v")
        logger.success(f"nginx: {version}")
        
        # Check if running
        result = run_command(["systemctl", "is-active", "nginx"])
        if result.stdout.strip() == "active":
            logger.success("  nginx service: running")
        else:
            logger.warning("  nginx service: not running")
            logger.info("  Fix: sudo systemctl start nginx")
            warnings_found += 1
    elif apache_installed:
        logger.success("apache2: installed")
        
        result = run_command(["systemctl", "is-active", "apache2"])
        if result.stdout.strip() == "active":
            logger.success("  apache2 service: running")
        else:
            logger.warning("  apache2 service: not running")
            warnings_found += 1
    else:
        logger.error("Web server: NOT INSTALLED")
        logger.info("  Fix: sudo apt install nginx")
        issues_found += 1
    
    logger.blank()
    
    # =========================================================================
    # Section 3: Node.js Environment
    # =========================================================================
    logger.info("═══ Node.js Environment ═══")
    logger.blank()
    
    if command_exists("node"):
        version = checker.get_version("node")
        logger.success(f"node: {version}")
        
        # Check npm
        if command_exists("npm"):
            npm_version = checker.get_version("npm")
            logger.success(f"npm: {npm_version}")
        else:
            logger.error("npm: NOT INSTALLED (should come with Node.js)")
            issues_found += 1
        
        # Check other package managers
        for pm in ["pnpm", "yarn", "bun"]:
            if command_exists(pm):
                pm_version = checker.get_version(pm)
                logger.success(f"{pm}: {pm_version}")
            else:
                logger.info(f"{pm}: not installed (optional)")
    else:
        logger.error("node: NOT INSTALLED")
        logger.info("  Fix: curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash - && sudo apt install -y nodejs")
        issues_found += 1
    
    logger.blank()
    
    # =========================================================================
    # Section 4: Python Environment
    # =========================================================================
    logger.info("═══ Python Environment ═══")
    logger.blank()
    
    if command_exists("python3"):
        version = checker.get_version("python3")
        logger.success(f"python3: {version}")
        
        if command_exists("pip3"):
            pip_version = checker.get_version("pip3")
            logger.success(f"pip3: {pip_version}")
        else:
            logger.warning("pip3: not installed")
            logger.info("  Fix: sudo apt install python3-pip")
            warnings_found += 1
    else:
        logger.warning("python3: not installed (needed for Python apps)")
        warnings_found += 1
    
    logger.blank()
    
    # =========================================================================
    # Section 5: SSL/TLS
    # =========================================================================
    logger.info("═══ SSL/TLS (Certbot) ═══")
    logger.blank()
    
    if command_exists("certbot"):
        version = checker.get_version("certbot")
        logger.success(f"certbot: {version}")
    else:
        logger.warning("certbot: NOT INSTALLED")
        logger.info("  Fix: sudo apt install certbot python3-certbot-nginx")
        warnings_found += 1
    
    logger.blank()
    
    # =========================================================================
    # Section 6: WASM Configuration
    # =========================================================================
    logger.info("═══ WASM Configuration ═══")
    logger.blank()
    
    # Check directories
    if DEFAULT_APPS_DIR.exists():
        logger.success(f"Apps directory: {DEFAULT_APPS_DIR}")
    else:
        logger.error(f"Apps directory: {DEFAULT_APPS_DIR} (NOT FOUND)")
        logger.info("  Fix: sudo wasm setup init")
        issues_found += 1
    
    if DEFAULT_LOG_DIR.exists():
        logger.success(f"Log directory: {DEFAULT_LOG_DIR}")
    else:
        logger.error(f"Log directory: {DEFAULT_LOG_DIR} (NOT FOUND)")
        issues_found += 1
    
    if DEFAULT_CONFIG_PATH.exists():
        logger.success(f"Config file: {DEFAULT_CONFIG_PATH}")
    else:
        logger.warning(f"Config file: {DEFAULT_CONFIG_PATH} (NOT FOUND)")
        logger.info("  Fix: sudo wasm setup init")
        warnings_found += 1
    
    logger.blank()
    
    # =========================================================================
    # Section 7: SSH Configuration
    # =========================================================================
    logger.info("═══ SSH Configuration ═══")
    logger.blank()
    
    from wasm.validators.ssh import ssh_key_exists, test_ssh_connection
    
    key_exists, key_path = ssh_key_exists()
    if key_exists:
        logger.success(f"SSH key: {key_path}")
        
        # Test GitHub connection
        success, msg = test_ssh_connection("github.com")
        if success:
            logger.success("GitHub SSH: connected")
        else:
            logger.warning("GitHub SSH: not configured")
            logger.info("  Add your SSH key to GitHub: https://github.com/settings/keys")
    else:
        logger.warning("SSH key: NOT FOUND")
        logger.info("  Fix: wasm setup ssh --generate")
        warnings_found += 1
    
    logger.blank()
    
    # =========================================================================
    # Summary
    # =========================================================================
    logger.info("═══ Summary ═══")
    logger.blank()
    
    if issues_found == 0 and warnings_found == 0:
        logger.success("✓ All checks passed! Your system is ready for deployments.")
    elif issues_found == 0:
        logger.warning(f"⚠ {warnings_found} warning(s) found. System can function but some features may be limited.")
    else:
        logger.error(f"✗ {issues_found} issue(s) and {warnings_found} warning(s) found.")
        logger.info("Run 'sudo wasm setup init' to fix most issues automatically.")
    
    logger.blank()
    
    return 0 if issues_found == 0 else 1
