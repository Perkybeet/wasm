/**
 * The new-app wizard against the real backend: a seeded directory on the server is inspected
 * by the real `POST /api/apps/inspect`, the review proposes what it found, and the deploy is
 * queued with `POST /api/apps` and handed over to its deployment page. Runs in both themes,
 * with the CSP and console gates of the `problems` fixture and axe on every step.
 */

import type { Page } from "@playwright/test";

import { expect, expectNoA11yViolations, settle, signIn, test, totpCode } from "./fixtures";
import type { ConsoleServer } from "./fixtures";
import { wizardSource } from "./wizard-sources";

/** The CSRF header every write through `page.request` carries, mirrored from its cookie. */
async function csrf(page: Page): Promise<Record<string, string>> {
  const cookie = (await page.context().cookies()).find((entry) => entry.name === "wasm_csrf");
  return cookie ? { "X-WASM-CSRF": cookie.value } : {};
}

/**
 * Leaves the worker's machine as seeded once this spec's deploy has ended: other specs count its
 * apps. A deploy that failed has removed its app already; one that succeeded is deleted.
 * Elevation takes a two-factor code, and a code already used for signing in is refused, so a
 * refusal waits for the next one.
 */
async function forgetApp(page: Page, server: ConsoleServer, domain: string): Promise<void> {
  // Nothing on screen may keep asking about the app once it is gone.
  await page.goto("about:blank");
  await expect
    .poll(
      async () => {
        const active = (await (await page.request.get("/api/jobs/active")).json()) as { jobs: { metadata?: { domain?: string } }[] };
        return active.jobs.some((job) => job.metadata?.domain === domain);
      },
      { timeout: 90_000, intervals: [1_000] },
    )
    .toBe(false);
  // A first deploy that fails undoes itself, app record included.
  if ((await page.request.get(`/api/apps/${domain}`)).status() === 404) return;
  const secret = server.totpSecret ?? "";
  let elevated = await page.request.post("/api/auth/elevate", { data: { code: totpCode(secret) }, headers: await csrf(page) });
  if (!elevated.ok()) {
    await page.waitForTimeout(30_000 - (Date.now() % 30_000) + 500);
    elevated = await page.request.post("/api/auth/elevate", { data: { code: totpCode(secret) }, headers: await csrf(page) });
  }
  expect(elevated.ok(), await elevated.text()).toBe(true);
  const deleted = await page.request.delete(`/api/apps/${domain}?remove_files=true&remove_ssl=true`, { headers: await csrf(page) });
  expect(deleted.ok(), await deleted.text()).toBe(true);
  await expect.poll(async () => (await page.request.get(`/api/apps/${domain}`)).status(), { timeout: 60_000 }).toBe(404);
}

/** Waits for every finite animation to end, so axe never measures a colour mid-transition. */
async function stillness(page: Page): Promise<void> {
  await page.waitForFunction(() =>
    document.getAnimations().every((animation) => {
      const iterations = animation.effect?.getComputedTiming().iterations;
      return animation.playState !== "running" || iterations === Infinity;
    }),
  );
}

async function inspect(page: Page, source: string): Promise<void> {
  await page.getByLabel("Repository or directory").fill(source);
  await page.getByRole("button", { name: "Inspect source" }).click();
  await expect(page.getByRole("heading", { level: 2, name: "Review" })).toBeFocused();
}

test("inspects a directory on the server, deploys it and lands on its deployment", async ({ page, consoleServer }) => {
  // The deploy runs to its end (a failed health check: nothing listens in the sandbox) before
  // the test lets go of the machine.
  test.setTimeout(180_000);
  const domain = "tienda-nueva.qrboda.com";
  await signIn(page, consoleServer, "/apps/new");
  await expect(page.getByRole("heading", { level: 1 })).toHaveText("New application");
  await settle(page);
  await expectNoA11yViolations(page, "the wizard's source step");

  const source = await wizardSource(page, "storefront");
  const inspected = page.waitForResponse((response) => response.url().endsWith("/api/apps/inspect"));
  await inspect(page, source);
  expect((await inspected).request().postDataJSON()).toEqual({ source });

  // What the real inspection found: a Next.js project on npm with a lock file.
  const found = page.getByRole("region", { name: "What WASM found" });
  await expect(found.getByText("npm ci")).toBeVisible();
  await expect(found.getByText("npm run build")).toBeVisible();
  await expect(page.getByRole("combobox", { name: "Deploy as" })).toHaveText(/Next\.js/);
  // The source is not asked for again (WCAG 3.3.7).
  await expect(page.getByLabel("Repository or directory")).toHaveCount(0);

  // Every variable of .env.example is a field; the ones without a default are marked.
  await expect(page.getByLabel(/^LOG_LEVEL/)).toHaveValue("info");
  await expect(page.getByLabel(/^NEXTAUTH_SECRET/)).toHaveAttribute("type", "password");
  await page.getByLabel("Domain", { exact: true }).fill(domain);
  await page.getByLabel(/^DATABASE_URL/).fill("postgres://storefront@localhost/storefront");
  for (const name of ["NEXTAUTH_SECRET", "STRIPE_SECRET_KEY", "SMTP_PASSWORD"]) {
    await page.getByRole("button", { name: `Generate ${name}` }).click();
    await expect(page.getByLabel(new RegExp(`^${name}`))).toHaveValue(/^[A-Za-z0-9_-]{43}$/);
  }
  await settle(page);
  await expectNoA11yViolations(page, "the wizard's review step");

  await page.getByRole("button", { name: "Continue" }).click();
  await expect(page.getByRole("heading", { level: 2, name: "Deploy" })).toBeFocused();
  await expect(page.getByText(`https://${domain}`)).toBeVisible();
  await stillness(page);
  await expectNoA11yViolations(page, "the wizard's deploy step");

  const queued = page.waitForRequest((request) => request.url().endsWith("/api/apps") && request.method() === "POST");
  await page.getByRole("button", { name: `Deploy ${domain}` }).click();
  const body = (await queued).postDataJSON() as { env_vars: Record<string, string> };
  expect(body).toMatchObject({ domain, source, app_type: "nextjs", webserver: "nginx", ssl: true, layout: "releases" });
  expect(Object.keys(body.env_vars).sort()).toEqual(
    ["DATABASE_URL", "LOG_LEVEL", "NEXTAUTH_SECRET", "NEXTAUTH_URL", "SMTP_HOST", "SMTP_PASSWORD", "STRIPE_SECRET_KEY", "UPLOADS_DIR"].sort(),
  );

  // The deploy's history row appears once the deployer starts; the wizard hands over to it.
  await expect(page).toHaveURL(new RegExp(`/apps/${domain.replace(/\./g, "\\.")}/deployments/\\d+$`), { timeout: 30_000 });
  await expect(page.getByRole("heading", { level: 1 })).toHaveText(domain);

  await forgetApp(page, consoleServer, domain);
});

test("a taken domain and the variables without a default keep the operator on Review", async ({ page, consoleServer }) => {
  await signIn(page, consoleServer, "/apps/new");
  await inspect(page, await wizardSource(page, "storefront"));
  await page.getByLabel("Domain", { exact: true }).fill("picconia.com");
  await page.getByRole("button", { name: "Continue" }).click();
  await expect(page.getByText("picconia.com is already deployed.", { exact: false })).toBeVisible();
  // Focus goes to the first field that needs attention.
  await expect(page.getByLabel("Domain", { exact: true })).toBeFocused();
  await expect(page.getByText(".env.example gives it no value, so the app expects one.")).toHaveCount(4);
  await expect(page.getByRole("heading", { level: 2, name: "Review" })).toBeVisible();
  await settle(page);
  await expectNoA11yViolations(page, "the review step with errors");

  // Going back keeps what was typed, and continuing needs no second inspection.
  await page.getByRole("button", { name: "Back" }).click();
  await expect(page.getByRole("heading", { level: 2, name: "Source" })).toBeFocused();
  await page.getByRole("button", { name: "Continue" }).click();
  await expect(page.getByLabel("Domain", { exact: true })).toHaveValue("picconia.com");
});

test("a directory that does not exist is refused on its field, in the server's words", async ({ page, consoleServer, problems }) => {
  // The API answers an unfetchable source with a 500 (SourceError has no status of its own),
  // which Chromium logs as a failed resource.
  problems.expect(/status of 500 .* \/api\/apps\/inspect$/);
  await signIn(page, consoleServer, "/apps/new");
  await page.getByLabel("Repository or directory").fill("/var/www/src/does-not-exist");
  await page.getByRole("button", { name: "Inspect source" }).click();
  await expect(page.getByText("Source path does not exist: /var/www/src/does-not-exist")).toBeVisible();
  await expect(page.getByLabel("Repository or directory")).toHaveAttribute("aria-invalid", "true");
  await settle(page);
  await expectNoA11yViolations(page, "a refused source");
});
