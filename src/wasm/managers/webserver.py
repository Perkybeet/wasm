# Copyright (c) 2024-2026 Yago Lopez Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
One implementation of "manage virtual hosts", parameterised by web server.

``nginx_manager.py`` and ``apache_manager.py`` used to be the same file with
different strings in it: 57 windows of eight identical lines, and two public
APIs that had drifted apart anyway, so a caller could not treat them as
interchangeable even though that is the whole point of having both. Every fix
had to be applied twice, and in practice never was: the nginx side grew
``create_advanced_site``, the apache side grew ``enable_module``, and only one
of them validated anything.

What actually differs between the two backends is data, not behaviour: a unit
name, a configuration directory, a filename suffix, the syntax-check command,
and whether enabling a site means writing a symlink or calling ``a2ensite``.
That set is :class:`WebServerBackend`. Everything else is
:class:`WebServerManager`, and both concrete managers are thin subclasses of it,
so the contract is the same by construction rather than by convention.

Four rules the old code broke and this one keeps:

- **Every operation goes through the runner.** Argv, timeout, no shell.
- **Every change to disk goes through the filesystem seam.** Writing a vhost,
  linking it into ``sites-enabled`` and deleting it again are the three things
  ``--dry-run`` most needs to be honest about, and none of them is a subprocess.
- **Nothing crosses a boundary as a dict with magic keys.** ``get_status`` and
  ``list_sites`` return records whose field names are part of a type.
- **A domain is validated before it becomes a path.** WASM runs as root, so a
  domain that carries a slash is an arbitrary file write, not a typo.
"""

from __future__ import annotations

import os
import re
import sqlite3
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path
from string import Template
from typing import Any

from jinja2 import Environment, PackageLoader, TemplateNotFound
from jinja2 import TemplateError as JinjaTemplateError

from wasm.core.config import (
    APACHE_SITES_AVAILABLE,
    APACHE_SITES_ENABLED,
    NGINX_SITES_AVAILABLE,
    NGINX_SITES_ENABLED,
)
from wasm.core.exceptions import (
    ApacheError,
    DomainError,
    NginxError,
    SiteError,
    TemplateError,
    ValidationError,
    WASMError,
)
from wasm.core.fs import FileSystem
from wasm.core.runner import CommandRunner
from wasm.core.store import Site, WASMStore, WebServer, get_store
from wasm.managers.base_manager import BaseManager, MappingRecord
from wasm.validators.domain import is_valid_domain
from wasm.validators.names import resolve_within, validate_filename

#: A reload or a syntax check is a local operation; anything slower than this
#: means the web server is wedged and the caller needs to know now.
_CONTROL_TIMEOUT = 30

#: Port a proxy site targets when the caller does not say otherwise.
DEFAULT_PROXY_PORT = 3000

#: Mode of a virtual host file. World readable, like the rest of the web server
#: configuration; the secrets live in the environment file, not here.
_CONFIG_MODE = 0o644


def _as_port(value: Any) -> int | None:
    """
    Read a port out of a template context.

    The context is a free-form mapping supplied by callers, so a value that is
    not a port is possible. It must not abort a write that has already happened;
    the store row simply records no port.

    Args:
        value: Candidate port from the template context.

    Returns:
        The port, or None when the value is not one.
    """
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


@dataclass
class WebServerStatus(MappingRecord):
    """
    What a web server is doing right now.

    Attributes:
        name: Backend name, ``nginx`` or ``apache``.
        installed: Whether the binary is on PATH.
        version: Reported version, or None when it could not be parsed.
        active: Whether the unit is running.
        enabled: Whether the unit starts at boot.
    """

    name: str
    installed: bool
    version: str | None
    active: bool
    enabled: bool


@dataclass
class SiteInfo(MappingRecord):
    """
    One virtual host as it exists on disk.

    Attributes:
        domain: Domain the configuration serves.
        enabled: Whether the site is enabled in the web server.
        config_path: Absolute path of the configuration file.
        webserver: Backend that owns the file.
    """

    domain: str
    enabled: bool
    config_path: str
    webserver: str


@dataclass(frozen=True)
class WebServerBackend:
    """
    Everything that differs between one web server and another.

    Attributes:
        name: Short name used in records and messages.
        binary: Executable that must exist for the backend to be installed.
        service: systemd unit to reload, restart and query.
        version_argv: Command that prints the version.
        version_pattern: Pattern whose first group is the version.
        config_test_argv: Command that checks the configuration syntax.
        validation_argv: Command that checks the syntax of an arbitrary main
            configuration file; the path of that file is appended.
        validation_wrapper: :class:`string.Template` body of the throwaway
            main configuration that wraps a single virtual host so
            ``validation_argv`` can check it. ``$snippet`` is the staged
            virtual host file and ``$server_root`` the directory holding the
            backend's own configuration.
        sites_available: Directory holding every virtual host file.
        sites_enabled: Directory holding the enabled ones.
        config_suffix: Suffix appended to the domain to name the file.
        template_package: Package directory holding the Jinja templates.
        default_site_names: Distribution-provided sites that WASM does not own.
        enable_site_program: Program that enables a site, or None when enabling
            means writing a symlink into ``sites_enabled``.
        disable_site_program: Counterpart of ``enable_site_program``.
        module_enable_program: Program that enables a module, or None when the
            backend has no module system.
        module_disable_program: Counterpart of ``module_enable_program``.
        required_modules: Modules that must be enabled before a site works.
        error: Exception type raised for failures of this backend, so existing
            callers keep catching what they already catch.
    """

    name: str
    binary: str
    service: str
    version_argv: tuple[str, ...]
    version_pattern: re.Pattern[str]
    config_test_argv: tuple[str, ...]
    validation_argv: tuple[str, ...]
    validation_wrapper: str
    sites_available: Path
    sites_enabled: Path
    config_suffix: str
    template_package: str
    default_site_names: frozenset[str]
    error: type[SiteError]
    enable_site_program: str | None = None
    disable_site_program: str | None = None
    module_enable_program: str | None = None
    module_disable_program: str | None = None
    required_modules: tuple[str, ...] = ()
    webserver_record: str = WebServer.NGINX.value


#: Main configuration wrapping one staged virtual host for ``nginx -t -c``.
#: A server block is only valid inside http{}, and a main configuration is
#: only valid with an events{} block, so the wrapper supplies the minimal
#: skeleton and nothing else: including the live nginx.conf instead would make
#: the new snippet collide with the site it is about to replace.
_NGINX_VALIDATION_WRAPPER = """\
# Written by WASM to check one virtual host without touching the live
# configuration. Deleted as soon as nginx -t has answered.
events {
}
http {
    include $snippet;
}
"""

#: Main configuration wrapping one staged virtual host for apache. The live
#: module set is loaded first: a vhost using ProxyPass is only valid with
#: mod_proxy present, exactly as it will be at the next reload.
_APACHE_VALIDATION_WRAPPER = """\
# Written by WASM to check one virtual host without touching the live
# configuration. Deleted as soon as the syntax check has answered.
ServerRoot "$server_root"
IncludeOptional $server_root/mods-enabled/*.load
IncludeOptional $server_root/mods-enabled/*.conf
Include $snippet
"""

NGINX_BACKEND = WebServerBackend(
    name="nginx",
    binary="nginx",
    service="nginx",
    version_argv=("nginx", "-v"),
    version_pattern=re.compile(r"nginx/(\S+)"),
    config_test_argv=("nginx", "-t"),
    validation_argv=("nginx", "-t", "-c"),
    validation_wrapper=_NGINX_VALIDATION_WRAPPER,
    sites_available=NGINX_SITES_AVAILABLE,
    sites_enabled=NGINX_SITES_ENABLED,
    config_suffix="",
    template_package="templates/nginx",
    default_site_names=frozenset({"default"}),
    error=NginxError,
    webserver_record=WebServer.NGINX.value,
)

APACHE_BACKEND = WebServerBackend(
    name="apache",
    binary="apache2",
    service="apache2",
    version_argv=("apache2", "-v"),
    version_pattern=re.compile(r"Apache/(\S+)"),
    config_test_argv=("apache2ctl", "configtest"),
    # Not ``configtest``: apache2ctl hard-codes that word to ``-t`` on the live
    # configuration and drops any further arguments. Bare flags fall through to
    # the passthrough branch, which still sources /etc/apache2/envvars, so the
    # ${APACHE_*} variables the module files reference keep resolving.
    validation_argv=("apache2ctl", "-t", "-f"),
    validation_wrapper=_APACHE_VALIDATION_WRAPPER,
    sites_available=APACHE_SITES_AVAILABLE,
    sites_enabled=APACHE_SITES_ENABLED,
    config_suffix=".conf",
    template_package="templates/apache",
    default_site_names=frozenset({"000-default", "default-ssl"}),
    error=ApacheError,
    enable_site_program="a2ensite",
    disable_site_program="a2dissite",
    module_enable_program="a2enmod",
    module_disable_program="a2dismod",
    required_modules=("proxy", "proxy_http", "proxy_wstunnel", "rewrite", "headers"),
    webserver_record=WebServer.APACHE.value,
)


class WebServerManager(BaseManager):
    """
    Manage virtual hosts for one web server backend.

    The public methods are the contract both backends honour: same names, same
    signatures, same return types. Where a backend cannot do something at all -
    nginx has no runtime module system - the method still exists and reports
    that honestly instead of being absent from one of the two classes.
    """

    def __init__(
        self,
        backend: WebServerBackend,
        verbose: bool = False,
        runner: CommandRunner | None = None,
        fs: FileSystem | None = None,
    ) -> None:
        """
        Initialize the manager.

        Args:
            backend: The web server this instance drives.
            verbose: Enable verbose logging.
            runner: Command runner to execute with. Defaults to the process-wide
                one.
            fs: Filesystem to write configurations and symlinks through.
                Defaults to the process-wide one.
        """
        super().__init__(verbose=verbose, runner=runner, fs=fs)
        self.backend = backend

    # -- Wiring ------------------------------------------------------------

    @cached_property
    def store(self) -> WASMStore:
        """
        The persistence layer, opened on first use.

        Opening it lazily keeps a read-only operation such as rendering a
        template from touching SQLite at all.

        Returns:
            The store singleton.
        """
        return get_store()

    @cached_property
    def jinja_env(self) -> Environment:
        """
        The template environment for this backend.

        Returns:
            A Jinja environment loading from the backend's template directory.

        Raises:
            TemplateError: When the templates cannot be located, which means the
                package was installed without its data files.
        """
        try:
            return Environment(
                loader=PackageLoader("wasm", self.backend.template_package),
                trim_blocks=True,
                lstrip_blocks=True,
                autoescape=False,  # noqa: S701 - web server config, not markup
            )
        except (ValueError, ImportError) as exc:
            raise TemplateError(
                f"Could not load {self.backend.name} templates",
                details=(
                    f"Package directory {self.backend.template_package} is missing. "
                    "Reinstall the wasm package."
                ),
            ) from exc

    @property
    def sites_available(self) -> Path:
        """
        Directory holding every virtual host file.

        Returns:
            The backend's sites-available directory.
        """
        return self.backend.sites_available

    @property
    def sites_enabled(self) -> Path:
        """
        Directory holding the enabled virtual host files.

        Returns:
            The backend's sites-enabled directory.
        """
        return self.backend.sites_enabled

    # -- Service state -----------------------------------------------------

    def is_installed(self) -> bool:
        """
        Check whether the web server is installed.

        Returns:
            True when the backend binary is on PATH.
        """
        return self.runner.exists(self.backend.binary)

    def get_version(self) -> str | None:
        """
        Get the web server version.

        Returns:
            The version string, or None when it cannot be determined.
        """
        result = self._run(list(self.backend.version_argv), timeout=_CONTROL_TIMEOUT)
        # nginx prints its banner on stderr and apache on stdout; reading both
        # removes a per-backend special case that used to be wrong for one of
        # them after every refactor.
        match = self.backend.version_pattern.search(f"{result.stdout}\n{result.stderr}")
        return match.group(1) if match else None

    def is_running(self) -> bool:
        """
        Check whether the web server is currently running.

        Returns:
            True when the unit reports itself active.
        """
        result = self._run(
            ["systemctl", "is-active", self.backend.service], timeout=_CONTROL_TIMEOUT
        )
        return result.stdout.strip() == "active"

    def is_boot_enabled(self) -> bool:
        """
        Check whether the web server starts at boot.

        Returns:
            True when the unit is enabled.
        """
        result = self._run(
            ["systemctl", "is-enabled", self.backend.service], timeout=_CONTROL_TIMEOUT
        )
        return result.stdout.strip() == "enabled"

    def get_status(self) -> WebServerStatus:
        """
        Get the web server status.

        Returns:
            A record describing installation, version and unit state.
        """
        return WebServerStatus(
            name=self.backend.name,
            installed=self.is_installed(),
            version=self.get_version(),
            active=self.is_running(),
            enabled=self.is_boot_enabled(),
        )

    def test_config(self) -> bool:
        """
        Test the web server configuration syntax.

        Returns:
            True when the configuration is valid.
        """
        result = self._run(list(self.backend.config_test_argv), timeout=_CONTROL_TIMEOUT)
        # apache2ctl exits non-zero on warnings it then describes as "Syntax OK".
        return result.success or "Syntax OK" in f"{result.stdout}\n{result.stderr}"

    def reload(self) -> bool:
        """
        Reload the web server configuration.

        The syntax check runs first: reloading a broken configuration is how a
        deploy takes every other site on the box down with it.

        Returns:
            True when the reload succeeded.
        """
        if not self.test_config():
            self.logger.error(f"{self.backend.name} configuration test failed")
            return False

        return self._run(
            ["systemctl", "reload", self.backend.service], timeout=_CONTROL_TIMEOUT
        ).success

    def restart(self) -> bool:
        """
        Restart the web server.

        Returns:
            True when the restart succeeded.
        """
        return self._run(
            ["systemctl", "restart", self.backend.service], timeout=_CONTROL_TIMEOUT
        ).success

    def enable_module(self, module: str) -> bool:
        """
        Enable a web server module.

        Args:
            module: Module name.

        Returns:
            True when the module was enabled. False when the backend has no
            runtime module system, as with nginx, where modules are compiled in.
        """
        program = self.backend.module_enable_program
        if program is None:
            self.logger.debug(f"{self.backend.name} has no runtime modules; ignoring {module}")
            return False
        return self._run([program, module], timeout=_CONTROL_TIMEOUT).success

    def disable_module(self, module: str) -> bool:
        """
        Disable a web server module.

        Args:
            module: Module name.

        Returns:
            True when the module was disabled. False when the backend has no
            runtime module system.
        """
        program = self.backend.module_disable_program
        if program is None:
            self.logger.debug(f"{self.backend.name} has no runtime modules; ignoring {module}")
            return False
        return self._run([program, module], timeout=_CONTROL_TIMEOUT).success

    # -- Paths -------------------------------------------------------------

    def config_path(self, domain: str) -> Path:
        """
        Resolve the configuration file a domain maps to.

        This is the only place a domain becomes a path, and it is where the
        domain is checked. WASM writes these files as root, so a name carrying a
        slash, a newline or a ``..`` segment is an arbitrary file write; the
        allowlist rejects it before it reaches the filesystem, and
        :func:`resolve_within` catches the case where the name is clean but a
        symlink in the directory is not.

        Args:
            domain: Domain name.

        Returns:
            The absolute path of the virtual host file.

        Raises:
            DomainError: When the domain is not a valid domain name.
            ValidationError: When the resulting file name is not a single, inert
                path component.
            SecurityError: When the path escapes the configuration directory.
        """
        candidate = domain.strip().lower()
        valid, reason = is_valid_domain(candidate)
        if not valid:
            raise DomainError(
                f"Invalid domain: {domain!r}",
                details=(
                    f"{reason}. A domain becomes the name of a file in "
                    f"{self.backend.sites_available}, so only letters, digits, "
                    "hyphens and dots are accepted."
                ),
            )

        filename = validate_filename(f"{candidate}{self.backend.config_suffix}")
        return resolve_within(self.backend.sites_available, filename)

    def _link_path(self, domain: str) -> Path:
        """
        Resolve the enabled-site path a domain maps to.

        Unlike :meth:`config_path` this does not go through
        :func:`resolve_within`: the entry in ``sites-enabled`` is a symlink whose
        whole purpose is to point at another directory, so resolving it and
        demanding that it stay inside would reject every correctly enabled site.
        The safety comes from the name, which :meth:`config_path` has already
        validated as a single inert path component.

        Args:
            domain: Domain name.

        Returns:
            The absolute path inside the enabled-sites directory.

        Raises:
            DomainError: When the domain is not a valid domain name.
        """
        return self.backend.sites_enabled / self.config_path(domain).name

    def site_exists(self, domain: str) -> bool:
        """
        Check whether a site configuration exists.

        Args:
            domain: Domain name.

        Returns:
            True when the configuration file is present.
        """
        return self.config_path(domain).exists()

    def site_enabled(self, domain: str) -> bool:
        """
        Check whether a site is enabled.

        Args:
            domain: Domain name.

        Returns:
            True when the site is enabled.
        """
        link = self._link_path(domain)
        # A dangling symlink is still an enabled site as far as the web server
        # is concerned, and Path.exists() follows the link, so it would say no.
        return link.exists() or link.is_symlink()

    def list_sites(self) -> list[SiteInfo]:
        """
        List the sites this backend serves.

        Returns:
            One record per virtual host file WASM considers its own, in a stable
            alphabetical order.
        """
        sites: list[SiteInfo] = []
        if not self.sites_available.exists():
            return sites

        suffix = self.backend.config_suffix
        for config_file in sorted(self.sites_available.iterdir()):
            if not config_file.is_file():
                continue
            if suffix and config_file.suffix != suffix:
                continue
            domain = config_file.name[: -len(suffix)] if suffix else config_file.name
            if domain in self.backend.default_site_names:
                continue
            link = self.sites_enabled / config_file.name
            sites.append(
                SiteInfo(
                    domain=domain,
                    enabled=link.exists() or link.is_symlink(),
                    config_path=str(config_file),
                    webserver=self.backend.name,
                )
            )
        return sites

    # -- Rendering ---------------------------------------------------------

    def build_context(
        self, domain: str, context: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        """
        Merge caller-supplied template variables over the defaults.

        Args:
            domain: Domain name.
            context: Caller overrides.

        Returns:
            The full template context.
        """
        ctx: dict[str, Any] = {
            "domain": domain,
            "port": DEFAULT_PROXY_PORT,
            "app_path": f"/var/www/apps/{domain}",
            "ssl": False,
            "ssl_certificate": f"/etc/letsencrypt/live/{domain}/fullchain.pem",
            "ssl_certificate_key": f"/etc/letsencrypt/live/{domain}/privkey.pem",
        }
        if context:
            ctx.update(context)
        return ctx

    def render_config(
        self,
        domain: str,
        template: str = "proxy",
        context: Mapping[str, Any] | None = None,
    ) -> str:
        """
        Render a virtual host configuration without writing anything.

        Rendering is separated from writing so the output can be asserted on in
        a test, which is what makes the templates reviewable at all.

        Args:
            domain: Domain name.
            template: Template name, without the ``.conf.j2`` suffix.
            context: Template variables, merged over the defaults.

        Returns:
            The rendered configuration.

        Raises:
            DomainError: When the domain is not a valid domain name.
            TemplateError: When the template is missing or fails to render.
        """
        # Validating here as well as in config_path keeps a caller that only
        # renders from smuggling a newline into a server_name directive.
        self.config_path(domain)
        ctx = self.build_context(domain, context)

        try:
            template_obj = self.jinja_env.get_template(f"{template}.conf.j2")
            return template_obj.render(**ctx)
        except TemplateNotFound as exc:
            raise TemplateError(
                f"Template not found: {template}.conf.j2",
                details=(
                    f"Available {self.backend.name} templates: "
                    f"{', '.join(self.list_templates()) or 'none'}."
                ),
            ) from exc
        except JinjaTemplateError as exc:
            raise TemplateError(
                f"Template rendering failed: {exc}",
                details=f"Template {template}.conf.j2 for {domain}.",
            ) from exc

    def list_templates(self) -> list[str]:
        """
        List the template names this backend offers.

        Returns:
            Template names without the ``.conf.j2`` suffix, sorted.
        """
        return sorted(
            name.removesuffix(".conf.j2")
            for name in self.jinja_env.list_templates()
            if name.endswith(".conf.j2")
        )

    # -- Site lifecycle ----------------------------------------------------

    def create_site(
        self,
        domain: str,
        template: str = "proxy",
        context: Mapping[str, Any] | None = None,
    ) -> bool:
        """
        Create a virtual host configuration.

        Args:
            domain: Domain name.
            template: Template name, without the ``.conf.j2`` suffix.
            context: Template variables, merged over the defaults.

        Returns:
            True when the configuration was written.

        Raises:
            NginxError: When an nginx site already exists or cannot be written.
            ApacheError: When an apache site already exists or cannot be
                written.
            DomainError: When the domain is not a valid domain name.
            TemplateError: When the template is missing or fails to render.
        """
        if self.site_exists(domain):
            raise self.backend.error(
                f"Site already exists: {domain}",
                details=f"Use update_site() to change {self.config_path(domain)}.",
            )

        for module in self.backend.required_modules:
            self.enable_module(module)

        return self._write_site(domain, template, context)

    def update_site(
        self,
        domain: str,
        template: str = "proxy",
        context: Mapping[str, Any] | None = None,
    ) -> bool:
        """
        Rewrite an existing virtual host configuration in place.

        The file is replaced atomically and the symlink is left alone, so a site
        is never briefly missing from the web server. The previous implementation
        deleted the site and recreated it, which also dropped its store record
        and its enabled state on the way through.

        Args:
            domain: Domain name.
            template: Template name, without the ``.conf.j2`` suffix.
            context: Template variables, merged over the defaults.

        Returns:
            True when the configuration was rewritten.

        Raises:
            NginxError: When the nginx site does not exist.
            ApacheError: When the apache site does not exist.
            DomainError: When the domain is not a valid domain name.
            TemplateError: When the template is missing or fails to render.
        """
        if not self.site_exists(domain):
            raise self.backend.error(
                f"Site does not exist: {domain}",
                details="Create it first with create_site().",
            )

        return self._write_site(domain, template, context)

    def _write_site(
        self,
        domain: str,
        template: str,
        context: Mapping[str, Any] | None,
    ) -> bool:
        """
        Render a configuration and put it on disk atomically.

        Args:
            domain: Domain name.
            template: Template name.
            context: Template variables.

        Returns:
            True when the file was written.

        Raises:
            NginxError: When the nginx configuration cannot be written.
            ApacheError: When the apache configuration cannot be written.
        """
        config_path = self.config_path(domain)
        ctx = self.build_context(domain, context)
        content = self.render_config(domain, template, ctx)

        try:
            # The seam writes through a sibling and renames, so a reload racing
            # this call sees either the old file or the new one, never half of
            # one - and a rehearsal writes neither, including the sibling.
            self.fs.write_text(config_path, content, mode=_CONFIG_MODE)
        except OSError as exc:
            raise self.backend.error(
                f"Failed to write configuration: {config_path}",
                details=str(exc),
            ) from exc

        self._record_site(domain, config_path, ctx)
        self.logger.debug(f"Wrote site configuration: {config_path}")
        return True

    def _record_site(self, domain: str, config_path: Path, ctx: Mapping[str, Any]) -> None:
        """
        Register or refresh the site in the store.

        The store is a cache of what is on disk, so a failure to update it is
        logged and swallowed: the configuration file is the source of truth and
        it has already been written.

        Args:
            domain: Domain name.
            config_path: Path of the configuration file.
            ctx: The template context the file was rendered from.
        """
        ssl_enabled = bool(ctx.get("ssl", False))
        proxy_port = _as_port(ctx.get("port"))
        certificate = (
            str(ctx["ssl_certificate"]) if ssl_enabled and ctx.get("ssl_certificate") else None
        )
        key = (
            str(ctx["ssl_certificate_key"])
            if ssl_enabled and ctx.get("ssl_certificate_key")
            else None
        )

        try:
            existing = self.store.get_site(domain)
            if existing is not None:
                existing.webserver = self.backend.webserver_record
                existing.config_path = str(config_path)
                existing.proxy_port = proxy_port
                existing.ssl_enabled = ssl_enabled
                existing.ssl_certificate = certificate
                existing.ssl_key = key
                self.store.update_site(existing)
                return

            self.store.create_site(
                Site(
                    domain=domain,
                    webserver=self.backend.webserver_record,
                    config_path=str(config_path),
                    proxy_port=proxy_port,
                    ssl_enabled=ssl_enabled,
                    ssl_certificate=certificate,
                    ssl_key=key,
                    enabled=self.site_enabled(domain),
                )
            )
        except (WASMError, sqlite3.Error) as exc:
            self.logger.debug(f"Could not register site in store: {exc}")

    def enable_site(self, domain: str) -> bool:
        """
        Enable a site.

        Args:
            domain: Domain name.

        Returns:
            True when the site is enabled, including when it already was.

        Raises:
            NginxError: When the nginx site does not exist or cannot be enabled.
            ApacheError: When the apache site does not exist or cannot be
                enabled.
        """
        if not self.site_exists(domain):
            raise self.backend.error(
                f"Site does not exist: {domain}",
                details=f"Expected {self.config_path(domain)}.",
            )

        if self.site_enabled(domain):
            self.logger.debug(f"Site already enabled: {domain}")
            return True

        link = self._link_path(domain)
        program = self.backend.enable_site_program
        if program is None:
            try:
                self.fs.make_dir(link.parent)
                self.fs.symlink(self.config_path(domain), link)
            except OSError as exc:
                raise self.backend.error(
                    f"Failed to enable site: {domain}",
                    details=str(exc),
                ) from exc
        else:
            result = self._run([program, link.name], timeout=_CONTROL_TIMEOUT)
            if not result.success:
                raise self.backend.error(
                    f"Failed to enable site: {domain}",
                    details=result.stderr or result.stdout,
                )

        self._record_enabled(domain, True)
        self.logger.debug(f"Enabled site: {domain}")
        return True

    def disable_site(self, domain: str) -> bool:
        """
        Disable a site.

        Args:
            domain: Domain name.

        Returns:
            True when the site is disabled, including when it already was.

        Raises:
            NginxError: When the nginx site cannot be disabled.
            ApacheError: When the apache site cannot be disabled.
        """
        if not self.site_enabled(domain):
            self.logger.debug(f"Site already disabled: {domain}")
            return True

        link = self._link_path(domain)
        program = self.backend.disable_site_program
        if program is None:
            try:
                self.fs.remove(link, missing_ok=True)
            except OSError as exc:
                raise self.backend.error(
                    f"Failed to disable site: {domain}",
                    details=str(exc),
                ) from exc
        else:
            result = self._run([program, link.name], timeout=_CONTROL_TIMEOUT)
            if not result.success:
                raise self.backend.error(
                    f"Failed to disable site: {domain}",
                    details=result.stderr or result.stdout,
                )

        self._record_enabled(domain, False)
        self.logger.debug(f"Disabled site: {domain}")
        return True

    def _record_enabled(self, domain: str, enabled: bool) -> None:
        """
        Record the enabled state of a site in the store.

        Args:
            domain: Domain name.
            enabled: New state.
        """
        try:
            site = self.store.get_site(domain)
            if site is not None:
                site.enabled = enabled
                self.store.update_site(site)
        except (WASMError, sqlite3.Error) as exc:
            self.logger.debug(f"Could not update site in store: {exc}")

    def delete_site(self, domain: str) -> bool:
        """
        Delete a site configuration.

        Args:
            domain: Domain name.

        Returns:
            True when nothing is left on disk for this domain.

        Raises:
            NginxError: When the nginx configuration cannot be removed.
            ApacheError: When the apache configuration cannot be removed.
        """
        if self.site_enabled(domain):
            self.disable_site(domain)

        config_path = self.config_path(domain)
        try:
            self.fs.remove(config_path, missing_ok=True)
        except OSError as exc:
            raise self.backend.error(
                f"Failed to delete site: {domain}",
                details=str(exc),
            ) from exc

        try:
            self.store.delete_site(domain)
        except (WASMError, sqlite3.Error) as exc:
            self.logger.debug(f"Could not remove site from store: {exc}")

        self.logger.debug(f"Deleted site: {domain}")
        return True

    def get_site_config(self, domain: str) -> str | None:
        """
        Read a site configuration.

        Args:
            domain: Domain name.

        Returns:
            The file content, or None when the site does not exist or cannot be
            read.
        """
        config_path = self.config_path(domain)
        try:
            return config_path.read_text()
        except OSError as exc:
            self.logger.debug(f"Could not read {config_path}: {exc}")
            return None

    # -- Validating a configuration without installing it --------------------

    def validate_config_text(self, config_text: str, *, domain: str) -> None:
        """
        Ask the web server whether it would accept a configuration snippet.

        The snippet is staged into a throwaway directory together with a
        minimal main configuration that includes it, and the backend's own
        syntax checker runs against that wrapper. The live configuration is
        never touched: a broken snippet used to be written first and checked
        never, which took the site down at the next reload.

        Args:
            config_text: The virtual host configuration to check.
            domain: Domain the configuration is meant for. Validated the same
                way as everywhere else before it names a staged file.

        Returns:
            None. Returning at all means the server accepted the snippet.

        Raises:
            ValidationError: When the server rejects the snippet. ``details``
                carries the server's own output verbatim.
            NginxError: When the nginx snippet cannot be staged.
            ApacheError: When the apache snippet cannot be staged.
            DomainError: When the domain is not a valid domain name.
        """
        snippet_name = self.config_path(domain).name
        # A random directory name for the same reason the filesystem seam uses
        # a random sibling: a predictable path in a world-writable directory is
        # a symlink an attacker can plant, and this code runs as root.
        staging = Path(tempfile.gettempdir()) / f"wasm-validate-{os.urandom(6).hex()}"
        snippet = staging / snippet_name
        wrapper = staging / "wasm-validate.conf"
        wrapper_text = Template(self.backend.validation_wrapper).substitute(
            snippet=str(snippet),
            server_root=str(self.backend.sites_available.parent),
        )

        try:
            try:
                self.fs.write_text(snippet, config_text, mode=_CONFIG_MODE)
                self.fs.write_text(wrapper, wrapper_text, mode=_CONFIG_MODE)
            except OSError as exc:
                raise self.backend.error(
                    f"Could not stage the configuration of {domain} for validation",
                    details=str(exc),
                ) from exc
            result = self._run(
                [*self.backend.validation_argv, str(wrapper)], timeout=_CONTROL_TIMEOUT
            )
        finally:
            if staging.exists():
                self.fs.remove_tree(staging)

        # The same tolerance test_config() needs: apache2ctl exits non-zero on
        # warnings it then describes as "Syntax OK".
        if result.success or "Syntax OK" in f"{result.stdout}\n{result.stderr}":
            return

        output = "\n".join(stream for stream in (result.stderr, result.stdout) if stream.strip())
        raise ValidationError(
            f"{self.backend.name} rejected the configuration for {domain}",
            details=output,
        )

    def replace_site_config(self, domain: str, config_text: str) -> Path:
        """
        Validate a hand-edited configuration and install it atomically.

        Args:
            domain: Domain of the site.
            config_text: The new configuration, written verbatim.

        Returns:
            The path of the configuration file that was replaced.

        Raises:
            NginxError: When the nginx site does not exist or cannot be
                written.
            ApacheError: When the apache site does not exist or cannot be
                written.
            ValidationError: When the server rejects the configuration. The
                file on disk is left exactly as it was.
            DomainError: When the domain is not a valid domain name.
        """
        if not self.site_exists(domain):
            raise self.backend.error(
                f"Site does not exist: {domain}",
                details="Create it first with create_site().",
            )

        self.validate_config_text(config_text, domain=domain)

        config_path = self.config_path(domain)
        try:
            # The seam writes through a sibling and renames, so a reload racing
            # this call sees either the old configuration or the new one.
            self.fs.write_text(config_path, config_text, mode=_CONFIG_MODE)
        except OSError as exc:
            raise self.backend.error(
                f"Failed to write configuration: {config_path}",
                details=str(exc),
            ) from exc

        self.logger.debug(f"Replaced site configuration: {config_path}")
        return config_path
