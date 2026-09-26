/**
 * Services against the real backend: the list, a unit's own page, its actions, its unit file
 * editor (behind elevation) and deleting it (behind elevation, confirmed by typing the name).
 */

import { expect, expectNoA11yViolations, settle, signIn, test, toasts, totpCode } from "./fixtures";

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
  // Its facts say who manages it (a foreign unit's page says WASM did not create it).
  await expect(page.getByText("Managed by", { exact: true })).toBeVisible();
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

test("checks the unit with systemd-analyze before saving, and blocks a save it rejects", async ({ page, consoleServer }) => {
  await signIn(page, consoleServer, "/services/wasm-picconia-com");
  const textarea = page.getByLabel("Unit file for wasm-picconia-com", { exact: true });
  const original = await textarea.inputValue();
  // The fake systemd-analyze refuses a unit with no ExecStart=; nothing else about the file
  // needs to be realistic for the check to fail.
  const withoutExecStart = original
    .split("\n")
    .filter((line) => !line.startsWith("ExecStart="))
    .join("\n");
  await textarea.fill(withoutExecStart);

  const verified = page.waitForResponse((response) => response.url().endsWith("/api/services/verify"));
  await page.getByRole("button", { name: "Save unit file" }).click();
  expect((await verified).status()).toBe(200);

  await expect(page.getByText(/systemd-analyze rejected the unit/)).toBeVisible();
  await expect(page.getByText(/Service has no ExecStart= setting\. Refusing\./)).toBeVisible();
  await expect(page.getByRole("dialog", { name: "Confirm it's you" })).toHaveCount(0);
  await expectNoA11yViolations(page, "the unit editor after a failed verify");

  // Nothing was written: reloading shows the unit exactly as it was before the attempt.
  await page.reload();
  await expect(page.getByLabel("Unit file for wasm-picconia-com", { exact: true })).toHaveValue(original);
});

test("shows every unit, including one WASM did not create, read-only, behind the show-all-units toggle", async ({ page, consoleServer, problems }) => {
  // The per-name read only ever answers what the store tracks: expected, once, while the
  // foreign unit's own page falls back to the all-units listing to tell it apart from a name
  // that does not exist at all (see ServiceDetailPage).
  problems.expect(/status of 404 .*\/api\/services\/postgresql$/);
  await signIn(page, consoleServer, "/services");
  await expect(page.getByRole("link", { name: "postgresql" })).toHaveCount(0);

  await page.getByRole("switch", { name: "Show all units" }).click();
  await expect(page).toHaveURL(/[?&]all=true/);

  const row = page.getByRole("row").filter({ has: page.getByText("postgresql", { exact: true }) });
  await expect(row).toBeVisible();
  // The Managed column on a desktop (a phone says it beside the name instead).
  await expect(row.getByRole("cell", { name: "Foreign", exact: true })).toBeVisible();
  await expect(row.getByRole("button", { name: /Actions for/ })).toHaveCount(0);

  await settle(page);
  await expectNoA11yViolations(page, "the services list with every unit shown");

  await row.getByRole("link", { name: "postgresql" }).click();
  await expect(page).toHaveURL(/\/services\/postgresql$/);
  await expect(page.getByText("WASM did not create this unit")).toBeVisible();
  await expect(page.getByRole("button", { name: /Delete/ })).toHaveCount(0);
  await expect(page.getByLabel(/Unit file for/)).toHaveCount(0);
  await expectNoA11yViolations(page, "a foreign unit's own page");
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
