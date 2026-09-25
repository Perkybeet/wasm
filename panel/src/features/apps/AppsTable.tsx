import { Link } from "@tanstack/react-router";
import type { ReactNode } from "react";

import type { MetricsSnapshot } from "../../api/queries/metrics";
import { AppStatePill } from "../../components/page/AppStatePill";
import { RelativeTime } from "../../components/page/RelativeTime";
import { STATE_RANK, appStatus, deployStatus } from "../../components/page/status";
import { DataTable } from "../../components/ui/DataTable";
import type { Column } from "../../components/ui/DataTable";
import { STATUS, StatusGlyph } from "../../components/ui/StatusPill";
import { cx } from "../../lib/cx";
import { formatBytes, formatPercent, parseTimestamp } from "../../lib/format";
import type { AppInfo, Deployment } from "./data";
import { appReading, deployMoment, readsApps } from "./data";

const TONE_TEXT = { ok: "text-ok", warn: "text-warn", fail: "text-fail", idle: "text-idle" } as const;

/** A table cell with nothing to report: a dash on screen, the reason for screen readers. */
function Nothing({ reason }: { reason: string }) {
  return (
    <>
      <span aria-hidden="true" className="text-fg-faint">
        -
      </span>
      <span className="sr-only">{reason}</span>
    </>
  );
}

/** A deployment's outcome as a glyph and when it happened, for dense rows. */
export function DeployMoment({ deploy }: { deploy: Deployment }) {
  const view = deployStatus(deploy.status);
  return (
    <span className="inline-flex items-center gap-1.5">
      <StatusGlyph state={view.state} size={10} className={TONE_TEXT[STATUS[view.state].tone]} />
      <span className="sr-only">{`${view.label}, `}</span>
      <RelativeTime value={deployMoment(deploy)} className="text-fg-muted" />
    </span>
  );
}

interface Row {
  app: AppInfo;
  deploy: Deployment | undefined;
  cpu: number | null;
  memory: number | null;
}

export interface AppsTableProps {
  apps: readonly AppInfo[];
  /** The newest deploy of each domain (latestDeployByDomain). */
  deploys: ReadonlyMap<string, Deployment>;
  metrics: MetricsSnapshot | undefined;
  caption: string;
  loading?: boolean;
  /** Shown in place of rows when there are none. */
  empty?: ReactNode;
  /** A per-row menu; the list offers one, the overview does not. */
  rowActions?: (app: AppInfo) => ReactNode;
  /** `full` adds the port, for the applications list. */
  detail?: "summary" | "full";
  className?: string;
}

/**
 * Every application with its state, type, last deploy and, when the collector reads them, its
 * CPU and memory now. The domain is a link to the app, so it opens in a new tab like any
 * other link.
 */
export function AppsTable({
  apps,
  deploys,
  metrics,
  caption,
  loading = false,
  empty,
  rowActions,
  detail = "summary",
  className,
}: AppsTableProps) {
  const rows: Row[] = apps.map((app) => ({ app, deploy: deploys.get(app.domain), ...appReading(metrics, app.domain) }));
  const withReadings = readsApps(metrics);

  const columns: Column<Row>[] = [
    {
      id: "state",
      header: "State",
      width: "w-32",
      cell: (row) => <AppStatePill status={row.app.status} appearance="inline" size="sm" />,
      sortValue: (row) => STATE_RANK[appStatus(row.app.status).state],
    },
    {
      id: "domain",
      header: "Application",
      cell: (row) => (
        <Link
          to="/apps/$domain"
          params={{ domain: row.app.domain }}
          className="-mx-1 rounded-[4px] px-1 py-0.5 font-medium text-fg hover:underline hover:underline-offset-2 focus-visible:outline-2 focus-visible:outline-focus"
        >
          {row.app.domain}
        </Link>
      ),
      sortValue: (row) => row.app.domain,
    },
    {
      id: "type",
      header: "Type",
      mono: true,
      width: "w-36",
      hideBelow: "sm",
      cell: (row) => (row.app.app_type ? <span className="text-fg-muted">{row.app.app_type}</span> : <Nothing reason="Unknown type" />),
      sortValue: (row) => row.app.app_type ?? null,
    },
    ...(detail === "full"
      ? [
          {
            id: "port",
            header: "Port",
            mono: true,
            width: "w-24",
            align: "end",
            hideBelow: "md",
            cell: (row: Row) => (row.app.port ? <span className="text-fg-muted">{row.app.port}</span> : <Nothing reason="No port" />),
            sortValue: (row: Row) => row.app.port ?? null,
          } satisfies Column<Row>,
        ]
      : []),
    {
      id: "deploy",
      header: "Last deploy",
      width: "w-40",
      cell: (row) => (row.deploy ? <DeployMoment deploy={row.deploy} /> : <Nothing reason="No recent deploy" />),
      sortValue: (row) => {
        const moment = row.deploy ? parseTimestamp(deployMoment(row.deploy)) : null;
        return moment === null ? null : -moment.getTime();
      },
    },
    ...(withReadings
      ? ([
          {
            id: "cpu",
            header: "CPU",
            align: "end",
            mono: true,
            hideBelow: "md",
            cell: (row) => (row.cpu === null ? <Nothing reason="No reading" /> : formatPercent(row.cpu)),
            sortValue: (row) => row.cpu,
          },
          {
            id: "memory",
            header: "Memory",
            align: "end",
            mono: true,
            hideBelow: "md",
            cell: (row) => (row.memory === null ? <Nothing reason="No reading" /> : formatBytes(row.memory)),
            sortValue: (row) => row.memory,
          },
        ] satisfies Column<Row>[])
      : []),
  ];

  return (
    <DataTable
      columns={columns}
      rows={rows}
      getRowId={(row) => row.app.domain}
      caption={caption}
      loading={loading}
      {...(empty !== undefined ? { empty } : {})}
      {...(rowActions ? { rowActions: (row: Row) => rowActions(row.app) } : {})}
      defaultSort={{ column: "domain", direction: "ascending" }}
      className={cx(className)}
    />
  );
}
