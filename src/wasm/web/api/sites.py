"""
Sites API endpoints.

This module is a client of :class:`~wasm.managers.webserver.WebServerManager`,
through its nginx and apache bindings. It used to be a second implementation of
them, and the two had already diverged in production in the worst possible way:
the API wrote its virtual host to ``sites-available/example_com`` while every
manager, the CLI and the store use ``sites-available/example.com``. A site
created from the panel was therefore invisible to ``wasm site list``, could not
be enabled, disabled or deleted from the CLI, and was skipped by certificate
issuance. The file name is now produced by exactly one piece of code -
:meth:`~wasm.managers.webserver.WebServerManager.config_path` - and the panel
never renders a server block itself.

Two further rules, the same ones the services API follows:

- **Every domain is validated before it becomes a path.** The manager's
  ``config_path`` is the single place a domain turns into a file name, and it
  validates and contains it; :func:`wasm.web.api.deps.strict_domain` refuses at
  the edge anything that would only survive by being rewritten.
- **Handlers are synchronous.** They call nginx, apache2ctl and systemctl,
  which block. Declared ``async def`` they would run on the event loop and
  freeze the panel for every other client; declared ``def``, FastAPI runs them
  in the threadpool.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from wasm.core.exceptions import ValidationError
from wasm.core.store import get_store
from wasm.managers.apache_manager import ApacheManager
from wasm.managers.nginx_manager import NginxManager
from wasm.managers.webserver import WebServerManager
from wasm.web.api.auth import get_current_session
from wasm.web.api.deps import WASMErrorRoute, strict_domain

router = APIRouter(route_class=WASMErrorRoute)

#: Web server used when none is installed and none was requested.
DEFAULT_WEBSERVER = "nginx"

#: Manager class per web server name. This is the allowlist for the
#: ``webserver`` field: anything not in it is a client error, not a fallback.
#: Typed as the concrete classes because both expose their configuration
#: directory as a class attribute, which detection reads without building one.
MANAGERS: dict[str, type[NginxManager] | type[ApacheManager]] = {
    "nginx": NginxManager,
    "apache": ApacheManager,
}


class SiteInfo(BaseModel):
    """
    A configured virtual host.

    Attributes:
        name: The domain, which is also the configuration file name.
        webserver: Web server serving it.
        enabled: Whether the site is enabled.
        config_path: Absolute path of the configuration file.
        has_ssl: Whether the configuration carries TLS directives.
    """

    name: str
    webserver: str
    enabled: bool
    config_path: str
    has_ssl: bool = False


class SiteListResponse(BaseModel):
    """Response for listing sites."""

    sites: list[SiteInfo]
    total: int
    webserver: str


class SiteActionResponse(BaseModel):
    """Response for site actions."""

    success: bool
    message: str
    site: str


class SiteConfigResponse(BaseModel):
    """Response carrying the raw configuration of a site."""

    site: str
    webserver: str
    config: str
    path: str


class ReloadResponse(BaseModel):
    """Response for a web server reload."""

    success: bool
    message: str
    webserver: str


class CreateSiteRequest(BaseModel):
    """
    Request to create a site.

    Attributes:
        domain: Domain to serve. It is also the configuration file name.
        webserver: Web server to configure, detected when omitted.
        template: Manager template to render.
        port: Upstream port for the proxy template.
        ssl: Render the template with TLS directives.
        enable: Enable the site once written.
    """

    domain: str
    webserver: str | None = None
    template: str = "proxy"
    port: int = Field(default=3000, ge=1, le=65535)
    ssl: bool = False
    enable: bool = True


class UpdateSiteConfigRequest(BaseModel):
    """Request to replace the raw configuration of a site."""

    config: str


def detect_webserver() -> str:
    """
    Work out which web server this host uses.

    Returns:
        ``nginx`` or ``apache``. Nginx is the answer when neither is installed,
        because it is what a fresh deployment will configure.
    """
    for name, manager_class in MANAGERS.items():
        if manager_class(verbose=False).is_installed():
            return name

    for name, manager_class in MANAGERS.items():
        if manager_class.SITES_AVAILABLE.exists():
            return name

    return DEFAULT_WEBSERVER


def _manager_for(webserver: str | None) -> tuple[str, WebServerManager]:
    """
    Resolve a web server name to its manager.

    Args:
        webserver: Requested web server, or None to detect one.

    Returns:
        Tuple of the resolved name and its manager.

    Raises:
        ValidationError: When the name is not a web server WASM supports.
    """
    name = (webserver or detect_webserver()).lower()
    manager_class = MANAGERS.get(name)
    if manager_class is None:
        raise ValidationError(
            f"Unknown web server: {webserver!r}",
            details=f"Use one of: {', '.join(sorted(MANAGERS))}.",
        )
    return name, manager_class(verbose=False)


def _has_ssl(config: str) -> bool:
    """
    Report whether a rendered configuration serves TLS.

    Args:
        config: The configuration file content.

    Returns:
        True when it carries a certificate directive.
    """
    return "ssl_certificate" in config or "SSLCertificateFile" in config


@router.get("", response_model=SiteListResponse)
def list_sites(session: Annotated[dict, Depends(get_current_session)]) -> SiteListResponse:
    """
    List every configured site.

    Args:
        session: The authenticated session.

    Returns:
        The sites known to the store, falling back to what the manager finds on
        disk when the store has no record of them.
    """
    webserver, manager = _manager_for(None)

    sites = [
        SiteInfo(
            name=site.domain,
            webserver=site.webserver,
            enabled=site.enabled,
            config_path=site.config_path or "",
            has_ssl=site.ssl_enabled,
        )
        for site in get_store().list_sites()
    ]

    if not sites:
        sites = [
            SiteInfo(
                name=entry.domain,
                webserver=entry.webserver,
                enabled=entry.enabled,
                config_path=entry.config_path,
                has_ssl=_has_ssl(manager.get_site_config(entry.domain) or ""),
            )
            for entry in manager.list_sites()
        ]

    return SiteListResponse(sites=sites, total=len(sites), webserver=webserver)


@router.post("", response_model=SiteActionResponse)
def create_site(
    data: CreateSiteRequest, session: Annotated[dict, Depends(get_current_session)]
) -> SiteActionResponse:
    """
    Create a virtual host from one of the manager's templates.

    The configuration file is named by the manager, which is what the CLI, the
    store and certificate issuance all expect.

    Args:
        data: The create request.
        session: The authenticated session.

    Returns:
        The action outcome.

    Raises:
        HTTPException: 409 when the site already exists.
        ValidationError: When the template is not one the backend offers.
        DomainError: When the domain is not acceptable.
        SiteError: When the manager cannot write the configuration.
    """
    domain = strict_domain(data.domain)
    webserver, manager = _manager_for(data.webserver)

    templates = manager.list_templates()
    if data.template not in templates:
        raise ValidationError(
            f"Unknown template: {data.template!r}",
            details=f"Available {webserver} templates: {', '.join(templates) or 'none'}.",
        )

    if manager.site_exists(domain):
        raise HTTPException(status_code=409, detail=f"Site already exists: {domain}")

    manager.create_site(
        domain, template=data.template, context={"port": data.port, "ssl": data.ssl}
    )

    if data.enable:
        manager.enable_site(domain)

    return SiteActionResponse(
        success=True,
        message=f"Site created: {domain}. Reload {webserver} to serve it.",
        site=domain,
    )


@router.post("/reload", response_model=ReloadResponse)
def reload_webserver(session: Annotated[dict, Depends(get_current_session)]) -> ReloadResponse:
    """
    Test and reload the web server configuration.

    Registered before ``/{domain}`` so the literal path wins: FastAPI matches
    routes in registration order, and a parametrised route declared first would
    swallow this one.

    Args:
        session: The authenticated session.

    Returns:
        The reload outcome.

    Raises:
        HTTPException: 400 when the configuration does not pass its own test,
            500 when the reload itself fails.
    """
    webserver, manager = _manager_for(None)

    if not manager.test_config():
        raise HTTPException(
            status_code=400,
            detail=f"{webserver} configuration test failed; the running config was kept",
        )

    if not manager.reload():
        raise HTTPException(status_code=500, detail=f"Failed to reload {webserver}")

    return ReloadResponse(success=True, message=f"{webserver} reloaded", webserver=webserver)


@router.get("/{domain}", response_model=SiteInfo)
def get_site(domain: str, session: Annotated[dict, Depends(get_current_session)]) -> SiteInfo:
    """
    Describe one site.

    Args:
        domain: Domain of the site.
        session: The authenticated session.

    Returns:
        The site description.

    Raises:
        HTTPException: 404 when no such site exists.
        DomainError: When the domain is not acceptable.
    """
    validated = strict_domain(domain)

    site = get_store().get_site(validated)
    if site:
        return SiteInfo(
            name=site.domain,
            webserver=site.webserver,
            enabled=site.enabled,
            config_path=site.config_path or "",
            has_ssl=site.ssl_enabled,
        )

    webserver, manager = _manager_for(None)
    if not manager.site_exists(validated):
        raise HTTPException(status_code=404, detail=f"Site not found: {validated}")

    return SiteInfo(
        name=validated,
        webserver=webserver,
        enabled=manager.site_enabled(validated),
        config_path=str(manager.config_path(validated)),
        has_ssl=_has_ssl(manager.get_site_config(validated) or ""),
    )


@router.get("/{domain}/config", response_model=SiteConfigResponse)
def get_site_config(
    domain: str, session: Annotated[dict, Depends(get_current_session)]
) -> SiteConfigResponse:
    """
    Read the raw configuration of a site.

    Args:
        domain: Domain of the site.
        session: The authenticated session.

    Returns:
        The configuration content.

    Raises:
        HTTPException: 404 when no such site exists.
        DomainError: When the domain is not acceptable.
    """
    validated = strict_domain(domain)
    webserver, manager = _manager_for(None)

    content = manager.get_site_config(validated)
    if content is None:
        raise HTTPException(status_code=404, detail=f"Site not found: {validated}")

    return SiteConfigResponse(
        site=validated,
        webserver=webserver,
        config=content,
        path=str(manager.config_path(validated)),
    )


@router.put("/{domain}/config", response_model=SiteActionResponse)
def update_site_config(
    domain: str,
    data: UpdateSiteConfigRequest,
    session: Annotated[dict, Depends(get_current_session)],
) -> SiteActionResponse:
    """
    Replace the raw configuration of a site.

    The path comes from the manager, so a hand-edited configuration can only
    ever overwrite the file that domain already owns. The manager validates
    the text against the web server itself before persisting it: a broken
    configuration used to be written unchecked and took the site down at the
    next reload.

    Args:
        domain: Domain of the site.
        data: The new configuration.
        session: The authenticated session.

    Returns:
        The action outcome.

    Raises:
        HTTPException: 404 when no such site exists.
        ValidationError: When the web server rejects the configuration; the
            error carries the server's own output verbatim and the file on
            disk is left as it was.
        SiteError: When the file cannot be staged or written.
        DomainError: When the domain is not acceptable.
    """
    validated = strict_domain(domain)
    webserver, manager = _manager_for(None)

    if not manager.site_exists(validated):
        raise HTTPException(status_code=404, detail=f"Site not found: {validated}")

    manager.replace_site_config(validated, data.config)

    return SiteActionResponse(
        success=True,
        message=f"Configuration updated for {validated}. Reload {webserver} to apply it.",
        site=validated,
    )


@router.post("/{domain}/enable", response_model=SiteActionResponse)
def enable_site(
    domain: str, session: Annotated[dict, Depends(get_current_session)]
) -> SiteActionResponse:
    """
    Enable a site and reload the web server.

    Args:
        domain: Domain of the site.
        session: The authenticated session.

    Returns:
        The action outcome.

    Raises:
        SiteError: When the manager refuses the operation.
        DomainError: When the domain is not acceptable.
    """
    validated = strict_domain(domain)
    _, manager = _manager_for(None)

    manager.enable_site(validated)
    manager.reload()

    return SiteActionResponse(success=True, message=f"Site enabled: {validated}", site=validated)


@router.post("/{domain}/disable", response_model=SiteActionResponse)
def disable_site(
    domain: str, session: Annotated[dict, Depends(get_current_session)]
) -> SiteActionResponse:
    """
    Disable a site and reload the web server.

    Args:
        domain: Domain of the site.
        session: The authenticated session.

    Returns:
        The action outcome.

    Raises:
        SiteError: When the manager refuses the operation.
        DomainError: When the domain is not acceptable.
    """
    validated = strict_domain(domain)
    _, manager = _manager_for(None)

    manager.disable_site(validated)
    manager.reload()

    return SiteActionResponse(success=True, message=f"Site disabled: {validated}", site=validated)


@router.delete("/{domain}", response_model=SiteActionResponse)
def delete_site(
    domain: str, session: Annotated[dict, Depends(get_current_session)]
) -> SiteActionResponse:
    """
    Delete a site configuration.

    Args:
        domain: Domain of the site.
        session: The authenticated session.

    Returns:
        The action outcome.

    Raises:
        HTTPException: 404 when no such site exists.
        SiteError: When the manager refuses the operation.
        DomainError: When the domain is not acceptable.
    """
    validated = strict_domain(domain)
    _, manager = _manager_for(None)

    if not manager.site_exists(validated):
        raise HTTPException(status_code=404, detail=f"Site not found: {validated}")

    manager.delete_site(validated)

    return SiteActionResponse(success=True, message=f"Site deleted: {validated}", site=validated)
