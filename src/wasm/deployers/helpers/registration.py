# Copyright (c) 2024-2026 Yago Lopez Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
Writing an app, its site and its service into the store.

Three near-identical "look it up, build the row, update or create" blocks lived
in the deployer. They are persistence, not deployment, so they live next to
each other here and the deployer just calls them.
"""

from __future__ import annotations

from pathlib import Path

from wasm.core.config import APACHE_SITES_AVAILABLE, NGINX_SITES_AVAILABLE, SYSTEMD_DIR
from wasm.core.store import App, Service, Site, WASMStore


class StoreRegistrar:
    """Persists the rows a deployment produces, keyed by their natural keys."""

    def __init__(self, store: WASMStore):
        """
        Args:
            store: The store to write to.
        """
        self.store = store

    def register_app(
        self,
        *,
        domain: str,
        app_type: str,
        source: str,
        branch: str | None,
        port: int | None,
        app_path: Path | None,
        webserver: str,
        ssl_enabled: bool,
        status: str,
        is_static: bool,
        env_vars: dict[str, str],
    ) -> App:
        """
        Create or update the application row.

        Args:
            domain: Natural key of the application.
            app_type: Deployer type identifier.
            source: Git URL or local path the code came from.
            branch: Git branch, when any.
            port: Listening port, or None for static sites.
            app_path: Directory the application lives in.
            webserver: ``nginx`` or ``apache``.
            ssl_enabled: Whether a certificate was requested.
            status: Lifecycle status to record.
            is_static: Whether the app is served straight off disk.
            env_vars: Environment variables recorded with the app.

        Returns:
            The stored application row.
        """
        existing = self.store.get_app(domain)
        app = App(
            id=existing.id if existing else None,
            domain=domain,
            app_type=app_type,
            source=source,
            branch=branch,
            port=port if not is_static else None,
            app_path=str(app_path),
            webserver=webserver,
            ssl_enabled=ssl_enabled,
            status=status,
            is_static=is_static,
            env_vars=env_vars,
        )
        if existing:
            # created_at belongs to the first deployment, not to this one.
            app.created_at = existing.created_at
            return self.store.update_app(app)
        return self.store.create_app(app)

    def register_site(
        self,
        *,
        domain: str,
        webserver: str,
        template: str,
        app_path: Path | None,
        port: int,
        with_ssl: bool,
    ) -> Site:
        """
        Create or update the site row for a domain.

        Args:
            domain: Natural key of the site.
            webserver: ``nginx`` or ``apache``.
            template: Template the config was rendered from. ``static`` means
                the site serves files rather than proxying.
            app_path: Document root for static sites.
            port: Upstream port for proxied sites.
            with_ssl: Whether the written config enables TLS.

        Returns:
            The stored site row.
        """
        sites_dir = NGINX_SITES_AVAILABLE if webserver == "nginx" else APACHE_SITES_AVAILABLE
        is_static = template == "static"
        app = self.store.get_app(domain)
        existing = self.store.get_site(domain)

        site = Site(
            id=existing.id if existing else None,
            app_id=app.id if app else None,
            domain=domain,
            webserver=webserver,
            config_path=str(sites_dir / domain),
            enabled=True,
            is_static=is_static,
            document_root=str(app_path) if is_static else None,
            proxy_port=port if not is_static else None,
            ssl_enabled=with_ssl,
            ssl_certificate=f"/etc/letsencrypt/live/{domain}/fullchain.pem" if with_ssl else None,
            ssl_key=f"/etc/letsencrypt/live/{domain}/privkey.pem" if with_ssl else None,
        )
        if existing:
            return self.store.update_site(site)
        return self.store.create_site(site)

    def register_service(
        self,
        *,
        domain: str,
        name: str,
        command: str,
        working_directory: Path | None,
        environment: dict[str, str],
        port: int,
        user: str,
        group: str,
    ) -> Service:
        """
        Create or update the systemd service row.

        Args:
            domain: Domain the service belongs to, used to link the app row.
            name: Service name, without the ``.service`` suffix.
            command: Resolved ExecStart command line.
            working_directory: Unit WorkingDirectory.
            environment: Environment written into the unit.
            port: Port the service listens on.
            user: Unit User.
            group: Unit Group.

        Returns:
            The stored service row.
        """
        app = self.store.get_app(domain)
        existing = self.store.get_service(name)

        service = Service(
            id=existing.id if existing else None,
            app_id=app.id if app else None,
            name=name,
            unit_file=str(SYSTEMD_DIR / f"{name}.service"),
            working_directory=str(working_directory),
            command=command,
            user=user,
            group=group,
            enabled=True,
            status="inactive",  # start() flips this once systemd confirms
            port=port,
            environment=environment,
        )
        if existing:
            return self.store.update_service(service)
        return self.store.create_service(service)

    def forget(self, *, domain: str, service_name: str | None) -> None:
        """
        Remove every row a deployment created for a domain.

        Args:
            domain: Domain whose app and site rows are removed.
            service_name: Service row to remove, when the deployment made one.
        """
        if service_name and self.store.get_service(service_name):
            self.store.delete_service(service_name)
        if self.store.get_site(domain):
            self.store.delete_site(domain)
        if self.store.get_app(domain):
            self.store.delete_app(domain)
