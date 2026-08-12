"""
Certificates API endpoints.

This module is a client of :class:`~wasm.managers.cert_manager.CertManager`. It
used to invoke certbot itself, which meant the panel had its own opinion about
which plugin to use, its own timeout, its own parsing of certbot's output and
no idea that the CLI records the resulting SSL state in the store. Issuing a
certificate from the panel therefore left the store believing the site was
still plain HTTP.

Two structural fixes live here as well:

- **Literal routes are registered before parametrised ones.** ``/renew-all``
  used to be declared two hundred lines after ``/{domain}`` and was
  unreachable: FastAPI matches in registration order, so every request for it
  was handled as a certificate named ``renew-all``.
- **Certbot never runs on the request path.** Issuing and renewing take ACME
  round trips measured in seconds to minutes, so they are queued on the job
  manager and the endpoint answers ``202 Accepted`` with a job id.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from wasm.managers.cert_manager import CertificateInfo, CertManager
from wasm.web.api.auth import get_current_session
from wasm.web.api.deps import JobAcceptedResponse, WASMErrorRoute, strict_domain
from wasm.web.jobs import JobType, cert_create_job, cert_renew_job, get_job_manager

router = APIRouter(route_class=WASMErrorRoute)


class CertInfo(BaseModel):
    """
    One certificate as certbot reports it.

    Attributes:
        domain: Certificate (lineage) name.
        domains: Every domain the certificate covers.
        valid_until: Raw expiry line from certbot, including its validity note.
        expires_on: Expiry date as ``YYYY-MM-DD`` when it could be parsed.
        days_remaining: Whole days until expiry, negative once expired.
        auto_renew: Whether certbot's renewal timer covers this certificate.
        path: Directory holding the certificate files.
        key_path: Path of the private key. The key itself is never read.
    """

    domain: str
    domains: list[str] = []
    valid_until: str | None = None
    expires_on: str | None = None
    days_remaining: int | None = None
    auto_renew: bool = True
    path: str | None = None
    key_path: str | None = None


class CertListResponse(BaseModel):
    """Response for listing certificates."""

    certificates: list[CertInfo]
    total: int


class CertActionResponse(BaseModel):
    """Response for certificate actions that complete immediately."""

    success: bool
    message: str
    domain: str


class CreateCertRequest(BaseModel):
    """
    Request to obtain a certificate.

    Attributes:
        email: Registration and expiry-notice address.
        webserver: Web server whose certbot plugin should be used.
        include_www: Also cover the ``www`` subdomain.
    """

    email: str | None = None
    webserver: str = "nginx"
    include_www: bool = False


class RenewCertRequest(BaseModel):
    """Request to renew certificates."""

    force: bool = False


def _days_remaining(expiry: str | None) -> int | None:
    """
    Work out how many days a certificate has left.

    Args:
        expiry: Expiry date as ``YYYY-MM-DD``, or None when certbot's output
            could not be parsed.

    Returns:
        Whole days until expiry, or None when the date is unknown.
    """
    if not expiry:
        return None
    try:
        expires = datetime.strptime(expiry, "%Y-%m-%d").date()
    except ValueError:
        return None
    return (expires - date.today()).days


def _to_cert_info(entry: CertificateInfo) -> CertInfo:
    """
    Convert a manager certificate entry into the API model.

    Args:
        entry: One entry of ``certbot certificates``.

    Returns:
        The API representation.
    """
    expiry = entry.get("expiry")
    cert_path = entry.get("cert_path")
    return CertInfo(
        domain=entry.get("name", ""),
        domains=list(entry.get("domains", [])),
        valid_until=entry.get("expiry_full"),
        expires_on=expiry,
        days_remaining=_days_remaining(expiry),
        path=cert_path,
        key_path=entry.get("key_path"),
    )


@router.get("", response_model=CertListResponse)
def list_certificates(session: Annotated[dict, Depends(get_current_session)]) -> CertListResponse:
    """
    List every certificate certbot knows about.

    Args:
        session: The authenticated session.

    Returns:
        The certificates, with days remaining computed for each.
    """
    certificates = [
        _to_cert_info(entry) for entry in CertManager(verbose=False).list_certificates()
    ]
    return CertListResponse(certificates=certificates, total=len(certificates))


@router.post("/renew-all", response_model=JobAcceptedResponse, status_code=202)
def renew_all_certificates(
    session: Annotated[dict, Depends(get_current_session)], data: RenewCertRequest | None = None
) -> JobAcceptedResponse:
    """
    Renew every certificate that is due.

    Declared before ``/{domain}`` on purpose: a parametrised route registered
    first would match ``renew-all`` as a domain and this endpoint would never
    be reached.

    Args:
        data: Renewal options.
        session: The authenticated session.

    Returns:
        The queued job.
    """
    force = data.force if data else False
    job = get_job_manager().create_job(
        job_type=JobType.CERT_RENEW,
        name="Renew all certificates",
        description="Renewing every certificate that is due",
        func=cert_renew_job,
        kwargs={"domain": None, "force": force},
        metadata={"domain": "all", "force": force},
    )
    return JobAcceptedResponse(
        job_id=job.id,
        status=job.status.value,
        message="Renewal queued",
        job=job.to_dict(),
    )


@router.get("/{domain}", response_model=CertInfo)
def get_certificate(
    domain: str, session: Annotated[dict, Depends(get_current_session)]
) -> CertInfo:
    """
    Describe one certificate.

    Args:
        domain: Certificate name.
        session: The authenticated session.

    Returns:
        The certificate description.

    Raises:
        HTTPException: 404 when no certificate covers the domain.
    """
    validated = strict_domain(domain)

    entry = CertManager(verbose=False).get_cert_info(validated)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"Certificate not found: {validated}")

    return _to_cert_info(entry)


@router.post("/{domain}", response_model=JobAcceptedResponse, status_code=202)
def create_certificate(
    domain: str,
    session: Annotated[dict, Depends(get_current_session)],
    data: CreateCertRequest | None = None,
) -> JobAcceptedResponse:
    """
    Obtain a certificate for a domain.

    Args:
        domain: Primary domain of the certificate.
        data: Issuance options.
        session: The authenticated session.

    Returns:
        The queued job.
    """
    validated = strict_domain(domain)
    options = data or CreateCertRequest()

    job = get_job_manager().create_job(
        job_type=JobType.CERT_CREATE,
        name=f"SSL for {validated}",
        description=f"Obtaining an SSL certificate for {validated}",
        func=cert_create_job,
        kwargs={
            "domain": validated,
            "email": options.email,
            "webserver": options.webserver,
            "include_www": options.include_www,
        },
        metadata={"domain": validated},
    )
    return JobAcceptedResponse(
        job_id=job.id,
        status=job.status.value,
        message=f"Certificate issuance queued for {validated}",
        job=job.to_dict(),
    )


@router.post("/{domain}/renew", response_model=JobAcceptedResponse, status_code=202)
def renew_certificate(
    domain: str,
    session: Annotated[dict, Depends(get_current_session)],
    data: RenewCertRequest | None = None,
) -> JobAcceptedResponse:
    """
    Renew one certificate.

    Args:
        domain: Certificate name.
        data: Renewal options.
        session: The authenticated session.

    Returns:
        The queued job.

    Raises:
        HTTPException: 404 when no such certificate exists.
    """
    validated = strict_domain(domain)

    manager = CertManager(verbose=False)
    if not manager.cert_exists(validated):
        raise HTTPException(status_code=404, detail=f"Certificate not found: {validated}")

    force = data.force if data else False
    job = get_job_manager().create_job(
        job_type=JobType.CERT_RENEW,
        name=f"Renew {validated}",
        description=f"Renewing the certificate for {validated}",
        func=cert_renew_job,
        kwargs={"domain": validated, "force": force},
        metadata={"domain": validated, "force": force},
    )
    return JobAcceptedResponse(
        job_id=job.id,
        status=job.status.value,
        message=f"Renewal queued for {validated}",
        job=job.to_dict(),
    )


@router.post("/{domain}/revoke", response_model=CertActionResponse)
def revoke_certificate(
    domain: str, session: Annotated[dict, Depends(get_current_session)]
) -> CertActionResponse:
    """
    Revoke a certificate and delete its files.

    Args:
        domain: Certificate name.
        session: The authenticated session.

    Returns:
        The action outcome.

    Raises:
        CertificateError: When certbot refuses the revocation.
    """
    validated = strict_domain(domain)

    CertManager(verbose=False).revoke(validated)

    return CertActionResponse(
        success=True, message=f"Certificate revoked for {validated}", domain=validated
    )


@router.delete("/{domain}", response_model=CertActionResponse)
def delete_certificate(
    domain: str, session: Annotated[dict, Depends(get_current_session)]
) -> CertActionResponse:
    """
    Delete a certificate without revoking it.

    Args:
        domain: Certificate name.
        session: The authenticated session.

    Returns:
        The action outcome.

    Raises:
        CertificateError: When certbot refuses the deletion.
    """
    validated = strict_domain(domain)

    CertManager(verbose=False).delete(validated)

    return CertActionResponse(
        success=True, message=f"Certificate deleted for {validated}", domain=validated
    )
