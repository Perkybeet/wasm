/**
 * What the Cron page reads beyond the raw API shape: a run's outcome in the console's state
 * language, and the calendar presets the editor offers.
 *
 * There is no endpoint that previews a calendar expression's next runs before saving one - the
 * only next run WASM computes is `next_run` on the job systemd already has, returned by
 * `POST /api/cron` and `GET /api/cron` after the fact. The editor therefore does not attempt a
 * live "next five runs" preview (that would mean re-implementing systemd's calendar engine in
 * TypeScript, which drifts from the real thing by construction); it shows what the backend
 * validates on save, and the single next run the saved job reports.
 */

import type { Status } from "../../components/ui/StatusPill";
import type { CronJobList } from "../../api/queries/cron";

export type CronJob = CronJobList["jobs"][number];

export type Schedule = "hourly" | "daily" | "weekly" | "monthly" | "custom";

export const SCHEDULE_PRESETS: readonly { value: Schedule; label: string }[] = [
  { value: "hourly", label: "Hourly" },
  { value: "daily", label: "Daily" },
  { value: "weekly", label: "Weekly" },
  { value: "monthly", label: "Monthly" },
  { value: "custom", label: "Custom" },
];

export interface RunView {
  state: Status;
  label: string;
}

/**
 * A run's systemd `Result` (`success`, `exit-code`, `signal`, `timeout`, `resources`,
 * `core-dump`, `watchdog`, `start-limit-hit`), or the job's own `never ran`/`unknown`, in the
 * console's state language.
 */
export function runStatus(result: string | null | undefined): RunView {
  const word = (result ?? "").trim().toLowerCase();
  if (word === "" || word === "never ran") return { state: "unknown", label: "Never run" };
  if (word === "success") return { state: "running", label: "Succeeded" };
  if (word === "unknown") return { state: "unknown", label: "Unknown" };
  return { state: "failed", label: result ?? "Failed" };
}

export interface CronSearch {
  /** Free text matched against the job name and command. */
  q?: string;
}

function text(value: unknown): string | undefined {
  if (typeof value !== "string") return undefined;
  const trimmed = value.trim();
  return trimmed === "" ? undefined : trimmed.slice(0, 200);
}

export function validateCronSearch(search: Record<string, unknown>): CronSearch {
  const q = text(search["q"]);
  return q !== undefined ? { q } : {};
}

export function isFiltered(search: CronSearch): boolean {
  return search.q !== undefined;
}

export function filterJobs(jobs: readonly CronJob[], search: CronSearch): CronJob[] {
  const needle = search.q?.toLowerCase();
  if (needle === undefined) return [...jobs];
  return jobs.filter((job) => `${job.name} ${job.command}`.toLowerCase().includes(needle));
}
