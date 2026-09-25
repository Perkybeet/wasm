import { useQuery } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { Rocket } from "lucide-react";

import { deploymentsQuery } from "../../api/queries/deployments";
import { DeployStatePill } from "../../components/page/AppStatePill";
import { ErrorBlock } from "../../components/page/QueryState";
import { RelativeTime } from "../../components/page/RelativeTime";
import { Section } from "../../components/page/Section";
import { DataTable } from "../../components/ui/DataTable";
import type { Column } from "../../components/ui/DataTable";
import { EmptyState } from "../../components/ui/EmptyState";
import { formatDuration } from "../../lib/format";
import type { Deployment } from "../apps/data";
import { deployMoment } from "../apps/data";

/** How many deploys the overview shows; the rest are on each app's Deployments tab. */
export const RECENT_COUNT = 8;

const LINK = "rounded-[4px] hover:underline hover:underline-offset-2 focus-visible:outline-2 focus-visible:outline-focus";

const TRIGGERS: Record<string, string> = { panel: "Console", cli: "CLI", webhook: "Webhook" };

const COLUMNS: readonly Column<Deployment>[] = [
  {
    id: "status",
    header: "Status",
    width: "w-32",
    cell: (row) => <DeployStatePill status={row.status} appearance="inline" size="sm" />,
  },
  {
    id: "app",
    header: "Application",
    cell: (row) => (
      <Link to="/apps/$domain" params={{ domain: row.domain }} className={`${LINK} font-medium text-fg`}>
        {row.domain}
      </Link>
    ),
  },
  {
    id: "commit",
    header: "Commit",
    mono: true,
    cell: (row) => (
      <Link
        to="/apps/$domain/deployments/$id"
        params={{ domain: row.domain, id: String(row.id) }}
        className={`${LINK} text-fg-muted`}
      >
        <span className="text-fg">{row.git_commit?.slice(0, 7) ?? `#${String(row.id)}`}</span>
        {row.git_branch ? <span className="text-fg-faint">{` ${row.git_branch}`}</span> : null}
        <span className="sr-only">{`, deploy ${String(row.id)} of ${row.domain}`}</span>
      </Link>
    ),
  },
  {
    id: "trigger",
    header: "Trigger",
    hideBelow: "md",
    cell: (row) => <span className="text-fg-muted">{TRIGGERS[row.triggered_by] ?? row.triggered_by}</span>,
  },
  {
    id: "when",
    header: "When",
    cell: (row) => <RelativeTime value={deployMoment(row)} className="text-fg-muted" />,
  },
  {
    id: "duration",
    header: "Duration",
    align: "end",
    mono: true,
    hideBelow: "sm",
    cell: (row) =>
      row.duration_s === null || row.duration_s === undefined ? (
        <span className="text-fg-faint">{row.status === "running" ? "Running" : "-"}</span>
      ) : (
        <span className="text-fg-muted">{formatDuration(row.duration_s)}</span>
      ),
  },
];

/**
 * The latest deploys across every app. Kept live by the `job` events: a deploy starting or
 * ending refreshes the history, so a new row appears and a running one settles by itself.
 */
export function RecentDeployments() {
  const deploys = useQuery(deploymentsQuery({ limit: RECENT_COUNT }));
  return (
    <Section
      title="Recent deployments"
      actions={
        <Link to="/activity" className={`${LINK} text-13 font-medium text-accent-fg`}>
          All activity
        </Link>
      }
    >
      {deploys.isError && deploys.data === undefined ? (
        <ErrorBlock error={deploys.error} title="Could not load recent deployments" onRetry={() => void deploys.refetch()} retrying={deploys.isRefetching} />
      ) : (
        <DataTable
          columns={COLUMNS}
          rows={deploys.data?.items ?? []}
          getRowId={(row) => String(row.id)}
          caption="Recent deployments, newest first"
          loading={deploys.isPending}
          density="compact"
          empty={
            <EmptyState
              icon={<Rocket />}
              title="No deploys yet"
              description="Every deploy and update of every app is listed here as it happens."
              className="border-0 py-8"
            />
          }
        />
      )}
    </Section>
  );
}
