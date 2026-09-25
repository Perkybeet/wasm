import { useQuery } from "@tanstack/react-query";
import { useNavigate } from "@tanstack/react-router";
import { Box, Keyboard, LogOut, Monitor, Moon, Plus, Sun } from "lucide-react";
import { useMemo } from "react";

import { appState, appsQuery } from "../api/queries/apps";
import { useSignOut } from "../features/auth/useSignOut";
import type { Command } from "./CommandPalette";
import { NAV_GROUPS, SETTINGS_ITEM, SETTINGS_TABS } from "./nav";
import { useTheme } from "./theme";

const THEME_ICONS = { system: Monitor, light: Sun, dark: Moon } as const;

/**
 * What the palette can do: every page, every application (loaded when the palette opens),
 * and the actions that are not a page.
 */
export function useConsoleCommands(open: boolean, openShortcuts: () => void): Command[] {
  const navigate = useNavigate();
  const [theme, setTheme] = useTheme();
  const { signOut } = useSignOut();
  const { data: apps } = useQuery({ ...appsQuery(), enabled: open });

  return useMemo(() => {
    const go = (to: string, params?: Record<string, string>) => () => {
      void navigate({ to, ...(params ? { params: params as never } : {}) });
    };

    const pages: Command[] = [...NAV_GROUPS.flat(), SETTINGS_ITEM].map((item) => {
      const Icon = item.icon;
      return {
        id: `page:${item.to}`,
        group: "Pages",
        label: item.label,
        icon: <Icon />,
        kind: "navigate",
        run: go(item.to),
        ...(item.keywords !== undefined ? { keywords: item.keywords } : {}),
        ...(item.shortcut !== undefined ? { shortcut: item.shortcut } : {}),
      };
    });
    const settingsIcon = <SETTINGS_ITEM.icon />;
    for (const tab of SETTINGS_TABS.slice(1)) {
      pages.push({
        id: `page:${tab.to}`,
        group: "Pages",
        label: `${tab.label} settings`.replace("API tokens settings", "API tokens"),
        icon: settingsIcon,
        kind: "navigate",
        run: go(tab.to),
        ...(tab.keywords !== undefined ? { keywords: `settings ${tab.keywords}` } : {}),
      });
    }

    const applications: Command[] = (apps?.apps ?? []).map((app) => ({
      id: `app:${app.domain}`,
      group: "Applications",
      label: app.domain,
      icon: <Box />,
      keywords: [app.name, app.app_type ?? ""].join(" "),
      status: appState(app.status),
      kind: "navigate",
      run: go("/apps/$domain", { domain: app.domain }),
    }));

    const actions: Command[] = [
      { id: "action:new-app", group: "Actions", label: "New application", icon: <Plus />, keywords: "deploy create", kind: "navigate", run: go("/apps/new") },
      ...(["dark", "light", "system"] as const)
        .filter((choice) => choice !== theme)
        .map((choice): Command => {
          const Icon = THEME_ICONS[choice];
          return {
            id: `action:theme-${choice}`,
            group: "Actions",
            label: choice === "system" ? "Follow the system theme" : `Switch to the ${choice} theme`,
            icon: <Icon />,
            keywords: "theme appearance colour color mode",
            kind: "action",
            run: () => {
              setTheme(choice);
            },
          };
        }),
      { id: "action:shortcuts", group: "Actions", label: "Keyboard shortcuts", icon: <Keyboard />, shortcut: ["?"], keywords: "help keys", kind: "action", run: openShortcuts },
      { id: "action:sign-out", group: "Actions", label: "Sign out", icon: <LogOut />, keywords: "log out logout exit", kind: "navigate", run: () => void signOut() },
    ];

    return [...pages, ...applications, ...actions];
  }, [apps, navigate, theme, setTheme, openShortcuts, signOut]);
}
