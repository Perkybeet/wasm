"""
Git webhook auto-deploy: the panel's only deliberately session-less mutation.

``POST /hooks/deploy/{domain}`` is called server-to-server by a git forge, so
there is no session, no cookie and therefore no CSRF to check. What
authenticates a delivery is a per-application secret, stored by the store and
presented the way each forge presents it: GitHub and Gitea sign the raw body
with HMAC-SHA256, GitLab sends the secret itself in a header. All three are
compared in constant time.

The refusals are deliberately unhelpful. A domain without a secret answers the
same generic 404 an unknown domain does, so the hook cannot be used to map
which applications are deployed; a failed signature answers 401 with no hint
of what was checked. Every outcome - accepted, ignored or refused - is written
to the audit log, never including the secret or the presented signature.

The routers here are mounted in :mod:`wasm.web.server`, not in
:mod:`wasm.web.api.router`: the hook must not inherit the ``/api`` prefix and
its conventions, and the secret-management endpoints live under ``/api/apps``
where the middleware audits them like any other authenticated mutation.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import threading
import time
from collections import OrderedDict
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from wasm.core.exceptions import DeploymentError, DomainError
from wasm.core.store import DeploymentTrigger, get_store
from wasm.web.api.auth import get_current_session
from wasm.web.api.deps import WASMErrorRoute, strict_domain
from wasm.web.auth import get_audit_logger, get_client_ip
from wasm.web.jobs import JobContext, JobType, _require_context, get_job_manager

#: The unauthenticated delivery surface, mounted at ``/hooks``.
router = APIRouter(route_class=WASMErrorRoute)

#: Secret management, mounted under ``/api/apps`` with ordinary session
#: authentication; POST and DELETE there require the ``admin`` scope through
#: the blanket policy in :func:`wasm.web.auth.required_scope`.
admin_router = APIRouter(route_class=WASMErrorRoute)

#: How many delivery ids the replay cache remembers.
DELIVERY_CACHE_SIZE = 512

#: How long a remembered delivery id stays a duplicate, in seconds. Forges
#: redeliver on timeout within seconds; ten minutes covers their retries
#: without remembering deliveries forever.
DELIVERY_TTL_SECONDS = 600

_GITHUB_SIGNATURE_PREFIX = "sha256="


class DeliveryCache:
    """
    Remembers recent delivery ids so a replayed delivery deploys nothing.

    In memory on purpose: a replay window has to survive a forge's automatic
    retries, not a panel restart, and the panel is a single process.
    """

    def __init__(
        self, capacity: int = DELIVERY_CACHE_SIZE, ttl: float = DELIVERY_TTL_SECONDS
    ) -> None:
        """
        Args:
            capacity: Ids remembered before the oldest is dropped.
            ttl: Seconds after which a remembered id stops being a duplicate.
        """
        self._capacity = capacity
        self._ttl = ttl
        self._entries: OrderedDict[str, float] = OrderedDict()
        self._lock = threading.Lock()

    def seen(self, delivery_id: str) -> bool:
        """
        Check a delivery id and remember it in the same motion.

        Args:
            delivery_id: The id, already scoped to a domain by the caller.

        Returns:
            True when the id was already presented within the TTL.
        """
        now = time.monotonic()
        with self._lock:
            while self._entries:
                _oldest, stamp = next(iter(self._entries.items()))
                if now - stamp > self._ttl:
                    self._entries.popitem(last=False)
                else:
                    break

            if delivery_id in self._entries:
                return True

            self._entries[delivery_id] = now
            while len(self._entries) > self._capacity:
                self._entries.popitem(last=False)
            return False

    def clear(self) -> None:
        """Forget everything. For tests, which share the process-wide cache."""
        with self._lock:
            self._entries.clear()


_deliveries = DeliveryCache()


class WebhookSecretResponse(BaseModel):
    """
    A freshly minted webhook secret, shown this once and never again.

    Attributes:
        domain: Application the secret belongs to.
        secret: The secret in clear. This response is the only place the API
            ever returns it.
        hook_url: Where the forge should deliver.
    """

    domain: str
    secret: str
    hook_url: str


class WebhookDisabledResponse(BaseModel):
    """
    Confirmation that webhooks were disabled for an application.

    Attributes:
        domain: Application the secret was removed from.
        enabled: Always false.
    """

    domain: str
    enabled: bool = False


def mint_webhook_secret(domain: str) -> str:
    """
    Generate, store and return a fresh webhook secret for an application.

    The caller shows it once; regenerating replaces the old secret in the same
    motion, so revocation and rotation are the same operation.

    Args:
        domain: Application domain, already validated.

    Returns:
        The secret in clear.

    Raises:
        DeploymentError: When no application is deployed at the domain.
    """
    secret = secrets.token_urlsafe(32)
    if not get_store().set_webhook_secret(domain, secret):
        raise DeploymentError(
            f"Application not found: {domain}",
            details="Deploy it first, or check 'wasm list' for the exact domain.",
        )
    return secret


def webhook_update_job(domain: str, job_context: JobContext | None = None) -> dict[str, Any]:
    """
    Update a deployed application, recorded as webhook-triggered.

    A mirror of :func:`wasm.web.jobs.update_app_job` with the provenance
    changed: that function pins ``trigger="panel"``, and the deployment
    history has to say a robot did this, not an operator. The deployment
    itself is still the deployer's one implementation; only this wiring
    differs.

    Args:
        domain: Domain of the application to update.
        job_context: Injected by the job manager.

    Returns:
        Summary of the update.

    Raises:
        DeploymentError: When the application is unknown or the deploy fails.
    """
    from wasm.deployers import get_deployer
    from wasm.managers.backup_manager import RollbackManager

    context = _require_context(job_context)
    context.set_metadata("domain", domain)

    app = get_store().get_app(domain)
    if app is None:
        raise DeploymentError(
            f"Application not found: {domain}",
            details="It may have been deleted after the webhook was accepted.",
        )

    context.update("Creating rollback point", 10)
    RollbackManager(verbose=False).create_pre_deploy_backup(domain)

    deployer = get_deployer(app.app_type, verbose=False)
    deployer.configure(
        domain=domain,
        source=app.source,
        port=app.port,
        webserver=app.webserver,
        ssl=app.ssl_enabled,
        branch=app.branch,
        env_vars=app.env_vars,
        trigger=DeploymentTrigger.WEBHOOK.value,
    )

    context.update("Redeploying", 30)
    if not deployer.deploy():
        raise DeploymentError(
            f"Update failed for {domain}",
            details="Roll back with 'wasm rollback' or inspect the job log.",
        )

    context.update("Update complete", 100)
    return {"domain": domain, "status": "updated", "trigger": DeploymentTrigger.WEBHOOK.value}


def _hmac_hex(secret: str, body: bytes) -> str:
    """
    Compute the signature GitHub and Gitea expect.

    Args:
        secret: The webhook secret in clear.
        body: The raw request body, exactly as delivered.

    Returns:
        The lowercase hex HMAC-SHA256 digest.
    """
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _verify_provider(secret: str, body: bytes, request: Request) -> str | None:
    """
    Identify and verify the forge behind a delivery.

    Every provider header present is tried, and the first one that verifies
    wins; a delivery carrying only wrong credentials verifies as nothing.

    Args:
        secret: The application's webhook secret in clear.
        body: The raw request body.
        request: The incoming request.

    Returns:
        ``github``, ``gitea`` or ``gitlab`` when a credential verified, else
        None. Which header failed is deliberately not reported.
    """
    headers = request.headers

    github = headers.get("X-Hub-Signature-256")
    if github and github.startswith(_GITHUB_SIGNATURE_PREFIX):
        presented = github[len(_GITHUB_SIGNATURE_PREFIX) :].strip().lower()
        if hmac.compare_digest(_hmac_hex(secret, body), presented):
            return "github"

    gitea = headers.get("X-Gitea-Signature")
    if gitea and hmac.compare_digest(_hmac_hex(secret, body), gitea.strip().lower()):
        return "gitea"

    gitlab = headers.get("X-Gitlab-Token")
    if gitlab and hmac.compare_digest(secret.encode(), gitlab.encode()):
        return "gitlab"

    return None


def _delivery_id(request: Request) -> str | None:
    """
    Read the delivery id, whichever forge sent it.

    Args:
        request: The incoming request.

    Returns:
        The id, or None when the delivery carries none.
    """
    return (
        request.headers.get("X-GitHub-Delivery")
        or request.headers.get("X-Gitea-Delivery")
        or request.headers.get("X-Gitlab-Event-UUID")
    )


def _pushed_branch(body: bytes) -> str | None:
    """
    Extract the branch from a push payload.

    All three forges put ``refs/heads/<branch>`` in ``ref`` for push events.

    Args:
        body: The raw request body.

    Returns:
        The branch name, or None when the payload names no branch - a tag
        push, a ping event, or a body that is not the JSON it claims to be.
    """
    try:
        payload = json.loads(body) if body else {}
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(payload, dict):
        return None

    ref = payload.get("ref")
    if not isinstance(ref, str) or not ref:
        return None
    if ref.startswith("refs/heads/"):
        return ref[len("refs/heads/") :]
    if ref.startswith("refs/"):
        return None
    return ref


def _record(request: Request, domain: str, result: str, detail: str) -> None:
    """
    Write one hook outcome to the audit log.

    Args:
        request: The delivery being answered.
        domain: Domain as it appeared in the path.
        result: ``accepted``, ``ignored`` or ``denied``.
        detail: Extra context. Never a secret and never a signature.
    """
    audit = get_audit_logger()
    if audit:
        audit.record(
            action="hooks.deploy",
            result=result,
            client_ip=get_client_ip(request),
            resource=f"/hooks/deploy/{domain}",
            detail=detail,
        )


@router.post("/deploy/{domain}")
async def deliver(domain: str, request: Request) -> JSONResponse:
    """
    Accept a signed push notification and queue the update it asks for.

    Async because the raw body has to be awaited before anything can be
    verified: the HMAC covers the bytes on the wire, not a parsed view of
    them.

    Args:
        domain: Domain of the application to update.
        request: The incoming delivery.

    Returns:
        202 with the queued job id; 200 when the delivery is authentic but
        ignored (wrong branch, or a replayed delivery id).

    Raises:
        HTTPException: A generic 404 when the domain has no webhook configured
            or does not exist - the two are indistinguishable on purpose - and
            401 with no details when no presented credential verifies.
    """
    body = await request.body()

    try:
        validated = strict_domain(domain)
    except DomainError:
        _record(request, domain, "denied", "not a domain")
        raise HTTPException(status_code=404, detail="Not found") from None

    secret = get_store().get_webhook_secret(validated)
    if not secret:
        _record(request, validated, "denied", "unknown domain or webhooks not configured")
        raise HTTPException(status_code=404, detail="Not found")

    provider = _verify_provider(secret, body, request)
    if provider is None:
        _record(request, validated, "denied", "signature verification failed")
        raise HTTPException(status_code=401, detail="Unauthorized")

    delivery = _delivery_id(request)
    if delivery and _deliveries.seen(f"{validated}:{delivery}"):
        _record(request, validated, "ignored", f"duplicate delivery {delivery} ({provider})")
        return JSONResponse(status_code=200, content={"status": "ignored", "reason": "duplicate"})

    app = get_store().get_app(validated)
    branch = _pushed_branch(body)
    if app is not None and app.branch and branch != app.branch:
        _record(
            request,
            validated,
            "ignored",
            f"push to {branch or 'no branch'}, app tracks {app.branch} ({provider})",
        )
        return JSONResponse(status_code=200, content={"status": "ignored", "reason": "branch"})

    job = get_job_manager().create_job(
        job_type=JobType.UPDATE,
        name=f"Update {validated}",
        description=f"Webhook-triggered update of {validated}",
        func=webhook_update_job,
        kwargs={"domain": validated},
        metadata={
            "domain": validated,
            "trigger": DeploymentTrigger.WEBHOOK.value,
            "provider": provider,
            "branch": branch,
            "delivery": delivery,
        },
    )

    _record(
        request,
        validated,
        "accepted",
        f"queued job {job.id} ({provider}, branch {branch or 'any'})",
    )
    return JSONResponse(status_code=202, content={"job_id": job.id, "status": job.status.value})


def _known_app_domain(domain: str) -> str:
    """
    Validate a domain and require an application behind it.

    This is the authenticated management surface, so unlike the hook itself it
    may say plainly that nothing is deployed there.

    Args:
        domain: Domain from the request path.

    Returns:
        The validated domain.

    Raises:
        HTTPException: 404 when no application is deployed at the domain.
        DomainError: When the value is not a domain.
    """
    validated = strict_domain(domain)
    if get_store().get_app(validated) is None:
        raise HTTPException(status_code=404, detail=f"Application not found: {validated}")
    return validated


@admin_router.post("/{domain}/webhook-secret", response_model=WebhookSecretResponse)
def create_webhook_secret(
    domain: str,
    request: Request,
    session: Annotated[dict, Depends(get_current_session)],
) -> WebhookSecretResponse:
    """
    Mint (or replace) the webhook secret of an application.

    The secret appears in this response and nowhere else, ever again: the
    store keeps it for signature verification, but no listing or detail
    endpoint returns it. Calling this again rotates the secret, revoking the
    old one in the same motion.

    Args:
        domain: Domain of the application.
        request: The incoming request, for the audit record and the hook URL.
        session: The authenticated session.

    Returns:
        The secret, shown once, and the URL to configure at the forge.

    Raises:
        HTTPException: 404 when the application is unknown.
    """
    validated = _known_app_domain(domain)
    secret = mint_webhook_secret(validated)

    audit = get_audit_logger()
    if audit:
        audit.record(
            action="hooks.secret.mint",
            result="success",
            client_ip=get_client_ip(request),
            actor=str(session.get("sid")),
            resource=f"/api/apps/{validated}/webhook-secret",
            detail="webhook secret issued; shown once",
        )

    return WebhookSecretResponse(
        domain=validated,
        secret=secret,
        hook_url=f"{str(request.base_url).rstrip('/')}/hooks/deploy/{validated}",
    )


@admin_router.delete("/{domain}/webhook-secret", response_model=WebhookDisabledResponse)
def delete_webhook_secret(
    domain: str,
    request: Request,
    session: Annotated[dict, Depends(get_current_session)],
) -> WebhookDisabledResponse:
    """
    Disable webhooks for an application by discarding its secret.

    Args:
        domain: Domain of the application.
        request: The incoming request, for the audit record.
        session: The authenticated session.

    Returns:
        Confirmation that deliveries will now be answered with 404.

    Raises:
        HTTPException: 404 when the application is unknown.
    """
    validated = _known_app_domain(domain)
    get_store().set_webhook_secret(validated, None)

    audit = get_audit_logger()
    if audit:
        audit.record(
            action="hooks.secret.disable",
            result="success",
            client_ip=get_client_ip(request),
            actor=str(session.get("sid")),
            resource=f"/api/apps/{validated}/webhook-secret",
            detail="webhook secret discarded",
        )

    return WebhookDisabledResponse(domain=validated)
