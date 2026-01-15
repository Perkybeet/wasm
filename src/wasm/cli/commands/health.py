# Copyright (c) 2024-2025 Yago López Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
Health check command for WASM.

Provides system-wide health diagnostics.
"""

import shutil
from argparse import Namespace
from datetime import datetime
from pathlib import Path

from wasm.core.config import Config
from wasm.core.logger import Logger


def _print_status(logger: Logger, key: str, value: str, status: str) -> None:
    """Print a key-value pair with status indicator."""
    if status == "ok":
        indicator = "\033[32m[OK]\033[0m"
    elif status == "warning":
        indicator = "\033[33m[!]\033[0m"
    elif status == "error":
        indicator = "\033[31m[X]\033[0m"
    else:  # info
        indicator = "\033[34m[i]\033[0m"

    print(f"  {indicator} {key}: {value}")


def handle_health(args: Namespace) -> int:
    """
    Handle the health check command.

    Performs system diagnostics and shows overall health status.
    """
    logger = Logger(verbose=args.verbose)
    config = Config()

    logger.header("System Health Check")
    logger.blank()

    issues = []
    warnings = []

    # 1. Check disk space
    logger.info("Checking disk space...")
    try:
        apps_dir = config.apps_directory
        if apps_dir.exists():
            stat = shutil.disk_usage(str(apps_dir))
            free_gb = stat.free / (1024 ** 3)
            total_gb = stat.total / (1024 ** 3)
            used_percent = ((stat.total - stat.free) / stat.total) * 100

            if free_gb < 1.0:
                issues.append(f"Low disk space: {free_gb:.1f}GB free")
                _print_status(logger, "Disk Space", f"{free_gb:.1f}GB free / {total_gb:.1f}GB total ({used_percent:.0f}% used)", "error")
            elif free_gb < 5.0:
                warnings.append(f"Disk space is getting low: {free_gb:.1f}GB free")
                _print_status(logger, "Disk Space", f"{free_gb:.1f}GB free / {total_gb:.1f}GB total ({used_percent:.0f}% used)", "warning")
            else:
                _print_status(logger, "Disk Space", f"{free_gb:.1f}GB free / {total_gb:.1f}GB total ({used_percent:.0f}% used)", "ok")
        else:
            _print_status(logger, "Disk Space", "Apps directory not found", "warning")
    except Exception as e:
        warnings.append(f"Could not check disk space: {e}")

    # 2. Check web servers
    logger.blank()
    logger.info("Checking web servers...")

    from wasm.managers.nginx_manager import NginxManager
    from wasm.managers.apache_manager import ApacheManager

    nginx = NginxManager(verbose=args.verbose)
    apache = ApacheManager(verbose=args.verbose)

    nginx_installed = nginx.is_installed()
    apache_installed = apache.is_installed()

    if nginx_installed:
        nginx_status = nginx.get_status()
        if nginx_status.get("active"):
            _print_status(logger, "Nginx", "Running", "ok")
        else:
            issues.append("Nginx is installed but not running")
            _print_status(logger, "Nginx", "Stopped", "error")
    else:
        _print_status(logger, "Nginx", "Not installed", "info")

    if apache_installed:
        apache_status = apache.get_status()
        if apache_status.get("active"):
            _print_status(logger, "Apache", "Running", "ok")
        else:
            warnings.append("Apache is installed but not running")
            _print_status(logger, "Apache", "Stopped", "warning")
    else:
        _print_status(logger, "Apache", "Not installed", "info")

    if not nginx_installed and not apache_installed:
        issues.append("No web server installed")

    # 3. Check deployed applications
    logger.blank()
    logger.info("Checking deployed applications...")

    from wasm.core.store import get_store
    from wasm.managers.service_manager import ServiceManager

    store = get_store()
    service_manager = ServiceManager(verbose=args.verbose)

    apps = store.list_apps()
    apps_running = 0
    apps_stopped = 0
    apps_failed = 0

    for app in apps:
        try:
            status = service_manager.status(app.domain.replace(".", "-"))
            if status.get("active"):
                apps_running += 1
            else:
                apps_stopped += 1
                warnings.append(f"App '{app.domain}' is not running")
        except Exception:
            apps_failed += 1

    total_apps = len(apps)
    if total_apps > 0:
        if apps_stopped > 0 or apps_failed > 0:
            _print_status(logger, "Applications", f"{apps_running}/{total_apps} running, {apps_stopped} stopped", "warning")
        else:
            _print_status(logger, "Applications", f"{apps_running}/{total_apps} running", "ok")
    else:
        _print_status(logger, "Applications", "No applications deployed", "info")

    # 4. Check SSL certificates
    logger.blank()
    logger.info("Checking SSL certificates...")

    from wasm.managers.cert_manager import CertManager
    cert_manager = CertManager(verbose=args.verbose)

    try:
        certs = cert_manager.list_certificates()
        expiring_soon = []

        for cert in certs:
            if cert.get("expires"):
                try:
                    expires = datetime.fromisoformat(cert["expires"].replace("Z", "+00:00"))
                    days_left = (expires - datetime.now(expires.tzinfo)).days
                    if days_left < 7:
                        issues.append(f"Certificate for {cert['domain']} expires in {days_left} days")
                        expiring_soon.append(cert["domain"])
                    elif days_left < 30:
                        warnings.append(f"Certificate for {cert['domain']} expires in {days_left} days")
                        expiring_soon.append(cert["domain"])
                except Exception:
                    pass

        if expiring_soon:
            _print_status(logger, "SSL Certificates", f"{len(certs)} total, {len(expiring_soon)} expiring soon", "warning")
        elif certs:
            _print_status(logger, "SSL Certificates", f"{len(certs)} total, all valid", "ok")
        else:
            _print_status(logger, "SSL Certificates", "None configured", "info")
    except Exception as e:
        _print_status(logger, "SSL Certificates", f"Could not check: {e}", "warning")

    # 5. Check system resources
    logger.blank()
    logger.info("Checking system resources...")

    try:
        # Memory check using /proc/meminfo
        with open("/proc/meminfo") as f:
            meminfo = {}
            for line in f:
                parts = line.split(":")
                if len(parts) == 2:
                    key = parts[0].strip()
                    value = parts[1].strip().split()[0]
                    meminfo[key] = int(value)

        total_mem = meminfo.get("MemTotal", 0) / 1024 / 1024  # GB
        free_mem = (meminfo.get("MemAvailable", 0) or meminfo.get("MemFree", 0)) / 1024 / 1024  # GB
        used_percent = ((total_mem - free_mem) / total_mem) * 100 if total_mem > 0 else 0

        if used_percent > 90:
            issues.append(f"High memory usage: {used_percent:.0f}%")
            _print_status(logger, "Memory", f"{free_mem:.1f}GB free / {total_mem:.1f}GB total ({used_percent:.0f}% used)", "error")
        elif used_percent > 75:
            warnings.append(f"Memory usage is high: {used_percent:.0f}%")
            _print_status(logger, "Memory", f"{free_mem:.1f}GB free / {total_mem:.1f}GB total ({used_percent:.0f}% used)", "warning")
        else:
            _print_status(logger, "Memory", f"{free_mem:.1f}GB free / {total_mem:.1f}GB total ({used_percent:.0f}% used)", "ok")
    except Exception as e:
        _print_status(logger, "Memory", f"Could not check: {e}", "warning")

    # Summary
    logger.blank()
    logger.blank()

    if issues:
        logger.error(f"Health check found {len(issues)} issue(s):")
        for issue in issues:
            logger.error(f"  - {issue}")
        logger.blank()

    if warnings:
        logger.warning(f"Health check found {len(warnings)} warning(s):")
        for warning in warnings:
            logger.warning(f"  - {warning}")
        logger.blank()

    if not issues and not warnings:
        logger.success("All systems healthy!")
        return 0
    elif issues:
        logger.error("System has issues that need attention.")
        return 1
    else:
        logger.warning("System is healthy with minor warnings.")
        return 0
