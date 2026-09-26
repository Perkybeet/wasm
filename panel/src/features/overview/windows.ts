/**
 * The machine charts' time ranges: just the values and labels, so a route's `validateSearch`
 * can read them without pulling in `MachineCharts.tsx` (and, with it, uPlot) into the eagerly
 * loaded route-options chunk. `MachineCharts.tsx` re-uses this list rather than defining its own.
 */

import type { MetricWindow } from "../../api/queries/metrics";

export const WINDOWS: readonly { value: MetricWindow; label: string }[] = [
  { value: "1h", label: "1h" },
  { value: "24h", label: "24h" },
  { value: "7d", label: "7d" },
  { value: "30d", label: "30d" },
];
