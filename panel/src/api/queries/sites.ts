import { queryOptions } from "@tanstack/react-query";

import { request } from "../client";
import type { ResponseOf } from "../client";

export type SiteList = ResponseOf<"/api/sites", "get">;
export type Site = ResponseOf<"/api/sites/{domain}", "get">;

export const siteKeys = {
  all: ["sites"] as const,
  detail: (domain: string) => ["site", domain] as const,
  config: (domain: string) => ["site", domain, "config"] as const,
};

export const sitesQuery = () =>
  queryOptions({
    queryKey: siteKeys.all,
    queryFn: ({ signal }) => request("get", "/api/sites", { signal }),
  });

export const siteQuery = (domain: string) =>
  queryOptions({
    queryKey: siteKeys.detail(domain),
    queryFn: ({ signal }) => request("get", "/api/sites/{domain}", { params: { domain }, signal }),
  });

export const siteConfigQuery = (domain: string) =>
  queryOptions({
    queryKey: siteKeys.config(domain),
    queryFn: ({ signal }) => request("get", "/api/sites/{domain}/config", { params: { domain }, signal }),
  });
