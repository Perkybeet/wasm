/**
 * The Databases pages against the real backend and its seeded machine: creating a database,
 * creating a user (whose password is shown exactly once), and the SQL console's read and
 * write modes against the fake SQL clients' scripted output (scripts/console_server.py).
 * Runs in both themes (playwright.config.ts's two projects), with the CSP and console gates
 * of the `problems` fixture; each test that changes the page checks axe once.
 */

import type { Page } from "@playwright/test";

import { expect, expectNoA11yViolations, settle, signIn, test, totpCode } from "./fixtures";

/** The visible toast queue, scoped so it never collides with the page's own aria-live echo. */
function toasts(page: Page) {
  return page.getByRole("region", { name: "Notifications" });
}

test("engines, databases and users are listed, and the page passes axe", async ({ page, consoleServer }) => {
  await signIn(page, consoleServer, "/databases");
  await expect(page.getByRole("heading", { level: 1 })).toHaveText("Databases");

  const engines = page.getByRole("region", { name: "Engines" });
  await expect(engines.getByText("PostgreSQL")).toBeVisible();
  await expect(engines.getByText("MySQL/MariaDB")).toBeVisible();
  // Redis is installed but stopped in the seed; MongoDB is not installed at all.
  await expect(engines.getByRole("button", { name: "Start", exact: true })).toBeVisible();
  await expect(engines.getByRole("button", { name: "Install" })).toBeVisible();

  const databases = page.getByRole("region", { name: "Databases" });
  await expect(databases.getByRole("link", { name: "arennalabs_production" })).toBeVisible();
  await expect(databases.getByRole("link", { name: "picconia_wp" })).toBeVisible();

  await settle(page);
  await expectNoA11yViolations(page, "the databases page");
});

test("creating a database sends the right request and confirms it", async ({ page, consoleServer }) => {
  await signIn(page, consoleServer, "/databases");

  const section = page.getByRole("region", { name: "Databases" });
  await section.getByRole("button", { name: "New database" }).click();
  const dialog = page.getByRole("dialog", { name: "Create database" });
  await expectNoA11yViolations(page, "the create database dialog");

  await dialog.getByRole("combobox", { name: "Engine" }).click();
  await page.getByRole("option", { name: "PostgreSQL" }).click();
  await dialog.getByLabel("Name").fill("acme_shop");

  const created = page.waitForRequest(
    (request) => request.url().endsWith("/api/databases/databases") && request.method() === "POST",
  );
  await dialog.getByRole("button", { name: "Create database" }).click();
  expect((await created).postDataJSON()).toEqual({ engine: "postgresql", name: "acme_shop", owner: null, encoding: null });
  await expect(toasts(page).getByText("Database 'acme_shop' created")).toBeVisible();
  await expect(dialog).not.toBeVisible();
});

test("creating a user shows its password exactly once", async ({ page, consoleServer }) => {
  await signIn(page, consoleServer, "/databases");

  const section = page.getByRole("region", { name: "Users" });
  await section.getByRole("button", { name: "New user" }).click();
  const dialog = page.getByRole("dialog", { name: "Create user" });
  await dialog.getByLabel("Username").fill("shop_app");
  await expectNoA11yViolations(page, "the create user dialog");

  await dialog.getByRole("button", { name: "Create user" }).click();

  const done = page.getByRole("dialog", { name: "shop_app created" });
  await expect(done).toBeVisible();
  await expect(done.getByText(/shown once/i)).toBeVisible();
  await expect(done.getByRole("textbox", { name: "Username" })).toHaveValue("shop_app");
  const password = done.getByRole("textbox", { name: "Password" });
  await expect(password).not.toHaveValue("");
  await settle(page);
  await expectNoA11yViolations(page, "the created-user panel");

  await done.getByRole("button", { name: "Done" }).click();
  await expect(done).not.toBeVisible();
});

test("a read query against the SQL console renders as a grid", async ({ page, consoleServer }) => {
  await signIn(page, consoleServer, "/databases/postgresql/arennalabs_production");
  await expect(page.getByRole("heading", { level: 1 })).toHaveText("arennalabs_production");

  const console_ = page.getByRole("region", { name: "SQL console" });
  await expect(console_.getByRole("radio", { name: "Read" })).toHaveAttribute("aria-checked", "true");
  await console_.getByRole("textbox").fill("SELECT id, email, total FROM demo_orders");

  const ran = page.waitForResponse((response) => response.url().endsWith("/api/databases/query"));
  await console_.getByRole("button", { name: "Run" }).click();
  const response = await ran;
  expect(response.status()).toBe(200);
  const body = (await response.json()) as { mode: string };
  expect(body.mode).toBe("read");

  await expect(console_.getByText(/^3 rows/)).toBeVisible();
  await expect(console_.getByRole("cell", { name: "maria@example.com" })).toBeVisible();
  await expect(console_.getByRole("cell", { name: "129.90" })).toBeVisible();

  await settle(page);
  await expectNoA11yViolations(page, "the SQL console with results");
});

test("write mode asks the operator to confirm it's them before it runs", async ({ page, consoleServer, problems }) => {
  problems.expect(/status of 403 .*\/api\/databases\/query$/);
  await signIn(page, consoleServer, "/databases/postgresql/arennalabs_production");

  const console_ = page.getByRole("region", { name: "SQL console" });
  await console_.getByRole("radio", { name: "Write" }).click();
  await console_.getByRole("textbox").fill("UPDATE demo_orders SET total = 1 WHERE id = 1024");

  const attempt = page.waitForResponse((response) => response.url().endsWith("/api/databases/query"));
  await console_.getByRole("button", { name: "Run" }).click();
  expect((await attempt).status()).toBe(403);

  const elevate = page.getByRole("dialog", { name: "Confirm it's you" });
  await expect(elevate).toBeVisible();
  await expectNoA11yViolations(page, "the elevation dialog");

  await elevate.getByLabel("Authentication code").fill(totpCode(consoleServer.totpSecret ?? ""));
  const retried = page.waitForResponse((response) => response.url().endsWith("/api/databases/query"));
  await elevate.getByRole("button", { name: "Confirm" }).click();
  expect((await retried).status()).toBe(200);
  await expect(elevate).not.toBeVisible();
  await expect(console_.getByText("The statement returned no rows.")).toBeVisible();
});
