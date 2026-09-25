/**
 * The console's destinations, written once. The sidebar, the mobile menu, the command
 * palette, the `g` shortcuts and the tab bars all read from here, so a page cannot be
 * reachable from one and missing from another.
 */

import {
  Archive,
  Boxes,
  Clock,
  Cog,
  Database,
  Gauge,
  History,
  Server,
  Settings,
  ShieldCheck,
} from "lucide-react";
import type { LucideIcon } from "lucide-react";

import type { FileRouteTypes } from "../routeTree.gen";

/** Every path of the console, as the router knows it. */
export type ConsolePath = FileRouteTypes["to"];

export interface NavItem {
  label: string;
  to: ConsolePath;
  icon: LucideIcon;
  /** The two-key shortcut that opens it, such as ["g", "a"]. */
  shortcut?: readonly string[];
  /** Other words an operator might type for it in the palette. */
  keywords?: string;
}

/** The sidebar, top to bottom. Groups are separated by space, not labels. */
export const NAV_GROUPS: readonly (readonly NavItem[])[] = [
  [{ label: "Overview", to: "/", icon: Gauge, shortcut: ["g", "o"], keywords: "home dashboard health" }],
  [
    { label: "Applications", to: "/apps", icon: Boxes, shortcut: ["g", "a"], keywords: "apps sites deploy" },
    { label: "Databases", to: "/databases", icon: Database, shortcut: ["g", "b"], keywords: "mysql postgres redis mongodb sql" },
    { label: "Services", to: "/services", icon: Cog, keywords: "systemd units daemons" },
    { label: "Cron", to: "/cron", icon: Clock, keywords: "schedule timers jobs" },
  ],
  [
    { label: "Domains and certificates", to: "/domains", icon: ShieldCheck, keywords: "ssl tls https certbot nginx apache sites" },
    { label: "Backups", to: "/backups", icon: Archive, keywords: "restore snapshots" },
  ],
  [
    { label: "Activity", to: "/activity", icon: History, keywords: "audit log jobs history deployments" },
    { label: "Server", to: "/server", icon: Server, keywords: "machine health cpu memory disk processes monitor" },
  ],
];

export const SETTINGS_ITEM: NavItem = {
  label: "Settings",
  to: "/settings",
  icon: Settings,
  shortcut: ["g", "s"],
  keywords: "preferences configuration",
};

export interface TabItem {
  label: string;
  to: ConsolePath;
  /** Active only on this exact path (the first tab), not on paths beneath it. */
  exact?: boolean;
  keywords?: string;
}

/** An application's sections (D8). Each is a URL under /apps/$domain. */
export const APP_TABS: readonly TabItem[] = [
  { label: "Overview", to: "/apps/$domain", exact: true },
  { label: "Deployments", to: "/apps/$domain/deployments", keywords: "deploys builds history rollback" },
  { label: "Logs", to: "/apps/$domain/logs", keywords: "journal output" },
  { label: "Metrics", to: "/apps/$domain/metrics", keywords: "cpu memory charts" },
  { label: "Environment", to: "/apps/$domain/environment", keywords: "env variables secrets" },
  { label: "Domains", to: "/apps/$domain/domains", keywords: "certificate ssl www" },
  { label: "Diagnose", to: "/apps/$domain/diagnose", keywords: "down why broken health" },
  { label: "Settings", to: "/apps/$domain/settings", keywords: "webhook source build port delete" },
];

export const SETTINGS_TABS: readonly TabItem[] = [
  { label: "General", to: "/settings", exact: true, keywords: "apps directory web server email" },
  { label: "Security", to: "/settings/security", keywords: "two-factor 2fa totp sessions lockout" },
  { label: "Notifications", to: "/settings/notifications", keywords: "alerts email slack webhook channels" },
  { label: "API tokens", to: "/settings/tokens", keywords: "automation ci scope" },
  { label: "About", to: "/settings/about", keywords: "version update" },
];
