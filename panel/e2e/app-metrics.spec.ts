/**
 * An application's metrics against the real backend: CPU and memory from the history
 * endpoint (seeded thirty days deep for tienda.cittek.es), a sentence per chart, deploys
 * drawn by the chart itself as marker links and listed below it, adaptive axis and table time
 * labels, and the range kept in the URL. Both themes, with the CSP and console gates of the
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

  // The newest seeded deploy is two hours old: listed under the charts and marked on the
  // chart itself, inside its own figure. (A rollback test in the same worker may have added
  // deploys, and marked that one rolled back; a deploy newer than the last reading has
  // nowhere on the axis to be marked.)
  const deploys = page.getByRole("region", { name: "Deploys in this range" });
  const listed = deploys.getByRole("link", { name: /^Deploy \d+, / });
  await expect(listed.first()).toBeVisible();
  const cpuChart = page.locator("figure").filter({ hasText: "CPU" });
  const marked = cpuChart.getByRole("link", { name: /^Deploy \d+, / });
  await expect(marked.first()).toBeVisible();
  expect(await marked.count()).toBeLessThanOrEqual(await listed.count());
  // The accessible summary says how many of the chart's own markers are in view.
  await expect(cpu).toHaveAccessibleName(/\d+ markers? in view\.$/);
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

test("a deploy marker on the chart names the deploy and opens it", async ({ page, consoleServer }) => {
  await signIn(page, consoleServer, `/apps/${DOMAIN}/metrics`);
  const cpuChart = page.locator("figure").filter({ hasText: "CPU" });
  await expect(cpuChart.getByRole("img")).toBeVisible();
  // Whatever state the deploy is in by now: other tests in this worker may have rolled it back.
  const marker = cpuChart.getByRole("link", { name: /^Deploy \d+, / }).first();
  await expect(marker).toBeVisible();
  const name = await marker.evaluate((el) => el.getAttribute("aria-label"));
  await marker.click();
  await expect(page).toHaveURL(/\/apps\/[^/]+\/deployments\/\d+/);
  const id = /^Deploy (\d+),/.exec(name ?? "")?.[1];
  await expect(page.getByRole("heading", { level: 2, name: `Deployment ${id ?? ""}` })).toBeVisible();
  await settle(page);
  await expectNoA11yViolations(page, "a deployment opened from a chart marker");
});

test("the 7d range's table reads dates, not a bare clock", async ({ page, consoleServer }) => {
  await signIn(page, consoleServer, `/apps/${DOMAIN}/metrics?range=7d`);
  await expect(page.getByRole("img", { name: /^CPU, last 7 days/ })).toBeVisible();
  await page.getByRole("button", { name: "View as table" }).first().click();
  const region = page.getByRole("region", { name: "CPU data" });
  await expect(region).toBeVisible();
  // A span of a week needs a date on every row - "Sep 19" or "Sep 19 14:00" - not "14:00" alone.
  const firstCell = region.getByRole("cell").first();
  await expect(firstCell).toHaveText(/^[A-Z][a-z]{2} \d{1,2}(?: \d{2}:\d{2})?$/);
  await settle(page);
  await expectNoA11yViolations(page, "an app's metrics table over 7 days");
});

test("a static site says it has no process to measure", async ({ page, consoleServer }) => {
  await signIn(page, consoleServer, "/apps/bodas.arennalabs.com/metrics?range=30d");
  await expect(page.getByRole("heading", { level: 2, name: "A static site has no process to measure" })).toBeVisible();
  await settle(page);
  await expectNoA11yViolations(page, "a static site's metrics tab");
});
