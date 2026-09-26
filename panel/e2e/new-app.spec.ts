/**
 * The new-app wizard against the real backend: a seeded directory on the server is inspected
 * by the real `POST /api/apps/inspect`, the review proposes what it found, and the deploy is
 * queued with `POST /api/apps` and handed over to its deployment page. Runs in both themes,
 * with the CSP and console gates of the `problems` fixture and axe on every step.
 */

import type { Page } from "@playwright/test";

import { confirmItsYou, expect, expectNoA11yViolations, settle, signIn, stillness, test } from "./fixtures";
import type { ConsoleServer } from "./fixtures";
import { inspectSource, wizardSource } from "./wizard-sources";

/** The CSRF header every write through `page.request` carries, mirrored from its cookie. */
async function csrf(page: Page): Promise<Record<string, string>> {
  const cookie = (await page.context().cookies()).find((entry) => entry.name === "wasm_csrf");
  return cookie ? { "X-WASM-CSRF": cookie.value } : {};
}

/**
 * Leaves the worker's machine as seeded once this spec's deploy has ended: other specs count its
 * apps. A deploy that failed has removed its app already; one that succeeded is deleted.
 * Deleting it needs sudo mode, confirmed here with an unspent second factor.
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
  const elevated = await page.request.post("/api/auth/elevate", { data: { code: server.secondFactor() }, headers: await csrf(page) });
  expect(elevated.ok(), await elevated.text()).toBe(true);
  const deleted = await page.request.delete(`/api/apps/${domain}?remove_files=true&remove_ssl=true`, { headers: await csrf(page) });
  expect(deleted.ok(), await deleted.text()).toBe(true);
  await expect.poll(async () => (await page.request.get(`/api/apps/${domain}`)).status(), { timeout: 60_000 }).toBe(404);
}


test("inspects a directory on the server, deploys it and lands on its deployment", async ({ page, consoleServer, problems }) => {
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
  await inspectSource(page, consoleServer, problems, source);
  expect((await inspected).request().postDataJSON()).toEqual({ source });

  // What the real inspection found: a Next.js project on npm with a lock file, deployable here.
  const found = page.getByRole("region", { name: "What WASM found" });
  await expect(found.getByText(/^WASM can deploy this as Next\.js/)).toBeVisible();
  await expect(found.getByText("npm ci")).toBeVisible();
  await expect(found.getByText("npm run build")).toBeVisible();
  await expect(page.getByRole("combobox", { name: "Deploy as" })).toHaveText(/Next\.js/);
  // The source is not asked for again (WCAG 3.3.7).
  await expect(page.getByLabel("Repository or directory")).toHaveCount(0);

  // Every variable of .env.example is a field; the ones without a default are marked.
  await expect(page.getByLabel(/^LOG_LEVEL/)).toHaveValue("info");
  await expect(page.getByLabel(/^NEXTAUTH_SECRET/)).toHaveAttribute("type", "password");
  await page.getByLabel("Domain", { exact: true }).fill(domain);
  // Checked as it is typed: qrboda.com is a seeded zone, so this name resolves here.
  await expect(page.getByText(`${domain} points here`)).toBeVisible();
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

test("a taken domain and the variables without a default keep the operator on Review", async ({ page, consoleServer, problems }) => {
  await signIn(page, consoleServer, "/apps/new");
  await inspectSource(page, consoleServer, problems, await wizardSource(page, "storefront"));
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
  // SourceError now answers 400, in the API's error contract, not the unqualified 500 an
  // earlier build gave it - Chromium still logs the failed resource either way.
  // Reading a directory on the server needs sudo mode first, which is a 403 of its own.
  problems.expect(/status of 40[03] .* \/api\/apps\/inspect$/);
  await signIn(page, consoleServer, "/apps/new");
  await page.getByLabel("Repository or directory").fill("/var/www/src/does-not-exist");
  const inspected = page.waitForResponse((response) => response.url().endsWith("/api/apps/inspect") && response.status() !== 403);
  await page.getByRole("button", { name: "Inspect source" }).click();
  const refusal = page.getByText("Source path does not exist: /var/www/src/does-not-exist");
  const confirm = page.getByRole("dialog", { name: "Confirm it's you" });
  await expect(confirm.or(refusal)).toBeVisible();
  if (await confirm.isVisible()) await confirmItsYou(page, consoleServer);
  expect((await inspected).status()).toBe(400);
  await expect(refusal).toBeVisible();
  await expect(page.getByLabel("Repository or directory")).toHaveAttribute("aria-invalid", "true");
  await settle(page);
  await expectNoA11yViolations(page, "a refused source");
});

test("the type select lists every type the deployer registry knows, not a hand-kept copy", async ({ page, consoleServer, problems }) => {
  await signIn(page, consoleServer, "/apps/new");
  await inspectSource(page, consoleServer, problems, await wizardSource(page, "storefront"));
  await page.getByRole("combobox", { name: "Deploy as" }).click();
  // Detected first, the closest match on top.
  const options = page.getByRole("listbox").getByRole("option");
  await expect(options.first()).toHaveText(/Next\.js/);
  // auto is real, registered type this build did not have a hand-kept label for before.
  await expect(page.getByRole("option", { name: "Auto-detect" })).toBeVisible();
  await expect(page.getByRole("option", { name: "Docker Compose" })).toBeVisible();
  await page.keyboard.press("Escape");
});

test("a domain that resolves elsewhere warns instead of blocking the deploy", async ({ page, consoleServer, problems }) => {
  await signIn(page, consoleServer, "/apps/new");
  await inspectSource(page, consoleServer, problems, await wizardSource(page, "storefront"));
  // old.qrboda.com is modelled as pointing at another server.
  await page.getByLabel("Domain", { exact: true }).fill("old.qrboda.com");
  await expect(page.getByText("old.qrboda.com points somewhere else")).toBeVisible();
  await page.getByLabel(/^DATABASE_URL/).fill("x");
  for (const name of ["NEXTAUTH_SECRET", "STRIPE_SECRET_KEY", "SMTP_PASSWORD"]) {
    await page.getByRole("button", { name: `Generate ${name}` }).click();
  }
  await settle(page);
  await expectNoA11yViolations(page, "a domain that resolves elsewhere");
  await page.getByRole("button", { name: "Continue" }).click();
  await expect(page.getByRole("heading", { level: 2, name: "Deploy" })).toBeFocused();
});

test("also serving www and resource limits reach the deploy request", async ({ page, consoleServer, problems }) => {
  test.setTimeout(120_000);
  // A bare two-label domain outside any seeded zone: www only ever means something for one of
  // these, and this one deliberately has no DNS record, which is not a reason to refuse it.
  const domain = "wasm-e2e-wizard.example";
  await signIn(page, consoleServer, "/apps/new");
  await inspectSource(page, consoleServer, problems, await wizardSource(page, "landing"));
  await page.getByLabel("Domain", { exact: true }).fill(domain);
  await expect(page.getByText(`${domain} has no DNS record yet`)).toBeVisible();
  await page.getByRole("checkbox", { name: "Also serve www" }).check();
  // The static source has no port and no environment; HTTPS is turned off so the deploy does
  // not also wait on a certificate order that a domain with no DNS record cannot complete.
  await page.getByRole("checkbox", { name: "Serve it over HTTPS" }).uncheck();
  await page.getByText("Resource limits").click();
  const limits = page.getByText("Resource limits").locator("xpath=ancestor::details");
  await limits.getByLabel("Memory").fill("256");
  await limits.getByLabel("Tasks").fill("64");
  await settle(page);
  await expectNoA11yViolations(page, "www and resource limits on the review step");

  await page.getByRole("button", { name: "Continue" }).click();
  await expect(page.getByRole("heading", { level: 2, name: "Deploy" })).toBeFocused();

  const queued = page.waitForRequest((request) => request.url().endsWith("/api/apps") && request.method() === "POST");
  await page.getByRole("button", { name: `Deploy ${domain}` }).click();
  const body = (await queued).postDataJSON() as Record<string, unknown>;
  expect(body).toMatchObject({ domain, ssl: false, include_www: true, memory_max_mb: 256, cpu_quota_percent: null, tasks_max: 64 });

  await forgetApp(page, consoleServer, domain);
});


test("a source WASM cannot deploy as it is gets the inspection's verdict and the file to add", async ({ page, consoleServer, problems }) => {
  // Chromium logs the inspection's refusal as a failed resource.
  problems.expect(/status of 400 .* \/api\/apps\/inspect$/);
  problems.expect(/status of 403 .* \/api\/apps\/inspect$/);
  await signIn(page, consoleServer, "/apps/new");
  const source = await wizardSource(page, "container-api");
  await page.getByLabel("Repository or directory").fill(source);
  await page.getByRole("button", { name: "Inspect source" }).click();
  const confirm = page.getByRole("dialog", { name: "Confirm it's you" });
  const verdict = page.getByRole("alert").filter({ hasText: `WASM cannot deploy ${source} as it is` });
  await expect(confirm.or(verdict)).toBeVisible();
  if (await confirm.isVisible()) await confirmItsYou(page, consoleServer);

  await expect(verdict.getByText("The repository has a Dockerfile but no Compose file.")).toBeVisible();
  // The compose file to commit, as the file it is: indentation included.
  await expect(verdict.locator("pre").filter({ hasText: /^services:\n {2}app:\n {4}build: \./ })).toBeVisible();
  await expect(page.getByLabel("Repository or directory")).not.toHaveAttribute("aria-invalid", "true");
  await settle(page);
  await expectNoA11yViolations(page, "an inspection's verdict");

  await verdict.getByRole("button", { name: "Choose the type yourself" }).click();
  await expect(page.getByRole("heading", { level: 2, name: "Review" })).toBeFocused();
});
