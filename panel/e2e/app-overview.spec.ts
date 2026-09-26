/**
 * One application's page against the real backend: the header with its state and live
 * address, Update queuing a job and the header following it over the event stream, the
 * confirmations in front of Stop and Delete, and the Overview tab. Runs in both themes, with
 * the CSP and console gates of the `problems` fixture.
 */

import type { Page } from "@playwright/test";

import { expect, expectNoA11yViolations, settle, signIn, stillness, test, toasts, totpCode } from "./fixtures";

const DOMAIN = "picconia.com";

function header(page: Page) {
  return page.locator("main header").filter({ has: page.getByRole("heading", { level: 1 }) });
}

/** The header's state pill: the element that carries data-state. */
function pill(page: Page) {
  return header(page).locator("[data-state]").first();
}

test("the header says the app's state, type and port, links to the live site, and passes axe", async ({ page, consoleServer }) => {
  await signIn(page, consoleServer, `/apps/${DOMAIN}`);
  await expect(page.getByRole("heading", { level: 1 })).toHaveText(DOMAIN);
  await expect(pill(page)).toHaveAttribute("data-state", "running");
  await expect(pill(page)).toHaveText("Running");
  await expect(header(page).getByText("nextjs", { exact: true })).toBeVisible();

  const live = header(page).getByRole("link", { name: new RegExp(`^${DOMAIN.replace(".", "\\.")}`) });
  await expect(live).toHaveAttribute("href", `https://${DOMAIN}`);
  await expect(live).toHaveAttribute("target", "_blank");

  const tabs = page.getByRole("navigation", { name: "Application sections" });
  await expect(tabs.getByRole("link", { name: "Overview" })).toHaveAttribute("aria-current", "page");

  await expect(page.getByText("29 days left").or(page.getByText(/^\d+ days left$/))).toBeVisible();
  await settle(page);
  await expectNoA11yViolations(page, "an application's overview");
});

test("Update queues a job; the header follows it over the event stream to its end", async ({ page, consoleServer }) => {
  // What the stream tells the console about this app, in order. The seeded job ends within
  // milliseconds, so the "deploying" frame can be batched away before it is painted; that the
  // header draws it is pinned by the unit tests, and here the stream is shown to carry it.
  await page.addInitScript(() => {
    const seen: string[] = [];
    (window as unknown as { __appStates: string[] }).__appStates = seen;
    const Native = window.EventSource;
    window.EventSource = class extends Native {
      constructor(url: string | URL, init?: EventSourceInit) {
        super(url, init);
        this.addEventListener("app", (event: MessageEvent<string>) => {
          const data = JSON.parse(event.data) as { domain?: string; status?: string };
          if (data.domain === "picconia.com" && data.status) seen.push(data.status);
        });
      }
    };
  });
  await signIn(page, consoleServer, `/apps/${DOMAIN}`);
  await expect(pill(page)).toHaveAttribute("data-state", "running");

  const queued = page.waitForRequest((request) => request.url().endsWith("/api/jobs/update") && request.method() === "POST");
  await header(page).getByRole("button", { name: "Update" }).click();
  expect((await queued).postDataJSON()).toEqual({ domain: DOMAIN });

  // The sandbox has no checkout to update, so the job fails; the header keeps the failure,
  // in the updater's own words, until it is dismissed.
  const failure = page.getByText(`Update of ${DOMAIN} failed`);
  await expect(failure).toBeVisible();
  await expect(page.locator("pre").filter({ hasText: `Application not found: ${DOMAIN}` }).first()).toBeVisible();

  // The stream said deploying, then the app's state once the job ended; the header shows it.
  await expect
    .poll(() => page.evaluate(() => (window as unknown as { __appStates: string[] }).__appStates))
    .toEqual(expect.arrayContaining(["deploying", "running"]));
  const states = await page.evaluate(() => (window as unknown as { __appStates: string[] }).__appStates);
  expect(states.indexOf("deploying")).toBeLessThan(states.lastIndexOf("running"));
  await expect(pill(page)).toHaveAttribute("data-state", "running");
  await expect(header(page).getByRole("button", { name: "Update" })).not.toHaveAttribute("aria-busy");

  // The job's failure also raised the notice toast. Base UI hides a high-priority toast from
  // assistive technology until it is focused (its live region speaks for it) while leaving it
  // focusable, which axe reports as aria-hidden-focus: a design-system finding, reported, not
  // this page's. Dismissed here so axe judges the page. (A role query cannot see into it for
  // the same reason.)
  const toasts = page.locator('.toast button[aria-label="Dismiss notification"]');
  await expect(toasts).not.toHaveCount(0);
  // Stacked toasts behind the front one are transparent, so they are closed without a pointer.
  await toasts.evaluateAll((buttons) => {
    for (const button of buttons) (button as HTMLButtonElement).click();
  });
  await expect(toasts).toHaveCount(0);
  await stillness(page);
  await expectNoA11yViolations(page, "a failed update in the header");
  await page.getByRole("button", { name: "Dismiss", exact: true }).click();
  await expect(failure).toBeHidden();
});

test("Stop asks first; the header follows the unit down and back up", async ({ page, consoleServer }) => {
  // Its own app: stopping changes the worker's machine for every later test.
  const domain = "convertidordepdf.com";
  await signIn(page, consoleServer, `/apps/${domain}`);
  await expect(pill(page)).toHaveAttribute("data-state", "running");

  await header(page).getByRole("button", { name: "More actions" }).click();
  await page.getByRole("menuitem", { name: "Stop" }).click();
  const dialog = page.getByRole("dialog", { name: `Stop ${domain}?` });
  await expect(dialog).toBeVisible();
  await expectNoA11yViolations(page, "the stop confirmation");
  await dialog.getByRole("button", { name: "Stop application" }).click();
  await expect(dialog).toBeHidden();
  await expect(pill(page)).toHaveAttribute("data-state", "stopped");
  await expect(toasts(page).getByText(`Stopped ${domain}`)).toBeVisible();

  await header(page).getByRole("button", { name: "More actions" }).click();
  await page.getByRole("menuitem", { name: "Start" }).click();
  await expect(pill(page)).toHaveAttribute("data-state", "running");
});

test("Delete stays disabled until the domain is typed", async ({ page, consoleServer }) => {
  await signIn(page, consoleServer, `/apps/${DOMAIN}`);
  await header(page).getByRole("button", { name: "More actions" }).click();
  await page.getByRole("menuitem", { name: "Delete application" }).click();

  // Deleting is a sudo-mode action: "Confirm it's you" comes first, never on top of the dialog.
  const elevate = page.getByRole("dialog", { name: "Confirm it's you" });
  await expect(elevate).toBeVisible();
  await elevate.getByLabel("Authentication code").fill(totpCode(consoleServer.totpSecret ?? ""));
  await elevate.getByRole("button", { name: "Confirm" }).click();
  await expect(elevate).toBeHidden();

  const dialog = page.getByRole("alertdialog", { name: `Delete ${DOMAIN}` });
  await expect(dialog).toBeVisible();
  const confirm = dialog.getByRole("button", { name: "Delete application" });
  await expect(confirm).toBeDisabled();
  await dialog.getByRole("textbox").fill("picconia");
  await expect(confirm).toBeDisabled();
  await dialog.getByRole("textbox").fill(DOMAIN);
  await expect(confirm).toBeEnabled();
  await expectNoA11yViolations(page, "the delete confirmation");

  // Not confirmed: the worker's machine keeps its app.
  await dialog.getByRole("button", { name: "Cancel" }).click();
  await expect(dialog).toBeHidden();
  await expect(header(page).getByRole("button", { name: "More actions" })).toBeFocused();
});

test("Roll back lists the backups of an app deployed in place", async ({ page, consoleServer }) => {
  await signIn(page, consoleServer, `/apps/${DOMAIN}`);
  await header(page).getByRole("button", { name: "More actions" }).click();
  await page.getByRole("menuitem", { name: "Roll back" }).click();
  const dialog = page.getByRole("dialog", { name: `Roll back ${DOMAIN}` });
  await expect(dialog.getByRole("radio")).not.toHaveCount(0);
  await expect(dialog.getByRole("button", { name: "Roll back" })).toBeDisabled();
  await dialog.getByRole("radio").first().check();
  await expect(dialog.getByRole("button", { name: "Roll back" })).toBeEnabled();
  await expectNoA11yViolations(page, "the rollback dialog");
  await page.keyboard.press("Escape");
  await expect(dialog).toBeHidden();
});

test("the overview tab shows the deploys as dots that open each deploy, and the runtime", async ({ page, consoleServer }) => {
  const domain = "clientes.arennalabs.com";
  await signIn(page, consoleServer, `/apps/${domain}`);
  const dots = page.getByRole("list", { name: /^Last \d+ deploys, oldest first$/ });
  const newest = dots.getByRole("link").last();
  await expect(newest).toHaveAccessibleName(/^Deploy \d+: Failed c07d5e3/);
  // Its unit is the one systemd gave up on.
  await expect(pill(page)).toHaveAttribute("data-state", "failed");

  const facts = (await (await page.request.get(`/api/apps/${domain}`)).json()) as { source: string | null; branch: string | null };
  const runtime = page.getByRole("region", { name: "Runtime" });
  await expect(runtime.getByText("/var/www/apps/clientes.arennalabs.com")).toBeVisible();
  if (facts.source !== null) {
    const repo = runtime.getByRole("link", { name: new RegExp(`^${facts.source.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}`) });
    if (facts.source.startsWith("https://")) await expect(repo).toHaveAttribute("href", facts.source);
    else await expect(runtime.getByText(facts.source, { exact: true })).toBeVisible();
  }
  if (facts.branch !== null) await expect(runtime.getByText(facts.branch, { exact: true })).toBeVisible();
  // Not every seeded app has a recorded branch: the row still shows, and says so.
  else await expect(runtime.getByText("Not recorded").first()).toBeVisible();
  await expect(page.getByRole("region", { name: "Domains" }).getByText("No certificate").first()).toBeVisible();

  await newest.click();
  await expect(page).toHaveURL(/\/apps\/clientes\.arennalabs\.com\/deployments\/\d+$/);
});

test("on a phone the header's actions fold into one menu", async ({ page, consoleServer }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await signIn(page, consoleServer, `/apps/${DOMAIN}`);
  await expect(header(page).getByRole("button", { name: "Update" })).toBeHidden();
  await header(page).getByRole("button", { name: `Actions for ${DOMAIN}` }).click();
  await expect(page.getByRole("menuitem", { name: "Update" })).toBeVisible();
  await expect(page.getByRole("menuitem", { name: "Restart" })).toBeVisible();
  await page.keyboard.press("Escape");
  await settle(page);
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
  expect(overflow).toBeLessThanOrEqual(0);
  await expectNoA11yViolations(page, "an application on a phone");
});
