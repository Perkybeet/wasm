# Copyright (c) 2024-2025 Yago López Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
Store CLI commands for WASM.

Provides CLI handlers for managing the SQLite persistence store:
- Initialize/migrate database
- Import legacy apps
- Statistics
- Export/import
"""

import json
import sys
from argparse import Namespace
from pathlib import Path
from typing import List, Dict, Any

from wasm.core.logger import Logger
from wasm.core.config import Config
from wasm.core.exceptions import WASMError


def _detect_app_type(app_path: Path) -> str:
    """
    Detect application type from project files.
    
    Detection order:
    1. next.config.js/ts/mjs → nextjs
    2. vite.config.js/ts → vite
    3. package.json exists → nodejs
    4. requirements.txt / setup.py / pyproject.toml → python
    5. index.html → static
    6. Default → unknown
    """
    # Next.js detection
    next_configs = ["next.config.js", "next.config.ts", "next.config.mjs"]
    for config_file in next_configs:
        if (app_path / config_file).exists():
            return "nextjs"
    
    # Also check package.json for next dependency
    package_json = app_path / "package.json"
    if package_json.exists():
        try:
            import json as json_module
            with open(package_json) as f:
                pkg = json_module.load(f)
                deps = pkg.get("dependencies", {})
                dev_deps = pkg.get("devDependencies", {})
                all_deps = {**deps, **dev_deps}
                
                if "next" in all_deps:
                    return "nextjs"
                if "vite" in all_deps:
                    return "vite"
        except Exception:
            pass
    
    # Vite detection (config file)
    vite_configs = ["vite.config.js", "vite.config.ts"]
    for config_file in vite_configs:
        if (app_path / config_file).exists():
            return "vite"
    
    # Generic Node.js (has package.json but not next/vite)
    if package_json.exists():
        return "nodejs"
    
    # Python detection
    python_markers = ["requirements.txt", "setup.py", "pyproject.toml", "Pipfile"]
    for marker in python_markers:
        if (app_path / marker).exists():
            return "python"
    
    # Static site detection
    if (app_path / "index.html").exists():
        return "static"
    
    return "unknown"


def handle_store(args: Namespace) -> int:
    """Handle wasm store <action> commands."""
    action = getattr(args, "action", None)
    verbose = getattr(args, "verbose", False)
    
    if not action:
        logger = Logger(verbose=verbose)
        logger.error("No action specified")
        logger.info("Use: wasm store --help")
        return 1
    
    handlers = {
        "init": _store_init,
        "stats": _store_stats,
        "import": _store_import,
        "export": _store_export,
        "sync": _store_sync,
        "path": _store_path,
    }
    
    handler = handlers.get(action)
    if not handler:
        logger = Logger(verbose=verbose)
        logger.error(f"Unknown action: {action}")
        return 1
    
    return handler(args, verbose)


def _store_init(args: Namespace, verbose: bool) -> int:
    """Initialize the store database."""
    logger = Logger(verbose=verbose)
    
    from wasm.core.store import get_store, WASMStore
    
    logger.header("WASM Store Initialization")
    
    try:
        # Reset and reinitialize
        WASMStore.reset_instance()
        store = get_store()
        
        logger.success(f"Store initialized at: {store.db_path}")
        logger.info(f"Schema version: 1")
        
        return 0
    except Exception as e:
        logger.error(f"Failed to initialize store: {e}")
        return 1


def _store_stats(args: Namespace, verbose: bool) -> int:
    """Show store statistics."""
    logger = Logger(verbose=verbose)
    json_output = getattr(args, "json", False)
    
    from wasm.core.store import get_store
    
    try:
        store = get_store()
        stats = store.get_statistics()
        
        if json_output:
            print(json.dumps(stats, indent=2))
            return 0
        
        logger.header("WASM Store Statistics")
        logger.blank()
        
        logger.key_value("Database Path", str(store.db_path))
        logger.blank()
        
        logger.info("Resources:")
        logger.key_value("  Applications", str(stats['total_apps']))
        logger.key_value("    Running", str(stats['running_apps']))
        logger.key_value("  Sites", str(stats['total_sites']))
        logger.key_value("  Services", str(stats['total_services']))
        logger.key_value("  Databases", str(stats['total_databases']))
        
        if stats['apps_by_type']:
            logger.blank()
            logger.info("Applications by Type:")
            for app_type, count in stats['apps_by_type'].items():
                logger.key_value(f"  {app_type}", str(count))
        
        if stats['databases_by_engine']:
            logger.blank()
            logger.info("Databases by Engine:")
            for engine, count in stats['databases_by_engine'].items():
                logger.key_value(f"  {engine}", str(count))
        
        return 0
    except Exception as e:
        logger.error(f"Failed to get statistics: {e}")
        return 1


def _store_import(args: Namespace, verbose: bool) -> int:
    """Import legacy apps from systemd services and nginx configs."""
    logger = Logger(verbose=verbose)
    
    from wasm.core.store import get_store, App, Site, Service, AppStatus
    from wasm.core.config import (
        NGINX_SITES_AVAILABLE,
        NGINX_SITES_ENABLED,
        APACHE_SITES_AVAILABLE,
        SYSTEMD_DIR,
    )
    from wasm.core.utils import domain_to_app_name
    
    store = get_store()
    config = Config()
    
    logger.header("Import Legacy Applications")
    logger.blank()
    
    imported_apps = 0
    imported_sites = 0
    imported_services = 0
    
    # 1. Import from Nginx sites
    if NGINX_SITES_AVAILABLE.exists():
        logger.step(1, 3, "Scanning Nginx sites")
        for config_file in NGINX_SITES_AVAILABLE.iterdir():
            if config_file.is_file() and config_file.name != "default":
                domain = config_file.name
                site_exists = store.get_site(domain) is not None
                
                enabled = (NGINX_SITES_ENABLED / domain).exists()
                
                # Try to detect if it's a static site or proxy
                is_static = False
                proxy_port = None
                try:
                    content = config_file.read_text()
                    if "proxy_pass" in content:
                        # Extract port from proxy_pass
                        import re
                        match = re.search(r"proxy_pass\s+http://(?:127\.0\.0\.1|localhost):(\d+)", content)
                        if match:
                            proxy_port = int(match.group(1))
                    else:
                        is_static = True
                except Exception:
                    pass
                
                # Check if we have an app directory
                # Try multiple naming conventions
                app_name = domain_to_app_name(domain)
                possible_paths = [
                    config.apps_directory / app_name,           # africarsrent-com
                    config.apps_directory / domain,              # africarsrent.com
                    config.apps_directory / f"wasm-{app_name}",  # wasm-africarsrent-com
                ]
                
                app_path = None
                for path in possible_paths:
                    if path.exists():
                        app_path = path
                        break
                
                # Create or update app record if app directory exists
                app_id = None
                if app_path:
                    # Detect app type from project files
                    app_type = _detect_app_type(app_path)
                    
                    existing_app = store.get_app(domain)
                    if not existing_app:
                        app = App(
                            domain=domain,
                            app_type=app_type,
                            app_path=str(app_path),
                            webserver="nginx",
                            ssl_enabled=(NGINX_SITES_ENABLED / domain).exists(),
                            status=AppStatus.UNKNOWN.value,
                            is_static=is_static,
                            port=proxy_port,
                        )
                        app = store.create_app(app)
                        app_id = app.id
                        imported_apps += 1
                        logger.substep(f"Imported app: {domain} ({app_type})")
                    elif existing_app.app_type == "unknown" and app_type != "unknown":
                        # Update existing app's type if it was unknown
                        existing_app.app_type = app_type
                        store.update_app(existing_app)
                        app_id = existing_app.id
                        imported_apps += 1
                        logger.substep(f"Updated app type: {domain} → {app_type}")
                    else:
                        app_id = existing_app.id
                
                # Create site record only if it doesn't exist
                if not site_exists:
                    site = Site(
                        app_id=app_id,
                        domain=domain,
                        webserver="nginx",
                        config_path=str(config_file),
                        enabled=enabled,
                        is_static=is_static,
                        proxy_port=proxy_port,
                    )
                    store.create_site(site)
                    imported_sites += 1
    else:
        logger.step(1, 3, "No Nginx sites found")
    
    # 2. Import from Apache sites
    if APACHE_SITES_AVAILABLE.exists():
        logger.step(2, 3, "Scanning Apache sites")
        for config_file in APACHE_SITES_AVAILABLE.iterdir():
            if config_file.is_file() and not config_file.name.startswith("000-"):
                domain = config_file.name.replace(".conf", "")
                
                if store.get_site(domain):
                    continue
                
                # Similar logic as nginx...
                site = Site(
                    domain=domain,
                    webserver="apache",
                    config_path=str(config_file),
                    enabled=True,
                )
                store.create_site(site)
                imported_sites += 1
    else:
        logger.step(2, 3, "No Apache sites found")
    
    # 3. Import from systemd services (wasm-* prefix)
    logger.step(3, 3, "Scanning systemd services")
    if SYSTEMD_DIR.exists():
        for service_file in SYSTEMD_DIR.glob("wasm-*.service"):
            # Extract app name from service file name
            service_name = service_file.stem  # wasm-example-com
            app_name = service_name[5:]  # example-com (remove wasm- prefix)
            
            if store.get_service(app_name):
                continue
            
            # Parse service file for details
            working_dir = ""
            command = ""
            user = "www-data"
            group = "www-data"
            port = None
            env = {}
            
            try:
                content = service_file.read_text()
                import re
                
                wd_match = re.search(r"WorkingDirectory=(.+)", content)
                if wd_match:
                    working_dir = wd_match.group(1)
                
                exec_match = re.search(r"ExecStart=(.+)", content)
                if exec_match:
                    command = exec_match.group(1)
                
                user_match = re.search(r"User=(.+)", content)
                if user_match:
                    user = user_match.group(1)
                
                group_match = re.search(r"Group=(.+)", content)
                if group_match:
                    group = group_match.group(1)
                
                # Parse environment variables
                for env_match in re.finditer(r"Environment=\"?([^=]+)=([^\"]+)\"?", content):
                    env[env_match.group(1)] = env_match.group(2)
                    if env_match.group(1) == "PORT":
                        port = int(env_match.group(2))
            except Exception:
                pass
            
            # Get app_id if we have an app for this domain
            domain = app_name.replace("-", ".")
            app = store.get_app(domain)
            app_id = app.id if app else None
            
            service = Service(
                app_id=app_id,
                name=app_name,
                unit_file=str(service_file),
                working_directory=working_dir,
                command=command,
                user=user,
                group=group,
                port=port,
                environment=env,
            )
            store.create_service(service)
            imported_services += 1
            logger.substep(f"Imported service: {app_name}")
    
    logger.blank()
    logger.success(f"Import complete!")
    logger.key_value("  Apps", str(imported_apps))
    logger.key_value("  Sites", str(imported_sites))
    logger.key_value("  Services", str(imported_services))
    
    return 0


def _store_export(args: Namespace, verbose: bool) -> int:
    """Export store data to JSON."""
    logger = Logger(verbose=verbose)
    output_file = getattr(args, "output", None)
    
    from wasm.core.store import get_store
    
    try:
        store = get_store()
        
        data = {
            "apps": [app.to_dict() for app in store.list_apps()],
            "sites": [site.to_dict() for site in store.list_sites()],
            "services": [svc.to_dict() for svc in store.list_services()],
            "databases": [db.to_dict() for db in store.list_databases()],
            "statistics": store.get_statistics(),
        }
        
        json_output = json.dumps(data, indent=2, default=str)
        
        if output_file:
            Path(output_file).write_text(json_output)
            logger.success(f"Exported to: {output_file}")
        else:
            print(json_output)
        
        return 0
    except Exception as e:
        logger.error(f"Export failed: {e}")
        return 1


def _store_sync(args: Namespace, verbose: bool) -> int:
    """Sync store with actual systemd service states."""
    logger = Logger(verbose=verbose)
    
    from wasm.core.store import get_store, AppStatus
    from wasm.managers.service_manager import ServiceManager
    
    store = get_store()
    service_manager = ServiceManager(verbose=verbose)
    
    logger.header("Sync Service States")
    
    synced_services = 0
    synced_apps = 0
    services = store.list_services()
    
    for service in services:
        # Get actual state from systemd
        systemd_status = service_manager.get_status(service.name)
        
        active = systemd_status.get("active", False)
        enabled = systemd_status.get("enabled", False)
        
        # Convert current store status to bool for comparison
        current_active = service.status == "active"
        current_enabled = service.enabled
        
        # Update service if different
        if current_active != active or current_enabled != enabled:
            store.update_service_status(service.name, active=active, enabled=enabled)
            logger.substep(f"Updated service {service.name}: active={active}, enabled={enabled}")
            synced_services += 1
        
        # Also update the associated app status
        if service.app_id:
            app = store.get_app_by_id(service.app_id)
            if app:
                new_status = AppStatus.RUNNING.value if active else AppStatus.STOPPED.value
                if app.status != new_status:
                    store.update_app_status(app.domain, new_status)
                    logger.substep(f"Updated app {app.domain}: {new_status}")
                    synced_apps += 1
    
    logger.blank()
    logger.success(f"Sync complete! Updated {synced_services} services, {synced_apps} apps.")
    
    return 0


def _store_path(args: Namespace, verbose: bool) -> int:
    """Show the database file path."""
    from wasm.core.store import get_store
    
    store = get_store()
    print(store.db_path)
    
    return 0
