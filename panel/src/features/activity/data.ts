/**
 * What the Activity page reads beyond the raw API shape.
 *
 * The page is meant to merge the audit log and the jobs history into one timeline (actor,
 * action, resource, result). No audit log endpoint exists in `wasm.web.api` - every mutation
 * writes to the `wasm.audit` Python logger, not to a queryable store - so the timeline is jobs
 * history only; sign-ins, configuration changes and other audited events are not shown, and
 * this is reported rather than invented. `JobRecord` (src/wasm/core/store.py) also carries no
 * actor: a job's row cannot say who queued it, only what it did, to what, and how it ended.
 */

import type { JobList } from "../../api/queries/jobs";

export type ActivityJob = JobList["jobs"][number];

/** `wasm.web.jobs.JobType`, in the console's words. */
const ACTION_LABELS: Record<string, string> = {
  deploy: "Deploy",
  update: "Update",
  backup: "Backup",
  restore: "Restore",
  cert_create: "Issue certificate",
  cert_renew: "Renew certificate",
  service_action: "Service action",
  site_action: "Site action",
  delete: "Delete",
  custom: "Custom",
};

export function actionLabel(type: string): string {
  return ACTION_LABELS[type] ?? type;
}

/** The domain a job acted on, when it named one. */
export function jobResource(job: ActivityJob): string | null {
  const domain = job.metadata?.["domain"];
  return typeof domain === "string" && domain !== "" ? domain : null;
}

export interface ActivitySearch {
  status?: string;
  /** An exact job type, as `wasm.web.jobs.JobType` names it. */
  type?: string;
  domain?: string;
}

const STATUSES = new Set(["pending", "running", "completed", "failed", "cancelled"]);
const TYPES = new Set(Object.keys(ACTION_LABELS));

function text(value: unknown): string | undefined {
  if (typeof value !== "string") return undefined;
  const trimmed = value.trim();
  return trimmed === "" ? undefined : trimmed.slice(0, 200);
}

export function validateActivitySearch(search: Record<string, unknown>): ActivitySearch {
  const status = text(search["status"]);
  const type = text(search["type"]);
  const domain = text(search["domain"]);
  return {
    ...(status !== undefined && STATUSES.has(status) ? { status } : {}),
    ...(type !== undefined && TYPES.has(type) ? { type } : {}),
    ...(domain !== undefined ? { domain } : {}),
  };
}

export function isFiltered(search: ActivitySearch): boolean {
  return search.status !== undefined || search.type !== undefined || search.domain !== undefined;
}

/** The jobs a search keeps; the API filters by status and domain, so only `type` is left to do here. */
export function filterJobs(jobs: readonly ActivityJob[], search: ActivitySearch): ActivityJob[] {
  if (search.type === undefined) return [...jobs];
  return jobs.filter((job) => job.type === search.type);
}
