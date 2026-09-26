import { keepPreviousData, useQuery } from "@tanstack/react-query";
import type { ReactNode } from "react";

import { metricSeriesQuery } from "../../api/queries/metrics";
import type { MetricWindow } from "../../api/queries/metrics";
import { ErrorBlock } from "../../components/page/QueryState";
import { Section } from "../../components/page/Section";
import { SegmentedControl } from "../../components/page/SegmentedControl";
import { Chart } from "../../components/ui/Chart";
import { Skeleton } from "../../components/ui/Skeleton";
import { formatBytes, formatBytesRate, formatPercent } from "../../lib/format";
import { alignSeries, latest, resolutionWords } from "./series";
import type { Points } from "./series";
import { WINDOWS } from "./windows";

const WINDOW_WORDS: Record<MetricWindow, string> = {
  "1h": "Last hour",
  "24h": "Last 24 hours",
  "7d": "Last 7 days",
  "30d": "Last 30 days",
};

const CHART_HEIGHT = 132;
/** The whole chart block: caption (36px), readout (16px) and plot, 12px apart. */
const CHART_BLOCK_HEIGHT = 36 + 12 + 16 + 12 + CHART_HEIGHT;

/**
 * One metric's history, refreshed as often as a new point could change the picture. A new
 * range keeps the previous one on screen until it arrives: the chart (and its enlarged view,
 * where the range can be changed too) stays put instead of dropping back to a skeleton.
 */
function useSeries(metric: string, window: MetricWindow, enabled = true) {
  return useQuery({
    ...metricSeriesQuery(metric, window),
    refetchInterval: window === "1h" ? 30_000 : 5 * 60_000,
    placeholderData: keepPreviousData,
    enabled,
  });
}

interface ChartSpec {
  title: string;
  series: readonly { label: string; metric: string }[];
  format: (value: number) => string;
  /** A metric whose newest value bounds the value axis: the disk's size, the RAM installed. */
  ceiling?: string;
  /** A fixed value axis, for percentages. */
  range?: readonly [number, number];
}

const CHARTS: readonly ChartSpec[] = [
  { title: "CPU", series: [{ label: "CPU", metric: "cpu.percent" }], format: formatPercent, range: [0, 100] },
  { title: "Memory", series: [{ label: "Used", metric: "mem.used_bytes" }], format: formatBytes, ceiling: "mem.total_bytes" },
  {
    title: "Network",
    series: [
      { label: "In", metric: "net.rx_bytes_s" },
      { label: "Out", metric: "net.tx_bytes_s" },
    ],
    format: formatBytesRate,
  },
  { title: "Disk", series: [{ label: "Used", metric: "disk.used_bytes" }], format: formatBytes, ceiling: "disk.total_bytes" },
];

function ChartFrame({ children }: { children: ReactNode }) {
  return <div className="min-w-0 rounded-card border border-border bg-surface p-4 shadow-raised">{children}</div>;
}

/**
 * Exactly as tall as the chart that replaces it (the caption's 36px, the readout's 16px, the
 * plot, and the gaps between), so the sections below do not move when the data lands.
 */
function ChartSkeleton({ title }: { title: string }) {
  return (
    <div aria-busy="true" className="flex flex-col gap-3" style={{ minHeight: CHART_BLOCK_HEIGHT }}>
      <span className="sr-only">{`Loading the ${title.toLowerCase()} chart`}</span>
      <div aria-hidden="true" className="flex flex-col gap-3">
        <div className="flex h-9 flex-col justify-center gap-1.5">
          <Skeleton className="h-3.5 w-20" />
          <Skeleton className="h-3 w-28" />
        </div>
        <div className="flex h-4 items-center">
          <Skeleton className="h-3 w-24" />
        </div>
        <Skeleton className="h-33 w-full rounded-control" />
      </div>
    </div>
  );
}

function MetricChart({ spec, window, onWindowChange }: { spec: ChartSpec } & MachineChartsProps) {
  const [first, second] = spec.series;
  const one = useSeries(first?.metric ?? "", window);
  // Hooks run in a fixed order: the second series and the ceiling are fetched only when the
  // chart has one, and otherwise the query is left disabled.
  const two = useSeries(second?.metric ?? "", window, second !== undefined);
  const ceiling = useSeries(spec.ceiling ?? "", window, spec.ceiling !== undefined);

  const queries = [one, ...(second ? [two] : [])];
  const failed = queries.find((query) => query.isError);
  if (failed && queries.some((query) => query.data === undefined)) {
    return (
      <ChartFrame>
        <ErrorBlock compact error={failed.error} title={`Could not load the ${spec.title.toLowerCase()} history`} onRetry={() => void failed.refetch()} />
      </ChartFrame>
    );
  }
  if (queries.some((query) => query.data === undefined)) {
    return (
      <ChartFrame>
        <ChartSkeleton title={spec.title} />
      </ChartFrame>
    );
  }

  const points: Points[] = queries.map((query) => query.data?.points ?? []);
  const aligned = alignSeries(points);
  const max = spec.ceiling ? latest(ceiling.data?.points) : null;
  const spacing = resolutionWords(queries[0]?.data?.resolution);
  const description = [WINDOW_WORDS[window], spacing, max !== null ? `of ${spec.format(max)}` : null]
    .filter((part) => part !== null)
    .join(", ");

  // One point is not a line: a panel that just started has a sample or two, and a chart of
  // them would be an empty frame with a collapsed time axis.
  if (aligned.timestamps.length < 2) {
    return (
      <ChartFrame>
        <div className="flex flex-col" style={{ minHeight: CHART_BLOCK_HEIGHT }}>
          <h3 className="text-13 font-medium text-fg">{spec.title}</h3>
          <p className="text-12 text-fg-faint">{WINDOW_WORDS[window]}</p>
          <p className="m-auto max-w-[30ch] py-4 text-center text-13 text-pretty text-fg-muted">
            Collecting samples. The panel records one every few seconds while it runs.
          </p>
        </div>
      </ChartFrame>
    );
  }

  const range = spec.range ?? (max !== null ? ([0, max] as const) : undefined);
  return (
    <ChartFrame>
      <Chart
        title={spec.title}
        description={description}
        timestamps={aligned.timestamps}
        series={spec.series.map((s, i) => ({ label: s.label, values: aligned.values[i] ?? [] }))}
        formatValue={spec.format}
        height={CHART_HEIGHT}
        rangeSelector={{ value: window, control: <RangeControl window={window} onWindowChange={onWindowChange} /> }}
        {...(range !== undefined ? { yRange: range } : {})}
      />
    </ChartFrame>
  );
}

export interface MachineChartsProps {
  window: MetricWindow;
  onWindowChange: (window: MetricWindow) => void;
}

/** The one range control, on the section and again in an enlarged chart. */
function RangeControl({ window, onWindowChange }: MachineChartsProps) {
  return <SegmentedControl label="Time range" options={WINDOWS} value={window} onValueChange={onWindowChange} />;
}

/** The machine's recent history: CPU, memory, network and disk over the chosen window. */
export function MachineCharts({ window, onWindowChange }: MachineChartsProps) {
  return (
    <Section
      title="Machine"
      description="CPU, memory, network and disk over time."
      actions={<RangeControl window={window} onWindowChange={onWindowChange} />}
    >
      {/* Sized by the room the page gives it, not the viewport: the sidebar comes and goes.
          Four abreast only where each still gets a readable width (about 350px). */}
      <div className="@container">
        <div className="grid gap-4 @2xl:grid-cols-2 @[88rem]:grid-cols-4">
          {CHARTS.map((spec) => (
            <MetricChart key={spec.title} spec={spec} window={window} onWindowChange={onWindowChange} />
          ))}
        </div>
      </div>
    </Section>
  );
}
