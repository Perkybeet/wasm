/**
 * Screenshots of an application's tabs, for review: both themes (the light and dark
 * projects), a 1440px desktop and a 390px phone. Tagged @screens, so left out of the default
 * run; write them with
 *
 *     WASM_SCREENS=1 npx playwright test e2e/app-tabs-screens.spec.ts
 *
 * into $WASM_TABS_SCREENS (default /tmp/console-tabs/<theme>/). A deployment is found by
 * asking the API, since seeded ids depend on what else the machine was seeded with.
 *
 * The pages still pass the CSP and console gates of every test, so a screenshot is never of
 * a page that is quietly broken.
 */

import type { Page } from "@playwright/test";
import path from "node:path";

import { expect, settle, signIn, test } from "./fixtures";

const OUT = process.env.WASM_TABS_SCREENS ?? "/tmp/console-tabs";

const DESKTOP = { width: 1440, height: 900 };
const PHONE = { width: 390, height: 844 };

interface DeploymentRow {
  id: number;
  status: string;
  error: string | null;
}

/** The id of a seeded deployment of a domain: the newest one matching `pick`. */
async function deploymentId(page: Page, domain: string, pick: (row: DeploymentRow) => boolean): Promise<number> {
  const response = await page.request.get(`/api/deployments?domain=${domain}&limit=50`);
  const body = (await response.json()) as { items: DeploymentRow[] };
  const row = body.items.find(pick);
  if (!row) throw new Error(`No seeded deployment of ${domain} matches`);
  return row.id;
}

interface Screen {
  name: string;
  path: (page: Page) => Promise<string> | string;
  /** Waits for what the screenshot is about, once the page is up. */
  ready?: (page: Page) => Promise<void>;
}

const SCREENS: readonly Screen[] = [
  { name: "deployments-releases", path: () => "/apps/tienda.cittek.es/deployments" },
  { name: "deployments-inplace", path: () => "/apps/pedidos.cittek.es/deployments" },
  {
    name: "deployment-succeeded",
    path: async (page) => `/apps/tienda.cittek.es/deployments/${String(await deploymentId(page, "tienda.cittek.es", (d) => d.status === "success"))}`,
  },
  {
    name: "deployment-build-failed",
    path: async (page) =>
      `/apps/tienda.cittek.es/deployments/${String(await deploymentId(page, "tienda.cittek.es", (d) => d.status === "failed" && (d.error ?? "").includes("Type error")))}`,
  },
  {
    name: "deployment-health-failed",
    path: async (page) =>
      `/apps/tienda.cittek.es/deployments/${String(await deploymentId(page, "tienda.cittek.es", (d) => d.status === "failed" && (d.error ?? "").includes("health check")))}`,
  },
  { name: "logs", path: () => "/apps/tienda.cittek.es/logs" },
  { name: "logs-failed-unit", path: () => "/apps/clientes.arennalabs.com/logs" },
  { name: "metrics-24h", path: () => "/apps/tienda.cittek.es/metrics" },
  { name: "metrics-7d", path: () => "/apps/tienda.cittek.es/metrics?range=7d" },
  { name: "environment", path: () => "/apps/pedidos.cittek.es/environment" },
  { name: "diagnose", path: () => "/apps/tienda.cittek.es/diagnose" },
  { name: "diagnose-down", path: () => "/apps/clientes.arennalabs.com/diagnose" },
  { name: "settings-releases", path: () => "/apps/tienda.cittek.es/settings" },
  { name: "settings-inplace", path: () => "/apps/pedidos.cittek.es/settings" },
];

test.describe("app tabs @screens", () => {
  for (const screen of SCREENS) {
    test(`app tab ${screen.name}`, async ({ page, consoleServer }, testInfo) => {
      const dir = path.join(OUT, testInfo.project.name);
      await page.setViewportSize(DESKTOP);
      await signIn(page, consoleServer, "/apps");
      await page.goto(await screen.path(page));
      await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
      await screen.ready?.(page);
      await settle(page);
      // Streams and skeletons settle a beat after the network goes idle.
      await page.waitForTimeout(600);
      await page.screenshot({ path: path.join(dir, `${screen.name}-1440.png`), fullPage: true });

      await page.setViewportSize(PHONE);
      await settle(page);
      await page.waitForTimeout(400);
      await page.screenshot({ path: path.join(dir, `${screen.name}-390.png`), fullPage: true });
    });
  }
});
