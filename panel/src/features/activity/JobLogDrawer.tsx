import { useQuery } from "@tanstack/react-query";
import { useMemo } from "react";

import { ErrorBlock } from "../../components/page/QueryState";
import { RelativeTime } from "../../components/page/RelativeTime";
import { Drawer } from "../../components/ui/Drawer";
import { LogViewer } from "../../components/ui/LogViewer";
import type { LogLine } from "../../components/ui/LogViewer";
import { Skeleton } from "../../components/ui/Skeleton";
import { DeployStatePill } from "../../components/page/AppStatePill";
import { jobActionLabel, jobResource } from "./data";
import type { ActivityJob } from "./data";
import { jobLogQuery } from "./queries";

export interface JobLogDrawerProps {
  job: ActivityJob | null;
  onOpenChange: (open: boolean) => void;
}

function toLines(content: string): LogLine[] {
  if (content === "") return [];
  return content.split("\n").map((text, id) => ({ id, text }));
}

/**
 * One job's captured log, read once: history is a finished (or long-running) job's record, not
 * a stream to follow, so this reads `GET /api/jobs/{id}/log` rather than opening a socket.
 */
export function JobLogDrawer({ job, onOpenChange }: JobLogDrawerProps) {
  const log = useQuery({ ...jobLogQuery(job?.id ?? "", 5000), enabled: job !== null });
  const lines = useMemo(() => toLines(log.data?.content ?? ""), [log.data]);
  const resource = job ? jobResource(job) : null;

  return (
    <Drawer
      open={job !== null}
      onOpenChange={onOpenChange}
      size="lg"
      title={job ? job.name : "Job log"}
      description={
        job ? (
          <span className="flex flex-wrap items-center gap-x-2 gap-y-1">
            <DeployStatePill status={job.status} appearance="inline" size="sm" />
            <span>{jobActionLabel(job.type)}</span>
            {resource ? <span translate="no" className="mono">{resource}</span> : null}
            <RelativeTime value={job.started_at ?? job.created_at} />
          </span>
        ) : undefined
      }
    >
      {job === null ? null : log.isError && log.data === undefined ? (
        <ErrorBlock error={log.error} title="Could not load the job's log" onRetry={() => void log.refetch()} retrying={log.isRefetching} />
      ) : log.data === undefined ? (
        <div aria-busy="true">
          <span className="sr-only">Loading the log</span>
          <Skeleton className="h-80 w-full rounded-card" />
        </div>
      ) : (
        <div className="flex flex-col gap-2">
          {job.error ? (
            <ErrorBlock compact error={{ detail: job.error }} title="This job failed" />
          ) : null}
          <LogViewer lines={lines} label={`Log of ${job.name}`} filename={`${job.id}.log`} height={480} emptyMessage="This job left no captured output." />
          {log.data.truncated ? <p className="text-12 text-fg-faint">Only the last 5,000 lines are shown.</p> : null}
        </div>
      )}
    </Drawer>
  );
}
