/**
 * Services against the real backend: the list, a unit's own page, its actions, its unit file
 * editor (behind elevation) and deleting it (behind elevation, confirmed by typing the name).
 */

import type { Page } from "@playwright/test";

import { expect, expectNoA11yViolations, settle, signIn, test, totpCode } from "./fixtures";

/** The visible toast queue, scoped so it never collides with the page's own aria-live echo. */
function toasts(page: Page) {
  return page.getByRole("region", { name: "Notifications" });
}

test("lists a seeded WASM-managed service and opens its page", async ({ page, consoleServer }) => {
  await signIn(page, consoleServer, "/services");
  await expect(page.getByRole("heading", { level: 1 })).toHaveText("Services");

  const table = page.getByRole("region", { name: /^Services/ });
  const link = table.getByRole("link", { name: "wasm-picconia-com" });
  await expect(link).toBeVisible();

  await settle(page);
  await expectNoA11yViolations(page, "the services list");

  await link.click();
  await expect(page).toHaveURL(/\/services\/wasm-picconia-com$/);
  await expect(page.getByRole("heading", { level: 1 })).toHaveText("wasm-picconia-com");
  await expect(page.getByText("WASM-managed")).toBeVisible();
  await expect(page.getByRole("region", { name: /Logs for/ })).toBeVisible();
  await expectNoA11yViolations(page, "a service's own page");
});

test("restarts a unit from its own page", async ({ page, consoleServer }) => {
  await signIn(page, consoleServer, "/services/wasm-picconia-com");
  const restarted = page.waitForResponse((response) => response.url().endsWith("/api/services/wasm-picconia-com/restart"));
  await page.getByRole("button", { name: "Restart" }).click();
  expect((await restarted).status()).toBe(200);
  await expect(toasts(page).getByText("Restarted wasm-picconia-com")).toBeVisible();
});

test("creates a service in simple mode and finds it in the list", async ({ page, consoleServer }) => {
  await signIn(page, consoleServer, "/services");
  await page.getByRole("button", { name: "New service" }).click();
  const dialog = page.getByRole("dialog", { name: "New service" });
  await expectNoA11yViolations(page, "the new service dialog");

  await dialog.getByLabel("Name", { exact: true }).fill("e2e-worker");
  await dialog.getByLabel("Command", { exact: true }).fill("/usr/bin/node worker.js");
  const created = page.waitForResponse((response) => response.url().endsWith("/api/services") && response.request().method() === "POST");
  await dialog.getByRole("button", { name: "Create service" }).click();
  expect((await created).status()).toBe(200);
  await expect(toasts(page).getByText("Created e2e-worker")).toBeVisible();

  await page.getByRole("searchbox", { name: "Search services" }).fill("e2e-worker");
  await expect(page.getByRole("link", { name: "e2e-worker" })).toBeVisible();
});

test("saving the unit file asks to confirm it's you, then shows the saved result", async ({ page, consoleServer, problems }) => {
  problems.expect(/status of 403 .*\/api\/services\/wasm-picconia-com\/config$/);
  await signIn(page, consoleServer, "/services/wasm-picconia-com");
  const textarea = page.getByLabel("Unit file for wasm-picconia-com", { exact: true });
  await expect(textarea).toBeVisible();
  const original = await textarea.inputValue();
  await textarea.fill(`${original}\n# edited by e2e\n`);
  await page.getByRole("button", { name: "Save unit file" }).click();

  const confirm = page.getByRole("dialog", { name: "Confirm it's you" });
  await expect(confirm).toBeVisible();
  await expectNoA11yViolations(page, "the elevation dialog");
  await page.getByLabel("Authentication code").fill(totpCode(consoleServer.totpSecret ?? ""));
  await page.getByRole("button", { name: "Confirm" }).click();
  await expect(confirm).toBeHidden();

  await expect(toasts(page).getByText("Saved the unit file for wasm-picconia-com")).toBeVisible();
  await expect(textarea).toHaveValue(/# edited by e2e/);

  // The elevation covers the next ten minutes; reloading the unit file's own read (no
  // elevation needed for GET) confirms the write actually landed, not just the toast.
  await page.reload();
  await expect(page.getByLabel("Unit file for wasm-picconia-com", { exact: true })).toHaveValue(/# edited by e2e/);
});

test("deleting a service is confirmed by typing its name and needs a fresh confirmation", async ({ page, consoleServer, problems }) => {
  problems.expect(/status of 403 .*\/api\/services\/e2e-deleteme$/);
  await signIn(page, consoleServer, "/services");
  await page.getByRole("button", { name: "New service" }).click();
  const createDialog = page.getByRole("dialog", { name: "New service" });
  await createDialog.getByLabel("Name", { exact: true }).fill("e2e-deleteme");
  await createDialog.getByLabel("Command", { exact: true }).fill("/usr/bin/node worker.js");
  await createDialog.getByRole("button", { name: "Create service" }).click();
  await expect(toasts(page).getByText("Created e2e-deleteme")).toBeVisible();

  await page.goto("/services/e2e-deleteme");
  await page.getByRole("button", { name: "Delete service" }).click();
  const dialog = page.getByRole("alertdialog", { name: "Delete e2e-deleteme" });
  await expect(dialog).toBeVisible();
  const confirmButton = dialog.getByRole("button", { name: "Delete service" });
  await expect(confirmButton).toBeDisabled();
  await dialog.locator("input").fill("e2e-deleteme");
  await expect(confirmButton).toBeEnabled();
  await confirmButton.click();

  const elevate = page.getByRole("dialog", { name: "Confirm it's you" });
  await expect(elevate).toBeVisible();
  await page.getByLabel("Authentication code").fill(totpCode(consoleServer.totpSecret ?? ""));
  await page.getByRole("button", { name: "Confirm" }).click();

  await expect(page).toHaveURL(/\/services$/);
  await expect(toasts(page).getByText("Deleted e2e-deleteme")).toBeVisible();
  await expect(page.getByRole("link", { name: "e2e-deleteme" })).toHaveCount(0);
});
