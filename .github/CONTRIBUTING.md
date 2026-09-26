# Contributing to WASM (Web App System Management)

Thank you for your interest in contributing to WASM! We welcome contributions from the community and are grateful for any help you can provide.

## 📋 Project Overview

WASM is a robust Python CLI tool for managing web applications, sites, services, and SSL certificates on Linux servers. It supports both Nginx and Apache, with automated deployment workflows for various application types.

## 📜 License Notice

WASM is licensed under the **GNU Affero General Public License, version 3 or later** (from
2.1.0). By contributing, you agree that:

1. Your contribution is licensed under the AGPL-3.0-or-later, like the rest of the project.
2. You have the right to submit it.
3. You also grant the author (Yago López Prado) a perpetual, irrevocable licence to use your
   contribution under other terms as well. This keeps it possible to offer WASM under a
   separate commercial licence to those who need one, alongside the AGPL, which stays.

For a licence on other terms, write to yago.lopez.adeje@gmail.com.

## 🏗️ Architecture

```
wasm/
├── src/
│   └── wasm/
│       ├── __init__.py
│       ├── main.py                 # Entry point & CLI router
│       ├── cli/
│       │   ├── __init__.py
│       │   ├── parser.py           # Argument parser configuration
│       │   ├── interactive.py      # Inquirer-based guided mode
│       │   └── commands/
│       │       ├── __init__.py
│       │       ├── webapp.py       # Web app deployment commands
│       │       ├── site.py         # Site management commands
│       │       ├── service.py      # Service management commands
│       │       └── cert.py         # Certificate management commands
│       │
│       ├── core/
│       │   ├── __init__.py
│       │   ├── config.py           # Global configuration & paths
│       │   ├── logger.py           # Logging system with verbose support
│       │   ├── exceptions.py       # Custom exceptions
│       │   └── utils.py            # Common utilities
│       │
│       ├── managers/
│       │   ├── __init__.py
│       │   ├── site_manager.py     # Site creation/management base
│       │   ├── nginx_manager.py    # Nginx-specific operations
│       │   ├── apache_manager.py   # Apache-specific operations
│       │   ├── service_manager.py  # Systemd service management
│       │   ├── cert_manager.py     # Certbot/SSL management
│       │   └── source_manager.py   # Git/URL source handling
│       │
│       ├── deployers/
│       │   ├── __init__.py
│       │   ├── base.py             # Abstract base deployer
│       │   ├── nextjs.py           # Next.js deployment workflow
│       │   ├── nodejs.py           # Node.js deployment workflow
│       │   ├── vite.py             # Vite deployment workflow
│       │   ├── python.py           # Python/Django/Flask workflow
│       │   └── static.py           # Static site deployment
│       │
│       ├── templates/
│       │   ├── __init__.py
│       │   ├── nginx/
│       │   │   ├── nextjs.conf.j2
│       │   │   ├── nodejs.conf.j2
│       │   │   ├── vite.conf.j2
│       │   │   ├── static.conf.j2
│       │   │   └── proxy.conf.j2
│       │   ├── apache/
│       │   │   ├── nextjs.conf.j2
│       │   │   ├── nodejs.conf.j2
│       │   │   └── ...
│       │   └── systemd/
│       │       ├── nextjs.service.j2
│       │       ├── nodejs.service.j2
│       │       └── ...
│       │
│       └── validators/
│           ├── __init__.py
│           ├── domain.py           # Domain validation
│           ├── port.py             # Port validation & availability
│           └── source.py           # Source URL validation
│
├── tests/
│   ├── __init__.py
│   ├── conftest.py
│   ├── test_cli/
│   ├── test_managers/
│   └── test_deployers/
│
├── debian/                         # Debian packaging files
│   ├── control
│   ├── rules
│   ├── changelog
│   ├── copyright
│   ├── compat
│   └── wasm.install
│
├── docs/
│   ├── installation.md
│   ├── usage.md
│   ├── deployers.md
│   └── configuration.md
│
├── pyproject.toml
├── setup.py
├── README.md
├── LICENSE
└── Makefile                        # Build automation
```

## 🔧 Development Setup

### Prerequisites

- Python 3.10+
- pip & virtualenv
- Git
- For testing: Docker (optional)

### Installation for Development

```bash
# Clone the repository
git clone https://github.com/Perkybeet/wasm.git
cd wasm

# Create virtual environment
python3 -m venv venv
source venv/bin/activate

# Install in development mode
pip install -e ".[dev]"

# Verify installation
wasm --version
```

## 📐 Coding Standards

### Python Style

- Follow **PEP 8** strictly
- Use **type hints** for all function signatures
- Maximum line length: **100 characters**
- Use **f-strings** for string formatting
- Docstrings: Google style format

### Example Function

```python
from typing import Optional
from pathlib import Path

from wasm.core.logger import Logger
from wasm.core.exceptions import WASMError


def deploy_application(
    source: str,
    domain: str,
    app_type: str,
    port: int,
    *,
    ssl: bool = True,
    verbose: bool = False
) -> bool:
    """
    Deploy a web application to the server.

    Args:
        source: Git URL or path to the application source.
        domain: Target domain for the application.
        app_type: Type of application (nextjs, nodejs, vite, etc.).
        port: Port number for the application.
        ssl: Whether to configure SSL certificate. Defaults to True.
        verbose: Enable verbose logging. Defaults to False.

    Returns:
        True if deployment was successful, False otherwise.

    Raises:
        WASMError: If deployment fails at any step.
        ValueError: If invalid parameters are provided.
    """
    logger = Logger(verbose=verbose)
    logger.info(f"Starting deployment for {domain}")
    # Implementation...
```

### Naming Conventions

| Type | Convention | Example |
|------|------------|---------|
| Modules | snake_case | `site_manager.py` |
| Classes | PascalCase | `NginxManager` |
| Functions | snake_case | `create_site()` |
| Constants | UPPER_SNAKE | `DEFAULT_PORT` |
| Private | _prefix | `_validate_input()` |

## 🚀 Adding a New Deployer

When adding support for a new application type:

### 1. Create the Deployer Class

```python
# src/wasm/deployers/myapp.py

from wasm.deployers.base import BaseDeployer
from wasm.core.config import Config


class MyAppDeployer(BaseDeployer):
    """Deployer for MyApp applications."""
    
    APP_TYPE = "myapp"
    DISPLAY_NAME = "MyApp Framework"
    
    # Required dependencies
    SYSTEM_DEPS = ["nodejs", "npm"]
    
    def validate_source(self) -> bool:
        """Validate the source has required MyApp files."""
        required_files = ["package.json", "myapp.config.js"]
        return self._check_required_files(required_files)
    
    def install_dependencies(self) -> bool:
        """Install application dependencies."""
        return self._run_command("npm install")
    
    def build(self) -> bool:
        """Build the application for production."""
        return self._run_command("npm run build")
    
    def get_start_command(self) -> str:
        """Return the command to start the application."""
        return "npm run start"
    
    def get_nginx_template(self) -> str:
        """Return the Nginx template name for this app type."""
        return "myapp.conf.j2"
    
    def get_health_check_path(self) -> str:
        """Return the health check endpoint path."""
        return "/api/health"
```

### 2. Register the Deployer

```python
# src/wasm/deployers/__init__.py

from wasm.deployers.myapp import MyAppDeployer

DEPLOYERS = {
    # ... existing deployers
    "myapp": MyAppDeployer,
}
```

### 3. Create Templates

Create the necessary templates in `src/wasm/templates/`:

- `nginx/myapp.conf.j2`
- `apache/myapp.conf.j2` (if applicable)
- `systemd/myapp.service.j2`

### 4. Add Tests

```python
# tests/test_deployers/test_myapp.py

import pytest
from wasm.deployers.myapp import MyAppDeployer


class TestMyAppDeployer:
    def test_app_type(self):
        deployer = MyAppDeployer(config)
        assert deployer.APP_TYPE == "myapp"
    
    def test_validate_source_success(self, mock_source):
        # ...
```

## 📝 CLI Command Guidelines

### Command Structure

```
wasm <resource> <action> [options]
```

**Resources:** `webapp`, `site`, `service`, `cert`  
**Actions:** `create`, `delete`, `list`, `status`, `restart`, etc.

### Adding a New Command

1. Add parser arguments in `cli/parser.py`
2. Create command handler in `cli/commands/`
3. Add interactive prompts in `cli/interactive.py`
4. Update help text and documentation

### Example Command Handler

```python
# src/wasm/cli/commands/webapp.py

from wasm.core.logger import Logger
from wasm.deployers import get_deployer


def handle_webapp_create(args) -> int:
    """
    Handle 'wasm webapp create' command.
    
    Returns:
        Exit code (0 for success, non-zero for failure).
    """
    logger = Logger(verbose=args.verbose)
    
    try:
        logger.step("Initializing deployment")
        
        deployer = get_deployer(args.type)
        deployer.configure(
            domain=args.domain,
            source=args.source,
            port=args.port,
        )
        
        logger.step("Fetching source code")
        deployer.fetch_source()
        
        logger.step("Installing dependencies")
        deployer.install_dependencies()
        
        logger.step("Building application")
        deployer.build()
        
        logger.step("Creating site configuration")
        deployer.create_site()
        
        logger.step("Creating systemd service")
        deployer.create_service()
        
        logger.step("Starting application")
        deployer.start()
        
        logger.success(f"✓ Application deployed at https://{args.domain}")
        return 0
        
    except Exception as e:
        logger.error(f"Deployment failed: {e}")
        return 1
```

## 🧪 Testing

### Running Tests

```bash
# Run all tests
pytest

# Run with coverage
pytest --cov=wasm --cov-report=html

# Run specific test file
pytest tests/test_deployers/test_nextjs.py

# Run with verbose output
pytest -v
```

### Test Categories

- **Unit tests:** Test individual functions and classes
- **Integration tests:** Test component interactions
- **E2E tests:** Full deployment workflows (require sudo/docker)

## 📦 Building the .deb Package

### Prerequisites

```bash
sudo apt install devscripts debhelper dh-python python3-all
```

### Build Process

```bash
# Update version in pyproject.toml and debian/changelog
make version VERSION=X.Y.Z

# Build the package
make deb

# The .deb file will be in ../wasm_X.Y.Z_all.deb
```

### Testing the Package

```bash
# Install locally (replace X.Y.Z with actual version)
sudo dpkg -i ../wasm_X.Y.Z_all.deb

# Test installation
wasm --version

# Remove
sudo apt remove wasm
```

## 🔄 Git Workflow

### Branch Naming

- `feature/` - New features
- `fix/` - Bug fixes
- `docs/` - Documentation
- `refactor/` - Code refactoring
- `release/` - Release preparation

### Commit Messages

Follow [Conventional Commits](https://www.conventionalcommits.org/):

```
type(scope): description

[optional body]

[optional footer]
```

**Types:** `feat`, `fix`, `docs`, `style`, `refactor`, `test`, `chore`

**Examples:**
```
feat(deployers): add support for Vite applications
fix(nginx): correct proxy headers in configuration
docs(readme): update installation instructions
```

### Pull Request Process

1. Create feature branch from `main`
2. Make changes and add tests
3. Ensure all tests pass
4. Update documentation if needed
5. Submit PR with clear description
6. Request review
7. Squash and merge after approval

## 📊 Logging Guidelines

### Log Levels

| Level | Method | Use Case |
|-------|--------|----------|
| DEBUG | `logger.debug()` | Detailed info (--verbose only) |
| INFO | `logger.info()` | General progress |
| STEP | `logger.step()` | Major workflow steps |
| SUCCESS | `logger.success()` | Successful operations |
| WARNING | `logger.warning()` | Non-critical issues |
| ERROR | `logger.error()` | Failures |

### Output Format

**Normal mode:**
```
[1/6] Fetching source code...
[2/6] Installing dependencies...
[3/6] Building application...
✓ Application deployed successfully
```

**Verbose mode:**
```
[1/6] Fetching source code...
      → Cloning from git@github.com:user/repo.git
      → Target directory: /var/www/apps/myapp
      → Clone completed in 3.2s
[2/6] Installing dependencies...
      → Running: npm install
      → Found 156 packages
      → Installation completed in 12.5s
...
```

## 🔐 Security Considerations

- Never log sensitive data (passwords, tokens, keys)
- Validate all user inputs
- Use secure defaults
- Sanitize paths to prevent directory traversal
- Run with minimal required privileges

## 📚 Documentation

- Keep README.md up to date
- Document all public functions
- Add examples for new features
- Update man pages for CLI changes

## ❓ Getting Help

- Open an issue for bugs or features
- Use discussions for questions
- Check existing issues before creating new ones

## 📬 Contact

For questions about contributions, licensing, or commercial use:

- **Author:** Yago López Prado
- **Email:** yago.lopez.adeje@gmail.com | hello@bitbeet.dev
- **Phone:** +34 637 881 066
- **Website:** [bitbeet.dev](https://bitbeet.dev)

---

Thank you for contributing to WASM! 🙏
