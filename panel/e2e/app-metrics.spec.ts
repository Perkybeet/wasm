/**
 * An application's metrics against the real backend: CPU and memory from the history
 * endpoint (seeded thirty days deep for tienda.cittek.es), a sentence per chart, deploys marked
 * and listed, and the range kept in the URL. Both themes, with the CSP and console gates of the
 * `problems` fixture.
 */

import { expect, expectNoA11yViolations, settle, signIn, test } from "./fixtures";

const DOMAIN = "tienda.cittek.es";

test("the charts summarise the range in words; switching it updates the URL and the summary", async ({ page, consoleServer }) => {
  await signIn(page, consoleServer, `/apps/${DOMAIN}/metrics`);
  const cpu = page.getByRole("img", { name: /^CPU, last 24 hours/ });
  await expect(cpu).toBeVisible();
  await expect(page.getByRole("img", { name: /^Memory, last 24 hours/ })).toBeVisible();
  await expect(page.getByText(/Average .*, limit 512 MB\.$/)).toBeVisible();

  // The newest seeded deploy is two hours old: listed under the charts and marked on them. (A
  // rollback test in the same worker may have added deploys, and marked that one rolled back;
  // a deploy newer than the last reading has nowhere on the axis to be marked.)
  const deploys = page.getByRole("region", { name: "Deploys in this range" });
  const listed = deploys.getByRole("link", { name: /^Deploy \d+, / });
  await expect(listed.first()).toBeVisible();
  const marked = page.locator("figure").first().locator("..").getByRole("link", { name: /^Deploy \d+, / });
  await expect(marked.first()).toBeVisible();
  expect(await marked.count()).toBeLessThanOrEqual(await listed.count());
  await settle(page);
  await expectNoA11yViolations(page, "an app's metrics");

  await page.getByRole("radio", { name: "7d" }).click();
  await expect(page).toHaveURL(/[?&]range=7d/);
  await expect(page.getByRole("img", { name: /^CPU, last 7 days/ })).toBeVisible();
  expect(await deploys.getByRole("link").count()).toBeGreaterThanOrEqual(4);

  await page.getByRole("radio", { name: "24h" }).click();
  await expect(page).not.toHaveURL(/range=/);

  await page.getByRole("button", { name: "View as table" }).first().click();
  await expect(page.getByRole("region", { name: "CPU data" })).toBeVisible();
  await settle(page);
  await expectNoA11yViolations(page, "an app's metrics as a table");
});

test("a static site says it has no process to measure", async ({ page, consoleServer }) => {
  await signIn(page, consoleServer, "/apps/bodas.arennalabs.com/metrics?range=30d");
  await expect(page.getByRole("heading", { level: 2, name: "A static site has no process to measure" })).toBeVisible();
  await settle(page);
  await expectNoA11yViolations(page, "a static site's metrics tab");
});
