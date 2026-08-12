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

    #: Only the seam itself. This started at sixteen files and is now closed:
    #: adding a second entry is not paying down debt, it is reopening the hole.
    SUBPROCESS_ALLOWED = {"src/wasm/core/runner.py"}

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


class TestErrorHandling:
    """
    Blind excepts can only become fewer.

    There were 302 of them, 149 of which logged nothing. That is the mechanism
    by which five calls to methods that do not exist shipped for entire
    releases: every AttributeError became a cosmetic warning. The lint rule is
    disabled per package while the debt is paid down, so this test is what
    stops it growing back in the meantime.
    """

    #: Current count. Lower it when you fix some; never raise it.
    MAX_BLIND_EXCEPTS = 66

    def test_blind_excepts_do_not_grow(self):
        found: list[str] = []
        for path in python_files():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.ExceptHandler):
                    continue
                caught = node.type
                bare = caught is None
                broad = isinstance(caught, ast.Name) and caught.id in {"Exception", "BaseException"}
                if bare or broad:
                    found.append(f"{relative(path)}:{node.lineno}")

        assert len(found) <= self.MAX_BLIND_EXCEPTS, (
            f"blind excepts went from {self.MAX_BLIND_EXCEPTS} to {len(found)}.\n"
            "Catch the specific exception. A broad catch belongs only in an error "
            "boundary, and it logs.\n" + "\n".join(f"  {line}" for line in found[-20:])
        )

        assert len(found) >= self.MAX_BLIND_EXCEPTS - 10, (
            f"blind excepts are down to {len(found)}. Lower MAX_BLIND_EXCEPTS to "
            f"{len(found)} so the ratchet keeps holding."
        )

    def test_no_bare_except_anywhere(self):
        """A bare ``except:`` also swallows KeyboardInterrupt and SystemExit."""
        offenders = []
        for path in python_files():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            offenders += [
                f"{relative(path)}:{node.lineno}"
                for node in ast.walk(tree)
                if isinstance(node, ast.ExceptHandler) and node.type is None
            ]

        assert not offenders, "bare excepts:\n" + "\n".join(f"  {o}" for o in offenders)


class TestFilesystemSeam:
    """
    Changes to disk go through wasm.core.fs, so --dry-run can refuse them.

    Enforcing the flag only in the command runner made it true for what WASM
    executes and false for what WASM writes: an adversarial review showed
    ``wasm --dry-run backup delete <id> --force`` announcing that nothing would
    change and then deleting the archive, because a deletion is a
    ``Path.unlink`` and never reaches a subprocess.
    """

    #: Calls that put something on disk or take it off.
    MUTATIONS = frozenset(
        {
            "write_text",
            "write_bytes",
            "mkdir",
            "unlink",
            "rmdir",
            "rmtree",
            "symlink_to",
            "makedirs",
            "copytree",
            "touch",
        }
    )

    #: The seam itself, and the two places that legitimately write outside it:
    #: the runner streams a database dump straight to a file descriptor it
    #: opens with the right mode, which is the point of that method, and the
    #: auth store writes its own state before the CLI context exists.
    SEAM_FILES = {"src/wasm/core/fs.py", "src/wasm/core/runner.py"}

    #: What is left of the migration, by file. This may only fall.
    DIRECT_MUTATIONS_ALLOWED = 32

    def direct_mutations(self) -> list[str]:
        """
        Find calls that change the filesystem without going through the seam.

        Returns:
            Locations, as ``path:line:call``.
        """
        found: list[str] = []
        for path in python_files():
            if relative(path) in self.SEAM_FILES:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                    continue
                if node.func.attr not in self.MUTATIONS:
                    continue
                receiver = node.func.value
                name = getattr(receiver, "attr", None) or getattr(receiver, "id", None)
                # A call on the seam is the seam being used, not bypassed.
                if name in ("fs", "_fs"):
                    continue
                found.append(f"{relative(path)}:{node.lineno}:{node.func.attr}")
        return sorted(found)

    def test_direct_filesystem_mutations_do_not_grow(self):
        found = self.direct_mutations()

        assert len(found) <= self.DIRECT_MUTATIONS_ALLOWED, (
            f"direct filesystem mutations went from {self.DIRECT_MUTATIONS_ALLOWED} to "
            f"{len(found)}. Route the change through wasm.core.fs, or --dry-run "
            "will announce that nothing changed and then change it.\n"
            + "\n".join(f"  {line}" for line in found[-15:])
        )

        assert len(found) >= self.DIRECT_MUTATIONS_ALLOWED - 5, (
            f"direct filesystem mutations are down to {len(found)}. Lower "
            f"DIRECT_MUTATIONS_ALLOWED to {len(found)} so the ratchet keeps holding."
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

    #: Nothing. The single-page application that loaded Tailwind and Font
    #: Awesome from public CDNs has been replaced by server-rendered pages, so
    #: this list is empty and must stay that way.
    CDN_ALLOWED: set[str] = set()

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

    def test_every_data_file_the_code_loads_is_declared(self):
        """
        A file the package reads at runtime has to be in package-data.

        The panel's templates were not, so the wheel installed cleanly and then
        could not render a single page. Nothing catches that except looking at
        the artifact, or this.
        """
        declared = (REPO / "pyproject.toml").read_text(encoding="utf-8")

        needed = {
            "templates/**/*.j2": SRC / "templates",
            "web/templates/**/*.html": SRC / "web/templates",
            "web/static/**/*": SRC / "web/static",
        }

        missing = [
            pattern
            for pattern, directory in needed.items()
            if directory.exists() and pattern not in declared
        ]

        assert not missing, (
            "These directories hold files the package reads at runtime but are "
            "not in [tool.setuptools.package-data]:\n"
            + "\n".join(f"  {pattern}" for pattern in missing)
        )

    def test_the_debian_build_dependencies_agree(self):
        """
        wasm.dsc and debian.control must declare the same build dependencies.

        OBS builds the buildroot from the .dsc and debhelper checks against
        debian/control, so a difference between them means the build either
        fails for a dependency that is declared in the wrong file, or succeeds
        on one machine and not another.
        """

        def build_deps(text: str) -> set[str]:
            lines = text.splitlines()
            start = next(i for i, line in enumerate(lines) if line.startswith("Build-Depends:"))
            collected = lines[start].split(":", 1)[1]
            index = start
            while collected.rstrip().endswith(","):
                index += 1
                collected += lines[index]
            return {part.strip() for part in collected.split(",") if part.strip()}

        dsc = build_deps((REPO / "obs/wasm.dsc").read_text(encoding="utf-8"))
        control = build_deps((REPO / "obs/debian.control").read_text(encoding="utf-8"))

        assert dsc == control, (
            "obs/wasm.dsc and obs/debian.control disagree:\n"
            f"  only in wasm.dsc:      {sorted(dsc - control)}\n"
            f"  only in debian.control: {sorted(control - dsc)}"
        )

    def test_nothing_runs_the_package_during_a_distribution_build(self):
        """
        A packaging recipe must not execute the package it is building.

        Doing it to generate the shell completions made every runtime import a
        build dependency, and one missing entry failed all twenty-two OBS
        targets at once. The completions are committed instead.
        """
        recipes = {
            "obs/debian.rules": (REPO / "obs/debian.rules").read_text(encoding="utf-8"),
            "rpm/wasm.spec": (REPO / "rpm/wasm.spec").read_text(encoding="utf-8"),
        }

        offenders = [
            f"{name}: {line.strip()}"
            for name, text in recipes.items()
            for line in text.splitlines()
            if "_WASM_COMPLETE" in line or "-m wasm" in line
        ]

        assert not offenders, (
            "These lines run the package during a distribution build:\n"
            + "\n".join(f"  {line}" for line in offenders)
        )

    def test_version_is_consistent_across_packaging_files(self):
        """The version lives in six files; drift caused corrective releases."""
        import subprocess
        import sys

        result = subprocess.run(
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
            except Exception as exc:
                failures.append(f"{module.name}: {exc.__class__.__name__}: {exc}")

        assert not failures, "Modules that fail to import:\n" + "\n".join(
            f"  {line}" for line in failures
        )
