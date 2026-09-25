# Copyright (c) 2024-2026 Yago Lopez Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
The machine-wide health check ``wasm health`` and the panel both report.

This used to live entirely inside the ``wasm health`` command handler, which
is where ``GET /api/system/health`` would have had to duplicate it rather
than call it. The checking logic - disk space, the web servers, every
deployed application, certificates close to expiry, memory pressure - moved
here unchanged; :mod:`wasm.cli.commands.health` now only formats and prints
:class:`HealthReport`, and the endpoint serialises the same object to JSON.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from datetime import datetime

from wasm.core.app_state import RUNNING, STATIC, resolve_states
from wasm.core.config import Config
from wasm.core.exceptions import WASMError
from wasm.core.store import get_store
from wasm.managers.apache_manager import ApacheManager
from wasm.managers.cert_manager import CertificateInfo, CertManager
from wasm.managers.nginx_manager import NginxManager
from wasm.managers.service_manager import ServiceManager

#: A certificate this close to expiry is an incident, not a reminder.
CERT_CRITICAL_DAYS = 7

#: A certificate this close to expiry deserves a warning.
CERT_WARNING_DAYS = 30


@dataclass(frozen=True)
class HealthCheck:
    """
    One item of the health report.

    Attributes:
        name: What was checked, such as ``"Disk Space"`` or ``"Nginx"``.
        value: Human readable result.
        status: One of ``"ok"``, ``"warning"``, ``"error"`` or ``"info"``.
    """

    name: str
    value: str
    status: str


@dataclass
class HealthReport:
    """
    The result of a full health check.

    Attributes:
        disk: Disk space check, or ``None`` when free space could not be
            read at all - the one check that can be skipped outright rather
            than answer "could not check", because it needs a working
            filesystem call to have anything to report.
        nginx: Nginx's own status.
        apache: Apache's own status.
        applications: Summary across every deployed application.
        certificates: Summary across every certificate.
        memory: Memory pressure check.
        issues: Problems serious enough to fail the check.
        warnings: Problems worth an operator's attention that do not.
    """

    disk: HealthCheck | None
    nginx: HealthCheck
    apache: HealthCheck
    applications: HealthCheck
    certificates: HealthCheck
    memory: HealthCheck
    issues: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def checks(self) -> list[HealthCheck]:
        """Every check that ran, in the order the command prints them."""
        return [
            check
            for check in (
                self.disk,
                self.nginx,
                self.apache,
                self.applications,
                self.certificates,
                self.memory,
            )
            if check is not None
        ]

    @property
    def verdict(self) -> str:
        """``"error"`` if anything failed, ``"warning"`` if not but something needs attention, else ``"healthy"``."""
        if self.issues:
            return "error"
        if self.warnings:
            return "warning"
        return "healthy"


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


def _check_disk_space(config: Config, warnings: list[str], issues: list[str]) -> HealthCheck | None:
    """
    Report free space under the applications directory.

    Args:
        config: Configuration the applications directory is read from.
        warnings: Warning list to append to.
        issues: Issue list to append to.

    Returns:
        The check, or ``None`` when disk usage could not be read at all.
    """
    try:
        apps_dir = config.apps_directory
        if not apps_dir.exists():
            return HealthCheck("Disk Space", "Apps directory not found", "warning")

        stat = shutil.disk_usage(str(apps_dir))
        free_gb = stat.free / (1024**3)
        total_gb = stat.total / (1024**3)
        used_percent = ((stat.total - stat.free) / stat.total) * 100
        value = f"{free_gb:.1f}GB free / {total_gb:.1f}GB total ({used_percent:.0f}% used)"

        if free_gb < 1.0:
            issues.append(f"Low disk space: {free_gb:.1f}GB free")
            return HealthCheck("Disk Space", value, "error")
        if free_gb < 5.0:
            warnings.append(f"Disk space is getting low: {free_gb:.1f}GB free")
            return HealthCheck("Disk Space", value, "warning")
        return HealthCheck("Disk Space", value, "ok")
    except OSError as exc:
        warnings.append(f"Could not check disk space: {exc}")
        return None


def _check_web_servers(
    verbose: bool, issues: list[str], warnings: list[str]
) -> tuple[HealthCheck, HealthCheck]:
    """
    Report whether nginx and Apache are installed and running.

    Args:
        verbose: Passed through to the web server managers.
        issues: Issue list to append to.
        warnings: Warning list to append to.

    Returns:
        The nginx check and the Apache check.
    """
    nginx = NginxManager(verbose=verbose)
    apache = ApacheManager(verbose=verbose)

    nginx_installed = nginx.is_installed()
    apache_installed = apache.is_installed()

    if nginx_installed:
        if nginx.get_status().get("active"):
            nginx_check = HealthCheck("Nginx", "Running", "ok")
        else:
            issues.append("Nginx is installed but not running")
            nginx_check = HealthCheck("Nginx", "Stopped", "error")
    else:
        nginx_check = HealthCheck("Nginx", "Not installed", "info")

    if apache_installed:
        if apache.get_status().get("active"):
            apache_check = HealthCheck("Apache", "Running", "ok")
        else:
            warnings.append("Apache is installed but not running")
            apache_check = HealthCheck("Apache", "Stopped", "warning")
    else:
        apache_check = HealthCheck("Apache", "Not installed", "info")

    if not nginx_installed and not apache_installed:
        issues.append("No web server installed")

    return nginx_check, apache_check


def _check_applications(verbose: bool, warnings: list[str]) -> HealthCheck:
    """
    Summarise every deployed application's state.

    Args:
        verbose: Passed through to :class:`ServiceManager`.
        warnings: Warning list to append to, one entry per unhealthy app.

    Returns:
        The applications check.
    """
    store = get_store()
    service_manager = ServiceManager(verbose=verbose)

    apps = store.list_apps()

    # The same resolver `wasm list` uses. When these two commands each decided
    # for themselves what "running" meant, list reported fifteen applications
    # running while health reported seven stopped, and five of the seven were
    # static sites that have no service to run in the first place.
    states = resolve_states(apps, service_manager)

    apps_running = sum(1 for s in states.values() if s.label == RUNNING)
    apps_static = sum(1 for s in states.values() if s.label == STATIC)
    unhealthy = [(domain, s) for domain, s in states.items() if not s.healthy]

    for domain, current in unhealthy:
        warnings.append(f"App '{domain}' - {current.detail or current.label.lower()}")

    total_apps = len(apps)
    if total_apps == 0:
        return HealthCheck("Applications", "No applications deployed", "info")

    served = apps_running + apps_static
    summary = f"{served}/{total_apps} serving"
    if apps_static:
        summary += f" ({apps_static} static)"
    if unhealthy:
        return HealthCheck("Applications", f"{summary}, {len(unhealthy)} need attention", "warning")
    return HealthCheck("Applications", summary, "ok")


def _check_certificates(verbose: bool, issues: list[str], warnings: list[str]) -> HealthCheck:
    """
    Summarise certificates close to expiry.

    Args:
        verbose: Passed through to :class:`CertManager`.
        issues: Issue list to append to.
        warnings: Warning list to append to.

    Returns:
        The certificates check.
    """
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
            if days_left < CERT_CRITICAL_DAYS:
                issues.append(f"Certificate for {label} expires in {days_left} days")
                expiring_soon.append(label)
            elif days_left < CERT_WARNING_DAYS:
                warnings.append(f"Certificate for {label} expires in {days_left} days")
                expiring_soon.append(label)

        if expiring_soon:
            return HealthCheck(
                "SSL Certificates",
                f"{len(certs)} total, {len(expiring_soon)} expiring soon",
                "warning",
            )
        if certs:
            return HealthCheck("SSL Certificates", f"{len(certs)} total, all valid", "ok")
        return HealthCheck("SSL Certificates", "None configured", "info")
    except WASMError as exc:
        return HealthCheck("SSL Certificates", f"Could not check: {exc}", "warning")


def _check_memory(issues: list[str], warnings: list[str]) -> HealthCheck:
    """
    Report memory pressure from ``/proc/meminfo``.

    Args:
        issues: Issue list to append to.
        warnings: Warning list to append to.

    Returns:
        The memory check.
    """
    try:
        meminfo = _read_meminfo()

        total_mem = meminfo.get("MemTotal", 0) / 1024 / 1024  # GB
        free_mem = (meminfo.get("MemAvailable", 0) or meminfo.get("MemFree", 0)) / 1024 / 1024  # GB
        used_percent = ((total_mem - free_mem) / total_mem) * 100 if total_mem > 0 else 0
        value = f"{free_mem:.1f}GB free / {total_mem:.1f}GB total ({used_percent:.0f}% used)"

        if used_percent > 90:
            issues.append(f"High memory usage: {used_percent:.0f}%")
            return HealthCheck("Memory", value, "error")
        if used_percent > 75:
            warnings.append(f"Memory usage is high: {used_percent:.0f}%")
            return HealthCheck("Memory", value, "warning")
        return HealthCheck("Memory", value, "ok")
    except (OSError, ValueError, ZeroDivisionError) as exc:
        return HealthCheck("Memory", f"Could not check: {exc}", "warning")


def collect_health_report(*, verbose: bool = False) -> HealthReport:
    """
    Run every check and return the structured report.

    The one implementation ``wasm health`` and ``GET /api/system/health``
    both call: disk space, the web servers, every deployed application,
    certificates close to expiry and memory pressure.

    Args:
        verbose: Passed through to the managers this instantiates.

    Returns:
        Every check, plus the issues and warnings that decide the verdict.
    """
    config = Config()
    issues: list[str] = []
    warnings: list[str] = []

    disk = _check_disk_space(config, warnings, issues)
    nginx, apache = _check_web_servers(verbose, issues, warnings)
    applications = _check_applications(verbose, warnings)
    certificates = _check_certificates(verbose, issues, warnings)
    memory = _check_memory(issues, warnings)

    return HealthReport(
        disk=disk,
        nginx=nginx,
        apache=apache,
        applications=applications,
        certificates=certificates,
        memory=memory,
        issues=issues,
        warnings=warnings,
    )
