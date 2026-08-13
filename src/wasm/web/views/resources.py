# Copyright (c) 2024-2026 Yago Lopez Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
Everything the pages show, shaped into plain data.

A template never reaches into a manager or a model. It is handed a context
built here, so the markup cannot decide what a record means, and a screen that
lists certificates ends up speaking the same language as the one that lists
services.

That language is four words: ``active``, ``idle``, ``busy`` and ``failed``.
The state rail, the badge and the notice all agree on them, so an operator
learns the vocabulary once. Every mapping from a systemd status, a certbot
expiry date or a job status onto those four words happens in this module and
nowhere else.

Reads go through the same managers the CLI uses. There is one implementation
of the product; this is a view of it.
"""

from __future__ import annotations

import datetime as dt
import logging
import re
from collections.abc import Callable, Iterator, Mapping, Sequence
from typing import Any

from wasm.core.config import REDACTED
from wasm.web.views.rendering import filesize, since

log = logging.getLogger(__name__)

#: Certbot renews at thirty days. Before that a certificate is a fact; after
#: it, it is a thing to watch, so that is where the rail turns amber.
CERT_WARNING_DAYS = 30

#: How many finished jobs the activity screen keeps on show.
RECENT_JOBS = 25

#: How many of a job's log lines are printed inline. The full stream is in the
#: docked drawer; this is the tail an operator reads to see what broke.
JOB_LOG_TAIL = 40

#: How many jobs an application's own page lists.
APP_JOBS = 10

#: Credentials embedded in a connection string. ``redact_secrets`` works on key
#: names, so ``DATABASE_URL`` survives it with the password in plain sight.
_URL_CREDENTIALS = re.compile(r"(?P<prefix>[a-zA-Z][\w+.-]*://[^:/?#@\s]+:)[^@\s]+(?P<at>@)")

#: Job status as the job manager records it, mapped onto the panel vocabulary.
#: A queued job is amber like a running one: from the operator's side both mean
#: "this machine is mid-change", which is what the rail is for.
_JOB_STATES: dict[str, str] = {
    "pending": "busy",
    "running": "busy",
    "completed": "active",
    "failed": "failed",
    "cancelled": "idle",
}


# --------------------------------------------------------------- resources


def resource_rows(kind: str) -> list[dict[str, Any]]:
    """
    Read a resource list from the store, shaped for the row component.

    Args:
        kind: Which resource to read: apps, sites, services or databases.

    Returns:
        Rows ready for the template. Empty when the store cannot be read.
    """
    from wasm.core.store import get_store

    store = get_store()
    readers: dict[str, Callable[[], list[Any]]] = {
        "apps": store.list_apps,
        "sites": store.list_sites,
        "services": store.list_services,
        "databases": store.list_databases,
    }
    try:
        records = readers[kind]()
    except Exception as exc:
        log.warning("Could not read %s from the store: %s", kind, exc)
        return []

    return [_shape(kind, record) for record in records]


def _service_actions(record: Any) -> list[dict[str, Any]]:
    """
    Build the buttons a systemd unit should offer, given what it is doing.

    Offering every verb at all times is how an operator ends up clicking "Start"
    on a running unit and reading a failure that is really a no-op. The row
    offers what would change something: a running unit can be restarted or
    stopped, a stopped one can be started, and either can be enabled or
    disabled for the next boot.

    The API has had all five of these since the beginning. The panel only ever
    called restart, so a service the operator wanted stopped could not be
    stopped from the screen that showed it was running.

    Args:
        record: A :class:`~wasm.core.store.Service`.

    Returns:
        Action descriptions for the row's buttons.
    """
    name = record.name
    running = str(record.status).lower() in {"active", "running"}

    actions: list[dict[str, Any]] = []
    if running:
        actions.append(
            {
                "label": "Restart",
                "endpoint": f"/api/services/{name}/restart",
                "done": f"Restarted {name}",
                "confirm": None,
            }
        )
        actions.append(
            {
                "label": "Stop",
                "endpoint": f"/api/services/{name}/stop",
                "done": f"Stopped {name}",
                "confirm": f"Stop {name}? Whatever it serves stops answering.",
            }
        )
    else:
        actions.append(
            {
                "label": "Start",
                "endpoint": f"/api/services/{name}/start",
                "done": f"Started {name}",
                "confirm": None,
            }
        )

    if record.enabled:
        actions.append(
            {
                "label": "Disable",
                "endpoint": f"/api/services/{name}/disable",
                "done": f"Disabled {name}",
                "confirm": f"Disable {name}? It will not start on the next boot.",
            }
        )
    else:
        actions.append(
            {
                "label": "Enable",
                "endpoint": f"/api/services/{name}/enable",
                "done": f"Enabled {name}",
                "confirm": None,
            }
        )

    return actions


def _site_actions(record: Any) -> list[dict[str, Any]]:
    """
    Build the buttons a web server site should offer.

    The row displayed "enabled" or "disabled" and gave no way to change it,
    which is the most conspicuous kind of dead screen: it states a fact the
    operator is obviously there to act on.

    Args:
        record: A :class:`~wasm.core.store.Site`.

    Returns:
        Action descriptions for the row's buttons.
    """
    domain = record.domain

    if record.enabled:
        return [
            {
                "label": "Disable",
                "endpoint": f"/api/sites/{domain}/disable",
                "done": f"Disabled {domain}",
                "confirm": (
                    f"Disable {domain}? The web server stops answering for it "
                    "until it is enabled again."
                ),
            }
        ]

    return [
        {
            "label": "Enable",
            "endpoint": f"/api/sites/{domain}/enable",
            "done": f"Enabled {domain}",
            "confirm": None,
        }
    ]


def _shape(kind: str, record: Any) -> dict[str, Any]:
    """
    Turn a store record into the fields the row component needs.

    The endpoints are built here rather than in the template. A template that
    concatenates ``/api/`` with a resource kind is a template that can be wrong
    about a route, which is exactly how the databases list came to offer a
    delete button pointing at an address that does not exist.

    Args:
        kind: Which resource this is.
        record: The store dataclass.

    Returns:
        A row context.
    """
    if kind == "apps":
        return {
            "id": record.domain,
            "domain": record.domain,
            "state": record.status,
            # The rail is a colour, and a colour alone is unreadable to anyone
            # who cannot tell these hues apart and invisible to a screen
            # reader. Records that have a real state carry it in words too.
            "state_text": record.status,
            "meta": [
                ("type", record.app_type),
                ("port", record.port or "—"),
                ("ssl", "yes" if record.ssl_enabled else "no"),
            ],
            "href": f"/apps/{record.domain}",
            "log_url": f"/ws/logs/{record.domain}",
            "restart_endpoint": f"/api/apps/{record.domain}/restart",
            # Taking a backup is the one thing an operator wants to do
            # immediately before anything risky, so it belongs on the row
            # rather than three screens away.
            "actions": [
                {
                    "label": "Back up",
                    "endpoint": f"/apps/{record.domain}/backup",
                    "done": f"Backup started for {record.domain}",
                    "confirm": None,
                }
            ],
            "delete_endpoint": f"/api/apps/{record.domain}",
            "delete_consequence": (
                "The service, the web server configuration and the deployed "
                "files are removed. This cannot be undone."
            ),
        }
    if kind == "sites":
        return {
            "id": record.domain,
            "domain": record.domain,
            "state": "active" if record.enabled else "idle",
            "state_text": "enabled" if record.enabled else "disabled",
            "meta": [
                ("server", record.webserver),
                ("ssl", "yes" if record.ssl_enabled else "no"),
                ("port", record.proxy_port or "—"),
            ],
            "href": None,
            "log_url": None,
            "restart_endpoint": None,
            "actions": _site_actions(record),
            "delete_endpoint": f"/api/sites/{record.domain}",
            "delete_consequence": (
                "The web server stops answering for this domain. The "
                "application itself is left alone."
            ),
        }
    if kind == "services":
        return {
            "id": record.name,
            "domain": record.name,
            "state": record.status,
            "state_text": record.status,
            "meta": [("port", record.port or "—"), ("user", record.user)],
            "href": None,
            "log_url": f"/ws/logs/{record.name}",
            "restart_endpoint": None,
            "actions": _service_actions(record),
            "delete_endpoint": f"/api/services/{record.name}",
            "delete_consequence": "The systemd unit is stopped and removed.",
        }

    name = getattr(record, "name", "?")
    engine = getattr(record, "engine", "?")
    return {
        "id": f"{engine}-{name}",
        "domain": name,
        "state": "idle",
        # A database record carries no health, so naming a state would invent
        # one. The rail stays grey and the row says nothing it cannot know.
        "state_text": None,
        "meta": [("engine", engine), ("user", getattr(record, "username", None) or "—")],
        "href": None,
        "log_url": None,
        "restart_endpoint": None,
        # Two segments, and both are meant. The API router mounts the databases
        # module under /api/databases and the module declares its own
        # /databases collection beside /engines, /users and /backups. The row
        # used to point at /api/databases/{name}, which matches nothing at all.
        "delete_endpoint": f"/api/databases/databases/{engine}/{name}",
        "delete_consequence": "Every table in it goes with it. This cannot be undone.",
    }


def needs_attention(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """
    Pick out what an operator should look at first.

    Args:
        rows: Shaped application rows.

    Returns:
        The subset in a failed state.
    """
    return [dict(row) for row in rows if row["state"] in {"failed", "error"}]


# ------------------------------------------------------------ certificates


def certificate_rows() -> tuple[list[dict[str, Any]], dict[str, str] | None]:
    """
    List the certificates certbot manages on this machine.

    Returns:
        The rows, and a problem to show above them when certbot cannot answer.
        The problem carries the fix and, when there is one, the tool's own
        output, which is never paraphrased.
    """
    from wasm.managers.cert_manager import CertManager

    manager = CertManager(verbose=False)
    if not manager.is_installed():
        return [], {
            "fix": (
                "certbot is not installed, so this machine cannot issue or renew "
                "certificates. Install it with your package manager, for example "
                "apt install certbot, and reload this page."
            ),
            "output": "",
        }

    entries = manager.list_certificates()
    return [_shape_certificate(entry) for entry in entries], None


def _shape_certificate(entry: Any) -> dict[str, Any]:
    """
    Turn one ``certbot certificates`` entry into a row context.

    Args:
        entry: A :class:`~wasm.managers.cert_manager.CertificateInfo`.

    Returns:
        A row context.
    """
    name = entry.name or "unnamed"
    domains = list(entry.domains)
    days = days_until(entry.expiry)
    covers = domains[0] if domains else name
    if len(domains) > 1:
        covers = f"{covers} +{len(domains) - 1}"

    return {
        "id": name,
        "name": name,
        "state": certificate_state(days),
        "domains": domains,
        "expires_on": entry.expiry or "unknown",
        "days_remaining": days,
        "meta": [
            ("expires", entry.expiry or "unknown"),
            ("in", _days_label(days)),
            ("covers", covers),
            ("issued by", "certbot"),
        ],
        # certbot prints its own validity note; it is shown as it comes.
        "note": entry.expiry_full or entry.cert_path,
        "cert_path": entry.cert_path or "—",
        "key_path": entry.key_path or "—",
        "renew_endpoint": f"/api/certs/{name}/renew",
        # Certificates were the only resource in the panel with no way to get
        # rid of one. Revoking and deleting are different acts and both are
        # offered: revoking tells the authority the key is no longer trusted,
        # deleting only stops this machine from serving it.
        "revoke_endpoint": f"/api/certs/{name}/revoke",
        "delete_endpoint": f"/api/certs/{name}",
        "delete_consequence": (
            "The certificate and its private key are removed from this "
            "machine. Any site still configured for HTTPS with it stops "
            "answering until another one is issued."
        ),
        "revoke_question": (
            f"Revoke the certificate for {name}? The certificate authority is "
            "told the key is no longer trusted, and it cannot be un-revoked. "
            "The files stay on disk."
        ),
    }


def days_until(expiry: str | None) -> int | None:
    """
    Count the days left before a date certbot printed.

    Args:
        expiry: Expiry date as ``YYYY-MM-DD``, or None when certbot's output
            could not be parsed.

    Returns:
        Days remaining, negative once expired, or None when unknown.
    """
    if not expiry:
        return None
    try:
        expires = dt.date.fromisoformat(expiry)
    except ValueError:
        return None
    return (expires - dt.date.today()).days


def certificate_state(days: int | None) -> str:
    """
    Map days remaining onto the panel's state vocabulary.

    Args:
        days: Days before expiry, negative once expired.

    Returns:
        One of ``active``, ``busy``, ``failed`` or ``idle``.
    """
    if days is None:
        return "idle"
    if days <= 0:
        return "failed"
    if days <= CERT_WARNING_DAYS:
        return "busy"
    return "active"


def _days_label(days: int | None) -> str:
    """
    Render days remaining the way an operator reads them.

    Args:
        days: Days before expiry, negative once expired.

    Returns:
        A short phrase such as "45 days" or "expired".
    """
    if days is None:
        return "unknown"
    if days < 0:
        return f"expired {abs(days)} days ago"
    if days == 0:
        return "expires today"
    return f"{days} days"


def domains_without_certificate(
    certificates: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """
    List the deployed domains no certificate covers.

    This is what makes issuing possible without a free text field: the panel
    already knows every domain it serves, so the operator picks one instead of
    typing it and finding out later that certbot disagreed.

    Args:
        certificates: Shaped certificate rows.

    Returns:
        One entry per uncovered domain, with the endpoint that issues for it.
    """
    covered: set[str] = set()
    for certificate in certificates:
        covered.update(certificate["domains"])
        covered.add(certificate["name"])

    seen: set[str] = set()
    uncovered: list[dict[str, Any]] = []
    for row in resource_rows("apps") + resource_rows("sites"):
        domain = row["domain"]
        if domain in covered or domain in seen:
            continue
        seen.add(domain)
        uncovered.append(
            {
                "id": f"uncovered-{domain}",
                "domain": domain,
                "state": "idle",
                "meta": [("certificate", "none"), ("https", "unavailable")],
                "issue_endpoint": f"/api/certs/{domain}",
            }
        )
    return uncovered


# ----------------------------------------------------------------- backups


def backup_rows() -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """
    List every backup on this machine, newest first.

    Returns:
        The rows and a storage summary holding the directory, the archive
        count and the total size.
    """
    from wasm.managers.backup_manager import BackupManager

    manager = BackupManager(verbose=False)
    records = manager.list_backups()
    usage = manager.get_storage_usage()
    count = int(usage["total_backups"])
    directory = str(manager.backup_dir)
    size = filesize(int(usage["total_size_bytes"]))
    storage = {
        "directory": directory,
        "count": count,
        "size": size,
        "summary": f"{count} {'archive' if count == 1 else 'archives'}, {size} in {directory}",
    }
    return [_shape_backup(record) for record in records], storage


def _shape_backup(record: Any) -> dict[str, Any]:
    """
    Turn backup metadata into a row context.

    Args:
        record: A :class:`~wasm.managers.backup_manager.BackupMetadata`.

    Returns:
        A row context, including the exact wording of both confirmations. A
        destructive question that does not name the domain and the consequence
        is not a question, it is a speed bump.
    """
    contents = ", ".join(backup_contents(record))
    return {
        "id": record.id,
        "domain": record.domain,
        # A stored archive is not a running thing: grey means "nothing wrong
        # here", which is the truth until someone verifies it.
        "state": "idle",
        "meta": [
            ("taken", since(record.created_at)),
            ("size", filesize(record.size_bytes)),
            ("holds", contents),
            ("type", record.app_type),
        ],
        "note": record.description or record.id,
        "created_at": record.created_at,
        "contents": backup_contents(record),
        # Checking an archive is sound is the one thing that can be done to a
        # backup safely, and the only way to learn it is restorable before the
        # day it has to be.
        "verify_endpoint": f"/backups/{record.id}/verify",
        "restore_endpoint": f"/api/backups/{record.id}/restore",
        "restore_question": (
            f"Restore {record.id} into {record.domain}? "
            f"This overwrites the files currently deployed at {record.domain} "
            "and cannot be undone."
        ),
        "delete_endpoint": f"/api/backups/{record.id}",
        "delete_question": (
            f"Delete backup {record.id} of {record.domain}? "
            "The archive is removed from disk and cannot be recovered."
        ),
    }


def backup_contents(record: Any) -> list[str]:
    """
    Say what is inside an archive.

    Args:
        record: A :class:`~wasm.managers.backup_manager.BackupMetadata`.

    Returns:
        The parts the archive carries, application files always first.
    """
    contents = ["files"]
    if record.includes_env:
        contents.append("env")
    if record.includes_databases:
        contents.append("databases")
    if record.includes_docker_volumes:
        contents.append("volumes")
    if record.includes_node_modules:
        contents.append("node_modules")
    if record.includes_build:
        contents.append("build")
    return contents


# ---------------------------------------------------------------- activity


def job_rows() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """
    Read what this machine is doing and what it just did.

    Returns:
        The jobs in flight and the finished ones, newest first.
    """
    from wasm.web.jobs import get_job_manager

    manager = get_job_manager()
    running = [_shape_job(job) for job in manager.get_active_jobs()]
    active_ids = {row["id"] for row in running}
    recent = [
        _shape_job(job)
        for job in manager.get_all_jobs(limit=RECENT_JOBS)
        if job.id not in active_ids
    ]
    return running, recent


def jobs_for_domain(domain: str, limit: int = APP_JOBS) -> list[dict[str, Any]]:
    """
    Read the jobs that touched one domain.

    Args:
        domain: The domain to filter on.
        limit: Most jobs to return.

    Returns:
        The matching jobs, newest first.
    """
    from wasm.web.jobs import get_job_manager

    jobs = get_job_manager().get_all_jobs(limit=RECENT_JOBS)
    matching = [job for job in jobs if job.metadata.get("domain") == domain]
    return [_shape_job(job) for job in matching[:limit]]


def running_job_count() -> int | None:
    """
    Count the jobs currently queued or running.

    Returns:
        The count, or None when the job manager cannot be reached.
    """
    from wasm.web.jobs import get_job_manager

    try:
        return len(get_job_manager().get_active_jobs())
    except RuntimeError as exc:  # pragma: no cover - the worker refused to start
        log.warning("Could not count running jobs: %s", exc)
        return None


def _shape_job(job: Any) -> dict[str, Any]:
    """
    Turn a job into a row context, with its output.

    Args:
        job: A :class:`~wasm.web.jobs.Job`.

    Returns:
        A row context. ``error`` and ``output`` are the tool's own words and
        are never rewritten; the template puts the fix above them.
    """
    started = job.started_at
    finished = job.completed_at
    state = _JOB_STATES.get(job.status.value, "idle")
    resource = str(job.metadata.get("domain") or "—")

    return {
        "id": job.id,
        "name": job.name,
        "description": job.description,
        "state": state,
        "status": job.status.value,
        "type": job.type.value,
        "resource": resource,
        "progress": job.progress,
        "total_steps": job.total_steps,
        "current_step": job.current_step or "—",
        "queued": since(job.created_at),
        "started": since(started) if started else "not started",
        "finished": since(finished) if finished else "—",
        "error": job.error or "",
        "output": _job_output(job),
        "fix": _job_fix(job),
        "cancel_endpoint": f"/api/jobs/{job.id}/cancel",
        "log_url": f"/ws/jobs/{job.id}",
    }


def _job_output(job: Any) -> str:
    """
    Render the tail of a job's log.

    Args:
        job: A :class:`~wasm.web.jobs.Job`.

    Returns:
        One line per entry, timestamped, or an empty string when it said
        nothing.
    """
    return "\n".join(
        f"{entry.timestamp:%H:%M:%S} {entry.message}" for entry in job.logs[-JOB_LOG_TAIL:]
    )


def _job_fix(job: Any) -> str:
    """
    Say what to do about a failed job, above its own output.

    Args:
        job: A :class:`~wasm.web.jobs.Job`.

    Returns:
        The line shown above the verbatim output, empty when the job did not
        fail.
    """
    if job.status.value != "failed":
        return ""
    step = job.current_step or "an early step"
    return f"{job.name} stopped at {step}. Fix what the output below reports, then run it again."


# ---------------------------------------------------------------- settings


def settings_sections() -> tuple[list[dict[str, Any]], dict[str, str] | None]:
    """
    Read the stored configuration with every secret replaced.

    The panel holds root over this machine, so the configuration screen is one
    request away from being a credential dump. Everything shown here has been
    through :func:`~wasm.core.config.redact_secrets` first, and a redacted
    field keeps the placeholder rather than going blank, because a blank field
    reads as "nothing is set" and invites someone to overwrite a working
    credential with an empty string.

    Returns:
        The sections, and a problem to show instead when the configuration
        cannot be read.
    """
    from wasm.core.config import DEFAULT_CONFIG_PATH, Config, redact_secrets
    from wasm.core.exceptions import WASMError

    try:
        stored = redact_secrets(Config().to_dict())
    except (OSError, WASMError) as exc:
        return [], {
            "fix": (
                f"The configuration at {DEFAULT_CONFIG_PATH} could not be read. "
                "Check that the file is valid YAML and readable by root."
            ),
            "output": str(exc),
        }

    scalars = {key: value for key, value in stored.items() if not isinstance(value, dict)}
    sections: list[dict[str, Any]] = []
    if scalars:
        sections.append({"name": "general", "entries": list(_flatten("", scalars))})
    for name in sorted(key for key, value in stored.items() if isinstance(value, dict)):
        sections.append({"name": name, "entries": list(_flatten(name, stored[name]))})
    return sections, None


def _flatten(prefix: str, node: Mapping[str, Any]) -> Iterator[dict[str, Any]]:
    """
    Walk a configuration subtree into dotted key and value pairs.

    Args:
        prefix: Dotted path of ``node``, empty at the top level.
        node: The subtree to walk.

    Yields:
        One entry per leaf, in the order the file holds them.
    """
    for key, value in node.items():
        dotted = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(value, Mapping):
            yield from _flatten(dotted, value)
        else:
            yield {
                "key": dotted,
                "value": _render_setting(value),
                "redacted": value == REDACTED,
            }


def _render_setting(value: Any) -> str:
    """
    Render a configuration value as a system tool would print it.

    Args:
        value: The stored value.

    Returns:
        Its text form. An unset value is an em dash, never an empty cell,
        so a missing setting cannot be mistaken for a hidden one.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None or value == "":
        return "—"
    if isinstance(value, (list, tuple)):
        return ", ".join(str(item) for item in value) if value else "—"
    return str(value)


# ------------------------------------------------------------- application


def application_detail(domain: str) -> dict[str, Any] | None:
    """
    Read everything one application's page shows.

    Args:
        domain: The application's domain.

    Returns:
        The page context, or None when no such application is deployed.
    """
    from wasm.core.store import get_store

    store = get_store()
    app = store.get_app(domain)
    if app is None:
        return None

    service = store.get_service_by_app_id(app.id) if app.id else None
    site = store.get_site_by_app_id(app.id) if app.id else None

    source = app.source or "—"
    if app.branch:
        source = f"{source} ({app.branch})"

    facts = [
        ("type", app.app_type),
        ("port", str(app.port) if app.port else "—"),
        ("path", app.app_path or "—"),
        ("source", source),
        ("web server", app.webserver),
        ("last deploy", since(app.deployed_at) if app.deployed_at else "never"),
        ("created", since(app.created_at) if app.created_at else "—"),
    ]

    certificate = [
        ("https", "enabled" if app.ssl_enabled else "disabled"),
        ("certificate", app.ssl_certificate or "—"),
        ("key", app.ssl_key or "—"),
    ]

    return {
        "domain": app.domain,
        "state": app.status,
        "status": app.status,
        "facts": facts,
        "certificate": certificate,
        "service": _service_facts(service),
        "service_name": service.name if service else None,
        "site": _site_facts(site),
        "env": redact_env(app.env_vars),
        "jobs": jobs_for_domain(app.domain),
        "log_url": f"/ws/logs/{app.domain}",
        "start_endpoint": f"/api/apps/{app.domain}/start",
        "stop_endpoint": f"/api/apps/{app.domain}/stop",
        "restart_endpoint": f"/api/apps/{app.domain}/restart",
        "delete_endpoint": f"/api/apps/{app.domain}",
        "delete_consequence": (
            "The service, the web server configuration and the deployed files "
            "are removed. This cannot be undone."
        ),
    }


def _service_facts(service: Any) -> list[tuple[str, str]]:
    """
    Describe the systemd unit behind an application.

    Args:
        service: The store record, or None when there is no unit.

    Returns:
        Label and value pairs, empty when there is no unit.
    """
    if service is None:
        return []
    return [
        ("unit", service.name),
        ("status", service.status),
        ("enabled", "yes" if service.enabled else "no"),
        ("user", f"{service.user}:{service.group}"),
        ("working directory", service.working_directory or "—"),
        ("command", service.command or "—"),
    ]


def _site_facts(site: Any) -> list[tuple[str, str]]:
    """
    Describe the web server configuration in front of an application.

    Args:
        site: The store record, or None when there is no site.

    Returns:
        Label and value pairs, empty when there is no site.
    """
    if site is None:
        return []
    return [
        ("server", site.webserver),
        ("enabled", "yes" if site.enabled else "no"),
        ("config", site.config_path or "—"),
        ("document root", site.document_root or "—"),
        ("proxy port", str(site.proxy_port) if site.proxy_port else "—"),
    ]


def redact_env(env: Mapping[str, str] | None) -> list[dict[str, Any]]:
    """
    Render an application's environment with its secrets hidden.

    Two passes, because one is not enough. :func:`~wasm.core.config.redact_secrets`
    works on key names, so ``API_KEY`` disappears but ``DATABASE_URL`` does
    not, and a connection string carries the password in the middle of the
    value. The second pass masks the credentials inside any URL that survived.

    Args:
        env: The stored environment, or None.

    Returns:
        One entry per variable, sorted by name, each saying whether it was
        redacted so the screen can show a hidden value as hidden.
    """
    from wasm.core.config import redact_secrets

    if not env:
        return []

    entries: list[dict[str, Any]] = []
    for name, raw in sorted(redact_secrets(dict(env)).items()):
        value = str(raw)
        masked = _URL_CREDENTIALS.sub(rf"\g<prefix>{REDACTED}\g<at>", value)
        entries.append(
            {
                "name": name,
                "value": masked if masked != value else value,
                "redacted": value == REDACTED or masked != value,
            }
        )
    return entries
