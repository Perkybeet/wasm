/**
 * Cron against the real backend: the seeded jobs, creating one and seeing its next run,
 * running one now, enabling and disabling it, and its run history.
 */

import type { Page } from "@playwright/test";

import { expect, expectNoA11yViolations, settle, signIn, test } from "./fixtures";

/** The visible toast queue, scoped so it never collides with the page's own aria-live echo. */
function toasts(page: Page) {
  return page.getByRole("region", { name: "Notifications" });
}

test("lists the seeded jobs with their schedule and next run, and passes axe", async ({ page, consoleServer }) => {
  await signIn(page, consoleServer, "/cron");
  await expect(page.getByRole("heading", { level: 1 })).toHaveText("Cron");

  const table = page.getByRole("region", { name: /^Cron jobs/ });
  await expect(table.getByText("sitemap", { exact: true })).toBeVisible();
  await expect(table.getByText("cleanup-tmp", { exact: true })).toBeVisible();

  await settle(page);
  await expectNoA11yViolations(page, "the cron jobs list");
});

test("creates a job with a daily preset and sees it listed with a next run", async ({ page, consoleServer }) => {
  await signIn(page, consoleServer, "/cron");
  await page.getByRole("button", { name: "New job" }).click();
  const dialog = page.getByRole("dialog", { name: "New cron job" });
  await expectNoA11yViolations(page, "the new job dialog");

  await dialog.getByLabel("Name", { exact: true }).fill("e2e-report");
  await dialog.getByLabel("Command", { exact: true }).fill("/usr/bin/wasm backup create example.com");

  const created = page.waitForResponse((response) => response.url().endsWith("/api/cron") && response.request().method() === "POST");
  await dialog.getByRole("button", { name: "Create job" }).click();
  const response = await created;
  expect(response.status()).toBe(201);
  const body = (await response.json()) as { job: { next_run: string } | null };
  expect(body.job?.next_run).toBeTruthy();

  await expect(toasts(page).getByText("Created e2e-report")).toBeVisible();
  const row = page.getByRole("row").filter({ has: page.getByText("e2e-report", { exact: true }) });
  await expect(row).toBeVisible();
  await expect(row.getByText("Enabled")).toBeVisible();
});

test("runs a job now, and its history opens with the run", async ({ page, consoleServer }) => {
  await signIn(page, consoleServer, "/cron");
  await page.getByRole("button", { name: "Actions for sitemap" }).click();
  const started = page.waitForResponse((response) => response.url().endsWith("/api/cron/sitemap/run"));
  await page.getByRole("menuitem", { name: "Run now" }).click();
  expect((await started).status()).toBe(200);
  await expect(toasts(page).getByText("Started sitemap")).toBeVisible();

  await page.getByRole("button", { name: "Actions for sitemap" }).click();
  await page.getByRole("menuitem", { name: "View runs" }).click();
  const drawer = page.getByRole("dialog", { name: "Runs of sitemap" });
  await expect(drawer).toBeVisible();
  await expectNoA11yViolations(page, "the runs drawer");
});

test("disables and re-enables a job's timer", async ({ page, consoleServer }) => {
  await signIn(page, consoleServer, "/cron");
  await page.getByRole("button", { name: "Actions for cleanup-tmp" }).click();
  const disabled = page.waitForResponse((response) => response.url().endsWith("/api/cron/cleanup-tmp/disable"));
  await page.getByRole("menuitem", { name: "Disable" }).click();
  expect((await disabled).status()).toBe(200);
  await expect(toasts(page).getByText("Disabled cleanup-tmp")).toBeVisible();

  // The State column's pill, not the Next Run column: a disabled job's next run is "-" with
  // an sr-only reason of "Disabled" too, and getByText alone would match both.
  const row = page.getByRole("row").filter({ has: page.getByText("cleanup-tmp", { exact: true }) });
  const state = row.getByRole("cell").first();
  await expect(state.getByText("Disabled")).toBeVisible();

  await page.getByRole("button", { name: "Actions for cleanup-tmp" }).click();
  const enabled = page.waitForResponse((response) => response.url().endsWith("/api/cron/cleanup-tmp/enable"));
  await page.getByRole("menuitem", { name: "Enable" }).click();
  expect((await enabled).status()).toBe(200);
  await expect(state.getByText("Enabled")).toBeVisible();
});
