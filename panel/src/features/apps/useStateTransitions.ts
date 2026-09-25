import { useEffect, useRef } from "react";

import { announce } from "../../app/Announcer";
import { appStatus } from "../../components/page/status";
import type { AppInfo } from "./data";

/**
 * The sentence that tells a screen reader which apps changed state between two readings of
 * the list, or null when none did. Apps that appeared or disappeared are not transitions.
 */
export function describeTransitions(
  before: ReadonlyMap<string, string>,
  after: readonly Pick<AppInfo, "domain" | "status">[],
): { message: string; failed: boolean } | null {
  const changes: string[] = [];
  let failed = false;
  for (const app of after) {
    const was = before.get(app.domain);
    const view = appStatus(app.status);
    if (was === undefined || was === view.label) continue;
    changes.push(`${app.domain}: ${view.label}`);
    if (view.state === "failed") failed = true;
  }
  return changes.length === 0 ? null : { message: `${changes.join(". ")}.`, failed };
}

/**
 * Announces apps changing state in a live list (the `app` events refresh it): politely, or
 * assertively when one failed. The first reading is the page loading, not a transition, and
 * says nothing.
 */
export function useStateTransitions(apps: readonly AppInfo[] | undefined): void {
  const previous = useRef<Map<string, string> | null>(null);
  useEffect(() => {
    if (apps === undefined) return;
    const before = previous.current;
    previous.current = new Map(apps.map((app) => [app.domain, appStatus(app.status).label]));
    if (before === null) return;
    const change = describeTransitions(before, apps);
    if (change !== null) announce(change.message, change.failed ? "assertive" : "polite");
  }, [apps]);
}
