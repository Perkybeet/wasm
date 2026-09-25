/**
 * An application's Settings tab against the real backend: the deploy webhook's secret shown
 * once, the deliveries, resource limits refused and then saved with the exact body, an
 * in-place app migrated to releases after reading its plan, and the deletion that waits for
 * the domain to be typed. Sudo-mode actions go through the real "Confirm it's you".
 *
 * A migration cannot be undone through the API and both theme projects may run in the same
 * worker, so each project migrates its own app; the limits alternate by project for the same
 * reason. Screenshots of the states worth reviewing are written when WASM_TABS_SCREENS is set.
 */

import type { Page, TestInfo } from "@playwright/test";
import path from "node:path";

import type { ConsoleServer } from "./fixtures";
import { expect, expectNoA11yViolations, settle, signIn, test, totpCode } from "./fixtures";

const RELEASE_APP = "tienda.cittek.es";
const IN_PLACE_APP = "pedidos.cittek.es";

function region(page: Page, name: string) {
  return page.getByRole("region", { name, exact: true });
}

/** Answers "Confirm it's you" with a fresh two-factor code. */
async function confirmItsYou(page: Page, server: ConsoleServer): Promise<void> {
  const dialog = page.getByRole("dialog", { name: "Confirm it's you" });
  await expect(dialog).toBeVisible();
  if (server.totpSecret === null) throw new Error("the E2E server runs with two-factor sign-in");
  await dialog.getByLabel("Authentication code").fill(totpCode(server.totpSecret));
  await dialog.getByRole("button", { name: "Confirm" }).click();
  await expect(dialog).toBeHidden();
}

/**
 * Waits for every finite animation to end: a dialog fades in, and axe measuring contrast
 * mid-fade reports a colour that is on screen for a few frames only.
 */
async function stillness(page: Page): Promise<void> {
  await page.waitForFunction(() =>
    document.getAnimations().every((animation) => {
      const iterations = animation.effect?.getComputedTiming().iterations;
      return animation.playState !== "running" || iterations === Infinity;
    }),
  );
}

/** A screenshot for review, when asked for: `WASM_TABS_SCREENS=/tmp/console-tabs`. */
async function review(page: Page, testInfo: TestInfo, name: string): Promise<void> {
  const out = process.env.WASM_TABS_SCREENS;
  if (!out) return;
  await settle(page);
  await page.screenshot({ path: path.join(out, testInfo.project.name, `${name}.png`), fullPage: false });
}

test("the webhook lists its deliveries and shows a new secret once", async ({ page, consoleServer }, testInfo) => {
  await page.setViewportSize({ width: 1440, height: 1000 });
  await signIn(page, consoleServer, `/apps/${RELEASE_APP}/settings`);
  const webhook = region(page, "Deploy webhook");
  const deliveries = webhook.getByRole("table");
  await expect(deliveries.getByRole("link", { name: /^Deploy \d+$/ }).first()).toBeVisible();
  await expect(webhook.getByText(/^Last delivery/)).toBeVisible();

  // Deliveries were signed with a secret, so a new one is a replacement: asked first.
  await webhook.getByRole("button", { name: "Regenerate secret" }).click();
  const confirm = page.getByRole("dialog", { name: "Regenerate the secret?" });
  await expect(confirm).toBeVisible();
  const minted = page.waitForResponse((r) => r.url().endsWith(`/api/apps/${RELEASE_APP}/webhook-secret`) && r.request().method() === "POST");
  await confirm.getByRole("button", { name: "Regenerate secret" }).click();
  const body = (await (await minted).json()) as { secret: string; hook_url: string };

  const shown = webhook.getByRole("region", { name: "Copy the secret now" });
  await expect(shown.getByText(body.secret, { exact: true })).toBeVisible();
  await expect(shown.getByText(body.hook_url, { exact: true })).toBeVisible();
  expect(body.hook_url).toMatch(new RegExp(`/hooks/deploy/${RELEASE_APP.replace(/\./g, "\\.")}$`));
  await expect(shown.getByRole("button", { name: "Copy secret" })).toBeVisible();
  await expect(shown.getByRole("button", { name: "Copy payload URL" })).toBeVisible();
  await expect(webhook.getByText("Enabled", { exact: true })).toBeVisible();
  await shown.scrollIntoViewIfNeeded();
  await stillness(page);
  await review(page, testInfo, "settings-webhook-secret-1440");
  await expectNoA11yViolations(page, "a webhook secret shown once");

  await shown.getByRole("button", { name: "Hide the secret" }).click();
  await expect(webhook.getByText(body.secret, { exact: true })).toHaveCount(0);
});

test("limits are refused as the backend would, then saved with exactly what the form says", async ({ page, consoleServer }, testInfo) => {
  // Alternating by project keeps the second run in one worker a change too.
  const memory = testInfo.project.name === "dark" ? 320 : 256;
  await signIn(page, consoleServer, `/apps/${IN_PLACE_APP}/settings`);
  const limits = region(page, "Resource limits");
  const memoryField = limits.getByRole("textbox", { name: "Memory" });
  await expect(memoryField).toBeVisible();

  await memoryField.fill("32");
  await limits.getByRole("button", { name: "Save limits" }).click();
  await expect(limits.getByText("A memory limit of 32M is too small. Allow at least 64M, or no limit.")).toBeVisible();
  await expectNoA11yViolations(page, "a refused limit");

  await memoryField.fill(String(memory));
  await limits.getByRole("textbox", { name: "CPU" }).fill("50");
  await limits.getByRole("textbox", { name: "Tasks" }).fill("128");
  const patched = page.waitForRequest((r) => r.url().endsWith(`/api/apps/${IN_PLACE_APP}/limits`) && r.method() === "PATCH");
  await limits.getByRole("button", { name: "Save limits" }).click();
  await confirmItsYou(page, consoleServer);
  expect((await patched).postDataJSON()).toEqual({ memory_max_mb: memory, cpu_quota_percent: 50, tasks_max: 128, restart: false });

  await expect(limits.getByText(/^Saved\. .* was rewritten; the running process keeps its old limits until it restarts\.$/)).toBeVisible();
  await expect(limits.getByText(`MemoryMax=${String(memory)}M CPUQuota=50% TasksMax=128`)).toBeVisible();
  await expect(limits.getByRole("button", { name: "Restart now" })).toBeVisible();
});

test("an in-place app moves to releases after its plan is read and confirmed", async ({ page, consoleServer }, testInfo) => {
  const domain = testInfo.project.name === "dark" ? "docs.cittek.es" : "blog.cittek.es";
  await page.setViewportSize({ width: 1440, height: 1000 });
  await signIn(page, consoleServer, `/apps/${domain}/settings`);
  const releases = region(page, "Releases");

  const current = (await (await page.request.get(`/api/apps/${domain}`)).json()) as { layout: string };
  test.skip(current.layout === "releases", `${domain} was migrated by an earlier attempt in this worker`);

  await releases.getByRole("button", { name: "Plan the migration" }).click();
  await expect(releases.getByText(/is not a git checkout/)).toBeVisible();
  await expect(releases.getByText("uploads, storage", { exact: true })).toBeVisible();
  await expect(releases.getByText("The usual upload directories found in the tree")).toBeVisible();
  await expect(releases.getByText(/, rewritten to run from current$/)).toBeVisible();
  await releases.scrollIntoViewIfNeeded();
  await review(page, testInfo, "settings-migration-plan-1440");
  await expectNoA11yViolations(page, "a migration plan");

  await releases.getByRole("button", { name: "Migrate to releases" }).click();
  await confirmItsYou(page, consoleServer);
  const dialog = page.getByRole("dialog", { name: `Migrate ${domain} to releases?` });
  await expect(dialog).toContainText("uploads, storage move to shared/");
  await stillness(page);
  await review(page, testInfo, "settings-migration-confirm-1440");
  await expectNoA11yViolations(page, "the migration confirmation");

  const migrated = page.waitForResponse((r) => r.url().endsWith(`/api/apps/${domain}/migrate`) && r.request().method() === "POST");
  await dialog.getByRole("button", { name: "Migrate to releases" }).click();
  const response = await migrated;
  expect(response.status()).toBe(200);
  expect(response.request().postDataJSON()).toEqual({ persist: ["uploads", "storage"] });
  const result = (await response.json()) as { files_before: number; files_after: number };
  expect(result.files_after).toBe(result.files_before);

  await expect(dialog).toBeHidden();
  await expect(page.getByText(`${String(result.files_before)} files before, ${String(result.files_after)} after`, { exact: false })).toBeVisible();
  // The app is now on releases: the section shows the release serving.
  await expect(region(page, "Source and runtime").getByText("Releases", { exact: true })).toBeVisible();
  await expect(releases.getByText("Serving", { exact: true })).toBeVisible();
});

test("deleting waits for the domain to be typed, and cancelling deletes nothing", async ({ page, consoleServer }) => {
  await signIn(page, consoleServer, `/apps/${RELEASE_APP}/settings`);
  const deletions: string[] = [];
  page.on("request", (request) => {
    if (request.method() === "DELETE" && request.url().includes(`/api/apps/${RELEASE_APP}`)) deletions.push(request.url());
  });
  const zone = region(page, "Danger zone");
  await zone.getByRole("checkbox", { name: /Also delete its files/ }).click();
  await zone.getByRole("button", { name: "Delete application" }).click();
  await confirmItsYou(page, consoleServer);

  const dialog = page.getByRole("alertdialog", { name: `Delete ${RELEASE_APP}` });
  await expect(dialog).toContainText("Kept: backups and the files.");
  const action = dialog.getByRole("button", { name: "Delete application" });
  await expect(action).toBeDisabled();
  await dialog.getByRole("textbox").fill("tienda");
  await expect(action).toBeDisabled();
  await dialog.getByRole("textbox").fill(RELEASE_APP);
  await expect(action).toBeEnabled();
  await stillness(page);
  await expectNoA11yViolations(page, "the delete confirmation");

  await dialog.getByRole("button", { name: "Cancel" }).click();
  await expect(dialog).toBeHidden();
  expect(deletions).toEqual([]);
  await expect(page).toHaveURL(new RegExp(`/apps/${RELEASE_APP.replace(/\./g, "\\.")}/settings$`));
});
