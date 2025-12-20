# spec file for package wasm-cli
#
# Copyright (c) 2024 Perkybeet
# License: MIT
#

Name:           wasm-cli
Version:        0.11.2
Release:        1%{?dist}
Summary:        Web App System Management CLI Tool
License:        MIT
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
# Web interface dependencies (optional, install via pip if not available)
Recommends:     python3-fastapi >= 0.109.0
Recommends:     python3-uvicorn >= 0.27.0
Recommends:     python3-jose >= 3.3.0
Recommends:     python3-passlib >= 1.7.4
Recommends:     python3-bcrypt
Recommends:     python3-psutil >= 5.9.0
Recommends:     python3-aiofiles >= 23.0.0
%endif

# openSUSE specific
%if 0%{?suse_version}
BuildRequires:  python3-wheel
Requires:       python3-Jinja2 >= 3.1.0
Requires:       python3-PyYAML >= 6.0
# inquirer may need to be installed via pip on SUSE
# Web interface dependencies (optional, install via pip if not available)
Recommends:     python3-fastapi >= 0.109.0
Recommends:     python3-uvicorn >= 0.27.0
Recommends:     python3-python-jose >= 3.3.0
Recommends:     python3-passlib >= 1.7.4
Recommends:     python3-bcrypt
Recommends:     python3-psutil >= 5.9.0
Recommends:     python3-aiofiles >= 23.0.0
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
* Sun Dec 21 2025 Perkybeet <yago.lopez.adeje@gmail.com> - 0.11.2-1
- Fix: Make web dependencies optional (Recommends) for OBS packages
- Web deps like python3-fastapi not available as native packages in Fedora/openSUSE

* Sat Dec 20 2025 Perkybeet <yago.lopez.adeje@gmail.com> - 0.11.1-1
- Fix: Include web dependencies in OBS packages (deb/rpm)
- Add python3-fastapi, python3-uvicorn, python3-jose as dependencies
- Add python3-passlib, python3-bcrypt, python3-psutil, python3-aiofiles
- Improve error messages for missing web dependencies
- Show OS-specific installation instructions (apt/dnf/zypper/pip)

* Thu Dec 19 2024 Perkybeet <yago.lopez.adeje@gmail.com> - 0.11.0-1
- New feature: Web Interface Dashboard (wasm web start/stop/status)
- REST API for remote application management
- Real-time WebSocket updates for logs and events
- Token-based authentication with JWT
- Rate limiting and brute force protection
- API endpoints: /api/apps, /api/services, /api/sites, /api/certs
- API endpoints: /api/backups, /api/monitor, /api/system, /api/config
- Background job processing with progress tracking
- Optional dependencies: pip install wasm-cli[web]

* Thu Dec 19 2024 Perkybeet <yago.lopez.adeje@gmail.com> - 0.10.5-1
- Detect OOM (Out of Memory) build failures with exit code 137
- Provide actionable suggestions for resolving memory issues
- Add OutOfMemoryError exception with swap/memory configuration tips

* Thu Dec 19 2024 Perkybeet <yago.lopez.adeje@gmail.com> - 0.10.4-1
- CI: Automatic deployment to OBS on release
- Fix: OBS deployment configuration in GitHub Actions

* Thu Dec 19 2024 Perkybeet <yago.lopez.adeje@gmail.com> - 0.10.3-1
- Fix: Git pull with unstaged/uncommitted changes during wasm update
- Auto-stash local changes before pull, restore after
- Handle divergent branches with automatic reset to remote
- Handle rebase conflicts gracefully
- Preserve .env and untracked files during force updates

* Thu Dec 19 2024 Perkybeet <yago.lopez.adeje@gmail.com> - 0.10.2-1
- Fix: Git "dubious ownership" error during wasm update
- Auto-configure git safe.directory for app directories

* Thu Dec 19 2024 Perkybeet <yago.lopez.adeje@gmail.com> - 0.10.1-1
- Add man page (wasm.1) for all distributions
- Fix RPM packaging to include man page
- Improve documentation

* Wed Dec 18 2024 Perkybeet <yago.lopez.adeje@gmail.com> - 0.10.0-1
- Initial RPM package for OBS
- Backup and rollback system
- AI-powered security monitoring
- Shell completions for bash, zsh, fish
- Support for Next.js, Node.js, Vite, Python, and static sites
