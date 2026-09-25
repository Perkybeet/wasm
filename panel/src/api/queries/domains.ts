import { queryOptions } from "@tanstack/react-query";

import { request } from "../client";
import type { ResponseOf } from "../client";

export type AppDomainList = ResponseOf<"/api/apps/{domain}/domains", "get">;
export type AppDomain = AppDomainList["domains"][number];
export type AppDomainChange = ResponseOf<"/api/apps/{domain}/domains", "post">;
export type DnsCheck = ResponseOf<"/api/apps/{domain}/domains/{name}/dns", "get">;

/** What a domain is to its application: the application itself, served the same, or sent to it. */
export type DomainKind = "primary" | "alias" | "redirect";

/**
 * Keys of an application's domains. They live under the application's own prefix
 * (`["app", domain]`), so a job on the app ending, which invalidates that prefix, refreshes
 * them with everything else read about it.
 */
export const domainKeys = {
  list: (app: string) => ["app", app, "domains"] as const,
  dns: (app: string, name: string) => ["app", app, "domains", "dns", name] as const,
};

export const appDomainsQuery = (app: string) =>
  queryOptions({
    queryKey: domainKeys.list(app),
    queryFn: ({ signal }) => request("get", "/api/apps/{domain}/domains", { params: { domain: app }, signal }),
  });

/**
 * Whether a name resolves to this server. Asked again every time it is read: DNS is exactly
 * the thing an operator is waiting on to change.
 */
export const dnsCheckQuery = (app: string, name: string) =>
  queryOptions({
    queryKey: domainKeys.dns(app, name),
    queryFn: ({ signal }) =>
      request("get", "/api/apps/{domain}/domains/{name}/dns", { params: { domain: app, name }, signal }),
    staleTime: 0,
    gcTime: 60_000,
  });
