/**
 * What a certificate's expiry means to the operator, decided once for the certificates table,
 * its drawer and an app's Domains tab. Pure, so every threshold is tested without a page.
 */

import type { CertEntry } from "../../api/queries/certs";
import type { Job } from "../../api/queries/jobs";
import { CERT_WARNING_DAYS } from "../overview/attention";

export { CERT_WARNING_DAYS };

export type CertTone = "ok" | "warn" | "fail" | "idle" | "busy";

export interface CertificateView {
  tone: CertTone;
  /** The state in words, for a table cell: "Valid for 46 days", "Expires in 12 days". */
  label: string;
  /** True when the operator should act: renew soon or now. */
  attention: boolean;
}

function days(count: number): string {
  return `${String(count)} ${count === 1 ? "day" : "days"}`;
}

/** The state of a certificate from the days it has left. */
export function certificateView(cert: Pick<CertEntry, "days_remaining">): CertificateView {
  const left = cert.days_remaining;
  if (left === null || left === undefined) return { tone: "idle", label: "Expiry unknown", attention: false };
  if (left < 0) return { tone: "fail", label: left === -1 ? "Expired yesterday" : `Expired ${days(-left)} ago`, attention: true };
  if (left === 0) return { tone: "warn", label: "Expires today", attention: true };
  if (left < CERT_WARNING_DAYS) return { tone: "warn", label: `Expires in ${days(left)}`, attention: true };
  return { tone: "ok", label: `Valid for ${days(left)}`, attention: false };
}

/** Most urgent first: expired, then the fewest days left; unknown expiry last. */
export function byUrgency(a: Pick<CertEntry, "days_remaining">, b: Pick<CertEntry, "days_remaining">): number {
  const left = (cert: Pick<CertEntry, "days_remaining">): number => cert.days_remaining ?? Number.POSITIVE_INFINITY;
  return left(a) - left(b);
}

/** The certificate jobs the backend runs (its JobType values). */
export const CERT_JOB_TYPES: ReadonlySet<string> = new Set(["cert_create", "cert_renew"]);

const RUNNING = new Set(["pending", "running"]);

/**
 * The certificate job queued or running for a certificate name, if any. Jobs name their
 * certificate in `metadata.domain`; renewing every certificate names "all".
 */
export function certificateJobFor(jobs: readonly Job[] | undefined, name: string): Job | null {
  return (
    jobs?.find(
      (job) =>
        CERT_JOB_TYPES.has(job.type) &&
        RUNNING.has(job.status) &&
        (job.metadata?.["domain"] === name || job.metadata?.["domain"] === "all"),
    ) ?? null
  );
}

/** Whether a certificate lists a name among the ones it covers. */
export function covers(cert: Pick<CertEntry, "domain" | "domains"> | null | undefined, name: string): boolean {
  if (!cert) return false;
  return cert.domain === name || cert.domains.includes(name);
}
