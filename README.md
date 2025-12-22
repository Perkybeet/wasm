# WASM - Web App System Management

<p align="center">
  <img src="docs/assets/logo_bg.png" alt="WASM Logo" width="400">
</p>

<p align="center">
  <strong>Deploy, manage, and monitor web applications with ease</strong>
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
  <a href="https://github.com/Perkybeet/wasm/issues">
    <img src="https://img.shields.io/github/issues/Perkybeet/wasm" alt="GitHub Issues">
  </a>
</p>

<p align="center">
  <a href="#installation">Installation</a> •
  <a href="#quick-start">Quick Start</a> •
  <a href="#features">Features</a> •
  <a href="#documentation">Documentation</a>
</p>

---

## 🚀 What is WASM?

**WASM (Web App System Management)** is a powerful CLI tool designed to simplify the deployment and management of web applications on Linux servers. It automates the entire process from cloning your code to serving it with Nginx/Apache, including SSL certificates and systemd services.

### Key Capabilities

- 🌐 **Site Management** - Create and manage Nginx/Apache virtual hosts
- 🔒 **SSL Certificates** - Automated Let's Encrypt certificates via Certbot
- ⚙️ **Service Management** - Create and control systemd services
- 🚀 **One-Command Deployment** - Deploy full-stack applications instantly
- 🎯 **Multi-Framework Support** - Next.js, Node.js, Vite, Python, and more
- 🧭 **Interactive Mode** - Guided step-by-step deployment wizard

---

## 📦 Installation

### Ubuntu/Debian - From OBS Repository (Recommended)

```bash
# Add repository key
curl -fsSL https://download.opensuse.org/repositories/home:/Perkybeet/xUbuntu_24.04/Release.key | \
  gpg --dearmor | sudo tee /usr/share/keyrings/wasm.gpg > /dev/null

# Add repository (Ubuntu 24.04)
echo 'deb [signed-by=/usr/share/keyrings/wasm.gpg] https://download.opensuse.org/repositories/home:/Perkybeet/xUbuntu_24.04/ /' | \
  sudo tee /etc/apt/sources.list.d/wasm.list

# Install
sudo apt update
sudo apt install wasm
```

Supported Ubuntu versions:
- Ubuntu 22.04 LTS (Jammy Jellyfish)
- Ubuntu 24.04 LTS (Noble Numbat)

### Fedora - From OBS Repository

```bash
# Add repository
sudo dnf config-manager --add-repo \
  https://download.opensuse.org/repositories/home:/Perkybeet/Fedora_40/home:Perkybeet.repo

# Install
sudo dnf install wasm-cli
```

### openSUSE - From OBS Repository

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

### Debian - From OBS Repository

```bash
# Add repository key
curl -fsSL https://download.opensuse.org/repositories/home:/Perkybeet/Debian_12/Release.key | \
  gpg --dearmor | sudo tee /etc/apt/trusted.gpg.d/home_Perkybeet.gpg > /dev/null

# Add repository
echo 'deb https://download.opensuse.org/repositories/home:/Perkybeet/Debian_12/ /' | \
  sudo tee /etc/apt/sources.list.d/home_Perkybeet.list

# Install
sudo apt update
sudo apt install wasm-cli
```

### From .deb Package (GitHub Release)

Download the latest `.deb` from the [GitHub Releases](https://github.com/Perkybeet/wasm/releases/latest) page:

```bash
# Download latest version (check releases page for current version)
VERSION=$(curl -s https://api.github.com/repos/Perkybeet/wasm/releases/latest | grep -oP '"tag_name": "v\K[^"]+')
wget "https://github.com/Perkybeet/wasm/releases/latest/download/wasm_${VERSION}_all.deb"
sudo dpkg -i "wasm_${VERSION}_all.deb"
sudo apt install -f  # Install dependencies if needed
```

Or manually download from: **https://github.com/Perkybeet/wasm/releases/latest**

### From PyPI

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

## 🏃 Quick Start

### Deploy a Next.js Application

```bash
# One-liner deployment
wasm webapp create \
  --domain myapp.example.com \
  --source git@github.com:user/my-nextjs-app.git \
  --type nextjs \
  --port 3000

# Short version
wasm wp create -d myapp.example.com -s git@github.com:user/app.git -t nextjs -p 3000
```

### Interactive Mode

For a guided experience, simply run:

```bash
wasm
```

Or explicitly:

```bash
wasm --interactive
```

You'll be guided through all the options step by step:

```
┌─────────────────────────────────────────────────┐
│           WASM - Web App System Management      │
└─────────────────────────────────────────────────┘

? What would you like to do?
❯ 🚀 Deploy a Web Application
  🌐 Manage Sites (Nginx/Apache)
  ⚙️  Manage Services
  🔒 Manage SSL Certificates
  📊 View Status Dashboard
  ⚡ Exit

? Select application type:
❯ Next.js
  Node.js (Express, Fastify, etc.)
  Vite (React, Vue, Svelte)
  Python (Django, Flask, FastAPI)
  Static Site
  Custom

? Enter the domain name: myapp.example.com
? Enter the source (Git URL or path): git@github.com:user/app.git
? Enter the port number: 3000
? Configure SSL certificate? Yes

[1/7] 📥 Cloning repository...
[2/7] 📦 Installing dependencies...
[3/7] 🔨 Building application...
[4/7] 🌐 Creating Nginx configuration...
[5/7] 🔒 Obtaining SSL certificate...
[6/7] ⚙️  Creating systemd service...
[7/7] 🚀 Starting application...

✅ Deployment complete!

   URL: https://myapp.example.com
   Status: Running
   Service: wasm-myapp-example-com
```

---

## 📋 Features

### Web Application Deployment

```bash
# Create a new web application
wasm webapp create [options]

Options:
  -d, --domain DOMAIN     Target domain (e.g., example.com)
  -s, --source SOURCE     Git URL or local path to source code
  -t, --type TYPE         Application type (nextjs, nodejs, vite, python, static)
  -p, --port PORT         Application port (default: auto-assigned)
  -w, --webserver SERVER  Web server (nginx, apache) [default: nginx]
  --no-ssl                Skip SSL certificate configuration
  --branch BRANCH         Git branch to deploy [default: main]
  --env-file FILE         Path to .env file to use
  -v, --verbose           Enable verbose output

# List deployed applications
wasm webapp list

# Get application status
wasm webapp status myapp.example.com

# Restart application
wasm webapp restart myapp.example.com

# Remove application
wasm webapp delete myapp.example.com

# Update application (pull & rebuild)
wasm webapp update myapp.example.com
```

### Site Management

```bash
# Create a site (without deploying an app)
wasm site create -d example.com -w nginx

# List all sites
wasm site list

# Enable/disable site
wasm site enable example.com
wasm site disable example.com

# Delete site
wasm site delete example.com

# Show site configuration
wasm site show example.com
```

### Service Management

```bash
# Create a custom service
wasm service create --name myservice --command "/usr/bin/myapp" --user www-data

# List managed services
wasm service list

# Control services
wasm service start myservice
wasm service stop myservice
wasm service restart myservice

# View service status
wasm service status myservice

# View service logs
wasm service logs myservice
wasm service logs myservice --follow
wasm service logs myservice --lines 100

# Delete service
wasm service delete myservice
```

### SSL Certificate Management

```bash
# Obtain certificate for a domain
wasm cert create -d example.com

# List certificates
wasm cert list

# Renew all certificates
wasm cert renew

# Revoke a certificate
wasm cert revoke example.com

# Show certificate info
wasm cert info example.com
```

---

## 🎯 Supported Application Types

| Type | Framework | Auto-Detection |
|------|-----------|----------------|
| `nextjs` | Next.js | `next.config.js` |
| `nodejs` | Express, Fastify, Koa, etc. | `package.json` with start script |
| `vite` | React, Vue, Svelte (Vite) | `vite.config.js` |
| `python` | Django, Flask, FastAPI | `requirements.txt`, `pyproject.toml` |
| `static` | HTML/CSS/JS | `index.html` |

Each type has a specific deployment workflow that includes:
- Dependency installation
- Build process
- Environment configuration
- Service setup
- Health checks

---

## ⚙️ Configuration

### Global Configuration

Configuration file location: `/etc/wasm/config.yaml`

```yaml
# Default web server
webserver: nginx

# Default apps directory
apps_directory: /var/www/apps

# Default user for services
service_user: www-data

# SSL settings
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

### Per-Project Configuration

You can include a `.wasm.yaml` file in your project root:

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

## 📊 Verbose Mode

Add `--verbose` or `-v` for detailed output:

```bash
wasm webapp create -d example.com -s git@github.com:user/app.git -t nextjs -v
```

**Verbose output example:**

```
[1/7] 📥 Cloning repository...
      ├─ Source: git@github.com:user/app.git
      ├─ Branch: main
      ├─ Target: /var/www/apps/example-com
      └─ Completed in 4.2s

[2/7] 📦 Installing dependencies...
      ├─ Package manager: npm
      ├─ Command: npm ci --production=false
      ├─ Packages installed: 1,247
      └─ Completed in 45.3s

[3/7] 🔨 Building application...
      ├─ Command: npm run build
      ├─ Output directory: .next
      ├─ Build size: 12.4 MB
      └─ Completed in 32.1s
...
```

---

## 🗂️ Directory Structure

WASM organizes deployed applications as follows:

```
/var/www/apps/
├── example-com/
│   ├── current/          # Current deployment (symlink)
│   ├── releases/         # Previous releases (for rollback)
│   │   ├── 20241215120000/
│   │   └── 20241214150000/
│   ├── shared/           # Shared files (uploads, logs)
│   └── .env              # Environment variables
│
└── another-app/
    └── ...
```

---

## 🔧 Requirements

### System Requirements

- **OS:** Ubuntu 20.04+, Debian 11+
- **Python:** 3.10+
- **Privileges:** sudo access for service management

### Optional Dependencies

- **nginx** or **apache2** - Web server
- **certbot** - SSL certificates
- **git** - Source code management
- **nodejs** / **nvm** - For Node.js applications
- **python3-venv** - For Python applications

WASM will check and prompt for missing dependencies during installation.

---

## 🤝 Contributing

We welcome contributions! Please see our [Contributing Guide](.github/CONTRIBUTING.md) for details.

```bash
# Development setup
git clone https://github.com/Perkybeet/wasm.git
cd wasm
python -m venv venv
source venv/bin/activate
pip install -e ".[dev]"

# Run tests
pytest

# Build and upload to OBS (all distributions)
make obs-upload

# Check OBS build status
make obs-status
```

For detailed information about building and uploading to OBS, see [docs/OBS_SETUP.md](docs/OBS_SETUP.md).

---

## 📜 License

**WASM-NCSAL 1.0** - Free for personal and educational use.  
For commercial use or business environments, a commercial license is required.

This project is licensed under the **WASM Non-Commercial Source-Available License (WASM-NCSAL) Version 1.0**.

### ✅ You CAN (Free):
- Use for **personal projects** and **learning**
- Study, modify, and adapt the code
- Contribute improvements to the project
- Distribute copies (maintaining the license)

### ❌ You CANNOT (Requires License):
- Use in **commercial environments** (companies, startups, agencies)
- Use to **reduce business costs** or gain competitive advantages
- Sell or monetize the software or derivatives
- Provide paid services using this software

### 💼 Need a Commercial License?

If you're a business or want to use WASM commercially:

- 📧 **Email:** yago.lopez.adeje@gmail.com | hello@bitbeet.dev
- 📱 **Phone:** +34 637 881 066
- 🌐 **Web:** [bitbeet.dev](https://bitbeet.dev)

👉 **[Read full license terms](LICENSE)**

---

## 🙏 Acknowledgments

- [Certbot](https://certbot.eff.org/) for SSL automation
- [python-inquirer](https://github.com/magmax/python-inquirer) for interactive CLI
- The open-source community

---

<p align="center">
  Made with ❤️ by <a href="https://bitbeet.dev">Bitbeet</a>
</p>
