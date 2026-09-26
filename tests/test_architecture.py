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

    #: The same rule for asyncio's process API, which spells the escape hatch
    #: differently and therefore walked straight past the import check above:
    #: ``asyncio.create_subprocess_exec`` imports asyncio, not subprocess.
    #:
    #: The panel's journal streams are here because the runner is synchronous
    #: and a WebSocket cannot block on it. That is a real constraint and a real
    #: piece of debt: these two call sites are outside the seam, so nothing can
    #: intercept them in a test, and the guarantees the runner gives - argv
    #: only, mandatory timeouts, one place that knows how to clean a process up
    #: - are hand-rolled there instead. Closing it means an async streaming
    #: method on CommandRunner. Until then, this list must not grow.
    ASYNC_SUBPROCESS_ALLOWED = {"src/wasm/web/websockets/router.py"}

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

    def test_nothing_spawns_a_process_through_asyncio(self):
        """
        The import check has a blind spot, and something was already in it.

        ``asyncio.create_subprocess_exec`` runs a process without importing
        subprocess, so the panel's two journal streams spawned journalctl
        outside the seam and the guard said nothing. One of them then leaked a
        process per connection for want of the cleanup the runner does once,
        which is exactly the class of bug the seam exists to make impossible.
        """
        offenders = set()
        for path in python_files():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                function = node.func
                name = getattr(function, "attr", None) or getattr(function, "id", None)
                if name in {"create_subprocess_exec", "create_subprocess_shell"}:
                    offenders.add(relative(path))

        check_ratchet(
            offenders,
            self.ASYNC_SUBPROCESS_ALLOWED,
            "only the runner spawns processes, asyncio included",
        )

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
    #: wasm/web/websockets/router.py's WebSocket handlers no longer swallow
    #: everything with ``except Exception: pass/break``: each loop now catches
    #: the specific exception it guards against (WebSocketDisconnect,
    #: RuntimeError, json.JSONDecodeError), and the four remaining broad
    #: catches are the genuine per-connection error boundaries, and they log.
    MAX_BLIND_EXCEPTS = 38

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


class TestOneImplementation:
    """
    A rule copy-pasted into several files drifts the moment one copy changes
    and the others do not - app type detection had four implementations with
    contradicting precedence before this project settled on "use the existing
    one or move it somewhere both callers can reach".
    """

    def test_global_flags_folding_has_one_implementation(self):
        """
        ``--verbose``/``--dry-run``/``--no-color`` have always been
        re-spellable after a subcommand's name, and four command modules
        each grew their own private callback for folding them into the
        shared context, differing only in incidental details (``hidden=True``
        here, a missing logger-cache invalidation there) that made the
        duplication easy to miss in review. All four now import the one
        decorator ``wasm.cli.app`` declares - the same module ``json_option``
        already lives in.
        """
        from wasm.cli.app import global_flags
        from wasm.cli.commands import config, setup, web, webapp

        for module in (config, setup, web, webapp):
            assert module.global_flags is global_flags, (
                f"{module.__name__}.global_flags is not wasm.cli.app.global_flags - "
                "it has its own copy of the folding logic"
            )


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


class TestPydanticBridge:
    """
    The web layer runs on pydantic 1 and pydantic 2 alike.

    pip and Fedora install pydantic 2, but Ubuntu 24.04 and Debian 12 package
    python3-pydantic 1.10 and the .deb depends on the distribution package.
    CI tests against pip's pydantic 2, which is how v1.5.0 imported
    ``field_validator`` - a name pydantic 1 does not have - and ``wasm web
    start`` died with ImportError on every Ubuntu 24.04 install while every
    test stayed green. The names the two majors spell differently are bridged
    once, in ``wasm.web.pydantic_compat``, and nowhere else.
    """

    #: The bridge itself, the only module allowed to know which pydantic is
    #: installed.
    SHIM = "src/wasm/web/pydantic_compat.py"

    #: Names only one major version has. The v2 spellings raise ImportError
    #: under 1.10; the v1 spellings are deprecated shims under 2.x and their
    #: semantics differ. Either way, importing them from pydantic ties the
    #: module to one major version.
    VERSION_SPECIFIC_IMPORTS = frozenset(
        {
            # pydantic 2 only.
            "field_validator",
            "model_validator",
            "field_serializer",
            "model_serializer",
            "computed_field",
            "ConfigDict",
            "TypeAdapter",
            "RootModel",
            # pydantic 1 only; use the bridge's v2-spelled equivalents.
            "validator",
            "root_validator",
        }
    )

    #: Methods that exist only on pydantic 2 models. ``.model_dump()`` is how
    #: the ImportError of v1.5.0 had a sibling waiting in deps.py: it imports
    #: fine everywhere and then dies with AttributeError on the first error
    #: response a pydantic 1 machine renders.
    V2_ONLY_METHODS = frozenset(
        {
            "model_dump",
            "model_dump_json",
            "model_validate",
            "model_validate_json",
            "model_construct",
            "model_copy",
            "model_json_schema",
        }
    )

    def test_version_specific_names_are_imported_only_by_the_bridge(self):
        offenders = set()
        for path in python_files():
            if relative(path) == self.SHIM:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.ImportFrom):
                    continue
                if (node.module or "").split(".")[0] != "pydantic":
                    continue
                for alias in node.names:
                    if alias.name in self.VERSION_SPECIFIC_IMPORTS:
                        offenders.add(f"{relative(path)}:{node.lineno}:{alias.name}")

        assert not offenders, (
            "These files import a name only one pydantic major version has:\n"
            + "\n".join(f"  {name}" for name in sorted(offenders))
            + "\n\nImport it from wasm.web.pydantic_compat instead, adding the "
            "bridge there if it is missing. Ubuntu 24.04 ships pydantic 1.10."
        )

    def test_v2_only_model_methods_are_called_only_by_the_bridge(self):
        offenders = set()
        for path in python_files():
            if relative(path) == self.SHIM:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                    continue
                if node.func.attr in self.V2_ONLY_METHODS:
                    offenders.add(f"{relative(path)}:{node.lineno}:{node.func.attr}")

        assert not offenders, (
            "These calls exist only on pydantic 2 models and raise "
            "AttributeError under the pydantic 1.10 that Ubuntu 24.04 ships:\n"
            + "\n".join(f"  {name}" for name in sorted(offenders))
            + "\n\nUse the helpers in wasm.web.pydantic_compat instead."
        )

    def test_field_constraints_spelled_only_one_major_understands(self):
        """
        ``Field(pattern=...)`` is pydantic 2's spelling and ``Field(regex=...)``
        pydantic 1's; each raises under the other. Neither is bridged: a field
        that needs a pattern uses an explicit validator through the bridge's
        ``field_validator``, which also gives the refusal a message an operator
        can read.
        """
        offenders = set()
        for path in python_files():
            if relative(path) == self.SHIM:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
                if name != "Field":
                    continue
                for keyword in node.keywords:
                    if keyword.arg in ("pattern", "regex"):
                        offenders.add(f"{relative(path)}:{node.lineno}:{keyword.arg}")

        assert not offenders, (
            "These Field() constraints only exist in one pydantic major "
            "version:\n"
            + "\n".join(f"  {name}" for name in sorted(offenders))
            + "\n\nValidate with an explicit field_validator from "
            "wasm.web.pydantic_compat instead."
        )


class TestSelfContained:
    """The console works on a machine with no route to the internet."""

    STATIC = SRC / "web/static"

    #: Nothing. The first single-page application loaded Tailwind and Font
    #: Awesome from public CDNs; the console bundles every dependency into its
    #: build, so this list is empty and must stay that way.
    CDN_ALLOWED: set[str] = set()

    def references(self, html: str) -> list[str]:
        """
        Return every ``src`` and ``href`` in a document.

        Args:
            html: The document.

        Returns:
            The attribute values, in order.
        """
        return re.findall(r"""\b(?:src|href)\s*=\s*["']([^"']*)["']""", html)

    def test_the_built_console_references_only_same_origin_assets(self):
        """
        Every script, stylesheet and icon the console loads comes from this server.

        A third-party origin would be both a machine that cannot render its
        own panel offline and a third party injecting code into a page with
        root over the machine; the CSP would also block it, silently, in
        production only.
        """
        html = (self.STATIC / "index.html").read_text(encoding="utf-8")
        refs = self.references(html)

        assert refs, "the built index.html references nothing; is it the Vite build?"
        foreign = [ref for ref in refs if not ref.startswith("/") or ref.startswith("//")]
        assert not foreign, f"index.html references other origins: {foreign}"

        code = re.findall(r"""<(?:script|link)\b[^>]*\b(?:src|href)=["']([^"']+)["']""", html)
        outside_assets = [
            ref for ref in code if not ref.startswith("/assets/") and not ref.endswith(".svg")
        ]
        assert not outside_assets, f"code outside /assets/: {outside_assets}"

    def test_every_asset_the_console_names_is_committed(self):
        """
        A build committed in part is a console that loads a blank page.

        OBS packages ``git archive HEAD``: a chunk that was built but never
        committed does not exist on any installed machine.
        """
        html = (self.STATIC / "index.html").read_text(encoding="utf-8")
        missing = [
            ref
            for ref in self.references(html)
            if ref.startswith("/") and not (self.STATIC / ref.lstrip("/")).is_file()
        ]

        assert not missing, f"index.html names files that are not in the build: {missing}"

    def test_no_external_assets(self):
        """
        Nothing the console serves is fetched from a third party.

        The bundle is scanned as well as the document: a stylesheet
        ``@import`` or ``url()`` pointing at a CDN would load exactly like a
        script tag would.
        """
        markup = re.compile(r"""(?:src|href)\s*=\s*["']https?://""")
        css_remote = re.compile(
            r"""url\(\s*["']?(?:https?:)?//|@import\s+(?:url\()?["']?(?:https?:)?//"""
        )
        offenders = set()
        files = [p for p in self.STATIC.rglob("*") if p.suffix in {".html", ".js", ".css"}]
        for path in files:
            text = path.read_text(encoding="utf-8", errors="replace")
            if markup.search(text) or (path.suffix == ".css" and css_remote.search(text)):
                offenders.add(relative(path))

        check_ratchet(offenders, self.CDN_ALLOWED, "the console serves no external assets")


class TestPackaging:
    """What the code imports is what the packaging declares."""

    #: Modules imported conditionally, inside a try/except ImportError, to
    #: degrade when an optional extra is absent.
    #:
    #: rich is not one of these: wasm.core.logger imports it unconditionally
    #: at module scope, so a machine without it cannot run any command at
    #: all. It is a hard dependency in all four packaging files and belongs
    #: in the checked set, not here.
    OPTIONAL = {"psutil", "inquirer", "questionary", "httpx", "tomli"}

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
        pyproject.toml, setup.py, debian.control or the RPM spec - but this
        test only ever read pyproject.toml, so a Debian or RPM gap would have
        passed silently. All four packaging manifests are checked now.

        pyproject.toml and setup.py both declare PyPI distribution names
        (``PyYAML``, ``Jinja2``); the Debian and RPM package names are always
        the import name with a ``python3-`` (or distro-macro) prefix, so the
        bare import name is looked for there instead.
        """
        sources = {
            "pyproject.toml": (REPO / "pyproject.toml").read_text(encoding="utf-8").lower(),
            "setup.py": (REPO / "setup.py").read_text(encoding="utf-8").lower(),
            "obs/debian.control": (REPO / "obs/debian.control").read_text(encoding="utf-8").lower(),
            "rpm/wasm.spec": (REPO / "rpm/wasm.spec").read_text(encoding="utf-8").lower(),
        }
        pypi_sources = {"pyproject.toml", "setup.py"}

        missing = set()
        for name in self.third_party_imports() - self.OPTIONAL:
            pypi_name = self.DISTRIBUTION_NAMES.get(name, name).lower()
            for filename, text in sources.items():
                needle = pypi_name if filename in pypi_sources else name
                if needle not in text:
                    missing.add(f"{name} missing from {filename}")

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

    def test_the_source_distribution_carries_the_console(self):
        """
        The sdist is built from MANIFEST.in, not from package-data.

        A console missing from the sdist is a console missing from every
        distribution package built from it, and nothing fails until somebody
        opens the panel.
        """
        manifest = (REPO / "MANIFEST.in").read_text(encoding="utf-8")

        assert "recursive-include src/wasm/web/static *" in manifest
        assert "web/templates" not in manifest, "the Jinja pages are gone"

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

    def test_the_packaged_default_config_names_no_dead_or_removed_setting(self):
        """
        obs/wasm.default.yaml is what a fresh package installs as
        /etc/wasm/config.yaml, so it is a default like DEFAULT_CONFIG is.

        It shipped ``use_ai: true`` and an OpenAI key slot for an AI
        analysis that no longer exists, and ``auto_terminate: true`` for a
        switch wasm.core.config refuses to read at all (REMOVED_KEYS).
        """
        import yaml

        from wasm.core.config import REMOVED_KEYS

        dead = {"monitor.use_ai", "monitor.ai_interval", "monitor.openai", "databases.backup_dir"}
        packaged = yaml.safe_load((REPO / "obs/wasm.default.yaml").read_text(encoding="utf-8"))

        def dotted(tree: dict, prefix: str = "") -> set[str]:
            keys = set()
            for key, value in tree.items():
                name = f"{prefix}{key}"
                keys.add(name)
                if isinstance(value, dict):
                    keys |= dotted(value, f"{name}.")
            return keys

        named = dotted(packaged)
        assert not named & (dead | set(REMOVED_KEYS)), sorted(named & (dead | set(REMOVED_KEYS)))

    def test_maintainer_scripts_never_loosen_or_pip_install(self):
        """
        What runs as root on every package upgrade must not undo WASM's own rules.

        The Debian postinst pip-installed a module into the system Python with
        --break-system-packages, reassigned every file under /var/www/apps to
        www-data (a bind-mounted database directory included), and opened
        /etc/wasm and config.yaml, which hold credentials, to a group. It ran on
        every upgrade.
        """
        spec = (REPO / "rpm/wasm.spec").read_text(encoding="utf-8")
        # Only the scriptlet runs on the machine; the changelog below it is prose.
        post = spec.split("\n%post", 1)[1].split("\n%", 1)[0] if "\n%post" in spec else ""
        preun = spec.split("\n%preun", 1)[1].split("\n%", 1)[0] if "\n%preun" in spec else ""
        scripts = {
            "obs/debian.postinst": (REPO / "obs/debian.postinst").read_text(encoding="utf-8"),
            "rpm/wasm.spec %post": post,
        }
        prerm_path = REPO / "obs/debian.prerm"
        if prerm_path.exists():
            scripts["obs/debian.prerm"] = prerm_path.read_text(encoding="utf-8")
        if preun:
            scripts["rpm/wasm.spec %preun"] = preun
        forbidden = (
            ("pip install", "pip3 install", "--break-system-packages"),
            # Not just the www-data/var-www spelling that shipped: no maintainer
            # script may recursively chown anything, anywhere.
            ("chown -R",),
            ("chmod 755 /etc/wasm", "chmod 640 /etc/wasm/config.yaml"),
        )

        offenders = [
            f"{name}: {line.strip()}"
            for name, text in scripts.items()
            for line in text.splitlines()
            if not line.strip().startswith("#")
            for group in forbidden
            if any(needle in line for needle in group)
        ]

        assert not offenders, "Maintainer scripts must not do this:\n" + "\n".join(
            f"  {line}" for line in offenders
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


class TestMaintainerScripts:
    """
    Maintainer scripts run as root, unattended, on every install and upgrade.

    A ``pip install`` there reaches outside the package manager's view entirely:
    it is what put 'inquirer' in the system Python on every Debian upgrade,
    unmanaged and unremovable by dpkg. A permission loosened there defeats what
    wasm.core.config and wasm.web.auth enforce at runtime: /etc/wasm holds the
    web panel's signing key and token hash next to config.yaml's credentials, so
    both must land at 0700/0600, root:root, on every distribution and stay
    there across upgrades.
    """

    #: Scripts dpkg invokes directly. Only the ones that exist are read.
    DEBIAN_SCRIPTS = (
        "obs/debian.preinst",
        "obs/debian.postinst",
        "obs/debian.prerm",
        "obs/debian.postrm",
    )

    RPM_SCRIPTLET_NAMES = ("%pre", "%post", "%preun", "%postun")

    PIP_INSTALL = re.compile(r"\bpip3?\s+install\b|\bpython3?\s+-m\s+pip\s+install\b")
    CHMOD_LINE = re.compile(r"\bchmod\s+(?:-\w+\s+)?0?([0-7]{3})\s+(\S+)")
    CHOWN_LINE = re.compile(r"\bch(?:own|grp)\s+(?:-\w+\s+)?(\S+)\s+(\S+)")
    ATTR_LINE = re.compile(r"%attr\((\d+),\s*([^,]+),\s*([^)]+)\)(.*)")

    def _existing_debian_scripts(self) -> dict[str, str]:
        """
        Read every maintainer script that is actually shipped.

        Returns:
            Mapping of repo-relative path to file content, for scripts that
            exist. Debian ships preinst/prerm only when a package needs them.
        """
        found = {}
        for name in self.DEBIAN_SCRIPTS:
            path = REPO / name
            if path.exists():
                found[name] = path.read_text(encoding="utf-8")
        return found

    def _rpm_text(self) -> str:
        return (REPO / "rpm/wasm.spec").read_text(encoding="utf-8")

    def _rpm_scriptlets(self) -> dict[str, str]:
        """
        Split rpm/wasm.spec into the body of each %pre/%post/%preun/%postun.

        Returns:
            Mapping of "rpm/wasm.spec %scriptlet" to its body text, for every
            scriptlet the spec actually defines.
        """
        sections: dict[str, list[str]] = {}
        current: str | None = None
        for line in self._rpm_text().splitlines():
            stripped = line.strip()
            head = stripped.split()[0] if stripped else ""
            if head in self.RPM_SCRIPTLET_NAMES:
                current = head
                sections[current] = []
                continue
            if stripped.startswith("%"):
                current = None
                continue
            if current is not None:
                sections[current].append(line)
        return {f"rpm/wasm.spec {name}": "\n".join(body) for name, body in sections.items()}

    def test_no_maintainer_script_pip_installs(self):
        """
        WASM must never pip-install into the system Python from a packaging
        script, in any spelling: pip, pip3, python -m pip, python3 -m pip.
        """
        offenders = []
        sources = dict(self._existing_debian_scripts())
        sources.update(self._rpm_scriptlets())

        for name, text in sources.items():
            for lineno, line in enumerate(text.splitlines(), start=1):
                if self.PIP_INSTALL.search(line):
                    offenders.append(f"{name}:{lineno}: {line.strip()}")

        assert not offenders, "pip install found in a maintainer script:\n" + "\n".join(
            f"  {o}" for o in offenders
        )

    def test_etc_wasm_directory_is_always_0700_root(self):
        """
        /etc/wasm must be 0700, root:root, matching SECRET_DIR_MODE/DIR_MODE.

        Every chmod naming the directory must set exactly 0700, and every
        chown or chgrp naming it must leave it owned by the root group: a
        script that ever sets it wider, even only until the next line
        tightens it again, is a window a local attacker can race.
        """
        offenders = []
        exists = []
        sources = dict(self._existing_debian_scripts())
        sources["obs/debian.rules"] = (REPO / "obs/debian.rules").read_text(encoding="utf-8")
        sources.update(self._rpm_scriptlets())

        for name, text in sources.items():
            for lineno, line in enumerate(text.splitlines(), start=1):
                chmod = self.CHMOD_LINE.search(line)
                if chmod and chmod.group(2).rstrip("/").endswith("etc/wasm"):
                    exists.append(f"{name}:{lineno}")
                    if int(chmod.group(1), 8) != 0o700:
                        offenders.append(f"{name}:{lineno}: {line.strip()} (must be 0700)")
                chown = self.CHOWN_LINE.search(line)
                if chown and chown.group(2).rstrip("/").endswith("etc/wasm"):
                    spec = chown.group(1)
                    group = spec.split(":")[-1] if ":" in spec else spec
                    if group != "root":
                        offenders.append(
                            f"{name}:{lineno}: {line.strip()} (group must be root, not {group})"
                        )

        for lineno, line in enumerate(self._rpm_text().splitlines(), start=1):
            attr = self.ATTR_LINE.search(line)
            if not attr:
                continue
            mode, owner, group, rest = attr.groups()
            if "wasm" not in rest or "config.yaml" in rest:
                continue
            exists.append(f"rpm/wasm.spec:{lineno}")
            if int(mode, 8) != 0o700:
                offenders.append(f"rpm/wasm.spec:{lineno}: {line.strip()} (must be 0700)")
            if owner.strip() != "root" or group.strip() != "root":
                offenders.append(f"rpm/wasm.spec:{lineno}: {line.strip()} (must be root:root)")

        assert exists, "no chmod/%attr for /etc/wasm found in any packaging script"
        assert not offenders, "\n".join(f"  {o}" for o in offenders)

    def test_config_yaml_is_always_0600_root_no_group_access(self):
        """
        config.yaml must be 0600, root:root, matching SECRET_MODE/FILE_MODE.

        No script may grant a 'wasm' group (or any group) read access: the
        bug this guards against chowned it root:wasm and chmod 640'd it "for
        interactive mode", which made every credential in it world-readable
        to any account added to that group.
        """
        offenders = []
        exists = []
        sources = dict(self._existing_debian_scripts())
        sources["obs/debian.rules"] = (REPO / "obs/debian.rules").read_text(encoding="utf-8")
        sources.update(self._rpm_scriptlets())

        for name, text in sources.items():
            for lineno, line in enumerate(text.splitlines(), start=1):
                chmod = self.CHMOD_LINE.search(line)
                if chmod and chmod.group(2).rstrip("/").endswith("config.yaml"):
                    exists.append(f"{name}:{lineno}")
                    if int(chmod.group(1), 8) != 0o600:
                        offenders.append(f"{name}:{lineno}: {line.strip()} (must be 0600)")
                chown = self.CHOWN_LINE.search(line)
                if chown and chown.group(2).rstrip("/").endswith("config.yaml"):
                    spec = chown.group(1)
                    group = spec.split(":")[-1] if ":" in spec else spec
                    if group != "root":
                        offenders.append(
                            f"{name}:{lineno}: {line.strip()} (group must be root, not {group})"
                        )

        for lineno, line in enumerate(self._rpm_text().splitlines(), start=1):
            attr = self.ATTR_LINE.search(line)
            if not attr:
                continue
            mode, owner, group, rest = attr.groups()
            if "config.yaml" not in rest:
                continue
            exists.append(f"rpm/wasm.spec:{lineno}")
            if int(mode, 8) != 0o600:
                offenders.append(f"rpm/wasm.spec:{lineno}: {line.strip()} (must be 0600)")
            if owner.strip() != "root" or group.strip() != "root":
                offenders.append(f"rpm/wasm.spec:{lineno}: {line.strip()} (must be root:root)")

        assert exists, "no chmod/%attr for config.yaml found in any packaging script"
        assert not offenders, "\n".join(f"  {o}" for o in offenders)

    def test_package_removal_stops_and_disables_wasm_monitor(self):
        """
        'wasm monitor install' writes and enables a systemd unit that neither
        dpkg nor rpm ever shipped, so removing the package left it running
        under a binary that had just disappeared. Both maintainer scripts
        must stop and disable it.
        """
        prerm = (REPO / "obs/debian.prerm").read_text(encoding="utf-8")
        preun = self._rpm_scriptlets().get("rpm/wasm.spec %preun")

        assert preun is not None, "rpm/wasm.spec has no %preun scriptlet"

        for name, text in (("obs/debian.prerm", prerm), ("rpm/wasm.spec %preun", preun)):
            assert "wasm-monitor" in text, f"{name} never mentions wasm-monitor.service"
            assert re.search(r"systemctl\s+stop\s+wasm-monitor", text), (
                f"{name} does not stop wasm-monitor.service"
            )
            assert re.search(r"systemctl\s+disable\s+wasm-monitor", text), (
                f"{name} does not disable wasm-monitor.service"
            )

    def test_removal_scripts_never_start_or_enable_anything(self):
        """
        A script that runs on removal has exactly one job for a unit WASM
        installed itself: make sure it is not running. 'start', 'enable' or
        'restart' would be the postinst/%post update-and-restart logic
        landing where it can only ever fire on the way out.
        """
        prerm = (REPO / "obs/debian.prerm").read_text(encoding="utf-8")
        preun = self._rpm_scriptlets().get("rpm/wasm.spec %preun", "")

        for name, text in (("obs/debian.prerm", prerm), ("rpm/wasm.spec %preun", preun)):
            offenders = [
                line.strip()
                for line in text.splitlines()
                if re.search(r"systemctl\s+(start|enable|restart)\b", line)
            ]
            assert not offenders, f"{name} starts or enables a unit on removal: {offenders}"

    def test_debian_prerm_only_stops_units_on_an_actual_removal(self):
        """
        $1 is "remove" only when the package is being uninstalled outright;
        "upgrade" (and the failure/deconfigure variants dpkg also calls this
        with) must leave the unit alone - it is meant to survive the bump.
        """
        prerm = (REPO / "obs/debian.prerm").read_text(encoding="utf-8")
        assert re.search(r'case\s+"\$1"\s+in', prerm), "prerm does not branch on $1"

        remove_start = prerm.index("remove)")
        next_case = prerm.index(";;", remove_start)
        remove_body = prerm[remove_start:next_case]
        assert "systemctl" in remove_body, "prerm's remove) branch never calls systemctl"

        after_remove = prerm[next_case:]
        upgrade_start = after_remove.index("upgrade")
        upgrade_body = after_remove[upgrade_start : after_remove.index(";;", upgrade_start)]
        assert "systemctl" not in upgrade_body, "prerm must not touch the unit on upgrade"

    def test_rpm_preun_only_stops_units_on_an_actual_removal(self):
        """$1 is 0 in %preun only on final removal, never on an upgrade."""
        preun = self._rpm_scriptlets().get("rpm/wasm.spec %preun", "")
        assert re.search(r"\$1\s*(-eq|=)\s*0", preun), (
            "rpm/wasm.spec %preun does not guard on $1 == 0 (final removal)"
        )

    def test_python3_venv_is_a_debian_dependency(self):
        """
        wasm.deployers.python.PythonDeployer.pre_install runs
        'python3 -m venv <path>'. On Debian and Ubuntu, venv (and the
        ensurepip bootstrap it uses without --without-pip) ships in the
        separate python3-venv package, not in python3 itself.
        """
        lines = (REPO / "obs/debian.control").read_text(encoding="utf-8").splitlines()
        start = next(i for i, line in enumerate(lines) if line.startswith("Depends:"))
        block = [lines[start]]
        index = start + 1
        while index < len(lines) and lines[index].startswith((" ", "\t")):
            block.append(lines[index])
            index += 1
        depends_block = "\n".join(block)

        assert "python3-venv" in depends_block, "obs/debian.control Depends is missing python3-venv"

    def test_py_typed_marker_exists(self):
        """
        pyproject.toml's package-data has declared 'py.typed' since the
        package was first typed, but the file itself was never created - so
        every wheel built from this repo shipped without it, and mypy
        treats an installed wasm as untyped (implicit Any) from any project
        that depends on it.
        """
        marker = SRC / "py.typed"
        assert marker.exists(), (
            f"{marker} does not exist, but pyproject.toml's "
            "[tool.setuptools.package-data] declares 'py.typed'"
        )
        assert marker.read_text(encoding="utf-8") == "", "py.typed is a marker file; it stays empty"


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
