/**
 * Screenshots of every page for a person (or a model) to review: both themes (the light and
 * dark projects) and a 390px phone next to the desktop viewport. Tagged @screens and left
 * out of the default run; `npm run e2e:screens` writes them to e2e/__screens__/<theme>/.
 *
 * The pages still pass the CSP and console gates of every test, so a screenshot is never of
 * a page that is quietly broken.
 */

import path from "node:path";

import { expect, settle, signIn, test } from "./fixtures";

const OUT = path.join(import.meta.dirname, "__screens__");

const PHONE = { width: 390, height: 844 };

/** Every route of the console, with the seeded machine's names filled in. */
const ROUTES: readonly { name: string; path: string }[] = [
  { name: "overview", path: "/" },
  { name: "apps", path: "/apps" },
  { name: "apps-new", path: "/apps/new" },
  { name: "app-overview", path: "/apps/picconia.com" },
  { name: "app-deployments", path: "/apps/picconia.com/deployments" },
  { name: "app-deployment", path: "/apps/arennalabs.com/deployments/2" },
  { name: "app-logs", path: "/apps/picconia.com/logs" },
  { name: "app-metrics", path: "/apps/picconia.com/metrics" },
  { name: "app-environment", path: "/apps/picconia.com/environment" },
  { name: "app-domains", path: "/apps/picconia.com/domains" },
  { name: "app-diagnose", path: "/apps/clientes.arennalabs.com/diagnose" },
  { name: "app-settings", path: "/apps/picconia.com/settings" },
  { name: "databases", path: "/databases" },
  { name: "database", path: "/databases/postgresql/arennalabs_production" },
  { name: "services", path: "/services" },
  { name: "service", path: "/services/wasm-picconia-com" },
  { name: "cron", path: "/cron" },
  { name: "domains", path: "/domains" },
  { name: "backups", path: "/backups" },
  { name: "activity", path: "/activity" },
  { name: "server", path: "/server" },
  { name: "settings", path: "/settings" },
  { name: "settings-security", path: "/settings/security" },
  { name: "settings-notifications", path: "/settings/notifications" },
  { name: "settings-tokens", path: "/settings/tokens" },
  { name: "settings-about", path: "/settings/about" },
];

test.describe("screens @screens", () => {
  test("sign-in", async ({ page }, testInfo) => {
    const dir = path.join(OUT, testInfo.project.name);
    await page.goto("/login");
    await expect(page.getByRole("heading", { level: 1 })).toHaveText("Sign in");
    await settle(page);
    await page.screenshot({ path: path.join(dir, "login-desktop.png"), fullPage: true });
    await page.setViewportSize(PHONE);
    await settle(page);
    await page.screenshot({ path: path.join(dir, "login-mobile.png"), fullPage: true });
  });

  for (const route of ROUTES) {
    test(route.name, async ({ page, consoleServer }, testInfo) => {
      const dir = path.join(OUT, testInfo.project.name);
      await signIn(page, consoleServer, route.path);
      await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
      await settle(page);
      await page.screenshot({ path: path.join(dir, `${route.name}-desktop.png`), fullPage: true });

      await page.setViewportSize(PHONE);
      await settle(page);
      await page.screenshot({ path: path.join(dir, `${route.name}-mobile.png`), fullPage: true });
    });
  }
});
