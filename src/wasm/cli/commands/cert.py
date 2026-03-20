"""
Certificate command handlers for WASM.
"""

import sys
from argparse import Namespace
from pathlib import Path

from wasm.core.logger import Logger
from wasm.core.exceptions import WASMError
from wasm.managers.cert_manager import CertManager
from wasm.validators.domain import validate_domain


def handle_cert(args: Namespace) -> int:
    """
    Handle cert commands.
    
    Args:
        args: Parsed arguments.
        
    Returns:
        Exit code.
    """
    action = args.action
    
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
        if args.verbose:
            import traceback
            traceback.print_exc()
        return 1


def _handle_create(args: Namespace) -> int:
    """Handle cert create command."""
    logger = Logger(verbose=args.verbose)
    manager = CertManager(verbose=args.verbose)
    
    if not manager.is_installed():
        raise WASMError("Certbot is not installed")
    
    # Validate domains
    domains = [validate_domain(d) for d in args.domain]
    primary_domain = domains[0]
    additional_domains = domains[1:] if len(domains) > 1 else None
    
    logger.info(f"Obtaining certificate for: {', '.join(domains)}")
    
    webroot = Path(args.webroot) if args.webroot else None
    
    manager.obtain(
        domain=primary_domain,
        email=args.email,
        webroot=webroot,
        standalone=args.standalone,
        nginx=args.nginx,
        apache=args.apache,
        dry_run=args.dry_run,
        additional_domains=additional_domains,
        expand=getattr(args, "expand", False),
    )
    
    if args.dry_run:
        logger.success("Dry run completed successfully")
    else:
        logger.success(f"Certificate obtained for: {primary_domain}")
    
    return 0


def _handle_list(args: Namespace) -> int:
    """Handle cert list command."""
    logger = Logger(verbose=args.verbose)
    manager = CertManager(verbose=args.verbose)
    
    if not manager.is_installed():
        raise WASMError("Certbot is not installed")
    
    logger.header("SSL Certificates")
    
    certificates = manager.list_certificates()
    
    if not certificates:
        logger.info("No certificates found")
        return 0
    
    headers = ["Name", "Domains", "Expiry"]
    rows = []
    
    for cert in certificates:
        domains = ", ".join(cert.get("domains", [])[:2])
        if len(cert.get("domains", [])) > 2:
            domains += f" (+{len(cert['domains']) - 2} more)"
        expiry = cert.get("expiry", "Unknown")
        rows.append([cert.get("name", ""), domains, expiry])
    
    logger.table(headers, rows)
    
    return 0


def _handle_info(args: Namespace) -> int:
    """Handle cert info command."""
    logger = Logger(verbose=args.verbose)
    manager = CertManager(verbose=args.verbose)
    
    if not manager.is_installed():
        raise WASMError("Certbot is not installed")
    
    domain = validate_domain(args.domain)
    
    info = manager.get_cert_info(domain)
    
    if not info:
        raise WASMError(f"Certificate not found: {domain}")
    
    logger.header(f"Certificate: {domain}")
    
    logger.key_value("Name", info.get("name", ""))
    logger.key_value("Domains", ", ".join(info.get("domains", [])))
    logger.key_value("Expiry", info.get("expiry_full", "Unknown"))
    
    if info.get("cert_path"):
        logger.key_value("Certificate", info["cert_path"])
    if info.get("key_path"):
        logger.key_value("Private Key", info["key_path"])
    
    # Test certificate
    test = manager.test_cert(domain)
    if test.get("valid"):
        logger.blank()
        logger.success("Certificate is valid")
        logger.key_value("Valid from", test.get("not_before", ""))
        logger.key_value("Valid until", test.get("not_after", ""))
    
    return 0


def _handle_renew(args: Namespace) -> int:
    """Handle cert renew command."""
    logger = Logger(verbose=args.verbose)
    manager = CertManager(verbose=args.verbose)
    
    if not manager.is_installed():
        raise WASMError("Certbot is not installed")
    
    domain = args.domain
    if domain:
        domain = validate_domain(domain)
        logger.info(f"Renewing certificate: {domain}")
    else:
        logger.info("Renewing all certificates")
    
    manager.renew(
        domain=domain,
        force=args.force,
        dry_run=args.dry_run,
    )
    
    if args.dry_run:
        logger.success("Dry run completed successfully")
    else:
        logger.success("Certificate(s) renewed")
    
    return 0


def _handle_revoke(args: Namespace) -> int:
    """Handle cert revoke command."""
    logger = Logger(verbose=args.verbose)
    manager = CertManager(verbose=args.verbose)
    
    if not manager.is_installed():
        raise WASMError("Certbot is not installed")
    
    domain = validate_domain(args.domain)
    
    response = input(f"Revoke certificate for '{domain}'? [y/N] ")
    if response.lower() != "y":
        logger.info("Aborted")
        return 0
    
    logger.info(f"Revoking certificate: {domain}")
    manager.revoke(domain, delete=args.delete)
    logger.success(f"Certificate revoked: {domain}")
    
    return 0


def _handle_delete(args: Namespace) -> int:
    """Handle cert delete command."""
    logger = Logger(verbose=args.verbose)
    manager = CertManager(verbose=args.verbose)
    
    if not manager.is_installed():
        raise WASMError("Certbot is not installed")
    
    domain = validate_domain(args.domain)
    
    if not args.force:
        response = input(f"Delete certificate for '{domain}'? [y/N] ")
        if response.lower() != "y":
            logger.info("Aborted")
            return 0
    
    logger.info(f"Deleting certificate: {domain}")
    manager.delete(domain)
    logger.success(f"Certificate deleted: {domain}")
    
    return 0
