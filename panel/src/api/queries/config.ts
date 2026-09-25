import { queryOptions } from "@tanstack/react-query";

import { request } from "../client";
import type { ResponseOf } from "../client";

export type ConsoleConfig = ResponseOf<"/api/config", "get">;

export const configKeys = {
  all: ["config"] as const,
  defaults: ["config", "defaults"] as const,
};

export const configQuery = () =>
  queryOptions({
    queryKey: configKeys.all,
    queryFn: ({ signal }) => request("get", "/api/config", { signal }),
  });

export const configDefaultsQuery = () =>
  queryOptions({
    queryKey: configKeys.defaults,
    queryFn: ({ signal }) => request("get", "/api/config/defaults", { signal }),
    staleTime: Number.POSITIVE_INFINITY,
  });
