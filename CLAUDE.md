# WASM - Context for AI Assistants

Python 3.10+ CLI for deploying web apps on Linux servers. Automates Nginx/Apache, SSL, systemd, databases, and backups.

**Repository**: https://github.com/Perkybeet/wasm | **License**: WASM-NCSAL 1.0

---

## Version Management (CRITICAL)

**MUST update ALL 3 files for every release:**

| File | Line | Format |
|------|------|--------|
| `src/wasm/__init__.py` | 11 | `__version__ = "X.Y.Z"` |
| `setup.py` | 12 | `version="X.Y.Z"` |
| `pyproject.toml` | 7 | `version = "X.Y.Z"` |

**OBS packaging files (MUST update for releases):**

| File | What to Update |
|------|----------------|
| `rpm/wasm.spec` | `Version:` (line 8) + add `%changelog` entry |
| `obs/wasm.dsc` | `Version:` (line 5) + tarball filename in `Files:` |
| `obs/debian.changelog` | Add new entry at TOP (Debian format) |
| `obs/debian.control` | Verify Python deps match code imports |

### Python Package Mapping (for OBS builds)

| Import | Debian Package | RPM Package |
|--------|----------------|-------------|
| `inquirer` | `python3-inquirer` | `python3-inquirer` |
| `jinja2` | `python3-jinja2` | `python3-jinja2` |
| `yaml` | `python3-yaml` | `python3-pyyaml` |
| `fastapi` | `python3-fastapi` | `python3-fastapi` |
| `uvicorn` | `python3-uvicorn` | `python3-uvicorn` |
| `psutil` | `python3-psutil` | `python3-psutil` |

**Pre-release verification:**
```bash
grep -E "version.*=.*\"?[0-9]+\.[0-9]+\.[0-9]+" src/wasm/__init__.py setup.py pyproject.toml rpm/wasm.spec obs/wasm.dsc
```

---

## Architecture

### Design Patterns

| Pattern | Location | Purpose |
|---------|----------|---------|
| Strategy | `deployers/*.py` | Different deploy logic per app type |
| Template Method | `BaseDeployer.deploy()` | 7-step deployment workflow |
| Registry | `deployers/registry.py` | Auto-registration of deployers |
| DAO | `core/store.py` | SQLite persistence (Apps, Sites, Services) |
| Adapter | `managers/*.py` | Unified interface for nginx/apache/systemd |

### Key Abstractions

```
BaseDeployer (deployers/base.py)
  -> NextJSDeployer, NodeJSDeployer, ViteDeployer, PythonDeployer, StaticDeployer

BaseManager (managers/base_manager.py)
  -> NginxManager, ApacheManager, ServiceManager, CertManager, BackupManager

Store (core/store.py) - SQLite singleton
  -> Dataclasses: App, Site, Service, Cert, Backup, Database
```

### Adding a New Deployer

1. Create `deployers/mytype.py` inheriting `BaseDeployer`
2. Set class attributes: `APP_TYPE`, `DISPLAY_NAME`, `DETECTION_FILES`, `DEFAULT_PORT`
3. Implement: `detect()`, `get_install_command()`, `get_build_command()`, `get_start_command()`
4. Register: `DeployerRegistry.register(MyTypeDeployer)` at end of file

---

## Code Conventions

### Naming
- Service names: `wasm-{domain}` (e.g., `wasm-example-com`)
- App directories: `/var/www/apps/{app_name}/`
- Private functions: `_prefix`
- Constants: `UPPER_SNAKE_CASE`

### Docstrings (Google-style)
```python
def method(self, param: str) -> bool:
    """
    Brief description.

    Args:
        param: Description of parameter.

    Returns:
        True if successful.

    Raises:
        WASMError: When operation fails.
    """
```

### Exception Handling
- All exceptions inherit from `WASMError` (`core/exceptions.py`)
- Include actionable details: `raise DeploymentError("Message", details="how to fix")`
- Common: `DeploymentError`, `BuildError`, `NginxError`, `ServiceError`, `CertificateError`

### What NOT to Do
- No emojis in code or commits
- No AI assistant references in commits (Claude, Copilot, etc.)
- No over-commenting obvious code
- No bare `except:` - always catch specific exceptions
- No `type` as variable name (shadows builtin)
- No relative paths in systemd services (use absolute)

---

## Release Workflow

1. Update versions in all 3 files + OBS files
2. Verify: `grep -E "X\.Y\.Z" src/wasm/__init__.py setup.py pyproject.toml rpm/wasm.spec obs/wasm.dsc`
3. Run tests: `pytest tests/`
4. Commit: `git commit -m "vX.Y.Z: Summary of changes"`
5. Tag: `git tag -a vX.Y.Z -m "Release vX.Y.Z"`
6. Push: `git push && git push origin vX.Y.Z`

GitHub Actions auto-publishes to PyPI and OBS on tag push.

**OBS Build Monitoring**: https://build.opensuse.org/package/show/home:Perkybeet/wasm (builds take 15-30 min)

---

## Testing

```bash
pytest tests/                        # Run all tests
pytest --cov=src/wasm tests/         # With coverage
pytest tests/test_store.py -v        # Specific file
```

Test files: `tests/test_{module}.py`

---

## Common Mistakes to Avoid

### OBS Build Failures
**Problem**: Python import added but not declared in `obs/debian.control`
```python
import inquirer  # Requires python3-inquirer in Depends/Recommends
```
**Fix**: Always update `obs/debian.control` when adding imports

### Service Path Issues
**Problem**: Relative or nvm paths fail in systemd
```python
# Wrong: "node server.js" or "~/.nvm/versions/node/.../node"
# Right: "/usr/bin/node server.js"
```
**Fix**: Use `shutil.which()` or hardcode `/usr/bin/` paths

### .env Quotes in Systemd
**Problem**: Quoted values cause systemd `Environment=` failures
```bash
# Wrong in .env: DATABASE_URL="postgres://..."
# Right: DATABASE_URL=postgres://...
```
**Fix**: `core/utils.py` strips quotes when reading .env files

---

## Quick Reference

```bash
# Development install
pip install -e ".[dev]"

# Linting
ruff check src/wasm/
black --check src/wasm/

# Type checking
mypy src/wasm/

# Build package
python -m build

# Check imports vs debian.control deps
grep -rh "^import\|^from" src/wasm/ | grep -v __pycache__ | cut -d' ' -f2 | cut -d'.' -f1 | sort -u
```
