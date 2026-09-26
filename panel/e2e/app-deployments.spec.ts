/**
 * An application's deployments against the real backend: the history with keyset "Load
 * more", a failed deploy's page with its error verbatim and the phase it stopped in, an
 * instant rollback of a release app, a live update streaming its build log until it
 * succeeds, and a deploy's own actions: rebuilding its commit and going back to it. Runs in
 * both themes, with the CSP and console gates of the `problems` fixture.
 *
 * The seeded machine (scripts/console_server.py, "an application's tabs" and "a deploy's
 * actions"): tienda.cittek.es is on releases with thirteen deploys, two of whose releases are
 * on disk and not serving; pedidos.cittek.es is in place with a real tree whose update runs the
 * real update sequence, its npm output streamed a line at a time, and a deployment whose
 * snapshot backup still exists; bodas.arennalabs.com is a static site with one deploy.
 */

import type { Page } from "@playwright/test";

import { expect, expectNoA11yViolations, settle, signIn, stillness, test } from "./fixtures";

const RELEASES_APP = "tienda.cittek.es";
const LIVE_APP = "pedidos.cittek.es";
const STATIC_APP = "bodas.arennalabs.com";

interface Row {
  id: number;
  status: string;
  error: string | null;
  commit_message: string | null;
  git_commit: string | null;
  release_id: string | null;
  job_id: string | null;
  rollback_available: boolean;
  rollback_unavailable_reason: string | null;
  snapshot_backup: string | null;
}

interface ReleaseRow {
  id: string;
  active: boolean;
}

/** The CSRF header every write through `page.request` carries, mirrored from its cookie. */
async function csrf(page: Page): Promise<Record<string, string>> {
  const cookie = (await page.context().cookies()).find((entry) => entry.name === "wasm_csrf");
  return cookie ? { "X-WASM-CSRF": cookie.value } : {};
}

async function serving(page: Page, domain: string): Promise<string> {
  const body = (await (await page.request.get(`/api/apps/${domain}/releases`)).json()) as { items: ReleaseRow[] };
  const active = body.items.find((release) => release.active);
  if (!active) throw new Error(`nothing serves ${domain}`);
  return active.id;
}

/** Puts a release back in service through the API, as the Deployments tab's Activate does. */
/** The seeded deploy of 19d3f6e, whose release is on disk: not a row a rollback to it wrote since. */
function seededOnDisk(row: Row): boolean {
  return row.git_commit === "19d3f6e" && row.release_id !== null && row.rollback_available;
}

async function activate(page: Page, domain: string, release: string): Promise<void> {
  const response = await page.request.post(`/api/apps/${domain}/releases/${release}/activate`, { headers: await csrf(page) });
  expect(response.ok(), await response.text()).toBe(true);
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

test("an update streams its build log live and flips to success", async ({ page, consoleServer }) => {
  await signIn(page, consoleServer, `/apps/${LIVE_APP}/deployments`);
  const latest = await deployment(page, LIVE_APP, () => true);

  await page.locator("main header").getByRole("button", { name: "Update", exact: true }).click();
  // The update records its deploy within moments; opened as soon as it is there.
  let next = latest;
  await expect
    .poll(
      async () => {
        next = await deployment(page, LIVE_APP, () => true);
        return next;
      },
      { intervals: [250] },
    )
    .not.toBe(latest);
  await page.goto(`/apps/${LIVE_APP}/deployments/${String(next)}`);
  const log = page.getByRole("region", { name: `Build log of deployment ${String(next)} of ${LIVE_APP}` });

  // Streamed while running: the install's output arrives before the build's.
  await expect(page.getByText("In progress", { exact: true }).first()).toBeVisible();
  await expect(log.getByText("added 214 packages, and audited 215 packages in 6s")).toBeVisible();
  await expect(log.getByText("Build finished in 4.2s")).toBeVisible({ timeout: 20_000 });
  await expect(page.getByText("Succeeded", { exact: true }).first()).toBeVisible({ timeout: 20_000 });
  await expect(phase(page, "build")).toHaveAttribute("data-state", "done");
  await expect(phase(page, "activate")).toHaveAttribute("data-state", "done");
  // The header's pill pulses once when the app comes back up; axe judges it after that.
  await expect(page.locator("main header").locator("[data-state]").first()).toHaveAttribute("data-state", "running");

  await dismissToasts(page);
  await settle(page);
  await stillness(page);
  await expectNoA11yViolations(page, "a deploy that just succeeded");
});

test("rebuilding a deploy's commit says what it does first, then follows the job to its end", async ({ page, consoleServer }) => {
  await signIn(page, consoleServer, `/apps/${RELEASES_APP}/deployments`);
  const id = await deployment(page, RELEASES_APP, seededOnDisk);
  await page.goto(`/apps/${RELEASES_APP}/deployments/${String(id)}`);

  await page.getByRole("button", { name: "Rebuild this commit" }).click();
  const dialog = page.getByRole("dialog", { name: "Rebuild commit 19d3f6e?" });
  await expect(dialog.getByText(/If a release built from 19d3f6e is still on disk and is not the one serving, it is activated in seconds/)).toBeVisible();
  await settle(page);
  await expectNoA11yViolations(page, "the rebuild confirmation");

  const queued = page.waitForResponse((response) => response.url().endsWith(`/api/apps/${RELEASES_APP}/deployments/${String(id)}/rebuild`));
  await dialog.getByRole("button", { name: "Rebuild" }).click();
  expect((await queued).status()).toBe(202);
  await expect(dialog).toBeHidden();
  // The sandbox's repository cache holds no commits, so the job ends in the updater's own
  // words - the part that matters here is that the page followed the job to its end.
  await expect(page.getByText("The rebuild failed")).toBeVisible({ timeout: 30_000 });
  await expect(page.locator("pre").filter({ hasText: "Commit 19d3f6e does not exist in the repository" }).first()).toBeVisible();
});

test("a release app goes back to a deployment through its own endpoint, and forward again", async ({ page, consoleServer }) => {
  await signIn(page, consoleServer, `/apps/${RELEASES_APP}/deployments`);
  const before = await serving(page, RELEASES_APP);
  const id = await deployment(page, RELEASES_APP, seededOnDisk);
  await page.goto(`/apps/${RELEASES_APP}/deployments/${String(id)}`);

  await page.getByRole("button", { name: "Roll back to this" }).click();
  const dialog = page.getByRole("dialog", { name: `Roll back to deployment ${String(id)}?` });
  await expect(dialog.getByText(/is activated in seconds and the app restarts/)).toBeVisible();
  await settle(page);
  await expectNoA11yViolations(page, "the rollback-to-a-deployment confirmation");
  const queued = page.waitForResponse((response) => response.url().endsWith(`/api/apps/${RELEASES_APP}/deployments/${String(id)}/rollback`));
  await dialog.getByRole("button", { name: "Roll back", exact: true }).click();
  expect((await queued).status()).toBe(202);
  await expect(page.getByText(`Rolled back to deployment ${String(id)}.`)).toBeVisible({ timeout: 30_000 });
  expect(await serving(page, RELEASES_APP)).toMatch(/-19d3f6e$/);

  // Forward again, so the worker's machine is as it was for the next test.
  await activate(page, RELEASES_APP, before);
});

test("the deploy that is live says why it cannot be gone back to, and offers the other versions", async ({ page, consoleServer }) => {
  await signIn(page, consoleServer, `/apps/${RELEASES_APP}/deployments`);
  const live = await serving(page, RELEASES_APP);
  const id = await deployment(page, RELEASES_APP, (row) => row.release_id === live);
  await page.goto(`/apps/${RELEASES_APP}/deployments/${String(id)}`);
  await expect(page.getByText(`Can't roll back to this deployment: Release ${live} is already live.`)).toBeVisible();
  await page.getByRole("button", { name: "Roll back…" }).click();
  const chooser = page.getByRole("dialog", { name: `Roll back ${RELEASES_APP}` });
  await expect(chooser.getByRole("radio").first()).toBeVisible();
  await chooser.getByRole("button", { name: "Cancel" }).click();
  await expect(chooser).toBeHidden();
});

test("an in-place deploy whose snapshot still exists offers to go back to it", async ({ page, consoleServer }) => {
  await signIn(page, consoleServer, `/apps/${LIVE_APP}/deployments`);
  const response = await page.request.get(`/api/deployments?domain=${LIVE_APP}&limit=50`);
  const rows = ((await response.json()) as { items: Row[] }).items;
  const restorable = rows.find((row) => row.rollback_available && row.snapshot_backup !== null);
  if (!restorable?.snapshot_backup) throw new Error(`no seeded deployment of ${LIVE_APP} has a snapshot`);
  await page.goto(`/apps/${LIVE_APP}/deployments/${String(restorable.id)}`);
  await page.getByRole("button", { name: "Roll back to this" }).click();
  const dialog = page.getByRole("dialog", { name: `Roll back to deployment ${String(restorable.id)}?` });
  await expect(dialog.getByText(new RegExp(`restored from backup ${restorable.snapshot_backup}.*A backup of the current state is taken first`))).toBeVisible();
  await settle(page);
  await expectNoA11yViolations(page, "an in-place rollback to a deployment");
  // Not confirmed: the worker's app keeps its files.
  await dialog.getByRole("button", { name: "Cancel" }).click();
  await expect(dialog).toBeHidden();
});

test.describe("in a browser five and a half hours from the server", () => {
  test.use({ timezoneId: "Asia/Kolkata" });

  test("a static site's deploy has no health check, and its phases take the log's own seconds", async ({ page, consoleServer }) => {
    await signIn(page, consoleServer, `/apps/${STATIC_APP}/deployments`);
    const id = await deployment(page, STATIC_APP, () => true);
    await page.goto(`/apps/${STATIC_APP}/deployments/${String(id)}`);
    await expect(phase(page, "health")).toHaveAttribute("data-state", "not_applicable");
    await expect(phase(page, "health")).toContainText("Not applicable");
    await expect(page.getByText(/Health does not apply: a static site is served as files/)).toBeVisible();
    // Activating to the last line: four seconds, whatever zone the browser is in.
    await expect(phase(page, "activate")).toContainText("4.0s");
    await expect(phase(page, "fetch")).toContainText("6.0s");
    await settle(page);
    await expectNoA11yViolations(page, "a static site's deploy");
  });
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
