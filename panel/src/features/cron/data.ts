/**
 * What the Cron page reads beyond the raw API shape: a run's outcome in the console's state
 * language, the calendar presets the editor offers, and the formatting its live preview needs.
 *
 * The preset values double as the `schedule` the API takes (`hourly`, `daily`, `weekly`,
 * `monthly`): `CronManager` expands the alias itself (`SCHEDULE_ALIASES`), so the dialog sends
 * the alias rather than keeping its own copy of what each one expands to - one implementation,
 * and it is `POST /api/cron/preview` that shows the operator what the alias actually means.
 */

import type { Status } from "../../components/ui/StatusPill";
import type { CronJobList } from "../../api/queries/cron";
import { parseTimestamp } from "../../lib/format";

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
  /** Systemd's own word for how a failed run ended (`exit-code`, `timeout`), shown in mono. */
  detail?: string;
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
  // The state word is the state; systemd's reason goes beside it, as the services list does.
  return { state: "failed", label: "Failed", detail: word };
}

/**
 * A schedule in words: the preset's name, or for an expression the one shape worth naming
 * ("*-*-* 03:30:00" is every day at 03:30); anything else is "Custom", with the expression
 * itself shown beside it.
 */
export function scheduleWords(schedule: string, onCalendar: string): string {
  const preset = SCHEDULE_PRESETS.find((option) => option.value === schedule.trim().toLowerCase() && option.value !== "custom");
  if (preset) return preset.label;
  const daily = /^\*-\*-\*\s+(\d{1,2}):(\d{2})(?::00)?$/.exec(onCalendar.trim());
  if (daily) return `Every day at ${(daily[1] ?? "").padStart(2, "0")}:${daily[2] ?? ""}`;
  return "Custom";
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

/**
 * One of the preview's next runs, in full: unambiguous regardless of the reader's own zone,
 * the way a next-run list should read next to the relative time the rest of the console uses.
 * Falls back to the raw value on anything `parseTimestamp` cannot place in time.
 */
export function absoluteWithOffset(value: string): string {
  const date = parseTimestamp(value);
  if (date === null) return value;
  // Explicit fields, not `dateStyle`/`timeStyle`: mixed with `timeZoneName` those throw
  // ("Invalid option") on the ICU build this ships with, even though both are valid alone.
  return new Intl.DateTimeFormat(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
    second: "2-digit",
    timeZoneName: "shortOffset",
  }).format(date);
}
