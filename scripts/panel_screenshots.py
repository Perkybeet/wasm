#!/usr/bin/env python3
# Copyright (c) 2024-2026 Yago Lopez Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
Capture review screenshots of every principal panel screen, in both themes.

``scripts/panel_browser_check.py`` proves the shell behaves; this script
exists so a person can judge how it *looks*, which no assertion can. It
reuses the same harness - the in-process panel seeded by
``tests/panel_factory.py`` - and drives it through Chromium at a desktop
viewport, saving one PNG per screen per theme, plus a phone-sized capture of
the dashboard.

Usage:
    python scripts/panel_screenshots.py --out /tmp/panel-screens

Themes are switched with Playwright's colour-scheme emulation: the shell
renders no explicit data-theme by default, so the stylesheet's
prefers-color-scheme block decides, exactly as it does for an operator who
never touched the toggle.
"""

from __future__ import annotations

import argparse
import contextlib
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from panel_browser_check import Panel

#: The desktop review viewport.
DESKTOP = {"width": 1440, "height": 900}

#: The phone review viewport.
PHONE = {"width": 390, "height": 844}

#: What is captured: name, path, whether the whole page height is wanted.
#: The application detail is captured full-page because its lazily loaded
#: sections are exactly what needs reviewing.
SCREENS: tuple[tuple[str, str, bool], ...] = (
    ("dashboard", "/", False),
    ("apps", "/apps", False),
    ("app", "/apps/picconia.com", True),
    ("databases", "/databases", True),
    ("deployments", "/deployments", False),
    ("settings", "/settings", True),
)

#: Milliseconds to let charts, SSE swaps and lazily loaded sections land.
SETTLE = 2600


def sign_in(page: Any, panel: Panel) -> None:
    """
    Args:
        page: The browser page to authenticate.
        panel: The running panel, for its URL and token.
    """
    page.goto(f"{panel.url}/login")
    page.fill("#token", panel.token)
    page.click("button[type=submit]")
    page.wait_for_url(f"{panel.url}/")


def capture(out: Path) -> int:
    """
    Run the panel and photograph it.

    Args:
        out: Directory the PNGs are written into.

    Returns:
        A process exit status.
    """
    from playwright.sync_api import sync_playwright

    # Livelier counts than the behaviour check: a failed application, real
    # certificate paths and a deployment history give the reviewer states to
    # look at instead of a wall of green.
    panel = Panel(certs=2, backups=1, failed=1, deployments=4)
    panel.start()
    out.mkdir(parents=True, exist_ok=True)
    written: list[str] = []

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()

            for theme in ("light", "dark"):
                context = browser.new_context(viewport=DESKTOP, color_scheme=theme)
                page = context.new_page()

                page.goto(f"{panel.url}/login")
                page.wait_for_selector(".auth__panel")
                page.screenshot(path=str(out / f"login-{theme}.png"))
                written.append(f"login-{theme}.png")

                sign_in(page, panel)
                for name, path, full in SCREENS:
                    page.goto(f"{panel.url}{path}")
                    page.wait_for_timeout(SETTLE)
                    page.screenshot(path=str(out / f"{name}-{theme}.png"), full_page=full)
                    written.append(f"{name}-{theme}.png")
                context.close()

                phone = browser.new_context(viewport=PHONE, color_scheme=theme)
                phone_page = phone.new_page()
                sign_in(phone_page, panel)
                phone_page.wait_for_timeout(SETTLE)
                phone_page.screenshot(path=str(out / f"dashboard-mobile-{theme}.png"))
                written.append(f"dashboard-mobile-{theme}.png")
                phone.close()

            browser.close()
    finally:
        panel.stop()

    for name in written:
        print(f"wrote {out / name}")
    print(f"{len(written)} screenshots")
    return 0


def main() -> int:
    """
    Returns:
        A process exit status.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True, help="Directory to write PNGs into")
    args = parser.parse_args()

    try:
        with contextlib.suppress(KeyboardInterrupt):
            return capture(args.out)
        return 130
    except ModuleNotFoundError as exc:
        if exc.name != "playwright":
            raise
        print(
            "Playwright is not installed, so this script cannot open a browser.\n"
            "    pip install playwright\n"
            "    playwright install --with-deps chromium\n",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
