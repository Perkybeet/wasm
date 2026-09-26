import { useInfiniteQuery, useQuery } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { Rocket } from "lucide-react";

import { appQuery } from "../../../api/queries/apps";
import type { Deployment } from "../../../api/queries/deployments";
import { deploymentPagesQuery } from "../../../api/queries/deployments";
import { useDocumentTitle } from "../../../app/documentTitle";
import { DeployStatePill } from "../../../components/page/AppStatePill";
import { useNow } from "../../../components/page/clock";
import { ErrorBlock } from "../../../components/page/QueryState";
import { RelativeTime } from "../../../components/page/RelativeTime";
import { Section } from "../../../components/page/Section";
import { Button } from "../../../components/ui/Button";
import { Badge } from "../../../components/ui/Badge";
import { DataTable } from "../../../components/ui/DataTable";
import type { Column } from "../../../components/ui/DataTable";
import { EmptyState } from "../../../components/ui/EmptyState";
import { formatCount, formatDuration, parseTimestamp } from "../../../lib/format";
import { ReleasesSection } from "./ReleasesSection";
import { shortCommit, triggerWords } from "./words";

/** Rows per page. The store keeps the last twenty deploys of an app. */
export const DEPLOYMENTS_PAGE = 10;

const RUNNING = new Set(["queued", "running"]);

/** How long a deploy took, or has been running for. */
function Duration({ deployment }: { deployment: Deployment }) {
  const running = RUNNING.has(deployment.status);
  const started = parseTimestamp(deployment.started_at);
  const now = useNow(() => (running ? 1_000 : 3_600_000));
  if (running) {
    if (started === null) return <span className="text-fg-faint">-</span>;
    return <span className="text-fg-muted">{formatDuration(Math.max(0, (now - started.getTime()) / 1000))}</span>;
  }
  if (deployment.duration_s === null || deployment.duration_s === undefined) return <span className="text-fg-faint">-</span>;
  return <>{formatDuration(deployment.duration_s)}</>;
}

function columns(domain: string): Column<Deployment>[] {
  return [
    {
      id: "id",
      header: "Deploy",
      width: "w-20",
      cell: (row) => (
        <Link
          to="/apps/$domain/deployments/$id"
          params={{ domain, id: String(row.id) }}
          className="mono -mx-1 inline-flex h-7 items-center rounded-[4px] px-1 text-12 font-medium text-accent-fg hover:underline hover:underline-offset-2 focus-visible:outline-2 focus-visible:outline-focus"
        >
          <span className="sr-only">{`Deployment ${String(row.id)}`}</span>
          <span aria-hidden="true">{`#${String(row.id)}`}</span>
        </Link>
      ),
    },
    {
      id: "status",
      header: "Status",
      width: "w-36",
      cell: (row) => <DeployStatePill status={row.status} appearance="inline" size="sm" />,
    },
    {
      id: "commit",
      header: "Commit",
      cell: (row) => {
        const commit = shortCommit(row.git_commit);
        return (
          <div className="flex min-w-0 flex-col py-1.5 leading-4">
            <span className="flex min-w-0 items-baseline gap-2">
              {commit ? (
                <span translate="no" className="mono text-12 text-fg">
                  {commit}
                </span>
              ) : (
                <span className="text-12 text-fg-faint">No commit</span>
              )}
              {row.git_branch ? (
                <span translate="no" className="mono hidden truncate text-12 text-fg-faint sm:inline">
                  {row.git_branch}
                </span>
              ) : null}
            </span>
            {row.commit_message ? (
              <span title={row.commit_message} className="hidden max-w-[24rem] truncate text-12 text-fg-faint md:block">
                {row.commit_message}
              </span>
            ) : null}
          </div>
        );
      },
    },
    {
      id: "trigger",
      header: "Started by",
      hideBelow: "md",
      cell: (row) => {
        const words = triggerWords(row.triggered_by);
        const Icon = words.icon;
        return (
          <span className="flex items-center gap-1.5 text-fg-muted">
            <Icon aria-hidden="true" className="size-3.5 shrink-0" />
            {words.label}
          </span>
        );
      },
    },
    {
      id: "started",
      header: "Started",
      cell: (row) => <RelativeTime value={row.started_at} className="text-fg-muted" />,
    },
    {
      id: "duration",
      header: "Duration",
      align: "end",
      mono: true,
      hideBelow: "sm",
      cell: (row) => <Duration deployment={row} />,
    },
  ];
}

function History({ domain }: { domain: string }) {
  const pages = useInfiniteQuery(deploymentPagesQuery({ domain, limit: DEPLOYMENTS_PAGE }));
  const rows = pages.data?.pages.flatMap((page) => page.items) ?? [];
  const total = pages.data?.pages[0]?.total ?? 0;

  if (pages.isError && pages.data === undefined) {
    return (
      <Section title="History">
        <ErrorBlock error={pages.error} title="Could not load the deploys" onRetry={() => void pages.refetch()} retrying={pages.isRefetching} />
      </Section>
    );
  }

  return (
    <Section
      title="History"
      description="Every deploy and update of this app, newest first. Each one opens its build log."
      badge={
        pages.data ? (
          // The same count badge every section title carries.
          <Badge>
            {formatCount(total)}
            <span className="sr-only"> in total</span>
          </Badge>
        ) : undefined
      }
    >
      <DataTable
        caption={`Deploys of ${domain}, newest first`}
        columns={columns(domain)}
        rows={rows}
        getRowId={(row) => String(row.id)}
        loading={pages.isPending}
        density="compact"
        empty={
          <EmptyState
            level={3}
            icon={<Rocket />}
            title="No deploys recorded yet"
            description="Deploys and updates started from the console, the command line or a push to the repository appear here, each with its build log."
            command={`wasm update ${domain}`}
            className="py-8"
          />
        }
      />
      {pages.isError ? (
        <ErrorBlock compact live error={pages.error} title="Could not load older deploys" onRetry={() => void pages.fetchNextPage()} />
      ) : null}
      {pages.hasNextPage ? (
        <div className="flex flex-wrap items-center gap-3">
          <Button size="sm" loading={pages.isFetchingNextPage} onClick={() => void pages.fetchNextPage()}>
            Load older deploys
          </Button>
          <span className="text-12 text-fg-faint">{`Showing ${formatCount(rows.length)} of ${formatCount(total)}`}</span>
        </div>
      ) : rows.length > DEPLOYMENTS_PAGE ? (
        <p className="text-12 text-fg-faint">{`All ${formatCount(total)} deploys the history keeps are shown.`}</p>
      ) : null}
    </Section>
  );
}

/**
 * Every deploy of one app, and what it can go back to: its releases, switched in seconds, or
 * for an app deployed in place, the backups a rollback restores.
 */
export function DeploymentsTab({ domain }: { domain: string }) {
  useDocumentTitle(`Deployments - ${domain}`, 1);
  const app = useQuery(appQuery(domain));

  return (
    <div className="grid min-w-0 gap-8 lg:grid-cols-[minmax(0,1fr)_20rem]">
      <History domain={domain} />
      {app.data ? <ReleasesSection domain={domain} layout={app.data.layout} /> : null}
    </div>
  );
}
