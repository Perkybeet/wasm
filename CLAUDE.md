# WASM - Context for AI Assistants

## Project Overview

**WASM (Web App System Management)** is a CLI tool for deploying and managing web applications on Linux servers. It automates deployment, Nginx/Apache configuration, SSL certificates, systemd services, and database management.

- **License**: WASM-NCSAL 1.0 (Non-commercial, source-available)
- **Language**: Python 3.10+
- **Main Branch**: `main`
- **Repository**: https://github.com/Perkybeet/wasm

## Version Management

When creating a new release (patch, minor, or major), the version number **MUST** be updated in all the following files:

### Version Files (CRITICAL)
1. `src/wasm/__init__.py` - Line 11: `__version__ = "X.Y.Z"`
2. `setup.py` - Line 12: `version="X.Y.Z"`
3. `pyproject.toml` - Line 7: `version = "X.Y.Z"`

### Release Checklist

When creating a new version:

1. **Update version in all 3 files listed above**
2. **Create git commit** with descriptive message
3. **Create annotated git tag**: `git tag -a vX.Y.Z -m "Release vX.Y.Z"`
4. **Push commit and tag**: `git push && git push origin vX.Y.Z`
5. GitHub Actions will automatically:
   - Publish to PyPI
   - Deploy to OpenBuildService (OBS)
   - Create GitHub Release

## Code Style & Conventions

- **No AI references**: Never include "Claude", "Copilot", or AI assistant references in commits
- **No emojis**: Avoid emojis in code unless explicitly requested
- **Minimal comments**: Only comment non-obvious logic
- **No over-engineering**: Keep solutions simple and focused
- **Type hints**: Use where beneficial, not mandatory everywhere

## Testing

- Test suite: `pytest`
- Run tests: `pytest tests/`
- Coverage: `pytest --cov=src/wasm tests/`

## Important Notes

- The project uses **setuptools** (not Poetry) for compatibility with Ubuntu 22.04 LTS
- Environment variables from `.env` files should have quotes stripped before passing to systemd
- Update checker verifies GitHub Releases API every 1 hour, not 24 hours
- All services use the prefix `wasm-` (e.g., `wasm-example-com`)

## Common Commands

```bash
# Install dev dependencies
pip install -e ".[dev]"

# Run tests
pytest

# Check for version consistency
grep -r "0\.13\." --include="*.py" --include="*.toml" src/ setup.py pyproject.toml

# Create release
make release VERSION=0.13.12

# Build package
python -m build

# Upload to PyPI (done automatically by GitHub Actions)
twine upload dist/*
```

## Project Structure

```
wasm/
├── src/wasm/
│   ├── __init__.py           # Version definition
│   ├── main.py               # CLI entry point
│   ├── cli/                  # CLI commands and parser
│   ├── core/                 # Core utilities
│   ├── deployers/            # App type deployers (nextjs, nodejs, etc)
│   ├── managers/             # System managers (nginx, apache, service, etc)
│   ├── templates/            # Jinja2 templates for configs
│   └── validators/           # Input validators
├── tests/                    # Test suite
├── setup.py                  # Setup config (Jammy compatibility)
├── pyproject.toml            # Modern Python project config
└── README.md                 # Public documentation
```

## Deployment Flow

1. User runs: `wasm create -d example.com -s git@github.com:user/repo.git -t nextjs`
2. WASM:
   - Validates inputs
   - Clones repository to `/var/www/apps/wasm-example-com/`
   - Detects app type (or uses specified type)
   - Installs dependencies
   - Builds application
   - Creates systemd service
   - Configures Nginx/Apache
   - Obtains SSL certificate (optional)
   - Starts service

## Key Design Decisions

- **No Docker**: Direct deployment to systemd for better performance
- **Nginx/Apache**: Reverse proxy to app running on localhost
- **Systemd**: Service management for reliability and auto-restart
- **SQLite store**: Tracks deployed apps, services, and configurations
- **Update checker**: Post-command, non-blocking, cached (1 hour)
