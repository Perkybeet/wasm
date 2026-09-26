# Copyright (c) 2024-2026 Yago Lopez Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
``wasm domain``: the names an application answers on.

A presentation layer over :mod:`wasm.deployers.domains`, the functions the
panel's ``/api/apps/{domain}/domains`` endpoints call too. An alias serves the
application exactly like its primary domain; a redirect sends visitors to the
primary with a permanent redirect. Every change re-renders the site the way a
deploy does, is tested by the web server before it is reloaded, and extends
the certificate when the site serves TLS. ``wasm create --www`` records
``www.<domain>`` as a redirect.

An application deployed before 2.0 starts with only its primary domain on
record: the ``www`` a 1.x ``--www`` deploy served lived only in the web
server's configuration. The first change made here keeps it, recording it as
an alias, and ``wasm domain list`` shows it from then on.
"""

from __future__ import annotations

import json

import click

from wasm.cli.app import Context, WasmGroup, json_option, pass_context
from wasm.core.exceptions import DependencyError
from wasm.core.logger import Logger
from wasm.core.store import DomainRecord
from wasm.deployers.domains import (
    ADDABLE_KINDS,
    DomainChange,
    add_domain,
    check_dns,
    list_domains,
    remove_domain,
)


def print_domains(logger: Logger, domains: list[DomainRecord]) -> None:
    """
    Render an application's domains for a human.

    Args:
        logger: Logger the command writes through.
        domains: Its domains, primary first.
    """
    logger.table(
        ["Domain", "Kind", "Added"],
        [[record.domain, record.kind, record.created_at or "-"] for record in domains],
    )


def _report(logger: Logger, change: DomainChange) -> None:
    """
    Say what a change adopted, and what the certificate now covers.

    Args:
        logger: Logger the command writes through.
        change: What the operation did.
    """
    for name in change.adopted:
        logger.info(f"{name} was already served by the site and is now recorded as an alias")
    if change.certificate_issued:
        logger.success("The certificate covers every domain")
    logger.blank()
    print_domains(logger, list(change.domains))


def _warn_about_dns(logger: Logger, domain: str) -> None:
    """
    Warn, before a name is added, when it does not resolve to this server.

    Only a warning: the operator may be about to change the DNS record, and a
    server behind NAT cannot see its public address to compare.

    Args:
        logger: Logger the command writes through.
        domain: The name about to be added.
    """
    try:
        dns = check_dns(domain)
    except DependencyError as exc:
        logger.debug(f"DNS check skipped: {exc}")
        return
    if dns.points_here:
        return
    logger.warning(f"{dns.domain} does not resolve to this server")
    logger.key_value("It resolves to", ", ".join(dns.resolved_addresses) or "nothing")
    logger.key_value("This server", ", ".join(dns.expected_addresses) or "no address found")
    logger.info("Visitors and the certificate authority reach it only once DNS points here.")


@click.group("domain", cls=WasmGroup)
def cli() -> None:
    """
    Serve an application on more domains: aliases and redirects.

    An alias serves the application like its primary domain; a redirect sends
    visitors to the primary with a permanent redirect. 'wasm create --www'
    records www.<domain> as a redirect.

    An application deployed before 2.0 starts with only its primary domain on
    record. If it was deployed with --www, the first 'wasm domain add' or
    'remove' keeps www, recording it as an alias.
    """


@cli.command("list")
@click.argument("app_domain", metavar="APP")
@json_option("Print the domains as JSON.")
@pass_context
def list_command(ctx: Context, app_domain: str) -> None:
    """
    List the domains APP answers on.

    The primary domain comes first, then the aliases, then the redirects.
    """
    domains = list_domains(app_domain)
    if ctx.json_output:
        click.echo(
            json.dumps(
                {
                    "app": app_domain,
                    "items": [
                        {"domain": r.domain, "kind": r.kind, "created_at": r.created_at}
                        for r in domains
                    ],
                }
            )
        )
        return
    print_domains(ctx.logger, domains)


@cli.command("add")
@click.argument("app_domain", metavar="APP")
@click.argument("domain")
@click.option(
    "--kind",
    type=click.Choice(ADDABLE_KINDS),
    default=ADDABLE_KINDS[0],
    show_default=True,
    help="alias serves the application on DOMAIN; redirect sends DOMAIN to APP.",
)
@click.option(
    "--no-cert",
    is_flag=True,
    help="Do not extend the certificate now. Run the same command again to extend it.",
)
@pass_context
def add_command(ctx: Context, app_domain: str, domain: str, kind: str, no_cert: bool) -> None:
    """
    Make APP answer on DOMAIN too.

    The site is re-rendered and tested before the web server reloads it. When
    APP is served over TLS, its certificate is extended to cover DOMAIN; if
    that fails, DOMAIN stays and the command can be run again once DNS points
    here. Adding a name APP already has retries its certificate.
    """
    logger = ctx.logger
    _warn_about_dns(logger, domain)
    change = add_domain(
        app_domain, domain, kind, issue_cert=not no_cert, logger=logger, verbose=ctx.verbose
    )
    if change.certificate_error is not None:
        logger.error(
            "The certificate was not extended to cover it", details=change.certificate_error
        )
        logger.info(f"Once DNS points here, run again: wasm domain add {change.app} {domain}")
    elif change.tls and no_cert:
        logger.info(f"Extend the certificate later with: wasm domain add {change.app} {domain}")
    _report(logger, change)


@cli.command("remove")
@click.argument("app_domain", metavar="APP")
@click.argument("domain")
@pass_context
def remove_command(ctx: Context, app_domain: str, domain: str) -> None:
    """
    Stop APP answering on DOMAIN.

    The primary domain cannot be removed; delete the application instead. The
    certificate is not revoked: it keeps covering DOMAIN until it is next
    issued.
    """
    logger = ctx.logger
    change = remove_domain(app_domain, domain, logger=logger, verbose=ctx.verbose)
    if change.tls:
        logger.info("The certificate was not revoked; it covers the name until it is next issued")
    _report(logger, change)
