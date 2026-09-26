/**
 * Where the console server keeps the projects the new-app wizard is pointed at.
 *
 * `scripts/console_server.py` (seed_domains_and_sources) writes them into the sandbox's
 * stand-in for /var/www/src, beside its /var/www/apps. The sandbox lives in a fresh temporary
 * directory per server, so the path is read off the server's own apps directory.
 */

import type { Page } from "@playwright/test";
import path from "node:path";

import { confirmItsYou, expect } from "./fixtures";
import type { ConsoleServer, PageProblems } from "./fixtures";

/**
 * Chromium's log of the refusal a local-path inspection gets before sudo mode: reading a
 * directory on the server as root is an elevated action, so the first inspection of a session
 * is answered 403 `elevation_required` and retried after "Confirm it's you".
 */
const INSPECT_NEEDS_SUDO = /status of 403 .* \/api\/apps\/inspect$/;

export type WizardSource = "storefront" | "landing";

/** The absolute path of a seeded source on the server `page` is signed in to. */
export async function wizardSource(page: Page, name: WizardSource): Promise<string> {
  const response = await page.request.get("/api/config/apps-directory");
  if (!response.ok()) throw new Error(`GET /api/config/apps-directory answered ${String(response.status())}`);
  const { apps_directory: apps } = (await response.json()) as { apps_directory: string };
  return path.posix.join(path.posix.dirname(apps), "src", name);
}

/**
 * Inspects a source from the wizard's first step and waits for the Review step, confirming
 * it's the operator when the session is not in sudo mode yet.
 */
export async function inspectSource(page: Page, server: ConsoleServer, problems: PageProblems, source: string): Promise<void> {
  problems.expect(INSPECT_NEEDS_SUDO);
  await page.getByLabel("Repository or directory").fill(source);
  await page.getByRole("button", { name: "Inspect source" }).click();
  const confirm = page.getByRole("dialog", { name: "Confirm it's you" });
  const review = page.getByRole("heading", { level: 2, name: "Review" });
  await expect(confirm.or(review)).toBeVisible();
  if (await confirm.isVisible()) await confirmItsYou(page, server);
  await expect(review).toBeFocused();
}
