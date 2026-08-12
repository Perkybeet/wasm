# WASM - Context for AI Assistants

Python 3.10+ CLI for deploying web apps on Linux servers. Automates Nginx/Apache, SSL,
systemd, databases and backups, and serves an optional control panel.

**Repository**: https://github.com/Perkybeet/wasm | **License**: WASM-NCSAL 1.0

---

## The four rules

These are not style preferences. Each one exists because its absence produced a specific
class of defect that shipped to users. Breaking one is how the project regresses.

### 1. Nothing runs a process except `CommandRunner`

`src/wasm/core/runner.py` is the only module allowed to import `subprocess`. Everything
that calls nginx, systemctl, certbot, git, npm or a database client goes through it.

```python
result = self.runner.run(["systemctl", "restart", unit], timeout=30, check=True)
self.runner.stream(["npm", "install"], on_line=logger.substep, timeout=900)
self.runner.capture_to_file(["pg_dump", db], destination, compress=True)
```

Argv only, never a shell. Timeouts are mandatory. Secrets go through `env=` or `input=`,
never argv, because everything on a command line is visible in `ps` to every local user.

`tests/conftest.py` makes real process execution fail in every test, so code that bypasses
the runner cannot be tested and fails loudly instead. `tests/test_architecture.py` enforces
the import rule.

`--dry-run` is implemented here too, as `DryRunRunner`. It is not wired per command: that is
what left the flag honoured in three code paths and silently ignored in every destructive
one.

### 2. `except Exception` is only allowed at an error boundary, and must log

There were 302 of them, 149 silent. That is the mechanism by which five calls to methods
that did not exist shipped for entire releases: every `AttributeError` became a cosmetic
warning. Catch the specific exception. If you genuinely need a broad catch, it belongs in
the CLI or API error boundary and it logs.

mypy is the guard for this class of bug and it blocks CI.

### 3. There is one implementation of each thing

The version lived in six hand-synchronised files, app-type detection had four
implementations with contradicting precedence, and the web API was a second implementation
of the whole product. When you find yourself writing something that already exists, use the
existing one or move it somewhere both callers can reach.

The web layer in particular is a **client** of the managers, never a parallel
implementation. An endpoint translates HTTP to a manager call and back.

### 4. Guards go at the chokepoint

A rule enforced in the caller is a rule with as many holes as there are callers. Unit
ownership is checked in `ServiceManager`, not in the endpoints that use it. Read-only SQL is
enforced by the database server, not by a keyword allowlist. Escaping is done by the
template engine, not by remembering.

---

## Architecture

```
src/wasm/
  core/
    runner.py       the only place processes are executed
    store.py        SQLite persistence with versioned migrations
    config.py       layered config; secrets written 0600, redacted on the way out
    exceptions.py   WASMError hierarchy, used for real
  validators/       names, environments, sources, ports, domains
  managers/         adapters: web server, systemd, certs, backups, databases, source
  deployers/        strategies over a declarative pipeline
  web/
    api/            thin layer over the managers
    views/          server-rendered pages
    templates/      Jinja, autoescaped
    static/vendor/  third-party assets, pinned by SHA-256
  cli/              argparse tree; handlers hold no business logic
```

### Adding a deployer

1. Create `deployers/mytype.py` implementing the deployer interface.
2. Set `APP_TYPE`, `DISPLAY_NAME`, `DETECTION_FILES`, `DEFAULT_PORT`.
3. Implement `detect()`, `get_install_command()`, `get_build_command()`, `get_start_command()`.
4. Register with `DeployerRegistry.register(MyTypeDeployer)` at the end of the file.
5. Add a `detect()` test with a fake file tree, including the ambiguous cases.

---

## Conventions

- Service names: `wasm-{domain}`. App directories: `/var/www/apps/{app_name}/`.
- Google-style docstrings on everything public, with Args/Returns/Raises.
- Type hints everywhere, modern syntax (`X | None`, `list[str]`).
- Actionable errors: `raise DeploymentError("what happened", details="how to fix it")`.
- Comments explain **why**, never what.
- No emojis in code, comments or commit messages. No AI assistant references in commits.
- Absolute paths in systemd units; `shutil.which()` or `/usr/bin/`, never nvm paths.
- WASM requires root. There is no `sudo` inside argv and no `run_command_sudo`.

---

## Commands

```bash
pip install -e ".[all,dev]"     # development install

pytest                          # tests
pytest --cov=wasm               # with coverage
ruff check src/wasm tests       # lint (blocking in CI)
ruff format src/wasm tests      # format (blocking in CI)
mypy                            # types (blocking in CI)

python scripts/release.py --check         # version consistency
python scripts/vendor_assets.py --check   # vendored asset checksums
```

---

## Releasing

The version has one source of truth: `[project].version` in `pyproject.toml`.

```bash
python scripts/release.py 1.0.0 -m "Summary of the change"
git commit -am "v1.0.0: Summary"
git tag -a v1.0.0 -m "Release v1.0.0"
git push && git push origin v1.0.0
```

`scripts/release.py` propagates to `setup.py`, `src/wasm/__init__.py`, `rpm/wasm.spec`,
`obs/wasm.dsc` and both changelogs. CI runs `--check` and refuses to publish on a mismatch.
Do not edit those files by hand.

GitHub Actions publishes to PyPI and OBS on tag push.
**OBS builds**: https://build.opensuse.org/package/show/home:Perkybeet/wasm (15-30 min)

---

## Packaging notes

The OBS tarball is produced with `git archive HEAD`, so **only committed files ship** and
the build environment has no network. That is why the panel has no build step and why every
third-party asset is vendored into `src/wasm/web/static/vendor/` with a checksum.

When adding a dependency, declare it in all four places: `pyproject.toml`, `setup.py`,
`obs/debian.control` and `rpm/wasm.spec`. `tests/test_architecture.py` fails if an import is
undeclared. Check the package exists on every target: `python3-inquirer` does not exist in
Debian or Ubuntu, which is why interactive mode never worked there.

| Import | Debian | RPM |
|--------|--------|-----|
| `jinja2` | `python3-jinja2` | `python3-jinja2` |
| `yaml` | `python3-yaml` | `python3-pyyaml` |
| `fastapi` | `python3-fastapi` | `python3-fastapi` |
| `starlette` | `python3-starlette` | `python3-starlette` |
| `pydantic` | `python3-pydantic` | `python3-pydantic` |
| `uvicorn` | `python3-uvicorn` | `python3-uvicorn` |
| `psutil` | `python3-psutil` | `python3-psutil` |
| `jose` | `python3-jose` | `python3-jose` (openSUSE: `python3-python-jose`) |

---

## The panel

Server-rendered HTML updated by htmx, with Alpine for small islands and xterm.js for the log
drawer. No build step, no Node, no CDN. This is the architecture Coolify uses, translated to
Python.

Colour only ever encodes state: navigation, surfaces and text are achromatic, so anything
coloured on screen is telling the operator something. The design direction is recorded in
`docs/superpowers/specs/2026-08-12-wasm-panel-design-direction.md`.

A system error is never paraphrased. Show nginx's or systemd's own output verbatim in mono,
with the suggested fix above it.
