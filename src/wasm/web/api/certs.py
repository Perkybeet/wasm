"""
Certificates API endpoints.

Provides endpoints for managing SSL certificates.
"""

import subprocess
from pathlib import Path
from typing import List, Optional
from datetime import datetime

from fastapi import APIRouter, Request, HTTPException, Depends
from pydantic import BaseModel

from wasm.web.api.auth import get_current_session

router = APIRouter()

LETSENCRYPT_LIVE = Path("/etc/letsencrypt/live")


class CertInfo(BaseModel):
    """Certificate information."""
    domain: str
    issuer: Optional[str] = None
    valid_from: Optional[str] = None
    valid_until: Optional[str] = None
    days_remaining: Optional[int] = None
    auto_renew: bool = True
    path: Optional[str] = None


class CertListResponse(BaseModel):
    """Response for listing certificates."""
    certificates: List[CertInfo]
    total: int


class CertActionResponse(BaseModel):
    """Response for certificate actions."""
    success: bool
    message: str
    domain: str


def _get_cert_info(domain: str) -> Optional[CertInfo]:
    """Get certificate information for a domain."""
    cert_path = LETSENCRYPT_LIVE / domain / "cert.pem"
    
    if not cert_path.exists():
        return None
    
    try:
        # Use openssl to get cert info
        result = subprocess.run(
            ["openssl", "x509", "-in", str(cert_path), "-noout", "-dates", "-issuer"],
            capture_output=True,
            text=True,
            timeout=10
        )
        
        if result.returncode != 0:
            return CertInfo(
                domain=domain,
                path=str(cert_path.parent)
            )
        
        output = result.stdout
        
        # Parse dates
        valid_from = None
        valid_until = None
        days_remaining = None
        issuer = None
        
        for line in output.split("\n"):
            if line.startswith("notBefore="):
                valid_from = line.split("=", 1)[1].strip()
            elif line.startswith("notAfter="):
                valid_until = line.split("=", 1)[1].strip()
                # Calculate days remaining
                try:
                    # Parse date like "Dec 19 12:00:00 2025 GMT"
                    exp_date = datetime.strptime(valid_until, "%b %d %H:%M:%S %Y %Z")
                    days_remaining = (exp_date - datetime.now()).days
                except Exception:
                    pass
            elif line.startswith("issuer="):
                issuer = line.split("=", 1)[1].strip()
        
        return CertInfo(
            domain=domain,
            issuer=issuer,
            valid_from=valid_from,
            valid_until=valid_until,
            days_remaining=days_remaining,
            auto_renew=True,
            path=str(cert_path.parent)
        )
    except Exception:
        return CertInfo(
            domain=domain,
            path=str(cert_path.parent) if cert_path.exists() else None
        )


@router.get("", response_model=CertListResponse)
async def list_certificates(
    request: Request,
    session: dict = Depends(get_current_session)
):
    """
    List all SSL certificates.
    """
    certificates = []
    
    if LETSENCRYPT_LIVE.exists():
        for domain_dir in LETSENCRYPT_LIVE.iterdir():
            if domain_dir.is_dir() and not domain_dir.name.startswith("."):
                cert_info = _get_cert_info(domain_dir.name)
                if cert_info:
                    certificates.append(cert_info)
    
    return CertListResponse(
        certificates=certificates,
        total=len(certificates)
    )


@router.get("/{domain}", response_model=CertInfo)
async def get_certificate(
    domain: str,
    request: Request,
    session: dict = Depends(get_current_session)
):
    """
    Get certificate details for a domain.
    """
    cert_info = _get_cert_info(domain)
    
    if not cert_info:
        raise HTTPException(status_code=404, detail=f"Certificate not found: {domain}")
    
    return cert_info


@router.post("/{domain}", response_model=CertActionResponse)
async def create_certificate(
    domain: str,
    request: Request,
    session: dict = Depends(get_current_session)
):
    """
    Create/obtain a new SSL certificate for a domain.
    """
    from wasm.validators.domain import validate_domain
    
    try:
        validated_domain = validate_domain(domain)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    
    try:
        # Check if certbot is available
        result = subprocess.run(
            ["which", "certbot"],
            capture_output=True
        )
        if result.returncode != 0:
            raise HTTPException(
                status_code=500,
                detail="Certbot is not installed. Run: apt install certbot python3-certbot-nginx"
            )
        
        # Run certbot
        result = subprocess.run(
            [
                "certbot", "certonly",
                "--nginx",
                "-d", validated_domain,
                "--non-interactive",
                "--agree-tos",
                "--register-unsafely-without-email"
            ],
            capture_output=True,
            text=True,
            timeout=120
        )
        
        if result.returncode != 0:
            raise HTTPException(
                status_code=500,
                detail=f"Certbot failed: {result.stderr}"
            )
        
        return CertActionResponse(
            success=True,
            message=f"Certificate obtained for {domain}",
            domain=domain
        )
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=500, detail="Certificate request timed out")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/{domain}/renew", response_model=CertActionResponse)
async def renew_certificate(
    domain: str,
    request: Request,
    session: dict = Depends(get_current_session)
):
    """
    Renew an existing SSL certificate.
    """
    cert_info = _get_cert_info(domain)
    
    if not cert_info:
        raise HTTPException(status_code=404, detail=f"Certificate not found: {domain}")
    
    try:
        result = subprocess.run(
            ["certbot", "renew", "--cert-name", domain, "--force-renewal"],
            capture_output=True,
            text=True,
            timeout=120
        )
        
        if result.returncode != 0:
            raise HTTPException(
                status_code=500,
                detail=f"Renewal failed: {result.stderr}"
            )
        
        return CertActionResponse(
            success=True,
            message=f"Certificate renewed for {domain}",
            domain=domain
        )
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=500, detail="Certificate renewal timed out")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/{domain}/revoke", response_model=CertActionResponse)
async def revoke_certificate(
    domain: str,
    request: Request,
    session: dict = Depends(get_current_session)
):
    """
    Revoke an SSL certificate.
    """
    cert_info = _get_cert_info(domain)
    
    if not cert_info:
        raise HTTPException(status_code=404, detail=f"Certificate not found: {domain}")
    
    try:
        # Try to find the certificate file
        cert_path = Path(f"/etc/letsencrypt/live/{domain}/cert.pem")
        
        if cert_path.exists():
            result = subprocess.run(
                ["certbot", "revoke", "--cert-path", str(cert_path), "--non-interactive"],
                capture_output=True,
                text=True,
                timeout=120
            )
        else:
            # Try using --cert-name instead
            result = subprocess.run(
                ["certbot", "revoke", "--cert-name", domain, "--non-interactive"],
                capture_output=True,
                text=True,
                timeout=120
            )
        
        if result.returncode != 0:
            raise HTTPException(
                status_code=500,
                detail=f"Revocation failed: {result.stderr}"
            )
        
        return CertActionResponse(
            success=True,
            message=f"Certificate revoked for {domain}",
            domain=domain
        )
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=500, detail="Certificate revocation timed out")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/{domain}", response_model=CertActionResponse)
async def delete_certificate(
    domain: str,
    request: Request,
    session: dict = Depends(get_current_session)
):
    """
    Delete an SSL certificate.
    """
    cert_info = _get_cert_info(domain)
    
    if not cert_info:
        raise HTTPException(status_code=404, detail=f"Certificate not found: {domain}")
    
    try:
        result = subprocess.run(
            ["certbot", "delete", "--cert-name", domain, "--non-interactive"],
            capture_output=True,
            text=True,
            timeout=60
        )
        
        if result.returncode != 0:
            raise HTTPException(
                status_code=500,
                detail=f"Deletion failed: {result.stderr}"
            )
        
        return CertActionResponse(
            success=True,
            message=f"Certificate deleted for {domain}",
            domain=domain
        )
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=500, detail="Certificate deletion timed out")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/renew-all")
async def renew_all_certificates(
    request: Request,
    session: dict = Depends(get_current_session)
):
    """
    Renew all certificates that are due for renewal.
    """
    try:
        result = subprocess.run(
            ["certbot", "renew"],
            capture_output=True,
            text=True,
            timeout=300
        )
        
        return {
            "success": result.returncode == 0,
            "output": result.stdout,
            "errors": result.stderr if result.returncode != 0 else None
        }
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=500, detail="Renewal process timed out")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
