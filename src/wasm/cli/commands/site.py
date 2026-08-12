# Copyright (c) 2024-2026 Yago Lopez Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
The ``wasm site`` command group.

Virtual hosts on nginx and apache: create, list, enable, disable, delete and
show. The work lives in the private ``_site_*`` functions, so the Click
commands and the legacy :func:`handle_site` argparse entry point run exactly
the same code and only differ in how the parameters arrive. That entry point
stays until :mod:`wasm.cli.parser` and the interactive menu are cut over.

Every manager this module needs is imported here, at module level. Importing
CertManager inside the create path meant ``wasm site delete`` raised NameError
on every run, and the broad handler around it turned that into a warning about
a certificate that had in fact never been touched.
"""

from __future__ import annotations

import sys
from argparse import Namespace

import click

from wasm.cli.app import Context, pass_context
from wasm.core.exceptions import WASMError
from wasm.core.logger import Logger
from wasm.managers.apache_manager import ApacheManager
from wasm.managers.cert_manager import CertManager
from wasm.managers.nginx_manager import NginxManager
from wasm.validators.domain import should_include_www, validate_domain
from wasm.validators.port import MAX_PORT, MIN_PORT

#: Alternative spellings of the subcommands. They predate the group and live in
#: scripts and in muscle memory, so dropping one is a breaking change.
COMMAND_ALIASES: dict[str, str] = {
    "cat": "show",
    "ls": "list",
    "remove": "delete",
    "rm": "delete",
}

DEFAULT_PORT = 3000
DEFAULT_TEMPLATE = "proxy"
WEBSERVERS = ("nginx", "apache")

_NOT_FOUND_HINT = "Run 'wasm site list' to see the virtual hosts this server knows about."


class SiteGroup(click.Group):
    """
    The ``site`` group, resolving the historical spellings of its subcommands.

    Aliases resolve but are not listed, so the help stays one line per
    operation instead of ten lines for six operations.
    """

    def get_command(self, ctx: click.Context, name: str) -> click.Command | None:
        """
        Look a subcommand up by its name or by one of its aliases.

        Args:
            ctx: The Click context.
            name: Name the user typed.

        Returns:
            The command, or None when nothing answers to that name.
        """
        return super().get_command(ctx, COMMAND_ALIASES.get(name, name))


def _get_manager(webserver: str, verbose: bool = False) -> ApacheManager | NginxManager:
    """
    Get the appropriate site manager.

    Args:
        webserver: Either "apache" or "nginx".
        verbose: Enable verbose logging.

    Returns:
        The manager for the requested web server.
    """
    if webserver == "apache":
        return ApacheManager(verbose=verbose)
    return NginxManager(verbose=verbose)


def _site_create(
    *,
    domain: str,
    webserver: str,
    template: str,
    port: int,
    no_ssl: bool,
    www: bool,
    logger: Logger,
    verbose: bool,
) -> None:
    """
    Create or update a virtual host and, unless asked not to, secure it.

    The site is written without SSL first because certbot validates over plain
    HTTP; the certificate paths are only added to the configuration once a
    certificate really exists.

    Args:
        domain: Domain the site serves.
        webserver: Either "nginx" or "apache".
        template: Template name, without the ``.conf.j2`` suffix.
        port: Port the application listens on behind the proxy.
        no_ssl: Skip certificate issuance entirely.
        www: Also serve and certify ``www.<domain>``.
        logger: Logger for progress output.
        verbose: Enable verbose logging in the managers.

    Raises:
        DomainError: When the domain is not a valid domain name.
        NginxError: When the nginx configuration cannot be written.
        ApacheError: When the apache configuration cannot be written.
    """
    domain = validate_domain(domain)
    manager = _get_manager(webserver, verbose=verbose)
    include_www = www and should_include_www(domain)

    server_names = domain
    if include_www:
        server_names = f"{domain} www.{domain}"
        logger.info(f"Including www.{domain}")

    # Step 1: Create site without SSL (needed for certbot webroot validation)
    context = {
        "port": port,
        "ssl": False,
        "server_names": server_names,
    }

    logger.info(f"Creating site: {domain}")
    if manager.site_exists(domain):
        logger.info("Site config already exists, updating...")
        manager.update_site(domain, template=template, context=context)
    else:
        manager.create_site(domain, template=template, context=context)
        manager.enable_site(domain)
    manager.reload()

    # Step 2: Obtain SSL certificate if requested
    ssl_obtained = False
    if not no_ssl:
        cert_manager = CertManager(verbose=verbose)
        if cert_manager.is_installed():
            additional_domains = None
            if include_www:
                additional_domains = [f"www.{domain}"]

            # Check if a valid certificate already exists and covers all domains
            if cert_manager.cert_exists(domain):
                test = cert_manager.test_cert(domain)
                if test.get("valid"):
                    all_required = [domain] + (additional_domains or [])
                    if cert_manager.cert_covers_domains(domain, all_required):
                        logger.info("Valid SSL certificate found covering all domains")
                        ssl_obtained = True
                    else:
                        logger.info("Existing certificate does not cover all domains, expanding...")
                else:
                    logger.warning("Existing certificate invalid, obtaining new one...")

            if not ssl_obtained:
                logger.info(f"Obtaining SSL certificate for {domain}...")
                try:
                    cert_manager.obtain(
                        domain,
                        nginx=webserver == "nginx",
                        apache=webserver == "apache",
                        additional_domains=additional_domains,
                    )
                    ssl_obtained = True
                except WASMError as e:
                    logger.warning(f"SSL certificate failed: {e}")
                    logger.info(
                        "Site created without SSL. You can add it later with: wasm cert create"
                    )
        else:
            logger.warning("Certbot not installed, skipping SSL")
            logger.info("Install with: sudo apt install certbot")

    # Step 3: Update site config with SSL if certificate was obtained
    if ssl_obtained:
        cert_paths = cert_manager.get_cert_path(domain)
        context["ssl"] = True
        context["ssl_certificate"] = str(cert_paths["fullchain"])
        context["ssl_certificate_key"] = str(cert_paths["privkey"])

        manager.update_site(domain, template=template, context=context)
        manager.reload()
        logger.success(f"Site created with SSL: {domain}")
    else:
        logger.success(f"Site created: {domain}")


def _site_list(*, webserver: str, logger: Logger, verbose: bool) -> None:
    """
    Print every virtual host on the requested web servers.

    Args:
        webserver: "nginx", "apache" or "all".
        logger: Logger for the table.
        verbose: Enable verbose logging in the managers.
    """
    logger.header("Web Server Sites")

    all_sites = []

    if webserver in ("nginx", "all"):
        nginx = NginxManager(verbose=verbose)
        if nginx.is_installed():
            for site in nginx.list_sites():
                site["webserver"] = "nginx"
                all_sites.append(site)

    if webserver in ("apache", "all"):
        apache = ApacheManager(verbose=verbose)
        if apache.is_installed():
            for site in apache.list_sites():
                site["webserver"] = "apache"
                all_sites.append(site)

    if not all_sites:
        logger.info("No sites found")
        return

    headers = ["Domain", "Enabled", "Web Server"]
    rows = []

    for site in all_sites:
        status = "✓" if site["enabled"] else "✗"
        rows.append([site["domain"], status, site["webserver"]])

    logger.table(headers, rows)


def _site_enable(*, domain: str, logger: Logger, verbose: bool) -> None:
    """
    Enable a virtual host on whichever web server owns it.

    Args:
        domain: Domain of the site.
        logger: Logger for progress output.
        verbose: Enable verbose logging in the managers.

    Raises:
        WASMError: When neither web server has a configuration for the domain.
        DomainError: When the domain is not a valid domain name.
    """
    domain = validate_domain(domain)

    # Try Nginx first, then Apache
    nginx = NginxManager(verbose=verbose)
    apache = ApacheManager(verbose=verbose)

    if nginx.site_exists(domain):
        nginx.enable_site(domain)
        nginx.reload()
        logger.success(f"Site enabled (nginx): {domain}")
    elif apache.site_exists(domain):
        apache.enable_site(domain)
        apache.reload()
        logger.success(f"Site enabled (apache): {domain}")
    else:
        raise WASMError(f"Site not found: {domain}", details=_NOT_FOUND_HINT)


def _site_disable(*, domain: str, logger: Logger, verbose: bool) -> None:
    """
    Disable a virtual host, leaving its configuration file in place.

    Args:
        domain: Domain of the site.
        logger: Logger for progress output.
        verbose: Enable verbose logging in the managers.

    Raises:
        DomainError: When the domain is not a valid domain name.
    """
    domain = validate_domain(domain)

    nginx = NginxManager(verbose=verbose)
    apache = ApacheManager(verbose=verbose)

    if nginx.site_enabled(domain):
        nginx.disable_site(domain)
        nginx.reload()
        logger.success(f"Site disabled (nginx): {domain}")
    elif apache.site_enabled(domain):
        apache.disable_site(domain)
        apache.reload()
        logger.success(f"Site disabled (apache): {domain}")
    else:
        logger.warning(f"Site not enabled: {domain}")


def _site_delete(*, domain: str, logger: Logger, verbose: bool) -> None:
    """
    Delete a virtual host and the certificate issued for it.

    Confirmation belongs to the caller: this function always deletes.

    Args:
        domain: Domain of the site.
        logger: Logger for progress output.
        verbose: Enable verbose logging in the managers.

    Raises:
        WASMError: When neither web server has a configuration for the domain.
        DomainError: When the domain is not a valid domain name.
    """
    domain = validate_domain(domain)

    nginx = NginxManager(verbose=verbose)
    apache = ApacheManager(verbose=verbose)

    deleted = False

    if nginx.site_exists(domain):
        nginx.delete_site(domain)
        nginx.reload()
        deleted = True
        logger.success(f"Site deleted (nginx): {domain}")

    if apache.site_exists(domain):
        apache.delete_site(domain)
        apache.reload()
        deleted = True
        logger.success(f"Site deleted (apache): {domain}")

    # Delete SSL certificate if exists
    cert_manager = CertManager(verbose=verbose)
    if cert_manager.is_installed() and cert_manager.cert_exists(domain):
        logger.info(f"Deleting SSL certificate for {domain}...")
        try:
            cert_manager.delete(domain)
            logger.success(f"Certificate deleted: {domain}")
        except WASMError as e:
            logger.warning(f"Failed to delete certificate: {e}")

    if not deleted:
        raise WASMError(f"Site not found: {domain}", details=_NOT_FOUND_HINT)


def _site_show(*, domain: str, verbose: bool) -> None:
    """
    Print the configuration file of a virtual host.

    Args:
        domain: Domain of the site.
        verbose: Enable verbose logging in the managers.

    Raises:
        WASMError: When neither web server has a configuration for the domain.
        DomainError: When the domain is not a valid domain name.
    """
    domain = validate_domain(domain)

    nginx = NginxManager(verbose=verbose)
    apache = ApacheManager(verbose=verbose)

    config = None

    if nginx.site_exists(domain):
        config = nginx.get_site_config(domain)
    elif apache.site_exists(domain):
        config = apache.get_site_config(domain)

    if not config:
        raise WASMError(f"Site not found: {domain}", details=_NOT_FOUND_HINT)

    click.echo(config)


@click.group(cls=SiteGroup, name="site")
def cli() -> None:
    """
    Manage the nginx and apache virtual hosts on this server.

    A site is one domain, its web server configuration and its certificate.
    Aliases: ls for list, rm or remove for delete, cat for show.
    """


@cli.command("create")
@click.option("--domain", "-d", required=True, help="Domain to serve, such as example.com.")
@click.option(
    "--webserver",
    "-w",
    type=click.Choice(WEBSERVERS),
    default="nginx",
    show_default=True,
    help="Web server to configure.",
)
@click.option(
    "--template",
    "-t",
    default=DEFAULT_TEMPLATE,
    show_default=True,
    help="Configuration template to render.",
)
@click.option(
    "--port",
    "-p",
    type=click.IntRange(MIN_PORT, MAX_PORT),
    default=DEFAULT_PORT,
    show_default=True,
    help="Port the application listens on behind the proxy.",
)
@click.option("--no-ssl", is_flag=True, help="Serve plain HTTP and request no certificate.")
@click.option(
    "--www",
    is_flag=True,
    help="Also serve www.<domain> and cover it with the certificate.",
)
@pass_context
def create(
    state: Context,
    domain: str,
    webserver: str,
    template: str,
    port: int,
    no_ssl: bool,
    www: bool,
) -> None:
    """Create a site and secure it with a certificate."""
    _site_create(
        domain=domain,
        webserver=webserver,
        template=template,
        port=port,
        no_ssl=no_ssl,
        www=www,
        logger=state.logger,
        verbose=state.verbose,
    )


@cli.command("list")
@click.option(
    "--webserver",
    "-w",
    type=click.Choice([*WEBSERVERS, "all"]),
    default="all",
    show_default=True,
    help="Only show sites from this web server.",
)
@pass_context
def list_sites(state: Context, webserver: str) -> None:
    """List the sites configured on this server."""
    _site_list(webserver=webserver, logger=state.logger, verbose=state.verbose)


@cli.command("enable")
@click.argument("domain")
@pass_context
def enable(state: Context, domain: str) -> None:
    """Start serving a site that is configured but off."""
    _site_enable(domain=domain, logger=state.logger, verbose=state.verbose)


@cli.command("disable")
@click.argument("domain")
@pass_context
def disable(state: Context, domain: str) -> None:
    """Stop serving a site, keeping its configuration."""
    _site_disable(domain=domain, logger=state.logger, verbose=state.verbose)


@cli.command("delete")
@click.argument("domain")
@click.option(
    "--force",
    "-f",
    "-y",
    is_flag=True,
    help="Delete without asking for confirmation.",
)
@pass_context
def delete(state: Context, domain: str, force: bool) -> None:
    """Delete a site, its configuration and its certificate."""
    # Validated before the prompt so that the question names the domain exactly
    # as it will be acted on, and so a typo fails without asking anything.
    domain = validate_domain(domain)

    if not force and not click.confirm(
        f"Delete the site {domain}, its web server configuration "
        f"and the SSL certificate issued for it?",
        default=False,
    ):
        state.logger.info("Aborted")
        return

    _site_delete(domain=domain, logger=state.logger, verbose=state.verbose)


@cli.command("show")
@click.argument("domain")
@pass_context
def show(state: Context, domain: str) -> None:
    """Print the configuration file of a site."""
    _site_show(domain=domain, verbose=state.verbose)


def handle_site(args: Namespace) -> int:
    """
    Handle site commands coming from the argparse parser.

    Kept while :mod:`wasm.cli.parser` and the interactive menu still dispatch
    through argparse. It calls the same private functions as the Click
    commands.

    Args:
        args: Parsed arguments.

    Returns:
        Exit code.
    """
    action = args.action

    handlers = {
        "create": _handle_create,
        "list": _handle_list,
        "ls": _handle_list,
        "enable": _handle_enable,
        "disable": _handle_disable,
        "delete": _handle_delete,
        "remove": _handle_delete,
        "rm": _handle_delete,
        "show": _handle_show,
        "cat": _handle_show,
    }

    handler = handlers.get(action)
    if not handler:
        print(f"Unknown action: {action}", file=sys.stderr)
        return 1

    try:
        return handler(args)
    except WASMError as e:
        logger = Logger(verbose=args.verbose)
        logger.error(str(e))
        return 1
    except Exception as e:
        logger = Logger(verbose=args.verbose)
        logger.error(f"Unexpected error: {e}")
        logger.debug(f"Unhandled {type(e).__name__} in site {action}")
        if args.verbose:
            import traceback

            traceback.print_exc()
        return 1


def _handle_create(args: Namespace) -> int:
    """Handle site create command."""
    _site_create(
        domain=args.domain,
        webserver=getattr(args, "webserver", "nginx"),
        template=getattr(args, "template", DEFAULT_TEMPLATE),
        port=getattr(args, "port", DEFAULT_PORT),
        no_ssl=getattr(args, "no_ssl", False),
        www=getattr(args, "www", False),
        logger=Logger(verbose=args.verbose),
        verbose=args.verbose,
    )
    return 0


def _handle_list(args: Namespace) -> int:
    """Handle site list command."""
    _site_list(
        webserver=getattr(args, "webserver", "all"),
        logger=Logger(verbose=args.verbose),
        verbose=args.verbose,
    )
    return 0


def _handle_enable(args: Namespace) -> int:
    """Handle site enable command."""
    _site_enable(domain=args.domain, logger=Logger(verbose=args.verbose), verbose=args.verbose)
    return 0


def _handle_disable(args: Namespace) -> int:
    """Handle site disable command."""
    _site_disable(domain=args.domain, logger=Logger(verbose=args.verbose), verbose=args.verbose)
    return 0


def _handle_delete(args: Namespace) -> int:
    """Handle site delete command."""
    logger = Logger(verbose=args.verbose)
    domain = validate_domain(args.domain)

    if not getattr(args, "force", False):
        response = input(f"Delete site '{domain}'? [y/N] ")
        if response.lower() != "y":
            logger.info("Aborted")
            return 0

    _site_delete(domain=domain, logger=logger, verbose=args.verbose)
    return 0


def _handle_show(args: Namespace) -> int:
    """Handle site show command."""
    _site_show(domain=args.domain, verbose=args.verbose)
    return 0
