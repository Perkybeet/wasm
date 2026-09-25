/**
 * The fake API an application's page needs in a test: the signed-in shell, the app itself and
 * the machine-wide lists its header reads. Tests add the routes of the tab they exercise.
 */

import { json, signedInRoutes } from "../../test/fakes";
import type { RouteHandler } from "../../test/fakes";

export const TAB_DOMAIN = "shop.example.com";

export const TAB_APP = {
  name: TAB_DOMAIN,
  domain: TAB_DOMAIN,
  status: "running",
  active: true,
  enabled: true,
  pid: 41234,
  uptime: "Fri 2026-09-25 13:06:35 UTC",
  port: 3000,
  app_type: "nextjs",
  path: `/var/www/apps/shop-example-com`,
  layout: "inplace",
  memory_max_mb: null,
  cpu_quota_percent: null,
  tasks_max: null,
};

export function appRoutes(app: Partial<typeof TAB_APP> | Record<string, unknown> = {}, extra: Record<string, RouteHandler> = {}): Record<string, RouteHandler> {
  const merged = { ...TAB_APP, ...app };
  return {
    ...signedInRoutes(),
    [`GET /api/apps/${TAB_DOMAIN}`]: () => json(200, merged),
    "GET /api/certs": () => json(200, { certificates: [], total: 0 }),
    "GET /api/sites": () => json(200, { sites: [], total: 0, webserver: "nginx" }),
    "GET /api/jobs/active": () => json(200, { jobs: [], total: 0, active: 0 }),
    ...extra,
  };
}
