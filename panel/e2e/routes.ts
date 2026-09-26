/**
 * Every route of the console, with the seeded machine's names filled in. Shared by the
 * screenshot pass and the layout-shift gate, so a page added to one is measured by the other.
 */

import type { Page } from "@playwright/test";

/** The newest deployment of an app, as the API lists it: seeded ids are not fixed. */
function newestDeployment(domain: string) {
  return async (page: Page): Promise<string> => {
    const response = await page.request.get(`/api/deployments?domain=${domain}&limit=1`);
    const newest = ((await response.json()) as { items: { id: number }[] }).items[0];
    if (newest === undefined) throw new Error(`the seed has no deployment of ${domain}`);
    return `/apps/${domain}/deployments/${String(newest.id)}`;
  };
}

export interface ConsoleRoute {
  name: string;
  path: string | ((page: Page) => Promise<string>);
}

export const ROUTES: readonly ConsoleRoute[] = [
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

/** Resolves a route's path, asking the API when the route names seeded data by id. */
export async function routePath(page: Page, route: ConsoleRoute): Promise<string> {
  return typeof route.path === "string" ? route.path : route.path(page);
}
