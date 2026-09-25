import { useQuery } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { Boxes, Plus } from "lucide-react";
import { useMemo } from "react";

import { appsQuery } from "../../api/queries/apps";
import type { MetricWindow } from "../../api/queries/metrics";
import { PageHeader } from "../../app/PageHeader";
import { ErrorBlock } from "../../components/page/QueryState";
import { Section, Sections } from "../../components/page/Section";
import { Badge } from "../../components/ui/Badge";
import { buttonClassName } from "../../components/ui/Button";
import { EmptyState } from "../../components/ui/EmptyState";
import { AppsTable } from "../apps/AppsTable";
import { latestDeployByDomain, recentDeploysQuery, useLatestMetrics } from "../apps/data";
import { useStateTransitions } from "../apps/useStateTransitions";
import { MachineCharts } from "./MachineCharts";
import { NeedsAttention } from "./NeedsAttention";
import { RecentDeployments } from "./RecentDeployments";

function NewAppLink() {
  return (
    <Link to="/apps/new" className={buttonClassName("primary")}>
      <Plus aria-hidden="true" />
      New application
    </Link>
  );
}

function Applications() {
  const apps = useQuery(appsQuery());
  const deploys = useQuery(recentDeploysQuery());
  const metrics = useLatestMetrics();
  const latest = useMemo(() => latestDeployByDomain(deploys.data?.items ?? []), [deploys.data]);
  useStateTransitions(apps.data?.apps);

  const total = apps.data?.total;
  return (
    <Section
      title="Applications"
      badge={total !== undefined && total > 0 ? <Badge>{total}</Badge> : undefined}
      actions={
        <Link
          to="/apps"
          className="rounded-[4px] text-13 font-medium text-accent-fg hover:underline hover:underline-offset-2 focus-visible:outline-2 focus-visible:outline-focus"
        >
          Search and filter
        </Link>
      }
    >
      {apps.isError && apps.data === undefined ? (
        <ErrorBlock error={apps.error} title="Could not load applications" onRetry={() => void apps.refetch()} retrying={apps.isRefetching} />
      ) : (
        <AppsTable
          apps={apps.data?.apps ?? []}
          deploys={latest}
          metrics={metrics}
          caption="Applications on this machine"
          loading={apps.isPending}
          empty={
            <EmptyState
              icon={<Boxes />}
              title="Deploy your first application"
              description="Point WASM at a Git repository or a directory: it detects the stack, builds it and serves it with a certificate."
              action={<NewAppLink />}
              command="wasm create -d example.com -s https://github.com/you/app"
              className="border-0 py-10"
            />
          }
        />
      )}
    </Section>
  );
}

export interface OverviewPageProps {
  window: MetricWindow;
  onWindowChange: (window: MetricWindow) => void;
}

/**
 * The first page: what needs attention on this machine, then its recent history, every
 * application with its state, and the latest deploys.
 */
export function OverviewPage({ window, onWindowChange }: OverviewPageProps) {
  return (
    <>
      <PageHeader
        title="Overview"
        description="The state of this machine and everything deployed on it."
        actions={<NewAppLink />}
      />
      <Sections>
        <NeedsAttention />
        <MachineCharts window={window} onWindowChange={onWindowChange} />
        <Applications />
        <RecentDeployments />
      </Sections>
    </>
  );
}
