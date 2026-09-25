import { Outlet, useNavigate, useRouter, useRouterState } from "@tanstack/react-router";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { MouseEvent } from "react";

import { Drawer } from "../components/ui/Drawer";
import { useServerEvents } from "../realtime/events";
import { CommandPalette } from "./CommandPalette";
import { ErrorBoundary } from "./ErrorBoundary";
import { focusPageTitle } from "./focus";
import { NAV_GROUPS, SETTINGS_ITEM } from "./nav";
import { useKeyboardShortcuts } from "./shortcuts";
import type { KeyBinding } from "./shortcuts";
import { ShortcutsDialog } from "./ShortcutsDialog";
import { Sidebar, SidebarFooter, SidebarNav } from "./Sidebar";
import { Topbar } from "./Topbar";
import { useConsoleCommands } from "./useConsoleCommands";

/** The first focusable element of every page: straight past the navigation to the content. */
function SkipLink() {
  const skip = (event: MouseEvent<HTMLAnchorElement>): void => {
    event.preventDefault();
    const main = document.getElementById("main");
    main?.focus();
    main?.scrollIntoView({ block: "start" });
  };
  return (
    <a
      href="#main"
      onClick={skip}
      className="fixed top-2 left-2 z-[70] -translate-y-[200%] rounded-control bg-surface-raised px-3 py-2 text-13 font-medium text-fg opacity-0 shadow-overlay focus:translate-y-0 focus:opacity-100 focus-visible:outline-2 focus-visible:outline-focus"
    >
      Skip to content
    </a>
  );
}

const APP_PATH = /^\/apps\/([^/]+)(?:\/|$)/;

/**
 * The frame around every signed-in page: sidebar, topbar with the machine strip, the page,
 * and the three overlays anyone can summon from anywhere (palette, shortcuts, mobile menu).
 * It also opens the live event stream and moves focus to each new page's heading.
 */
export function Shell() {
  const navigate = useNavigate();
  const router = useRouter();
  const pathname = useRouterState({ select: (state) => state.location.pathname });

  const [paletteOpen, setPaletteOpen] = useState(false);
  const [shortcutsOpen, setShortcutsOpen] = useState(false);
  const [navOpen, setNavOpen] = useState(false);
  const returnFocus = useRef<HTMLElement | null>(null);
  // Choosing a page in the menu hands focus to that page's heading, not back to the button.
  const [menuNavigated, setMenuNavigated] = useState(false);

  useServerEvents();

  useEffect(
    () =>
      router.subscribe("onRendered", (event) => {
        if (!event.pathChanged || event.fromLocation === undefined) return;
        setNavOpen(false);
        focusPageTitle();
      }),
    [router],
  );

  const openPalette = useCallback(() => {
    returnFocus.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    setPaletteOpen(true);
  }, []);
  const openShortcuts = useCallback(() => {
    setShortcutsOpen(true);
  }, []);

  // Mod+K works everywhere, fields included: it is a chord, not text.
  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent): void => {
      if (event.key.toLowerCase() !== "k" || !(event.metaKey || event.ctrlKey) || event.altKey || event.shiftKey) return;
      event.preventDefault();
      if (paletteOpen) {
        setPaletteOpen(false);
        return;
      }
      // Another dialog owns the keyboard until it is closed.
      if (document.querySelector('[role="dialog"], [role="alertdialog"]')) return;
      openPalette();
    };
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("keydown", onKeyDown);
    };
  }, [paletteOpen, openPalette]);

  const bindings = useMemo<KeyBinding[]>(() => {
    const go = (to: string) => () => {
      void navigate({ to });
    };
    const navShortcuts = [...NAV_GROUPS.flat(), SETTINGS_ITEM].flatMap((item) =>
      item.shortcut ? [{ keys: item.shortcut, description: `Go to ${item.label.toLowerCase()}`, run: go(item.to) }] : [],
    );
    return [
      ...navShortcuts,
      {
        keys: ["g", "d"],
        description: "Go to deployments, or activity outside an app",
        run: () => {
          const match = APP_PATH.exec(router.state.location.pathname);
          const domain = match?.[1];
          if (domain !== undefined && domain !== "new") {
            void navigate({ to: "/apps/$domain/deployments", params: { domain: decodeURIComponent(domain) } });
          } else {
            void navigate({ to: "/activity" });
          }
        },
      },
      {
        keys: ["/"],
        description: "Search this page, or everything",
        run: () => {
          const search = document.querySelector<HTMLElement>("main [data-page-search]");
          if (search) search.focus();
          else openPalette();
        },
      },
      { keys: ["?"], description: "Show keyboard shortcuts", run: openShortcuts },
    ];
  }, [navigate, router, openPalette, openShortcuts]);

  useKeyboardShortcuts(bindings);

  const commands = useConsoleCommands(paletteOpen, openShortcuts);

  const closeMenuAfterNavigation = useCallback(() => {
    setMenuNavigated(true);
    setNavOpen(false);
  }, []);

  return (
    <div className="flex min-h-dvh bg-bg text-fg">
      <SkipLink />
      <Sidebar />
      <div className="flex min-w-0 flex-1 flex-col">
        <Topbar
          onOpenPalette={openPalette}
          onOpenShortcuts={openShortcuts}
          onOpenNav={() => {
            setMenuNavigated(false);
            setNavOpen(true);
          }}
        />
        <main id="main" tabIndex={-1} className="flex-1 outline-none">
          <div className="mx-auto w-full max-w-[1200px] px-4 pt-6 pb-16 sm:px-6 lg:px-8 lg:pt-8">
            <ErrorBoundary resetKey={pathname}>
              <Outlet />
            </ErrorBoundary>
          </div>
        </main>
      </div>

      <Drawer
        open={navOpen}
        onOpenChange={setNavOpen}
        title="Menu"
        finalFocus={!menuNavigated}
      >
        <div className="flex min-h-full flex-col justify-between gap-8">
          <SidebarNav onNavigate={closeMenuAfterNavigation} className="-mx-2" />
          <div className="-mx-2 border-t border-border pt-3">
            <SidebarFooter onNavigate={closeMenuAfterNavigation} />
          </div>
        </div>
      </Drawer>

      <CommandPalette
        open={paletteOpen}
        onOpenChange={setPaletteOpen}
        commands={commands}
        returnFocus={() => returnFocus.current}
      />
      <ShortcutsDialog open={shortcutsOpen} onOpenChange={setShortcutsOpen} shortcuts={bindings} />
    </div>
  );
}
