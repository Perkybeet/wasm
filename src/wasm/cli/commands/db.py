# Copyright (c) 2024-2026 Yago López Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
The ``wasm db`` command group.

Engine installation, databases, users, privileges, backups and the query
console. Every command is a thin shell around
:mod:`wasm.managers.database`: this module parses, confirms and prints, and
never speaks to an engine itself.

Two things are deliberate here:

- **The work lives in module-level functions, not in the Click callbacks.**
  ``handle_db`` still routes the argparse tree that is being retired, and both
  paths call the same functions, so the two front ends cannot drift apart
  before the old one is deleted.
- **Nothing spawns a process.** The one exception is :func:`_open_client`,
  which hands the terminal to ``psql`` or ``mysql`` and is documented where it
  is defined.
"""

from __future__ import annotations

import json
import os
from argparse import Namespace
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, NoReturn

import click

from wasm.cli.app import Context, pass_context
from wasm.core.config import Config
from wasm.core.exceptions import DatabaseError
from wasm.core.logger import Logger
from wasm.managers.database import (
    BaseDatabaseManager,
    DatabaseRegistry,
    get_db_manager,
)

#: Engines whose server enforces a read-only transaction. For anything else a
#: read-only request cannot be honoured, so it is refused rather than granted
#: on paper: the manager would accept the flag and run the statement anyway.
READ_ONLY_ENGINES = frozenset({"mariadb", "mysql", "postgres", "postgresql"})

#: Placeholder printed in a connection string when the operator gave no
#: password. It is a blank to fill in, not a credential.
PASSWORD_PLACEHOLDER = "<PASSWORD>"  # noqa: S105


class EngineParamType(click.ParamType):
    """
    A database engine name, checked against the registry as it is parsed.

    Resolution is the registry's, so every spelling it accepts keeps working
    (``pg`` and ``postgres`` for PostgreSQL, ``mariadb`` for MySQL). Rejecting
    an unknown engine here rather than three calls later means a typo costs a
    usage error instead of a half-finished operation.
    """

    name = "engine"

    def convert(
        self,
        value: Any,
        param: click.Parameter | None,
        ctx: click.Context | None,
    ) -> str:
        """
        Check that the registry knows the engine.

        Args:
            value: The name the operator typed.
            param: The parameter being converted.
            ctx: The Click context.

        Returns:
            The name, unchanged, for the manager to resolve again.
        """
        if get_db_manager(str(value)) is None:
            self.fail(
                f"unknown database engine {value!r}. "
                f"Available: {', '.join(sorted(DatabaseRegistry.list_engines()))}",
                param,
                ctx,
            )
        return str(value)


#: The type every ``--engine`` option and every engine argument uses.
ENGINE = EngineParamType()


def _exit(code: int) -> NoReturn:
    """
    Leave the command with an exit status.

    Args:
        code: Process exit code.

    Raises:
        click.exceptions.Exit: Always. Click turns it into the exit status.
    """
    click.get_current_context().exit(code)


def _get_manager(engine: str | None, logger: Logger) -> BaseDatabaseManager | None:
    """
    Resolve an engine name to its manager.

    Args:
        engine: Engine name or alias.
        logger: Logger for the error message.

    Returns:
        The manager, or None when the name is missing or unknown.
    """
    if not engine:
        logger.error("Database engine is required")
        logger.info("Available engines: " + ", ".join(DatabaseRegistry.list_engines()))
        return None

    manager = get_db_manager(engine, verbose=logger.verbose)
    if not manager:
        logger.error(f"Unknown database engine: {engine}")
        logger.info("Available engines: " + ", ".join(DatabaseRegistry.list_engines()))
        return None

    return manager


def _confirm(question: str, *, force: bool) -> bool:
    """
    Ask before doing something that cannot be undone.

    Args:
        question: The question, naming the exact resource and consequence.
        force: Skip the question because the operator already said so.

    Returns:
        True when the operation may proceed.
    """
    if force:
        return True
    try:
        return click.confirm(question, default=False)
    except click.Abort:
        # A closed stdin or a Ctrl-C is a refusal, not a failure.
        return False


def _privilege_list(privileges: str | None) -> list[str] | None:
    """
    Split the comma-separated privilege option into entries.

    Only the list syntax is undone here. Which keywords are acceptable is the
    manager's whitelist to decide, and duplicating it in the CLI is how the two
    lists end up disagreeing.

    Args:
        privileges: The raw option value, or None for the engine's default.

    Returns:
        The entries, or None when the operator gave none.
    """
    if not privileges:
        return None
    return [part.strip() for part in privileges.split(",") if part.strip()]


def _open_client(argv: Sequence[str]) -> NoReturn:
    """
    Replace this process with an interactive database client.

    This is the only execution in the CLI that does not go through the
    CommandRunner, and it has to be. The runner captures output and returns
    when the process is done, which turns a ``psql`` session into a hang with
    no prompt and no way to type into it. A client that owns the terminal is
    the whole point of ``wasm db connect``, so this process steps aside for it.

    Args:
        argv: Program and arguments, as the manager built them.

    Raises:
        DatabaseError: When the client is not on PATH or cannot be executed.
    """
    try:
        os.execvp(argv[0], list(argv))  # noqa: S606
    except OSError as exc:
        raise DatabaseError(
            f"Could not start the database client: {argv[0]}",
            details=f"{exc}. Install the engine's client package and try again.",
        ) from exc


# ==================== Engine management ====================


def _install(engine: str, *, logger: Logger) -> int:
    """
    Install a database engine.

    Args:
        engine: Engine name or alias.
        logger: Logger for progress and errors.

    Returns:
        Process exit code.
    """
    manager = _get_manager(engine, logger)
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


def _uninstall(engine: str, *, purge: bool, force: bool, logger: Logger) -> int:
    """
    Remove a database engine.

    Args:
        engine: Engine name or alias.
        purge: Also delete the data directory and the configuration.
        force: Do not ask for confirmation.
        logger: Logger for progress and errors.

    Returns:
        Process exit code.
    """
    manager = _get_manager(engine, logger)
    if not manager:
        return 1

    try:
        if not manager.is_installed():
            logger.info(f"{manager.DISPLAY_NAME} is not installed")
            return 0

        question = f"Uninstall {manager.DISPLAY_NAME} from this server?"
        if purge:
            question = (
                f"Uninstall {manager.DISPLAY_NAME} and delete every database it holds, "
                "along with its configuration? This cannot be undone"
            )
        if not _confirm(question, force=force):
            logger.info("Cancelled")
            return 0

        logger.step(1, 2, f"Uninstalling {manager.DISPLAY_NAME}...")
        manager.uninstall(purge=purge)

        logger.step(2, 2, "Uninstallation complete")
        logger.success(f"{manager.DISPLAY_NAME} uninstalled")

        return 0
    except DatabaseError as e:
        logger.error(str(e))
        return 1


def _status(engine: str | None, *, json_output: bool, logger: Logger) -> int:
    """
    Report whether engines are installed and running.

    Args:
        engine: Engine name or alias. All installed engines when omitted.
        json_output: Print the statuses as JSON.
        logger: Logger for errors.

    Returns:
        Process exit code.
    """
    if engine:
        manager = _get_manager(engine, logger)
        if manager is None:
            return 1
        managers = [manager]
    else:
        managers = DatabaseRegistry.get_all_managers(verbose=logger.verbose)

    statuses = [manager.get_status() for manager in managers]

    if json_output:
        click.echo(json.dumps(statuses, indent=2))
        return 0

    for status in statuses:
        installed = "yes" if status["installed"] else "no"
        version = status.get("version", "N/A")

        click.echo(f"\n{status['display_name']}")
        click.echo(f"  Installed: {installed}")
        if status["installed"]:
            click.echo(f"  Version:   {version}")
            click.echo(f"  Status:    {'running' if status.get('running') else 'stopped'}")
            click.echo(f"  Port:      {status['port']}")
            click.echo(f"  Service:   {status['service']}")

    return 0


def _start(engine: str, *, logger: Logger) -> int:
    """
    Start an engine's service.

    Args:
        engine: Engine name or alias.
        logger: Logger for progress and errors.

    Returns:
        Process exit code.
    """
    manager = _get_manager(engine, logger)
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


def _stop(engine: str, *, logger: Logger) -> int:
    """
    Stop an engine's service.

    Args:
        engine: Engine name or alias.
        logger: Logger for progress and errors.

    Returns:
        Process exit code.
    """
    manager = _get_manager(engine, logger)
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


def _restart(engine: str, *, logger: Logger) -> int:
    """
    Restart an engine's service.

    Args:
        engine: Engine name or alias.
        logger: Logger for progress and errors.

    Returns:
        Process exit code.
    """
    manager = _get_manager(engine, logger)
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


def _engines(*, json_output: bool, logger: Logger) -> int:
    """
    List the engines WASM knows how to manage.

    Args:
        json_output: Print the list as JSON.
        logger: Logger, used for its verbosity setting.

    Returns:
        Process exit code.
    """
    engines = []
    for engine in DatabaseRegistry.list_engines():
        manager = get_db_manager(engine, verbose=logger.verbose)
        if manager:
            engines.append(
                {
                    "name": manager.ENGINE_NAME,
                    "display_name": manager.DISPLAY_NAME,
                    "installed": manager.is_installed(),
                    "version": manager.get_version() if manager.is_installed() else None,
                    "port": manager.DEFAULT_PORT,
                }
            )

    if json_output:
        click.echo(json.dumps(engines, indent=2))
        return 0

    click.echo("\nAvailable Database Engines:")
    click.echo("-" * 50)

    for eng in engines:
        installed = "*" if eng["installed"] else " "
        version = f"v{eng['version']}" if eng["version"] else "not installed"
        click.echo(f"  [{installed}] {eng['display_name']:<20} {version:<15} (port {eng['port']})")

    click.echo("")
    return 0


# ==================== Database management ====================


def _create(
    name: str,
    *,
    engine: str,
    owner: str | None,
    encoding: str | None,
    logger: Logger,
) -> int:
    """
    Create a database and record it in the store.

    Args:
        name: Database name.
        engine: Engine name or alias.
        owner: User that will own the database.
        encoding: Character encoding.
        logger: Logger for progress and errors.

    Returns:
        Process exit code.
    """
    manager = _get_manager(engine, logger)
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

        from wasm.core.store import Database, get_store

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


def _drop(name: str, *, engine: str, force: bool, logger: Logger) -> int:
    """
    Delete a database and forget it in the store.

    Args:
        name: Database name.
        engine: Engine name or alias.
        force: Do not ask for confirmation.
        logger: Logger for progress and errors.

    Returns:
        Process exit code.
    """
    manager = _get_manager(engine, logger)
    if not manager:
        return 1

    try:
        if not manager.is_running():
            logger.error(f"{manager.DISPLAY_NAME} is not running")
            return 1

        question = (
            f"Drop database '{name}' from {manager.DISPLAY_NAME}, "
            "deleting every table and row in it? This cannot be undone"
        )
        if not _confirm(question, force=force):
            logger.info("Cancelled")
            return 0

        manager.drop_database(name, force=force)

        from wasm.core.store import get_store

        store = get_store()
        store.delete_database(name, manager.ENGINE_NAME)

        logger.success(f"Dropped database: {name}")

        return 0
    except DatabaseError as e:
        logger.error(str(e))
        return 1


def _list(*, engine: str | None, json_output: bool, logger: Logger) -> int:
    """
    List the databases of every running engine.

    Args:
        engine: Engine name or alias. Every installed engine when omitted.
        json_output: Print the list as JSON.
        logger: Logger for errors.

    Returns:
        Process exit code.
    """
    from wasm.core.store import get_store

    store = get_store()

    if engine:
        manager = _get_manager(engine, logger)
        if manager is None:
            return 1
        managers = [manager]
    else:
        managers = DatabaseRegistry.get_installed(verbose=logger.verbose)

    all_databases = []

    for manager in managers:
        if not manager.is_running():
            continue

        try:
            databases = manager.list_databases()
            for db in databases:
                db_dict = db.to_dict()

                # A database WASM created is marked, and so is the app it was
                # created for, because that is the association the operator
                # cannot get from the engine itself.
                store_db = store.get_database(db.name, manager.ENGINE_NAME)
                if store_db:
                    db_dict["tracked"] = True
                    if store_db.app_id:
                        app = store.get_app_by_id(store_db.app_id)
                        if app:
                            db_dict["linked_app"] = app.domain
                else:
                    db_dict["tracked"] = False

                all_databases.append(db_dict)
        except Exception as e:
            # One unreachable engine must not hide the databases of the others.
            if logger.verbose:
                logger.warning(f"Could not list {manager.DISPLAY_NAME} databases: {e}")

    if json_output:
        click.echo(json.dumps(all_databases, indent=2))
        return 0

    if not all_databases:
        logger.info("No databases found")
        return 0

    by_engine: dict[str, list[dict[str, Any]]] = {}
    for entry in all_databases:
        by_engine.setdefault(entry.get("engine", "unknown"), []).append(entry)

    for eng, entries in by_engine.items():
        click.echo(f"\n{eng.upper()}")
        click.echo("-" * 50)
        for entry in entries:
            size = entry.get("size", "")
            tables = entry.get("tables", 0)
            tracked = "*" if entry.get("tracked") else " "
            linked = f" -> {entry['linked_app']}" if entry.get("linked_app") else ""

            size_str = f" ({size})" if size else ""
            tables_str = f" - {tables} tables" if tables else ""

            click.echo(f"  [{tracked}] {entry['name']}{size_str}{tables_str}{linked}")

    click.echo("")
    click.echo("  [*] = tracked by WASM")
    return 0


def _info(name: str, *, engine: str, json_output: bool, logger: Logger) -> int:
    """
    Show what an engine knows about one database.

    Args:
        name: Database name.
        engine: Engine name or alias.
        json_output: Print the details as JSON.
        logger: Logger for errors.

    Returns:
        Process exit code.
    """
    manager = _get_manager(engine, logger)
    if not manager:
        return 1

    try:
        if not manager.is_running():
            logger.error(f"{manager.DISPLAY_NAME} is not running")
            return 1

        info = manager.get_database_info(name)

        if json_output:
            click.echo(json.dumps(info.to_dict(), indent=2))
            return 0

        click.echo(f"\nDatabase: {info.name}")
        click.echo(f"Engine:   {info.engine}")
        if info.size:
            click.echo(f"Size:     {info.size}")
        if info.tables:
            click.echo(f"Tables:   {info.tables}")
        if info.owner:
            click.echo(f"Owner:    {info.owner}")
        if info.encoding:
            click.echo(f"Encoding: {info.encoding}")

        return 0
    except DatabaseError as e:
        logger.error(str(e))
        return 1


# ==================== User management ====================


def _user_create(
    username: str,
    *,
    engine: str,
    password: str | None,
    database: str | None,
    host: str,
    logger: Logger,
) -> int:
    """
    Create a database user, generating a password when none is given.

    Args:
        username: User name.
        engine: Engine name or alias.
        password: Password. Generated and printed once when omitted.
        database: Database to grant the new user access to.
        host: Host the user may connect from.
        logger: Logger for progress and errors.

    Returns:
        Process exit code.
    """
    manager = _get_manager(engine, logger)
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

        logger.success(f"Created user: {user_info.username}")

        if not password:
            logger.info(f"  Password: {generated_password}")
            logger.warning("  Save this password - it won't be shown again!")

        if database:
            try:
                manager.grant_privileges(username, database, host=host)
                logger.info(f"  Granted privileges on: {database}")
            except Exception as e:
                # The user exists either way; a failed grant is reported and
                # fixed with `wasm db grant`, not by rolling the user back.
                logger.warning(f"  Could not grant privileges: {e}")

        return 0
    except DatabaseError as e:
        logger.error(str(e))
        return 1


def _user_delete(username: str, *, engine: str, host: str, force: bool, logger: Logger) -> int:
    """
    Delete a database user.

    Args:
        username: User name.
        engine: Engine name or alias.
        host: Host restriction the user was created with.
        force: Do not ask for confirmation.
        logger: Logger for progress and errors.

    Returns:
        Process exit code.
    """
    manager = _get_manager(engine, logger)
    if not manager:
        return 1

    try:
        if not manager.is_running():
            logger.error(f"{manager.DISPLAY_NAME} is not running")
            return 1

        question = (
            f"Delete user '{username}'@'{host}' from {manager.DISPLAY_NAME}? "
            "Anything connecting as this user will stop working"
        )
        if not _confirm(question, force=force):
            logger.info("Cancelled")
            return 0

        manager.drop_user(username, host=host)
        logger.success(f"Deleted user: {username}")

        return 0
    except DatabaseError as e:
        logger.error(str(e))
        return 1


def _user_list(*, engine: str, json_output: bool, logger: Logger) -> int:
    """
    List an engine's users.

    Args:
        engine: Engine name or alias.
        json_output: Print the list as JSON.
        logger: Logger for errors.

    Returns:
        Process exit code.
    """
    manager = _get_manager(engine, logger)
    if not manager:
        return 1

    try:
        if not manager.is_running():
            logger.error(f"{manager.DISPLAY_NAME} is not running")
            return 1

        users = manager.list_users()

        if json_output:
            click.echo(json.dumps([u.to_dict() for u in users], indent=2))
            return 0

        if not users:
            logger.info("No users found")
            return 0

        click.echo(f"\n{manager.DISPLAY_NAME} Users:")
        click.echo("-" * 50)
        for user in users:
            host_str = f"@{user.host}" if user.host != "localhost" else ""
            privs = ", ".join(user.privileges[:3]) if user.privileges else ""
            if len(user.privileges) > 3:
                privs += f" (+{len(user.privileges) - 3} more)"

            click.echo(f"  {user.username}{host_str}")
            if privs:
                click.echo(f"    Privileges: {privs}")

        click.echo("")
        return 0
    except DatabaseError as e:
        logger.error(str(e))
        return 1


def _grant(
    username: str,
    database: str,
    *,
    engine: str,
    privileges: str | None,
    host: str,
    logger: Logger,
) -> int:
    """
    Grant a user privileges on a database.

    Args:
        username: User name.
        database: Database name.
        engine: Engine name or alias.
        privileges: Comma-separated privileges, or None for the engine default.
        host: Host the grant applies to.
        logger: Logger for progress and errors.

    Returns:
        Process exit code.
    """
    manager = _get_manager(engine, logger)
    if not manager:
        return 1

    try:
        manager.grant_privileges(
            username,
            database,
            privileges=_privilege_list(privileges),
            host=host,
        )
        logger.success(f"Granted privileges on {database} to {username}")
        return 0
    except DatabaseError as e:
        logger.error(str(e))
        return 1


def _revoke(
    username: str,
    database: str,
    *,
    engine: str,
    privileges: str | None,
    host: str,
    logger: Logger,
) -> int:
    """
    Take privileges away from a user.

    Args:
        username: User name.
        database: Database name.
        engine: Engine name or alias.
        privileges: Comma-separated privileges, or None for the engine default.
        host: Host the grant applies to.
        logger: Logger for progress and errors.

    Returns:
        Process exit code.
    """
    manager = _get_manager(engine, logger)
    if not manager:
        return 1

    try:
        manager.revoke_privileges(
            username,
            database,
            privileges=_privilege_list(privileges),
            host=host,
        )
        logger.success(f"Revoked privileges on {database} from {username}")
        return 0
    except DatabaseError as e:
        logger.error(str(e))
        return 1


# ==================== Backup and restore ====================


def _backup(
    database: str,
    *,
    engine: str,
    output: Path | None,
    compress: bool,
    logger: Logger,
) -> int:
    """
    Write a database to a backup file.

    Args:
        database: Database name.
        engine: Engine name or alias.
        output: Where to write the backup. The engine's backup directory when
            omitted.
        compress: Compress the backup with gzip.
        logger: Logger for progress and errors.

    Returns:
        Process exit code.
    """
    manager = _get_manager(engine, logger)
    if not manager:
        return 1

    try:
        if not manager.is_running():
            logger.error(f"{manager.DISPLAY_NAME} is not running")
            return 1

        logger.step(1, 2, f"Creating backup of {database}...")

        backup_info = manager.backup(
            database=database,
            output_path=output,
            compress=compress,
        )

        logger.step(2, 2, "Backup complete")
        logger.success(f"Backup created: {backup_info.path}")
        logger.info(f"  Size: {backup_info.to_dict()['size_human']}")

        return 0
    except DatabaseError as e:
        logger.error(str(e))
        return 1


def _restore(
    database: str,
    backup_file: Path,
    *,
    engine: str,
    drop_existing: bool,
    force: bool,
    logger: Logger,
) -> int:
    """
    Load a backup into a database.

    Args:
        database: Target database name.
        backup_file: Backup file to read.
        drop_existing: Drop the target database before restoring.
        engine: Engine name or alias.
        force: Do not ask for confirmation.
        logger: Logger for progress and errors.

    Returns:
        Process exit code.
    """
    manager = _get_manager(engine, logger)
    if not manager:
        return 1

    try:
        if not manager.is_running():
            logger.error(f"{manager.DISPLAY_NAME} is not running")
            return 1

        if drop_existing:
            question = (
                f"Drop database '{database}' and restore it from {backup_file}? "
                "Everything currently in it is lost"
            )
        else:
            question = (
                f"Restore database '{database}' from {backup_file}? "
                "Existing rows may be overwritten"
            )
        if not _confirm(question, force=force):
            logger.info("Cancelled")
            return 0

        logger.step(1, 2, f"Restoring {database}...")

        manager.restore(
            database=database,
            backup_path=backup_file,
            drop_existing=drop_existing,
        )

        logger.step(2, 2, "Restore complete")
        logger.success(f"Database {database} restored")

        return 0
    except DatabaseError as e:
        logger.error(str(e))
        return 1


def _backups(*, engine: str | None, database: str | None, json_output: bool, logger: Logger) -> int:
    """
    List the backups on this server.

    Args:
        engine: Engine name or alias. Every installed engine when omitted.
        database: Only list backups of this database.
        json_output: Print the list as JSON.
        logger: Logger for errors.

    Returns:
        Process exit code.
    """
    if engine:
        manager = _get_manager(engine, logger)
        if manager is None:
            return 1
        managers = [manager]
    else:
        managers = DatabaseRegistry.get_installed(verbose=logger.verbose)

    all_backups = []

    for manager in managers:
        try:
            for backup in manager.list_backups(database=database):
                all_backups.append(backup.to_dict())
        except Exception as e:
            # An unreadable backup directory for one engine must not hide the
            # backups of the others.
            if logger.verbose:
                logger.warning(f"Could not list {manager.DISPLAY_NAME} backups: {e}")

    if json_output:
        click.echo(json.dumps(all_backups, indent=2))
        return 0

    if not all_backups:
        logger.info("No backups found")
        return 0

    click.echo("\nAvailable Backups:")
    click.echo("-" * 60)
    for backup_dict in all_backups:
        click.echo(f"  {backup_dict['database']} ({backup_dict['engine']})")
        click.echo(f"    Path:    {backup_dict['path']}")
        click.echo(f"    Size:    {backup_dict['size_human']}")
        click.echo(f"    Created: {backup_dict['created']}")
        click.echo("")

    return 0


# ==================== Query and connection ====================


def _query(database: str, query: str, *, engine: str, read_only: bool, logger: Logger) -> int:
    """
    Run one statement against a database.

    Args:
        database: Database name.
        query: The statement.
        engine: Engine name or alias.
        read_only: Run inside the engine's own read-only transaction.
        logger: Logger for errors.

    Returns:
        Process exit code.
    """
    manager = _get_manager(engine, logger)
    if not manager:
        return 1

    if read_only and manager.ENGINE_NAME.lower() not in READ_ONLY_ENGINES:
        logger.error(f"Read-only mode is not available for {manager.DISPLAY_NAME}")
        logger.info(
            "WASM can only hold PostgreSQL and MySQL to a read-only transaction. "
            "Re-run with --write if you accept that the statement may change data."
        )
        return 1

    try:
        if not manager.is_running():
            logger.error(f"{manager.DISPLAY_NAME} is not running")
            return 1

        # The guarantee is the engine's transaction, not a keyword check here:
        # a leading keyword does not tell you what a statement does, and
        # WITH x AS (DELETE ... RETURNING *) SELECT * FROM x begins with WITH.
        success, output = manager.execute_query(database, query, read_only=read_only)

        if output:
            click.echo(output)

        return 0 if success else 1
    except DatabaseError as e:
        logger.error(str(e))
        return 1


def _connect(
    *,
    engine: str,
    database: str | None,
    username: str | None,
    dry_run: bool,
    logger: Logger,
) -> int:
    """
    Open an interactive session with the engine's own client.

    Args:
        engine: Engine name or alias.
        database: Database to open.
        username: User to connect as.
        dry_run: Report the client command instead of running it.
        logger: Logger for progress and errors.

    Returns:
        Process exit code. On success this process has already been replaced by
        the client and nothing is returned.
    """
    manager = _get_manager(engine, logger)
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
    except (DatabaseError, NotImplementedError) as e:
        logger.error(str(e))
        return 1

    if dry_run:
        logger.info(f"would run: {' '.join(cmd)}")
        return 0

    logger.info(f"Connecting to {manager.DISPLAY_NAME}...")
    logger.info(f"Command: {' '.join(cmd)}")

    _open_client(cmd)


def _connection_string(
    database: str,
    username: str,
    *,
    engine: str,
    password: str | None,
    host: str,
    logger: Logger,
) -> int:
    """
    Print a connection string for an application to use.

    Args:
        database: Database name.
        username: User name.
        engine: Engine name or alias.
        password: Password. A placeholder is printed when omitted, so the
            string can be shown without a secret in it.
        host: Host the application will connect to.
        logger: Logger for errors.

    Returns:
        Process exit code.
    """
    manager = _get_manager(engine, logger)
    if not manager:
        return 1

    conn_string = manager.get_connection_string(
        database=database,
        username=username,
        password=password or PASSWORD_PLACEHOLDER,
        host=host,
    )

    click.echo(conn_string)
    return 0


def _config(*, engine: str, user: str | None, password: str | None, logger: Logger) -> int:
    """
    Store the administrative credentials WASM uses for an engine.

    Args:
        engine: Engine name or alias.
        user: Administrative user name.
        password: Administrative password.
        logger: Logger for progress and errors.

    Returns:
        Process exit code.
    """
    config = Config()

    if user:
        config.set(f"databases.credentials.{engine}.user", user)
    if password:
        config.set(f"databases.credentials.{engine}.password", password)

    if config.save():
        logger.success(f"Updated credentials for {engine}")
        return 0

    logger.error("Failed to save configuration")
    return 1


# ==================== The argparse front end, on its way out ====================

#: How each legacy action reaches the function that does the work. The lambdas
#: exist so the argparse tree and the Click tree share one implementation until
#: ``wasm.cli.parser`` is deleted.
_LEGACY_ACTIONS: dict[str, Callable[[Namespace, Logger], int]] = {
    "install": lambda args, log: _install(args.engine, logger=log),
    "uninstall": lambda args, log: _uninstall(
        args.engine, purge=args.purge, force=args.force, logger=log
    ),
    "status": lambda args, log: _status(
        getattr(args, "engine", None), json_output=getattr(args, "json", False), logger=log
    ),
    "start": lambda args, log: _start(args.engine, logger=log),
    "stop": lambda args, log: _stop(args.engine, logger=log),
    "restart": lambda args, log: _restart(args.engine, logger=log),
    "engines": lambda args, log: _engines(json_output=getattr(args, "json", False), logger=log),
    "create": lambda args, log: _create(
        args.name,
        engine=args.engine,
        owner=getattr(args, "owner", None),
        encoding=getattr(args, "encoding", None),
        logger=log,
    ),
    "drop": lambda args, log: _drop(args.name, engine=args.engine, force=args.force, logger=log),
    "list": lambda args, log: _list(
        engine=getattr(args, "engine", None),
        json_output=getattr(args, "json", False),
        logger=log,
    ),
    "info": lambda args, log: _info(
        args.name, engine=args.engine, json_output=getattr(args, "json", False), logger=log
    ),
    "user-create": lambda args, log: _user_create(
        args.username,
        engine=args.engine,
        password=getattr(args, "password", None),
        database=getattr(args, "database", None),
        host=getattr(args, "host", "localhost"),
        logger=log,
    ),
    "user-delete": lambda args, log: _user_delete(
        args.username,
        engine=args.engine,
        host=getattr(args, "host", "localhost"),
        force=args.force,
        logger=log,
    ),
    "user-list": lambda args, log: _user_list(
        engine=args.engine, json_output=getattr(args, "json", False), logger=log
    ),
    "grant": lambda args, log: _grant(
        args.username,
        args.database,
        engine=args.engine,
        privileges=getattr(args, "privileges", None),
        host=getattr(args, "host", "localhost"),
        logger=log,
    ),
    "revoke": lambda args, log: _revoke(
        args.username,
        args.database,
        engine=args.engine,
        privileges=getattr(args, "privileges", None),
        host=getattr(args, "host", "localhost"),
        logger=log,
    ),
    "backup": lambda args, log: _backup(
        args.database,
        engine=args.engine,
        output=Path(args.output) if getattr(args, "output", None) else None,
        compress=not getattr(args, "no_compress", False),
        logger=log,
    ),
    "restore": lambda args, log: _restore(
        args.database,
        Path(args.file),
        engine=args.engine,
        drop_existing=getattr(args, "drop", False),
        force=args.force,
        logger=log,
    ),
    "backups": lambda args, log: _backups(
        engine=getattr(args, "engine", None),
        database=getattr(args, "database", None),
        json_output=getattr(args, "json", False),
        logger=log,
    ),
    # The argparse tree has no way to ask for a write, so it keeps its old
    # behaviour. Read-only by default is the Click front end's contract.
    "query": lambda args, log: _query(
        args.database, args.query, engine=args.engine, read_only=False, logger=log
    ),
    "connect": lambda args, log: _connect(
        engine=args.engine,
        database=getattr(args, "database", None),
        username=getattr(args, "username", None),
        dry_run=False,
        logger=log,
    ),
    "connection-string": lambda args, log: _connection_string(
        args.database,
        args.username,
        engine=args.engine,
        password=getattr(args, "password", None),
        host=getattr(args, "host", "localhost"),
        logger=log,
    ),
    "config": lambda args, log: _config(
        engine=args.engine,
        user=getattr(args, "user", None),
        password=getattr(args, "password", None),
        logger=log,
    ),
}


def handle_db(args: Namespace) -> int:
    """
    Route a parsed argparse namespace to the right database action.

    Kept while ``wasm.cli.parser`` is still the front end. It delegates to the
    same functions the Click commands call.

    Args:
        args: The parsed arguments.

    Returns:
        Process exit code.
    """
    verbose = getattr(args, "verbose", False)
    logger = Logger(verbose=verbose)
    action = getattr(args, "action", None)

    if not action:
        logger.error("No action specified")
        logger.info("Use: wasm db --help")
        return 1

    handler = _LEGACY_ACTIONS.get(action)
    if not handler:
        logger.error(f"Unknown action: {action}")
        return 1

    return handler(args, logger)


# ==================== The Click front end ====================


class DatabaseGroup(click.Group):
    """
    The ``db`` group, with the shorthand its subcommands have always had.

    ``wasm db ls`` is in scripts and in muscle memory, so it resolves here
    rather than being a second registration that drifts from the first.
    """

    #: Local shorthand to the command it stands for.
    ALIASES: dict[str, str] = {"ls": "list"}

    def get_command(self, ctx: click.Context, name: str) -> click.Command | None:
        """
        Look a subcommand up, resolving the group's own shorthand.

        Args:
            ctx: The Click context.
            name: The name the operator typed.

        Returns:
            The command, or None when there is no such subcommand.
        """
        return super().get_command(ctx, self.ALIASES.get(name, name))


@click.group(cls=DatabaseGroup, name="db")
def cli() -> None:
    """Install engines, and manage databases, users and backups."""


@cli.command()
@click.argument("engine", type=ENGINE)
@pass_context
def install(ctx: Context, engine: str) -> None:
    """Install a database engine on this server."""
    _exit(_install(engine, logger=ctx.logger))


@cli.command()
@click.argument("engine", type=ENGINE)
@click.option("--purge", is_flag=True, help="Also delete the data and the configuration.")
@click.option("--force", "-f", "-y", is_flag=True, help="Do not ask for confirmation.")
@pass_context
def uninstall(ctx: Context, engine: str, purge: bool, force: bool) -> None:
    """Remove a database engine from this server."""
    _exit(_uninstall(engine, purge=purge, force=force, logger=ctx.logger))


@cli.command()
@click.argument("engine", type=ENGINE, required=False)
@pass_context
def status(ctx: Context, engine: str | None) -> None:
    """Show which engines are installed and which are running."""
    _exit(_status(engine, json_output=ctx.json_output, logger=ctx.logger))


@cli.command()
@click.argument("engine", type=ENGINE)
@pass_context
def start(ctx: Context, engine: str) -> None:
    """Start an engine's service."""
    _exit(_start(engine, logger=ctx.logger))


@cli.command()
@click.argument("engine", type=ENGINE)
@pass_context
def stop(ctx: Context, engine: str) -> None:
    """Stop an engine's service."""
    _exit(_stop(engine, logger=ctx.logger))


@cli.command()
@click.argument("engine", type=ENGINE)
@pass_context
def restart(ctx: Context, engine: str) -> None:
    """Restart an engine's service."""
    _exit(_restart(engine, logger=ctx.logger))


@cli.command()
@pass_context
def engines(ctx: Context) -> None:
    """List the engines WASM can manage, and their versions."""
    _exit(_engines(json_output=ctx.json_output, logger=ctx.logger))


@cli.command()
@click.argument("name")
@click.option("--engine", "-e", type=ENGINE, required=True, help="Engine to create it on.")
@click.option("--owner", "-o", help="User that will own the database.")
@click.option("--encoding", help="Character encoding. Defaults to UTF8.")
@pass_context
def create(
    ctx: Context,
    name: str,
    engine: str,
    owner: str | None,
    encoding: str | None,
) -> None:
    """Create a database and record it in the store."""
    _exit(_create(name, engine=engine, owner=owner, encoding=encoding, logger=ctx.logger))


@cli.command()
@click.argument("name")
@click.option("--engine", "-e", type=ENGINE, required=True, help="Engine the database is on.")
@click.option("--force", "-f", "-y", is_flag=True, help="Do not ask for confirmation.")
@pass_context
def drop(ctx: Context, name: str, engine: str, force: bool) -> None:
    """Delete a database and everything in it."""
    _exit(_drop(name, engine=engine, force=force, logger=ctx.logger))


@cli.command("list")
@click.option("--engine", "-e", type=ENGINE, help="Only this engine. Defaults to all of them.")
@pass_context
def list_databases(ctx: Context, engine: str | None) -> None:
    """List the databases on every running engine."""
    _exit(_list(engine=engine, json_output=ctx.json_output, logger=ctx.logger))


@cli.command()
@click.argument("name")
@click.option("--engine", "-e", type=ENGINE, required=True, help="Engine the database is on.")
@pass_context
def info(ctx: Context, name: str, engine: str) -> None:
    """Show the size, owner and encoding of a database."""
    _exit(_info(name, engine=engine, json_output=ctx.json_output, logger=ctx.logger))


@cli.command("user-create")
@click.argument("username")
@click.option("--engine", "-e", type=ENGINE, required=True, help="Engine to create the user on.")
@click.option("--password", "-p", help="Password. One is generated and shown when omitted.")
@click.option("--database", "-d", help="Database to grant the new user access to.")
@click.option(
    "--host", default="localhost", show_default=True, help="Host the user may connect from."
)
@pass_context
def user_create(
    ctx: Context,
    username: str,
    engine: str,
    password: str | None,
    database: str | None,
    host: str,
) -> None:
    """Create a database user."""
    _exit(
        _user_create(
            username,
            engine=engine,
            password=password,
            database=database,
            host=host,
            logger=ctx.logger,
        )
    )


@cli.command("user-delete")
@click.argument("username")
@click.option("--engine", "-e", type=ENGINE, required=True, help="Engine the user is on.")
@click.option(
    "--host", default="localhost", show_default=True, help="Host the user was created for."
)
@click.option("--force", "-f", "-y", is_flag=True, help="Do not ask for confirmation.")
@pass_context
def user_delete(ctx: Context, username: str, engine: str, host: str, force: bool) -> None:
    """Delete a database user."""
    _exit(_user_delete(username, engine=engine, host=host, force=force, logger=ctx.logger))


@cli.command("user-list")
@click.option("--engine", "-e", type=ENGINE, required=True, help="Engine to list the users of.")
@pass_context
def user_list(ctx: Context, engine: str) -> None:
    """List the users of an engine."""
    _exit(_user_list(engine=engine, json_output=ctx.json_output, logger=ctx.logger))


@cli.command()
@click.argument("username")
@click.argument("database")
@click.option("--engine", "-e", type=ENGINE, required=True, help="Engine the database is on.")
@click.option("--privileges", help="Comma-separated privileges. Defaults to the engine's full set.")
@click.option("--host", default="localhost", show_default=True, help="Host the grant applies to.")
@pass_context
def grant(
    ctx: Context,
    username: str,
    database: str,
    engine: str,
    privileges: str | None,
    host: str,
) -> None:
    """Grant a user privileges on a database."""
    _exit(
        _grant(
            username, database, engine=engine, privileges=privileges, host=host, logger=ctx.logger
        )
    )


@cli.command()
@click.argument("username")
@click.argument("database")
@click.option("--engine", "-e", type=ENGINE, required=True, help="Engine the database is on.")
@click.option("--privileges", help="Comma-separated privileges. Defaults to the engine's full set.")
@click.option("--host", default="localhost", show_default=True, help="Host the grant applies to.")
@pass_context
def revoke(
    ctx: Context,
    username: str,
    database: str,
    engine: str,
    privileges: str | None,
    host: str,
) -> None:
    """Take privileges away from a user."""
    _exit(
        _revoke(
            username, database, engine=engine, privileges=privileges, host=host, logger=ctx.logger
        )
    )


@cli.command()
@click.argument("database")
@click.option("--engine", "-e", type=ENGINE, required=True, help="Engine the database is on.")
@click.option(
    "--output",
    "-o",
    type=click.Path(dir_okay=False, path_type=Path),
    help="Where to write the file. Defaults to the engine's backup directory.",
)
@click.option("--no-compress", is_flag=True, help="Write the dump without gzip.")
@pass_context
def backup(
    ctx: Context,
    database: str,
    engine: str,
    output: Path | None,
    no_compress: bool,
) -> None:
    """Write a database to a backup file."""
    _exit(
        _backup(
            database,
            engine=engine,
            output=output,
            compress=not no_compress,
            logger=ctx.logger,
        )
    )


@cli.command()
@click.argument("database")
@click.argument("file", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--engine", "-e", type=ENGINE, required=True, help="Engine the database is on.")
@click.option("--drop", is_flag=True, help="Drop the database before restoring it.")
@click.option("--force", "-f", "-y", is_flag=True, help="Do not ask for confirmation.")
@pass_context
def restore(
    ctx: Context,
    database: str,
    file: Path,
    engine: str,
    drop: bool,
    force: bool,
) -> None:
    """Load a backup into a database."""
    _exit(
        _restore(
            database,
            file,
            engine=engine,
            drop_existing=drop,
            force=force,
            logger=ctx.logger,
        )
    )


@cli.command()
@click.option("--engine", "-e", type=ENGINE, help="Only this engine. Defaults to all of them.")
@click.option("--database", "-d", help="Only backups of this database.")
@pass_context
def backups(ctx: Context, engine: str | None, database: str | None) -> None:
    """List the database backups on this server."""
    _exit(
        _backups(
            engine=engine,
            database=database,
            json_output=ctx.json_output,
            logger=ctx.logger,
        )
    )


@cli.command()
@click.argument("database")
@click.argument("query")
@click.option("--engine", "-e", type=ENGINE, required=True, help="Engine the database is on.")
@click.option(
    "--write",
    is_flag=True,
    help="Allow the statement to change data. Without it the engine runs it in a read-only transaction.",
)
@pass_context
def query(ctx: Context, database: str, query: str, engine: str, write: bool) -> None:
    """Run one statement against a database, read-only unless told otherwise."""
    _exit(_query(database, query, engine=engine, read_only=not write, logger=ctx.logger))


@cli.command()
@click.option("--engine", "-e", type=ENGINE, required=True, help="Engine to connect to.")
@click.option("--database", "-d", help="Database to open.")
@click.option("--username", "-u", help="User to connect as.")
@pass_context
def connect(ctx: Context, engine: str, database: str | None, username: str | None) -> None:
    """Open a session with the engine's own client."""
    _exit(
        _connect(
            engine=engine,
            database=database,
            username=username,
            dry_run=ctx.dry_run,
            logger=ctx.logger,
        )
    )


@cli.command("connection-string")
@click.argument("database")
@click.argument("username")
@click.option("--engine", "-e", type=ENGINE, required=True, help="Engine the database is on.")
@click.option("--password", "-p", help="Password. A placeholder is printed when omitted.")
@click.option("--host", default="localhost", show_default=True, help="Host the application uses.")
@pass_context
def connection_string(
    ctx: Context,
    database: str,
    username: str,
    engine: str,
    password: str | None,
    host: str,
) -> None:
    """Print a connection string for an application to use."""
    _exit(
        _connection_string(
            database,
            username,
            engine=engine,
            password=password,
            host=host,
            logger=ctx.logger,
        )
    )


@cli.command()
@click.option("--engine", "-e", type=ENGINE, required=True, help="Engine the credentials are for.")
@click.option("--user", "-u", help="Administrative user, such as root or postgres.")
@click.option("--password", "-p", help="Administrative password.")
@pass_context
def config(ctx: Context, engine: str, user: str | None, password: str | None) -> None:
    """Store the administrative credentials WASM uses for an engine."""
    _exit(_config(engine=engine, user=user, password=password, logger=ctx.logger))
