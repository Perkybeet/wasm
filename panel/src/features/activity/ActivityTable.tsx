import { ScrollText } from "lucide-react";
import type { ReactNode } from "react";

import { STATE_RANK } from "../../components/page/status";
import { RelativeTime } from "../../components/page/RelativeTime";
import { DataTable } from "../../components/ui/DataTable";
import type { Column } from "../../components/ui/DataTable";
import { IconButton } from "../../components/ui/IconButton";
import { Mono } from "../../components/ui/Mono";
import { StatusPill } from "../../components/ui/StatusPill";
import { parseTimestamp } from "../../lib/format";
import { actionWords, actorWords, detailOf, resourceOf, resultView, rowActor } from "./data";
import type { ActivityJob, ActivityRow } from "./data";

function Nothing() {
  return <span className="text-fg-faint">-</span>;
}

export interface ActivityTableProps {
  rows: readonly ActivityRow[];
  caption: string;
  loading?: boolean;
  empty?: ReactNode;
  /** Opens a job's captured log; audit rows have none, so they get no action at all. */
  onOpenJobLog: (job: ActivityJob) => void;
  className?: string;
}

/**
 * The jobs history and the audit log, merged into one timeline: what happened, who did it, to
 * what, and how it ended. Only a job row opens anything - see `./data` for the merge and the
 * words each column uses.
 */
export function ActivityTable({ rows, caption, loading = false, empty, onOpenJobLog, className }: ActivityTableProps) {
  const columns: Column<ActivityRow>[] = [
    {
      id: "result",
      header: "Result",
      width: "w-32",
      cell: (row) => {
        const view = resultView(row);
        return <StatusPill state={view.state} label={view.label} appearance="inline" size="sm" />;
      },
      sortValue: (row) => STATE_RANK[resultView(row).state],
    },
    {
      id: "time",
      header: "Time",
      width: "w-36",
      cell: (row) => <RelativeTime value={row.timestamp} />,
      sortValue: (row) => parseTimestamp(row.timestamp)?.getTime() ?? null,
    },
    {
      id: "actor",
      header: "Actor",
      width: "w-44",
      hideBelow: "sm",
      cell: (row) => {
        const words = actorWords(row);
        // A job queued before jobs carried an actor: said quietly, with no raw value to show.
        if (rowActor(row) === null) return <span className="text-fg-muted">{words.label}</span>;
        return (
          <span className="flex max-w-44 min-w-0 flex-col">
            <span className="truncate text-fg">{words.label}</span>
            <Mono tone="faint" truncate title={words.raw} className="text-12">
              {words.raw}
            </Mono>
          </span>
        );
      },
      sortValue: (row) => actorWords(row).label,
    },
    {
      id: "action",
      header: "Action",
      cell: (row) => {
        const words = actionWords(row);
        return (
          <span className="flex max-w-56 min-w-0 flex-col">
            <span className="truncate text-fg">{words.label}</span>
            <Mono tone="faint" truncate title={words.raw} className="text-12">
              {words.raw}
            </Mono>
          </span>
        );
      },
      sortValue: (row) => actionWords(row).label,
    },
    {
      id: "resource",
      header: "Resource",
      hideBelow: "md",
      cell: (row) => {
        const resource = resourceOf(row);
        return resource === null ? (
          <Nothing />
        ) : (
          <Mono truncate title={resource} className="block max-w-64">
            {resource}
          </Mono>
        );
      },
      sortValue: (row) => resourceOf(row),
    },
    {
      id: "detail",
      header: "Detail",
      hideBelow: "lg",
      cell: (row) => {
        const detail = detailOf(row);
        return detail === null ? (
          <Nothing />
        ) : (
          <span title={detail} className="block max-w-72 truncate text-fg-muted">
            {detail}
          </span>
        );
      },
    },
  ];

  return (
    <DataTable
      columns={columns}
      rows={rows}
      getRowId={(row) => row.id}
      caption={caption}
      loading={loading}
      rowActions={(row) =>
        row.kind === "job" ? (
          <IconButton label={`View log of ${row.job.name}`} icon={<ScrollText />} size="sm" onClick={() => onOpenJobLog(row.job)} />
        ) : null
      }
      {...(empty !== undefined ? { empty } : {})}
      defaultSort={{ column: "time", direction: "descending" }}
      {...(className !== undefined ? { className } : {})}
    />
  );
}
