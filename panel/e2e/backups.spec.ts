/**
 * The Backups page against the real backend and its seeded machine: the full option set on
 * create, and the typed-domain confirmation before a restore. Runs in both themes
 * (playwright.config.ts's two projects), with the CSP and console gates of the `problems`
 * fixture.
 */

import { expect, expectNoA11yViolations, settle, signIn, test, toasts, totpCode } from "./fixtures";

function rows(page: import("@playwright/test").Page) {
  return page.getByRole("region", { name: "Backups" }).getByRole("row").filter({ hasNot: page.getByRole("columnheader") });
}

/** The visible toast queue, scoped so it never collides with the page's own aria-live echo. */
test("every seeded backup is listed, and the page passes axe", async ({ page, consoleServer }) => {
  await signIn(page, consoleServer, "/backups");
  await expect(page.getByRole("heading", { level: 1 })).toHaveText("Backups");
  // As many as the API lists now: other tests in this worker create backups too.
  const listed = (await (await page.request.get("/api/backups")).json()) as { backups: unknown[] };
  expect(listed.backups.length, "the seed has backups").toBeGreaterThan(0);
  await expect(rows(page)).toHaveCount(listed.backups.length);
  await expect(page.getByRole("link", { name: "picconia.com" }).first()).toBeVisible();

  await settle(page);
  await expectNoA11yViolations(page, "the backups page");
});

test("creating a backup with databases included sends the right request", async ({ page, consoleServer }) => {
  await signIn(page, consoleServer, "/backups");

  await page.getByRole("button", { name: "New backup" }).click();
  const dialog = page.getByRole("dialog", { name: "Create backup" });
  await expectNoA11yViolations(page, "the create backup dialog");

  await dialog.getByRole("combobox", { name: "Application" }).click();
  await page.getByRole("option", { name: "picconia.com" }).click();
  await dialog.getByLabel("Description").fill("Before the migration");
  await dialog.getByRole("checkbox", { name: "Databases" }).check();
  await dialog.getByLabel("Tags").fill("manual, pre-migration");

  const queued = page.waitForRequest((request) => request.url().endsWith("/api/backups") && request.method() === "POST");
  await dialog.getByRole("button", { name: "Create backup" }).click();
  expect((await queued).postDataJSON()).toEqual({
    domain: "picconia.com",
    description: "Before the migration",
    include_env: true,
    include_node_modules: false,
    include_build: false,
    include_database: true,
    include_docker_volumes: false,
    schemas: [],
    redis_method: "rdb",
    tags: ["manual", "pre-migration"],
  });
  await expect(toasts(page).getByText("Backup queued for picconia.com", { exact: true })).toBeVisible();
  await expect(dialog).not.toBeVisible();
});

test("restoring a backup is confirmed by typing the target domain", async ({ page, consoleServer, problems }) => {
  // Restoring is sudo mode (D5): the first attempt asks the operator to confirm it's them.
  problems.expect(/status of 403 .*\/api\/backups\/.*\/restore$/);
  await signIn(page, consoleServer, "/backups");

  const row = rows(page)
    .filter({ has: page.getByRole("link", { name: "picconia.com" }) })
    .first();
  await row.getByRole("button", { name: /^Actions for/ }).click();
  await page.getByRole("menuitem", { name: "Restore" }).click();

  const dialog = page.getByRole("alertdialog", { name: /^Restore / });
  await expect(dialog).toBeVisible();
  const confirmButton = dialog.getByRole("button", { name: "Restore" });
  await expect(confirmButton).toBeDisabled();

  const domainField = dialog.getByRole("textbox").nth(0);
  await expect(domainField).toHaveValue("picconia.com");
  const confirmField = dialog.getByRole("textbox").nth(1);
  await confirmField.fill("not-the-domain");
  await expect(confirmButton).toBeDisabled();
  await confirmField.fill("picconia.com");
  await expect(confirmButton).toBeEnabled();
  await expectNoA11yViolations(page, "the restore confirmation");

  await confirmButton.click();

  const elevate = page.getByRole("dialog", { name: "Confirm it's you" });
  await expect(elevate).toBeVisible();
  await expectNoA11yViolations(page, "the elevation dialog");
  await elevate.getByLabel("Authentication code").fill(totpCode(consoleServer.totpSecret ?? ""));
  const requested = page.waitForRequest(
    (request) => request.url().includes("/api/backups/") && request.url().endsWith("/restore") && request.method() === "POST",
  );
  await elevate.getByRole("button", { name: "Confirm" }).click();
  const body = (await requested).postDataJSON() as { target_domain: string | null; restore_env: boolean; verify: boolean };
  expect(body).toEqual({ target_domain: null, restore_env: true, verify: true });
  await expect(elevate).toBeHidden();
  await expect(toasts(page).getByText("Restore queued for picconia.com", { exact: true })).toBeVisible();
});

test("restoring into a different domain is confirmed by typing that domain", async ({ page, consoleServer }) => {
  await signIn(page, consoleServer, "/backups");

  const row = rows(page)
    .filter({ has: page.getByRole("link", { name: "picconia.com" }) })
    .first();
  await row.getByRole("button", { name: /^Actions for/ }).click();
  await page.getByRole("menuitem", { name: "Restore" }).click();

  const dialog = page.getByRole("alertdialog", { name: /^Restore / });
  const domainField = dialog.getByRole("textbox").nth(0);
  await domainField.fill("picconia-staging.example.com");
  const confirmField = dialog.getByRole("textbox").nth(1);
  await confirmField.fill("picconia.com");
  await expect(dialog.getByRole("button", { name: "Restore" })).toBeDisabled();
  await confirmField.fill("picconia-staging.example.com");
  await expect(dialog.getByRole("button", { name: "Restore" })).toBeEnabled();
});

test("verifying a backup updates its row with the result", async ({ page, consoleServer }) => {
  await signIn(page, consoleServer, "/backups");

  const row = rows(page)
    .filter({ has: page.getByRole("link", { name: "picconia.com" }) })
    .first();
  await expect(row.getByText("Never verified")).toBeVisible();

  await row.getByRole("button", { name: /^Actions for/ }).click();
  const verified = page.waitForResponse(
    (response) => response.url().includes("/api/backups/") && response.url().endsWith("/verify") && response.request().method() === "POST",
  );
  await page.getByRole("menuitem", { name: "Verify" }).click();
  expect((await verified).status()).toBe(200);

  await expect(row.getByText("Verified", { exact: true })).toBeVisible();
  // The "Created" column has its own relative time too; the verified one is the last <time>.
  await expect(row.locator("time").last()).toHaveText(/ago$|just now/);
  await settle(page);
  await expectNoA11yViolations(page, "a backup row after verifying");

  // The server, not the session, remembers it: a reload shows the same verdict.
  await page.reload();
  await expect(
    rows(page)
      .filter({ has: page.getByRole("link", { name: "picconia.com" }) })
      .first()
      .getByText("Verified", { exact: true }),
  ).toBeVisible();
});

test("the storage bar and schedules read from the API", async ({ page, consoleServer }) => {
  await signIn(page, consoleServer, "/backups");
  await expect(page.getByText(/^\d+(\.\d+)? (B|KB|MB|GB)$/).first()).toBeVisible();

  const schedules = page.getByRole("region", { name: "Schedules" });
  await expect(schedules).toBeVisible();
  await settle(page);
  await expectNoA11yViolations(page, "the schedules section");
});
