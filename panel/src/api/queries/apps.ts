import { queryOptions } from "@tanstack/react-query";

import { request } from "../client";
import type { ResponseOf } from "../client";

export type AppList = ResponseOf<"/api/apps", "get">;
export type App = ResponseOf<"/api/apps/{domain}", "get">;

/**
 * Keys of the application queries. The list and each app are separate entries so that a
 * state change of one app (the `app` server event) patches its detail and invalidates the
 * list without refetching every other app.
 */
export const appKeys = {
  list: ["apps"] as const,
  detail: (domain: string) => ["app", domain] as const,
  env: (domain: string, unmask: boolean) => ["app", domain, "env", { unmask }] as const,
  logs: (domain: string, lines: number) => ["app", domain, "logs", { lines }] as const,
};

export const appsQuery = () =>
  queryOptions({
    queryKey: appKeys.list,
    queryFn: ({ signal }) => request("get", "/api/apps", { signal }),
  });

export const appQuery = (domain: string) =>
  queryOptions({
    queryKey: appKeys.detail(domain),
    queryFn: ({ signal }) => request("get", "/api/apps/{domain}", { params: { domain }, signal }),
  });

export const appEnvQuery = (domain: string, unmask = false) =>
  queryOptions({
    queryKey: appKeys.env(domain, unmask),
    queryFn: ({ signal }) =>
      request("get", "/api/apps/{domain}/env", { params: { domain }, query: { unmask }, signal }),
  });

export const appLogsQuery = (domain: string, lines = 200) =>
  queryOptions({
    queryKey: appKeys.logs(domain, lines),
    queryFn: ({ signal }) =>
      request("get", "/api/apps/{domain}/logs", { params: { domain }, query: { lines }, signal }),
  });

const APP_STATES = new Set(["running", "deploying", "failed", "stopped", "static"]);

/** An app's `status` as one of the console's states; anything unrecognised is "unknown". */
export function appState(status: string | null | undefined): "running" | "deploying" | "failed" | "stopped" | "static" | "unknown" {
  return status && APP_STATES.has(status) ? (status as "running" | "deploying" | "failed" | "stopped" | "static") : "unknown";
}
