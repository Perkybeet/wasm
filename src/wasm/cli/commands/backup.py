# Copyright (c) 2024-2026 Yago Lopez Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
Backup and rollback commands.

The work itself lives in the private ``_backup_*`` helpers. The Click commands
and the argparse handlers :mod:`wasm.cli.parser` still calls are both thin
adapters over them, so the two entry points cannot drift while the migration
finishes.

Two things the argparse tree got wrong and this module does not:

- ``wasm backup new`` and ``wasm backup ls`` reached the handler with the alias
  as the action name, which fell through to "Unknown backup action". The alias
  table is now one constant, used by the Click group and by the handler.
- ``--include-docker-volumes``, ``--schemas``, ``--redis-method``,
  ``--retention-count`` and ``--retention-days`` were declared, documented and
  then never passed to :class:`BackupManager`. They are wired through now.

Backups are self-contained since archive format 2.0.0: the archive carries the
application directory plus, when asked for, the database dumps and Docker
volumes, so it restores on a machine that knows nothing about this one. The
help text says so because it is now true.
"""

from __future__ import annotations

import json
from argparse import Namespace
from collections.abc import Sequence
from typing import Any

import click

from wasm.cli.app import Context, pass_context
from wasm.core.exceptions import WASMError
from wasm.core.logger import Logger
from wasm.managers.backup_manager import BackupManager, BackupMetadata, RollbackManager

#: Alternative spellings for the actions under ``wasm backup``. They are in
#: scripts and in muscle memory, so they resolve rather than fail.
BACKUP_ALIASES: dict[str, str] = {
    "check": "verify",
    "ls": "list",
    "new": "create",
    "remove": "delete",
    "rm": "delete",
    "show": "info",
}

#: Alternative spellings for the actions under ``wasm backup schedule``.
SCHEDULE_ALIASES: dict[str, str] = {
    "ls": "list",
    "remove": "delete",
    "rm": "delete",
}

#: Ways to capture a Redis instance into a backup.
REDIS_METHODS: tuple[str, ...] = ("rdb", "aof")


class AliasedGroup(click.Group):
    """
    A group that answers to the alternative spellings of its subcommands.

    Only the canonical names are listed in ``--help``: an alias is there so an
    old script keeps working, not so the help page grows a second copy of every
    command.
    """

    def __init__(self, *args: Any, aliases: dict[str, str] | None = None, **kwargs: Any) -> None:
        """
        Args:
            *args: Passed to click.Group.
            aliases: Alternative spelling to the canonical subcommand name.
            **kwargs: Passed to click.Group.
        """
        super().__init__(*args, **kwargs)
        self.aliases = aliases or {}

    def get_command(self, ctx: click.Context, name: str) -> click.Command | None:
        """
        Look a subcommand up, resolving an alias first.

        Args:
            ctx: Click context.
            name: Name or alias the user typed.

        Returns:
            The command, or None if there is no such subcommand.
        """
        return super().get_command(ctx, self.aliases.get(name, name))

    def resolve_command(
        self, ctx: click.Context, args: list[str]
    ) -> tuple[str | None, click.Command | None, list[str]]:
        """
        Resolve the command to run, reporting the canonical name.

        Args:
            ctx: Click context.
            args: Remaining arguments.

        Returns:
            The command name, the command and the arguments left for it.
        """
        _, command, remaining = super().resolve_command(ctx, args)
        return (command.name if command else None), command, remaining


def _finish(code: int) -> None:
    """
    Leave the current command with an exit code Click will propagate.

    Returning the code is not enough: Click only forwards a callback's return
    value when it is driven with ``standalone_mode=False``.

    Args:
        code: Process exit code.
    """
    click.get_current_context().exit(code)


def _parse_tags(raw: str | None) -> list[str]:
    """
    Split a comma-separated tag list.

    Args:
        raw: The value of ``--tags``, or None.

    Returns:
        The tags, without surrounding whitespace and without empty entries.
    """
    if not raw:
        return []
    return [tag.strip() for tag in raw.split(",") if tag.strip()]


def _human_bytes(size_bytes: float) -> str:
    """
    Render a byte count in the largest unit that keeps it under 1024.

    Args:
        size_bytes: Size in bytes.

    Returns:
        A human-readable size, for example "1.4 GB".
    """
    size = float(size_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def _print_backup_table(
    backups: Sequence[BackupMetadata], logger: Logger, indent: bool = False
) -> None:
    """
    Print one line per backup.

    Args:
        backups: Backups to print, newest first.
        logger: Logger to print through.
        indent: Indent the lines, for use under a domain heading.
    """
    prefix = "  " if indent else ""

    for backup in backups:
        tags_str = f" [{', '.join(backup.tags)}]" if backup.tags else ""
        commit_str = f" ({backup.git_commit})" if backup.git_commit else ""
        desc_str = f" - {backup.description}" if backup.description else ""

        logger.info(
            f"{prefix}- {backup.id}: {backup.size_human}, "
            f"{backup.age}{commit_str}{tags_str}{desc_str}"
        )


def _create_backup(
    *,
    logger: Logger,
    domain: str,
    description: str = "",
    include_env: bool = True,
    include_node_modules: bool = False,
    include_build: bool = False,
    include_databases: bool = False,
    include_docker_volumes: bool = False,
    schemas: Sequence[str] | None = None,
    redis_method: str = "rdb",
    retention_count: int | None = None,
    retention_days: int | None = None,
    tags: str | None = None,
) -> int:
    """
    Create a backup of an application and report what went into it.

    Args:
        logger: Logger to report through.
        domain: Domain of the application to back up.
        description: Note to store with the backup.
        include_env: Put the ``.env`` files in the archive.
        include_node_modules: Put ``node_modules`` in the archive.
        include_build: Put the build output in the archive.
        include_databases: Dump the application databases into the archive.
        include_docker_volumes: Copy the Docker volumes into the archive.
        schemas: PostgreSQL schemas to dump instead of whole databases.
        redis_method: How to capture Redis, "rdb" or "aof".
        retention_count: Keep at most this many backups of the application.
        retention_days: Delete backups older than this many days.
        tags: Comma-separated tags to file the backup under.

    Returns:
        0 on success, 1 if the backup could not be created.
    """
    try:
        manager = BackupManager(verbose=logger.verbose)

        logger.step(1, 2, f"Creating backup for {domain}")
        metadata = manager.create(
            domain=domain,
            description=description,
            include_env=include_env,
            include_node_modules=include_node_modules,
            include_build=include_build,
            include_databases=include_databases,
            include_docker_volumes=include_docker_volumes,
            schemas=list(schemas) if schemas else None,
            redis_method=redis_method,
            retention_count=retention_count,
            retention_days=retention_days,
            tags=_parse_tags(tags),
        )
    except WASMError as exc:
        logger.error(f"Backup failed: {exc}")
        return 1

    logger.step(2, 2, "Backup complete")
    logger.success(f"Created backup: {metadata.id}")
    logger.info(f"  Size: {metadata.size_human}")
    if metadata.git_commit:
        logger.info(f"  Commit: {metadata.git_commit} ({metadata.git_branch})")
    if metadata.database_backups:
        logger.info(f"  Databases: {len(metadata.database_backups)} dumped into the archive")
        for db_info in metadata.database_backups:
            size = _human_bytes(db_info.get("size_bytes", 0))
            logger.info(f"    - {db_info['engine']}/{db_info['name']} ({size})")
    if metadata.docker_volume_backups:
        logger.info(f"  Volumes: {len(metadata.docker_volume_backups)} copied into the archive")
        for volume in metadata.docker_volume_backups:
            logger.info(f"    - {volume.get('name', '?')}")

    return 0


def _list_backups(
    *,
    logger: Logger,
    domain: str | None = None,
    tags: str | None = None,
    limit: int | None = None,
    json_output: bool = False,
) -> int:
    """
    List the backups WASM knows about.

    Args:
        logger: Logger to report through.
        domain: Only list backups of this application.
        tags: Comma-separated tags to filter by.
        limit: Maximum number of backups to show.
        json_output: Print the backups as JSON instead of a table.

    Returns:
        0 on success, 1 if the backups could not be read.
    """
    try:
        manager = BackupManager(verbose=logger.verbose)
        backups = manager.list_backups(
            domain=domain,
            tags=_parse_tags(tags) or None,
            limit=limit,
        )
    except WASMError as exc:
        logger.error(f"Error listing backups: {exc}")
        return 1

    if json_output:
        click.echo(json.dumps([backup.to_dict() for backup in backups], indent=2))
        return 0

    if not backups:
        logger.info(f"No backups found for {domain}" if domain else "No backups found")
        return 0

    if domain:
        _print_backup_table(backups, logger)
        return 0

    by_domain: dict[str, list[BackupMetadata]] = {}
    for backup in backups:
        by_domain.setdefault(backup.domain, []).append(backup)

    for dom, dom_backups in by_domain.items():
        logger.info(f"\n[{dom}]")
        _print_backup_table(dom_backups, logger, indent=True)

    return 0


def _restore_backup(
    *,
    logger: Logger,
    backup_id: str,
    target_domain: str | None = None,
    restore_env: bool = True,
    verify: bool = True,
    force: bool = False,
) -> int:
    """
    Restore an application from a backup.

    Args:
        logger: Logger to report through.
        backup_id: Backup to restore.
        target_domain: Restore into this domain instead of the original one.
        restore_env: Restore the ``.env`` files from the archive.
        verify: Check the archive against its recorded checksum first.
        force: Do not ask for confirmation.

    Returns:
        0 on success, 1 if the restore failed or the backup is unknown.
    """
    try:
        manager = BackupManager(verbose=logger.verbose)

        metadata = manager.get_backup(backup_id)
        if not metadata:
            logger.error(f"Backup not found: {backup_id}")
            return 1

        target = target_domain or metadata.domain

        if not force and not click.confirm(
            f"Replace the files of {target} with backup {backup_id} "
            f"({metadata.size_human}, taken {metadata.age}). "
            "Anything deployed there now is overwritten. Continue?",
            default=False,
        ):
            logger.info("Cancelled")
            return 0

        logger.step(1, 3, "Verifying backup")
        if verify:
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
            restore_env=restore_env,
            verify_checksum=verify,
        )
    except WASMError as exc:
        logger.error(f"Restore failed: {exc}")
        return 1

    logger.step(3, 3, "Restore complete")
    logger.success(f"Successfully restored {target} from {backup_id}")
    return 0


def _delete_backup(*, logger: Logger, backup_id: str, force: bool = False) -> int:
    """
    Delete a backup and its archive.

    Args:
        logger: Logger to report through.
        backup_id: Backup to delete.
        force: Do not ask for confirmation.

    Returns:
        0 on success, 1 if the backup is unknown or could not be deleted.
    """
    try:
        manager = BackupManager(verbose=logger.verbose)

        metadata = manager.get_backup(backup_id)
        if not metadata:
            logger.error(f"Backup not found: {backup_id}")
            return 1

        if not force and not click.confirm(
            f"Permanently delete backup {backup_id} of {metadata.domain} "
            f"({metadata.size_human}, taken {metadata.age}). "
            "The archive is removed from disk and cannot be recovered. Continue?",
            default=False,
        ):
            logger.info("Cancelled")
            return 0

        manager.delete(backup_id)
    except WASMError as exc:
        logger.error(f"Delete failed: {exc}")
        return 1

    logger.success(f"Deleted backup: {backup_id}")
    return 0


def _verify_backup(*, logger: Logger, backup_id: str) -> int:
    """
    Check that a backup archive is intact.

    Args:
        logger: Logger to report through.
        backup_id: Backup to verify.

    Returns:
        0 if the backup is valid, 1 otherwise.
    """
    try:
        manager = BackupManager(verbose=logger.verbose)

        logger.info(f"Verifying backup: {backup_id}")
        result = manager.verify(backup_id)
    except WASMError as exc:
        logger.error(f"Verification failed: {exc}")
        return 1

    if result["valid"]:
        logger.success("Backup is valid")
        if result.get("checksum_ok"):
            logger.info("  [OK] Checksum verified")
        if result.get("files_ok"):
            logger.info(f"  [OK] Archive valid ({result.get('file_count', '?')} files)")
    else:
        logger.error("Backup is invalid")
        for err in result["errors"]:
            logger.error(f"  [ERROR] {err}")

    for warn in result.get("warnings", []):
        logger.warning(f"  [WARN] {warn}")

    return 0 if result["valid"] else 1


def _show_backup(*, logger: Logger, backup_id: str, json_output: bool = False) -> int:
    """
    Show everything recorded about a backup.

    Args:
        logger: Logger to report through.
        backup_id: Backup to describe.
        json_output: Print the metadata as JSON instead of a listing.

    Returns:
        0 on success, 1 if the backup is unknown.
    """
    try:
        manager = BackupManager(verbose=logger.verbose)
        metadata = manager.get_backup(backup_id)
    except WASMError as exc:
        logger.error(f"Error: {exc}")
        return 1

    if not metadata:
        logger.error(f"Backup not found: {backup_id}")
        return 1

    if json_output:
        click.echo(json.dumps(metadata.to_dict(), indent=2))
        return 0

    logger.info(f"Backup: {metadata.id}")
    logger.info(f"  Domain:      {metadata.domain}")
    logger.info(f"  App Name:    {metadata.app_name}")
    logger.info(f"  App Type:    {metadata.app_type}")
    logger.info(f"  Size:        {metadata.size_human}")
    logger.info(f"  Created:     {metadata.created_at} ({metadata.age})")
    logger.info(f"  Format:      {metadata.version}")

    if metadata.description:
        logger.info(f"  Description: {metadata.description}")

    if metadata.git_commit:
        logger.info(f"  Git Commit:  {metadata.git_commit}")
        logger.info(f"  Git Branch:  {metadata.git_branch}")

    if metadata.tags:
        logger.info(f"  Tags:        {', '.join(metadata.tags)}")

    logger.info("  Archive contains:")
    logger.info(f"    - .env files:     {'Yes' if metadata.includes_env else 'No'}")
    logger.info(f"    - node_modules:   {'Yes' if metadata.includes_node_modules else 'No'}")
    logger.info(f"    - build output:   {'Yes' if metadata.includes_build else 'No'}")
    logger.info(f"    - databases:      {len(metadata.database_backups)}")
    logger.info(f"    - docker volumes: {len(metadata.docker_volume_backups)}")

    for db_info in metadata.database_backups:
        logger.info(f"        {db_info.get('engine', '?')}/{db_info.get('name', '?')}")

    if metadata.checksum:
        logger.info(f"  Checksum:    {metadata.checksum[:16]}...")

    return 0


def _show_storage(*, logger: Logger, json_output: bool = False) -> int:
    """
    Show how much disk the backups take.

    Args:
        logger: Logger to report through.
        json_output: Print the usage as JSON instead of a listing.

    Returns:
        0 on success, 1 if the usage could not be read.
    """
    try:
        manager = BackupManager(verbose=logger.verbose)
        usage = manager.get_storage_usage()
    except WASMError as exc:
        logger.error(f"Error: {exc}")
        return 1

    if json_output:
        click.echo(json.dumps(usage, indent=2))
        return 0

    logger.info("Backup Storage Usage")
    logger.info(
        f"  Total: {_human_bytes(usage['total_size_bytes'])} ({usage['total_backups']} backups)"
    )
    logger.info("")

    for app_name, app_usage in usage["by_app"].items():
        logger.info(
            f"  {app_name}: {_human_bytes(app_usage['size_bytes'])} ({app_usage['count']} backups)"
        )

    return 0


def _create_schedule(
    *,
    logger: Logger,
    domain: str,
    schedule: str = "daily",
    retention_count: int = 7,
    retention_days: int = 30,
) -> int:
    """
    Install a systemd timer that backs an application up on its own.

    Args:
        logger: Logger to report through.
        domain: Domain of the application to back up.
        schedule: hourly, daily, weekly, monthly or a systemd OnCalendar value.
        retention_count: Keep at most this many backups.
        retention_days: Delete backups older than this many days.

    Returns:
        0 on success, 1 if the schedule could not be installed.
    """
    from wasm.core.utils import domain_to_app_name
    from wasm.managers.backup_scheduler import BackupSchedule, BackupScheduler

    try:
        scheduler = BackupScheduler(verbose=logger.verbose)
        backup_schedule = BackupSchedule(
            domain=domain,
            app_name=domain_to_app_name(domain),
            schedule=schedule,
            retention_count=retention_count,
            retention_days=retention_days,
        )
        scheduler.create_schedule(backup_schedule)
    except WASMError as exc:
        logger.error(f"Failed to create schedule: {exc}")
        return 1

    logger.success(f"Backup schedule created for {domain}")
    logger.info(f"  Schedule: {backup_schedule.on_calendar}")
    logger.info(f"  Retention: {retention_count} backups / {retention_days} days")
    return 0


def _list_schedules(*, logger: Logger) -> int:
    """
    List the applications that back themselves up on a timer.

    Args:
        logger: Logger to report through.

    Returns:
        0 on success, 1 if the schedules could not be read.
    """
    from wasm.managers.backup_scheduler import BackupScheduler

    try:
        scheduler = BackupScheduler(verbose=logger.verbose)
        schedules = scheduler.list_schedules()
    except WASMError as exc:
        logger.error(f"Failed to list schedules: {exc}")
        return 1

    if not schedules:
        logger.info("No backup schedules found")
        return 0

    logger.header("Backup Schedules")
    for sched in schedules:
        logger.info(
            f"  {sched['app_name']}: "
            f"next={sched.get('next_run', '?')} "
            f"last={sched.get('last_run', 'never')}"
        )

    return 0


def _delete_schedule(*, logger: Logger, domain: str) -> int:
    """
    Stop backing an application up on a timer.

    Args:
        logger: Logger to report through.
        domain: Domain whose schedule is removed.

    Returns:
        0 on success, 1 if the schedule could not be removed.
    """
    from wasm.managers.backup_scheduler import BackupScheduler

    try:
        scheduler = BackupScheduler(verbose=logger.verbose)
        scheduler.remove_schedule(domain)
    except WASMError as exc:
        logger.error(f"Failed to remove schedule: {exc}")
        return 1

    logger.success(f"Backup schedule removed for {domain}")
    return 0


def _rollback_app(
    *,
    logger: Logger,
    domain: str,
    backup_id: str | None = None,
    rebuild: bool = True,
) -> int:
    """
    Roll an application back to a backup, taking a safety backup first.

    Args:
        logger: Logger to report through.
        domain: Domain of the application to roll back.
        backup_id: Backup to return to, defaulting to the most recent one.
        rebuild: Rebuild the application after the files are back.

    Returns:
        0 on success, 1 if there is nothing to roll back to or the restore
        failed.
    """
    try:
        rollback_manager = RollbackManager(verbose=logger.verbose)

        if not backup_id:
            backups = rollback_manager.list_rollback_points(domain)
            if not backups:
                logger.error(f"No backups found for {domain}")
                return 1

            logger.info(f"Rolling back to latest backup: {backups[0].id}")
            logger.info(f"  Created: {backups[0].age}")
            if backups[0].description:
                logger.info(f"  Description: {backups[0].description}")

        logger.step(1, 3, "Creating safety backup")
        try:
            rollback_manager.create_pre_deploy_backup(
                domain=domain, description="Pre-rollback safety backup"
            )
        except WASMError as exc:
            # A missing safety net is worth a warning, not an abort: the
            # operator asked to go back and already has a reason to.
            logger.warning(f"Could not create safety backup: {exc}")

        logger.step(2, 3, "Restoring from backup")
        rollback_manager.rollback(domain=domain, backup_id=backup_id, rebuild=rebuild)
    except WASMError as exc:
        logger.error(f"Rollback failed: {exc}")
        return 1

    logger.step(3, 3, "Rollback complete")
    logger.success(f"Successfully rolled back {domain}")
    return 0


@click.group()
def cli() -> None:
    """
    Container for the commands this module defines.

    ``wasm.cli.app`` picks ``backup`` or ``rollback`` out of it by name; the
    group itself is never typed by anyone.
    """


@cli.group(
    "backup",
    cls=AliasedGroup,
    aliases=BACKUP_ALIASES,
    invoke_without_command=True,
)
@click.pass_context
def backup(ctx: click.Context) -> None:
    """
    Create, inspect and restore application backups.

    A backup is a single self-contained archive: the application directory
    plus, when you ask for them, its database dumps and Docker volumes. Run
    without an action to list the backups on this server.
    """
    if ctx.invoked_subcommand is not None:
        return

    # `wasm backup` has always listed the backups, and scripts rely on it.
    state = ctx.ensure_object(Context)
    _finish(_list_backups(logger=state.logger, json_output=state.json_output))


@backup.command("create")
@click.argument("domain")
@click.option("-m", "--description", default="", help="Note to store with the backup.")
@click.option("--no-env", is_flag=True, help="Leave the .env files out of the archive.")
@click.option(
    "--include-node-modules",
    is_flag=True,
    help="Include node_modules. The archive gets much bigger.",
)
@click.option(
    "--include-build",
    is_flag=True,
    help="Include the build output (.next, dist, build).",
)
@click.option(
    "--include-databases",
    "--include-db",
    "include_databases",
    is_flag=True,
    help="Dump the application databases into the archive.",
)
@click.option(
    "--include-docker-volumes",
    is_flag=True,
    help="Copy the application's Docker volumes into the archive.",
)
@click.option(
    "--schemas",
    metavar="SCHEMA",
    multiple=True,
    help="Dump only this PostgreSQL schema. Repeat for several.",
)
@click.option(
    "--redis-method",
    type=click.Choice(REDIS_METHODS),
    default="rdb",
    show_default=True,
    help="How to capture Redis: a point-in-time rdb or the aof log.",
)
@click.option(
    "--retention-count",
    type=click.INT,
    help="Keep at most this many backups of the application.",
)
@click.option(
    "--retention-days",
    type=click.INT,
    help="Delete backups of the application older than this many days.",
)
@click.option("-t", "--tags", help="Comma-separated tags to file the backup under.")
@pass_context
def backup_create(
    state: Context,
    domain: str,
    description: str,
    no_env: bool,
    include_node_modules: bool,
    include_build: bool,
    include_databases: bool,
    include_docker_volumes: bool,
    schemas: tuple[str, ...],
    redis_method: str,
    retention_count: int | None,
    retention_days: int | None,
    tags: str | None,
) -> None:
    """
    Back an application up into one restorable archive.

    The archive holds the application directory and, with --include-databases
    or --include-docker-volumes, its data as well, so it can be restored on a
    server that knows nothing about this one.
    """
    _finish(
        _create_backup(
            logger=state.logger,
            domain=domain,
            description=description,
            include_env=not no_env,
            include_node_modules=include_node_modules,
            include_build=include_build,
            include_databases=include_databases,
            include_docker_volumes=include_docker_volumes,
            schemas=schemas,
            redis_method=redis_method,
            retention_count=retention_count,
            retention_days=retention_days,
            tags=tags,
        )
    )


@backup.command("list")
@click.argument("domain", required=False)
@click.option("-t", "--tags", help="Only show backups carrying one of these tags.")
@click.option("-n", "--limit", type=click.INT, help="Show at most this many backups.")
@pass_context
def backup_list(state: Context, domain: str | None, tags: str | None, limit: int | None) -> None:
    """
    List the backups on this server, newest first.

    Give a domain to see only that application's backups.
    """
    _finish(
        _list_backups(
            logger=state.logger,
            domain=domain,
            tags=tags,
            limit=limit,
            json_output=state.json_output,
        )
    )


@backup.command("restore")
@click.argument("backup_id")
@click.option("--target-domain", help="Restore into this domain instead of the original one.")
@click.option("--no-env", is_flag=True, help="Keep the current .env files.")
@click.option("--no-verify", is_flag=True, help="Skip the checksum check before restoring.")
@click.option("-f", "--force", is_flag=True, help="Do not ask for confirmation.")
@pass_context
def backup_restore(
    state: Context,
    backup_id: str,
    target_domain: str | None,
    no_env: bool,
    no_verify: bool,
    force: bool,
) -> None:
    """
    Put an application back the way a backup left it.

    The files under the target domain are replaced by the ones in the archive.
    """
    _finish(
        _restore_backup(
            logger=state.logger,
            backup_id=backup_id,
            target_domain=target_domain,
            restore_env=not no_env,
            verify=not no_verify,
            force=force,
        )
    )


@backup.command("delete")
@click.argument("backup_id")
@click.option("-f", "-y", "--force", is_flag=True, help="Do not ask for confirmation.")
@pass_context
def backup_delete(state: Context, backup_id: str, force: bool) -> None:
    """
    Delete a backup and remove its archive from disk.
    """
    _finish(_delete_backup(logger=state.logger, backup_id=backup_id, force=force))


@backup.command("verify")
@click.argument("backup_id")
@pass_context
def backup_verify(state: Context, backup_id: str) -> None:
    """
    Check that a backup archive is intact and restorable.

    Compares the archive against its recorded checksum and reads the contents
    without writing anything.
    """
    _finish(_verify_backup(logger=state.logger, backup_id=backup_id))


@backup.command("info")
@click.argument("backup_id")
@pass_context
def backup_info(state: Context, backup_id: str) -> None:
    """
    Show what a backup contains and where it came from.
    """
    _finish(_show_backup(logger=state.logger, backup_id=backup_id, json_output=state.json_output))


@backup.command("storage")
@pass_context
def backup_storage(state: Context) -> None:
    """
    Show how much disk the backups take, per application.
    """
    _finish(_show_storage(logger=state.logger, json_output=state.json_output))


@backup.group("schedule", cls=AliasedGroup, aliases=SCHEDULE_ALIASES)
def backup_schedule() -> None:
    """
    Back applications up automatically on a timer.
    """


@backup_schedule.command("create")
@click.argument("domain")
@click.option(
    "--schedule",
    default="daily",
    show_default=True,
    help="hourly, daily, weekly, monthly, or a systemd OnCalendar expression.",
)
@click.option(
    "--retention-count",
    type=click.INT,
    default=7,
    show_default=True,
    help="Keep at most this many backups of the application.",
)
@click.option(
    "--retention-days",
    type=click.INT,
    default=30,
    show_default=True,
    help="Delete backups of the application older than this many days.",
)
@pass_context
def schedule_create(
    state: Context,
    domain: str,
    schedule: str,
    retention_count: int,
    retention_days: int,
) -> None:
    """
    Back an application up on a timer and drop the old backups.
    """
    _finish(
        _create_schedule(
            logger=state.logger,
            domain=domain,
            schedule=schedule,
            retention_count=retention_count,
            retention_days=retention_days,
        )
    )


@backup_schedule.command("list")
@pass_context
def schedule_list(state: Context) -> None:
    """
    Show which applications back themselves up, and when they last did.
    """
    _finish(_list_schedules(logger=state.logger))


@backup_schedule.command("delete")
@click.argument("domain")
@pass_context
def schedule_delete(state: Context, domain: str) -> None:
    """
    Stop backing an application up automatically.

    The backups already taken are kept.
    """
    _finish(_delete_schedule(logger=state.logger, domain=domain))


@cli.command("rollback")
@click.argument("domain")
@click.argument("backup_id", required=False)
@click.option("--no-rebuild", is_flag=True, help="Do not rebuild after the files are back.")
@pass_context
def rollback(state: Context, domain: str, backup_id: str | None, no_rebuild: bool) -> None:
    """
    Return an application to its most recent backup.

    Takes a safety backup of the current state first, then restores. Name a
    backup id to go somewhere other than the latest one.
    """
    _finish(
        _rollback_app(
            logger=state.logger,
            domain=domain,
            backup_id=backup_id,
            rebuild=not no_rebuild,
        )
    )


def handle_backup(args: Namespace) -> int:
    """
    Handle ``wasm backup <action>`` on the argparse path.

    Kept while :mod:`wasm.cli.parser` still routes through argparse; it shares
    every helper with the Click commands rather than repeating them.

    Args:
        args: Parsed arguments.

    Returns:
        Process exit code.
    """
    verbose = getattr(args, "verbose", False)
    logger = Logger(verbose=verbose)
    action = BACKUP_ALIASES.get(
        getattr(args, "action", None) or "list", getattr(args, "action", None) or "list"
    )

    if action == "create":
        return _create_backup(
            logger=logger,
            domain=getattr(args, "domain", ""),
            description=getattr(args, "description", ""),
            include_env=not getattr(args, "no_env", False),
            include_node_modules=getattr(args, "include_node_modules", False),
            include_build=getattr(args, "include_build", False),
            include_databases=getattr(args, "include_databases", False),
            include_docker_volumes=getattr(args, "include_docker_volumes", False),
            schemas=getattr(args, "schemas", None),
            redis_method=getattr(args, "redis_method", "rdb"),
            retention_count=getattr(args, "retention_count", None),
            retention_days=getattr(args, "retention_days", None),
            tags=getattr(args, "tags", None),
        )
    if action == "list":
        return _list_backups(
            logger=logger,
            domain=getattr(args, "domain", None),
            tags=getattr(args, "tags", None),
            limit=getattr(args, "limit", None),
            json_output=getattr(args, "json", False),
        )
    if action == "restore":
        return _restore_backup(
            logger=logger,
            backup_id=getattr(args, "backup_id", ""),
            target_domain=getattr(args, "target_domain", None),
            restore_env=not getattr(args, "no_env", False),
            verify=not getattr(args, "no_verify", False),
            force=getattr(args, "force", False),
        )
    if action == "delete":
        return _delete_backup(
            logger=logger,
            backup_id=getattr(args, "backup_id", ""),
            force=getattr(args, "force", False),
        )
    if action == "verify":
        return _verify_backup(logger=logger, backup_id=getattr(args, "backup_id", ""))
    if action == "info":
        return _show_backup(
            logger=logger,
            backup_id=getattr(args, "backup_id", ""),
            json_output=getattr(args, "json", False),
        )
    if action == "storage":
        return _show_storage(logger=logger, json_output=getattr(args, "json", False))
    if action == "schedule":
        return _handle_backup_schedule(args, logger)

    logger.error(f"Unknown backup action: {action}")
    return 1


def _handle_backup_schedule(args: Namespace, logger: Logger) -> int:
    """
    Handle ``wasm backup schedule <action>`` on the argparse path.

    Args:
        args: Parsed arguments.
        logger: Logger to report through.

    Returns:
        Process exit code.
    """
    raw_action = getattr(args, "schedule_action", None)
    if not raw_action:
        logger.error("Schedule requires an action: create, list, or delete")
        return 1

    action = SCHEDULE_ALIASES.get(raw_action, raw_action)

    if action == "create":
        return _create_schedule(
            logger=logger,
            domain=getattr(args, "domain", ""),
            schedule=getattr(args, "schedule", "daily"),
            retention_count=getattr(args, "retention_count", 7),
            retention_days=getattr(args, "retention_days", 30),
        )
    if action == "list":
        return _list_schedules(logger=logger)
    if action == "delete":
        return _delete_schedule(logger=logger, domain=getattr(args, "domain", ""))

    logger.error(f"Unknown schedule action: {action}")
    return 1


def handle_rollback(args: Namespace) -> int:
    """
    Handle ``wasm rollback`` on the argparse path.

    Args:
        args: Parsed arguments.

    Returns:
        Process exit code.
    """
    verbose = getattr(args, "verbose", False)
    logger = Logger(verbose=verbose)

    domain = getattr(args, "domain", None)
    if not domain:
        logger.error("Domain is required")
        return 1

    return _rollback_app(
        logger=logger,
        domain=domain,
        backup_id=getattr(args, "backup_id", None),
        rebuild=not getattr(args, "no_rebuild", False),
    )
