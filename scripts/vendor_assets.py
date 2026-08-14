#!/usr/bin/env python3
"""
Download and verify the panel's third-party frontend assets.

The panel used to load Tailwind and Font Awesome from public CDNs. That meant a
control panel with root over the machine could not render without internet
access, and that two third parties were injecting JavaScript into it. Every
asset now lives in the repository, so the panel works on an air-gapped box and
the OBS tarball, which is produced with ``git archive HEAD``, ships everything
it needs.

Vendoring without an update process is how CVEs accumulate, so each file is
pinned by version and by SHA-256 in ``scripts/vendor.lock.json``.

Usage:
    scripts/vendor_assets.py --check     Verify the vendored files. Used by CI.
    scripts/vendor_assets.py --fetch     Download anything missing or stale.
    scripts/vendor_assets.py --update    Re-pin to the versions below.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VENDOR_DIR = ROOT / "src/wasm/web/static/vendor"
LOCK_FILE = ROOT / "scripts/vendor.lock.json"

JSDELIVR = "https://cdn.jsdelivr.net/npm"

# Pinned versions. htmx stays on 2.x deliberately: 4.0 has been in beta since
# July 2026 with no stable date and no published migration guide.
HTMX = "2.0.10"
# The official htmx SSE extension, so the machine strip can swap fragments off
# the panel's one /events stream instead of polling.
HTMX_SSE = "2.2.3"
XTERM = "6.0.0"
UPLOT = "1.6.32"
GEIST = "5.3.0"
# Nayuki's QR generator, for drawing the 2FA enrolment QR in the browser so
# the otpauth secret never leaves the host. The npm package's index.js is the
# compiled typescript-javascript file from the upstream repository, published
# as an ES module.
QRCODEGEN = "1.8.0"


@dataclass(frozen=True)
class Asset:
    """
    One vendored file.

    Attributes:
        url: Where to download it from.
        dest: Path relative to the vendor directory.
        licence: SPDX identifier, recorded so the packaging stays honest.
    """

    url: str
    dest: str
    licence: str


def _font(family: str, weight: int) -> Asset:
    """
    Build the asset entry for one Geist weight.

    Args:
        family: Fontsource package name, such as ``geist-sans``.
        weight: Numeric font weight.

    Returns:
        The asset descriptor.
    """
    name = f"{family}-latin-{weight}-normal.woff2"
    return Asset(
        url=f"{JSDELIVR}/@fontsource/{family}@{GEIST}/files/{name}",
        dest=f"fonts/{name}",
        licence="OFL-1.1",
    )


ASSETS: tuple[Asset, ...] = (
    Asset(f"{JSDELIVR}/htmx.org@{HTMX}/dist/htmx.min.js", "htmx.min.js", "BSD-2-Clause"),
    Asset(f"{JSDELIVR}/htmx-ext-sse@{HTMX_SSE}/dist/sse.js", "sse.js", "0BSD"),
    Asset(f"{JSDELIVR}/@xterm/xterm@{XTERM}/lib/xterm.js", "xterm.js", "MIT"),
    Asset(f"{JSDELIVR}/@xterm/xterm@{XTERM}/css/xterm.css", "xterm.css", "MIT"),
    Asset(f"{JSDELIVR}/uplot@{UPLOT}/dist/uPlot.iife.min.js", "uplot.min.js", "MIT"),
    Asset(f"{JSDELIVR}/uplot@{UPLOT}/dist/uPlot.min.css", "uplot.min.css", "MIT"),
    Asset(f"{JSDELIVR}/nayuki-qr-code-generator@{QRCODEGEN}/index.js", "qrcodegen.js", "MIT"),
    # Interface and prose: titles at 600, labels and buttons at 500, body at
    # 400. The design direction v2 retired IBM Plex entirely, condensed cut
    # included.
    _font("geist-sans", 400),
    _font("geist-sans", 500),
    _font("geist-sans", 600),
    # Everything that comes from the system: paths, unit names, ports, logs.
    _font("geist-mono", 400),
    _font("geist-mono", 500),
)


def _digest(data: bytes) -> str:
    """
    Return the SHA-256 of some bytes.

    Args:
        data: Content to hash.

    Returns:
        Lowercase hexadecimal digest.
    """
    return hashlib.sha256(data).hexdigest()


def _download(url: str) -> bytes:
    """
    Fetch a URL over HTTPS.

    Args:
        url: Absolute https URL.

    Returns:
        The response body.

    Raises:
        SystemExit: When the URL is not https or the download fails.
    """
    if not url.startswith("https://"):
        raise SystemExit(f"Refusing to fetch a non-https URL: {url}")
    request = urllib.request.Request(url, headers={"User-Agent": "wasm-vendor"})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.read()
    except urllib.error.URLError as exc:
        raise SystemExit(f"Failed to download {url}: {exc}") from exc


def _load_lock() -> dict[str, dict[str, str]]:
    """
    Read the checksum lock file.

    Returns:
        Mapping of destination path to its recorded metadata.
    """
    if not LOCK_FILE.exists():
        return {}
    return json.loads(LOCK_FILE.read_text(encoding="utf-8"))


def _save_lock(lock: dict[str, dict[str, str]]) -> None:
    """
    Write the checksum lock file.

    Args:
        lock: Mapping of destination path to its metadata.
    """
    LOCK_FILE.write_text(json.dumps(lock, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def check() -> int:
    """
    Verify every vendored file against the lock.

    Returns:
        Process exit code.
    """
    lock = _load_lock()
    if not lock:
        print("No lock file. Run scripts/vendor_assets.py --update first.")
        return 1

    problems: list[str] = []
    for asset in ASSETS:
        recorded = lock.get(asset.dest)
        if recorded is None:
            problems.append(f"{asset.dest}: not in the lock file")
            continue
        if recorded["url"] != asset.url:
            problems.append(f"{asset.dest}: lock points at {recorded['url']}")
        path = VENDOR_DIR / asset.dest
        if not path.exists():
            problems.append(f"{asset.dest}: missing, run --fetch")
            continue
        actual = _digest(path.read_bytes())
        if actual != recorded["sha256"]:
            problems.append(f"{asset.dest}: checksum mismatch")

    stale = set(lock) - {a.dest for a in ASSETS}
    problems.extend(f"{name}: in the lock but no longer used" for name in sorted(stale))

    if problems:
        print("Vendored assets are not in the expected state:")
        for problem in problems:
            print(f"  - {problem}")
        return 1

    print(f"All {len(ASSETS)} vendored assets match the lock")
    return 0


def fetch(*, repin: bool) -> int:
    """
    Download the assets, optionally recording new checksums.

    Args:
        repin: When True, accept whatever the server returns and record its
            checksum. When False, refuse content that does not match the lock.

    Returns:
        Process exit code.
    """
    lock = _load_lock()
    for asset in ASSETS:
        path = VENDOR_DIR / asset.dest
        recorded = lock.get(asset.dest)

        if not repin and recorded and path.exists():
            if _digest(path.read_bytes()) == recorded["sha256"]:
                continue

        data = _download(asset.url)
        digest = _digest(data)

        if not repin:
            if recorded is None:
                print(f"{asset.dest} is not in the lock. Use --update to add it.")
                return 1
            if digest != recorded["sha256"]:
                print(
                    f"{asset.dest}: the server returned content that does not match the "
                    f"lock.\n  expected {recorded['sha256']}\n  got      {digest}\n"
                    "Refusing to write it. A pinned asset changing upstream is either a "
                    "republished version or a compromise; check before running --update."
                )
                return 1

        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        lock[asset.dest] = {"url": asset.url, "sha256": digest, "licence": asset.licence}
        print(f"  fetched {asset.dest} ({len(data):,} bytes)")

    for name in set(lock) - {a.dest for a in ASSETS}:
        del lock[name]
        stale = VENDOR_DIR / name
        stale.unlink(missing_ok=True)
        print(f"  removed {name}")

    _save_lock(lock)
    return check()


def main(argv: list[str] | None = None) -> int:
    """
    Command line entry point.

    Args:
        argv: Arguments, defaulting to sys.argv.

    Returns:
        Process exit code.
    """
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--check", action="store_true", help="verify against the lock")
    group.add_argument("--fetch", action="store_true", help="download missing or stale files")
    group.add_argument("--update", action="store_true", help="re-pin to the versions in this file")
    args = parser.parse_args(argv)

    if args.update:
        return fetch(repin=True)
    if args.fetch:
        return fetch(repin=False)
    return check()


if __name__ == "__main__":
    sys.exit(main())
