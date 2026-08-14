# spec file for package wasm-cli
#
# Copyright (c) 2024-2025 Yago López Prado
# License: WASM-NCSAL (Non-Commercial Source-Available License)
#

Name:           wasm-cli
Version:        1.5.1
Release:        1%{?dist}
Summary:        Web App System Management CLI Tool
License:        WASM-NCSAL
URL:            https://github.com/Perkybeet/wasm
Source0:        wasm-%{version}.tar.gz
Source1:        wasm.default.yaml
Source2:        wasm.1
BuildArch:      noarch

# On Leap 15.x, python3 is 3.6. It cannot parse this code, so building against
# it produced a package that installed and then failed with SyntaxError on the
# first command, which is worse than not building. The 3.11 flavor is packaged
# there, so name it explicitly; %%python3_sitelib follows %%__python3.
%if 0%{?suse_version} && 0%{?suse_version} < 1600
%define __python3 /usr/bin/python3.11
%define python3_pkgversion 311
%else
%define python3_pkgversion 3
%endif

# Build requirements. Deliberately only what builds a wheel: nothing here runs
# the package, so no runtime import is needed at build time.
BuildRequires:  python%{python3_pkgversion}-devel
BuildRequires:  python%{python3_pkgversion}-setuptools
BuildRequires:  python%{python3_pkgversion}-pip
BuildRequires:  python%{python3_pkgversion}-wheel

# %%py3_build, %%py3_install and %%python3_sitelib live here. python3-devel
# happens to pull it in, but python311-devel does not, and on Leap 15.x the
# build then ran the literal string "%%py3_build" as a shell command.
%if 0%{?suse_version}
BuildRequires:  python-rpm-macros
%endif

# Fedora/RHEL specific
%if 0%{?fedora} || 0%{?rhel}
Requires:       python3-click >= 8.0
Requires:       python3-jinja2 >= 3.1.0
Requires:       python3-pyyaml >= 6.0
Requires:       python3-rich
# questionary reached Fedora in 42. On 40 and 41 the package built and then
# refused to install: the automatic generator turns the wheel metadata into
# python3.Xdist(questionary), and nothing there provides it. Filter that and ask
# for it softly. Interactive mode checks for it and says so when it is missing;
# every other command works without it.
%if 0%{?fedora} && 0%{?fedora} < 42
%global __requires_exclude ^python3\\.[0-9]+dist\\(questionary\\)
Recommends:     python3-questionary
%else
Requires:       python3-questionary
%endif
# The panel is optional. Everything it needs is packaged in Fedora.
Suggests:       python3-fastapi
Suggests:       python3-starlette
Suggests:       python3-pydantic
Suggests:       python3-uvicorn
Suggests:       python3-psutil
%endif

# openSUSE specific. The package names are capitalised there, and on Leap 15.x
# they carry the flavor prefix.
%if 0%{?suse_version}
Requires:       python%{python3_pkgversion}-click >= 8.0
Requires:       python%{python3_pkgversion}-Jinja2 >= 3.1.0
Requires:       python%{python3_pkgversion}-PyYAML >= 6.0
Requires:       python%{python3_pkgversion}-rich
Suggests:       python%{python3_pkgversion}-fastapi
Suggests:       python%{python3_pkgversion}-starlette
Suggests:       python%{python3_pkgversion}-pydantic
Suggests:       python%{python3_pkgversion}-uvicorn
Suggests:       python%{python3_pkgversion}-psutil
%if 0%{?suse_version} < 1600
Requires:       python311
# questionary is not packaged for 3.11 on Leap. Interactive mode already checks
# for it and explains its absence, and every other command works without it, so
# it is recommended rather than required.
Recommends:     python311-questionary
%else
Requires:       python3 >= 3.10
Requires:       python3-questionary
%endif
%endif

# Runtime requirements (common)
%if 0%{?fedora} || 0%{?rhel}
Requires:       python3 >= 3.10
%endif

# Suggested packages (not required for installation)
Suggests:       nginx
Suggests:       certbot
Suggests:       git
Suggests:       nodejs
Suggests:       npm

%description
WASM (Web App System Management) is a robust CLI tool for deploying 
and managing web applications on Linux servers. It handles site 
configuration (Nginx/Apache), SSL certificates (Certbot), systemd 
services, and automated deployment workflows for various application types.

Features:
 * Deploy Next.js, Node.js, Vite, Python, and static applications
 * Nginx and Apache site management
 * SSL certificate management via Certbot/Let's Encrypt
 * Systemd service management
 * Interactive mode with guided prompts
 * One-command deployments
 * Resource and service observability
 * Backup and rollback system
 * Control panel for remote management (optional)
 * REST API with token-based authentication

%prep
%autosetup -n wasm-%{version}

%build
# The pyproject macros where they exist (Fedora deprecated %%py3_build in 43 and
# the openSUSE guidelines forbid the legacy equivalents), and the old ones on
# the distributions that do not have them yet.
#
# Every %% above is doubled on purpose. A macro named in a comment inside a
# scriptlet is still expanded, and these expand to several lines: the # only
# comments out the first one and rpm runs the rest. That is what broke all
# thirteen RPM targets in 1.0.3, where this comment ran setup.py with the
# words "in 43 and" as its arguments.
#
# The branch is on the distribution, not on whether the macro exists. openSUSE
# defines %%pyproject_wheel too, but wires it to whichever Python flavor is
# primary that week (python314 as of writing) rather than to the interpreter
# python3-devel installs, so the build asked for /usr/bin/python3.14 and there
# was none. %%py3_build uses %%{__python3}, which is the one that is there.
# Refuse to build against an interpreter that cannot run the result. Without
# this, Leap 15.6 built a package against Python 3.6 and published it; it
# installed cleanly and raised SyntaxError on the first command.
%{__python3} -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' || { \
    echo "wasm-cli needs Python 3.10 or newer, and %{__python3} is older."; \
    exit 1; }

%if 0%{?fedora} || 0%{?rhel}
%pyproject_wheel
%else
%py3_build
%endif

%install
%if 0%{?fedora} || 0%{?rhel}
%pyproject_install
%else
%py3_install
%endif

# Shell completion. Committed, not generated here: Click's scripts contain no
# command names, so they cannot drift, and running Python during the build made
# every runtime import a build dependency.
install -Dm644 src/wasm/completions/wasm.bash %{buildroot}%{_datadir}/bash-completion/completions/wasm

%if ! 0%{?suse_version}
install -Dm644 src/wasm/completions/wasm.fish %{buildroot}%{_datadir}/fish/vendor_completions.d/wasm.fish
install -Dm644 src/wasm/completions/_wasm %{buildroot}%{_datadir}/zsh/site-functions/_wasm
%endif

# Install default configuration
install -Dm644 %{SOURCE1} %{buildroot}%{_sysconfdir}/wasm/config.yaml

# Install man page
install -Dm644 %{SOURCE2} %{buildroot}%{_mandir}/man1/wasm.1

# Create wasm-specific directories only (not /var/www or /var/backups)
install -d %{buildroot}/var/log/wasm

%files
%license LICENSE
%doc README.md
%doc docs/
%{python3_sitelib}/wasm/
%{python3_sitelib}/wasm_cli-*
%{_bindir}/wasm
%{_mandir}/man1/wasm.1*
%{_datadir}/bash-completion/completions/wasm
%if ! 0%{?suse_version}
%{_datadir}/fish/vendor_completions.d/wasm.fish
%{_datadir}/zsh/site-functions/_wasm
%endif
%dir %{_sysconfdir}/wasm
%config(noreplace) %{_sysconfdir}/wasm/config.yaml
%dir /var/log/wasm

%post
echo "WASM installed successfully!"
echo "Run 'wasm setup' to configure the tool."

# Upgrade config file with new defaults (preserves user values)
if [ -f /etc/wasm/config.yaml ]; then
    echo ""
    echo "Upgrading configuration with new defaults..."
    wasm config upgrade --quiet 2>/dev/null || true
fi

# Update wasm-monitor service if it exists and is enabled
if systemctl is-enabled wasm-monitor.service >/dev/null 2>&1; then
    echo ""
    echo "Updating wasm-monitor service..."
    wasm monitor install >/dev/null 2>&1 || true
    systemctl daemon-reload
    systemctl restart wasm-monitor.service 2>/dev/null || true
    echo "wasm-monitor service updated and restarted"
    echo ""
    echo "NOTE: the monitor no longer terminates processes or deletes files."
    echo "It decided what was malicious by matching substrings against a"
    echo "process command line, and acted on that as root. It now reports"
    echo "resource use, service health and notable processes, and acts on"
    echo "nothing. The auto_terminate and use_ai settings are ignored."
fi

%changelog
* Fri Aug 14 2026 Yago Lopez Prado <yago.lopez.adeje@gmail.com> - 1.5.1-1
- Fix: panel failed to start on distro pydantic v1 (Ubuntu 24.04); pydantic v1/v2 bridge with architecture guards and distro-import CI gates
* Fri Aug 14 2026 Yago Lopez Prado <yago.lopez.adeje@gmail.com> - 1.5.0-1
- Observability and product polish: per-app charts with deploy markers, user cron jobs, CLI deep links, command palette, log search
* Fri Aug 14 2026 Yago Lopez Prado <yago.lopez.adeje@gmail.com> - 1.4.0-1
- The panel manages the whole product: databases, services, sites, env vars, settings, scheduled backups, deployment history with rollback, live charts, git webhooks, notifications
* Fri Aug 14 2026 Yago Lopez Prado <yago.lopez.adeje@gmail.com> - 1.3.0-1
- Panel security: TLS by default, TOTP 2FA, scoped API tokens, session management, browser E2E in CI
* Thu Aug 13 2026 Yago Lopez Prado <yago.lopez.adeje@gmail.com> - 1.2.1-1
- Fix: wasm reported no applications on a machine whose records existed, after the monitor service created /var/lib/wasm and the store moved to it; a database that already exists now outranks an empty location
* Thu Aug 13 2026 Yago Lopez Prado <yago.lopez.adeje@gmail.com> - 1.2.0-1
- Fix: the monitor service never started, failing every 30 seconds since installation, because its unit named a state directory systemd was never asked to create
- Fix: the panel's journal streams left a journalctl process running on every disconnection
- Fix: the panel's live event feed did not exist; /events now streams state changes and notices as server-sent events
- Fix: the log drawer and the mobile navigation never ran at all, blocked by the panel's own script-src policy
- Fix: the machine strip and the navigation scrolled off the top of every page
- Fix: an action that succeeded reported nothing, and a failure was shown as truncated JSON instead of the tool's own output
- Fix: page routes answered a manager failure with a plain-text Internal Server Error
- Fix: a session renewing itself silently invalidated the CSRF token every control was using
- Fix: text colours and badge backgrounds now meet WCAG AA on every surface they are placed on
- Feature: deploy an application from the panel
- Feature: start, stop, enable and disable services, enable and disable sites, revoke and delete certificates
- Feature: take a backup from an application's row, and verify that an archive is sound
- Enhancement: wasm web start explains how to reach a loopback panel over an SSH tunnel, and offers a port it has checked is free
- Refactor: remove Alpine, which the panel's content security policy had always refused to execute
* Wed Aug 12 2026 Yago Lopez Prado <yago.lopez.adeje@gmail.com> - 1.1.0-1
- Fix: wasm list reports the state systemd and the port actually report, not the value written at deploy time; it and wasm health no longer contradict each other
- Fix: a service systemd is restarting every few seconds, or one that accepts no connections, is no longer reported as running
- Fix: static sites are no longer counted as stopped; they have no service to run
- Fix: the RPM builds on Fedora and openSUSE again; a macro named in a comment inside the build section was expanded and ran
- Fix: Leap 15.x builds against the packaged Python 3.11 instead of 3.6, which could not run the result
- Fix: the package installs on Debian 12, Ubuntu 22.04 and Fedora 41, where python3-questionary does not exist
- Fix: wasm web start refuses a port that is already taken instead of printing a token and then failing to bind
- Fix: wasm --no-color applies to wasm health, and the logger follows a redirected stdout
- Change: wasm list is a coloured table where colour encodes state, and it names what needs attention
- Change: publishing waits for the .deb and the RPM to be built, installed and run in clean containers
* Wed Aug 12 2026 Yago Lopez Prado <yago.lopez.adeje@gmail.com> - 1.0.3-1
- Fix: the Debian build dependencies in wasm.dsc and debian.control agree, and are the minimum that builds a wheel
* Wed Aug 12 2026 Yago Lopez Prado <yago.lopez.adeje@gmail.com> - 1.0.2-1
- Fix: interactive mode works again; it still imported inquirer after that stopped being a dependency
- Fix: distribution packages build; the completion scripts are committed instead of generated, so the build no longer needs to run the package
- Fix: the RPM spec no longer uses a macro as a tag, which failed every Fedora and openSUSE target
- Change: publishing waits for tests, lint, types, a clean install and a container-built .deb
* Wed Aug 12 2026 Yago Lopez Prado <yago.lopez.adeje@gmail.com> - 1.0.1-1
- Fix: package manager availability is asked of the command runner, not of the process PATH
- Fix: a project whose lock file names a package manager refuses to install with a different one
* Wed Aug 12 2026 Yago Lopez Prado <yago.lopez.adeje@gmail.com> - 1.0.0-1
- Security: closes six critical issues, including arbitrary code execution as root through systemd unit environment injection
- Security: the process monitor no longer terminates processes or deletes directories; it reports instead of acting
- Security: the panel serves no third-party asset, uses HttpOnly session cookies with CSRF, and writes an audit log
- Security: database dumps, archive extraction and SQL privileges are validated instead of interpolated
- Feature: control panel rebuilt as server-rendered pages, with no build step and no CDN
- Feature: --dry-run is enforced at the execution and filesystem seams, so it holds for every command
- Change: command line migrated to Click; every command, alias and option is preserved
- Change: backup archives are self-contained and verified; format version 2.0.0
- Packaging: supports Ubuntu 26.04 and Python 3.14; drops python-jose and python3-inquirer
* Fri Mar 20 2026 Perkybeet <yago.lopez.adeje@gmail.com> - 0.15.8-1
- Feature: Auto-verify and expand SSL certificates for www subdomain coverage
- Enhancement: cert_manager.obtain() checks domain coverage before skipping existing certs
- Enhancement: Site create verifies existing certs cover all required domains including www

* Fri Mar 20 2026 Perkybeet <yago.lopez.adeje@gmail.com> - 0.15.7-1
- Fix: Site create fails with "site already exists" when config file remains after deletion
- Fix: Site create SSL step fails with "site already exists" on second create_site call
- Enhancement: Site create updates existing config instead of failing

* Fri Mar 20 2026 Perkybeet <yago.lopez.adeje@gmail.com> - 0.15.6-1
- Feature: Site create now obtains SSL certificates automatically
- Feature: Add --no-ssl flag to site create to skip SSL
- Enhancement: Site create reuses existing valid certificates instead of re-obtaining
- Enhancement: Interactive site create prompts for SSL configuration

* Fri Mar 20 2026 Perkybeet <yago.lopez.adeje@gmail.com> - 0.15.5-1
- Feature: Add --www flag to site create for www subdomain in web server config
- Enhancement: Interactive site create prompts for www inclusion

* Fri Mar 20 2026 Perkybeet <yago.lopez.adeje@gmail.com> - 0.15.4-1
- Fix: Interactive mode crashes due to missing Namespace attributes (6 bugs)
- Fix: Service create uses wrong attribute name (command vs exec_command)
- Fix: Webapp delete, logs, update missing required attributes
- Fix: Service logs and cert revoke missing required attributes
- Feature: Add --www flag for including www subdomain in SSL certificates
- Feature: Add --expand flag for expanding existing SSL certificates
- Enhancement: Interactive mode prompts for log lines, follow mode, and branch
- Enhancement: Nginx/Apache templates support server_names with www

* Wed Mar 11 2026 Perkybeet <yago.lopez.adeje@gmail.com> - 0.15.3-1
- Fix: SourceManager.fetch() parameter mismatch in Docker Compose deployer
- Fix: ServiceManager.create_service() API mismatch in Docker Compose deployer
- Fix: Docker Compose build/update timeouts (600000s to 600s/300s)
- Feature: Headless worker support for Docker Compose apps with no exposed ports
- Enhancement: Add _PASS pattern to EnvManager secret detection
- Enhancement: ServiceManager.create_service() accepts extra template context

* Tue Mar 10 2026 Perkybeet <yago.lopez.adeje@gmail.com> - 0.15.2-1
- Fix: Web interface token display uses print() for reliable visibility
- Enhancement: Release workflow validates OBS credentials before proceeding

* Mon Mar 09 2026 Perkybeet <yago.lopez.adeje@gmail.com> - 0.15.1-1
- Fix: Python deployer compatibility with Python 3.10 (os.walk instead of Path.walk)
- Fix: Django WSGI/ASGI detection break placement
- Fix: Docker Compose false-positive detection when framework config files present
- Enhancement: Update command uses stored app type from database
- Enhancement: Verbose command output logging via Logger.command_output()
- Enhancement: Web jobs use specific WASM exceptions instead of bare Exception
- Enhancement: Web server uses logging module instead of print()
- Enhancement: setup.py adds web/monitor/all extras and fixes entry point
- Enhancement: .gitignore updated for rpm/*.spec, .claude/, .ruff_cache/

* Mon Mar 09 2026 Perkybeet <yago.lopez.adeje@gmail.com> - 0.15.0-1
- Feature: Add Docker Compose deployer with full deployment lifecycle
- Feature: Add environment variable manager with .env.example discovery and secret auto-generation
- Feature: Add advanced Nginx configuration builder with multi-route proxying via wasm.nginx.yaml
- Feature: Add backup scheduler with systemd timer integration
- Feature: Add wasm env CLI command for configure, show, and export operations
- Feature: Add wasm backup schedule CLI subcommand for create, list, and delete
- Enhancement: Add DockerError exception type for Docker-related failures
- Enhancement: Add DOCKER_COMPOSE app type to store and registry
- Enhancement: Update web dashboard UI with improved styling and page components
- Enhancement: Add create_advanced_site method to NginxManager
- Enhancement: Improve PostgreSQL and Redis database managers

* Fri Feb 06 2026 Perkybeet <yago.lopez.adeje@gmail.com> - 0.14.4-1
- Fix: service_exists() not finding legacy wasm- prefixed services
- Fix: Update command fails to restart legacy services

* Fri Feb 06 2026 Perkybeet <yago.lopez.adeje@gmail.com> - 0.14.3-1
- Fix: Monorepo detection too aggressive (single Next.js apps misdetected)
- Fix: Update command crashes with MonorepoDeployer (missing pre_install)
- Fix: Update checker shows false positive when already on latest version
- Fix: Update checker recommends pip when installed via apt/dnf
- Fix: Release workflow supports manual re-trigger via workflow_dispatch

* Wed Feb 04 2026 Perkybeet <yago.lopez.adeje@gmail.com> - 0.14.2-1
- Feature: Add MonorepoDeployer for Turborepo/pnpm workspace deployments
- Feature: New CLI options --subdomains, --workspaces, --no-database
- Fix: Update command now queries database for app_path (supports legacy apps)
- Add workspace and turbo helpers for monorepo detection

* Wed Jan 28 2026 Perkybeet <yago.lopez.adeje@gmail.com> - 0.14.1-1
- Fix: API verify() returns correct keys (checksum_ok, files_ok)
- Fix: BackupMetadata now persists includes_build field
- Fix: Backup rotation uses direct app_name lookup
- Fix: Remove non-ASCII characters from CLI output
- Feature: Database backup integration (MySQL, PostgreSQL, MongoDB, Redis)
- Feature: New --include-databases flag for backup create
- Enhancement: API and CLI now expose all backup options

* Thu Jan 15 2026 Perkybeet <yago.lopez.adeje@gmail.com> - 0.14.0-1
- Feature: New 'wasm health' command for system diagnostics
- Feature: New 'wasm config' command (upgrade, show, path)
- Feature: Automatic config migration on package upgrade
- Feature: Persistent threat storage with SQLite (threat_store.py)
- Feature: New API endpoints /api/monitor/threats/history and resolve
- Fix: Monitor module duplicate return statement (dead code)
- Fix: Inconsistent scan_interval defaults (30s local, 3600s AI)
- Fix: API /scan now respects global config (auto_terminate, use_ai)
- Fix: CPU/Memory thresholds now used for logging and alerts
- Enhancement: Monitor frontend uses WebSocket instead of HTTP polling
- Enhancement: Auto-update wasm-monitor service on package upgrade
- Enhancement: Improved process fallback with better ps parsing
- Enhancement: Notification report includes all threats for audit

* Thu Jan 15 2026 Perkybeet <yago.lopez.adeje@gmail.com> - 0.13.16-1
- Fix: Move python3-inquirer from Depends to Recommends
- Fix: Package installs on systems without python3-inquirer in repos
- Enhancement: Interactive mode now optional (install inquirer via pip)

* Thu Jan 15 2026 Perkybeet <yago.lopez.adeje@gmail.com> - 0.13.15-1
- Enhancement: Set update checker interval to instant (CHECK_INTERVAL = 0)
- Enhancement: Change update notification color to softer yellow
- Feature: Add --changelog flag to view current version changelog
- Feature: New version.py module for version information display

* Thu Jan 15 2026 Perkybeet <yago.lopez.adeje@gmail.com> - 0.13.14-1
- Fix: OBS build failures - add missing python3-inquirer dependency
- Enhancement: Ensure all OBS package dependencies are declared

* Thu Jan 15 2026 Perkybeet <yago.lopez.adeje@gmail.com> - 0.13.13-1
- Fix: Critical bare except clause in monitor API
- Fix: Static apps (Vite) trying to restart non-existent services
- Fix: Update checker now detects installation method
- Feature: Update banner in web dashboard
- Feature: /api/system/version endpoint for update checking

* Wed Jan 14 2026 Perkybeet <yago.lopez.adeje@gmail.com> - 0.13.12-1
- chore: Clean distribution packages (remove Docker files, dev tools)
- feat: Add .gitattributes to exclude dev files from git archive
- feat: Add MANIFEST.in to control PyPI source distribution

* Wed Jan 14 2026 Perkybeet <yago.lopez.adeje@gmail.com> - 0.13.11-1
- Fix: Environment variables with quotes properly stripped from .env files
- Feature: Automatic update checker via GitHub Releases API
- Enhancement: Update checker runs post-command to avoid delays

* Thu Jan 08 2026 Perkybeet <yago.lopez.adeje@gmail.com> - 0.13.7-1
- Fix: 'wasm store sync' now updates app status along with service status

* Thu Jan 08 2026 Perkybeet <yago.lopez.adeje@gmail.com> - 0.13.6-1
- Fix: 'wasm store sync' attribute naming (service.status vs service.active)

* Thu Jan 08 2026 Perkybeet <yago.lopez.adeje@gmail.com> - 0.13.5-1
- Fix: 'wasm store import' finds app directories with multiple naming conventions

* Thu Jan 08 2026 Perkybeet <yago.lopez.adeje@gmail.com> - 0.13.4-1
- Fix: 'wasm store import' using wrong attribute name (unit_file)

* Thu Jan 08 2026 Perkybeet <yago.lopez.adeje@gmail.com> - 0.13.3-1
- Fix: Corrupted debian.postrm script causing upgrade failure

* Thu Jan 08 2026 Perkybeet <yago.lopez.adeje@gmail.com> - 0.13.2-1
- Feature: SQLite persistence store for tracking deployed apps
- New: Store tracks apps, sites, services, and databases
- New: wasm store commands (init, stats, import, export, sync, path)
- Enhancement: webapp list/status commands now use SQLite store
- Enhancement: Database create/drop commands track in store
- Enhancement: Managers (nginx, apache, service, cert) register in store
- Fix: GitHub Actions .deb build missing pybuild-plugin-pyproject

* Tue Dec 30 2025 Perkybeet <yago.lopez.adeje@gmail.com> - 0.13.1-1
- Fix: Systemd services failing with 'Permission denied' when using nvm
- Fix: Detect and avoid private paths (nvm, ~/.local) in service ExecStart
- Fix: Prefer global Node.js installation over user-specific nvm paths
- Enhancement: Add helpful error messages for nvm path issues

* Mon Feb 24 2025 Perkybeet <yago.lopez.adeje@gmail.com> - 0.13.0-1
- Feature: Database UI overhaul with logs, tabs, and SQL import
- Feature: Database credential management via config.yaml
- Fix: MySQL connection with password protection
- Fix: Local environment installation issues
- Real-time WebSocket updates for logs and events
- Token-based authentication with JWT
- Rate limiting and brute force protection
- API endpoints: /api/apps, /api/services, /api/sites, /api/certs
- API endpoints: /api/backups, /api/monitor, /api/system, /api/config
- Background job processing with progress tracking
- Optional dependencies: pip install wasm-cli[web]
- Detect OOM (Out of Memory) build failures with exit code 137
- Provide actionable suggestions for resolving memory issues
- Add OutOfMemoryError exception with swap/memory configuration tips
- CI: Automatic deployment to OBS on release
- Fix: OBS deployment configuration in GitHub Actions
- Fix: Git pull with unstaged/uncommitted changes during wasm update
- Auto-stash local changes before pull, restore after
- Handle divergent branches with automatic reset to remote
- Handle rebase conflicts gracefully
- Preserve .env and untracked files during force updates
- Fix: Git "dubious ownership" error during wasm update
- Auto-configure git safe.directory for app directories
- Add man page (wasm.1) for all distributions
- Fix RPM packaging to include man page
- Improve documentation
- Feature: wasm store import auto-detects app type

* Wed Dec 18 2024 Perkybeet <yago.lopez.adeje@gmail.com> - 0.10.0-1
- Initial RPM package for OBS
- Backup and rollback system
- AI-powered security monitoring
- Shell completions for bash, zsh, fish
- Support for Next.js, Node.js, Vite, Python, and static sites
