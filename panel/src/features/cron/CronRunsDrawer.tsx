import { useQuery } from "@tanstack/react-query";

import { cronRunsQuery } from "../../api/queries/cron";
import { RelativeTime } from "../../components/page/RelativeTime";
import { ErrorBlock } from "../../components/page/QueryState";
import { Drawer } from "../../components/ui/Drawer";
import { Skeleton } from "../../components/ui/Skeleton";
import { StatusPill } from "../../components/ui/StatusPill";
import { SystemOutput } from "../../components/ui/SystemOutput";

function runView(success: boolean | null): { state: "running" | "failed" | "unknown"; label: string } {
  if (success === true) return { state: "running", label: "Succeeded" };
  if (success === false) return { state: "failed", label: "Failed" };
  return { state: "unknown", label: "Unknown" };
}

export interface CronRunsDrawerProps {
  /** The job whose runs are shown; the drawer is closed when null. */
  name: string | null;
  onOpenChange: (open: boolean) => void;
}

/** A job's recorded executions, newest first, each with its exit code and own output. */
export function CronRunsDrawer({ name, onOpenChange }: CronRunsDrawerProps) {
  const runs = useQuery({ ...cronRunsQuery(name ?? "", 20), enabled: name !== null });

  return (
    <Drawer
      open={name !== null}
      onOpenChange={onOpenChange}
      title={name !== null ? `Runs of ${name}` : "Runs"}
      description="Newest first, read from the unit's journal."
    >
      {name === null ? null : runs.isError && runs.data === undefined ? (
        <ErrorBlock error={runs.error} title="Could not load the run history" onRetry={() => void runs.refetch()} retrying={runs.isRefetching} />
      ) : runs.data === undefined ? (
        <div aria-busy="true" className="flex flex-col gap-3">
          <span className="sr-only">Loading run history</span>
          {[0, 1, 2].map((i) => (
            <Skeleton key={i} className="h-16 w-full rounded-card" />
          ))}
        </div>
      ) : runs.data.runs.length === 0 ? (
        <p className="text-13 text-fg-muted">This job has not run yet.</p>
      ) : (
        <ul className="flex flex-col gap-3">
          {runs.data.runs.map((run, index) => {
            const view = runView(run.success);
            return (
              <li key={`${run.started}-${String(index)}`} className="rounded-card border border-border bg-surface px-3 py-2.5">
                <div className="flex flex-wrap items-center justify-between gap-x-3 gap-y-1">
                  <div className="flex items-center gap-2">
                    <StatusPill state={view.state} label={view.label} appearance="inline" size="sm" />
                    <RelativeTime value={run.started} className="text-12 text-fg-muted" />
                  </div>
                  <span className="mono text-12 text-fg-faint">
                    {run.exit_code === null ? "No exit code" : `Exit ${String(run.exit_code)}`}
                  </span>
                </div>
                {run.output.trim() !== "" ? (
                  <details className="mt-2">
                    <summary className="cursor-pointer text-12 text-fg-muted hover:text-fg">Output</summary>
                    <div className="mt-1.5 rounded-control border border-border bg-bg-sunken px-2.5 py-2">
                      <SystemOutput label={`Output of this run of ${name}`}>{run.output}</SystemOutput>
                    </div>
                  </details>
                ) : null}
              </li>
            );
          })}
        </ul>
      )}
    </Drawer>
  );
}
