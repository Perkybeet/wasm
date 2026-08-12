"""
Structural invariants.

Ordinary tests check what the code does. These check what it is allowed to
look like, because the defects that hurt this project most were not logic
errors in one function: they were patterns repeated across the tree. A blind
except in three hundred places is what let five calls to methods that do not
exist ship for entire releases. Direct subprocess calls in sixteen files are
what made every manager untestable.

Each guard here carries an explicit list of the places that still violate it.
The test fails both when a new violation appears and when a listed file stops
violating the rule, so the list can only shrink and cannot quietly go stale.
"""

from __future__ import annotations

import ast
import importlib
import pkgutil
import re
from pathlib import Path

import pytest

import wasm

SRC = Path(wasm.__file__).resolve().parent
REPO = SRC.parent.parent


def python_files() -> list[Path]:
    """
    Return every Python source file in the package.

    Returns:
        Paths, sorted for stable failure messages.
    """
    return sorted(p for p in SRC.rglob("*.py") if "__pycache__" not in p.parts)


def relative(path: Path) -> str:
    """
    Return a path relative to the repository root.

    Args:
        path: Absolute path inside the repository.

    Returns:
        A short path suitable for a failure message.
    """
    return str(path.relative_to(REPO))


def check_ratchet(violations: set[str], known: set[str], rule: str) -> None:
    """
    Compare violations against the list of known offenders.

    Args:
        violations: Files that currently break the rule.
        known: Files recorded as breaking it.
        rule: What the rule is, for the failure message.

    Raises:
        AssertionError: When a new violation appears, or when a recorded one
            has been fixed without updating the list.
    """
    added = violations - known
    assert not added, (
        f"New violations of '{rule}':\n"
        + "\n".join(f"  {name}" for name in sorted(added))
        + "\n\nFix them, or add them to the list in tests/test_architecture.py "
        "with a reason."
    )

    fixed = known - violations
    assert not fixed, (
        f"These files no longer violate '{rule}':\n"
        + "\n".join(f"  {name}" for name in sorted(fixed))
        + "\n\nRemove them from the list so it keeps shrinking."
    )


class TestExecutionSeam:
    """
    Everything that runs a process goes through the runner.

    Without a single seam there is nothing to inject in tests, which is why
    the deployers and managers sat at zero coverage while shipping bugs.
    """

    #: Only the seam itself may import subprocess. The rest of this list is
    #: what remains of the sixteen files that used to bypass it, and it is
    #: expected to reach one entry.
    SUBPROCESS_ALLOWED = {
        "src/wasm/core/runner.py",
        "src/wasm/cli/commands/db.py",
        "src/wasm/cli/commands/service.py",
        "src/wasm/core/update_checker.py",
    }

    def test_nothing_else_imports_subprocess(self):
        offenders = set()
        for path in python_files():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    names = [node.module or ""]
                else:
                    continue
                if any(name == "subprocess" or name.startswith("subprocess.") for name in names):
                    offenders.add(relative(path))

        check_ratchet(offenders, self.SUBPROCESS_ALLOWED, "only the runner imports subprocess")

    def test_no_shell_execution_anywhere(self):
        """
        A shell is never involved.

        Every injection this project had came from a value being reinterpreted
        as shell syntax: a database dump inside 'bash -c \"echo ...\"', a
        domain name in an install script, an environment value in a unit file.
        """
        offenders = set()
        for path in python_files():
            source = path.read_text(encoding="utf-8")
            if "shell=True" in source or "os.system(" in source:
                offenders.add(relative(path))

        assert not offenders, (
            "These files execute through a shell:\n"
            + "\n".join(f"  {name}" for name in sorted(offenders))
            + "\n\nBuild an argv and use CommandRunner instead."
        )


class TestTemplateSafety:
    """Templates escape by default, everywhere."""

    def test_every_jinja_environment_autoescapes(self):
        """
        A Jinja Environment defaults to autoescape=False.

        The panel rendered server data into pages that hold root over the
        machine, so an environment created without autoescaping is an XSS
        waiting for a hostile domain name or an error message.
        """
        offenders = set()
        for path in python_files():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
                if name != "Environment":
                    continue
                if not any(kw.arg == "autoescape" for kw in node.keywords):
                    offenders.add(f"{relative(path)}:{node.lineno}")

        assert not offenders, (
            "These Jinja environments do not set autoescape:\n"
            + "\n".join(f"  {name}" for name in sorted(offenders))
            + "\n\nPass autoescape=select_autoescape(...)."
        )


class TestSelfContained:
    """The panel works on a machine with no route to the internet."""

    #: The panel that is being replaced. Removed once the new one is wired in.
    CDN_ALLOWED = {
        "src/wasm/web/static/index.html",
        "src/wasm/web/static/login.html",
    }

    def test_no_external_assets(self):
        """
        Nothing the panel serves is fetched from a third party.

        A control panel with root over the machine used to load Tailwind and
        Font Awesome from public CDNs, so it could not render without internet
        access and two third parties were injecting JavaScript into it.
        """
        pattern = re.compile(r"""(?:src|href)\s*=\s*["']https?://""")
        offenders = set()
        web = SRC / "web"
        for path in list(web.rglob("*.html")) + list(web.rglob("*.js")) + list(web.rglob("*.css")):
            if "vendor" in path.parts:
                continue
            if pattern.search(path.read_text(encoding="utf-8", errors="replace")):
                offenders.add(relative(path))

        check_ratchet(offenders, self.CDN_ALLOWED, "the panel serves no external assets")

    def test_vendored_assets_match_their_checksums(self):
        """Vendoring without an update process is how CVEs accumulate."""
        import json

        lock = json.loads((REPO / "scripts/vendor.lock.json").read_text(encoding="utf-8"))
        vendor = SRC / "web/static/vendor"

        import hashlib

        for name, meta in lock.items():
            path = vendor / name
            assert path.exists(), f"{name} is in the lock but not vendored"
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            assert digest == meta["sha256"], f"{name} does not match its recorded checksum"


class TestPackaging:
    """What the code imports is what the packaging declares."""

    #: Modules imported conditionally, inside a try/except ImportError, to
    #: degrade when an optional extra is absent.
    OPTIONAL = {"psutil", "inquirer", "questionary", "rich", "httpx", "tomli"}

    #: Import names whose distribution is spelled differently.
    DISTRIBUTION_NAMES = {
        "jinja2": "Jinja2",
        "yaml": "PyYAML",
        "jose": "python-jose",
        "uvicorn": "uvicorn",
    }

    def third_party_imports(self) -> set[str]:
        """
        Return the top-level third-party modules the package imports.

        Returns:
            Import names, excluding the standard library and wasm itself.
        """
        import sys

        stdlib = set(sys.stdlib_module_names)
        found: set[str] = set()
        for path in python_files():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.level == 0:
                    names = [node.module or ""]
                else:
                    continue
                for name in names:
                    root = name.split(".")[0]
                    if root and root not in stdlib and root != "wasm":
                        found.add(root)
        return found

    #: Imported but not declared. Each entry is a bug with a decided fix, not a
    #: dispensation.
    UNDECLARED_KNOWN: set[str] = set()

    def test_every_import_is_declared(self):
        """
        An undeclared import is a package that fails to start after install.

        pydantic reached eleven modules without ever being declared in
        pyproject.toml, setup.py, debian.control or the RPM spec.
        """
        declared = (REPO / "pyproject.toml").read_text(encoding="utf-8").lower()
        missing = set()
        for name in self.third_party_imports() - self.OPTIONAL:
            distribution = self.DISTRIBUTION_NAMES.get(name, name).lower()
            if distribution not in declared:
                missing.add(name)

        check_ratchet(missing, self.UNDECLARED_KNOWN, "every import is declared")

    def test_version_is_consistent_across_packaging_files(self):
        """The version lives in six files; drift caused corrective releases."""
        import subprocess
        import sys

        result = subprocess.run(  # noqa: S603 - fixed argv, developer tool
            [sys.executable, str(REPO / "scripts/release.py"), "--check"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert result.returncode == 0, result.stdout + result.stderr

    test_version_is_consistent_across_packaging_files = pytest.mark.allow_subprocess(
        test_version_is_consistent_across_packaging_files
    )


class TestImportable:
    """Every module imports on its own."""

    def test_no_module_fails_to_import(self):
        """
        Catches syntax errors, circular imports and missing optional guards in
        code paths no test happens to reach.
        """
        failures = []
        for module in pkgutil.walk_packages(wasm.__path__, "wasm."):
            try:
                importlib.import_module(module.name)
            except Exception as exc:  # noqa: BLE001 - reporting is the point
                failures.append(f"{module.name}: {exc.__class__.__name__}: {exc}")

        assert not failures, "Modules that fail to import:\n" + "\n".join(
            f"  {line}" for line in failures
        )
