/**
 * An application's deployments against the real backend: the history with keyset "Load
 * more", a failed deploy's page with its error verbatim and the phase it stopped in, an
 * instant rollback of a release app, and a live update streaming its build log until it
 * succeeds. Runs in both themes, with the CSP and console gates of the `problems` fixture.
 *
 * The seeded machine (scripts/console_server.py, "an application's tabs"): tienda.cittek.es is
 * on releases with thirteen deploys, pedidos.cittek.es is in place with a real tree whose update
 * runs the real update sequence, its npm output streamed a line at a time.
 */

import type { Page } from "@playwright/test";

import { expect, expectNoA11yViolations, settle, signIn, stillness, test } from "./fixtures";

const RELEASES_APP = "tienda.cittek.es";
const LIVE_APP = "pedidos.cittek.es";

interface Row {
  id: number;
  status: string;
  error: string | null;
  commit_message: string | null;
}

async function deployment(page: Page, domain: string, pick: (row: Row) => boolean): Promise<number> {
  const response = await page.request.get(`/api/deployments?domain=${domain}&limit=50`);
  const body = (await response.json()) as { items: Row[] };
  const row = body.items.find(pick);
  if (!row) throw new Error(`no seeded deployment of ${domain} matches`);
  return row.id;
}

/** Dismisses the toasts a job's end raises, which axe would judge instead of the page. */
async function dismissToasts(page: Page): Promise<void> {
  const toasts = page.locator('.toast button[aria-label="Dismiss notification"]');
  await toasts.evaluateAll((buttons) => {
    for (const button of buttons) (button as HTMLButtonElement).click();
  });
  await expect(toasts).toHaveCount(0);
}

function phase(page: Page, key: string) {
  return page.getByRole("list", { name: "Deploy phases" }).locator(`[data-phase="${key}"]`);
}

test("the history pages by keyset, links each deploy to its page, and passes axe", async ({ page, consoleServer }) => {
  await signIn(page, consoleServer, `/apps/${RELEASES_APP}/deployments`);
  const table = page.getByRole("region", { name: `Deploys of ${RELEASES_APP}, newest first` });
  await expect(table.getByRole("row")).toHaveCount(11);
  const first = table.getByRole("link").first();
  await expect(first).toHaveAttribute("href", new RegExp(`/apps/${RELEASES_APP.replace(/\./g, "\\.")}/deployments/\\d+$`));

  const older = page.waitForRequest((request) => request.url().includes("/api/deployments?") && request.url().includes("before_id="));
  await page.getByRole("button", { name: "Load older deploys" }).click();
  expect(new URL((await older).url()).searchParams.get("domain")).toBe(RELEASES_APP);
  await expect(page.getByRole("button", { name: "Load older deploys" })).toBeHidden();
  expect(await table.getByRole("row").count()).toBeGreaterThan(11);

  const releases = page.getByRole("list", { name: `Releases of ${RELEASES_APP}, newest first` });
  await expect(releases.getByText("Serving", { exact: true })).toBeVisible();
  await expect(releases.getByText("Removed from disk")).toBeVisible();
  await settle(page);
  await expectNoA11yViolations(page, "an app's deployments");

  await first.click();
  await expect(page.getByRole("heading", { level: 2, name: /^Deployment \d+$/ })).toBeVisible();
});

test("the table shows each deploy's commit message beside its hash", async ({ page, consoleServer }) => {
  await signIn(page, consoleServer, `/apps/${RELEASES_APP}/deployments`);
  const response = await page.request.get(`/api/deployments?domain=${RELEASES_APP}&limit=50`);
  const body = (await response.json()) as { items: Row[] };
  const row = body.items.find((item) => item.commit_message !== null);
  if (!row?.commit_message) throw new Error(`no seeded deployment of ${RELEASES_APP} has a commit message`);
  const table = page.getByRole("region", { name: `Deploys of ${RELEASES_APP}, newest first` });
  // Truncated in the table, but the full subject line is there on hover, in the title attribute.
  await expect(table.getByTitle(row.commit_message)).toBeVisible();
});

test("a failed deploy says where it stopped, the fix above and the error verbatim", async ({ page, consoleServer }) => {
  await signIn(page, consoleServer, `/apps/${RELEASES_APP}/deployments`);
  const id = await deployment(page, RELEASES_APP, (row) => row.status === "failed" && (row.error ?? "").includes("Type error"));
  await page.goto(`/apps/${RELEASES_APP}/deployments/${String(id)}`);
  await expect(page.getByText("The deploy failed while building")).toBeVisible();
  await expect(page.locator("pre").filter({ hasText: "Type error: Property 'total' does not exist on type 'Order'." })).toBeVisible();
  await expect(phase(page, "build")).toHaveAttribute("data-state", "failed");
  await expect(phase(page, "install")).toHaveAttribute("data-state", "done");
  await expect(phase(page, "activate")).toHaveAttribute("data-state", "unrecorded");
  const log = page.getByRole("region", { name: `Build log of deployment ${String(id)} of ${RELEASES_APP}` });
  await expect(log.getByText("Failed to compile.")).toBeVisible();
  await settle(page);
  await expectNoA11yViolations(page, "a failed deploy");
});

test("a release app rolls back to an earlier release in seconds, and forward again", async ({ page, consoleServer }) => {
  await signIn(page, consoleServer, `/apps/${RELEASES_APP}/deployments`);
  const releases = page.getByRole("list", { name: `Releases of ${RELEASES_APP}, newest first` });
  const serving = releases.getByRole("listitem").filter({ has: page.getByText("Serving", { exact: true }) });
  const before = (await serving.locator(".mono").first().textContent()) ?? "";

  // The newest release that is not serving and still on disk: the one to go back to.
  const target = releases.getByRole("listitem").filter({ has: page.getByRole("button", { name: /Roll back to this/ }) }).first();
  const targetId = (await target.locator(".mono").first().textContent()) ?? "";
  await target.getByRole("button", { name: /Roll back to this/ }).click();
  const dialog = page.getByRole("dialog", { name: "Roll back to this release?" });
  await expect(dialog).toBeVisible();
  await expectNoA11yViolations(page, "the rollback confirmation");
  const activated = page.waitForResponse((response) => response.url().endsWith(`/releases/${targetId}/activate`));
  await dialog.getByRole("button", { name: "Roll back" }).click();
  expect((await activated).status()).toBe(200);
  await expect(dialog).toBeHidden();
  await expect(serving.locator(".mono").first()).toHaveText(targetId);

  // Forward again, so the worker's machine is as it was for the next test.
  await releases.getByRole("listitem").filter({ hasText: before }).getByRole("button", { name: /Activate/ }).click();
  const forward = page.getByRole("dialog", { name: "Activate this release?" });
  await forward.getByRole("button", { name: "Activate" }).click();
  await expect(forward).toBeHidden();
  await expect(serving.locator(".mono").first()).toHaveText(before);
});

test("a redeploy streams its build log live and flips to success", async ({ page, consoleServer }) => {
  await signIn(page, consoleServer, `/apps/${LIVE_APP}/deployments`);
  const latest = await deployment(page, LIVE_APP, () => true);
  await page.goto(`/apps/${LIVE_APP}/deployments/${String(latest)}`);
  await expect(page.getByRole("heading", { level: 2, name: `Deployment ${String(latest)}` })).toBeVisible();

  await page.getByRole("button", { name: "Redeploy" }).click();
  // The console opens the deploy the update started as soon as it is recorded.
  await expect(page).not.toHaveURL(new RegExp(`/deployments/${String(latest)}$`));
  const heading = page.getByRole("heading", { level: 2, name: /^Deployment \d+$/ });
  await expect(heading).not.toHaveText(`Deployment ${String(latest)}`);
  const title = (await heading.textContent()) ?? "";
  const log = page.getByRole("region", { name: `Build log of ${title.toLowerCase()} of ${LIVE_APP}` });

  // Streamed while running: the install's output arrives before the build's.
  await expect(page.getByText("In progress", { exact: true }).first()).toBeVisible();
  await expect(log.getByText("added 214 packages, and audited 215 packages in 6s")).toBeVisible();
  await expect(log.getByText("Build finished in 4.2s")).toBeVisible({ timeout: 20_000 });
  await expect(page.getByText("Succeeded", { exact: true }).first()).toBeVisible({ timeout: 20_000 });
  await expect(phase(page, "build")).toHaveAttribute("data-state", "done");
  await expect(phase(page, "activate")).toHaveAttribute("data-state", "done");

  await dismissToasts(page);
  await settle(page);
  await stillness(page);
  await expectNoA11yViolations(page, "a deploy that just succeeded");
});

test("an in-place app offers its backups to roll back to", async ({ page, consoleServer }) => {
  await signIn(page, consoleServer, `/apps/${LIVE_APP}/deployments`);
  const points = page.getByRole("list", { name: `Backups of ${LIVE_APP}, newest first` });
  // Two seeded, and one more for every update a test in this worker ran before.
  await expect(points.getByRole("listitem").nth(1)).toBeVisible();
  await points.getByRole("button", { name: /Roll back to this/ }).first().click();
  const dialog = page.getByRole("dialog", { name: "Roll back to this backup?" });
  await expect(dialog).toBeVisible();
  await expectNoA11yViolations(page, "the backup rollback confirmation");
  // Not confirmed: the worker's app keeps its files.
  await dialog.getByRole("button", { name: "Cancel" }).click();
  await expect(dialog).toBeHidden();
});
