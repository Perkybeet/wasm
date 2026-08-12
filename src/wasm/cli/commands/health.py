# Copyright (c) 2024-2025 Yago López Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
Health check command for WASM.

Provides system-wide health diagnostics.

This is the command an operator runs when something looks wrong, so it is the
last place that should quietly report nothing. Two defects made it do exactly
that: it asked ServiceManager for a ``status`` method that does not exist, so
every application was counted as failed, and it looked for an ``expires`` key
in certificate data that carries ``expiry``, so no certificate ever appeared to
be close to renewal.

The check itself lives in :func:`run_health_check`. The Click command and the
argparse handler that :mod:`wasm.cli.parser` still calls are both two lines
around it, so the two entry points cannot drift while the migration finishes.
"""

from __future__ import annotations

import shutil
from argparse import Namespace
from datetime import datetime

import click

from wasm.cli.app import Context, pass_context
from wasm.core.app_state import RUNNING, STATIC, resolve_states
from wasm.core.config import Config
from wasm.core.exceptions import WASMError
from wasm.core.logger import Logger
from wasm.core.store import get_store
from wasm.managers.apache_manager import ApacheManager
from wasm.managers.cert_manager import CertificateInfo, CertManager
from wasm.managers.nginx_manager import NginxManager
from wasm.managers.service_manager import ServiceManager

#: A certificate this close to expiry is an incident, not a reminder.
_CERT_CRITICAL_DAYS = 7

#: A certificate this close to expiry deserves a warning.
_CERT_WARNING_DAYS = 30


def _print_status(logger: Logger, key: str, value: str, status: str) -> None:
    """
    Print a key-value pair with status indicator.

    Args:
        logger: Logger of the current command. Writing through it is what makes
            ``wasm --no-color health`` colourless; the escape codes used to be
            written to stdout directly, so the flag did nothing here.
        key: Name of the checked item.
        value: Human readable result.
        status: One of "ok", "warning", "error" or "info".
    """
    logger.check(key, value, status)


def _days_until(expiry: str) -> int | None:
    """
    Days left before a certificate expiry date.

    Args:
        expiry: Expiry date as certbot reports it, ``YYYY-MM-DD``.

    Returns:
        Whole days remaining, or None when the date cannot be parsed.
    """
    try:
        expires = datetime.fromisoformat(expiry.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (expires - datetime.now(expires.tzinfo)).days


def _certificate_label(cert: CertificateInfo) -> str:
    """
    Name a certificate for an operator-facing message.

    Args:
        cert: Certificate entry from the certificate manager.

    Returns:
        The lineage name, falling back to the first covered domain.
    """
    name = cert.get("name")
    if name:
        return name
    domains = cert.get("domains") or []
    return domains[0] if domains else "unknown"


def _read_meminfo() -> dict[str, int]:
    """
    Read ``/proc/meminfo`` into its numeric fields.

    Returns:
        Field name to value in kilobytes, for every line that carries a number.

    Raises:
        OSError: When /proc/meminfo cannot be read.
        ValueError: When a value is not a number.
    """
    meminfo: dict[str, int] = {}
    with open("/proc/meminfo") as handle:
        for line in handle:
            key, separator, value = line.partition(":")
            if separator and value.strip():
                meminfo[key.strip()] = int(value.split()[0])
    return meminfo


def run_health_check(verbose: bool = False) -> int:
    """
    Inspect the server and report what is wrong with it.

    Checks disk space, the web servers, every deployed application, the
    certificates close to expiry and memory pressure.

    Args:
        verbose: Print the detail of each step.

    Returns:
        1 when the check found issues, 0 otherwise.
    """
    logger = Logger(verbose=verbose)
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
            free_gb = stat.free / (1024**3)
            total_gb = stat.total / (1024**3)
            used_percent = ((stat.total - stat.free) / stat.total) * 100

            if free_gb < 1.0:
                issues.append(f"Low disk space: {free_gb:.1f}GB free")
                _print_status(
                    logger,
                    "Disk Space",
                    f"{free_gb:.1f}GB free / {total_gb:.1f}GB total ({used_percent:.0f}% used)",
                    "error",
                )
            elif free_gb < 5.0:
                warnings.append(f"Disk space is getting low: {free_gb:.1f}GB free")
                _print_status(
                    logger,
                    "Disk Space",
                    f"{free_gb:.1f}GB free / {total_gb:.1f}GB total ({used_percent:.0f}% used)",
                    "warning",
                )
            else:
                _print_status(
                    logger,
                    "Disk Space",
                    f"{free_gb:.1f}GB free / {total_gb:.1f}GB total ({used_percent:.0f}% used)",
                    "ok",
                )
        else:
            _print_status(logger, "Disk Space", "Apps directory not found", "warning")
    except OSError as e:
        warnings.append(f"Could not check disk space: {e}")

    # 2. Check web servers
    logger.blank()
    logger.info("Checking web servers...")

    nginx = NginxManager(verbose=verbose)
    apache = ApacheManager(verbose=verbose)

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

    store = get_store()
    service_manager = ServiceManager(verbose=verbose)

    apps = store.list_apps()

    # The same resolver `wasm list` uses. When these two commands each decided
    # for themselves what "running" meant, list reported fifteen applications
    # running while this reported seven stopped, and five of the seven were
    # static sites that have no service to run in the first place.
    states = resolve_states(apps, service_manager)

    apps_running = sum(1 for s in states.values() if s.label == RUNNING)
    apps_static = sum(1 for s in states.values() if s.label == STATIC)
    unhealthy = [(domain, s) for domain, s in states.items() if not s.healthy]

    for domain, current in unhealthy:
        warnings.append(f"App '{domain}' - {current.detail or current.label.lower()}")

    total_apps = len(apps)
    served = apps_running + apps_static
    if total_apps > 0:
        summary = f"{served}/{total_apps} serving"
        if apps_static:
            summary += f" ({apps_static} static)"
        if unhealthy:
            _print_status(
                logger,
                "Applications",
                f"{summary}, {len(unhealthy)} need attention",
                "warning",
            )
        else:
            _print_status(logger, "Applications", summary, "ok")
    else:
        _print_status(logger, "Applications", "No applications deployed", "info")

    # 4. Check SSL certificates
    logger.blank()
    logger.info("Checking SSL certificates...")

    cert_manager = CertManager(verbose=verbose)

    try:
        certs = cert_manager.list_certificates()
        expiring_soon = []

        for cert in certs:
            expiry = cert.get("expiry")
            if not expiry:
                continue

            days_left = _days_until(expiry)
            if days_left is None:
                warnings.append(
                    f"Certificate for {_certificate_label(cert)} has an unreadable expiry date"
                )
                continue

            label = _certificate_label(cert)
            if days_left < _CERT_CRITICAL_DAYS:
                issues.append(f"Certificate for {label} expires in {days_left} days")
                expiring_soon.append(label)
            elif days_left < _CERT_WARNING_DAYS:
                warnings.append(f"Certificate for {label} expires in {days_left} days")
                expiring_soon.append(label)

        if expiring_soon:
            _print_status(
                logger,
                "SSL Certificates",
                f"{len(certs)} total, {len(expiring_soon)} expiring soon",
                "warning",
            )
        elif certs:
            _print_status(logger, "SSL Certificates", f"{len(certs)} total, all valid", "ok")
        else:
            _print_status(logger, "SSL Certificates", "None configured", "info")
    except WASMError as e:
        _print_status(logger, "SSL Certificates", f"Could not check: {e}", "warning")

    # 5. Check system resources
    logger.blank()
    logger.info("Checking system resources...")

    try:
        meminfo = _read_meminfo()

        total_mem = meminfo.get("MemTotal", 0) / 1024 / 1024  # GB
        free_mem = (meminfo.get("MemAvailable", 0) or meminfo.get("MemFree", 0)) / 1024 / 1024  # GB
        used_percent = ((total_mem - free_mem) / total_mem) * 100 if total_mem > 0 else 0

        if used_percent > 90:
            issues.append(f"High memory usage: {used_percent:.0f}%")
            _print_status(
                logger,
                "Memory",
                f"{free_mem:.1f}GB free / {total_mem:.1f}GB total ({used_percent:.0f}% used)",
                "error",
            )
        elif used_percent > 75:
            warnings.append(f"Memory usage is high: {used_percent:.0f}%")
            _print_status(
                logger,
                "Memory",
                f"{free_mem:.1f}GB free / {total_mem:.1f}GB total ({used_percent:.0f}% used)",
                "warning",
            )
        else:
            _print_status(
                logger,
                "Memory",
                f"{free_mem:.1f}GB free / {total_mem:.1f}GB total ({used_percent:.0f}% used)",
                "ok",
            )
    except (OSError, ValueError, ZeroDivisionError) as e:
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


@click.command("health")
@pass_context
def cli(ctx: Context) -> int:
    """
    Check the server and report anything that needs attention.

    Looks at free disk space, the web server, every deployed application,
    certificates close to expiry and memory pressure. It only reads.
    """
    return run_health_check(verbose=ctx.verbose)


def handle_health(args: Namespace) -> int:
    """
    Handle the health check command.

    Kept while :mod:`wasm.cli.parser` still routes through argparse; it shares
    :func:`run_health_check` with the Click command rather than repeating it.

    Args:
        args: Parsed arguments; only ``verbose`` is read.

    Returns:
        1 when the check found issues, 0 otherwise.
    """
    return run_health_check(verbose=getattr(args, "verbose", False))
