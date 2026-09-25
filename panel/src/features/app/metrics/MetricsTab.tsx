import { useQuery } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { ChartLine } from "lucide-react";
import type { ReactNode } from "react";

import { appQuery } from "../../../api/queries/apps";
import type { App } from "../../../api/queries/apps";
import { deploymentsQuery } from "../../../api/queries/deployments";
import { metricSeriesQuery } from "../../../api/queries/metrics";
import { useDocumentTitle } from "../../../app/documentTitle";
import { useNow } from "../../../components/page/clock";
import { ErrorBlock } from "../../../components/page/QueryState";
import { Section } from "../../../components/page/Section";
import { SegmentedControl } from "../../../components/page/SegmentedControl";
import { appStatus, deployStatus } from "../../../components/page/status";
import { Chart } from "../../../components/ui/Chart";
import { EmptyState } from "../../../components/ui/EmptyState";
import { Skeleton } from "../../../components/ui/Skeleton";
import { STATUS, StatusGlyph } from "../../../components/ui/StatusPill";
import { cx } from "../../../lib/cx";
import { formatBytes, formatPercent, parseTimestamp } from "../../../lib/format";
import { appLimits } from "../../apps/data";
import { alignSeries } from "../../overview/series";
import { DeployMarkers } from "./DeployMarkers";
import type { DeployMark } from "./DeployMarkers";
import { RANGES, clip, momentWords, rangeSpec, sentence, summarise } from "./ranges";
import type { MetricRange, Points } from "./ranges";

const CHART_HEIGHT = 180;

const TONE_TEXT = { ok: "text-ok", warn: "text-warn", fail: "text-fail", idle: "text-idle" } as const;

interface ChartSpec {
  title: string;
  metric: (domain: string) => string;
  format: (value: number) => string;
  limit: (app: App) => number | null;
  /** What the value is, beside the title. */
  unit: string;
}

const CHARTS: readonly ChartSpec[] = [
  {
    title: "CPU",
    metric: (domain) => `app.${domain}.cpu.percent`,
    format: formatPercent,
    limit: (app) => appLimits(app).cpu,
    unit: "Percent of one CPU",
  },
  {
    title: "Memory",
    metric: (domain) => `app.${domain}.mem.bytes`,
    format: formatBytes,
    limit: (app) => appLimits(app).memory,
    unit: "Resident memory of the unit",
  },
];

function Frame({ children }: { children: ReactNode }) {
  return <div className="min-w-0 rounded-card border border-border bg-surface p-4 shadow-raised">{children}</div>;
}

function ChartSkeleton({ title }: { title: string }) {
  return (
    <Frame>
      <div aria-busy="true" className="flex flex-col gap-3">
        <span className="sr-only">{`Loading the ${title.toLowerCase()} chart`}</span>
        <div aria-hidden="true" className="flex flex-col gap-3">
          <Skeleton className="h-3.5 w-24" />
          <Skeleton className="h-3 w-64" />
          <Skeleton className="h-45 w-full rounded-control" />
        </div>
      </div>
    </Frame>
  );
}

/** One metric of the app over the range, with its limit as a second line and its deploys marked. */
function MetricChart({
  spec,
  app,
  range,
  marks,
  now,
}: {
  spec: ChartSpec;
  app: App;
  range: MetricRange;
  marks: readonly DeployMark[];
  now: number;
}) {
  const detail = rangeSpec(range);
  const series = useQuery({
    ...metricSeriesQuery(spec.metric(app.domain), detail.window),
    refetchInterval: range === "1h" ? 30_000 : 5 * 60_000,
  });

  if (series.isError && series.data === undefined) {
    return (
      <Frame>
        <ErrorBlock compact error={series.error} title={`Could not load the ${spec.title.toLowerCase()} history`} onRetry={() => void series.refetch()} />
      </Frame>
    );
  }
  if (series.data === undefined) return <ChartSkeleton title={spec.title} />;

  const points: Points = clip(series.data.points, range, now);
  const summary = summarise(points);
  const limit = spec.limit(app);
  if (summary === null) {
    return (
      <Frame>
        <div className="flex flex-col gap-1">
          <h3 className="text-13 font-medium text-fg">{spec.title}</h3>
          <p className="text-13 text-fg-muted">{`No readings in the ${detail.words.toLowerCase()}.`}</p>
        </div>
      </Frame>
    );
  }

  const aligned = alignSeries([points]);
  const values = aligned.values[0] ?? [];
  // A limit far above the readings would flatten them against the axis; drawn only when the
  // app comes near it, and always said in the summary.
  const drawLimit = limit !== null && summary.peak >= limit / 3;
  const lines = [
    { label: spec.title, values },
    ...(drawLimit ? [{ label: "Limit", values: values.map(() => limit) }] : []),
  ];
  const from = aligned.timestamps[0] ?? 0;
  const to = aligned.timestamps.at(-1) ?? 0;

  return (
    <Frame>
      <div className="flex flex-col gap-3">
        <DeployMarkers domain={app.domain} marks={marks} from={from} to={to}>
          <Chart
            title={spec.title}
            description={`${detail.words}. ${spec.unit}.`}
            timestamps={aligned.timestamps}
            series={lines}
            formatValue={spec.format}
            height={CHART_HEIGHT}
          />
        </DeployMarkers>
        <p className="border-t border-border pt-3 text-12 text-pretty text-fg-muted" data-summary="">
          <span className="sr-only">{`${spec.title}: `}</span>
          {sentence(summary, range, spec.format, limit)}
        </p>
      </div>
    </Frame>
  );
}

/** The deploys in the range, in words: what the marks on the charts are. */
function DeployList({ domain, marks }: { domain: string; marks: readonly DeployMark[] }) {
  return (
    <Section title="Deploys in this range" level={3}>
      {marks.length === 0 ? (
        <p className="text-13 text-fg-muted">None. A deploy restarts the app, which usually shows as a drop in memory.</p>
      ) : (
        <ul className="flex flex-wrap gap-2">
          {marks.map((mark) => {
            const view = deployStatus(mark.status);
            return (
              <li key={mark.id}>
                <Link
                  to="/apps/$domain/deployments/$id"
                  params={{ domain, id: String(mark.id) }}
                  className="flex h-7 items-center gap-1.5 rounded-pill border border-border bg-surface px-2.5 text-12 text-fg hover:bg-surface-hover focus-visible:outline-2 focus-visible:outline-focus"
                >
                  <span className={cx("flex", TONE_TEXT[STATUS[view.state].tone])}>
                    <StatusGlyph state={view.state} size={10} />
                  </span>
                  <span className="sr-only">{mark.label}</span>
                  <span aria-hidden="true" className="flex items-baseline gap-1.5">
                    <span className="mono">{`#${String(mark.id)}`}</span>
                    <span className="text-fg-muted">{mark.when}</span>
                  </span>
                </Link>
              </li>
            );
          })}
        </ul>
      )}
    </Section>
  );
}

export interface MetricsTabProps {
  domain: string;
  range: MetricRange;
  onRangeChange: (range: MetricRange) => void;
}

/**
 * The app's CPU and memory over the last hour, day, week or month, against its limits, with its
 * deploys marked. Each chart has a sentence that says what it shows and a table of its numbers.
 */
export function MetricsTab({ domain, range, onRangeChange }: MetricsTabProps) {
  useDocumentTitle(`Metrics - ${domain}`, 1);
  const app = useQuery(appQuery(domain));
  const deploys = useQuery(deploymentsQuery({ domain, limit: 200 }));
  // One clock for both charts and the marks: the range ends at the same instant for all.
  const now = Math.floor(useNow(() => 60_000) / 1000);

  if (app.data === undefined) {
    return app.isError ? null : (
      <div className="grid gap-4 xl:grid-cols-2">
        <ChartSkeleton title="CPU" />
        <ChartSkeleton title="Memory" />
      </div>
    );
  }

  // A site of the static type has no process even when a unit was left behind for it.
  if (appStatus(app.data.status).state === "static" || app.data.app_type === "static") {
    return (
      <EmptyState
        level={2}
        icon={<ChartLine />}
        title="A static site has no process to measure"
        description="The web server serves its files directly, so there is no unit whose CPU and memory could be charted. The machine's own charts are on the overview."
        action={
          <Link to="/" className="rounded-[4px] text-13 font-medium text-accent-fg hover:underline hover:underline-offset-2 focus-visible:outline-2 focus-visible:outline-focus">
            Machine overview
          </Link>
        }
        className="py-16"
      />
    );
  }

  const from = now - rangeSpec(range).seconds;
  const marks: DeployMark[] = (deploys.data?.items ?? []).flatMap((deploy) => {
    const at = parseTimestamp(deploy.started_at);
    if (at === null) return [];
    const seconds = Math.floor(at.getTime() / 1000);
    if (seconds <= from || seconds > now) return [];
    const word = deployStatus(deploy.status).label.toLowerCase();
    const when = momentWords(seconds, range);
    return [{ id: deploy.id, at: seconds, status: deploy.status, when, label: `Deploy ${String(deploy.id)}, ${word}, ${when}` }];
  });
  marks.sort((a, b) => a.at - b.at);

  return (
    <Section
      title="CPU and memory"
      description="Read from the app's unit every few seconds while the panel runs; older readings are kept as minute and hour averages."
      actions={
        <SegmentedControl
          label="Time range"
          options={RANGES.map((spec) => ({ value: spec.value, label: spec.label }))}
          value={range}
          onValueChange={onRangeChange}
        />
      }
    >
      <div className="grid min-w-0 gap-4 xl:grid-cols-2">
        {CHARTS.map((spec) => (
          <MetricChart key={spec.title} spec={spec} app={app.data} range={range} marks={marks} now={now} />
        ))}
      </div>
      <DeployList domain={domain} marks={marks} />
    </Section>
  );
}
