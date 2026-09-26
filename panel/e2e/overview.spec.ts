/**
 * The overview against the real backend and its seeded machine: the failed app under Needs
 * attention linking to its page, the machine charts and their range, the applications and
 * the recent deploys. Runs in both themes; every test fails on a CSP violation or a console
 * error through the `problems` fixture.
 */

import { expect, expectNoA11yViolations, settle, signIn, test } from "./fixtures";

/** The app the seed fails: its newest deploy failed with npm's own words. */
const FAILED = "clientes.arennalabs.com";

test("a failed app is under Needs attention, in its own words, and links to its page", async ({ page, consoleServer }) => {
  await signIn(page, consoleServer);
  await expect(page).toHaveURL(/\/$/);

  const attention = page.getByRole("region", { name: /^Needs attention/ });
  const item = attention.getByRole("listitem").filter({ has: page.getByRole("link", { name: FAILED, exact: true }) });
  await expect(item).toBeVisible();
  await expect(item.getByText("Last deploy failed")).toBeVisible();
  // Verbatim, not paraphrased: the first line of the error the deploy recorded.
  await expect(item.getByText("npm ERR! code ELIFECYCLE", { exact: true })).toBeVisible();
  await expect(item.getByRole("link", { name: `Diagnose ${FAILED}` })).toHaveAttribute("href", `/apps/${FAILED}/diagnose`);
  await expect(item.getByRole("link", { name: `View log of the deploy of ${FAILED}` })).toHaveAttribute(
    "href",
    new RegExp(`^/apps/${FAILED.replace(/\./g, "\\.")}/deployments/\\d+$`),
  );

  await settle(page);
  await expectNoA11yViolations(page, "the overview");

  await item.getByRole("link", { name: FAILED, exact: true }).click();
  await expect(page).toHaveURL(new RegExp(`/apps/${FAILED.replace(/\./g, "\\.")}$`));
  await expect(page.getByRole("heading", { level: 1 })).toHaveText(FAILED);
});

test("the expiring certificate and the failed unit are named too", async ({ page, consoleServer }) => {
  await signIn(page, consoleServer);
  const attention = page.getByRole("region", { name: /^Needs attention/ });
  // The seed's arennalabs.com certificate expires in twelve days.
  const cert = attention.getByRole("listitem").filter({ has: page.getByRole("link", { name: "arennalabs.com", exact: true }) });
  await expect(cert.getByText(/^Certificate expires in \d+ days$/)).toBeVisible();

  // A failed app names its own unit; each failed WASM unit beyond those is named and links to it.
  const services = (await (await page.request.get("/api/services")).json()) as {
    services: { name: string; managed: boolean; active_state?: string | null }[];
  };
  const apps = (await (await page.request.get("/api/apps")).json()) as { apps: { unit?: string | null }[] };
  const appUnits = new Set(apps.apps.map((app) => app.unit));
  const orphans = services.services.filter((unit) => unit.managed && unit.active_state === "failed" && !appUnits.has(unit.name));
  expect(orphans.length, "the seed has a failed unit that belongs to no app").toBeGreaterThan(0);
  for (const unit of orphans) {
    await expect(attention.getByRole("link", { name: unit.name, exact: true })).toHaveAttribute("href", `/services/${unit.name}`);
    const item = attention.getByRole("listitem").filter({ has: page.getByRole("link", { name: unit.name, exact: true }) });
    await expect(item.getByText("The unit has failed")).toBeVisible();
  }
});

test("the machine, the applications and the recent deploys fill in from the API", async ({ page, consoleServer }) => {
  await signIn(page, consoleServer);

  const machine = page.getByRole("region", { name: "Machine" });
  await expect(machine.getByRole("radio", { name: "1h" })).toHaveAttribute("aria-checked", "true");
  // A chart, or the collecting state while the fresh server has fewer than two samples.
  for (const title of ["CPU", "Memory", "Network", "Disk"]) {
    await expect(machine.getByText(title, { exact: true }).first()).toBeVisible();
  }

  const apps = page.getByRole("region", { name: "Applications on this machine" });
  // Every seeded app, and the header row.
  const total = ((await (await page.request.get("/api/apps")).json()) as { total: number }).total;
  await expect(apps.getByRole("row")).toHaveCount(total + 1);
  const failedRow = apps.getByRole("row").filter({ has: page.getByRole("link", { name: FAILED, exact: true }) });
  // The state cell, not the last deploy's "Failed, 2m ago" beside it.
  await expect(failedRow.getByRole("cell", { name: "Failed", exact: true })).toHaveCount(1);

  // The newest deploys, as the API has them now: other tests in this worker deploy too.
  const recent = page.getByRole("region", { name: "Recent deployments, newest first" });
  const history = (await (await page.request.get("/api/deployments?limit=8")).json()) as { items: { domain: string }[] };
  await expect(recent.getByRole("row")).toHaveCount(history.items.length + 1);
  const newest = history.items[0];
  if (newest === undefined) throw new Error("the seed has deployments");
  await expect(recent.getByRole("row").nth(1).getByRole("link", { name: newest.domain, exact: true })).toBeVisible();
});

test("the chart range is part of the URL", async ({ page, consoleServer }) => {
  await signIn(page, consoleServer);
  const range = page.getByRole("radiogroup", { name: "Time range" });
  await range.getByRole("radio", { name: "24h" }).click();
  await expect(page).toHaveURL(/\/\?window=24h$/);
  await expect(page.getByText("Last 24 hours").first()).toBeVisible();

  await page.reload();
  await expect(page.getByRole("radio", { name: "24h" })).toHaveAttribute("aria-checked", "true");
  // Arrow keys move the choice, as in any radio group; a week is its own window.
  await page.getByRole("radio", { name: "24h" }).focus();
  await page.keyboard.press("ArrowRight");
  await expect(page).toHaveURL(/\/\?window=7d$/);
  await expect(page.getByText(/^Last 7 days/).first()).toBeVisible();
});

test("on a phone the page never scrolls sideways; wide tables scroll inside themselves", async ({ page, consoleServer }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await signIn(page, consoleServer);
  const total = ((await (await page.request.get("/api/apps")).json()) as { total: number }).total;
  await expect(page.getByRole("region", { name: "Applications on this machine" }).getByRole("row")).toHaveCount(total + 1);
  await settle(page);
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
  expect(overflow).toBeLessThanOrEqual(0);
  await expectNoA11yViolations(page, "the overview on a phone");
});
