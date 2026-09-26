# Copyright (c) 2024-2026 Yago Lopez Prado
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
The one implementation of "the names an application answers on".

``wasm domain`` and ``/api/apps/{domain}/domains`` are presentation over the
functions here. Each change goes through the same four places, in this order:

1. **The store**, which refuses a name that is not a domain, that belongs to
   another application, or that would be a second primary
   (:meth:`wasm.core.store.WASMStore.add_domain`).
2. **The site**, rendered again by the application's own deployer
   (:meth:`wasm.deployers.base.BaseDeployer.refresh_site`), exactly as a deploy
   renders it. The web server reads the names from the store where it writes
   the file, so a redeploy later renders the same names again. The whole
   configuration is tested before the reload, and a change the web server
   refuses is put back - the row and the file both.
3. **The certificate**, expanded under the same lineage to cover every name,
   redirects included (:meth:`wasm.managers.cert_manager.CertManager.obtain`
   reads the same rows). Only for an application that already serves TLS; a
   removed name is never revoked, the lineage simply keeps covering it.
4. **Nothing else.** The unit, the build and the release are not touched.

Applications deployed before domains had rows get only their primary from the
store migration, which may not read the web server's files. The first change
made here adopts, as aliases, whatever else the live site answered on - the
``www`` a 1.x ``--www`` deploy served - so adding one name never silently
drops another.

:func:`check_dns` answers the question to ask before a certificate is ordered:
does this name resolve to this machine?
"""

from __future__ import annotations

import ipaddress
import socket
import sqlite3
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from wasm.core.exceptions import (
    CertificateError,
    DependencyError,
    DeploymentError,
    DomainConflictError,
    DomainError,
    ValidationError,
    WASMError,
)
from wasm.core.logger import Logger
from wasm.core.store import App, DomainKind, DomainRecord, get_store
from wasm.deployers.base import BaseDeployer
from wasm.deployers.helpers.layout import app_root
from wasm.deployers.registry import get_deployer
from wasm.validators.domain import validate_domain

#: What can fail while a change is applied, and is undone before it is raised.
_APPLY_ERRORS = (WASMError, OSError, sqlite3.Error)

#: The kinds a name can be added as. The primary is the application itself.
ADDABLE_KINDS: tuple[str, ...] = (DomainKind.ALIAS.value, DomainKind.REDIRECT.value)

#: Resolves a host the way :func:`socket.getaddrinfo` does.
Resolver = Callable[..., Sequence[tuple[Any, ...]]]

#: IP address, either family.
_Address = ipaddress.IPv4Address | ipaddress.IPv6Address


@dataclass(frozen=True)
class DomainChange:
    """
    What a change to an application's domains did.

    Attributes:
        app: The application's primary domain.
        domains: Every domain it answers on afterwards, primary first.
        tls: Whether its site serves TLS. A name added to a site that does
            not is served over plain HTTP, like the rest of it.
        adopted: Names the live site already answered on that had no row,
            recorded as aliases before the change (see the module docs).
        certificate_issued: Whether the certificate was confirmed to cover
            every domain, ordering an expansion when it did not.
        certificate_error: Why it was not, in certbot's own words, when an
            order was attempted and failed. The domain stays; the site keeps
            serving TLS with the certificate it had.
    """

    app: str
    domains: tuple[DomainRecord, ...]
    tls: bool
    adopted: tuple[str, ...] = ()
    certificate_issued: bool = False
    certificate_error: str | None = None


@dataclass(frozen=True)
class DnsCheck:
    """
    Whether a name resolves to this machine.

    Attributes:
        domain: The name that was resolved.
        expected_addresses: This machine's addresses: every address on a
            non-loopback interface, sorted.
        resolved_addresses: What the name resolves to (A and AAAA), sorted.
            Empty when it does not resolve.
        points_here: True when it resolves and every address it resolves to
            is one of this machine's. An AAAA record pointing elsewhere makes
            it False: Let's Encrypt prefers IPv6, and so do most browsers.
    """

    domain: str
    expected_addresses: tuple[str, ...]
    resolved_addresses: tuple[str, ...]
    points_here: bool


def list_domains(app_domain: str) -> list[DomainRecord]:
    """
    List the domains an application answers on.

    Args:
        app_domain: The application's primary domain.

    Returns:
        Its domains, primary first, then aliases, then redirects.

    Raises:
        WASMError: When no application is deployed at ``app_domain``.
    """
    app = _application(app_domain)
    return get_store().list_domains(app.domain)


def add_domain(
    app_domain: str,
    domain: str,
    kind: str = DomainKind.ALIAS.value,
    *,
    issue_cert: bool = True,
    logger: Logger | None = None,
    verbose: bool = False,
) -> DomainChange:
    """
    Make an application answer on another name.

    Args:
        app_domain: The application's primary domain.
        domain: The name to add.
        kind: ``alias`` to serve the application on it, ``redirect`` to send
            it permanently to the primary.
        issue_cert: Expand the certificate to cover the name, when the site
            serves TLS. False leaves the certificate alone: the name is served
            with a certificate that does not cover it until
            :func:`issue_certificate` runs, or this is called again.
        logger: Where progress and warnings go.
        verbose: Verbosity of the managers.

    Returns:
        What was done.

    Raises:
        WASMError: When the application is unknown.
        DomainError: When the name is not a domain or ``kind`` is ``primary``.
        DomainConflictError: When the name belongs to another application,
            to this one in another role, or has a site of its own. Adding a
            name this application already has in the same role is not an
            error: the site and the certificate are brought up to date again.
        ValidationError: When the application's type writes its own web
            server configuration (monorepo, docker-compose), or the web
            server refuses the new configuration; nothing is left changed:
            the row and the file are put back.
    """
    log = logger or Logger(verbose=verbose)
    app = _application(app_domain)
    name = validate_domain(domain)
    deployer = _site_deployer(app, verbose=verbose)
    store = get_store()

    if name != app.domain and deployer.webserver_manager().site_exists(name):
        raise DomainConflictError(
            f"{name} already has a site of its own",
            details=f"Delete it first if {app.domain} should answer on it: wasm site delete {name}",
        )

    tls = _serves_tls(app, deployer)
    # Adding a name the application already has, in the same role, is how a
    # certificate order that failed is retried once DNS points here: the site
    # is rendered and the certificate ordered again, and the row is kept.
    retry = any(r.domain == name and r.kind == kind for r in store.list_domains(app.domain))
    if not retry:
        # The row first: the store is what refuses a bad name, a bad kind or
        # a taken one, and the name must be known before the live names are
        # adopted, or re-adding as a redirect the www a 1.x site served would
        # find it already adopted as an alias.
        store.add_domain(app.domain, name, kind)
    try:
        adopted = _adopt_served_names(app, deployer, log)
        deployer.refresh_site(with_ssl=tls)
    except _APPLY_ERRORS:
        if not retry:
            store.remove_domain(app.domain, name)
        raise
    log.success(f"{app.domain} answers on {name} ({kind})")

    issued, error = False, None
    if tls and issue_cert:
        issued, error = _try_certificate(deployer, log)
    return _change(app, tls, adopted, issued, error)


def remove_domain(
    app_domain: str,
    domain: str,
    *,
    logger: Logger | None = None,
    verbose: bool = False,
) -> DomainChange:
    """
    Stop an application answering on a name.

    The certificate is left alone: it keeps covering the name until it is
    next ordered with a different set, and nothing is revoked.

    Args:
        app_domain: The application's primary domain.
        domain: The alias or redirect to remove.
        logger: Where progress and warnings go.
        verbose: Verbosity of the managers.

    Returns:
        What was done.

    Raises:
        WASMError: When the application is unknown.
        DomainError: When the name is the primary, or not one of the
            application's domains.
        ValidationError: When the application's type writes its own web
            server configuration, or the web server refuses the new
            configuration; the row and the file are put back.
    """
    log = logger or Logger(verbose=verbose)
    app = _application(app_domain)
    name = domain.strip().lower()
    deployer = _site_deployer(app, verbose=verbose)
    store = get_store()

    tls = _serves_tls(app, deployer)
    adopted = _adopt_served_names(app, deployer, log)
    records = {record.domain: record for record in store.list_domains(app.domain)}
    if not store.remove_domain(app.domain, name):
        raise DomainError(
            f"{name} is not a domain of {app.domain}",
            details=f"See the domains it answers on with: wasm domain list {app.domain}",
        )
    try:
        deployer.refresh_site(with_ssl=tls)
    except _APPLY_ERRORS:
        store.add_domain(app.domain, name, records[name].kind)
        raise
    log.success(f"{app.domain} no longer answers on {name}")
    return _change(app, tls, adopted)


def issue_certificate(
    app_domain: str, *, logger: Logger | None = None, verbose: bool = False
) -> DomainChange:
    """
    Make the certificate of an application cover every one of its domains.

    For the panel, which never runs certbot on the request path: it adds the
    domain with ``issue_cert=False`` and queues this.

    Args:
        app_domain: The application's primary domain.
        logger: Where progress goes.
        verbose: Verbosity of the managers.

    Returns:
        What was done.

    Raises:
        WASMError: When the application is unknown.
        DeploymentError: When its site does not serve TLS; there is no
            certificate to expand, and ``wasm cert create`` is how one starts.
        CertificateError: When certbot fails, carrying its output verbatim.
    """
    log = logger or Logger(verbose=verbose)
    app = _application(app_domain)
    deployer = _site_deployer(app, verbose=verbose)
    if not _serves_tls(app, deployer):
        raise DeploymentError(
            f"{app.domain} is not served over TLS",
            details=f"Obtain its first certificate with: wasm cert create -d {app.domain}",
        )
    _cover_every_domain(deployer)
    log.success(f"The certificate of {app.domain} covers every domain")
    return _change(app, True, certificate_issued=True)


def check_dns(
    domain: str,
    *,
    resolver: Resolver | None = None,
    local_addresses: Callable[[], Iterable[str]] | None = None,
) -> DnsCheck:
    """
    Resolve a name and compare it with this machine's addresses.

    Args:
        domain: The name to resolve.
        resolver: Resolves a host like :func:`socket.getaddrinfo`, which is
            the default.
        local_addresses: Lists this machine's addresses. Defaults to
            :func:`machine_addresses`.

    Returns:
        The comparison.

    Raises:
        DomainError: When the name is not a domain.
        DependencyError: When this machine's addresses cannot be listed.
    """
    name = validate_domain(domain)
    expected = _sorted_addresses((local_addresses or machine_addresses)())
    try:
        infos = (resolver or socket.getaddrinfo)(name, None, type=socket.SOCK_STREAM)
    except (OSError, UnicodeError):
        # NXDOMAIN, no records, or no resolver at all: nothing points here.
        infos = []
    resolved = _sorted_addresses(str(info[4][0]) for info in infos)
    return DnsCheck(
        domain=name,
        expected_addresses=expected,
        resolved_addresses=resolved,
        points_here=bool(resolved) and set(resolved) <= set(expected),
    )


def machine_addresses() -> tuple[str, ...]:
    """
    List the addresses of this machine's network interfaces.

    Loopback, link-local, multicast and unspecified addresses are left out:
    no public DNS record should point at them. A server behind NAT sees only
    its private address here, and a domain pointing at its public one reads
    as not pointing here.

    Returns:
        The addresses, sorted, IPv4 first.

    Raises:
        DependencyError: When psutil is not installed.
    """
    try:
        import psutil
    except ImportError as exc:
        raise DependencyError(
            "psutil is needed to list this server's addresses",
            details="Install it with: apt install python3-psutil (or pip install psutil).",
        ) from exc

    found: list[str] = []
    for entries in psutil.net_if_addrs().values():
        for entry in entries:
            if entry.family in (socket.AF_INET, socket.AF_INET6):
                found.append(str(entry.address))
    return _sorted_addresses(found)


# -- Internals ----------------------------------------------------------------


def _application(app_domain: str) -> App:
    """
    Read the row of a deployed application.

    Args:
        app_domain: Its primary domain.

    Returns:
        The row.

    Raises:
        WASMError: When nothing is deployed there.
    """
    domain = validate_domain(app_domain)
    app = get_store().get_app(domain)
    if app is None:
        raise WASMError(
            f"Application not found: {domain}",
            details="Run 'wasm list' to see what is deployed.",
        )
    return app


def _site_deployer(app: App, *, verbose: bool) -> BaseDeployer:
    """
    Build the deployer that renders an application's site, configured from its row.

    Args:
        app: The application.
        verbose: Verbosity of the deployer.

    Returns:
        The deployer.

    Raises:
        DeploymentError: When the type is unknown.
        ValidationError: When the type writes its own web server
            configuration instead of rendering it through a deployer.
    """
    try:
        deployer = get_deployer(app.app_type, verbose=verbose)
    except ValueError as exc:
        raise DeploymentError(
            f"{app.domain} has an application type WASM does not know: {app.app_type}",
            details=f"Redeploy it with an explicit type: wasm create -d {app.domain} --type ...",
        ) from exc
    if not isinstance(deployer, BaseDeployer):
        raise ValidationError(
            f"{app.app_type} applications do not support aliases and redirects yet",
            details=(
                f"{app.domain} writes the web server configuration of its services itself. "
                "Serve another name with its own application or site instead."
            ),
        )
    deployer.configure(
        app.domain,
        app.source or str(app_root(app)),
        port=app.port,
        webserver=app.webserver,
        # The certificate step is asked for explicitly; ssl only has to let it run.
        ssl=True,
        branch=app.branch,
        app_path=app_root(app),
    )
    return deployer


def _serves_tls(app: App, deployer: BaseDeployer) -> bool:
    """
    Tell whether an application's site serves TLS now.

    Args:
        app: The application.
        deployer: Its deployer.

    Returns:
        True when the site (or, without a site row, the application) records
        TLS and the certificate it would name is on disk.
    """
    site = get_store().get_site(app.domain)
    recorded = site.ssl_enabled if site is not None else app.ssl_enabled
    return bool(recorded) and deployer.has_certificate()


def _adopt_served_names(app: App, deployer: BaseDeployer, log: Logger) -> tuple[str, ...]:
    """
    Record, as aliases, the names the live site answers on and the store does not know.

    Args:
        app: The application.
        deployer: Its deployer.
        log: Where each adoption is reported.

    Returns:
        The names adopted.
    """
    store = get_store()
    known = {record.domain for record in store.list_domains(app.domain)}
    adopted: list[str] = []
    for name in deployer.webserver_manager().served_names(app.domain):
        if name in known:
            continue
        try:
            store.add_domain(app.domain, name, DomainKind.ALIAS.value)
        except DomainConflictError as exc:
            log.warning(f"The site of {app.domain} also answers on {name}, which is not its: {exc}")
            continue
        adopted.append(name)
        log.substep(f"Kept {name}, which the site already answered on, as an alias")
    return tuple(adopted)


def _try_certificate(deployer: BaseDeployer, log: Logger) -> tuple[bool, str | None]:
    """
    Expand the certificate, reporting a certbot failure instead of raising it.

    Args:
        deployer: The application's deployer.
        log: Where the failure is reported.

    Returns:
        ``(True, None)`` once it covers every domain, or ``(False, output)``
        with certbot's own words when the order failed.
    """
    try:
        _cover_every_domain(deployer)
    except CertificateError as exc:
        if exc.output and exc.details and exc.details.strip() != exc.output.strip():
            # A diagnosis (a record pointing elsewhere, say) goes above certbot's
            # own words, which stay verbatim underneath.
            output = f"{exc.message}\n{exc.details}\n\n{exc.output}"
        elif exc.output:
            output = exc.output
        else:
            output = exc.details or exc.message
        log.warning(f"The certificate was not extended: {exc.message}")
        return False, output
    return True, None


def _cover_every_domain(deployer: BaseDeployer) -> None:
    """
    Order the certificate for every domain, then load it.

    ``obtain`` returns at once when the lineage already covers them all; the
    site is reloaded either way, so an expanded certificate is served.

    Args:
        deployer: The application's deployer.

    Raises:
        CertificateError: When certbot fails.
    """
    deployer.obtain_certificate()
    deployer.refresh_site(with_ssl=True)


def _change(
    app: App,
    tls: bool,
    adopted: tuple[str, ...] = (),
    certificate_issued: bool = False,
    certificate_error: str | None = None,
) -> DomainChange:
    """
    Describe an application's domains after a change.

    Args:
        app: The application.
        tls: Whether its site serves TLS.
        adopted: Names adopted from the live site.
        certificate_issued: Whether the certificate covers every domain.
        certificate_error: Certbot's output when an order failed.

    Returns:
        The description.
    """
    return DomainChange(
        app=app.domain,
        domains=tuple(get_store().list_domains(app.domain)),
        tls=tls,
        adopted=adopted,
        certificate_issued=certificate_issued,
        certificate_error=certificate_error,
    )


def _sorted_addresses(addresses: Iterable[str]) -> tuple[str, ...]:
    """
    Parse, filter, deduplicate and sort addresses.

    Args:
        addresses: Address strings, possibly with an IPv6 ``%scope``.

    Returns:
        The usable ones - not loopback, link-local, multicast or unspecified -
        in canonical form, IPv4 first.
    """
    parsed: set[_Address] = set()
    for text in addresses:
        try:
            address = ipaddress.ip_address(text.split("%", 1)[0])
        except ValueError:
            continue
        if (
            address.is_loopback
            or address.is_link_local
            or address.is_multicast
            or address.is_unspecified
        ):
            continue
        parsed.add(address)
    ordered = sorted(parsed, key=lambda address: (address.version, int(address)))
    return tuple(str(address) for address in ordered)
