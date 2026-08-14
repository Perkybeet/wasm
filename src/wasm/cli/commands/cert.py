# Copyright (c) 2024-2026 Yago Lopez Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
The ``wasm cert`` command group.

Everything an operator does to a Let's Encrypt certificate goes through here:
obtaining one, listing what the machine holds, renewing, revoking and deleting.

The commands are thin. Each one validates what the user typed, builds a
:class:`~wasm.managers.cert_manager.CertManager` and calls a single private
function that both this Click tree and the argparse handler still wired into
``wasm.cli.parser`` share, so the two front ends cannot drift while the
migration finishes.

Two rules shape the code:

- **Issuance is rate limited.** Nothing here re-issues a certificate to work
  around a question it could not answer; the manager refuses instead.
- **Destructive commands name the resource and the consequence.** Revoking is
  not undoable and deleting is not revoking, so the prompts say which one the
  operator is about to do.
"""

from __future__ import annotations

import sys
from argparse import Namespace
from collections.abc import Sequence
from pathlib import Path

import click

from wasm.cli.app import Context, pass_context
from wasm.cli.panel_links import open_in_panel
from wasm.core.exceptions import CertificateError, DomainError, WASMError
from wasm.core.logger import Logger
from wasm.managers.cert_manager import CertManager
from wasm.validators.domain import validate_domain

#: Alternative spellings of the subcommands. They are in scripts, in the
#: published documentation and in muscle memory, so removing one is a breaking
#: change. The root group resolves the aliases of the group itself (``ssl``,
#: ``certificate``); these are local to ``wasm cert``.
COMMAND_ALIASES: dict[str, str] = {
    "new": "create",
    "obtain": "create",
    "ls": "list",
    "show": "info",
    "remove": "delete",
    "rm": "delete",
}


class CertGroup(click.Group):
    """A group that also answers to the older spellings of its subcommands."""

    def get_command(self, ctx: click.Context, name: str) -> click.Command | None:
        """
        Look a subcommand up by name or by alias.

        Args:
            ctx: The Click context.
            name: The name the user typed.

        Returns:
            The command, or None when there is no such subcommand.
        """
        return super().get_command(ctx, COMMAND_ALIASES.get(name, name))

    def resolve_command(
        self, ctx: click.Context, args: list[str]
    ) -> tuple[str | None, click.Command | None, list[str]]:
        """
        Resolve the next argument to a subcommand.

        Args:
            ctx: The Click context.
            args: The remaining arguments.

        Returns:
            The command name, the command, and the arguments left for it.
        """
        # Report the canonical name, so usage errors quote a command that
        # appears in --help.
        _, command, remaining = super().resolve_command(ctx, args)
        return (command.name if command else None), command, remaining


def _normalise_domain(ctx: click.Context, param: click.Parameter, value: str | None) -> str | None:
    """
    Turn one domain argument into its canonical form, or reject it.

    Args:
        ctx: The Click context.
        param: The parameter being processed.
        value: The domain as typed, or None when the option was not given.

    Returns:
        The normalised domain, or None.

    Raises:
        click.BadParameter: When the value is not a domain name. Reporting it
            here makes it a usage error, before certbot or the filesystem is
            touched.
    """
    if value is None:
        return None
    try:
        return validate_domain(value)
    except DomainError as exc:
        raise click.BadParameter(str(exc), ctx=ctx, param=param) from exc


def _normalise_domains(
    ctx: click.Context, param: click.Parameter, value: tuple[str, ...]
) -> tuple[str, ...]:
    """
    Turn every repetition of ``--domain`` into its canonical form.

    Args:
        ctx: The Click context.
        param: The parameter being processed.
        value: The domains as typed.

    Returns:
        The normalised domains, in the order they were given.

    Raises:
        click.BadParameter: When any value is not a domain name.
    """
    return tuple(str(_normalise_domain(ctx, param, item)) for item in value)


def _manager(verbose: bool) -> CertManager:
    """
    Build a certificate manager, refusing to continue without certbot.

    Args:
        verbose: Whether the manager should log the detail of each step.

    Returns:
        The manager.

    Raises:
        CertificateError: When certbot is not installed.
    """
    manager = CertManager(verbose=verbose)
    if not manager.is_installed():
        raise CertificateError(
            "Certbot is not installed",
            details=(
                "Install it with 'apt install certbot' on Debian and Ubuntu, "
                "or 'dnf install certbot' on Fedora and RHEL."
            ),
        )
    return manager


# -- The work, shared by the Click tree and the argparse handler --------------


def _create_certificate(
    manager: CertManager,
    logger: Logger,
    *,
    domains: Sequence[str],
    email: str | None,
    webroot: Path | None,
    standalone: bool,
    nginx: bool,
    apache: bool,
    expand: bool,
    dry_run: bool = False,
) -> int:
    """
    Obtain a certificate covering the given domains.

    Args:
        manager: The certificate manager.
        logger: Where progress is reported.
        domains: Validated domains. The first one names the certificate.
        email: Address for expiry warnings and account recovery.
        webroot: Directory the ACME challenge files are written to.
        standalone: Answer the challenge with certbot's own web server.
        nginx: Prove control through the running Nginx.
        apache: Prove control through the running Apache.
        expand: Add the domains to an existing certificate.
        dry_run: Ask Let's Encrypt for a staging certificate. The Click tree
            leaves this False: ``--dry-run`` is enforced at the command runner.

    Returns:
        Exit code.

    Raises:
        CertificateError: When issuance fails.
    """
    primary = domains[0]
    additional = list(domains[1:]) or None

    logger.info(f"Obtaining certificate for: {', '.join(domains)}")

    manager.obtain(
        domain=primary,
        email=email,
        webroot=webroot,
        standalone=standalone,
        nginx=nginx,
        apache=apache,
        dry_run=dry_run,
        additional_domains=additional,
        expand=expand,
    )

    if dry_run:
        logger.success("Dry run completed successfully")
    else:
        logger.success(f"Certificate obtained for: {primary}")
    return 0


def _list_certificates(manager: CertManager, logger: Logger) -> int:
    """
    Show every certificate certbot manages on this machine.

    Args:
        manager: The certificate manager.
        logger: Where the table is written.

    Returns:
        Exit code.
    """
    logger.header("SSL Certificates")

    certificates = manager.list_certificates()
    if not certificates:
        logger.info("No certificates found")
        return 0

    rows = []
    for cert in certificates:
        shown = ", ".join(cert.domains[:2])
        if len(cert.domains) > 2:
            shown += f" (+{len(cert.domains) - 2} more)"
        rows.append([cert.name, shown, cert.expiry or "Unknown"])

    logger.table(["Name", "Domains", "Expiry"], rows)
    return 0


def _show_certificate(manager: CertManager, logger: Logger, domain: str) -> int:
    """
    Show what one certificate covers and how long it is valid for.

    Args:
        manager: The certificate manager.
        logger: Where the detail is written.
        domain: Validated domain name.

    Returns:
        Exit code.

    Raises:
        CertificateError: When no certificate covers the domain.
    """
    info = manager.get_cert_info(domain)
    if not info:
        raise CertificateError(
            f"Certificate not found: {domain}",
            details="Run 'wasm cert list' to see what this machine holds.",
        )

    logger.header(f"Certificate: {domain}")
    logger.key_value("Name", info.name)
    logger.key_value("Domains", ", ".join(info.domains))
    logger.key_value("Expiry", info.expiry_full or "Unknown")

    if info.cert_path:
        logger.key_value("Certificate", info.cert_path)
    if info.key_path:
        logger.key_value("Private Key", info.key_path)

    test = manager.test_cert(domain)
    if test.valid:
        logger.blank()
        logger.success("Certificate is valid")
        logger.key_value("Valid from", test.not_before or "")
        logger.key_value("Valid until", test.not_after or "")

    return 0


def _renew_certificates(
    manager: CertManager,
    logger: Logger,
    *,
    domain: str | None,
    force: bool,
    dry_run: bool = False,
) -> int:
    """
    Renew one certificate, or every certificate that is due.

    Args:
        manager: The certificate manager.
        logger: Where progress is reported.
        domain: Validated domain to renew, or None for all of them.
        force: Renew even when the certificate is not near expiry.
        dry_run: Rehearse against the Let's Encrypt staging environment.

    Returns:
        Exit code.

    Raises:
        CertificateError: When renewal fails.
    """
    if domain:
        logger.info(f"Renewing certificate: {domain}")
    else:
        logger.info("Renewing all certificates that are due")
        if force:
            # Five renewals per domain per week, and this asks for one of each.
            logger.warning(
                "Forcing renewal of every certificate on this machine. "
                "Each one counts against the Let's Encrypt rate limit."
            )

    manager.renew(domain=domain, force=force, dry_run=dry_run)

    if dry_run:
        logger.success("Dry run completed successfully")
    else:
        logger.success("Certificate(s) renewed")
    return 0


def _revoke_certificate(manager: CertManager, logger: Logger, domain: str, *, delete: bool) -> int:
    """
    Revoke a certificate, having already confirmed it with the operator.

    Args:
        manager: The certificate manager.
        logger: Where progress is reported.
        domain: Validated domain name.
        delete: Also remove the files certbot keeps for the lineage.

    Returns:
        Exit code.

    Raises:
        CertificateError: When revocation fails.
    """
    logger.info(f"Revoking certificate: {domain}")
    manager.revoke(domain, delete=delete)
    logger.success(f"Certificate revoked: {domain}")
    return 0


def _delete_certificate(manager: CertManager, logger: Logger, domain: str) -> int:
    """
    Delete the files of a certificate, having already confirmed it.

    Args:
        manager: The certificate manager.
        logger: Where progress is reported.
        domain: Validated domain name.

    Returns:
        Exit code.

    Raises:
        CertificateError: When deletion fails.
    """
    logger.info(f"Deleting certificate: {domain}")
    manager.delete(domain)
    logger.success(f"Certificate deleted: {domain}")
    return 0


# -- The Click tree -----------------------------------------------------------


@click.group(cls=CertGroup, name="cert")
def cli() -> None:
    """
    Obtain, inspect and retire the SSL certificates of this server.

    Certificates come from Let's Encrypt through certbot. Issuing one is rate
    limited, so a certificate that already covers what you asked for is left
    alone rather than requested again.
    """


@cli.command("create", short_help="Obtain a certificate. Also 'new' and 'obtain'.")
@click.option(
    "-d",
    "--domain",
    "domains",
    metavar="DOMAIN",
    multiple=True,
    required=True,
    callback=_normalise_domains,
    help="Domain to cover. Repeat it for every extra name on the certificate.",
)
@click.option(
    "-e",
    "--email",
    metavar="ADDRESS",
    help="Address Let's Encrypt sends expiry warnings to.",
)
@click.option(
    "-w",
    "--webroot",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    help="Directory served at the domain, where the challenge files are written.",
)
@click.option(
    "--standalone",
    is_flag=True,
    help="Answer the challenge with certbot's own server. Port 80 must be free.",
)
@click.option("--nginx", is_flag=True, help="Answer the challenge through the running Nginx.")
@click.option("--apache", is_flag=True, help="Answer the challenge through the running Apache.")
@click.option(
    "--expand",
    is_flag=True,
    help="Add these domains to the existing certificate instead of leaving it alone.",
)
@pass_context
def create_command(
    ctx: Context,
    domains: tuple[str, ...],
    email: str | None,
    webroot: Path | None,
    standalone: bool,
    nginx: bool,
    apache: bool,
    expand: bool,
) -> None:
    """
    Obtain a certificate for one or more domains.

    The first --domain names the certificate; the rest travel on it as extra
    names. Without --standalone, --nginx, --apache or --webroot, WASM picks the
    method that suits the web server it finds running.
    """
    manager = _manager(ctx.verbose)
    _create_certificate(
        manager,
        ctx.logger,
        domains=domains,
        email=email,
        webroot=webroot,
        standalone=standalone,
        nginx=nginx,
        apache=apache,
        expand=expand,
    )


@cli.command("list", short_help="List every certificate. Also 'ls'.")
@click.option(
    "--open",
    "open_panel",
    is_flag=True,
    help="Print the panel URL for the certificate list, opening it if a display is available.",
)
@pass_context
def list_command(ctx: Context, open_panel: bool) -> None:
    """List the certificates this machine holds, with their expiry dates."""
    code = _list_certificates(_manager(ctx.verbose), ctx.logger)
    if open_panel and code == 0:
        open_in_panel("/certificates", logger=ctx.logger)


@cli.command("info", short_help="Show one certificate. Also 'show'.")
@click.argument("domain", callback=_normalise_domain)
@pass_context
def info_command(ctx: Context, domain: str) -> None:
    """Show what the certificate for DOMAIN covers and when it expires."""
    _show_certificate(_manager(ctx.verbose), ctx.logger, domain)


@cli.command("renew", short_help="Renew certificates that are due.")
@click.option(
    "-d",
    "--domain",
    metavar="DOMAIN",
    callback=_normalise_domain,
    help="Renew only this certificate. Default: every certificate that is due.",
)
@click.option(
    "--force",
    is_flag=True,
    help="Renew even when the certificate is not near expiry. Counts against the rate limit.",
)
@pass_context
def renew_command(ctx: Context, domain: str | None, force: bool) -> None:
    """
    Renew certificates before they expire.

    Certbot renews a certificate in its last thirty days, so running this on a
    fresh one does nothing unless you pass --force.
    """
    _renew_certificates(_manager(ctx.verbose), ctx.logger, domain=domain, force=force)


@cli.command("revoke", short_help="Revoke a certificate.")
@click.argument("domain", callback=_normalise_domain)
@click.option(
    "--delete/--keep-files",
    default=True,
    help="Also remove the certificate files afterwards. Default: remove them.",
)
@pass_context
def revoke_command(ctx: Context, domain: str, delete: bool) -> None:
    """
    Tell Let's Encrypt that the certificate for DOMAIN must no longer be trusted.

    Revocation cannot be undone, and the site keeps serving the revoked
    certificate until you issue a replacement and reload the web server.
    """
    consequence = " and delete its files" if delete else ""
    if not click.confirm(
        f"Revoke the certificate for {domain}{consequence}? "
        f"Browsers will reject {domain} until a new certificate is issued",
        default=False,
    ):
        ctx.logger.info("Cancelled")
        return

    _revoke_certificate(_manager(ctx.verbose), ctx.logger, domain, delete=delete)


@cli.command("delete", short_help="Delete a certificate. Also 'remove' and 'rm'.")
@click.argument("domain", callback=_normalise_domain)
@click.option("-f", "--force", is_flag=True, help="Do not ask for confirmation.")
@pass_context
def delete_command(ctx: Context, domain: str, force: bool) -> None:
    """
    Remove the certificate files for DOMAIN from this machine.

    The certificate is not revoked, so it stays valid until it expires. A web
    server still pointing at these files will fail to reload once they are gone.
    """
    if not force and not click.confirm(
        f"Delete the certificate files for {domain}? "
        "It is not revoked, and issuing a replacement counts against the "
        "Let's Encrypt rate limit",
        default=False,
    ):
        ctx.logger.info("Cancelled")
        return

    _delete_certificate(_manager(ctx.verbose), ctx.logger, domain)


# -- The argparse front end, until wasm.cli.parser is retired -----------------


def handle_cert(args: Namespace) -> int:
    """
    Handle cert commands coming from the argparse parser.

    Args:
        args: Parsed arguments.

    Returns:
        Exit code.
    """
    handlers = {
        "create": _handle_create,
        "obtain": _handle_create,
        "new": _handle_create,
        "list": _handle_list,
        "ls": _handle_list,
        "info": _handle_info,
        "show": _handle_info,
        "renew": _handle_renew,
        "revoke": _handle_revoke,
        "delete": _handle_delete,
        "remove": _handle_delete,
        "rm": _handle_delete,
    }

    handler = handlers.get(args.action)
    if not handler:
        print(f"Unknown action: {args.action}", file=sys.stderr)
        return 1

    try:
        return handler(args)
    except WASMError as exc:
        logger = Logger(verbose=args.verbose)
        logger.error(str(exc))
        if exc.details:
            logger.info(exc.details)
        return 1


def _handle_create(args: Namespace) -> int:
    """
    Handle ``cert create``.

    Args:
        args: Parsed arguments.

    Returns:
        Exit code.
    """
    logger = Logger(verbose=args.verbose)
    return _create_certificate(
        _manager(args.verbose),
        logger,
        domains=[validate_domain(d) for d in args.domain],
        email=args.email,
        webroot=Path(args.webroot) if args.webroot else None,
        standalone=args.standalone,
        nginx=args.nginx,
        apache=args.apache,
        expand=getattr(args, "expand", False),
        dry_run=args.dry_run,
    )


def _handle_list(args: Namespace) -> int:
    """
    Handle ``cert list``.

    Args:
        args: Parsed arguments.

    Returns:
        Exit code.
    """
    return _list_certificates(_manager(args.verbose), Logger(verbose=args.verbose))


def _handle_info(args: Namespace) -> int:
    """
    Handle ``cert info``.

    Args:
        args: Parsed arguments.

    Returns:
        Exit code.
    """
    return _show_certificate(
        _manager(args.verbose),
        Logger(verbose=args.verbose),
        validate_domain(args.domain),
    )


def _handle_renew(args: Namespace) -> int:
    """
    Handle ``cert renew``.

    Args:
        args: Parsed arguments.

    Returns:
        Exit code.
    """
    return _renew_certificates(
        _manager(args.verbose),
        Logger(verbose=args.verbose),
        domain=validate_domain(args.domain) if args.domain else None,
        force=args.force,
        dry_run=args.dry_run,
    )


def _handle_revoke(args: Namespace) -> int:
    """
    Handle ``cert revoke``.

    Args:
        args: Parsed arguments.

    Returns:
        Exit code.
    """
    logger = Logger(verbose=args.verbose)
    domain = validate_domain(args.domain)

    if not click.confirm(
        f"Revoke the certificate for {domain}? "
        f"Browsers will reject {domain} until a new certificate is issued",
        default=False,
    ):
        logger.info("Cancelled")
        return 0

    return _revoke_certificate(_manager(args.verbose), logger, domain, delete=args.delete)


def _handle_delete(args: Namespace) -> int:
    """
    Handle ``cert delete``.

    Args:
        args: Parsed arguments.

    Returns:
        Exit code.
    """
    logger = Logger(verbose=args.verbose)
    domain = validate_domain(args.domain)

    if not args.force and not click.confirm(
        f"Delete the certificate files for {domain}? "
        "It is not revoked, and issuing a replacement counts against the "
        "Let's Encrypt rate limit",
        default=False,
    ):
        logger.info("Cancelled")
        return 0

    return _delete_certificate(_manager(args.verbose), logger, domain)
