"""
Site command handlers for WASM.
"""

import sys
from argparse import Namespace

from wasm.core.logger import Logger
from wasm.core.exceptions import WASMError
from wasm.managers.nginx_manager import NginxManager
from wasm.managers.apache_manager import ApacheManager
from wasm.validators.domain import validate_domain


def handle_site(args: Namespace) -> int:
    """
    Handle site commands.
    
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
        if args.verbose:
            import traceback
            traceback.print_exc()
        return 1


def _get_manager(webserver: str, verbose: bool = False):
    """Get the appropriate site manager."""
    if webserver == "apache":
        return ApacheManager(verbose=verbose)
    return NginxManager(verbose=verbose)


def _handle_create(args: Namespace) -> int:
    """Handle site create command."""
    logger = Logger(verbose=args.verbose)

    from wasm.validators.domain import should_include_www

    domain = validate_domain(args.domain)
    manager = _get_manager(args.webserver, verbose=args.verbose)

    include_www = getattr(args, "www", False) and should_include_www(domain)
    server_names = domain
    if include_www:
        server_names = f"{domain} www.{domain}"
        logger.info(f"Including www.{domain}")

    context = {
        "port": args.port,
        "ssl": False,  # SSL will be handled separately
        "server_names": server_names,
    }

    logger.info(f"Creating site: {domain}")
    manager.create_site(domain, template=args.template, context=context)
    manager.enable_site(domain)
    manager.reload()
    
    logger.success(f"Site created and enabled: {domain}")
    return 0


def _handle_list(args: Namespace) -> int:
    """Handle site list command."""
    logger = Logger(verbose=args.verbose)
    
    logger.header("Web Server Sites")
    
    all_sites = []
    
    # List Nginx sites
    if args.webserver in ["nginx", "all"]:
        nginx = NginxManager(verbose=args.verbose)
        if nginx.is_installed():
            for site in nginx.list_sites():
                site["webserver"] = "nginx"
                all_sites.append(site)
    
    # List Apache sites
    if args.webserver in ["apache", "all"]:
        apache = ApacheManager(verbose=args.verbose)
        if apache.is_installed():
            for site in apache.list_sites():
                site["webserver"] = "apache"
                all_sites.append(site)
    
    if not all_sites:
        logger.info("No sites found")
        return 0
    
    headers = ["Domain", "Enabled", "Web Server"]
    rows = []
    
    for site in all_sites:
        status = "✓" if site["enabled"] else "✗"
        rows.append([site["domain"], status, site["webserver"]])
    
    logger.table(headers, rows)
    
    return 0


def _handle_enable(args: Namespace) -> int:
    """Handle site enable command."""
    logger = Logger(verbose=args.verbose)
    
    domain = validate_domain(args.domain)
    
    # Try Nginx first, then Apache
    nginx = NginxManager(verbose=args.verbose)
    apache = ApacheManager(verbose=args.verbose)
    
    if nginx.site_exists(domain):
        nginx.enable_site(domain)
        nginx.reload()
        logger.success(f"Site enabled (nginx): {domain}")
    elif apache.site_exists(domain):
        apache.enable_site(domain)
        apache.reload()
        logger.success(f"Site enabled (apache): {domain}")
    else:
        raise WASMError(f"Site not found: {domain}")
    
    return 0


def _handle_disable(args: Namespace) -> int:
    """Handle site disable command."""
    logger = Logger(verbose=args.verbose)
    
    domain = validate_domain(args.domain)
    
    nginx = NginxManager(verbose=args.verbose)
    apache = ApacheManager(verbose=args.verbose)
    
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
    
    return 0


def _handle_delete(args: Namespace) -> int:
    """Handle site delete command."""
    logger = Logger(verbose=args.verbose)
    
    domain = validate_domain(args.domain)
    
    if not args.force:
        response = input(f"Delete site '{domain}'? [y/N] ")
        if response.lower() != "y":
            logger.info("Aborted")
            return 0
    
    nginx = NginxManager(verbose=args.verbose)
    apache = ApacheManager(verbose=args.verbose)
    
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
    
    if not deleted:
        raise WASMError(f"Site not found: {domain}")
    
    return 0


def _handle_show(args: Namespace) -> int:
    """Handle site show command."""
    logger = Logger(verbose=args.verbose)
    
    domain = validate_domain(args.domain)
    
    nginx = NginxManager(verbose=args.verbose)
    apache = ApacheManager(verbose=args.verbose)
    
    config = None
    
    if nginx.site_exists(domain):
        config = nginx.get_site_config(domain)
    elif apache.site_exists(domain):
        config = apache.get_site_config(domain)
    
    if not config:
        raise WASMError(f"Site not found: {domain}")
    
    print(config)
    
    return 0
