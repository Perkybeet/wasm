/**
 * Screenshots of every page for a person (or a model) to review: both themes (the light and
 * dark projects), a 1440px desktop and a 390px phone. Tagged @screens and left out of the
 * default run; `npm run e2e:screens` writes them to e2e/__screens__/<theme>/, or to
 * $WASM_SCREENS_DIR/<theme>/ when set (a review pass keeps its captures out of the tree).
 *
 * The pages still pass the CSP and console gates of every test, so a screenshot is never of
 * a page that is quietly broken.
 */

import type { Page } from "@playwright/test";
import path from "node:path";

import { expect, settle, signIn, test } from "./fixtures";

const OUT = process.env.WASM_SCREENS_DIR ?? path.join(import.meta.dirname, "__screens__");

const DESKTOP = { width: 1440, height: 900 };
const PHONE = { width: 390, height: 844 };

/** The newest deployment of an app, as the API lists it: seeded ids are not fixed. */
function newestDeployment(domain: string) {
  return async (page: Page): Promise<string> => {
    const response = await page.request.get(`/api/deployments?domain=${domain}&limit=1`);
    const newest = ((await response.json()) as { items: { id: number }[] }).items[0];
    if (newest === undefined) throw new Error(`the seed has no deployment of ${domain}`);
    return `/apps/${domain}/deployments/${String(newest.id)}`;
  };
}

/** Every route of the console, with the seeded machine's names filled in. */
const ROUTES: readonly { name: string; path: string | ((page: Page) => Promise<string>) }[] = [
  { name: "overview", path: "/" },
  { name: "apps", path: "/apps" },
  { name: "apps-new", path: "/apps/new" },
  { name: "app-overview", path: "/apps/picconia.com" },
  { name: "app-deployments", path: "/apps/picconia.com/deployments" },
  { name: "app-deployment", path: newestDeployment("tienda.cittek.es") },
  { name: "app-deployment-failed", path: newestDeployment("clientes.arennalabs.com") },
  { name: "app-logs", path: "/apps/picconia.com/logs" },
  { name: "app-metrics", path: "/apps/tienda.cittek.es/metrics" },
  { name: "app-metrics-7d", path: "/apps/tienda.cittek.es/metrics?range=7d" },
  { name: "app-environment", path: "/apps/blog.cittek.es/environment" },
  { name: "app-domains", path: "/apps/picconia.com/domains" },
  { name: "app-diagnose", path: "/apps/clientes.arennalabs.com/diagnose" },
  { name: "app-settings", path: "/apps/picconia.com/settings" },
  { name: "databases", path: "/databases" },
  { name: "database", path: "/databases/postgresql/arennalabs_production" },
  { name: "services", path: "/services" },
  { name: "service", path: "/services/wasm-picconia-com" },
  { name: "cron", path: "/cron" },
  { name: "domains", path: "/domains" },
  { name: "domains-sites", path: "/domains?tab=sites" },
  { name: "domains-site", path: "/domains/sites/qrboda.com" },
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
    await page.setViewportSize(DESKTOP);
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
      await page.setViewportSize(DESKTOP);
      if (typeof route.path === "string") {
        await signIn(page, consoleServer, route.path);
      } else {
        await signIn(page, consoleServer);
        await page.goto(await route.path(page));
      }
      await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
      await settle(page);
      await page.screenshot({ path: path.join(dir, `${route.name}-desktop.png`), fullPage: true });

      await page.setViewportSize(PHONE);
      await settle(page);
      await page.screenshot({ path: path.join(dir, `${route.name}-mobile.png`), fullPage: true });
    });
  }
});
