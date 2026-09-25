/**
 * Reads of one application that no shared query module offers yet, and lookups of the app in
 * the machine-wide lists. The certificate and the site are found in `GET /api/certs` and
 * `GET /api/sites` rather than asked for by domain: an app without either is a normal state,
 * and the per-domain endpoints answer it with a 404 the browser logs as an error.
 *
 * The keyed reads live under the app's own key (`["app", domain, ...]`), so a job on the app
 * finishing, which invalidates that prefix, refreshes them with everything else about it.
 */

import { queryOptions } from "@tanstack/react-query";

import { isApiError, request } from "../../api/client";
import type { ResponseOf } from "../../api/client";
import { appKeys } from "../../api/queries/apps";
import type { CertList } from "../../api/queries/certs";
import type { SiteList } from "../../api/queries/sites";

export type Releases = ResponseOf<"/api/apps/{domain}/releases", "get">;
export type Release = Releases["items"][number];
export type RollbackPoints = ResponseOf<"/api/apps/{domain}/rollback-points", "get">;
export type RollbackPoint = RollbackPoints["items"][number];
export type WebhookDeliveries = ResponseOf<"/api/apps/{domain}/webhook/deliveries", "get">;

export const appDetailKeys = {
  releases: (domain: string) => [...appKeys.detail(domain), "releases"] as const,
  rollbackPoints: (domain: string) => [...appKeys.detail(domain), "rollback-points"] as const,
  webhookDeliveries: (domain: string) => [...appKeys.detail(domain), "webhook-deliveries"] as const,
};

/**
 * The app's releases. An app still deployed in place has none, and the API says so with a
 * 409; that answer is data here (`null`), not a failure, so the page can offer backups instead.
 */
export const releasesQuery = (domain: string) =>
  queryOptions({
    queryKey: appDetailKeys.releases(domain),
    queryFn: async ({ signal }): Promise<Releases | null> => {
      try {
        return await request("get", "/api/apps/{domain}/releases", { params: { domain }, signal });
      } catch (error: unknown) {
        if (isApiError(error) && error.status === 409) return null;
        throw error;
      }
    },
  });

/** Backups the app can be rolled back to, newest first. */
export const rollbackPointsQuery = (domain: string) =>
  queryOptions({
    queryKey: appDetailKeys.rollbackPoints(domain),
    queryFn: ({ signal }) => request("get", "/api/apps/{domain}/rollback-points", { params: { domain }, signal }),
  });

/** Deploys the app's webhook triggered, newest first. */
export const webhookDeliveriesQuery = (domain: string) =>
  queryOptions({
    queryKey: appDetailKeys.webhookDeliveries(domain),
    queryFn: ({ signal }) => request("get", "/api/apps/{domain}/webhook/deliveries", { params: { domain }, signal }),
  });

/** The certificate covering a domain, read from the machine's list of certificates. */
export function findCertificate(list: CertList | undefined, domain: string): CertList["certificates"][number] | null | undefined {
  if (list === undefined) return undefined;
  return list.certificates.find((cert) => cert.domain === domain || cert.domains.includes(domain)) ?? null;
}

/** The web server site serving a domain, read from the machine's list of sites. */
export function findSite(list: SiteList | undefined, domain: string): SiteList["sites"][number] | null | undefined {
  if (list === undefined) return undefined;
  return list.sites.find((site) => site.name === domain) ?? null;
}
