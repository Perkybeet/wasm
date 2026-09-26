/**
 * Settings > Security and API tokens against the real backend.
 *
 * - A read token is created behind "Confirm it's you", shown once, authenticates a real API
 *   call, and stops working the moment it is revoked.
 * - Two-factor authentication is enrolled end to end on a server that starts without it: the
 *   key shown for manual entry produces the code that turns it on, a fresh sign-in then needs
 *   that authenticator, and turning it off again takes a code and a confirmation.
 * - A failure toast is announced once and keeps its close button reachable (the Base UI
 *   aria-hidden-focus defect), checked in the browser.
 */

import { request as playwrightRequest } from "@playwright/test";
import type { APIRequestContext, Page } from "@playwright/test";

import { expect, expectNoA11yViolations, settle, signIn, startConsoleServer, test, totpCode } from "./fixtures";
import type { ConsoleServer } from "./fixtures";
import { confirmItsYou, expectAccessibleToast, stillness, toastSaying } from "./settings.helpers";

/** The CSRF header a mutation from a request context must carry, from its cookie. */
async function csrfHeaders(context: APIRequestContext): Promise<Record<string, string>> {
  const { cookies } = await context.storageState();
  const csrf = cookies.find((cookie) => cookie.name === "wasm_csrf");
  return csrf ? { "X-WASM-CSRF": csrf.value } : {};
}

/** A second, API-only session on the server, as another browser would hold. */
async function otherSession(server: ConsoleServer): Promise<APIRequestContext> {
  const context = await playwrightRequest.newContext({ baseURL: server.url });
  const response = await context.post("/api/auth/login", {
    data: { token: server.token, ...(server.totpSecret !== null ? { totp_code: server.secondFactor() } : {}) },
  });
  expect(response.ok()).toBe(true);
  return context;
}

test("create a read token, see it once, use it, revoke it", async ({ page, consoleServer, problems, playwright }) => {
  // Creating a token asks "Confirm it's you" by answering 403 first, by design.
  problems.expect(/status of 403 .* \/api\/auth\/tokens$/);
  await signIn(page, consoleServer, "/settings/tokens");
  const name = `e2e-read-${String(Date.now())}`;

  await page.getByRole("button", { name: "Create token" }).first().click();
  const dialog = page.getByRole("dialog", { name: "Create an API token" });
  await dialog.getByLabel(/^Name/).fill(name);
  await expect(dialog.getByRole("radio", { name: "Read" })).toBeChecked();
  await expect(dialog.getByRole("radio", { name: "Admin" })).toHaveAccessibleDescription(/managing tokens/);
  await stillness(page);
  await expectNoA11yViolations(page, "the create token dialog");
  await dialog.getByRole("button", { name: "Create token" }).click();
  await confirmItsYou(page, consoleServer);

  const once = page.getByRole("dialog", { name: "Copy your new token" });
  await expect(once).toBeVisible();
  const token = (await once.getByTestId("new-token").textContent()) ?? "";
  expect(token).toMatch(/^wasm_tok_\S+$/);
  await expect(once.getByRole("alert")).toContainText("This is the only time the token is shown");
  await stillness(page);
  await expectNoA11yViolations(page, "the new token, shown once");
  await once.getByRole("button", { name: "Done" }).click();
  await expect(once).toBeHidden();
  await expect(toastSaying(page, `Created token ${name}`)).toBeVisible();
  // Once: nowhere on the page any more, and not after a reload either.
  await expect(page.getByText(token)).toHaveCount(0);

  const row = page.getByRole("row").filter({ hasText: name });
  await expect(row).toContainText("read");
  await expect(row).toContainText("Active");

  // The token is real: it reads, as a read token may.
  const api = await playwright.request.newContext({ baseURL: consoleServer.url });
  const authorized = { Authorization: `Bearer ${token}` };
  expect((await api.get("/api/apps", { headers: authorized })).status()).toBe(200);

  await row.getByRole("button", { name: `Revoke ${name}` }).click();
  const confirm = page.getByRole("alertdialog", { name: `Revoke ${name}` });
  await confirm.getByRole("textbox").fill(name);
  await confirm.getByRole("button", { name: "Revoke token" }).click();
  await expect(toastSaying(page, `Revoked token ${name}`)).toBeVisible();
  await expect(row).toContainText("Revoked");
  await expect(row.getByRole("button", { name: `Revoke ${name}` })).toHaveCount(0);

  expect((await api.get("/api/apps", { headers: authorized })).status()).toBe(401);
  await api.dispose();
});

test("signing out other sessions leaves this browser in and signs every other one out", async ({ page, consoleServer, browser }) => {
  await signIn(page, consoleServer, "/settings/security");
  const table = page.getByRole("region", { name: "Active sessions" });
  const dataRows = () => table.getByRole("row").filter({ hasNot: page.getByRole("columnheader") });
  // The worker's server may already carry sessions from earlier tests; only the count going up
  // by the one about to sign in, and every one of them but this browser's own leaving, is asserted.
  await expect(dataRows()).not.toHaveCount(0);
  const before = await dataRows().count();

  const otherContext = await browser.newContext({ baseURL: consoleServer.url });
  const otherPage = await otherContext.newPage();
  await signIn(otherPage, consoleServer, "/settings/security");

  await page.reload();
  await expect(dataRows()).toHaveCount(before + 1);

  await page.getByRole("button", { name: "Sign out other sessions" }).click();
  const dialog = page.getByRole("dialog", { name: "Sign out other sessions?" });
  await expect(dialog).toBeVisible();
  await expectNoA11yViolations(page, "the sign out other sessions confirmation");
  await dialog.getByRole("button", { name: "Sign out other sessions" }).click();
  await expect(toastSaying(page, /^Signed out \d+ other sessions?$/)).toBeVisible();
  await expect(dialog).toBeHidden();
  await expect(dataRows()).toHaveCount(1);
  await expect(dataRows().getByText("This browser")).toBeVisible();
  await settle(page);
  await expectNoA11yViolations(page, "the sessions list after signing others out");

  // The other browser is signed out at its next request.
  await otherPage.reload();
  await expect(otherPage).toHaveURL(/\/login/);
  await otherContext.close();
});

test("a failure toast is announced once and stays reachable from the keyboard", async ({ page, consoleServer, problems }) => {
  problems.expect(/status of 404 .* \/api\/auth\/sessions\/[0-9a-f]+$/);
  const other = await otherSession(consoleServer);
  const { current_session: sid } = (await (await other.get("/api/auth/sessions")).json()) as { current_session: string };
  const prefix = sid.slice(0, 8);

  await signIn(page, consoleServer, "/settings/security");
  const signOut = page.getByRole("button", { name: `Sign out session ${prefix}` });
  await expect(signOut).toBeVisible();
  // The other browser signs out while this page still lists it.
  expect((await other.post("/api/auth/logout", { headers: await csrfHeaders(other) })).ok()).toBe(true);
  await other.dispose();

  await signOut.click();
  const toast = toastSaying(page, `Could not sign out session ${prefix}`);
  await expect(toast).toBeVisible();
  await expect(toast).toContainText("No active session matches that prefix. It may have expired.");
  await stillness(page);
  await expectNoA11yViolations(page, "a page with a failure toast");
  await expectAccessibleToast(page, `Could not sign out session ${prefix}`, "assertive");
});

// ---------------------------------------------------------------------------------------
// Replacing the backup codes would spend the worker server's pool that every other test signs
// in with, so this one gets a server of its own, with the usual eight.

const withOwnBackupCodes = test.extend<object, { consoleServer: ConsoleServer }>({
  consoleServer: [
    // eslint-disable-next-line no-empty-pattern -- Playwright requires the destructuring form
    async ({}, use) => {
      const server = await startConsoleServer(["--totp"]);
      try {
        await use(server);
      } finally {
        await server.stop();
      }
    },
    { scope: "worker", timeout: 75_000 },
  ],
});

withOwnBackupCodes("new backup codes replace the old ones after confirming it's you, and are shown once", async ({ page, consoleServer, problems }) => {
  // The first ask is refused until the operator confirms it's them, as designed.
  problems.expect(/status of 403 .* \/api\/auth\/2fa\/backup-codes$/);
  await signIn(page, consoleServer, "/settings/security");
  await page.getByRole("button", { name: "New backup codes" }).click();
  const ask = page.getByRole("dialog", { name: "Replace your backup codes?" });
  await expect(ask).toBeVisible();
  await stillness(page);
  await expectNoA11yViolations(page, "the replace backup codes question");
  await ask.getByRole("button", { name: "Replace backup codes" }).click();
  await confirmItsYou(page, consoleServer);

  const shown = page.getByRole("dialog", { name: "Save your backup codes" });
  await expect(shown.getByRole("list", { name: "Backup codes" }).getByRole("listitem")).toHaveCount(8);
  await expect(shown.getByRole("button", { name: "Done" })).toBeDisabled();
  await stillness(page);
  await expectNoA11yViolations(page, "the new backup codes");
  await shown.getByRole("checkbox", { name: "I have saved these codes somewhere safe" }).click();
  await shown.getByRole("button", { name: "Done" }).click();
  await expect(toastSaying(page, "Replaced the backup codes")).toBeVisible();
  await expect(page.getByText(/^8 of 8 backup codes left$/)).toBeVisible();
});

// ---------------------------------------------------------------------------------------
// Enrolment needs a server without two-factor: this group starts its own, in its own worker.

const withoutTwoFactor = test.extend<object, { consoleServer: ConsoleServer }>({
  consoleServer: [
    // eslint-disable-next-line no-empty-pattern -- Playwright requires the destructuring form
    async ({}, use) => {
      const server = await startConsoleServer([]);
      try {
        await use(server);
      } finally {
        await server.stop();
      }
    },
    { scope: "worker", timeout: 75_000 },
  ],
});

/** Signs in on a fresh page with the token and a code from `secret`. */
async function signInWithCode(page: Page, server: ConsoleServer, secret: string): Promise<void> {
  await page.goto("/login");
  await page.getByLabel("Access token").fill(server.token);
  await page.getByRole("button", { name: "Sign in" }).click();
  await page.getByLabel("Two-factor code").fill(totpCode(secret));
  await page.getByRole("button", { name: "Verify" }).click();
  await expect(page).not.toHaveURL(/\/login/);
}

withoutTwoFactor("enrol two-factor end to end, sign in with it, turn it off", async ({ page, consoleServer, problems, browser }) => {
  // Starting the enrolment asks "Confirm it's you" by answering 403 first, by design; two-
  // factor is off going in, so it is confirmed with the access token. That confirmation
  // elevates the session for the next 10 minutes, so turning it off at the end of this same
  // session does not ask again.
  problems.expect(/status of 403 .* \/api\/auth\/2fa\/enroll$/);
  await signIn(page, consoleServer, "/settings/security");
  const section = page.getByRole("region", { name: "Two-factor authentication" });
  await expect(section.getByText("Off", { exact: true })).toBeVisible();
  await section.getByRole("button", { name: "Set up two-factor authentication" }).click();
  await confirmItsYou(page, consoleServer);

  const dialog = page.getByRole("dialog", { name: "Set up two-factor authentication" });
  const qr = dialog.getByRole("img", { name: /QR code to add WASM/ });
  await expect(qr).toBeVisible();
  await expect(qr.locator("path")).toHaveAttribute("d", /^M\d/);
  const secret = ((await dialog.getByTestId("totp-secret").textContent()) ?? "").replace(/\s+/g, "");
  expect(secret).toMatch(/^[A-Z2-7]{16,}$/);
  await stillness(page);
  await expectNoA11yViolations(page, "the enrolment dialog");

  await dialog.getByLabel("Authentication code").fill(totpCode(secret));
  await dialog.getByRole("button", { name: "Turn on" }).click();

  const codes = page.getByRole("dialog", { name: "Save your backup codes" });
  await expect(codes).toBeVisible();
  const list = codes.getByRole("list", { name: "Backup codes" }).getByRole("listitem");
  await expect(list).toHaveCount(8);
  await expect(list.first()).toHaveText(/^[0-9a-f]{4}-[0-9a-f]{4}$/);
  await expect(codes.getByRole("button", { name: "Done" })).toBeDisabled();
  await page.keyboard.press("Escape");
  await expect(codes.getByRole("alert")).toContainText("They cannot be shown again");
  await expect(codes).toBeVisible();
  await stillness(page);
  await expectNoA11yViolations(page, "the backup codes");
  await codes.getByRole("checkbox", { name: "I have saved these codes somewhere safe" }).click();
  await codes.getByRole("button", { name: "Done" }).click();
  await expect(toastSaying(page, "Turned on two-factor authentication")).toBeVisible();
  await expect(section.getByText("On", { exact: true })).toBeVisible();
  await expect(section.getByText("8 of 8 backup codes left")).toBeVisible();

  // The enrolment is real: a fresh sign-in now needs the authenticator.
  const fresh = await browser.newContext({ baseURL: consoleServer.url });
  const second = await fresh.newPage();
  await signInWithCode(second, consoleServer, secret);
  await fresh.close();

  await section.getByRole("button", { name: "Turn off" }).click();
  const off = page.getByRole("dialog", { name: "Turn off two-factor authentication" });
  await off.getByLabel("Authentication or backup code").fill(totpCode(secret));
  await off.getByRole("button", { name: "Turn off" }).click();
  // Still elevated from confirming the enrolment above: no second "Confirm it's you".
  await expect(toastSaying(page, "Turned off two-factor authentication")).toBeVisible();
  await expect(section.getByRole("button", { name: "Set up two-factor authentication" })).toBeVisible();
  await settle(page);
  await expectNoA11yViolations(page, "security after turning two-factor off");
});
