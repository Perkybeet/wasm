/**
 * Activity against the real backend: the jobs timeline (there is no audit log endpoint yet,
 * so this is jobs history only - the page says so), filtering by result, and opening a job's
 * captured log.
 */

import { expect, expectNoA11yViolations, settle, signIn, test } from "./fixtures";

test("lists the seeded job history and says it is not a full audit log", async ({ page, consoleServer }) => {
  await signIn(page, consoleServer, "/activity");
  await expect(page.getByRole("heading", { level: 1 })).toHaveText("Activity");
  await expect(page.getByText(/not a full audit log/)).toBeVisible();

  const table = page.getByRole("region", { name: /^Activity/ });
  await expect(table.getByRole("row").filter({ hasNot: page.getByRole("columnheader") }).first()).toBeVisible();

  await settle(page);
  await expectNoA11yViolations(page, "the activity timeline");
});

test("filtering by result narrows the timeline", async ({ page, consoleServer }) => {
  await signIn(page, consoleServer, "/activity");
  const table = page.getByRole("region", { name: /^Activity/ });
  const rows = () => table.getByRole("row").filter({ hasNot: page.getByRole("columnheader") });
  // count() does not wait like toBeVisible() does; without this the jobs list is still loading
  // (or the skeleton rows have not yet been replaced) and the count below reads as 0.
  await expect(rows().first()).toBeVisible();
  const before = await rows().count();
  expect(before).toBeGreaterThan(0);

  await page.getByRole("combobox", { name: "Result" }).click();
  await page.getByRole("option", { name: "failed", exact: true }).click();
  await expect(page).toHaveURL(/\/activity\?status=failed$/);
  await expect(rows().first()).toBeVisible();
  const count = await rows().count();
  expect(count).toBeLessThanOrEqual(before);
  for (let i = 0; i < count; i += 1) {
    await expect(rows().nth(i).getByText("Failed", { exact: true })).toBeVisible();
  }

  await page.getByRole("button", { name: "Clear filters" }).click();
  await expect(page).toHaveURL(/\/activity$/);
});

test("opening a job with a captured log shows it verbatim", async ({ page, consoleServer }) => {
  await signIn(page, consoleServer, "/activity");
  const table = page.getByRole("region", { name: /^Activity/ });
  const description = "Seeded update of arennalabs.com with a captured log";
  const row = table.getByRole("row").filter({ has: page.getByText(description) });
  await expect(row).toBeVisible();
  await row.getByText(description).click();

  const drawer = page.getByRole("dialog", { name: "Update arennalabs.com" });
  await expect(drawer).toBeVisible();
  await expect(drawer.getByText(/Updating arennalabs\.com/)).toBeVisible();
  await expect(drawer.getByText(/Update of arennalabs\.com finished/)).toBeVisible();
  await expectNoA11yViolations(page, "a job's log");
});
