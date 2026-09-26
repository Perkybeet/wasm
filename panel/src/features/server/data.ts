/**
 * What the Server page reads beyond the raw API shape: the health verdict and each check's
 * status in the console's state language.
 */

import type { Status } from "../../components/ui/StatusPill";
import type { SystemHealth } from "../../api/queries/system";

export type Verdict = SystemHealth["verdict"];
export type HealthCheck = SystemHealth["checks"][number];

export interface VerdictView {
  state: Status;
  label: string;
}

/** `collect_health_report`'s verdict: "error", "warning" or "healthy" (wasm.managers.health). */
export function verdictView(verdict: string): VerdictView {
  switch (verdict) {
    case "healthy":
      return { state: "running", label: "Healthy" };
    case "warning":
      return { state: "warning", label: "Needs attention" };
    case "error":
      return { state: "failed", label: "Critical" };
    default:
      return { state: "unknown", label: verdict };
  }
}

/** One check's status: "ok", "warning", "error" or "info". */
export function checkView(status: string): VerdictView {
  switch (status) {
    case "ok":
      return { state: "running", label: "OK" };
    case "warning":
      return { state: "warning", label: "Warning" };
    case "error":
      return { state: "failed", label: "Error" };
    default:
      return { state: "unknown", label: "Info" };
  }
}

/**
 * `wasm health` names its checks in Title Case for the terminal ("Disk Space"); the console
 * writes labels in sentence case. Known names are reworded, anything else is shown as sent.
 */
const CHECK_NAMES: Readonly<Record<string, string>> = {
  "Disk Space": "Disk space",
  "SSL Certificates": "SSL certificates",
};

export function checkName(name: string): string {
  return CHECK_NAMES[name] ?? name;
}

/** A reason for the verdict: an issue fails the check, a warning only needs attention. */
export interface HealthReason {
  level: "issue" | "warning";
  message: string;
  /** The certificate the message is about, when it is about one, to link to it. */
  certificate: CertificateMention | null;
}

export interface CertificateMention {
  /** The message up to the certificate's name: "Certificate for ". */
  before: string;
  name: string;
  /** The rest of the message: " expired 3 days ago". */
  after: string;
}

/**
 * The certificate a health message names. `collect_health_report` words each one as
 * "Certificate for <name> expired N days ago", "... expires in N days" or "... has an
 * unreadable expiry date" (wasm.managers.health._check_certificates), with the certbot lineage
 * name, which carries no spaces.
 *
 * The contract is that wording, not a field: src/wasm/managers/health.py builds these strings
 * with f"Certificate for {label} ..." where the label comes from _certificate_label (the lineage
 * name, else the first covered domain). Rewording them there, or naming a certificate with
 * something that can hold a space, silently stops the link from appearing here: change this
 * pattern and its cases in data.test.ts in the same commit.
 */
export function certificateMention(message: string): CertificateMention | null {
  const match = /^(Certificate for )(\S+)( .+)$/.exec(message);
  if (match === null) return null;
  const [, before = "", name = "", after = ""] = match;
  return { before, name, after };
}

/** The report's issues, then its warnings: why the verdict is what it is, most serious first. */
export function healthReasons(report: Pick<SystemHealth, "issues" | "warnings">): HealthReason[] {
  return [
    ...report.issues.map((message) => ({ level: "issue" as const, message, certificate: certificateMention(message) })),
    ...report.warnings.map((message) => ({ level: "warning" as const, message, certificate: certificateMention(message) })),
  ];
}
