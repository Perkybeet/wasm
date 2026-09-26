# Copyright (c) 2024-2026 Yago Lopez Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
Diagnose why an application is down.

An operator staring at a 502 has to correlate several independent sources by
hand: is the unit even running, is it listening on the port WASM thinks it is,
does nginx reach it, what does the process's own log say, did the kernel kill
it for memory, is the certificate still valid, did the last deploy even
succeed, is the disk full. Each of those lives behind a different command.
This module runs all of them and orders the answer by how likely each one is
to be the actual cause, so the answer to "why is my app down" is the first
line, not a page of systemctl output to read yourself.

Two things shape the design:

- **Every probe is isolated.** A host with certbot uninstalled or a journal
  that has rotated away must not stop the port check from running. Each probe
  is called by :func:`diagnose`, and only :class:`~wasm.core.exceptions.WASMError`,
  :class:`OSError` and :class:`ValueError` - the exceptions a probe can
  actually raise - are caught there and turned into a ``skip`` check carrying
  the error as evidence. A probe that returns cleanly always reports on its
  own check; nothing here swallows one check's failure into another's.
- **Evidence is verbatim.** CLAUDE.md's rule for the panel applies here too: a
  system error is never paraphrased. ``journalctl``, ``ss`` and nginx's own
  error log reach the operator exactly as those programs printed them; only
  the summary line and the probable cause are WASM's own words.

The verdict and probable cause come from an explicit, ordered list of rules in
:func:`_decide`, each backed by its own test: a rule earlier in the list wins
over one later, and a diagnosis with no clear cause reports the aggregate
severity of the checks instead of guessing.
"""

from __future__ import annotations

import logging
import re
import shutil
import sqlite3
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal
from urllib.error import HTTPError, URLError

from wasm.core.config import Config
from wasm.core.exceptions import WASMError
from wasm.core.runner import CommandRunner, get_runner
from wasm.core.store import App, DeploymentStatus, WASMStore, get_store
from wasm.core.utils import domain_to_app_name
from wasm.deployers.helpers.health_gate import HealthCheck
from wasm.managers.cert_manager import CertManager
from wasm.managers.service_manager import ServiceManager

log = logging.getLogger(__name__)

#: Where nginx writes its error log. A module constant rather than a Config
#: field: it is not something an operator configures per deployment, and
#: tests point it at a disposable file by assigning this name.
NGINX_ERROR_LOG = Path("/var/log/nginx/error.log")

#: A local HTTP request that has not answered in this long is not going to.
_HTTP_PROBE_TIMEOUT = 3.0

#: nginx's plain HTTP listener. Diagnosis probes this rather than 443 so it
#: never has to negotiate TLS to ask "does nginx route this Host at all".
_NGINX_PROBE_PORT = 80

#: How far back OOM activity is still considered relevant to a current outage.
_OOM_WINDOW = "-7d"

#: How many bytes of the nginx error log are read from the end. Bounded so a
#: log nobody has rotated in a year cannot make this command slow.
_NGINX_LOG_TAIL_BYTES = 65536

#: How many matching nginx error log lines are kept as evidence.
_NGINX_LOG_MAX_LINES = 20

#: A certificate this close to expiry is worth a warning on its own check,
#: even when it is not yet the probable cause. Mirrors wasm health.
_CERT_WARNING_DAYS = 30

#: Disk usage past this is the probable cause by itself.
_DISK_FAIL_PERCENT = 95.0

#: Disk usage past this is worth a warning on the check, short of being named
#: the cause.
_DISK_WARN_PERCENT = 90.0

CheckStatus = Literal["ok", "warn", "fail", "skip"]
Verdict = Literal["healthy", "degraded", "down"]

#: What an HTTP probe returns: the status code it got, or the error it hit.
#: Exactly one of the two is set.
HttpGet = Callable[[str, Mapping[str, str]], tuple[int | None, str | None]]

#: What reads free disk space. Matches shutil.disk_usage's signature so the
#: real function is the default with no wrapping needed.
DiskUsage = Callable[[str], "shutil._ntuple_diskusage"]


@dataclass(frozen=True)
class Check:
    """
    The result of one diagnostic probe.

    Attributes:
        name: Stable identifier for the probe, such as ``"unit"`` or ``"port"``.
        status: ``"ok"`` when nothing is wrong, ``"warn"`` for something worth
            an operator's attention that is not the cause, ``"fail"`` for
            something that likely is, ``"skip"`` when the probe itself could
            not run.
        summary: One line, in WASM's own words.
        evidence: The raw output the probe collected - journal lines, ss
            output, an exception message - verbatim and unmodified.
    """

    name: str
    status: CheckStatus
    summary: str
    evidence: str = ""


@dataclass(frozen=True)
class Diagnosis:
    """
    The full answer to "why is this app down".

    Attributes:
        domain: The domain that was diagnosed.
        verdict: ``"healthy"`` when every check is clean, ``"degraded"`` when
            something needs attention but the app is likely still serving,
            ``"down"`` when it most likely is not.
        probable_cause: One sentence naming the most likely explanation, or
            None when the checks disagree with each other or are all clean.
        checks: Every probe that ran, in the order :data:`_PROBES` lists them.
    """

    domain: str
    verdict: Verdict
    probable_cause: str | None
    checks: tuple[Check, ...]


@dataclass
class _Context:
    """Shared, read-only state every probe needs. Built once per diagnosis."""

    domain: str
    app_name: str
    app: App | None
    runner: CommandRunner
    store: WASMStore
    now: datetime
    http_get: HttpGet
    disk_usage: DiskUsage
    config: Config
    service_manager: ServiceManager
    cert_manager: CertManager


ProbeResult = tuple[Check, dict[str, Any]]
Probe = Callable[[_Context], ProbeResult]


# -- HTTP probing -------------------------------------------------------------


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Report a redirect as the answer instead of following it."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        """
        Decline every redirect, so it surfaces through the response status.

        Returns:
            None, which makes urllib return the redirect response as-is.
        """
        return None


def _default_http_get(url: str, headers: Mapping[str, str]) -> tuple[int | None, str | None]:
    """
    Make one real HTTP GET request with a short timeout.

    Args:
        url: Address to request. Always http://127.0.0.1:<port><path>, built
            by the caller from a path the store validated.
        headers: Extra request headers, such as an explicit Host.

    Returns:
        The status code and None, or None and the error, depending on whether
        the request reached a server at all.
    """
    opener = urllib.request.build_opener(_NoRedirect())
    request = urllib.request.Request(url, headers=dict(headers))
    try:
        with opener.open(request, timeout=_HTTP_PROBE_TIMEOUT) as response:
            return response.status, None
    except HTTPError as exc:
        return exc.code, None
    except (URLError, OSError, ValueError) as exc:
        return None, str(exc)


# -- Small parsers -------------------------------------------------------------


def _parse_systemctl_show(output: str) -> dict[str, str]:
    """
    Parse ``systemctl show``'s ``Key=Value`` lines.

    Args:
        output: Standard output of a ``systemctl show`` call.

    Returns:
        Property name to value, for every line that had one.
    """
    fields: dict[str, str] = {}
    for line in output.splitlines():
        key, separator, value = line.partition("=")
        if separator:
            fields[key.strip()] = value.strip()
    return fields


def _as_int(value: str | None) -> int:
    """
    Read a systemctl property as an integer, defensively.

    Args:
        value: The raw property value, or None when it was absent.

    Returns:
        The integer, or 0 when the value is missing or not a number.
    """
    try:
        return int(str(value).strip() or 0)
    except ValueError:
        return 0


@dataclass(frozen=True)
class _ListenEntry:
    """One listening socket, as ``ss`` reported it."""

    port: int
    processes: tuple[tuple[str, str], ...]  # (process name, pid), pid as text


#: Matches one ``("name",pid=123,...)`` group inside ss's users: column. A
#: single port can list several processes - nginx's workers all inherit the
#: same listening fd - so every match is kept.
_SS_PROCESS_RE = re.compile(r'\("([^"]+)",pid=(\d+)')


def _parse_ss_output(output: str) -> list[_ListenEntry]:
    """
    Parse ``ss -ltnpH`` into the listening sockets it reports.

    Args:
        output: Standard output of the ss call.

    Returns:
        One entry per LISTEN line that named a port.
    """
    entries: list[_ListenEntry] = []
    for line in output.splitlines():
        parts = line.split()
        if len(parts) < 4 or parts[0].upper() != "LISTEN":
            continue
        port_match = re.search(r":(\d+)$", parts[3])
        if not port_match:
            continue
        processes = tuple(
            (name, pid)
            for token in parts[4:]
            if token.startswith("users:")
            for name, pid in _SS_PROCESS_RE.findall(token)
        )
        entries.append(_ListenEntry(port=int(port_match.group(1)), processes=processes))
    return entries


def _days_until(expiry: str, now: datetime) -> int | None:
    """
    Days left before a certificate expiry date.

    Args:
        expiry: Expiry date as certbot reports it, ``YYYY-MM-DD``.
        now: The moment to measure from.

    Returns:
        Whole days remaining, negative when already expired, or None when the
        date cannot be parsed.
    """
    try:
        expires = datetime.fromisoformat(expiry)
    except ValueError:
        return None
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    reference = now if now.tzinfo else now.replace(tzinfo=timezone.utc)
    return (expires - reference).days


# -- Probes ---------------------------------------------------------------


def _check_unit(ctx: _Context) -> ProbeResult:
    """Ask systemd what state the app's unit is in."""
    if ctx.app is not None and ctx.app.is_static:
        return (
            Check("unit", "skip", "Static site: served directly by the web server, no unit", ""),
            {},
        )

    info = ctx.service_manager.inspect_unit(ctx.app_name)
    if not info.exists:
        return (
            Check(
                "unit",
                "fail",
                f"No systemd unit found for {info.unit}",
                f"Expected a unit at {info.path}",
            ),
            {"exists": False},
        )

    result = ctx.runner.run(
        [
            "systemctl",
            "show",
            "-p",
            "ActiveState,SubState,Result,ExecMainStatus,NRestarts",
            info.unit_file,
        ],
        timeout=15,
    )
    fields = _parse_systemctl_show(result.stdout)
    active_state = fields.get("ActiveState", "")
    sub_state = fields.get("SubState", "")
    result_code = fields.get("Result", "")
    exec_main_status = fields.get("ExecMainStatus", "")
    restarts = _as_int(fields.get("NRestarts"))

    caveat = "" if info.managed else " (not managed by WASM)"
    if active_state == "active" and sub_state == "running":
        status: CheckStatus = "ok"
        summary = f"{info.unit_file} is active (running){caveat}"
    elif sub_state == "auto-restart" or active_state == "activating":
        status = "fail"
        summary = f"{info.unit_file} is restarting{caveat} ({restarts} restarts so far)"
    elif active_state == "failed" or sub_state == "failed":
        status = "fail"
        summary = f"{info.unit_file} failed{caveat} (Result={result_code or 'unknown'})"
    else:
        status = "warn"
        summary = (
            f"{info.unit_file} is {active_state or 'unknown'}/{sub_state or 'unknown'}{caveat}"
        )

    facts = {
        "exists": True,
        "managed": info.managed,
        "active_state": active_state,
        "sub_state": sub_state,
        "result": result_code,
        "exec_main_status": exec_main_status,
        "restarts": restarts,
    }
    return Check("unit", status, summary, result.stdout.strip()), facts


def _check_port(ctx: _Context) -> ProbeResult:
    """Check whether anything listens on the port WASM recorded for this app."""
    if ctx.app is not None and ctx.app.is_static:
        return Check("port", "skip", "Static site: no backend port to check", ""), {}

    recorded_port = ctx.app.port if ctx.app else None
    if recorded_port is None:
        return Check("port", "skip", "No port recorded for this app", ""), {}

    result = ctx.runner.run(["ss", "-ltnpH"], timeout=10)
    entries = _parse_ss_output(result.stdout)
    evidence = result.stdout.strip()

    on_recorded_port = [e for e in entries if e.port == recorded_port]
    if on_recorded_port:
        procs = ", ".join(f"{name} (pid {pid})" for name, pid in on_recorded_port[0].processes)
        summary = f"Port {recorded_port} is listening" + (f" ({procs})" if procs else "")
        return Check("port", "ok", summary, evidence), {
            "recorded_port": recorded_port,
            "listening": True,
        }

    # Nothing answers on the recorded port. Before calling it dead, check
    # whether the app's own process is listening somewhere else - a config
    # drift between the unit's PORT and what WASM has on record.
    info = ctx.service_manager.inspect_unit(ctx.app_name)
    main_pid = None
    if info.exists:
        pid_result = ctx.runner.run(
            ["systemctl", "show", "-p", "MainPID", info.unit_file], timeout=10
        )
        main_pid = _parse_systemctl_show(pid_result.stdout).get("MainPID")

    actual_port = None
    if main_pid and main_pid != "0":
        for entry in entries:
            if any(pid == main_pid for _, pid in entry.processes):
                actual_port = entry.port
                break

    if actual_port is not None:
        return (
            Check(
                "port",
                "fail",
                f"The app listens on {actual_port}, not the recorded port {recorded_port}",
                evidence,
            ),
            {
                "recorded_port": recorded_port,
                "listening": False,
                "actual_port": actual_port,
                "mismatch": True,
            },
        )

    return (
        Check("port", "fail", f"Nothing is listening on port {recorded_port}", evidence),
        {
            "recorded_port": recorded_port,
            "listening": False,
            "actual_port": None,
            "mismatch": False,
        },
    )


def _check_http_direct(ctx: _Context) -> ProbeResult:
    """
    Probe the app directly on 127.0.0.1:<port>, bypassing nginx.

    Asks what the health gate asks - the application's path, judged by its
    expectation - so a diagnosis never calls healthy what a deploy would
    roll back, or the reverse.
    """
    if ctx.app is None or ctx.app.is_static or not ctx.app.port:
        return Check("http_direct", "skip", "No backend port to probe directly", ""), {}

    check = HealthCheck.for_app(ctx.app)
    url = check.url(ctx.app.port)
    code, error = ctx.http_get(url, {})
    if error is not None:
        return (
            Check(
                "http_direct",
                "fail",
                f"Could not reach the app directly on port {ctx.app.port}",
                error,
            ),
            {"ok": False, "status": None, "error": error},
        )

    ok = code is not None and check.accepts(code)
    status: CheckStatus = "ok" if ok else "fail"
    return (
        Check(
            "http_direct",
            status,
            f"HTTP {code} from 127.0.0.1:{ctx.app.port}",
            f"GET {url} -> {code} (healthy: {check.describe_expect()})",
        ),
        {"ok": ok, "status": code, "error": None},
    )


def _check_http_nginx(ctx: _Context) -> ProbeResult:
    """
    Probe the app through nginx, with the domain as the Host header.

    The same path and expectation as the direct probe, with one allowance:
    port 80 redirecting (to HTTPS, usually) is the site working, not the
    application failing its expectation.
    """
    check = HealthCheck.for_app(ctx.app)
    url = check.url(_NGINX_PROBE_PORT)
    code, error = ctx.http_get(url, {"Host": ctx.domain})
    if error is not None:
        return (
            Check("http_nginx", "fail", f"Could not reach nginx for {ctx.domain}", error),
            {"ok": False, "status": None, "error": error},
        )

    ok = code is not None and (check.accepts(code) or 300 <= code < 400)
    status: CheckStatus = "ok" if ok else "fail"
    evidence = f"GET {url} Host: {ctx.domain} -> {code}"
    return (
        Check("http_nginx", status, f"HTTP {code} from nginx for Host: {ctx.domain}", evidence),
        {"ok": ok, "status": code, "error": None},
    )


def _check_journal(ctx: _Context) -> ProbeResult:
    """Read the unit's last 50 journal lines, verbatim."""
    if ctx.app is not None and ctx.app.is_static:
        return Check("journal", "skip", "Static site: no unit to read logs from", ""), {}

    info = ctx.service_manager.inspect_unit(ctx.app_name)
    if not info.exists:
        return Check("journal", "skip", f"No unit named {info.unit} to read logs from", ""), {}

    result = ctx.runner.run(
        ["journalctl", "-u", info.unit_file, "-n", "50", "--no-pager", "-o", "short-iso"],
        timeout=15,
    )
    if not result.success:
        return (
            Check(
                "journal",
                "skip",
                f"Could not read the journal for {info.unit_file}",
                result.stderr.strip(),
            ),
            {},
        )

    text = result.stdout.strip()
    if not text:
        return Check("journal", "ok", f"No journal entries for {info.unit_file}", ""), {}

    lines = len(text.splitlines())
    return Check("journal", "ok", f"Last {lines} journal line(s) for {info.unit_file}", text), {}


def _check_nginx_log(ctx: _Context) -> ProbeResult:
    """Read the tail of nginx's error log, filtered to this domain and upstream errors."""
    try:
        with NGINX_ERROR_LOG.open("rb") as handle:
            handle.seek(0, 2)
            size = handle.tell()
            handle.seek(max(0, size - _NGINX_LOG_TAIL_BYTES))
            data = handle.read()
    except OSError as exc:
        return Check("nginx_log", "skip", f"Could not read {NGINX_ERROR_LOG}", str(exc)), {}

    text = data.decode("utf-8", errors="replace")
    needle = ctx.domain.lower()
    matches = [
        line for line in text.splitlines() if needle in line.lower() or "upstream" in line.lower()
    ]

    if not matches:
        return Check("nginx_log", "ok", "No nginx error log lines mention this domain", ""), {}

    tail = matches[-_NGINX_LOG_MAX_LINES:]
    summary = f"{len(matches)} nginx error log line(s) mention this domain or an upstream"
    return Check("nginx_log", "warn", summary, "\n".join(tail)), {"matches": len(matches)}


def _check_certificate(ctx: _Context) -> ProbeResult:
    """Check the certificate covering this domain, reusing CertManager's own parsing."""
    cert = ctx.cert_manager.get_cert_info(ctx.domain)
    ssl_expected = ctx.app.ssl_enabled if ctx.app is not None else None

    if cert is None:
        if ssl_expected:
            return (
                Check(
                    "certificate",
                    "fail",
                    f"TLS is enabled for {ctx.domain} but no certificate was found",
                    "",
                ),
                {"found": False},
            )
        return Check("certificate", "skip", f"No certificate found for {ctx.domain}", ""), {
            "found": False
        }

    label = cert.name or ctx.domain
    evidence = cert.expiry_full or f"Expiry Date: {cert.expiry}"

    if not cert.expiry:
        return Check(
            "certificate", "warn", f"Could not read the expiry date for {label}", evidence
        ), {"found": True}

    days_left = _days_until(cert.expiry, ctx.now)
    if days_left is None:
        return Check("certificate", "warn", f"Unreadable expiry date for {label}", evidence), {
            "found": True
        }

    facts = {"found": True, "expiry": cert.expiry, "days_left": days_left, "expired": days_left < 0}
    if days_left < 0:
        return Check(
            "certificate", "fail", f"The certificate for {label} expired on {cert.expiry}", evidence
        ), facts
    if days_left < _CERT_WARNING_DAYS:
        return (
            Check(
                "certificate",
                "warn",
                f"The certificate for {label} expires in {days_left} day(s)",
                evidence,
            ),
            facts,
        )
    return (
        Check(
            "certificate",
            "ok",
            f"The certificate for {label} is valid for {days_left} more day(s)",
            evidence,
        ),
        facts,
    )


def _check_last_deployment(ctx: _Context) -> ProbeResult:
    """Read the outcome of the last deployment recorded for this domain."""
    deployments = ctx.store.list_deployments(ctx.domain, limit=1)
    if not deployments:
        return Check("last_deployment", "skip", "No deployment recorded for this domain", ""), {}

    record = deployments[0]
    evidence_lines = [
        f"status={record.status}",
        f"started_at={record.started_at}",
        f"finished_at={record.finished_at}",
    ]
    if record.git_commit:
        evidence_lines.append(f"git_commit={record.git_commit}")
    if record.error:
        evidence_lines.append(f"error={record.error}")
    evidence = "\n".join(evidence_lines)

    if record.status == DeploymentStatus.FAILED.value:
        summary = "The last deployment failed"
        if record.error:
            summary += f": {record.error}"
        return Check("last_deployment", "fail", summary, evidence), {"failed": True}

    if record.status == DeploymentStatus.SUCCESS.value:
        return Check("last_deployment", "ok", "The last deployment succeeded", evidence), {
            "failed": False
        }

    return Check("last_deployment", "warn", f"The last deployment is {record.status}", evidence), {
        "failed": False
    }


def _check_oom(ctx: _Context) -> ProbeResult:
    """
    Look for OOM activity in the kernel log.

    Correlating a kernel OOM message with one specific unit exactly would need
    matching cgroups or the pid at the moment of the kill, which the message
    does not reliably carry. Presence of OOM activity while the unit is
    failing or crash-looping is what :func:`_decide` treats as the cause; this
    probe reports the activity, the unit checks report the failure.
    """
    result = ctx.runner.run(
        ["journalctl", "-k", "--since", _OOM_WINDOW, "--grep", "oom", "-o", "short-iso"],
        timeout=15,
    )
    if not result.success:
        return Check("oom", "skip", "Could not read the kernel log", result.stderr.strip()), {}

    text = result.stdout.strip()
    if not text:
        return Check("oom", "ok", "No OOM kills in the kernel log in the last 7 days", ""), {
            "found": False
        }

    return Check("oom", "warn", "The kernel log shows OOM activity in the last 7 days", text), {
        "found": True
    }


def _check_disk(ctx: _Context) -> ProbeResult:
    """Check free space on the filesystem holding the apps directory."""
    apps_dir = ctx.config.apps_directory
    usage = ctx.disk_usage(str(apps_dir))
    percent_used = ((usage.total - usage.free) / usage.total * 100) if usage.total else 0.0
    free_gb = usage.free / (1024**3)
    evidence = f"total={usage.total} used={usage.total - usage.free} free={usage.free}"
    summary = (
        f"{free_gb:.1f}GB free ({percent_used:.0f}% used) on the filesystem holding {apps_dir}"
    )

    if percent_used > _DISK_FAIL_PERCENT:
        status: CheckStatus = "fail"
    elif percent_used > _DISK_WARN_PERCENT:
        status = "warn"
    else:
        status = "ok"
    return Check("disk", status, summary, evidence), {"percent_used": percent_used}


#: Every probe, in the order they run and appear in a diagnosis. A probe's
#: name here is what a failure is reported under if the probe itself raises.
_PROBES: tuple[tuple[str, Probe], ...] = (
    ("unit", _check_unit),
    ("port", _check_port),
    ("http_direct", _check_http_direct),
    ("http_nginx", _check_http_nginx),
    ("journal", _check_journal),
    ("nginx_log", _check_nginx_log),
    ("certificate", _check_certificate),
    ("last_deployment", _check_last_deployment),
    ("oom", _check_oom),
    ("disk", _check_disk),
)


# -- Verdict ----------------------------------------------------------------


def _decide(
    ctx: _Context, checks: tuple[Check, ...], facts: dict[str, dict[str, Any]]
) -> tuple[Verdict, str | None]:
    """
    Turn the collected facts into a verdict and a probable cause.

    Rules are ordered from most specific and most severe to least; the first
    one that matches wins. A diagnosis where nothing specific matches falls
    back to the aggregate severity of the checks, with no named cause.

    Args:
        ctx: The context the checks ran with.
        checks: Every check that ran.
        facts: Each check's name to the internal facts it collected.

    Returns:
        The verdict and the probable cause, or None when no rule matched.
    """
    unit = facts.get("unit", {})
    port = facts.get("port", {})
    http_direct = facts.get("http_direct", {})
    http_nginx = facts.get("http_nginx", {})
    cert = facts.get("certificate", {})
    disk = facts.get("disk", {})
    oom = facts.get("oom", {})

    active_state = unit.get("active_state", "")
    sub_state = unit.get("sub_state", "")
    crash_looping = sub_state == "auto-restart" or active_state == "activating"
    unit_failed = active_state == "failed" or sub_state == "failed"

    if unit.get("exists") is False:
        return (
            "down",
            f"No systemd unit found for {ctx.app_name}; it has never been started or its unit was removed.",
        )

    if (unit_failed or crash_looping) and oom.get("found"):
        return "down", "Killed by the kernel for running out of memory (limit or machine)."

    if crash_looping:
        exit_status = unit.get("exec_main_status") or "unknown"
        return (
            "down",
            f"The process exits with status {exit_status} at start; see the last journal lines below.",
        )

    if unit_failed:
        result_code = unit.get("result") or "unknown"
        exit_status = unit.get("exec_main_status") or "unknown"
        return (
            "down",
            f"The unit failed to start (Result={result_code}, exit status {exit_status}); "
            "see the last journal lines below.",
        )

    if port.get("recorded_port") is not None and not port.get("listening"):
        recorded_port = port["recorded_port"]
        if port.get("mismatch"):
            return "down", f"Listening on {port['actual_port']}, WASM routes to {recorded_port}."
        return "down", f"The process is running but not listening on port {recorded_port}."

    if http_direct.get("ok") and not http_nginx.get("ok"):
        nginx_status = http_nginx.get("status")
        detail = (
            f"returns {nginx_status}"
            if nginx_status is not None
            else f"cannot connect ({http_nginx.get('error')})"
        )
        return (
            "down",
            f"The app answers directly, but nginx {detail} for {ctx.domain}; nginx cannot reach the app.",
        )

    if cert.get("expired"):
        return "down", f"The TLS certificate for {ctx.domain} expired on {cert.get('expiry')}."

    if disk.get("percent_used", 0) > _DISK_FAIL_PERCENT:
        return (
            "degraded",
            f"The disk holding the apps directory is {disk['percent_used']:.0f}% full.",
        )

    statuses = {c.status for c in checks}
    if "fail" in statuses:
        return "down", None
    if "warn" in statuses:
        return "degraded", None
    return "healthy", None


# -- Entry point --------------------------------------------------------------


def _safe_get_app(store: WASMStore, domain: str) -> App | None:
    """
    Look up an application record, tolerating a store that cannot answer.

    Args:
        store: The store to read.
        domain: Domain to look up.

    Returns:
        The application record, or None when it does not exist or the store
        could not be read.
    """
    try:
        return store.get_app(domain)
    except (WASMError, sqlite3.Error) as exc:
        log.debug(f"Could not read {domain} from the store: {exc}")
        return None


def diagnose(
    domain: str,
    *,
    runner: CommandRunner | None = None,
    store: WASMStore | None = None,
    now: Callable[[], datetime] | None = None,
    http_get: HttpGet | None = None,
    disk_usage: DiskUsage | None = None,
) -> Diagnosis:
    """
    Correlate everything WASM can read about a domain into one diagnosis.

    Args:
        domain: The domain to diagnose.
        runner: Command runner every probe executes through. Defaults to the
            process-wide runner.
        store: Store every probe reads through. Defaults to the process-wide
            store.
        now: Returns the moment to measure certificate expiry against.
            Defaults to the current UTC time.
        http_get: Performs the direct and nginx HTTP probes. Defaults to a
            real, short-timeout request; tests inject a fake so no probe ever
            opens a socket.
        disk_usage: Reads free space for a path, with :func:`shutil.disk_usage`'s
            signature. Defaults to :func:`shutil.disk_usage`.

    Returns:
        Every probe's result and the verdict derived from them.
    """
    resolved_runner = runner or get_runner()
    resolved_store = store or get_store()

    ctx = _Context(
        domain=domain,
        app_name=domain_to_app_name(domain),
        app=_safe_get_app(resolved_store, domain),
        runner=resolved_runner,
        store=resolved_store,
        now=(now or (lambda: datetime.now(timezone.utc)))(),
        http_get=http_get or _default_http_get,
        disk_usage=disk_usage or shutil.disk_usage,
        config=Config(),
        service_manager=ServiceManager(runner=resolved_runner),
        cert_manager=CertManager(runner=resolved_runner),
    )

    checks: list[Check] = []
    facts: dict[str, dict[str, Any]] = {}
    for name, probe in _PROBES:
        try:
            check, probe_facts = probe(ctx)
        except (WASMError, OSError, ValueError) as exc:
            check = Check(name, "skip", "Could not complete this check", str(exc))
            probe_facts = {}
        checks.append(check)
        facts[name] = probe_facts

    verdict, cause = _decide(ctx, tuple(checks), facts)
    return Diagnosis(domain=domain, verdict=verdict, probable_cause=cause, checks=tuple(checks))
