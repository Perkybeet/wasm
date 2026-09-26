import { queryOptions } from "@tanstack/react-query";

import { request } from "../client";
import type { ResponseOf } from "../client";

export type SiteList = ResponseOf<"/api/sites", "get">;
export type Site = ResponseOf<"/api/sites/{domain}", "get">;
/** One entry of the machine's list of sites. */
export type SiteEntry = SiteList["sites"][number];
export type SiteConfig = ResponseOf<"/api/sites/{domain}/config", "get">;
export type SiteTemplates = ResponseOf<"/api/sites/templates", "get">;

export const siteKeys = {
  all: ["sites"] as const,
  /** Every single site's own entry: the prefix of `detail` and `config`. */
  details: ["site"] as const,
  detail: (domain: string) => ["site", domain] as const,
  config: (domain: string) => ["site", domain, "config"] as const,
  templates: ["sites", "templates"] as const,
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

/** Templates a site can be created from, for the detected web server. */
export const siteTemplatesQuery = () =>
  queryOptions({
    queryKey: siteKeys.templates,
    queryFn: ({ signal }) => request("get", "/api/sites/templates", { signal }),
  });
