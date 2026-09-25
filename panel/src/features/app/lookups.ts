/**
 * Finding one app in the machine-wide lists. The certificate and the site are found in
 * `GET /api/certs` and `GET /api/sites` rather than asked for by domain: an app without either
 * is a normal state, and the per-domain endpoints answer it with a 404 the browser logs as an
 * error.
 */

import type { CertList } from "../../api/queries/certs";
import type { SiteList } from "../../api/queries/sites";

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
