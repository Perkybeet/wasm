"""
Shared seed data for the panel's browser check and its API/view tests.

A now-deleted ``scripts/panel_browser_check.py`` used to build its own
domains, applications, services and sites inline. ``scripts/console_server.py``
seeds a populated console the same way now, through this module instead of
repeating that setup with its own set of magic numbers, and CLAUDE.md rule 3
says there is one implementation of each thing. This module is that one
implementation.

Importable both from pytest, as ``tests.panel_factory`` (``tests/`` is a
package, and pytest puts the repository root -- the first parent directory
without an ``__init__.py`` -- on ``sys.path`` when it collects a package like
this one), and from a plain script, which is not run under pytest and so adds
the repository root to ``sys.path`` itself before importing this module. See
``scripts/console_server.py`` for that.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from wasm.core.fs import get_fs
from wasm.core.store import (
    App,
    AppStatus,
    Database,
    DatabaseEngine,
    JobRecord,
    Service,
    Site,
    WASMStore,
)

#: The domains the panel browser check has always used, in the order it
#: always created them. Seeding with ``apps=len(DEFAULT_DOMAINS)`` and every
#: other count equal to it reproduces, field for field, what the script
#: created before this factory existed.
DEFAULT_DOMAINS: tuple[str, ...] = (
    "arennalabs.com",
    "picconia.com",
    "cittek.es",
    "qrboda.com",
    "convertidordepdf.com",
    "clientes.arennalabs.com",
    "taller.arennalabs.com",
    "bodas.arennalabs.com",
)


@dataclass
class SeededState:
    """
    What :func:`seed_panel_state` put in the store.

    Attributes:
        domains: Every domain seeded, in creation order.
        app_ids: Domain to application id.
        service_domains: Domains that also got a systemd service record.
        site_domains: Domains that also got a site record.
        failed_domains: Domains whose application status was forced to
            ``"failed"``.
        cert_domains: Domains whose site carries an explicit certificate
            path. Every seeded app and site has ``ssl_enabled=True``
            regardless of this count, matching what the browser check always
            did; this only controls the additional, more specific detail of
            a populated certificate path.
        backup_domains: Domains nominated to look like they have a backup.
            Bookkeeping only: the store has no table for backups, which are
            filesystem records owned by ``BackupManager``
            (``tests/test_web_views.py``'s ``write_backup`` helper writes
            them for the one suite that exercises that page). Nothing is
            written to disk here. Kept so a caller that wants to know which
            domains were meant to have one does not have to invent its own
            numbering.
        deployment_domains: Domains that also got a small deployment
            history: an older successful deploy and a newer attempt whose
            outcome matches the application's own state, so the deployments
            screen and an application's history read like a machine that has
            been worked on.
    """

    domains: list[str] = field(default_factory=list)
    app_ids: dict[str, int] = field(default_factory=dict)
    service_domains: list[str] = field(default_factory=list)
    site_domains: list[str] = field(default_factory=list)
    failed_domains: list[str] = field(default_factory=list)
    cert_domains: list[str] = field(default_factory=list)
    backup_domains: list[str] = field(default_factory=list)
    deployment_domains: list[str] = field(default_factory=list)
    static_domains: list[str] = field(default_factory=list)
    database_names: list[str] = field(default_factory=list)


def _domain_pool(count: int) -> list[str]:
    """
    Args:
        count: How many domains are needed.

    Returns:
        ``count`` distinct domains, the realistic ones first, padded with
        synthetic ones if more are asked for than :data:`DEFAULT_DOMAINS`
        holds.
    """
    domains = list(DEFAULT_DOMAINS[:count])
    index = len(DEFAULT_DOMAINS)
    while len(domains) < count:
        domains.append(f"extra-{index}.example.com")
        index += 1
    return domains


def seed_panel_state(
    store: WASMStore,
    *,
    apps: int = 3,
    services: int = 2,
    sites: int = 2,
    certs: int = 2,
    backups: int = 1,
    failed: int = 1,
    deployments: int = 0,
) -> SeededState:
    """
    Populate a store with a machine's worth of realistic data.

    A screen with nothing on it exercises none of the layout that broke in
    past releases, so both the browser check and any test that wants a
    populated screen call this instead of building applications, units and
    sites by hand.

    Args:
        store: The store to write to.
        apps: How many applications to create.
        services: How many of them (the first ``services``, in creation
            order) also get a systemd service record.
        sites: How many of them (the first ``sites``, in creation order)
            also get a site record.
        certs: How many of them (the first ``certs``, in creation order,
            and only among those that got a site) get an explicit
            certificate path on that site.
        backups: How many of them (the first ``backups``, in creation
            order) are recorded in the result as nominally having a
            backup. See :class:`SeededState` for why nothing is written.
        failed: How many of them (the last ``failed``, in creation order)
            have their application status forced to ``"failed"`` instead of
            the running/stopped pattern every other seeded app follows.
        deployments: How many of them (the first ``deployments``, in
            creation order) also get a two-entry deployment history. The
            newer attempt fails exactly when the application itself is
            seeded failed, so the history never contradicts the state pill
            beside it.

    Returns:
        What was created.

    Raises:
        ValueError: When ``services``, ``sites``, ``certs``, ``backups``,
            ``failed`` or ``deployments`` asks for more applications than
            ``apps`` creates.
    """
    for name, value in (
        ("services", services),
        ("sites", sites),
        ("certs", certs),
        ("backups", backups),
        ("failed", failed),
        ("deployments", deployments),
    ):
        if value > apps:
            raise ValueError(f"{name}={value} cannot exceed apps={apps}")

    domains = _domain_pool(apps)
    state = SeededState(domains=list(domains))
    state.backup_domains = list(domains[:backups])
    failing = set(domains[apps - failed :]) if failed else set()
    getting_certs = set(domains[:certs])

    for index, domain in enumerate(domains):
        is_failing = domain in failing
        status = AppStatus.FAILED.value if is_failing else ("running" if index % 3 else "stopped")

        app = store.create_app(
            App(
                domain=domain,
                app_type="nextjs",
                source="https://github.com/you/app",
                port=3000 + index,
                app_path=f"/var/www/apps/{domain}",
                status=status,
                ssl_enabled=True,
            )
        )
        assert app.id is not None
        state.app_ids[domain] = app.id
        if is_failing:
            state.failed_domains.append(domain)

        if index < services:
            store.create_service(
                Service(
                    app_id=app.id,
                    name=f"wasm-{domain.replace('.', '-')}",
                    unit_file=f"/etc/systemd/system/wasm-{domain}.service",
                    working_directory=f"/var/www/apps/{domain}",
                    command="/usr/bin/node server.js",
                    status="active" if index % 3 else "inactive",
                    enabled=index % 2 == 0,
                    port=3000 + index,
                )
            )
            state.service_domains.append(domain)

        if index < sites:
            wants_cert = domain in getting_certs
            store.create_site(
                Site(
                    app_id=app.id,
                    domain=domain,
                    webserver="nginx",
                    config_path=f"/etc/nginx/sites-available/{domain}",
                    enabled=index % 4 != 0,
                    proxy_port=3000 + index,
                    ssl_enabled=True,
                    ssl_certificate=(
                        f"/etc/letsencrypt/live/{domain}/fullchain.pem" if wants_cert else None
                    ),
                    ssl_key=(f"/etc/letsencrypt/live/{domain}/privkey.pem" if wants_cert else None),
                )
            )
            state.site_domains.append(domain)
            if wants_cert:
                state.cert_domains.append(domain)

        if index < deployments:
            first = store.record_deployment_start(
                domain, "cli", git_commit="9f2c41a", git_branch="main"
            )
            # The subject git log gives the commit, as a real deploy records it.
            store.annotate_deployment(first, commit_message="Add the order history page")
            store.finish_deployment(first, "success")
            second = store.record_deployment_start(
                domain, "panel", git_commit="c07d5e3", git_branch="main"
            )
            store.annotate_deployment(second, commit_message="Upgrade Next.js to 15.2")
            if is_failing:
                store.finish_deployment(
                    second,
                    "failed",
                    error="npm ERR! code ELIFECYCLE\nnpm ERR! errno 1\n"
                    "npm ERR! app@1.4.2 build: `next build`\n"
                    "npm ERR! Exit status 1",
                )
            else:
                store.finish_deployment(second, "success")
            state.deployment_domains.append(domain)

    return state


#: A build log as the deploy pipeline captures it: one line per step, the
#: tools' own words, ANSI colour included, so the console's log viewer has the
#: real thing to render.
_BUILD_LOG = (
    "==> Fetching https://github.com/you/app (main)\n"
    "HEAD is now at {commit} Update dependencies\n"
    "==> Installing dependencies\n"
    "added 812 packages, and audited 813 packages in 14s\n"
    "\x1b[33mnpm WARN\x1b[0m deprecated inflight@1.0.6: This module is not supported\n"
    "==> Building\n"
    "   \x1b[1mNext.js 15.2.4\x1b[0m\n"
    "   Creating an optimized production build ...\n"
    " \x1b[32m✓\x1b[0m Compiled successfully in 21.4s\n"
    "{outcome}"
)

_SUCCESS_TAIL = (
    "==> Activating release {commit}\n"
    "==> Health check passed: GET http://127.0.0.1:{port}/ answered 200 in 84 ms\n"
    "Deployed {domain}\n"
)

_FAILURE_TAIL = (
    "\x1b[31mnpm ERR!\x1b[0m code ELIFECYCLE\n"
    "\x1b[31mnpm ERR!\x1b[0m errno 1\n"
    "\x1b[31mnpm ERR!\x1b[0m app@1.4.2 build: `next build`\n"
    "\x1b[31mnpm ERR!\x1b[0m Exit status 1\n"
    "Deploy failed; the previous release keeps serving {domain}\n"
)


def seed_console_state(store: WASMStore) -> SeededState:
    """
    Populate a store with every state the console has to draw.

    :func:`seed_panel_state` gives every application a Next.js unit; the
    console also has to show static sites, captured build logs, databases
    and a job history, so this builds on it rather than beside it: six
    applications with units (running, stopped and one failed, each with a
    deployment history), then two static sites with no unit at all, then the
    rest. Build logs are written where
    :class:`wasm.deployers.recorder.DeploymentRecorder` writes them, next to
    the store, through the filesystem seam.

    Args:
        store: The store to write to. Its directory also receives the
            captured build logs.

    Returns:
        What was created.
    """
    state = seed_panel_state(
        store, apps=6, services=6, sites=6, certs=4, backups=3, failed=1, deployments=6
    )

    for domain in DEFAULT_DOMAINS[6:8]:
        app = store.create_app(
            App(
                domain=domain,
                app_type="static",
                source="https://github.com/you/landing",
                port=None,
                # What a real static deploy records (no start command): the state
                # resolver reads it and never asks systemd about a unit that does
                # not exist, so the site reads Static rather than Stopped.
                is_static=True,
                app_path=f"/var/www/apps/{domain}",
                status=AppStatus.RUNNING.value,
                ssl_enabled=True,
            )
        )
        assert app.id is not None
        state.domains.append(domain)
        state.app_ids[domain] = app.id
        state.static_domains.append(domain)
        store.create_site(
            Site(
                app_id=app.id,
                domain=domain,
                webserver="nginx",
                config_path=f"/etc/nginx/sites-available/{domain}",
                enabled=True,
                proxy_port=None,
                ssl_enabled=True,
                ssl_certificate=f"/etc/letsencrypt/live/{domain}/fullchain.pem",
                ssl_key=f"/etc/letsencrypt/live/{domain}/privkey.pem",
            )
        )
        state.site_domains.append(domain)
        state.cert_domains.append(domain)

    log_root = store.db_path.parent / "deploy-logs"
    fs = get_fs()
    for index, domain in enumerate(state.deployment_domains):
        directory = log_root / domain
        fs.make_dir(directory, mode=0o700, parents=True)
        for record in store.list_deployments(domain):
            assert record.id is not None
            commit = record.git_commit or "0000000"
            tail = _FAILURE_TAIL if record.status == "failed" else _SUCCESS_TAIL
            content = _BUILD_LOG.format(
                commit=commit,
                outcome=tail.format(commit=commit, port=3000 + index, domain=domain),
            )
            path = directory / f"{record.id}.log"
            fs.write_text(path, content, mode=0o600)
            store.annotate_deployment(record.id, log_path=str(path))

    first, second = state.domains[0], state.domains[1]
    for name, engine, port, owner in (
        ("arennalabs_production", DatabaseEngine.POSTGRESQL.value, 5432, first),
        ("picconia_wp", DatabaseEngine.MYSQL.value, 3306, second),
        ("sessions", DatabaseEngine.REDIS.value, 6379, None),
    ):
        store.create_database(
            Database(
                app_id=state.app_ids[owner] if owner else None,
                name=name,
                engine=engine,
                port=port,
                username=None if engine == DatabaseEngine.REDIS.value else name,
            )
        )
        state.database_names.append(name)

    now = datetime.now()
    # Worded the way the job manager words them (web/api/jobs.py), and started by
    # whoever starts such jobs on a real machine: the console, a token, the timer.
    for offset, (job_type, status, domain, error, description, actor) in enumerate(
        (
            ("deploy", "completed", first, None, f"Deploying {first}", "master"),
            (
                "backup",
                "completed",
                second,
                None,
                f"Creating a backup of {second}",
                "token:nightly-backup",
            ),
            ("cert_renew", "completed", "all", None, "Renewing every certificate due", None),
            (
                "update",
                "failed",
                state.failed_domains[0],
                "npm ERR! code ELIFECYCLE",
                f"Updating the application at {state.failed_domains[0]}",
                "webhook",
            ),
        )
    ):
        started = now - timedelta(hours=offset + 1)
        store.create_job(
            JobRecord(
                id=f"seed{offset:04d}",
                type=job_type,
                name=f"{job_type.replace('_', ' ').capitalize()} {domain}",
                description=description,
                actor=actor,
                status=status,
                progress=100,
                domain=domain,
                error=error,
                created_at=started.isoformat(),
                started_at=started.isoformat(),
                finished_at=(started + timedelta(minutes=2)).isoformat(),
            )
        )

    return state
