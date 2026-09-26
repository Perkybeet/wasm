/**
 * Domains and certificates against the real backend: an application's Domains tab adding a
 * name after asking DNS where it points, the certificate job extending (or failing to extend)
 * the certificate with certbot's own words, and a site's configuration editor whose save is
 * refused when nginx's test fails. The console server models DNS, certbot and nginx's syntax
 * check over the sandbox (seed_domains_and_sources). Runs in both themes, with the CSP and
 * console gates of the `problems` fixture and axe on every state.
 */

import type { Page } from "@playwright/test";

import { confirmItsYou, expect, expectNoA11yViolations, settle, signIn, stillness, test } from "./fixtures";

/** Closes every toast, which sit over the page and are not what axe is judging. */
async function dismissToasts(page: Page): Promise<void> {
  await page.locator('.toast button[aria-label="Dismiss notification"]').evaluateAll((buttons) => {
    for (const button of buttons) (button as HTMLButtonElement).click();
  });
}

function row(page: Page, table: string, name: string) {
  return page.getByRole("region", { name: table }).getByRole("row").filter({ has: page.getByText(name, { exact: true }) });
}

test("an alias is added after DNS says it points here, and the certificate is extended to it", async ({ page, consoleServer }) => {
  const app = "qrboda.com";
  const alias = `blog.${app}`;
  await signIn(page, consoleServer, `/apps/${app}/domains`);
  const primary = page.getByRole("region", { name: `Domains of ${app}` }).getByRole("row").filter({ hasText: "Primary" });
  await expect(primary).toContainText(app);
  await expect(primary).toContainText("Covered");
  await settle(page);
  await expectNoA11yViolations(page, "an application's Domains tab");

  await page.getByRole("button", { name: "Add domain" }).click();
  const dialog = page.getByRole("dialog", { name: `Add a domain to ${app}` });
  await dialog.getByLabel("Domain").fill(alias);
  // The first press asks DNS; nothing is added yet.
  const lookup = page.waitForResponse((response) => response.url().endsWith(`/api/apps/${app}/domains/${alias}/dns`));
  await dialog.getByRole("button", { name: "Check DNS" }).click();
  expect((await lookup).status()).toBe(200);
  await expect(dialog.getByText(`${alias} points here`)).toBeVisible();
  await expect(dialog.getByText("203.0.113.10").first()).toBeVisible();
  await stillness(page);
  await expectNoA11yViolations(page, "the DNS verdict");

  const added = page.waitForRequest((request) => request.url().endsWith(`/api/apps/${app}/domains`) && request.method() === "POST");
  await dialog.getByRole("button", { name: "Add domain" }).click();
  expect((await added).postDataJSON()).toEqual({ domain: alias, kind: "alias" });
  await expect(dialog).toBeHidden();

  // The certificate job runs certbot for every name; the banner follows it to its end.
  await expect(page.getByText(`The certificate covers every domain of ${app}`)).toBeVisible({ timeout: 20_000 });
  const aliasRow = page.getByRole("region", { name: `Domains of ${app}` }).getByRole("row").filter({ hasText: alias });
  await expect(aliasRow).toContainText("Alias");
  await expect(aliasRow).toContainText("Covered");
  await expect(page.getByRole("list", { name: "Names on the certificate" })).toContainText(alias);
  await dismissToasts(page);
  await stillness(page);
  await expectNoA11yViolations(page, "a domain added with its certificate");
});

test("a name that does not point here is added, and the failed order is shown in certbot's words", async ({ page, consoleServer, problems }) => {
  const app = "cittek.es";
  const name = `new.${app}`;
  // Removing it first asks for elevation (403), which Chromium logs as a failed resource.
  problems.expect(/status of 403 .* \/api\/apps\/cittek\.es\/domains\/new\.cittek\.es$/);
  await signIn(page, consoleServer, `/apps/${app}/domains`);
  await page.getByRole("button", { name: "Add domain" }).click();
  const dialog = page.getByRole("dialog", { name: `Add a domain to ${app}` });
  await dialog.getByLabel("Domain").fill(name);
  await dialog.getByRole("radio", { name: /Redirect/ }).check();
  await dialog.getByRole("button", { name: "Check DNS" }).click();
  await expect(dialog.getByText(`${name} has no DNS record yet`)).toBeVisible();
  await expect(dialog.getByText("Nothing: no A or AAAA record")).toBeVisible();
  await dialog.getByRole("button", { name: "Add anyway" }).click();
  await expect(dialog).toBeHidden();

  const failure = page.getByText("The certificate was not extended");
  await expect(failure).toBeVisible({ timeout: 20_000 });
  await expect(page.locator("pre").filter({ hasText: `DNS problem: NXDOMAIN looking up A for ${name}` })).toBeVisible();
  const redirect = page.getByRole("region", { name: `Domains of ${app}` }).getByRole("row").filter({ hasText: name });
  await expect(redirect).toContainText("Redirect");
  await expect(redirect).toContainText("Not covered");
  await dismissToasts(page);
  await stillness(page);
  await expectNoA11yViolations(page, "a failed certificate order");

  // Removing it needs the operator to type the name and confirm it's them.
  await redirect.getByRole("button", { name: `Actions for ${name}` }).click();
  await page.getByRole("menuitem", { name: "Remove" }).click();
  const confirm = page.getByRole("alertdialog", { name: `Remove ${name}` });
  await confirm.getByRole("textbox").fill(name);
  await confirm.getByRole("button", { name: "Remove domain" }).click();
  await confirmItsYou(page, consoleServer);
  await expect(confirm).toBeHidden();
  await expect(redirect).toHaveCount(0);
});

test("a configuration nginx rejects is not saved, and nginx's output says why", async ({ page, consoleServer, problems }) => {
  const site = "arennalabs.com";
  // Chromium logs both refusals as failed resources: the save first asks for elevation (403),
  // then nginx's test refuses the text (400). Both are the behaviour under test.
  problems.expect(/status of 403 .* \/api\/sites\/arennalabs\.com\/config$/);
  problems.expect(/status of 400 .* \/api\/sites\/arennalabs\.com\/config$/);
  await signIn(page, consoleServer, `/domains/sites/${site}`);
  const editor = page.getByRole("textbox", { name: `Configuration of ${site}` });
  await expect(editor).toHaveValue(/server_name arennalabs\.com;/);
  const original = await editor.inputValue();
  await settle(page);
  await expectNoA11yViolations(page, "a site's configuration editor");

  // "Test" asks the same question saving would, without saving or asking to confirm it's you:
  // the untouched file passes.
  const passed = page.waitForResponse((response) => response.url().endsWith(`/api/sites/${site}/config/test`));
  await page.getByRole("button", { name: "Test", exact: true }).click();
  expect((await passed).status()).toBe(200);
  await expect(page.getByText("This passed nginx's configuration test. Nothing was saved yet.")).toBeVisible();
  await expect(page.locator("pre").filter({ hasText: "syntax is ok" })).toBeVisible();
  await stillness(page);
  await expectNoA11yViolations(page, "a passed configuration test");

  // A lost semicolon at the end of the file.
  await editor.fill(`${original}    listen 8080\n`);
  await expect(page.getByText("Unsaved changes")).toBeVisible();

  const tested = page.waitForResponse((response) => response.url().endsWith(`/api/sites/${site}/config/test`));
  await page.getByRole("button", { name: "Test", exact: true }).click();
  expect((await tested).status()).toBe(200);
  await expect(page.getByText("Nothing was saved: the configuration test failed.")).toBeVisible();
  const testOutput = page.locator("pre").filter({ hasText: 'nginx: [emerg] unexpected end of file, expecting ";" or "}"' });
  await expect(testOutput).toContainText(/test failed/);
  await expect(editor).toHaveAttribute("aria-invalid", "true");
  await stillness(page);
  await expectNoA11yViolations(page, "a failed configuration test");

  // Testing never touches the file, saving or not.
  const stillOriginal = (await (await page.request.get(`/api/sites/${site}/config`)).json()) as { config: string };
  expect(stillOriginal.config).toBe(original);

  const saved = page.waitForResponse(
    (response) => response.url().endsWith(`/api/sites/${site}/config`) && response.request().method() === "PUT" && response.status() !== 403,
  );
  await page.getByRole("button", { name: "Test and save" }).click();
  await confirmItsYou(page, consoleServer);
  const refused = await saved;
  expect(refused.status()).toBe(400);

  await expect(page.getByText("Nothing was saved: the configuration test failed.")).toBeVisible();
  const output = page.locator("pre").filter({ hasText: 'nginx: [emerg] unexpected end of file, expecting ";" or "}"' });
  await expect(output).toContainText(/test failed/);
  await expect(editor).toHaveAttribute("aria-invalid", "true");
  await page.getByRole("button", { name: /^Go to line \d+$/ }).click();
  await expect(editor).toBeFocused();
  await stillness(page);
  await expectNoA11yViolations(page, "a refused configuration");

  // The file on disk is the one it was.
  const onDisk = (await (await page.request.get(`/api/sites/${site}/config`)).json()) as { config: string };
  expect(onDisk.config).toBe(original);
  await page.getByRole("button", { name: "Discard changes" }).click();
  await expect(editor).toHaveValue(original);
});

test("the certificates and sites tabs: the most urgent certificate first, renewing it, and the tab in the URL", async ({ page, consoleServer }) => {
  await signIn(page, consoleServer, "/domains");
  await expect(page.getByRole("heading", { level: 1 })).toHaveText("Domains and certificates");
  const certificates = page.getByRole("region", { name: "Certificates" });
  // arennalabs.com expires in 12 days: first, and flagged.
  await expect(certificates.getByRole("row").nth(1)).toContainText("arennalabs.com");
  await expect(certificates.getByRole("row").nth(1)).toContainText(/Expires in 1[12] days/);
  await expect(certificates.getByRole("row").nth(1)).toContainText(/Expires in \d+ days/);
  // CertInfo.issuer, in words rather than the raw "C = US, O = Let's Encrypt, CN = R11".
  await expect(certificates.getByRole("row").nth(1)).toContainText("Let's Encrypt R11");
  await settle(page);
  await expectNoA11yViolations(page, "the certificates tab");

  const renewal = row(page, "Certificates", "bodas.arennalabs.com");
  await renewal.getByRole("button", { name: "Actions for bodas.arennalabs.com" }).click();
  await page.getByRole("menuitem", { name: "Renew now" }).click();
  // The job's own banner on the page (the live region may say the same words).
  await expect(page.getByRole("main").getByText("Renewed bodas.arennalabs.com")).toBeVisible({ timeout: 20_000 });
  await expect(renewal).toContainText(/Valid for (89|90) days/);

  await page.getByRole("button", { name: "Issue certificate" }).click();
  const issue = page.getByRole("dialog", { name: "Issue a certificate" });
  await issue.getByRole("button", { name: "Issue certificate" }).click();
  await expect(issue.getByText("Enter a domain, such as app.example.com.")).toBeVisible();
  await stillness(page);
  await expectNoA11yViolations(page, "the issue dialog");
  await page.keyboard.press("Escape");

  await page.getByRole("tab", { name: /Sites/ }).click();
  await expect(page).toHaveURL(/\/domains\?tab=sites$/);
  await expect(page.getByRole("region", { name: "Sites" })).toBeVisible();
  await expect(row(page, "Sites", "convertidordepdf.com")).toContainText("Disabled");

  // SiteInfo.server_names: qrboda.com's site was seeded to answer on more than its own name.
  // Another test may since have added an alias to it too, so the expectation comes from the
  // API rather than a fixed list.
  const qrboda = (await (await page.request.get("/api/sites/qrboda.com")).json()) as { server_names: string[] };
  expect(qrboda.server_names.length).toBeGreaterThan(1);
  const qrbodaRow = row(page, "Sites", "qrboda.com");
  const shownNames = qrboda.server_names.slice(0, 3).join(", ");
  await expect(qrbodaRow).toContainText(shownNames);
  const hiddenCount = qrboda.server_names.length - 3;
  if (hiddenCount > 0) await expect(qrbodaRow).toContainText(`+${String(hiddenCount)} more`);
  await dismissToasts(page);
  await settle(page);
  await expectNoA11yViolations(page, "the sites tab");
});

test("a site's own page lists every name it serves, and a template is offered from the API", async ({ page, consoleServer }) => {
  await signIn(page, consoleServer, "/domains?tab=sites");
  const sites = page.getByRole("region", { name: "Sites" });
  await expect(sites).toBeVisible();

  // GET /api/sites/templates, not a list the dialog invents: every template it names is one
  // this call actually returned. The button sits in the toolbar above the table, not inside
  // the table's own scrollable region.
  const templatesResponse = page.waitForResponse((response) => response.url().endsWith("/api/sites/templates"));
  await page.getByRole("button", { name: "Create site" }).click();
  const createDialog = page.getByRole("dialog", { name: "Create a site" });
  await expect(createDialog).toBeVisible();
  const { templates: offeredTemplates } = (await (await templatesResponse).json()) as { templates: string[] };
  expect(offeredTemplates.length).toBeGreaterThan(0);
  for (const name of offeredTemplates) {
    // The template's own name is the radio's value; two labels can share a word ("Advanced
    // proxy", "Reverse proxy"), so the value is what is unambiguous.
    await expect(createDialog.locator(`input[type="radio"][value="${name}"]`)).toBeAttached();
  }
  await stillness(page);
  await expectNoA11yViolations(page, "the create site dialog");
  await page.keyboard.press("Escape");
  await expect(createDialog).toBeHidden();

  const qrboda = (await (await page.request.get("/api/sites/qrboda.com")).json()) as { server_names: string[] };
  await page.goto("/domains/sites/qrboda.com");
  await expect(page.getByRole("heading", { level: 1 })).toHaveText("qrboda.com");
  await expect(page.getByText(qrboda.server_names.join(", "))).toBeVisible();
  await settle(page);
  await expectNoA11yViolations(page, "a site's own page with the names it serves");
});

test("deleting a certificate needs the operator to confirm it's them", async ({ page, consoleServer, problems }) => {
  await signIn(page, consoleServer, "/domains");

  // Other tests here rely on arennalabs.com, cittek.es, qrboda.com and bodas.arennalabs.com
  // keeping their certificates; picked from the API so whichever other seeded certificate
  // exists works, rather than assuming one by name. Revoking is not exercised on top of this:
  // CertManager.revoke() asks certbot for --delete-after-revoke, so a revoked certificate is
  // already gone - the two are not independently sequenceable on one lineage, and this is the
  // one every "Revoke" ends as anyway.
  const reserved = new Set(["arennalabs.com", "cittek.es", "qrboda.com", "bodas.arennalabs.com"]);
  const certs = (await (await page.request.get("/api/certs")).json()) as { certificates: { domain: string }[] };
  const target = certs.certificates.find((cert) => !reserved.has(cert.domain));
  if (!target) throw new Error("No certificate free of other tests' use was seeded to delete.");
  const domain = target.domain;
  const escaped = domain.replace(/\./g, "\\.");
  problems.expect(new RegExp(`status of 403 .* /api/certs/${escaped}$`));
  const certRow = row(page, "Certificates", domain);

  await certRow.getByRole("button", { name: `Actions for ${domain}` }).click();
  await page.getByRole("menuitem", { name: "Delete" }).click();
  const confirm = page.getByRole("alertdialog", { name: `Delete ${domain}` });
  await confirm.getByRole("textbox").fill(domain);
  const deleted = page.waitForResponse(
    (response) => response.url().endsWith(`/api/certs/${domain}`) && response.request().method() === "DELETE" && response.status() !== 403,
  );
  await confirm.getByRole("button", { name: "Delete certificate" }).click();
  await confirmItsYou(page, consoleServer);
  expect((await deleted).status()).toBe(200);
  await expect(confirm).toBeHidden();
  await expect(page.getByText(`Deleted ${domain}`)).toBeVisible();
  await stillness(page);
  await expectNoA11yViolations(page, "a deleted certificate");
});

test("on a phone the domains pages keep to the screen", async ({ page, consoleServer }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await signIn(page, consoleServer, "/domains?tab=sites");
  await expect(page.getByRole("region", { name: "Sites" })).toBeVisible();
  await settle(page);
  for (const path of ["/domains?tab=sites", "/domains", "/apps/picconia.com/domains", "/domains/sites/picconia.com"]) {
    await page.goto(path);
    await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
    await settle(page);
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
    expect(overflow, path).toBeLessThanOrEqual(0);
  }
  await expectNoA11yViolations(page, "a site on a phone");
});
