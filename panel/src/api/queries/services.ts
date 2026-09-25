import { queryOptions } from "@tanstack/react-query";

import { request } from "../client";
import type { ResponseOf } from "../client";

export type ServiceList = ResponseOf<"/api/services", "get">;
export type Service = ResponseOf<"/api/services/{name}", "get">;

export const serviceKeys = {
  all: ["services"] as const,
  list: (wasmOnly: boolean) => ["services", "list", { wasmOnly }] as const,
  detail: (name: string) => ["service", name] as const,
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
