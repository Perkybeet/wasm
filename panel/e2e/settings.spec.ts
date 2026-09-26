/**
 * Settings > General against the real backend: every typed section loads, a value the server
 * refuses comes back beside its field in the server's own words, and a fixed value saves.
 * Every settings page passes axe and the CSP and console gates, in both themes.
 */

import { expect, expectNoA11yViolations, settle, signIn, test, totpCode } from "./fixtures";
import { expectAccessibleToast, holdToast, stillness } from "./settings.helpers";

const PAGES = [
  { path: "/settings", heading: "Applications directory", title: /^General settings/ },
  { path: "/settings/security", heading: "Two-factor authentication", title: /^Security settings/ },
  { path: "/settings/notifications", heading: "Where alerts go", title: /^Notifications settings/ },
  { path: "/settings/tokens", heading: "Tokens for automation", title: /^API tokens/ },
  { path: "/settings/about", heading: "Version and updates", title: /^About/ },
] as const;

test("every settings page loads its sections and passes axe", async ({ page, consoleServer }) => {
  await signIn(page, consoleServer, "/settings");
  const tabs = page.getByRole("navigation", { name: "Settings sections" });
  for (const entry of PAGES) {
    await page.goto(entry.path);
    await expect(page.getByRole("heading", { level: 2, name: entry.heading })).toBeVisible();
    await expect(page).toHaveTitle(entry.title);
    await expect(tabs.locator('a[aria-current="page"]')).toHaveCount(1);
    // Nothing still loading when axe looks.
    await expect(page.locator("[aria-busy=true]")).toHaveCount(0);
    await settle(page);
    await expectNoA11yViolations(page, entry.path);
  }
});

test("a refused value is shown beside its field, verbatim; the fixed value saves", async ({ page, consoleServer, problems }) => {
  // Saving configuration is sudo mode (D5): the first write of the session asks to confirm it's you.
  problems.expect(/status of 403 .* \/api\/config\/backup$/);
  problems.expect(/status of 422 .* \/api\/config\/backup$/);
  problems.expect(/status of 400 .* \/api\/config\/apps-directory$/);
  await signIn(page, consoleServer, "/settings");
  const backups = page.getByRole("region", { name: "Backups" });
  const retention = backups.getByLabel("Backups kept per application");
  await expect(retention).toHaveValue(/^\d+$/);
  const original = await retention.inputValue();
  const save = backups.getByRole("button", { name: "Save changes" });
  await expect(save).toBeDisabled();

  await retention.fill("500");
  await expect(backups.getByText("Unsaved changes")).toBeVisible();
  await expect(backups.getByText("wasm config set backup.max_per_app 500")).toBeVisible();
  await save.click();

  const elevate = page.getByRole("dialog", { name: "Confirm it's you" });
  await expect(elevate).toBeVisible();
  await expectNoA11yViolations(page, "the elevation dialog");
  await elevate.getByLabel("Authentication code").fill(totpCode(consoleServer.totpSecret ?? ""));
  const refused = page.waitForResponse((response) => response.url().endsWith("/api/config/backup") && response.request().method() === "PUT");
  await elevate.getByRole("button", { name: "Confirm" }).click();
  expect((await refused).status()).toBe(422);
  await expect(elevate).toBeHidden();

  // pydantic's words, whichever major version the server runs.
  const message = backups.getByText(/less than or equal to 100/);
  await expect(message).toBeVisible();
  await expect(retention).toHaveAttribute("aria-invalid", "true");
  await expect(retention).toHaveAccessibleDescription(/less than or equal to 100/);
  await stillness(page);
  await expectNoA11yViolations(page, "a section with a refused value");

  // A path the configuration's own rule refuses (a 400 with no field): beside the one field
  // of its section, with the server's fix.
  const directory = page.getByRole("region", { name: "Applications directory" });
  const input = directory.getByLabel("Directory");
  const originalDirectory = await input.inputValue();
  await input.fill("relative/apps");
  await directory.getByRole("button", { name: "Save changes" }).click();
  await expect(directory.getByText(/apps_directory must be an absolute path Got 'relative\/apps'/)).toBeVisible();
  await expect(input).toHaveAttribute("aria-invalid", "true");
  await directory.getByRole("button", { name: "Discard" }).click();
  await expect(input).toHaveValue(originalDirectory);

  await retention.fill("12");
  await expect(message).toBeHidden();
  await save.click();
  await holdToast(page, "Saved the backup settings");
  await expect(save).toBeDisabled();
  await stillness(page);
  await expectNoA11yViolations(page, "a page with a success toast");
  await expectAccessibleToast(page, "Saved the backup settings", "polite");

  // The server holds it: a fresh load shows the saved value.
  await page.reload();
  await expect(page.getByRole("region", { name: "Backups" }).getByLabel("Backups kept per application")).toHaveValue("12");

  // Leave the worker's server as it was.
  await page.getByRole("region", { name: "Backups" }).getByLabel("Backups kept per application").fill(original);
  await page.getByRole("region", { name: "Backups" }).getByRole("button", { name: "Save changes" }).click();
  await expect(page.getByRole("region", { name: "Backups" }).getByRole("button", { name: "Save changes" })).toBeDisabled();
});
