/**
 * The applications list against the real backend: every seeded app, search with `/`, filters
 * that live in the URL, and a row's menu acting through the API. Runs in both themes, with
 * the CSP and console gates of the `problems` fixture.
 */

import type { Page } from "@playwright/test";

import { expect, expectNoA11yViolations, settle, signIn, test } from "./fixtures";

/** How many apps the seeded machine has, as the API counts them. */
async function seeded(page: Page): Promise<number> {
  const response = await page.request.get("/api/apps");
  return ((await response.json()) as { total: number }).total;
}

/** Rows of the table, header excluded. */
function rows(page: Page) {
  return page.getByRole("region", { name: /^Applications/ }).getByRole("row").filter({ hasNot: page.getByRole("columnheader") });
}

test("every seeded app is listed with its state, and the page passes axe", async ({ page, consoleServer }) => {
  await signIn(page, consoleServer, "/apps");
  await expect(page.getByRole("heading", { level: 1 })).toHaveText("Applications");
  const total = await seeded(page);
  await expect(rows(page)).toHaveCount(total);
  await expect(page.getByText(`${String(total)} applications`)).toBeVisible();

  const picconia = rows(page).filter({ has: page.getByRole("link", { name: "picconia.com", exact: true }) });
  await expect(picconia.getByText("Running")).toBeVisible();
  await expect(picconia.getByText("nextjs")).toBeVisible();
  const landing = rows(page).filter({ has: page.getByRole("link", { name: "bodas.arennalabs.com", exact: true }) });
  await expect(landing.getByText("Static", { exact: true })).toBeVisible();

  await settle(page);
  await expectNoA11yViolations(page, "the applications list");
});

test("/ focuses the search, and the search lives in the URL", async ({ page, consoleServer }) => {
  await signIn(page, consoleServer, "/apps");
  const total = await seeded(page);
  await expect(rows(page)).toHaveCount(total);

  await page.getByRole("heading", { level: 1 }).click();
  await page.keyboard.press("/");
  const search = page.getByRole("searchbox", { name: "Search applications" });
  await expect(search).toBeFocused();
  await search.fill("arennalabs");
  await expect(page).toHaveURL(/\/apps\?q=arennalabs$/);
  await expect(rows(page)).toHaveCount(4);
  await expect(page.getByText(`4 of ${String(total)} applications`)).toBeVisible();

  // A shared link opens the same view.
  await page.goto("/apps?q=nothing-matches-this");
  await expect(page.getByText("No application matches")).toBeVisible();
  await page.getByRole("button", { name: "Clear filters" }).last().click();
  await expect(page).toHaveURL(/\/apps$/);
  await expect(rows(page)).toHaveCount(total);
});

test("the state filter narrows the list and Back undoes it", async ({ page, consoleServer }) => {
  await signIn(page, consoleServer, "/apps");
  const total = await seeded(page);
  await expect(rows(page)).toHaveCount(total);

  await page.getByRole("combobox", { name: "State" }).click();
  await page.getByRole("option", { name: "Static" }).click();
  await expect(page).toHaveURL(/\/apps\?state=static$/);
  await expect(rows(page)).toHaveCount(2);
  await expectNoA11yViolations(page, "a filtered list");

  await page.goBack();
  await expect(page).toHaveURL(/\/apps$/);
  await expect(rows(page)).toHaveCount(total);
});

test("a row's menu restarts the app and queues an update through the API", async ({ page, consoleServer }) => {
  await signIn(page, consoleServer, "/apps");
  await expect(rows(page)).toHaveCount(await seeded(page));

  await page.getByRole("button", { name: "Actions for picconia.com" }).click();
  await expectNoA11yViolations(page, "a row's menu");
  const restarted = page.waitForResponse((response) => response.url().endsWith("/api/apps/picconia.com/restart"));
  await page.getByRole("menuitem", { name: "Restart" }).click();
  expect((await restarted).status()).toBe(200);
  await expect(page.getByText("Restarted picconia.com")).toBeVisible();

  await page.getByRole("button", { name: "Actions for picconia.com" }).click();
  const queued = page.waitForRequest((request) => request.url().endsWith("/api/jobs/update") && request.method() === "POST");
  await page.getByRole("menuitem", { name: "Update" }).click();
  expect((await queued).postDataJSON()).toEqual({ domain: "picconia.com" });
  await expect(page.getByText("Update of picconia.com queued")).toBeVisible();
});

test("a static site has nothing to restart", async ({ page, consoleServer }) => {
  await signIn(page, consoleServer, "/apps");
  await page.getByRole("button", { name: "Actions for bodas.arennalabs.com" }).click();
  await expect(page.getByRole("menuitem", { name: "Restart" })).toHaveAttribute("aria-disabled", "true");
  await page.keyboard.press("Escape");
  await expect(page.getByRole("button", { name: "Actions for bodas.arennalabs.com" })).toBeFocused();
});

test("on a phone the table scrolls inside itself, never the page", async ({ page, consoleServer }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await signIn(page, consoleServer, "/apps");
  await expect(rows(page)).toHaveCount(await seeded(page));
  await settle(page);
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
  expect(overflow).toBeLessThanOrEqual(0);
  await expectNoA11yViolations(page, "the list on a phone");
});
