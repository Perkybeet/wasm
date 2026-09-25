/**
 * The applications list's filters, which live in the URL (`/apps?q=shop&state=failed&type=nextjs`)
 * so a filtered view can be bookmarked, shared and reached with the back button.
 */

import type { Status } from "../../components/ui/StatusPill";
import { appStatus } from "../../components/page/status";
import type { AppInfo } from "./data";

/** The states the list filters by: the console's state language. */
export const STATE_FILTERS = ["running", "deploying", "failed", "stopped", "static", "unknown"] as const satisfies readonly Status[];

export type StateFilter = (typeof STATE_FILTERS)[number];

export interface AppsSearch {
  /** Free text matched against the domain and the type. */
  q?: string;
  state?: StateFilter;
  /** An app type exactly as the backend names it: `nextjs`, `static`, `python`. */
  type?: string;
}

function text(value: unknown): string | undefined {
  if (typeof value !== "string") return undefined;
  const trimmed = value.trim();
  return trimmed === "" ? undefined : trimmed.slice(0, 200);
}

/**
 * Reads the search params, dropping anything malformed instead of failing: a hand-edited or
 * stale link still opens the list, just less filtered.
 */
export function validateAppsSearch(search: Record<string, unknown>): AppsSearch {
  const q = text(search["q"]);
  const state = text(search["state"]);
  const type = text(search["type"]);
  return {
    ...(q !== undefined ? { q } : {}),
    ...(state !== undefined && (STATE_FILTERS as readonly string[]).includes(state) ? { state: state as StateFilter } : {}),
    ...(type !== undefined && /^[\w.-]+$/.test(type) ? { type } : {}),
  };
}

/** Whether any filter narrows the list. */
export function isFiltered(search: AppsSearch): boolean {
  return search.q !== undefined || search.state !== undefined || search.type !== undefined;
}

/** The apps a search keeps, in their original order. */
export function filterApps(apps: readonly AppInfo[], search: AppsSearch): AppInfo[] {
  const needle = search.q?.toLowerCase();
  return apps.filter((app) => {
    if (search.state !== undefined && appStatus(app.status).state !== search.state) return false;
    if (search.type !== undefined && app.app_type !== search.type) return false;
    if (needle !== undefined) {
      const haystack = `${app.domain} ${app.app_type ?? ""}`.toLowerCase();
      if (!haystack.includes(needle)) return false;
    }
    return true;
  });
}

/** Every app type present, alphabetically, for the type filter. */
export function appTypes(apps: readonly AppInfo[]): string[] {
  return [...new Set(apps.map((app) => app.app_type).filter((type): type is string => typeof type === "string" && type !== ""))].sort();
}
