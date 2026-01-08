# Copyright (c) 2024-2025 Yago López Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
Database CLI commands for WASM.

Provides CLI handlers for database management:
- Engine installation/management
- Database creation/deletion
- User management
- Backup/restore
- Query execution
"""

import json
import os
import subprocess
import sys
from argparse import Namespace
from typing import Optional

from wasm.core.logger import Logger
from wasm.core.config import Config
from wasm.core.exceptions import DatabaseError, DatabaseNotFoundError
from wasm.managers.database import (
    DatabaseRegistry,
    get_db_manager,
    BaseDatabaseManager,
)


def handle_db(args: Namespace) -> int:
    """Handle wasm db <action> commands."""
    action = getattr(args, "action", None)
    verbose = getattr(args, "verbose", False)
    
    if not action:
        logger = Logger(verbose=verbose)
        logger.error("No action specified")
        logger.info("Use: wasm db --help")
        return 1
    
    handlers = {
        # Engine management
        "install": _db_install,
        "uninstall": _db_uninstall,
        "status": _db_status,
        "start": _db_engine_start,
        "stop": _db_engine_stop,
        "restart": _db_engine_restart,
        "engines": _db_engines,
        
        # Database management
        "create": _db_create,
        "drop": _db_drop,
        "list": _db_list,
        "info": _db_info,
        
        # User management
        "user-create": _db_user_create,
        "user-delete": _db_user_delete,
        "user-list": _db_user_list,
        "grant": _db_grant,
        "revoke": _db_revoke,
        
        # Backup & restore
        "backup": _db_backup,
        "restore": _db_restore,
        "backups": _db_backups,
        
        # Query & connection
        "query": _db_query,
        "connect": _db_connect,
        "connection-string": _db_connection_string,
        "config": _db_config,
    }
    
    handler = handlers.get(action)
    if not handler:
        logger = Logger(verbose=verbose)
        logger.error(f"Unknown action: {action}")
        return 1
    
    return handler(args, verbose)


def _get_manager(engine: str, verbose: bool) -> Optional[BaseDatabaseManager]:
    """Get database manager or show error."""
    logger = Logger(verbose=verbose)
    
    if not engine:
        logger.error("Database engine is required")
        logger.info("Available engines: " + ", ".join(DatabaseRegistry.list_engines()))
        return None
    
    manager = get_db_manager(engine, verbose=verbose)
    if not manager:
        logger.error(f"Unknown database engine: {engine}")
        logger.info("Available engines: " + ", ".join(DatabaseRegistry.list_engines()))
        return None
    
    return manager


# ==================== Engine Management ====================

def _db_install(args: Namespace, verbose: bool) -> int:
    """Install a database engine."""
    logger = Logger(verbose=verbose)
    engine = getattr(args, "engine", None)
    
    manager = _get_manager(engine, verbose)
    if not manager:
        return 1
    
    try:
        if manager.is_installed():
            version = manager.get_version()
            logger.info(f"{manager.DISPLAY_NAME} is already installed (v{version})")
            return 0
        
        logger.step(1, 2, f"Installing {manager.DISPLAY_NAME}...")
        manager.install()
        
        logger.step(2, 2, "Installation complete")
        version = manager.get_version()
        logger.success(f"{manager.DISPLAY_NAME} v{version} installed successfully")
        
        return 0
    except DatabaseError as e:
        logger.error(str(e))
        return 1


def _db_uninstall(args: Namespace, verbose: bool) -> int:
    """Uninstall a database engine."""
    logger = Logger(verbose=verbose)
    engine = getattr(args, "engine", None)
    purge = getattr(args, "purge", False)
    force = getattr(args, "force", False)
    
    manager = _get_manager(engine, verbose)
    if not manager:
        return 1
    
    try:
        if not manager.is_installed():
            logger.info(f"{manager.DISPLAY_NAME} is not installed")
            return 0
        
        # Confirm if not forced
        if not force:
            msg = f"Uninstall {manager.DISPLAY_NAME}?"
            if purge:
                msg += " (ALL DATA WILL BE DELETED)"
            
            try:
                response = input(f"{msg} [y/N]: ")
                if response.lower() not in ["y", "yes"]:
                    logger.info("Cancelled")
                    return 0
            except (EOFError, KeyboardInterrupt):
                logger.info("\nCancelled")
                return 0
        
        logger.step(1, 2, f"Uninstalling {manager.DISPLAY_NAME}...")
        manager.uninstall(purge=purge)
        
        logger.step(2, 2, "Uninstallation complete")
        logger.success(f"{manager.DISPLAY_NAME} uninstalled")
        
        return 0
    except DatabaseError as e:
        logger.error(str(e))
        return 1


def _db_status(args: Namespace, verbose: bool) -> int:
    """Show database engine status."""
    logger = Logger(verbose=verbose)
    engine = getattr(args, "engine", None)
    json_output = getattr(args, "json", False)
    
    if engine:
        managers = [_get_manager(engine, verbose)]
        if managers[0] is None:
            return 1
    else:
        # Show all installed engines
        managers = DatabaseRegistry.get_all_managers(verbose=verbose)
    
    statuses = []
    for manager in managers:
        status = manager.get_status()
        statuses.append(status)
    
    if json_output:
        print(json.dumps(statuses, indent=2))
        return 0
    
    for status in statuses:
        installed = "✓" if status["installed"] else "✗"
        running = "●" if status.get("running") else "○"
        version = status.get("version", "N/A")
        
        print(f"\n{status['display_name']}")
        print(f"  Installed: {installed}")
        if status["installed"]:
            print(f"  Version:   {version}")
            print(f"  Status:    {running} {'running' if status.get('running') else 'stopped'}")
            print(f"  Port:      {status['port']}")
            print(f"  Service:   {status['service']}")
    
    return 0


def _db_engine_start(args: Namespace, verbose: bool) -> int:
    """Start a database engine."""
    logger = Logger(verbose=verbose)
    engine = getattr(args, "engine", None)
    
    manager = _get_manager(engine, verbose)
    if not manager:
        return 1
    
    try:
        if not manager.is_installed():
            logger.error(f"{manager.DISPLAY_NAME} is not installed")
            logger.info(f"Install with: wasm db install {manager.ENGINE_NAME}")
            return 1
        
        if manager.is_running():
            logger.info(f"{manager.DISPLAY_NAME} is already running")
            return 0
        
        manager.start()
        logger.success(f"{manager.DISPLAY_NAME} started")
        return 0
    except DatabaseError as e:
        logger.error(str(e))
        return 1


def _db_engine_stop(args: Namespace, verbose: bool) -> int:
    """Stop a database engine."""
    logger = Logger(verbose=verbose)
    engine = getattr(args, "engine", None)
    
    manager = _get_manager(engine, verbose)
    if not manager:
        return 1
    
    try:
        if not manager.is_running():
            logger.info(f"{manager.DISPLAY_NAME} is not running")
            return 0
        
        manager.stop()
        logger.success(f"{manager.DISPLAY_NAME} stopped")
        return 0
    except DatabaseError as e:
        logger.error(str(e))
        return 1


def _db_engine_restart(args: Namespace, verbose: bool) -> int:
    """Restart a database engine."""
    logger = Logger(verbose=verbose)
    engine = getattr(args, "engine", None)
    
    manager = _get_manager(engine, verbose)
    if not manager:
        return 1
    
    try:
        if not manager.is_installed():
            logger.error(f"{manager.DISPLAY_NAME} is not installed")
            return 1
        
        manager.restart()
        logger.success(f"{manager.DISPLAY_NAME} restarted")
        return 0
    except DatabaseError as e:
        logger.error(str(e))
        return 1


def _db_engines(args: Namespace, verbose: bool) -> int:
    """List available database engines."""
    logger = Logger(verbose=verbose)
    json_output = getattr(args, "json", False)
    
    engines = []
    for engine in DatabaseRegistry.list_engines():
        manager = get_db_manager(engine, verbose=verbose)
        if manager:
            engines.append({
                "name": manager.ENGINE_NAME,
                "display_name": manager.DISPLAY_NAME,
                "installed": manager.is_installed(),
                "version": manager.get_version() if manager.is_installed() else None,
                "port": manager.DEFAULT_PORT,
            })
    
    if json_output:
        print(json.dumps(engines, indent=2))
        return 0
    
    print("\nAvailable Database Engines:")
    print("-" * 50)
    
    for eng in engines:
        installed = "✓" if eng["installed"] else " "
        version = f"v{eng['version']}" if eng["version"] else "not installed"
        print(f"  [{installed}] {eng['display_name']:<20} {version:<15} (port {eng['port']})")
    
    print()
    return 0


# ==================== Database Management ====================

def _db_create(args: Namespace, verbose: bool) -> int:
    """Create a new database."""
    logger = Logger(verbose=verbose)
    engine = getattr(args, "engine", None)
    name = getattr(args, "name", None)
    owner = getattr(args, "owner", None)
    encoding = getattr(args, "encoding", None)
    
    if not name:
        logger.error("Database name is required")
        return 1
    
    manager = _get_manager(engine, verbose)
    if not manager:
        return 1
    
    try:
        if not manager.is_installed():
            logger.error(f"{manager.DISPLAY_NAME} is not installed")
            return 1
        
        if not manager.is_running():
            logger.error(f"{manager.DISPLAY_NAME} is not running")
            logger.info(f"Start with: wasm db start {manager.ENGINE_NAME}")
            return 1
        
        info = manager.create_database(name, owner=owner, encoding=encoding)
        
        # Register in store
        from wasm.core.store import get_store, Database
        store = get_store()
        
        db_record = Database(
            name=name,
            engine=manager.ENGINE_NAME,
            host="localhost",
            port=manager.DEFAULT_PORT,
            username=owner,
            encoding=encoding,
        )
        store.create_database(db_record)
        
        logger.success(f"Created database: {info.name}")
        
        if info.size:
            logger.info(f"  Size: {info.size}")
        if info.encoding:
            logger.info(f"  Encoding: {info.encoding}")
        
        return 0
    except DatabaseError as e:
        logger.error(str(e))
        return 1


def _db_drop(args: Namespace, verbose: bool) -> int:
    """Drop a database."""
    logger = Logger(verbose=verbose)
    engine = getattr(args, "engine", None)
    name = getattr(args, "name", None)
    force = getattr(args, "force", False)
    
    if not name:
        logger.error("Database name is required")
        return 1
    
    manager = _get_manager(engine, verbose)
    if not manager:
        return 1
    
    try:
        if not manager.is_running():
            logger.error(f"{manager.DISPLAY_NAME} is not running")
            return 1
        
        # Confirm if not forced
        if not force:
            try:
                response = input(f"Drop database '{name}'? This cannot be undone! [y/N]: ")
                if response.lower() not in ["y", "yes"]:
                    logger.info("Cancelled")
                    return 0
            except (EOFError, KeyboardInterrupt):
                logger.info("\nCancelled")
                return 0
        
        manager.drop_database(name, force=force)
        
        # Remove from store
        from wasm.core.store import get_store
        store = get_store()
        store.delete_database(name, manager.ENGINE_NAME)
        
        logger.success(f"Dropped database: {name}")
        
        return 0
    except DatabaseError as e:
        logger.error(str(e))
        return 1


def _db_list(args: Namespace, verbose: bool) -> int:
    """List databases."""
    logger = Logger(verbose=verbose)
    engine = getattr(args, "engine", None)
    json_output = getattr(args, "json", False)
    
    from wasm.core.store import get_store
    store = get_store()
    
    if engine:
        managers = [_get_manager(engine, verbose)]
        if managers[0] is None:
            return 1
    else:
        # List databases from all running engines
        managers = DatabaseRegistry.get_installed(verbose=verbose)
    
    all_databases = []
    
    for manager in managers:
        if not manager.is_running():
            continue
        
        try:
            databases = manager.list_databases()
            for db in databases:
                db_dict = db.to_dict()
                
                # Check if it's tracked in store and get app association
                store_db = store.get_database(db.name, manager.ENGINE_NAME)
                if store_db:
                    db_dict['tracked'] = True
                    if store_db.app_id:
                        app = store.get_app_by_id(store_db.app_id)
                        if app:
                            db_dict['linked_app'] = app.domain
                else:
                    db_dict['tracked'] = False
                
                all_databases.append(db_dict)
        except Exception as e:
            if verbose:
                logger.warning(f"Could not list {manager.DISPLAY_NAME} databases: {e}")
    
    if json_output:
        print(json.dumps(all_databases, indent=2))
        return 0
    
    if not all_databases:
        logger.info("No databases found")
        return 0
    
    # Group by engine
    by_engine = {}
    for db in all_databases:
        eng = db.get("engine", "unknown")
        if eng not in by_engine:
            by_engine[eng] = []
        by_engine[eng].append(db)
    
    for eng, dbs in by_engine.items():
        print(f"\n📦 {eng.upper()}")
        print("-" * 50)
        for db in dbs:
            size = db.get("size", "")
            tables = db.get("tables", 0)
            tracked = "✓" if db.get("tracked") else " "
            linked = f" → {db['linked_app']}" if db.get("linked_app") else ""
            
            size_str = f" ({size})" if size else ""
            tables_str = f" - {tables} tables" if tables else ""
            
            print(f"  [{tracked}] {db['name']}{size_str}{tables_str}{linked}")
    
    print()
    print("  [✓] = tracked by WASM")
    return 0


def _db_info(args: Namespace, verbose: bool) -> int:
    """Show database information."""
    logger = Logger(verbose=verbose)
    engine = getattr(args, "engine", None)
    name = getattr(args, "name", None)
    json_output = getattr(args, "json", False)
    
    if not name:
        logger.error("Database name is required")
        return 1
    
    manager = _get_manager(engine, verbose)
    if not manager:
        return 1
    
    try:
        if not manager.is_running():
            logger.error(f"{manager.DISPLAY_NAME} is not running")
            return 1
        
        info = manager.get_database_info(name)
        
        if json_output:
            print(json.dumps(info.to_dict(), indent=2))
            return 0
        
        print(f"\nDatabase: {info.name}")
        print(f"Engine:   {info.engine}")
        if info.size:
            print(f"Size:     {info.size}")
        if info.tables:
            print(f"Tables:   {info.tables}")
        if info.owner:
            print(f"Owner:    {info.owner}")
        if info.encoding:
            print(f"Encoding: {info.encoding}")
        
        return 0
    except DatabaseError as e:
        logger.error(str(e))
        return 1


# ==================== User Management ====================

def _db_user_create(args: Namespace, verbose: bool) -> int:
    """Create a database user."""
    logger = Logger(verbose=verbose)
    engine = getattr(args, "engine", None)
    username = getattr(args, "username", None)
    password = getattr(args, "password", None)
    database = getattr(args, "database", None)
    host = getattr(args, "host", "localhost")
    
    if not username:
        logger.error("Username is required")
        return 1
    
    manager = _get_manager(engine, verbose)
    if not manager:
        return 1
    
    try:
        if not manager.is_running():
            logger.error(f"{manager.DISPLAY_NAME} is not running")
            return 1
        
        user_info, generated_password = manager.create_user(
            username=username,
            password=password,
            host=host,
            database=database,
        )
        
        logger.success(f"Created user: {username}")
        
        if not password:
            logger.info(f"  Password: {generated_password}")
            logger.warning("  Save this password - it won't be shown again!")
        
        # Grant privileges if database specified
        if database:
            try:
                manager.grant_privileges(username, database, host=host)
                logger.info(f"  Granted privileges on: {database}")
            except Exception as e:
                logger.warning(f"  Could not grant privileges: {e}")
        
        return 0
    except DatabaseError as e:
        logger.error(str(e))
        return 1


def _db_user_delete(args: Namespace, verbose: bool) -> int:
    """Delete a database user."""
    logger = Logger(verbose=verbose)
    engine = getattr(args, "engine", None)
    username = getattr(args, "username", None)
    host = getattr(args, "host", "localhost")
    force = getattr(args, "force", False)
    
    if not username:
        logger.error("Username is required")
        return 1
    
    manager = _get_manager(engine, verbose)
    if not manager:
        return 1
    
    try:
        if not manager.is_running():
            logger.error(f"{manager.DISPLAY_NAME} is not running")
            return 1
        
        if not force:
            try:
                response = input(f"Delete user '{username}'? [y/N]: ")
                if response.lower() not in ["y", "yes"]:
                    logger.info("Cancelled")
                    return 0
            except (EOFError, KeyboardInterrupt):
                logger.info("\nCancelled")
                return 0
        
        manager.drop_user(username, host=host)
        logger.success(f"Deleted user: {username}")
        
        return 0
    except DatabaseError as e:
        logger.error(str(e))
        return 1


def _db_user_list(args: Namespace, verbose: bool) -> int:
    """List database users."""
    logger = Logger(verbose=verbose)
    engine = getattr(args, "engine", None)
    json_output = getattr(args, "json", False)
    
    manager = _get_manager(engine, verbose)
    if not manager:
        return 1
    
    try:
        if not manager.is_running():
            logger.error(f"{manager.DISPLAY_NAME} is not running")
            return 1
        
        users = manager.list_users()
        
        if json_output:
            print(json.dumps([u.to_dict() for u in users], indent=2))
            return 0
        
        if not users:
            logger.info("No users found")
            return 0
        
        print(f"\n{manager.DISPLAY_NAME} Users:")
        print("-" * 50)
        for user in users:
            host_str = f"@{user.host}" if user.host != "localhost" else ""
            privs = ", ".join(user.privileges[:3]) if user.privileges else ""
            if len(user.privileges) > 3:
                privs += f" (+{len(user.privileges) - 3} more)"
            
            print(f"  {user.username}{host_str}")
            if privs:
                print(f"    Privileges: {privs}")
        
        print()
        return 0
    except DatabaseError as e:
        logger.error(str(e))
        return 1


def _db_grant(args: Namespace, verbose: bool) -> int:
    """Grant privileges to a user."""
    logger = Logger(verbose=verbose)
    engine = getattr(args, "engine", None)
    username = getattr(args, "username", None)
    database = getattr(args, "database", None)
    privileges = getattr(args, "privileges", None)
    host = getattr(args, "host", "localhost")
    
    if not username or not database:
        logger.error("Username and database are required")
        return 1
    
    manager = _get_manager(engine, verbose)
    if not manager:
        return 1
    
    try:
        privs_list = privileges.split(",") if privileges else None
        manager.grant_privileges(username, database, privileges=privs_list, host=host)
        logger.success(f"Granted privileges on {database} to {username}")
        return 0
    except DatabaseError as e:
        logger.error(str(e))
        return 1


def _db_revoke(args: Namespace, verbose: bool) -> int:
    """Revoke privileges from a user."""
    logger = Logger(verbose=verbose)
    engine = getattr(args, "engine", None)
    username = getattr(args, "username", None)
    database = getattr(args, "database", None)
    privileges = getattr(args, "privileges", None)
    host = getattr(args, "host", "localhost")
    
    if not username or not database:
        logger.error("Username and database are required")
        return 1
    
    manager = _get_manager(engine, verbose)
    if not manager:
        return 1
    
    try:
        privs_list = privileges.split(",") if privileges else None
        manager.revoke_privileges(username, database, privileges=privs_list, host=host)
        logger.success(f"Revoked privileges on {database} from {username}")
        return 0
    except DatabaseError as e:
        logger.error(str(e))
        return 1


# ==================== Backup & Restore ====================

def _db_backup(args: Namespace, verbose: bool) -> int:
    """Backup a database."""
    logger = Logger(verbose=verbose)
    engine = getattr(args, "engine", None)
    database = getattr(args, "database", None)
    output = getattr(args, "output", None)
    no_compress = getattr(args, "no_compress", False)
    
    if not database:
        logger.error("Database name is required")
        return 1
    
    manager = _get_manager(engine, verbose)
    if not manager:
        return 1
    
    try:
        if not manager.is_running():
            logger.error(f"{manager.DISPLAY_NAME} is not running")
            return 1
        
        logger.step(1, 2, f"Creating backup of {database}...")
        
        from pathlib import Path
        output_path = Path(output) if output else None
        
        backup_info = manager.backup(
            database=database,
            output_path=output_path,
            compress=not no_compress,
        )
        
        logger.step(2, 2, "Backup complete")
        logger.success(f"Backup created: {backup_info.path}")
        logger.info(f"  Size: {backup_info.to_dict()['size_human']}")
        
        return 0
    except DatabaseError as e:
        logger.error(str(e))
        return 1


def _db_restore(args: Namespace, verbose: bool) -> int:
    """Restore a database from backup."""
    logger = Logger(verbose=verbose)
    engine = getattr(args, "engine", None)
    database = getattr(args, "database", None)
    backup_file = getattr(args, "file", None)
    drop_existing = getattr(args, "drop", False)
    force = getattr(args, "force", False)
    
    if not database or not backup_file:
        logger.error("Database name and backup file are required")
        return 1
    
    manager = _get_manager(engine, verbose)
    if not manager:
        return 1
    
    try:
        if not manager.is_running():
            logger.error(f"{manager.DISPLAY_NAME} is not running")
            return 1
        
        # Confirm if not forced
        if not force:
            msg = f"Restore database '{database}' from {backup_file}?"
            if drop_existing:
                msg = f"DROP and restore database '{database}' from {backup_file}?"
            
            try:
                response = input(f"{msg} [y/N]: ")
                if response.lower() not in ["y", "yes"]:
                    logger.info("Cancelled")
                    return 0
            except (EOFError, KeyboardInterrupt):
                logger.info("\nCancelled")
                return 0
        
        logger.step(1, 2, f"Restoring {database}...")
        
        from pathlib import Path
        manager.restore(
            database=database,
            backup_path=Path(backup_file),
            drop_existing=drop_existing,
        )
        
        logger.step(2, 2, "Restore complete")
        logger.success(f"Database {database} restored")
        
        return 0
    except DatabaseError as e:
        logger.error(str(e))
        return 1


def _db_backups(args: Namespace, verbose: bool) -> int:
    """List available backups."""
    logger = Logger(verbose=verbose)
    engine = getattr(args, "engine", None)
    database = getattr(args, "database", None)
    json_output = getattr(args, "json", False)
    
    if engine:
        managers = [_get_manager(engine, verbose)]
        if managers[0] is None:
            return 1
    else:
        managers = DatabaseRegistry.get_installed(verbose=verbose)
    
    all_backups = []
    
    for manager in managers:
        try:
            backups = manager.list_backups(database=database)
            for backup in backups:
                all_backups.append(backup.to_dict())
        except Exception as e:
            if verbose:
                logger.warning(f"Could not list {manager.DISPLAY_NAME} backups: {e}")
    
    if json_output:
        print(json.dumps(all_backups, indent=2))
        return 0
    
    if not all_backups:
        logger.info("No backups found")
        return 0
    
    print("\nAvailable Backups:")
    print("-" * 60)
    for backup in all_backups:
        print(f"  {backup['database']} ({backup['engine']})")
        print(f"    Path:    {backup['path']}")
        print(f"    Size:    {backup['size_human']}")
        print(f"    Created: {backup['created']}")
        print()
    
    return 0


# ==================== Query & Connection ====================

def _db_query(args: Namespace, verbose: bool) -> int:
    """Execute a query."""
    logger = Logger(verbose=verbose)
    engine = getattr(args, "engine", None)
    database = getattr(args, "database", None)
    query = getattr(args, "query", None)
    
    if not database or not query:
        logger.error("Database and query are required")
        return 1
    
    manager = _get_manager(engine, verbose)
    if not manager:
        return 1
    
    try:
        if not manager.is_running():
            logger.error(f"{manager.DISPLAY_NAME} is not running")
            return 1
        
        success, output = manager.execute_query(database, query)
        
        if output:
            print(output)
        
        return 0 if success else 1
    except DatabaseError as e:
        logger.error(str(e))
        return 1


def _db_connect(args: Namespace, verbose: bool) -> int:
    """Connect to a database interactively."""
    logger = Logger(verbose=verbose)
    engine = getattr(args, "engine", None)
    database = getattr(args, "database", None)
    username = getattr(args, "username", None)
    
    manager = _get_manager(engine, verbose)
    if not manager:
        return 1
    
    try:
        if not manager.is_installed():
            logger.error(f"{manager.DISPLAY_NAME} is not installed")
            return 1
        
        if not manager.is_running():
            logger.error(f"{manager.DISPLAY_NAME} is not running")
            return 1
        
        cmd = manager.get_interactive_command(database=database, username=username)
        
        logger.info(f"Connecting to {manager.DISPLAY_NAME}...")
        logger.info(f"Command: {' '.join(cmd)}")
        
        # Execute interactively
        return subprocess.call(cmd)
    except Exception as e:
        logger.error(str(e))
        return 1


def _db_connection_string(args: Namespace, verbose: bool) -> int:
    """Generate a connection string."""
    logger = Logger(verbose=verbose)
    engine = getattr(args, "engine", None)
    database = getattr(args, "database", None)
    username = getattr(args, "username", None)
    password = getattr(args, "password", None)
    host = getattr(args, "host", "localhost")
    
    if not database or not username:
        logger.error("Database and username are required")
        return 1
    
    manager = _get_manager(engine, verbose)
    if not manager:
        return 1
    
    if not password:
        password = "<PASSWORD>"
    
    conn_string = manager.get_connection_string(
        database=database,
        username=username,
        password=password,
        host=host,
    )
    
    print(conn_string)
    return 0


def _db_config(args: Namespace, verbose: bool) -> int:
    """Configure database credentials."""
    logger = Logger(verbose=verbose)
    engine = getattr(args, "engine", None)
    user = getattr(args, "user", None)
    password = getattr(args, "password", None)
    
    if not engine:
        logger.error("Engine is required")
        return 1
        
    config = Config()
    
    # Ensure structure exists
    if "databases" not in config._config:
        config._config["databases"] = {}
    if "credentials" not in config._config["databases"]:
        config._config["databases"]["credentials"] = {}
    if engine not in config._config["databases"]["credentials"]:
        config._config["databases"]["credentials"][engine] = {}
        
    creds = config._config["databases"]["credentials"][engine]
    
    if user:
        creds["user"] = user
    if password:
        creds["password"] = password
        
    if config.save():
        logger.success(f"Updated credentials for {engine}")
        return 0
    else:
        logger.error("Failed to save configuration")
        return 1
