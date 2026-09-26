/**
 * Server > Health against the real backend: the verdict never stands without its reasons.
 *
 * The seeded machine's report is "Needs attention" with warnings (applications down, a
 * certificate close to expiry). A console server started with --expired-certificate has the
 * first certificate expired, which makes the report critical with an issue naming it; that
 * certificate's name links to the certificates page filtered to it.
 */

import { expect, expectNoA11yViolations, settle, signIn, startConsoleServer, test } from "./fixtures";
import type { ConsoleServer } from "./fixtures";

function healthSection(page: import("@playwright/test").Page) {
  return page.locator("section", { has: page.getByRole("heading", { level: 2, name: "Health" }) });
}

test("a report that needs attention lists its warnings beside the checks", async ({ page, consoleServer }) => {
  await signIn(page, consoleServer, "/server");
  const health = healthSection(page);
  await expect(health.getByText("Needs attention", { exact: true })).toBeVisible();
  // The report refreshes in place: the verdict and its reasons are live regions, so a change
  // while the page is open is read out.
  await expect(health.getByRole("status", { name: "Health verdict" })).toContainText("Needs attention");
  const reasons = health.getByRole("group", { name: "Reasons" });
  await expect(reasons).toHaveAttribute("aria-live", "polite");
  await expect(reasons.getByRole("listitem").first()).toBeVisible();
  // Every reason carries its level as a word, beside the report's own sentence.
  await expect(reasons.getByRole("listitem").filter({ hasText: "Warning" }).filter({ hasText: /^Warning.*App '.+' - / }).first()).toBeVisible();
  await expect(reasons.getByText(/^Critical/)).toHaveCount(0);
  const expiring = reasons.getByRole("listitem").filter({ hasText: /Certificate for picconia\.com expires in \d+ days/ });
  await expect(expiring.getByRole("link", { name: "picconia.com" })).toHaveAttribute("href", "/domains?q=picconia.com");
  await settle(page);
  await expectNoA11yViolations(page, "the health reasons");
});

const withExpiredCertificate = test.extend<object, { consoleServer: ConsoleServer }>({
  consoleServer: [
    // eslint-disable-next-line no-empty-pattern -- Playwright requires the destructuring form
    async ({}, use) => {
      const server = await startConsoleServer(["--expired-certificate"]);
      try {
        await use(server);
      } finally {
        await server.stop();
      }
    },
    { scope: "worker", timeout: 75_000 },
  ],
});

withExpiredCertificate("a critical report names the expired certificate and links to it", async ({ page, consoleServer }) => {
  await signIn(page, consoleServer, "/server");
  const health = healthSection(page);
  await expect(health.getByText("Critical", { exact: true }).first()).toBeVisible();
  const reasons = health.getByRole("group", { name: "Reasons" });
  const issue = reasons.getByRole("listitem").first();
  await expect(issue).toContainText(/^CriticalCertificate for arennalabs\.com expired \d+ days ago$/);
  await settle(page);
  await expectNoA11yViolations(page, "a critical health report");

  await issue.getByRole("link", { name: "arennalabs.com" }).click();
  await expect(page).toHaveURL(/\/domains\?q=arennalabs\.com$/);
  await expect(page.getByRole("searchbox", { name: "Filter certificates by name" })).toHaveValue("arennalabs.com");
  const certificates = page.getByRole("region", { name: "Certificates matching the filter" });
  await expect(certificates.getByRole("row").nth(1)).toContainText("arennalabs.com");
  await expect(certificates.getByRole("row").nth(1)).toContainText(/Expired/);
  // Only certificates covering that name are left.
  for (const row of await certificates.getByRole("row").all()) {
    if ((await row.getByRole("columnheader").count()) > 0) continue;
    await expect(row).toContainText("arennalabs.com");
  }
  await settle(page);
  await expectNoA11yViolations(page, "the certificates filtered from the health report");
});
