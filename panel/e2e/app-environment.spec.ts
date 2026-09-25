/**
 * An application's environment against the real backend: a messy `.env` pasted into the
 * bulk editor is saved as exactly the map EnvManager reads from the same file (the shared
 * fixture tests/fixtures/env/messy.env and its .json), and a secret is shown only after the
 * operator confirms it's them. Runs in both themes with the CSP and console gates of the
 * `problems` fixture.
 *
 * The paste replaces the whole file, so each theme project writes to its own app: the light
 * one to the in-place pedidos.cittek.es (<app>/.env), the dark one to the release app
 * tienda.cittek.es (shared/.env). Both projects can share a worker's machine.
 */

import type { Page, TestInfo } from "@playwright/test";
import { readFileSync } from "node:fs";
import path from "node:path";

import type { ConsoleServer } from "./fixtures";
import { expect, expectNoA11yViolations, settle, signIn, test, totpCode } from "./fixtures";

const FIXTURES = path.resolve(import.meta.dirname, "..", "..", "tests", "fixtures", "env");
const MESSY = readFileSync(path.join(FIXTURES, "messy.env"), "utf8");
const MESSY_MAP = JSON.parse(readFileSync(path.join(FIXTURES, "messy.json"), "utf8")) as Record<string, string>;

const SCREENS = process.env.WASM_TABS_SCREENS ?? "/tmp/console-tabs";

function appFor(testInfo: TestInfo): string {
  return testInfo.project.name === "dark" ? "tienda.cittek.es" : "pedidos.cittek.es";
}

async function confirmItsYou(page: Page, server: ConsoleServer): Promise<void> {
  const dialog = page.getByRole("dialog", { name: "Confirm it's you" });
  await expect(dialog).toBeVisible();
  await dialog.getByLabel("Authentication code").fill(totpCode(server.totpSecret ?? ""));
  await dialog.getByRole("button", { name: "Confirm" }).click();
  await expect(dialog).toBeHidden();
}

function table(page: Page, domain: string) {
  return page.getByRole("table", { name: `Environment variables of ${domain}` });
}

test("a messy .env pasted in is saved as exactly what EnvManager reads from it", async ({ page, consoleServer, problems }, testInfo) => {
  const domain = appFor(testInfo);
  // Reading the values in clear is refused until the operator confirms it's them.
  problems.expect(new RegExp(`status of 403 .*/api/apps/${domain.replace(/\./g, "\\.")}/env$`));
  await signIn(page, consoleServer, `/apps/${domain}/environment`);
  await expect(table(page, domain)).toBeVisible();
  await settle(page);
  await expectNoA11yViolations(page, "the environment tab");

  await page.getByRole("button", { name: "Paste .env" }).click();
  const paste = page.getByRole("dialog", { name: "Paste a .env file" });
  await paste.getByLabel(".env contents").fill(MESSY);
  const count = Object.keys(MESSY_MAP).length;
  await expect(paste.getByText(`${String(count)} variables found`)).toBeVisible();
  await expect(paste.getByText(/PORT is set on lines \d+ and \d+; the later line wins\./)).toBeVisible();
  await paste.getByRole("radio", { name: "Replace all" }).click();
  await expectNoA11yViolations(page, "the paste dialog");
  await paste.getByRole("button", { name: `Stage ${String(count)} variables` }).click();
  await expect(paste).toBeHidden();

  await page.getByRole("button", { name: "Review and save" }).click();
  await confirmItsYou(page, consoleServer);
  const review = page.getByRole("dialog", { name: "Review changes" });
  await expect(review).toBeVisible();
  await review.getByRole("switch", { name: "Show values" }).click();
  await expectNoA11yViolations(page, "the review dialog");

  const put = page.waitForRequest((request) => request.method() === "PUT" && request.url().endsWith(`/api/apps/${domain}/env`));
  await review.getByRole("button", { name: "Save changes" }).click();
  expect((await put).postDataJSON()).toEqual({ variables: MESSY_MAP });

  const saved = page.getByRole("dialog", { name: "Environment saved" });
  await expect(saved).toBeVisible();
  await saved.getByRole("button", { name: "Restart now" }).click();
  await expect(saved).toBeHidden();
  await expect(page.getByRole("region", { name: "Notifications" }).getByText(`Restarted ${domain}`)).toBeVisible();

  // The file on disk now reads back as the pasted map: what the table shows comes from it.
  await expect(table(page, domain).getByRole("cell", { name: "GREETING", exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Reveal the value of GREETING" }).click();
  await expect(table(page, domain).getByText("hola # not a comment", { exact: true })).toBeVisible();
  await expect(table(page, domain).getByRole("cell", { name: "SESSION_SECRET", exact: true })).toHaveCount(0);
});

test("a secret is shown only after confirming it's you", async ({ page, consoleServer, problems }) => {
  // The same value in the seeded file and in messy.env, so the other test's save does not matter.
  const domain = "pedidos.cittek.es";
  problems.expect(/status of 403 .*\/api\/apps\/pedidos\.cittek\.es\/env$/);
  await signIn(page, consoleServer, `/apps/${domain}/environment`);
  await expect(table(page, domain)).toBeVisible();
  await expect(table(page, domain).getByText("2f7c9e1a4b6d8f0a3c5e7b9d1f2a4c6e")).toHaveCount(0);

  await page.getByRole("button", { name: "Reveal the value of JWT_SECRET" }).click();
  await expectNoA11yViolations(page, "the confirmation before a secret is revealed");
  await confirmItsYou(page, consoleServer);
  await expect(table(page, domain).getByText("2f7c9e1a4b6d8f0a3c5e7b9d1f2a4c6e", { exact: true })).toBeVisible();
  await expectNoA11yViolations(page, "a revealed secret");

  await page.getByRole("button", { name: "Hide the value of JWT_SECRET" }).click();
  await expect(table(page, domain).getByText("2f7c9e1a4b6d8f0a3c5e7b9d1f2a4c6e")).toHaveCount(0);
});

test.describe("environment dialogs @screens", () => {
  const DESKTOP = { width: 1440, height: 900 };
  const PHONE = { width: 390, height: 844 };

  test("app tab environment dialogs", async ({ page, consoleServer, problems }, testInfo) => {
    problems.expect(/status of 403 .*\/api\/apps\/blog\.cittek\.es\/env$/);
    const dir = path.join(SCREENS, testInfo.project.name);
    // Read only: the dialogs are photographed and cancelled, nothing is saved.
    const domain = "blog.cittek.es";
    await page.setViewportSize(DESKTOP);
    await signIn(page, consoleServer, `/apps/${domain}/environment`);
    await expect(table(page, domain)).toBeVisible();

    await page.getByRole("button", { name: "Paste .env" }).click();
    const paste = page.getByRole("dialog", { name: "Paste a .env file" });
    await paste.getByLabel(".env contents").fill(`export API_URL=https://api.cittek.es\n${MESSY}`);
    await settle(page);
    await page.screenshot({ path: path.join(dir, "environment-paste-1440.png") });
    await page.setViewportSize(PHONE);
    await settle(page);
    await page.screenshot({ path: path.join(dir, "environment-paste-390.png") });

    await paste.getByRole("button", { name: "Remove the export prefixes" }).click();
    await paste.getByRole("button", { name: /^Stage \d+ variables$/ }).click();
    await page.setViewportSize(DESKTOP);
    await page.getByRole("button", { name: "Review and save" }).click();
    await confirmItsYou(page, consoleServer);
    const review = page.getByRole("dialog", { name: "Review changes" });
    await review.getByRole("switch", { name: "Show values" }).click();
    await settle(page);
    await page.screenshot({ path: path.join(dir, "environment-review-1440.png") });
    await page.setViewportSize(PHONE);
    await settle(page);
    await page.screenshot({ path: path.join(dir, "environment-review-390.png") });
    await review.getByRole("button", { name: "Cancel" }).click();

    await page.setViewportSize(DESKTOP);
    await settle(page);
    await page.screenshot({ path: path.join(dir, "environment-draft-1440.png"), fullPage: true });
  });
});
