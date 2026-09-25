import type { ReactNode } from "react";

import { DeployStatePill } from "../../components/page/AppStatePill";
import { RelativeTime } from "../../components/page/RelativeTime";
import { STATE_RANK, deployStatus } from "../../components/page/status";
import { DataTable } from "../../components/ui/DataTable";
import type { Column } from "../../components/ui/DataTable";
import { formatDuration, parseTimestamp } from "../../lib/format";
import { actionLabel, jobResource } from "./data";
import type { ActivityJob } from "./data";

function Nothing() {
  return <span className="text-fg-faint">-</span>;
}

/** Seconds between two timestamps the backend sent, or null while either is missing. */
function durationSeconds(job: ActivityJob): number | null {
  const start = parseTimestamp(job.started_at ?? job.created_at);
  const end = parseTimestamp(job.completed_at);
  if (start === null || end === null) return null;
  return Math.max(0, (end.getTime() - start.getTime()) / 1000);
}

export interface ActivityTableProps {
  jobs: readonly ActivityJob[];
  caption: string;
  loading?: boolean;
  empty?: ReactNode;
  onRowActivate: (job: ActivityJob) => void;
  className?: string;
}

/**
 * The jobs history as one timeline: what ran, on what, when, how it ended. A row opens its
 * log. See `./data` for why there is no actor column - jobs carry none, and there is no
 * separate audit log to merge in today.
 */
export function ActivityTable({ jobs, caption, loading = false, empty, onRowActivate, className }: ActivityTableProps) {
  const columns: Column<ActivityJob>[] = [
    {
      id: "result",
      header: "Result",
      width: "w-32",
      cell: (row) => <DeployStatePill status={row.status} appearance="inline" size="sm" />,
      sortValue: (row) => STATE_RANK[deployStatus(row.status).state],
    },
    {
      id: "action",
      header: "Action",
      width: "w-40",
      cell: (row) => actionLabel(row.type),
      sortValue: (row) => actionLabel(row.type),
    },
    {
      id: "resource",
      header: "Resource",
      mono: true,
      cell: (row) => {
        const resource = jobResource(row);
        return resource ?? <Nothing />;
      },
      sortValue: (row) => jobResource(row),
    },
    {
      id: "description",
      header: "Job",
      hideBelow: "md",
      cell: (row) => (
        <span title={row.description} className="block max-w-96 truncate text-fg-muted">
          {row.description || row.name}
        </span>
      ),
    },
    {
      id: "started",
      header: "Started",
      width: "w-36",
      cell: (row) => <RelativeTime value={row.started_at ?? row.created_at} />,
      sortValue: (row) => parseTimestamp(row.started_at ?? row.created_at)?.getTime() ?? null,
    },
    {
      id: "duration",
      header: "Duration",
      align: "end",
      mono: true,
      width: "w-24",
      hideBelow: "sm",
      cell: (row) => {
        const seconds = durationSeconds(row);
        return seconds === null ? <Nothing /> : formatDuration(seconds);
      },
      sortValue: (row) => durationSeconds(row),
    },
  ];

  return (
    <DataTable
      columns={columns}
      rows={jobs}
      getRowId={(row) => row.id}
      caption={caption}
      loading={loading}
      onRowActivate={onRowActivate}
      {...(empty !== undefined ? { empty } : {})}
      defaultSort={{ column: "started", direction: "descending" }}
      {...(className !== undefined ? { className } : {})}
    />
  );
}
