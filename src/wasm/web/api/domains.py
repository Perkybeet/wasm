# Copyright (c) 2024-2026 Yago Lopez Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
The domains an application answers on, over the API.

A thin translation of :mod:`wasm.deployers.domains` to HTTP - the rules, the
re-rendering and the certificate all live there, where ``wasm domain`` reads
them too. Two things are decided here because they are about HTTP:

- **Certbot never runs on the request path.** Adding a domain records it and
  re-renders the site at once; when the site serves TLS, extending the
  certificate is queued as a job and its id returned, the same way
  ``POST /api/certs/{domain}`` queues issuance.
- **Removing a domain needs sudo mode**, like every other destructive action.

Mounted under ``/apps`` beside :mod:`wasm.web.api.apps`, which owns no path
under ``/{domain}/domains``.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from wasm.core.store import DomainKind, DomainRecord, get_store
from wasm.deployers.domains import (
    DomainChange,
    add_domain,
    check_dns,
    issue_certificate,
    list_domains,
    remove_domain,
)
from wasm.web.api.auth import get_current_session
from wasm.web.api.deps import WASMErrorRoute, require_elevated, strict_domain
from wasm.web.jobs import JobContext, JobType, get_job_manager
from wasm.web.pydantic_compat import iso_offset_validator

router = APIRouter(route_class=WASMErrorRoute)


class AppDomain(BaseModel):
    """
    One domain an application answers on.

    Attributes:
        domain: The domain.
        kind: ``primary`` (the domain the application was deployed as),
            ``alias`` (served the same) or ``redirect`` (sent permanently to
            the primary).
        created_at: When it was added, ISO 8601 with an explicit UTC offset.
    """

    domain: str
    kind: str
    created_at: str | None = None

    _iso_timestamps = iso_offset_validator("created_at")


class AppDomainList(BaseModel):
    """
    Every domain an application answers on.

    Attributes:
        app: The application's primary domain.
        domains: Its domains, primary first, then aliases, then redirects.
    """

    app: str
    domains: list[AppDomain]


class AddAppDomainRequest(BaseModel):
    """
    A domain to add to an application.

    Attributes:
        domain: The bare domain name.
        kind: ``alias`` or ``redirect``.
    """

    domain: str
    kind: str = Field(default=DomainKind.ALIAS.value, description="alias or redirect")


class AppDomainChange(BaseModel):
    """
    An application's domains after a change.

    Attributes:
        app: The application's primary domain.
        domains: Its domains afterwards, primary first.
        tls: Whether its site serves TLS.
        adopted: Names the live site already answered on without a record,
            kept as aliases so the change did not drop them.
        certificate_job_id: The job extending the certificate to every
            domain, when the site serves TLS and a domain was added.
    """

    app: str
    domains: list[AppDomain]
    tls: bool
    adopted: list[str] = Field(default_factory=list)
    certificate_job_id: str | None = None


class DnsCheckResponse(BaseModel):
    """
    Whether a domain resolves to this server.

    Attributes:
        domain: The domain that was resolved.
        expected_addresses: This server's addresses.
        resolved_addresses: What the domain resolves to, A and AAAA.
        points_here: Whether every address it resolves to is this server's.
    """

    domain: str
    expected_addresses: list[str]
    resolved_addresses: list[str]
    points_here: bool


def _require_app(domain: str) -> str:
    """
    Validate an application's domain and require that it is deployed.

    Args:
        domain: The domain from the path.

    Returns:
        The validated domain.

    Raises:
        HTTPException: 404 when nothing is deployed there.
    """
    validated = strict_domain(domain)
    if get_store().get_app(validated) is None:
        raise HTTPException(status_code=404, detail=f"Application not found: {validated}")
    return validated


def _entry(record: DomainRecord) -> AppDomain:
    """
    Translate a domain record to its response model.

    Args:
        record: The store's record.

    Returns:
        The response model.
    """
    return AppDomain(domain=record.domain, kind=record.kind, created_at=record.created_at)


def _changed(change: DomainChange, job_id: str | None = None) -> AppDomainChange:
    """
    Translate the result of a change to its response model.

    Args:
        change: What the operation did.
        job_id: The certificate job queued after it, if any.

    Returns:
        The response model.
    """
    return AppDomainChange(
        app=change.app,
        domains=[_entry(record) for record in change.domains],
        tls=change.tls,
        adopted=list(change.adopted),
        certificate_job_id=job_id,
    )


def certificate_job(app_domain: str, job_context: JobContext | None = None) -> dict[str, Any]:
    """
    Extend an application's certificate to every one of its domains, as a job.

    Args:
        app_domain: The application's primary domain.
        job_context: Injected by the job manager.

    Returns:
        The application and the domains its certificate now covers.

    Raises:
        CertificateError: When certbot fails; the job fails with its output.
    """
    if job_context is not None:
        job_context.set_metadata("domain", app_domain)
        job_context.update("Extending the certificate to every domain", 20)
    change = issue_certificate(app_domain)
    if job_context is not None:
        job_context.update("Certificate covers every domain", 100)
    return {"domain": change.app, "domains": [record.domain for record in change.domains]}


@router.get("/{domain}/domains", response_model=AppDomainList)
def get_domains(
    domain: str, session: Annotated[dict[str, Any], Depends(get_current_session)]
) -> AppDomainList:
    """
    List the domains an application answers on.

    Args:
        domain: The application's primary domain.
        session: Authenticated session, injected.

    Returns:
        Its domains, primary first.
    """
    validated = _require_app(domain)
    return AppDomainList(app=validated, domains=[_entry(r) for r in list_domains(validated)])


@router.post("/{domain}/domains", response_model=AppDomainChange, status_code=201)
def post_domain(
    domain: str,
    data: AddAppDomainRequest,
    session: Annotated[dict[str, Any], Depends(get_current_session)],
) -> AppDomainChange:
    """
    Make an application answer on another domain.

    The domain is recorded and the site re-rendered and reloaded before this
    returns. When the site serves TLS, extending the certificate to the new
    domain is queued as a job; follow ``certificate_job_id``.

    Args:
        domain: The application's primary domain.
        data: The domain to add and its kind.
        session: Authenticated session, injected.

    Returns:
        The application's domains afterwards.
    """
    validated = _require_app(domain)
    name = strict_domain(data.domain)
    change = add_domain(validated, name, data.kind, issue_cert=False)

    job_id: str | None = None
    if change.tls:
        job = get_job_manager().create_job(
            job_type=JobType.CERT_CREATE,
            name=f"SSL for {validated}",
            description=f"Extending the certificate of {validated} to {name}",
            func=certificate_job,
            kwargs={"app_domain": validated},
            metadata={"domain": validated},
        )
        job_id = job.id
    return _changed(change, job_id)


@router.delete("/{domain}/domains/{name}", response_model=AppDomainChange)
def delete_domain(
    domain: str,
    name: str,
    session: Annotated[dict[str, Any], Depends(require_elevated)],
) -> AppDomainChange:
    """
    Stop an application answering on a domain.

    The site is re-rendered and reloaded before this returns. The certificate
    is left alone and nothing is revoked.

    Args:
        domain: The application's primary domain.
        name: The alias or redirect to remove.
        session: Authenticated and elevated session, injected.

    Returns:
        The application's domains afterwards.
    """
    validated = _require_app(domain)
    return _changed(remove_domain(validated, strict_domain(name)))


@router.get("/{domain}/domains/{name}/dns", response_model=DnsCheckResponse)
def get_domain_dns(
    domain: str,
    name: str,
    session: Annotated[dict[str, Any], Depends(get_current_session)],
) -> DnsCheckResponse:
    """
    Check whether a domain resolves to this server.

    Meant for before a domain is added, so the name does not have to be one
    of the application's yet.

    Args:
        domain: The application's primary domain.
        name: The domain to resolve.
        session: Authenticated session, injected.

    Returns:
        What it resolves to, compared with this server's addresses.
    """
    _require_app(domain)
    check = check_dns(strict_domain(name))
    return DnsCheckResponse(
        domain=check.domain,
        expected_addresses=list(check.expected_addresses),
        resolved_addresses=list(check.resolved_addresses),
        points_here=check.points_here,
    )
