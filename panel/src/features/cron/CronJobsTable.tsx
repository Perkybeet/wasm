import type { ReactNode } from "react";

import { RelativeTime } from "../../components/page/RelativeTime";
import { STATE_RANK } from "../../components/page/status";
import { DataTable } from "../../components/ui/DataTable";
import type { Column } from "../../components/ui/DataTable";
import { Mono } from "../../components/ui/Mono";
import { StatusPill } from "../../components/ui/StatusPill";
import { parseTimestamp } from "../../lib/format";
import type { CronJob } from "./data";
import { runStatus } from "./data";

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

export interface CronJobsTableProps {
  jobs: readonly CronJob[];
  caption: string;
  loading?: boolean;
  empty?: ReactNode;
  onRowActivate?: (job: CronJob) => void;
  rowActions?: (job: CronJob) => ReactNode;
  className?: string;
}

/** Every cron job: its schedule in words and as written, its next run and its last result. */
export function CronJobsTable({ jobs, caption, loading = false, empty, onRowActivate, rowActions, className }: CronJobsTableProps) {
  const columns: Column<CronJob>[] = [
    {
      id: "enabled",
      header: "State",
      width: "w-24",
      cell: (row) => <StatusPill state={row.enabled ? "running" : "stopped"} label={row.enabled ? "Enabled" : "Disabled"} appearance="inline" size="sm" />,
      sortValue: (row) => (row.enabled ? 0 : 1),
    },
    {
      id: "name",
      header: "Job",
      cell: (row) => (
        <span translate="no" className="mono font-medium text-fg">
          {row.name}
        </span>
      ),
      sortValue: (row) => row.name,
    },
    {
      id: "schedule",
      header: "Schedule",
      hideBelow: "sm",
      cell: (row) => (
        <span className="flex flex-col">
          <span className="text-fg-muted capitalize">{row.schedule}</span>
          <Mono tone="faint" truncate className="text-12">
            {row.on_calendar}
          </Mono>
        </span>
      ),
      sortValue: (row) => row.schedule,
    },
    {
      id: "next_run",
      header: "Next run",
      width: "w-40",
      hideBelow: "md",
      cell: (row) =>
        row.enabled ? <RelativeTime value={row.next_run} fallback={row.next_run} /> : <Nothing reason="Disabled" />,
      sortValue: (row) => parseTimestamp(row.next_run)?.getTime() ?? null,
    },
    {
      id: "last_result",
      header: "Last result",
      width: "w-36",
      cell: (row) => {
        const view = runStatus(row.last_result);
        return <StatusPill state={view.state} label={view.label} appearance="inline" size="sm" />;
      },
      sortValue: (row) => STATE_RANK[runStatus(row.last_result).state],
    },
  ];

  return (
    <DataTable
      columns={columns}
      rows={jobs}
      getRowId={(row) => row.name}
      caption={caption}
      loading={loading}
      {...(empty !== undefined ? { empty } : {})}
      {...(onRowActivate ? { onRowActivate } : {})}
      {...(rowActions ? { rowActions } : {})}
      defaultSort={{ column: "name", direction: "ascending" }}
      {...(className !== undefined ? { className } : {})}
    />
  );
}
