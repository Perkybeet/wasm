#!/usr/bin/env python3
# Copyright (c) 2024-2026 Yago Lopez Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
Drive the panel in a real browser and report what a person would see.

The suite proves the server sends the right HTML and that every address in it
resolves. It cannot prove that the sidebar stays put when the page scrolls,
that the log drawer opens, or that the navigation is reachable on a phone,
because all three are decided by a layout engine. Those are exactly the things
that were broken, and the reason they shipped is that nothing here ever ran.

This is a script rather than a test because it needs Chromium, which is a
150 MB download that must not be a condition of running ``pytest`` on a build
host with no network. It is meant to be run by hand, and by anyone reviewing a
change to the shell:

    python scripts/panel_browser_check.py --shots /tmp/panel

Exits non-zero when a check fails, so it can also be wired into CI on a runner
that has a browser.
"""

from __future__ import annotations

import argparse
import contextlib
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

# This is a script, not a test, so pytest never puts the repository root on
# sys.path for it. It still wants tests/panel_factory.py: that module is the
# one implementation of the panel's seed data (CLAUDE.md rule 3), shared with
# tests/test_panel_factory.py and any future test that wants a populated
# panel, and this is the other caller.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

#: Where the panel is served for the duration of the run.
HOST = "127.0.0.1"

#: A viewport wide enough for the sidebar to be permanent.
DESKTOP = {"width": 1280, "height": 800}

#: A viewport narrow enough for the off-canvas navigation.
PHONE = {"width": 390, "height": 780}


def free_port() -> int:
    """
    Returns:
        A port nothing is listening on right now.
    """
    with socket.socket() as probe:
        probe.bind((HOST, 0))
        return int(probe.getsockname()[1])


class Panel:
    """The panel, running in this process, with a machine's worth of data.

    The seed counts default to what this script has always built: every
    domain gets a service and a site, none failed, no explicit certificate
    paths and no deployment history. ``scripts/panel_screenshots.py`` reuses
    this harness with livelier counts, so the review captures show state.
    """

    def __init__(
        self,
        *,
        certs: int = 0,
        backups: int = 0,
        failed: int = 0,
        deployments: int = 0,
    ) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="wasm-browser-check-"))
        self.port = free_port()
        self.token = ""
        self._server: Any = None
        self._certs = certs
        self._backups = backups
        self._failed = failed
        self._deployments = deployments

    @property
    def url(self) -> str:
        """
        Returns:
            The panel's base URL.
        """
        return f"http://{HOST}:{self.port}"

    def start(self) -> None:
        """Populate a store, build the app and serve it on a thread."""
        from tests.panel_factory import DEFAULT_DOMAINS, seed_panel_state
        from wasm.core.store import WASMStore

        WASMStore.reset_instance()
        store = WASMStore(self.root / "wasm.db")

        # A screen with nothing on it exercises none of the layout that broke.
        # These counts reproduce, field for field, what this script used to
        # build inline: every one of the eight domains gets a service and a
        # site, none of them failed, none carries an explicit certificate
        # path. tests/test_panel_factory.py pins that equivalence down.
        domain_count = len(DEFAULT_DOMAINS)
        seed_panel_state(
            store,
            apps=domain_count,
            services=domain_count,
            sites=domain_count,
            certs=self._certs,
            backups=self._backups,
            failed=self._failed,
            deployments=self._deployments,
        )

        import uvicorn

        from wasm.web.auth import SecurityConfig
        from wasm.web.server import create_app, get_token_manager

        app = create_app(SecurityConfig(state_dir=self.root / "state", rate_limit_requests=5000))
        self.token = get_token_manager().generate_master_token()

        self._server = uvicorn.Server(
            uvicorn.Config(app, host=HOST, port=self.port, log_level="error")
        )
        threading.Thread(target=self._server.run, daemon=True).start()
        for _ in range(200):
            if self._server.started:
                return
            time.sleep(0.05)
        raise RuntimeError("the panel did not start")

    def stop(self) -> None:
        """Ask the server to exit."""
        if self._server is not None:
            self._server.should_exit = True


class Checks:
    """Collects results so every check runs before anything is reported."""

    def __init__(self) -> None:
        self.results: list[tuple[bool, str, str]] = []

    def record(self, ok: bool, name: str, detail: str = "") -> None:
        """
        Args:
            ok: Whether the check passed.
            name: What was checked.
            detail: What was actually seen.
        """
        self.results.append((ok, name, detail))

    def report(self) -> int:
        """
        Print every result.

        Returns:
            A process exit status.
        """
        failed = 0
        for ok, name, detail in self.results:
            print(f"{'ok  ' if ok else 'FAIL'} {name}{f'  [{detail}]' if detail else ''}")
            failed += 0 if ok else 1

        print()
        print(f"{len(self.results) - failed}/{len(self.results)} checks passed")
        return 1 if failed else 0


def run(shots: Path | None) -> int:
    """
    Open the panel in Chromium and check what renders.

    Args:
        shots: Directory to write screenshots to, or None to skip them.

    Returns:
        A process exit status.
    """
    from playwright.sync_api import sync_playwright

    panel = Panel()
    panel.start()
    checks = Checks()

    if shots:
        shots.mkdir(parents=True, exist_ok=True)

    def shot(page: Any, name: str) -> None:
        """
        Args:
            page: The page to capture.
            name: File name, without extension.
        """
        if shots:
            page.screenshot(path=str(shots / f"{name}.png"), full_page=False)

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            context = browser.new_context(viewport=DESKTOP)
            page = context.new_page()

            console: list[str] = []
            page.on("console", lambda m: console.append(f"{m.type}: {m.text}"))
            failures: list[str] = []
            page.on("pageerror", lambda e: failures.append(str(e)))

            # ---------------------------------------------------------- sign in
            page.goto(f"{panel.url}/login")
            shot(page, "01-login")

            checks.record(
                page.locator(".auth__panel").is_visible(),
                "the sign-in form sits on a surface",
            )
            box = page.locator(".auth__panel").bounding_box() or {}
            checks.record(
                bool(box) and box["width"] < DESKTOP["width"] * 0.5,
                "the sign-in panel is a panel, not the whole width",
                f"{box.get('width')}px",
            )

            page.fill("#token", panel.token)
            page.click("button[type=submit]")
            page.wait_for_url(f"{panel.url}/")
            shot(page, "02-dashboard")

            # ------------------------------------------------- the scroll bug
            page.goto(f"{panel.url}/apps")
            page.wait_for_selector(".row")
            strip_before = (page.locator("#machine-strip").bounding_box() or {}).get("y")
            nav_before = (page.locator(".sidebar .nav__item").first.bounding_box() or {}).get("y")

            page.mouse.wheel(0, 4000)
            page.wait_for_timeout(400)

            strip_after = (page.locator("#machine-strip").bounding_box() or {}).get("y")
            nav_after = (page.locator(".sidebar .nav__item").first.bounding_box() or {}).get("y")
            scrolled = page.evaluate("window.scrollY")

            shot(page, "03-apps-scrolled")

            checks.record(
                scrolled > 100,
                "the page actually scrolled",
                f"scrollY={scrolled}",
            )
            checks.record(
                strip_after is not None and strip_after >= -1,
                "the machine strip stays on screen when the page scrolls",
                f"y {strip_before} -> {strip_after}",
            )
            checks.record(
                nav_after is not None and nav_after >= -1,
                "the navigation stays on screen when the page scrolls",
                f"y {nav_before} -> {nav_after}",
            )
            checks.record(
                page.locator(".sidebar__footer button", has_text="Sign out").is_visible(),
                "sign out is visible without hunting for it",
            )

            # A flex column stretches its children, so the state badge rendered
            # as a coloured bar the full width of the row: a green slab on every
            # running application, against a design rule that says anything
            # coloured on screen is telling the operator something.
            badge = page.locator(".row .badge").first.bounding_box() or {}
            row = page.locator(".row").first.bounding_box() or {}
            share = (badge.get("width", 0) / row["width"]) if row.get("width") else 1
            checks.record(
                share < 0.35,
                "the state badge is a label, not a banner across the row",
                f"{share:.0%} of the row",
            )

            # --------------------------------------------------- the log drawer
            page.goto(f"{panel.url}/apps")
            page.wait_for_selector("[data-follow-log]")
            drawer_open_before = page.locator("#log-drawer").get_attribute("data-open")
            page.locator("[data-follow-log]").first.click()
            page.wait_for_timeout(1200)
            drawer_open_after = page.locator("#log-drawer").get_attribute("data-open")
            shot(page, "04-log-drawer")

            checks.record(
                drawer_open_after == "true",
                "clicking Logs opens the drawer",
                f"data-open {drawer_open_before} -> {drawer_open_after}",
            )
            checks.record(
                page.evaluate("!!window.wasmPanel && !!window.wasmPanel.followLog"),
                "the client registered its drawer hooks",
            )
            checks.record(
                page.evaluate(
                    "getComputedStyle(document.documentElement)"
                    ".getPropertyValue('--log-drawer-offset').trim() !== ''"
                ),
                "the open drawer pushes the notices stack clear of itself",
            )

            # -------------------------------------------------- deploy screen
            page.goto(f"{panel.url}/apps/new")
            page.wait_for_selector("form")
            shot(page, "05-deploy")
            checks.record(
                page.locator("#domain").is_visible() and page.locator("#source").is_visible(),
                "the deployment form renders its fields",
            )
            checks.record(
                page.locator("#app_type option").count() >= 8,
                "every deployer type is offered",
                f"{page.locator('#app_type option').count()} options",
            )

            # ------------------------------------------------------ the feed
            page.goto(f"{panel.url}/")
            page.wait_for_timeout(1500)
            checks.record(
                not any("Lost the live connection" in message for message in console),
                "the live feed connects instead of reporting a dropped connection",
                next((m for m in console if "Lost the live" in m), ""),
            )

            # ------------------------------------------- the command palette
            page.keyboard.press("Control+k")
            page.wait_for_timeout(300)
            checks.record(
                bool(page.evaluate("document.getElementById('command-palette').open")),
                "Ctrl+K opens the command palette",
            )
            page.keyboard.type("settings")
            page.wait_for_timeout(300)
            first = page.locator("#palette-list .palette__item").first
            checks.record(
                first.count() > 0 and "settings" in first.inner_text().lower(),
                "typing filters the palette down to what was asked for",
                first.inner_text().strip() if first.count() else "no items",
            )
            shot(page, "08-palette")
            page.keyboard.press("Escape")
            page.wait_for_timeout(300)
            checks.record(
                not page.evaluate("document.getElementById('command-palette').open"),
                "Escape closes the palette again",
            )

            # --------------------------------------------------------- mobile
            phone = browser.new_context(viewport=PHONE)
            phone_page = phone.new_page()
            phone_page.goto(f"{panel.url}/login")
            phone_page.fill("#token", panel.token)
            phone_page.click("button[type=submit]")
            phone_page.wait_for_url(f"{panel.url}/")
            phone_page.goto(f"{panel.url}/apps")
            phone_page.wait_for_selector(".nav-toggle")
            shot(phone_page, "06-phone-closed")

            sidebar = phone_page.locator(".sidebar")
            offscreen = (sidebar.bounding_box() or {"x": 0})["x"] < 0
            checks.record(offscreen, "the sidebar is off-canvas on a phone")

            phone_page.locator(".nav-toggle").click()
            phone_page.wait_for_timeout(500)
            shot(phone_page, "07-phone-open")

            onscreen = (sidebar.bounding_box() or {"x": -1})["x"] >= 0
            checks.record(onscreen, "the toggle brings the navigation on screen")
            checks.record(
                phone_page.locator(".sidebar__footer button", has_text="Sign out").is_visible(),
                "sign out is reachable on a phone",
            )

            phone_page.keyboard.press("Escape")
            phone_page.wait_for_timeout(400)
            checks.record(
                (sidebar.bounding_box() or {"x": 0})["x"] < 0,
                "Escape closes the navigation again",
            )

            # ------------------------------------------------- console health
            checks.record(not failures, "no uncaught JavaScript errors", "; ".join(failures[:3]))
            errors = [m for m in console if m.startswith("error")]
            checks.record(
                not errors, "nothing logged an error to the console", "; ".join(errors[:3])
            )

            browser.close()
    finally:
        panel.stop()

    return checks.report()


def main() -> int:
    """
    Returns:
        A process exit status.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--shots", type=Path, default=None, help="Write screenshots here")
    args = parser.parse_args()

    try:
        with contextlib.suppress(KeyboardInterrupt):
            return run(args.shots)
        return 130
    except ModuleNotFoundError as exc:
        # A 150 MB browser download is not a condition of running the rest of
        # the suite, so it is not a project dependency: the traceback this
        # would otherwise print is correct but unhelpful about what to do
        # next.
        if exc.name != "playwright":
            raise
        print(
            "Playwright is not installed, so this check cannot open a browser.\n"
            "Install it and its Chromium runtime, then try again:\n\n"
            "    pip install playwright\n"
            "    playwright install --with-deps chromium\n",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
