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
2. **Update OBS files** (see OBS Package Management section below)
3. **Create git commit** with descriptive message
4. **Create annotated git tag**: `git tag -a vX.Y.Z -m "Release vX.Y.Z"`
5. **Push commit and tag**: `git push && git push origin vX.Y.Z`
6. GitHub Actions will automatically:
   - Publish to PyPI
   - Deploy to OpenBuildService (OBS)
   - Create GitHub Release

## OBS Package Management

**CRITICAL**: OpenBuildService (OBS) packages require additional maintenance beyond version numbers.

### OBS-Specific Files (MUST update for each release)

1. **`rpm/wasm.spec`**:
   - Update `Version:` line (line 8)
   - Add changelog entry in `%changelog` section with release notes

2. **`obs/wasm.dsc`**:
   - Update `Version:` line (line 5)
   - Update tarball filename in `Files:` section (line 13)

3. **`obs/debian.changelog`**:
   - Add new version entry at the TOP of the file
   - Include all changes from the release
   - Use proper Debian changelog format

4. **`obs/debian.control`**:
   - Verify ALL Python dependencies are declared in `Depends:` section
   - **Common mistake**: Adding Python imports without declaring package dependencies

### Common OBS Build Failures

#### Missing Python Dependencies (v0.13.14 issue)
**Problem**: Build failed because `python3-inquirer` was imported in code but not declared in `obs/debian.control`.

**Symptoms**:
- Debian/Ubuntu builds fail on OBS
- Error: "ModuleNotFoundError: No module named 'inquirer'"
- RPM builds may succeed if dependency is in Requires section

**Prevention**:
- When adding Python imports, ALWAYS update `obs/debian.control`
- Cross-check imports with declared dependencies before each release
- Test builds locally with `dpkg-buildpackage` if possible

**Package name mapping**:
| Python import | Debian package | RPM package |
|---------------|---------------|-------------|
| `inquirer` | `python3-inquirer` | `python3-inquirer` |
| `jinja2` | `python3-jinja2` | `python3-jinja2` |
| `yaml` | `python3-yaml` | `python3-pyyaml` |
| `fastapi` | `python3-fastapi` | `python3-fastapi` |

### OBS Build Monitoring

- Monitor builds at: https://build.opensuse.org/package/show/home:Perkybeet/wasm
- Builds typically take 15-30 minutes
- If builds fail, check build logs for missing dependencies

### Pre-Release Verification

Before creating a tag, verify:
```bash
# Check all version files are consistent
grep -r "X\.Y\.Z" src/wasm/__init__.py setup.py pyproject.toml rpm/wasm.spec obs/wasm.dsc

# Verify debian.control dependencies match Python imports
grep "^import\|^from" -r src/wasm/ | grep -v __pycache__ | sort -u

# Check changelog entries exist for new version
head -20 obs/debian.changelog
head -30 rpm/wasm.spec | grep -A 20 "%changelog"
```

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
make release VERSION=X.Y.Z

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
