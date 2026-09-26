/**
 * Turns the metrics history endpoint's answers (`[unix seconds, value]` pairs, oldest first,
 * one request per metric) into what the Chart component draws: one ascending time axis and
 * one value per series per time.
 */

export type Points = readonly (readonly [number, number])[];

export interface AlignedSeries {
  timestamps: number[];
  values: (number | null)[][];
}

/**
 * Merges several point lists onto one time axis. A series with no sample at a time gets a
 * gap (null), never an invented value: the collector samples every metric in the same tick,
 * so gaps are real (the collector was down, a counter reset).
 */
export function alignSeries(series: readonly Points[]): AlignedSeries {
  const times = new Set<number>();
  for (const points of series) for (const [time] of points) times.add(time);
  const timestamps = [...times].sort((a, b) => a - b);
  const index = new Map(timestamps.map((time, i) => [time, i]));
  const values = series.map((points) => {
    const row: (number | null)[] = Array.from({ length: timestamps.length }, () => null);
    for (const [time, value] of points) {
      const at = index.get(time);
      if (at !== undefined && Number.isFinite(value)) row[at] = value;
    }
    return row;
  });
  return { timestamps, values };
}

/** The newest value of a series, or null when it has none. */
export function latest(points: Points | undefined): number | null {
  const last = points?.at(-1);
  return last === undefined ? null : last[1];
}

/**
 * How far apart a history read's points are, in words, from the endpoint's `resolution`: a
 * week is hour means, not samples, and a chart should say so. Nothing for raw samples.
 */
export function resolutionWords(resolution: string | undefined): string | null {
  switch (resolution) {
    case "minute":
      return "minute averages";
    case "hour":
      return "hourly averages";
    default:
      return null;
  }
}
