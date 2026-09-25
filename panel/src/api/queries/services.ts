import { queryOptions } from "@tanstack/react-query";

import { request } from "../client";
import type { ResponseOf } from "../client";

export type ServiceList = ResponseOf<"/api/services", "get">;
export type Service = ResponseOf<"/api/services/{name}", "get">;
export type ServiceLogs = ResponseOf<"/api/services/{name}/logs", "get">;
export type ServiceConfig = ResponseOf<"/api/services/{name}/config", "get">;

export const serviceKeys = {
  all: ["services"] as const,
  list: (wasmOnly: boolean) => ["services", "list", { wasmOnly }] as const,
  detail: (name: string) => ["service", name] as const,
  logs: (name: string, lines: number) => ["service", name, "logs", { lines }] as const,
  config: (name: string) => ["service", name, "config"] as const,
};

export const servicesQuery = (wasmOnly = false) =>
  queryOptions({
    queryKey: serviceKeys.list(wasmOnly),
    queryFn: ({ signal }) => request("get", "/api/services", { query: { wasm_only: wasmOnly }, signal }),
  });

export const serviceQuery = (name: string) =>
  queryOptions({
    queryKey: serviceKeys.detail(name),
    queryFn: ({ signal }) => request("get", "/api/services/{name}", { params: { name }, signal }),
  });

/** A backlog read of the unit's journal. The Logs section follows `/ws/logs/{name}` live; this is its initial paint and its "download more" fallback. */
export const serviceLogsQuery = (name: string, lines = 200) =>
  queryOptions({
    queryKey: serviceKeys.logs(name, lines),
    queryFn: ({ signal }) => request("get", "/api/services/{name}/logs", { params: { name }, query: { lines }, signal }),
  });

/**
 * The unit file content. Reading it needs an admin credential server-side (it can carry
 * inlined secrets in simple-mode units), so this is only fetched once the editor is opened,
 * never alongside the rest of the detail page.
 */
export const serviceConfigQuery = (name: string) =>
  queryOptions({
    queryKey: serviceKeys.config(name),
    queryFn: ({ signal }) => request("get", "/api/services/{name}/config", { params: { name }, signal }),
  });
