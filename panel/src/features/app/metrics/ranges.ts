/**
 * The time ranges of an app's charts, and what the charts say in words.
 *
 * The history endpoint reads each range as a window of its own, from the store's tiers: an hour
 * of raw samples, a day of minute means, a week and a month of hour means. It says which in
 * `resolution`, and the charts say it in words.
 */

import type { MetricWindow } from "../../../api/queries/metrics";

export type MetricRange = "1h" | "24h" | "7d" | "30d";

export interface RangeSpec {
  value: MetricRange;
  label: string;
  /** The range in words, for a chart's description: "Last 7 days". */
  words: string;
  seconds: number;
  /** The window the history endpoint is asked for. */
  window: MetricWindow;
}

const SPECS: Readonly<Record<MetricRange, RangeSpec>> = {
  "1h": { value: "1h", label: "1h", words: "Last hour", seconds: 3_600, window: "1h" },
  "24h": { value: "24h", label: "24h", words: "Last 24 hours", seconds: 86_400, window: "24h" },
  "7d": { value: "7d", label: "7d", words: "Last 7 days", seconds: 7 * 86_400, window: "7d" },
  "30d": { value: "30d", label: "30d", words: "Last 30 days", seconds: 30 * 86_400, window: "30d" },
};

export const RANGES: readonly RangeSpec[] = [SPECS["1h"], SPECS["24h"], SPECS["7d"], SPECS["30d"]];

export const DEFAULT_RANGE: MetricRange = "24h";

export function rangeSpec(range: MetricRange): RangeSpec {
  return SPECS[range];
}

export function isRange(value: unknown): value is MetricRange {
  return typeof value === "string" && RANGES.some((spec) => spec.value === value);
}

export type Points = readonly (readonly [number, number])[];

/** The points of a read that fall in the range ending now (Unix seconds). */
export function clip(points: Points | undefined, range: MetricRange, now: number): Points {
  if (points === undefined) return [];
  const from = now - rangeSpec(range).seconds;
  return points.filter(([time]) => time > from);
}

export interface Summary {
  latest: number;
  average: number;
  peak: number;
  /** When the peak was read, Unix seconds. */
  peakAt: number;
}

export function summarise(points: Points): Summary | null {
  const last = points.at(-1);
  if (last === undefined) return null;
  let total = 0;
  let peak = points[0] ?? last;
  for (const point of points) {
    total += point[1];
    if (point[1] > peak[1]) peak = point;
  }
  return { latest: last[1], average: total / points.length, peak: peak[1], peakAt: peak[0] };
}

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"] as const;

function pad(value: number): string {
  return String(value).padStart(2, "0");
}

/** A moment as short as the range allows: "14:05" within a day, "Sep 22, 14:05" beyond. */
export function momentWords(seconds: number, range: MetricRange): string {
  const date = new Date(seconds * 1000);
  const time = `${pad(date.getHours())}:${pad(date.getMinutes())}`;
  if (range === "1h" || range === "24h") return time;
  return `${MONTHS[date.getMonth()] ?? ""} ${String(date.getDate())}, ${time}`;
}

/** One sentence a person reads instead of the chart: "Average 3.1%, peak 41% at 14:05, now 2.4%." */
export function sentence(summary: Summary, range: MetricRange, format: (value: number) => string, limit: number | null): string {
  const parts = [
    `Average ${format(summary.average)}`,
    `peak ${format(summary.peak)} at ${momentWords(summary.peakAt, range)}`,
    `latest ${format(summary.latest)}`,
  ];
  if (limit !== null) parts.push(`limit ${format(limit)}`);
  return `${parts.join(", ")}.`;
}
