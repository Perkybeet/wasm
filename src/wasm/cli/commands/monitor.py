"""
Monitor command handlers for WASM.

Commands for managing the AI-powered process monitor.
"""

import sys
from argparse import Namespace

from wasm.core.logger import Logger
from wasm.core.exceptions import WASMError, MonitorError, EmailError


def handle_monitor(args: Namespace) -> int:
    """
    Handle monitor commands.
    
    Args:
        args: Parsed arguments.
        
    Returns:
        Exit code.
    """
    action = args.action
    
    handlers = {
        "status": _handle_status,
        "info": _handle_status,
        "scan": _handle_scan,
        "run": _handle_run,
        "install": _handle_install,
        "enable": _handle_enable,
        "disable": _handle_disable,
        "uninstall": _handle_uninstall,
        "test-email": _handle_test_email,
        "config": _handle_config,
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


def _handle_status(args: Namespace) -> int:
    """Handle monitor status command."""
    from wasm.monitor.process_monitor import ProcessMonitor
    
    logger = Logger(verbose=args.verbose)
    monitor = ProcessMonitor(verbose=args.verbose)
    
    logger.header("WASM Process Monitor Status")
    
    status = monitor.get_service_status()
    
    # Service status
    if status["installed"]:
        logger.key_value("Installed", "Yes")
        logger.key_value("Enabled", "Yes" if status["enabled"] else "No")
        
        if status["active"]:
            logger.key_value("Status", "🟢 Running")
            if status["pid"]:
                logger.key_value("PID", str(status["pid"]))
            if status["uptime"]:
                logger.key_value("Started", status["uptime"])
        else:
            logger.key_value("Status", "🔴 Stopped")
    else:
        logger.key_value("Installed", "No")
        logger.info("\nRun 'wasm monitor install' to install the monitor service")
    
    # Configuration summary
    logger.info("")
    logger.key_value("Scan Interval", f"{monitor.config.scan_interval}s")
    logger.key_value("AI Analysis", "Enabled" if monitor.config.use_ai else "Disabled")
    logger.key_value("Auto-Terminate", "Enabled" if monitor.config.auto_terminate else "Disabled")
    logger.key_value("Dry Run", "Yes" if monitor.config.dry_run else "No")
    
    return 0


def _handle_scan(args: Namespace) -> int:
    """Handle monitor scan command (single scan)."""
    from wasm.monitor.process_monitor import ProcessMonitor, MonitorConfig
    
    logger = Logger(verbose=args.verbose)
    
    # Check for options
    dry_run = getattr(args, "dry_run", False)
    force_ai = getattr(args, "force_ai", False)
    scan_all = getattr(args, "all", False)
    
    config = None
    from wasm.core.config import Config
    global_config = Config()
    
    config = MonitorConfig(
        enabled=True,
        scan_interval=0,
        auto_terminate=global_config.get("monitor.auto_terminate", True),
        terminate_malicious_only=global_config.get("monitor.terminate_malicious_only", True),
        use_ai=global_config.get("monitor.use_ai", True),
        dry_run=dry_run,
    )
    
    if dry_run:
        logger.warning("Running in DRY RUN mode - no processes will be terminated")
    
    if force_ai:
        logger.info("Force AI analysis enabled - will analyze all detected processes")
    
    if scan_all:
        logger.warning("Scanning ALL processes with AI - this may be slow and expensive")
    
    monitor = ProcessMonitor(config=config, verbose=args.verbose)
    
    logger.info("Running security scan...")
    logger.info("")
    
    # Use force_ai and scan_all options
    reports = monitor.scan_once(force_ai=force_ai, analyze_all=scan_all)
    
    if not reports:
        logger.success("System scan complete - no threats detected")
        return 0
    
    # Display results
    logger.header(f"Detected {len(reports)} Threats")
    
    for report in reports:
        if report.threat_level == "malicious":
            logger.error(f"🚨 MALICIOUS: {report.process_name} (PID: {report.pid})")
        else:
            logger.warning(f"⚠️  SUSPICIOUS: {report.process_name} (PID: {report.pid})")
        
        logger.info(f"   User: {report.user}")
        logger.info(f"   CPU: {report.cpu_percent:.1f}% | Memory: {report.memory_percent:.1f}%")
        logger.info(f"   Reason: {report.reason}")
        
        if report.action_taken:
            logger.info(f"   Action: {report.action_taken}")
        
        logger.info("")
    
    return 0


def _handle_run(args: Namespace) -> int:
    """Handle monitor run command (continuous mode)."""
    from wasm.monitor.process_monitor import ProcessMonitor
    
    logger = Logger(verbose=args.verbose)
    monitor = ProcessMonitor(verbose=args.verbose)
    
    logger.info("Starting WASM Process Monitor...")
    logger.info(f"Scan interval: {monitor.config.scan_interval} seconds")
    logger.info("Press Ctrl+C to stop")
    logger.info("")
    
    try:
        monitor.run()
    except KeyboardInterrupt:
        logger.info("\nShutting down...")
        monitor.stop()
    
    return 0


def _handle_install(args: Namespace) -> int:
    """Handle monitor install command."""
    from wasm.monitor.process_monitor import ProcessMonitor
    
    logger = Logger(verbose=args.verbose)
    monitor = ProcessMonitor(verbose=args.verbose)
    
    logger.info("Installing WASM Process Monitor service...")
    
    monitor.install_service()
    
    logger.info("")
    logger.info("To enable the service, run:")
    logger.info("  sudo wasm monitor enable")
    
    return 0


def _handle_enable(args: Namespace) -> int:
    """Handle monitor enable command - installs service and dependencies if needed."""
    from wasm.core.utils import run_command, command_exists
    
    logger = Logger(verbose=args.verbose)
    
    # Step 1: Check and install dependencies
    logger.step(1, 4, "Checking dependencies")
    
    missing_deps = []
    try:
        import psutil
        logger.debug("psutil: OK")
    except ImportError:
        missing_deps.append("psutil")
        logger.debug("psutil: MISSING")
    
    try:
        import httpx
        logger.debug("httpx: OK")
    except ImportError:
        try:
            import requests
            logger.debug("requests: OK (alternative to httpx)")
        except ImportError:
            missing_deps.append("httpx")
            logger.debug("httpx/requests: MISSING")
    
    if missing_deps:
        logger.info(f"Installing missing dependencies: {', '.join(missing_deps)}")
        
        # Try pip3 first, then pip
        pip_cmd = "pip3" if command_exists("pip3") else "pip"
        
        result = run_command([pip_cmd, "install"] + missing_deps)
        if not result.success:
            logger.error(f"Failed to install dependencies: {result.stderr}")
            logger.info("Try manually: pip install psutil httpx")
            return 1
        
        logger.success("Dependencies installed")
    else:
        logger.success("All dependencies available")
    
    # Now import the monitor (after dependencies are installed)
    from wasm.monitor.process_monitor import ProcessMonitor
    monitor = ProcessMonitor(verbose=args.verbose)
    
    # Step 2: Install service if not installed
    logger.step(2, 4, "Checking service installation")
    
    status = monitor.get_service_status()
    if not status["installed"]:
        logger.info("Installing monitor service...")
        monitor.install_service()
        logger.success("Service installed")
    else:
        logger.success("Service already installed")
    
    # Step 3: Enable and start service
    logger.step(3, 4, "Enabling monitor service")
    monitor.enable_service()
    
    # Step 4: Verify
    logger.step(4, 4, "Verifying service status")
    status = monitor.get_service_status()
    
    if status["active"]:
        logger.success("WASM Process Monitor is now running!")
        logger.info("")
        logger.info("The monitor will scan for threats every hour.")
        logger.info("View logs with: sudo journalctl -u wasm-monitor -f")
    else:
        logger.warning("Service enabled but may not be running yet")
    
    return 0


def _handle_disable(args: Namespace) -> int:
    """Handle monitor disable command."""
    from wasm.monitor.process_monitor import ProcessMonitor
    
    logger = Logger(verbose=args.verbose)
    monitor = ProcessMonitor(verbose=args.verbose)
    
    status = monitor.get_service_status()
    if not status["installed"]:
        logger.error("Monitor service not installed")
        return 1
    
    logger.info("Disabling WASM Process Monitor service...")
    monitor.disable_service()
    
    return 0


def _handle_uninstall(args: Namespace) -> int:
    """Handle monitor uninstall command."""
    from wasm.monitor.process_monitor import ProcessMonitor
    
    logger = Logger(verbose=args.verbose)
    monitor = ProcessMonitor(verbose=args.verbose)
    
    status = monitor.get_service_status()
    if not status["installed"]:
        logger.warning("Monitor service is not installed")
        return 0
    
    logger.info("Uninstalling WASM Process Monitor service...")
    monitor.uninstall_service()
    
    return 0


def _handle_test_email(args: Namespace) -> int:
    """Handle monitor test-email command."""
    from wasm.monitor.email_notifier import EmailNotifier
    
    logger = Logger(verbose=args.verbose)
    notifier = EmailNotifier(verbose=args.verbose)
    
    if not notifier.recipients:
        logger.error("No email recipients configured")
        logger.info("Configure 'monitor.email_recipients' in /etc/wasm/config.yaml")
        return 1
    
    logger.info(f"Sending test email to: {', '.join(notifier.recipients)}")
    
    try:
        notifier.send_test_email()
        logger.success("Test email sent successfully!")
        return 0
    except EmailError as e:
        logger.error(f"Failed to send test email: {e}")
        return 1


def _handle_config(args: Namespace) -> int:
    """Handle monitor config command - show configuration."""
    from wasm.core.config import Config
    
    logger = Logger(verbose=args.verbose)
    config = Config()
    
    logger.header("Monitor Configuration")
    
    # General settings
    logger.info("")
    logger.key_value("Enabled", str(config.get("monitor.enabled", False)))
    logger.key_value("Scan Interval", f"{config.get('monitor.scan_interval', 3600)}s")
    logger.key_value("CPU Threshold", f"{config.get('monitor.cpu_threshold', 80.0)}%")
    logger.key_value("Memory Threshold", f"{config.get('monitor.memory_threshold', 80.0)}%")
    
    # Actions
    logger.info("")
    logger.info("Actions:")
    logger.key_value("  Auto-Terminate", str(config.get("monitor.auto_terminate", True)))
    logger.key_value("  Malicious Only", str(config.get("monitor.terminate_malicious_only", True)))
    logger.key_value("  Dry Run", str(config.get("monitor.dry_run", False)))
    
    # AI
    logger.info("")
    logger.info("AI Analysis:")
    logger.key_value("  Use AI", str(config.get("monitor.use_ai", True)))
    logger.key_value("  Model", config.get("monitor.openai.model", "gpt-4o-mini"))
    api_key = config.get("monitor.openai.api_key", "")
    logger.key_value("  API Key", "Configured" if api_key else "Not configured")
    
    # Email
    logger.info("")
    logger.info("Email Notifications:")
    smtp_host = config.get("monitor.smtp.host", "")
    logger.key_value("  SMTP Host", smtp_host if smtp_host else "Not configured")
    logger.key_value("  SMTP Port", str(config.get("monitor.smtp.port", 465)))
    recipients = config.get("monitor.email_recipients", [])
    if recipients:
        logger.key_value("  Recipients", ", ".join(recipients))
    else:
        logger.key_value("  Recipients", "Not configured")
    
    return 0
