/**
 * An application's Diagnose tab against the real backend: the probes of `wasm diagnose` run
 * over the seeded machine, the verdict and the probable cause first, every check with its
 * status as a word and its output verbatim, and "Run again" asking the machine again. Both
 * themes, with the CSP and console gates of the `problems` fixture.
 */

import type { Page } from "@playwright/test";

import { expect, expectNoA11yViolations, settle, signIn, test } from "./fixtures";

/** Its unit is failed: systemd gave up restarting it. */
const FAILED = "clientes.arennalabs.com";
/** On releases, running and answering. */
const RUNNING = "tienda.cittek.es";

function verdict(page: Page) {
  return page.getByRole("heading", { level: 2, name: /^Verdict:/ });
}

test("a failed app: the verdict and its probable cause first, then every check verbatim", async ({ page, consoleServer }) => {
  await signIn(page, consoleServer, `/apps/${FAILED}/diagnose`);
  await expect(verdict(page)).toBeVisible();
  await expect(verdict(page).locator("[data-verdict]")).not.toHaveAttribute("data-verdict", "healthy");
  await expect(page.getByText(/^(Probable cause|No single cause)$/)).toBeVisible();

  const checks = page.getByRole("region", { name: "Checks" });
  const rows = checks.getByRole("listitem");
  expect(await rows.count()).toBeGreaterThan(5);
  const unit = rows.filter({ hasText: "Service unit" });
  await expect(unit.getByText("Failed", { exact: true })).toBeVisible();

  // Every output is the system's own, in mono, one press away.
  const journal = rows.filter({ hasText: "Journal" });
  // Open unless it already is: a journal with errors in it is a warning, open from the start.
  if (!(await journal.locator("details").evaluate((details) => (details as HTMLDetailsElement).open))) {
    await journal.locator("summary").click();
  }
  await expect(journal.locator("pre")).toBeVisible();
  await expect(journal.locator("pre")).toContainText("Start request repeated too quickly");

  await settle(page);
  await expectNoA11yViolations(page, "a failed app's diagnosis");
});

test("Run again asks the machine again and keeps the page", async ({ page, consoleServer }) => {
  await signIn(page, consoleServer, `/apps/${RUNNING}/diagnose`);
  await expect(verdict(page)).toBeVisible();
  const unit = page.getByRole("region", { name: "Checks" }).getByRole("listitem").filter({ hasText: "Service unit" });
  await expect(unit.getByText("Passed", { exact: true })).toBeVisible();

  const again = page.waitForResponse((response) => response.url().endsWith(`/api/apps/${RUNNING}/diagnose`) && response.ok());
  await page.getByRole("button", { name: "Run again" }).click();
  await again;
  await expect(page.getByRole("button", { name: "Run again" })).not.toHaveAttribute("aria-busy");
  await expect(page.getByText(/^Checked /)).toContainText("just now");
  await expect(verdict(page)).toBeVisible();

  await expect(page.getByText(`wasm diagnose ${RUNNING}`)).toBeVisible();
  await settle(page);
  await expectNoA11yViolations(page, "a running app's diagnosis");
});
