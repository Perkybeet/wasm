# Copyright (c) 2024-2026 Yago Lopez Prado
# SPDX-License-Identifier: AGPL-3.0-or-later

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

import logging
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
    CertificateError,
    DomainError,
    NginxError,
    SiteError,
    TemplateError,
    ValidationError,
    WASMError,
)
from wasm.core.fs import FileSystem
from wasm.core.runner import CommandRunner
from wasm.core.store import DomainKind, Site, WASMStore, WebServer, get_store
from wasm.managers.base_manager import BaseManager, MappingRecord
from wasm.managers.cert_manager import CertManager
from wasm.validators.domain import is_valid_domain, should_include_www
from wasm.validators.names import resolve_within, validate_filename

#: Module logger for the orchestration functions below. They are not manager
#: methods, so they have no ``self.logger``; this is the same standard-library
#: logger the job system uses for the same reason.
_logger = logging.getLogger(__name__)

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
        server_name_pattern: Matches a directive naming what a virtual host
            answers on; its first group is the space-separated names.
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
    server_name_pattern: re.Pattern[str] = re.compile(r"^\s*server_name\s+([^;]*);", re.MULTILINE)


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
    server_name_pattern=re.compile(r"^\s*Server(?:Name|Alias)\s+(.+?)\s*$", re.MULTILINE),
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
        return self.config_errors() is None

    def config_errors(self) -> str | None:
        """
        Test the web server configuration and say what the server objected to.

        Returns:
            None when the configuration is valid; otherwise the server's own
            output, verbatim, which is what an operator fixing it needs.
        """
        result = self._run(list(self.backend.config_test_argv), timeout=_CONTROL_TIMEOUT)
        # apache2ctl exits non-zero on warnings it then describes as "Syntax OK".
        if result.success or "Syntax OK" in f"{result.stdout}\n{result.stderr}":
            return None
        output = "\n".join(stream for stream in (result.stderr, result.stdout) if stream.strip())
        return output or (
            f"{' '.join(self.backend.config_test_argv)} exited with status {result.exit_code}"
        )

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

        # ``server_names`` is what nginx is given verbatim; apache needs the
        # same list split into its ServerName and its ServerAliases, so both
        # are derived here from the one value rather than passed separately
        # and left to disagree.
        served = str(ctx.get("server_names") or domain).split()
        ctx["server_names"] = " ".join(served)
        ctx["server_aliases"] = [name for name in served if name != domain]
        # A name that is served and redirected at once would be two server
        # blocks claiming it; serving it is what the operator can see working.
        ctx["redirect_domains"] = [
            name for name in ctx.get("redirect_domains") or [] if name not in served
        ]
        return ctx

    def _check_names(self, ctx: Mapping[str, Any]) -> None:
        """
        Refuse a context naming anything that is not a domain.

        Every name lands in a ``server_name``, ``ServerAlias`` or redirect
        target directive of a file written as root, where a ``;`` or a newline
        ends the directive and starts one of the caller's choosing. The primary
        has always been checked by :meth:`config_path`; this checks the rest.

        Args:
            ctx: The full template context.

        Raises:
            DomainError: When a served or redirected name is not a domain.
        """
        # build_context already rejoined server_names on single spaces, so a
        # newline in it can no longer end a directive; what is left to refuse
        # is a token that is not a domain, such as ``evil.com;``.
        for name in [*ctx["server_names"].split(), *ctx["redirect_domains"]]:
            valid, reason = is_valid_domain(name)
            if not valid or name != name.strip():
                raise DomainError(
                    f"Invalid domain in the configuration of {ctx['domain']}: {name!r}",
                    details=f"{reason or 'Surrounding whitespace'}. Every name a site "
                    "answers on becomes a directive in its configuration file.",
                )

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
        self._check_names(ctx)

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
            DomainConflictError: When the domain is another application's
                alias or redirect.
            TemplateError: When the template is missing or fails to render.
        """
        if self.site_exists(domain):
            raise self.backend.error(
                f"Site already exists: {domain}",
                details=f"Use update_site() to change {self.config_path(domain)}.",
            )

        # A name that is another application's alias or redirect is already
        # in that application's server blocks; a second site claiming it is a
        # conflict nginx settles by file order, with a warning nobody reads.
        try:
            owner = self.store.domain_owner(domain.strip().lower())
        except (WASMError, sqlite3.Error) as exc:
            # No store to ask - a rehearsal on a machine that has none yet.
            self.logger.debug(f"Could not check who owns {domain}: {exc}")
            owner = None
        if owner is not None and owner[1] != DomainKind.PRIMARY.value:
            raise WASMStore.conflict(domain.strip().lower(), owner)

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
        names = self._application_names(domain.strip().lower())
        ctx = self.build_context(domain, {**(context or {}), **names})
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

    def _application_names(self, domain: str) -> dict[str, Any]:
        """
        Read the names an application's site answers on, from the store.

        This is the one place those names reach a configuration file. The
        deployer, the certificate step and the site endpoints all rewrite the
        same vhost; if each brought its own list, whichever ran last would
        decide which aliases survive, and a redeploy that knew nothing about
        domains would quietly drop them all.

        Args:
            domain: Domain of the site being written.

        Returns:
            ``server_names`` and ``redirect_domains`` for the template when the
            domain is an application's, overriding whatever the caller passed;
            empty for a site that is no application's, which keeps the names
            it was given (``wasm site create --www``), and for a store that
            cannot be read, which is reported.
        """
        try:
            records = self.store.list_domains(domain)
        except (WASMError, sqlite3.Error) as exc:
            # A rehearsal on a machine with no database yet has no rows to
            # read; anything worse has already failed whoever called this.
            self.logger.warning(f"Could not read the domains of {domain}: {exc}")
            return {}
        if not records:
            return {}
        served = [r.domain for r in records if r.kind != DomainKind.REDIRECT.value]
        redirects = [r.domain for r in records if r.kind == DomainKind.REDIRECT.value]
        return {"server_names": " ".join(served), "redirect_domains": redirects}

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

    def served_names(self, domain: str) -> list[str]:
        """
        Read which names a site's configuration answers on.

        The file is the only record of what a 1.x deploy served: ``include_www``
        was never stored anywhere else.

        Args:
            domain: Domain of the site.

        Returns:
            Every domain named by a ``server_name`` (nginx) or ``ServerName``
            / ``ServerAlias`` (apache) directive, lowercased, in file order,
            once each. Catch-alls, wildcards and anything else that is not a
            domain are left out. Empty when the site does not exist.
        """
        if not self.site_exists(domain):
            return []
        text = self.get_site_config(domain) or ""
        names: list[str] = []
        for match in self.backend.server_name_pattern.finditer(text):
            for token in match.group(1).split():
                name = token.lower()
                if name not in names and is_valid_domain(name)[0]:
                    names.append(name)
        return names

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

    def test_config_text(self, config_text: str, *, domain: str) -> tuple[bool, str]:
        """
        Ask the web server whether it would accept a configuration snippet.

        The snippet is staged into a throwaway directory through the
        filesystem seam, together with a minimal main configuration that
        includes it, and the backend's own syntax checker runs against that
        wrapper. The live configuration is never touched and nothing staged
        outlives this call, whichever way it answers - a "try before you
        save" caller and :meth:`validate_config_text` share this one
        implementation of that instead of each staging its own copy.

        Args:
            config_text: The virtual host configuration to check.
            domain: Domain the configuration is meant for. Validated the same
                way as everywhere else before it names a staged file.

        Returns:
            Whether the server accepted the snippet, and its own output
            verbatim. The output is not empty on a pass either: nginx and
            apache2ctl both print a confirmation ("syntax is ok" / "Syntax
            OK") even when there is nothing wrong.

        Raises:
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

        output = "\n".join(stream for stream in (result.stderr, result.stdout) if stream.strip())
        # The same tolerance test_config() needs: apache2ctl exits non-zero on
        # warnings it then describes as "Syntax OK".
        ok = result.success or "Syntax OK" in f"{result.stdout}\n{result.stderr}"
        return ok, output

    def validate_config_text(self, config_text: str, *, domain: str) -> None:
        """
        Ask the web server whether it would accept a configuration snippet.

        Raises rather than answering, for the caller about to install the
        text and needing to stop if it is refused. Built on
        :meth:`test_config_text`, which a caller that only wants to preview
        the answer - never installing anything - calls directly.

        Args:
            config_text: The virtual host configuration to check.
            domain: Domain the configuration is meant for.

        Returns:
            None. Returning at all means the server accepted the snippet.

        Raises:
            ValidationError: When the server rejects the snippet. ``details``
                and ``output`` both carry the server's own output verbatim.
            NginxError: When the nginx snippet cannot be staged.
            ApacheError: When the apache snippet cannot be staged.
            DomainError: When the domain is not a valid domain name.
        """
        ok, output = self.test_config_text(config_text, domain=domain)
        if ok:
            return

        raise ValidationError(
            f"{self.backend.name} rejected the configuration for {domain}",
            details=output,
            output=output,
        )

    def replace_site_config(self, domain: str, config_text: str, *, validate: bool = True) -> Path:
        """
        Validate a hand-edited configuration and install it atomically.

        Args:
            domain: Domain of the site.
            config_text: The new configuration, written verbatim.
            validate: Check it with the web server first. False is only for
                putting back a configuration that was live a moment ago, when
                what just replaced it was refused.

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

        if validate:
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


# -- Cross-backend orchestration -------------------------------------------
#
# The two functions below are the chokepoints for "delete a site" and
# "create a secured site". Each used to be written once per caller - the CLI's
# ``site delete``, the CLI's application delete, the panel's site delete - and
# only one of the three checked both nginx and apache, so a site created on
# the backend a given path did not check outlived every deletion path that
# was not the one it happened to use. Same story for "create with SSL": the
# panel rendered ``ssl_certificate`` into the vhost before any certificate had
# been asked for, because writing the vhost and obtaining the certificate were
# two separate call sites that had drifted apart.
#
# Both functions take already-built managers rather than constructing their
# own from scratch: every caller already builds its managers through names it
# owns and that its own tests patch (the CLI through module-level imports, the
# web API through its ``MANAGERS`` registry), and building fresh managers here
# instead would silently stop honouring that.


@dataclass(frozen=True)
class SiteDeletion(MappingRecord):
    """
    What deleting a site actually found and removed.

    Attributes:
        domain: Domain that was targeted.
        nginx_removed: Whether an nginx vhost was found and removed.
        apache_removed: Whether an apache vhost was found and removed.
        certificate_removed: Whether a certificate was found and removed.
    """

    domain: str
    nginx_removed: bool = False
    apache_removed: bool = False
    certificate_removed: bool = False

    @property
    def removed_anything(self) -> bool:
        """True when at least one vhost or the certificate was removed."""
        return self.nginx_removed or self.apache_removed or self.certificate_removed


def delete_site_completely(
    domain: str,
    *,
    nginx: WebServerManager | None = None,
    apache: WebServerManager | None = None,
    cert_manager: CertManager | None = None,
    delete_certificate: bool = True,
    verbose: bool = False,
) -> SiteDeletion:
    """
    Remove a domain's virtual host from every backend, and its certificate.

    Each step is independent and best-effort: a web server or certbot failure
    on one step is logged and does not stop the others from being attempted,
    the same tolerance :meth:`ServiceManager.delete_service` already applies
    to stopping and disabling a unit before removing its file. A deletion that
    aborted on the first failure used to leave the other backend's vhost, or
    the certificate, behind.

    Args:
        domain: Domain to remove.
        nginx: Nginx-backed manager to use. Defaults to a fresh one bound to
            the real configuration tree; callers under test inject one bound
            to a sandbox.
        apache: Apache-backed manager to use, same default rule.
        cert_manager: Certificate manager to use, same default rule.
        delete_certificate: Also remove the certificate. False leaves it in
            place, for a caller that only wants the vhosts gone.
        verbose: Enable verbose logging on any manager built by default.

    Returns:
        What was actually found and removed.
    """
    nginx = nginx or WebServerManager(NGINX_BACKEND, verbose=verbose)
    apache = apache or WebServerManager(APACHE_BACKEND, verbose=verbose)
    cert_manager = cert_manager or CertManager(verbose=verbose)

    nginx_removed = False
    if nginx.site_exists(domain):
        try:
            nginx.delete_site(domain)
            nginx.reload()
            nginx_removed = True
        except SiteError as exc:
            _logger.warning("Could not remove the nginx site for %s: %s", domain, exc)

    apache_removed = False
    if apache.site_exists(domain):
        try:
            apache.delete_site(domain)
            apache.reload()
            apache_removed = True
        except SiteError as exc:
            _logger.warning("Could not remove the apache site for %s: %s", domain, exc)

    certificate_removed = False
    if delete_certificate and cert_manager.is_installed() and cert_manager.cert_exists(domain):
        try:
            cert_manager.delete(domain)
            certificate_removed = True
        except CertificateError as exc:
            _logger.warning("Could not remove the certificate for %s: %s", domain, exc)

    return SiteDeletion(
        domain=domain,
        nginx_removed=nginx_removed,
        apache_removed=apache_removed,
        certificate_removed=certificate_removed,
    )


@dataclass(frozen=True)
class SecuredSite(MappingRecord):
    """
    What :func:`create_secured_site` wrote and whether TLS ended up enabled.

    Attributes:
        domain: Domain that was configured.
        webserver: Backend that now serves it.
        site_existed: Whether the vhost already existed and was updated
            rather than created.
        ssl_requested: Whether TLS was asked for.
        ssl_enabled: Whether the site ended up serving TLS. False whenever
            ``ssl_requested`` is False, and also when it was requested but
            issuance failed - the site still exists, over plain HTTP.
        certificate_reused: Whether an existing, valid certificate already
            covered every requested domain, so nothing was issued.
        certificate_error: Why TLS was not enabled, when it was requested and
            did not end up enabled. None otherwise.
    """

    domain: str
    webserver: str
    site_existed: bool = False
    ssl_requested: bool = False
    ssl_enabled: bool = False
    certificate_reused: bool = False
    certificate_error: str | None = None


def create_secured_site(
    domain: str,
    *,
    manager: WebServerManager,
    webserver: str,
    cert_manager: CertManager | None = None,
    template: str = "proxy",
    port: int = DEFAULT_PROXY_PORT,
    www: bool = False,
    ssl: bool = True,
    enable: bool = True,
) -> SecuredSite:
    """
    Create or update a virtual host and, unless told not to, secure it with TLS.

    The vhost is always written without TLS first: certbot's nginx and apache
    plugins, and the webroot fallback, all need a plain HTTP vhost in place to
    validate the domain against. Only once a certificate is confirmed - reused
    or freshly obtained - is the vhost rewritten with the certificate paths
    and reloaded. ``POST /api/sites`` used to render ``ssl_certificate`` into
    the vhost from the request's ``ssl`` flag alone, before any certificate
    had been asked for, which is a config nginx then refused to reload.

    Args:
        domain: Domain to serve.
        manager: Web server manager to write the vhost through.
        webserver: Name of the backend ``manager`` drives (``nginx`` or
            ``apache``), used to pick the matching certbot plugin. Not read
            off ``manager`` itself, so a caller's own manager double does not
            need to carry a ``backend`` attribute.
        cert_manager: Certificate manager to use when ``ssl`` is true.
            Defaults to a fresh one.
        template: Template name, without the ``.conf.j2`` suffix.
        port: Port the application listens on behind the proxy.
        www: Also serve and certify ``www.<domain>``.
        ssl: Secure the site with a certificate. False writes a plain HTTP
            vhost and does nothing else.
        enable: Enable the site once written, when it did not already exist.

    Returns:
        What was written and whether TLS ended up enabled.

    Raises:
        DomainError: When the domain is not a valid domain name.
        DomainConflictError: When the domain is another application's alias
            or redirect (raised by ``create_site``).
        NginxError: When the nginx configuration cannot be written.
        ApacheError: When the apache configuration cannot be written.
        TemplateError: When the template is missing or fails to render.
    """
    include_www = www and should_include_www(domain)
    server_names = f"{domain} www.{domain}" if include_www else domain
    context: dict[str, Any] = {"port": port, "ssl": False, "server_names": server_names}

    site_existed = manager.site_exists(domain)
    if site_existed:
        manager.update_site(domain, template=template, context=context)
    else:
        manager.create_site(domain, template=template, context=context)
        if enable:
            manager.enable_site(domain)
    manager.reload()

    if not ssl:
        return SecuredSite(
            domain=domain,
            webserver=webserver,
            site_existed=site_existed,
            ssl_requested=False,
        )

    cert_manager = cert_manager or CertManager()
    if not cert_manager.is_installed():
        return SecuredSite(
            domain=domain,
            webserver=webserver,
            site_existed=site_existed,
            ssl_requested=True,
            certificate_error="certbot is not installed",
        )

    additional_domains = [f"www.{domain}"] if include_www else None
    required_domains = [domain, *(additional_domains or [])]

    certificate_reused = False
    if cert_manager.cert_exists(domain):
        test = cert_manager.test_cert(domain)
        if test.get("valid") and cert_manager.cert_covers_domains(domain, required_domains):
            certificate_reused = True

    certificate_error: str | None = None
    if not certificate_reused:
        try:
            cert_manager.obtain(
                domain,
                nginx=webserver == "nginx",
                apache=webserver == "apache",
                additional_domains=additional_domains,
            )
        except WASMError as exc:
            certificate_error = str(exc)

    if certificate_error is not None:
        return SecuredSite(
            domain=domain,
            webserver=webserver,
            site_existed=site_existed,
            ssl_requested=True,
            certificate_error=certificate_error,
        )

    cert_paths = cert_manager.get_cert_path(domain)
    context["ssl"] = True
    context["ssl_certificate"] = str(cert_paths["fullchain"])
    context["ssl_certificate_key"] = str(cert_paths["privkey"])
    manager.update_site(domain, template=template, context=context)
    manager.reload()

    return SecuredSite(
        domain=domain,
        webserver=webserver,
        site_existed=site_existed,
        ssl_requested=True,
        ssl_enabled=True,
        certificate_reused=certificate_reused,
    )
