/**
 * "Needs attention": the problems on this machine, gathered from every source that can report
 * one, grouped by what they are about, the worst first. Pure, so every rule is tested without
 * a page.
 */

import type { CertList } from "../../api/queries/certs";
import type { ObservationList } from "../../api/queries/monitor";
import type { Machine } from "../../api/queries/system";
import { appStatus, deployStatus } from "../../components/page/status";
import type { AppInfo, Deployment } from "../apps/data";
import { deployMoment, latestDeployByDomain } from "../apps/data";
import { serviceState } from "../services/data";
import type { ServiceInfo } from "../services/data";

type Cert = CertList["certificates"][number];
type Observation = ObservationList["observations"][number];

/** Days before expiry at which a certificate starts needing attention (spec, Task 3.11). */
export const CERT_WARNING_DAYS = 21;

export type Severity = "fail" | "warn";

export interface AttentionReason {
  /** Where the problem comes from, which decides where it is fixed. */
  kind: "state" | "deploy" | "certificate" | "units" | "monitor";
  severity: Severity;
  /** What is wrong, in the console's words: "Last deploy failed". */
  summary: string;
  /** The system's own words, verbatim, when there are any: the first line of the error. */
  detail?: string;
  /** When it happened, as the backend sent it. */
  when?: string | null;
  /** A deployment to open for the full story. */
  deploymentId?: number;
}

export type AttentionSubject =
  | { kind: "app"; domain: string }
  | { kind: "certificate"; domain: string }
  | { kind: "units" }
  | { kind: "unit"; name: string }
  | { kind: "monitor"; process: string; pid: number };

export interface AttentionItem {
  /** Stable across refreshes, for React keys and tests. */
  id: string;
  subject: AttentionSubject;
  /** What it is about: a domain, "systemd", a process. */
  title: string;
  severity: Severity;
  reasons: AttentionReason[];
}

export interface AttentionSources {
  apps?: readonly AppInfo[] | undefined;
  /** Recent deployments across the machine, newest first. */
  deployments?: readonly Deployment[] | undefined;
  certificates?: readonly Cert[] | undefined;
  observations?: readonly Observation[] | undefined;
  machine?: Machine | undefined;
  /** WASM's own units (GET /api/services): names the failed ones no app accounts for. */
  units?: readonly ServiceInfo[] | undefined;
}

/** The first line of a multi-line error, which is where tools put the error itself. */
function firstLine(text: string | null | undefined): string | undefined {
  const line = text
    ?.split("\n")
    .map((part) => part.trim())
    .find((part) => part !== "");
  return line === undefined || line === "" ? undefined : line;
}

const RANK: Record<Severity, number> = { fail: 0, warn: 1 };

/**
 * The names an app's unit can have, for an app whose record does not name it: the current one
 * (`domain_to_app_name`) and the one apps deployed before v0.14.1 kept (`legacy_app_name`).
 */
export function appUnitNames(domain: string): string[] {
  const name = domain.toLowerCase().replace(/[^a-z0-9-]/g, "-");
  return [name, `wasm-${name}`];
}

/**
 * Everything that needs an operator, grouped by subject:
 *
 * - an app whose state is a problem (failed, crash-looping, not answering);
 * - an app whose newest deploy failed or was rolled back;
 * - a certificate expiring within 21 days, or expired;
 * - systemd units WASM manages that failed or keep crashing, beyond the apps' own units (an
 *   app's state already names those): each by name when the unit list is known, otherwise as
 *   a count from the machine's tally;
 * - an open monitor observation.
 */
export function collectAttention({ apps, deployments, certificates, observations, machine, units }: AttentionSources): AttentionItem[] {
  const byDomain = new Map<string, AttentionItem>();
  const add = (domain: string, subject: AttentionSubject, reason: AttentionReason): void => {
    const known = byDomain.get(domain);
    if (known) {
      known.reasons.push(reason);
      if (RANK[reason.severity] < RANK[known.severity]) known.severity = reason.severity;
      return;
    }
    byDomain.set(domain, { id: `${subject.kind}:${domain}`, subject, title: domain, severity: reason.severity, reasons: [reason] });
  };

  const appDomains = new Set(apps?.map((app) => app.domain));
  let failedByState = 0;

  for (const app of apps ?? []) {
    const view = appStatus(app.status);
    if (!view.attention) continue;
    if (view.state === "failed") failedByState += 1;
    add(app.domain, { kind: "app", domain: app.domain }, {
      kind: "state",
      severity: view.state === "failed" ? "fail" : "warn",
      summary: view.label === "Failed" ? "The service has failed" : `The service is in state ${view.label}`,
    });
  }

  for (const deploy of latestDeployByDomain(deployments ?? []).values()) {
    const view = deployStatus(deploy.status);
    if (!view.attention) continue;
    // A deploy of something that is no longer an app (deleted since) is history, not a problem.
    if (apps !== undefined && !appDomains.has(deploy.domain)) continue;
    const reason: AttentionReason = {
      kind: "deploy",
      severity: deploy.status === "failed" ? "fail" : "warn",
      summary: deploy.status === "failed" ? "Last deploy failed" : "Last deploy was rolled back",
      when: deployMoment(deploy),
      deploymentId: deploy.id,
    };
    const detail = firstLine(deploy.error);
    add(deploy.domain, { kind: "app", domain: deploy.domain }, detail === undefined ? reason : { ...reason, detail });
  }

  for (const cert of certificates ?? []) {
    const days = cert.days_remaining;
    if (days === null || days === undefined || days >= CERT_WARNING_DAYS) continue;
    const subject: AttentionSubject = appDomains.has(cert.domain)
      ? { kind: "app", domain: cert.domain }
      : { kind: "certificate", domain: cert.domain };
    const reason: AttentionReason =
      days < 0
        ? { kind: "certificate", severity: "fail", summary: "Certificate expired" }
        : {
            kind: "certificate",
            severity: "warn",
            summary: days === 0 ? "Certificate expires today" : `Certificate expires in ${String(days)} ${days === 1 ? "day" : "days"}`,
          };
    add(cert.domain, subject, { ...reason, ...(cert.expires_on ? { detail: `Valid until ${cert.expires_on}` } : {}) });
  }

  const items = [...byDomain.values()];

  if (units !== undefined) {
    const appUnits = new Set(apps?.flatMap((app) => [app.unit ?? "", ...appUnitNames(app.domain)]));
    for (const unit of units) {
      if (!unit.managed || appUnits.has(unit.name)) continue;
      const view = serviceState(unit);
      const failed = view.state === "failed";
      if (!failed && view.state !== "warning") continue;
      const result = (unit.result ?? "").trim();
      items.push({
        id: `unit:${unit.name}`,
        subject: { kind: "unit", name: unit.name },
        title: unit.name,
        severity: failed ? "fail" : "warn",
        reasons: [
          {
            kind: "units",
            severity: failed ? "fail" : "warn",
            summary: failed ? "The unit has failed" : "systemd keeps restarting the unit",
            ...(result !== "" && result !== "success" ? { detail: `Result=${result}` } : {}),
          },
        ],
      });
    }
  }

  const failedUnits = machine?.units.failed ?? 0;
  if (units === undefined && failedUnits > failedByState) {
    const extra = failedUnits - failedByState;
    items.push({
      id: "units",
      subject: { kind: "units" },
      title: "Services",
      severity: "fail",
      reasons: [
        {
          kind: "units",
          severity: "fail",
          summary: `systemd reports ${String(extra)} failed WASM ${extra === 1 ? "unit" : "units"}`,
        },
      ],
    });
  }

  for (const observation of observations ?? []) {
    if (observation.acknowledged) continue;
    // The monitor observes and never acts; its findings ("warning", "notice") are for a
    // person to look at, not failures of anything WASM runs.
    const severity: Severity = "warn";
    items.push({
      id: `monitor:${String(observation.id ?? `${observation.process_name}-${String(observation.pid)}`)}`,
      subject: { kind: "monitor", process: observation.process_name, pid: observation.pid },
      title: observation.process_name,
      severity,
      reasons: [
        {
          kind: "monitor",
          severity,
          summary: `Monitor ${observation.severity}: ${observation.signal}`,
          when: observation.observed_at,
          ...(observation.detail ? { detail: observation.detail } : {}),
        },
      ],
    });
  }

  return items.sort((a, b) => RANK[a.severity] - RANK[b.severity] || a.title.localeCompare(b.title));
}
