# spec file for package wasm-cli
#
# Copyright (c) 2024-2025 Yago López Prado
# License: WASM-NCSAL (Non-Commercial Source-Available License)
#

Name:           wasm-cli
Version:        0.13.5
Release:        1%{?dist}
Summary:        Web App System Management CLI Tool
License:        WASM-NCSAL
URL:            https://github.com/Perkybeet/wasm
Source0:        wasm-%{version}.tar.gz
Source1:        wasm.default.yaml
Source2:        wasm.1
BuildArch:      noarch

# Build requirements - use python3 macros
BuildRequires:  python3-devel
BuildRequires:  python3-setuptools
BuildRequires:  python3-pip

# Fedora/RHEL specific
%if 0%{?fedora} || 0%{?rhel}
BuildRequires:  python3-wheel
Requires:       python3-jinja2 >= 3.1.0
Requires:       python3-pyyaml >= 6.0
Requires:       python3-inquirer >= 3.1.0
%endif

# openSUSE specific
%if 0%{?suse_version}
BuildRequires:  python3-wheel
Requires:       python3-Jinja2 >= 3.1.0
Requires:       python3-PyYAML >= 6.0
# inquirer may need to be installed via pip on SUSE
%endif

# Runtime requirements (common)
Requires:       python3 >= 3.10

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
 * AI-powered security monitoring
 * Backup and rollback system
 * Web dashboard for remote management (optional)
 * REST API with token-based authentication

%prep
%autosetup -n wasm-%{version}

%build
%py3_build

%install
%py3_install

# Install completion scripts
install -Dm644 src/wasm/completions/wasm.bash %{buildroot}%{_datadir}/bash-completion/completions/wasm

# For openSUSE: fish and zsh completions directories may not exist
%if 0%{?suse_version}
# SUSE: Only install if directories are provided by system packages
# Skip fish/zsh completions on SUSE to avoid directory ownership issues
%else
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
%{python3_sitelib}/wasm_cli*.egg-info/
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
echo ""
echo "Note: You may need to install python3-inquirer via pip:"
echo "  pip3 install inquirer"

%changelog
* Wed Jan 08 2026 Perkybeet <yago.lopez.adeje@gmail.com> - 0.13.5-1
- Fix: 'wasm store import' finds app directories with multiple naming conventions

* Wed Jan 08 2026 Perkybeet <yago.lopez.adeje@gmail.com> - 0.13.4-1
- Fix: 'wasm store import' using wrong attribute name (unit_file)

* Wed Jan 08 2026 Perkybeet <yago.lopez.adeje@gmail.com> - 0.13.3-1
- Fix: Corrupted debian.postrm script causing upgrade failure

* Wed Jan 08 2026 Perkybeet <yago.lopez.adeje@gmail.com> - 0.13.2-1
- Feature: SQLite persistence store for tracking deployed apps
- New: Store tracks apps, sites, services, and databases
- New: wasm store commands (init, stats, import, export, sync, path)
- Enhancement: webapp list/status commands now use SQLite store
- Enhancement: Database create/drop commands track in store
- Enhancement: Managers (nginx, apache, service, cert) register in store
- Fix: GitHub Actions .deb build missing pybuild-plugin-pyproject

* Mon Dec 30 2025 Perkybeet <yago.lopez.adeje@gmail.com> - 0.13.1-1
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

* Wed Dec 18 2024 Perkybeet <yago.lopez.adeje@gmail.com> - 0.10.0-1
- Initial RPM package for OBS
- Backup and rollback system
- AI-powered security monitoring
- Shell completions for bash, zsh, fish
- Support for Next.js, Node.js, Vite, Python, and static sites
