# WASM - Web App System Management

<p align="center">
  <img src="docs/assets/logo_bg.png" alt="WASM Logo" width="400">
</p>

<p align="center">
  <a href="https://build.opensuse.org/package/show/home:Perkybeet/wasm">
    <img src="https://build.opensuse.org/projects/home:Perkybeet/packages/wasm/badge.svg?type=default" alt="OBS Build Status">
  </a>
  <a href="https://pypi.org/project/wasm-cli/">
    <img src="https://img.shields.io/pypi/v/wasm-cli?color=blue&logo=pypi&logoColor=white" alt="PyPI Version">
  </a>
  <a href="https://pypi.org/project/wasm-cli/">
    <img src="https://img.shields.io/pypi/pyversions/wasm-cli?logo=python&logoColor=white" alt="Python Version">
  </a>
  <a href="https://github.com/Perkybeet/wasm/blob/main/LICENSE">
    <img src="https://img.shields.io/github/license/Perkybeet/wasm?color=blue" alt="License">
  </a>
  <a href="https://github.com/Perkybeet/wasm/stargazers">
    <img src="https://img.shields.io/github/stars/Perkybeet/wasm?style=social" alt="GitHub Stars">
  </a>
  <a href="https://pypi.org/project/wasm-cli/">
    <img src="https://img.shields.io/pypi/dm/wasm-cli?color=blue&logo=pypi" alt="PyPI Downloads">
  </a>
</p>

---

## Overview

WASM is a command-line tool for deploying and managing web applications on Linux servers. It automates the deployment workflow from repository cloning through production serving, handling Nginx/Apache configuration, SSL certificates, systemd services, and application builds.

### Core Functionality

- Deploy Next.js, Node.js, Vite, Python, and static applications
- Configure Nginx and Apache virtual hosts
- Manage SSL certificates via Let's Encrypt/Certbot
- Create and control systemd services
- Database management (MySQL, PostgreSQL, Redis, MongoDB)
- Backup and rollback system
- Web-based dashboard (optional)
- Process monitoring and security scanning

---

## Installation

### Ubuntu/Debian (Recommended)

```bash
# Add GPG key
curl -fsSL https://download.opensuse.org/repositories/home:/Perkybeet/xUbuntu_24.04/Release.key | \
  gpg --dearmor | sudo tee /usr/share/keyrings/wasm.gpg > /dev/null

# Add repository
echo 'deb [signed-by=/usr/share/keyrings/wasm.gpg] https://download.opensuse.org/repositories/home:/Perkybeet/xUbuntu_24.04/ /' | \
  sudo tee /etc/apt/sources.list.d/wasm.list

# Install
sudo apt update
sudo apt install wasm
```

**Supported versions:**
- Ubuntu 22.04 LTS (Jammy Jellyfish)
- Ubuntu 24.04 LTS (Noble Numbat)
- Debian 12 (Bookworm)

### Fedora

```bash
sudo dnf config-manager --add-repo \
  https://download.opensuse.org/repositories/home:/Perkybeet/Fedora_40/home:Perkybeet.repo
sudo dnf install wasm-cli
```

### openSUSE

```bash
# Tumbleweed
sudo zypper ar -f \
  https://download.opensuse.org/repositories/home:/Perkybeet/openSUSE_Tumbleweed/ \
  home_Perkybeet
sudo zypper install wasm-cli

# Leap 15.6
sudo zypper ar -f \
  https://download.opensuse.org/repositories/home:/Perkybeet/openSUSE_Leap_15.6/ \
  home_Perkybeet
sudo zypper install wasm-cli
```

### PyPI

```bash
pip install wasm-cli
```

### From Source

```bash
git clone https://github.com/Perkybeet/wasm.git
cd wasm
pip install -e .
```

---

## Quick Start

### Deploy a Next.js Application

```bash
wasm create \
  --domain myapp.example.com \
  --source git@github.com:user/my-nextjs-app.git \
  --type nextjs \
  --port 3000
```

Short syntax:

```bash
wasm create -d myapp.example.com -s git@github.com:user/app.git -t nextjs -p 3000
```

### Interactive Mode

Run `wasm` without arguments to enter interactive mode:

```bash
wasm
```

The interactive wizard will guide you through:
1. Application type selection
2. Domain configuration
3. Source repository
4. Port assignment
5. SSL certificate setup

---

## Usage

### Application Management

```bash
# Deploy application
wasm create -d example.com -s git@github.com:user/repo.git -t nextjs

# List deployed applications
wasm list

# View application status
wasm status example.com

# Restart application
wasm restart example.com

# Update application (git pull + rebuild)
wasm update example.com

# Remove application
wasm delete example.com

# View application logs
wasm logs example.com --follow
```

### Site Configuration

```bash
# Create Nginx/Apache site
wasm site create -d example.com -w nginx

# List all sites
wasm site list

# Enable or disable site
wasm site enable example.com
wasm site disable example.com

# Delete site
wasm site delete example.com
```

### SSL Certificates

```bash
# Obtain Let's Encrypt certificate
wasm cert create -d example.com

# List certificates
wasm cert list

# Renew certificates
wasm cert renew

# View certificate details
wasm cert info example.com
```

### Service Management

```bash
# Create systemd service
wasm service create --name myservice --command "/usr/bin/myapp" --user www-data

# Control services
wasm service start myservice
wasm service stop myservice
wasm service restart myservice

# View service status and logs
wasm service status myservice
wasm service logs myservice --follow

# Delete service
wasm service delete myservice
```

### Database Management

```bash
# Install database engine
wasm db install mysql

# Create database
wasm db create mydb --engine mysql

# List databases
wasm db list --engine mysql

# Backup database
wasm db backup mydb --engine mysql --output backup.sql.gz

# Restore database
wasm db restore mydb backup.sql.gz --engine mysql
```

### Backup and Rollback

```bash
# Create backup
wasm backup create example.com -m "Pre-deployment backup"

# List backups
wasm backup list example.com

# Restore from backup
wasm backup restore BACKUP_ID

# Quick rollback to last backup
wasm rollback example.com
```

---

## Supported Application Types

| Type | Framework | Auto-Detection |
|------|-----------|----------------|
| `nextjs` | Next.js | `next.config.js`, `next.config.mjs` |
| `nodejs` | Express, Fastify, Koa | `package.json` with start script |
| `vite` | React, Vue, Svelte (Vite) | `vite.config.js`, `vite.config.ts` |
| `python` | Django, Flask, FastAPI | `requirements.txt`, `pyproject.toml` |
| `static` | HTML/CSS/JS | `index.html` |

## Deployment Workflow

For each application type, WASM executes:

1. Clone repository to `/var/www/apps/wasm-<app-name>/`
2. Install dependencies (npm, pip, etc.)
3. Build application (if applicable)
4. Create systemd service
5. Configure Nginx/Apache reverse proxy
6. Obtain SSL certificate (optional)
7. Start service and verify status

---

## Configuration

### Global Configuration

Configuration file: `/etc/wasm/config.yaml`

```yaml
# Web server preference
webserver: nginx

# Application directory
apps_directory: /var/www/apps

# Service user
service_user: www-data

# SSL configuration
ssl:
  enabled: true
  provider: certbot
  email: admin@example.com

# Logging
logging:
  level: info
  file: /var/log/wasm/wasm.log

# Node.js settings
nodejs:
  default_version: 20
  use_nvm: false

# Python settings
python:
  default_version: "3.11"
  use_venv: true
```

### Per-Application Configuration

Create `.wasm.yaml` in your project root:

```yaml
type: nextjs
port: 3000
build_command: npm run build
start_command: npm run start
env_vars:
  NODE_ENV: production
health_check:
  path: /api/health
  timeout: 30
```

---

## Command Reference

### Application Commands

```
wasm create [options]          Deploy new application
wasm list                       List deployed applications
wasm status <domain>            Show application status
wasm restart <domain>           Restart application
wasm stop <domain>              Stop application
wasm start <domain>             Start application
wasm update <domain>            Update application (git pull + rebuild)
wasm delete <domain>            Remove application
wasm logs <domain> [options]    View application logs
```

### Site Commands

```
wasm site create <domain>       Create site configuration
wasm site list                  List all sites
wasm site enable <domain>       Enable site
wasm site disable <domain>      Disable site
wasm site delete <domain>       Delete site
wasm site show <domain>         Display site configuration
```

### Service Commands

```
wasm service create [options]   Create systemd service
wasm service list               List managed services
wasm service status <name>      Show service status
wasm service start <name>       Start service
wasm service stop <name>        Stop service
wasm service restart <name>     Restart service
wasm service logs <name>        View service logs
wasm service delete <name>      Delete service
```

### Certificate Commands

```
wasm cert create <domain>       Obtain SSL certificate
wasm cert list                  List certificates
wasm cert info <domain>         Show certificate details
wasm cert renew [domain]        Renew certificates
wasm cert revoke <domain>       Revoke certificate
```

### Database Commands

```
wasm db install <engine>        Install database engine
wasm db create <name>           Create database
wasm db drop <name>             Drop database
wasm db list                    List databases
wasm db backup <name>           Backup database
wasm db restore <name> <file>   Restore database
wasm db user-create <username>  Create database user
wasm db grant <user> <db>       Grant privileges
```

### Backup Commands

```
wasm backup create <domain>     Create backup
wasm backup list [domain]       List backups
wasm backup restore <id>        Restore from backup
wasm backup delete <id>         Delete backup
wasm backup verify <id>         Verify backup integrity
wasm rollback <domain> [id]     Quick rollback
```

---

## Web Dashboard

WASM includes an optional web-based dashboard for remote management.

### Installation

```bash
pip install wasm-cli[web]
```

### Start Dashboard

```bash
wasm web start --host 0.0.0.0 --port 8080
```

### Features

- Application deployment and management
- Real-time logs and monitoring
- SSL certificate management
- Service control
- Backup creation and restoration
- Database management
- REST API with token authentication

Access the dashboard at `http://your-server:8080`

---

## System Requirements

- **Operating System**: Ubuntu 20.04+, Debian 11+, Fedora 38+, openSUSE Leap 15.5+
- **Python**: 3.10 or higher
- **Privileges**: sudo access for service management

### Optional Dependencies

- nginx or apache2 (web server)
- certbot (SSL certificates)
- git (repository cloning)
- nodejs/npm (for Node.js applications)
- python3-venv (for Python applications)
- mysql-server or postgresql (database support)

WASM will check for missing dependencies and prompt installation when needed.

---

## Directory Structure

```
/var/www/apps/
├── wasm-example-com/
│   ├── current/              # Active deployment
│   ├── releases/             # Previous releases (for rollback)
│   │   ├── 20260114120000/
│   │   └── 20260113150000/
│   ├── shared/               # Persistent files (uploads, logs)
│   └── .env                  # Environment variables
│
└── wasm-another-app/
    └── ...
```

---

## Development

```bash
# Clone repository
git clone https://github.com/Perkybeet/wasm.git
cd wasm

# Create virtual environment
python -m venv venv
source venv/bin/activate

# Install development dependencies
pip install -e ".[dev]"

# Run tests
pytest

# Run linters
black src/
isort src/
ruff check src/
```

---

## License

This project is licensed under the **WASM Non-Commercial Source-Available License (WASM-NCSAL) Version 1.0**.

### Free Usage

You may use WASM free of charge for:
- Personal projects
- Educational purposes
- Research and development
- Non-commercial use

### Commercial Usage

Commercial use requires a license. This includes:
- Use within commercial organizations
- Providing paid services using WASM
- Reducing operational costs in business environments
- Any revenue-generating use case

### Obtain Commercial License

For commercial licensing inquiries:

- **Email**: yago.lopez.adeje@gmail.com, hello@bitbeet.dev
- **Phone**: +34 637 881 066
- **Web**: [bitbeet.dev](https://bitbeet.dev)

**[Read full license terms](LICENSE)**

---

## Acknowledgments

- [Certbot](https://certbot.eff.org/) - SSL certificate automation
- [python-inquirer](https://github.com/magmax/python-inquirer) - Interactive CLI
- The open-source community

---

## Support

- **Documentation**: [GitHub Wiki](https://github.com/Perkybeet/wasm/wiki)
- **Issues**: [GitHub Issues](https://github.com/Perkybeet/wasm/issues)
- **Email**: yago.lopez.adeje@gmail.com

---

<p align="center">
  Developed by <a href="https://bitbeet.dev">Bitbeet</a>
</p>
