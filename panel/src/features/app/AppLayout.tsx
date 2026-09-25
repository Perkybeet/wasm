import { useQuery } from "@tanstack/react-query";
import { Link, Outlet } from "@tanstack/react-router";
import { Boxes, ExternalLink, X } from "lucide-react";

import { isApiError } from "../../api/client";
import { appQuery } from "../../api/queries/apps";
import type { App } from "../../api/queries/apps";
import { certsQuery } from "../../api/queries/certs";
import { LinkTabs } from "../../app/LinkTabs";
import { APP_TABS } from "../../app/nav";
import { PageHeader } from "../../app/PageHeader";
import { ErrorBlock } from "../../components/page/QueryState";
import { appStatus } from "../../components/page/status";
import { useAnnounceChange } from "../../components/page/useAnnounceChange";
import { buttonClassName } from "../../components/ui/Button";
import { EmptyState } from "../../components/ui/EmptyState";
import { IconButton } from "../../components/ui/IconButton";
import { Skeleton } from "../../components/ui/Skeleton";
import { Spinner } from "../../components/ui/Spinner";
import { StatusPill } from "../../components/ui/StatusPill";
import type { StatusView } from "../../components/page/status";
import { AppActions } from "./AppActions";
import { findCertificate } from "./queries";
import { jobStep, jobWords, useAppJob } from "./useAppJob";
import type { AppJob } from "./useAppJob";

const BREADCRUMBS = [{ label: "Applications", to: "/apps" }] as const;

/**
 * Where the app answers: HTTPS unless the certificate list shows none covers it (while the
 * list loads, HTTPS, which is how WASM deploys by default).
 */
function liveUrl(domain: string, hasCertificate: boolean): string {
  return `${hasCertificate ? "https" : "http"}://${domain}`;
}

/** The header's facts line: state, type, port, and the live site. Phrasing content only: it sits in a paragraph. */
function Facts({ app, view, hasCertificate }: { app: App; view: StatusView; hasCertificate: boolean }) {
  const url = liveUrl(app.domain, hasCertificate);
  return (
    <span className="flex flex-wrap items-center gap-x-3 gap-y-1.5">
      <StatusPill state={view.state} label={view.label} />
      {app.app_type ? (
        <span translate="no" className="mono text-12 text-fg-muted">
          {app.app_type}
        </span>
      ) : null}
      {app.port ? (
        <span className="text-13 text-fg-muted">
          Port{" "}
          <span translate="no" className="mono text-12 text-fg">
            {app.port}
          </span>
        </span>
      ) : null}
      <a
        href={url}
        target="_blank"
        rel="noreferrer"
        className="inline-flex items-center gap-1 rounded-[4px] text-13 font-medium text-accent-fg hover:underline hover:underline-offset-2 focus-visible:outline-2 focus-visible:outline-focus"
      >
        <span translate="no">{url.replace(/^https?:\/\//, "")}</span>
        <ExternalLink aria-hidden="true" className="size-3.5" />
        <span className="sr-only"> (opens the live site in a new tab)</span>
      </a>
    </span>
  );
}

function FactsSkeleton() {
  return (
    <span aria-hidden="true" className="flex items-center gap-3">
      <Skeleton className="h-6 w-20 rounded-pill" />
      <Skeleton className="h-3 w-14" />
      <Skeleton className="h-3 w-28" />
    </span>
  );
}

/** What is being done to the app, under its header: the running job's step, or how the last one failed. */
function JobStatus({ job, domain }: { job: AppJob; domain: string }) {
  if (job.running) {
    const words = jobWords(job.running.type);
    const step = jobStep(job.running);
    return (
      <div className="flex min-w-0 items-center gap-2.5 rounded-control border border-border bg-surface px-3 py-2 text-13 shadow-raised">
        <Spinner size={14} className="text-warn" />
        <span className="shrink-0 font-medium text-fg">{`${words.running} ${domain}`}</span>
        {step !== null ? (
          <code translate="no" className="min-w-0 truncate text-12 text-fg-muted" title={step}>
            {step}
          </code>
        ) : null}
      </div>
    );
  }
  if (job.failed) {
    const words = jobWords(job.failed.type);
    return (
      <div className="relative">
        {/* Not live: the job's failure is already announced by its notice toast. */}
        <ErrorBlock
          error={{ detail: job.failed.error ?? "The job failed without saying why. Its log is on the Activity page." }}
          title={`${words.noun} of ${domain} failed`}
          className="pr-12"
        />
        <IconButton label="Dismiss" icon={<X />} size="sm" onClick={job.dismiss} className="absolute top-2.5 right-2.5" />
      </div>
    );
  }
  return null;
}

function NotFound({ domain }: { domain: string }) {
  return (
    <>
      <PageHeader title={domain} breadcrumbs={BREADCRUMBS} />
      <EmptyState
        level={2}
        icon={<Boxes />}
        title="No application at this domain"
        description="It may have been deleted, or the address has a typo. Every app on this machine is in the applications list."
        action={
          <Link to="/apps" className={buttonClassName("secondary")}>
            All applications
          </Link>
        }
        command="wasm list"
        className="py-16"
      />
    </>
  );
}

/**
 * One application: a header with its state, its live address and its actions, the job being
 * run on it, and its sections as tabs whose state is the URL.
 */
export function AppLayout({ domain }: { domain: string }) {
  const app = useQuery(appQuery(domain));
  const certs = useQuery(certsQuery());
  const cert = findCertificate(certs.data, domain);
  const job = useAppJob(domain);

  const base = appStatus(app.data?.status);
  // A job running on the app is its state, whatever systemd says about the unit meanwhile.
  const view: StatusView = job.running ? { state: "deploying", label: jobWords(job.running.type).running, attention: false } : base;

  useAnnounceChange(
    app.data ? view.label : null,
    `${domain}: ${view.label}`,
    view.state === "failed" ? "assertive" : "polite",
  );

  if (app.isError && isApiError(app.error) && app.error.status === 404) return <NotFound domain={domain} />;

  return (
    <>
      <PageHeader
        title={domain}
        breadcrumbs={BREADCRUMBS}
        description={
          app.data ? <Facts app={app.data} view={view} hasCertificate={cert !== null} /> : <FactsSkeleton />
        }
        actions={app.data ? <AppActions app={app.data} busy={job.running !== null} onJobQueued={job.track} /> : undefined}
      />
      <div className="-mt-4 mb-8 flex flex-col gap-4">
        {app.isError && app.data === undefined ? (
          <ErrorBlock error={app.error} title={`Could not load ${domain}`} onRetry={() => void app.refetch()} retrying={app.isRefetching} />
        ) : null}
        <JobStatus job={job} domain={domain} />
        <LinkTabs label="Application sections" tabs={APP_TABS.map((tab) => ({ ...tab, params: { domain } }))} />
      </div>
      <Outlet />
    </>
  );
}
