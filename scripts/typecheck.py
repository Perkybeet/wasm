#!/usr/bin/env python3
"""
Run mypy against a baseline that can only shrink.

Type checking is the guard that matters most in this codebase: every call to a
method that does not exist, and which shipped for entire releases because a
blind except swallowed the AttributeError, is caught by it. So it has to block
CI.

It cannot block at zero yet, because the refactor inherited 393 errors and is
down to a couple of dozen. Turning the gate off until the last one is fixed
would mean no gate at all in the meantime, and the errors that matter would
arrive unnoticed. So the gate compares against a committed count instead:

- More errors than the baseline fails the build.
- Fewer errors than the baseline also fails the build, with the new number to
  write down, so the baseline cannot quietly go stale.
- Errors in the categories that catch a call to something that does not exist
  fail the build at zero, whatever the baseline says. Those never get a
  grace period.

Usage:
    scripts/typecheck.py            Check against the baseline.
    scripts/typecheck.py --update   Record the current count.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASELINE_FILE = ROOT / "scripts/typecheck.baseline.json"

#: Error codes that mean "this name does not exist". They are the reason this
#: gate exists, so they are never allowed, baseline or not.
#:
#: ``import-not-found`` is here because it caught a real one: inquirer stayed
#: imported after it stopped being a dependency, so ``wasm --interactive``
#: crashed on any clean install while the developer's machine, which still had
#: the package, said nothing.
FATAL_CODES = frozenset(
    {
        "attr-defined",
        "name-defined",
        "call-arg",
        "used-before-def",
        "import-not-found",
    }
)

#: Codes whose count depends on which optional packages happen to be installed.
#: Baselining them makes the gate fail for reasons that have nothing to do with
#: the change being checked.
ENVIRONMENT_DEPENDENT = frozenset({"import-untyped"})

ERROR_LINE = re.compile(
    r"^(?P<file>[^:]+):(?P<line>\d+): error: (?P<message>.*?)\s*\[(?P<code>[a-z-]+)\]$"
)


def run_mypy() -> list[dict[str, str]]:
    """
    Run mypy and parse its findings.

    Returns:
        One dict per error, with file, line, message and code.
    """
    result = subprocess.run(
        [sys.executable, "-m", "mypy", "--no-error-summary", "--no-color-output"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    findings = []
    for line in result.stdout.splitlines():
        match = ERROR_LINE.match(line.strip())
        if match:
            findings.append(match.groupdict())
    return findings


def load_baseline() -> dict[str, int]:
    """
    Read the recorded error counts.

    Returns:
        Counts by error code, empty when there is no baseline yet.
    """
    if not BASELINE_FILE.exists():
        return {}
    return json.loads(BASELINE_FILE.read_text(encoding="utf-8"))


def main(argv: list[str] | None = None) -> int:
    """
    Command line entry point.

    Args:
        argv: Arguments, defaulting to sys.argv.

    Returns:
        Process exit code.
    """
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    parser.add_argument("--update", action="store_true", help="record the current counts")
    args = parser.parse_args(argv)

    findings = [f for f in run_mypy() if f["code"] not in ENVIRONMENT_DEPENDENT]
    counts = Counter(f["code"] for f in findings)

    if args.update:
        BASELINE_FILE.write_text(
            json.dumps(dict(sorted(counts.items())), indent=2) + "\n", encoding="utf-8"
        )
        print(f"Baseline recorded: {sum(counts.values())} errors across {len(counts)} codes")
        return 0

    fatal = [f for f in findings if f["code"] in FATAL_CODES]
    if fatal:
        print("These refer to names that do not exist and are never allowed:\n")
        for finding in fatal:
            print(f"  {finding['file']}:{finding['line']}  {finding['message']}")
        print(
            "\nThis is the defect class the type gate exists for: a call to a method "
            "that is not there, which a blind except would turn into a warning nobody "
            "reads."
        )
        return 1

    baseline = load_baseline()
    if not baseline:
        print("No baseline. Run scripts/typecheck.py --update.")
        return 1

    problems = []
    for code, count in sorted(counts.items()):
        allowed = baseline.get(code, 0)
        if count > allowed:
            problems.append(f"{code}: {count}, baseline allows {allowed}")

    improved = [
        f"{code}: {counts.get(code, 0)}, baseline says {allowed}"
        for code, allowed in sorted(baseline.items())
        if counts.get(code, 0) < allowed
    ]

    if problems:
        print("New type errors:\n")
        for problem in problems:
            print(f"  {problem}")
        for finding in findings:
            if finding["code"] in {p.split(":")[0] for p in problems}:
                print(f"    {finding['file']}:{finding['line']}  {finding['message']}")
        return 1

    if improved:
        print("Type errors went down. Run scripts/typecheck.py --update to record it:\n")
        for line in improved:
            print(f"  {line}")
        return 1

    print(f"Type errors at the baseline: {sum(counts.values())}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
