/**
 * Where the console server keeps the projects the new-app wizard is pointed at.
 *
 * `scripts/console_server.py` (seed_domains_and_sources) writes them into the sandbox's
 * stand-in for /var/www/src, beside its /var/www/apps. The sandbox lives in a fresh temporary
 * directory per server, so the path is read off the server's own apps directory.
 */

import type { Page } from "@playwright/test";
import path from "node:path";

export type WizardSource = "storefront" | "landing";

/** The absolute path of a seeded source on the server `page` is signed in to. */
export async function wizardSource(page: Page, name: WizardSource): Promise<string> {
  const response = await page.request.get("/api/config/apps-directory");
  if (!response.ok()) throw new Error(`GET /api/config/apps-directory answered ${String(response.status())}`);
  const { apps_directory: apps } = (await response.json()) as { apps_directory: string };
  return path.posix.join(path.posix.dirname(apps), "src", name);
}
