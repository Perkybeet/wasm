"""
Backup and rollback CLI commands for WASM.
"""

from argparse import Namespace
from typing import Optional

from wasm.core.logger import Logger
from wasm.core.exceptions import WASMError
from wasm.managers.backup_manager import BackupManager, RollbackManager, BackupError


def handle_backup(args: Namespace) -> int:
    """Handle wasm backup <action> commands."""
    action = getattr(args, "action", "list")
    verbose = getattr(args, "verbose", False)
    
    if action == "create":
        return _backup_create(args, verbose)
    elif action == "list":
        return _backup_list(args, verbose)
    elif action == "restore":
        return _backup_restore(args, verbose)
    elif action == "delete":
        return _backup_delete(args, verbose)
    elif action == "verify":
        return _backup_verify(args, verbose)
    elif action == "info":
        return _backup_info(args, verbose)
    elif action == "storage":
        return _backup_storage(args, verbose)
    else:
        logger = Logger(verbose=verbose)
        logger.error(f"Unknown backup action: {action}")
        return 1


def handle_rollback(args: Namespace) -> int:
    """Handle wasm rollback command."""
    verbose = getattr(args, "verbose", False)
    logger = Logger(verbose=verbose)
    
    domain = getattr(args, "domain", None)
    backup_id = getattr(args, "backup_id", None)
    no_rebuild = getattr(args, "no_rebuild", False)
    
    if not domain:
        logger.error("Domain is required")
        return 1
    
    try:
        rollback_manager = RollbackManager(verbose=verbose)
        
        # Show available backups if no ID provided
        if not backup_id:
            backups = rollback_manager.list_rollback_points(domain)
            if not backups:
                logger.error(f"No backups found for {domain}")
                return 1
            
            logger.info(f"Rolling back to latest backup: {backups[0].id}")
            logger.info(f"  Created: {backups[0].age}")
            if backups[0].description:
                logger.info(f"  Description: {backups[0].description}")
        
        # Perform rollback
        logger.step(1, 3, "Creating safety backup")
        try:
            rollback_manager.create_pre_deploy_backup(
                domain=domain,
                description="Pre-rollback safety backup"
            )
        except Exception as e:
            logger.warning(f"Could not create safety backup: {e}")
        
        logger.step(2, 3, "Restoring from backup")
        rollback_manager.rollback(
            domain=domain,
            backup_id=backup_id,
            rebuild=not no_rebuild,
        )
        
        logger.step(3, 3, "Rollback complete")
        logger.success(f"Successfully rolled back {domain}")
        
        return 0
        
    except BackupError as e:
        logger.error(f"Rollback failed: {e}")
        return 1
    except Exception as e:
        logger.error(f"Unexpected error: {e}")
        if verbose:
            import traceback
            traceback.print_exc()
        return 1


def _backup_create(args: Namespace, verbose: bool) -> int:
    """Create a backup."""
    logger = Logger(verbose=verbose)
    
    domain = getattr(args, "domain", None)
    description = getattr(args, "description", "")
    include_env = not getattr(args, "no_env", False)
    include_node_modules = getattr(args, "include_node_modules", False)
    include_build = getattr(args, "include_build", False)
    tags = getattr(args, "tags", None)
    
    if not domain:
        logger.error("Domain is required")
        return 1
    
    try:
        manager = BackupManager(verbose=verbose)
        
        logger.step(1, 2, f"Creating backup for {domain}")
        
        # Parse tags
        tag_list = []
        if tags:
            tag_list = [t.strip() for t in tags.split(",")]
        
        metadata = manager.create(
            domain=domain,
            description=description,
            include_env=include_env,
            include_node_modules=include_node_modules,
            include_build=include_build,
            tags=tag_list,
        )
        
        logger.step(2, 2, "Backup complete")
        logger.success(f"Created backup: {metadata.id}")
        logger.info(f"  Size: {metadata.size_human}")
        if metadata.git_commit:
            logger.info(f"  Commit: {metadata.git_commit} ({metadata.git_branch})")
        
        return 0
        
    except BackupError as e:
        logger.error(f"Backup failed: {e}")
        return 1
    except Exception as e:
        logger.error(f"Unexpected error: {e}")
        if verbose:
            import traceback
            traceback.print_exc()
        return 1


def _backup_list(args: Namespace, verbose: bool) -> int:
    """List backups."""
    logger = Logger(verbose=verbose)
    
    domain = getattr(args, "domain", None)
    tags = getattr(args, "tags", None)
    limit = getattr(args, "limit", None)
    json_output = getattr(args, "json", False)
    
    try:
        manager = BackupManager(verbose=verbose)
        
        # Parse tags
        tag_list = None
        if tags:
            tag_list = [t.strip() for t in tags.split(",")]
        
        backups = manager.list_backups(
            domain=domain,
            tags=tag_list,
            limit=limit,
        )
        
        if json_output:
            import json
            print(json.dumps([b.to_dict() for b in backups], indent=2))
            return 0
        
        if not backups:
            if domain:
                logger.info(f"No backups found for {domain}")
            else:
                logger.info("No backups found")
            return 0
        
        # Group by domain if listing all
        if domain:
            _print_backup_table(backups, logger)
        else:
            by_domain = {}
            for backup in backups:
                if backup.domain not in by_domain:
                    by_domain[backup.domain] = []
                by_domain[backup.domain].append(backup)
            
            for dom, dom_backups in by_domain.items():
                logger.info(f"\n📦 {dom}")
                _print_backup_table(dom_backups, logger, indent=True)
        
        return 0
        
    except Exception as e:
        logger.error(f"Error listing backups: {e}")
        if verbose:
            import traceback
            traceback.print_exc()
        return 1


def _print_backup_table(backups, logger, indent: bool = False):
    """Print backup table."""
    prefix = "  " if indent else ""
    
    for backup in backups:
        tags_str = ""
        if backup.tags:
            tags_str = f" [{', '.join(backup.tags)}]"
        
        commit_str = ""
        if backup.git_commit:
            commit_str = f" ({backup.git_commit})"
        
        desc_str = ""
        if backup.description:
            desc_str = f" - {backup.description}"
        
        logger.info(
            f"{prefix}• {backup.id}: {backup.size_human}, "
            f"{backup.age}{commit_str}{tags_str}{desc_str}"
        )


def _backup_restore(args: Namespace, verbose: bool) -> int:
    """Restore from backup."""
    logger = Logger(verbose=verbose)
    
    backup_id = getattr(args, "backup_id", None)
    target_domain = getattr(args, "target_domain", None)
    no_env = getattr(args, "no_env", False)
    no_verify = getattr(args, "no_verify", False)
    force = getattr(args, "force", False)
    
    if not backup_id:
        logger.error("Backup ID is required")
        return 1
    
    try:
        manager = BackupManager(verbose=verbose)
        
        # Get backup info
        metadata = manager.get_backup(backup_id)
        if not metadata:
            logger.error(f"Backup not found: {backup_id}")
            return 1
        
        target = target_domain or metadata.domain
        
        # Confirm
        if not force:
            logger.warning(f"This will restore {target} from backup {backup_id}")
            logger.warning(f"  Created: {metadata.age}")
            logger.warning(f"  Size: {metadata.size_human}")
            logger.info("")
            confirm = input("Continue? [y/N]: ").strip().lower()
            if confirm != "y":
                logger.info("Cancelled")
                return 0
        
        logger.step(1, 3, "Verifying backup")
        if not no_verify:
            verify_result = manager.verify(backup_id)
            if not verify_result["valid"]:
                logger.error("Backup verification failed:")
                for err in verify_result["errors"]:
                    logger.error(f"  - {err}")
                return 1
        
        logger.step(2, 3, f"Restoring to {target}")
        manager.restore(
            backup_id=backup_id,
            target_domain=target_domain,
            restore_env=not no_env,
            verify_checksum=not no_verify,
        )
        
        logger.step(3, 3, "Restore complete")
        logger.success(f"Successfully restored {target} from {backup_id}")
        
        return 0
        
    except BackupError as e:
        logger.error(f"Restore failed: {e}")
        return 1
    except Exception as e:
        logger.error(f"Unexpected error: {e}")
        if verbose:
            import traceback
            traceback.print_exc()
        return 1


def _backup_delete(args: Namespace, verbose: bool) -> int:
    """Delete a backup."""
    logger = Logger(verbose=verbose)
    
    backup_id = getattr(args, "backup_id", None)
    force = getattr(args, "force", False)
    
    if not backup_id:
        logger.error("Backup ID is required")
        return 1
    
    try:
        manager = BackupManager(verbose=verbose)
        
        # Get backup info
        metadata = manager.get_backup(backup_id)
        if not metadata:
            logger.error(f"Backup not found: {backup_id}")
            return 1
        
        # Confirm
        if not force:
            logger.warning(f"This will permanently delete backup: {backup_id}")
            logger.warning(f"  Domain: {metadata.domain}")
            logger.warning(f"  Size: {metadata.size_human}")
            logger.warning(f"  Created: {metadata.age}")
            confirm = input("Delete? [y/N]: ").strip().lower()
            if confirm != "y":
                logger.info("Cancelled")
                return 0
        
        manager.delete(backup_id)
        logger.success(f"Deleted backup: {backup_id}")
        
        return 0
        
    except BackupError as e:
        logger.error(f"Delete failed: {e}")
        return 1


def _backup_verify(args: Namespace, verbose: bool) -> int:
    """Verify a backup's integrity."""
    logger = Logger(verbose=verbose)
    
    backup_id = getattr(args, "backup_id", None)
    
    if not backup_id:
        logger.error("Backup ID is required")
        return 1
    
    try:
        manager = BackupManager(verbose=verbose)
        
        logger.info(f"Verifying backup: {backup_id}")
        result = manager.verify(backup_id)
        
        if result["valid"]:
            logger.success("Backup is valid")
            if result.get("checksum_verified"):
                logger.info("  ✓ Checksum verified")
            if result.get("archive_valid"):
                logger.info(f"  ✓ Archive valid ({result.get('file_count', '?')} files)")
        else:
            logger.error("Backup is invalid")
            for err in result["errors"]:
                logger.error(f"  ✗ {err}")
        
        for warn in result.get("warnings", []):
            logger.warning(f"  ⚠ {warn}")
        
        return 0 if result["valid"] else 1
        
    except Exception as e:
        logger.error(f"Verification failed: {e}")
        return 1


def _backup_info(args: Namespace, verbose: bool) -> int:
    """Show backup information."""
    logger = Logger(verbose=verbose)
    
    backup_id = getattr(args, "backup_id", None)
    json_output = getattr(args, "json", False)
    
    if not backup_id:
        logger.error("Backup ID is required")
        return 1
    
    try:
        manager = BackupManager(verbose=verbose)
        
        metadata = manager.get_backup(backup_id)
        if not metadata:
            logger.error(f"Backup not found: {backup_id}")
            return 1
        
        if json_output:
            import json
            print(json.dumps(metadata.to_dict(), indent=2))
            return 0
        
        logger.info(f"Backup: {metadata.id}")
        logger.info(f"  Domain:      {metadata.domain}")
        logger.info(f"  App Name:    {metadata.app_name}")
        logger.info(f"  App Type:    {metadata.app_type}")
        logger.info(f"  Size:        {metadata.size_human}")
        logger.info(f"  Created:     {metadata.created_at} ({metadata.age})")
        
        if metadata.description:
            logger.info(f"  Description: {metadata.description}")
        
        if metadata.git_commit:
            logger.info(f"  Git Commit:  {metadata.git_commit}")
            logger.info(f"  Git Branch:  {metadata.git_branch}")
        
        if metadata.tags:
            logger.info(f"  Tags:        {', '.join(metadata.tags)}")
        
        logger.info(f"  Includes:")
        logger.info(f"    - .env files:     {'Yes' if metadata.includes_env else 'No'}")
        logger.info(f"    - node_modules:   {'Yes' if metadata.includes_node_modules else 'No'}")
        
        if metadata.checksum:
            logger.info(f"  Checksum:    {metadata.checksum[:16]}...")
        
        return 0
        
    except Exception as e:
        logger.error(f"Error: {e}")
        return 1


def _backup_storage(args: Namespace, verbose: bool) -> int:
    """Show backup storage usage."""
    logger = Logger(verbose=verbose)
    json_output = getattr(args, "json", False)
    
    try:
        manager = BackupManager(verbose=verbose)
        usage = manager.get_storage_usage()
        
        if json_output:
            import json
            print(json.dumps(usage, indent=2))
            return 0
        
        # Format total size
        total_bytes = usage["total_size_bytes"]
        for unit in ["B", "KB", "MB", "GB"]:
            if total_bytes < 1024:
                total_str = f"{total_bytes:.1f} {unit}"
                break
            total_bytes /= 1024
        else:
            total_str = f"{total_bytes:.1f} TB"
        
        logger.info(f"Backup Storage Usage")
        logger.info(f"  Total: {total_str} ({usage['total_backups']} backups)")
        logger.info("")
        
        for app_name, app_usage in usage["by_app"].items():
            app_bytes = app_usage["size_bytes"]
            for unit in ["B", "KB", "MB", "GB"]:
                if app_bytes < 1024:
                    app_str = f"{app_bytes:.1f} {unit}"
                    break
                app_bytes /= 1024
            else:
                app_str = f"{app_bytes:.1f} TB"
            
            logger.info(f"  {app_name}: {app_str} ({app_usage['count']} backups)")
        
        return 0
        
    except Exception as e:
        logger.error(f"Error: {e}")
        return 1
